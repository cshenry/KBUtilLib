"""Engine integration: the REAL ``AnthropicBackend`` driven by the REAL
``CouncilWorker`` against a real store, with only the socket replaced by an
injected ``httpx2.MockTransport`` response.

THE OFFLINE COUNTERPART OF PRD ACCEPTANCE CRITERION 4: "an endpoint whose
provider reports no usage object halts with ``budget_usage_unknown``".  There
is no supported way to make the live Anthropic API omit its usage object, so
that criterion can never be met live; it is met here, end to end through the
backend's own parsing, instead.  The live criteria are the operator's
``scripts/council/anthropic_acceptance_rehearsal.py``.

PROVENANCE: the envelopes are the phase-1 ``FIXTURE BASIS``
(``agent-io/work-records/anthropic-sdk-smoke.md``) -- client-compatible with
the installed SDK, but written, not captured from the API.
"""

from __future__ import annotations

import importlib.util
import json
from typing import Any, Dict, List, Optional

import pytest

from kbutillib.domains.ai.council.engine import CouncilWorker
from kbutillib.domains.ai.council.types import Roster, SeatSpec

from .engine_support import (
    critic_answer,
    make_query,
    make_task,
    meta_answer,
    proposer_answer,
    run,
)

pytestmark = [
    pytest.mark.skipif(
        importlib.util.find_spec("anthropic") is None,
        reason="anthropic SDK not installed (kbutillib[anthropic], Python >= 3.10)",
    ),
    pytest.mark.skipif(
        importlib.util.find_spec("sqlalchemy") is None,
        reason="the council store needs the 'council' extra (pip install -e '.[council]')",
    ),
]

ENDPOINT = "a-haiku"
WORKER_SETTINGS: Dict[str, Any] = {
    "heartbeat_interval_s": 0.0,
    "poll_interval_s": 0.01,
    "retry_backoff_s": (0.02, 0.05),
    "retry_jitter": 0.0,
    "idle_grace_s": 0.3,
    "max_idle_s": 15.0,
}


def _seat(name: str, model: str) -> SeatSpec:
    return SeatSpec(seat_name=name, endpoint=ENDPOINT, endpoint_kind="anthropic", model=model,
                    canonical_model=f"anthropic/{model}", max_output_tokens=256,
                    temperature=None, timeout_s=5)


def _roster() -> Roster:
    return Roster(proposers=[_seat("proposer_1", "m1"), _seat("proposer_2", "m2")],
                  critic=_seat("critic", "m3"), meta=_seat("meta", "m4"))


def _backend(usage: Optional[Dict[str, Any]], seen: List[Dict[str, Any]]) -> Any:
    import httpx2

    from kbutillib.domains.ai.llm.apis.anthropic import AnthropicBackend

    def handler(request: Any) -> Any:
        body = json.loads(request.content)
        seen.append(body)
        system = body.get("system", "")
        answer = (critic_answer() if "critic" in system else
                  meta_answer("A:1") if "meta" in system else proposer_answer("A:1"))
        envelope: Dict[str, Any] = {
            "id": "msg_x", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": json.dumps(answer)}],
            "stop_reason": "end_turn", "stop_sequence": None,
        }
        if usage is not None:
            envelope["usage"] = usage
        return httpx2.Response(200, json=envelope)

    return AnthropicBackend({"api_key": "sk-test", "name": ENDPOINT},
                            transport=httpx2.MockTransport(handler))


def _worker(store: Any, backend: Any, budget: int, concurrency: int = 1) -> CouncilWorker:
    config = {"endpoints": {ENDPOINT: {"kind": "anthropic", "account": "anthropic-haiku-4.5",
                                       "max_concurrency": concurrency, "max_total_tokens": budget}}}
    return CouncilWorker(store, config, {ENDPOINT: backend}, "alice", "w-1", **WORKER_SETTINGS)


def test_AC4_no_usage_object_halts_budget_usage_unknown(store: Any) -> None:
    seen: List[Dict[str, Any]] = []
    backend = _backend(None, seen)
    worker = _worker(store, backend, budget=1000)
    job_id = worker.submit(make_query(), [make_task()], _roster())
    run(worker.work(job_id=job_id, until_idle=True))
    run(backend.aclose())

    state = store.get_endpoint_state(ENDPOINT)
    assert state["halt_kind"] == "budget_usage_unknown"
    assert "no usage" in state["halt_message"]
    answered = [r for r in store.get_calls(job_id) if r["status"] == "ok"]
    assert answered, "the reply that arrived is still stored"
    assert answered[0]["fresh_usage"] is None
    assert seen and all("temperature" not in body for body in seen)


def test_usage_reported_lands_in_the_store_as_the_sum_and_drives_the_budget(store: Any) -> None:
    """The contrast case: Anthropic's own field names, stored in the engine's
    vocabulary, so the budget sees real spend and halts ``budget_exhausted``."""
    seen: List[Dict[str, Any]] = []
    usage = {"input_tokens": 5, "output_tokens": 7, "cache_creation_input_tokens": 11,
             "cache_read_input_tokens": 13}
    backend = _backend(usage, seen)
    worker = _worker(store, backend, budget=10)
    job_id = worker.submit(make_query(), [make_task()], _roster())
    run(worker.work(job_id=job_id, until_idle=True))
    run(backend.aclose())

    answered = [r for r in store.get_calls(job_id) if r["status"] == "ok"]
    assert answered[0]["fresh_usage"] == {"prompt_tokens": 29, "completion_tokens": 7}
    state = store.get_endpoint_state(ENDPOINT)
    assert state["halt_kind"] == "budget_exhausted"
    assert "spent 36 of a 10-token budget" in state["halt_message"]
    # One call at a time, so the halt stopped the next one.
    assert len(seen) == 1
