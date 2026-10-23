"""``council_run`` -- stage 4 of the standing ``function_to_go`` pipeline.

Nothing here reaches a network, spends a token or needs a GPU. Three
different depths of scaffolding, each chosen for what it can prove:

* **A FAKE COUNCIL** (:class:`_FakeCouncil`) for the orchestration: the gate,
  the credential refusals, the chunk order, the ``.done`` markers, the retry
  pass and the ``RUN.json`` record. It records what it was asked, so "ONE
  chunk at a time, in plan order" is an assertion about calls rather than
  about a log line.
* **A REAL STORE** for the supervisor: the ceiling arithmetic reads
  ``endpoint_state.epoch_tokens``, which is the number the worker itself
  writes, so a fake store here would be testing this module's opinion of the
  store rather than the store.
* **A REAL STORE AND A REAL** ``CouncilWorker`` **over a** ``FakeBackend``
  for :class:`TestCeilingHaltsARunningWorker`, which is the one assertion the
  PRD would not let be made by reading: that a halt written mid-run actually
  STOPS a running worker rather than merely being recorded. It does -- the
  claimable-call query excludes halted endpoints and ``work()``'s idle check
  reports them as blockers -- and no second stopping mechanism was added.

THE ASSERTIONS THAT MATTER MOST:

* :class:`TestRefusals` -- one test per refusal, each naming its own check. A
  single "it refuses a bad run" test would pass with every check but one
  missing.
* :class:`TestResume` -- a resumed run after a killed chunk re-executes ONLY
  that chunk, against the job it already submitted rather than a duplicate of
  the same 2,000 items.
* :class:`TestRetryPass` -- the run is NOT reported complete before the retry
  pass has run or been declared unnecessary.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from kbutillib.domains.ai.council.approval import (
    CONSUMED_KEY,
    RUN_FILENAME,
    Approval,
    ApprovalRefused,
)
from kbutillib.domains.ai.council.quote import PLAN_FILENAME, load_plan
from kbutillib.domains.genome.ontology_mapping.council_run import (
    AMSC_TOKEN_PATH,
    CHUNK_MARKER_DIR,
    CREDENTIAL_CHECKS,
    DETACH_NOTE,
    HALT_KINDS,
    RETRY_ERROR_KIND,
    RETRY_ROUND_CAP,
    RunRefused,
    _Supervisor,
    council_run,
    gateway_endpoints,
    invalid_output_items,
    resolve_credentials,
    run_summary,
)

#: The bench the fixture plan was built against, and the roster beside it.
PLANNED_BENCH = "b" * 64
PLANNED_ROSTER = "r" * 64
MAPPING_HASH = "e" * 64

#: Two gateways over three endpoints: ``argo`` carries two seats on ONE
#: endpoint and ``mag`` carries two seats on TWO endpoints. Deliberately
#: uneven -- a gateway->endpoint join that assumed one endpoint per gateway
#: would pass a one-to-one fixture by accident, and the ceiling is keyed on
#: the gateway while the store's spend counter is keyed on the endpoint.
ENDPOINTS: Dict[str, Dict[str, Any]] = {
    "argo": {"kind": "argo", "account": "argo-anl", "max_concurrency": 24},
    "mag": {
        "kind": "openai",
        "base_url": "https://mag.example/v1",
        "account": "mag-shared",
        "api_key_env": "AMSC_TOKEN",
        "max_concurrency": 20,
        "max_requests_per_minute": 120,
    },
    "mag2": {
        "kind": "openai",
        "base_url": "https://mag.example/v1",
        "account": "mag-shared",
        "api_key_env": "AMSC_TOKEN",
        "max_concurrency": 20,
        "max_requests_per_minute": 120,
    },
}

#: The approved ceiling, well above anything these fixtures spend except
#: where a test deliberately lowers it.
CEILING = {"argo": 1_000_000, "mag": 1_000_000}


def _transport() -> List[Dict[str, Any]]:
    """The plan's ``transport_allocation``, shaped as stage 1 writes it."""
    return [
        {
            "role": "proposer",
            "seat_name": "proposer_1",
            "endpoint": "argo",
            "endpoint_kind": "argo",
            "gateway": "argo",
            "model": "gemini35flash",
            "canonical_model": "gemini-3.5-flash",
            "route_fingerprint": "1" * 64,
        },
        {
            "role": "proposer",
            "seat_name": "proposer_2",
            "endpoint": "mag",
            "endpoint_kind": "openai",
            "gateway": "mag",
            "model": "gpt-5.6-sol",
            "canonical_model": "gpt-5.6-sol",
            "route_fingerprint": "2" * 64,
        },
        {
            "role": "proposer",
            "seat_name": "proposer_3",
            "endpoint": "mag2",
            "endpoint_kind": "openai",
            "gateway": "mag",
            "model": "gpt-5.6-luna",
            "canonical_model": "gpt-5.6-luna",
            "route_fingerprint": "3" * 64,
        },
        {
            "role": "critic",
            "seat_name": "critic",
            "endpoint": "argo",
            "endpoint_kind": "argo",
            "gateway": "argo",
            "model": "claudesonnet5",
            "canonical_model": "claude-sonnet-5",
            "route_fingerprint": "4" * 64,
        },
    ]


def _plan_dict(n_items: int = 5, chunk_size: int = 2) -> Dict[str, Any]:
    """A ``PLAN.json`` body with *n_items* items chunked *chunk_size* apiece.

    Items inline, so no scratch file is needed; the plan's chunk blocks are
    built with the same ``offset``/``items``/``first_item_id``/
    ``last_item_id`` arithmetic ``council_plan`` writes, because that block is
    exactly what stage 4 slices against.
    """
    items = [
        {
            "item_id": f"t{index}",
            "query_id": f"{index:064x}",
            "evidence": {"text": f"hydrolase {index}", "source": "ADP1"},
            "menu": [{"id": "GO:0016787", "label": "hydrolase activity", "rank": 1}],
        }
        for index in range(n_items)
    ]
    chunks = [
        {
            "index": number,
            "offset": offset,
            "items": len(items[offset : offset + chunk_size]),
            "first_item_id": items[offset]["item_id"],
            "last_item_id": items[min(offset + chunk_size, len(items)) - 1]["item_id"],
        }
        for number, offset in enumerate(range(0, len(items), chunk_size))
    ]
    return {
        "schema_version": 1,
        "generated_at": "2026-10-22T09:00:00Z",
        "run_id": "2026-10-23-adp1",
        "query": "go_mf@1",
        "source": "council/go_mf@1",
        "profile": "go_council_v3",
        "mapping_hash": MAPPING_HASH,
        "bench_fingerprint": PLANNED_BENCH,
        "roster_fingerprint": PLANNED_ROSTER,
        "parameter_set": {
            "query": "go_mf@1",
            "bench_fingerprint": PLANNED_BENCH,
            "mapping_hash": MAPPING_HASH,
        },
        "parameter_set_hash": "p" * 64,
        "transport_allocation": _transport(),
        "items": items,
        "chunks": chunks,
        "concurrency": {"chunk_size": chunk_size, "chunks_in_flight": 1},
    }


def _write_plan(run_dir: Path, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Write ``PLAN.json`` with its own declared ``plan_hash``.

    The hash is :attr:`Plan.plan_hash` via the shipped ``load_plan`` -- stage
    1's own rule rather than a second one this test could get wrong in the
    same way the product does.
    """
    body = dict(body or _plan_dict())
    path = run_dir / PLAN_FILENAME
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    body["plan_hash"] = load_plan(run_dir).plan_hash
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    return body


# ---------------------------------------------------------------------------
# The injected collaborators
# ---------------------------------------------------------------------------


class _FakeConfig:
    """Just enough ``CouncilConfig`` for the credential and gateway joins."""

    def __init__(self, endpoints: Optional[Mapping[str, Mapping[str, Any]]] = None) -> None:
        self._endpoints = {
            name: dict(cfg) for name, cfg in dict(endpoints or ENDPOINTS).items()
        }
        self.default_profile = "go_council_v3"
        self.queues: Dict[str, Any] = {}

    def endpoint(self, name: str) -> Dict[str, Any]:
        return dict(self._endpoints[name])


class _FakeStore:
    """The six ``endpoint_state`` operations stage 4 uses, and nothing else.

    Spend is recorded the way the worker records it (``add_tokens`` into
    ``epoch_tokens``), so the ceiling arithmetic under test reads the column
    it reads in production.
    """

    def __init__(self) -> None:
        self.state: Dict[str, Dict[str, Any]] = {}
        self.halts: List[Dict[str, str]] = []
        self.items: Dict[str, List[Dict[str, Any]]] = {}
        self.calls: Dict[str, List[Dict[str, Any]]] = {}

    def _row(self, endpoint: str) -> Dict[str, Any]:
        return self.state.setdefault(
            endpoint,
            {
                "endpoint": endpoint,
                "budget_epoch": 0,
                "epoch_tokens": 0,
                "lifetime_tokens": 0,
                "halt_kind": None,
                "halt_message": None,
            },
        )

    def get_endpoint_state(self, endpoint: str, **_kw: Any) -> Dict[str, Any]:
        return dict(self._row(endpoint))

    def add_tokens(self, endpoint: str, n: int, **_kw: Any) -> Dict[str, Any]:
        row = self._row(endpoint)
        row["epoch_tokens"] += int(n)
        row["lifetime_tokens"] += int(n)
        return dict(row)

    def set_halt(self, endpoint: str, kind: str, message: str = "", **_kw: Any) -> Dict[str, Any]:
        row = self._row(endpoint)
        row["halt_kind"] = kind
        row["halt_message"] = message
        self.halts.append({"endpoint": endpoint, "kind": kind, "message": message})
        return dict(row)

    def reset_halt(self, endpoint: str, **_kw: Any) -> Dict[str, Any]:
        row = self._row(endpoint)
        row["halt_kind"] = None
        row["halt_message"] = None
        return dict(row)

    def reset_budget(self, endpoint: str, **_kw: Any) -> Dict[str, Any]:
        row = self._row(endpoint)
        row["budget_epoch"] += 1
        row["epoch_tokens"] = 0
        if row["halt_kind"] in ("budget_exhausted", "budget_usage_unknown"):
            row["halt_kind"] = None
            row["halt_message"] = None
        return dict(row)

    def get_items(self, job_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = [dict(row) for row in self.items.get(job_id, ())]
        return [row for row in rows if status is None or row.get("status") == status]

    def get_calls(self, job_id: str, item_id: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = [dict(row) for row in self.calls.get(job_id, ())]
        return [row for row in rows if item_id is None or row.get("item_id") == item_id]


class _FakeLock:
    """The one thing stage 4 asks the execution lock: who holds it."""

    def __init__(self, holder: Optional[Dict[str, Any]] = None) -> None:
        self.holder = holder

    def probe(self) -> Optional[Dict[str, Any]]:
        return dict(self.holder) if self.holder else None


class _FakeCouncil:
    """A council whose jobs settle the instant they are run.

    Records every submission in :attr:`submissions` (in order, with the items
    it was handed) and every drain in :attr:`runs`, which is how the "ONE
    chunk at a time, in plan order" property is asserted on calls rather than
    on prose.

    ``on_run`` lets a test make one specific job behave badly -- spend
    tokens, leave an ``invalid_output`` call behind, never settle -- without
    a second fake.
    """

    def __init__(
        self,
        store: Optional[_FakeStore] = None,
        *,
        config: Optional[_FakeConfig] = None,
        holder: Optional[Dict[str, Any]] = None,
        on_run: Optional[Any] = None,
    ) -> None:
        self._store = store or _FakeStore()
        self._config = config or _FakeConfig()
        self._lock = _FakeLock(holder)
        self._on_run = on_run
        self.submissions: List[Dict[str, Any]] = []
        self.runs: List[str] = []
        self.settled: set = set()

    # -- the surface stage 4 uses --------------------------------------
    def config(self, reload: bool = False) -> _FakeConfig:
        return self._config

    def require_schema(self) -> _FakeStore:
        return self._store

    def store(self, **_kw: Any) -> _FakeStore:
        return self._store

    def execution_lock(self) -> _FakeLock:
        return self._lock

    def submit(self, query: str, items: Any, **kwargs: Any) -> str:
        job_id = f"job-{len(self.submissions) + 1}"
        rows = [dict(row) for row in items]
        self.submissions.append(
            {
                "job_id": job_id,
                "query": query,
                "label": kwargs.get("label"),
                "profile": kwargs.get("profile"),
                "item_ids": [row["item_id"] for row in rows],
            }
        )
        self._store.items[job_id] = [
            {**row, "status": "pending"} for row in rows
        ]
        self._store.calls.setdefault(job_id, [])
        return job_id

    def run(self, job_id: str, **_kw: Any) -> Dict[str, Any]:
        self.runs.append(job_id)
        if self._on_run is not None:
            self._on_run(self, job_id)
        self.settled.add(job_id)
        for row in self._store.items.get(job_id, ()):
            if row["status"] == "pending":
                row["status"] = "ok"
        return self.job_status(job_id)

    def job_status(self, job_id: str) -> Dict[str, Any]:
        rows = self._store.items.get(job_id, [])
        by_status: Dict[str, int] = {}
        for row in rows:
            by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        outstanding = by_status.get("pending", 0)
        return {
            "job_id": job_id,
            "status": "done" if not outstanding else "running",
            "n_items": len(rows),
            "by_status": by_status,
            "percent_complete": 100.0 if not outstanding else 0.0,
            "pending_calls_by_role_endpoint": (
                {"proposer": {"argo": outstanding}} if outstanding else {}
            ),
            "blockers": [],
        }

    # -- helpers a test drives it with ---------------------------------
    def fail_invalid_output(self, job_id: str, item_ids: Sequence[str]) -> None:
        """Make *item_ids* of *job_id* lose their verdict to a truncation."""
        wanted = set(item_ids)
        for row in self._store.items.get(job_id, ()):
            if row["item_id"] in wanted:
                row["status"] = "error"
        self._store.calls[job_id] = list(self._store.calls.get(job_id, ())) + [
            {
                "call_id": f"{job_id}-{item_id}-p1",
                "item_id": item_id,
                "role": "proposer",
                "status": "error",
                "error": {
                    "kind": RETRY_ERROR_KIND,
                    "message": "the reply was truncated at the token ceiling",
                },
            }
            for item_id in item_ids
        ]


def _approve(run_dir: Path, plan: Mapping[str, Any], **overrides: Any) -> Approval:
    """Write a real ``APPROVED.json`` bound to this plan and quote.

    A real :class:`Approval` and its own ``write`` -- not a hand-written dict
    shaped like one -- so the gate under test reads the artifact stage 3
    actually produces.
    """
    quote = {
        "schema_version": 1,
        "plan_hash": plan["plan_hash"],
        "headline": {"upper": {"total_tokens": 2_000_000}},
        "gateways": [
            {"gateway": "argo", "total_tokens_upper": 1_000_000},
            {"gateway": "mag", "total_tokens_upper": 1_000_000},
        ],
    }
    (run_dir / "QUOTE.json").write_text(
        json.dumps(quote, indent=2, sort_keys=True), encoding="utf-8"
    )
    from kbutillib.domains.ai.council.approval import quote_hash

    approval = Approval(
        approved_at=overrides.pop("approved_at", "2026-10-23T09:00:00Z"),
        expires_at=overrides.pop("expires_at", "2099-01-01T00:00:00Z"),
        approver="test",
        plan_hash=str(plan["plan_hash"]),
        quote_hash=quote_hash(quote),
        ceiling_tokens=dict(overrides.pop("ceiling_tokens", CEILING)),
        **overrides,
    )
    approval.write(run_dir)
    return approval


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A run directory holding a real plan, quote and approval."""
    directory = tmp_path / "2026-10-23-adp1"
    directory.mkdir()
    _approve(directory, _write_plan(directory))
    return directory


@pytest.fixture
def env() -> Dict[str, str]:
    """An environment with both credentials resolvable, nothing else."""
    return {"ARGO_USER": "chenry", "AMSC_TOKEN": "tok-live"}


def _live() -> Dict[str, Any]:
    """The live bench, matching the plan exactly: nothing has moved."""
    return {
        "bench_fingerprint": PLANNED_BENCH,
        "roster_fingerprint": PLANNED_ROSTER,
        "transport_allocation": _transport(),
    }


def _run(run_dir: Path, council: _FakeCouncil, env: Dict[str, str], **kwargs: Any) -> Dict[str, Any]:
    """``council_run`` with the live bench resolved from the plan itself.

    ``validate_approval`` resolves the live bench from the configuration when
    it is not given one, and these fixtures have no real roster -- so the
    bench check is satisfied by handing the gate the plan's own recorded
    bench. The bench check itself is covered beside the gate
    (``test_approval.py``), not here.
    """
    import kbutillib.domains.ai.council.approval as approval_module

    original = approval_module.live_bench
    approval_module.live_bench = lambda *_a, **_kw: _live()
    try:
        return council_run(
            run_dir,
            council=council,
            environ=dict(env),
            poll_interval_s=0.01,
            **kwargs,
        )
    finally:
        approval_module.live_bench = original


# ---------------------------------------------------------------------------
# The gateway -> endpoint join
# ---------------------------------------------------------------------------


class TestGatewayEndpoints:
    """The ceiling is keyed on the gateway; the spend counter is not."""

    def test_the_plans_own_record_is_the_join(self) -> None:
        mapping = gateway_endpoints(_plan_dict())
        assert mapping == {"argo": ["argo"], "mag": ["mag", "mag2"]}

    def test_an_endpoint_serving_two_seats_appears_once(self) -> None:
        """``argo`` carries a proposer AND the critic; it is one endpoint."""
        assert gateway_endpoints(_plan_dict())["argo"] == ["argo"]

    def test_a_plan_with_no_transport_block_joins_nothing(self) -> None:
        assert gateway_endpoints({"transport_allocation": []}) == {}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class TestCredentials:
    """Neither credential is exported on this host and neither fails loudly."""

    def test_both_resolve_from_the_environment(self, env: Dict[str, str]) -> None:
        rows = resolve_credentials(_plan_dict(), _FakeConfig(), environ=dict(env))
        by_endpoint = {row["endpoint"]: row for row in rows}
        assert by_endpoint["argo"]["name"] == "ARGO_USER"
        assert by_endpoint["argo"]["source"] == "environment"
        assert by_endpoint["mag"]["source"] == "environment"
        assert set(by_endpoint) == {"argo", "mag", "mag2"}

    def test_no_credential_value_is_ever_returned(self, env: Dict[str, str]) -> None:
        """The rows name what was resolved, never the secret itself."""
        rows = resolve_credentials(_plan_dict(), _FakeConfig(), environ=dict(env))
        assert "tok-live" not in json.dumps(rows)
        assert "chenry" not in json.dumps(rows)

    def test_a_missing_argo_user_refuses_naming_its_check(self) -> None:
        with pytest.raises(RunRefused) as excinfo:
            resolve_credentials(
                _plan_dict(), _FakeConfig(), environ={"AMSC_TOKEN": "t"}
            )
        assert excinfo.value.check == "argo_user"
        assert "argo_user" in CREDENTIAL_CHECKS
        assert "ARGO_USER" in str(excinfo.value)

    def test_an_endpoints_own_user_env_is_honoured(self) -> None:
        config = _FakeConfig(
            {**ENDPOINTS, "argo": {**ENDPOINTS["argo"], "user_env": "ANL_USER"}}
        )
        rows = resolve_credentials(
            _plan_dict(), config, environ={"ANL_USER": "x", "AMSC_TOKEN": "t"}
        )
        assert {row["name"] for row in rows if row["endpoint"] == "argo"} == {"ANL_USER"}

    def test_the_amsc_token_falls_back_to_the_file_and_is_exported(
        self, tmp_path: Path
    ) -> None:
        """RESOLVES, which is what retires the ``kbu-env.sh`` wrapper."""
        token_file = tmp_path / "token"
        token_file.write_text("tok-from-file\n", encoding="utf-8")
        environ = {"ARGO_USER": "chenry"}
        rows = resolve_credentials(
            _plan_dict(), _FakeConfig(), environ=environ, amsc_token_path=token_file
        )
        assert environ["AMSC_TOKEN"] == "tok-from-file"
        assert [row["source"] for row in rows if row["endpoint"] == "mag"] == [
            f"file:{token_file}"
        ]

    def test_an_unresolvable_amsc_token_refuses_naming_its_check(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(RunRefused) as excinfo:
            resolve_credentials(
                _plan_dict(),
                _FakeConfig(),
                environ={"ARGO_USER": "chenry"},
                amsc_token_path=tmp_path / "absent",
            )
        assert excinfo.value.check == "amsc_token"
        assert "amsc_token" in CREDENTIAL_CHECKS
        assert "EMPTY" in str(excinfo.value), (
            "the refusal must say WHY a missing token does not fail loudly"
        )

    def test_an_empty_token_file_is_as_unresolvable_as_a_missing_one(
        self, tmp_path: Path
    ) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("   \n", encoding="utf-8")
        with pytest.raises(RunRefused) as excinfo:
            resolve_credentials(
                _plan_dict(),
                _FakeConfig(),
                environ={"ARGO_USER": "chenry"},
                amsc_token_path=token_file,
            )
        assert excinfo.value.check == "amsc_token"

    def test_an_endpoint_declaring_no_api_key_env_needs_nothing(self) -> None:
        """A local vLLM port ignores the header's value; refusing it would be wrong."""
        config = _FakeConfig(
            {
                **ENDPOINTS,
                "mag": {k: v for k, v in ENDPOINTS["mag"].items() if k != "api_key_env"},
                "mag2": {k: v for k, v in ENDPOINTS["mag2"].items() if k != "api_key_env"},
            }
        )
        rows = resolve_credentials(
            _plan_dict(), config, environ={"ARGO_USER": "chenry"}
        )
        assert {row["requires"] for row in rows if row["kind"] == "openai"} == {"none"}

    def test_a_cli_seat_is_reported_rather_than_refused(self) -> None:
        """A CLI login lives in the CLI's own state; its own preflight checks it."""
        plan = _plan_dict()
        plan["transport_allocation"] = [
            {
                "role": "meta",
                "seat_name": "meta",
                "endpoint": "claude",
                "endpoint_kind": "claude_cli",
                "gateway": "claude-max",
            }
        ]
        rows = resolve_credentials(
            plan, _FakeConfig({"claude": {"kind": "claude_cli"}}), environ={}
        )
        assert rows == [
            {
                "endpoint": "claude",
                "kind": "claude_cli",
                "requires": "interactive_subscription_login",
                "name": "",
                "source": "none",
            }
        ]

    def test_the_default_token_path_is_the_hosts(self) -> None:
        assert AMSC_TOKEN_PATH == "~/.config/amsc/token"


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    """One test per refusal, each asserting the check it names."""

    def test_an_absent_approval_refuses_through_the_gate(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        (run_dir / "APPROVED.json").unlink()
        with pytest.raises(ApprovalRefused) as excinfo:
            _run(run_dir, _FakeCouncil(), env)
        assert excinfo.value.check == "approval_present"

    def test_an_edited_quote_refuses_naming_quote_hash(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        quote = json.loads((run_dir / "QUOTE.json").read_text(encoding="utf-8"))
        quote["headline"]["upper"]["total_tokens"] = 1
        (run_dir / "QUOTE.json").write_text(json.dumps(quote), encoding="utf-8")
        with pytest.raises(ApprovalRefused) as excinfo:
            _run(run_dir, _FakeCouncil(), env)
        assert excinfo.value.check == "quote_hash"

    def test_a_re_planned_job_refuses_naming_plan_hash(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        plan = json.loads((run_dir / PLAN_FILENAME).read_text(encoding="utf-8"))
        plan["items"].append(
            {
                "item_id": "t99",
                "query_id": "f" * 64,
                "evidence": {"text": "new", "source": "ADP1"},
                "menu": [],
            }
        )
        (run_dir / PLAN_FILENAME).write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(ApprovalRefused) as excinfo:
            _run(run_dir, _FakeCouncil(), env)
        assert excinfo.value.check == "plan_hash"

    def test_an_expired_approval_refuses_naming_expires_at(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        plan = json.loads((run_dir / PLAN_FILENAME).read_text(encoding="utf-8"))
        _approve(run_dir, plan, expires_at="2026-01-01T00:00:00Z")
        with pytest.raises(ApprovalRefused) as excinfo:
            _run(run_dir, _FakeCouncil(), env)
        assert excinfo.value.check == "expires_at"

    def test_a_second_undeclared_run_refuses_naming_consumed(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """An approval is a ceiling for ONE run."""
        _run(run_dir, _FakeCouncil(), env)
        with pytest.raises(ApprovalRefused) as excinfo:
            _run(run_dir, _FakeCouncil(), env, resume=False)
        assert excinfo.value.check == "consumed"

    def test_an_unresolvable_argo_user_refuses_before_anything_is_submitted(
        self, run_dir: Path
    ) -> None:
        council = _FakeCouncil()
        with pytest.raises(RunRefused) as excinfo:
            _run(run_dir, council, {"AMSC_TOKEN": "t"})
        assert excinfo.value.check == "argo_user"
        assert council.submissions == [], "a refused run must not submit anything"

    def test_an_unresolvable_amsc_token_refuses_before_anything_is_submitted(
        self, run_dir: Path, tmp_path: Path
    ) -> None:
        council = _FakeCouncil()
        with pytest.raises(RunRefused) as excinfo:
            _run(
                run_dir,
                council,
                {"ARGO_USER": "chenry"},
                amsc_token_path=tmp_path / "absent",
            )
        assert excinfo.value.check == "amsc_token"
        assert council.submissions == []

    def test_a_refused_run_consumes_no_approval(
        self, run_dir: Path, tmp_path: Path, env: Dict[str, str]
    ) -> None:
        """A run that could not start must leave the approval spendable."""
        with pytest.raises(RunRefused):
            _run(run_dir, _FakeCouncil(), {"ARGO_USER": "chenry"},
                 amsc_token_path=tmp_path / "absent")
        assert not (run_dir / RUN_FILENAME).exists()
        record = _run(run_dir, _FakeCouncil(), env)
        assert record["complete"] is True

    def test_an_absent_plan_refuses_naming_its_check(
        self, tmp_path: Path, env: Dict[str, str]
    ) -> None:
        empty = tmp_path / "nothing"
        empty.mkdir()
        with pytest.raises(RunRefused) as excinfo:
            _run(empty, _FakeCouncil(), env)
        assert excinfo.value.check == "plan_present"

    def test_a_chunk_block_that_disagrees_with_the_items_refuses(
        self, tmp_path: Path, env: Dict[str, str]
    ) -> None:
        """A wrong chunk block would silently submit the wrong 2,000 items."""
        directory = tmp_path / "2026-10-23-bad"
        directory.mkdir()
        body = _plan_dict()
        body["chunks"][0]["last_item_id"] = "t9"
        plan = _write_plan(directory, body)
        _approve(directory, plan)
        council = _FakeCouncil()
        with pytest.raises(RunRefused) as excinfo:
            _run(directory, council, env)
        assert excinfo.value.check == "chunk_bounds"
        # EVERY chunk is checked before the approval is consumed and before a
        # budget epoch is opened: a refusal discovered on chunk 7 would have
        # spent six chunks and burned the approval for nothing.
        assert council.submissions == []
        assert not (directory / RUN_FILENAME).exists()
        assert council.require_schema().state == {}

    def test_a_LATER_chunk_block_that_disagrees_refuses_before_chunk_one_runs(
        self, tmp_path: Path, env: Dict[str, str]
    ) -> None:
        """The bounds check covers every chunk, not just the first."""
        directory = tmp_path / "2026-10-23-late"
        directory.mkdir()
        body = _plan_dict()
        body["chunks"][-1]["first_item_id"] = "t0"
        plan = _write_plan(directory, body)
        _approve(directory, plan)
        council = _FakeCouncil()
        with pytest.raises(RunRefused) as excinfo:
            _run(directory, council, env)
        assert excinfo.value.check == "chunk_bounds"
        assert council.submissions == []


# ---------------------------------------------------------------------------
# One chunk at a time, in plan order
# ---------------------------------------------------------------------------


class TestChunkOrder:
    """ONE chunk at a time is load-bearing, not caution -- issue 84."""

    def test_the_chunks_are_submitted_in_plan_order_one_at_a_time(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        council = _FakeCouncil()
        record = _run(run_dir, council, env)
        # Five items at chunk_size 2 -> three chunks, then no retry job.
        assert [sub["item_ids"] for sub in council.submissions] == [
            ["t0", "t1"],
            ["t2", "t3"],
            ["t4"],
        ]
        assert council.runs == ["job-1", "job-2", "job-3"], (
            "each chunk must be drained before the next is submitted"
        )
        assert [row["index"] for row in record["chunks"]] == [0, 1, 2]

    def test_a_chunk_is_drained_before_the_next_is_submitted(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """Interleaved submit/run, which is what keeps the pending set small."""
        order: List[str] = []
        council = _FakeCouncil(on_run=lambda c, job: order.append(f"run:{job}"))
        original_submit = council.submit

        def _submit(query: str, items: Any, **kwargs: Any) -> str:
            job_id = original_submit(query, items, **kwargs)
            order.append(f"submit:{job_id}")
            return job_id

        council.submit = _submit  # type: ignore[method-assign]
        _run(run_dir, council, env)
        assert order == [
            "submit:job-1",
            "run:job-1",
            "submit:job-2",
            "run:job-2",
            "submit:job-3",
            "run:job-3",
        ]

    def test_the_run_record_cites_issue_84_beside_the_chunk_size(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        record = _run(run_dir, _FakeCouncil(), env)
        assert record["chunks_in_flight"] == 1
        assert "issue 84" in record["chunks_in_flight_issue"]
        assert "217 ms" in record["chunks_in_flight_issue"]

    def test_each_chunk_records_items_ok_error_duration_and_tokens(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        store = _FakeStore()

        def _spend(council: _FakeCouncil, job_id: str) -> None:
            store.add_tokens("argo", 1000)
            store.add_tokens("mag", 400)
            store.add_tokens("mag2", 600)

        record = _run(run_dir, _FakeCouncil(store, on_run=_spend), env)
        first = record["chunks"][0]
        assert first["items"] == 2
        assert first["ok"] == 2
        assert first["error"] == 0
        assert first["duration_s"] >= 0
        # mag's two endpoints are summed into one gateway, which is the join
        # the ceiling is keyed on.
        assert first["tokens_by_gateway"] == {"argo": 1000, "mag": 1000}
        assert first["claim_probe"]["pending_calls"] == 2
        assert "issue 84" in first["claim_probe"]["what_this_is"]


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


class TestResume:
    """A resumed run after a killed chunk re-executes ONLY that chunk."""

    def test_done_markers_are_written_per_chunk(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        _run(run_dir, _FakeCouncil(), env)
        markers = sorted(p.name for p in (run_dir / CHUNK_MARKER_DIR).glob("*.done"))
        assert markers == ["chunk-0000.done", "chunk-0001.done", "chunk-0002.done"]

    def test_a_resume_re_executes_only_the_killed_chunk(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """Chunks 0 and 1 are done; the resume must touch chunk 2 and nothing else."""
        first = _FakeCouncil()
        _run(run_dir, first, env)
        # Delete the LAST chunk's markers, i.e. the state a kill mid-chunk-2
        # leaves behind (its .job marker survives a kill; its .done does not).
        (run_dir / CHUNK_MARKER_DIR / "chunk-0002.done").unlink()
        (run_dir / CHUNK_MARKER_DIR / "chunk-0002.job").unlink()

        second = _FakeCouncil()
        record = _run(run_dir, second, env)
        assert [sub["item_ids"] for sub in second.submissions] == [["t4"]], (
            "a resume must not re-submit the chunks that already settled"
        )
        assert len(second.runs) == 1
        # What THIS invocation did. The skipped chunks' rows in ``chunks``
        # keep their original execution's duration, tokens and counts --
        # overwriting them with "skipped" would destroy the run's only record
        # of the work -- so the per-invocation view is reported separately.
        assert record["this_invocation"] == {
            "skipped": [0, 1],
            "adopted": [],
            "submitted": [2],
        }

    def test_a_killed_chunk_is_re_adopted_rather_than_re_submitted(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """Its ``.job`` marker survives, so the same 2,000 items are not re-asked."""
        first = _FakeCouncil()
        _run(run_dir, first, env)
        (run_dir / CHUNK_MARKER_DIR / "chunk-0001.done").unlink()
        adopted = json.loads(
            (run_dir / CHUNK_MARKER_DIR / "chunk-0001.job").read_text(encoding="utf-8")
        )

        second = _FakeCouncil(first.require_schema())
        record = _run(run_dir, second, env)
        assert second.submissions == [], "the surviving .job marker must be adopted"
        assert second.runs == [adopted["job_id"]]
        assert record["this_invocation"]["adopted"] == [1]

    def test_a_resume_does_not_reopen_the_budget_epoch(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """The ceiling covers the WHOLE run, so earlier spend still counts."""
        store = _FakeStore()
        council = _FakeCouncil(store, on_run=lambda c, j: store.add_tokens("argo", 500))
        _run(run_dir, council, env)
        spent = store.get_endpoint_state("argo")["epoch_tokens"]
        epoch = store.get_endpoint_state("argo")["budget_epoch"]

        record = _run(run_dir, _FakeCouncil(store), env)
        assert store.get_endpoint_state("argo")["epoch_tokens"] == spent
        assert store.get_endpoint_state("argo")["budget_epoch"] == epoch
        assert any("NOT reset" in warning for warning in record["warnings"])

    def test_a_half_written_marker_re_executes_rather_than_skips(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """Skipping work nobody can prove was done is the one wrong answer."""
        _run(run_dir, _FakeCouncil(), env)
        (run_dir / CHUNK_MARKER_DIR / "chunk-0001.done").write_text(
            '{"job_id": "job-', encoding="utf-8"
        )
        (run_dir / CHUNK_MARKER_DIR / "chunk-0001.job").unlink()
        second = _FakeCouncil()
        _run(run_dir, second, env)
        assert [sub["item_ids"] for sub in second.submissions] == [["t2", "t3"]]

    def test_a_re_executed_chunk_replaces_its_record_rather_than_duplicating_it(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """Two rows for chunk 1 would make stage 5 read the same job twice."""
        _run(run_dir, _FakeCouncil(), env)
        (run_dir / CHUNK_MARKER_DIR / "chunk-0001.done").unlink()
        record = _run(run_dir, _FakeCouncil(), env)
        keys = [(row.get("retry", False), row["index"]) for row in record["chunks"]]
        assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# The retry pass
# ---------------------------------------------------------------------------


class TestRetryPass:
    """A declared stage, capped at two rounds, and the run waits for it."""

    def test_the_round_cap_is_two(self) -> None:
        assert RETRY_ROUND_CAP == 2

    def test_only_invalid_output_items_without_a_verdict_are_retried(self) -> None:
        store = _FakeStore()
        store.items["job-1"] = [
            {"item_id": "t0", "status": "error", "evidence": {}, "menu": []},
            {"item_id": "t1", "status": "ok", "evidence": {}, "menu": []},
            {"item_id": "t2", "status": "error", "evidence": {}, "menu": []},
        ]
        store.calls["job-1"] = [
            {"item_id": "t0", "error": {"kind": "invalid_output"}},
            # t1 truncated on ONE seat but was decided anyway: re-asking it
            # would re-spend for an answer the run already has.
            {"item_id": "t1", "error": {"kind": "invalid_output"}},
            # t2 failed for a reason a retry pass is not for.
            {"item_id": "t2", "error": {"kind": "transport"}},
        ]
        assert invalid_output_items(store, "job-1") == ["t0"]

    def test_the_retry_pass_re_asks_the_lost_items(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        def _truncate(council: _FakeCouncil, job_id: str) -> None:
            if job_id == "job-1":
                council.fail_invalid_output(job_id, ["t0"])

        council = _FakeCouncil(on_run=_truncate)
        record = _run(run_dir, council, env)
        assert record["retry"]["ran"] is True
        assert [sub["item_ids"] for sub in council.submissions][-1] == ["t0"]
        assert record["retry"]["rounds"][0]["retry_round"] == 1

    def test_a_retry_round_is_recorded_so_stage_5_can_find_it(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """Stage 5 reads ``chunks[].job_id`` and ``chunks[].retry``."""
        from kbutillib.domains.genome.ontology_mapping.ontology_mapping_utils import (
            _run_job_ids,
        )

        def _truncate(council: _FakeCouncil, job_id: str) -> None:
            if job_id == "job-1":
                council.fail_invalid_output(job_id, ["t1"])

        _run(run_dir, _FakeCouncil(on_run=_truncate), env)
        ids = _run_job_ids(run_dir)
        assert ids["job_ids"] == ["job-1", "job-2", "job-3"]
        assert ids["retry_job_ids"] == ["job-4"]

    def test_a_second_round_is_over_the_first_rounds_residue(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        def _truncate(council: _FakeCouncil, job_id: str) -> None:
            if job_id in ("job-1", "job-4"):
                council.fail_invalid_output(job_id, ["t0"])

        council = _FakeCouncil(on_run=_truncate)
        record = _run(run_dir, council, env)
        rounds = record["retry"]["rounds"]
        assert [row["retry_round"] for row in rounds] == [1, 2]
        assert rounds[1]["retried_from"] == [rounds[0]["job_id"]]

    def test_the_round_cap_stops_a_deterministically_truncating_seat(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """Every round truncates; it must still stop at two."""
        council = _FakeCouncil(
            on_run=lambda c, job: c.fail_invalid_output(job, ["t0"])
            if job in ("job-1", "job-4", "job-5")
            else None
        )
        record = _run(run_dir, council, env)
        assert len(record["retry"]["rounds"]) == RETRY_ROUND_CAP

    def test_no_truncation_declares_the_pass_unnecessary_and_says_so(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        record = _run(run_dir, _FakeCouncil(), env)
        assert record["retry"]["ran"] is False
        assert record["retry"]["declared_unnecessary"] is True
        assert "nothing to re-ask" in record["retry"]["reason"]
        assert record["complete"] is True

    def test_the_run_is_not_complete_before_the_retry_pass_has_run(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """A halted chunk pass leaves the retry pass un-run, so not complete."""
        store = _FakeStore()
        store.set_halt("argo", "auth", "the credential was rejected")
        record = _run(run_dir, _FakeCouncil(store), env)
        assert record["complete"] is False
        assert record["retry"]["ran"] is False
        assert record["retry"]["declared_unnecessary"] is False
        assert "not run" in record["retry"]["reason"]

    def test_retry_rounds_zero_is_recorded_as_a_declaration(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        record = _run(run_dir, _FakeCouncil(), env, retry_rounds=0)
        assert record["retry"]["declared_unnecessary"] is True
        assert "retry_rounds=0" in record["retry"]["reason"]
        assert record["complete"] is True

    def test_the_record_says_why_this_error_kind(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        record = _run(run_dir, _FakeCouncil(), env)
        assert record["retry"]["error_kind"] == "invalid_output"
        assert "RETRYABLE_KINDS" in record["retry"]["why"]


# ---------------------------------------------------------------------------
# The ceiling, the halts and the stop
# ---------------------------------------------------------------------------


class TestCeilingAndHalts:
    """The store's own counter and the store's own halt -- never a second one."""

    def _supervisor(self, store: _FakeStore, **kwargs: Any) -> _Supervisor:
        return _Supervisor(
            store,
            ceiling_tokens=kwargs.pop("ceiling_tokens", {"argo": 100, "mag": 100}),
            endpoints_by_gateway=gateway_endpoints(_plan_dict()),
            run_dir=kwargs.pop("run_dir", Path("/nonexistent")),
            **kwargs,
        )

    def test_spend_is_summed_per_gateway_over_its_endpoints(self) -> None:
        store = _FakeStore()
        store.add_tokens("mag", 30)
        store.add_tokens("mag2", 45)
        assert self._supervisor(store).spend() == {"argo": 0, "mag": 75}

    def test_the_ceiling_trips_on_the_gateway_sum_not_one_endpoint(self) -> None:
        store = _FakeStore()
        store.add_tokens("mag", 60)
        store.add_tokens("mag2", 60)
        trip = self._supervisor(store).check()
        assert trip is not None
        assert trip["reason"] == "ceiling"
        assert trip["gateways"][0]["gateway"] == "mag"
        assert trip["gateways"][0]["overshoot_tokens"] == 20

    def test_opening_an_epoch_zeroes_every_endpoint_of_every_gateway(self) -> None:
        store = _FakeStore()
        store.add_tokens("argo", 10)
        store.add_tokens("mag2", 10)
        epochs = self._supervisor(store).open_epoch()
        assert store.get_endpoint_state("mag2")["epoch_tokens"] == 0
        assert epochs == {"argo": {"argo": 1}, "mag": {"mag": 1, "mag2": 1}}

    def test_a_ceiling_halt_is_written_on_every_endpoint_with_its_reason(
        self,
    ) -> None:
        store = _FakeStore()
        store.add_tokens("argo", 500)
        supervisor = self._supervisor(store)
        trip = supervisor.check()
        halted = supervisor.halt(trip)
        assert sorted(halted) == ["argo", "mag", "mag2"]
        for endpoint in halted:
            state = store.get_endpoint_state(endpoint)
            assert state["halt_kind"] == HALT_KINDS["ceiling"] == "budget_exhausted"
            assert "overshoot" in state["halt_message"]

    def test_a_gateway_halt_this_stage_did_not_write_stops_the_run(self) -> None:
        store = _FakeStore()
        store.set_halt("mag", "quota_exhausted", "sustained rate limit")
        trip = self._supervisor(store).check()
        assert trip is not None
        assert trip["reason"] == "gateway_halt"
        assert "sustained rate limit" in trip["detail"]

    def test_a_gateway_halt_is_reported_rather_than_overwritten(self) -> None:
        """Overwriting its kind would lose the reason the operator needs."""
        store = _FakeStore()
        store.set_halt("mag", "auth", "the credential was rejected")
        supervisor = self._supervisor(store)
        assert supervisor.halt(supervisor.check()) == []
        assert store.get_endpoint_state("mag")["halt_kind"] == "auth"

    def test_an_operator_stop_file_stops_the_run(self, tmp_path: Path) -> None:
        (tmp_path / "STOP").write_text("", encoding="utf-8")
        trip = self._supervisor(_FakeStore(), run_dir=tmp_path).check()
        assert trip is not None
        assert trip["reason"] == "operator_stop"

    def test_an_in_process_stop_event_stops_the_run(self) -> None:
        event = threading.Event()
        event.set()
        trip = self._supervisor(_FakeStore(), stop_event=event).check()
        assert trip is not None
        assert trip["reason"] == "operator_stop"

    def test_a_chunk_past_its_budget_stops_rather_than_wedging_the_run(self) -> None:
        supervisor = self._supervisor(_FakeStore(), clock=lambda: 100.0)
        assert supervisor.check(started=0.0, timeout_s=1000.0) is None
        trip = supervisor.check(started=0.0, timeout_s=10.0)
        assert trip is not None
        assert trip["reason"] == "chunk_timeout"
        assert HALT_KINDS["chunk_timeout"] == "chunk_timeout"

    def test_a_low_ceiling_halts_a_run_in_progress_and_records_the_reason(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """The whole point: an approved ceiling stops a run that is spending."""
        plan = json.loads((run_dir / PLAN_FILENAME).read_text(encoding="utf-8"))
        _approve(run_dir, plan, ceiling_tokens={"argo": 1500, "mag": 1_000_000})
        store = _FakeStore()
        council = _FakeCouncil(store, on_run=lambda c, j: store.add_tokens("argo", 1000))
        record = _run(run_dir, council, env)

        assert record["halt"]["reason"] == "ceiling"
        assert "overshoot" in record["halt"]["detail"]
        assert record["complete"] is False
        # Chunk 1 spent 1000 and chunk 2 took it to 2000, past the 1500
        # ceiling; chunk 3 must never have been submitted.
        assert len(council.submissions) == 2
        assert store.get_endpoint_state("argo")["halt_kind"] == "budget_exhausted"
        assert any("HALTED" in warning for warning in record["warnings"])

    def test_a_gateway_with_no_approved_ceiling_is_warned_about(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        plan = json.loads((run_dir / PLAN_FILENAME).read_text(encoding="utf-8"))
        _approve(run_dir, plan, ceiling_tokens={"argo": 1_000_000})
        record = _run(run_dir, _FakeCouncil(), env)
        assert any("no ceiling for gateway" in w for w in record["warnings"])
        assert any("mag" in w for w in record["warnings"])

    def test_a_pre_existing_halt_stops_the_run_before_the_first_submission(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        store = _FakeStore()
        store.set_halt("mag2", "auth", "the credential was rejected")
        council = _FakeCouncil(store)
        record = _run(run_dir, council, env)
        assert record["halt"]["reason"] == "gateway_halt"
        assert council.submissions == []

    def test_a_budget_halt_of_an_earlier_run_is_cleared_by_the_new_epoch(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """``reset_budget`` clears exactly that kind, which is why it is reused."""
        store = _FakeStore()
        for endpoint in ("argo", "mag", "mag2"):
            store.set_halt(endpoint, "budget_exhausted", "an earlier run")
        record = _run(run_dir, _FakeCouncil(store), env)
        assert record["halt"] is None
        assert record["complete"] is True


# ---------------------------------------------------------------------------
# The halt really does stop a running worker
# ---------------------------------------------------------------------------


class TestCeilingHaltsARunningWorker:
    """The one claim the PRD would not let be made by reading.

    A REAL store and a REAL :class:`CouncilWorker` over a ``FakeBackend``: a
    halt is written while the worker is mid-sweep, and what is asserted is
    that the worker STOPS MAKING CALLS and comes out of its loop reporting the
    halt as a blocker -- not merely that the halt row exists.

    It does, as shipped. The claimable-call query excludes a halted
    endpoint's calls (``store/queries.py::select_claimable``) and
    ``_scope_state`` reports one as a ``halted`` blocker, so no second
    stopping mechanism was added to make this pass.
    """

    def _store(self, tmp_path: Path) -> Any:
        from kbutillib.domains.ai.council.store import open_store

        store = open_store(f"sqlite:///{tmp_path}/council.sqlite", check_version=False)
        store.init_schema()
        return store

    #: Items and calls a full run of this fixture makes: 12 items x (3
    #: proposers + 1 critic) = 48, plus meta on whatever disagrees. Named so
    #: the mid-run assertion can say "well short of a full run" in numbers.
    ITEMS = 12
    FULL_RUN_CALLS = 48

    def _harness(self, store: Any, *, delay: float = 0.0, concurrency: int = 4) -> Any:
        from kbutillib.domains.ai.council.engine.worker import CouncilWorker

        from .ai.council import engine_support as support

        monitor = support.InFlight()
        backend = support.InstrumentedBackend(
            support.scripted(
                {
                    "proposer": support.proposer_answer("A:1"),
                    "critic": support.critic_answer(),
                    "meta": support.meta_answer("A:1"),
                }
            ),
            delay=delay,
            monitor=monitor,
            usage={"prompt_tokens": 100, "completion_tokens": 10},
        )
        worker = CouncilWorker(
            store,
            support.endpoint_config(
                fake1={"account": "acct", "max_concurrency": concurrency}
            ),
            {"fake1": backend},
            user="test",
        )
        job_id = worker.submit(
            support.make_query(),
            [support.make_task(f"item-{n}", text=f"text {n}") for n in range(self.ITEMS)],
            support.make_roster(),
            label="ceiling halt",
        )
        return worker, job_id, monitor, support

    def test_a_halted_endpoints_calls_are_never_claimed(self, tmp_path: Path) -> None:
        """The claim filter, on its own: halt first, then try to work."""
        store = self._store(tmp_path)
        try:
            worker, job_id, monitor, support = self._harness(store)
            store.set_halt(
                "fake1", "budget_exhausted", "spent 1200 of a 1000-token budget"
            )
            summary = support.run(worker.work(job_id=job_id, until_idle=True))
            assert monitor.total == 0, (
                f"the halt did not stop the worker: it made {monitor.total} call(s)"
            )
            assert [b["kind"] for b in summary["blockers"]] == ["halted"]
            assert store.job_status(job_id)["status"] != "done"
        finally:
            store.close()

    def test_a_halt_written_MID_RUN_stops_the_worker_and_is_reported(
        self, tmp_path: Path
    ) -> None:
        """The assertion the PRD would not let be made by reading.

        The halt is written from another thread while the worker is in the
        middle of a sweep -- exactly what the supervisor's poll does -- and
        what is asserted is that the worker STOPS SPENDING and comes out of
        its loop reporting the halt, not merely that the halt row exists.
        """
        store = self._store(tmp_path)
        try:
            worker, job_id, monitor, support = self._harness(
                store, delay=0.02, concurrency=2
            )
            halted_at: Dict[str, int] = {}

            def _supervise() -> None:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if monitor.total >= 4:
                        halted_at["calls"] = monitor.total
                        store.set_halt(
                            "fake1",
                            "budget_exhausted",
                            "spent 1200 of a 1000-token budget (overshoot 200)",
                        )
                        return
                    time.sleep(0.005)

            watcher = threading.Thread(target=_supervise, daemon=True)
            watcher.start()
            summary = support.run(worker.work(job_id=job_id, until_idle=True))
            watcher.join(5)

            assert halted_at, "the supervisor thread never saw the run spending"
            assert monitor.total < self.FULL_RUN_CALLS, (
                f"the worker ran to completion ({monitor.total} calls) despite "
                "the halt, so the halt did not stop it"
            )
            assert [b["kind"] for b in summary["blockers"]] == ["halted"]
            assert store.job_status(job_id)["percent_complete"] < 100.0
            state = store.get_endpoint_state("fake1")
            assert state["halt_kind"] == "budget_exhausted"
            assert "overshoot" in state["halt_message"]

            # And clearing it lets the SAME worker finish, which is what makes
            # a halt a stop rather than a kill: no call was lost, no retry
            # budget was spent, and nothing had to be re-submitted.
            before = monitor.total
            store.reset_halt("fake1")
            support.run(worker.work(job_id=job_id, until_idle=True))
            assert monitor.total > before
            assert store.job_status(job_id)["percent_complete"] == 100.0
        finally:
            store.close()


# ---------------------------------------------------------------------------
# The record, and the detach note
# ---------------------------------------------------------------------------


class TestRunRecord:
    """``RUN.json`` is the hand-off to stage 5 and the operator's log."""

    def test_the_consumed_approval_is_recorded_on_first_use(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        record = _run(run_dir, _FakeCouncil(), env)
        on_disk = json.loads((run_dir / RUN_FILENAME).read_text(encoding="utf-8"))
        assert on_disk[CONSUMED_KEY] == record[CONSUMED_KEY]
        assert len(on_disk[CONSUMED_KEY]) == 64

    def test_the_record_carries_the_ceiling_the_epoch_and_the_credentials(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        record = _run(run_dir, _FakeCouncil(), env)
        assert record["ceiling_tokens"] == CEILING
        assert record["budget_epoch"] == {"argo": {"argo": 1}, "mag": {"mag": 1, "mag2": 1}}
        assert {row["endpoint"] for row in record["credentials"]} == {
            "argo",
            "mag",
            "mag2",
        }
        assert record["endpoints_by_gateway"] == {"argo": ["argo"], "mag": ["mag", "mag2"]}

    def test_the_record_is_valid_json_on_disk_after_every_chunk(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """Stage 5 reads this file; it must never be seen half-written."""
        seen: List[int] = []

        def _peek(council: _FakeCouncil, job_id: str) -> None:
            path = run_dir / RUN_FILENAME
            if path.exists():
                seen.append(len(json.loads(path.read_text(encoding="utf-8"))))

        _run(run_dir, _FakeCouncil(on_run=_peek), env)
        assert len(seen) >= 3

    def test_the_summary_is_derived_from_the_chunk_rows(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        store = _FakeStore()
        record = _run(
            run_dir,
            _FakeCouncil(store, on_run=lambda c, j: store.add_tokens("argo", 700)),
            env,
        )
        summary = run_summary(record)
        assert summary["chunks"] == 3
        assert summary["chunks_settled"] == 3
        assert summary["items"] == 5
        assert summary["ok"] == 5
        assert summary["tokens_by_gateway"]["argo"] == 2100

    def test_the_record_carries_the_detach_note(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        record = _run(run_dir, _FakeCouncil(), env)
        assert "systemd-run --user --scope --collect" in record["detach"]
        assert "nohup" in record["detach"]

    def test_the_detach_note_names_what_does_not_work_and_why(self) -> None:
        for word in ("setsid", "nohup", "disown", "cgroup"):
            assert word in DETACH_NOTE
        assert "systemd-run --user --scope --collect" in DETACH_NOTE


class TestWhoDrains:
    """A foreground worker beside a daemon would double the claim rate."""

    def test_a_held_execution_lock_means_this_stage_only_watches(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        council = _FakeCouncil(holder={"kind": "daemon", "pid": 4242})
        # The daemon settles each job as soon as it appears, which is what a
        # draining daemon looks like from here.
        original_submit = council.submit

        def _submit(query: str, items: Any, **kwargs: Any) -> str:
            job_id = original_submit(query, items, **kwargs)
            for row in council.require_schema().items[job_id]:
                row["status"] = "ok"
            return job_id

        council.submit = _submit  # type: ignore[method-assign]
        record = _run(run_dir, council, env)
        assert council.runs == [], "stage 4 must not run a second worker"
        assert record["complete"] is True
        assert [row["drain"]["mode"] for row in record["chunks"]] == ["watch"] * 3
        assert record["chunks"][0]["drain"]["drained_by"]["kind"] == "daemon"

    def test_a_free_lock_means_this_stage_drains_the_chunk_itself(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        council = _FakeCouncil()
        record = _run(run_dir, council, env)
        assert council.runs == ["job-1", "job-2", "job-3"]
        assert [row["drain"]["mode"] for row in record["chunks"]] == ["foreground"] * 3

    def test_a_drain_that_raises_is_not_swallowed(
        self, run_dir: Path, env: Dict[str, str]
    ) -> None:
        """A worker crash is a bug, not a chunk outcome."""

        def _boom(council: _FakeCouncil, job_id: str) -> None:
            raise RuntimeError("the worker fell over")

        with pytest.raises(RuntimeError, match="fell over"):
            _run(run_dir, _FakeCouncil(on_run=_boom), env)
