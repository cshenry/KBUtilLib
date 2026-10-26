"""Structural-only gene calling driver for Bakta 1.11.4.

WHY THIS FILE EXISTS
--------------------
Bakta 1.11.4's CLI cannot call CDS *without* annotating them. In
``bakta/main.py`` (1.11.4, lines 238-351) the CDS branch is one
straight-line block: once ``--skip-cds`` is off, it always runs UPS/IPS
lookup, the PSC/PSCC Diamond searches, AMRFinderPlus, the expert Diamond
systems, pseudogene detection and Pfam. There is no flag that stops after
prediction. So structural-only calling has to be driven from *inside*
Bakta's own Python API, calling exactly the structural functions
``main.py`` calls, in exactly ``main.py``'s order, and nothing else.

That is what this file is: a standalone driver that is executed *inside*
the Bakta container (or by a native Bakta-capable interpreter), never
imported by KBUtilLib.

HARD CONSTRAINTS (load-bearing -- do not relax)
-----------------------------------------------
1. This module imports **only** ``bakta`` and the standard library. It must
   never import ``kbutillib``: it runs inside an image that has Bakta but
   not KBUtilLib, with ``--network none`` and a read-only database mount.
2. It is pinned to Bakta :data:`PINNED_BAKTA_VERSION`. A different Bakta
   exits :data:`EXIT_VERSION_MISMATCH` (3) with the version pair on stderr,
   because "mirror ``main.py``'s order exactly" is a claim about one
   specific source file and silently drifting off it is the failure mode
   this pin exists to prevent.
3. It calls **no** function from ``bakta.ups``, ``bakta.ips``,
   ``bakta.psc``, ``bakta.pscc`` or ``bakta.expert.*``, and none of
   ``feat_cds.predict_pfam`` / ``feat_cds.predict_pseudo_candidates`` /
   ``feat_cds.detect_pseudogenes`` / ``anno.combine_annotation``. No
   functional annotation is produced, requested or looked up.
4. The output JSON is built **field by field** from Bakta's feature dicts
   (see :func:`_feature_record`). Bakta's raw dicts are never handed to
   ``json.dump``: they carry ``aa_digest`` (raw ``bytes``, unserialisable --
   Bakta's own ``bakta/io/json.py`` pops it before dumping) and a
   ``class`` value that is an ``SO`` namedtuple rather than a string.

STRUCTURAL CALL ORDER (mirrors bakta/main.py 1.11.4)
----------------------------------------------------
``cfg.check_output_path`` -> ``cfg.setup`` -> ``db.check`` ->
``fasta.import_sequences`` -> ``bu.qc_sequences`` ->
``fasta.export_sequences`` (``<tmp>/sequences.fna``) -> build ``data`` ->
``t_rna.predict_t_rnas`` -> ``tm_rna.predict_tm_rnas`` ->
``r_rna.predict_r_rnas`` -> ``nc_rna.predict_nc_rnas`` ->
``crispr.predict_crispr`` -> ``feat_cds.predict`` ->
``feat_cds.filter_length`` -> ``orf.detect_spurious`` ->
``anno.detect_feature_overlaps`` -> (this driver's own)
:func:`_reconcile_cds_overlaps`.

``main.py`` steps deliberately NOT mirrored, and why:

* ``bu.test_dependencies()`` -- probes every binary Bakta might shell out
  to, including the AMRFinderPlus/Diamond stack this driver never uses.
* ``nc_rna_region.predict_nc_rna_regions`` -- disabled via
  ``--skip-ncrna-region``; it is a *region* (cis-regulatory) caller, not a
  gene caller.
* ``feat_cds.revise_translational_exceptions`` and
  ``feat_cds.revise_special_cases_annotated`` -- both rewrite CDS ends
  using functional evidence (selenocysteine/pyrolysine recoding, and the
  dnaA/repA special cases keyed on an already-assigned gene symbol). With
  no annotation there is nothing to revise from.
* ``feat_cds.import_user_cdss`` -- no ``--regions`` input is ever passed.
* sORF, gap and oriC/oriT -- disabled via ``--skip-sorf`` / ``--skip-gap``
  / ``--skip-ori``; the sORF branch is annotation-gated by construction
  (``s_orf.annotation_filter``).
* locus-tag assignment, ``anno.select_gene_symbols`` and every writer --
  this driver emits its own JSON and assigns no ids.

DISCARD ACCUMULATOR
-------------------
``main.py`` lines 244-256 drop length-discarded CDS *before* AntiFam and
drop spurious CDS *before* ``data['features']``, so neither ever reaches
``detect_feature_overlaps``. This driver keeps those in a separate
accumulator (:func:`_call_cdss`) and concatenates it onto the emitted
feature list at the end, so a discarded CDS is reported with its reason
instead of silently vanishing -- while the overlap pass still sees exactly
the feature set Bakta would have shown it.

CDS OVERLAP RULE (this driver's own, NOT Bakta's)
-------------------------------------------------
Bakta's ``anno.detect_feature_overlaps`` discards a CDS for an overlap of
ANY size with a tmRNA/tRNA/rRNA/CRISPR feature -- one shared base is
enough. Live acceptance on poplar caught it discarding two exact RefSeq
genes in *S. coelicolor* -- ``SC_RS16190`` (10 bp of tRNA-Lys at its 3'
end) and ``SC_RS22655`` (3 bp) -- so per Chris's decision of 2026-10-22
("don't cut
small overlapping genes, this causes bugs") the pass is re-judged
afterwards by :func:`_reconcile_cds_overlaps`: a CDS stays discarded only
when the overlapping non-coding features cover at least
:data:`CDS_OVERLAP_DISCARD_FRACTION` of its nucleotide length, and is
otherwise re-admitted carrying an ``rna_overlap`` block.

Bakta itself is neither forked nor monkeypatched -- its pass runs exactly
as it always did, and the re-admission only ever edits CDS *it* discarded
for overlap. Every non-CDS discard (a tRNA dropped for a tmRNA overlap, an
ncRNA-region dropped for a better-scoring one) and both CDS discards that
never reach the overlap pass (length, spurious/AntiFam) are untouched.

OUTPUT
------
``{"features": [...], "contig_ids": [...], "versions": {"bakta": str,
"db": {"major": int, "minor": int, "type": str}}}``, where ``features``
carries *every* feature including discarded ones (a discarded feature
carries a ``discarded`` block). See :func:`_feature_record` for the
per-feature fields.

CLI
---
``python bakta_call_features.py --db DB --contigs FASTA --out JSON
--threads N --translation-table T [--no-non-coding]
[--cds-overlap-discard-fraction F]``
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

#: The one Bakta release this driver is validated against. Bumping it means
#: re-reading bakta/main.py's feature block and re-checking the call order.
PINNED_BAKTA_VERSION = "1.11.4"

#: Exit code used for -- and only for -- a Bakta version mismatch.
EXIT_VERSION_MISMATCH = 3

#: Exit code for any other driver-level failure (bad input, no sequences).
EXIT_ERROR = 1

#: Fraction of a CDS's nucleotide length that overlapping non-coding
#: features must cover before the CDS is discarded for overlap. See the
#: module docstring's CDS OVERLAP RULE section for why this exists; it is
#: overridable per run via ``--cds-overlap-discard-fraction``.
CDS_OVERLAP_DISCARD_FRACTION = 0.5

#: The feature types Bakta's CDS overlap pass actually tests a CDS against
#: (``bakta/features/annotation.py`` 1.11.4 ``detect_feature_overlaps``:
#: tmRNA, tRNA, rRNA, CRISPR, in that order, each match overwriting the
#: previous one's ``discarded`` block). ncRNA *genes* are deliberately
#: absent -- that pass does not test CDS against them at all, so no CDS is
#: ever discarded for an ncRNA-gene overlap and folding ncRNA coverage into
#: the fraction would let a feature type Bakta ignores tip a CDS into a
#: discard. Its fifth case, a user-provided CDS, is not a non-coding
#: feature and cannot arise here: this driver passes no ``--regions``.
_CDS_OVERLAP_FEATURE_TYPES = ("tmRNA", "tRNA", "rRNA", "crispr")

#: Bakta embeds the overlap interval in the discard description as
#: ``[start,stop]`` (see bakta/features/annotation.py 1.11.4, e.g. line
#: 236) rather than as numeric fields, so it is recovered by pattern.
_OVERLAP_INTERVAL_RE = re.compile(r"\[(\d+),(\d+)\]")

#: Prefix Bakta uses for Rfam accessions in ``db_xrefs``
#: (``bakta.constants.DB_XREF_RFAM``).
_RFAM_PREFIX = "RFAM:"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_driver_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse this driver's own CLI (not Bakta's).

    Args:
        argv: Argument list, or ``None`` to read ``sys.argv[1:]``.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        prog="bakta_call_features.py",
        description=(
            "Call genes structurally with Bakta "
            f"{PINNED_BAKTA_VERSION} -- no functional annotation."
        ),
    )
    parser.add_argument("--db", required=True, help="Bakta database directory")
    parser.add_argument(
        "--contigs", required=True, help="Input contigs in FASTA format"
    )
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--threads", type=int, default=1, help="CPU threads")
    parser.add_argument(
        "--translation-table",
        type=int,
        default=11,
        dest="translation_table",
        help="Translation table (11/4/25)",
    )
    parser.add_argument(
        "--no-non-coding",
        action="store_true",
        dest="no_non_coding",
        help="Skip tRNA/tmRNA/rRNA/ncRNA/CRISPR calling (CDS only)",
    )
    parser.add_argument(
        "--cds-overlap-discard-fraction",
        type=float,
        default=CDS_OVERLAP_DISCARD_FRACTION,
        dest="cds_overlap_discard_fraction",
        help=(
            "Fraction of a CDS's length that overlapping non-coding "
            "features must cover for the CDS to be discarded for overlap "
            f"(default {CDS_OVERLAP_DISCARD_FRACTION})"
        ),
    )
    return parser.parse_args(argv)


def _build_bakta_argv(
    db: str,
    contigs: str,
    output: str,
    threads: int,
    translation_table: int,
    no_non_coding: bool,
) -> list[str]:
    """Build the argv Bakta's own parser is fed.

    Bakta's configuration is only reachable through its own
    ``bakta.utils.parse_arguments`` + ``bakta.config.setup`` pair, so the
    flags are assembled here and handed to that parser rather than a
    ``Namespace`` being hand-built -- a hand-built namespace would silently
    miss any attribute ``cfg.setup`` reads.

    ``--complete`` and ``--compliant`` are NEVER passed: ``--complete``
    would make every contig a circular replicon (changing pyrodigal's
    ``closed`` flag and enabling the origin-spanning edge-CDS merge), and
    ``--compliant`` would force ``min_sequence_length`` to 200, silently
    dropping short contigs the caller handed in.

    Args:
        db: Bakta database directory.
        contigs: Input FASTA path.
        output: Bakta output directory (must not already exist).
        threads: CPU threads.
        translation_table: Translation table passed to pyrodigal.
        no_non_coding: When True, also skip every non-coding caller.

    Returns:
        The argv list, ``argv[0]`` included.
    """
    argv = [
        "bakta",
        "--db",
        db,
        "--output",
        output,
        "--keep-contig-headers",
        "--min-contig-length",
        "1",
        "--translation-table",
        str(translation_table),
        "--threads",
        str(threads),
        # Callers this driver never runs; disabling them keeps cfg honest
        # about what the run did, rather than relying on this file simply
        # not invoking them.
        "--skip-ncrna-region",
        "--skip-sorf",
        "--skip-gap",
        "--skip-ori",
        "--skip-pseudo",
        "--skip-plot",
    ]
    if no_non_coding:
        argv += [
            "--skip-trna",
            "--skip-tmrna",
            "--skip-rrna",
            "--skip-ncrna",
            "--skip-crispr",
        ]
    argv.append(contigs)
    return argv


# ---------------------------------------------------------------------------
# Output record construction (field by field -- never json.dump a Bakta dict)
# ---------------------------------------------------------------------------


def _rfam_accession(db_xrefs: Any) -> str | None:
    """Pull the Rfam accession out of a Bakta ``db_xrefs`` list.

    Args:
        db_xrefs: The feature's ``db_xrefs`` value (expected: list of str).

    Returns:
        The bare accession (e.g. ``"RF00001"``) for the first
        ``RFAM:``-prefixed xref, or ``None``. GO/KEGG/SO xrefs are ignored
        -- nothing else from ``db_xrefs`` is carried into the output.
    """
    if not isinstance(db_xrefs, (list, tuple)):
        return None
    for xref in db_xrefs:
        if isinstance(xref, str) and xref.startswith(_RFAM_PREFIX):
            return xref[len(_RFAM_PREFIX) :]
    return None


def _ncrna_class_name(value: Any) -> str | None:
    """Render Bakta's ncRNA ``class`` value as a string.

    ``bakta.features.nc_rna.determine_class`` returns an ``SO`` namedtuple
    (``bakta/so.py``) or ``None`` -- not a string -- so it needs rendering
    before it can go into JSON.

    Args:
        value: The feature's ``class`` value.

    Returns:
        The SO term's name (e.g. ``"ribozyme"``), the value itself when it
        is already a string, or ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value)


def _discard_record(discarded: Any) -> dict[str, Any] | None:
    """Normalise a Bakta ``discarded`` block for output.

    Bakta's discard dicts are shaped per discard type: ``filter_length``
    and ``orf.detect_spurious`` emit ``{type, description[, score,
    evalue]}``, while ``annotation.detect_feature_overlaps`` emits
    ``{type, feature_type, description}`` with the overlap interval
    embedded in the description text as ``[start,stop]``.

    Args:
        discarded: The feature's ``discarded`` value.

    Returns:
        ``{"type", "feature_type", "description", "overlap_start",
        "overlap_stop", "overlap_fraction"}`` (``None`` for anything
        absent), or ``None`` when the feature was not discarded.
        ``overlap_fraction`` is present only on a CDS the overlap rule
        judged and still discarded (see :func:`_reconcile_cds_overlaps`);
        a length or spurious discard has no overlap and reports ``None``.
    """
    if not isinstance(discarded, dict):
        return None
    description = discarded.get("description")
    overlap_start: int | None = None
    overlap_stop: int | None = None
    if isinstance(description, str):
        match = _OVERLAP_INTERVAL_RE.search(description)
        if match is not None:
            overlap_start = int(match.group(1))
            overlap_stop = int(match.group(2))
    return {
        "type": discarded.get("type"),
        "feature_type": discarded.get("feature_type"),
        "description": description,
        "overlap_start": overlap_start,
        "overlap_stop": overlap_stop,
        "overlap_fraction": discarded.get("overlap_fraction"),
    }


def _feature_record(feature: dict[str, Any]) -> dict[str, Any]:
    """Build one output record from one Bakta feature dict, field by field.

    Deliberately omits ``db_xrefs`` (only the Rfam accession is carried
    forward), ``aa_digest`` (raw ``bytes``), ``aa_hexdigest``, ``frame``,
    ``evalue``, ``start_type`` and ``rbs_motif``: none of them are part of
    a structural call, and ``aa_digest`` is not even JSON-serialisable.

    A CDS the overlap rule re-admitted carries ``rna_overlap`` (see
    :func:`_reconcile_cds_overlaps`); nothing else ever does.

    ``truncated`` and ``edge`` are passed through verbatim because they are
    Bakta's own partiality markers and the consumer derives
    partial/start_complete/stop_complete from them. Bakta sets
    ``truncated`` to the *biological* end that is truncated, already
    strand-resolved (``bakta/features/cds.py`` 1.11.4 lines 118-124), and
    *pops* it when two partial CDS are merged across a circular origin,
    marking ``edge`` instead (lines 162-163) -- so an edge feature is
    complete, not partial.

    Args:
        feature: A Bakta feature dict as produced by its structural
            predictors.

    Returns:
        The output record. Keys whose Bakta source field is absent are
        present with value ``None`` for the always-emitted fields, and
        absent entirely for the type-specific ones.
    """
    record: dict[str, Any] = {
        "type": feature.get("type"),
        "contig_id": feature.get("sequence"),
        "start": feature.get("start"),
        "stop": feature.get("stop"),
        "strand": feature.get("strand"),
        "nt": feature.get("nt"),
        "product": feature.get("product"),
        "truncated": feature.get("truncated"),
        "edge": bool(feature.get("edge", False)),
    }
    if "aa" in feature:
        aa = feature["aa"]
        if isinstance(aa, str) and aa.endswith("*"):
            aa = aa[:-1]  # Bakta already strips this; belt and braces
        record["aa"] = aa
    for source_key, out_key in (
        ("amino_acid", "amino_acid"),
        # Bakta's tRNA key is `anti_codon`, not `anticodon`
        # (bakta/features/t_rna.py 1.11.4 line 97).
        ("anti_codon", "anticodon"),
        ("score", "score"),
    ):
        if source_key in feature:
            record[out_key] = feature[source_key]
    rfam = _rfam_accession(feature.get("db_xrefs"))
    if rfam is not None:
        record["rfam"] = rfam
    if "class" in feature:
        record["ncrna_class"] = _ncrna_class_name(feature["class"])
    rna_overlap = feature.get("rna_overlap")
    if isinstance(rna_overlap, dict):
        record["rna_overlap"] = dict(rna_overlap)
    discard = _discard_record(feature.get("discarded"))
    if discard is not None:
        record["discarded"] = discard
    return record


# ---------------------------------------------------------------------------
# CDS overlap rule (this driver's own -- see the module docstring)
# ---------------------------------------------------------------------------


def _overlap_interval(
    start_a: int, stop_a: int, start_b: int, stop_b: int
) -> tuple[int, int] | None:
    """Intersect two 1-based inclusive coordinate intervals.

    Args:
        start_a: First interval's start.
        stop_a: First interval's stop.
        start_b: Second interval's start.
        stop_b: Second interval's stop.

    Returns:
        ``(start, stop)`` of the shared span, or ``None`` when the two do
        not touch.
    """
    start = max(start_a, start_b)
    stop = min(stop_a, stop_b)
    return (start, stop) if start <= stop else None


def _cds_rna_overlaps(
    cds: dict[str, Any], non_coding: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Describe every non-coding overlap of one CDS.

    Recomputed from the feature list rather than read back out of Bakta's
    discard description, because Bakta rewrites ``cds['discarded']`` once
    per overlapping feature (``bakta/features/annotation.py`` 1.11.4), so
    the surviving text names only the LAST overlap found -- which is
    neither necessarily the largest nor the union.

    Args:
        cds: A Bakta CDS feature dict.
        non_coding: Candidate non-coding features, any contig; the contig
            is matched here on Bakta's ``sequence`` key.

    Returns:
        One ``{feature_type, overlap_start, overlap_stop, overlap_bp}``
        per overlapping feature, in *non_coding* order. Strand is
        deliberately ignored: a CDS sitting on the opposite strand of an
        rRNA is as unlikely to be real as one on the same strand, and
        Bakta's own pass ignores strand too.
    """
    overlaps: list[dict[str, Any]] = []
    for feature in non_coding:
        if feature.get("sequence") != cds.get("sequence"):
            continue
        interval = _overlap_interval(
            cds["start"], cds["stop"], feature["start"], feature["stop"]
        )
        if interval is None:
            continue
        start, stop = interval
        overlaps.append(
            {
                "feature_type": feature.get("type"),
                "overlap_start": start,
                "overlap_stop": stop,
                "overlap_bp": stop - start + 1,
            }
        )
    return overlaps


def _covered_bp(overlaps: list[dict[str, Any]]) -> int:
    """Count the distinct CDS positions covered by *overlaps*.

    The intervals are unioned, not summed: two non-coding features can
    overlap each other as well as the CDS (a tRNA inside an rRNA operon,
    say), and summing would double-count the shared bases and could push a
    CDS over the discard threshold on the strength of positions counted
    twice.

    Args:
        overlaps: Entries from :func:`_cds_rna_overlaps`.

    Returns:
        The number of distinct covered positions, in bp.
    """
    covered = 0
    reach: int | None = None  # highest stop merged so far
    for entry in sorted(overlaps, key=lambda item: item["overlap_start"]):
        start = entry["overlap_start"]
        stop = entry["overlap_stop"]
        if reach is None or start > reach:
            covered += stop - start + 1
            reach = stop
        elif stop > reach:
            covered += stop - reach
            reach = stop
    return covered


def _reconcile_cds_overlaps(data: dict, discard_fraction: float) -> None:
    """Re-admit CDS the overlap pass discarded for a small overlap.

    Runs immediately AFTER ``anno.detect_feature_overlaps`` and mutates
    only the CDS *that pass* discarded. A CDS stays discarded when the
    overlapping non-coding features cover at least *discard_fraction* of
    its nucleotide length -- its ``discarded`` block keeps type
    ``'overlap'`` and gains ``overlap_fraction``. Below the threshold the
    ``discarded`` block is deleted (the CDS is kept) and an
    ``rna_overlap`` block records the LARGEST single overlap, so the
    overlap stays visible to the caller instead of being either silently
    dropped or silently kept.

    ``overlap_fraction`` is the same number in both cases: the fraction of
    the CDS covered by the UNION of its overlaps, i.e. the quantity the
    rule compared against the threshold. With one overlapping feature --
    which is every case observed in acceptance -- it is simply
    ``overlap_bp / length``.

    Bakta's own handling of everything else is untouched: non-CDS discards
    (a tRNA dropped for a tmRNA overlap, an ncRNA-region dropped for a
    better-scoring one) are left exactly as Bakta left them, and the length
    and spurious/AntiFam CDS discards never reach the overlap pass at all
    (see :func:`_call_cdss`).

    Args:
        data: Bakta's ``data`` dict, after the overlap pass has run.
        discard_fraction: Covered fraction at or above which a CDS stays
            discarded (:data:`CDS_OVERLAP_DISCARD_FRACTION`).
    """
    # Non-coding features that are THEMSELVES discarded stay in the
    # candidate set on purpose: Bakta's CDS loop reads per-contig lists it
    # built before marking tRNA-vs-tmRNA discards and never re-filters
    # them, so a CDS can be discarded over a tRNA that is itself discarded.
    # Mirroring that keeps the cause set identical to the one being
    # re-judged.
    non_coding = [
        feature
        for feature in data["features"]
        if feature.get("type") in _CDS_OVERLAP_FEATURE_TYPES
    ]
    for cds in data["features"]:
        if cds.get("type") != "cds":
            continue
        discarded = cds.get("discarded")
        if not isinstance(discarded, dict) or discarded.get("type") != "overlap":
            continue
        if discarded.get("feature_type") not in _CDS_OVERLAP_FEATURE_TYPES:
            # The pass's one non-RNA case, an overlap with a user-provided
            # CDS. Unreachable here (no --regions is ever passed) and not
            # this rule's business, so Bakta's verdict stands.
            continue
        overlaps = _cds_rna_overlaps(cds, non_coding)
        if not overlaps:
            continue
        length = cds["stop"] - cds["start"] + 1
        # A non-positive length would be a Bakta bug; keeping the discard
        # is the safe reading of one.
        fraction = _covered_bp(overlaps) / length if length > 0 else 1.0
        if fraction >= discard_fraction:
            discarded["overlap_fraction"] = fraction
            continue
        largest = max(overlaps, key=lambda item: item["overlap_bp"])
        del cds["discarded"]
        cds["rna_overlap"] = {**largest, "overlap_fraction": fraction}


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------


def _call_cdss(feat_cds: Any, orf: Any, data: dict) -> tuple[list, list]:
    """Predict CDS structurally, mirroring bakta/main.py 1.11.4:238-256.

    The two length/spurious filters are the only CDS steps in ``main.py``
    that run before any annotation lookup. Each marks its victims with a
    ``discarded`` block and ``main.py`` then drops them from the working
    list, so a length-discarded CDS never reaches AntiFam and neither ever
    reaches ``detect_feature_overlaps``. That exact sequencing is preserved
    here; the dropped CDS are collected separately so they can still be
    reported.

    Args:
        feat_cds: The ``bakta.features.cds`` module.
        orf: The ``bakta.features.orf`` module.
        data: Bakta's ``data`` dict.

    Returns:
        ``(kept_cdss, discarded_cdss)``.
    """
    discarded: list = []
    cdss = feat_cds.predict(data)
    if len(cdss) > 0:
        discarded.extend(feat_cds.filter_length(cdss))
        cdss = [cds for cds in cdss if "discarded" not in cds]
    if len(cdss) > 0:
        discarded.extend(orf.detect_spurious(cdss))
        cdss = [cds for cds in cdss if "discarded" not in cds]
    return cdss, discarded


def main(argv: list[str] | None = None) -> int:
    """Run the driver.

    Args:
        argv: Driver arguments, or ``None`` to read ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success, :data:`EXIT_VERSION_MISMATCH` on a
        Bakta version mismatch, :data:`EXIT_ERROR` otherwise.
    """
    args = _parse_driver_args(argv)

    # The version gate runs before any other bakta import: the whole point
    # of mirroring main.py is that it is THIS main.py being mirrored.
    import bakta

    found_version = getattr(bakta, "__version__", None)
    if found_version != PINNED_BAKTA_VERSION:
        print(
            f"Bakta version mismatch: driver pinned {PINNED_BAKTA_VERSION}, "
            f"image has {found_version}",
            file=sys.stderr,
        )
        return EXIT_VERSION_MISMATCH

    import bakta.config as cfg
    import bakta.db as db
    import bakta.features.annotation as anno
    import bakta.features.cds as feat_cds
    import bakta.features.crispr as crispr
    import bakta.features.nc_rna as nc_rna
    import bakta.features.orf as orf
    import bakta.features.r_rna as r_rna
    import bakta.features.t_rna as t_rna
    import bakta.features.tm_rna as tm_rna
    import bakta.io.fasta as fasta
    import bakta.utils as bu

    # Bakta's check_output_path refuses a directory that already exists
    # (without --force), so point it at a not-yet-created child of a fresh
    # temp dir. Nothing in the output dir is read back: this driver emits
    # its own JSON.
    output_root = Path(tempfile.mkdtemp(prefix="bakta_call_features_"))
    output_path = output_root / "out"
    # main.py registers bu.cleanup via atexit to remove cfg.tmp_path. This
    # driver cleans up explicitly instead, because in native mode (no
    # throwaway container around the run) a leaked tmp dir per call would
    # accumulate in the caller's TMPDIR.
    try:
        return _run(
            args=args,
            found_version=found_version,
            output_path=output_path,
            cfg=cfg,
            db=db,
            anno=anno,
            feat_cds=feat_cds,
            crispr=crispr,
            nc_rna=nc_rna,
            orf=orf,
            r_rna=r_rna,
            t_rna=t_rna,
            tm_rna=tm_rna,
            fasta=fasta,
            bu=bu,
        )
    finally:
        shutil.rmtree(output_root, ignore_errors=True)
        if cfg.tmp_path is not None:
            shutil.rmtree(cfg.tmp_path, ignore_errors=True)


def _run(
    *,
    args: argparse.Namespace,
    found_version: str,
    output_path: Path,
    cfg: Any,
    db: Any,
    anno: Any,
    feat_cds: Any,
    crispr: Any,
    nc_rna: Any,
    orf: Any,
    r_rna: Any,
    t_rna: Any,
    tm_rna: Any,
    fasta: Any,
    bu: Any,
) -> int:
    """Configure Bakta and run the structural callers.

    Split out of :func:`main` only so ``main`` can wrap it in the temp-dir
    cleanup; the body is the mirrored ``bakta/main.py`` sequence.

    Args:
        args: Parsed driver arguments.
        found_version: The verified ``bakta.__version__``.
        output_path: Bakta ``--output`` directory (must not yet exist).
        cfg: ``bakta.config``.
        db: ``bakta.db``.
        anno: ``bakta.features.annotation``.
        feat_cds: ``bakta.features.cds``.
        crispr: ``bakta.features.crispr``.
        nc_rna: ``bakta.features.nc_rna``.
        orf: ``bakta.features.orf``.
        r_rna: ``bakta.features.r_rna``.
        t_rna: ``bakta.features.t_rna``.
        tm_rna: ``bakta.features.tm_rna``.
        fasta: ``bakta.io.fasta``.
        bu: ``bakta.utils``.

    Returns:
        The process exit code.
    """
    sys.argv = _build_bakta_argv(
        db=args.db,
        contigs=args.contigs,
        output=str(output_path),
        threads=args.threads,
        translation_table=args.translation_table,
        no_non_coding=args.no_non_coding,
    )
    bakta_args = bu.parse_arguments()

    # main.py sets cfg.prefix before configuring anything else, and
    # cfg.setup() reads cfg.output_path as already-resolved state rather
    # than setting it.
    cfg.prefix = (
        bakta_args.prefix if bakta_args.prefix else Path(bakta_args.genome).stem
    )
    cfg.check_output_path(bakta_args.output, bakta_args.force)
    cfg.setup(bakta_args)
    cfg.db_info = db.check(cfg.db_path)

    sequences = fasta.import_sequences(cfg.genome_path)
    replicons = bu.parse_replicon_table(cfg.replicons) if cfg.replicons else None
    sequences, complete_genome = bu.qc_sequences(sequences, replicons)
    if len(sequences) == 0:
        print("Error: input file contains no valid sequences.", file=sys.stderr)
        return EXIT_ERROR
    sequences_path = cfg.tmp_path.joinpath("sequences.fna")
    fasta.export_sequences(sequences, sequences_path)

    data = {
        "genome": {
            "genus": cfg.genus,
            "species": cfg.species,
            "strain": cfg.strain,
            "taxon": cfg.taxon,
            "complete": cfg.complete or complete_genome,
            "gram": cfg.gram,
            "translation_table": cfg.translation_table,
        },
        "stats": {"size": sum([seq["length"] for seq in sequences])},
        "features": [],
        "sequences": sequences,
    }

    # Non-coding callers, in main.py's order. nc_rna_region is skipped (see
    # module docstring) and so never appears here.
    if not args.no_non_coding:
        data["features"].extend(t_rna.predict_t_rnas(data, sequences_path))
        data["features"].extend(tm_rna.predict_tm_rnas(data, sequences_path))
        data["features"].extend(r_rna.predict_r_rnas(data, sequences_path))
        data["features"].extend(nc_rna.predict_nc_rnas(data, sequences_path))
        data["features"].extend(crispr.predict_crispr(data, sequences_path))

    cdss, discarded_cdss = _call_cdss(feat_cds, orf, data)
    data["features"].extend(cdss)

    anno.detect_feature_overlaps(data)
    # Bakta discards a CDS for an overlap of ANY size; this re-judges only
    # the CDS it just discarded against the fraction threshold.
    _reconcile_cds_overlaps(data, args.cds_overlap_discard_fraction)

    payload = {
        "features": [
            _feature_record(feature)
            for feature in list(data["features"]) + discarded_cdss
        ],
        "contig_ids": [seq["id"] for seq in sequences],
        "versions": {
            "bakta": found_version,
            "db": {
                "major": cfg.db_info.get("major"),
                "minor": cfg.db_info.get("minor"),
                "type": cfg.db_info.get("type"),
            },
        },
    }
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
