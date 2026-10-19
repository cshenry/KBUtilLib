"""Structural interfaces the council is written against.

These are :class:`typing.Protocol` declarations, not base classes: a backend,
a query type or a store is anything with the right shape, so the engine never
imports a concrete implementation and tests can substitute a fake without
inheriting anything.

All three are ``runtime_checkable``, which lets a worker assert the shape of
an object it was handed.  Note that ``runtime_checkable`` only checks that
the members EXIST -- it does not check signatures -- so an ``isinstance``
pass here is a sanity check, not a contract proof.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import (
    Any,
    ContextManager,
    Dict,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    runtime_checkable,
)

from .types import (
    CompletionRequest,
    CompletionResult,
    CouncilTask,
    SeatSpec,
)

__all__ = ["Backend", "QueryRenderer", "CouncilStore"]


@runtime_checkable
class Backend(Protocol):
    """One way of reaching models: an HTTP gateway or a headless CLI.

    The hard rule is in :meth:`complete`: a backend MUST NOT raise for a call
    failure.  The council runs N seats per item and a raise would abort the
    whole item over one bad seat, so every failure comes back as a
    ``CompletionResult`` with ``ok=False`` and ``error`` set.

    This is the council's M1 name for what M2.1 generalised into
    :class:`kbutillib.domains.ai.llm.protocol.ApiType`.  The shape is the
    same and the two ``runtime_checkable`` protocols accept the same objects;
    the only difference is vocabulary -- an ``ApiType`` takes an ``LLMRequest``
    and knows nothing about seats or roles, which is what lets a plug-in
    provider be written without the council.  Nothing in the council
    constructs a backend any more: it asks ``llm.get_api(kind, endpoint)``.
    """

    kind: str

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        """Run one invocation.

        Must not raise for call failures -- returns ``ok=False`` with
        ``error`` set instead.
        """
        ...

    async def models(self) -> List[Dict[str, Any]]:
        """List the models this backend can serve.

        Free (no token spend).  Returns ``[]`` when the backend cannot
        enumerate models, which is not an error.
        """
        ...

    def effective_params(self, seat: SeatSpec) -> Dict[str, Any]:
        """Return the generation parameters this backend would actually send.

        Backends differ in which parameters they accept, so this is what goes
        into the prompt hash rather than the seat's requested params.
        """
        ...

    async def aclose(self) -> None:
        """Release any client/process resources held by the backend."""
        ...


@runtime_checkable
class QueryRenderer(Protocol):
    """Turns a task into prompts and turns raw answers into generic shapes.

    Implemented by ``QueryType``.  This is the only place domain vocabulary
    (GO ids, reaction ids, ...) exists: :meth:`postprocess` renames the
    domain's field names to the generic ones the engine counts votes on, so
    the engine itself stays domain-neutral.
    """

    name: str
    version: str
    entity_field: str

    def render(self, role: str, task: CouncilTask, context: Dict[str, Any]) -> Tuple[str, str]:
        """Return ``(system, user)`` prompts for *role*.

        *context* is ``{}`` for a proposer, ``{"votes", "ballots"}`` for the
        critic and ``{"votes", "ballots", "critic"}`` for meta.
        """
        ...

    def answer_schema(self, role: str) -> Dict[str, Any]:
        """Return the JSON Schema the answer for *role* must satisfy."""
        ...

    def postprocess(self, role: str, parsed: Dict[str, Any]) -> Dict[str, Any]:
        """Map the domain answer onto the generic shape for *role*.

        Generic shapes (after postprocess):

        * proposer -- ``{"picked": [{"id", "confidence", "why"}],
          "overall_confidence", "no_functional_content", "notes"}``
        * critic -- ``{"verdict": strongly_consistent|consistent|ambiguous|
          inconsistent, "reason", "overpicked": [id], "missed": [id],
          "contentless_input": bool}``
        * meta -- ``{"final_ids": [id], "final_confidence",
          "no_functional_content", "reasoning"}``
        """
        ...


@runtime_checkable
class CouncilStore(Protocol):
    """Where jobs, items, call rows and verdicts live.

    Call rows ARE the queue: a worker claims pending calls, runs them through
    the configured backends, and advances each item through
    proposers -> critic -> meta -> verdict.  One implementation satisfies this
    protocol, a single-user SQLite file on local disk; reuse lookup is keyed
    on the content hashes from :mod:`.types`, so a question this store has
    already paid for is not asked again by a later job.

    Row shapes are plain dicts keyed by the column names in
    :mod:`.store.schema`, not models, because the store is also read by the
    CLI and by operators with a SQL prompt.

    Methods are grouped below in contract order: schema, query types,
    jobs/items/calls, the queue, endpoint state, job control, workers,
    search.
    """

    # -- schema -----------------------------------------------------------

    def init_schema(self) -> None:
        """Create every table and stamp the current schema version."""
        ...

    def schema_version(self) -> int:
        """Return the stored schema version (0 if not initialised)."""
        ...

    def migrate(self) -> int:
        """Bring the database up to the current schema version."""
        ...

    # -- query types ------------------------------------------------------

    def put_query_type(
        self,
        name: str,
        version: str,
        spec: Dict[str, Any],
        source: str = "user",
        created_by: Optional[str] = None,
    ) -> None:
        """Save a query type.

        Immutable: re-putting a DIFFERENT spec under an existing
        ``(name, version)`` raises ``QueryTypeExists``; re-putting an
        identical one is a no-op.
        """
        ...

    def get_query_type(
        self, name: str, version: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Return one query type, or the latest active version."""
        ...

    def list_query_types(self, include_retired: bool = False) -> List[Dict[str, Any]]:
        """Return the saved query types."""
        ...

    def retire_query_type(self, name: str, version: str) -> bool:
        """Mark a query type retired without removing it."""
        ...

    # -- jobs, items, calls -----------------------------------------------

    def create_job(
        self,
        job: Dict[str, Any],
        items: List[Dict[str, Any]],
        proposer_calls: List[Dict[str, Any]],
    ) -> str:
        """Write a job, its items and its proposer calls in ONE transaction."""
        ...

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return one job row."""
        ...

    def list_jobs(
        self,
        user: Optional[str] = None,
        status: Optional[str] = None,
        include_deleted: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return job rows, excluding tombstoned jobs by default."""
        ...

    def get_items(self, job_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return a job's items."""
        ...

    def get_calls(self, job_id: str, item_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return a job's call rows."""
        ...

    def add_calls(self, calls: List[Dict[str, Any]]) -> List[str]:
        """Append call rows -- the critic and meta rounds."""
        ...

    def settle_item(
        self, job_id: str, item_id: str, status: str, verdict: Any = None
    ) -> None:
        """Record an item's final status and verdict."""
        ...

    # -- the queue --------------------------------------------------------

    def claim_calls(
        self,
        worker_id: str,
        endpoints: Set[str],
        limit: int,
        now: Optional[datetime] = None,
        *,
        user: Optional[str] = None,
        seats: Optional[Set[str]] = None,
        roles: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Atomically claim up to *limit* due pending calls.

        Every call in the store whose job is neither cancelled nor paused nor
        tombstoned, whose endpoint is in *endpoints* and is not halted, and
        which is due (``not_before`` null or past) -- with NO filter on
        ``jobs.submitted_by``, which is a provenance label and not an owner.
        Ordered by job priority then submission time then item order then
        role.  *user* is a deprecated no-op.

        *seats* and *roles*, when given (not ``None``), restrict the claim to
        one of those seats / roles; ``None`` (the default) claims across
        every seat and role, unchanged from before these parameters existed.
        """
        ...

    def complete_call(
        self,
        call_id: str,
        claim_token: str,
        status: str,
        reply_text: Optional[str] = None,
        reply_parsed: Optional[Dict[str, Any]] = None,
        fresh_usage: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
        reused_from: Optional[str] = None,
        normalised: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Record a claimed call's outcome.

        ``normalised`` is the repair record ``engine/normalise.py`` made for
        this reply, or ``None`` when it needed none; ``reply_parsed`` is the
        repaired answer and ``reply_text`` the untouched original.

        Raises ``StaleClaim`` if the row was reclaimed and re-handed to
        another worker since this claim was taken.
        """
        ...

    def release_call(
        self,
        call_id: str,
        claim_token: str,
        not_before: Optional[datetime] = None,
        increment_attempt: bool = True,
    ) -> None:
        """Put a claimed call back to pending, clearing its token."""
        ...

    def note_call_error(self, call_id: str, error: Optional[Dict[str, Any]]) -> None:
        """Record a call's last error without changing its status.

        The engine calls this inside the same transaction as a
        ``release_call``, because ``job_status``'s ``waiting_for_model`` and
        ``configuration_mismatch`` blockers read ``calls.error`` on PENDING
        rows and nothing else in the contract can write it there.
        """
        ...

    def set_job_status(
        self, job_id: str, status: str, only_if: Optional[List[str]] = None
    ) -> bool:
        """Move a job's status, optionally only from one of *only_if*.

        The worker owns the ``queued -> running`` and ``-> done``
        transitions and makes them inside the transaction that settles the
        call or the item; the guard is what keeps them off a cancelled job.
        """
        ...

    def transaction(self, immediate: bool = True) -> ContextManager[Any]:
        """Run a block as one transaction, yielding the connection."""
        ...

    def record_attempt(
        self,
        call_id: str,
        attempt_no: int,
        started_at: datetime,
        finished_at: datetime,
        outcome_kind: str,
        usage: Optional[Dict[str, Any]] = None,
        worker_id: Optional[str] = None,
        queued_at: Optional[datetime] = None,
    ) -> None:
        """Record one try at a call.

        ``queued_at`` is the additive, nullable ``attempts.queued_at`` column
        (schema unchanged, no version bump): when the service-time split
        this call is a service and has a queue-wait to report, else absent
        -- "nothing to say", not zero.  Defaults to ``None`` so every caller
        from before this parameter existed is unaffected.

        Raises ``ValueError`` if any argument is a dict containing a
        ``system`` or ``user`` key: prompt text never enters this table.
        """
        ...

    def find_reusable(self, prompt_hash: str) -> Optional[Dict[str, Any]]:
        """Return an already-answered call with this prompt hash, from any user."""
        ...

    def reclaim_stale(
        self, now: Optional[datetime] = None, max_age: timedelta = timedelta(minutes=10)
    ) -> int:
        """Return calls held by dead workers to the queue; return how many."""
        ...

    # -- endpoint state ---------------------------------------------------
    #
    # Keyed on the store's own fixed identity, never on a provenance label:
    # a store is one machine's, so its spend against a gateway is one number.
    # The ``user`` keyword on each of these is a deprecated no-op.

    def get_endpoint_state(
        self, endpoint: str, *, user: Optional[str] = None
    ) -> Dict[str, Any]:
        """Return the endpoint's budget and halt state."""
        ...

    def add_tokens(
        self, endpoint: str, n: int, *, user: Optional[str] = None
    ) -> Dict[str, Any]:
        """Add *n* tokens to the current epoch and the lifetime total."""
        ...

    def set_halt(
        self,
        endpoint: str,
        kind: str,
        message: str = "",
        *,
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Halt an endpoint: no further calls are claimed for it."""
        ...

    def reset_halt(self, endpoint: str, *, user: Optional[str] = None) -> Dict[str, Any]:
        """Clear a halt.  Never changes the token counters."""
        ...

    def reset_budget(
        self, endpoint: str, *, user: Optional[str] = None
    ) -> Dict[str, Any]:
        """Start a new budget epoch, keeping the lifetime total."""
        ...

    # -- job control ------------------------------------------------------

    def job_status(self, job_id: str) -> Dict[str, Any]:
        """Progress, pending work by role and endpoint, blockers, throughput."""
        ...

    def results(self, job_id: str, include_pending: bool = False) -> List[Dict[str, Any]]:
        """Decided items with their verdicts."""
        ...

    def cancel_job(self, job_id: str, user: Optional[str] = None) -> None:
        """Cancel a job: terminal, and it supersedes every pending call.

        *user* is a deprecated no-op: there is no ownership to enforce.
        """
        ...

    def delete_job(self, job_id: str, user: Optional[str] = None) -> None:
        """Tombstone a cancelled or done job.  No row is physically removed."""
        ...

    # -- workers ----------------------------------------------------------

    def register_worker(self, row: Dict[str, Any]) -> str:
        """Insert or refresh a worker row."""
        ...

    def heartbeat_worker(self, worker_id: str, now: Optional[datetime] = None) -> bool:
        """Stamp a worker as alive."""
        ...

    def live_workers(
        self,
        user: Optional[str] = None,
        now: Optional[datetime] = None,
        max_age: timedelta = timedelta(minutes=2),
    ) -> List[Dict[str, Any]]:
        """Return workers that have heartbeated within *max_age*."""
        ...

    def remove_worker(self, worker_id: str) -> bool:
        """Delete a worker row."""
        ...

    def stop_requested(self, worker_id: str) -> bool:
        """Has this worker been asked to stop?"""
        ...

    # -- search -----------------------------------------------------------

    def search_calls(
        self,
        query_name: Optional[str] = None,
        canonical_model: Optional[str] = None,
        entity_hash: Optional[str] = None,
        data_hash: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Find call rows across every job in the store."""
        ...
