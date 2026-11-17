"""The seeded sampler that turns classified conflicts into a TUNING MANIFEST.

A manifest is a frozen, reproducible statement of which conflicts get used
for what.  Everything here is a pure function of ``(matched, seed, plan)``, so
the same three inputs produce a BYTE-IDENTICAL file -- there is no clock, no
``uuid``, no iteration over an unordered set, and no RNG call whose position
in a stream depends on anything but the seed and the data.  That property is
the whole point: a tuning result nobody can regenerate the sample for is a
result nobody can check.

THE PILOT IS A LOCKED TEST SET
==============================
``pool: "pilot"`` is sampled first and is never mixed into ``tuning`` or
``heldout``.  Pool assignment is then made on ``entity_hash`` rather than on
the row, and EVERY row sharing a pilot entity -- a second menu for the same
protein, say -- is withheld from tuning and heldout as well (recorded as
``excluded_pilot_entity``).  Without that, tuning on one menu of an entity
would leak into the pilot's other menu for the same protein and the pilot
would quietly stop measuring generalisation.  The 60/40 tuning/heldout split
is likewise made over ENTITY GROUPS, not rows, for the same reason.

WHAT THE GUARD SLICES ARE FOR
=============================
``regression`` (``agree_assigned`` rows) and ``decline_guard`` (rows across
all classes in population proportion) exist so a tuning change that improves
contested items while breaking uncontested ones, or while shifting the
decline rate, cannot pass unnoticed.  They are guards, not training signal,
so the pilot-entity rule above does not constrain them; where they do share
an entity with another pool, the manifest reports it (``guard_entity_overlap``)
rather than hiding it.

EVERY SLICE RECORDS ITS DENOMINATOR AND ITS SHORTFALL
=====================================================
Each slice takes ``min(requested, eligible)`` and writes down both numbers.
A manifest that asked for 400 decline-guard rows and got 112 says so, in the
file, next to the slice -- because "n=112" discovered later, in a result
table, is indistinguishable from a sampling bug.

The one hard refusal is a corpus too small to pilot at all: below
``plan.min_conflicts`` conflicts, :func:`build_manifest` raises
:class:`~.conflicts.Refused` and says how many more ALCF items the observed
conflict rate implies are needed.  Between that floor and the full 150 the
pilot simply shrinks, and the shortfall is reported.

ORDER IS INTERLEAVED, NEVER CONCATENATED
========================================
A tranche's execution order is a seeded weighted interleaving of its
``class x source`` strata (:func:`execution_order`), frozen in the manifest.
Working a tranche in class order instead would correlate position in the
queue with class, so a drift in the adjudicators over the run -- fatigue, a
model swap, a rate-limit detour -- would land unevenly across the classes and
read as a class effect.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
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

from ..code_version import code_version
from .conflicts import CLASSES, MatchedItem, MatchResult, Refused

__all__ = [
    "POOLS",
    "Manifest",
    "Plan",
    "build_manifest",
    "execution_order",
    "largest_remainder",
]

#: Every pool a manifest row may land in.  ``pilot`` is the locked test set;
#: ``tuning`` and ``heldout`` are the 60/40 split of what is left;
#: ``regression`` and ``decline_guard`` are the two guard slices.
POOLS = ("pilot", "tuning", "heldout", "regression", "decline_guard")


@dataclass(frozen=True)
class Plan:
    """The sampling plan.  Recorded verbatim in the manifest it produced.

    ``class_weights`` is a plan parameter and not a constant precisely so a
    later tranche can give the DECLINED classes weight: today the pilot is
    half ``disjoint`` and half ``partial`` because those are the two classes
    where both councils named terms and a human can adjudicate term against
    term, but ``alcf_declined`` / ``gomf_declined`` are real disagreements
    that will want their own slice once the decline policy is being tuned.
    Give them a weight and list them in ``pilot_classes`` and they are in --
    no code change.
    """

    #: Rows wanted in the pilot.  Takes ``min(this, available)``.
    pilot_size: int = 150
    #: Classes the pilot (and the tuning/heldout split) draw from.
    pilot_classes: Tuple[str, ...] = ("disjoint", "partial")
    #: Relative class shares within the pilot; a class that cannot fill its
    #: share contributes all it has and the others take up the slack.
    class_weights: Mapping[str, float] = field(
        default_factory=lambda: {"disjoint": 0.5, "partial": 0.5}
    )
    #: Pilot rows that also get a position-swapped twin, to measure A/B
    #: position bias on the same item.
    swap_slice: int = 30
    #: Share of the post-pilot entity GROUPS that go to tuning.
    tuning_fraction: float = 0.6
    #: The uncontested-regression guard.
    regression_size: int = 200
    regression_classes: Tuple[str, ...] = ("agree_assigned",)
    #: The decline-rate guard, drawn across all classes in population
    #: proportion.
    decline_guard_size: int = 400
    #: Below this many conflicts there is no pilot worth locking, and
    #: :func:`build_manifest` refuses.
    min_conflicts: int = 100

    def as_dict(self) -> Dict[str, Any]:
        return {
            "pilot_size": self.pilot_size,
            "pilot_classes": list(self.pilot_classes),
            "class_weights": {k: self.class_weights[k] for k in sorted(self.class_weights)},
            "swap_slice": self.swap_slice,
            "tuning_fraction": self.tuning_fraction,
            "regression_size": self.regression_size,
            "regression_classes": list(self.regression_classes),
            "decline_guard_size": self.decline_guard_size,
            "min_conflicts": self.min_conflicts,
        }


@dataclass(frozen=True)
class Manifest:
    """A frozen sample.  :meth:`to_json` is the byte-identical artefact.

    ``manifest_id`` is a digest of the SAMPLE -- seed, plan, entries, order
    and report -- and deliberately NOT of ``code_version``.  Two manifests
    with the same id contain the same rows in the same order even if one was
    written from a dirty tree; the commit is recorded beside the id so the
    build is known, without making the id change every time the tree does.
    """

    manifest_id: str
    seed: Any
    plan: Mapping[str, Any]
    code_version: Mapping[str, Any]
    entries: Tuple[Mapping[str, Any], ...]
    execution_order: Mapping[str, Tuple[str, ...]]
    report: Mapping[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "seed": self.seed,
            "plan": dict(self.plan),
            "code_version": dict(self.code_version),
            "entries": [dict(e) for e in self.entries],
            "execution_order": {k: list(v) for k, v in self.execution_order.items()},
            "report": dict(self.report),
        }

    def to_json(self) -> str:
        return _canonical(self.as_dict()) + "\n"

    def write(self, path: Any) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.to_json(), encoding="utf-8")
        return out

    def pool(self, name: str) -> Tuple[Mapping[str, Any], ...]:
        return tuple(e for e in self.entries if e["pool"] == name)

    def entity_hashes(self, name: str) -> frozenset:
        return frozenset(e["entity_hash"] for e in self.entries if e["pool"] == name)


# ---------------------------------------------------------------------------
# determinism primitives
# ---------------------------------------------------------------------------


def _canonical(payload: Any) -> str:
    """Sorted-key, fixed-separator JSON -- the only serialisation used here."""
    return json.dumps(payload, sort_keys=True, indent=2, separators=(",", ": "))


def _rng(seed: Any, *purpose: str) -> random.Random:
    """An independent stream per purpose, stable across processes.

    Seeding from a STRING rather than from a counter means adding a new draw
    somewhere does not shift every later draw -- a property worth having the
    first time a plan parameter changes and an unrelated slice must not move.
    """
    return random.Random("|".join([str(seed), *purpose]))


def _bit(seed: Any, *parts: str) -> bool:
    """A seeded coin for one named thing, independent of iteration order.

    Hashed rather than drawn from a stream so an item's orientation depends
    only on the seed and the item's own key: re-running with one extra item
    in the corpus must not re-orient the items that were already there.
    """
    digest = hashlib.sha256("|".join([str(seed), *parts]).encode("utf-8")).digest()
    return bool(digest[0] & 1)


def largest_remainder(total: int, weights: Mapping[str, float]) -> Dict[str, int]:
    """Apportion *total* over *weights*, summing to exactly *total*.

    Ties in the remainder are broken by key name, so the answer does not
    depend on dict order.  Returns ``{}`` when there is nothing to divide or
    no positive weight.
    """
    positive = {k: float(w) for k, w in weights.items() if w > 0}
    if total <= 0 or not positive:
        return {k: 0 for k in weights}
    pool = sum(positive.values())
    quota = {k: total * w / pool for k, w in positive.items()}
    out = {k: int(math.floor(q)) for k, q in quota.items()}
    short = total - sum(out.values())
    order = sorted(positive, key=lambda k: (-(quota[k] - out[k]), k))
    for k in order[:short]:
        out[k] += 1
    for k in weights:
        out.setdefault(k, 0)
    return out


def _waterfill(
    total: int, weights: Mapping[str, float], capacity: Mapping[str, int]
) -> Dict[str, int]:
    """Largest-remainder apportionment, then give a short bucket's deficit away.

    This is the "a class that cannot fill its half contributes all its rows
    and the other class fills the rest" rule, generalised: any bucket whose
    share exceeds its capacity is pinned at capacity and dropped, and the
    remaining total is re-apportioned over whoever still has room.  Iterating
    rather than redistributing once matters when the second bucket saturates
    too, which is exactly the small-corpus case.
    """
    alloc: Dict[str, int] = {k: 0 for k in weights}
    active = {k: float(w) for k, w in weights.items() if w > 0 and capacity.get(k, 0) > 0}
    remaining = total
    while remaining > 0 and active:
        raw = largest_remainder(remaining, active)
        saturated = [k for k, v in raw.items() if v >= capacity.get(k, 0) - alloc[k]]
        if not saturated:
            for k, v in raw.items():
                alloc[k] += v
            remaining = 0
            break
        for k in saturated:
            room = capacity.get(k, 0) - alloc[k]
            alloc[k] += room
            remaining -= room
            active.pop(k)
    return alloc


def _key_str(item: MatchedItem) -> str:
    return f"{item.entity_hash}:{item.data_hash}"


def _ordered(items: Iterable[MatchedItem]) -> List[MatchedItem]:
    """Canonical input order: by key.  Every shuffle starts from this."""
    return sorted(items, key=lambda i: (i.entity_hash, i.data_hash))


def _select_stratified(
    rows: Sequence[MatchedItem], n: int, seed: Any, *purpose: str
) -> Tuple[List[MatchedItem], Dict[str, int]]:
    """Take *n* of *rows* proportionally by SOURCE TOOL, without replacement.

    Returns the chosen rows in canonical order plus the per-source
    allocation, which the manifest reports: "proportional by source" is a
    claim a reader must be able to check against the realised counts.
    """
    pool = _ordered(rows)
    by_source: Dict[str, List[MatchedItem]] = {}
    for row in pool:
        by_source.setdefault(row.source, []).append(row)
    want = min(max(n, 0), len(pool))
    sizes = {s: len(v) for s, v in by_source.items()}
    alloc = _waterfill(want, {s: float(c) for s, c in sizes.items()}, sizes)
    chosen: List[MatchedItem] = []
    for source in sorted(by_source):
        candidates = list(by_source[source])
        _rng(seed, *purpose, source).shuffle(candidates)
        chosen.extend(candidates[: alloc.get(source, 0)])
    return _ordered(chosen), {s: alloc.get(s, 0) for s in sorted(sizes)}


def _stratum(item: MatchedItem) -> str:
    return f"{item.kind}|{item.source}"


def execution_order(entries: Sequence[Mapping[str, Any]], seed: Any, tranche: str) -> Tuple[str, ...]:
    """A seeded WEIGHTED INTERLEAVING of a tranche's ``class|source`` strata.

    Each stratum is shuffled, then its rows are laid on a virtual timeline at
    evenly spaced positions ``(i + offset) / size`` with a seeded offset, and
    everything is merged by position.  A stratum with twice the rows therefore
    appears twice as often throughout -- the order is interleaved end to end,
    never a concatenation of per-stratum blocks, and never a plain shuffle
    (which would leave the strata clumped by chance).
    """
    strata: Dict[str, List[Mapping[str, Any]]] = {}
    for entry in entries:
        strata.setdefault(f"{entry['class']}|{entry['source']}", []).append(entry)
    schedule: List[Tuple[float, str, int, str]] = []
    for name in sorted(strata):
        rows = sorted(strata[name], key=lambda e: e["id"])
        rng = _rng(seed, "order", tranche, name)
        rng.shuffle(rows)
        offset = rng.random()
        size = len(rows)
        for i, entry in enumerate(rows):
            schedule.append(((i + offset) / size, name, i, entry["id"]))
    schedule.sort(key=lambda s: (s[0], s[1], s[2]))
    return tuple(entry_id for _, _, _, entry_id in schedule)


# ---------------------------------------------------------------------------
# the sampler
# ---------------------------------------------------------------------------


def _items_of(matched: Any) -> List[MatchedItem]:
    if isinstance(matched, MatchResult):
        return _ordered(matched.items)
    return _ordered(matched)


def _refusal(have: int, matched_rows: int, plan: Plan) -> Refused:
    """Refuse a corpus too small to pilot, in units the submitter can act on.

    The actionable number is not "you are 38 short" but "run N more ALCF
    items": the conflict rate is what converts between them, so it is
    measured here and quoted rather than assumed.
    """
    rate = (have / matched_rows) if matched_rows else 0.0
    if rate <= 0:
        extra = "unknown (no conflicts observed, so no rate to extrapolate from)"
    else:
        to_floor = math.ceil((plan.min_conflicts - have) / rate)
        to_full = max(math.ceil((plan.pilot_size - have) / rate), 0)
        extra = (
            f"about {to_floor} more ALCF items to reach the floor of "
            f"{plan.min_conflicts}, or about {to_full} to fill the full "
            f"{plan.pilot_size}-item pilot, at the observed conflict rate of "
            f"{100.0 * rate:.1f}% ({have}/{matched_rows} matched rows)"
        )
    return Refused(
        f"only {have} disjoint+partial conflicts, below the floor of "
        f"{plan.min_conflicts}; need {extra}"
    )


def build_manifest(matched: Any, seed: Any, plan: Optional[Plan] = None) -> Manifest:
    """Sample *matched* into pools and freeze the result as a :class:`Manifest`.

    *matched* is a :class:`~.conflicts.MatchResult` or any iterable of
    :class:`~.conflicts.MatchedItem`.  *seed* may be any value with a stable
    ``str()`` -- an int or a short string is usual.

    Raises :class:`~.conflicts.Refused` when there are fewer than
    ``plan.min_conflicts`` conflicts, or when two selected rows would carry
    the same entry id.
    """
    plan = plan or Plan()
    items = _items_of(matched)
    population: Dict[str, int] = {cls: 0 for cls in CLASSES}
    for item in items:
        population[item.kind] = population.get(item.kind, 0) + 1

    conflicts = [i for i in items if i.kind in plan.pilot_classes]
    if len(conflicts) < plan.min_conflicts:
        raise _refusal(len(conflicts), len(items), plan)

    # -- pilot: min(requested, available), halves by class weight ----------
    pilot_target = min(plan.pilot_size, len(conflicts))
    by_class: Dict[str, List[MatchedItem]] = {c: [] for c in plan.pilot_classes}
    for item in conflicts:
        by_class[item.kind].append(item)
    capacity = {c: len(rows) for c, rows in by_class.items()}
    weights = {c: float(plan.class_weights.get(c, 0.0)) for c in plan.pilot_classes}
    class_alloc = _waterfill(pilot_target, weights, capacity)

    pilot: List[MatchedItem] = []
    pilot_strata: Dict[str, int] = {}
    for cls in plan.pilot_classes:
        chosen, alloc = _select_stratified(
            by_class[cls], class_alloc.get(cls, 0), seed, "pilot", cls
        )
        pilot.extend(chosen)
        for source, n in alloc.items():
            if n:
                pilot_strata[f"{cls}|{source}"] = n
    pilot = _ordered(pilot)
    pilot_keys = {i.key for i in pilot}
    pilot_entities = {i.entity_hash for i in pilot}

    # -- tuning / heldout: entity GROUPS, 60/40, pilot entities withheld ---
    rest = [i for i in conflicts if i.key not in pilot_keys]
    withheld = [i for i in rest if i.entity_hash in pilot_entities]
    splittable = [i for i in rest if i.entity_hash not in pilot_entities]
    groups: Dict[str, List[MatchedItem]] = {}
    for item in splittable:
        groups.setdefault(item.entity_hash, []).append(item)
    # A group's stratum is its canonically-first row's; a group whose menus
    # fall in different classes still travels as ONE unit, so it needs one
    # label and that label must not depend on dict order.
    group_strata: Dict[str, List[str]] = {}
    for entity, rows in groups.items():
        group_strata.setdefault(_stratum(_ordered(rows)[0]), []).append(entity)

    tuning_entities: List[str] = []
    heldout_entities: List[str] = []
    for name in sorted(group_strata):
        entities = sorted(group_strata[name])
        _rng(seed, "split", name).shuffle(entities)
        split = largest_remainder(
            len(entities),
            {"tuning": plan.tuning_fraction, "heldout": 1.0 - plan.tuning_fraction},
        )
        tuning_entities.extend(entities[: split["tuning"]])
        heldout_entities.extend(entities[split["tuning"]:])
    tuning = _ordered(r for e in tuning_entities for r in groups[e])
    heldout = _ordered(r for e in heldout_entities for r in groups[e])

    # -- guard slices: min(requested, eligible), denominators recorded -----
    # Without replacement: a row lands in ONE pool.  Note that ``withheld``
    # rows are NOT excluded here -- C6 withholds them from tuning and heldout,
    # and keeping them out of the population guard as well would narrow that
    # guard towards the entities nothing else wanted.
    used = pilot_keys | {i.key for i in tuning} | {i.key for i in heldout}
    regression_pool = [
        i for i in items if i.kind in plan.regression_classes and i.key not in used
    ]
    regression, regression_alloc = _select_stratified(
        regression_pool, plan.regression_size, seed, "regression"
    )
    used |= {i.key for i in regression}

    guard_pool = [i for i in items if i.key not in used]
    guard_by_class: Dict[str, List[MatchedItem]] = {}
    for item in guard_pool:
        guard_by_class.setdefault(item.kind, []).append(item)
    guard_alloc = _waterfill(
        min(plan.decline_guard_size, len(guard_pool)),
        {c: float(population.get(c, 0)) for c in guard_by_class},
        {c: len(rows) for c, rows in guard_by_class.items()},
    )
    decline_guard: List[MatchedItem] = []
    guard_strata: Dict[str, int] = {}
    for cls in sorted(guard_by_class):
        chosen, alloc = _select_stratified(
            guard_by_class[cls], guard_alloc.get(cls, 0), seed, "decline_guard", cls
        )
        decline_guard.extend(chosen)
        for source, n in alloc.items():
            if n:
                guard_strata[f"{cls}|{source}"] = n
    decline_guard = _ordered(decline_guard)

    # -- the position-bias slice, drawn from the pilot ---------------------
    swap_candidates = list(pilot)
    _rng(seed, "swap").shuffle(swap_candidates)
    swap_keys = {i.key for i in swap_candidates[: min(plan.swap_slice, len(pilot))]}

    # -- entries -----------------------------------------------------------
    selected: List[Tuple[str, MatchedItem]] = (
        [("pilot", i) for i in pilot]
        + [("tuning", i) for i in tuning]
        + [("heldout", i) for i in heldout]
        + [("regression", i) for i in regression]
        + [("decline_guard", i) for i in decline_guard]
    )
    counted = Counter(i.item_id for _, i in selected)
    clashing = sorted(name for name, n in counted.items() if n > 1)
    if clashing:
        raise Refused(
            "two selected rows share an item_id, so a manifest entry id would "
            "not identify one row; name fewer candidate jobs, or re-key them",
            clashing,
        )
    entries: List[Dict[str, Any]] = []
    for pool_name, item in selected:
        alcf_is_A = _bit(seed, "ab", item.entity_hash, item.data_hash)
        entries.append(_entry(item, pool_name, item.item_id, alcf_is_A, False))
        if item.key in swap_keys:
            entries.append(
                _entry(item, pool_name, f"{item.item_id}:swap", not alcf_is_A, True)
            )
    entries.sort(key=lambda e: (POOLS.index(e["pool"]), e["entity_hash"], e["data_hash"], e["id"]))

    order = {
        name: execution_order([e for e in entries if e["pool"] == name], seed, name)
        for name in POOLS
    }

    # ENTRIES, not rows: a swapped row contributes two entries.  The row
    # count per pool is ``pools[name]["rows"]``.
    pool_entries = {name: sum(1 for e in entries if e["pool"] == name) for name in POOLS}
    report: Dict[str, Any] = {
        "population": population,
        "matched_rows": len(items),
        "conflicts_available": len(conflicts),
        "conflict_rate_pct": round(100.0 * len(conflicts) / len(items), 4) if items else 0.0,
        "pools": {
            "pilot": _slice_report(plan.pilot_size, len(conflicts), pilot, pilot_strata, class_alloc),
            "tuning": _slice_report(None, len(splittable), tuning, _realised_strata(tuning)),
            "heldout": _slice_report(None, len(splittable), heldout, _realised_strata(heldout)),
            "regression": _slice_report(
                plan.regression_size, len(regression_pool), regression, _realised_strata(regression),
                {"by_source": regression_alloc},
            ),
            "decline_guard": _slice_report(
                plan.decline_guard_size, len(guard_pool), decline_guard, guard_strata, guard_alloc
            ),
        },
        "entries_per_pool": pool_entries,
        "swap_slice": {
            "requested": plan.swap_slice,
            "eligible": len(pilot),
            "selected": len(swap_keys),
            "shortfall": max(plan.swap_slice - len(swap_keys), 0),
        },
        "excluded_pilot_entity": {
            "rows": len(withheld),
            "entities": len({i.entity_hash for i in withheld}),
            "keys": [[i.entity_hash, i.data_hash] for i in _ordered(withheld)],
        },
        "entity_split": {
            "entity_groups": len(groups),
            "tuning_entities": len(tuning_entities),
            "heldout_entities": len(heldout_entities),
            "tuning_fraction_realised": (
                round(len(tuning_entities) / len(groups), 4) if groups else None
            ),
        },
        "guard_entity_overlap": _guard_overlap(pilot, tuning, heldout, regression, decline_guard),
        "entries": len(entries),
    }
    # ALWAYS present, even when it can say nothing.  Sampling from a JSONL
    # cannot recover how many keys the extractor found unstable or turned
    # away, and omitting the block entirely would read as "none" -- which is
    # the one reading that must not be available to a reader of a manifest.
    if isinstance(matched, MatchResult):
        report["extract_diagnostics"] = {
            "carried": True,
            "unstable": {
                "count": len(matched.unstable),
                "by_side": {
                    label: sum(1 for v in matched.unstable.values() if v == label)
                    for label in sorted(set(matched.unstable.values()))
                },
            },
            "excluded_rows": dict(matched.excluded),
            "candidate_only_keys": len(matched.candidate_only),
            "baseline_only_keys": matched.baseline_only,
        }
    else:
        report["extract_diagnostics"] = {
            "carried": False,
            "note": (
                "sampled from a conflict list, not from a MatchResult: the "
                "extractor's unstable and excluded-row counts are NOT known "
                "here and must be read from the extract step's own output"
            ),
        }

    plan_dict = plan.as_dict()
    body = {
        "seed": seed,
        "plan": plan_dict,
        "entries": entries,
        "execution_order": {k: list(v) for k, v in order.items()},
        "report": report,
    }
    manifest_id = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()[:32]
    return Manifest(
        manifest_id=manifest_id,
        seed=seed,
        plan=plan_dict,
        code_version=code_version(),
        entries=tuple(entries),
        execution_order={k: tuple(v) for k, v in order.items()},
        report=report,
    )


def _entry(
    item: MatchedItem, pool: str, entry_id: str, alcf_is_A: bool, swap: bool
) -> Dict[str, Any]:
    """One manifest entry: a ``(key, orientation)`` pair, not just an item.

    The swapped twin of an item is a SEPARATE entry over the same key with
    the opposite orientation, which is the only way a position-bias slice can
    compare like with like.
    """
    return {
        "id": entry_id,
        "item_id": item.item_id,
        "entity_hash": item.entity_hash,
        "data_hash": item.data_hash,
        "class": item.kind,
        "sub_kind": item.item_sub_kind,
        "source": item.source,
        "pool": pool,
        "alcf_is_A": alcf_is_A,
        "swap": swap,
        "candidate_job_id": item.candidate_job_id,
        "baseline_item_id": item.baseline_item_id,
        "baseline_job_id": item.baseline_job_id,
    }


def _realised_strata(rows: Sequence[MatchedItem]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        out[_stratum(row)] = out.get(_stratum(row), 0) + 1
    return dict(sorted(out.items()))


def _slice_report(
    requested: Optional[int],
    eligible: int,
    rows: Sequence[MatchedItem],
    strata: Mapping[str, int],
    allocation: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "requested": requested,
        "eligible": eligible,
        "rows": len(rows),
        "entities": len({r.entity_hash for r in rows}),
        "strata": dict(sorted(strata.items())),
    }
    if requested is not None:
        out["shortfall"] = max(requested - len(rows), 0)
    if allocation is not None:
        out["allocation"] = dict(sorted(allocation.items()))
    return out


def _guard_overlap(
    pilot: Sequence[MatchedItem],
    tuning: Sequence[MatchedItem],
    heldout: Sequence[MatchedItem],
    regression: Sequence[MatchedItem],
    decline_guard: Sequence[MatchedItem],
) -> Dict[str, int]:
    """How many guard entities also appear in a pilot/tuning/heldout pool.

    Reported rather than prevented: the guard slices measure the population,
    and excluding every entity already spoken for would bias them towards the
    entities nothing else wanted.  The number being visible is what lets a
    reader decide whether it matters for a given comparison.
    """
    signal = {
        "pilot": {i.entity_hash for i in pilot},
        "tuning": {i.entity_hash for i in tuning},
        "heldout": {i.entity_hash for i in heldout},
    }
    out: Dict[str, int] = {}
    for guard_name, guard in (("regression", regression), ("decline_guard", decline_guard)):
        entities = {i.entity_hash for i in guard}
        for pool_name, pool_entities in signal.items():
            out[f"{guard_name}_x_{pool_name}"] = len(entities & pool_entities)
    return out
