"""KOFAMSCAN annotation utilities for KBUtilLib.

Implements ``KofamscanUtils``, which wraps KofamScan's ``exec_annotation``
(https://github.com/takaram/kofam_scan) to annotate a caller-supplied set of
amino-acid (protein) sequences with KEGG Orthology (KO) assignments against a
locally registered KOfam profile set.

Profile set
-----------
The profile-set root directory and the selected set name are explicit
constructor arguments (``KofamscanUtils(profiles_path=..., profile_set=...)``)
resolved per-request by the caller — NOT config keys, mirroring
``BaktaUtils(db_path=...)``. A registered KOfam root can hold several
complete ``profiles/<name>/`` + ``profiles/<name>.txt`` pairs (e.g. a
production set and a small guard set); *profile_set* selects between them.

Docker/native dispatch
-----------------------
Follows the existing KBUtilLib convention: config key
``kofamscan.docker_image`` (empty => native ``exec_annotation`` binary,
default path ``/opt/kofam_scan/exec_annotation``, overridable via
``kofamscan.executable``) and ``kofamscan.docker_workdir`` for the tempdir
base. Docker containers run with ``--user <uid>:<gid>``, ``--network none``,
and the profile-set root bind-mounted read-only.

Output format and the mandatory significance filter
-----------------------------------------------------
Uses ``-f detail-tsv``, not ``-f mapper``. ``mapper`` only returns KO ids;
``detail-tsv`` additionally returns a significance flag, ``thrshld``,
``score`` and ``E-value``, which populate ``Term.evidence`` with no second
lookup. Critically, ``detail-tsv`` reports *every* HMM hit, including
below-threshold ones — filtering to rows carrying the ``*`` significance
flag is mandatory, not an optimisation: on a real corpus this is the
difference between ~130k rows and ~4.7k calls, roughly a 5x inflation if
unfiltered. ``_parse_kofam_detail_tsv`` performs this filter internally.

The multimap
------------
A protein with several significant KO hits produces several consecutive
rows. ``_build_kofam_records`` emits a ``Term`` pair per hit — every hit,
not just the last (a documented upstream-parser bug this module does not
reproduce).

The bridge
----------
KofamScan's primary product is the bare KO id, which does not join the
downstream reaction-mapping table on its own (measured: 0% — the mapping
table's descriptions follow a ``"symbol; definition"`` convention that a
bare KO-list definition can never reproduce). ``KofamscanUtils`` therefore
emits **two** ``Term``s per significant hit, in a stable but semantically
unordered pair: first the raw KO id, then — when bridgeable — the composed
``"symbol; definition"`` text that *does* join the mapping table. When no
bridge is available the pair degrades to its first element (the KO id
alone); this is never treated as an error.

``_bridge_ko`` is a **pure lookup function** over an in-memory table — no
I/O, no string normalisation at runtime. The table itself (a precomputed
``ko_id -> composed_string`` mapping) is produced by a separate,
sibling-task build script and loaded from the path named by the config key
``kofamscan.ko_function_map``. An absent table (unset config key, or a
missing file) is **not a failure**: the module logs a warning, degrades
every pair to its KO id, and records ``bridged_fraction = 0.0`` plus a
``ko_function_map: absent`` marker in ``AnnotationResult.parameters`` so the
degraded state is a visible number rather than a silent loss of coverage.

``AnnotationResult.parameters`` also records three versions on every run:
the profile set name, its paired ``ko_list`` version (the profile-set
directory name itself, per KofamScan's on-disk versioning convention — see
``BaktaUtils`` for the analogous ``db/version.json`` case), and the KEGG
``ko`` release the symbol table was built from (read from the table's own
header when present).
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .annotator_utils import (
    AnnotationRecord,
    AnnotationResult,
    AnnotatorUtils,
    Term,
    _guard_protein,
)

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOOL = "kofamscan"
_INSTALL_HINT = (
    "Build the kbutillib/kofamscan Docker image (docker/kofamscan/Dockerfile) "
    "and set kofamscan.docker_image in config.yaml, or install exec_annotation "
    "(default path /opt/kofam_scan/exec_annotation; override via "
    "kofamscan.executable). Requires a registered KOfam profile set with its "
    "paired ko_list (profiles/<name>/ + profiles/<name>.txt)."
)

# Significance flag KofamScan's detail-tsv marks a call with; any other value
# (typically an empty string) in that column is a below-threshold hit and
# must be filtered out.
_SIGNIFICANT_FLAG = "*"


# ---------------------------------------------------------------------------
# Pure parse helpers (offline-unit-testable)
# ---------------------------------------------------------------------------


def _parse_kofam_detail_tsv(text: str) -> list[dict[str, str]]:
    """Parse KofamScan ``-f detail-tsv`` output, filtered to significant hits.

    ``detail-tsv`` rows are tab-separated:
    ``<sig_flag>\\t<gene name>\\t<KO>\\t<thrshld>\\t<score>\\t<E-value>\\t<KO
    definition>``, where ``sig_flag`` is ``"*"`` for a call and blank
    otherwise. This is a **multimap**: a gene with several hits produces
    several consecutive rows, all of which are retained (subject to the
    significance filter) — no last-hit-wins collapsing.

    Args:
        text: Full text of the ``detail-tsv`` output file.

    Returns:
        List of row dicts (``gene_id``, ``ko_id``, ``thrshld``, ``score``,
        ``evalue``, ``definition``) for rows carrying the ``*`` significance
        flag only. Below-threshold rows (blank flag) are excluded — this is
        mandatory, not an optimisation (see module docstring).
    """
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        if parts[0].strip() != _SIGNIFICANT_FLAG:
            continue
        gene_id = parts[1].strip()
        ko_id = parts[2].strip()
        if not gene_id or not ko_id:
            continue
        definition = "\t".join(parts[6:]).strip() if len(parts) > 6 else ""
        rows.append(
            {
                "gene_id": gene_id,
                "ko_id": ko_id,
                "thrshld": parts[3].strip(),
                "score": parts[4].strip(),
                "evalue": parts[5].strip(),
                "definition": definition,
            }
        )
    return rows


def _bridge_ko(ko_id: str, table: dict[str, str]) -> str | None:
    """Pure lookup: the composed ``"symbol; definition"`` bridge text for *ko_id*.

    No I/O, no string normalisation — the composition (KEGG symbol + the
    ``ko_list`` definition, trailing ``[EC:...]`` stripped) already happened
    when *table* was built. This function is intentionally nothing more than
    a dict lookup, which is what makes it exhaustively unit-testable without
    any generated artifact: callers inject *table* directly.

    Args:
        ko_id: A KEGG Orthology id (e.g. ``"K00003"``).
        table: The loaded ``{ko_id: composed_string}`` mapping.

    Returns:
        The composed bridge string, or ``None`` when *ko_id* has no entry
        (a non-bridging KO — not an error; the caller degrades the D5 pair
        to its first element).
    """
    return table.get(ko_id)


def _build_kofam_records(
    rows: list[dict[str, str]],
    table: dict[str, str] | None,
    input_ids: set[str] | None = None,
) -> tuple[list[AnnotationRecord], float]:
    """Build AnnotationRecords (and the run's bridged fraction) from detail-tsv rows.

    Emits, per significant hit, a stable-ordered pair of Terms — first the
    raw KO id (``Term(namespace="KO", id=ko_id, value=ko_id)``), then, when
    *table* bridges it, the composed text (``Term(namespace=None, id=None,
    value=<bridge text>)``). A gene with N significant hits yields terms for
    all N hits (the explicit multimap regression this module guards
    against). ``Term.evidence`` on both Terms of a pair carries the hit's
    ``thrshld``, ``score`` and ``evalue``.

    Args:
        rows: Significant-only rows as returned by
            :func:`_parse_kofam_detail_tsv`.
        table: The loaded ``{ko_id: composed_string}`` bridge table, or
            ``None`` when the table is absent (every pair degrades to its
            KO id alone).
        input_ids: When given, only rows whose ``gene_id`` is a member of
            this set are retained.

    Returns:
        Tuple of (list of ``AnnotationRecord``, ``bridged_fraction``).
        ``bridged_fraction`` is ``bridged_distinct_KOs / total_distinct_KOs``
        across the whole run, or exactly ``0.0`` when *table* is ``None``
        (an absent table is never treated as "0 of 0 bridged").
    """
    per_gene: dict[str, list[Term]] = {}
    distinct_kos: set[str] = set()
    bridged_kos: set[str] = set()

    for row in rows:
        gene_id = row["gene_id"]
        if input_ids is not None and gene_id not in input_ids:
            continue
        ko_id = row["ko_id"]
        distinct_kos.add(ko_id)

        evidence: dict[str, Any] = {
            "thrshld": row.get("thrshld"),
            "score": row.get("score"),
            "evalue": row.get("evalue"),
        }

        terms = per_gene.setdefault(gene_id, [])
        terms.append(
            Term(namespace="KO", id=ko_id, value=ko_id, evidence=dict(evidence))
        )

        bridged_text = _bridge_ko(ko_id, table) if table is not None else None
        if bridged_text:
            bridged_kos.add(ko_id)
            terms.append(
                Term(
                    namespace=None,
                    id=None,
                    value=bridged_text,
                    evidence=dict(evidence),
                )
            )

    records = [
        AnnotationRecord(gene_id=gene_id, terms=terms)
        for gene_id, terms in per_gene.items()
        if terms
    ]

    if table is None:
        bridged_fraction = 0.0
    else:
        bridged_fraction = (
            len(bridged_kos) / len(distinct_kos) if distinct_kos else 0.0
        )

    return records, bridged_fraction


def _parse_ko_function_map_text(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Parse a ``ko_function_map.tsv`` payload into ``(table, metadata)``.

    Format: optional leading ``# key: value`` comment lines carrying
    provenance metadata (KEGG release, ``ko_list`` version, generation
    date), followed by tab-separated data rows: ``ko_id\\tcomposed_string``.

    Args:
        text: Full text of the ``ko_function_map.tsv`` file.

    Returns:
        Tuple of (``{ko_id: composed_string}``, ``{metadata_key: value}``).
        Malformed data rows (not exactly 2 tab-separated fields, or an empty
        id/value) are silently skipped.
    """
    table: dict[str, str] = {}
    metadata: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            body = line[1:].strip()
            if ":" in body:
                key, _, value = body.partition(":")
                key = key.strip()
                value = value.strip()
                if key:
                    metadata[key] = value
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        ko_id, composed = parts[0].strip(), parts[1].strip()
        if ko_id and composed:
            table[ko_id] = composed
    return table, metadata


def _load_ko_function_map(
    path: str,
) -> tuple[dict[str, str], dict[str, str]] | None:
    """Load the ``ko_function_map.tsv`` bridge table from disk.

    Args:
        path: Filesystem path to the generated table (from the
            ``kofamscan.ko_function_map`` config key). An empty string
            means the config key is unset.

    Returns:
        ``(table, metadata)`` tuple, or ``None`` when *path* is empty or the
        file cannot be read — the "absent table" case, which is a reported
        degradation, not a failure (see module docstring).
    """
    if not path:
        return None
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_ko_function_map_text(text)


# ---------------------------------------------------------------------------
# KofamscanUtils class
# ---------------------------------------------------------------------------


class KofamscanUtils(AnnotatorUtils):
    """Annotate proteins with KO assignments using KofamScan's ``exec_annotation``.

    Config keys (read via ``get_config_value``):
        ``kofamscan.docker_image`` — Docker image tag to run
            ``exec_annotation`` in. Empty (the default) => run the native
            binary on the host.
        ``kofamscan.executable`` — native executable path (default
            ``"/opt/kofam_scan/exec_annotation"``). Ignored when
            ``kofamscan.docker_image`` is set.
        ``kofamscan.docker_workdir`` — base dir for the per-run bind-mounted
            work dir (default ``~/.kbutillib/kofamscan_work``).
        ``kofamscan.ko_function_map`` — path to the generated
            ``ko_function_map.tsv`` bridge table. Unset/missing degrades
            gracefully (see module docstring); it is never a hard failure.

    Example::

        ku = KofamscanUtils(
            profiles_path="/scratch/kbdl/refdata/kofam-2025-11-03",
            profile_set="2025-11-03",
        )
        if ku.is_available():
            result = ku.annotate({"gene1": "MKTAYIAKQ...", "gene2": "MNFSTPD..."})
            for rec in result.records:
                print(rec.gene_id, [(t.namespace, t.value) for t in rec.terms])

    Raises:
        ValueError: If the input contains nucleotide-looking sequences.
        ToolUnavailableError: If the Docker image / native binary is absent.
    """

    _tool_name = _TOOL
    _install_hint = _INSTALL_HINT

    def __init__(
        self, profiles_path: str, profile_set: str, **kwargs: Any
    ) -> None:
        """Initialize KofamscanUtils against a specific registered profile set.

        Args:
            profiles_path: Root directory holding ``profiles/<name>/`` +
                ``profiles/<name>.txt`` pairs. Resolved per-request by the
                caller — a constructor argument, not a config key.
            profile_set: The bare profile-set name to run (e.g.
                ``"2025-11-03"``), selecting
                ``<profiles_path>/profiles/<profile_set>`` and its paired
                ``<profiles_path>/profiles/<profile_set>.txt``.
            **kwargs: Forwarded to ``AnnotatorUtils.__init__``.
        """
        super().__init__(**kwargs)
        self.profiles_path = profiles_path
        self.profile_set = profile_set
        self._kofamscan_exe: str = self.get_config_value(
            "kofamscan.executable", default="/opt/kofam_scan/exec_annotation"
        )
        #: When set, exec_annotation runs inside this Docker image instead of
        #: as a local executable. Empty string => native executable mode.
        self._docker_image: str = (
            self.get_config_value("kofamscan.docker_image", default="") or ""
        )
        self._ko_function_map_path: str = (
            self.get_config_value("kofamscan.ko_function_map", default="") or ""
        )

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if exec_annotation can run, side-effect-free.

        In Docker mode (``kofamscan.docker_image`` set) this checks that the
        image is present locally (``docker image inspect``). Otherwise it
        checks that the configured executable resolves — absolute paths are
        checked for existence + the executable bit; bare names are resolved
        via ``shutil.which``.

        Returns:
            True if exec_annotation is runnable via the active mode.
        """
        if self._docker_image:
            return self._docker_image_present()
        exe = Path(self._kofamscan_exe)
        if exe.is_absolute():
            return exe.is_file() and os.access(exe, os.X_OK)
        return shutil.which(self._kofamscan_exe) is not None

    def _docker_workdir_base(self) -> str | None:
        """Base dir for the per-run work dir (``tempfile.TemporaryDirectory``).

        Returns None in native mode. In Docker mode the work dir is
        bind-mounted into the container (see ``ProkkaUtils`` for the
        macOS/Docker-Desktop caveat this avoids). Override with
        ``kofamscan.docker_workdir``.
        """
        if not self._docker_image:
            return None
        base = (
            self.get_config_value("kofamscan.docker_workdir", default="")
            or str(Path.home() / ".kbutillib" / "kofamscan_work")
        )
        Path(base).mkdir(parents=True, exist_ok=True)
        return base

    def _docker_image_present(self) -> bool:
        """Return True if ``docker image inspect <kofamscan.docker_image>`` exits 0."""
        if not self._docker_image:
            return False
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self._docker_image],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    # ------------------------------------------------------------------
    # Public annotate method
    # ------------------------------------------------------------------

    def annotate(  # type: ignore[override]
        self,
        proteins: dict[str, str],
        threads: int = 1,
        **params: Any,
    ) -> AnnotationResult:
        """Annotate protein sequences with KO assignments using KofamScan.

        Args:
            proteins: Mapping ``{caller_id: amino_acid_sequence}``. Values
                must be protein sequences; sequences that look like
                nucleotide (>10% non-protein chars) raise ``ValueError``.
            threads: Number of CPUs passed to ``--cpu``. Default 1.
            **params: Extra parameters merged into the recorded
                ``AnnotationResult.parameters`` dict. Not forwarded to the
                ``exec_annotation`` CLI.

        Returns:
            An ``AnnotationResult`` with:
            - ``tool = "kofamscan"``.
            - ``db_version`` = the selected profile-set name.
            - ``records`` keyed by the caller's original ids; genes with no
              significant hit are absent.
            - ``parameters`` carrying ``profile_set``, ``ko_list_version``,
              ``kegg_release``, and ``bridged_fraction`` on every run, plus
              ``ko_function_map = "absent"`` when the bridge table could not
              be loaded.

        Raises:
            ToolUnavailableError: If exec_annotation is not available.
            ValueError: If the input sequences fail the protein alphabet
                guard.
            subprocess.CalledProcessError: If exec_annotation exits
                non-zero.
        """
        self._require_available()
        _guard_protein(proteins)

        resolved_profiles = str(Path(self.profiles_path).expanduser().resolve())
        loaded = _load_ko_function_map(self._ko_function_map_path)
        table = loaded[0] if loaded is not None else None
        metadata = loaded[1] if loaded is not None else {}

        if loaded is None:
            _LOG.warning(
                "kofamscan.ko_function_map (%r) is unset or unreadable; "
                "KOFAMSCAN terms will carry KO ids only, with "
                "bridged_fraction=0.0.",
                self._ko_function_map_path or "<unset>",
            )

        run_id = uuid.uuid4().hex

        with tempfile.TemporaryDirectory(dir=self._docker_workdir_base()) as tmpdir:
            tmp = Path(tmpdir)
            fasta_path = tmp / "input.faa"
            _write_faa(fasta_path, proteins)
            scan_tmp_dir = tmp / "scan_tmp"
            scan_tmp_dir.mkdir()
            out_path = tmp / "out.tsv"

            tsv_text, command = self._run_kofamscan(
                fasta_path=fasta_path,
                out_path=out_path,
                scan_tmp_dir=scan_tmp_dir,
                resolved_profiles=resolved_profiles,
                threads=threads,
            )

        rows = _parse_kofam_detail_tsv(tsv_text)
        records, bridged_fraction = _build_kofam_records(rows, table, set(proteins))

        parameters: dict[str, Any] = {
            "threads": threads,
            "profiles_path": resolved_profiles,
            "profile_set": self.profile_set,
            "ko_list_version": self.profile_set,
            "kegg_release": metadata.get("kegg_release") if loaded is not None else None,
            "bridged_fraction": round(bridged_fraction, 4),
            "docker_image": self._docker_image,
            **params,
        }
        if loaded is None:
            parameters["ko_function_map"] = "absent"

        return AnnotationResult(
            tool=_TOOL,
            tool_version=(self._docker_image or self._kofamscan_exe) or None,
            db_version=self.profile_set,
            run_id=run_id,
            command=command,
            parameters=parameters,
            records=records,
        )

    # ------------------------------------------------------------------
    # Internal subprocess runner
    # ------------------------------------------------------------------

    def _run_kofamscan(
        self,
        fasta_path: Path,
        out_path: Path,
        scan_tmp_dir: Path,
        resolved_profiles: str,
        threads: int,
    ) -> tuple[str, str]:
        """Run exec_annotation and return (detail-tsv text, command string).

        Args:
            fasta_path: Path to the input FASTA.
            out_path: Destination for the ``-o`` detail-tsv output.
            scan_tmp_dir: Scratch dir passed to ``--tmp-dir``.
            resolved_profiles: Absolute, resolved profile-set root.
            threads: CPU count.

        Returns:
            Tuple of (detail-tsv text — empty string if the output file is
            absent, shlex-quoted command string).

        Raises:
            subprocess.CalledProcessError: If exec_annotation exits
                non-zero.
        """
        if self._docker_image:
            work = fasta_path.parent.resolve()
            if out_path.parent.resolve() != work or scan_tmp_dir.parent.resolve() != work:
                raise ValueError(
                    "Docker mode requires the input FASTA, output file and "
                    "scan tmp dir to share a parent dir; got "
                    f"fasta={fasta_path.parent} out={out_path.parent} "
                    f"tmp={scan_tmp_dir.parent}"
                )
            inner_args = [
                self._kofamscan_exe,
                "--cpu", str(threads),
                "-p", f"/profiles/profiles/{self.profile_set}",
                "-k", f"/profiles/profiles/{self.profile_set}.txt",
                "-f", "detail-tsv",
                "--tmp-dir", f"/work/{scan_tmp_dir.name}",
                "-o", f"/work/{out_path.name}",
                f"/work/{fasta_path.name}",
            ]
            cmd = [
                "docker", "run", "--rm",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--network", "none",
                "-v", f"{work}:/work",
                "-v", f"{resolved_profiles}:/profiles:ro",
                self._docker_image,
                *inner_args,
            ]
        else:
            cmd = [
                self._kofamscan_exe,
                "--cpu", str(threads),
                "-p", f"{resolved_profiles}/profiles/{self.profile_set}",
                "-k", f"{resolved_profiles}/profiles/{self.profile_set}.txt",
                "-f", "detail-tsv",
                "--tmp-dir", str(scan_tmp_dir),
                "-o", str(out_path),
                str(fasta_path),
            ]

        command_str = shlex.join(cmd)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )

        tsv_text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        return tsv_text, command_str


# ---------------------------------------------------------------------------
# FASTA writer (one-line-per-record, no wrapping)
# ---------------------------------------------------------------------------


def _write_faa(path: Path, sequences: dict[str, str]) -> None:
    """Write ``{id: sequence}`` as a one-line-per-record FASTA (no wrapping).

    Args:
        path: Destination ``.faa`` path (created/overwritten).
        sequences: Mapping of id -> amino-acid sequence. Headers are written
            verbatim from the caller's ids.
    """
    lines: list[str] = []
    for seq_id, seq in sequences.items():
        lines.append(f">{seq_id}")
        lines.append(seq.strip())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Convenience re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "KofamscanUtils",
    "_parse_kofam_detail_tsv",
    "_bridge_ko",
    "_build_kofam_records",
    "_parse_ko_function_map_text",
    "_load_ko_function_map",
    "_write_faa",
]
