"""In-process API types for tests: no network, no tokens, no CLI login.

:class:`FakeBackend` answers from a caller-supplied script, so a test can
drive any sequence of agreements, disagreements and typed failures.
:class:`ReplayBackend` answers from a recorded table keyed by the call's
trace, so a real council transcript can be replayed deterministically.

Both implement :class:`~kbutillib.domains.ai.llm.protocol.ApiType` (and so
also the council's older ``Backend`` protocol, which is the same shape), and
neither ever raises for a call failure -- that is the contract, and the tests
that depend on it are the reason it holds.

These two stay in the COUNCIL rather than moving to ``llm/apis`` with
the four real ones, and they are deliberately NOT registered as api types:
``fake`` and ``replay`` are not kinds a shipped configuration may name, so a
test that wants them registers them itself (see
``tests/domains/ai/llm/test_llm_wrapper.py``).  That is what makes it
impossible for a packaged config to name an endpoint that silently answers
from a script instead of from a model.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ...llm.types import as_llm_request, generation_params
from ..types import (
    CompletionRequest,
    CompletionResult,
    CouncilError,
    InvalidOutput,
    as_council_error,
)

__all__ = ["FakeBackend", "ReplayBackend"]

_DEFAULT_USAGE: Dict[str, Optional[int]] = {"prompt_tokens": 0, "completion_tokens": 0}


def _result_from_parsed(
    req: Any, parsed: Mapping[str, Any], elapsed: float, usage: Dict[str, Optional[int]]
) -> CompletionResult:
    """Build a successful :class:`CompletionResult` carrying *parsed*."""
    return CompletionResult(
        ok=True,
        text=json.dumps(parsed, sort_keys=True),
        parsed=dict(parsed),
        resolved_model=as_llm_request(req).model,
        usage=dict(usage),
        elapsed_s=elapsed,
        finish_reason="stop",
        truncated=False,
        # The fake returns the script's dict verbatim; no schema was enforced
        # on the way out, and claiming otherwise would hide a bug in a test
        # that is meant to exercise schema handling.
        schema_enforced=False,
    )


def _result_from_error(
    req: Any, error: CouncilError, elapsed: float
) -> CompletionResult:
    """Build a failed :class:`CompletionResult` carrying *error*."""
    return CompletionResult(
        ok=False,
        resolved_model=as_llm_request(req).model,
        elapsed_s=elapsed,
        error=error,
    )


class FakeBackend:
    """A backend whose every answer comes from *script*.

    Args:
        script: Called with the :class:`CompletionRequest`.  Return a ``dict``
            for a successful parsed answer, a :class:`CouncilError` or an
            ``Exception`` *instance* for a failure.  Raising from the script
            works too and is treated identically -- the backend converts it
            rather than letting it escape.
        kind: The backend's ``kind`` string.  Defaults to ``"fake"``; override
            it to exercise kind-specific behaviour.
        usage: Token usage reported on successful calls.  Defaults to zeroes
            rather than ``None`` so budget accounting sees a measured (if
            free) call instead of raising ``BudgetUsageUnknown``.

    Every request is appended to :attr:`calls` in order, so a test can assert
    on what was asked as well as on what came back.
    """

    def __init__(
        self,
        script: Callable[[CompletionRequest], Any],
        kind: str = "fake",
        *,
        usage: Optional[Dict[str, Optional[int]]] = None,
    ) -> None:
        self.kind = kind
        self._script = script
        self._usage = dict(usage) if usage is not None else dict(_DEFAULT_USAGE)
        self.calls: List[CompletionRequest] = []

    async def complete(self, req: Any) -> CompletionResult:
        """Record *req*, run the script, and return its answer as a result.

        The request is recorded AS GIVEN -- a ``CompletionRequest`` stays one
        -- because the engine tests assert on ``req.role`` and
        ``req.seat.seat_name``, which an ``LLMRequest`` does not carry.  Only
        the result builders normalise.
        """
        self.calls.append(req)
        started = time.monotonic()
        try:
            answer = self._script(req)
        except BaseException as exc:  # noqa: BLE001 - a backend never raises
            return _result_from_error(req, as_council_error(exc), time.monotonic() - started)

        elapsed = time.monotonic() - started
        if isinstance(answer, CouncilError):
            return _result_from_error(req, answer, elapsed)
        if isinstance(answer, BaseException):
            return _result_from_error(req, as_council_error(answer), elapsed)
        if isinstance(answer, Mapping):
            return _result_from_parsed(req, answer, elapsed, self._usage)
        return _result_from_error(
            req,
            InvalidOutput(
                f"FakeBackend script returned {type(answer).__name__}, expected a dict"
            ).to_error(),
            elapsed,
        )

    async def models(self) -> List[Dict[str, Any]]:
        """Return ``[]`` -- a fake enumerates nothing."""
        return []

    def effective_params(self, request: Any) -> Dict[str, Any]:
        """Return the request's own generation params unchanged.

        A fake applies no provider mapping, so what it would "send" is what it
        was asked for.  Accepts an ``LLMRequest`` or a ``SeatSpec``, which is
        why the engine tests can keep passing a seat.
        """
        return generation_params(request)

    async def aclose(self) -> None:
        """No resources to release."""
        return None


class ReplayBackend:
    """A backend that answers from a recorded transcript.

    Args:
        table: Maps ``(task_id, role, seat_name)`` to the parsed answer dict.
        kind: The backend's ``kind`` string, default ``"replay"``.

    The key is read out of :attr:`CompletionRequest.trace_id`, which carries
    ``"task_id|role|seat_name"``.  A trace that is not in the table (or is not
    in that three-part form) yields ``ok=False`` with an ``invalid_output``
    error rather than a raise, so a replay with a gap fails the one call it
    has no recording for instead of the whole run.
    """

    def __init__(
        self,
        table: Mapping[Tuple[str, str, str], Mapping[str, Any]],
        kind: str = "replay",
    ) -> None:
        self.kind = kind
        self._table = {tuple(k): dict(v) for k, v in dict(table).items()}
        self.calls: List[CompletionRequest] = []

    async def complete(self, req: Any) -> CompletionResult:
        """Look *req*'s trace up in the table and replay its recorded answer."""
        self.calls.append(req)
        started = time.monotonic()
        parts = tuple((req.trace_id or "").split("|"))
        if len(parts) != 3:
            return _result_from_error(
                req,
                InvalidOutput(
                    f"trace_id {req.trace_id!r} is not 'task_id|role|seat_name'"
                ).to_error(),
                time.monotonic() - started,
            )
        parsed = self._table.get(parts)
        if parsed is None:
            return _result_from_error(
                req,
                InvalidOutput(f"no recorded answer for {parts}").to_error(),
                time.monotonic() - started,
            )
        return _result_from_parsed(req, parsed, time.monotonic() - started, dict(_DEFAULT_USAGE))

    async def models(self) -> List[Dict[str, Any]]:
        """Return ``[]`` -- a replay enumerates nothing."""
        return []

    def effective_params(self, request: Any) -> Dict[str, Any]:
        """Return the request's own generation params unchanged."""
        return generation_params(request)

    async def aclose(self) -> None:
        """No resources to release."""
        return None
