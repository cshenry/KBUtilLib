"""The account-wide REQUEST PACER, and the sustained-rate-limit halt.

A CONCURRENCY CAP IS NOT A RATE LIMIT.  MAG's binding limit is a rate -- 120
requests per minute, ACCOUNT-WIDE, alongside a 20-parallel cap that is also
account-wide (measured 2026-10-21; a config asking 24 parallel errored 427 of
500 pilot items over 1,925 transport attempts).  The configuration measured to
work, "MAG queues summing to 6", is concurrency standing in for rate control:
it lands near 100 req/min only because the measured service time is 3-4 s, and
it overshoots the moment the gateway gets faster.

So the assertion that matters here is
:meth:`TestThePacerEnforcesARate.test_the_concurrency_caps_alone_would_exceed_the_rate`
and its sibling: THE SERVICE TIME IS ZERO, so the per-model semaphores alone
would fire every request at once, and what holds the dispatch rate down has to
be the bucket.  A test with a slow backend would pass with no pacer at all.

Nothing here reaches a network or spends a token: every seat is a
``FakeBackend`` over a ``fake`` endpoint.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import pytest

from kbutillib.domains.ai.council.engine.worker import (
    DEFAULT_RATE_BURST,
    RATE_LIMIT_HALT_AFTER,
    RATE_LIMIT_WINDOW_S,
    RETRYABLE_KINDS,
    CouncilWorker,
    _RateBucket,
)
from kbutillib.domains.ai.llm.types import LLMError

from . import engine_support as support

#: Requests per minute the paced tests configure.  High enough that the whole
#: suite stays fast and low enough that the pacing is unmistakable: 1,200/min
#: is 20/s, so 10 requests take at least 450 ms of pacing against a backend
#: whose service time is ZERO.
PACED_RPM = 1200.0

#: Concurrency the paced endpoint declares.  Deliberately larger than the
#: number of requests in the test, so the SEMAPHORE cannot be what is pacing
#: them -- every request could be in flight at once as far as it is concerned.
PACED_CONCURRENCY = 16


def _endpoints(**overrides: Any) -> Dict[str, Any]:
    """Two aliases on ONE account, both declaring the same per-minute limit.

    Two aliases because that is the case a bucket in the api type could not
    express: the wrapper caches an api type per ENDPOINT, so two aliases of
    one subscription would be two buckets, which is the wrong granularity for
    a limit the provider enforces account-wide.
    """
    shared = {
        "account": "mag-shared",
        "max_concurrency": PACED_CONCURRENCY,
        "max_requests_per_minute": PACED_RPM,
        **overrides,
    }
    return support.endpoint_config(fake1=dict(shared), fake2=dict(shared))


def _worker(
    store: Any, backends: Dict[str, Any], *, config: Optional[Dict[str, Any]] = None, **kwargs: Any
) -> CouncilWorker:
    return CouncilWorker(
        store,
        config if config is not None else _endpoints(),
        backends,
        user="test",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The bucket itself
# ---------------------------------------------------------------------------


class TestTheBucket:
    """A token bucket, driven by an injected clock so nothing sleeps."""

    def _bucket(self, rpm: float = 60.0) -> Any:
        """A bucket on a clock the test advances by hand."""
        state = {"now": 0.0}
        slept: List[float] = []

        async def _sleep(seconds: float) -> None:
            slept.append(seconds)
            state["now"] += seconds

        bucket = _RateBucket(
            "mag-shared", rpm, clock=lambda: state["now"], sleep=_sleep
        )
        return bucket, state, slept

    def test_the_burst_is_one_because_the_limit_is_windowed(self) -> None:
        """A burst probe overestimated sustained throughput by ~3x."""
        assert DEFAULT_RATE_BURST == 1.0
        bucket, _state, _slept = self._bucket()
        assert bucket.capacity == 1.0

    def test_the_first_dispatch_does_not_wait(self) -> None:
        bucket, _state, slept = self._bucket()
        assert support.run(bucket.acquire()) == 0.0
        assert slept == []

    def test_the_second_dispatch_waits_a_whole_interval(self) -> None:
        """60/min is one per second, and there is no burst to spend."""
        bucket, _state, slept = self._bucket(rpm=60.0)
        support.run(bucket.acquire())
        assert support.run(bucket.acquire()) == pytest.approx(1.0)
        assert slept == [pytest.approx(1.0)]

    def test_time_already_elapsed_is_credited(self) -> None:
        bucket, state, slept = self._bucket(rpm=60.0)
        support.run(bucket.acquire())
        state["now"] += 1.0
        assert support.run(bucket.acquire()) == 0.0
        assert slept == []

    def test_credit_does_not_accumulate_past_the_burst(self) -> None:
        """An idle hour must not buy an hour's worth of requests at once."""
        bucket, state, slept = self._bucket(rpm=60.0)
        support.run(bucket.acquire())
        state["now"] += 3600.0
        assert support.run(bucket.acquire()) == 0.0
        assert support.run(bucket.acquire()) == pytest.approx(1.0)

    def test_the_rate_is_the_configured_one(self) -> None:
        bucket, _state, _slept = self._bucket(rpm=120.0)
        assert bucket.requests_per_minute == 120.0
        assert bucket.rate_per_s == pytest.approx(2.0)

    def test_a_non_positive_rate_is_refused_rather_than_meaning_never(self) -> None:
        with pytest.raises(ValueError, match="max_requests_per_minute"):
            _RateBucket("mag-shared", 0)
        with pytest.raises(ValueError, match="max_requests_per_minute"):
            _RateBucket("mag-shared", -5)

    def test_the_report_names_the_account_and_what_it_held_back(self) -> None:
        bucket, _state, _slept = self._bucket(rpm=60.0)
        support.run(bucket.acquire())
        support.run(bucket.acquire())
        report = bucket.report()
        assert report["account"] == "mag-shared"
        assert report["requests_per_minute"] == 60.0
        assert report["dispatched"] == 2
        assert report["waits"] == 1
        assert report["wait_seconds"] == pytest.approx(1.0)

    def test_it_survives_being_used_on_a_second_event_loop(self) -> None:
        """One worker object may be used by two successive ``work()`` calls."""
        bucket = _RateBucket("mag-shared", 6000.0)
        assert support.run(bucket.acquire()) == 0.0
        support.run(bucket.acquire())
        assert bucket.dispatched == 2


# ---------------------------------------------------------------------------
# The pacer, in the worker, against a zero-service-time backend
# ---------------------------------------------------------------------------


class TestThePacerEnforcesARate:
    """The service time is ZERO, so only a pacer can hold the rate down."""

    REQUESTS = 10

    def _dispatch(
        self, worker: CouncilWorker, endpoint: str, n: int
    ) -> List[float]:
        """Hold *endpoint*'s slot *n* times, returning the dispatch times.

        Goes through :meth:`CouncilWorker._slot` because that is the ONE
        chokepoint every dispatch passes through -- ``_invoke`` and
        ``_invoke_batch`` each hold it exactly once per backend invocation,
        so one token is taken per request whether it carries one item or
        twenty.
        """
        stamps: List[float] = []

        async def _one() -> None:
            async with worker._slot(endpoint):
                stamps.append(time.monotonic())

        async def _all() -> None:
            await asyncio.gather(*[_one() for _ in range(n)])

        support.run(_all())
        return stamps

    def test_the_concurrency_caps_alone_would_exceed_the_rate(
        self, store: Any
    ) -> None:
        """The control: with no declared rate, 10 dispatches are instant.

        This is what makes the next test mean something. The semaphore allows
        16 in flight and the body takes no time, so an unpaced endpoint fires
        all 10 inside a millisecond -- which at 1,200/min would be a 12x
        overshoot of the rate the paced test then measures.
        """
        worker = _worker(
            store,
            {"fake1": None},
            config=support.endpoint_config(
                fake1={"account": "mag-shared", "max_concurrency": PACED_CONCURRENCY}
            ),
        )
        assert worker._limit_by_endpoint["fake1"].pacer is None
        stamps = self._dispatch(worker, "fake1", self.REQUESTS)
        elapsed = max(stamps) - min(stamps)
        minimum_if_paced = (self.REQUESTS - 1) / (PACED_RPM / 60.0)
        assert elapsed < minimum_if_paced / 4, (
            f"the unpaced control took {elapsed:.3f}s, which is not clearly "
            f"below the {minimum_if_paced:.3f}s a paced run must take -- this "
            "test can no longer tell the two apart"
        )

    def test_dispatch_is_held_to_the_configured_requests_per_minute(
        self, store: Any
    ) -> None:
        worker = _worker(store, {"fake1": None})
        stamps = self._dispatch(worker, "fake1", self.REQUESTS)
        elapsed = max(stamps) - min(stamps)
        expected = (self.REQUESTS - 1) / (PACED_RPM / 60.0)
        assert elapsed >= expected * 0.9, (
            f"{self.REQUESTS} dispatches took {elapsed:.3f}s, under the "
            f"{expected:.3f}s that {PACED_RPM:g} req/min allows: the pacer is "
            "not limiting the rate"
        )

    def test_two_aliases_on_one_account_share_ONE_bucket(self, store: Any) -> None:
        """The whole point of the pacer living on the account.

        Five dispatches to each of two endpoint aliases must be paced as TEN
        requests against one limit, not as two independent fives -- the
        gateway enforces per account, and two buckets would let the account
        run at twice its rate.
        """
        worker = _worker(store, {"fake1": None, "fake2": None})
        bucket = worker._limit_by_endpoint["fake1"].pacer
        assert bucket is worker._limit_by_endpoint["fake2"].pacer is not None

        stamps: List[float] = []

        async def _one(endpoint: str) -> None:
            async with worker._slot(endpoint):
                stamps.append(time.monotonic())

        async def _all() -> None:
            await asyncio.gather(
                *[_one("fake1") for _ in range(5)],
                *[_one("fake2") for _ in range(5)],
            )

        support.run(_all())
        elapsed = max(stamps) - min(stamps)
        expected = 9 / (PACED_RPM / 60.0)
        assert elapsed >= expected * 0.9, (
            f"ten dispatches across two aliases of one account took "
            f"{elapsed:.3f}s, under the {expected:.3f}s one shared bucket "
            "allows: the two aliases are being paced separately"
        )
        assert bucket.dispatched == 10

    def test_the_per_model_concurrency_cap_is_still_enforced(
        self, store: Any
    ) -> None:
        """The pacer ADDS a limit; it must not replace the parallel cap."""
        worker = _worker(
            store,
            {"fake1": None},
            config=_endpoints(max_concurrency=2, max_requests_per_minute=60_000.0),
        )
        limit = worker._limit_by_endpoint["fake1"]
        assert limit.capacity == 2
        assert limit.pacer is not None

        peak = {"current": 0, "peak": 0}

        async def _one() -> None:
            async with worker._slot("fake1"):
                peak["current"] += 1
                peak["peak"] = max(peak["peak"], peak["current"])
                await asyncio.sleep(0.01)
                peak["current"] -= 1

        async def _all() -> None:
            await asyncio.gather(*[_one() for _ in range(8)])

        support.run(_all())
        assert peak["peak"] <= 2


# ---------------------------------------------------------------------------
# Where the rate comes from
# ---------------------------------------------------------------------------


class TestWhereTheRateComesFrom:
    """Derived from the account, overridable by the queue child's share."""

    def test_an_account_with_no_declared_rate_gets_no_bucket(
        self, store: Any
    ) -> None:
        """``None`` means "no separate per-minute limit known", not unlimited."""
        worker = _worker(
            store,
            {"fake1": None},
            config=support.endpoint_config(fake1={"account": "a"}),
        )
        assert worker._limit_by_endpoint["fake1"].pacer is None
        assert worker.pacer_report() == []

    def test_the_tightest_declared_rate_on_the_account_is_the_real_one(
        self, store: Any
    ) -> None:
        worker = _worker(
            store,
            {"fake1": None, "fake2": None},
            config=support.endpoint_config(
                fake1={"account": "a", "max_requests_per_minute": 120},
                fake2={"account": "a", "max_requests_per_minute": 60},
            ),
        )
        assert worker._limit_by_endpoint["fake1"].pacer.requests_per_minute == 60.0

    def test_two_accounts_get_two_buckets(self, store: Any) -> None:
        worker = _worker(
            store,
            {"fake1": None, "fake2": None},
            config=support.endpoint_config(
                fake1={"account": "argo-anl", "max_requests_per_minute": 2400},
                fake2={"account": "mag-shared", "max_requests_per_minute": 120},
            ),
        )
        assert worker._limit_by_endpoint["fake1"].pacer is not (
            worker._limit_by_endpoint["fake2"].pacer
        )
        assert [row["requests_per_minute"] for row in worker.pacer_report()] == [
            2400.0,
            120.0,
        ]

    def test_a_queue_childs_share_overrides_the_accounts_own_rate(
        self, store: Any
    ) -> None:
        """A rate must be apportioned: queue children are separate processes.

        Handing each child the full account rate would pace the account at
        ``children x rate``, which is worse than no pacer because it looks
        enforced.
        """
        worker = _worker(store, {"fake1": None}, requests_per_minute=40.0)
        assert worker._limit_by_endpoint["fake1"].pacer.requests_per_minute == 40.0

    def test_the_pacer_report_says_what_it_held_back(self, store: Any) -> None:
        worker = _worker(store, {"fake1": None}, requests_per_minute=60.0)

        async def _two() -> None:
            async with worker._slot("fake1"):
                pass

        support.run(_two())
        report = worker.pacer_report()
        assert len(report) == 1
        assert report[0]["account"] == "mag-shared"
        assert report[0]["dispatched"] == 1


class TestTheDaemonApportionsTheAccountRate:
    """``_queue_requests_per_minute``: the share one child may dispatch at."""

    @pytest.fixture(autouse=True)
    def temp_home(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """A throwaway ``HOME``: the daemon's directory defaults under it.

        Nothing here starts a daemon -- ``_queue_requests_per_minute`` is
        arithmetic over the configuration -- but constructing one resolves
        that directory, and a test must never resolve the operator's own.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

    def _daemon(self, tmp_path: Any, config: Any) -> Any:
        from kbutillib.domains.ai.council.daemon import CouncilDaemon

        class _Council:
            def config(self, reload: bool = False) -> Any:
                return config

            def store_url(self) -> str:
                return f"sqlite:///{tmp_path}/council.sqlite"

            def user(self) -> str:
                return "test"

        return CouncilDaemon(_Council())

    def _config(self, **queue_overrides: Any) -> Any:
        """A loadable configuration with FULL queue coverage.

        Built through ``load_council_config`` rather than by constructing a
        ``CouncilConfig`` directly, because it is the loader that fills each
        endpoint's ``name`` and it is ``_validate_queues`` -- which resolves
        every declared PROFILE's roster -- that records which account each
        queue charges. A fixture that skipped the profiles would leave
        ``queue_account`` empty and prove nothing.

        Four queues cover every seat/role the one profile can submit, which
        queue mode requires: two on MAG's account (the only one declaring a
        per-minute rate) and two on ARGO's.
        """
        from kbutillib.domains.ai.council.config import load_council_config

        queues: Dict[str, Any] = {
            "argo_chair": {"seats": ["proposer_1"], "concurrency": 8},
            "mag_sol": {"seats": ["proposer_2"], "concurrency": 3},
            "mag_luna": {"seats": ["proposer_3"], "concurrency": 3},
            "argo_review": {"seats": ["critic", "meta"], "concurrency": 8},
        }
        for name, override in queue_overrides.items():
            queues[name] = {**queues.get(name, {}), **dict(override)}
        return load_council_config(
            {
                "endpoints": {
                    "mag": {
                        "kind": "openai",
                        "base_url": "https://mag.example/v1",
                        "account": "mag-shared",
                        "max_concurrency": 20,
                        # The one declared per-minute rate, and the measured
                        # one: 120 req/min account-wide.
                        "max_requests_per_minute": 120,
                    },
                    "argo": {
                        "kind": "argo",
                        "base_url": "https://argo.example/api",
                        "account": "argo-anl",
                        "max_concurrency": 24,
                    },
                },
                "models": {
                    "sol": {"endpoint": "mag", "canonical": "test/sol"},
                    "luna": {"endpoint": "mag", "canonical": "test/luna"},
                    "flash": {"endpoint": "argo", "canonical": "test/flash"},
                    "cr": {"endpoint": "argo", "canonical": "test/cr"},
                    "mt": {"endpoint": "argo", "canonical": "test/mt"},
                },
                "profiles": {
                    "test": {
                        "proposers": ["flash", "sol", "luna"],
                        "critic": "cr",
                        "meta": "mt",
                    }
                },
                "default_profile": "test",
                "queues": queues,
            }
        )

    def test_two_queues_on_one_account_split_its_rate(self, tmp_path: Any) -> None:
        """MAG's 120 req/min across queues summing to 6, weighted by concurrency."""
        config = self._config()
        daemon = self._daemon(tmp_path, config)
        queues = dict(config.queues)
        assert daemon._queue_requests_per_minute("mag_sol", queues) == pytest.approx(60.0)
        assert daemon._queue_requests_per_minute("mag_luna", queues) == pytest.approx(60.0)

    def test_the_shares_sum_to_the_accounts_rate(self, tmp_path: Any) -> None:
        config = self._config(
            mag_sol={"concurrency": 4},
            mag_luna={"concurrency": 2},
        )
        daemon = self._daemon(tmp_path, config)
        queues = dict(config.queues)
        shares = {
            name: daemon._queue_requests_per_minute(name, queues)
            for name in ("mag_sol", "mag_luna")
        }
        assert sum(shares.values()) == pytest.approx(120.0)
        assert shares == {
            "mag_sol": pytest.approx(80.0),
            "mag_luna": pytest.approx(40.0),
        }

    def test_workers_above_one_divide_the_share_further(self, tmp_path: Any) -> None:
        """Each child is its own process, so each is its own consumer."""
        config = self._config(mag_sol={"concurrency": 3, "workers": 2})
        daemon = self._daemon(tmp_path, config)
        queues = dict(config.queues)
        # concurrency*workers: 3*2 + 3*1 = 9; one mag_sol child's weight is 3.
        assert daemon._queue_requests_per_minute("mag_sol", queues) == pytest.approx(40.0)

    def test_an_account_with_no_declared_rate_apportions_nothing(
        self, tmp_path: Any
    ) -> None:
        config = self._config()
        daemon = self._daemon(tmp_path, config)
        assert (
            daemon._queue_requests_per_minute("argo_chair", dict(config.queues)) is None
        )

    def test_a_queue_the_configuration_does_not_declare_apportions_nothing(
        self, tmp_path: Any
    ) -> None:
        config = self._config()
        daemon = self._daemon(tmp_path, config)
        assert daemon._queue_requests_per_minute("nope", dict(config.queues)) is None


# ---------------------------------------------------------------------------
# The sustained rate limit is a HALT, not only a retry
# ---------------------------------------------------------------------------


class TestASustainedRateLimitHalts:
    """The bucket cannot see other consumers, so a run of 429s must halt.

    The first pilot reached 1,925 transport attempts for 500 items and errored
    427 of them by retrying into a rate limit, which is the spiral this rule
    exists to stop.
    """

    def test_a_429_is_flagged_while_staying_a_retryable_transport_failure(
        self,
    ) -> None:
        """A flag and not a new kind: one 429 is still retried."""
        import httpx

        from kbutillib.domains.ai.llm.apis.http import OpenAICompatibleBackend

        backend = OpenAICompatibleBackend(
            {"name": "mag", "base_url": "https://mag.example/v1"}
        )
        error = backend._status_error(
            httpx.Response(429, text="slow down"),
            None,
        )
        assert error.kind == "transport"
        assert error.kind in RETRYABLE_KINDS
        assert error.retryable is True
        assert error.halts_endpoint is False
        assert error.rate_limited is True

    def test_other_transport_failures_are_not_flagged(self) -> None:
        """Counting "any transport failure" would halt on a flaky network."""
        import httpx

        from kbutillib.domains.ai.llm.apis.http import OpenAICompatibleBackend

        backend = OpenAICompatibleBackend(
            {"name": "mag", "base_url": "https://mag.example/v1"}
        )
        error = backend._status_error(httpx.Response(500, text="boom"), None)
        assert error.kind == "transport"
        assert error.rate_limited is False

    def test_the_flag_defaults_off_so_an_older_stored_error_reads_false(self) -> None:
        """``calls.error`` is persisted JSON; the field is additive."""
        assert LLMError(kind="transport").rate_limited is False
        assert LLMError.model_validate({"kind": "transport"}).rate_limited is False

    def test_one_rate_limit_does_not_halt(self, store: Any) -> None:
        worker = _worker(store, {"fake1": None})
        assert worker._note_rate_limit("fake1") is None

    def test_a_sustained_run_of_them_halts_the_endpoint(self, store: Any) -> None:
        worker = _worker(store, {"fake1": None})
        for _ in range(RATE_LIMIT_HALT_AFTER - 1):
            assert worker._note_rate_limit("fake1") is None
        halt = worker._note_rate_limit("fake1")
        assert halt is not None
        assert halt.kind == "quota_exhausted"
        assert halt.halts_endpoint is True
        assert "SUSTAINED" in halt.message
        assert "1200 req/min" in halt.message

    def test_the_count_is_per_ACCOUNT_not_per_endpoint(self, store: Any) -> None:
        """The gateway refuses per account, so that is the scope to count at."""
        worker = _worker(store, {"fake1": None, "fake2": None})
        for index in range(RATE_LIMIT_HALT_AFTER - 1):
            endpoint = "fake1" if index % 2 else "fake2"
            assert worker._note_rate_limit(endpoint) is None
        assert worker._note_rate_limit("fake1") is not None

    def test_responses_older_than_the_window_do_not_count(self, store: Any) -> None:
        worker = _worker(store, {"fake1": None})
        limit = worker._limit_by_endpoint["fake1"]
        stale = time.monotonic() - RATE_LIMIT_WINDOW_S - 1
        limit.rate_limited_at = [stale] * (RATE_LIMIT_HALT_AFTER * 2)
        assert worker._note_rate_limit("fake1") is None
        assert limit.rate_limited_at == [pytest.approx(limit.rate_limited_at[0])]

    def test_the_halt_message_says_to_lower_the_rate(self, store: Any) -> None:
        worker = _worker(store, {"fake1": None})
        halt = None
        for _ in range(RATE_LIMIT_HALT_AFTER):
            halt = worker._note_rate_limit("fake1")
        assert "max_requests_per_minute" in halt.message
        assert "reset-halt" in halt.message

    def test_an_unpaced_account_still_halts_and_says_so(self, store: Any) -> None:
        """A rate limit from a gateway nobody declared a rate for still halts."""
        worker = _worker(
            store,
            {"fake1": None},
            config=support.endpoint_config(fake1={"account": "a"}),
        )
        halt = None
        for _ in range(RATE_LIMIT_HALT_AFTER):
            halt = worker._note_rate_limit("fake1")
        assert halt is not None
        assert "no configured per-minute limit" in halt.message

    def test_a_sustained_rate_limit_halts_a_running_worker_end_to_end(
        self, store: Any
    ) -> None:
        """The whole rule, through ``_invoke``: halted, not retried into.

        The proposer seat answers 429 every time. The first few are retried
        (a 429 against a windowed limit is normal), and once the run is
        SUSTAINED the endpoint is halted -- which stops the claim, so the
        worker comes out of its loop reporting a ``halted`` blocker instead of
        burning attempts.
        """
        import httpx

        from kbutillib.domains.ai.llm.apis.http import OpenAICompatibleBackend
        from kbutillib.domains.ai.llm.types import LLMResult

        rate_limited = (
            OpenAICompatibleBackend({"name": "mag", "base_url": "https://m.example/v1"})
            ._status_error(httpx.Response(429, text="slow down"), None)
        )

        class _AlwaysRateLimited:
            kind = "fake"

            def __init__(self) -> None:
                self.calls: List[Any] = []

            async def complete(self, req: Any) -> Any:
                self.calls.append(req)
                return LLMResult(ok=False, error=rate_limited)

            def effective_params(self, request: Any) -> Dict[str, Any]:
                return {"temperature": 0}

            async def aclose(self) -> None:
                return None

        backend = _AlwaysRateLimited()
        worker = _worker(
            store,
            {"fake1": backend},
            config=support.endpoint_config(
                fake1={
                    "account": "mag-shared",
                    "max_concurrency": 4,
                    "max_requests_per_minute": 60_000,
                }
            ),
            retry_backoff_s=(0.001,),
            retry_jitter=0.0,
            max_attempts=10,
        )
        job_id = worker.submit(
            support.make_query(),
            [support.make_task(f"item-{n}", text=f"text {n}") for n in range(6)],
            support.make_roster(),
            label="rate limited",
        )
        summary = support.run(worker.work(job_id=job_id, until_idle=True))

        state = store.get_endpoint_state("fake1")
        assert state["halt_kind"] == "quota_exhausted"
        assert "SUSTAINED" in state["halt_message"]
        assert [b["kind"] for b in summary["blockers"]] == ["halted"]
        # 6 items x 3 proposers = 18 calls, each allowed 10 attempts: an
        # unhalted run would make up to 180. The halt has to cut it short.
        assert len(backend.calls) < 60, (
            f"{len(backend.calls)} calls were made: the sustained rate limit "
            "was retried into rather than halted"
        )
