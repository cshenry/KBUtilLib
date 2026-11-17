"""Where an ALCF bench council and the ``go_mf@4`` baseline DISAGREE.

Two matchers live here, and conflating them is the mistake this docstring
exists to prevent.

:func:`match_items` is the BENCH REPORT's matcher, moved here unchanged from
``scripts/council/alcf_bench_report.py`` so the library and the script share
one implementation.  It pairs on ``item_id`` and REFUSES on any ambiguity --
a duplicate eligible baseline row, a baseline row whose hashes differ.  That
strictness is the point of a bench report: it compares one named candidate
job against explicitly named baseline jobs, and picking a winner implicitly
would silently choose which answer got reported.

:func:`match` is the TUNING EXTRACTOR's matcher, and it is a different
question asked of a different corpus.  It joins on the key
``(entity_hash, data_hash)`` and IGNORES ``item_id`` entirely, because on the
real stores ``item_id`` does not identify an entity across stores:

* Bench A mints 12-hex ``item_id`` values that are PREFIXES of the 64-hex
  ``entity_hash`` it also stores, so no candidate id equals the baseline id
  for the same protein.
* The production ``go_mf@4`` baseline for a single entity spans roughly 29
  job rows -- cancelled originals, chunked re-runs and retries -- so there is
  no one row to pair with even within the baseline.

Refusing on that shape, as :func:`match_items` rightly does, would refuse the
entire corpus.  So the tuning extractor COALESCES instead: per side, it
collects every admitted answer for a key, and if they all selected the same
terms it keeps one representative (provenance = the lexicographically lowest
``(job_id, item_id)``, with the answer count recorded).  Only when a side's
own answers DISAGREE with each other does it refuse to use the key -- and
then it does so narrowly, marking that key ``unstable_alcf`` or
``unstable_gomf``, excluding it from the conflict set and counting it, rather
than failing the run.  An unstable key is a real finding about reproducibility
and it must not be laundered into a conflict.

ADMISSION IS NARROW AND THE REST IS REPORTED, NEVER DROPPED SILENTLY.  A row
is admitted only when BOTH its item status and its verdict status are ``ok``,
and baseline rows additionally only from ``go_mf@4`` jobs.  Everything else
is counted by status in :attr:`MatchResult.excluded`.  One measured entity on
h100 (``009948a3814c``) carries cancelled rows in a ``go_mf@3`` job AND a
cancelled ``go_mf@4`` job beside its good answer; a matcher that took the
latest row, or any row, would have tuned against a cancellation.

Nothing here opens a network connection or spends a token.  A store argument
may be an open store or a URL string (opened with ``SqliteStore`` and closed
again), so the callers that already hold a store do not open a second one.
"""

from __future__ import annotations

import contextlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

__all__ = [
    "BASELINE_QUERY",
    "CLASSES",
    "PARTIAL_SUB_KINDS",
    "MatchResult",
    "MatchedItem",
    "Refused",
    "UNSTABLE_CLASSES",
    "classify",
    "match",
    "match_items",
    "selected_set",
    "sub_kind",
    "verdict_ok",
]

#: The only query type a baseline row may come from, on EITHER matcher.
BASELINE_QUERY = ("go_mf", "4")

#: The six classes :func:`classify` chooses between, in report order.  The
#: "candidate" side is ALCF and the "baseline" side is ``go_mf@4``, so
#: ``alcf_declined`` means the CANDIDATE declined, not the baseline.
CLASSES = (
    "agree_assigned",
    "agree_declined",
    "alcf_declined",
    "gomf_declined",
    "disjoint",
    "partial",
)

#: Sub-kinds recorded on a ``partial`` only.  ``crossed`` is the interesting
#: one: the two councils share a term and each also picked one the other did
#: not, which no superset relation describes.
PARTIAL_SUB_KINDS = ("alcf_superset", "gomf_superset", "crossed")

#: The two keys whose own side could not agree with itself.
UNSTABLE_CLASSES = ("unstable_alcf", "unstable_gomf")

#: ``(entity_hash, data_hash)`` -- what the tuning extractor joins on.
Key = Tuple[str, str]


class Refused(Exception):
    """The inputs are missing, mismatched or ambiguous; ``ids`` lists them."""

    def __init__(self, message: str, ids: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.ids = list(ids)


# ---------------------------------------------------------------------------
# verdict helpers
# ---------------------------------------------------------------------------


def selected_set(verdict: Optional[Mapping[str, Any]]) -> frozenset:
    return frozenset((verdict or {}).get("selected_ids") or [])


def verdict_ok(item: Mapping[str, Any]) -> bool:
    """The BENCH REPORT's admission test (``match_items`` and its callers).

    Deliberately lenient about a missing verdict -- ``status == "ok"`` with no
    verdict row reads as ok here -- because that is the behaviour the bench
    report's tests pin.  The tuning extractor uses :func:`_row_status`, which
    is stricter; see its docstring.
    """
    verdict = item.get("verdict") or {}
    return item.get("status") == "ok" and verdict.get("status", "ok") == "ok"


# ---------------------------------------------------------------------------
# the bench report's matcher: pairs on item_id, refuses on ambiguity
# ---------------------------------------------------------------------------


def match_items(
    candidate_items: Sequence[Mapping[str, Any]],
    baseline_jobs: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Pair candidate items with baseline rows; REFUSE rather than choose.

    ``baseline_jobs`` are ``{"job_id", "query_name", "query_version",
    "items": [...]}`` dicts, one per explicitly named job.

    Returns ``{"pairs": {item_id: (cand_item, base_item)}, "unmatched":
    {item_id: reason}}``. Raises :class:`Refused` on a baseline job that is
    not go_mf@4, a duplicate eligible match, or an item id present in the
    baseline whose entity_hash/data_hash differ from the candidate's.

    LEGACY BY DESIGN: this is ``item_id`` matching, kept for the bench report.
    The tuning extractor must use :func:`match` instead -- see the module
    docstring for why ``item_id`` cannot join the real stores.
    """
    wrong = [
        j["job_id"] for j in baseline_jobs
        if (j.get("query_name"), str(j.get("query_version"))) != BASELINE_QUERY
    ]
    if wrong:
        raise Refused("baseline job(s) are not go_mf@4", wrong)

    by_id: Dict[str, List[Tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    for job in baseline_jobs:
        for row in job.get("items", []):
            by_id[row["item_id"]].append((job["job_id"], row))

    pairs: Dict[str, Tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    unmatched: Dict[str, str] = {}
    duplicates: List[str] = []
    mismatched: List[str] = []
    for cand in candidate_items:
        item_id = cand["item_id"]
        rows = by_id.get(item_id, [])
        same_hash = [
            (jid, r) for jid, r in rows
            if r.get("entity_hash") == cand.get("entity_hash")
            and r.get("data_hash") == cand.get("data_hash")
        ]
        if rows and not same_hash:
            mismatched.append(item_id)
            continue
        eligible = [(jid, r) for jid, r in same_hash if verdict_ok(r)]
        if len(eligible) > 1:
            duplicates.append(item_id)
        elif len(eligible) == 1:
            pairs[item_id] = (cand, eligible[0][1])
        elif same_hash:
            unmatched[item_id] = "baseline row has no ok verdict"
        else:
            unmatched[item_id] = "no baseline row in the named jobs"
    if duplicates:
        raise Refused(
            "duplicate eligible baseline match (refusing to pick a winner)", duplicates
        )
    if mismatched:
        raise Refused(
            "baseline row exists but entity_hash/data_hash differ from the candidate",
            mismatched,
        )
    return {"pairs": pairs, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def _sides(matched: Any) -> Tuple[frozenset, frozenset]:
    """``(candidate_selected, baseline_selected)`` from an item or a mapping.

    Accepts a :class:`MatchedItem` or any mapping carrying the same two keys,
    so a test can classify a literal without building a dataclass.
    """
    if isinstance(matched, MatchedItem):
        return matched.candidate_selected, matched.baseline_selected
    return (
        frozenset(matched["candidate_selected"]),
        frozenset(matched["baseline_selected"]),
    )


def classify(matched: Any) -> str:
    """Exactly one of :data:`CLASSES` for one matched, de-duplicated item.

    ``partial`` carries a further distinction; ask :func:`sub_kind` for it.
    """
    candidate, baseline = _sides(matched)
    if not candidate and not baseline:
        return "agree_declined"
    if not candidate:
        return "alcf_declined"
    if not baseline:
        return "gomf_declined"
    if candidate == baseline:
        return "agree_assigned"
    if not candidate & baseline:
        return "disjoint"
    return "partial"


def sub_kind(matched: Any) -> Optional[str]:
    """One of :data:`PARTIAL_SUB_KINDS` for a ``partial``, else ``None``."""
    if classify(matched) != "partial":
        return None
    candidate, baseline = _sides(matched)
    if candidate > baseline:
        return "alcf_superset"
    if baseline > candidate:
        return "gomf_superset"
    return "crossed"


# ---------------------------------------------------------------------------
# the tuning extractor's matcher: joins on the key, coalesces, never refuses
# a corpus because one key is messy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchedItem:
    """One ``(entity_hash, data_hash)`` key both sides answered, classified.

    ``item_id`` is the CANDIDATE side's representative id and is what the
    manifest names an item by; it is provenance, not the join key.  Both
    sides' ``(job_id, item_id)`` are carried so any row in a manifest can be
    walked back to the two store rows it came from.
    """

    entity_hash: str
    data_hash: str
    item_id: str
    candidate_job_id: str
    baseline_item_id: str
    baseline_job_id: str
    candidate_selected: frozenset
    baseline_selected: frozenset
    candidate_answers: int
    baseline_answers: int
    source: str
    evidence_text: str
    menu: Tuple[Mapping[str, Any], ...]
    kind: str
    item_sub_kind: Optional[str]
    #: ``verdict.decided_by`` per side -- ``unanimous``, ``meta`` or
    #: ``deterministic_empty``.  Carried because an adjudicator reading a
    #: decline needs to know whether a council decided it or a deterministic
    #: rule produced it, and those are not the same disagreement.
    candidate_decided_by: Optional[str] = None
    baseline_decided_by: Optional[str] = None
    #: ``verdict.decline_reason`` per side, when the side declined.
    candidate_decline_reason: Optional[str] = None
    baseline_decline_reason: Optional[str] = None

    @property
    def key(self) -> Key:
        return (self.entity_hash, self.data_hash)

    @property
    def duplicate_count(self) -> int:
        """Rows coalesced away: every admitted answer beyond the first, both
        sides.  Zero on the clean one-answer-per-side case."""
        return (self.candidate_answers - 1) + (self.baseline_answers - 1)

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-ready dict, with the sets sorted so output is stable."""
        return {
            "item_id": self.item_id,
            "entity_hash": self.entity_hash,
            "data_hash": self.data_hash,
            "class": self.kind,
            "sub_kind": self.item_sub_kind,
            "source": self.source,
            "evidence_text": self.evidence_text,
            "candidate_selected": sorted(self.candidate_selected),
            "baseline_selected": sorted(self.baseline_selected),
            "candidate_job_id": self.candidate_job_id,
            "baseline_item_id": self.baseline_item_id,
            "baseline_job_id": self.baseline_job_id,
            "candidate_answers": self.candidate_answers,
            "baseline_answers": self.baseline_answers,
            "duplicate_count": self.duplicate_count,
            "candidate_decided_by": self.candidate_decided_by,
            "baseline_decided_by": self.baseline_decided_by,
            "candidate_decline_reason": self.candidate_decline_reason,
            "baseline_decline_reason": self.baseline_decline_reason,
            "menu": [dict(entry) for entry in self.menu],
        }


@dataclass(frozen=True)
class MatchResult:
    """What :func:`match` found, with every excluded row accounted for."""

    #: Keys both sides answered stably, classified, sorted by key.
    items: Tuple[MatchedItem, ...] = ()
    #: ``key -> "unstable_alcf" | "unstable_gomf"``, excluded from ``items``.
    unstable: Mapping[Key, str] = field(default_factory=dict)
    #: ``"candidate:error" -> n`` -- every row admission turned away.
    excluded: Mapping[str, int] = field(default_factory=dict)
    #: Keys one side answered and the other did not.
    candidate_only: Tuple[Key, ...] = ()
    baseline_only: int = 0
    #: Class -> count over ``items``, all six classes always present, plus
    #: the two unstable counts.
    counts: Mapping[str, int] = field(default_factory=dict)
    #: The baseline job ids actually read (useful when they were discovered).
    baseline_job_ids: Tuple[str, ...] = ()

    @property
    def conflicts(self) -> Tuple[MatchedItem, ...]:
        """``disjoint`` and ``partial`` only -- the adjudicable disagreements.

        ``alcf_declined`` and ``gomf_declined`` are disagreements too, but
        they are not in the conflict set the pilot samples from: a decline is
        adjudicated against the policy's P5, not against a rival term list.
        """
        return tuple(i for i in self.items if i.kind in ("disjoint", "partial"))


def _row_status(item: Mapping[str, Any]) -> str:
    """The TUNING EXTRACTOR's admission label; ``"ok"`` means admitted.

    Stricter than :func:`verdict_ok` in one way that matters: an item whose
    status is ``ok`` but which carries NO verdict is labelled
    ``"no_verdict"`` and excluded, rather than read as an empty selection.
    Reading it as empty would turn a row nobody decided into a decline, and
    declines are a class this corpus measures.
    """
    status = item.get("status")
    if status != "ok":
        return str(status)
    verdict = item.get("verdict")
    if not verdict:
        return "no_verdict"
    vstatus = verdict.get("status", "ok")
    return "ok" if vstatus == "ok" else f"verdict:{vstatus}"


@contextlib.contextmanager
def _opened(store: Any) -> Iterator[Any]:
    """Yield an open store, closing it only if we opened it ourselves."""
    if isinstance(store, str):
        from ..store.sqlite import SqliteStore

        opened = SqliteStore(store, create_parents=False)
        try:
            yield opened
        finally:
            opened.close()
    else:
        yield store


def _job_rows(
    store: Any,
    job_ids: Optional[Sequence[str]],
    *,
    side: str,
    require_query: Optional[Tuple[str, str]],
) -> Tuple[List[Tuple[str, Mapping[str, Any]]], Tuple[str, ...]]:
    """``([(job_id, item_row), ...], job_ids_read)`` for one side.

    ``job_ids`` of ``None`` means "every job of ``require_query`` in this
    store", which is how the baseline is normally named: the production
    ``go_mf@4`` baseline is spread over too many jobs to list, and listing
    them by hand is how a retry chunk gets left out.  Explicitly named jobs
    are CHECKED against ``require_query`` and refuse if they do not match --
    naming a ``go_mf@3`` job as a baseline is an error, not a filter.
    """
    if job_ids is None:
        if require_query is None:
            raise Refused(f"{side} jobs must be named explicitly")
        name, version = require_query
        discovered = [
            j["job_id"]
            for j in store.list_jobs()
            if j.get("query_name") == name and str(j.get("query_version")) == version
        ]
        job_ids = sorted(discovered)
    else:
        # Every NAMED job is checked for existence, on both sides: a typo'd
        # job id would otherwise read as a job with no items, and "0 matched"
        # is indistinguishable from "the bench disagreed with nothing".
        job_ids = list(job_ids)
        queries = {jid: _query_of(store, jid, side) for jid in job_ids}
        if require_query is not None:
            wrong = [jid for jid in job_ids if queries[jid] != require_query]
            if wrong:
                raise Refused(
                    f"{side} job(s) are not {require_query[0]}@{require_query[1]}", wrong
                )
    rows: List[Tuple[str, Mapping[str, Any]]] = []
    for jid in job_ids:
        for row in store.get_items(jid):
            rows.append((jid, row))
    return rows, tuple(job_ids)


def _query_of(store: Any, job_id: str, side: str) -> Tuple[str, str]:
    job = store.get_job(job_id)
    if job is None:
        raise Refused(f"{side} job not found", [job_id])
    return (job["query_name"], str(job["query_version"]))


def _collect(
    rows: Iterable[Tuple[str, Mapping[str, Any]]],
    *,
    side: str,
    excluded: Counter,
) -> Dict[Key, List[Tuple[str, str, Mapping[str, Any]]]]:
    """Group admitted rows by key as ``[(job_id, item_id, row), ...]``.

    Rows missing either hash cannot be keyed at all and are excluded as
    ``no_key``: a null ``entity_hash`` would collide every such row into one
    bogus key.
    """
    by_key: Dict[Key, List[Tuple[str, str, Mapping[str, Any]]]] = defaultdict(list)
    for job_id, row in rows:
        status = _row_status(row)
        if status != "ok":
            excluded[f"{side}:{status}"] += 1
            continue
        entity_hash, data_hash = row.get("entity_hash"), row.get("data_hash")
        if not entity_hash or not data_hash:
            excluded[f"{side}:no_key"] += 1
            continue
        by_key[(entity_hash, data_hash)].append((job_id, row["item_id"], row))
    return by_key


def _coalesce(
    answers: Sequence[Tuple[str, str, Mapping[str, Any]]],
) -> Tuple[Optional[Tuple[str, str, Mapping[str, Any], frozenset]], bool]:
    """``((job_id, item_id, row, selected), stable)`` for one side of a key.

    ``stable`` is False when the side's own admitted answers selected
    different term sets -- the caller then marks the key unstable rather than
    choosing between them.  The representative is the lexicographically
    lowest ``(job_id, item_id)``, which is a rule rather than a preference:
    "latest wins" would silently prefer a retry over the original it was
    retrying.
    """
    ordered = sorted(answers, key=lambda a: (a[0], a[1]))
    selections = {selected_set(row.get("verdict")) for _, _, row in ordered}
    job_id, item_id, row = ordered[0]
    return (job_id, item_id, row, selected_set(row.get("verdict"))), len(selections) == 1


def _source_of(row: Mapping[str, Any]) -> str:
    """The SOURCE TOOL that produced the annotation, from ``evidence.source``.

    ``"unknown"`` rather than ``None`` so it is a stratum like any other:
    sampling proportionally by source must not drop the rows whose source was
    never recorded.
    """
    evidence = row.get("evidence") or {}
    source = evidence.get("source")
    return str(source) if source else "unknown"


def _evidence_text(row: Mapping[str, Any]) -> str:
    return str((row.get("evidence") or {}).get("text") or "")


def _verdict_field(row: Mapping[str, Any], name: str) -> Optional[str]:
    value = (row.get("verdict") or {}).get(name)
    return None if value is None else str(value)


def match(
    candidate_store: Any,
    candidate_jobs: Sequence[str],
    baseline_store: Any,
    baseline_jobs: Optional[Sequence[str]] = None,
) -> MatchResult:
    """Join SEVERAL candidate jobs against the ``go_mf@4`` baseline by key.

    ``candidate_store`` / ``baseline_store`` are each an open store or a
    sqlite URL.  ``candidate_jobs`` is a list of job ids; they may be any
    query type, because a candidate bench is by definition not ``go_mf@4``.
    ``baseline_jobs`` is a list of ``go_mf@4`` job ids, or ``None`` for every
    ``go_mf@4`` job in the baseline store.

    The two stores may be the same object, which is how a self-comparison or
    a single-file test fixture is expressed.

    Never raises for a messy key -- see the module docstring.  Raises
    :class:`Refused` only for inputs that name the wrong thing: a baseline
    job that is not ``go_mf@4``, or a job id that is not in its store.
    """
    excluded: Counter = Counter()
    with _opened(candidate_store) as cstore, _opened(baseline_store) as bstore:
        candidate_rows, _ = _job_rows(
            cstore, list(candidate_jobs), side="candidate", require_query=None
        )
        baseline_rows, baseline_ids = _job_rows(
            bstore, baseline_jobs, side="baseline", require_query=BASELINE_QUERY
        )
        candidate_by_key = _collect(candidate_rows, side="candidate", excluded=excluded)
        baseline_by_key = _collect(baseline_rows, side="baseline", excluded=excluded)

    items: List[MatchedItem] = []
    unstable: Dict[Key, str] = {}
    candidate_only: List[Key] = []
    for key in sorted(candidate_by_key):
        base_answers = baseline_by_key.get(key)
        if not base_answers:
            candidate_only.append(key)
            continue
        cand, cand_stable = _coalesce(candidate_by_key[key])
        base, base_stable = _coalesce(base_answers)
        if not cand_stable:
            # Candidate precedence when BOTH sides are unstable: the label
            # names a side that cannot be trusted, and the candidate is the
            # side under test.
            unstable[key] = "unstable_alcf"
            continue
        if not base_stable:
            unstable[key] = "unstable_gomf"
            continue
        cjob, cid, crow, cselected = cand
        bjob, bid, brow, bselected = base
        provisional = {
            "candidate_selected": cselected,
            "baseline_selected": bselected,
        }
        items.append(
            MatchedItem(
                entity_hash=key[0],
                data_hash=key[1],
                item_id=cid,
                candidate_job_id=cjob,
                baseline_item_id=bid,
                baseline_job_id=bjob,
                candidate_selected=cselected,
                baseline_selected=bselected,
                candidate_answers=len(candidate_by_key[key]),
                baseline_answers=len(base_answers),
                source=_source_of(crow),
                evidence_text=_evidence_text(crow),
                menu=tuple(crow.get("menu") or ()),
                kind=classify(provisional),
                item_sub_kind=sub_kind(provisional),
                candidate_decided_by=_verdict_field(crow, "decided_by"),
                baseline_decided_by=_verdict_field(brow, "decided_by"),
                candidate_decline_reason=_verdict_field(crow, "decline_reason"),
                baseline_decline_reason=_verdict_field(brow, "decline_reason"),
            )
        )

    counts: Dict[str, int] = {cls: 0 for cls in CLASSES}
    for item in items:
        counts[item.kind] += 1
    for label in UNSTABLE_CLASSES:
        counts[label] = sum(1 for v in unstable.values() if v == label)
    return MatchResult(
        items=tuple(items),
        unstable=dict(unstable),
        excluded=dict(sorted(excluded.items())),
        candidate_only=tuple(candidate_only),
        baseline_only=len(set(baseline_by_key) - set(candidate_by_key)),
        counts=counts,
        baseline_job_ids=baseline_ids,
    )
