"""BAKTA annotation utilities for KBUtilLib.

Implements ``BaktaUtils``, which wraps Bakta's **protein** entry point
(``bakta_proteins``, https://github.com/oschwengers/bakta) to annotate a
caller-supplied set of amino-acid (protein) sequences against a locally
registered Bakta reference database.

``annotate()`` is deliberately the ``bakta_proteins`` route, not the
whole-contig ``bakta`` nucleotide pipeline — the two are different input
shapes.

Structural gene calling (``call_features``)
-------------------------------------------
``BaktaUtils.call_features`` is the second, independent entry point: it
takes **contigs** and returns **structure only** — CDS, tRNA, tmRNA, rRNA,
ncRNA genes and CRISPR arrays with coordinates, strand and sequence, and no
functional annotation whatsoever.

It cannot be done by shelling out to the ``bakta`` CLI. In Bakta 1.11.4's
``bakta/main.py`` the CDS branch is one straight-line block (lines 238-351):
once ``--skip-cds`` is off it *always* runs UPS/IPS lookup, the PSC/PSCC
Diamond searches, AMRFinderPlus, the expert Diamond systems, pseudogene
detection and Pfam. There is no flag that stops after prediction. So the
work is driven from inside Bakta's Python API by a standalone driver,
``bakta_call_features.py``, which lives beside this module, is shipped as
package data, imports only ``bakta`` and the standard library, and is
bind-mounted read-only into the container and *run* — never imported by
KBUtilLib, whose environment has no Bakta. Read that file's docstring for
the exact mirrored call order and the list of ``main.py`` steps it
deliberately omits.

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

Per the PRD's originally resolved spec (D4), only the free-text ``product``
string was emitted as a ``Term``:

    Term(namespace="FUNCTION", id=None, value=<product>, evidence={...})

That FUNCTION-destined term is still emitted exactly this way, byte-for-byte
— reactions are reached downstream by joining this text against mapping
tables keyed on it, so it is never rewritten. D4's confinement of the
``psc`` ontology accessions to ``Term.evidence`` only has since been
**deliberately reversed** (see the ``kbdl-ontology-descriptions-v1`` PRD,
task ``annotator-description-emission``): Bakta's per-gene record also
carries ``gene``, and, nested under ``psc``, ``ec_ids``,
``kegg_orthology_id``, ``cog_id`` and ``go_ids``. These are now surfaced
**additionally** as their own namespaced, described Terms —

    Term(namespace="EC"|"KO"|"COG"|"GO", id=<accession>,
         value="<accession>: <description>")

— described via an injected ``OntologyDictionary`` (degrading to the bare
accession when no description is available), alongside a
``Term(namespace="role", ...)`` per ModelSEED role an EC accession resolves
to via an injected ``EcRoleResolver``. None of this touches the FUNCTION
term or its evidence dict: ``ec_ids``, ``kegg_orthology_id``, ``cog_id`` and
``go_ids`` continue to be carried in ``Term.evidence`` too (alongside
``aa_hexdigest`` when present), so nothing is silently dropped there either.

The payload's own version stamp, ``version = {"bakta": ..., "db":
{"version": ..., "type": ...}}``, is the source for
``AnnotationResult.tool_version`` / ``db_version``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kbutillib.domains.modeling.ec_role_resolver import EcRoleResolver

from .annotator_utils import (
    AnnotationRecord,
    AnnotationResult,
    AnnotatorUtils,
    Term,
    ToolUnavailableError,
    _guard_dna,
    _guard_protein,
    describe_or_accession,
)
from .ontology_dictionary import OntologyDictionary

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOOL = "bakta"
_INSTALL_HINT = (
    "Build the kbutillib/bakta Docker image (docker/bakta/Dockerfile) and set "
    "bakta.docker_image in config.yaml, or install bakta_proteins on PATH. "
    "Requires a registered Bakta database directory containing version.json."
)

# psc.* fields carried in Term.evidence (unconditionally, as before) AND now
# additionally surfaced as their own namespaced, described Terms (see module
# docstring) -- the PRD's deliberate reversal of D4's "evidence only" rule.
_EVIDENCE_ONLY_PSC_FIELDS: tuple[str, ...] = (
    "ec_ids",
    "kegg_orthology_id",
    "cog_id",
    "go_ids",
)

# psc.* field -> the ontology namespace its accessions belong to.
_PSC_FIELD_NAMESPACE: dict[str, str] = {
    "ec_ids": "EC",
    "kegg_orthology_id": "KO",
    "cog_id": "COG",
    "go_ids": "GO",
}

# ---------------------------------------------------------------------------
# Structural gene calling (call_features) constants
# ---------------------------------------------------------------------------

#: Name of the sibling driver module executed inside the Bakta image. It is
#: a file to be *run*, never a module to be imported -- it imports ``bakta``
#: at call time and KBUtilLib's own environment has no Bakta.
_DRIVER_FILENAME = "bakta_call_features.py"

#: Path the driver is bind-mounted at inside the container (read-only).
_DRIVER_CONTAINER_PATH = "/driver/bakta_call_features.py"

#: The Bakta release the driver is pinned to. Kept in step with
#: ``bakta_call_features.PINNED_BAKTA_VERSION`` -- duplicated rather than
#: imported because importing the driver would pull in ``bakta``.
_DRIVER_PINNED_BAKTA_VERSION = "1.11.4"

#: The driver's dedicated exit code for a Bakta version mismatch.
_DRIVER_EXIT_VERSION_MISMATCH = 3

#: Bakta feature types a structural call can produce
#: (``bakta.constants.FEATURE_*``; note ``cds`` is lowercase there).
_CALL_FEATURE_TYPES: frozenset[str] = frozenset(
    {"cds", "tRNA", "tmRNA", "rRNA", "ncRNA", "crispr"}
)

#: Bakta's three structural discard types
#: (``bakta.constants.DISCARD_TYPE_{OVERLAP,SPURIOUS,LENGTH}``).
_DISCARD_TYPES: tuple[str, ...] = ("overlap", "spurious", "length")

#: The only feature type permitted a ``?`` strand. Bakta's CRISPR caller
#: sets ``STRAND_UNKNOWN`` unconditionally
#: (``bakta/features/crispr.py`` 1.11.4 line 75) because a CRISPR array has
#: no coding direction; every other structural caller resolves ``+``/``-``.
_STRAND_UNKNOWN_TYPES: frozenset[str] = frozenset({"crispr"})

#: Bakta's truncation markers (``bakta.constants.FEATURE_END_*``). The value
#: names the *biological* end that is truncated and is already
#: strand-resolved by Bakta (``bakta/features/cds.py`` 1.11.4 lines
#: 118-124), so "5-prime" means the start codon is missing on either strand.
_TRUNCATED_5_PRIME = "5-prime"
_TRUNCATED_3_PRIME = "3-prime"
_TRUNCATED_BOTH = "both"

#: Number of trailing stderr characters quoted in a BaktaCallError.
_STDERR_TAIL_CHARS = 2000


class BaktaCallError(RuntimeError):
    """Raised when a structural Bakta gene-calling run fails or lies.

    Covers three distinct failures, deliberately under one type because a
    caller's recourse is the same for all three (re-run, or fix the input):

    * the driver exited non-zero -- including exit
      :data:`_DRIVER_EXIT_VERSION_MISMATCH`, where the message names both
      the pinned and the discovered Bakta version;
    * the driver's output was absent or not parseable JSON;
    * the output's ``contig_ids`` were not exactly the input contig ids,
      which would silently misattribute every coordinate in the result.
    """


@dataclass(frozen=True)
class BaktaCallResult:
    """Result of :meth:`BaktaUtils.call_features` -- structure, no function.

    Attributes:
        features: The KEPT features only, one dict each. Always carries
            ``type`` (one of :data:`_CALL_FEATURE_TYPES`), ``contig_id``,
            ``start``, ``stop`` (1-based inclusive, ``start <= stop``),
            ``strand``, ``nt``, ``product`` and ``partial``. CDS
            additionally carry ``aa`` (trailing ``*`` stripped),
            ``start_complete`` and ``stop_complete``. ``amino_acid``,
            ``anticodon``, ``rfam``, ``ncrna_class`` and ``score`` appear
            only on the feature types that have them. GO ``db_xrefs`` are
            dropped -- only the Rfam accession survives from ``db_xrefs``.
        discarded: ``{"counts": {"overlap": n, "spurious": n, "length": n},
            "cds": [...]}``. ``counts`` tallies EVERY discarded feature by
            Bakta's discard type -- including a tRNA dropped for overlapping
            a tmRNA, which would otherwise vanish without trace. ``cds``
            enumerates the discarded CDS only, each as
            ``{contig_id, start, stop, strand, reason, overlap_type,
            overlap_start, overlap_stop}``.
        versions: The driver's own version block (``bakta``, ``db``) plus
            ``image`` (the image reference, or ``"native"``), ``image_id``
            (the Docker image id, or ``"native"``) and ``driver_sha256``.
        call_non_coding: Echoes the ``call_non_coding`` argument the run was
            invoked with, so a result carrying no tRNAs can be told apart
            from a result where tRNAs were never looked for.
    """

    features: list[dict[str, Any]]
    discarded: dict[str, Any]
    versions: dict[str, Any]
    call_non_coding: bool


# ---------------------------------------------------------------------------
# Pure parse helpers (offline-unit-testable)
# ---------------------------------------------------------------------------


def _as_accession_list(value: Any) -> list[str]:
    """Normalize a ``psc.*`` field into a list of non-empty accession strings.

    Bakta's ``psc`` fields are inconsistently shaped across fields (e.g.
    ``ec_ids``/``kegg_orthology_id``/``go_ids`` are lists, ``cog_id`` is a
    bare string) — this normalizes either shape to a list so all four fields
    can be walked identically. ``None``/empty/non-string entries are
    dropped; anything else falls back to an empty list rather than raising.
    """
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v]
    return []


def _psc_ontology_terms(
    psc: dict[str, Any],
    ontology_dictionary: OntologyDictionary | None,
    ec_role_resolver: EcRoleResolver | None,
) -> list[Term]:
    """Build the additive, described Terms for a feature's ``psc`` block.

    One ``Term(namespace=<EC|KO|COG|GO>, id=<accession>, value="<accession>:
    <description>")`` per accession in ``ec_ids``/``kegg_orthology_id``/
    ``cog_id``/``go_ids``, degrading to the bare accession (never raising)
    when *ontology_dictionary* has no description on file. Each EC
    accession additionally yields a ``Term(namespace="role", ...)`` per
    ModelSEED role name *ec_role_resolver* resolves it to — this is how EC
    reaches reactions, through ModelSEED's curated role -> complex ->
    reaction chain. Never touches the FUNCTION-destined term or its
    evidence dict (built separately by the caller).

    Args:
        psc: The feature's ``psc`` dict (already type-checked by the
            caller).
        ontology_dictionary: Dictionary used to describe each accession, or
            ``None`` (every accession degrades to itself).
        ec_role_resolver: Resolver used to expand EC accessions into
            ModelSEED role names, or ``None`` (no role Terms are emitted).

    Returns:
        The list of additive Terms (possibly empty, when *psc* carries no
        ontology accessions).
    """
    terms: list[Term] = []
    for field_name, namespace in _PSC_FIELD_NAMESPACE.items():
        for accession in _as_accession_list(psc.get(field_name)):
            terms.append(
                Term(
                    namespace=namespace,
                    id=accession,
                    value=describe_or_accession(
                        namespace, accession, ontology_dictionary
                    ),
                )
            )
            if namespace == "EC" and ec_role_resolver is not None:
                for role in ec_role_resolver.roles_for_ec(accession):
                    terms.append(Term(namespace="role", id=None, value=role))
    return terms


def _parse_bakta_features(
    features: list[dict[str, Any]],
    input_ids: set[str] | None = None,
    ontology_dictionary: OntologyDictionary | None = None,
    ec_role_resolver: EcRoleResolver | None = None,
) -> list[AnnotationRecord]:
    """Convert Bakta's ``input.json`` ``features`` array into AnnotationRecords.

    Emits, per gene, the free-text ``product`` string under
    ``namespace="FUNCTION"`` — byte-identical to D4's original behaviour,
    since reactions are reached downstream by joining this exact text
    against mapping tables keyed on it. ``ec_ids``, ``kegg_orthology_id``,
    ``cog_id`` and ``go_ids`` (nested under ``feature["psc"]``) continue to
    be carried in ``Term.evidence`` (D4's original rule) AND are now
    additionally emitted as their own namespaced, described Terms (see
    :func:`_psc_ontology_terms`) — the PRD's deliberate, knowing reversal of
    D4's "evidence only" confinement. ``aa_hexdigest`` is also carried in
    evidence when present.

    Args:
        features: The parsed ``features`` array from Bakta's ``input.json``.
            Each element is expected to carry ``id`` (the caller's original
            protein id, preserved verbatim) and ``product``.
        input_ids: When given, only features whose ``id`` is a member of this
            set are retained (defends against stray rows from a shared
            output). ``None`` disables the filter.
        ontology_dictionary: Dictionary used to describe ``psc`` accessions,
            or ``None`` (every accession degrades to itself; never raises).
        ec_role_resolver: Resolver used to expand EC accessions into
            ModelSEED role names, or ``None`` (no role Terms are emitted).

    Returns:
        List of ``AnnotationRecord``. A feature with an empty/missing
        ``product`` yields no record and is therefore absent from the
        result — this is not an error.
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

        # FUNCTION-destined term: MUST stay byte-identical to D4's original
        # output. Never rewrite `product` or fold an accession into it.
        terms: list[Term] = [
            Term(namespace="FUNCTION", id=None, value=product, evidence=evidence)
        ]
        if isinstance(psc, dict):
            terms.extend(
                _psc_ontology_terms(psc, ontology_dictionary, ec_role_resolver)
            )

        records.append(AnnotationRecord(gene_id=gene_id, terms=terms))

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
# Structural-call parse helpers (offline-unit-testable)
# ---------------------------------------------------------------------------


def _completeness(truncated: Any) -> tuple[bool, bool]:
    """Derive ``(start_complete, stop_complete)`` from Bakta's marker.

    Bakta resolves the strand itself when it sets the marker: pyrodigal's
    ``partial_begin`` (left end in contig coordinates) becomes ``5-prime``
    on the forward strand and ``3-prime`` on the reverse
    (``bakta/features/cds.py`` 1.11.4 lines 118-124). So the marker already
    names the biological end, and start/stop completeness read straight off
    it on either strand -- which is what makes this strand-aware without a
    second strand flip here.

    Args:
        truncated: The feature's ``truncated`` value, or ``None``.

    Returns:
        ``(start_complete, stop_complete)``. Both True when *truncated* is
        ``None`` or an unrecognised value.
    """
    if truncated == _TRUNCATED_BOTH:
        return False, False
    if truncated == _TRUNCATED_5_PRIME:
        return False, True
    if truncated == _TRUNCATED_3_PRIME:
        return True, False
    return True, True


def _build_kept_feature(record: dict[str, Any]) -> dict[str, Any]:
    """Build one public kept-feature dict from one driver feature record.

    Args:
        record: A driver output ``features`` entry (not discarded).

    Returns:
        The public feature dict (see :class:`BaktaCallResult`).

    Raises:
        BaktaCallError: If the record's ``type`` is not a structural
            feature type, or its ``strand`` is not ``+``/``-`` (``?`` is
            permitted for ``crispr`` only -- see
            :data:`_STRAND_UNKNOWN_TYPES`).
    """
    feature_type = record.get("type")
    if feature_type not in _CALL_FEATURE_TYPES:
        raise BaktaCallError(
            f"Bakta driver emitted an unexpected feature type {feature_type!r}; "
            f"expected one of {sorted(_CALL_FEATURE_TYPES)}"
        )
    strand = record.get("strand")
    allowed_strands = {"+", "-"}
    if feature_type in _STRAND_UNKNOWN_TYPES:
        allowed_strands.add("?")
    if strand not in allowed_strands:
        raise BaktaCallError(
            f"Bakta driver emitted strand {strand!r} for a {feature_type} "
            f"feature; expected one of {sorted(allowed_strands)}"
        )

    truncated = record.get("truncated")
    feature: dict[str, Any] = {
        "type": feature_type,
        "contig_id": record.get("contig_id"),
        "start": record.get("start"),
        "stop": record.get("stop"),
        "strand": strand,
        "nt": record.get("nt"),
        "product": record.get("product"),
        # An edge feature is NOT partial: Bakta pops `truncated` when it
        # merges two partial CDS across a circular origin into one complete
        # gene (bakta/features/cds.py 1.11.4 lines 162-163).
        "partial": truncated is not None,
    }
    if feature_type == "cds":
        aa = record.get("aa")
        if isinstance(aa, str) and aa.endswith("*"):
            aa = aa[:-1]
        feature["aa"] = aa
        start_complete, stop_complete = _completeness(truncated)
        feature["start_complete"] = start_complete
        feature["stop_complete"] = stop_complete
    for key in ("amino_acid", "anticodon", "rfam", "ncrna_class", "score"):
        if key in record:
            feature[key] = record[key]
    return feature


def _build_discarded_cds(record: dict[str, Any]) -> dict[str, Any]:
    """Build one public discarded-CDS dict from one driver feature record.

    Args:
        record: A driver output ``features`` entry carrying a ``discarded``
            block, with ``type == "cds"``.

    Returns:
        ``{contig_id, start, stop, strand, reason, overlap_type,
        overlap_start, overlap_stop}``. The three ``overlap_*`` fields are
        ``None`` for a length/spurious discard, which has no counterpart
        feature and no interval.
    """
    discarded = record.get("discarded") or {}
    return {
        "contig_id": record.get("contig_id"),
        "start": record.get("start"),
        "stop": record.get("stop"),
        "strand": record.get("strand"),
        "reason": discarded.get("type"),
        "overlap_type": discarded.get("feature_type"),
        "overlap_start": discarded.get("overlap_start"),
        "overlap_stop": discarded.get("overlap_stop"),
    }


def _split_call_features(
    features: list[Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Split a driver ``features`` array into kept features and discards.

    Args:
        features: The driver output's ``features`` array. Carries every
            feature, discarded ones included; a discarded feature is
            identified by a ``discarded`` block.

    Returns:
        ``(kept, discarded)`` where *discarded* is the
        :class:`BaktaCallResult` ``discarded`` dict. ``counts`` is
        pre-seeded with all three discard types at 0 so a caller can index
        it unconditionally.

    Raises:
        BaktaCallError: Propagated from :func:`_build_kept_feature` for a
            malformed kept feature.
    """
    kept: list[dict[str, Any]] = []
    discarded_cds: list[dict[str, Any]] = []
    counts: dict[str, int] = {discard_type: 0 for discard_type in _DISCARD_TYPES}
    for record in features:
        if not isinstance(record, dict):
            continue
        discarded = record.get("discarded")
        if not isinstance(discarded, dict):
            kept.append(_build_kept_feature(record))
            continue
        reason = discarded.get("type")
        counts[reason] = counts.get(reason, 0) + 1
        if record.get("type") == "cds":
            discarded_cds.append(_build_discarded_cds(record))
    return kept, {"counts": counts, "cds": discarded_cds}


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

    Ontology description / EC-role resolution:
        ``ontology_dictionary`` — an ``OntologyDictionary`` instance used to
            describe ``psc.ec_ids``/``kegg_orthology_id``/``cog_id``/``go_ids``
            accessions. When not given, one is constructed from this
            instance's own config (``ontology_dictionary.{ko,ec,go,cog}_path``
            — see ``OntologyDictionary``), which degrades gracefully (never
            raises) when a namespace's staged dictionary is unset/unreadable.
        ``ec_role_resolver`` — an ``EcRoleResolver`` instance used to expand
            each EC accession into ModelSEED role names. When not given, no
            role-namespace Terms are emitted and the degradation is recorded
            in ``AnnotationResult.parameters["ec_role_resolver"]``.

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

    def __init__(
        self,
        db_path: str,
        ontology_dictionary: OntologyDictionary | None = None,
        ec_role_resolver: EcRoleResolver | None = None,
        docker_image: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize BaktaUtils against a specific, already-resolved database.

        Args:
            db_path: Path to the Bakta reference database directory
                (must contain ``version.json`` and ``bakta.db``). Resolved
                per-request by the caller — this is a constructor argument,
                not a config key, so multiple database versions can coexist
                without touching config.
            docker_image: Docker image to run Bakta in, overriding the
                ``bakta.docker_image`` config key for this instance.
                ``None`` (the default) means "read config". An explicit
                value — **including the empty string**, which selects
                native mode — is authoritative and the ambient config
                cannot override it. An image choice is a reproducibility
                decision belonging to the caller that resolved the
                database, not to whatever config file happens to be on the
                machine; the same reasoning that made ``db_path`` a
                constructor argument.
            ontology_dictionary: Dictionary used to describe ``psc.*``
                ontology accessions. When ``None`` (the default), one is
                constructed from this instance's own config, which degrades
                gracefully — never raises — for any namespace whose staged
                dictionary is unset/unreadable.
            ec_role_resolver: Resolver used to expand EC accessions into
                ModelSEED role names. When ``None`` (the default), no
                role-namespace Terms are emitted for this instance's runs;
                the degradation is recorded in
                ``AnnotationResult.parameters["ec_role_resolver"]``.
            **kwargs: Forwarded to ``AnnotatorUtils.__init__``.
        """
        super().__init__(**kwargs)
        self.db_path = db_path
        self._bakta_exe: str = self.get_config_value(
            "bakta.executable", default="bakta_proteins"
        )
        #: When set, Bakta runs inside this Docker image instead of as a
        #: local executable. Empty string => native executable mode. An
        #: explicit ``docker_image=`` wins over config (see the constructor
        #: docstring); ``None`` falls back to ``bakta.docker_image``.
        self._docker_image: str = (
            (self.get_config_value("bakta.docker_image", default="") or "")
            if docker_image is None
            else docker_image
        )
        #: Interpreter used to run the structural-calling driver in native
        #: mode (config key ``bakta.python``). Ignored in Docker mode, where
        #: the image's own ``python`` on ``/opt/conda/bin`` is used.
        self._call_python: str = (
            self.get_config_value("bakta.python", default="python3") or "python3"
        )
        self._ontology_dictionary: OntologyDictionary = (
            ontology_dictionary
            if ontology_dictionary is not None
            else OntologyDictionary(**kwargs)
        )
        #: EcRoleResolver instance, or None -- constructor-injected only
        #: (see module docstring: "the caller supplies the path ... via the
        #: constructor"). No automatic file discovery is attempted here, so
        #: an absent resolver can never raise; it simply degrades to no
        #: role-namespace Terms (recorded in parameters, see annotate()).
        self._ec_role_resolver: EcRoleResolver | None = ec_role_resolver

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

        if self._ec_role_resolver is None:
            self.log_warning(
                "No EcRoleResolver was provided to BaktaUtils; EC accessions "
                "will not yield role-namespace terms until an EcRoleResolver "
                "(built from ModelSEED's Annotations/Roles.tsv) is injected "
                "via BaktaUtils(ec_role_resolver=...)."
            )

        features = payload.get("features", []) if isinstance(payload, dict) else []
        records = _parse_bakta_features(
            features,
            set(proteins),
            ontology_dictionary=self._ontology_dictionary,
            ec_role_resolver=self._ec_role_resolver,
        )

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
        # Record ontology-dictionary degradation the same way KofamscanUtils
        # records "ko_function_map: absent" -- only namespaces this run
        # actually queried (i.e. that had at least one accession) can be
        # known to be degraded (see OntologyDictionary.degraded_namespaces).
        for namespace in sorted(self._ontology_dictionary.degraded_namespaces):
            parameters[f"ontology_{namespace.lower()}"] = "absent"
        if self._ec_role_resolver is None:
            parameters["ec_role_resolver"] = "absent"

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

    # ------------------------------------------------------------------
    # Structural gene calling (no functional annotation)
    # ------------------------------------------------------------------

    @staticmethod
    def _driver_path() -> Path:
        """Absolute path to the structural-calling driver file.

        Returns:
            ``<this package>/bakta_call_features.py``. It is resolved as a
            *file* rather than imported, because importing it would require
            ``bakta`` in KBUtilLib's own environment.
        """
        return Path(__file__).resolve().with_name(_DRIVER_FILENAME)

    @classmethod
    def _driver_sha256(cls) -> str:
        """Return the hex SHA-256 of the driver file's bytes.

        Recorded in ``BaktaCallResult.versions``. The image reference pins
        Bakta; this pins the code that drove it, which is the other half of
        "which call order produced these coordinates".

        Returns:
            The hex digest.
        """
        return hashlib.sha256(cls._driver_path().read_bytes()).hexdigest()

    def _docker_image_id(self) -> str:
        """Return the Docker image id of the configured image.

        Uses ``docker image inspect --format '{{.Id}}'`` specifically. The
        sibling helper ``annotator_utils.docker_image_identity`` is NOT
        reused here: it prefers a ``RepoDigest`` and returns a
        tag-qualified ``<image>@<id>`` identity string, whereas the
        structural-call result records the bare content id so two results
        can be compared for "same bytes of Bakta" directly.

        Returns:
            The image id (``"sha256:…"``), ``"native"`` in native mode, or
            the bare image reference when ``docker image inspect`` cannot
            be run at all. Never raises — provenance must not fail a run.
        """
        if not self._docker_image:
            return "native"
        try:
            result = subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    self._docker_image,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return self._docker_image
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return self._docker_image

    def _call_features_available(self) -> tuple[bool, str]:
        """Probe whether a structural call can run, side-effect-free.

        This is deliberately NOT :meth:`is_available`: that one probes the
        ``bakta_proteins`` executable, which structural calling never uses.
        In native mode what has to exist is the interpreter named by
        ``bakta.python``, because the driver is run with it.

        Returns:
            ``(available, detail)``; *detail* explains a False.
        """
        if self._docker_image:
            if self._docker_image_present():
                return True, ""
            return False, f"Docker image {self._docker_image} is not present locally"
        exe = Path(self._call_python)
        if exe.is_absolute():
            if exe.is_file() and os.access(exe, os.X_OK):
                return True, ""
        elif shutil.which(self._call_python) is not None:
            return True, ""
        return False, (
            f"bakta.python interpreter {self._call_python!r} is not on PATH "
            "(needed to run bakta_call_features.py in native mode)"
        )

    def _require_call_features_available(self) -> None:
        """Raise ToolUnavailableError if a structural call cannot run.

        Raises:
            ToolUnavailableError: With the probe's own detail string.
        """
        available, detail = self._call_features_available()
        if not available:
            raise ToolUnavailableError(
                tool=_TOOL, detail=detail, hint=_INSTALL_HINT
            )

    def _build_call_features_cmd(
        self,
        work: Path,
        contigs_path: Path,
        out_path: Path,
        threads: int,
        translation_table: int,
        call_non_coding: bool,
    ) -> list[str]:
        """Build the argv that runs the driver, Docker or native.

        Args:
            work: Host work dir; bind-mounted at ``/work`` in Docker mode.
            contigs_path: Input FASTA, inside *work*.
            out_path: Driver output JSON path, inside *work*.
            threads: CPU threads.
            translation_table: Translation table (11/4/25).
            call_non_coding: When False, ``--no-non-coding`` is passed.

        Returns:
            The argv list.
        """
        resolved_db = str(Path(self.db_path).expanduser().resolve())
        driver_flags = [
            "--threads",
            str(threads),
            "--translation-table",
            str(translation_table),
        ]
        if not call_non_coding:
            driver_flags.append("--no-non-coding")

        if self._docker_image:
            inner = shlex.join(
                [
                    "python",
                    _DRIVER_CONTAINER_PATH,
                    "--db",
                    "/db",
                    "--contigs",
                    f"/work/{contigs_path.name}",
                    "--out",
                    f"/work/{out_path.name}",
                    *driver_flags,
                ]
            )
            return [
                "docker", "run", "--rm",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--network", "none",
                "-v", f"{work.resolve()}:/work",
                "-v", f"{resolved_db}:/db:ro",
                # The driver is mounted read-only: the container must not be
                # able to rewrite the code whose sha256 this run reports.
                "-v", f"{self._driver_path()}:{_DRIVER_CONTAINER_PATH}:ro",
                # /opt/conda/bin must be prefixed onto PATH or neither
                # `python` nor tRNAscan-SE/Infernal/pyrodigal resolve -- see
                # docker/bakta/Dockerfile.
                "--entrypoint", "bash",
                self._docker_image,
                "-lc", f"PATH=/opt/conda/bin:$PATH {inner}",
            ]
        return [
            self._call_python,
            str(self._driver_path()),
            "--db",
            resolved_db,
            "--contigs",
            str(contigs_path),
            "--out",
            str(out_path),
            *driver_flags,
        ]

    def _run_driver(self, cmd: list[str]) -> tuple[int, str]:
        """Run the structural-calling driver.

        The single subprocess seam for structural calling: tests that need
        to supply a canned driver payload replace this method, and nothing
        else has to be mocked.

        Args:
            cmd: argv from :meth:`_build_call_features_cmd`.

        Returns:
            ``(returncode, stderr)``.
        """
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return result.returncode, result.stderr

    def call_features(
        self,
        contigs: Sequence[tuple[str, str]],
        *,
        translation_table: int = 11,
        threads: int = 1,
        call_non_coding: bool = True,
    ) -> BaktaCallResult:
        """Call genes structurally on *contigs* — no functional annotation.

        Runs ``bakta_call_features.py`` (this package's driver) inside the
        instance's Docker image, or with the ``bakta.python`` interpreter in
        native mode. The driver calls Bakta's own structural predictors in
        ``bakta/main.py``'s order and nothing else, so no UPS/IPS lookup, no
        PSC/PSCC Diamond search, no AMRFinderPlus, no expert systems, no
        pseudogene detection and no Pfam ever run. See that module's
        docstring for why a driver is needed instead of a CLI flag.

        Args:
            contigs: ``[(contig_id, nucleotide_sequence), ...]``. Ids are
                written to the FASTA verbatim (Bakta runs with
                ``--keep-contig-headers``) and are checked back against the
                driver's reported ids.
            translation_table: Translation table for CDS translation
                (11/4/25, Bakta's accepted set). Default 11.
            threads: CPU threads passed to Bakta. Default 1.
            call_non_coding: When True (the default) tRNA, tmRNA, rRNA,
                ncRNA-gene and CRISPR callers run too. When False only CDS
                are called. Echoed back on the result.

        Returns:
            A :class:`BaktaCallResult`.

        Raises:
            ToolUnavailableError: If the Docker image (Docker mode) or the
                ``bakta.python`` interpreter (native mode) is absent.
            ValueError: If *contigs* is empty, carries a duplicate or
                whitespace-bearing contig id, carries an empty sequence, or
                does not look like DNA.
            BaktaCallError: If the driver exits non-zero (the message
                quotes the stderr tail; exit
                :data:`_DRIVER_EXIT_VERSION_MISMATCH` names both Bakta
                versions), writes no/unparseable JSON, or reports a contig
                id set that is not exactly the input's.
        """
        self._require_call_features_available()

        if not contigs:
            raise ValueError("call_features requires at least one contig")
        sequences: dict[str, str] = {}
        for contig_id, sequence in contigs:
            if not contig_id or any(ch.isspace() for ch in contig_id):
                raise ValueError(
                    f"Invalid contig id {contig_id!r}: ids must be non-empty and "
                    "contain no whitespace (they are written as FASTA headers)"
                )
            if contig_id in sequences:
                raise ValueError(
                    f"Duplicate contig id {contig_id!r}; Bakta runs with "
                    "--keep-contig-headers and rejects duplicate ids"
                )
            if not (sequence or "").strip():
                # Caught here rather than let through: Bakta's FASTA importer
                # rejects a zero-length record outright, and even if it did
                # not, qc_sequences would drop it and the only symptom would
                # be a contig-id mismatch several steps later.
                raise ValueError(f"Contig {contig_id!r} has an empty sequence")
            sequences[contig_id] = sequence
        _guard_dna(sequences)

        with tempfile.TemporaryDirectory(dir=self._docker_workdir_base()) as tmpdir:
            work = Path(tmpdir)
            contigs_path = work / "contigs.fna"
            _write_one_line_fna(contigs_path, sequences)
            out_path = work / "call_features.json"

            cmd = self._build_call_features_cmd(
                work=work,
                contigs_path=contigs_path,
                out_path=out_path,
                threads=threads,
                translation_table=translation_table,
                call_non_coding=call_non_coding,
            )
            returncode, stderr = self._run_driver(cmd)
            if returncode != 0:
                raise BaktaCallError(self._driver_failure_message(returncode, stderr))
            try:
                payload = json.loads(out_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BaktaCallError(
                    "Bakta structural-calling driver exited 0 but its output JSON "
                    f"({out_path.name}) could not be read: {exc}"
                ) from exc

        if not isinstance(payload, dict):
            raise BaktaCallError(
                "Bakta structural-calling driver output is not a JSON object "
                f"(got {type(payload).__name__})"
            )

        reported_ids = payload.get("contig_ids")
        if not isinstance(reported_ids, list) or set(reported_ids) != set(sequences):
            raise BaktaCallError(
                "Bakta structural-calling driver reported contig ids "
                f"{reported_ids!r}, which are not exactly the input ids "
                f"{sorted(sequences)!r}; every coordinate in the result would be "
                "misattributed"
            )

        features = payload.get("features")
        if not isinstance(features, list):
            raise BaktaCallError(
                "Bakta structural-calling driver output has no 'features' array"
            )
        kept, discarded = _split_call_features(features)

        driver_versions = payload.get("versions")
        versions: dict[str, Any] = (
            dict(driver_versions) if isinstance(driver_versions, dict) else {}
        )
        versions["image"] = self._docker_image or "native"
        versions["image_id"] = self._docker_image_id()
        versions["driver_sha256"] = self._driver_sha256()

        return BaktaCallResult(
            features=kept,
            discarded=discarded,
            versions=versions,
            call_non_coding=call_non_coding,
        )

    @staticmethod
    def _driver_failure_message(returncode: int, stderr: str) -> str:
        """Compose the BaktaCallError message for a non-zero driver exit.

        Args:
            returncode: The driver's exit code.
            stderr: Its stderr, in full.

        Returns:
            The message. For exit
            :data:`_DRIVER_EXIT_VERSION_MISMATCH` it names BOTH the version
            the driver is pinned to and the version actually found, since
            the whole mismatch is only meaningful as a pair.
        """
        tail = (stderr or "").strip()[-_STDERR_TAIL_CHARS:]
        if returncode == _DRIVER_EXIT_VERSION_MISMATCH:
            found = "unknown"
            for line in tail.splitlines():
                marker = "image has "
                if "Bakta version mismatch" in line and marker in line:
                    found = line.split(marker, 1)[1].strip()
                    break
            return (
                "Bakta structural-calling driver refused to run: it is pinned to "
                f"Bakta {_DRIVER_PINNED_BAKTA_VERSION} but the Bakta it found "
                f"reports version {found}. stderr tail:\n{tail}"
            )
        return (
            f"Bakta structural-calling driver exited {returncode}. "
            f"stderr tail:\n{tail}"
        )


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


def _write_one_line_fna(path: Path, sequences: dict[str, str]) -> None:
    """Write ``{contig_id: nt_sequence}`` as a one-line-per-record FASTA.

    Headers are written with the id ONLY — no description. That is
    load-bearing, not tidiness: ``bakta.utils.qc_sequences`` reads the
    description and will flip a contig to ``complete``/``circular`` if it
    contains ``circular=true`` or ``complete``, and to
    chromosome/plasmid on those words — which changes pyrodigal's
    ``closed`` flag and enables the origin-spanning edge-CDS merge. A
    caller's contig description must not be able to do that silently.

    Args:
        path: Destination ``.fna`` path (created/overwritten).
        sequences: Mapping of contig id -> nucleotide sequence. Written
            uppercase, since Bakta uppercases on import anyway and this
            keeps the host-side slice comparison honest.
    """
    lines: list[str] = []
    for seq_id, seq in sequences.items():
        lines.append(f">{seq_id}")
        lines.append(seq.strip().upper())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Convenience re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "BaktaCallError",
    "BaktaCallResult",
    "BaktaUtils",
    "_parse_bakta_features",
    "_parse_bakta_db_version",
    "_split_call_features",
    "_write_one_line_faa",
    "_write_one_line_fna",
]
