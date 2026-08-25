"""BAKTA annotation utilities for KBUtilLib.

Implements ``BaktaUtils``, which wraps Bakta's **protein** entry point
(``bakta_proteins``, https://github.com/oschwengers/bakta) to annotate a
caller-supplied set of amino-acid (protein) sequences against a locally
registered Bakta reference database.

This is deliberately the ``bakta_proteins`` route, not the whole-contig
``bakta`` nucleotide pipeline — the two are different input shapes and only
the protein route is wired here.

Database
--------
The database directory is an explicit constructor argument
(``BaktaUtils(db_path=...)``), resolved per-request by the caller — NOT a
config key. This mirrors the ``cache_file=`` decision documented for other
per-request-resolved KBUtilLib annotators and keeps the database selection
out of static config.

``<db_path>/version.json`` is machine-readable
(``{"date", "major", "minor", "type"}``) and is read on every run so the
database's ``major`` version is recorded in ``AnnotationResult.parameters``.
This module does **not** assert tool-vs-database compatibility — Bakta owns
that decision, and an incompatible pair surfaces as a non-zero exit from
``bakta_proteins`` itself, which raises ``subprocess.CalledProcessError``.
When ``version.json`` is missing or unparseable, ``BaktaUtils`` fails fast
with ``ToolUnavailableError`` before ever invoking the tool, using a fixed
message: ``"Bakta DB at <resolved_path> is missing db/version.json or it is
not parseable"``.

Docker/native dispatch
-----------------------
Follows the existing KBUtilLib convention exactly (see ``ProkkaUtils`` /
``TransytUtils``): config key ``bakta.docker_image`` (empty => native
``bakta_proteins`` binary on ``PATH``) and ``bakta.docker_workdir`` for the
tempdir base. Availability is probed with ``docker image inspect``.

Docker containers run with ``--user <uid>:<gid>``, ``--network none``, and
the database bind-mounted read-only. ``bakta_proteins`` requires
``/opt/conda/bin`` on ``PATH`` inside the container or it fails with
*"AMRFinderPlus not found or not executable"* — the container is therefore
launched with ``--entrypoint bash`` so ``PATH`` can be prefixed before the
tool runs.

Result parsing
---------------
Reads Bakta's native JSON output at ``<output>/input.json``. Its
``features`` array carries one record per input protein, keyed by the
caller's own id (``feature["id"]``) — Bakta's protein entry point writes the
FASTA header verbatim from the caller's dict, so no id-remapping is needed
(unlike ``ProkkaUtils`` / ``DRAM2Utils``, which must remap to tool-safe ids).

Per the PRD's resolved spec (D4), only the free-text ``product`` string is
emitted as a ``Term``:

    Term(namespace="FUNCTION", id=None, value=<product>, evidence={...})

Bakta's per-gene record also carries ``gene``, and, nested under ``psc``,
``ec_ids``, ``kegg_orthology_id``, ``cog_id`` and ``go_ids``. None of these
are emitted as Terms — they would collide under the same ``FUNCTION``
namespace as the product string on the KBDL side and are recoverable later
by a deliberate widening. They are carried in ``Term.evidence`` (alongside
``aa_hexdigest`` when present) so nothing is silently dropped, since
``evidence`` never reaches the output map.

The payload's own version stamp, ``version = {"bakta": ..., "db":
{"version": ..., "type": ...}}``, is the source for
``AnnotationResult.tool_version`` / ``db_version``.
"""

from __future__ import annotations

import json
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
    ToolUnavailableError,
    _guard_protein,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOOL = "bakta"
_INSTALL_HINT = (
    "Build the kbutillib/bakta Docker image (docker/bakta/Dockerfile) and set "
    "bakta.docker_image in config.yaml, or install bakta_proteins on PATH. "
    "Requires a registered Bakta database directory containing version.json."
)

# psc.* fields carried in evidence but never emitted as Terms (D4).
_EVIDENCE_ONLY_PSC_FIELDS: tuple[str, ...] = (
    "ec_ids",
    "kegg_orthology_id",
    "cog_id",
    "go_ids",
)


# ---------------------------------------------------------------------------
# Pure parse helpers (offline-unit-testable)
# ---------------------------------------------------------------------------


def _parse_bakta_features(
    features: list[dict[str, Any]],
    input_ids: set[str] | None = None,
) -> list[AnnotationRecord]:
    """Convert Bakta's ``input.json`` ``features`` array into AnnotationRecords.

    Emits exactly one ``Term`` per gene — the free-text ``product`` string
    under ``namespace="FUNCTION"`` — per D4. ``ec_ids``, ``kegg_orthology_id``,
    ``cog_id`` and ``go_ids`` (nested under ``feature["psc"]``) are carried in
    ``Term.evidence`` only, never emitted as Terms. ``aa_hexdigest`` is also
    carried in evidence when present.

    Args:
        features: The parsed ``features`` array from Bakta's ``input.json``.
            Each element is expected to carry ``id`` (the caller's original
            protein id, preserved verbatim) and ``product``.
        input_ids: When given, only features whose ``id`` is a member of this
            set are retained (defends against stray rows from a shared
            output). ``None`` disables the filter.

    Returns:
        List of ``AnnotationRecord``. A feature with an empty/missing
        ``product`` yields no Term and is therefore absent from the result —
        this is not an error.
    """
    records: list[AnnotationRecord] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        gene_id = feature.get("id")
        if not gene_id:
            continue
        if input_ids is not None and gene_id not in input_ids:
            continue

        product = (feature.get("product") or "").strip()
        if not product:
            continue

        evidence: dict[str, Any] = {}
        psc = feature.get("psc")
        if isinstance(psc, dict):
            for key in _EVIDENCE_ONLY_PSC_FIELDS:
                value = psc.get(key)
                if value:
                    evidence[key] = value
        gene_val = feature.get("gene")
        if gene_val:
            evidence["gene"] = gene_val
        aa_hexdigest = feature.get("aa_hexdigest")
        if aa_hexdigest:
            evidence["aa_hexdigest"] = aa_hexdigest

        term = Term(namespace="FUNCTION", id=None, value=product, evidence=evidence)
        records.append(AnnotationRecord(gene_id=gene_id, terms=[term]))

    return records


def _parse_bakta_db_version(text: str) -> dict[str, Any]:
    """Parse a Bakta ``db/version.json`` payload.

    Args:
        text: Raw file contents of ``<db_path>/version.json``.

    Returns:
        The parsed JSON object as a dict.

    Raises:
        ValueError: If *text* is not parseable JSON, or does not decode to a
            dict carrying a ``major`` key.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not parseable JSON: {exc}") from exc
    if not isinstance(payload, dict) or "major" not in payload:
        raise ValueError("missing 'major' key")
    return payload


# ---------------------------------------------------------------------------
# BaktaUtils class
# ---------------------------------------------------------------------------


class BaktaUtils(AnnotatorUtils):
    """Annotate proteins with Bakta's protein entry point (``bakta_proteins``).

    Config keys (read via ``get_config_value``):
        ``bakta.docker_image`` — Docker image tag to run ``bakta_proteins``
            in. Empty (the default) => run the native ``bakta_proteins``
            binary on ``PATH``.
        ``bakta.executable`` — native executable name/path (default
            ``"bakta_proteins"``). Ignored when ``bakta.docker_image`` is set.
        ``bakta.docker_workdir`` — base dir for the per-run bind-mounted
            work dir (default ``~/.kbutillib/bakta_work``).

    Example::

        bu = BaktaUtils(db_path="/scratch/kbdl/refdata/bakta-db-6.0-full")
        if bu.is_available():
            result = bu.annotate({"gene1": "MKTAYIAKQ...", "gene2": "MNFSTPD..."})
            for rec in result.records:
                print(rec.gene_id, [t.value for t in rec.terms])

    Raises:
        ValueError: If the input contains nucleotide-looking sequences.
        ToolUnavailableError: If the Docker image / native binary is absent,
            or if the database's ``version.json`` is missing or unparseable.
    """

    _tool_name = _TOOL
    _install_hint = _INSTALL_HINT

    def __init__(self, db_path: str, **kwargs: Any) -> None:
        """Initialize BaktaUtils against a specific, already-resolved database.

        Args:
            db_path: Path to the Bakta reference database directory
                (must contain ``version.json`` and ``bakta.db``). Resolved
                per-request by the caller — this is a constructor argument,
                not a config key, so multiple database versions can coexist
                without touching config.
            **kwargs: Forwarded to ``AnnotatorUtils.__init__``.
        """
        super().__init__(**kwargs)
        self.db_path = db_path
        self._bakta_exe: str = self.get_config_value(
            "bakta.executable", default="bakta_proteins"
        )
        #: When set, bakta_proteins runs inside this Docker image instead of
        #: as a local executable. Empty string => native executable mode.
        self._docker_image: str = (
            self.get_config_value("bakta.docker_image", default="") or ""
        )

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if bakta_proteins can run, side-effect-free.

        In Docker mode (``bakta.docker_image`` set) this checks that the
        image is present locally (``docker image inspect``). Otherwise it
        checks that the configured executable resolves via ``shutil.which``
        (absolute paths are checked for existence + executable bit).

        Returns:
            True if bakta_proteins is runnable via the active mode.
        """
        if self._docker_image:
            return self._docker_image_present()
        exe = Path(self._bakta_exe)
        if exe.is_absolute():
            return exe.is_file() and os.access(exe, os.X_OK)
        return shutil.which(self._bakta_exe) is not None

    def _docker_workdir_base(self) -> str | None:
        """Base dir for the per-run work dir (``tempfile.TemporaryDirectory``).

        Returns None in native mode (system default ``$TMPDIR``). In Docker
        mode the work dir is bind-mounted into the container, so it must
        live under a path Docker Desktop shares by default (see
        ``ProkkaUtils._docker_workdir_base`` for the same macOS/Docker
        caveat). Override with ``bakta.docker_workdir``.
        """
        if not self._docker_image:
            return None
        base = (
            self.get_config_value("bakta.docker_workdir", default="")
            or str(Path.home() / ".kbutillib" / "bakta_work")
        )
        Path(base).mkdir(parents=True, exist_ok=True)
        return base

    def _docker_image_present(self) -> bool:
        """Return True if ``docker image inspect <bakta.docker_image>`` exits 0."""
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
        """Annotate protein sequences using Bakta's ``bakta_proteins``.

        Args:
            proteins: Mapping ``{caller_id: amino_acid_sequence}``. Values
                must be protein sequences; sequences that look like
                nucleotide (>10% non-protein chars) raise ``ValueError``.
            threads: Number of CPUs passed to ``--threads``. Default 1.
            **params: Extra parameters merged into the recorded
                ``AnnotationResult.parameters`` dict. Not forwarded to the
                ``bakta_proteins`` CLI.

        Returns:
            An ``AnnotationResult`` with:
            - ``tool = "bakta"``.
            - ``tool_version`` / ``db_version`` sourced from the payload's
              own ``version`` stamp.
            - ``records`` keyed by the caller's original ids; genes with no
              ``product`` are absent.

        Raises:
            ToolUnavailableError: If bakta_proteins is not available, or if
                the configured database's ``version.json`` is missing or
                unparseable.
            ValueError: If the input sequences fail the protein alphabet
                guard.
            subprocess.CalledProcessError: If ``bakta_proteins`` exits
                non-zero.
        """
        self._require_available()
        _guard_protein(proteins)

        resolved_db = str(Path(self.db_path).expanduser().resolve())
        db_payload = self._read_db_version(resolved_db)

        run_id = uuid.uuid4().hex

        with tempfile.TemporaryDirectory(dir=self._docker_workdir_base()) as tmpdir:
            tmp = Path(tmpdir)
            fasta_path = tmp / "input.faa"
            _write_one_line_faa(fasta_path, proteins)
            outdir = tmp / "bakta_out"

            payload, command = self._run_bakta(
                fasta_path=fasta_path,
                outdir=outdir,
                resolved_db=resolved_db,
                threads=threads,
            )

        features = payload.get("features", []) if isinstance(payload, dict) else []
        records = _parse_bakta_features(features, set(proteins))

        version_info = payload.get("version", {}) if isinstance(payload, dict) else {}
        tool_version = (
            version_info.get("bakta") if isinstance(version_info, dict) else None
        )
        db_info = version_info.get("db") if isinstance(version_info, dict) else None
        db_version = (
            db_info.get("version") if isinstance(db_info, dict) else None
        )

        parameters: dict[str, Any] = {
            "threads": threads,
            "db_path": resolved_db,
            "db_major": db_payload.get("major"),
            "db_type": db_payload.get("type"),
            "docker_image": self._docker_image,
            **params,
        }

        return AnnotationResult(
            tool=_TOOL,
            tool_version=tool_version,
            db_version=db_version,
            run_id=run_id,
            command=command,
            parameters=parameters,
            records=records,
        )

    # ------------------------------------------------------------------
    # Database version check
    # ------------------------------------------------------------------

    def _read_db_version(self, resolved_db: str) -> dict[str, Any]:
        """Read and parse ``<resolved_db>/version.json``.

        Args:
            resolved_db: Absolute, resolved path to the database directory.

        Returns:
            The parsed ``version.json`` payload.

        Raises:
            ToolUnavailableError: When the file is missing or unparseable.
                Uses the fixed wording required for ops-runbook matching:
                ``"Bakta DB at <resolved_db> is missing db/version.json or
                it is not parseable"``.
        """
        version_path = Path(resolved_db) / "version.json"
        try:
            text = version_path.read_text(encoding="utf-8")
            payload = _parse_bakta_db_version(text)
        except (OSError, ValueError):
            raise ToolUnavailableError(
                tool=_TOOL,
                detail=(
                    f"Bakta DB at {resolved_db} is missing db/version.json "
                    "or it is not parseable"
                ),
            ) from None
        return payload

    # ------------------------------------------------------------------
    # Internal subprocess runner
    # ------------------------------------------------------------------

    def _run_bakta(
        self,
        fasta_path: Path,
        outdir: Path,
        resolved_db: str,
        threads: int,
    ) -> tuple[dict[str, Any], str]:
        """Run bakta_proteins and return (parsed input.json, command string).

        Args:
            fasta_path: Path to the one-line-per-record input FASTA.
            outdir: Output directory. ``bakta_proteins`` refuses an
                ``--output`` directory that already exists, so this method
                creates only ``outdir.parent`` and leaves ``outdir`` itself
                for ``bakta_proteins`` to create.
            resolved_db: Absolute, resolved path to the database directory.
            threads: CPU count.

        Returns:
            Tuple of (parsed ``input.json`` payload — ``{}`` if the file is
            absent, shlex-quoted command string).

        Raises:
            subprocess.CalledProcessError: If bakta_proteins exits non-zero.
        """
        # bakta_proteins refuses an --output directory that already exists,
        # so only the parent is created here; bakta_proteins creates
        # ``outdir`` itself.
        outdir.parent.mkdir(parents=True, exist_ok=True)

        if self._docker_image:
            work = fasta_path.parent.resolve()
            if outdir.parent.resolve() != work:
                raise ValueError(
                    "Docker mode requires the input FASTA and outdir to share "
                    f"a parent dir; got fasta={fasta_path.parent} "
                    f"outdir={outdir.parent}"
                )
            inner = (
                "PATH=/opt/conda/bin:$PATH bakta_proteins "
                f"--threads {threads} --db /db --output /work/{outdir.name} "
                f"/work/{fasta_path.name}"
            )
            cmd = [
                "docker", "run", "--rm",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--network", "none",
                "-v", f"{work}:/work",
                "-v", f"{resolved_db}:/db:ro",
                "--entrypoint", "bash",
                self._docker_image,
                "-lc", inner,
            ]
        else:
            cmd = [
                self._bakta_exe,
                "--threads", str(threads),
                "--db", resolved_db,
                "--output", str(outdir),
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

        json_path = outdir / "input.json"
        if not json_path.exists():
            return {}, command_str
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        return payload, command_str


# ---------------------------------------------------------------------------
# Shared FASTA writer (one-line-per-record, no wrapping)
# ---------------------------------------------------------------------------


def _write_one_line_faa(path: Path, sequences: dict[str, str]) -> None:
    """Write ``{id: sequence}`` as a one-line-per-record FASTA (no wrapping).

    Args:
        path: Destination ``.faa`` path (created/overwritten).
        sequences: Mapping of id -> amino-acid sequence. Headers are written
            verbatim from the caller's ids (no remapping).
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
    "BaktaUtils",
    "_parse_bakta_features",
    "_parse_bakta_db_version",
    "_write_one_line_faa",
]
