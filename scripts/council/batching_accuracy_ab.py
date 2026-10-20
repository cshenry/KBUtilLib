#!/usr/bin/env python3
"""Batched-vs-unbatched A/B harness for the council's critic and meta seats.

BINDING FRAMING -- READ THIS BEFORE READING A NUMBER THIS SCRIPT PRINTS
========================================================================
Batching puts up to ``batch_size`` independent closed-menu classifications
into ONE model context, where they can influence each other.  Making the
pipeline cheaper while quietly changing its answers is the worst outcome
available, and it would not announce itself.  This script measures that risk
on a sample, before an operator turns batching on for a full corpus run.

**This script measures AGREEMENT with the unbatched pipeline, NOT ACCURACY
against truth.**  No labelled ``go_mf`` data exists in this repository, so
there is no ground truth to score against.  A 100% agreement figure means
"batching changed nothing we could detect on this sample relative to running
the same items unbatched" -- it does NOT mean "the council is correct".  If
anyone later wants a genuine *accuracy* gate, it needs labelled items and a
numerical pass threshold, and this script derives neither, because neither
exists in this codebase today.  Nothing this script prints should be read as
"the accuracy gate passed" -- it never claims that, and if you are about to
write that sentence in a summary, stop.

WHAT IS COMPARED, PRECISELY
============================
For each sampled item:

1. Every PROPOSER seat answers the item twice: once alone (``batch_size``
   effectively 1) and once as part of a batch of up to ``--batch-size``
   sampled items.  The two answers are compared directly -- there is no
   upstream to freeze for a proposer, since a proposer's prompt is built
   from the item's own evidence and menu only.

2. The UNBATCHED proposer answers (not the batched ones) are tallied into
   one **frozen reference vote table** (``ballots``, ``votes``) per item,
   using the real ``engine.decide.tally()``.  This is the "same chair
   ballots and votes" the task's binding amendment requires to feed BOTH
   arms: whatever batching does to the CRITIC or META seat, it is measured
   against an upstream that did not itself move, so a critic disagreement
   is never secretly a proposer-batching artifact wearing a critic's name.

3. The CRITIC seat answers every item twice against that ONE frozen vote
   table: once alone, once batched.  The two critic answers are compared.

4. The META seat answers every item twice, also against the frozen vote
   table, but each arm sees ITS OWN arm's critic answer (the unbatched
   meta call sees the unbatched critic answer; the batched meta call sees
   the batched critic answer) -- votes and ballots are frozen; the critic's
   own verdict is not, because meta's job is to adjudicate what the critic
   actually said in that arm, and freezing it to the other arm's critic
   would silently test a council that cannot occur in either arm.

5. The real decision rule (``engine.decide.decide``) is then run twice per
   item -- once with the unbatched critic/meta calls, once with the batched
   ones, both against the SAME frozen proposer calls -- to produce the
   headline: **the fraction of items whose FINAL VERDICT differs between
   the two arms.**  An intermediate disagreement (the critic said something
   different) that the council still resolves to the same selected ids is
   NOT counted here; it is counted (separately) in the critic's own
   per-seat report, which is what makes the two numbers different and both
   worth printing.

SCOPE LIMITATION, STATED HERE RATHER THAN DISCOVERED LATER
=============================================================
This harness isolates each role's OWN sensitivity to batching by holding
every role's INPUT fixed across both arms (proposer votes/ballots always;
meta's critic input only within its own arm, per point 4 above).  It is
deliberately NOT a comparison of two fully self-consistent end-to-end
pipelines in which one role's batching-induced drift is allowed to
propagate into the next role's prompt.  A correlated effect across roles in
a real, unfrozen batched run could be larger than what this harness reports.
That is the trade this harness makes to produce a number that is
attributable to one seat at a time rather than a tangle of three.

META RUNS ON EVERY SAMPLED ITEM, NOT ONLY WHERE PRODUCTION WOULD REACH IT.
In production, ``decide()`` skips meta when the critic approves and the
vote is unanimous.  This harness invokes the meta seat for every sampled
item regardless, so its own per-seat report is a superset of what batching
would actually touch in a live run.  This is a deliberate simplification:
it spends more tokens than production would, in exchange for a meta-seat
report that never depends on which items happened to need meta.  The
HEADLINE verdict-difference figure is unaffected by this choice: ``decide()``
ignores the meta call entirely when it is not needed (see
``engine.decide.decide``, the unanimous-and-approved branch), so a meta
answer that production would never have asked for cannot change a verdict
that production would never have asked for it to decide.

SAFETY AGAINST A LIVE STORE
=============================
This script's ONLY interaction with ``--store`` is ``store.init_schema()``
(idempotent -- creates nothing on an already-initialised store) and the
read-only accessors ``store.get_job`` / ``store.get_items`` /
``QueryRegistry(store).get``.  It never calls ``claim_calls``,
``add_calls``, ``complete_call``, ``settle_item`` or any other write path,
so it cannot claim, settle or supersede a production call row, and it
writes its OWN results only to wherever ``--out`` points -- never into the
store.

COST, AND WHY THIS ASKS BEFORE SPENDING
==========================================
Both arms spend real subscription tokens against a live backend -- running
this is not free, and running it twice (once per arm) is not half of once.
Before any backend is called, this script prints a rough PROJECTED token
spend (an estimate, not a measurement) and asks for confirmation unless
``--yes`` is passed.  After each arm finishes, it prints the ACTUAL spend
that arm's backend reported.

NO AUTOMATIC AUTHORISATION
=============================
This script does not enable batching for anything, and it cannot: it never
writes to a deployed queue config, and it never calls any function that
flips a seat's verified-output-cap table.  An operator reads this report and
separately approves a measured CLI output cap before any deployed queue's
``batch_size`` is changed from 1.  This script's exit code is a GATE
RECOMMENDATION on the verdict-difference fraction, nothing more.

WHAT A GENUINE ACCURACY GATE WOULD NEED, AND WHY THIS IS NOT ONE
====================================================================
A genuine accuracy gate needs labelled items (a correct answer per item,
from a source other than this pipeline) and a numerical pass threshold
derived from how much disagreement with ground truth is tolerable.  Neither
exists in this repository today.  This script's ``--threshold`` is a
tolerance on AGREEMENT BETWEEN TWO ARMS OF THE SAME UNVALIDATED PIPELINE,
which is a different, weaker thing, and is named that way everywhere this
script prints a number.

USAGE
=====
::

    python scripts/council/batching_accuracy_ab.py \\
        --store sqlite:////home/me/.kbutillib/council/council.sqlite \\
        --job <job_id> --sample-size 100 --seed 1 --batch-size 20 \\
        --threshold 0.05 --out agent-io/research/batching-ab-<job_id>.json

Exit codes:

* ``0`` -- ran to completion; the verdict-difference fraction is at or
  below ``--threshold``.
* ``1`` -- ran to completion; the verdict-difference fraction EXCEEDS
  ``--threshold`` (the gate the ``--threshold`` argument exists to express).
* ``2`` -- a usage error: a bad argument, a missing job, or a declined cost
  confirmation.  Nothing was measured.
* ``3`` -- no arm could be measured at all (no backend could be resolved
  for any seat in the roster -- see "live measurement unavailable" below).
  The harness still "completed" in the sense the task asks for: it ran,
  found nothing it could call, and said so, rather than inventing a number.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The three roles the council runs, in protocol order.
ROLES: Tuple[str, ...] = ("proposer", "critic", "meta")

#: Fixed defaults -- all overridable on the command line.  The task names
#: "100 fixed-seed sampled go_mf items" as the specified deliverable size.
DEFAULT_SAMPLE_SIZE = 100
DEFAULT_SEED = 1
DEFAULT_BATCH_SIZE = 20

#: A crude chars-per-token estimate for the pre-run cost PROJECTION only.
#: The ACTUAL spend this script reports after each arm is measured usage
#: from the backend, never this estimate.
_CHARS_PER_TOKEN_ESTIMATE = 4


def _run(coro: Any) -> Any:
    """Run one coroutine to completion on a loop this call owns.

    Deliberately not ``asyncio.run``, which clears the thread's event loop
    on the way out -- same reasoning as ``tests/.../engine_support.run`` and
    ``scripts/council/replay_go_v3.py``'s own ``_run``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def sample_item_ids(
    item_ids: Sequence[str], sample_size: int, seed: int
) -> List[str]:
    """Deterministically sample up to *sample_size* ids from *item_ids*.

    ``random.Random(seed).sample`` over the STORE's own item order (not a
    sorted one): the store already orders items by submission
    (``item_order``), so this is reproducible given the same store content
    and the same seed, which is the property the task asks for ("a rerun
    compares the same items").  Sampling all of them when the job has fewer
    than *sample_size* items is not an error.
    """
    population = list(item_ids)
    k = min(max(0, sample_size), len(population))
    if k == len(population):
        return population
    return random.Random(seed).sample(population, k)


# ---------------------------------------------------------------------------
# Rebuilding a task/roster from a job's stored rows (read-only; no store write)
# ---------------------------------------------------------------------------


def task_from_item_row(item: Mapping[str, Any]) -> Any:
    """The ``CouncilTask`` a stored item row describes.

    Mirrors ``engine.worker.CouncilWorker._task_for`` exactly (same field
    names), reimplemented here rather than imported because that method is
    private to the worker and this script never constructs one.
    """
    from kbutillib.domains.ai.council.types import CouncilTask, MenuEntry

    return CouncilTask(
        item_id=str(item["item_id"]),
        evidence=dict(item.get("evidence") or {}),
        menu=[MenuEntry(**entry) for entry in (item.get("menu") or [])],
    )


def roster_from_job_row(job: Mapping[str, Any]) -> Any:
    """The ``Roster`` a stored job row was submitted with.

    Mirrors ``engine.worker.CouncilWorker._roster_for``.
    """
    from kbutillib.domains.ai.council.types import Roster, SeatSpec

    raw = dict(job.get("roster") or {})
    return Roster(
        proposers=[SeatSpec(**seat) for seat in (raw.get("proposers") or [])],
        critic=SeatSpec(**raw["critic"]),
        meta=SeatSpec(**raw["meta"]),
        allow_meta_overlap=bool(raw.get("allow_meta_overlap")),
    )


def query_from_job_row(store: Any, job: Mapping[str, Any]) -> Any:
    """The ``QueryType`` a stored job row names, read back through the registry."""
    from kbutillib.domains.ai.council.queries import QueryRegistry

    return QueryRegistry(store).get(str(job["query_name"]), str(job["query_version"]))


# ---------------------------------------------------------------------------
# One call outcome, and the gate/normalisation pipeline every answer passes
# through -- mirrors engine.worker's settlement path (normalise -> validate)
# ---------------------------------------------------------------------------


@dataclass
class CallOutcome:
    """One seat's answer for one item, after the same gate production uses.

    ``ok`` is "the backend invocation itself succeeded" (no transport
    failure, no truncation, and -- for a batched reply -- this item's answer
    was present in the wrapper).  ``valid`` is "and the answer satisfied the
    role's answer schema after normalisation" -- the same two-stage gate
    ``engine.worker`` applies before a reply is ever counted as a vote.
    ``parsed`` is the validated reply in the query type's OWN field names
    (not yet ``postprocess``-ed); ``None`` whenever ``valid`` is ``False``.
    """

    item_id: str
    ok: bool
    valid: bool
    parsed: Optional[Dict[str, Any]]
    reason: Optional[str]
    elapsed_s: float
    repairs: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class InvocationRecord:
    """One backend invocation -- single or batched -- for the cost ledger.

    Usage is recorded exactly once per invocation and never divided across
    its members, mirroring the PRD's binding rule for how one provider
    usage record is attributed across a batch (Q5): dividing it N ways
    would fabricate per-item numbers nobody measured.
    """

    role: str
    seat_name: str
    arm: str  # "unbatched" or "batched"
    n_members: int
    ok: bool
    usage: Optional[Dict[str, Any]]
    elapsed_s: float


def _finalize_answer(
    role: str, query: Any, schema: Mapping[str, Any], context: Mapping[str, Any], parsed: Any
) -> Tuple[Optional[Dict[str, Any]], Optional[str], List[Dict[str, Any]]]:
    """Apply the same normalise-then-validate gate production applies.

    Returns ``(accepted_or_None, invalid_reason_or_None, repairs)``.
    """
    from kbutillib.domains.ai.council.engine.normalise import normalise
    from kbutillib.domains.ai.council.engine.worker import _validate_answer

    if not isinstance(parsed, Mapping):
        return None, f"reply is a {type(parsed).__name__}, not an object", []
    repaired, repairs = normalise(role, dict(parsed), schema, context)
    complaint = _validate_answer(repaired, schema)
    if complaint is not None:
        return None, complaint, repairs
    return dict(repaired), None, repairs


def _outcome_from_result(item_id: str, role: str, query: Any, schema: Mapping[str, Any], context: Mapping[str, Any], result: Any) -> CallOutcome:
    """A :class:`CallOutcome` for one UNWRAPPED (single-item) backend reply."""
    if not result.ok:
        err = result.error
        reason = f"{err.kind}: {err.message}" if err is not None else "backend reported failure with no error"
        return CallOutcome(item_id, False, False, None, reason, result.elapsed_s)
    if result.truncated:
        return CallOutcome(
            item_id, False, False, None,
            "reply truncated at the token ceiling", result.elapsed_s,
        )
    parsed, invalid_reason, repairs = _finalize_answer(role, query, schema, context, result.parsed)
    if parsed is None:
        return CallOutcome(item_id, True, False, None, invalid_reason, result.elapsed_s, repairs)
    return CallOutcome(item_id, True, True, parsed, None, result.elapsed_s, repairs)


def _outcome_from_batch_member(
    item_id: str, role: str, query: Any, schema: Mapping[str, Any], context: Mapping[str, Any],
    entry: Optional[Mapping[str, Any]], elapsed_s: float,
) -> CallOutcome:
    """A :class:`CallOutcome` for one member of a successfully-answered batch."""
    if entry is None:
        return CallOutcome(item_id, True, False, None, "missing from the batched reply", elapsed_s)
    parsed, invalid_reason, repairs = _finalize_answer(role, query, schema, context, entry)
    if parsed is None:
        return CallOutcome(item_id, True, False, None, invalid_reason, elapsed_s, repairs)
    return CallOutcome(item_id, True, True, parsed, None, elapsed_s, repairs)


def settled_call_for(role: str, seat_name: str, query: Any, outcome: CallOutcome) -> Any:
    """The :class:`~engine.decide.SettledCall` *outcome* reduces to.

    Mirrors ``SettledCall.from_call_row``: ``query.postprocess`` is applied
    here, and a postprocess that raises becomes a failed seat rather than an
    uncaught exception, exactly as production treats it.
    """
    from kbutillib.domains.ai.council.engine.decide import SettledCall
    from kbutillib.domains.ai.council.types import CouncilError

    if not outcome.valid or outcome.parsed is None:
        return SettledCall(
            role=role,
            seat_name=seat_name,
            status="error",
            error=CouncilError(kind="invalid_output", message=outcome.reason or "no usable answer"),
        )
    try:
        parsed = dict(query.postprocess(role, dict(outcome.parsed)))
    except Exception as exc:  # noqa: BLE001 - becomes a failed seat, as production does
        return SettledCall(
            role=role,
            seat_name=seat_name,
            status="error",
            error=CouncilError(kind="invalid_output", message=f"{type(exc).__name__}: {exc}"),
        )
    return SettledCall(
        role=role, seat_name=seat_name, status="ok", parsed=parsed, normalised=list(outcome.repairs or ()),
    )


# ---------------------------------------------------------------------------
# Invocation: unbatched (one call per item) and batched (one call per group)
# ---------------------------------------------------------------------------

#: One item ready to be rendered: its id, its task, and the per-item context
#: ``query.render`` needs (``{}`` for a proposer; votes/ballots[/critic] for
#: critic/meta).
RoleEntry = Tuple[str, Any, Dict[str, Any]]


def _chunks(entries: Sequence[RoleEntry], size: int) -> List[List[RoleEntry]]:
    size = max(1, size)
    return [list(entries[i : i + size]) for i in range(0, len(entries), size)]


async def invoke_singles(
    backend: Any, seat: Any, role: str, query: Any, entries: Sequence[RoleEntry],
) -> Tuple[Dict[str, CallOutcome], List[InvocationRecord]]:
    """Answer every entry with its OWN backend invocation ("unbatched" arm)."""
    from kbutillib.domains.ai.council.types import CompletionRequest

    schema = query.answer_schema(role)
    outcomes: Dict[str, CallOutcome] = {}
    invocations: List[InvocationRecord] = []
    for item_id, task, context in entries:
        system, user = query.render(role, task, context)
        req = CompletionRequest(
            seat=seat, role=role, system=system, user=user, json_schema=schema,
            trace_id=f"{item_id}|{role}|{seat.seat_name}", deadline_s=seat.timeout_s,
        )
        result = await backend.complete(req)
        outcome = _outcome_from_result(item_id, role, query, schema, context, result)
        outcomes[item_id] = outcome
        invocations.append(
            InvocationRecord(
                role=role, seat_name=seat.seat_name, arm="unbatched", n_members=1,
                ok=bool(result.ok), usage=dict(result.usage) if result.usage is not None else None,
                elapsed_s=result.elapsed_s,
            )
        )
    return outcomes, invocations


async def invoke_batched(
    backend: Any, seat: Any, role: str, query: Any, entries: Sequence[RoleEntry], batch_size: int,
) -> Tuple[Dict[str, CallOutcome], List[InvocationRecord]]:
    """Answer *entries* in groups of up to *batch_size* ("batched" arm).

    A trailing group of exactly one item is sent UNWRAPPED, exactly as
    production's ``_run_group`` falls through to the single-call path for a
    one-row group -- a batch of one is not a batch.  A whole-invocation
    failure fails every member of that group; unlike production, this
    harness does not bisect and retry, because the point here is to MEASURE
    batching's behaviour once, not to make a production-grade queue out of
    a measurement tool.
    """
    from kbutillib.domains.ai.council.engine.worker import BATCH_WRAPPER_SCHEMA, _validate_answer
    from kbutillib.domains.ai.council.queries.spec import (
        BATCH_ITEM_KEY_FIELD,
        BATCH_SYSTEM_SUFFIX,
        render_batch_user,
        wrap_batch_answer_schema,
    )
    from kbutillib.domains.ai.council.types import CompletionRequest

    schema = query.answer_schema(role)
    wrapped_schema = wrap_batch_answer_schema(schema)
    outcomes: Dict[str, CallOutcome] = {}
    invocations: List[InvocationRecord] = []

    for group in _chunks(list(entries), batch_size):
        if len(group) == 1:
            single_outcomes, single_invocations = await invoke_singles(backend, seat, role, query, group)
            outcomes.update(single_outcomes)
            for inv in single_invocations:
                invocations.append(
                    InvocationRecord(
                        role=inv.role, seat_name=inv.seat_name, arm="batched",
                        n_members=1, ok=inv.ok, usage=inv.usage, elapsed_s=inv.elapsed_s,
                    )
                )
            continue

        rendered = [(item_id, *query.render(role, task, context)) for item_id, task, context in group]
        first_system = rendered[0][1]
        batched_system = f"{first_system}{BATCH_SYSTEM_SUFFIX}"
        batched_user = render_batch_user([(item_id, user) for item_id, _sys, user in rendered])
        trace_id = f"batch|{role}|{seat.seat_name}|{len(group)}"
        req = CompletionRequest(
            seat=seat, role=role, system=batched_system, user=batched_user,
            json_schema=wrapped_schema, trace_id=trace_id, deadline_s=seat.timeout_s,
        )
        result = await backend.complete(req)

        whole_failure_reason: Optional[str] = None
        if not result.ok:
            err = result.error
            whole_failure_reason = f"{err.kind}: {err.message}" if err is not None else "backend reported failure with no error"
        elif result.truncated:
            whole_failure_reason = "the batched reply was truncated at the token ceiling"
        else:
            complaint = _validate_answer(result.parsed, BATCH_WRAPPER_SCHEMA)
            if complaint is not None:
                whole_failure_reason = f"the batched reply did not satisfy the wrapper schema: {complaint}"

        invocations.append(
            InvocationRecord(
                role=role, seat_name=seat.seat_name, arm="batched", n_members=len(group),
                ok=whole_failure_reason is None,
                usage=dict(result.usage) if result.usage is not None else None,
                elapsed_s=result.elapsed_s,
            )
        )

        if whole_failure_reason is not None:
            for item_id, _sys, _user in rendered:
                outcomes[item_id] = CallOutcome(item_id, False, False, None, whole_failure_reason, result.elapsed_s)
            continue

        by_key: Dict[str, Dict[str, Any]] = {}
        for raw in result.parsed.get("answers") or ():
            if not isinstance(raw, Mapping):
                continue
            key = str(raw.get(BATCH_ITEM_KEY_FIELD) or "")
            if not key or key in by_key:
                # Duplicate or unkeyed element -- first occurrence wins,
                # mirroring the "duplicate_first must not win" contract the
                # real worker's batched-settlement path enforces.
                continue
            by_key[key] = {k: v for k, v in raw.items() if k != BATCH_ITEM_KEY_FIELD}

        for item_id, task, context in group:
            entry = by_key.get(item_id)
            outcomes[item_id] = _outcome_from_batch_member(
                item_id, role, query, schema, context, entry, result.elapsed_s
            )

    return outcomes, invocations


# ---------------------------------------------------------------------------
# Normalised per-role decision keys -- what "identical" and "differ" mean
# ---------------------------------------------------------------------------


def decision_key(role: str, call: Any) -> Optional[Tuple[Any, ...]]:
    """The normalised decision *call* (a ``SettledCall``) represents, or
    ``None`` if the seat produced no usable answer.

    * proposer -- the chosen menu-id SET, deduped, sorted.
    * critic -- ``(verdict, sorted(overpicked), sorted(missed))``: critic
      does not choose menu ids, so its "decision" is its audit verdict.
    * meta -- the final selected-id SET, deduped, sorted.
    """
    if call.status != "ok" or not isinstance(call.parsed, Mapping):
        return None
    parsed = call.parsed
    if role == "proposer":
        ids = []
        seen: set = set()
        for pick in parsed.get("picked") or ():
            pid = str(pick.get("id") or "") if isinstance(pick, Mapping) else ""
            if pid and pid not in seen:
                seen.add(pid)
                ids.append(pid)
        return tuple(sorted(ids))
    if role == "critic":
        return (
            str(parsed.get("verdict") or ""),
            tuple(sorted(str(x) for x in (parsed.get("overpicked") or ()))),
            tuple(sorted(str(x) for x in (parsed.get("missed") or ()))),
        )
    if role == "meta":
        return tuple(sorted(str(x) for x in (parsed.get("final_ids") or ())))
    raise ValueError(f"unknown role {role!r}")


# ---------------------------------------------------------------------------
# The per-seat report
# ---------------------------------------------------------------------------


@dataclass
class SeatReport:
    role: str
    seat_name: str
    n_items: int = 0
    n_identical: int = 0
    n_differ: int = 0
    n_invalid: int = 0
    disagreements: List[Dict[str, Any]] = field(default_factory=list)
    invalid_items: List[Dict[str, Any]] = field(default_factory=list)
    usage_unbatched: Dict[str, int] = field(default_factory=dict)
    usage_batched: Dict[str, int] = field(default_factory=dict)
    n_invocations_unbatched: int = 0
    n_invocations_batched: int = 0
    latency_unbatched_s: List[float] = field(default_factory=list)
    latency_batched_s: List[float] = field(default_factory=list)

    def to_jsonable(self) -> Dict[str, Any]:
        def _avg(xs: Sequence[float]) -> Optional[float]:
            return (sum(xs) / len(xs)) if xs else None

        return {
            "role": self.role,
            "seat_name": self.seat_name,
            "n_items_compared": self.n_items,
            "n_identical": self.n_identical,
            "n_differ": self.n_differ,
            "n_invalid": self.n_invalid,
            "disagreements": self.disagreements,
            "invalid_items": self.invalid_items,
            "usage": {"unbatched": self.usage_unbatched, "batched": self.usage_batched},
            "n_invocations": {
                "unbatched": self.n_invocations_unbatched,
                "batched": self.n_invocations_batched,
            },
            "latency_s_avg": {
                "unbatched": _avg(self.latency_unbatched_s),
                "batched": _avg(self.latency_batched_s),
            },
        }


def _sum_usage(into: MutableMapping[str, int], usage: Optional[Mapping[str, Any]]) -> None:
    if not usage:
        return
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            into[key] = into.get(key, 0) + int(value)


def build_seat_report(
    role: str, seat_name: str, item_ids: Sequence[str],
    unbatched_calls: Mapping[str, Any], batched_calls: Mapping[str, Any],
    unbatched_invocations: Sequence[InvocationRecord], batched_invocations: Sequence[InvocationRecord],
) -> SeatReport:
    """Compare two already-settled call maps for one (role, seat)."""
    report = SeatReport(role=role, seat_name=seat_name)
    for item_id in item_ids:
        unb_call = unbatched_calls.get(item_id)
        bat_call = batched_calls.get(item_id)
        unb_key = decision_key(role, unb_call) if unb_call is not None else None
        bat_key = decision_key(role, bat_call) if bat_call is not None else None
        report.n_items += 1
        if unb_key is None or bat_key is None:
            report.n_invalid += 1
            report.invalid_items.append(
                {
                    "item_id": item_id,
                    "unbatched_valid": unb_key is not None,
                    "batched_valid": bat_key is not None,
                }
            )
            continue
        if unb_key == bat_key:
            report.n_identical += 1
        else:
            report.n_differ += 1
            report.disagreements.append(
                {
                    "item_id": item_id,
                    "unbatched": list(unb_key),
                    "batched": list(bat_key),
                }
            )
    for inv in unbatched_invocations:
        _sum_usage(report.usage_unbatched, inv.usage)
        report.n_invocations_unbatched += 1
        report.latency_unbatched_s.append(inv.elapsed_s)
    for inv in batched_invocations:
        _sum_usage(report.usage_batched, inv.usage)
        report.n_invocations_batched += 1
        report.latency_batched_s.append(inv.elapsed_s)
    return report


# ---------------------------------------------------------------------------
# The full report
# ---------------------------------------------------------------------------


@dataclass
class ABReport:
    store_url: str
    job_id: str
    sample_size: int
    seed: int
    batch_size: int
    threshold: float
    item_ids: List[str]
    seat_reports: List[SeatReport]
    n_verdict_differs: int
    verdict_differing_items: List[Dict[str, Any]]
    generated_at: str

    @property
    def n_items(self) -> int:
        return len(self.item_ids)

    @property
    def verdict_difference_fraction(self) -> float:
        if not self.item_ids:
            return 0.0
        return self.n_verdict_differs / len(self.item_ids)

    @property
    def gate_passed(self) -> bool:
        return self.verdict_difference_fraction <= self.threshold

    def to_jsonable(self) -> Dict[str, Any]:
        return {
            "framing": {
                "measures": "agreement with the unbatched pipeline, NOT accuracy against truth",
                "note": (
                    "no labelled go_mf data exists in this repo; a genuine accuracy gate "
                    "needs labelled items and a numerical pass threshold, neither of which "
                    "this report derives"
                ),
            },
            "run": {
                "store": self.store_url,
                "job_id": self.job_id,
                "sample_size": self.sample_size,
                "seed": self.seed,
                "batch_size": self.batch_size,
                "threshold": self.threshold,
                "n_items": self.n_items,
                "item_ids": self.item_ids,
                "generated_at": self.generated_at,
            },
            "per_seat": [r.to_jsonable() for r in self.seat_reports],
            "headline": {
                "description": (
                    "the fraction of sampled items whose FINAL VERDICT (status, "
                    "selected ids, decline reason) differs between the unbatched "
                    "and batched arms, holding proposer votes/ballots frozen "
                    "across both arms"
                ),
                "n_items": self.n_items,
                "n_verdict_differs": self.n_verdict_differs,
                "fraction": self.verdict_difference_fraction,
                "threshold": self.threshold,
                "gate_passed": self.gate_passed,
                "differing_items": self.verdict_differing_items,
            },
            "authorisation": (
                "this report does not enable batching for anything; an operator must "
                "separately approve this report AND a measured CLI output cap before "
                "any deployed queue's batch_size is raised above 1"
            ),
        }


# ---------------------------------------------------------------------------
# Verdict comparison
# ---------------------------------------------------------------------------


def verdict_key(verdict: Any) -> Tuple[Any, ...]:
    return (verdict.status, tuple(sorted(verdict.selected_ids or ())), verdict.decline_reason)


# ---------------------------------------------------------------------------
# The orchestration -- importable and unit-testable without argparse
# ---------------------------------------------------------------------------


async def arun_ab(
    store: Any,
    job_id: str,
    *,
    sample_size: int,
    seed: int,
    batch_size: int,
    backend_for: Callable[[Any], Any],
) -> ABReport:
    """Run the full A/B comparison and return the report.

    *backend_for* maps a ``SeatSpec`` to an object with an async
    ``complete(CompletionRequest)``, e.g. a ``FakeBackend`` in a test or a
    thin live adapter built from ``--config`` on the command line.  This
    function never writes to *store*: every store call is one of
    ``get_job`` / ``get_items`` / ``QueryRegistry(store).get`` (via
    ``query_from_job_row``).
    """
    from kbutillib.domains.ai.council.engine.decide import decide, tally

    job = store.get_job(job_id)
    if job is None:
        raise ValueError(f"no such job: {job_id!r}")
    all_items = store.get_items(job_id)
    if not all_items:
        raise ValueError(f"job {job_id!r} has no items to sample")

    roster = roster_from_job_row(job)
    query = query_from_job_row(store, job)

    by_item_id = {str(row["item_id"]): row for row in all_items}
    sampled_ids = sample_item_ids(list(by_item_id), sample_size, seed)
    tasks = {item_id: task_from_item_row(by_item_id[item_id]) for item_id in sampled_ids}

    # -- proposers: each seat answers every item unbatched and batched -----
    proposer_unb_outcomes: Dict[str, Dict[str, CallOutcome]] = {}
    proposer_bat_outcomes: Dict[str, Dict[str, CallOutcome]] = {}
    proposer_unb_invocations: Dict[str, List[InvocationRecord]] = {}
    proposer_bat_invocations: Dict[str, List[InvocationRecord]] = {}

    for seat in roster.proposers:
        entries: List[RoleEntry] = [(item_id, tasks[item_id], {}) for item_id in sampled_ids]
        backend = backend_for(seat)
        unb_out, unb_inv = await invoke_singles(backend, seat, "proposer", query, entries)
        bat_out, bat_inv = await invoke_batched(backend, seat, "proposer", query, entries, batch_size)
        proposer_unb_outcomes[seat.seat_name] = unb_out
        proposer_bat_outcomes[seat.seat_name] = bat_out
        proposer_unb_invocations[seat.seat_name] = unb_inv
        proposer_bat_invocations[seat.seat_name] = bat_inv

    # -- the frozen reference: tally the UNBATCHED proposer answers --------
    proposer_ref_calls: Dict[str, List[Any]] = {}
    ref_votes: Dict[str, Dict[str, int]] = {}
    ref_ballots: Dict[str, List[Any]] = {}
    for item_id in sampled_ids:
        calls = [
            settled_call_for("proposer", seat.seat_name, query, proposer_unb_outcomes[seat.seat_name][item_id])
            for seat in roster.proposers
        ]
        proposer_ref_calls[item_id] = calls
        ballots, votes, _errors, _usage = tally(tasks[item_id], roster, calls)
        ref_ballots[item_id] = ballots
        ref_votes[item_id] = votes

    # -- critic: both arms fed the SAME frozen votes/ballots ----------------
    critic_seat = roster.critic
    critic_entries: List[RoleEntry] = [
        (item_id, tasks[item_id], {"votes": ref_votes[item_id], "ballots": ref_ballots[item_id]})
        for item_id in sampled_ids
    ]
    critic_backend = backend_for(critic_seat)
    critic_unb_outcomes, critic_unb_invocations = await invoke_singles(
        critic_backend, critic_seat, "critic", query, critic_entries
    )
    critic_bat_outcomes, critic_bat_invocations = await invoke_batched(
        critic_backend, critic_seat, "critic", query, critic_entries, batch_size
    )
    critic_unb_calls = {
        item_id: settled_call_for("critic", critic_seat.seat_name, query, critic_unb_outcomes[item_id])
        for item_id in sampled_ids
    }
    critic_bat_calls = {
        item_id: settled_call_for("critic", critic_seat.seat_name, query, critic_bat_outcomes[item_id])
        for item_id in sampled_ids
    }

    # -- meta: votes/ballots frozen; each arm sees ITS OWN critic answer ----
    meta_seat = roster.meta

    def _meta_context(item_id: str, critic_calls: Mapping[str, Any]) -> Dict[str, Any]:
        call = critic_calls[item_id]
        critic_payload = call.parsed if call.status == "ok" else None
        return {"votes": ref_votes[item_id], "ballots": ref_ballots[item_id], "critic": critic_payload}

    meta_unb_entries: List[RoleEntry] = [
        (item_id, tasks[item_id], _meta_context(item_id, critic_unb_calls)) for item_id in sampled_ids
    ]
    meta_bat_entries: List[RoleEntry] = [
        (item_id, tasks[item_id], _meta_context(item_id, critic_bat_calls)) for item_id in sampled_ids
    ]
    meta_backend = backend_for(meta_seat)
    meta_unb_outcomes, meta_unb_invocations = await invoke_singles(
        meta_backend, meta_seat, "meta", query, meta_unb_entries
    )
    meta_bat_outcomes, meta_bat_invocations = await invoke_batched(
        meta_backend, meta_seat, "meta", query, meta_bat_entries, batch_size
    )
    meta_unb_calls = {
        item_id: settled_call_for("meta", meta_seat.seat_name, query, meta_unb_outcomes[item_id])
        for item_id in sampled_ids
    }
    meta_bat_calls = {
        item_id: settled_call_for("meta", meta_seat.seat_name, query, meta_bat_outcomes[item_id])
        for item_id in sampled_ids
    }

    # -- headline: the real decision rule, both arms, same frozen proposers -
    n_differs = 0
    differing_items: List[Dict[str, Any]] = []
    for item_id in sampled_ids:
        _what_u, verdict_u = decide(
            tasks[item_id], roster, query, proposer_ref_calls[item_id],
            critic_unb_calls[item_id], meta_unb_calls[item_id],
        )
        _what_b, verdict_b = decide(
            tasks[item_id], roster, query, proposer_ref_calls[item_id],
            critic_bat_calls[item_id], meta_bat_calls[item_id],
        )
        if verdict_u is None or verdict_b is None:  # pragma: no cover - decide() is always terminal here
            continue
        if verdict_key(verdict_u) != verdict_key(verdict_b):
            n_differs += 1
            differing_items.append(
                {
                    "item_id": item_id,
                    "unbatched": list(verdict_key(verdict_u)),
                    "batched": list(verdict_key(verdict_b)),
                }
            )

    # -- per-seat reports ----------------------------------------------------
    seat_reports: List[SeatReport] = []
    for seat in roster.proposers:
        seat_reports.append(
            build_seat_report(
                "proposer", seat.seat_name, sampled_ids,
                {
                    item_id: settled_call_for("proposer", seat.seat_name, query, proposer_unb_outcomes[seat.seat_name][item_id])
                    for item_id in sampled_ids
                },
                {
                    item_id: settled_call_for("proposer", seat.seat_name, query, proposer_bat_outcomes[seat.seat_name][item_id])
                    for item_id in sampled_ids
                },
                proposer_unb_invocations[seat.seat_name],
                proposer_bat_invocations[seat.seat_name],
            )
        )
    seat_reports.append(
        build_seat_report(
            "critic", critic_seat.seat_name, sampled_ids,
            critic_unb_calls, critic_bat_calls, critic_unb_invocations, critic_bat_invocations,
        )
    )
    seat_reports.append(
        build_seat_report(
            "meta", meta_seat.seat_name, sampled_ids,
            meta_unb_calls, meta_bat_calls, meta_unb_invocations, meta_bat_invocations,
        )
    )

    return ABReport(
        store_url=getattr(store, "url", "") or "",
        job_id=job_id,
        sample_size=sample_size,
        seed=seed,
        batch_size=batch_size,
        threshold=float("nan"),  # filled in by the caller, which owns --threshold
        item_ids=sampled_ids,
        seat_reports=seat_reports,
        n_verdict_differs=n_differs,
        verdict_differing_items=differing_items,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def run_ab(
    store: Any, job_id: str, *, sample_size: int, seed: int, batch_size: int,
    threshold: float, backend_for: Callable[[Any], Any],
) -> ABReport:
    """Synchronous facade over :func:`arun_ab`, with ``threshold`` applied."""
    report = _run(arun_ab(store, job_id, sample_size=sample_size, seed=seed, batch_size=batch_size, backend_for=backend_for))
    report.threshold = threshold
    return report


# ---------------------------------------------------------------------------
# Cost projection and confirmation
# ---------------------------------------------------------------------------


def project_cost(store: Any, job_id: str, sample_size: int, seed: int, batch_size: int) -> str:
    """A rough, pre-run token-spend estimate, for the confirmation prompt.

    This is an ESTIMATE from prompt character counts and each seat's
    ``max_output_tokens`` ceiling, not a measurement.  The ACTUAL spend is
    whatever the backend reports after the run, printed separately.
    """
    job = store.get_job(job_id)
    if job is None:
        return f"job {job_id!r} not found; nothing to project"
    all_items = store.get_items(job_id)
    roster = roster_from_job_row(job)
    by_item_id = {str(row["item_id"]): row for row in all_items}
    sampled_ids = sample_item_ids(list(by_item_id), sample_size, seed)
    n = len(sampled_ids)
    if n == 0:
        return "no items sampled; nothing to project"

    avg_evidence_chars = sum(
        len(json.dumps(by_item_id[i].get("evidence") or {})) + len(json.dumps(by_item_id[i].get("menu") or []))
        for i in sampled_ids
    ) / n

    lines = [
        f"PROJECTED token spend (rough estimate, not a measurement) for {n} sampled item(s), "
        f"batch_size={batch_size}:",
    ]
    for role, seats in (
        ("proposer", roster.proposers),
        ("critic", [roster.critic]),
        ("meta", [roster.meta]),
    ):
        for seat in seats:
            n_unbatched = n
            n_batched = math.ceil(n / max(1, batch_size))
            est_in_per_item = avg_evidence_chars / _CHARS_PER_TOKEN_ESTIMATE
            est_out_unbatched = n_unbatched * seat.max_output_tokens
            est_out_batched = n_batched * seat.max_output_tokens  # batches still cap at one seat's ceiling per call
            est_in_total = n * est_in_per_item  # same per-item input content either way
            lines.append(
                f"  {role:<9} {seat.seat_name:<14} unbatched: {n_unbatched:>4} call(s), "
                f"~{int(est_in_total):>8} input tok, ~{int(est_out_unbatched):>8} output-ceiling tok  |  "
                f"batched: {n_batched:>4} call(s), ~{int(est_in_total):>8} input tok, "
                f"~{int(est_out_batched):>8} output-ceiling tok"
            )
    lines.append(
        "Output figures are the CEILING (max_output_tokens x calls), not expected spend; "
        "actual completion tokens are almost always far fewer. Input tokens are projected "
        "as roughly the SAME total across both arms, since batching concatenates the same "
        "per-item content rather than shrinking it -- the real saving is in system-prompt "
        "and request-overhead amortisation, which this rough estimate does not model."
    )
    return "\n".join(lines)


def confirm(prompt_text: str, *, yes: bool) -> bool:
    """Print *prompt_text*; return whether to proceed.

    Always proceeds when *yes*.  Otherwise asks on stdin, and refuses rather
    than guessing when stdin is not a terminal -- a scheduled job that forgot
    ``--yes`` should fail loudly, not hang or silently spend.
    """
    print(prompt_text)
    if yes:
        return True
    if not sys.stdin.isatty():
        print(
            "refusing to spend tokens without --yes in a non-interactive session",
            file=sys.stderr,
        )
        return False
    reply = input("Proceed and spend these tokens? [y/N] ").strip().lower()
    return reply in ("y", "yes")


# ---------------------------------------------------------------------------
# Live backend resolution (--config) -- optional; the harness completes
# without it (binding amendment d)
# ---------------------------------------------------------------------------


class _LiveBackend:
    """Adapter: a seat's resolved endpoint, reached through ``kbutillib.domains.ai.llm``.

    Thin by design -- it calls the SAME public ``llm.acomplete`` every real
    council call goes through, and nothing here duplicates engine internals.
    """

    def __init__(self, api_type: str, endpoint_mapping: Mapping[str, Any]) -> None:
        self._api_type = api_type
        self._endpoint = dict(endpoint_mapping)

    async def complete(self, req: Any) -> Any:
        from kbutillib.domains.ai import llm

        return await llm.acomplete(req, api_type=self._api_type, endpoint=self._endpoint)


def live_backend_for(config: Any) -> Callable[[Any], Any]:
    """A ``backend_for(seat)`` resolving each seat's endpoint out of *config*.

    Raises ``KeyError`` naming the missing endpoint alias if a roster seat's
    ``endpoint`` is not declared in *config* -- caught by the caller and
    reported as "no live arm could be run for this seat", per binding
    amendment (d): this is not a reason to fail the whole script.
    """

    def _for(seat: Any) -> Any:
        if seat.endpoint not in config.endpoints:
            raise KeyError(
                f"seat {seat.seat_name!r} names endpoint {seat.endpoint!r}, which "
                f"--config does not declare (declared: {sorted(config.endpoints)})"
            )
        endpoint_cfg = dict(config.endpoints[seat.endpoint].model_dump())
        endpoint_cfg.setdefault("name", seat.endpoint)
        endpoint_cfg.setdefault("kind", seat.endpoint_kind)
        temp_root = getattr(config, "cli_temp_root", None)
        if temp_root and "temp_root" not in endpoint_cfg:
            endpoint_cfg["temp_root"] = temp_root
        return _LiveBackend(endpoint_cfg["kind"], endpoint_cfg)

    return _for


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def render_text_report(report: ABReport) -> str:
    lines: List[str] = []
    add = lines.append
    add("=" * 78)
    add("BATCHING ACCURACY A/B -- AGREEMENT REPORT (not an accuracy measurement)")
    add("=" * 78)
    add(f"store:        {report.store_url}")
    add(f"job:          {report.job_id}")
    add(f"sample size:  {len(report.item_ids)} (seed={report.seed})")
    add(f"batch size:   {report.batch_size}")
    add("")
    add("-- per-seat agreement (identical / differ / invalid) --")
    for seat_report in report.seat_reports:
        j = seat_report.to_jsonable()
        add(
            f"  {seat_report.role:<9} {seat_report.seat_name:<14} "
            f"identical={seat_report.n_identical:>4} differ={seat_report.n_differ:>4} "
            f"invalid={seat_report.n_invalid:>4} of {seat_report.n_items}"
        )
        add(
            f"      usage: unbatched={j['usage']['unbatched']} "
            f"batched={j['usage']['batched']} "
            f"(invocations unbatched={seat_report.n_invocations_unbatched}, "
            f"batched={seat_report.n_invocations_batched})"
        )
    add("")
    add("-- headline: final-verdict agreement --")
    frac = report.verdict_difference_fraction
    add(
        f"  {report.n_verdict_differs}/{report.n_items} items "
        f"({frac:.1%}) had a different FINAL VERDICT between arms"
    )
    add(f"  threshold: {report.threshold:.1%}  ->  gate {'PASSED' if report.gate_passed else 'FAILED'}")
    add("")
    add(
        "This measures agreement with the unbatched pipeline, NOT accuracy "
        "against truth. It does not enable batching for anything."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batching_accuracy_ab.py",
        description=(
            "Measure AGREEMENT (not accuracy) between a council's unbatched and "
            "batched critic/meta seats on a fixed-seed sample of an existing job's "
            "items. Exits non-zero when the verdict-difference fraction exceeds "
            "--threshold. See the module docstring for the full binding framing: "
            "this is an agreement gate between two arms of the SAME unvalidated "
            "pipeline, not a correctness gate against labelled data -- no labelled "
            "go_mf data exists in this repo, and a genuine accuracy gate would need "
            "labelled items and a numerical pass threshold, neither of which this "
            "script derives."
        ),
    )
    parser.add_argument("--store", required=True, help="sqlite URL or path to an EXISTING council store (read-only: init_schema + get_job/get_items only)")
    parser.add_argument("--job", required=True, dest="job_id", help="job id to sample items from")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE, help=f"number of items to sample (default {DEFAULT_SAMPLE_SIZE}); samples all items if the job has fewer")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"sampling seed, for a reproducible rerun (default {DEFAULT_SEED})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help=f"the batched arm's batch size to compare against unbatched (default {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--threshold", type=float, required=True, help="exit 1 if the verdict-difference fraction exceeds this value (0.0-1.0)")
    parser.add_argument("--config", default=None, help="council config YAML for resolving LIVE endpoints; omit to use the packaged default+user config. Without a usable config, non-fake/replay seats cannot be measured (exit 3), per the task's binding amendment (d): the harness still completes and says so.")
    parser.add_argument("--out", type=Path, default=None, help="write the full JSON report here (NEVER inside the council store)")
    parser.add_argument("--yes", action="store_true", help="skip the cost confirmation prompt")
    return parser


def main(argv: Optional[Sequence[str]] = None, *, backend_for_override: Optional[Callable[[Any], Any]] = None) -> int:
    """The CLI entry point.

    *backend_for_override* is a private hook for tests: it replaces the
    normal ``--config``-derived live backend resolution with a caller-
    supplied ``backend_for(seat)``, which is how the test module drives this
    script against a scripted fake backend without spending a real token or
    needing a CouncilConfig on disk. It is not exposed as a CLI flag.
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    from kbutillib.domains.ai.council.store import open_store

    store = open_store(args.store)
    store.init_schema()

    if args.out is not None and store.path is not None:
        out_path = args.out.resolve()
        if out_path == store.path.resolve():
            print(f"refusing to write the report over the store file itself: {out_path}", file=sys.stderr)
            return 2

    try:
        projection = project_cost(store, args.job_id, args.sample_size, args.seed, args.batch_size)
    except Exception as exc:  # noqa: BLE001 - a usage error, not a crash
        print(f"could not project cost: {exc}", file=sys.stderr)
        return 2

    if not confirm(projection, yes=args.yes):
        print("not proceeding: confirmation declined or unavailable", file=sys.stderr)
        return 2

    if backend_for_override is not None:
        backend_for = backend_for_override
    else:
        from kbutillib.domains.ai.council.config import load_council_config

        try:
            config = load_council_config(args.config)
        except Exception as exc:  # noqa: BLE001 - reported, not crashed on
            print(f"could not load --config: {exc}", file=sys.stderr)
            return 2
        backend_for = live_backend_for(config)

    # Probe every seat's backend resolvability BEFORE spending anything, so
    # a job whose roster this script cannot reach reports that plainly
    # (exit 3) rather than failing midway through the unbatched arm.
    job = store.get_job(args.job_id)
    if job is None:
        print(f"no such job: {args.job_id!r}", file=sys.stderr)
        return 2
    roster = roster_from_job_row(job)
    unresolvable: List[str] = []
    for _role, seat in roster.seats():
        try:
            backend_for(seat)
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            unresolvable.append(f"{seat.seat_name} ({seat.endpoint}): {exc}")
    if unresolvable:
        print("no live arm could be run -- the following seats have no usable backend:", file=sys.stderr)
        for line in unresolvable:
            print(f"  {line}", file=sys.stderr)
        print(
            "This harness still completed in the sense the task asks for: it ran, "
            "found nothing it could call, and said so rather than inventing a number.",
            file=sys.stderr,
        )
        return 3

    report = run_ab(
        store, args.job_id, sample_size=args.sample_size, seed=args.seed,
        batch_size=args.batch_size, threshold=args.threshold, backend_for=backend_for,
    )

    print(render_text_report(report))

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report.to_jsonable(), indent=2, sort_keys=False) + "\n")
        print(f"\nwrote {args.out}")

    return 0 if report.gate_passed else 1


if __name__ == "__main__":
    sys.exit(main())
