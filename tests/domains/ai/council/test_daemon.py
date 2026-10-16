"""The council daemon (M2.4): one long-running worker per machine store.

What is REAL here, because the properties under test are not properties of a
mock:

* **The execution lock is the shipped one**, taken against a real file, and
  the competing holder is a real ``spawn``ed interpreter.  A lock that only
  excluded objects inside one Python process would be no lock at all, so
  nothing about exclusion is simulated.
* **The daemon's loop runs in a real detached or spawned process** for every
  test about signals, draining and deadlines, and it is really sent a real
  ``SIGTERM``.  The ``interrupted`` attempt and the unchanged retry count are
  then read back out of the SQLite file with ``sqlite3``, not from an object
  the test still holds.
* **The store is a real temporary SQLite file** and the worker is the shipped
  :class:`CouncilWorker`.  Nothing patches the queue, the claim, the
  permission gate or the attempt bookkeeping.

The ONE substitute is the api type: a test-registered plug-in ``kind`` whose
``models``/``preflight`` shape and whose answer delay this module chooses.
That is the sanctioned substitution (see ``llm``'s module docstring: ``fake``
is deliberately left unregistered so no shipped configuration can name it),
and it is what lets a council be driven end to end with no network, no token,
no CLI login and no GPU.

Every timing is injected through :class:`DaemonTimings`, so no test here needs
more than a few seconds of wall clock while the production defaults stay the
specified 30/60/10/120/130 seconds.  Every spawned child is killed in
teardown: a leaked daemon would hold the execution lock for the rest of the
session.

Nothing here makes a network call, spends a token, needs a CLI login or needs
a GPU.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
import yaml

from kbutillib.domains.ai import llm
from kbutillib.domains.ai.council.backends.testing import FakeBackend
from kbutillib.domains.ai.council.daemon import (
    DEFAULT_TIMINGS,
    LOG_FILENAME,
    CouncilDaemon,
    DaemonError,
    DaemonTimings,
)
from kbutillib.domains.ai.council.execlock import ExecutionLock, LockHeld
from kbutillib.domains.ai.council_utils import CouncilUtils

from . import engine_support as support

#: The kind the test api type is registered under.  See the module docstring.
KIND = "fake"

#: The model aliases every roster here seats.
SEATED = ("p1", "p2", "p3", "cr", "mt")

#: Timings short enough that a test is seconds rather than minutes, and still
#: ordered the way the production defaults are (stop_wait > drain, heartbeat >
#: idle poll is NOT required and is deliberately inverted here so a short test
#: still heartbeats more than once).
FAST = DaemonTimings(
    heartbeat_s=0.1,
    probe_s=0.3,
    idle_poll_s=0.2,
    drain_s=1.5,
    stop_wait_s=20.0,
    start_wait_s=30.0,
)


# ---------------------------------------------------------------------------
# The test api type: its readiness SHAPE and its answer DELAY are the knobs
# ---------------------------------------------------------------------------


def _role_of(req: Any) -> str:
    """The role this request is for, from either request shape.

    The daemon hands :class:`CouncilWorker` bare endpoint ALIASES (the normal
    M2 form), so every call goes through ``llm.acomplete`` and arrives as an
    :class:`LLMRequest` -- which deliberately carries no role, the wrapper
    being domain-neutral.  The worker puts ``"<item>|<role>|<seat>"`` in the
    trace id precisely so a provider's own logs can be read back, and that is
    what a real api type would have to use too.  A ``CompletionRequest`` (the
    injected-instance form, which ``CouncilUtils.run`` uses) carries ``role``
    directly.
    """
    role = getattr(req, "role", None)
    if role:
        return str(role)
    parts = str(getattr(req, "trace_id", "") or "").split("|")
    return parts[1] if len(parts) > 2 else "proposer"


def _script(req: Any) -> Dict[str, Any]:
    """Unanimous answers for the in-test query type (generic ``id`` field)."""
    role = _role_of(req)
    if role == "proposer":
        return support.proposer_answer("A:1")
    if role == "critic":
        return support.critic_answer()
    return support.meta_answer("A:1")


class _Api:
    """One api type whose behaviour is read from the endpoint's ``options``.

    One class rather than a hierarchy because the daemon's tests vary FOUR
    independent things -- whether the endpoint lists its models, whether a
    gate file has to exist first, how long an answer takes and whether the
    billing preflight can say how it bills -- and a subclass per combination
    would be sixteen classes for eight tests.

    ``complete`` delegates to the shipped :class:`FakeBackend`, so the one
    hard rule of the backend contract (never raise for a call failure) is the
    shipped implementation's and not this file's.

    Options:
        lists: serve the model listing at all (default True).  False is an
            endpoint that was asked and served nothing -- ``unavailable``.
        gate: a path that must EXIST before the listing is served.  This is
            how "a route that failed its first probe becomes eligible when it
            succeeds" is driven: a file, so it works across processes and
            across api type instances rather than relying on a counter inside
            one object.
        no_models: offer no ``models()`` member at all, so readiness is
            ``unknown`` -- there is nothing to ask.
        billing: the ``state`` a ``preflight()`` reports.  Absent means no
            ``preflight`` member, which the wrapper reads as ``ready``.
        delay: seconds to wait before answering.
        hang: never answer.  Cancelled by the daemon's stop deadline.
    """

    kind = KIND
    base_url = "test://daemon"

    def __init__(self, endpoint: Optional[Dict[str, Any]] = None) -> None:
        self.endpoint = dict(endpoint or {})
        self.options = dict(self.endpoint.get("options") or {})
        self._inner = FakeBackend(_script, kind=KIND)
        self.probes: List[str] = []

    @property
    def calls(self) -> List[Any]:
        return self._inner.calls

    async def complete(self, req: Any) -> Any:
        self.probes.append("complete")
        if self.options.get("hang"):
            await asyncio.sleep(3600)
        delay = float(self.options.get("delay") or 0)
        if delay:
            await asyncio.sleep(delay)
        return await self._inner.complete(req)

    def effective_params(self, request: Any) -> Dict[str, Any]:
        return self._inner.effective_params(request)

    async def aclose(self) -> None:
        return None

    async def _models(self) -> List[Dict[str, Any]]:
        self.probes.append("models")
        gate = self.options.get("gate")
        if gate and not Path(str(gate)).exists():
            return []
        if self.options.get("lists") is False:
            return []
        return [{"id": name} for name in SEATED]

    async def _preflight(self) -> Dict[str, Any]:
        self.probes.append("preflight")
        return {
            "state": str(self.options.get("billing")),
            "detail": "the test api type was told to answer this",
            "checked": [],
        }


def _factory(endpoint: Dict[str, Any]) -> _Api:
    """Build an api type with exactly the members its options ask for.

    ``models`` and ``preflight`` are OPTIONAL members of the api type
    contract, and WHICH of them an api type has is precisely what decides
    whether this machine can tell if a route is servable.  So they are
    attached per instance rather than defined on the class -- an api type that
    merely *returns* an empty listing has been asked and served nothing
    (``unavailable``), while one with no ``models`` at all could not be asked
    (``unknown``), and the two must not be spelled the same way.
    """
    options = dict(endpoint.get("options") or {})
    api = _Api(endpoint)
    if not options.get("no_models"):
        api.models = api._models  # type: ignore[method-assign]
    if options.get("billing"):
        api.preflight = api._preflight  # type: ignore[method-assign]
    BUILT.setdefault(str(endpoint.get("name") or ""), []).append(api)
    return api


#: Every instance the factory built, by endpoint alias.
BUILT: Dict[str, List[_Api]] = {}


def register_api_type() -> None:
    """Register :data:`KIND` in THIS process.  Also called by every child."""
    llm.register_api_type(KIND, _factory, replace=True)


@pytest.fixture(autouse=True)
def registered_api_type() -> Iterator[None]:
    """Register :data:`KIND` for one test and leave the registry as found.

    The wrapper's registry and client cache are process-global module state by
    design (a plug-in registers once per process), so a test that added to
    either would be visible to every later test in the session.
    """
    saved_registry = dict(llm._REGISTRY)
    saved_cache = dict(llm._CACHE)
    BUILT.clear()
    register_api_type()
    try:
        yield
    finally:
        support.run(llm.aclose_all())
        llm._REGISTRY.clear()
        llm._REGISTRY.update(saved_registry)
        llm._CACHE.clear()
        llm._CACHE.update(saved_cache)
        BUILT.clear()


@pytest.fixture(autouse=True)
def temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway ``HOME``, inherited by every child this module spawns.

    The daemon's directory defaults to ``~/.kbutillib/council`` and its lock,
    its log and its store all live there.  A test that touched the real one
    would fight the operator's own daemon for the lock on the machine it runs
    on -- and every child here inherits ``os.environ``, so setting it once
    here covers the subprocesses too.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


# ---------------------------------------------------------------------------
# Configurations, stores and jobs
# ---------------------------------------------------------------------------


def _endpoint(**options: Any) -> Dict[str, Any]:
    """One endpoint of the test kind, in the mode *options* describes.

    ``reuse_namespace`` is required of a kind that declares no ``base_url``:
    without one, two unrelated endpoints of the same kind would share an
    answer cache.
    """
    name = options.pop("reuse", "main")
    entry: Dict[str, Any] = {
        "kind": KIND,
        "reuse_namespace": f"{KIND}:{name}",
        "max_concurrency": int(options.pop("max_concurrency", 4)),
        "options": {k: v for k, v in options.items() if k != "budget"},
    }
    if options.get("budget"):
        entry["max_total_tokens"] = int(options["budget"])
    return entry


def _config(tmp_path: Path, endpoints: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """A five-seat council on the test kind, storing under *tmp_path*."""
    endpoints = endpoints or {"main": _endpoint()}
    first = sorted(endpoints)[0]
    models = {alias: {"endpoint": first, "canonical": f"test/{alias}"} for alias in SEATED}
    if "other" in endpoints:
        for alias in ("cr", "mt"):
            models[alias] = {"endpoint": "other", "canonical": f"test/{alias}"}
    return {
        "endpoints": endpoints,
        "models": models,
        "profiles": {
            "test": {"proposers": ["p1", "p2", "p3"], "critic": "cr", "meta": "mt"}
        },
        "default_profile": "test",
        "store": f"sqlite:///{tmp_path}/council/council.sqlite",
    }


def _bare_config(tmp_path: Path) -> Dict[str, Any]:
    """A council with NO endpoints at all -- just a store.

    What the lock, ``start``, ``stop`` and ``status`` tests run against, and
    deliberately so: those properties are about one process excluding another
    and have nothing to do with models.  An endpoint-less configuration also
    means the detached child the REAL ``kbu council daemon start`` spawns
    needs no api type registered in it, so those tests exercise the shipped
    argv rather than an injected one.
    """
    return {"store": f"sqlite:///{tmp_path}/council/council.sqlite"}


def _written(tmp_path: Path, config: Dict[str, Any]) -> str:
    """*config* as a YAML FILE, and its path.

    Needed by every test that lets ``start`` spawn a real detached child: the
    child is a fresh ``kbu council`` process and the only way it can be given
    this configuration is ``--config <path>``.  An in-memory configuration is
    refused by ``_default_child_argv`` precisely so that a child can never
    silently load the packaged default -- which would probe ARGO.
    """
    path = tmp_path / "council.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return str(path)


def _council(config: Any, **kwargs: Any) -> CouncilUtils:
    return CouncilUtils(
        council_config=config,
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


def _util(config: Any, **kwargs: Any) -> CouncilUtils:
    """An initialised facade over a real temp store.  Nothing is patched."""
    council = _council(config, **kwargs)
    council.db_init()
    return council


def _items(*texts: str) -> List[Dict[str, Any]]:
    """One item per *text*, each with the same closed menu.

    Different text per item on purpose: two items with identical evidence hash
    to the same prompt and the second would be settled from the first's answer
    by reuse, so a test would think it was measuring a call that never
    happened.
    """
    return [
        {
            "item_id": f"item-{index}",
            "evidence": {"text": text, "source": "test"},
            "menu": [entry.model_dump(mode="json") for entry in support.menu()],
        }
        for index, text in enumerate(texts, start=1)
    ]


def _submit(util: CouncilUtils, *texts: str, name: str = "daemon_test", **kwargs: Any) -> str:
    query = util.register_query(support.make_query(name, "1").to_spec())
    return util.submit(query, _items(*texts), **kwargs)


def _store_path(config: Dict[str, Any]) -> Path:
    return Path(str(config["store"]).replace("sqlite:///", "", 1))


def _rows(config: Dict[str, Any], sql: str, *args: Any) -> List[Tuple[Any, ...]]:
    """Read the store back out of SQLite, not out of a live object.

    Every assertion about the ``interrupted`` attempt and the unchanged retry
    count goes through here: the daemon that wrote them was a different
    process, and reading its work back off disk is the only way to know it
    landed.
    """
    conn = sqlite3.connect(str(_store_path(config)))
    try:
        return list(conn.execute(sql, args))
    finally:
        conn.close()


def _calls(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = _rows(
        config,
        "SELECT call_id, role, status, attempt_count, endpoint, served_model, "
        "claim_token, error FROM calls WHERE live = 1",
    )
    return [
        {
            "call_id": r[0],
            "role": r[1],
            "status": r[2],
            "attempt_count": r[3],
            "endpoint": r[4],
            "served_model": r[5],
            "claim_token": r[6],
            "error": json.loads(r[7]) if r[7] else None,
        }
        for r in rows
    ]


def _attempts(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = _rows(
        config, "SELECT call_id, attempt_no, outcome_kind, usage FROM attempts"
    )
    return [
        {
            "call_id": r[0],
            "attempt_no": r[1],
            "outcome_kind": r[2],
            "usage": json.loads(r[3]) if r[3] else None,
        }
        for r in rows
    ]


def _until(predicate: Any, timeout: float = 15.0, interval: float = 0.02) -> Any:
    """Poll *predicate* until it is truthy, or fail the test naming the wait."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(
        f"condition never became true within {timeout:g}s (last value: {last!r})"
    )


# ---------------------------------------------------------------------------
# Real children
# ---------------------------------------------------------------------------


def hold_the_lock(directory: str, kind: str, taken: Any, release: Any, results: Any) -> None:
    """Take the execution lock in a BRAND-NEW interpreter and hold it.

    A module-level function because ``spawn`` pickles the target by qualified
    name and re-imports this module in the child; the child shares nothing
    with the parent but the directory path, which is the point.
    """
    try:
        lock = ExecutionLock(directory)
        with lock.acquire(kind) as record:
            taken.set()
            release.wait(timeout=60)
            results.put(("held", record["pid"]))
    except BaseException as exc:  # noqa: BLE001 - reported, never raised blind
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def serve_the_store(config: Dict[str, Any], timings: DaemonTimings, results: Any) -> None:
    """Run the daemon's foreground loop in a brand-new interpreter.

    The child registers the test api type itself -- it is a fresh process and
    inherits nothing from pytest -- and then calls exactly what
    ``kbu council daemon start --foreground`` calls.  So the ``SIGTERM``
    handling, the drain, the deadline and the cwd are the shipped ones under a
    real signal.
    """
    try:
        register_api_type()
        daemon = _council(config).daemon(timings=timings)
        results.put(("served", daemon.run_foreground()))
    except BaseException as exc:  # noqa: BLE001 - reported, see above
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def _stop_and_collect(child: Any, results: Any) -> Dict[str, Any]:
    """``SIGTERM`` the daemon child, wait for it, and return its own summary.

    Always called BEFORE any assertion about the store: the child reports an
    exception of its own on the queue rather than by crashing, so a test that
    asserted on rows first would report "the call did not finish" for a child
    that never got as far as making it.
    """
    os.kill(child.pid, signal.SIGTERM)
    child.join(timeout=60)
    assert child.exitcode == 0, f"the daemon child exited {child.exitcode}"
    outcome, payload = results.get(timeout=30)
    assert outcome == "served", payload
    return dict(payload)


@pytest.fixture
def children() -> Iterator[Any]:
    """Spawn children, and kill every one of them however the test ends.

    A leaked daemon would hold this store's execution lock for the rest of the
    session, and (worse) would still be claiming calls out of a database the
    next test is about to assert on.
    """
    ctx = multiprocessing.get_context("spawn")
    spawned: List[Any] = []

    class _Children:
        context = ctx

        def start(self, target: Any, *args: Any) -> Any:
            proc = ctx.Process(target=target, args=args, daemon=True)
            proc.start()
            spawned.append(proc)
            return proc

    yield _Children()

    for proc in spawned:
        if proc.is_alive():
            proc.kill()
        proc.join(timeout=30)


@pytest.fixture
def detached() -> Iterator[List[CouncilDaemon]]:
    """Daemons started DETACHED by ``start()``, stopped however the test ends."""
    started: List[CouncilDaemon] = []
    yield started
    for daemon in started:
        holder = daemon.lock.probe()
        pid = (holder or {}).get("pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for _ in range(200):
            if daemon.lock.probe() is None:
                break
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# A. The singleton: one council per machine store
# ---------------------------------------------------------------------------


class TestTheLockIsTheSingleton:
    """The real ``execlock``, a real file, and a real competing process."""

    def test_a_second_start_is_refused_naming_the_holders_pid(
        self, tmp_path: Path, children: Any
    ) -> None:
        config = _bare_config(tmp_path)
        util = _util(config)
        daemon = util.daemon(timings=FAST)
        taken, release = children.context.Event(), children.context.Event()
        results = children.context.Queue()
        holder = children.start(
            hold_the_lock, str(daemon.directory), "daemon", taken, release, results
        )
        try:
            assert taken.wait(timeout=60), "the holding child never took the lock"
            with pytest.raises(LockHeld) as caught:
                daemon.start()
            message = str(caught.value)
            # The pid of the REAL other process, so a user knows what to stop.
            assert str(holder.pid) in message
            assert "daemon" in message
            assert str(daemon.lock.path) in message
            assert caught.value.holder is not None
            assert caught.value.holder["pid"] == holder.pid
            # And nothing was spawned: no second child, no log of one starting.
            assert not daemon.log_path.exists()
        finally:
            release.set()
            holder.join(timeout=30)
            util.close()

    def test_a_new_start_succeeds_once_the_holder_is_killed(
        self, tmp_path: Path, children: Any, detached: List[CouncilDaemon]
    ) -> None:
        """The whole reason the singleton is a file lock and not a lease.

        A ``daemon_lease`` row held by a killed daemon stayed held until its
        TTL lapsed, so the next start had to wait it out.  The kernel releases
        an ``flock`` the instant its holder dies.
        """
        config = _bare_config(tmp_path)
        util = _util(_written(tmp_path, config), store_url=config["store"])
        daemon = util.daemon(timings=FAST)
        taken, release = children.context.Event(), children.context.Event()
        results = children.context.Queue()
        holder = children.start(
            hold_the_lock, str(daemon.directory), "daemon", taken, release, results
        )
        assert taken.wait(timeout=60), "the holding child never took the lock"
        holder.kill()
        holder.join(timeout=30)
        assert not holder.is_alive()
        # NOT release.set(): the child was SIGKILLed, possibly while waiting on
        # that event's condition, so its wake-up accounting is permanently
        # short one slot and notify() in this process would block for ever.
        # There is nothing to release -- the holder is dead, which is the
        # point of the test.

        # No TTL, no reclaim, no wait.  And this is the SHIPPED argv: a real
        # `kbu council daemon start` child, detached into its own session.
        detached.append(daemon)
        report = daemon.start()
        assert report["started"] is True
        assert report["pid"] != holder.pid
        assert daemon.lock.probe()["pid"] == report["pid"]
        assert (daemon.directory / LOG_FILENAME).exists()
        util.close()

    def test_run_is_refused_while_the_daemon_holds_the_lock(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        util = _util(config)
        try:
            job_id = _submit(util, "alpha")
            with util.execution_lock().acquire("daemon") as record:
                with pytest.raises(LockHeld) as caught:
                    util.run(job_id)
            message = str(caught.value)
            assert "council daemon" in message
            assert str(record["pid"]) in message
            # The instruction, not just the refusal.
            assert "Stop it, or wait for it to finish" in message
            # Nothing was worked: every call is still pending and unspent.
            assert {call["status"] for call in _calls(config)} == {"pending"}
            assert _attempts(config) == []
        finally:
            util.close()

    def test_evaluate_is_refused_while_the_daemon_holds_the_lock(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        util = _util(config)
        try:
            query = util.register_query(support.make_query("daemon_test", "1").to_spec())
            with util.execution_lock().acquire("daemon"):
                with pytest.raises(LockHeld, match="council evaluate"):
                    util.evaluate(_items("alpha")[0], query=query)
        finally:
            util.close()

    def test_the_daemon_is_refused_while_a_run_holds_the_lock(
        self, tmp_path: Path
    ) -> None:
        """The other direction.  Both ways, or it is not mutual exclusion."""
        config = _bare_config(tmp_path)
        util = _util(config)
        daemon = util.daemon(timings=FAST)
        try:
            with util.execution_lock().acquire("run") as record:
                with pytest.raises(LockHeld) as caught:
                    daemon.start()
                assert "council run" in str(caught.value)
                assert str(record["pid"]) in str(caught.value)
        finally:
            util.close()

    def test_stop_refuses_to_signal_a_holder_that_is_not_a_daemon(
        self, tmp_path: Path
    ) -> None:
        """``daemon stop`` signals ONLY a daemon (review round 3).

        A ``kbu council run`` in somebody's terminal and a ``db migrate``
        halfway through dropping a table are lock holders too, and
        ``SIGTERM``-ing either because it was in the way is not this verb's
        business.
        """
        config = _bare_config(tmp_path)
        util = _util(config)
        daemon = util.daemon(timings=FAST)
        try:
            for kind in ("run", "evaluate", "migrate"):
                with util.execution_lock().acquire(kind):
                    with pytest.raises(DaemonError) as caught:
                        daemon.stop()
                    assert f"council {kind}" in str(caught.value)
                    assert "only signals a daemon" in str(caught.value)
        finally:
            util.close()

    def test_stop_says_so_when_nothing_is_running(self, tmp_path: Path) -> None:
        config = _bare_config(tmp_path)
        util = _util(config)
        try:
            report = util.daemon(timings=FAST).stop()
            assert report["stopped"] is False
            assert report["running"] is False
            assert "no daemon is running" in report["detail"]
        finally:
            util.close()

    def test_stop_signals_the_pid_in_the_lock_file(
        self, tmp_path: Path, detached: List[CouncilDaemon]
    ) -> None:
        """A real detached child, a real ``SIGTERM``, and the lock comes free."""
        config = _bare_config(tmp_path)
        util = _util(_written(tmp_path, config), store_url=config["store"])
        daemon = util.daemon(timings=FAST)
        detached.append(daemon)
        started = daemon.start()
        pid = started["pid"]
        assert daemon.lock.probe()["pid"] == pid

        report = daemon.stop()
        assert report["stopped"] is True
        assert report["pid"] == pid
        # The lock is free, and the process really is gone.
        assert daemon.lock.probe() is None
        _until(lambda: not _alive(pid))
        # It took its workers row with it (its last act before the release).
        assert util.daemon().status()["workers"] == []
        util.close()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not our process
        return True
    return True


# ---------------------------------------------------------------------------
# B. The loop
# ---------------------------------------------------------------------------


class TestTheLoop:
    def test_the_foreground_loop_completes_a_job_and_heartbeats(
        self, tmp_path: Path, children: Any
    ) -> None:
        config = _config(tmp_path)
        util = _util(config)
        job_id = _submit(util, "alpha", "beta")
        results = children.context.Queue()
        child = children.start(serve_the_store, config, FAST, results)

        # The job finishes...
        _until(lambda: util.job_status(job_id)["status"] == "done", timeout=30)
        status = util.job_status(job_id)
        assert status["percent_complete"] == 100.0
        assert status["by_status"] == {"ok": 2}

        # ...and the workers row was stamped more than once while it did.
        row = _until(lambda: (util.daemon().status()["workers"] or [None])[0])
        assert row["kind"] == "daemon"
        assert row["pid"] == child.pid
        assert row["state"] == "live"
        first = row["heartbeat_at"]
        _until(
            lambda: util.daemon().status()["workers"][0]["heartbeat_at"] > first,
            timeout=20,
        )

        summary = _stop_and_collect(child, results)
        assert summary["finished"] >= 5
        util.close()

    def test_the_cwd_moves_to_the_council_directory(
        self, tmp_path: Path, children: Any
    ) -> None:
        """Both CLI backends read project context out of the cwd.

        A daemon launched from an agent's worktree that kept that worktree
        would answer council questions with that repository in scope, so the
        chdir happens before anything is served -- and the summary reports
        where it went, because a property nobody can observe is a property
        nobody can keep.
        """
        config = _config(tmp_path)
        util = _util(config)
        results = children.context.Queue()
        child = children.start(serve_the_store, config, FAST, results)
        _until(lambda: util.daemon().status()["workers"], timeout=30)
        summary = _stop_and_collect(child, results)
        council_dir = (tmp_path / "council").resolve()
        assert Path(summary["cwd"]).resolve() == council_dir
        # The child did NOT inherit the directory pytest is running in.
        assert Path(summary["cwd"]).resolve() != Path.cwd().resolve()
        util.close()

    def test_sigterm_lets_an_in_flight_call_finish_and_claims_nothing_new(
        self, tmp_path: Path, children: Any
    ) -> None:
        """Stop CLAIMING, but never abandon a call already being paid for.

        One concurrency slot and a slow answer, so exactly one call is in
        flight when the signal lands.  That call must come back ``ok`` -- it
        was already being spent on -- and nothing else may be claimed.
        """
        config = _config(tmp_path, {"main": _endpoint(delay=1.5, max_concurrency=1)})
        util = _util(config)
        _submit(util, "alpha")
        results = children.context.Queue()
        # A drain window LONGER than the answer: this test is about the call
        # being allowed to finish, which is a different property from the
        # deadline cutting it off (TestTheStopDeadline).
        child = children.start(serve_the_store, config, FAST.scaled(drain_s=20.0), results)

        _until(
            lambda: [c for c in _calls(config) if c["status"] == "claimed"], timeout=30
        )
        summary = _stop_and_collect(child, results)
        assert summary["interrupted"] == 0, "the in-flight call was cut off"

        calls = _calls(config)
        done = [c for c in calls if c["status"] == "ok"]
        pending = [c for c in calls if c["status"] == "pending"]
        assert len(done) == 1, calls
        assert len(pending) == len(calls) - 1
        # Nothing new was claimed, and nothing was abandoned mid-flight.
        assert all(c["claim_token"] is None for c in calls)
        assert [a["outcome_kind"] for a in _attempts(config)] == ["ok"]
        util.close()


# ---------------------------------------------------------------------------
# C. Readiness, per (endpoint, served model)
# ---------------------------------------------------------------------------


class TestReadiness:
    def test_only_endpoints_the_doctor_reports_ready_are_served(
        self, tmp_path: Path
    ) -> None:
        config = _config(
            tmp_path,
            {"main": _endpoint(), "other": _endpoint(reuse="other", lists=False)},
        )
        util = _util(config)
        try:
            daemon = util.daemon(timings=FAST)
            store = util.require_schema()
            support.run(daemon._refresh_readiness(store))
            assert daemon.served == {"main"}
            states = {key: report["state"] for key, report in daemon.readiness.items()}
            assert states[("main", "p1")] == "ready"
            assert states[("other", "cr")] == "unavailable"
            # The whole roster was probed, not only the endpoints with calls.
            assert {endpoint for endpoint, _model in daemon.readiness} == {"main", "other"}
        finally:
            util.close()

    def test_readiness_is_per_endpoint_and_model_not_per_endpoint(
        self, tmp_path: Path
    ) -> None:
        """An endpoint that is up and does not serve the model a seat names.

        The listing is the readiness answer, so a seat on a model the endpoint
        does NOT list is ``unavailable`` even though the endpoint answered --
        which is the whole reason readiness is keyed on the pair.
        """
        config = _config(tmp_path)
        config["models"]["mt"] = {"endpoint": "main", "served_name": "not-served", "canonical": "test/mt"}
        util = _util(config)
        try:
            daemon = util.daemon(timings=FAST)
            support.run(daemon._refresh_readiness(util.require_schema()))
            assert daemon.readiness[("main", "p1")]["state"] == "ready"
            assert daemon.readiness[("main", "not-served")]["state"] == "unavailable"
            assert "does not list" in daemon.readiness[("main", "not-served")]["detail"]
            # The endpoint is still served: three of its routes are ready, and
            # the one that is not is refused per CALL, not per endpoint.
            assert daemon.served == {"main"}
        finally:
            util.close()

    def test_a_route_that_fails_its_first_probe_becomes_eligible_when_it_succeeds(
        self, tmp_path: Path
    ) -> None:
        """Probed again, not written off.  And the blocker says what is wrong.

        A gate FILE rather than a counter, because the probe is answered by
        whichever api type instance the wrapper's cache hands over and the
        test must not depend on which.
        """
        gate = tmp_path / "endpoint-is-up"
        config = _config(tmp_path, {"main": _endpoint(gate=str(gate))})
        util = _util(config)
        try:
            job_id = _submit(util, "alpha", allow_unavailable=True)
            daemon = util.daemon(timings=FAST)
            store = util.require_schema()

            # First probe: the gate file does not exist, so the endpoint lists
            # nothing.  Nothing is served, and every waiting call carries the
            # probe's own failure text.
            support.run(daemon._refresh_readiness(store))
            assert daemon.served == set()
            assert daemon.readiness[("main", "p1")]["state"] == "unavailable"
            errors = {c["call_id"]: c["error"] for c in _calls(config)}
            assert errors and all(e["kind"] == "model_unavailable" for e in errors.values())
            assert all("listed no models" in e["message"] for e in errors.values())
            blockers = util.job_status(job_id)["blockers"]
            assert any(b["kind"] == "waiting_for_model" for b in blockers), blockers

            # The endpoint comes back.  Same daemon, same objects: the route
            # becomes eligible on its own, with no restart.
            gate.write_text("up", encoding="utf-8")
            support.run(daemon._refresh_readiness(store))
            assert daemon.served == {"main"}
            assert daemon.readiness[("main", "p1")]["state"] == "ready"
            # ...and the blocker is taken off the calls it was put on.
            assert all(c["error"] is None for c in _calls(config))
            assert not [
                b for b in util.job_status(job_id)["blockers"]
                if b["kind"] == "waiting_for_model"
            ]
        finally:
            util.close()

    def test_a_cleared_halt_forces_a_probe_rather_than_waiting_for_the_interval(
        self, tmp_path: Path
    ) -> None:
        """``endpoints reset-halt`` and the daemon are two processes.

        A store is a file, not a server, so "immediately after reset-halt" is
        the daemon noticing on its next pass that an endpoint it had recorded
        as halted no longer is -- seconds rather than up to a probe interval.
        """
        config = _config(tmp_path)
        util = _util(config)
        try:
            daemon = util.daemon(timings=FAST)
            store = util.require_schema()
            store.set_halt("main", "quota_exhausted", "out of quota")
            support.run(daemon._refresh_readiness(store))
            assert daemon._halts["main"] == "quota_exhausted"
            assert daemon._halt_cleared(store) is False

            util.reset_halt("main")
            assert daemon._halt_cleared(store) is True
            # Asked and answered once: the next pass must not re-probe for a
            # halt that was already noticed as cleared.
            assert daemon._halt_cleared(store) is False
        finally:
            util.close()

    def test_a_route_with_no_report_yet_is_admitted_rather_than_stalled(
        self, tmp_path: Path
    ) -> None:
        """A job submitted since the last probe must not wait a probe interval."""
        config = _config(tmp_path)
        util = _util(config)
        try:
            daemon = util.daemon(timings=FAST)
            admitted, why = daemon._admits(
                {"endpoint": "main", "served_model": "brand-new", "job_id": "j"}
            )
            assert admitted is True
            assert why == ""
        finally:
            util.close()


# ---------------------------------------------------------------------------
# D. Per-job permissions
# ---------------------------------------------------------------------------


class TestPerJobPermissions:
    def test_one_jobs_allow_unverified_admits_only_its_own_unknown_calls(
        self, tmp_path: Path, children: Any
    ) -> None:
        """The permission is the JOB's, read per call (review round 3).

        An endpoint that lists its models (so every route is ``ready`` and the
        daemon serves it) but cannot say HOW it bills.  Two jobs in one queue,
        in flight at the same moment: the one submitted with
        ``allow_unverified`` is answered, the one without is refused -- and the
        endpoint is NOT halted, because the refusal is a fact about one job's
        permission and a halt would stop the other.
        """
        config = _config(tmp_path, {"main": _endpoint(billing="unknown")})
        util = _util(config)
        permitted = _submit(util, "alpha", name="permitted", allow_unverified=True)
        refused = _submit(util, "beta", name="refused")
        results = children.context.Queue()
        child = children.start(serve_the_store, config, FAST, results)
        try:
            _until(lambda: util.job_status(permitted)["status"] == "done", timeout=30)
            _until(
                lambda: all(
                    call["status"] == "error"
                    for call in util.require_schema().get_calls(refused)
                ),
                timeout=30,
            )
        finally:
            _stop_and_collect(child, results)

        assert util.job_status(permitted)["by_status"] == {"ok": 1}
        refused_calls = util.require_schema().get_calls(refused)
        assert refused_calls
        kinds = {dict(c["error"] or {}).get("kind") for c in refused_calls}
        assert kinds == {"billing_guard"}
        assert all("allow_unverified" in dict(c["error"])["message"] for c in refused_calls)
        # One job's refusal must never halt the endpoint the other is using.
        assert util.require_schema().get_endpoint_state("main")["halt_kind"] is None
        util.close()

    def test_an_unknown_route_is_admitted_for_a_permitted_job_only(
        self, tmp_path: Path
    ) -> None:
        """The daemon's own half of the same rule, at the ROUTE level.

        An api type with neither ``models()`` nor ``preflight()`` is
        ``unknown`` -- there is nothing to ask it.  The daemon then admits its
        calls for a job that carries ``allow_unverified`` and hands back the
        calls of one that does not, reading the options of each call's OWN job.
        """
        config = _config(tmp_path, {"main": _endpoint(no_models=True)})
        util = _util(config)
        try:
            permitted = _submit(util, "alpha", name="permitted", allow_unverified=True)
            daemon = util.daemon(timings=FAST)
            support.run(daemon._refresh_readiness(util.require_schema()))
            assert daemon.readiness[("main", "p1")]["state"] == "unknown"
            # Served, because a job in the store carries the permission.
            assert daemon.served == {"main"}

            rows = util.require_schema().get_calls(permitted)
            admitted, _why = daemon._admits(dict(rows[0]))
            assert admitted is True

            # The same route, for a job WITHOUT the permission, is not.
            unpermitted = dict(rows[0])
            unpermitted["job_options"] = {"allow_unverified": False}
            admitted, why = daemon._admits(unpermitted)
            assert admitted is False
            assert "allow_unverified" in why
        finally:
            util.close()


# ---------------------------------------------------------------------------
# E. The stop deadline
# ---------------------------------------------------------------------------


class TestTheStopDeadline:
    def test_a_hung_call_at_the_deadline_is_interrupted_and_costs_no_retry(
        self, tmp_path: Path, children: Any
    ) -> None:
        """The deadline, read back out of SQLite.

        A real :class:`CouncilWorker` in a real child, a test-registered api
        type that never answers, and a drain window short enough to measure.
        At the deadline the call must be PENDING again with its retry count
        untouched, carry an ``interrupted`` attempt whose usage is marked
        unknown, and -- because this endpoint has a token budget it can no
        longer account for -- leave the endpoint halted
        ``budget_usage_unknown``.
        """
        config = _config(
            tmp_path, {"main": _endpoint(hang=True, max_concurrency=1, budget=1000)}
        )
        util = _util(config)
        _submit(util, "alpha")
        results = children.context.Queue()
        child = children.start(serve_the_store, config, FAST, results)

        claimed = _until(
            lambda: [c for c in _calls(config) if c["status"] == "claimed"], timeout=30
        )
        assert len(claimed) == 1
        call_id = claimed[0]["call_id"]
        assert claimed[0]["attempt_count"] == 0

        summary = _stop_and_collect(child, results)
        assert summary["interrupted"] == 1

        # Back in the queue, unspent.
        calls = {c["call_id"]: c for c in _calls(config)}
        assert calls[call_id]["status"] == "pending"
        assert calls[call_id]["attempt_count"] == 0, "a stop must not cost a retry"
        assert calls[call_id]["claim_token"] is None, "the claim token was invalidated"

        # The audit trail says the call was made and that its cost is unknown.
        attempts = [a for a in _attempts(config) if a["call_id"] == call_id]
        assert [a["outcome_kind"] for a in attempts] == ["interrupted"]
        assert attempts[0]["usage"] == {"unknown": True}
        # Below zero: the engine's sequence is 1, 2, 3 and an attempt that
        # cost the call no retry must not take a number from it.
        assert attempts[0]["attempt_no"] == -1

        # A budget that cannot be measured cannot be enforced.
        state = util.require_schema().get_endpoint_state("main")
        assert state["halt_kind"] == "budget_usage_unknown"
        assert "interrupted" in state["halt_message"]
        util.close()

    def test_an_interrupted_call_can_be_retried_without_an_attempt_collision(
        self, tmp_path: Path
    ) -> None:
        """The interrupted attempt must not steal the next try's number.

        ``attempts`` is keyed ``(call_id, attempt_no)`` and the engine derives
        that number from the call's retry count -- which an interrupt
        deliberately does not move.  So the interrupted row is filed under the
        next FREE number, and the call's next real try still has one.
        """
        config = _config(tmp_path)
        util = _util(config)
        try:
            job_id = _submit(util, "alpha")
            store = util.require_schema()
            daemon = util.daemon(timings=FAST)
            worker_id = daemon.worker_id

            # Claim one call as this daemon, then interrupt it exactly as the
            # stop deadline would.
            rows = store.claim_calls(worker_id, {"main"}, 1)
            assert len(rows) == 1
            assert daemon._interrupt_claimed(store) == 1
            after = {c["call_id"]: c for c in _calls(config)}[rows[0]["call_id"]]
            assert after["status"] == "pending"
            assert after["attempt_count"] == 0

            # Now work the job for real.  Nothing collides, and the job
            # finishes -- which is the property the attempt numbering exists
            # to protect.
            util.run(job_id)
            assert util.job_status(job_id)["status"] == "done"
            mine = sorted(
                (a for a in _attempts(config) if a["call_id"] == rows[0]["call_id"]),
                key=lambda a: a["attempt_no"],
            )
            # The uncharged interrupt at -1, the call's first real try at 1.
            assert [(a["attempt_no"], a["outcome_kind"]) for a in mine] == [
                (-1, "interrupted"),
                (1, "ok"),
            ]
        finally:
            util.close()


# ---------------------------------------------------------------------------
# F. Resume from suspension
# ---------------------------------------------------------------------------


class TestResumeFromSuspension:
    def test_a_wall_clock_gap_is_detected_as_a_resume(self, tmp_path: Path) -> None:
        """Measured against the MONOTONIC clock, not on its own.

        ``CLOCK_MONOTONIC`` does not run while the machine is suspended and
        ``CLOCK_REALTIME`` does, so the difference between the two deltas is
        the time spent asleep.  Comparing wall-clock deltas alone would read a
        single long busy pass -- a twenty-minute CLI call -- as a resume, and
        a reclaim on every such pass is work for nothing.
        """
        util = _util(_bare_config(tmp_path))
        try:
            daemon = util.daemon(timings=DEFAULT_TIMINGS)
            wall = [1000.0]
            mono = [500.0]
            daemon._wall = lambda: wall[0]
            daemon._mono = lambda: mono[0]

            assert daemon._tick_clock() is False, "the first tick has nothing to compare"

            # A long BUSY pass: both clocks advanced together.
            wall[0] += 900.0
            mono[0] += 900.0
            assert daemon._tick_clock() is False
            assert daemon.resumes == 0

            # A suspend: the wall clock ran and the monotonic clock did not.
            wall[0] += 3600.0
            mono[0] += 1.0
            assert daemon._tick_clock() is True
            assert daemon.resumes == 1

            # A gap under the threshold (2 x 30 s) is not a resume.
            wall[0] += 40.0
            mono[0] += 1.0
            assert daemon._tick_clock() is False
            assert daemon.resumes == 1
        finally:
            util.close()

    def test_the_loop_reclaims_stale_claims_before_the_next_heartbeat(
        self, tmp_path: Path
    ) -> None:
        """Order matters: a claim lost to a sleeping laptop has to be back in
        the queue before anything reports the daemon as healthy again."""
        config = _bare_config(tmp_path)
        util = _util(config)
        try:
            daemon = util.daemon(timings=FAST)
            store = util.require_schema()
            order: List[str] = []
            real_reclaim = store.reclaim_stale
            real_beat = store.heartbeat_worker

            def _reclaim(*args: Any, **kwargs: Any) -> int:
                order.append("reclaim")
                return real_reclaim(*args, **kwargs)

            def _beat(*args: Any, **kwargs: Any) -> bool:
                order.append("heartbeat")
                return real_beat(*args, **kwargs)

            store.reclaim_stale = _reclaim  # type: ignore[method-assign]
            store.heartbeat_worker = _beat  # type: ignore[method-assign]

            # Mark the pass on which the resume is DETECTED.  The worker
            # reclaims and heartbeats on every pass of its own accord, so the
            # order of the whole run says nothing -- what matters is what
            # happens FIRST after the detection.
            real_tick = daemon._tick_clock
            def _tick() -> bool:
                resumed = real_tick()
                if resumed:
                    order.append("resume")
                return resumed

            daemon._tick_clock = _tick  # type: ignore[method-assign]

            # Make the SECOND tick look like a resume, then let the loop stop.
            ticks = iter([0.0, 10_000.0])
            daemon._wall = lambda: next(ticks, 10_000.0)
            daemon._mono = lambda: 0.0

            summary = support.run(_serve_two_passes(daemon))
            assert summary["resumes"] >= 1
            assert "resume" in order, order
            after = order[order.index("resume") + 1 :]
            assert after[:1] == ["reclaim"], order
        finally:
            util.close()


async def _serve_two_passes(daemon: CouncilDaemon) -> Dict[str, Any]:
    """Run ``_serve`` and stop it after its second pass.

    Driven from inside rather than by a signal because this test is about the
    ORDER of two store writes, not about signal handling -- which the
    subprocess tests above cover with a real ``SIGTERM``.  The loop is left
    entirely alone; only the wall clock lies.
    """
    served = asyncio.ensure_future(daemon._serve({"started_at": "now"}))
    deadline = time.monotonic() + 20
    while not served.done() and time.monotonic() < deadline:
        if daemon.resumes >= 1:
            # The same thing the SIGTERM handler does, called directly: this
            # test is about the ORDER of two store writes, and arranging a
            # real signal as well would be a second variable in it.
            daemon.request_stop()
        await asyncio.sleep(0.02)
    if not served.done():
        served.cancel()
        raise AssertionError("the daemon loop never reached a second pass")
    return await served


# ---------------------------------------------------------------------------
# G. The shipped argv and the shipped clocks
# ---------------------------------------------------------------------------


class TestWhatIsShipped:
    def test_the_production_timings_are_the_specified_ones(self) -> None:
        assert DEFAULT_TIMINGS.heartbeat_s == 30.0
        assert DEFAULT_TIMINGS.probe_s == 60.0
        assert DEFAULT_TIMINGS.idle_poll_s == 10.0
        assert DEFAULT_TIMINGS.drain_s == 120.0
        assert DEFAULT_TIMINGS.stop_wait_s == 130.0
        # Stopping waits out the whole drain window plus the tidy-up.
        assert DEFAULT_TIMINGS.stop_wait_s > DEFAULT_TIMINGS.drain_s

    def test_the_child_argv_is_this_interpreter_and_this_configuration(
        self, tmp_path: Path
    ) -> None:
        """Not ``kbu`` off ``PATH``: a venv whose bin is not on the daemon's
        PATH is the normal case on this fleet, and resolving ``kbu`` there is
        how a daemon ends up running a different installation of the library
        than the one that started it."""
        import sys

        path = _written(tmp_path, _bare_config(tmp_path))
        util = _util(path, store_url=f"sqlite:///{tmp_path}/council/council.sqlite")
        try:
            argv = util.daemon()._default_child_argv()
            assert argv[0] == sys.executable
            assert argv[1:4] == ["-m", "kbutillib", "council"]
            assert "--config" in argv and path in argv
            assert "--store" in argv
            assert argv[-3:] == ["daemon", "start", "--foreground"]
        finally:
            util.close()

    def test_a_detached_start_refuses_an_in_memory_configuration(
        self, tmp_path: Path
    ) -> None:
        """A child cannot be handed a dict, and must not be handed a guess.

        Approximating it by letting the child load the packaged default would
        have it probe ARGO and work ``~/.kbutillib/council`` -- neither of
        which the caller asked for.
        """
        util = _util(_bare_config(tmp_path))
        try:
            with pytest.raises(DaemonError) as caught:
                util.daemon(timings=FAST).start()
            assert "in-memory configuration" in str(caught.value)
            assert "--foreground" in str(caught.value)
            # And it refused BEFORE spawning anything.
            assert util.daemon().lock.probe() is None
        finally:
            util.close()

    def test_a_child_that_dies_before_taking_the_lock_is_reported_with_its_log(
        self, tmp_path: Path
    ) -> None:
        """A ``start`` that returned as soon as ``Popen`` did would report
        success for a child that was about to die of a config error."""
        import sys

        config = _bare_config(tmp_path)
        util = _util(config)
        try:
            daemon = util.daemon(
                timings=FAST.scaled(start_wait_s=10.0),
                child_argv=[sys.executable, "-c", "import sys; sys.exit(17)"],
            )
            with pytest.raises(DaemonError) as caught:
                daemon.start()
            assert "exited 17" in str(caught.value)
            assert str(daemon.log_path) in str(caught.value)
            assert daemon.lock.probe() is None
        finally:
            util.close()
