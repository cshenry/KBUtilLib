"""DRAM2 annotation utilities for KBUtilLib.

Implements ``DRAM2Utils``, which wraps the DRAM2 Nextflow pipeline
(https://github.com/WrightonLabCSU/DRAM2) to annotate a caller-supplied
set of amino-acid (protein) sequences.

DRAM2 is the Snakemake/Nextflow rewrite of DRAM1.  Unlike DRAM1's
``DRAM.py`` Python CLI, DRAM2 is a Nextflow pipeline launched as::

    nextflow run main.nf --annotate \\
        --input_genes <dir> \\
        --outdir <abs-dir> \\
        -profile conda \\
        --use_kofam --use_dbcan ...

This module:

- accepts ``{caller_id: protein_seq}``,
- guards that input is amino-acid (rejects nucleotide via
  :func:`_guard_protein`),
- writes the proteins to a single ``input.faa`` in a temp ``input_genes``
  directory, assigning synthetic prodigal-safe ids ``g_1, g_2, …``
  (1-based, in ``proteins`` insertion order) as the FASTA header ids
  and recording the ``{emitted_id -> caller_id}`` reverse map so that
  DRAM2 output rows can be translated back to the original caller ids,
- invokes the pinned DRAM2 ``nextflow run`` command with an explicitly
  constructed subprocess environment (``NXF_VER`` pinned; optional
  ``PATH`` prefix from config), and
- parses the ``raw-annotations.tsv`` published under
  ``{outdir}/RAW/raw-annotations.tsv``, translating each row's
  ``query_id`` (a synthetic ``g_<n>``) back to the caller id via the
  reverse map before constructing ``AnnotationRecord`` objects.

Note: the ``.faa`` header id is now a synthetic ``g_<n>`` token, NOT the
caller id.  The caller id is recovered from the ``{emitted_id ->
caller_id}`` reverse map built by ``_write_faa`` and passed to
``_parse_annotations_tsv``.  The public ``AnnotationResult.records[].gene_id``
value is always the original caller id.

CLI invocation pinned on h100 (2026-06-18)
-----------------------------------------
Pipeline:     ``/scratch1/fliu/hub_scratch/chenry/DRAM2/repo/main.nf``
              (also reachable via the env var ``$DRAM2_PIPELINE``)
              On the native h100 host set
              ``dram2.nextflow = /scratch1/fliu/hub_scratch/chenry/DRAM2/bin/nextflow-native``
Engine:       Nextflow 24.10.5 in micromamba env ``$CONDA_ENVS_PATH/env_nf``
Profile:      ``conda`` (drives per-process conda envs via micromamba)
Launch dir:   ``$DRAM2_ROOT`` — the config resolves database paths as
              ``${launchDir}/databases/<db>``, so the pipeline MUST be
              launched from that root.

Output schema pinned at build time (raw-annotations.tsv columns)
----------------------------------------------------------------
Captured from the upstream OWC golden snapshot
``repo/tests/data/owc/annotation/raw_annotations_snapshot.tsv`` produced
by a real DRAM2 run on the h100 install.  Header is tab-separated and
contains a fixed prefix block followed by per-database tuples::

    query_id  input_fasta  start_position  stop_position  strandedness
    rank  gene_number
    [for each enabled database <db>:]
        <db>_id  <db>_EC  <db>_bitScore  <db>_description
        [<db>_gene_name] [<db>_score_rank] [<db>_rank] [<db>_family] ...

``query_id`` is the emitted synthetic id (``g_<n>``), translated back to
the caller id via the reverse map before ``AnnotationRecord.gene_id`` is
set.

Namespace tagging
-----------------
Per the PRD's Confront-resolved spec 8, recognised database id columns
map to controlled namespaces:

- ``kegg_id``, ``kofam_id`` → ``"KO"`` (KEGG Orthology id, e.g. K12345)
- ``pfam_id``               → ``"PF"`` (Pfam accession, e.g. PF00001)
- any ``*_EC`` column       → ``"EC"`` (one Term per EC after splitting)
- ``dbcan_id``              → ``"CAZY"`` (dbCAN / CAZy family)

``<db>_description`` strings (the human-readable hit text) are emitted
as ``Term(namespace=None, id=None, value=<description>)`` free-text.
Unknown ``*_id`` columns (databases the module does not pre-tag) also
fall through to ``namespace=None`` free-text with the column name
included in ``evidence`` so the source DB is recoverable.

Empty / missing values are silently skipped; rows with only the prefix
columns populated (no hits in any DB) produce no ``Term``s and the gene
is absent from the result's ``records`` list.

Live integration test
---------------------
The live test (``KBU_DRAM2_LIVE=1`` + ``is_available()``) runs only on
h100.  It launches the full Nextflow pipeline on a tiny FASTA — which
spans Nextflow startup + per-process conda env build + at least one DB
search; expect runtime ≥ several minutes.  The offline parse tests use a
small slice of the upstream OWC golden fixture and pass with no DRAM2
installed.
"""

from __future__ import annotations

import logging
import os
import re
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOOL = "dram2"
_INSTALL_HINT = (
    "Install DRAM2 (Nextflow pipeline). On h100 the install root is "
    "/scratch1/fliu/hub_scratch/chenry/DRAM2 (via $DRAM2_ROOT); set "
    "dram2.pipeline / dram2.launch_dir / dram2.nextflow in config.yaml "
    "or rely on the $DRAM2_PIPELINE env var. "
    "On the native h100 host set "
    "dram2.nextflow = /scratch1/fliu/hub_scratch/chenry/DRAM2/bin/nextflow-native"
)

_LOG = logging.getLogger(__name__)

# Default published location of the combined raw annotations file (set by
# DRAM2's modules.config: `withName: COMBINE_ANNOTATIONS { publishDir =
# "${params.outdir}/RAW" }`).  The file name is hard-coded in
# `modules/local/annotate/combine_annotations.nf` (`--output raw-annotations.tsv`).
_RAW_ANNOTATIONS_SUBPATH = ("RAW", "raw-annotations.tsv")

# Default enabled databases for the annotate run.  These are the metabolic-
# function databases present on the h100 install (UniRef90 deliberately
# excluded per the install README; kegg excluded because the full KEGG payload
# requires a separately-formatted DB that the install does not carry by
# default; pfam excluded — it has no metabolic-function value in the inner-
# loop context and dramatically inflates runtime).  Callers can override by
# passing ``databases=[...]`` to ``annotate()``.
_DEFAULT_DATABASES: tuple[str, ...] = (
    "kofam",
    "dbcan",
    "merops",
    "vog",
)

# Column-name → controlled namespace lookup for `_id` columns.  Any column
# ending in `_id` that is not listed here falls through to namespace=None
# free-text (with the column name recorded in `evidence` for forensics).
_ID_COLUMN_NAMESPACE: dict[str, str] = {
    "kegg_id": "KO",
    "kofam_id": "KO",
    "pfam_id": "PF",
    "dbcan_id": "CAZY",
}

# Splitter for multi-valued EC fields (DRAM2 uses semicolons; also accept
# commas and runs of whitespace defensively).
_MULTI_VAL_RE = re.compile(r"[;,\s]+")

# Prefix columns that are NOT per-database hit data — skipped when scanning
# for `_id`/`_EC`/`_description` triplets.
_PREFIX_COLUMNS: frozenset[str] = frozenset({
    "query_id",
    "input_fasta",
    "start_position",
    "stop_position",
    "end_position",
    "strandedness",
    "rank",
    "gene_number",
})

# Default Nextflow version to pin (overridable via dram2.nxf_ver in config).
_DEFAULT_NXF_VER = "24.10.5"


# ---------------------------------------------------------------------------
# Pure parse helpers (offline-unit-testable)
# ---------------------------------------------------------------------------


def _parse_annotations_tsv(
    tsv_text: str,
    emitted_to_caller: dict[str, str],
) -> list[AnnotationRecord]:
    """Parse a DRAM2 ``raw-annotations.tsv`` and return AnnotationRecords.

    The header is a fixed-prefix block (``query_id``, ``input_fasta``,
    ``start_position``, ``stop_position``, ``strandedness``, ``rank``,
    ``gene_number``) followed by per-database tuples.  For each row, the
    parser walks every non-prefix column and:

    - For ``<db>_id`` columns: emits a ``Term`` keyed by namespace from
      :data:`_ID_COLUMN_NAMESPACE` (or ``namespace=None`` free-text for
      unrecognized DBs, with the column name in ``evidence["source"]``).
    - For ``<db>_EC`` columns: splits on ``;``/``,``/whitespace and emits
      one ``Term("EC", ec, ec)`` per non-empty value.  Each value is
      stripped of any leading ``EC:`` prefix (DRAM2's KEGG/KOFAM rows
      use ``EC:1.2.3.4`` form, dbCAN often emits bare ``1.2.3.4``).
    - For ``<db>_description`` columns: emits a free-text Term
      ``(None, None, description)``.

    Empty values are skipped.  Rows whose ``query_id`` is not a key in
    *emitted_to_caller* are dropped (guards against spurious ids from
    runs that mix batches).

    Args:
        tsv_text: Full text of the ``raw-annotations.tsv`` file.
        emitted_to_caller: Mapping ``{emitted_id -> caller_id}`` as
            returned by ``_write_faa``.  Only rows whose ``query_id``
            (the emitted synthetic ``g_<n>`` id) is a key in this map
            are retained; the corresponding caller id is set as
            ``AnnotationRecord.gene_id``.

    Returns:
        List of ``AnnotationRecord``.  Genes with no Terms emitted are
        omitted.  Records appear in ``proteins`` insertion order — the
        order of *emitted_to_caller* values (which mirrors the original
        caller's dict insertion order).
    """
    lines = tsv_text.splitlines()
    if not lines:
        return []

    header = lines[0].split("\t")
    if "query_id" not in header:
        return []
    col: dict[str, int] = {name: idx for idx, name in enumerate(header)}

    # Group terms by CALLER id (translated from emitted_id).
    per_gene: dict[str, list[Term]] = {}

    # Pre-compute the columns we will emit Terms from.  Build three lists so
    # the per-row loop is straight-line.
    id_columns: list[tuple[str, int]] = []          # (column_name, col_index)
    ec_columns: list[tuple[str, int]] = []
    desc_columns: list[tuple[str, int]] = []
    for name, idx in col.items():
        if name in _PREFIX_COLUMNS:
            continue
        if name.endswith("_id"):
            id_columns.append((name, idx))
        elif name.endswith("_EC"):
            ec_columns.append((name, idx))
        elif name.endswith("_description"):
            desc_columns.append((name, idx))

    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split("\t")
        # Pad short rows to header length
        while len(parts) < len(header):
            parts.append("")

        emitted_id = parts[col["query_id"]].strip()
        # Translate emitted id -> caller id; drop rows not in the map
        if emitted_id not in emitted_to_caller:
            continue
        caller_id = emitted_to_caller[emitted_id]

        row_terms: list[Term] = []
        for name, idx in id_columns:
            val = parts[idx].strip()
            if not val:
                continue
            ns = _ID_COLUMN_NAMESPACE.get(name)
            evidence = {"source": name}
            row_terms.append(
                Term(namespace=ns, id=val, value=val, evidence=evidence)
            )

        for name, idx in ec_columns:
            raw = parts[idx].strip()
            if not raw:
                continue
            for piece in _MULTI_VAL_RE.split(raw):
                piece = piece.strip()
                if not piece:
                    continue
                # Strip any leading "EC:" prefix DRAM2's KEGG/KOFAM rows use
                if piece.upper().startswith("EC:"):
                    piece = piece[3:]
                if not piece:
                    continue
                row_terms.append(
                    Term(
                        namespace="EC",
                        id=piece,
                        value=piece,
                        evidence={"source": name},
                    )
                )

        for name, idx in desc_columns:
            val = parts[idx].strip()
            if not val:
                continue
            row_terms.append(
                Term(
                    namespace=None,
                    id=None,
                    value=val,
                    evidence={"source": name},
                )
            )

        if row_terms:
            per_gene.setdefault(caller_id, []).extend(row_terms)

    # Emit records in caller insertion order (i.e. emitted_to_caller values
    # order, which mirrors the proteins dict order), skipping ids with no terms.
    records: list[AnnotationRecord] = []
    seen_caller_ids: set[str] = set()
    for caller_id in emitted_to_caller.values():
        if caller_id in seen_caller_ids:
            continue
        seen_caller_ids.add(caller_id)
        terms = per_gene.get(caller_id)
        if terms:
            records.append(AnnotationRecord(gene_id=caller_id, terms=terms))
    return records


def _parse_dram2_version(text: str) -> str | None:
    """Best-effort extraction of a DRAM2 / Nextflow pipeline version string.

    Looks for ``N E X T F L O W   ~  version X.Y.Z`` or
    ``Launching '... main.nf' [...]  DSL2 - revision: <sha> [<tag>]``
    style banners in *text*.

    Args:
        text: Captured stdout/stderr text from a Nextflow run.

    Returns:
        Pipeline revision/tag string (e.g. ``"v2.0.0-beta17"``), the
        Nextflow version, or ``None`` when nothing identifiable is found.
    """
    # Pipeline tag in the Launching banner: "[<sha> <tag>]"
    m = re.search(r"revision:\s*[0-9a-f]+\s*\[([^\]]+)\]", text)
    if m:
        return m.group(1).strip()
    # Nextflow engine banner
    m = re.search(r"N E X T F L O W\s*~\s*version\s+(\S+)", text)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# DRAM2Utils class
# ---------------------------------------------------------------------------


class DRAM2Utils(AnnotatorUtils):
    """Annotate proteins with DRAM2 (Nextflow pipeline).

    DRAM2 runs as a Nextflow pipeline (``main.nf``) — the module wraps
    that invocation.  Because DRAM2 resolves its database paths relative
    to ``launchDir``, the pipeline must be launched from a specific
    install root (``$DRAM2_ROOT``).  The module reads that root and the
    pipeline path from config (``dram2.launch_dir``, ``dram2.pipeline``)
    or the ``$DRAM2_ROOT`` / ``$DRAM2_PIPELINE`` environment variables.

    Config keys (read via ``get_config_value``):
        ``dram2.nextflow`` — path to the ``nextflow`` binary (default
            ``"nextflow"`` — must be on PATH).  On the native h100 host
            set this to
            ``/scratch1/fliu/hub_scratch/chenry/DRAM2/bin/nextflow-native``.
        ``dram2.pipeline`` — path to ``main.nf`` (default ``$DRAM2_PIPELINE``
            env var if set, else empty -> unavailable).
        ``dram2.launch_dir`` — directory the pipeline is launched from
            (default ``$DRAM2_ROOT`` env var if set, else empty -> unavailable).
        ``dram2.profile`` — Nextflow profile (default ``"conda"``).
        ``dram2.config`` — optional extra ``-c <file>`` config path (default
            empty / omit).
        ``dram2.nxf_ver`` — Nextflow engine version to pin via ``NXF_VER``
            in the subprocess environment (default ``"24.10.5"``).
        ``dram2.env_path`` — colon-separated list of directories to prepend
            to ``PATH`` in the subprocess environment (e.g.
            ``"/scratch1/.../DRAM2/env/env_nf/bin:/scratch1/.../micromamba/bin"``).
            Default empty — ``PATH`` is inherited unchanged.  YAML list
            values are NOT supported; supply as a single colon-joined string.
        ``dram2.work_root`` — directory under which ``mkdtemp``-based scratch
            dirs are created (default ``<launch_dir>/scratch``; created if
            absent; never ``/tmp`` unless explicitly set).
        ``dram2.keep_work`` — when True, preserve the scratch dir after a
            successful run (default False).

    Example::

        utils = DRAM2Utils()
        if utils.is_available():
            result = utils.annotate({"protein_001": "MKTAYIAK..."})
            for rec in result.records:
                print(rec.gene_id, [(t.namespace, t.id) for t in rec.terms])
    """

    _tool_name = _TOOL
    _install_hint = _INSTALL_HINT

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._nextflow_exe: str = self.get_config_value(
            "dram2.nextflow", default="nextflow"
        )
        self._pipeline: str = self.get_config_value(
            "dram2.pipeline", default=os.environ.get("DRAM2_PIPELINE", "")
        )
        self._launch_dir: str = self.get_config_value(
            "dram2.launch_dir", default=os.environ.get("DRAM2_ROOT", "")
        )
        self._profile: str = self.get_config_value(
            "dram2.profile", default="conda"
        )
        self._extra_config: str = self.get_config_value(
            "dram2.config", default=""
        )
        # Run-hardening config keys
        self._nxf_ver: str = self.get_config_value(
            "dram2.nxf_ver", default=_DEFAULT_NXF_VER
        )
        # dram2.env_path: single colon-separated string, NOT a YAML list.
        # Example: "/scratch1/.../DRAM2/env/env_nf/bin:/scratch1/.../micromamba/bin"
        self._env_path: str = self.get_config_value(
            "dram2.env_path", default=""
        )
        self._work_root: str = self.get_config_value(
            "dram2.work_root", default=""
        )
        self._keep_work: bool = bool(self.get_config_value(
            "dram2.keep_work", default=False
        ))

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True when DRAM2 can plausibly be launched on this host.

        The probe checks three things — all must hold:

        1. The configured ``nextflow`` binary is resolvable via
           :func:`shutil.which` (handles both bare names searched on PATH
           and absolute paths that exist and are executable).
        2. ``dram2.pipeline`` is set and points to an existing file
           (typically ``$DRAM2_PIPELINE`` / ``$DRAM2_ROOT/repo/main.nf``).
        3. ``dram2.launch_dir`` is set and points to an existing directory
           (typically ``$DRAM2_ROOT``).

        Additionally emits a WARNING (but does NOT return False) when the
        configured ``pipeline`` path does not resolve under ``launch_dir``.
        This catches the stale-``DRAM2_ROOT`` class of misconfiguration early
        (e.g. pipeline on ``/scratch1/...`` but launch_dir still pointing to
        the old ``/storage/...`` install root).

        Side-effect-free aside from the optional warning log.
        """
        if not self._nextflow_exe or shutil.which(self._nextflow_exe) is None:
            return False
        if not self._pipeline or not Path(self._pipeline).is_file():
            return False
        if not self._launch_dir or not Path(self._launch_dir).is_dir():
            return False
        # Warn (don't fail) when pipeline is not under launch_dir
        try:
            pipeline_resolved = Path(self._pipeline).resolve()
            launch_resolved = Path(self._launch_dir).resolve()
            pipeline_resolved.relative_to(launch_resolved)
        except ValueError:
            _LOG.warning(
                "DRAM2Utils misconfiguration: pipeline %r does not resolve "
                "under launch_dir %r.  Database paths may not resolve correctly.",
                self._pipeline,
                self._launch_dir,
            )
        return True

    # ------------------------------------------------------------------
    # Public annotate method
    # ------------------------------------------------------------------

    def annotate(  # type: ignore[override]
        self,
        proteins: dict[str, str],
        databases: tuple[str, ...] | list[str] | None = None,
        gene_coords: dict[str, tuple[int, int, int]] | None = None,
        run_config: str | None = None,
        threads: int = 1,
        keep_work: bool | None = None,
        **params: Any,
    ) -> AnnotationResult:
        """Annotate proteins with DRAM2.

        Args:
            proteins: Mapping of ``{caller_id: amino_acid_sequence}``.
                Sequences must be amino-acid; nucleotide-looking input
                is rejected by the protein alphabet guard.  Must be a
                standard insertion-ordered dict; behavior is undefined
                for unordered mappings.
            databases: Iterable of DRAM2 database short names to enable
                (e.g. ``["kofam", "dbcan"]``).  Each name ``X``
                appends a ``--use_X`` flag to the Nextflow invocation.
                Defaults to :data:`_DEFAULT_DATABASES`.
            gene_coords: Optional mapping of ``{caller_id: (start, stop,
                strand)}`` supplying genomic coordinates for prodigal-
                style FASTA headers.  When provided, the FASTA header for
                each id is written as ``>{emitted_id} # {start} # {stop} #
                {strand} #`` (coords looked up by CALLER id; strand
                normalised to numeric ``1``/``-1``).  When absent,
                synthetic coords are produced
                (``start=1, stop=3*len(seq), strand=1``).
            run_config: Optional path to an extra Nextflow ``-c`` config
                file.  When given, overrides the ``dram2.config`` value
                from the instance config (``self._extra_config``).
            threads: ``--threads`` value passed to the pipeline.
            keep_work: When True, preserve the scratch dir after a
                successful run.  When False, delete it.  When None
                (default), the ``dram2.keep_work`` config value is used.
                On failure the scratch dir is ALWAYS preserved regardless
                of this flag.
            **params: Extra parameters merged into the recorded
                ``AnnotationResult.parameters`` dict.  Not forwarded to
                the Nextflow CLI.

        Returns:
            An ``AnnotationResult`` with:
            - ``tool = "dram2"``
            - ``tool_version`` = pipeline revision/tag string when
              parseable from Nextflow output, else ``None``.
            - ``db_version`` = the comma-joined list of enabled DBs (the
              DRAM2 DBs themselves are content-addressed by file digest;
              the install root path is captured in ``parameters`` for
              full provenance).
            - ``run_id``: uuid4 hex.
            - ``command``: shlex-quoted ``nextflow run`` argv.
            - ``parameters``: resolved values including defaults, plus
              ``work_dir`` (absolute scratch path) and ``kept`` (bool).
            - ``records``: one per caller id that produced >=1 Term;
              genes with zero hits are absent; ``gene_id`` is always
              the original caller id.

        Raises:
            ToolUnavailableError: when ``is_available()`` is False.
            ValueError: when the input sequences fail the protein guard.
            subprocess.CalledProcessError: when ``nextflow run`` exits
                non-zero (scratch dir preserved before re-raise).
        """
        self._require_available()
        _guard_protein(proteins)

        databases = tuple(databases) if databases is not None else _DEFAULT_DATABASES

        # Resolve keep_work: kwarg wins over config
        effective_keep_work = self._keep_work if keep_work is None else keep_work

        # run_config overrides the instance-level extra_config when provided.
        effective_config = run_config if run_config is not None else self._extra_config

        run_id = uuid.uuid4().hex

        # Resolve work_root: config key > default (<launch_dir>/scratch)
        if self._work_root:
            work_root = Path(self._work_root)
        else:
            work_root = Path(self._launch_dir) / "scratch"
        work_root.mkdir(parents=True, exist_ok=True)

        scratch: str | None = None
        work_dir_abs: str = ""
        kept: bool = False

        try:
            scratch = tempfile.mkdtemp(prefix="dram2_", dir=str(work_root))
            work_dir_abs = str(Path(scratch).resolve())
            tmp = Path(scratch)
            genes_dir = tmp / "input_genes"
            genes_dir.mkdir()
            outdir = tmp / "out"
            workdir = tmp / "work"
            outdir.mkdir()
            workdir.mkdir()

            # Write proteins as prodigal-style multi-FASTA with synthetic
            # g_<n> ids. DRAM2 preserves the first whitespace token of each
            # header as `query_id`; the emitted_to_caller map is used to
            # translate results back to caller ids.
            faa_path = genes_dir / "input.faa"
            emitted_to_caller = self._write_faa(faa_path, proteins, gene_coords=gene_coords)

            tsv_text, tool_version, command = self._run_nextflow(
                genes_dir=genes_dir,
                outdir=outdir,
                workdir=workdir,
                databases=databases,
                threads=threads,
                effective_config=effective_config,
                run_id=run_id,
                work_root=work_root,
            )

        except Exception as exc:
            # Keep-on-failure: always preserve scratch + copy debug artifacts
            if scratch is not None:
                kept = True
                failed_dir = work_root / f"failed-{run_id}"
                try:
                    failed_dir.mkdir(parents=True, exist_ok=True)
                    # Copy pipeline_info/ from outdir if present
                    src_info = Path(scratch) / "out" / "pipeline_info"
                    if src_info.exists():
                        shutil.copytree(src_info, failed_dir / "pipeline_info")
                    # Copy .nextflow.log from launch_dir if present
                    nxf_log = Path(self._launch_dir) / ".nextflow.log"
                    if nxf_log.exists():
                        shutil.copy2(str(nxf_log), str(failed_dir / "nextflow.log"))
                except Exception:
                    # Never raise a secondary error while handling the primary
                    pass
                _LOG.error(
                    "DRAM2 run %s failed; scratch dir preserved at %s; "
                    "debug artifacts copied to %s",
                    run_id,
                    scratch,
                    failed_dir,
                )
            raise

        # On success: delete scratch unless keep_work
        if effective_keep_work:
            kept = True
        else:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)

        parameters: dict[str, Any] = {
            "databases": list(databases),
            "threads": threads,
            "pipeline": self._pipeline,
            "launch_dir": self._launch_dir,
            "profile": self._profile,
            "extra_config": effective_config,
            "input_protein_count": len(proteins),
            "work_dir": work_dir_abs,
            "kept": kept,
            **params,
        }
        if gene_coords is not None:
            parameters["gene_coords"] = True
        if run_config is not None:
            parameters["run_config"] = run_config

        records = _parse_annotations_tsv(tsv_text, emitted_to_caller)

        return AnnotationResult(
            tool=_TOOL,
            tool_version=tool_version,
            db_version=",".join(databases) if databases else None,
            run_id=run_id,
            command=command,
            parameters=parameters,
            records=records,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_faa(
        self,
        path: Path,
        proteins: dict[str, str],
        gene_coords: dict[str, tuple[int, int, int]] | None = None,
    ) -> dict[str, str]:
        """Write ``{caller_id: seq}`` to *path* as a prodigal-style multi-FASTA.

        Assigns synthetic prodigal-safe ids ``g_1, g_2, ...`` (1-based index
        over *proteins* in insertion order) as the FASTA header ids.
        DRAM2's ``combine_annotations.py`` splits each id on ``_``, takes the
        last token, and ``int()``s it (the prodigal ``contig_geneNumber``
        convention); ``g_<n>`` satisfies that requirement for any caller id.

        Every header is written as::

            >g_{n} # {start} # {stop} # {strand} #

        where coordinates are looked up by the **caller id** in *gene_coords*
        (unchanged synthetic fallback: ``start=1, stop=3*len(seq), strand=1``).

        Strand is always normalised to ``1`` (forward) or ``-1`` (reverse).
        Any truthy positive value is normalised to ``1``; any negative or
        zero value is normalised to ``-1``.

        Args:
            path: Destination ``.faa`` path (created/overwritten).
            proteins: ``{caller_id: amino_acid_sequence}`` mapping.
            gene_coords: Optional ``{caller_id: (start, stop, strand)}``
                mapping supplying real genomic coordinates.

        Returns:
            ``{emitted_id: caller_id}`` reverse map covering every input
            protein in insertion order.  Callers pass this to
            ``_parse_annotations_tsv`` to translate DRAM2 output rows back
            to the original caller ids.
        """
        lines: list[str] = []
        emitted_to_caller: dict[str, str] = {}
        for n, (cid, seq) in enumerate(proteins.items(), start=1):
            emitted_id = f"g_{n}"
            emitted_to_caller[emitted_id] = cid
            seq = seq.strip()
            if gene_coords is not None and cid in gene_coords:
                start, stop, strand_raw = gene_coords[cid]
                strand = 1 if strand_raw > 0 else -1
            else:
                start = 1
                stop = 3 * len(seq)
                strand = 1
            lines.append(f">{emitted_id} # {start} # {stop} # {strand} #")
            lines.append(seq)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return emitted_to_caller

    def _build_nextflow_command(
        self,
        genes_dir: Path,
        outdir: Path,
        workdir: Path,
        databases: tuple[str, ...],
        threads: int,
        effective_config: str = "",
    ) -> list[str]:
        """Build the ``nextflow run`` argv list.

        Args:
            genes_dir: Host-side directory containing the input ``.faa``
                file (passed as ``--input_genes``).
            outdir: Absolute output directory (passed as ``--outdir``).
            workdir: Nextflow work directory (passed as ``-work-dir``).
            databases: Tuple of database short names; each becomes a
                ``--use_<name>`` flag.
            threads: Threads to pass via ``--threads``.
            effective_config: Path to an extra ``-c`` Nextflow config
                file.  When non-empty, ``-c <path>`` is injected after
                the ``-profile`` flag.  Callers should supply the
                already-resolved value (``run_config`` arg wins over
                ``self._extra_config`` when both are set).

        Returns:
            Full argv list for ``subprocess.run``.
        """
        cmd: list[str] = [
            self._nextflow_exe,
            "run",
            self._pipeline,
            "-profile", self._profile,
        ]
        if effective_config:
            cmd.extend(["-c", effective_config])
        cmd.extend([
            "--annotate",
            "--input_genes", str(genes_dir),
            "--outdir", str(outdir),
            "-work-dir", str(workdir),
            "-ansi-log", "false",
            "--threads", str(threads),
        ])
        for db in databases:
            cmd.append(f"--use_{db}")
        return cmd

    def _build_subprocess_env(self) -> dict[str, str]:
        """Build the subprocess environment for Nextflow.

        Constructs an env dict from ``os.environ`` with:
        - ``NXF_VER`` set to ``self._nxf_ver`` (default ``"24.10.5"``)
        - ``PATH`` prefixed with ``self._env_path`` (if non-empty)

        ``self._env_path`` is a single colon-separated string (NOT a list),
        e.g. ``"/scratch1/.../DRAM2/env/env_nf/bin:/scratch1/.../micromamba/bin"``.

        Returns:
            New env dict (copy of os.environ plus the overrides above).
        """
        env: dict[str, str] = {**os.environ, "NXF_VER": self._nxf_ver}
        if self._env_path:
            current_path = os.environ.get("PATH", "")
            env["PATH"] = self._env_path + os.pathsep + current_path
        return env

    def _run_nextflow(
        self,
        genes_dir: Path,
        outdir: Path,
        workdir: Path,
        databases: tuple[str, ...],
        threads: int,
        effective_config: str = "",
        run_id: str = "",
        work_root: Path | None = None,
    ) -> tuple[str, str | None, str]:
        """Run the DRAM2 Nextflow pipeline and return (tsv_text, version, cmd).

        The subprocess is launched with ``cwd = self._launch_dir`` because
        DRAM2's config resolves database paths as ``${launchDir}/databases/<db>``.
        The subprocess environment is built explicitly via ``_build_subprocess_env``
        (NXF_VER pinned; optional PATH prefix from config); ``dram2-env.sh``
        is NOT sourced.

        Args:
            genes_dir: Host-side directory containing ``input.faa``.
            outdir: Absolute output directory; ``raw-annotations.tsv`` is
                expected to land at ``outdir/RAW/raw-annotations.tsv``.
            workdir: Nextflow work directory.
            databases: Tuple of database short names.
            threads: Threads value.
            effective_config: Resolved ``-c`` config path (``run_config``
                wins over ``self._extra_config`` when both are set;
                resolution happens in ``annotate``).
            run_id: UUID hex for this run (unused here; kept for API symmetry).
            work_root: Root dir (unused here; kept for API symmetry).

        Returns:
            Tuple of (raw-annotations.tsv text, pipeline version string,
            shlex-quoted command string).  Returns empty string for the
            tsv text when the published file is absent (e.g. all genes
            yielded no hits).

        Raises:
            subprocess.CalledProcessError: when the Nextflow run exits
                non-zero.
        """
        argv = self._build_nextflow_command(
            genes_dir=genes_dir,
            outdir=outdir,
            workdir=workdir,
            databases=databases,
            threads=threads,
            effective_config=effective_config,
        )
        cmd_str = shlex.join(argv)
        env = self._build_subprocess_env()

        result = subprocess.run(
            argv,
            cwd=self._launch_dir,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, argv, result.stdout, result.stderr
            )

        tsv_path = outdir.joinpath(*_RAW_ANNOTATIONS_SUBPATH)
        tsv_text = tsv_path.read_text(encoding="utf-8") if tsv_path.exists() else ""

        tool_version = _parse_dram2_version(result.stdout + "\n" + result.stderr)
        return tsv_text, tool_version, cmd_str


# ---------------------------------------------------------------------------
# Convenience re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "DRAM2Utils",
    "_parse_annotations_tsv",
    "_parse_dram2_version",
]
