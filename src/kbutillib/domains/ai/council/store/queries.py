"""Dialect-neutral SQL for the council's queue and job control.

Every statement here is built with SQLAlchemy Core against the one
:mod:`.schema` metadata and compiles unchanged on SQLite and PostgreSQL, so
the two store implementations share the queue's *meaning* and differ only in
how they take the lock around it:

* :class:`~.sqlite.SqliteStore` wraps select-then-update in ``BEGIN
  IMMEDIATE`` (see its ``transaction()``), which takes SQLite's single writer
  lock before the first read.
* ``PostgresStore`` will pass ``for_update=True`` to
  :func:`select_claimable`, which adds ``FOR UPDATE OF calls SKIP LOCKED`` so
  concurrent workers step over each other's rows instead of queueing behind
  them.

That is the whole dialect difference.  Keeping it to one keyword argument is
the reason this module exists: a claim-ordering or blocker rule fixed here is
fixed for both stores at once, and the two cannot drift into claiming the
same work twice in different ways.

Two non-SQL helpers live here as well -- :func:`refuse_prompt_text` and
:data:`BLOCKER_BY_ERROR_KIND`.  They are queue *rules* rather than queries,
but they are equally dialect-neutral and equally mandatory for both stores,
and putting them here is what stops ``PostgresStore`` having to import from
the SQLite module to get them right.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Mapping, Optional

import sqlalchemy as sa

from . import schema as sch

__all__ = [
    "ROLE_ORDER",
    "SETTLED_ITEM_STATUSES",
    "BLOCKER_BY_ERROR_KIND",
    "role_rank",
    "refuse_prompt_text",
    "error_kind",
    "select_claimable",
    "claim_update",
    "select_call",
    "select_held_daemon_generation",
    "select_daemon_generation",
    "complete_call_update",
    "release_call_update",
    "note_call_error_update",
    "set_job_status_update",
    "reclaim_stale_update",
    "select_reusable",
    "select_item_status_counts",
    "select_pending_call_facets",
    "count_calls_finished_since",
    "select_results",
    "cancel_job_update",
    "cancel_unsettled_items_update",
    "supersede_pending_calls_update",
    "tombstone_job_update",
]

#: Claim order within one item: a critic cannot run before its proposers and
#: meta cannot run before the critic, so a worker that can only take one call
#: must take the earliest stage.
ROLE_ORDER = ("proposer", "critic", "meta")

#: Item statuses that are final.  An item NOT in this set is unsettled, and
#: ``cancel_job`` settles exactly those -- written as "everything that is not
#: terminal" rather than ``== 'pending'`` so a later transient status is
#: cancelled too rather than silently surviving a cancellation.
SETTLED_ITEM_STATUSES = ("ok", "error", "cancelled")

#: ``CouncilError.kind`` on a pending call's last error -> the ``job_status``
#: blocker it means.  A kind that is absent from this map is a transient
#: failure the worker will simply retry, and is not a blocker.
#:
#: ``configuration_mismatch`` is required by the job-status contract but is
#: NOT in :data:`..types.ERROR_KINDS` on this commit.  The store does not
#: constrain the kinds it stores, so the blocker is wired up now and will
#: start reporting the moment something writes that kind.
BLOCKER_BY_ERROR_KIND = {
    "model_unavailable": "waiting_for_model",
    "configuration_mismatch": "configuration_mismatch",
}


def role_rank() -> Any:
    """``calls.role`` as a sortable integer, in :data:`ROLE_ORDER`.

    A CASE expression rather than a stored rank column: the order is a
    property of the protocol, not of the row, and both dialects sort a CASE
    in an ORDER BY identically.
    """
    return sa.case(
        {role: index for index, role in enumerate(ROLE_ORDER)},
        value=sch.calls.c.role,
        else_=len(ROLE_ORDER),
    )


def refuse_prompt_text(**arguments: Any) -> None:
    """Raise ``ValueError`` if any argument carries prompt text.

    The ``attempts`` table is the council's audit trail and is readable by
    every user of a shared store, so prompts -- which carry the evidence
    text a job was built from -- must never reach it.  The test is any dict,
    at any depth, with a ``system`` or ``user`` key: that is the shape of a
    rendered prompt everywhere in the council (``CompletionRequest``, the
    renderer's ``(system, user)`` pair), so it catches both a whole request
    passed by mistake and one buried in a usage dict.

    Raises:
        ValueError: naming the offending argument and key.
    """
    for name, value in arguments.items():
        found = _find_prompt_key(value)
        if found is not None:
            raise ValueError(
                f"refusing to record an attempt: argument {name!r} contains a "
                f"dict with a {found!r} key, which is prompt text. Prompts are "
                "never stored in the attempts table (the model's reply belongs "
                "in calls.reply_text instead)."
            )


def _find_prompt_key(value: Any, depth: int = 0) -> Optional[str]:
    """Return the first ``system``/``user`` key found in *value*, or None."""
    if depth > 8:  # pathological nesting is not worth walking
        return None
    if isinstance(value, Mapping):
        for key in ("system", "user"):
            if key in value:
                return key
        for nested in value.values():
            found = _find_prompt_key(nested, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        for nested in value:
            found = _find_prompt_key(nested, depth + 1)
            if found is not None:
                return found
    return None


def error_kind(error: Any) -> Optional[str]:
    """The ``kind`` of a stored ``calls.error`` payload, or None.

    ``calls.error`` is a serialised ``CouncilError``; a row that failed before
    one was built can hold anything, so this never raises.
    """
    if isinstance(error, Mapping):
        kind = error.get("kind")
        return str(kind) if kind is not None else None
    return None


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def select_claimable(
    user: str,
    endpoints: Iterable[str],
    now: datetime,
    limit: int,
    *,
    for_update: bool = False,
) -> sa.Select:
    """The calls *user*'s worker may claim, in claim order.

    A call is claimable when it is ``pending``, due (``not_before`` null or
    past), its endpoint is one this worker serves and is not halted for this
    user, and its job is this user's, not cancelled or paused, and not
    tombstoned.

    Order: job priority descending, then submission time, then item order,
    then role (:data:`ROLE_ORDER`), then ``call_id`` so the order is total
    and two workers scanning at once see the same queue.

    Args:
        for_update: add ``FOR UPDATE OF calls SKIP LOCKED``.  PostgreSQL only
            -- it is how that dialect gets what ``BEGIN IMMEDIATE`` gets
            SQLite, and SQLite's driver does not implement it.
    """
    calls, jobs, items = sch.calls, sch.jobs, sch.items

    # Per (user, endpoint): one user's exhausted quota must not stop another
    # user's calls on the same gateway, which is why the halt is looked up
    # against the claiming user rather than against the endpoint alone.
    halted = (
        sa.select(sa.literal(1))
        .where(
            sch.endpoint_state.c.user == user,
            sch.endpoint_state.c.endpoint == calls.c.endpoint,
            sch.endpoint_state.c.halt_kind.is_not(None),
        )
        .correlate(calls)
        .exists()
    )

    stmt = (
        sa.select(calls)
        .join(jobs, calls.c.job_id == jobs.c.job_id)
        .join(
            items,
            sa.and_(
                items.c.job_id == calls.c.job_id,
                items.c.item_id == calls.c.item_id,
            ),
        )
        .where(
            calls.c.status == "pending",
            sa.or_(calls.c.not_before.is_(None), calls.c.not_before <= now),
            calls.c.endpoint.in_(sorted(endpoints)),
            jobs.c.submitted_by == user,
            jobs.c.status.not_in(("cancelled", "paused")),
            jobs.c.deleted_at.is_(None),
            ~halted,
        )
        .order_by(
            jobs.c.priority.desc(),
            jobs.c.submitted_at.asc(),
            items.c.item_order.asc(),
            role_rank().asc(),
            calls.c.call_id.asc(),
        )
        .limit(limit)
    )
    if for_update:
        stmt = stmt.with_for_update(skip_locked=True, of=calls)
    return stmt


def claim_update() -> sa.Update:
    """An ``executemany``-ready UPDATE stamping one claim onto one call.

    One statement for the whole batch, one parameter set per call, because
    each claimed call gets its own ``claim_token`` -- a shared token would
    let a worker complete a call it no longer holds by quoting a sibling's.

    The ``status == 'pending'`` guard is belt and braces: under ``BEGIN
    IMMEDIATE`` (or ``SKIP LOCKED``) nothing can have taken the row between
    the select and this update, and a zero rowcount would mean the locking
    strategy is broken.
    """
    calls = sch.calls
    return (
        calls.update()
        .where(
            calls.c.call_id == sa.bindparam("b_call_id"),
            calls.c.status == "pending",
        )
        .values(
            status="claimed",
            claim_token=sa.bindparam("b_claim_token", type_=calls.c.claim_token.type),
            claimed_by=sa.bindparam("b_claimed_by", type_=calls.c.claimed_by.type),
            claimed_at=sa.bindparam("b_claimed_at", type_=calls.c.claimed_at.type),
            lease_generation=sa.bindparam(
                "b_lease_generation", type_=calls.c.lease_generation.type
            ),
        )
    )


def select_call(call_id: str, *, for_update: bool = False) -> sa.Select:
    """One call row by id, optionally locked for the rest of the transaction."""
    stmt = sa.select(sch.calls).where(sch.calls.c.call_id == call_id)
    if for_update:
        stmt = stmt.with_for_update(of=sch.calls)
    return stmt


def select_held_daemon_generation(user: str, worker_id: str) -> sa.Select:
    """The generation of *user*'s daemon lease, if *worker_id* holds it.

    ``claim_calls`` takes no generation argument, so the one it stamps on a
    claim is derived: a worker holding the lease claims under its generation
    and a foreground worker claims under none.  That is what lets
    ``complete_call`` tell a superseded daemon's late reply from a live one.
    """
    return sa.select(sch.daemon_lease.c.generation).where(
        sch.daemon_lease.c.user == user,
        sch.daemon_lease.c.holder_worker_id == worker_id,
    )


def select_daemon_generation(user: str) -> sa.Select:
    """The current generation of *user*'s daemon lease, whoever holds it."""
    return sa.select(sch.daemon_lease.c.generation).where(
        sch.daemon_lease.c.user == user
    )


# ---------------------------------------------------------------------------
# Completing and releasing
# ---------------------------------------------------------------------------


def complete_call_update(
    call_id: str,
    claim_token: str,
    *,
    status: str,
    reply_text: Optional[str],
    reply_parsed: Any,
    normalised: Any,
    fresh_usage: Any,
    error: Any,
    reused_from: Optional[str],
    finished_at: datetime,
) -> sa.Update:
    """Settle a claimed call.

    ``claim_token`` is in the WHERE as well as being checked by the caller,
    so a token that stopped being current between the two is a zero rowcount
    rather than an overwrite.  The token is cleared, which also makes a
    second completion with the same token a ``StaleClaim`` instead of a
    silent double-write.
    """
    calls = sch.calls
    return (
        calls.update()
        .where(calls.c.call_id == call_id, calls.c.claim_token == claim_token)
        .values(
            status=status,
            reply_text=reply_text,
            reply_parsed=reply_parsed,
            normalised=normalised,
            fresh_usage=fresh_usage,
            error=error,
            reused_from=reused_from,
            finished_at=finished_at,
            claim_token=None,
        )
    )


def release_call_update(
    call_id: str,
    claim_token: str,
    *,
    not_before: Optional[datetime],
    increment_attempt: bool,
) -> sa.Update:
    """Put a claimed call back on the queue, clearing the claim.

    ``not_before`` is written unconditionally, including as ``None``: a
    release with no backoff must clear an earlier one rather than leave the
    call sitting behind a deadline that has already been served.
    """
    calls = sch.calls
    values: Dict[str, Any] = {
        "status": "pending",
        "claim_token": None,
        "claimed_by": None,
        "claimed_at": None,
        "lease_generation": None,
        "not_before": not_before,
    }
    if increment_attempt:
        values["attempt_count"] = calls.c.attempt_count + 1
    return (
        calls.update()
        .where(calls.c.call_id == call_id, calls.c.claim_token == claim_token)
        .values(**values)
    )


def note_call_error_update(call_id: str, error: Any) -> sa.Update:
    """Write a call's LAST error, leaving its status and claim alone.

    The one column the queue could not write for itself.  ``job_status``
    reads ``calls.error`` on PENDING rows to report the
    ``waiting_for_model`` and ``configuration_mismatch`` blockers, but
    ``release_call``'s contract signature takes no error argument and
    ``complete_call`` moves the row out of ``pending`` -- so the worker
    writes the column itself, inside the same transaction as the release.
    Narrow on purpose: it touches exactly one column and can neither settle a
    call nor change whose claim it is under.
    """
    calls = sch.calls
    return calls.update().where(calls.c.call_id == call_id).values(error=error)


def set_job_status_update(
    job_id: str, status: str, only_if: Optional[Iterable[str]] = None
) -> sa.Update:
    """Move a job to *status*, optionally only from one of *only_if*.

    The guard is what keeps the transition safe: ``queued -> running`` on the
    first claim and ``-> done`` when the last item settles must both refuse
    to touch a job that has since been cancelled, and cancellation is
    terminal.  A caller that passes no guard is saying it has already
    established the current status inside this transaction.
    """
    jobs = sch.jobs
    stmt = jobs.update().where(jobs.c.job_id == job_id)
    if only_if is not None:
        stmt = stmt.where(jobs.c.status.in_(sorted(only_if)))
    return stmt.values(status=status)


def reclaim_stale_update(now: datetime, max_age: timedelta) -> sa.Update:
    """Return calls held by dead workers to the queue.

    A claim is abandoned when it is older than *max_age* AND its worker has
    no heartbeat newer than ``now - max_age``.  Both halves are needed: the
    age alone would steal a slow-but-live call mid-flight, and the heartbeat
    alone would reclaim a call the instant a worker's heartbeat was late.

    The reclaimed row keeps its ``attempt_count``.  Nothing was learned about
    the call -- the worker died -- so charging it an attempt would exhaust the
    retry budget of a call that has not actually been tried.
    """
    calls = sch.calls
    cutoff = now - max_age
    live_worker = (
        sa.select(sa.literal(1))
        .where(
            sch.workers.c.worker_id == calls.c.claimed_by,
            sch.workers.c.heartbeat_at.is_not(None),
            sch.workers.c.heartbeat_at > cutoff,
        )
        .correlate(calls)
        .exists()
    )
    return (
        calls.update()
        .where(
            calls.c.status == "claimed",
            # A null claimed_at is an abandoned claim, not a fresh one.
            sa.or_(calls.c.claimed_at.is_(None), calls.c.claimed_at < cutoff),
            ~live_worker,
        )
        .values(
            status="pending",
            claim_token=None,
            claimed_by=None,
            claimed_at=None,
            lease_generation=None,
        )
    )


def select_reusable(prompt_hash: str) -> sa.Select:
    """The newest answered call carrying *prompt_hash*, from any job or user.

    Crossing users is the point of a shared store: the prompt hash is built
    from the reuse namespace and the canonical model rather than from an
    endpoint alias, so two users asking the same deployment the same question
    get one answer between them.  Tombstoned jobs are deliberately included
    -- deleting a job must not throw away an answer somebody paid for.
    """
    calls = sch.calls
    return (
        sa.select(calls)
        .where(calls.c.prompt_hash == prompt_hash, calls.c.status == "ok")
        .order_by(
            # A row answered before finished_at was recorded still counts,
            # it is just the least recent candidate.
            sa.nullslast(calls.c.finished_at.desc()),
            calls.c.created_at.desc(),
            calls.c.call_id.asc(),
        )
        .limit(1)
    )


# ---------------------------------------------------------------------------
# Job control
# ---------------------------------------------------------------------------


def select_item_status_counts(job_id: str) -> sa.Select:
    """``(status, count)`` over one job's items."""
    return (
        sa.select(sch.items.c.status, sa.func.count().label("n"))
        .where(sch.items.c.job_id == job_id)
        .group_by(sch.items.c.status)
    )


def select_pending_call_facets(job_id: str) -> sa.Select:
    """Role, endpoint and last error of each still-pending call of a job.

    Only ``pending``: a claimed call is work in flight, not work waiting, and
    counting it as outstanding would make a busy job look stalled.  The error
    column comes back raw and is read in Python -- extracting a JSON field in
    SQL is the one part of this module that has no portable spelling.
    """
    calls = sch.calls
    return sa.select(calls.c.role, calls.c.endpoint, calls.c.error).where(
        calls.c.job_id == job_id, calls.c.status == "pending"
    )


def count_calls_finished_since(job_id: str, since: datetime) -> sa.Select:
    """How many of a job's calls finished at or after *since*."""
    calls = sch.calls
    return (
        sa.select(sa.func.count())
        .select_from(calls)
        .where(
            calls.c.job_id == job_id,
            calls.c.finished_at.is_not(None),
            calls.c.finished_at >= since,
        )
    )


def select_results(job_id: str, include_pending: bool) -> sa.Select:
    """A job's items in submission order, settled ones only by default."""
    items = sch.items
    stmt = sa.select(items).where(items.c.job_id == job_id)
    if not include_pending:
        stmt = stmt.where(items.c.status.in_(SETTLED_ITEM_STATUSES))
    return stmt.order_by(items.c.item_order, items.c.item_id)


def cancel_job_update(job_id: str) -> sa.Update:
    """Mark a job cancelled.  Terminal: nothing ever changes it again."""
    return (
        sch.jobs.update()
        .where(sch.jobs.c.job_id == job_id)
        .values(status="cancelled")
    )


def cancel_unsettled_items_update(job_id: str, now: datetime) -> sa.Update:
    """Settle a cancelled job's undecided items as ``cancelled``."""
    items = sch.items
    return (
        items.update()
        .where(
            items.c.job_id == job_id,
            items.c.status.not_in(SETTLED_ITEM_STATUSES),
        )
        .values(status="cancelled", decided_at=now)
    )


def supersede_pending_calls_update(job_id: str) -> sa.Update:
    """Retire a cancelled job's pending calls so no worker claims them.

    ``live`` goes false, which releases them from the one-live-call index.
    CLAIMED calls are untouched on purpose: a worker is already paying for
    them, and the contract has it store the reply -- it stays reusable for
    everyone, which is the cheapest possible outcome of a late cancellation.
    """
    calls = sch.calls
    return (
        calls.update()
        .where(calls.c.job_id == job_id, calls.c.status == "pending")
        .values(status="superseded", live=False)
    )


def tombstone_job_update(job_id: str, now: datetime) -> sa.Update:
    """Set a job's ``deleted_at``.  No row is ever physically removed.

    ``deleted_at IS NULL`` in the WHERE keeps a second delete from moving the
    tombstone's date, so "when was this deleted?" stays answerable.
    """
    jobs = sch.jobs
    return (
        jobs.update()
        .where(jobs.c.job_id == job_id, jobs.c.deleted_at.is_(None))
        .values(deleted_at=now)
    )
