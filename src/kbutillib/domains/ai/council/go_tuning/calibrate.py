"""Is the judge worth trusting?  Agreement, its interval, and the band.

The adjudication council is the instrument every later tuning decision is
read off, so before anything is tuned against it somebody has to establish
how often it is right.  That is what this module computes, from the marks
:mod:`.table` read back: one agreement figure, the slices that would expose
the ways it can be right on average and wrong in a pattern, and a BAND that
:func:`~.adjudicate.calibration_gate` turns into permission for the next
tranche.

AGREEMENT IS R/(R+W), AND EVERY OTHER MARK IS OUT OF BOTH ENDS
==============================================================
``?`` and a blank are not half-agreements and they are not disagreements, so
they are in neither the numerator nor the denominator.  A ``W`` whose note
named no replacement terms IS in the denominator: it says the judge was
wrong, which is the only thing agreement measures, and dropping it because it
produced no gold would quietly inflate the figure by exactly the hardest
rows.  A blank is different again -- it is an unanswered question rather than
an answer -- and :func:`report` REFUSES while any row is blank rather than
reporting over the subset that happens to have been done.  A partial pass
read as a finished one is how a run convinces itself it is trusted on the
thirty rows somebody started with.

WHY AN INTERVAL AND NOT JUST A PROPORTION
=========================================
``17/20`` and ``85/100`` are both 0.85 and they are not the same evidence.
The band therefore gates on the WILSON lower bound as well as on the point
estimate: 85/100 clears it (lower 0.767) and 17/20 does not (lower 0.640),
which is the difference between "this instrument is calibrated" and "we have
not measured it yet".  A PERFECT ten out of ten does not clear it either
(lower 0.722), which is the right answer and not a quirk.  Wilson rather
than the normal approximation because
the normal interval is badly behaved exactly where a good judge lives --
near 1.0, where it runs past 1 and its coverage collapses.

THE SLICES ARE THERE TO CATCH A PATTERN, NOT TO DECORATE THE REPORT
===================================================================
Three slices, each answering a specific failure:

* **per class** -- a judge that is excellent on ``disjoint`` and poor on
  ``partial`` is not an 85% judge, it is two instruments, and tuning against
  the pooled figure would optimise the easy half.
* **per source tool** -- the annotations are not one population; a judge
  calibrated on one tool's phrasing says nothing about another's.
* **per favoured side** -- THE SELF-PREFERENCE CHECK.  Agreement among the
  rows the judge gave to ``go_mf`` versus among the rows it gave to ALCF.
  A judge that is right whenever it picks one pipeline and wrong whenever it
  picks the other is not measuring the mapping, it is expressing a
  preference, and the pooled figure cannot see it.

POPULATION WEIGHTING, BECAUSE THE PILOT IS NOT THE POPULATION
=============================================================
The pilot is drawn proportionally by source but its CLASS mix is a plan
parameter (half ``disjoint``, half ``partial``), while the corpus is mostly
neither.  So the unweighted figure answers "how often was the judge right on
the rows we marked" and the weighted one answers "how often would it be
right on the corpus", and both are reported because they are different
questions.  Weights are the population's class share times its source share
(C13) over the ``class|source`` cells that actually have marks, renormalised;
cells with no marks contribute nothing rather than being imputed.

THE SWAP FLIP RATE IS THREE RATES
=================================
A swapped twin is the same item asked in the opposite order, so a difference
between the two answers is position bias.  It is reported three ways because
they are three severities (C13): a change of **final ids** is the council
settling differently; a change of **source-normalised verdict** is the only
one that moves a calibration figure, and it is the headline rate; a change of
the **off-menu flag** is a change in the research metadata without
necessarily a change in the answer.  Each gets its own Wilson interval --
a flip rate off 14 pairs is not evidence of anything and should not read as
though it were.

THE BAND, IN ORDER (C13)
========================
``insufficient``  no ``R`` and no ``W``: nothing has been marked.
``trusted``       agreement >= 0.85 AND Wilson lower >= 0.75.
``conditional``   agreement >= 0.70 and not ``trusted``.
``not_trusted``   anything else.

The order matters and is tested: ``conditional`` is defined as "0.70 and not
trusted" rather than as "0.70 to 0.85", because a run at 0.90 with a lower
bound of 0.70 is NOT trusted and must still be admitted as conditional
rather than falling through to ``not_trusted``.

The calibration record is BOUND to the manifest id, the adjudicate spec hash
and the resolved roster (C13).  ``calibration_gate`` matches on all three and
ignores a record that does not, so a report written without them is a report
no gate will ever accept -- which is why they are required arguments rather
than optional ones.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .conflicts import MatchResult, Refused
from .table import Gold

__all__ = [
    "BANDS",
    "CONDITIONAL_AGREEMENT",
    "Agreement",
    "CalibrationReport",
    "SwapFlips",
    "TRUSTED_AGREEMENT",
    "TRUSTED_LOWER",
    "WILSON_Z",
    "agreement_of",
    "band_for",
    "population_shares",
    "record",
    "record_marks",
    "report",
    "wilson",
]

#: The two-sided 95% normal quantile.  Spelled out rather than imported from
#: scipy: this module must not need a scientific stack to tell an operator
#: whether their judge is calibrated.
WILSON_Z = 1.959963984540054

#: The band thresholds, as C13 states them.
TRUSTED_AGREEMENT = 0.85
TRUSTED_LOWER = 0.75
CONDITIONAL_AGREEMENT = 0.70

#: Every band, in the order :func:`band_for` decides them.
BANDS = ("insufficient", "trusted", "conditional", "not_trusted")


# ---------------------------------------------------------------------------
# the statistics
# ---------------------------------------------------------------------------


def wilson(
    successes: int, total: int, z: float = WILSON_Z
) -> Tuple[Optional[float], Optional[float]]:
    """The Wilson score interval ``(lower, upper)``, or ``(None, None)``.

    ``(None, None)`` when *total* is zero: an interval over no observations
    is not a wide interval, it is the absence of one, and returning
    ``(0.0, 1.0)`` would let a band read it as a measurement.

    Clamped to ``[0, 1]`` only against floating-point overshoot at the
    extremes; the formula itself cannot leave the unit interval.
    """
    n = int(total)
    if n <= 0:
        return None, None
    k = int(successes)
    p = k / n
    denominator = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = (z / denominator) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class Agreement:
    """``R``/``W`` over one slice, with the proportion and its interval."""

    right: int
    wrong: int

    @property
    def n(self) -> int:
        return self.right + self.wrong

    @property
    def agreement(self) -> Optional[float]:
        return (self.right / self.n) if self.n else None

    @property
    def interval(self) -> Tuple[Optional[float], Optional[float]]:
        return wilson(self.right, self.n)

    def as_dict(self) -> Dict[str, Any]:
        lower, upper = self.interval
        return {
            "right": self.right,
            "wrong": self.wrong,
            "n": self.n,
            "agreement": self.agreement,
            "wilson_lower": lower,
            "wilson_upper": upper,
        }


def agreement_of(marks: Sequence[str]) -> Agreement:
    """An :class:`Agreement` over a sequence of mark letters."""
    return Agreement(
        right=sum(1 for m in marks if m == "R"),
        wrong=sum(1 for m in marks if m == "W"),
    )


def band_for(agreement: Optional[float], lower: Optional[float], n: int) -> str:
    """The band, decided in C13's order.  See the module docstring."""
    if n <= 0 or agreement is None or lower is None:
        return "insufficient"
    if agreement >= TRUSTED_AGREEMENT and lower >= TRUSTED_LOWER:
        return "trusted"
    if agreement >= CONDITIONAL_AGREEMENT:
        return "conditional"
    return "not_trusted"


@dataclass(frozen=True)
class SwapFlips:
    """Position bias over the swap pairs, three ways (C13).

    ``verdict`` is the headline -- the source-normalised verdict is what a
    calibration figure is computed over -- and it is the one ``rate`` and
    the interval describe.
    """

    pairs: int
    final_ids: int
    verdict: int
    off_menu: int

    @property
    def rate(self) -> Optional[float]:
        return (self.verdict / self.pairs) if self.pairs else None

    @property
    def interval(self) -> Tuple[Optional[float], Optional[float]]:
        return wilson(self.verdict, self.pairs)

    def as_dict(self) -> Dict[str, Any]:
        lower, upper = self.interval
        return {
            "pairs": self.pairs,
            "flipped_final_ids": self.final_ids,
            "flipped_verdict": self.verdict,
            "flipped_off_menu": self.off_menu,
            "flip_rate": self.rate,
            "wilson_lower": lower,
            "wilson_upper": upper,
        }


# ---------------------------------------------------------------------------
# the weights
# ---------------------------------------------------------------------------


def population_shares(matched: Any) -> Dict[str, Dict[str, float]]:
    """``{"class": {...}, "source": {...}}`` marginal shares over the corpus.

    Computed from the FULL matched extract rather than from the pilot, which
    is the point: the pilot's class mix is a plan parameter and its source
    mix is proportional, so only the extract knows what the corpus looks
    like.  Shares, not counts, so a report carrying them does not also carry
    a corpus size it was not asked about.
    """
    items = matched.items if isinstance(matched, MatchResult) else list(matched)
    classes: Dict[str, int] = {}
    sources: Dict[str, int] = {}
    for item in items:
        classes[str(item.kind)] = classes.get(str(item.kind), 0) + 1
        sources[str(item.source)] = sources.get(str(item.source), 0) + 1
    total = len(items)
    if not total:
        return {"class": {}, "source": {}}
    return {
        "class": {k: v / total for k, v in sorted(classes.items())},
        "source": {k: v / total for k, v in sorted(sources.items())},
    }


def _fallback_shares(
    manifest: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> Dict[str, Dict[str, float]]:
    """Class shares from the manifest's population, sources from the rows.

    The manifest records ``report["population"]`` -- the class counts over
    every matched row -- but no source marginal, so with no extract to hand
    the source shares fall back to the MARKED ROWS' own.  That makes the
    weighted figure a class-reweighting only, and :class:`CalibrationReport`
    says so in ``weights_from`` rather than letting a reader assume both
    marginals came from the corpus.
    """
    population = dict((manifest.get("report") or {}).get("population") or {})
    total = sum(int(v) for v in population.values())
    classes = (
        {k: int(v) / total for k, v in sorted(population.items()) if int(v) > 0}
        if total
        else {}
    )
    counts: Dict[str, int] = {}
    for row in rows:
        counts[str(row["source"])] = counts.get(str(row["source"]), 0) + 1
    n = sum(counts.values())
    sources = {k: v / n for k, v in sorted(counts.items())} if n else {}
    return {"class": classes, "source": sources}


def _weighted(
    rows: Sequence[Mapping[str, Any]], shares: Mapping[str, Mapping[str, float]]
) -> Tuple[Optional[float], Dict[str, Any]]:
    """Population-weighted agreement over the ``class|source`` cells.

    The weight of a cell is the class share times the source share (C13),
    and the result is renormalised over the cells that HAVE marks: a cell
    the pilot never drew contributes no agreement figure, and imputing one
    would be inventing data.
    """
    cells: Dict[Tuple[str, str], List[str]] = {}
    for row in rows:
        if row["mark"] not in ("R", "W"):
            continue
        cells.setdefault((str(row["class"]), str(row["source"])), []).append(
            str(row["mark"])
        )
    total_weight = 0.0
    total = 0.0
    detail: Dict[str, Any] = {}
    for (kind, source), marks in sorted(cells.items()):
        weight = float(shares.get("class", {}).get(kind, 0.0)) * float(
            shares.get("source", {}).get(source, 0.0)
        )
        slice_ = agreement_of(marks)
        if slice_.agreement is None:
            continue
        detail[f"{kind}|{source}"] = {
            "weight": weight,
            **slice_.as_dict(),
        }
        total_weight += weight
        total += weight * slice_.agreement
    if total_weight <= 0.0:
        return None, detail
    return total / total_weight, detail


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationReport:
    """What one marked pilot says about the judge, and what it permits."""

    manifest_id: str
    adjudicate_spec_hash: str
    roster_fingerprint: str
    overall: Agreement
    band: str
    by_class: Mapping[str, Agreement] = field(default_factory=dict)
    by_source: Mapping[str, Agreement] = field(default_factory=dict)
    by_side: Mapping[str, Agreement] = field(default_factory=dict)
    swap: SwapFlips = field(default_factory=lambda: SwapFlips(0, 0, 0, 0))
    weighted_agreement: Optional[float] = None
    weights_from: str = ""
    weight_cells: Mapping[str, Any] = field(default_factory=dict)
    off_menu_flags: int = 0
    both_acceptable: int = 0
    marks: Mapping[str, str] = field(default_factory=dict)
    counts: Mapping[str, int] = field(default_factory=dict)
    gold: Mapping[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "adjudicate_spec_hash": self.adjudicate_spec_hash,
            "roster_fingerprint": self.roster_fingerprint,
            "band": self.band,
            "overall": self.overall.as_dict(),
            "weighted_agreement": self.weighted_agreement,
            "weights_from": self.weights_from,
            "weight_cells": dict(self.weight_cells),
            "by_class": {k: v.as_dict() for k, v in sorted(self.by_class.items())},
            "by_source": {k: v.as_dict() for k, v in sorted(self.by_source.items())},
            "by_side": {k: v.as_dict() for k, v in sorted(self.by_side.items())},
            "swap": self.swap.as_dict(),
            "off_menu_flags": self.off_menu_flags,
            "both_acceptable": self.both_acceptable,
            "counts": dict(self.counts),
            "gold": dict(self.gold),
        }

    def to_markdown(self) -> str:
        return _markdown(self)


def _entry_index(manifest: Any) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    payload = manifest.as_dict() if hasattr(manifest, "as_dict") else dict(manifest)
    return str(payload.get("manifest_id") or ""), {
        str(e["id"]): dict(e) for e in payload.get("entries") or ()
    }


def report(
    results: Any,
    gold: Gold,
    manifest: Any,
    *,
    adjudicate_spec_hash: str,
    roster_fingerprint: str,
    population: Optional[Mapping[str, Mapping[str, float]]] = None,
) -> CalibrationReport:
    """The calibration report for one marked pilot.

    Args:
        results: the :class:`~.adjudicate.ItemResult` list the table was
            written from.  Supplies the side, the off-menu flag and the swap
            twins; the marks come from *gold* and the class/source from the
            manifest.
        gold: what :func:`~.table.read_marks` read back.
        manifest: the manifest, or its JSON -- the only thing that knows each
            row's class and source tool.
        adjudicate_spec_hash: ``adjudicate_spec_hash`` of the query version
            the marks were collected against.
        roster_fingerprint: the resolved roster's fingerprint.  Both are
            required: a record missing either is a record
            :func:`~.adjudicate.calibration_gate` will never match (C13).
        population: ``{"class": {...}, "source": {...}}`` marginal shares for
            the weighted figure, from :func:`population_shares`.  Omitted,
            the class shares come from the manifest's recorded population and
            the source shares from the marked rows themselves; see
            :func:`_fallback_shares`.

    Raises:
        Refused: the gold is for another manifest; a marked row is not in the
            manifest or has no result; or ANY row is still unmarked (C12 --
            see the module docstring on why a partial pass is not reported).
    """
    manifest_id, entries = _entry_index(manifest)
    if str(gold.manifest_id) != manifest_id:
        raise Refused(
            f"these marks were read against manifest {gold.manifest_id!r} but the "
            f"report was asked for {manifest_id!r}"
        )
    if gold.unreviewed:
        raise Refused(
            f"{len(gold.unreviewed)} of {len(gold.marks)} row(s) carry no mark; a "
            "calibration over the rows that happen to have been done would read as "
            "a finished pass",
            list(gold.unreviewed),
        )
    if not str(adjudicate_spec_hash) or not str(roster_fingerprint):
        raise Refused(
            "a calibration record must be bound to the adjudicate spec hash and the "
            "resolved roster; without both, no gate will ever match it"
        )

    by_result = {str(r.item_id): r for r in results}
    rows: List[Dict[str, Any]] = []
    unknown: List[str] = []
    missing: List[str] = []
    for item_id, mark in sorted(gold.marks.items()):
        entry = entries.get(item_id)
        result = by_result.get(item_id)
        if entry is None:
            unknown.append(item_id)
            continue
        if result is None:
            missing.append(item_id)
            continue
        twin = by_result.get(f"{entry['item_id']}:swap")
        rows.append(
            {
                "item_id": item_id,
                "mark": mark,
                "class": str(entry.get("class") or ""),
                "source": str(entry.get("source") or ""),
                "side": str(result.side),
                "off_menu": result.correct_term_off_menu,
                "result": result,
                "twin": twin,
            }
        )
    if unknown:
        raise Refused(
            f"{len(unknown)} marked row(s) are not entries of manifest "
            f"{manifest_id or '(unidentified)'}",
            sorted(unknown),
        )
    if missing:
        raise Refused(
            f"{len(missing)} marked row(s) have no adjudication result to calibrate "
            "against",
            sorted(missing),
        )

    overall = agreement_of([r["mark"] for r in rows])
    lower, _upper = overall.interval

    def slice_by(key: str) -> Dict[str, Agreement]:
        buckets: Dict[str, List[str]] = {}
        for row in rows:
            buckets.setdefault(str(row[key]), []).append(str(row["mark"]))
        return {name: agreement_of(marks) for name, marks in sorted(buckets.items())}

    shares = (
        {k: dict(v) for k, v in population.items()}
        if population is not None
        else _fallback_shares(
            manifest.as_dict() if hasattr(manifest, "as_dict") else dict(manifest),
            rows,
        )
    )
    weighted, cells = _weighted(rows, shares)

    pairs = [r for r in rows if r["twin"] is not None]
    swap = SwapFlips(
        pairs=len(pairs),
        final_ids=sum(
            1
            for r in pairs
            if frozenset(r["result"].final_ids) != frozenset(r["twin"].final_ids)
        ),
        verdict=sum(1 for r in pairs if r["result"].side != r["twin"].side),
        off_menu=sum(
            1
            for r in pairs
            if r["result"].correct_term_off_menu != r["twin"].correct_term_off_menu
        ),
    )

    return CalibrationReport(
        manifest_id=manifest_id,
        adjudicate_spec_hash=str(adjudicate_spec_hash),
        roster_fingerprint=str(roster_fingerprint),
        overall=overall,
        band=band_for(overall.agreement, lower, overall.n),
        by_class=slice_by("class"),
        by_source=slice_by("source"),
        by_side=slice_by("side"),
        swap=swap,
        weighted_agreement=weighted,
        weights_from=(
            "population" if population is not None else "manifest population x marked rows"
        ),
        weight_cells=cells,
        off_menu_flags=sum(1 for r in rows if r["off_menu"]),
        both_acceptable=sum(1 for r in rows if r["side"] == "both_acceptable"),
        marks=dict(gold.marks),
        counts=gold.counts,
        gold={
            "records": len(gold.records),
            "judge": sum(1 for g in gold.records if g.provenance == "judge"),
            "human": sum(1 for g in gold.records if g.provenance == "human"),
            "off_menu": sum(1 for g in gold.records if g.off_menu),
            "excluded": len(gold.excluded),
        },
    )


# ---------------------------------------------------------------------------
# the ledger record
# ---------------------------------------------------------------------------


def record(ledger: Any, calibration: CalibrationReport) -> Dict[str, Any]:
    """Append *calibration* to the ledger as a ``calibration`` record.

    The three binding fields keep the names
    :func:`~.adjudicate.calibration_gate` matches on
    (``manifest_id``/``adjudicate_spec_hash``/``roster_fingerprint``), and
    ``marks`` is the per-item mapping the gate counts its spot-check
    obligations against -- not a summary, because the gate asks about
    specific ids.
    """
    payload = calibration.as_dict()
    return ledger.append(
        "calibration",
        manifest_id=calibration.manifest_id,
        adjudicate_spec_hash=calibration.adjudicate_spec_hash,
        roster_fingerprint=calibration.roster_fingerprint,
        band=calibration.band,
        marks=dict(calibration.marks),
        agreement=calibration.overall.agreement,
        report=payload,
        source="calibrate",
    )


def record_marks(
    ledger: Any,
    gold: Gold,
    *,
    adjudicate_spec_hash: str,
    roster_fingerprint: str,
) -> Dict[str, Any]:
    """Import one marked table's marks straight into the ledger.

    This is the SPOT-CHECK path: a tranche's spot-check ids are marked on
    their own small table and the next tranche's gate needs those ids to
    carry marks and the band not to be ``not_trusted``.  Nothing here needs
    the results -- the marks alone give R/W -- so the band is computed from
    them directly with :func:`band_for` rather than from a full report.

    Use :func:`record` instead when a whole pilot has been marked: the
    record it writes carries the slices, and those are what tell an operator
    WHY a band came out where it did.

    Raises:
        Refused: a row is still unmarked, or a binding field is missing.
    """
    if gold.unreviewed:
        raise Refused(
            f"{len(gold.unreviewed)} of {len(gold.marks)} spot-check row(s) carry no "
            "mark; the gate asks whether the obligation was discharged, and a blank "
            "is not a mark",
            list(gold.unreviewed),
        )
    if not str(adjudicate_spec_hash) or not str(roster_fingerprint):
        raise Refused(
            "a calibration record must be bound to the adjudicate spec hash and the "
            "resolved roster; without both, no gate will ever match it"
        )
    counts = gold.counts
    slice_ = Agreement(right=counts.get("R", 0), wrong=counts.get("W", 0))
    lower, _upper = slice_.interval
    band = band_for(slice_.agreement, lower, slice_.n)
    return ledger.append(
        "calibration",
        manifest_id=gold.manifest_id,
        adjudicate_spec_hash=str(adjudicate_spec_hash),
        roster_fingerprint=str(roster_fingerprint),
        band=band,
        marks=dict(gold.marks),
        agreement=slice_.agreement,
        report={
            "scope": gold.scope,
            "overall": slice_.as_dict(),
            "counts": dict(counts),
            "gold": [g.as_dict() for g in gold.records],
            "excluded": dict(gold.excluded),
            "answer_sha256": gold.answer_sha256,
        },
        source="read-marks",
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{100.0 * value:.1f}%"


def _interval(slice_: Agreement) -> str:
    lower, upper = slice_.interval
    if lower is None or upper is None:
        return "-"
    return f"{_pct(lower)} - {_pct(upper)}"


def _slice_table(title: str, slices: Mapping[str, Agreement]) -> List[str]:
    lines = [f"### {title}", "", "| | R | W | n | agreement | Wilson 95% |", "|---|---:|---:|---:|---:|---|"]
    if not slices:
        lines.append("| (none) | 0 | 0 | 0 | - | - |")
    for name, slice_ in sorted(slices.items()):
        lines.append(
            f"| {name or '(blank)'} | {slice_.right} | {slice_.wrong} | {slice_.n} | "
            f"{_pct(slice_.agreement)} | {_interval(slice_)} |"
        )
    lines.append("")
    return lines


def _markdown(report_: CalibrationReport) -> str:
    overall = report_.overall
    counts = report_.counts
    lines: List[str] = [
        "# go_mf adjudication calibration",
        "",
        f"- manifest: `{report_.manifest_id or '(unidentified)'}`",
        f"- adjudicate spec: `{report_.adjudicate_spec_hash}`",
        f"- roster: `{report_.roster_fingerprint}`",
        f"- **band: {report_.band}**",
        "",
        "## Agreement",
        "",
        f"- marked R/W: {overall.right}/{overall.n}"
        f" (R {counts.get('R', 0)}, W {counts.get('W', 0)},"
        f" ? {counts.get('?', 0)}, blank {counts.get('blank', 0)})",
        f"- agreement: **{_pct(overall.agreement)}**, Wilson 95% {_interval(overall)}",
        f"- population-weighted: {_pct(report_.weighted_agreement)}"
        f" (weights from {report_.weights_from or 'n/a'})",
        "",
        "Band rule: `trusted` needs agreement >= "
        f"{TRUSTED_AGREEMENT:.2f} and a Wilson lower bound >= {TRUSTED_LOWER:.2f};"
        f" `conditional` needs agreement >= {CONDITIONAL_AGREEMENT:.2f}.",
        "",
    ]
    lines.extend(_slice_table("By class", report_.by_class))
    lines.extend(_slice_table("By source tool", report_.by_source))
    lines.extend(_slice_table("By favoured side (self-preference check)", report_.by_side))
    swap = report_.swap
    lines.extend(
        [
            "## Position swap",
            "",
            f"- pairs: {swap.pairs}",
            f"- flipped final ids: {swap.final_ids}",
            f"- flipped verdict (source-normalised): {swap.verdict}"
            f" -- rate {_pct(swap.rate)}, Wilson 95% "
            + (
                "-"
                if swap.interval[0] is None
                else f"{_pct(swap.interval[0])} - {_pct(swap.interval[1])}"
            ),
            f"- flipped off-menu flag: {swap.off_menu}",
            "",
            "## Counts",
            "",
            f"- off-menu flags: {report_.off_menu_flags}",
            f"- both-acceptable verdicts: {report_.both_acceptable}",
            f"- gold sets: {report_.gold.get('records', 0)}"
            f" ({report_.gold.get('judge', 0)} judge, {report_.gold.get('human', 0)} human)",
            f"- rows with no gold set: {report_.gold.get('excluded', 0)}",
            "",
        ]
    )
    return "\n".join(lines)
