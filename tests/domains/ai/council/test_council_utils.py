"""Tests for ``CouncilUtils`` -- the council's facade.

Everything here runs against a throwaway SQLite store in ``tmp_path`` with the
backends replaced by a ``FakeBackend`` (``build_backends`` is monkeypatched),
so a full submit -> run -> status -> results -> cancel -> rerun round trip
happens without a network call, a token, a CLI login or a GPU.  The ONE test
that drives real backends (``test_doctor_never_completes``) replaces them with
a recorder whose ``complete`` raises, which is how "doctor spends nothing" is
pinned rather than asserted in prose.

The query type is the shipped ``go_mf`` builtin, not an in-test one: the
facade's job is to make the real thing work, and ``go_mf``'s ``id_field`` is
``go_id`` rather than ``id``, so a facade that forgot the postprocess rename
would pass against a convenient test spec and fail against the product.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from kbutillib.domains.ai.council.backends.testing import FakeBackend
from kbutillib.domains.ai.council_utils import CouncilUtils, CouncilUtilsImpl

#: The closed menu every item here picks from.  Deliberately not in id order.
MENU = [
    {"id": "GO:0003824", "label": "catalytic activity", "rank": 1, "score": 0.9},
    {"id": "GO:0016740", "label": "transferase activity", "rank": 2, "score": 0.5},
]


def _config(tmp_path: Path) -> Dict[str, Any]:
    """A five-seat council on one OpenAI-compatible endpoint.

    The endpoint is a port nothing listens on: ``build_backends`` is patched
    in every test that submits, so the URL is only ever parsed.  It has to be
    a REAL endpoint kind because ``CouncilConfig`` cannot express a ``fake``
    one -- its ``kind`` is a Literal over the four shipped kinds.
    """
    return {
        "endpoints": {
            "local": {
                "kind": "openai",
                "base_url": "http://127.0.0.1:9/v1",
                "max_concurrency": 4,
            }
        },
        "models": {
            "p1": {"endpoint": "local", "canonical": "test/p1"},
            "p2": {"endpoint": "local", "canonical": "test/p2"},
            "p3": {"endpoint": "local", "canonical": "test/p3"},
            "cr": {"endpoint": "local", "canonical": "test/cr"},
            "mt": {"endpoint": "local", "canonical": "test/mt"},
        },
        "profiles": {
            "test": {"proposers": ["p1", "p2", "p3"], "critic": "cr", "meta": "mt"},
        },
        "default_profile": "test",
        "store": f"sqlite:///{tmp_path}/council.sqlite",
    }


def proposer_reply(*go_ids: str) -> Dict[str, Any]:
    """A schema-valid ``go_mf`` proposer answer (``go_id``, not ``id``)."""
    return {
        "picked": [
            {"go_id": go_id, "confidence": "high", "why": f"because {go_id}"}
            for go_id in go_ids
        ],
        "overall_confidence": "high",
        "no_functional_content": False,
        "notes": "",
    }


def critic_reply(verdict: str = "consistent") -> Dict[str, Any]:
    """A schema-valid ``go_mf`` critic answer."""
    return {
        "verdict": verdict,
        "reason": "the table matches the annotation",
        "overpicked": [],
        "missed": [],
        "contentless_input": False,
    }


def meta_reply(*go_ids: str) -> Dict[str, Any]:
    """A schema-valid ``go_mf`` meta answer (``final_go_ids``)."""
    return {
        "final_go_ids": list(go_ids),
        "final_confidence": "medium",
        "no_functional_content": False,
        "reasoning": "adjudicated",
    }


def unanimous_script(req: Any) -> Dict[str, Any]:
    """Every proposer picks ``GO:0003824``; the critic approves; meta agrees."""
    if req.role == "proposer":
        return proposer_reply("GO:0003824")
    if req.role == "critic":
        return critic_reply()
    return meta_reply("GO:0003824")


@pytest.fixture()
def util(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CouncilUtils:
    """An initialised facade whose backends are one shared ``FakeBackend``."""
    council = CouncilUtils(
        council_config=_config(tmp_path),
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )
    council.db_init()
    backend = FakeBackend(unanimous_script)
    council.fake_backend = backend  # type: ignore[attr-defined]
    monkeypatch.setattr(
        CouncilUtils,
        "build_backends",
        lambda self, roster, allow_unverified=None: {"local": backend},
    )
    yield council
    council.close()


def _items() -> List[Dict[str, Any]]:
    return [
        {
            "item_id": "item-1",
            "evidence": {"text": "alpha synthase", "source": "test"},
            "menu": MENU,
        },
        {
            "item_id": "item-2",
            "evidence": {"text": "beta transferase", "source": "test"},
            "menu": MENU,
        },
    ]


# ---------------------------------------------------------------------------
# submit -> run -> status -> results
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """One batch, all the way from a dict to a verdict."""

    def test_submit_run_status_results(self, util: CouncilUtils) -> None:
        job_id = util.submit("go_mf", _items(), label="facade test")
        assert job_id

        queued = util.job_status(job_id)
        assert queued["status"] == "queued"
        assert queued["n_items"] == 2
        # Nothing has been spent: the proposer calls are pending rows.
        assert queued["pending_calls_by_role_endpoint"]["proposer"]["local"] == 6

        status = util.run(job_id)
        assert status["status"] == "done"
        assert status["blockers"] == []
        assert status["percent_complete"] == 100.0
        assert status["worker"]["claimed"] >= 6

        rows = util.results(job_id)
        assert [row["item_id"] for row in rows] == ["item-1", "item-2"]
        for row in rows:
            assert row["status"] == "ok"
            assert row["verdict"]["selected_ids"] == ["GO:0003824"]
            # go_mf's id_field is `go_id`; a facade that skipped postprocess
            # would have an empty vote table here.
            assert row["verdict"]["votes"]["GO:0003824"] == 3

    def test_results_as_dataframe(self, util: CouncilUtils) -> None:
        pd = pytest.importorskip("pandas")
        job_id = util.submit("go_mf", _items()[:1])
        util.run(job_id)
        frame = util.results(job_id, as_dataframe=True)
        assert isinstance(frame, pd.DataFrame)
        assert list(frame["item_id"]) == ["item-1"]

    def test_meta_runs_when_the_proposers_disagree(self, util: CouncilUtils) -> None:
        """A split vote goes to meta, and meta's answer is the verdict."""

        def split(req: Any) -> Dict[str, Any]:
            if req.role == "proposer":
                if req.seat.seat_name == "proposer_1":
                    return proposer_reply("GO:0016740")
                return proposer_reply("GO:0003824")
            if req.role == "critic":
                return critic_reply("ambiguous")
            return meta_reply("GO:0016740")

        util.fake_backend._script = split  # type: ignore[attr-defined]
        job_id = util.submit("go_mf", _items()[:1])
        util.run(job_id)
        verdict = util.results(job_id)[0]["verdict"]
        assert verdict["decided_by"] == "meta"
        assert verdict["selected_ids"] == ["GO:0016740"]
        assert verdict["meta_skipped"] is False

    def test_empty_menu_is_decided_without_a_call(self, util: CouncilUtils) -> None:
        """An item with no candidates settles at submit time, spending nothing."""
        before = len(util.fake_backend.calls)  # type: ignore[attr-defined]
        job_id = util.submit(
            "go_mf",
            [{"item_id": "empty", "evidence": {"text": "x", "source": "t"}, "menu": []}],
        )
        assert util.job_status(job_id)["status"] == "done"
        assert len(util.fake_backend.calls) == before  # type: ignore[attr-defined]
        verdict = util.results(job_id)[0]["verdict"]
        assert verdict["decided_by"] == "deterministic_empty"


class TestJobControl:
    """``jobs``, ``cancel``, ``delete`` and ``rerun``."""

    def test_jobs_lists_this_user_and_hides_tombstones(self, util: CouncilUtils) -> None:
        job_id = util.submit("go_mf", _items()[:1], label="mine")
        rows = util.jobs()
        assert [row["job_id"] for row in rows] == [job_id]
        assert rows[0]["submitted_by"] == util.user()
        assert util.jobs(status="running") == []

        util.cancel(job_id)
        util.delete(job_id)
        assert util.jobs() == []
        assert [row["job_id"] for row in util.jobs(include_deleted=True)] == [job_id]

    def test_cancel_supersedes_the_pending_calls(self, util: CouncilUtils) -> None:
        job_id = util.submit("go_mf", _items())
        status = util.cancel(job_id)
        assert status["status"] == "cancelled"
        assert status["pending_calls_by_role_endpoint"] == {}
        # Cancelling twice is not an error: it is terminal, not a transition.
        assert util.cancel(job_id)["status"] == "cancelled"

    def test_delete_refuses_a_live_job(self, util: CouncilUtils) -> None:
        job_id = util.submit("go_mf", _items()[:1])
        with pytest.raises(ValueError, match="cancel it first"):
            util.delete(job_id)

    def test_rerun_asks_the_same_questions_as_a_new_job(self, util: CouncilUtils) -> None:
        first = util.submit("go_mf", _items(), label="first")
        util.run(first)
        second = util.rerun(first)
        assert second != first

        job = util.store().get_job(second)
        assert job["options"]["rerun_of"] == first
        assert job["query_name"] == "go_mf"
        original = {row["item_id"]: row for row in util.store().get_items(first)}
        for row in util.store().get_items(second):
            assert row["evidence"] == original[row["item_id"]]["evidence"]
            assert row["data_hash"] == original[row["item_id"]]["data_hash"]

        util.run(second)
        assert util.job_status(second)["status"] == "done"

    def test_rerun_can_reseat_the_council(self, util: CouncilUtils) -> None:
        first = util.submit("go_mf", _items()[:1])
        second = util.rerun(first, proposers=["p1", "p2"])
        roster = util.store().get_job(second)["roster"]
        assert [seat["seat_name"] for seat in roster["proposers"]] == [
            "proposer_1",
            "proposer_2",
        ]


class TestStoreDiscipline:
    """Nothing creates a schema implicitly."""

    def test_require_schema_refuses_an_unstamped_store(self, tmp_path: Path) -> None:
        council = CouncilUtils(
            council_config=_config(tmp_path),
            store_url=f"sqlite:///{tmp_path}/fresh.sqlite",
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        try:
            assert council.schema_version() == 0
            with pytest.raises(Exception, match="kbu council db init"):
                council.require_schema()
            with pytest.raises(Exception, match="kbu council db init"):
                council.jobs()
            # Still nothing: the refusal did not create anything on the way out.
            assert council.schema_version() == 0
        finally:
            council.close()

    def test_db_init_is_idempotent_and_seeds_the_builtins(self, tmp_path: Path) -> None:
        council = CouncilUtils(
            council_config=_config(tmp_path),
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        try:
            first = council.db_init()
            assert first["schema_version"] == 1
            assert sorted(first["query_types"]) == ["go_mf@1", "reaction_pickup@1"]
            assert council.db_init()["query_types"] == first["query_types"]
            check = council.db_check()
            assert check["ok"] is True
            assert check["schema_version"] == check["expected_schema_version"]
        finally:
            council.close()

    def test_db_migrate_stamps_a_fresh_store(self, tmp_path: Path) -> None:
        council = CouncilUtils(
            council_config=_config(tmp_path),
            store_url=f"sqlite:///{tmp_path}/m.sqlite",
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        try:
            report = council.db_migrate()
            assert report["schema_version_before"] == 0
            assert report["schema_version"] == 1
            assert report["migrated"] is True
            assert council.db_migrate()["migrated"] is False
        finally:
            council.close()


class TestQueriesAndCalls:
    """The query registry and the call search, through the facade."""

    def test_queries_lists_both_builtins(self, util: CouncilUtils) -> None:
        names = {(row["name"], row["version"]) for row in util.queries()}
        assert ("go_mf", "1") in names
        assert ("reaction_pickup", "1") in names

    def test_register_and_retire_a_query_type(self, util: CouncilUtils, tmp_path: Path) -> None:
        source = (
            Path(__file__).resolve().parents[4]
            / "src/kbutillib/domains/ai/council/queries/examples/custom_example.yaml"
        )
        spec = util.register_query(source)
        assert any(
            row["name"] == spec.name and row["version"] == spec.version
            for row in util.queries()
        )
        assert util.retire_query(spec.name, spec.version) is True
        assert not any(row["name"] == spec.name for row in util.queries())
        assert any(
            row["name"] == spec.name for row in util.queries(include_retired=True)
        )

    def test_search_calls_hashes_the_entity_for_you(self, util: CouncilUtils) -> None:
        from kbutillib.domains.ai.council.types import entity_hash

        job_id = util.submit("go_mf", _items())
        util.run(job_id)
        by_text = util.search_calls(entity="alpha synthase")
        assert by_text
        assert {row["entity_hash"] for row in by_text} == {entity_hash("alpha synthase")}
        assert {row["query_name"] for row in by_text} == {"go_mf"}
        assert util.search_calls(entity="nothing annotated this") == []


class TestEndpointState:
    """Budget and halt state for the configured endpoints."""

    def test_status_reports_every_configured_endpoint(self, util: CouncilUtils) -> None:
        rows = util.endpoint_status()
        assert [row["endpoint"] for row in rows] == ["local"]
        assert rows[0]["halted"] is False
        assert rows[0]["kind"] == "openai"

    def test_reset_halt_clears_a_halt_without_touching_the_counters(
        self, util: CouncilUtils
    ) -> None:
        store = util.store()
        store.add_tokens(util.user(), "local", 100)
        store.set_halt(util.user(), "local", "quota_exhausted", "out of quota")
        assert util.endpoint_status()[0]["halted"] is True

        state = util.reset_halt("local")
        assert not state["halt_kind"]
        assert state["lifetime_tokens"] == 100

        epoch = util.reset_budget("local")
        assert epoch["epoch_tokens"] == 0
        assert epoch["lifetime_tokens"] == 100


# ---------------------------------------------------------------------------
# evaluate, and the sync wrapper
# ---------------------------------------------------------------------------


class TestEvaluate:
    """``evaluate`` is a one-item job, not a second code path."""

    def test_evaluate_returns_the_verdict(self, util: CouncilUtils) -> None:
        verdict = util.evaluate(
            {
                "item_id": "one",
                "evidence": {"text": "alpha synthase", "source": "test"},
                "menu": MENU,
            }
        )
        assert verdict.item_id == "one"
        assert verdict.selected_ids == ["GO:0003824"]
        assert verdict.decided_by == "unanimous"

    def test_evaluate_refuses_more_than_one_item(self, util: CouncilUtils) -> None:
        with pytest.raises(ValueError, match="ONE item"):
            util.evaluate(_items())


class TestSyncWrapperInsideARunningLoop:
    """The sync form must work from a Jupyter cell, not raise in one."""

    def test_submit_and_run_from_inside_a_running_loop(self, util: CouncilUtils) -> None:
        async def notebook_cell() -> Dict[str, Any]:
            # A running loop on THIS thread: the sync wrapper must hand the
            # coroutine to a worker thread rather than try to nest a loop.
            assert asyncio.get_running_loop() is not None
            job_id = util.submit("go_mf", _items()[:1])
            return util.run(job_id)

        loop = asyncio.new_event_loop()
        try:
            status = loop.run_until_complete(notebook_cell())
        finally:
            loop.close()
        assert status["status"] == "done"

    def test_the_bare_worker_still_refuses_to_nest(self, util: CouncilUtils) -> None:
        """The facade adapts; the engine's own sync form still refuses.

        Pins the division of labour: ``CouncilWorker.submit`` raising inside a
        running loop is deliberate, and the facade's thread hand-off is what
        makes the notebook case work without changing that.
        """
        from kbutillib.domains.ai.council.engine.worker import _run_coroutine

        async def cell() -> None:
            async def inner() -> None:
                return None

            with pytest.raises(RuntimeError, match="already an event loop"):
                _run_coroutine(inner())

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cell())
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


class _RecordingBackend:
    """A backend that records the free probes and REFUSES to be completed.

    ``complete`` raising is the whole point: ``doctor`` must never spend, and a
    fake that merely returned a canned answer would let a regression through
    silently.
    """

    def __init__(self, kind: str = "openai", models: Any = None, version: Any = None,
                 billing: Any = None) -> None:
        self.kind = kind
        self.base_url = "http://127.0.0.1:9/v1"
        self.executable = "fake-cli"
        self._models = models if models is not None else []
        self._version = version
        self._billing = billing or {"state": "ready", "detail": "subscription", "reasons": []}
        self.probes: List[str] = []

    async def complete(self, req: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("doctor called complete(): it must never spend a token")

    async def models(self) -> List[Dict[str, Any]]:
        self.probes.append("models")
        return list(self._models)

    async def version(self) -> Any:
        self.probes.append("version")
        return self._version

    async def preflight(self) -> Dict[str, Any]:
        self.probes.append("preflight")
        return dict(self._billing)

    def effective_params(self, seat: Any) -> Dict[str, Any]:
        return {"model": seat.model}

    async def aclose(self) -> None:
        self.probes.append("aclose")


class TestDoctor:
    """What doctor checks, what it refuses to do, and what it reports."""

    @staticmethod
    def _council(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: Any) -> CouncilUtils:
        council = CouncilUtils(
            council_config=_config(tmp_path),
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        monkeypatch.setattr(
            CouncilUtils,
            "build_backends",
            lambda self, roster, allow_unverified=None: {"local": backend},
        )
        return council

    def test_doctor_never_calls_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _RecordingBackend(models=[{"id": "p1"}, {"id": "p2"}, {"id": "p3"}])
        council = self._council(tmp_path, monkeypatch, backend)
        report = council.doctor()
        assert "complete" not in backend.probes
        # One probe per ENDPOINT, not per seat: five seats, one /models GET.
        assert backend.probes.count("models") == 1
        assert report["summary"]["ready"] == 3
        assert report["summary"]["unavailable"] == 2
        assert report["exit_code"] == 1
        states = {seat["seat"]: seat["state"] for seat in report["seats"]}
        assert states["proposer_1"] == "ready"
        assert states["critic"] == "unavailable"

    def test_doctor_reports_an_unreachable_http_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _RecordingBackend(models=[])
        council = self._council(tmp_path, monkeypatch, backend)
        report = council.doctor()
        assert {seat["state"] for seat in report["seats"]} == {"unavailable"}
        assert report["exit_code"] == 1
        assert any("listed no models" in w for w in report["warnings"])

    def test_doctor_reports_dropped_billing_variable_names_not_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value-do-not-leak")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-another-secret")
        backend = _RecordingBackend(models=[{"id": "p1"}])
        council = self._council(tmp_path, monkeypatch, backend)
        report = council.doctor()
        assert "ANTHROPIC_API_KEY" in report["dropped_billing_vars"]
        assert "OPENAI_API_KEY" in report["dropped_billing_vars"]
        blob = json.dumps(report)
        assert "sk-secret-value-do-not-leak" not in blob
        assert "sk-another-secret" not in blob

    def test_doctor_reports_a_store_with_no_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _RecordingBackend(models=[{"id": "p1"}])
        council = self._council(tmp_path, monkeypatch, backend)
        report = council.doctor()
        assert report["store"]["reachable"] is True
        assert report["store"]["schema_version"] == 0
        assert any("kbu council db init" in w for w in report["warnings"])

    def test_an_unknown_seat_is_not_an_unavailable_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``verified: false`` is "nobody checked", which is exit code 2."""
        config = _config(tmp_path)
        for alias in config["models"]:
            config["models"][alias]["verified"] = False
        council = CouncilUtils(
            council_config=config,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        backend = _RecordingBackend(models=[{"id": "p1"}])
        monkeypatch.setattr(
            CouncilUtils,
            "build_backends",
            lambda self, roster, allow_unverified=None: {"local": backend},
        )
        report = council.doctor()
        assert {seat["state"] for seat in report["seats"]} == {"unknown"}
        assert report["exit_code"] == 2
        # Not a single probe: an unverified seat is answered from the config.
        assert backend.probes == ["models", "aclose"]

    def test_cli_readiness_is_version_plus_the_billing_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path)
        config["endpoints"]["cli"] = {"kind": "claude_cli", "executable": "fake-cli"}
        config["models"]["cr"] = {"endpoint": "cli", "served_name": "sonnet", "canonical": "test/cr"}
        config["models"]["mt"] = {"endpoint": "cli", "served_name": "opus", "canonical": "test/mt"}
        council = CouncilUtils(
            council_config=config,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        http = _RecordingBackend(models=[{"id": "p1"}, {"id": "p2"}, {"id": "p3"}])
        cli = _RecordingBackend(
            kind="claude_cli",
            version="9.9.9 (Fake)",
            billing={"state": "refused", "detail": "settings.json sets ANTHROPIC_API_KEY"},
        )
        monkeypatch.setattr(
            CouncilUtils,
            "build_backends",
            lambda self, roster, allow_unverified=None: {"local": http, "cli": cli},
        )
        report = council.doctor()
        assert cli.probes.count("version") == 1
        assert cli.probes.count("preflight") == 1
        assert "complete" not in cli.probes
        states = {seat["seat"]: seat["state"] for seat in report["seats"]}
        assert states["critic"] == "unavailable"
        assert states["meta"] == "unavailable"
        assert states["proposer_1"] == "ready"


class TestModelsAndServiceClient:
    """``models`` with no endpoint costs nothing, and the service client wires."""

    def test_models_with_no_endpoint_lists_the_configured_aliases(
        self, util: CouncilUtils
    ) -> None:
        rows = util.models()
        assert [row["alias"] for row in rows] == ["cr", "mt", "p1", "p2", "p3"]
        assert {row["kind"] for row in rows} == {"openai"}

    def test_service_client_is_constructed_without_a_request(
        self, util: CouncilUtils
    ) -> None:
        client = util.service_client(base_url="http://127.0.0.1:9", api_key="unused")
        try:
            assert client.base_url == "http://127.0.0.1:9"
        finally:
            client.close()


# ---------------------------------------------------------------------------
# The toolkit property and the Impl wrapper
# ---------------------------------------------------------------------------


class TestToolkitWiring:
    """``KBUtilLib().council`` is lazy, cached, and not an ArgoUtils."""

    def test_toolkit_property_is_lazy_and_cached(self) -> None:
        from kbutillib import KBUtilLib

        kbu = KBUtilLib()
        assert kbu._council is None
        council = kbu.council
        assert isinstance(council, CouncilUtilsImpl)
        assert kbu.council is council
        # Still no delegate: the property itself must not read a config file
        # or open a store.
        assert council._delegate is None

    def test_impl_delegates_on_first_real_use(self, tmp_path: Path) -> None:
        from kbutillib.core.shared_env_utils import SharedEnvUtils

        env = SharedEnvUtils(config_file=False, token_file=None, kbase_token_file=None)
        impl = CouncilUtilsImpl(
            env,
            council_config=_config(tmp_path),
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        assert impl.env is env
        assert impl.db_init()["schema_version"] == 1
        assert isinstance(impl._delegate, CouncilUtils)
        impl.close()

    def test_council_utils_does_not_inherit_argo_utils(self) -> None:
        """A paid constructor ping must not be behind ``kbu.council``."""
        names = {klass.__name__ for klass in CouncilUtils.__mro__}
        assert "ArgoUtils" not in names
        assert "SharedEnvUtils" in names
