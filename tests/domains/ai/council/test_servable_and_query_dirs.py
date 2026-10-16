"""Servable seats at submit, per-job permissions, and query directories (M2.3).

Two features, one property between them: **a council queue is per machine.**
There is no shared store and nothing coordinates across hosts, so a seat this
machine cannot reach is a job that would never finish, and a query type a user
keeps in a directory has to reach this machine's store by itself.

Everything here runs against a REAL temporary SQLite store and goes in through
the PUBLIC submit path (``CouncilUtils.submit`` and ``kbu council submit``).
The only substitute is the api type: a test-registered plug-in kind whose
``models``/``preflight`` shape the test chooses, which is exactly the thing
the offline doctor check reads.  Nothing patches the worker's per-call
permission decision -- that decision is what three of these tests are for.

Nothing here makes a network call, spends a token, needs a CLI login or needs
a GPU.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
import yaml
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.domains.ai import llm
from kbutillib.domains.ai.council.backends.testing import FakeBackend
from kbutillib.domains.ai.council.queries.errors import QuerySpecError
from kbutillib.domains.ai.council.servable import SeatUnavailable
from kbutillib.domains.ai.council.store.errors import QueryTypeExists
from kbutillib.domains.ai.council_utils import CouncilUtils

from . import engine_support as support

#: The kind the test api type is registered under.  ``fake`` is the name the
#: wrapper deliberately leaves UNREGISTERED ("a test registers them itself, so
#: no shipped configuration can ever name an endpoint that silently answers
#: from a script instead of from a model" -- ``llm``'s module docstring), and
#: it is one of the kinds a ``SeatSpec`` may name.  Registering it here is
#: therefore the sanctioned way to drive the real config -> roster -> wrapper
#: path with an api type whose SHAPE this module chooses.
KIND = "fake"

#: The model aliases every roster here seats.
SEATED = ("p1", "p2", "p3", "cr", "mt")


# ---------------------------------------------------------------------------
# The test api type: its SHAPE is the thing under test
# ---------------------------------------------------------------------------


def _script(req: Any) -> Dict[str, Any]:
    """Unanimous answers for the in-test query type (generic ``id`` field)."""
    if req.role == "proposer":
        return support.proposer_answer("A:1")
    if req.role == "critic":
        return support.critic_answer()
    return support.meta_answer("A:1")


class _Base:
    """A minimal api type: ``complete`` and ``effective_params``, nothing else.

    Those two are ``llm.protocol.REQUIRED_MEMBERS``; ``models`` and
    ``preflight`` are OPTIONAL, and which of them an api type has is precisely
    what decides whether this machine can tell if a seat is servable.  So the
    subclasses below add them one at a time rather than this class offering
    both and switching on a flag.

    ``complete`` delegates to the shipped :class:`FakeBackend`, so the one
    hard rule of the contract (never raise for a call failure) is the shipped
    implementation's and not this file's.
    """

    kind = KIND
    base_url = "test://servable"

    def __init__(self, endpoint: Optional[Dict[str, Any]] = None) -> None:
        self.endpoint = dict(endpoint or {})
        self._inner = FakeBackend(_script, kind=KIND)
        self.probes: List[str] = []

    @property
    def calls(self) -> List[Any]:
        return self._inner.calls

    async def complete(self, req: Any) -> Any:
        self.probes.append("complete")
        return await self._inner.complete(req)

    def effective_params(self, request: Any) -> Dict[str, Any]:
        return self._inner.effective_params(request)

    async def aclose(self) -> None:
        return None


class _Serving(_Base):
    """Lists the seated models: every seat on it is ``ready``."""

    async def models(self) -> List[Dict[str, Any]]:
        self.probes.append("models")
        return [{"id": name} for name in SEATED]


class _Unreachable(_Base):
    """Lists NOTHING, having been asked: every seat on it is ``unavailable``."""

    async def models(self) -> List[Dict[str, Any]]:
        self.probes.append("models")
        return []


class _CompleteOnly(_Base):
    """Neither probe, so readiness is ``unknown`` -- never an AttributeError."""


class _BillingUnknown(_Serving):
    """Reachable, but cannot say how it bills."""

    async def preflight(self) -> Dict[str, Any]:
        self.probes.append("preflight")
        return {"state": "unknown", "detail": "no billing file to read", "checked": []}


class _BillingRefused(_Serving):
    """Reachable, and it KNOWS it would bill the wrong way."""

    async def preflight(self) -> Dict[str, Any]:
        self.probes.append("preflight")
        return {"state": "refused", "detail": "an API key is set", "checked": []}


_MODES = {
    "ready": _Serving,
    "unreachable": _Unreachable,
    "complete_only": _CompleteOnly,
    "billing_unknown": _BillingUnknown,
    "billing_refused": _BillingRefused,
}

#: Every instance the factory built, by endpoint alias, so a test can assert
#: on what was probed and what was completed.
BUILT: Dict[str, List[_Base]] = {}


def _factory(endpoint: Dict[str, Any]) -> _Base:
    """Build the api type the endpoint's ``options.mode`` asks for."""
    mode = str(dict(endpoint.get("options") or {}).get("mode") or "ready")
    api = _MODES[mode](endpoint)
    BUILT.setdefault(str(endpoint.get("name") or ""), []).append(api)
    return api


@pytest.fixture(autouse=True)
def registered_api_type() -> Iterator[None]:
    """Register :data:`KIND` for one test and leave the registry as found.

    The wrapper's registry and client cache are process-global module state
    by design (a plug-in registers once per process), so a test that added to
    either would be visible to every later test in the session.
    """
    saved_registry = dict(llm._REGISTRY)
    saved_cache = dict(llm._CACHE)
    BUILT.clear()
    llm.register_api_type(KIND, _factory, replace=True)
    try:
        yield
    finally:
        _run(llm.aclose_all())
        llm._REGISTRY.clear()
        llm._REGISTRY.update(saved_registry)
        llm._CACHE.clear()
        llm._CACHE.update(saved_cache)
        BUILT.clear()


def _run(coro: Any) -> Any:
    """Run one coroutine on a loop this call owns (see ``support.run``)."""
    return support.run(coro)


# ---------------------------------------------------------------------------
# Configurations and items
# ---------------------------------------------------------------------------


def _endpoint(mode: str) -> Dict[str, Any]:
    """One endpoint of the test kind, in the mode *mode*.

    ``reuse_namespace`` is required of a kind that declares no ``base_url``:
    without one, two unrelated endpoints of the same kind would share an
    answer cache.  Here it also keeps the two endpoints of a mixed config
    (one working, one not) from reusing each other's answers.

    ``options`` is the per-endpoint bag a kind defines for itself, preserved
    verbatim and handed to its factory -- which is how :func:`_factory`
    learns which SHAPE of api type this endpoint wants.
    """
    return {
        "kind": KIND,
        "reuse_namespace": f"{KIND}:{mode}",
        "max_concurrency": 4,
        "options": {"mode": mode},
    }


def _config(
    tmp_path: Path,
    mode: str = "ready",
    critic_mode: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """A five-seat council on the test kind, storing in *tmp_path*.

    With *critic_mode* the critic and meta sit on a SECOND endpoint, which is
    how "one seat of the council is unservable" is expressed.
    """
    endpoints = {"main": _endpoint(mode)}
    models = {alias: {"endpoint": "main", "canonical": f"test/{alias}"} for alias in SEATED}
    if critic_mode is not None:
        endpoints["other"] = _endpoint(critic_mode)
        for alias in ("cr", "mt"):
            models[alias] = {"endpoint": "other", "canonical": f"test/{alias}"}
    config: Dict[str, Any] = {
        "endpoints": endpoints,
        "models": models,
        "profiles": {
            "test": {"proposers": ["p1", "p2", "p3"], "critic": "cr", "meta": "mt"}
        },
        "default_profile": "test",
        "store": f"sqlite:///{tmp_path}/council.sqlite",
    }
    config.update(extra)
    return config


def _util(tmp_path: Path, **kwargs: Any) -> CouncilUtils:
    """An initialised facade over a real temp store.  Nothing is patched."""
    council = CouncilUtils(
        council_config=_config(tmp_path, **kwargs),
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )
    council.db_init()
    return council


def _query(util: CouncilUtils, name: str = "servable_test") -> Any:
    """Register the in-test query type and return it."""
    return util.register_query(support.make_query(name, "1").to_spec())


def _items(*texts: str) -> List[Dict[str, Any]]:
    """One item per *text*, each with the same closed menu.

    Different text per item on purpose: two items with identical evidence hash
    to the same prompt and the second would be settled from the first's
    answer by reuse, which would mean a test thought it was measuring a call
    that never happened.
    """
    return [
        {
            "item_id": f"item-{index}",
            "evidence": {"text": text, "source": "test"},
            "menu": [entry.model_dump(mode="json") for entry in support.menu()],
        }
        for index, text in enumerate(texts, start=1)
    ]


# ---------------------------------------------------------------------------
# A. Servable seats at submit
# ---------------------------------------------------------------------------


class TestServableSeatsAtSubmit:
    """Submit runs the doctor's own offline check, once per endpoint."""

    def test_submit_refuses_an_unavailable_seat_and_names_it(
        self, tmp_path: Path
    ) -> None:
        """The refusal names the seat, its endpoint and the doctor's detail."""
        util = _util(tmp_path, mode="ready", critic_mode="unreachable")
        try:
            query = _query(util)
            with pytest.raises(SeatUnavailable) as caught:
                util.submit(query, _items("alpha"))
            message = str(caught.value)
            # The two seats on the unreachable endpoint, by name...
            assert "seat critic" in message
            assert "seat meta" in message
            assert "other" in message
            assert "listed no models" in message
            # ...and not the three on the endpoint that works.
            assert "proposer_1" not in message
            # The reason queues are per machine is part of the refusal.
            assert "PER MACHINE" in message
            assert {report["seat"] for report in caught.value.seats} == {"critic", "meta"}
            # Nothing was created, and nothing was spent.
            assert util.jobs() == []
            assert all(api.probes.count("complete") == 0 for api in BUILT["other"])
        finally:
            util.close()

    def test_one_probe_per_distinct_endpoint(self, tmp_path: Path) -> None:
        """Five seats on one endpoint cost ONE free listing, not five."""
        util = _util(tmp_path, mode="ready")
        try:
            util.submit(_query(util), _items("alpha"))
            probes = [probe for api in BUILT["main"] for probe in api.probes]
            assert probes.count("models") == 1
        finally:
            util.close()

    def test_allow_unavailable_creates_the_job_with_an_unservable_blocker(
        self, tmp_path: Path
    ) -> None:
        util = _util(tmp_path, mode="ready", critic_mode="unreachable")
        try:
            job_id = util.submit(
                _query(util), _items("alpha"), allow_unavailable=True
            )
            status = util.job_status(job_id)
            blockers = [b for b in status["blockers"] if b["kind"] == "unservable"]
            # Reported from the moment the job is queued, even though the
            # critic's own call rows do not exist yet.
            assert [b["endpoint"] for b in blockers] == ["other"]
            assert "listed no models" in blockers[0]["detail"]
            # The permission and the endpoint are recorded on the JOB.
            options = util.store().get_job(job_id)["options"]
            assert options["allow_unavailable"] is True
            assert "other" in options["unservable_endpoints"]
            assert "listed no models" in options["unservable_endpoints"]["other"]
        finally:
            util.close()

    def test_an_unservable_proposer_endpoint_blocks_the_job_it_queued(
        self, tmp_path: Path
    ) -> None:
        """With pending calls on it, the endpoint IS reported as a blocker."""
        util = _util(tmp_path, mode="unreachable")
        try:
            job_id = util.submit(
                _query(util), _items("alpha"), allow_unavailable=True
            )
            status = util.job_status(job_id)
            unservable = [b for b in status["blockers"] if b["kind"] == "unservable"]
            assert [b["endpoint"] for b in unservable] == ["main"]
            assert "wait" in unservable[0]["detail"]
        finally:
            util.close()

    def test_its_calls_wait_rather_than_spend(self, tmp_path: Path) -> None:
        """``allow_unavailable`` only let the job be created; the calls park."""
        util = _util(tmp_path, mode="unreachable")
        try:
            job_id = util.submit(
                _query(util), _items("alpha"), allow_unavailable=True
            )
            status = util.run(job_id)
            # Not done, and not failed: the calls are back in the queue with
            # their reason on them (``running`` is what claiming one and
            # giving it back leaves the job in).
            assert status["status"] not in ("done", "failed")
            assert [b["kind"] for b in status["blockers"] if b["kind"] == "unservable"] == [
                "unservable"
            ]
            assert [row["status"] for row in util.results(job_id, include_pending=True)] == [
                "pending"
            ]
            calls = util.store().get_calls(job_id, "item-1")
            assert {row["status"] for row in calls} == {"pending"}
            assert {row["error"]["kind"] for row in calls} == {"model_unavailable"}
            assert all(api.probes.count("complete") == 0 for api in BUILT["main"])
        finally:
            util.close()

    def test_an_unknown_endpoint_follows_allow_unverified(self, tmp_path: Path) -> None:
        """An api type with no probes at all is ``unknown``, not ``unavailable``."""
        util = _util(tmp_path, mode="complete_only")
        try:
            query = _query(util)
            with pytest.raises(SeatUnavailable) as caught:
                util.submit(query, _items("alpha"))
            message = str(caught.value)
            assert "allow_unverified=True" in message
            assert "neither models() nor preflight()" in message
            assert "allow_unavailable" not in message
            assert util.jobs() == []
        finally:
            util.close()

    def test_a_complete_only_api_type_runs_end_to_end_with_allow_unverified(
        self, tmp_path: Path
    ) -> None:
        """The whole public round trip over an api type that can only answer."""
        util = _util(tmp_path, mode="complete_only")
        try:
            job_id = util.submit(
                _query(util), _items("alpha"), allow_unverified=True
            )
            assert util.store().get_job(job_id)["options"]["allow_unverified"] is True
            status = util.run(job_id)
            assert status["status"] == "done"
            assert status["blockers"] == []
            rows = util.results(job_id)
            assert [row["status"] for row in rows] == ["ok"]
            assert rows[0]["verdict"]["selected_ids"] == ["A:1"]
        finally:
            util.close()

    def test_the_submit_check_never_completes(self, tmp_path: Path) -> None:
        """The refusal is reached without one model call being made."""
        util = _util(tmp_path, mode="unreachable")
        try:
            with pytest.raises(SeatUnavailable):
                util.submit(_query(util), _items("alpha"))
            assert all(api.probes.count("complete") == 0 for api in BUILT["main"])
        finally:
            util.close()


# ---------------------------------------------------------------------------
# A (CLI). The same refusal through ``kbu council submit``
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, config: Dict[str, Any]) -> Path:
    path = tmp_path / "council.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _items_file(tmp_path: Path) -> Path:
    import json

    path = tmp_path / "items.jsonl"
    path.write_text(
        "\n".join(json.dumps(item) for item in _items("alpha")), encoding="utf-8"
    )
    return path


class TestServableSeatsThroughTheCli:
    """``kbu council submit`` refuses the same job, and says why."""

    @staticmethod
    def _argv(tmp_path: Path, **config_kwargs: Any) -> List[str]:
        config_path = _write_config(tmp_path, _config(tmp_path, **config_kwargs))
        store = f"sqlite:///{tmp_path}/council.sqlite"
        argv = ["council", "--config", str(config_path), "--store", store]
        result = CliRunner().invoke(main, argv + ["db", "init"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        # The in-test query type has to be in the store before submit.
        util = CouncilUtils(
            council_config=_config(tmp_path, **config_kwargs),
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        try:
            _query(util)
        finally:
            util.close()
        return argv

    def test_submit_exits_nonzero_naming_the_seat(self, tmp_path: Path) -> None:
        argv = self._argv(tmp_path, mode="ready", critic_mode="unreachable")
        runner = CliRunner()
        result = runner.invoke(
            main,
            argv
            + [
                "submit",
                "--query",
                "servable_test",
                "--input",
                str(_items_file(tmp_path)),
            ],
        )
        assert result.exit_code != 0
        assert "SeatUnavailable" in result.output
        assert "seat critic" in result.output
        assert "allow_unavailable=True" in result.output
        # Refused means refused: no job row was created.
        assert "job_id" not in result.output

    def test_allow_unavailable_submits_and_warns(self, tmp_path: Path) -> None:
        import json

        argv = self._argv(tmp_path, mode="unreachable")
        result = CliRunner().invoke(
            main,
            argv
            + [
                "submit",
                "--query",
                "servable_test",
                "--input",
                str(_items_file(tmp_path)),
                "--allow-unavailable",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(
            [line for line in result.output.splitlines() if line.startswith("{")][0]
        )
        assert envelope["verb"] == "submit"
        assert envelope["data"]["job_id"]
        assert [b["kind"] for b in envelope["data"]["blockers"]] == ["unservable"]
        assert any("unservable" in warning for warning in envelope["warnings"])


# ---------------------------------------------------------------------------
# Round 3. Permissions belong to the JOB, not to a worker or a client
# ---------------------------------------------------------------------------


class TestPerJobPermissions:
    """One job's permission never admits another job's calls."""

    @staticmethod
    def _work_the_whole_queue(util: CouncilUtils) -> Dict[str, Any]:
        """Run ONE worker over every job in the store, permissively built.

        This is the shape the per-job rule exists for: a single worker, a
        single set of clients, two jobs with different permissions.  The
        worker's own ``allow_unverified`` is deliberately TRUE and the clients
        are built permissive, so the only thing that can refuse a call
        correctly is the worker reading that call's own job.
        """
        roster = util.roster()
        backends = util.build_backends(roster, allow_unverified=True)
        worker = util._worker(util.store(), backends, True)
        try:
            return _run(worker.work(until_idle=True))
        finally:
            _run(util._aclose_backends(backends))

    def test_one_jobs_permission_does_not_admit_anothers_unknown_calls(
        self, tmp_path: Path
    ) -> None:
        util = _util(tmp_path, mode="billing_unknown")
        try:
            query = _query(util)
            permitted = util.submit(
                query, _items("alpha"), label="permitted", allow_unverified=True
            )
            refused = util.submit(query, _items("beta"), label="refused")
            assert util.store().get_job(refused)["options"]["allow_unverified"] is False

            self._work_the_whole_queue(util)

            assert [row["status"] for row in util.results(permitted)] == ["ok"]
            refused_rows = util.results(refused)
            assert [row["status"] for row in refused_rows] == ["error"]
            errors = {
                row["error"]["kind"]
                for row in util.store().get_calls(refused, "item-1")
                if row["error"]
            }
            assert errors == {"billing_guard"}
            message = [
                row["error"]["message"]
                for row in util.store().get_calls(refused, "item-1")
                if row["error"]
            ][0]
            assert "allow_unverified" in message
            # And the refusal did NOT halt the endpoint: the halt is the
            # store's, and halting it would have stopped the permitted job.
            assert util.store().get_endpoint_state("main")["halt_kind"] is None
        finally:
            util.close()

    def test_a_refused_billing_check_is_not_bypassed_by_allow_unverified(
        self, tmp_path: Path
    ) -> None:
        util = _util(tmp_path, mode="billing_refused")
        try:
            job_id = util.submit(
                _query(util), _items("alpha"), allow_unverified=True
            )
            status = util.run(job_id)
            assert status["status"] != "done"
            calls = util.store().get_calls(job_id, "item-1")
            assert {row["error"]["kind"] for row in calls if row["error"]} == {
                "billing_guard"
            }
            assert "refused the billing preflight" in [
                row["error"]["message"] for row in calls if row["error"]
            ][0]
            # A refusal IS a fact about the endpoint, so this one halts it.
            assert util.store().get_endpoint_state("main")["halt_kind"] == "billing_guard"
            assert any(b["kind"] == "halted" for b in status["blockers"])
            # Nothing was spent.
            assert all(api.probes.count("complete") == 0 for api in BUILT["main"])
        finally:
            util.close()

    def test_the_permissions_are_persisted_in_the_jobs_options(
        self, tmp_path: Path
    ) -> None:
        util = _util(tmp_path, mode="ready")
        try:
            query = _query(util)
            plain = util.submit(query, _items("alpha"))
            both = util.submit(
                query,
                _items("beta"),
                allow_unverified=True,
                allow_unavailable=True,
            )
            assert util.store().get_job(plain)["options"] == {
                "allow_unverified": False,
                "allow_unavailable": False,
            }
            assert util.store().get_job(both)["options"] == {
                "allow_unverified": True,
                "allow_unavailable": True,
            }
        finally:
            util.close()


# ---------------------------------------------------------------------------
# B. Query directories
# ---------------------------------------------------------------------------


def _spec_mapping(name: str = "dir_query", version: str = "1", **overrides: Any) -> Dict[str, Any]:
    """A valid, minimal query-type spec as a plain mapping."""
    mapping = support.make_query(name, version).to_spec()
    mapping.update(overrides)
    return mapping


def _write_spec(directory: Path, filename: str, **kwargs: Any) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(yaml.safe_dump(_spec_mapping(**kwargs), sort_keys=True), encoding="utf-8")
    return path


def _snapshot(directory: Path) -> Dict[str, Tuple[int, int, bytes]]:
    """Every entry in *directory* with its mtime, size and bytes.

    Contents AND mtimes, because "never written" has to exclude a rewrite
    that happened to produce the same bytes, and the whole listing, because it
    has to exclude a sidecar file appearing beside the specs.
    """
    return {
        path.name: (path.stat().st_mtime_ns, path.stat().st_size, path.read_bytes())
        for path in sorted(directory.iterdir())
    }


def _dir_util(tmp_path: Path, *dirs: Any, **kwargs: Any) -> CouncilUtils:
    """A facade whose config names *dirs* as ``query_dirs``."""
    config = _config(tmp_path, **kwargs)
    config["query_dirs"] = [str(entry) for entry in dirs]
    council = CouncilUtils(
        council_config=config,
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )
    return council


class TestQueryDirectories:
    """``ai_council.query_dirs``: loaded on every open, never written to."""

    def test_a_yaml_in_a_query_dir_is_registered_on_the_first_open(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "queries"
        path = _write_spec(directory, "dir_query.yaml")
        util = _dir_util(tmp_path, directory)
        try:
            util.db_init()
            assert util.query("dir_query@1").name == "dir_query"
            rows = {row["name"]: row for row in util.queries()}
            assert rows["dir_query"]["source"] == "directory"
            assert rows["dir_query"]["source_file"] == str(path)
            # A builtin is still a builtin, and carries no file.
            assert rows["go_mf"]["source"] == "builtin"
            assert rows["go_mf"]["source_file"] == ""
        finally:
            util.close()

    def test_only_yaml_files_directly_in_the_directory_are_read(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "queries"
        _write_spec(directory, "a.yaml", name="dir_a")
        _write_spec(directory, "b.yml", name="dir_b")
        (directory / "notes.txt").write_text("not a spec", encoding="utf-8")
        _write_spec(directory / "nested", "c.yaml", name="dir_nested")
        util = _dir_util(tmp_path, directory)
        try:
            util.db_init()
            names = {row["name"] for row in util.queries()}
            assert {"dir_a", "dir_b"} <= names
            assert "dir_nested" not in names
        finally:
            util.close()

    def test_a_second_open_is_a_no_op(self, tmp_path: Path) -> None:
        directory = tmp_path / "queries"
        _write_spec(directory, "dir_query.yaml")
        first = _dir_util(tmp_path, directory)
        try:
            first.db_init()
            created = first.store().get_query_type("dir_query", "1")["created_at"]
        finally:
            first.close()
        before = _snapshot(directory)

        second = _dir_util(tmp_path, directory)
        try:
            # Opening the store is what loads them: no db_init this time.
            row = second.store().get_query_type("dir_query", "1")
            assert row["created_at"] == created
            assert second.query_dir_sources == {
                "dir_query@1": str(directory / "dir_query.yaml")
            }
        finally:
            second.close()
        assert _snapshot(directory) == before

    def test_a_changed_file_at_the_same_version_is_refused_naming_the_file(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "queries"
        path = _write_spec(directory, "dir_query.yaml")
        first = _dir_util(tmp_path, directory)
        try:
            first.db_init()
        finally:
            first.close()

        # Same name@version, different content: an edit without a new version.
        path.write_text(
            yaml.safe_dump(
                _spec_mapping(description="edited in place"), sort_keys=True
            ),
            encoding="utf-8",
        )
        before = _snapshot(directory)

        second = _dir_util(tmp_path, directory)
        try:
            with pytest.raises(QueryTypeExists) as caught:
                second.store()
            assert str(path) in str(caught.value)
            assert "immutable" in str(caught.value)
        finally:
            second.close()
        # The refusal wrote nothing into the directory either.
        assert _snapshot(directory) == before

    def test_an_identical_file_in_two_directories_is_not_a_conflict(
        self, tmp_path: Path
    ) -> None:
        """The same spec shared across machines must not trip immutability."""
        one = tmp_path / "one"
        two = tmp_path / "two"
        _write_spec(one, "dir_query.yaml")
        _write_spec(two, "dir_query.yaml")
        util = _dir_util(tmp_path, one, two)
        try:
            util.db_init()
            assert util.query("dir_query@1").name == "dir_query"
        finally:
            util.close()

    def test_an_invalid_file_is_an_error_naming_the_file(self, tmp_path: Path) -> None:
        directory = tmp_path / "queries"
        directory.mkdir()
        bad = directory / "broken.yaml"
        bad.write_text(
            yaml.safe_dump({"name": "broken", "version": "1"}), encoding="utf-8"
        )
        util = _dir_util(tmp_path, directory)
        try:
            with pytest.raises(QuerySpecError) as caught:
                util.db_init()
            assert str(bad) in str(caught.value)
        finally:
            util.close()

    def test_a_missing_directory_only_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        missing = tmp_path / "not-there"
        present = tmp_path / "queries"
        _write_spec(present, "dir_query.yaml")
        util = _dir_util(tmp_path, missing, present)
        try:
            with caplog.at_level("WARNING"):
                util.db_init()
            assert any(
                "does not exist" in record.getMessage() for record in caplog.records
            )
            # ...and the directory that IS there was still loaded.
            assert util.query("dir_query@1").name == "dir_query"
            assert not missing.exists()
        finally:
            util.close()

    def test_two_opens_and_a_refusal_write_nothing_into_the_directory(
        self, tmp_path: Path
    ) -> None:
        """Contents and mtimes unchanged -- these paths may be in Dropbox."""
        directory = tmp_path / "queries"
        _write_spec(directory, "dir_query.yaml")
        before = _snapshot(directory)

        for _ in range(2):
            util = _dir_util(tmp_path, directory)
            try:
                util.db_init()
                util.queries()
            finally:
                util.close()
        assert _snapshot(directory) == before
        assert sorted(p.name for p in directory.iterdir()) == ["dir_query.yaml"]

        # A refused changed-file open writes nothing either.  Rewriting the
        # spec is the TEST changing it, so the baseline moves with it.
        path = directory / "dir_query.yaml"
        path.write_text(
            yaml.safe_dump(_spec_mapping(description="edited"), sort_keys=True),
            encoding="utf-8",
        )
        after_edit = _snapshot(directory)
        util = _dir_util(tmp_path, directory)
        try:
            with pytest.raises(QueryTypeExists):
                util.store()
        finally:
            util.close()
        assert _snapshot(directory) == after_edit

    def test_db_init_reports_the_directories_it_loaded(self, tmp_path: Path) -> None:
        directory = tmp_path / "queries"
        _write_spec(directory, "dir_query.yaml")
        util = _dir_util(tmp_path, directory)
        try:
            report = util.db_init()
            assert report["query_dirs"] == [str(directory)]
            assert report["query_dir_types"] == ["dir_query@1"]
        finally:
            util.close()

    def test_a_tilde_and_a_variable_are_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = tmp_path / "queries"
        _write_spec(directory, "dir_query.yaml")
        monkeypatch.setenv("KBU_TEST_QUERY_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        util = _dir_util(tmp_path, "${KBU_TEST_QUERY_DIR}/queries")
        try:
            assert util.config().query_dirs == [str(directory)]
            util.db_init()
            assert util.query("dir_query@1").version == "1"
        finally:
            util.close()

        home_util = _dir_util(tmp_path, "~/queries")
        try:
            assert home_util.config().query_dirs == [str(directory)]
        finally:
            home_util.close()

    def test_query_list_shows_the_file_a_spec_came_from(self, tmp_path: Path) -> None:
        import json

        directory = tmp_path / "queries"
        path = _write_spec(directory, "dir_query.yaml")
        config = _config(tmp_path)
        config["query_dirs"] = [str(directory)]
        config_path = _write_config(tmp_path, config)
        argv = [
            "council",
            "--config",
            str(config_path),
            "--store",
            f"sqlite:///{tmp_path}/council.sqlite",
        ]
        runner = CliRunner()
        assert runner.invoke(main, argv + ["db", "init"], catch_exceptions=False).exit_code == 0

        result = runner.invoke(main, argv + ["query", "list", "--json"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(
            [line for line in result.output.splitlines() if line.startswith("{")][0]
        )
        rows = {row["name"]: row for row in envelope["data"]["query_types"]}
        assert rows["dir_query"]["source"] == "directory"
        assert rows["dir_query"]["source_file"] == str(path)

        human = runner.invoke(main, argv + ["query", "list"])
        assert human.exit_code == 0, human.output
        assert "source_file" in human.output
        assert str(path) in human.output
