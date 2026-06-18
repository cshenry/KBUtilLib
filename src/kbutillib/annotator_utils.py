"""Shared base for annotation tool utilities in KBUtilLib.

Defines the common interface (AnnotatorUtils), return types (Term,
AnnotationRecord, AnnotationResult), and ToolUnavailableError used by
all annotation tool modules (ProkkaUtils, DRAM2Utils, TransytUtils).

Dependency direction is one-way: this module knows nothing about
genome_annotation_aggregator.  GAA depends on KBUtilLib, never the
reverse.

Alphabet guards
---------------
``_guard_dna(sequences)`` accepts IUPAC nucleotide characters
{A,C,G,T,U,R,Y,S,W,K,M,B,D,H,V,N} plus ``-`` and ``*``.

``_guard_protein(sequences)`` accepts the 20 canonical amino acids plus
{B,Z,X} (ambiguous codes) plus ``-`` and ``*``.

Both guards are case-insensitive and ignore whitespace.  A ValueError is
raised when more than 10% of the non-whitespace characters in any
sequence fall outside the allowed alphabet.

Multi-ORF tie-break (ProkkaUtils caveat — documented here for
cross-module reference)
----------------------------------------------------------------------
When a single-gene contig yields multiple CDS rows in PROKKA's .tsv,
the *longest* CDS by length_bp is selected; ties are broken by the
smallest start coordinate.  This rule is implemented in ProkkaUtils but
is recorded here so downstream consumers understand the choice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .shared_env_utils import SharedEnvUtils

# ---------------------------------------------------------------------------
# Alphabet definitions
# ---------------------------------------------------------------------------

# IUPAC nucleotide: standard bases + ambiguity codes + gap/stop
_DNA_ALPHABET: frozenset[str] = frozenset(
    "ACGTURYSWKMBDHVNacgturysWkmBDHVN-*"
)
# Full IUPAC DNA set (case-insensitive)
_DNA_CHARS: frozenset[str] = frozenset(
    "ACGTURYSWKMBDHVN-*"
)

# Protein: 20 AA + ambiguous B/Z/X + gap/stop
_PROTEIN_CHARS: frozenset[str] = frozenset(
    "ACDEFGHIKLMNPQRSTVWYBZX-*"
)


# ---------------------------------------------------------------------------
# Return-type dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Term:
    """A single functional annotation term from a tool.

    Attributes:
        namespace: Controlled vocabulary namespace (e.g. "EC", "KO", "TC",
            "MSRXN", "MSCPD", "GENE", "COG") or None for free-text.
        id: The term identifier within the namespace, or None.
        value: The human-readable value or free-text string.
        evidence: Tool-specific evidence dict (score, e-value, inference,
            …).  May be empty.
    """

    namespace: str | None
    id: str | None
    value: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnnotationRecord:
    """Annotation result for a single gene / protein.

    Attributes:
        gene_id: The caller's original sequence id (preserved exactly).
        terms: List of functional annotation terms emitted for this
            sequence.
    """

    gene_id: str
    terms: list[Term] = field(default_factory=list)


@dataclass
class AnnotationResult:
    """Container for one annotation run over a set of sequences.

    Attributes:
        tool: Short tool name (e.g. "prokka", "dram2", "transyt").
        tool_version: Version string reported by the tool, or None.
        db_version: Database/reference version string, or None.
        run_id: Unique run identifier (uuid4 hex).
        command: The exact command line / docker invocation, shlex-quoted.
        parameters: Dict of resolved parameter values used for the run
            (includes defaults).
        records: List of per-gene annotation records; genes with zero
            called annotations may be absent.
    """

    tool: str
    tool_version: str | None
    db_version: str | None
    run_id: str
    command: str
    parameters: dict[str, Any]
    records: list[AnnotationRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ToolUnavailableError(Exception):
    """Raised when an annotation tool is not available on this system.

    Attributes:
        tool: Short tool name (e.g. "prokka").
        detail: Human-readable explanation of why the tool is unavailable.
    """

    def __init__(self, tool: str, detail: str, hint: str = "") -> None:
        self.tool = tool
        self.detail = detail
        self.hint = hint
        message = f"{tool} not available: {detail}."
        if hint:
            message += f" Install: {hint}"
        super().__init__(message)


# ---------------------------------------------------------------------------
# Alphabet guard helpers
# ---------------------------------------------------------------------------


def _check_alphabet(
    sequences: dict[str, str],
    allowed: frozenset[str],
    label: str,
) -> None:
    """Raise ValueError if >10% of non-whitespace chars are out-of-alphabet.

    The check is case-insensitive; whitespace is ignored.

    Args:
        sequences: Mapping of id → sequence string.
        allowed: Frozenset of allowed uppercase characters (the check
            converts each char to upper before membership testing).
        label: Short label used in the error message ("DNA" / "protein").

    Raises:
        ValueError: When any sequence exceeds the 10% out-of-alphabet
            threshold.
    """
    ws_re = re.compile(r"\s")
    for seq_id, seq in sequences.items():
        stripped = ws_re.sub("", seq)
        if not stripped:
            continue
        out = sum(1 for ch in stripped if ch.upper() not in allowed)
        fraction = out / len(stripped)
        if fraction > 0.10:
            raise ValueError(
                f"Sequence '{seq_id}' has {fraction:.1%} characters outside the "
                f"{label} alphabet ({out}/{len(stripped)} chars). "
                f"Expected {label} sequences."
            )


def _guard_dna(sequences: dict[str, str]) -> None:
    """Validate that sequences look like DNA/nucleotide input.

    Accepts IUPAC nucleotide characters
    {A,C,G,T,U,R,Y,S,W,K,M,B,D,H,V,N} plus ``-`` and ``*``.
    Case-insensitive; whitespace ignored.

    Raises:
        ValueError: If >10% of characters are outside the DNA alphabet.
    """
    _check_alphabet(sequences, _DNA_CHARS, "DNA")


def _guard_protein(sequences: dict[str, str]) -> None:
    """Validate that sequences look like amino-acid (protein) input.

    Accepts the 20 canonical amino acids plus {B,Z,X} (ambiguous codes)
    plus ``-`` and ``*``.  Case-insensitive; whitespace ignored.

    Raises:
        ValueError: If >10% of characters are outside the protein
            alphabet.
    """
    _check_alphabet(sequences, _PROTEIN_CHARS, "protein")


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class AnnotatorUtils(SharedEnvUtils):
    """Abstract base class for annotation tool utilities.

    All concrete annotation tool utilities (ProkkaUtils, DRAM2Utils,
    TransytUtils) inherit from this class and implement:

    * ``is_available(self) -> bool`` — side-effect-free probe.
    * ``annotate(self, sequences, **params) -> AnnotationResult``

    The base class provides ``_require_available()`` which calls
    ``is_available()`` and raises ``ToolUnavailableError`` when the tool
    is not present.

    Tool-specific install hints and tool names are set by each subclass
    via the ``_tool_name`` and ``_install_hint`` class-level attributes.

    Example::

        class ProkkaUtils(AnnotatorUtils):
            _tool_name = "prokka"
            _install_hint = "conda install -c bioconda prokka"

            def is_available(self) -> bool:
                ...

            def annotate(self, sequences, **params):
                self._require_available()
                ...
    """

    #: Short tool name used in error messages; override in subclasses.
    _tool_name: str = "annotator"

    #: Human-readable install hint; override in subclasses.
    _install_hint: str = ""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    # ------------------------------------------------------------------
    # Abstract interface (subclasses must override)
    # ------------------------------------------------------------------

    def is_available(self) -> bool:  # pragma: no cover
        """Return True if the underlying tool is installed and runnable.

        This method must be side-effect-free: no logging, no mutations,
        no disk writes.  It may probe the system (e.g. ``shutil.which``,
        ``subprocess.run``) but must return a plain bool.

        Subclasses must implement this method.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.is_available() is not implemented."
        )

    def annotate(
        self,
        sequences: dict[str, str],
        **params: Any,
    ) -> AnnotationResult:  # pragma: no cover
        """Annotate a set of sequences and return structured results.

        Args:
            sequences: Mapping of ``{id: sequence_string}``.  The
                molecule type (nucleotide vs amino-acid) is determined by
                the concrete subclass.
            **params: Tool-specific keyword parameters.

        Returns:
            An ``AnnotationResult`` whose ``records`` are keyed by the
            caller's ids (preserved exactly from *sequences*).

        Raises:
            ToolUnavailableError: If the tool is not installed.
            ValueError: If the input sequences fail the alphabet guard.

        Subclasses must implement this method.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.annotate() is not implemented."
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _require_available(self) -> None:
        """Raise ToolUnavailableError if is_available() returns False.

        Raises:
            ToolUnavailableError: With message format
                ``"{tool} not available: {detail}. Install: {hint}"``.
        """
        if not self.is_available():
            raise ToolUnavailableError(
                tool=self._tool_name,
                detail=f"{self._tool_name} is not installed or not on PATH",
                hint=self._install_hint,
            )
