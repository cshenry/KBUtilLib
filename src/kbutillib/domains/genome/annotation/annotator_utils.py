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

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from kbutillib.core.shared_env_utils import SharedEnvUtils

from .ontology_dictionary import OntologyDictionary

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
        version_kind: What ``tool_version`` actually identifies --
            ``"tool-version"`` (a release of the tool, e.g. ``"1.14.6"``),
            ``"artifact-digest"`` (a content hash of the artifact that ran,
            e.g. ``"kbutillib/transyt@sha256:<64 hex>"``), or ``None``
            meaning ``tool_version`` (if any) does NOT identify the tool
            version -- it is a path, a floating tag, a runner version, or
            absent.  See :func:`classify_version`.
        version_asserted_by: ``None`` when ``tool_version`` was OBSERVED
            (read back out of the tool or the image).  Otherwise a dict
            ``{"who": str, "date": str | None, "basis": str}`` naming who
            asserted the value and on what grounds, for a version the code
            cannot observe (see ``rast_utils._DEFAULT_KMER_DATASET``).
    """

    tool: str
    tool_version: str | None
    db_version: str | None
    run_id: str
    command: str
    parameters: dict[str, Any]
    records: list[AnnotationRecord] = field(default_factory=list)
    #: See the class docstring.  Added with a default so every existing
    #: positional or keyword construction keeps working unchanged.
    version_kind: str | None = None
    version_asserted_by: dict | None = None


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
# Ontology-description formatting (shared by BaktaUtils / KofamscanUtils)
# ---------------------------------------------------------------------------


def docker_image_identity(image: str, *, timeout: float = 10.0) -> str:
    """Return an identifying provenance string for a local Docker *image*.

    A bare tag is not an identity. ``kbutillib/kofamscan:latest`` names a
    different set of bytes before and after a rebuild, so recording it as a
    tool version says nothing about what actually ran. This pins the tag to
    the image's content hash.

    Resolution order, most to least portable:

    1. ``<image>@<RepoDigest>`` -- the registry digest. Only present for an
       image that was pulled from, or pushed to, a registry.
    2. ``<image>@<Id>`` -- the local image id. Every image has one, including
       images built locally and never pushed.
    3. ``<image>`` -- the bare tag, when ``docker image inspect`` cannot be
       run at all (no docker binary, timeout, image absent).

    Step 2 is not an optimisation; it is the case that actually applies here.
    Every ``kbutillib/*`` image on poplar is built locally and never pushed,
    so ``RepoDigests`` is an EMPTY list for all of them and a lone
    ``{{index .RepoDigests 0}}`` template fails with "slice index out of
    range" (measured 2026-10-15 against ``kbutillib/transyt:latest``). A
    RepoDigests-only implementation therefore degrades to the bare tag on
    exactly the images we care about.

    Args:
        image: Docker image reference, e.g. ``"kbutillib/bakta:latest"``.
        timeout: Seconds to allow ``docker image inspect``.

    Returns:
        The identity string. Never raises -- provenance must not fail a run.
    """
    if not image:
        return ""
    for field_template in ("{{index .RepoDigests 0}}", "{{.Id}}"):
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", "--format", field_template, image],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return image
        if result.returncode == 0:
            value = result.stdout.strip()
            if value:
                # A RepoDigest is already "<repo>@sha256:...", so returning it
                # verbatim avoids "<image>@<repo>@sha256:...".
                return value if "@" in value else f"{image}@{value}"
    return image


# A full digest reference: "<artifact>@sha256:<64 hex>".  Anchored at both
# ends by :func:`re.fullmatch` below -- a PREFIX match would accept
# "img@sha256:<64 hex>-dirty" and a 65-hex string, both of which name nothing
# resolvable.
_DIGEST_RE = re.compile(r".+@sha256:[0-9a-fA-F]{64}")


def classify_version(value: str | None, *, from_image: bool) -> str | None:
    """Classify what a ``tool_version`` string actually identifies.

    Callers of :class:`AnnotationResult` currently get one free-form
    version string that mixes three unrelated things: a semver tool
    release, an image content digest, and strings that identify neither
    (an executable path, a floating tag, a *runner* version).  This
    returns the ``version_kind`` for a value so a consumer can tell them
    apart instead of guessing from the shape of the string.

    Rules:

    * ``None``, empty, or whitespace-only -> ``None``.
    * ``from_image=True`` (the value came from
      :func:`docker_image_identity`): a FULL match of
      ``<artifact>@sha256:<exactly 64 hex>`` -> ``"artifact-digest"``.
      Anything else -> ``None``.  That "anything else" is the case that
      matters: ``docker_image_identity`` degrades to the bare tag when
      ``docker image inspect`` cannot be run, and a bare tag such as
      ``kbutillib/transyt:latest`` names a different set of bytes after
      every rebuild.
    * ``from_image=False``: a value starting with ``"nextflow/"`` ->
      ``None`` (that is the RUNNER's version, not the tool's -- see
      ``dram2_utils._parse_nextflow_engine_version``); an absolute
      filesystem path -> ``None`` (an executable path is not a version);
      anything else -> ``"tool-version"``.

    SCOPE.  This covers the formats the wired annotators actually emit.
    It is not a general-purpose version validator and does not judge
    arbitrary strings: ``from_image=False`` returns ``"tool-version"`` for
    any value that is not a runner label or an absolute path, including
    strings that are not versions at all.  Widening it means adding the
    new producer's format here, deliberately.

    Args:
        value: The ``tool_version`` string the annotator is about to
            record, or None.
        from_image: True when *value* came from a Docker image identity
            (i.e. from :func:`docker_image_identity`) rather than from the
            tool reporting its own version.

    Returns:
        ``"tool-version"``, ``"artifact-digest"``, or None.  Never raises
        -- provenance classification must not fail a run.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    if from_image:
        return "artifact-digest" if _DIGEST_RE.fullmatch(value) else None
    if value.startswith("nextflow/"):
        return None
    if os.path.isabs(value):
        return None
    return "tool-version"


def describe_or_accession(
    namespace: str,
    accession: str,
    ontology_dictionary: OntologyDictionary | None,
) -> str:
    """Return ``"<accession>: <description>"``, degrading to the bare accession.

    Looks up *accession* within *namespace* (one of ``"KO"``, ``"EC"``,
    ``"GO"``, ``"COG"``) via *ontology_dictionary*. Never raises: degrades
    to the bare *accession* when *ontology_dictionary* is ``None``, when
    the namespace's staged dictionary file is itself absent (see
    ``OntologyDictionary``'s own missing-file degradation), or when
    *accession* simply has no description on file.

    Uses exactly one ``": "`` separator after the accession. GO accessions
    (e.g. ``"GO:0006260"``) already embed a colon in the accession itself,
    which is why consumers must split on the *first* ``": "``
    (colon-space), not the first colon, to recover the accession.

    Args:
        namespace: One of the four ``OntologyDictionary.NAMESPACES``.
        accession: The accession to describe (e.g. ``"K00001"``,
            ``"1.1.1.1"``, ``"GO:0006260"``, ``"COG0001"``).
        ontology_dictionary: The dictionary to look up *accession* in, or
            ``None`` when no dictionary is available for this run.

    Returns:
        ``"<accession>: <description>"`` when a description is found,
        otherwise the bare *accession*.
    """
    description = (
        ontology_dictionary.describe(namespace, accession)
        if ontology_dictionary is not None
        else None
    )
    if description:
        return f"{accession}: {description}"
    return accession


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
