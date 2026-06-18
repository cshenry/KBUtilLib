"""PROKKA annotation utilities for KBUtilLib.

Implements ProkkaUtils, which wraps the PROKKA prokaryotic genome annotator
(https://github.com/tseemann/prokka) to annotate a caller-supplied set of
nucleotide CDS sequences.

Design: emulates the KBase ``kb_prokka`` genome re-annotation trick.
Each caller gene is written as its own single-gene contig (one FASTA
record) using a deterministic, PROKKA-safe internal id (``g{index}``).
PROKKA re-calls ORFs inside each contig, then the functional annotations
(product, EC, gene, COG) are mapped back to the caller's original ids.

Multi-ORF tie-break
-------------------
If PROKKA splits a single-gene contig into multiple CDS entries, the
**longest CDS by ``length_bp``** is selected; ties are broken by the
**smallest start coordinate** from the GFF.  The longest ORF is chosen
because it is the most complete and informative call.

Zero-ORF genes
--------------
Input genes for which PROKKA finds no CDS are silently absent from
``records`` in the returned ``AnnotationResult``.  This is not an error.

Long caller ids
---------------
Unlike the KBase app (which aborts when any feature id exceeds 32 chars),
ProkkaUtils internally remaps all caller ids to ``g{index}`` safe ids
before writing the FASTA.  The remap is reversed when building the result,
so callers always receive their original ids back.
"""

from __future__ import annotations

import re
import shlex
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
    _guard_dna,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_KINGDOMS: frozenset[str] = frozenset({"Bacteria", "Archaea", "Viruses"})

# Multi-value delimiter pattern for EC_number and COG fields in PROKKA TSV.
# PROKKA uses semicolons; we also handle commas and whitespace runs.
_MULTI_VAL_RE = re.compile(r"[;,\s]+")


# ---------------------------------------------------------------------------
# Pure parse helpers (unit-testable offline)
# ---------------------------------------------------------------------------


def _parse_gff_locus_map(gff_text: str) -> dict[str, tuple[str, int]]:
    """Parse a PROKKA GFF and return a mapping from locus_tag to (safe_id, start).

    Only CDS feature lines (column 3 == "CDS") are processed.  The first
    column is the sequence name (safe_id / contig id); the start coordinate
    is column 4 (1-based, inclusive).  The ``locus_tag`` attribute is
    extracted from the attribute column (column 9).

    Args:
        gff_text: Full text content of the PROKKA ``.gff`` file.

    Returns:
        Mapping ``{locus_tag: (safe_id, start)}`` for every CDS line that
        carries a ``locus_tag`` attribute.
    """
    result: dict[str, tuple[str, int]] = {}
    for line in gff_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # GFF3: seqname source feature start end score strand frame attributes
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        if parts[2] != "CDS":
            continue
        safe_id = parts[0]
        try:
            start = int(parts[3])
        except ValueError:
            continue
        attributes = parts[8]
        # Extract locus_tag=... from attribute string
        m = re.search(r"locus_tag=([^;]+)", attributes)
        if not m:
            continue
        locus_tag = m.group(1)
        result[locus_tag] = (safe_id, start)
    return result


def _parse_tsv(
    tsv_text: str,
    locus_to_safe_start: dict[str, tuple[str, int]],
    safe_to_caller: dict[str, str],
) -> list[AnnotationRecord]:
    """Parse a PROKKA ``.tsv`` file and return a list of AnnotationRecords.

    Only rows with ``ftype == "CDS"`` are processed.  When multiple CDS rows
    map to the same safe_id (i.e. the same input gene), the **longest CDS
    by ``length_bp``** is selected; ties are broken by the **smallest start
    coordinate** (from *locus_to_safe_start*).

    Field mapping:
    - ``product``    → ``Term(namespace=None, id=None, value=product)``
    - ``EC_number``  → one ``Term("EC", ec, ec)`` per value after splitting
      on ``;``, ``,``, or whitespace.
    - ``gene``       → ``Term("GENE", gene, gene)``
    - ``COG``        → one ``Term("COG", cog, cog)`` per value after
      splitting.

    Args:
        tsv_text: Full text content of the PROKKA ``.tsv`` file.
        locus_to_safe_start: Mapping ``{locus_tag: (safe_id, start)}``
            as returned by :func:`_parse_gff_locus_map`.
        safe_to_caller: Mapping ``{safe_id: caller_id}`` for reversing
            the internal id remap.

    Returns:
        List of :class:`AnnotationRecord` instances, one per caller id that
        yielded at least one CDS.  Caller ids with zero CDS are absent.
    """
    lines = tsv_text.splitlines()
    if not lines:
        return []

    header = lines[0].split("\t")
    col = {name: idx for idx, name in enumerate(header)}

    # Per safe_id: list of (length_bp, start, row_dict) candidates
    candidates: dict[str, list[tuple[int, int, dict[str, str]]]] = {}

    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split("\t")
        # Pad to header length with empty strings
        while len(parts) < len(header):
            parts.append("")

        ftype = parts[col["ftype"]].strip() if "ftype" in col else ""
        if ftype != "CDS":
            continue

        locus_tag = parts[col["locus_tag"]].strip() if "locus_tag" in col else ""
        if locus_tag not in locus_to_safe_start:
            continue

        safe_id, start = locus_to_safe_start[locus_tag]
        if safe_id not in safe_to_caller:
            continue

        try:
            length_bp = int(parts[col["length_bp"]].strip()) if "length_bp" in col else 0
        except ValueError:
            length_bp = 0

        row = {h: parts[i].strip() for i, h in enumerate(header) if i < len(parts)}
        candidates.setdefault(safe_id, []).append((length_bp, start, row))

    # Apply tie-break: longest length_bp, then smallest start
    records: list[AnnotationRecord] = []
    for safe_id, rows in candidates.items():
        caller_id = safe_to_caller[safe_id]
        # Sort: primary key = -length_bp (largest first), secondary = start (smallest first)
        rows.sort(key=lambda t: (-t[0], t[1]))
        best_row = rows[0][2]

        terms = _row_to_terms(best_row)
        records.append(AnnotationRecord(gene_id=caller_id, terms=terms))

    return records


def _row_to_terms(row: dict[str, str]) -> list[Term]:
    """Convert a single parsed TSV row dict into a list of Terms.

    Args:
        row: Dict mapping column name → value string.

    Returns:
        List of Term instances (product, EC, gene, COG as applicable).
        Empty fields are skipped.
    """
    terms: list[Term] = []

    product = row.get("product", "").strip()
    if product:
        terms.append(Term(namespace=None, id=None, value=product, evidence={}))

    ec_raw = row.get("EC_number", "").strip()
    if ec_raw:
        for ec in _MULTI_VAL_RE.split(ec_raw):
            ec = ec.strip()
            if ec:
                terms.append(Term(namespace="EC", id=ec, value=ec, evidence={}))

    gene_val = row.get("gene", "").strip()
    if gene_val:
        terms.append(
            Term(namespace="GENE", id=gene_val, value=gene_val, evidence={})
        )

    cog_raw = row.get("COG", "").strip()
    if cog_raw:
        for cog in _MULTI_VAL_RE.split(cog_raw):
            cog = cog.strip()
            if cog:
                terms.append(Term(namespace="COG", id=cog, value=cog, evidence={}))

    return terms


# ---------------------------------------------------------------------------
# Main utility class
# ---------------------------------------------------------------------------


class ProkkaUtils(AnnotatorUtils):
    """Annotation utilities backed by the PROKKA genome annotator.

    Emulates the KBase ``kb_prokka`` genome re-annotation workaround:
    each caller-supplied nucleotide CDS is written as its own single-gene
    contig, PROKKA re-calls ORFs within it, and the functional annotations
    (product, EC, gene, COG) are mapped back to the caller's original ids.

    Availability probe
    ------------------
    ``is_available()`` runs ``prokka --version`` and returns True only if
    the exit code is 0.  The method is side-effect-free.

    Example::

        utils = ProkkaUtils()
        if utils.is_available():
            result = utils.annotate({"gene_001": "ATGAAACCC..."}, gcode=11)
            for rec in result.records:
                print(rec.gene_id, [t.value for t in rec.terms])
    """

    _tool_name: str = "prokka"
    _install_hint: str = "conda install -c bioconda prokka"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._prokka_exe: str = self.get_config_value(
            "prokka.executable", default="prokka"
        )

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if ``prokka`` is on PATH and exits successfully.

        Side-effect-free: no logging, no mutation.

        Returns:
            True if ``prokka --version`` exits with code 0, False otherwise.
        """
        try:
            result = subprocess.run(
                [self._prokka_exe, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    # ------------------------------------------------------------------
    # Public annotate method
    # ------------------------------------------------------------------

    def annotate(  # type: ignore[override]
        self,
        sequences: dict[str, str],
        gcode: int = 11,
        kingdom: str | None = None,
        threads: int = 1,
        **params: Any,
    ) -> AnnotationResult:
        """Annotate nucleotide CDS sequences using PROKKA.

        Writes one single-gene-contig FASTA record per input gene, runs
        PROKKA over the combined FASTA, then parses the ``.tsv`` (CDS rows)
        and maps annotations back to the caller's gene ids.

        Args:
            sequences: Mapping ``{caller_id: nucleotide_cds_string}``.
                Values must be nucleotide sequences (IUPAC alphabet).
                Sequences that look like proteins (>10% non-DNA chars) raise
                ``ValueError``.
            gcode: Genetic code for Prodigal.  Default 11 (Bacteria/Archaea).
            kingdom: PROKKA ``--kingdom`` flag.  Must be one of
                ``{"Bacteria", "Archaea", "Viruses"}`` or ``None`` (omit
                flag).  Raises ``ValueError`` for other values.
            threads: Number of CPUs passed to ``--cpus``.  Default 1.
            **params: Ignored extra keyword arguments (for API compatibility).

        Returns:
            An ``AnnotationResult`` with:
            - ``tool = "prokka"``
            - ``records`` keyed by the caller's original ids.
            - Genes with zero called ORFs are absent from ``records``.

        Raises:
            ToolUnavailableError: If ``prokka`` is not on PATH.
            ValueError: If the input sequences fail the DNA alphabet guard,
                or if *kingdom* is not in the allowed set.
            subprocess.CalledProcessError: If PROKKA exits non-zero.
        """
        # Validate kingdom first (before availability check)
        if kingdom is not None and kingdom not in _VALID_KINGDOMS:
            raise ValueError(
                f"kingdom must be one of {sorted(_VALID_KINGDOMS)} or None, "
                f"got {kingdom!r}"
            )

        self._require_available()

        # Guard: input must be nucleotide
        _guard_dna(sequences)

        # Build safe-id remap: g{index} → caller_id
        caller_ids = list(sequences.keys())
        safe_to_caller: dict[str, str] = {
            f"g{i}": cid for i, cid in enumerate(caller_ids)
        }
        caller_to_safe: dict[str, str] = {v: k for k, v in safe_to_caller.items()}

        run_id = uuid.uuid4().hex

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # Write single-gene-contig FASTA
            fasta_path = tmp / "input.fasta"
            fasta_lines: list[str] = []
            for caller_id, seq in sequences.items():
                safe_id = caller_to_safe[caller_id]
                fasta_lines.append(f">{safe_id}")
                fasta_lines.append(seq)
            fasta_path.write_text("\n".join(fasta_lines) + "\n", encoding="utf-8")

            # Run PROKKA
            tsv_text, gff_text, tool_version, db_version, command = self._run_prokka(
                fasta_path=fasta_path,
                outdir=tmp / "prokka_out",
                gcode=gcode,
                kingdom=kingdom,
                threads=threads,
            )

        # Parse
        locus_map = _parse_gff_locus_map(gff_text)
        records = _parse_tsv(tsv_text, locus_map, safe_to_caller)

        parameters: dict[str, Any] = {
            "gcode": gcode,
            "kingdom": kingdom,
            "threads": threads,
            "remapped_ids": len(caller_ids),
        }

        return AnnotationResult(
            tool="prokka",
            tool_version=tool_version,
            db_version=db_version,
            run_id=run_id,
            command=command,
            parameters=parameters,
            records=records,
        )

    # ------------------------------------------------------------------
    # Internal subprocess runner
    # ------------------------------------------------------------------

    def _run_prokka(
        self,
        fasta_path: Path,
        outdir: Path,
        gcode: int,
        kingdom: str | None,
        threads: int,
    ) -> tuple[str, str, str | None, str | None, str]:
        """Run PROKKA and return (tsv_text, gff_text, tool_version, db_version, command).

        Args:
            fasta_path: Path to the input multi-FASTA file.
            outdir: Directory for PROKKA output (will be created if absent).
            gcode: Genetic code integer.
            kingdom: Optional kingdom string (None to omit flag).
            threads: CPU count.

        Returns:
            Tuple of (tsv_text, gff_text, tool_version, db_version, command_string).
            ``tool_version`` and ``db_version`` may be None if not parseable.

        Raises:
            subprocess.CalledProcessError: If PROKKA exits non-zero.
        """
        outdir.mkdir(parents=True, exist_ok=True)

        cmd: list[str] = [
            self._prokka_exe,
            "--outdir", str(outdir),
            "--prefix", "prokka",
            "--gcode", str(gcode),
            "--cpus", str(threads),
            "--force",
            "--quiet",
        ]
        if kingdom is not None:
            cmd.extend(["--kingdom", kingdom])
        cmd.append(str(fasta_path))

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

        # Read outputs
        tsv_path = outdir / "prokka.tsv"
        gff_path = outdir / "prokka.gff"

        tsv_text = tsv_path.read_text(encoding="utf-8") if tsv_path.exists() else ""
        gff_text = gff_path.read_text(encoding="utf-8") if gff_path.exists() else ""

        # Parse version from stderr (prokka --version writes to stderr)
        tool_version = self._parse_prokka_version(result.stderr)
        db_version = self._parse_db_version(result.stderr)

        return tsv_text, gff_text, tool_version, db_version, command_str

    def _parse_prokka_version(self, stderr: str) -> str | None:
        """Extract PROKKA version from process stderr.

        Prokka writes its version in the log lines like::

            [HH:MM:SS] This is prokka 1.14.6

        Args:
            stderr: Captured stderr text from the PROKKA run.

        Returns:
            Version string (e.g. ``"1.14.6"``) or None if not found.
        """
        m = re.search(r"This is prokka\s+(\S+)", stderr)
        if m:
            return m.group(1)
        # Fallback: probe via --version
        try:
            vr = subprocess.run(
                [self._prokka_exe, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # prokka --version writes to stderr: "prokka 1.14.6"
            for line in (vr.stderr + vr.stdout).splitlines():
                m2 = re.search(r"prokka\s+(\d+\.\d+\S*)", line, re.IGNORECASE)
                if m2:
                    return m2.group(1)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return None

    def _parse_db_version(self, stderr: str) -> str | None:
        """Extract database version info from PROKKA stderr, best-effort.

        Prokka logs DB version lines like::

            [HH:MM:SS] Databases: …

        Args:
            stderr: Captured stderr text from the PROKKA run.

        Returns:
            A db version string or None if unavailable.
        """
        m = re.search(r"Databases:\s*(.+)", stderr)
        if m:
            return m.group(1).strip()
        return None
