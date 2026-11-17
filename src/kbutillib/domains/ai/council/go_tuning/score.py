"""DID THE CANDIDATE ACTUALLY GET BETTER?  The held-out scorer and the stop rule.

A tuning loop that scores a candidate against a number somebody wrote down
last week is not measuring the candidate; it is measuring the drift between
two days.  So every comparison here is between TWO RUNS OF THE SAME ITEMS MADE
IN THE SAME ITERATION -- the candidate (``go_mf@N``) and a FRESH ``go_mf@4``
baseline -- and both of them must be independent runs in their own stores
(:mod:`.run`, C14), because the engine's reuse key ignores the job id and a
second run in one store silently reports the first run's answers.

WHAT IS MEASURED, AND WHERE IT IS ALLOWED TO BE MEASURED
========================================================
``heldout`` is the SELECTION set.  Its gold is the adjudicated answer (with
Chris's mark overriding it wherever he made one), and it is what
:func:`score` reads.

``pilot`` is a LOCKED TEST SET.  Its gold is Chris's own marks, it is read
exactly once, by :func:`audit`, for the single candidate proposed for
promotion -- and :func:`score` REFUSES to include a pilot item at all.  Not
"filters out": refuses.  A locked test set leaks by accident, once, in a
hurry, and the leak is invisible afterwards because the pilot number looks
fine; the only defence that works is a function that will not run.

``regression`` (items both councils already agreed on) and ``decline_guard``
(items in population proportion) are GUARDS.  They do not reward a candidate
for anything; they take a gain away from one that improved contested items by
breaking uncontested ones or by shifting how often the council declines.

THE PRIMARY METRIC IS EXACT SET EQUALITY
========================================
``primary`` is the share of scored conflict items whose final id set equals
the gold set EXACTLY, and a flip is set inequality (C15).  Not an F1, not a
Jaccard: the product's answer is the set, a superset is a wrong answer in the
same way a subset is, and a partial-credit metric would let a candidate that
systematically over-picks read as an improvement.

Items whose gold is OFF-MENU are excluded from the denominator, because no
answer drawn from the menu can equal them -- and then counted separately,
because they are the reason the loop eventually has to stop (see
:func:`stop_rule`): once most of what is still wrong is wrong for a reason no
prompt can fix, further tuning is measuring nothing.

A GAIN IS FOUR TESTS, NOT ONE
=============================
A candidate counts as a gain only when ALL of these hold:

1. ``gain > 2 * noise_floor`` -- bigger than twice what two identical
   baselines disagree by (:func:`measure_noise`).
2. the lower bound of a paired bootstrap 95% interval on the gain is ``> 0``,
   resampled by ENTITY so two menus for one protein cannot count as two
   independent observations (:func:`paired_bootstrap`).
3. ``candidate decline rate <= baseline decline rate + 0.02``.
4. ``candidate regression flip rate <= noise_floor + 0.02``.

Tests 1 and 2 are both required on purpose and neither implies the other: a
large gain on 30 items can have a negative interval, and a tiny gain on 4,000
items can have a positive one while being indistinguishable from drift.

A guard whose pool is EMPTY fails its test rather than passing it by default.
A run with no decline guard has not shown the decline rate held; reporting
that as a gain would be the one failure mode the guard exists to catch.

AND REPLACING THE INCUMBENT IS A FIFTH TEST (C19)
=================================================
The incumbent starts as ``go_mf@4``.  A candidate that passes the gain rule
replaces it only if it ALSO scores a higher primary than the incumbent ON THE
IDENTICAL LABELLED SUBSET; ties keep the older version.  That subset
condition is why :func:`score` takes the incumbent as a RUN and re-measures
it here, rather than comparing against a primary recorded in an earlier
iteration: gold grows between iterations, so last week's number was computed
over fewer labelled items and is not comparable to this week's.

Nothing in this module submits a job, spends a token or opens a network
connection.  It reads two finished stores and writes to the ledger.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from ..types import canonical_sha256

# ``_opened`` and ``_row_status`` are :mod:`.conflicts`' own helpers, imported
# rather than re-implemented.  The first is the "a store argument may be a URL
# or an already-open store" contract every reader in this package honours
# (:mod:`.adjudicate` imports it too).  The second is the ADMISSION TEST for a
# settled row, including its one subtlety: an ``ok`` item carrying NO verdict
# is not an empty selection.  A second copy of that rule here would be a
# second place for it to drift, and a drifted copy would reclassify declines.
from .conflicts import Refused, _opened, _row_status, selected_set

__all__ = [
    "BOOTSTRAP_LEVEL",
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "DECLINE_TOLERANCE",
    "GUARD_POOLS",
    "MAX_CANDIDATES",
    "MAX_WITHOUT_IMPROVEMENT",
    "NOT_TRUSTED",
    "OFF_MENU_CEILING",
    "REGRESSION_TOLERANCE",
    "SCORE_POOLS",
    "STOP_REASONS",
    "RunRef",
    "Score",
    "audit",
    "measure_noise",
    "paired_bootstrap",
    "score",
    "stop_rule",
]

#: The pools :func:`score` reads.  ``pilot`` is NOT one of them and may not be
#: added by a caller -- see :func:`score`.
SCORE_POOLS = ("heldout", "regression", "decline_guard")

#: The two guard slices, whose union is the noise floor's domain (C15).
GUARD_POOLS = ("regression", "decline_guard")

#: The band a calibration must not be in for judge-derived gold to be usable.
NOT_TRUSTED = "not_trusted"

#: How much worse the candidate's decline rate may be than the baseline's.
DECLINE_TOLERANCE = 0.02

#: How much above the noise floor the candidate's regression flip rate may sit.
REGRESSION_TOLERANCE = 0.02

#: Paired-bootstrap parameters.  The seed is FIXED and recorded on every
#: score: an interval nobody can regenerate is not evidence, and a seed drawn
#: from the clock makes the gate's own verdict unrepeatable.
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20261117
BOOTSTRAP_LEVEL = 0.95

#: Stop-rule thresholds (C19).
MAX_WITHOUT_IMPROVEMENT = 3
MAX_CANDIDATES = 8
OFF_MENU_CEILING = 0.5

#: Why a run stopped, in the order :func:`stop_rule` tests them.
STOP_REASONS = (
    "no_improvement_streak",
    "off_menu_ceiling",
    "candidate_budget",
)


# ---------------------------------------------------------------------------
# naming a run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRef:
    """One finished job, and the store it is in.

    A job id alone cannot name a measurement here: C14 puts every independent
    run in its OWN store, so "job J" is ambiguous across a loop's files and
    the pair is the identity.
    """

    store: Any
    job_id: str

    @classmethod
    def of(cls, value: Any) -> "RunRef":
        """Coerce a ``RunRef``, a ``(store, job_id)`` pair or a mapping.

        The pair form is what a caller holding a :class:`~.run.FreshRun`
        report has to hand, and the mapping form is that report itself.
        """
        if isinstance(value, RunRef):
            return value
        if isinstance(value, Mapping):
            store = value.get("store")
            job_id = value.get("job_id")
            if store is None or job_id is None:
                raise Refused(
                    "a run mapping must carry both 'store' and 'job_id'; got "
                    f"{sorted(value)}"
                )
            return cls(store, str(job_id))
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return cls(value[0], str(value[1]))
        raise Refused(
            "a run is a RunRef, a (store, job_id) pair or a mapping with "
            f"'store' and 'job_id'; got {type(value).__name__}"
        )

    def identity(self) -> Dict[str, Any]:
        """``{"store", "job_id"}`` for a ledger record."""
        return {"store": _store_label(self.store), "job_id": self.job_id}


def _store_label(store: Any) -> str:
    """How a store is named in a record: its URL when it has one."""
    if isinstance(store, str):
        return store
    return str(getattr(store, "url", None) or store)


# ---------------------------------------------------------------------------
# gold
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GoldEntry:
    item_id: str
    ids: frozenset
    off_menu: bool
    provenance: str
    mark: str = ""


def _gold_entry(item_id: str, payload: Any) -> _GoldEntry:
    if hasattr(payload, "ids") and hasattr(payload, "off_menu"):
        return _GoldEntry(
            item_id=str(item_id),
            ids=frozenset(str(i) for i in (payload.ids or ())),
            off_menu=bool(payload.off_menu),
            provenance=str(getattr(payload, "provenance", "") or ""),
            mark=str(getattr(payload, "mark", "") or ""),
        )
    if isinstance(payload, Mapping):
        return _GoldEntry(
            item_id=str(item_id),
            ids=frozenset(str(i) for i in (payload.get("ids") or ())),
            off_menu=bool(payload.get("off_menu")),
            provenance=str(payload.get("provenance") or ""),
            mark=str(payload.get("mark") or ""),
        )
    if isinstance(payload, (set, frozenset, tuple, list)):
        return _GoldEntry(
            item_id=str(item_id),
            ids=frozenset(str(i) for i in payload),
            off_menu=False,
            provenance="",
        )
    raise Refused(
        f"gold for item {item_id!r} is a {type(payload).__name__}; expected a "
        "GoldRecord, a mapping with 'ids', or an iterable of GO ids"
    )


def _as_gold(gold: Any) -> Dict[str, _GoldEntry]:
    """Normalise whatever the caller merged into ``item_id -> _GoldEntry``.

    Accepts a :class:`~.table.Gold`, a sequence of ``GoldRecord``-shaped
    objects, or a mapping keyed by item id.  The MERGE is the caller's: this
    function is given the result of "Chris's mark where he made one, the
    adjudicated answer otherwise" and does not re-derive it, because which
    source wins is a policy decision and not a detail of arithmetic.
    """
    if gold is None:
        return {}
    records = getattr(gold, "records", None)
    if records is not None and not isinstance(gold, Mapping):
        return {str(r.item_id): _gold_entry(r.item_id, r) for r in records}
    if isinstance(gold, Mapping):
        return {str(k): _gold_entry(k, v) for k, v in gold.items()}
    out: Dict[str, _GoldEntry] = {}
    for row in gold:
        item_id = (
            row.get("item_id") if isinstance(row, Mapping) else getattr(row, "item_id", None)
        )
        if item_id is None:
            raise Refused("a gold row carries no item_id")
        out[str(item_id)] = _gold_entry(str(item_id), row)
    return out


# ---------------------------------------------------------------------------
# the manifest's pools
# ---------------------------------------------------------------------------


def _entries(manifest: Any) -> Tuple[Mapping[str, Any], ...]:
    if isinstance(manifest, Mapping):
        return tuple(manifest.get("entries") or ())
    return tuple(getattr(manifest, "entries", ()) or ())


def _manifest_id(manifest: Any) -> str:
    if isinstance(manifest, Mapping):
        return str(manifest.get("manifest_id") or "")
    return str(getattr(manifest, "manifest_id", "") or "")


def _pool_items(manifest: Any, pool: str) -> Tuple[str, ...]:
    """The unique ``item_id``s of *pool*, in manifest order.

    De-duplicated because a pilot row may carry a position-swapped twin as a
    second entry over the same item, and a ``go_mf`` run has no orientation
    to swap: the two entries are one question and must not be counted twice.
    """
    out: List[str] = []
    seen = set()
    for entry in _entries(manifest):
        if str(entry.get("pool")) != pool:
            continue
        item_id = str(entry.get("item_id"))
        if item_id in seen:
            continue
        seen.add(item_id)
        out.append(item_id)
    return tuple(out)


def _entity_of(manifest: Any) -> Dict[str, str]:
    """``item_id -> entity_hash`` over every entry, for bootstrap grouping."""
    return {str(e.get("item_id")): str(e.get("entity_hash") or "") for e in _entries(manifest)}


# ---------------------------------------------------------------------------
# reading a run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Answer:
    item_id: str
    status: str
    selected: frozenset
    entity_hash: str
    data_hash: str
    evidence_hash: str
    menu_hash: str

    @property
    def declined(self) -> bool:
        """An ``ok`` item with an EMPTY selected set (C15).

        An item that errored is not a decline: nobody decided it.  Collapsing
        the two would let a candidate whose calls fail look like a candidate
        that got cautious.
        """
        return self.status == "ok" and not self.selected


def _read(run: RunRef, item_ids: Sequence[str]) -> Tuple[Dict[str, _Answer], Dict[str, Any]]:
    """``({item_id: _Answer}, job)`` for *item_ids*, from one finished job.

    Every item the job holds is read, not only the wanted ones: the
    completeness check below has to distinguish "the item is absent from this
    run" from "the item is present and did not settle ok", and those are
    different defects with different fixes.
    """
    wanted = set(str(i) for i in item_ids)
    with _opened(run.store) as store:
        job = store.get_job(run.job_id)
        if job is None:
            raise Refused(
                f"no job {run.job_id!r} in {_store_label(run.store)}", [run.job_id]
            )
        answers: Dict[str, _Answer] = {}
        for row in store.get_items(run.job_id):
            item_id = str(row["item_id"])
            if wanted and item_id not in wanted:
                continue
            answers[item_id] = _Answer(
                item_id=item_id,
                status=_row_status(row),
                selected=selected_set(row.get("verdict")),
                entity_hash=str(row.get("entity_hash") or ""),
                data_hash=str(row.get("data_hash") or ""),
                evidence_hash=canonical_sha256(row.get("evidence")),
                menu_hash=canonical_sha256(row.get("menu")),
            )
        return answers, dict(job)


def _require_comparable(
    item_ids: Sequence[str],
    runs: Sequence[Tuple[str, RunRef, Mapping[str, _Answer]]],
) -> None:
    """Refuse unless every run settled every required item on the SAME question.

    C15, in three parts, each of which has its own failure mode:

    * MISSING -- a run that never answered an item cannot be compared on it,
      and quietly dropping it would let a candidate improve its score by
      failing to run on the items it is bad at.
    * NOT ``ok`` -- an errored or undecided item is not a measurement.  C15
      requires complete ok results, so an incomplete run is refused rather
      than scored over what happened to land.
    * A DIFFERENT QUESTION -- the frozen evidence and menu hashes must match
      across the runs.  Two runs over "the same item id" with different menus
      are not a comparison at all, and this is the one defect that produces a
      plausible-looking number from nonsense input.
    """
    problems: List[str] = []
    offending: List[str] = []

    for label, _run, answers in runs:
        missing = sorted(set(item_ids) - set(answers))
        if missing:
            problems.append(
                f"{label} has no result for {len(missing)} required item(s)"
            )
            offending.extend(missing)
        bad = sorted(
            f"{i}:{answers[i].status}" for i in item_ids
            if i in answers and answers[i].status != "ok"
        )
        if bad:
            problems.append(f"{label} has {len(bad)} item(s) that did not settle ok")
            offending.extend(b.split(":", 1)[0] for b in bad)

    if not problems:
        reference_label, _reference_run, reference = runs[0]
        for label, _run, answers in runs[1:]:
            for item_id in item_ids:
                a, b = reference[item_id], answers[item_id]
                for field_name in ("evidence_hash", "menu_hash", "data_hash"):
                    if getattr(a, field_name) != getattr(b, field_name):
                        problems.append(
                            f"item {item_id} was asked a different question in "
                            f"{reference_label} and {label}: {field_name} "
                            f"{getattr(a, field_name)[:12]}... vs "
                            f"{getattr(b, field_name)[:12]}..."
                        )
                        offending.append(item_id)
                        break

    if problems:
        raise Refused(
            "the two runs are not comparable over the required manifest items: "
            + "; ".join(problems[:6])
            + ("" if len(problems) <= 6 else f" (+{len(problems) - 6} more)"),
            sorted(set(offending)),
        )


# ---------------------------------------------------------------------------
# the metrics
# ---------------------------------------------------------------------------


def _exact(answers: Mapping[str, _Answer], gold: Mapping[str, _GoldEntry], item_ids: Sequence[str]) -> Dict[str, bool]:
    """``item_id -> (final set == gold set)`` over *item_ids*."""
    return {i: answers[i].selected == gold[i].ids for i in item_ids}


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """A share, or ``None`` when there is nothing to take a share of.

    ``None`` rather than ``0.0``: an empty slice has not demonstrated a rate
    of zero, and a guard that read it as zero would pass by having no data.
    """
    return None if denominator <= 0 else numerator / denominator


def _decline(answers: Mapping[str, _Answer], item_ids: Sequence[str]) -> Dict[str, Any]:
    declined = [i for i in item_ids if answers[i].declined]
    return {
        "n": len(item_ids),
        "declined": len(declined),
        "rate": _rate(len(declined), len(item_ids)),
        "item_ids": declined,
    }


def _flips(
    answers: Mapping[str, _Answer],
    reference: Mapping[str, Any],
    item_ids: Sequence[str],
) -> Dict[str, Any]:
    """Share of *item_ids* whose selected set differs from *reference*'s.

    *reference* maps an item id to a set of ids; it is the same iteration's
    fresh baseline for a candidate (C15) and the originally agreed set when
    the baseline's own drift is what is being measured.
    """
    usable = [i for i in item_ids if i in reference]
    flipped = [i for i in usable if answers[i].selected != frozenset(reference[i])]
    return {
        "n": len(usable),
        "unavailable": len(item_ids) - len(usable),
        "flipped": len(flipped),
        "rate": _rate(len(flipped), len(usable)),
        "item_ids": flipped,
    }


def paired_bootstrap(
    candidate: Mapping[str, bool],
    baseline: Mapping[str, bool],
    groups: Mapping[str, str],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: Any = BOOTSTRAP_SEED,
    level: float = BOOTSTRAP_LEVEL,
) -> Dict[str, Any]:
    """A paired bootstrap interval on ``mean(candidate) - mean(baseline)``.

    PAIRED: both runs answered the same items, so the resample draws an item
    once and takes both outcomes.  Resampling the two runs independently
    would throw away the pairing and widen the interval for no reason.

    GROUPED BY ENTITY: the unit drawn is the entity group, not the item.  Two
    menus for one protein are correlated observations, and treating them as
    independent would shrink the interval until a protein that happens to
    appear twice could decide the gate.

    DETERMINISTIC: groups are drawn in sorted order from a
    :class:`random.Random` seeded with *seed* only, so the same inputs and
    the same seed give the same interval on any machine, in any process, in
    any order relative to other code's use of the global RNG.

    Returns:
        ``point`` (the observed gain), ``lower``/``upper`` (the interval
        bounds, ``None`` when there is nothing to resample), ``resamples``,
        ``seed``, ``level``, ``groups`` and ``n``.  The bounds are empirical
        quantiles of the resampled gains by NEAREST RANK -- no interpolation,
        because the resample distribution is discrete and an interpolated
        bound would claim a precision the 1,000 draws do not carry.
    """
    item_ids = sorted(set(candidate) & set(baseline))
    by_group: Dict[str, List[str]] = {}
    for item_id in item_ids:
        by_group.setdefault(str(groups.get(item_id) or item_id), []).append(item_id)
    keys = sorted(by_group)

    point = (
        None
        if not item_ids
        else sum(1 for i in item_ids if candidate[i]) / len(item_ids)
        - sum(1 for i in item_ids if baseline[i]) / len(item_ids)
    )
    out: Dict[str, Any] = {
        "point": point,
        "lower": None,
        "upper": None,
        "resamples": int(resamples),
        "seed": seed,
        "level": float(level),
        "groups": len(keys),
        "n": len(item_ids),
    }
    if not keys or resamples <= 0:
        return out

    rng = random.Random(seed)
    deltas: List[float] = []
    for _ in range(int(resamples)):
        drawn: List[str] = []
        for _k in range(len(keys)):
            drawn.extend(by_group[keys[rng.randrange(len(keys))]])
        if not drawn:  # pragma: no cover - a group is never empty
            continue
        hits_c = sum(1 for i in drawn if candidate[i])
        hits_b = sum(1 for i in drawn if baseline[i])
        deltas.append((hits_c - hits_b) / len(drawn))
    if not deltas:  # pragma: no cover - resamples > 0 always yields draws
        return out
    deltas.sort()
    alpha = (1.0 - float(level)) / 2.0
    lo = int(math.floor(alpha * len(deltas)))
    hi = min(len(deltas) - 1, int(math.ceil((1.0 - alpha) * len(deltas))) - 1)
    out["lower"] = deltas[min(lo, len(deltas) - 1)]
    out["upper"] = deltas[max(hi, 0)]
    return out


# ---------------------------------------------------------------------------
# noise
# ---------------------------------------------------------------------------


def measure_noise(
    job1: Any,
    job2: Any,
    manifest: Any,
    *,
    slices: Sequence[str] = GUARD_POOLS,
) -> Dict[str, Any]:
    """How much two IDENTICAL baselines disagree -- the noise floor (C15).

    Args:
        job1: one iteration-0 ``go_mf@4`` run (a :class:`RunRef`, a
            ``(store, job_id)`` pair or a :class:`~.run.FreshRun`).
        job2: the other.  A DIFFERENT store: two @4 runs in one store are not
            two measurements, because the second reads the first's answers
            back out of the call cache (see :mod:`.run`).
        manifest: the sample whose guard slices define the domain.
        slices: the pools the floor is measured over.  The guards, because
            they are the slices whose items are not supposed to move at all.

    Returns:
        ``noise_floor`` -- the fraction of UNIQUE keys in the UNION of the
        guard slices whose selected sets differ between the two runs -- plus
        ``per_slice`` (the same fraction within each slice, which is what
        tells an operator whether the floor is coming from the regression
        slice or from the decline guard), ``n``, ``flipped``, ``flipped_ids``
        and both runs' identities.

    Raises:
        Refused: the two runs are the same job in the same store; they ran
            different query types; or they are not comparable over the guard
            items (see C15's completeness rule).
    """
    run1, run2 = RunRef.of(job1), RunRef.of(job2)
    if _store_label(run1.store) == _store_label(run2.store) and run1.job_id == run2.job_id:
        raise Refused(
            "the noise floor is the disagreement between TWO runs; the same job "
            f"({run1.job_id}) was named twice, which measures zero by construction"
        )

    per_slice_ids = {name: _pool_items(manifest, name) for name in slices}
    union: List[str] = []
    for name in slices:
        for item_id in per_slice_ids[name]:
            if item_id not in union:
                union.append(item_id)
    if not union:
        raise Refused(
            "the manifest's guard slices ("
            + ", ".join(slices)
            + ") are empty, so there is nothing to measure a noise floor over"
        )

    answers1, job_a = _read(run1, union)
    answers2, job_b = _read(run2, union)
    query_a = f"{job_a.get('query_name')}@{job_a.get('query_version')}"
    query_b = f"{job_b.get('query_name')}@{job_b.get('query_version')}"
    if query_a != query_b:
        raise Refused(
            f"a noise floor compares two runs of the SAME query type; got "
            f"{query_a} and {query_b}, whose disagreement is a difference "
            "between versions and not noise"
        )
    _require_comparable(union, [("baseline 1", run1, answers1), ("baseline 2", run2, answers2)])

    reference = {i: answers1[i].selected for i in union}
    overall = _flips(answers2, reference, union)
    out: Dict[str, Any] = {
        "query": query_a,
        "manifest_id": _manifest_id(manifest),
        "slices": list(slices),
        "n": overall["n"],
        "flipped": overall["flipped"],
        "noise_floor": overall["rate"] or 0.0,
        "flipped_ids": overall["item_ids"],
        "per_slice": {
            name: {
                k: v
                for k, v in _flips(answers2, reference, per_slice_ids[name]).items()
                if k != "item_ids"
            }
            for name in slices
        },
        "runs": [run1.identity(), run2.identity()],
    }
    return out


# ---------------------------------------------------------------------------
# the score
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Score:
    """One candidate, measured against one fresh baseline over one pool."""

    pool: str
    audit: bool
    band: str
    manifest_id: str
    candidate: Mapping[str, Any]
    baseline: Mapping[str, Any]
    scored: Mapping[str, Any]
    primary: Mapping[str, Any]
    bootstrap: Mapping[str, Any]
    decline: Mapping[str, Any]
    regression: Mapping[str, Any]
    noise_floor: Optional[float]
    gain: Mapping[str, Any]
    incumbent: Mapping[str, Any]
    heldout: Mapping[str, Any]
    secondary: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_gain(self) -> bool:
        return bool(self.gain.get("is_gain"))

    @property
    def replaces_incumbent(self) -> bool:
        return bool(self.incumbent.get("replaced"))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "pool": self.pool,
            "audit": self.audit,
            "band": self.band,
            "manifest_id": self.manifest_id,
            "candidate": dict(self.candidate),
            "baseline": dict(self.baseline),
            "scored": dict(self.scored),
            "primary": dict(self.primary),
            "bootstrap": dict(self.bootstrap),
            "decline": dict(self.decline),
            "regression": dict(self.regression),
            "noise_floor": self.noise_floor,
            "gain": dict(self.gain),
            "incumbent": dict(self.incumbent),
            "heldout": dict(self.heldout),
            "secondary": dict(self.secondary),
        }

    def record(self, ledger: Any) -> Dict[str, Any]:
        """Append this score to the ledger as a ``score`` record.

        The fields :func:`stop_rule` matches on are promoted to the top of
        the record rather than left inside ``metrics``: the stop rule is the
        one reader that must work off the ledger alone, and a rule that had
        to reach into a nested report to find out whether a candidate
        improved would break the first time the report's shape changed.
        """
        return ledger.append(
            "score",
            audit=bool(self.audit),
            pool=self.pool,
            manifest_id=self.manifest_id,
            band=self.band,
            candidate=dict(self.candidate),
            baseline=dict(self.baseline),
            incumbent=dict(self.incumbent),
            noise_floor=self.noise_floor,
            primary=dict(self.primary),
            gain=dict(self.gain),
            replaces_incumbent=self.replaces_incumbent,
            scored=dict(self.scored),
            heldout=dict(self.heldout),
            metrics=self.as_dict(),
        )


def _usage(run: RunRef) -> Dict[str, Any]:
    """Token usage for one run, or why it could not be read."""
    from .adjudicate import usage_report

    with _opened(run.store) as store:
        try:
            report = usage_report(store, [run.job_id])
        except TypeError as exc:
            return {"unavailable": str(exc)}
    return {k: v for k, v in report.items() if k != "jobs"}


def _latency(run: RunRef) -> Dict[str, Any]:
    """Per-attempt service time for one run.  Reported only, never gated on.

    Service time is ``finished_at - started_at`` on the attempt row; the wall
    span is the whole job's.  Needs the ``attempts`` table, which the store
    contract does not publish a method for, so a store without an ``.engine``
    says so rather than reporting zeros.
    """
    with _opened(run.store) as store:
        engine = getattr(store, "engine", None)
        if engine is None:
            return {"unavailable": "the store exposes no .engine"}
        import sqlalchemy as sa

        from ..store import schema as sch

        join = sch.attempts.join(sch.calls, sch.calls.c.call_id == sch.attempts.c.call_id)
        statement = (
            sa.select(sch.attempts.c.started_at, sch.attempts.c.finished_at)
            .select_from(join)
            .where(sch.calls.c.job_id == run.job_id, sch.attempts.c.attempt_no >= 0)
        )
        starts: List[Any] = []
        ends: List[Any] = []
        service: List[float] = []
        with engine.connect() as conn:
            for row in conn.execute(statement):
                if row.started_at is None or row.finished_at is None:
                    continue
                starts.append(row.started_at)
                ends.append(row.finished_at)
                service.append((row.finished_at - row.started_at).total_seconds())
    if not service:
        return {"attempts": 0, "mean_service_s": None, "max_service_s": None, "wall_s": None}
    return {
        "attempts": len(service),
        "mean_service_s": sum(service) / len(service),
        "max_service_s": max(service),
        "wall_s": (max(ends) - min(starts)).total_seconds(),
    }


def _query_of(job: Mapping[str, Any]) -> str:
    return f"{job.get('query_name')}@{job.get('query_version')}"


def _side(run: RunRef, job: Mapping[str, Any], *, label: str) -> Dict[str, Any]:
    out = run.identity()
    out["query"] = _query_of(job)
    out["role"] = label
    return out


def _check_band(band: str, used: Sequence[_GoldEntry]) -> None:
    """Refuse judge-derived gold under a ``not_trusted`` band.

    The band is the calibration's verdict on the ADJUDICATOR, and gold whose
    provenance is ``judge`` is the adjudicator's own answer.  Scoring against
    it under a ``not_trusted`` band measures the candidate against a judge
    the run has already decided it cannot believe -- a number that looks like
    accuracy and is not.  Chris's own marks are unaffected: they were never
    the judge's, so the band says nothing about them.
    """
    if str(band) != NOT_TRUSTED:
        return
    judged = sorted(g.item_id for g in used if g.provenance != "human")
    if judged:
        raise Refused(
            f"the calibration band is {NOT_TRUSTED!r} and {len(judged)} of the "
            f"{len(used)} gold set(s) in play came from the judge rather than from "
            "Chris; score against Chris-marked gold only, or re-calibrate",
            judged,
        )


def _gain_rule(
    gain: Optional[float],
    bootstrap: Mapping[str, Any],
    decline: Mapping[str, Any],
    regression: Mapping[str, Any],
    noise_floor: Optional[float],
) -> Dict[str, Any]:
    """The four tests, each with the reason it failed."""
    floor = 0.0 if noise_floor is None else float(noise_floor)
    lower = bootstrap.get("lower")
    cand_decline = decline.get("candidate")
    base_decline = decline.get("baseline")
    cand_flip = regression.get("candidate")

    checks: Dict[str, bool] = {}
    reasons: List[str] = []

    checks["gain_over_noise"] = gain is not None and gain > 2.0 * floor
    if not checks["gain_over_noise"]:
        reasons.append(
            f"gain {_num(gain)} is not above twice the noise floor ({2.0 * floor:.4f})"
        )

    checks["bootstrap_lower_positive"] = lower is not None and lower > 0.0
    if not checks["bootstrap_lower_positive"]:
        reasons.append(
            f"the paired bootstrap {int(100 * float(bootstrap.get('level') or 0))}% "
            f"lower bound is {_num(lower)}, not above zero"
        )

    checks["decline_guard"] = (
        cand_decline is not None
        and base_decline is not None
        and cand_decline <= base_decline + DECLINE_TOLERANCE
    )
    if not checks["decline_guard"]:
        if cand_decline is None or base_decline is None:
            reasons.append(
                "the decline guard has no items, so the decline rate has not been "
                "shown to hold"
            )
        else:
            reasons.append(
                f"candidate decline rate {_num(cand_decline)} exceeds the baseline's "
                f"{_num(base_decline)} by more than {DECLINE_TOLERANCE}"
            )

    checks["regression_guard"] = cand_flip is not None and cand_flip <= floor + REGRESSION_TOLERANCE
    if not checks["regression_guard"]:
        if cand_flip is None:
            reasons.append(
                "the regression guard has no comparable items, so uncontested items "
                "have not been shown to hold"
            )
        else:
            reasons.append(
                f"candidate regression flip rate {_num(cand_flip)} exceeds the noise "
                f"floor + {REGRESSION_TOLERANCE} ({floor + REGRESSION_TOLERANCE:.4f})"
            )

    return {
        "gain": gain,
        "is_gain": all(checks.values()),
        "checks": checks,
        "reasons": reasons,
        "thresholds": {
            "noise_multiple": 2.0,
            # As GIVEN, which may be None, and as APPLIED: an unmeasured floor
            # is treated as zero, and a reader comparing a gain against a
            # recorded `None` could not tell what the rule actually required.
            "noise_floor": noise_floor,
            "gain_must_exceed": 2.0 * floor,
            "decline_tolerance": DECLINE_TOLERANCE,
            "regression_tolerance": REGRESSION_TOLERANCE,
            "regression_limit": floor + REGRESSION_TOLERANCE,
        },
    }


def _num(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _subset_digest(item_ids: Sequence[str]) -> str:
    """A digest of the LABELLED SUBSET a primary score was computed over.

    C19 compares a candidate against the incumbent "on the identical
    labelled heldout subset", and gold grows between iterations -- so two
    primaries are only comparable when this digest matches.  Recorded on
    every score so a later reader can tell whether a comparison it is
    tempted to make is legitimate.
    """
    return canonical_sha256(sorted(str(i) for i in item_ids))


def score(
    candidate_store: Any,
    candidate_job: str,
    baseline_store: Any,
    fresh_baseline_job: str,
    gold: Any,
    manifest: Any,
    noise_floor: Optional[float],
    *,
    band: str,
    pools: Sequence[str] = SCORE_POOLS,
    agreed: Optional[Mapping[str, Iterable[str]]] = None,
    incumbent: Any = None,
    incumbent_label: Optional[str] = None,
    ledger: Any = None,
    seed: Any = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> Score:
    """Score one candidate against one fresh ``go_mf@4`` baseline.

    Both jobs must be runs of the SAME items -- the manifest's
    ``heldout`` + ``regression`` + ``decline_guard`` slices -- made in the
    same iteration, each in its own store (:func:`~.run.fresh_run`).

    Args:
        candidate_store: the candidate run's store (an open store or a URL).
        candidate_job: its job id.
        baseline_store: the FRESH ``go_mf@4`` run's store.  A different file:
            see :mod:`.run`.
        fresh_baseline_job: its job id.
        gold: the MERGED gold -- Chris's marks where he made them, the
            adjudicated answer elsewhere.  A :class:`~.table.Gold`, a
            sequence of its records, or an ``item_id ->`` mapping.
        manifest: the sample the runs were made over.
        noise_floor: what :func:`measure_noise` reported.  ``None`` is
            treated as zero for the thresholds and recorded as ``None``, so a
            score computed before any noise was measured says so.
        band: the calibration band in force.  ``not_trusted`` refuses unless
            every gold set in play is Chris's own (see :func:`_check_band`).
        pools: which slices to read.  ``pilot`` is refused.
        agreed: ``item_id -> ids`` for the regression slice, from the extract
            the manifest was sampled from.  Supplies the BASELINE's own
            regression flip rate (how far the fresh @4 drifted from what @4
            originally agreed).  The candidate's gated rate does not need it
            -- C15 compares the candidate to the same iteration's fresh
            baseline -- so this is optional and its absence is reported
            rather than guessed at.
        incumbent: the incumbent's run, when it is not ``go_mf@4``.  ``None``
            means the incumbent IS the fresh baseline, which is the
            iteration-0 state (C19).  Re-measured here over this score's own
            labelled subset; see the module docstring for why a recorded
            primary from an earlier iteration will not do.
        incumbent_label: how to name the incumbent in the record.  Defaults
            to its run's query type.
        ledger: when given, the score is appended as a ``score`` record.
        seed: the paired bootstrap's seed.  Fixed by default.
        resamples: bootstrap resamples.  1,000 by default.

    Returns:
        A :class:`Score`.

    Raises:
        Refused: ``pilot`` was asked for, or reached anyway; the band forbids
            the gold in play; the runs are not comparable over the required
            items (C15); or no eligible gold covers the heldout slice, which
            makes the result unscorable rather than zero.
    """
    asked = tuple(str(p) for p in pools)
    if "pilot" in asked:
        raise Refused(
            "'pilot' is a LOCKED TEST SET and score() will not read it: candidates "
            "are selected on 'heldout' only, and the pilot is read once, by audit(), "
            "for the candidate proposed for promotion"
        )
    unknown = sorted(set(asked) - set(SCORE_POOLS))
    if unknown:
        raise Refused(f"score() reads {', '.join(SCORE_POOLS)}; not {', '.join(unknown)}")

    pool_ids = {name: _pool_items(manifest, name) for name in asked}
    pilot_ids = set(_pool_items(manifest, "pilot"))
    required: List[str] = []
    for name in asked:
        for item_id in pool_ids[name]:
            if item_id not in required:
                required.append(item_id)
    leaked = sorted(set(required) & pilot_ids)
    if leaked:
        raise Refused(
            f"{len(leaked)} item(s) in the requested pools are also in the manifest's "
            "pilot; the pilot is locked and score() will not read a pilot item under "
            "any pool name",
            leaked,
        )

    candidate_run = RunRef(candidate_store, str(candidate_job))
    baseline_run = RunRef(baseline_store, str(fresh_baseline_job))
    cand, cand_job = _read(candidate_run, required)
    base, base_job = _read(baseline_run, required)
    _require_comparable(
        required,
        [("the candidate", candidate_run, cand), ("the fresh baseline", baseline_run, base)],
    )

    gold_by_item = _as_gold(gold)
    required_set = set(required)
    in_play = [g for item_id, g in sorted(gold_by_item.items()) if item_id in required_set]
    _check_band(str(band), in_play)

    heldout_ids = pool_ids.get("heldout", ())
    with_gold = [i for i in heldout_ids if i in gold_by_item]
    off_menu = [i for i in with_gold if gold_by_item[i].off_menu]
    scored_ids = [i for i in with_gold if not gold_by_item[i].off_menu]
    if not scored_ids:
        raise Refused(
            "no eligible gold covers the heldout slice "
            f"({len(heldout_ids)} item(s), {len(with_gold)} with gold, "
            f"{len(off_menu)} of those off-menu), so this candidate is UNSCORABLE. "
            "That is not a score of zero: nothing was measured"
        )

    return _build(
        pool="heldout",
        audit_run=False,
        band=str(band),
        manifest=manifest,
        candidate_run=candidate_run,
        candidate_job=cand_job,
        baseline_run=baseline_run,
        baseline_job=base_job,
        cand=cand,
        base=base,
        gold_by_item=gold_by_item,
        scored_ids=scored_ids,
        off_menu_ids=off_menu,
        pool_ids=pool_ids,
        required=required,
        agreed=agreed,
        noise_floor=noise_floor,
        incumbent=incumbent,
        incumbent_label=incumbent_label,
        seed=seed,
        resamples=resamples,
        ledger=ledger,
    )


def audit(
    candidate_job: Any,
    fresh_baseline_job: Any,
    chris_gold: Any,
    manifest: Any,
    *,
    noise_floor: Optional[float] = None,
    band: str = "",
    ledger: Any = None,
    seed: Any = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> Score:
    """The LOCKED PILOT, read once, for the candidate proposed for promotion.

    The pilot measures generalisation, and it can only do that while nothing
    has been selected on it.  So this is the only function in the package
    that reads ``pool: "pilot"``, it is run ONCE per tuning pass, and its
    gold is CHRIS'S MARKS -- a judge-derived gold set here would make the
    audit a second measurement of the adjudicator rather than an independent
    check of the candidate, which is exactly the thing the pilot exists to
    avoid.

    The guards are NOT part of an audit: ``regression`` and ``decline_guard``
    are their own pools, and an audit is "pool pilot only".  The two gain
    tests that live on the measured pool -- the noise multiple and the
    bootstrap lower bound -- are computed, the two guard tests are reported
    as ``None``, and ``is_gain`` here therefore means "the gain held on the
    locked set", not "this candidate passes the promotion gate".  That gate
    is :func:`score`'s, against the guards, and it has already run.

    Args:
        candidate_job: the candidate's run (:class:`RunRef`, a
            ``(store, job_id)`` pair, or a :class:`~.run.FreshRun`).
        fresh_baseline_job: the same iteration's fresh ``go_mf@4`` run.
        chris_gold: gold for the pilot rows.  Every set in play must carry
            ``provenance == "human"``.
        manifest: the sample the runs were made over.
        noise_floor: what :func:`measure_noise` reported, for the first gain
            test.
        band: recorded for provenance.  No band check: the gold is Chris's,
            and the band is a verdict on the judge.
        ledger: when given, the audit is appended as a ``score`` record with
            ``audit=True``.

    Raises:
        Refused: a gold set in play is not Chris's; the runs are not
            comparable over the pilot items; or no eligible gold covers the
            pilot.
    """
    candidate_run = RunRef.of(candidate_job)
    baseline_run = RunRef.of(fresh_baseline_job)
    pilot_ids = _pool_items(manifest, "pilot")
    if not pilot_ids:
        raise Refused("the manifest has no pilot pool; there is nothing to audit")

    cand, cand_job = _read(candidate_run, pilot_ids)
    base, base_job = _read(baseline_run, pilot_ids)
    _require_comparable(
        list(pilot_ids),
        [("the candidate", candidate_run, cand), ("the fresh baseline", baseline_run, base)],
    )

    gold_by_item = _as_gold(chris_gold)
    with_gold = [i for i in pilot_ids if i in gold_by_item]
    not_human = sorted(i for i in with_gold if gold_by_item[i].provenance != "human")
    if not_human:
        raise Refused(
            f"{len(not_human)} of the {len(with_gold)} pilot gold set(s) did not come "
            "from Chris; the pilot audit is the one measurement that must not be "
            "taken against the judge's own answer",
            not_human,
        )
    off_menu = [i for i in with_gold if gold_by_item[i].off_menu]
    scored_ids = [i for i in with_gold if not gold_by_item[i].off_menu]
    if not scored_ids:
        raise Refused(
            "no eligible Chris-marked gold covers the pilot "
            f"({len(pilot_ids)} item(s), {len(with_gold)} with gold, "
            f"{len(off_menu)} of those off-menu); the audit is unscorable"
        )

    return _build(
        pool="pilot",
        audit_run=True,
        band=str(band),
        manifest=manifest,
        candidate_run=candidate_run,
        candidate_job=cand_job,
        baseline_run=baseline_run,
        baseline_job=base_job,
        cand=cand,
        base=base,
        gold_by_item=gold_by_item,
        scored_ids=scored_ids,
        off_menu_ids=off_menu,
        pool_ids={"pilot": pilot_ids},
        required=list(pilot_ids),
        agreed=None,
        noise_floor=noise_floor,
        incumbent=None,
        incumbent_label=None,
        seed=seed,
        resamples=resamples,
        ledger=ledger,
    )


def _build(
    *,
    pool: str,
    audit_run: bool,
    band: str,
    manifest: Any,
    candidate_run: RunRef,
    candidate_job: Mapping[str, Any],
    baseline_run: RunRef,
    baseline_job: Mapping[str, Any],
    cand: Mapping[str, _Answer],
    base: Mapping[str, _Answer],
    gold_by_item: Mapping[str, _GoldEntry],
    scored_ids: Sequence[str],
    off_menu_ids: Sequence[str],
    pool_ids: Mapping[str, Sequence[str]],
    required: Sequence[str],
    agreed: Optional[Mapping[str, Iterable[str]]],
    noise_floor: Optional[float],
    incumbent: Any,
    incumbent_label: Optional[str],
    seed: Any,
    resamples: int,
    ledger: Any,
) -> Score:
    """The arithmetic both :func:`score` and :func:`audit` do, once."""
    cand_exact = _exact(cand, gold_by_item, scored_ids)
    base_exact = _exact(base, gold_by_item, scored_ids)
    cand_primary = _rate(sum(1 for v in cand_exact.values() if v), len(scored_ids))
    base_primary = _rate(sum(1 for v in base_exact.values() if v), len(scored_ids))
    gain = None if cand_primary is None or base_primary is None else cand_primary - base_primary

    bootstrap = paired_bootstrap(
        cand_exact,
        base_exact,
        _entity_of(manifest),
        resamples=resamples,
        seed=seed,
    )

    guard_ids = pool_ids.get("decline_guard", ())
    regression_ids = pool_ids.get("regression", ())
    cand_decline = _decline(cand, guard_ids)
    base_decline = _decline(base, guard_ids)
    decline = {
        "n": len(guard_ids),
        "candidate": cand_decline["rate"],
        "baseline": base_decline["rate"],
        "candidate_declined": cand_decline["declined"],
        "baseline_declined": base_decline["declined"],
        "tolerance": DECLINE_TOLERANCE,
        "limit": None if base_decline["rate"] is None else base_decline["rate"] + DECLINE_TOLERANCE,
    }

    # C15: the candidate's regression flips are measured against THE SAME
    # ITERATION'S fresh baseline, so a drift both runs share is not charged to
    # the candidate.  The baseline's own flips are measured against the
    # originally agreed set, which is how far @4 has moved since the extract --
    # a different question, and the only one `agreed` is needed for.
    fresh_reference = {i: base[i].selected for i in regression_ids}
    cand_regression = _flips(cand, fresh_reference, regression_ids)
    agreed_map = (
        None
        if agreed is None
        else {str(k): frozenset(str(i) for i in v) for k, v in agreed.items()}
    )
    base_regression = (
        None if agreed_map is None else _flips(base, agreed_map, regression_ids)
    )
    cand_vs_agreed = None if agreed_map is None else _flips(cand, agreed_map, regression_ids)
    regression = {
        "n": len(regression_ids),
        "candidate": cand_regression["rate"],
        "candidate_flipped": cand_regression["flipped"],
        "compared_to": "the same iteration's fresh baseline (C15)",
        "baseline": None if base_regression is None else base_regression["rate"],
        "baseline_compared_to": "the agreed set from the extract",
        "candidate_vs_agreed": None if cand_vs_agreed is None else cand_vs_agreed["rate"],
        "tolerance": REGRESSION_TOLERANCE,
        # The limit ACTUALLY enforced, not a restatement of the input: an
        # unmeasured floor is treated as zero by the rule (see `_gain_rule`),
        # so reporting `None` here would show a threshold nothing applied.
        "limit": (0.0 if noise_floor is None else float(noise_floor))
        + REGRESSION_TOLERANCE,
    }
    if agreed_map is None:
        regression["baseline_unavailable"] = (
            "no `agreed` sets were supplied, so how far the fresh baseline has "
            "drifted from the originally agreed answer was not measured"
        )

    gain_block = (
        _gain_rule(gain, bootstrap, decline, regression, noise_floor)
        if not audit_run
        else _audit_gain_rule(gain, bootstrap, noise_floor)
    )

    incumbent_block = _incumbent(
        incumbent,
        incumbent_label,
        baseline_run=baseline_run,
        baseline_job=baseline_job,
        base_exact=base_exact,
        base_primary=base_primary,
        candidate_run=candidate_run,
        cand=cand,
        scored_ids=scored_ids,
        gold_by_item=gold_by_item,
        candidate_primary=cand_primary,
        is_gain=bool(gain_block.get("is_gain")),
        audit_run=audit_run,
    )

    differing = [i for i in required if cand[i].selected != base[i].selected]
    secondary = {
        "conflict_rate": {
            "overall": _rate(len(differing), len(required)),
            "n": len(required),
            "differing": len(differing),
            "by_pool": {
                name: _rate(
                    sum(1 for i in ids if cand[i].selected != base[i].selected), len(ids)
                )
                for name, ids in pool_ids.items()
            },
        },
        "tokens": {
            "candidate": _usage(candidate_run),
            "baseline": _usage(baseline_run),
        },
        "latency": {
            "candidate": _latency(candidate_run),
            "baseline": _latency(baseline_run),
        },
        "note": "reported only; nothing here gates a gain",
    }

    result = Score(
        pool=pool,
        audit=audit_run,
        band=band,
        manifest_id=_manifest_id(manifest),
        candidate=_side(candidate_run, candidate_job, label="candidate"),
        baseline=_side(baseline_run, baseline_job, label="fresh_baseline"),
        scored={
            "n": len(scored_ids),
            "item_ids": list(scored_ids),
            "digest": _subset_digest(scored_ids),
            "pools": {name: len(ids) for name, ids in pool_ids.items()},
            "required": len(required),
        },
        primary={
            "candidate": cand_primary,
            "baseline": base_primary,
            "gain": gain,
            "candidate_exact": sum(1 for v in cand_exact.values() if v),
            "baseline_exact": sum(1 for v in base_exact.values() if v),
            "n": len(scored_ids),
        },
        bootstrap=bootstrap,
        decline=decline,
        regression=regression,
        noise_floor=noise_floor,
        gain=gain_block,
        incumbent=incumbent_block,
        heldout=_off_menu_accounting(
            pool_ids.get(pool, ()),
            gold_by_item,
            scored_ids,
            off_menu_ids,
            incumbent_block,
        ),
        secondary=secondary,
    )
    if ledger is not None:
        result.record(ledger)
    return result


def _audit_gain_rule(
    gain: Optional[float],
    bootstrap: Mapping[str, Any],
    noise_floor: Optional[float],
) -> Dict[str, Any]:
    """The two gain tests an audit can make.  See :func:`audit`."""
    floor = 0.0 if noise_floor is None else float(noise_floor)
    checks = {
        "gain_over_noise": gain is not None and gain > 2.0 * floor,
        "bootstrap_lower_positive": bootstrap.get("lower") is not None
        and bootstrap["lower"] > 0.0,
    }
    reasons: List[str] = []
    if not checks["gain_over_noise"]:
        reasons.append(
            f"gain {_num(gain)} is not above twice the noise floor ({2.0 * floor:.4f})"
        )
    if not checks["bootstrap_lower_positive"]:
        reasons.append(
            f"the paired bootstrap lower bound is {_num(bootstrap.get('lower'))}, "
            "not above zero"
        )
    return {
        "gain": gain,
        "is_gain": all(checks.values()),
        "checks": checks,
        "reasons": reasons,
        "guards": None,
        "note": (
            "an audit is pool 'pilot' only, so the decline and regression guards -- "
            "which live on their own pools -- are not part of it. 'is_gain' here "
            "means the gain held on the locked set, not that the promotion gate "
            "passed; that gate is score()'s"
        ),
        "thresholds": {
            "noise_multiple": 2.0,
            "noise_floor": noise_floor,
            "gain_must_exceed": 2.0 * floor,
        },
    }


def _incumbent(
    incumbent: Any,
    label: Optional[str],
    *,
    baseline_run: RunRef,
    baseline_job: Mapping[str, Any],
    base_exact: Mapping[str, bool],
    base_primary: Optional[float],
    candidate_run: RunRef,
    cand: Mapping[str, _Answer],
    scored_ids: Sequence[str],
    gold_by_item: Mapping[str, _GoldEntry],
    candidate_primary: Optional[float],
    is_gain: bool,
    audit_run: bool,
) -> Dict[str, Any]:
    """Resolve the incumbent and decide whether the candidate replaces it (C19).

    ``incumbent is None`` is the iteration-0 state: the incumbent IS
    ``go_mf@4``, and the fresh baseline in this very comparison is a run of
    it, so its primary on this subset is already measured.  Any other
    incumbent is RE-MEASURED here over the same scored ids, because C19's
    comparison is "on the identical labelled heldout subset" and a primary
    recorded in an earlier iteration was computed over less gold.
    """
    if incumbent is None:
        resolved = {
            "label": str(label or _query_of(baseline_job)),
            "store": baseline_run.identity()["store"],
            "job_id": baseline_run.job_id,
            "primary": base_primary,
            "exact": sum(1 for v in base_exact.values() if v),
            "on_menu_misses": sum(1 for v in base_exact.values() if not v),
            "n": len(scored_ids),
            "measured": "the fresh baseline in this comparison",
        }
    else:
        run = RunRef.of(incumbent)
        answers, job = _read(run, scored_ids)
        # Against the CANDIDATE as well as on its own: C19's comparison is on
        # the identical labelled subset, and an incumbent run that answered
        # the same item ids over a different menu would satisfy "same ids"
        # while comparing two different questions.
        _require_comparable(
            list(scored_ids),
            [("the candidate", candidate_run, cand), ("the incumbent", run, answers)],
        )
        exact = _exact(answers, gold_by_item, scored_ids)
        resolved = {
            "label": str(label or _query_of(job)),
            "store": run.identity()["store"],
            "job_id": run.job_id,
            "primary": _rate(sum(1 for v in exact.values() if v), len(scored_ids)),
            "exact": sum(1 for v in exact.values() if v),
            "on_menu_misses": sum(1 for v in exact.values() if not v),
            "n": len(scored_ids),
            "measured": "re-measured over this score's own labelled subset",
        }

    resolved["subset_digest"] = _subset_digest(scored_ids)
    if audit_run:
        resolved["replaced"] = False
        resolved["detail"] = (
            "an audit does not promote anything; the incumbent is reported for "
            "provenance only"
        )
        return resolved

    higher = (
        candidate_primary is not None
        and resolved["primary"] is not None
        and candidate_primary > resolved["primary"]
    )
    resolved["higher_primary"] = higher
    resolved["replaced"] = bool(is_gain and higher)
    if resolved["replaced"]:
        resolved["detail"] = (
            f"the candidate passes the gain rule and scores "
            f"{_num(candidate_primary)} against the incumbent's "
            f"{_num(resolved['primary'])} on the same {len(scored_ids)} labelled "
            "item(s), so it becomes the incumbent"
        )
    elif not is_gain:
        resolved["detail"] = "the candidate did not pass the gain rule"
    elif candidate_primary == resolved["primary"]:
        resolved["detail"] = (
            f"a tie at {_num(candidate_primary)} on the same labelled subset; "
            "C19 keeps the older version"
        )
    else:
        resolved["detail"] = (
            f"the candidate's primary {_num(candidate_primary)} is not above the "
            f"incumbent's {_num(resolved['primary'])} on the same labelled subset"
        )
    return resolved


def _off_menu_accounting(
    pool_item_ids: Sequence[str],
    gold_by_item: Mapping[str, _GoldEntry],
    scored_ids: Sequence[str],
    off_menu_ids: Sequence[str],
    incumbent: Mapping[str, Any],
) -> Dict[str, Any]:
    """C19's off-menu ratio: how much of what is still wrong is unfixable.

    The denominator is the off-menu rows PLUS the on-menu exact-match misses
    under the incumbent -- that is, everything still wrong under the best
    version there is.  A zero denominator is NOT a stop: nothing is wrong, so
    there is no share of it to take (and ``0/0`` would otherwise read as a
    reason to give up on a run that is doing fine).
    """
    off_menu = len(off_menu_ids)
    misses = int(incumbent.get("on_menu_misses") or 0)
    denominator = off_menu + misses
    return {
        "pool_items": len(pool_item_ids),
        "with_gold": len([i for i in pool_item_ids if i in gold_by_item]),
        "scored": len(scored_ids),
        "off_menu_gold_rows": off_menu,
        "off_menu_item_ids": list(off_menu_ids),
        "incumbent_on_menu_misses": misses,
        "incumbent": incumbent.get("label"),
        "remaining_failures": denominator,
        "off_menu_share": _rate(off_menu, denominator),
        "ceiling": OFF_MENU_CEILING,
    }


# ---------------------------------------------------------------------------
# the stop rule
# ---------------------------------------------------------------------------


def stop_rule(ledger: Any, *, record: bool = True) -> Dict[str, Any]:
    """Should the tuning loop stop?  Read entirely off the ledger.

    Three reasons, tested in this order (C19):

    ``no_improvement_streak``
        :data:`MAX_WITHOUT_IMPROVEMENT` consecutive candidates that did not
        replace the incumbent.  A candidate without a gain never replaces
        one, so "three consecutive candidates without gain" is the strict
        subset of this that fires first; a candidate WITH a gain that did not
        beat the incumbent on the identical labelled subset counts too,
        because the loop has still not moved.

    ``off_menu_ceiling``
        more than half of what is still wrong under the incumbent is wrong
        because the correct term is not on the menu
        (``off_menu / (off_menu + on-menu misses) > 0.5``).  No prompt can
        fix those, so further tuning would be measuring the retrieval step.
        A ZERO DENOMINATOR DOES NOT STOP -- see
        :func:`_off_menu_accounting`.

    ``candidate_budget``
        :data:`MAX_CANDIDATES` candidates have been tried.

    Audit records are skipped: an audit is the promotion check on the locked
    pilot and is not a candidate attempt.

    Args:
        ledger: the run's :class:`~.ledger.Ledger`.
        record: append a ``stop`` record when the rule fires.  IDEMPOTENT --
            a stop already recorded for the same reason at the same candidate
            count is not written twice, so a loop may ask the rule on every
            iteration without filling the ledger with duplicates.

    Returns:
        ``stop`` (bool), ``reason`` (the first that fired, or ``None``),
        ``detail``, ``reasons`` (every one that fired), plus the counters the
        decision was made from and the ``incumbent`` the chain ended on.
    """
    records = [r for r in ledger.records("score") if not r.get("audit")]
    candidates = len(records)

    incumbent = "go_mf@4"
    streak = 0
    without_gain = 0
    for row in records:
        if bool(row.get("replaces_incumbent")):
            streak = 0
            incumbent = str(
                (row.get("candidate") or {}).get("query")
                or (row.get("incumbent") or {}).get("label")
                or incumbent
            )
        else:
            streak += 1
        if bool((row.get("gain") or {}).get("is_gain")):
            without_gain = 0
        else:
            without_gain += 1

    latest = records[-1] if records else {}
    heldout = dict(latest.get("heldout") or {})
    off_menu_share = heldout.get("off_menu_share")

    fired: List[Dict[str, Any]] = []
    if streak >= MAX_WITHOUT_IMPROVEMENT:
        fired.append(
            {
                "reason": "no_improvement_streak",
                "detail": (
                    f"{streak} consecutive candidate(s) did not replace the "
                    f"incumbent ({incumbent}); the limit is "
                    f"{MAX_WITHOUT_IMPROVEMENT}"
                ),
            }
        )
    if off_menu_share is not None and float(off_menu_share) > OFF_MENU_CEILING:
        fired.append(
            {
                "reason": "off_menu_ceiling",
                "detail": (
                    f"{heldout.get('off_menu_gold_rows')} of the "
                    f"{heldout.get('remaining_failures')} heldout failure(s) "
                    f"remaining under {incumbent} are off-menu "
                    f"({float(off_menu_share):.1%} > {OFF_MENU_CEILING:.0%}); what "
                    "is left is a retrieval problem, not a prompt problem"
                ),
            }
        )
    if candidates >= MAX_CANDIDATES:
        fired.append(
            {
                "reason": "candidate_budget",
                "detail": (
                    f"{candidates} candidate(s) have been scored; the budget is "
                    f"{MAX_CANDIDATES}"
                ),
            }
        )

    out: Dict[str, Any] = {
        "stop": bool(fired),
        "reason": fired[0]["reason"] if fired else None,
        "detail": fired[0]["detail"] if fired else "",
        "reasons": fired,
        "candidates": candidates,
        "consecutive_without_improvement": streak,
        "consecutive_without_gain": without_gain,
        "off_menu_share": off_menu_share,
        "incumbent": incumbent,
        "limits": {
            "max_without_improvement": MAX_WITHOUT_IMPROVEMENT,
            "max_candidates": MAX_CANDIDATES,
            "off_menu_ceiling": OFF_MENU_CEILING,
        },
        "recorded": None,
    }
    if fired and record:
        out["recorded"] = _record_stop(ledger, out)
    return out


def _record_stop(ledger: Any, decision: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Append the ``stop`` record, unless the same stop is already there."""
    for row in reversed(ledger.records("stop")):
        if str(row.get("source")) != "stop_rule":
            continue
        if str(row.get("reason")) == str(decision.get("reason")) and int(
            row.get("candidates") or -1
        ) == int(decision.get("candidates") or 0):
            return None
        break
    return ledger.append(
        "stop",
        reason=str(decision["reason"]),
        detail=str(decision["detail"]),
        source="stop_rule",
        candidates=int(decision["candidates"]),
        consecutive_without_improvement=int(decision["consecutive_without_improvement"]),
        consecutive_without_gain=int(decision["consecutive_without_gain"]),
        off_menu_share=decision.get("off_menu_share"),
        incumbent=decision.get("incumbent"),
        reasons=[dict(r) for r in decision.get("reasons") or ()],
    )
