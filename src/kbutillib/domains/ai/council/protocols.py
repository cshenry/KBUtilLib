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

from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from .types import (
    CompletionRequest,
    CompletionResult,
    CouncilTask,
    CouncilVerdict,
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
    the submitting user's own backends, and advances each item through
    proposers -> critic -> meta -> verdict.  One implementation is a local
    SQLite file, another a shared PostgreSQL database used by every KBUtilLib
    installation, which is why reuse lookup is keyed on the content hashes
    from :mod:`.types` rather than on anything user-local.

    PROVISIONAL: the task prompt that created this module referred to a
    "STORE CONTRACT" section that was not included in it, so the member list
    below is reconstructed from the queue behaviour the rest of the prompt
    describes.  The store milestone is expected to refine these signatures;
    the engine should treat this as the minimum surface, not a frozen one.
    """

    def create_job(
        self,
        query_name: str,
        query_version: str,
        roster: Dict[str, Any],
        config_snapshot: Dict[str, Any],
        submitted_by: str,
        tasks: Optional[List[CouncilTask]] = None,
    ) -> str:
        """Create a job with its items and return the job id."""
        ...

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """Return the job row, including its stored roster and snapshot."""
        ...

    def list_jobs(self, submitted_by: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return job rows, newest first, optionally for one user only."""
        ...

    def enqueue_calls(self, job_id: str, calls: List[Dict[str, Any]]) -> int:
        """Append pending call rows for *job_id*; return how many were added."""
        ...

    def claim_calls(self, worker_id: str, limit: int = 1) -> List[Dict[str, Any]]:
        """Atomically claim up to *limit* pending calls for *worker_id*."""
        ...

    def record_result(self, call_id: str, result: CompletionResult) -> None:
        """Persist the outcome of a claimed call."""
        ...

    def find_reusable(self, prompt_hash: str) -> Optional[Dict[str, Any]]:
        """Return a prior successful call with this prompt hash, if any."""
        ...

    def record_verdict(self, job_id: str, verdict: CouncilVerdict) -> None:
        """Persist the decided verdict for one item."""
        ...

    def get_verdicts(self, job_id: str) -> List[CouncilVerdict]:
        """Return every decided verdict for *job_id*."""
        ...

    def halt_endpoint(self, endpoint: str, reason: str) -> None:
        """Mark *endpoint* unusable for this store's pending work."""
        ...

    def halted_endpoints(self) -> Dict[str, str]:
        """Return ``{endpoint: reason}`` for every currently halted endpoint."""
        ...

    def close(self) -> None:
        """Release the store's connections."""
        ...
