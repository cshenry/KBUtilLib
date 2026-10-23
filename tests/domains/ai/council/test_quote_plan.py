"""Tests for ``quote_plan``: the stage that prices a run before it is approved.

THE PROPERTY THIS MODULE EXISTS FOR is that the quote spends nothing, and it
is tested rather than asserted: :class:`TestSpendsNothing` runs ``quote_plan``
with the ``socket`` module POISONED, so the test can only pass if no
connection is attempted at all.  It deliberately does not stub an HTTP client
and assert it went uncalled -- a seam every test replaces is a seam no test
covers, and the thing being checked is precisely that no such seam is reached.

The reuse counts here are checked against the WORKER's own prompt hashes:
every seeded call row is built by ``CouncilWorker._call_row``, which is the
code that will actually run.  A quote that agreed with a hash this test
computed itself would prove only that the test and the quote share a bug.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pytest
import sqlalchemy as sa

from kbutillib.domains.ai.council.config import CouncilConfig, load_council_config
from kbutillib.domains.ai.council.quote import (
    META_FIRE_RATE,
    META_FIRE_RATE_N,
    MISS_KINDS,
    PAID_REPAIR_FRACTION,
    Plan,
    PlanItem,
    fit_token_model,
    item_prompt_chars,
    load_plan,
    quote_plan,
)
from kbutillib.domains.ai.council.store import schema as sch
from kbutillib.domains.ai.council.types import Roster, SeatSpec

from .engine_support import make_query
from .store_contract import job_row, utcnow

#: Input tokens per prompt character seeded for each seat.  Distinct per seat
#: so a quote that mixed two seats up would land between them and fail.
SEEDED_RATIO = {
    "proposer_1": 0.30,
    "proposer_2": 0.25,
    "proposer_3": 0.20,
    "critic": 0.18,
    "meta": 0.40,
}

#: Completion tokens seeded per seat.  Constant per seat, so the fitted mean
#: is exactly this and an arithmetic assertion on the quote is possible.
SEEDED_OUTPUT = {
    "proposer_1": 100,
    "proposer_2": 200,
    "proposer_3": 300,
    "critic": 600,
    "meta": 500,
}

#: The two gateways the test config declares.  ``mag`` deliberately carries
#: NO rate in the default config, because "a gateway with no configured rate
#: reports unpriced" is one of the properties under test.
ARGO_ACCOUNT = "argo-anl"
MAG_ACCOUNT = "mag-account"


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def _config(
    *,
    rates: Optional[Mapping[str, Any]] = None,
    queues: Optional[Mapping[str, Any]] = None,
) -> CouncilConfig:
    """A council config with two ACCOUNTS, two queues and optional rates.

    Endpoints are ``openai``-kind with a URL: the quote never builds a
    backend, so what matters is only that the endpoint table is real enough
    to supply a reuse namespace, an account and a concurrency budget.
    """
    raw: Dict[str, Any] = {
        "endpoints": {
            "argo": {
                "kind": "openai",
                "base_url": "https://argo.example/v1",
                "account": ARGO_ACCOUNT,
                "max_concurrency": 24,
            },
            "mag": {
                "kind": "openai",
                "base_url": "https://mag.example/v1",
                "account": MAG_ACCOUNT,
                # The measured working configuration: MAG queues summing to
                # 6, and an account-wide 120 requests per minute.
                "max_concurrency": 6,
                "max_requests_per_minute": 120,
            },
        },
        "models": {},
        "profiles": {},
    }
    if rates is not None:
        raw["rates"] = dict(rates)
    if queues is not None:
        raw["queues"] = dict(queues)
    return load_council_config(raw)


def _seat(
    seat_name: str,
    model: str,
    *,
    endpoint: str = "argo",
    kind: str = "openai",
) -> SeatSpec:
    """One seat.  ``canonical_model`` is derived from *model* unless shared."""
    return SeatSpec(
        seat_name=seat_name,
        endpoint=endpoint,
        endpoint_kind=kind,
        model=model,
        canonical_model=f"vendor/{model}",
        max_output_tokens=512,
        temperature=0,
        timeout_s=30,
    )


def _roster(**overrides: SeatSpec) -> Roster:
    """The bench: proposer_1 on ARGO, 2 and 3 on MAG, critic and meta ARGO.

    The same shape as the measured ``function_to_go`` run, which is what
    makes the wall-clock and gateway assertions below mean anything.
    """
    seats: Dict[str, SeatSpec] = {
        "proposer_1": _seat("proposer_1", "gemini35flash", endpoint="argo"),
        "proposer_2": _seat("proposer_2", "gpt-5.6-sol", endpoint="mag"),
        "proposer_3": _seat("proposer_3", "gpt-5.6-luna", endpoint="mag"),
        "critic": _seat("critic", "claudesonnet5", endpoint="argo"),
        "meta": _seat("meta", "claudeopus5", endpoint="argo"),
    }
    seats.update(overrides)
    return Roster(
        proposers=[seats["proposer_1"], seats["proposer_2"], seats["proposer_3"]],
        critic=seats["critic"],
        meta=seats["meta"],
    )


def _menu(n_entries: int) -> List[Dict[str, Any]]:
    """A candidate menu, sized so prompt characters VARY between items."""
    return [
        {
            "id": f"GO:{i:07d}",
            "label": f"candidate {i}",
            "description": "a definition " * (i + 1),
            "rank": i + 1,
        }
        for i in range(n_entries)
    ]


def _plan_items(n_items: int = 6) -> List[PlanItem]:
    """*n_items* plan items, each with a query_id and a differently sized menu."""
    return [
        PlanItem(
            item_id=f"item-{i}",
            evidence={"text": f"protein function {i}", "source": "test"},
            menu=_menu(i % 3 + 1),
            query_id=f"ch-entity-{i:04d}",
        )
        for i in range(n_items)
    ]


def _plan(items: Optional[Sequence[PlanItem]] = None, version: str = "1") -> Plan:
    """A plan over *items* for the in-test query type at *version*."""
    return Plan(
        query_name="engine_test",
        query_version=version,
        items=tuple(_plan_items() if items is None else items),
        mapping_hash="m" * 64,
        profile="test",
    )


def _tasks(plan: Plan) -> List[Any]:
    """The plan's items as ``CouncilTask``s, for the worker's own row builder."""
    from kbutillib.domains.ai.council.types import CouncilTask

    return [
        CouncilTask(
            item_id=item.item_id,
            evidence=dict(item.evidence),
            menu=[dict(entry) for entry in item.menu],
        )
        for item in plan.items
    ]


def _seed_run(
    store: Any,
    plan: Plan,
    roster: Roster,
    config: CouncilConfig,
    *,
    query: Any = None,
    roles: Sequence[str] = ("proposer", "critic", "meta"),
    meta_items: Optional[Sequence[int]] = None,
) -> None:
    """Seed a COMPLETED run of *plan* on *roster*, as the worker would write it.

    The call rows -- and crucially their ``prompt_hash`` values -- come from
    ``CouncilWorker._call_row``, the same code the real run uses.  That is
    what makes the reuse assertions below a check on the quote PREDICTING
    the worker, rather than a check on two copies of one hash function.

    Critic and meta prompts embed the proposers' ballots, which a seeded run
    has none of; they are rendered against an empty context here.  The quote
    does not hash them either (it derives critic reuse from the proposers and
    projects meta), so nothing downstream depends on the difference.
    """
    from kbutillib.domains.ai.council.engine.worker import CouncilWorker

    query = query or make_query(plan.query_name, plan.query_version)
    worker = CouncilWorker(store, config, [], "tester")
    tasks = _tasks(plan)
    items = [worker._item_row(query, task, order) for order, task in enumerate(tasks)]

    calls: List[Dict[str, Any]] = []
    for index, task in enumerate(tasks):
        for role, seat in roster.seats():
            if role not in roles:
                continue
            if role == "meta" and meta_items is not None and index not in meta_items:
                continue
            calls.append(
                worker._call_row(query, role, seat, task, items[index], {})
            )
    job = job_row(
        query_name=plan.query_name,
        query_version=plan.query_version,
        roster_fingerprint=roster.fingerprint(),
    )
    job_id = store.create_job(job, items, calls)

    now = utcnow()
    with store.engine.begin() as conn:
        for row in store.get_calls(job_id):
            seat = str(row["seat_name"])
            conn.execute(
                sa.update(sch.calls)
                .where(sch.calls.c.call_id == row["call_id"])
                .values(status="ok", finished_at=now)
            )
            chars = item_prompt_chars(
                items[int(str(row["item_id"]).rsplit("-", 1)[1])]["evidence"],
                items[int(str(row["item_id"]).rsplit("-", 1)[1])]["menu"],
            )
            conn.execute(
                sa.insert(sch.attempts).values(
                    call_id=row["call_id"],
                    attempt_no=1,
                    started_at=now,
                    finished_at=now,
                    outcome_kind="ok",
                    usage={
                        "prompt_tokens": round(SEEDED_RATIO[seat] * chars),
                        "completion_tokens": SEEDED_OUTPUT[seat],
                    },
                )
            )


def _effective_params_resolver(config: CouncilConfig) -> Any:
    """The resolver the facade injects, built the same way here.

    The reuse assertions are only meaningful against the params the WORKER
    will hash with -- the api type's wire spelling (``max_tokens``), not the
    provider-independent one -- so the tests must use the same resolver the
    real entry points use. ``CouncilUtils._effective_params_resolver`` is
    the production copy; this builds the identical thing over a config the
    test owns, without needing the rest of the facade.
    """
    from kbutillib.domains.ai.llm import get_api
    from kbutillib.domains.ai.llm.types import LLMRequest
    from kbutillib.domains.ai.council.types import seat_generation_params

    def _resolve(seat: Any) -> Dict[str, Any]:
        try:
            mapping = dict(config.endpoint(seat.endpoint))
            mapping.setdefault("kind", seat.endpoint_kind)
            api = get_api(str(mapping["kind"]), mapping)
        except Exception:  # noqa: BLE001 - an unbuildable endpoint is not fatal
            return dict(seat_generation_params(seat))
        return dict(
            api.effective_params(
                LLMRequest(
                    model=seat.model,
                    max_output_tokens=seat.max_output_tokens,
                    temperature=seat.temperature,
                    params=dict(seat.extra or {}),
                    timeout_s=seat.timeout_s,
                )
            )
        )

    return _resolve


def _quote(
    store: Any,
    plan: Plan,
    roster: Roster,
    config: CouncilConfig,
    **kwargs: Any,
) -> Any:
    """``quote_plan`` with the token model fitted from *store*."""
    query = kwargs.pop("query", None) or make_query(
        plan.query_name, plan.query_version
    )
    kwargs.setdefault("effective_params", _effective_params_resolver(config))
    return quote_plan(
        plan,
        store=store,
        config=config,
        token_model=fit_token_model(store, seats=roster),
        roster=roster,
        query=query,
        **kwargs,
    )


def _line(quote: Any, name: str) -> Any:
    """One line of the call graph, by name."""
    for line in quote.lines:
        if line.line == name:
            return line
    raise AssertionError(f"no {name!r} line in {[l.line for l in quote.lines]}")


# ---------------------------------------------------------------------------
# The property the whole stage rests on
# ---------------------------------------------------------------------------


class TestSpendsNothing:
    """``quote_plan`` must complete with no network available."""

    @staticmethod
    def _poison(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make ANY real connection attempt raise, at the socket module.

        Deliberately NOT a stubbed HTTP client: a seam that every test
        replaces is a seam no test covers, and the claim here is that
        nothing in the quote's path reaches for a network at all -- not that
        one particular client went uncalled.  Poisoning ``socket`` catches
        httpx, requests, urllib, a raw ``socket()`` and a subprocess'
        parent-side connection alike.
        """

        def _refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "the quote attempted to open a socket; it must spend nothing "
                "and reach nothing"
            )

        monkeypatch.setattr(socket, "socket", _refuse)
        monkeypatch.setattr(socket, "create_connection", _refuse)
        monkeypatch.setattr(socket, "getaddrinfo", _refuse)
        monkeypatch.setattr(socket, "gethostbyname", _refuse)

    def test_the_poison_really_bites(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guard above fails a connection attempt, so the next test means something.

        Without this, a poison that silently did nothing would make
        :meth:`test_quote_plan_completes_with_no_network` pass for the wrong
        reason -- the single most likely way for a "spends nothing" test to
        be worthless.
        """
        self._poison(monkeypatch)
        with pytest.raises(AssertionError, match="attempted to open a socket"):
            socket.create_connection(("example.invalid", 443))
        with pytest.raises(AssertionError, match="attempted to open a socket"):
            socket.socket()

    def test_quote_plan_completes_with_no_network(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)

        self._poison(monkeypatch)
        quote = _quote(store, plan, roster, config)

        assert quote.plan_hash == plan.plan_hash
        assert quote.totals()["point"]["total_tokens"] >= 0
        assert [line.line for line in quote.lines]

    def test_writing_the_quote_needs_no_network_either(
        self, store: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)

        self._poison(monkeypatch)
        quote = _quote(store, plan, roster, config)
        written = quote.write(tmp_path)

        assert written.name == "QUOTE.json"
        assert json.loads(written.read_text())["plan_hash"] == plan.plan_hash


# ---------------------------------------------------------------------------
# Reuse: counted exactly, and every miss attributed
# ---------------------------------------------------------------------------


class TestReuse:
    """Reuse is COUNTED against the store, and each miss says why it missed."""

    def test_an_unseeded_store_reuses_nothing_and_calls_it_new(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)

        for seat in ("proposer_1", "proposer_2", "proposer_3"):
            line = _line(quote, seat)
            assert line.calls_reused == 0
            assert line.calls_paid == len(plan.items)
            assert line.misses["new_item"] == len(plan.items)
            assert line.misses["bench_change"] == 0
            assert line.misses["transport_change"] == 0

    def test_replaying_the_same_plan_on_the_same_bench_reuses_everything(
        self, store: Any
    ) -> None:
        """The whole point of the store: a question already paid for is free."""
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)
        quote = _quote(store, plan, roster, config)

        for seat in ("proposer_1", "proposer_2", "proposer_3"):
            line = _line(quote, seat)
            assert line.calls_reused == len(plan.items), seat
            assert line.calls_paid == 0, seat
            assert line.input_tokens_point == 0, seat
        # And the critic follows its upstream: every proposer reused, and an
        # answered critic call on the same bench exists, so it reuses too.
        assert _line(quote, "critic").calls_reused == len(plan.items)
        assert quote.reuse["items_fully_reused"] == len(plan.items)

    def test_changing_one_seats_model_reduces_reuse_for_that_seat_only(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)

        moved = _roster(proposer_2=_seat("proposer_2", "a-different-model", endpoint="mag"))
        quote = _quote(store, plan, moved, config)

        assert _line(quote, "proposer_2").calls_reused == 0
        assert _line(quote, "proposer_1").calls_reused == len(plan.items)
        assert _line(quote, "proposer_3").calls_reused == len(plan.items)

    def test_a_moved_model_is_attributed_to_the_BENCH_not_to_a_new_item(
        self, store: Any
    ) -> None:
        """A different model is a different experiment: a legitimate re-charge."""
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)

        moved = _roster(proposer_2=_seat("proposer_2", "a-different-model", endpoint="mag"))
        line = _line(_quote(store, plan, moved, config), "proposer_2")

        assert line.misses["bench_change"] == len(plan.items)
        assert line.misses["new_item"] == 0
        assert line.misses["transport_change"] == 0

    def test_a_transport_change_is_attributed_to_TRANSPORT_not_to_a_new_item(
        self, store: Any
    ) -> None:
        """THE CASE THE SPLIT EXISTS FOR.

        Same model, different route.  ``prompt_hash`` keys on
        ``endpoint_kind``, so every stored answer is unreusable -- but the
        BENCH has not moved, ``bench_fingerprint`` is unchanged and the
        corpus is not re-partitioned.  Reported as ``new_item`` this would
        read as a full corpus re-run, and a pure cost optimisation would
        look like a bug.
        """
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)

        # Same canonical_model, reached over a CLI instead of the gateway.
        rerouted_seat = SeatSpec(
            seat_name="proposer_2",
            endpoint="mag",
            endpoint_kind="claude_cli",
            model="gpt-5.6-sol",
            canonical_model="vendor/gpt-5.6-sol",
            max_output_tokens=512,
            temperature=0,
            timeout_s=30,
        )
        rerouted = _roster(proposer_2=rerouted_seat)
        quote = _quote(store, plan, rerouted, config)
        line = _line(quote, "proposer_2")

        assert line.calls_reused == 0
        assert line.misses["transport_change"] == len(plan.items)
        assert line.misses["new_item"] == 0
        assert line.misses["bench_change"] == 0

    def test_a_transport_change_does_not_move_the_partition_key(
        self, store: Any
    ) -> None:
        """The other half of the same fact, and the reason the split matters."""
        from kbutillib.domains.ai.council.types import bench_fingerprint

        plan, roster, config = _plan(), _roster(), _config()
        rerouted = _roster(
            proposer_2=SeatSpec(
                seat_name="proposer_2",
                endpoint="mag",
                endpoint_kind="claude_cli",
                model="gpt-5.6-sol",
                canonical_model="vendor/gpt-5.6-sol",
                max_output_tokens=512,
                temperature=0,
                timeout_s=30,
            )
        )
        assert bench_fingerprint(rerouted) == bench_fingerprint(roster)
        assert rerouted.fingerprint() != roster.fingerprint()

        quote = _quote(store, plan, rerouted, config)
        assert quote.parameter_set["bench_fingerprint"] == bench_fingerprint(roster)
        assert quote.provenance["roster_fingerprint"] == rerouted.fingerprint()

    def test_bumping_the_query_version_reduces_reuse_for_every_seat(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)

        bumped = _plan(items=plan.items, version="2")
        quote = _quote(store, bumped, roster, config)

        for seat in ("proposer_1", "proposer_2", "proposer_3", "critic"):
            line = _line(quote, seat)
            assert line.calls_reused == 0, seat
            assert line.calls_paid == len(plan.items), seat
        assert quote.reuse["items_fully_reused"] == 0

    def test_a_query_version_bump_is_attributed_to_a_NEW_ITEM(
        self, store: Any
    ) -> None:
        """At a new version the prompt genuinely has never been asked.

        Blaming the bench or the transport would accuse a configuration that
        did not move, so the probe narrows on the query identity first.
        """
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)

        quote = _quote(store, _plan(items=plan.items, version="2"), roster, config)
        line = _line(quote, "proposer_1")

        assert line.misses["new_item"] == len(plan.items)
        assert line.misses["bench_change"] == 0
        assert line.misses["transport_change"] == 0

    def test_the_three_miss_counts_are_reported_separately_and_add_up(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)

        mixed = _roster(
            # bench change
            proposer_2=_seat("proposer_2", "a-different-model", endpoint="mag"),
            # transport change
            proposer_3=SeatSpec(
                seat_name="proposer_3",
                endpoint="mag",
                endpoint_kind="codex_cli",
                model="gpt-5.6-luna",
                canonical_model="vendor/gpt-5.6-luna",
                max_output_tokens=512,
                temperature=0,
                timeout_s=30,
            ),
        )
        quote = _quote(store, plan, mixed, config)
        totals = quote.reuse["misses"]

        assert set(totals) == set(MISS_KINDS)
        assert totals["bench_change"] == len(plan.items)
        assert totals["transport_change"] == len(plan.items)
        per_seat = quote.reuse["by_seat"]
        assert per_seat["proposer_2"]["misses"]["bench_change"] == len(plan.items)
        assert per_seat["proposer_3"]["misses"]["transport_change"] == len(plan.items)


# ---------------------------------------------------------------------------
# Replay: the exact lines must reproduce what the run actually spent
# ---------------------------------------------------------------------------


class TestReplayAgainstRecordedActuals:
    """Re-quoting a stored run reproduces the tokens that run really used.

    The fitted ratio is ``sum(prompt_tokens) / sum(prompt_chars)`` over the
    sample, so re-applying it to those same characters must return that same
    total.  That is an arithmetic identity rather than a tolerance, which is
    exactly why it is worth asserting: it pins the estimator (ratio of sums,
    not mean of ratios) and would catch any later change that quietly moved
    which total the quote reproduces.
    """

    @staticmethod
    def _recorded(store: Any) -> Dict[str, Dict[str, int]]:
        """``{seat: {input, output, calls}}`` actually recorded in the store."""
        totals: Dict[str, Dict[str, int]] = {}
        with store.engine.connect() as conn:
            rows = conn.execute(
                sa.select(sch.calls.c.seat_name, sch.attempts.c.usage).select_from(
                    sch.attempts.join(
                        sch.calls, sch.calls.c.call_id == sch.attempts.c.call_id
                    )
                )
            )
            for seat_name, usage in rows:
                entry = totals.setdefault(
                    str(seat_name), {"input": 0, "output": 0, "calls": 0}
                )
                entry["input"] += int(usage["prompt_tokens"])
                entry["output"] += int(usage["completion_tokens"])
                entry["calls"] += 1
        return totals

    def test_the_exact_lines_agree_with_the_recorded_actuals(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)
        recorded = self._recorded(store)

        # Quoted at a NEW query version, so nothing reuses and every call on
        # the exact lines is paid -- i.e. the quote covers the same calls the
        # recorded run made.
        quote = _quote(store, _plan(items=plan.items, version="2"), roster, config)

        for seat in ("proposer_1", "proposer_2", "proposer_3", "critic"):
            line = _line(quote, seat)
            assert line.basis == "exact", seat
            assert line.calls_paid == recorded[seat]["calls"], seat
            assert round(line.input_tokens_point) == recorded[seat]["input"], seat
            assert round(line.output_tokens_point) == recorded[seat]["output"], seat

    def test_a_reused_call_is_quoted_at_zero_tokens(self, store: Any) -> None:
        """Because it is: the worker copies the stored answer."""
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)
        quote = _quote(store, plan, roster, config)

        for seat in ("proposer_1", "proposer_2", "proposer_3"):
            line = _line(quote, seat)
            assert line.input_tokens_point == 0, seat
            assert line.output_tokens_point == 0, seat


# ---------------------------------------------------------------------------
# The call graph
# ---------------------------------------------------------------------------


class TestCallGraph:
    """Five seats is not five identical calls, so the graph is enumerated."""

    def test_three_proposer_calls_per_item_unconditional(self, store: Any) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)

        proposers = [line for line in quote.lines if line.role == "proposer"]
        assert len(proposers) == 3
        for line in proposers:
            assert line.calls_total == len(plan.items)
            assert line.basis == "exact"

    def test_the_critic_line_is_UNCONDITIONAL_one_call_per_item(
        self, store: Any
    ) -> None:
        """``engine/decide.py`` step 5 returns ``need_critic`` with NO guard.

        So a critic call carries no information about whether the proposers
        disagreed, and quoting it as a fraction -- the intuitive reading of
        "the critic audits disputed items" -- would under-quote every run by
        most of a seat.
        """
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)
        line = _line(quote, "critic")

        assert line.calls_total == len(plan.items)
        assert line.basis == "exact"
        assert "unconditional" in line.basis_reason.lower()

    def test_the_critic_is_not_quoted_at_the_meta_fire_rate(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)

        assert _line(quote, "critic").calls_total > _line(quote, "meta").calls_total

    def test_meta_is_projected_at_the_fire_rate_with_its_sample_size(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)
        line = _line(quote, "meta")

        assert line.basis == "projected"
        assert line.calls_total == pytest.approx(len(plan.items) * META_FIRE_RATE)
        published = quote.to_dict()["meta_fire_rate"]
        assert published["rate"] == META_FIRE_RATE
        assert published["n"] == META_FIRE_RATE_N == 19268

    def test_the_fire_rate_is_never_published_without_its_sample_size(
        self, store: Any
    ) -> None:
        """The three withdrawn estimates are exactly this mistake."""
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config, meta_fire_rate=0.5)

        published = quote.to_dict()["meta_fire_rate"]
        assert published["rate"] == 0.5
        assert isinstance(published["n"], int) and published["n"] > 0

    def test_a_paid_repair_line_exists_and_is_the_measured_fraction(
        self, store: Any
    ) -> None:
        """The retry pass is a first-class STAGE, so it is a first-class LINE."""
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)
        repair = _line(quote, "paid_repair")

        assert repair.basis == "projected"
        others = [l for l in quote.lines if l.line != "paid_repair"]
        assert repair.input_tokens_point == pytest.approx(
            sum(l.input_tokens_point for l in others) * PAID_REPAIR_FRACTION
        )

    def test_every_line_carries_an_exact_or_projected_tag(self, store: Any) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)

        for line in quote.to_dict()["call_graph"]:
            assert line["basis"] in ("exact", "projected"), line["line"]
            assert line["basis_reason"]

    def test_the_projected_fraction_is_at_the_top_of_the_output(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        data = _quote(store, plan, roster, config).to_dict()

        headline = data["headline"]
        assert "projected_fraction_of_total" in headline
        # First key of the headline block, so a reader meets it before any
        # number it qualifies.
        assert next(iter(headline)) == "projected_fraction_of_total"
        assert 0.0 < headline["projected_fraction_of_total"] < 1.0

    def test_the_projected_fraction_is_the_projected_lines_share(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)

        projected = sum(
            line.total_tokens_point for line in quote.lines if line.basis == "projected"
        )
        total = sum(line.total_tokens_point for line in quote.lines)
        assert quote.totals()["projected_fraction_of_total"] == pytest.approx(
            projected / total, abs=1e-4
        )


class TestBand:
    """The approval's ceiling derives from the BAND, not the mean."""

    def test_the_upper_band_exceeds_the_point_estimate(self, store: Any) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        _seed_run(store, plan, roster, config)
        totals = _quote(
            store, _plan(items=plan.items, version="2"), roster, config
        ).totals()

        assert totals["upper"]["total_tokens"] > totals["point"]["total_tokens"]

    def test_input_is_not_banded_because_the_prompts_are_known(
        self, store: Any
    ) -> None:
        """Input is 86% of all tokens and the quote rendered every prompt.

        Banding it would widen the ceiling over the one quantity the quote
        is most certain of, and the band would stop meaning "the models may
        answer at length".
        """
        plan, roster, config = _plan(), _roster(), _config()
        totals = _quote(store, plan, roster, config).totals()

        assert totals["upper"]["input_tokens"] == totals["point"]["input_tokens"]
        assert totals["upper"]["output_tokens"] > totals["point"]["output_tokens"]


# ---------------------------------------------------------------------------
# Dollars: only where a rate is configured
# ---------------------------------------------------------------------------


class TestDollars:
    """A gateway with no rate is ``unpriced``, never a list-derived number."""

    #: A rate an operator derived from a real balance delta.
    MEASURED_RATE = {
        "input_per_mtok": 3.0,
        "output_per_mtok": 15.0,
        "provenance": "balance delta over the 2026-10-20 run",
        "is_floor": False,
    }

    def test_a_gateway_with_no_rate_reports_unpriced(self, store: Any) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)

        for gateway in quote.gateways:
            assert gateway["priced"] is False
            assert gateway["dollars_point"] is None
            assert gateway["dollars_upper"] is None
            assert "not substituted" in gateway["unpriced_reason"].lower()

    def test_unpriced_is_a_warning_not_a_silent_zero(self, store: Any) -> None:
        """A silent zero reads as "this run is free", which is the worst answer."""
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)

        assert any("unpriced" in warning for warning in quote.warnings)
        headline = quote.to_dict()["headline"]
        assert headline["dollars_point"] is None
        assert headline["dollars_complete"] is False
        assert set(headline["unpriced_gateways"]) == {ARGO_ACCOUNT, MAG_ACCOUNT}

    def test_a_configured_rate_is_reported_with_its_provenance(
        self, store: Any
    ) -> None:
        plan, roster = _plan(), _roster()
        config = _config(rates={ARGO_ACCOUNT: self.MEASURED_RATE})
        quote = _quote(store, plan, roster, config)

        argo = next(g for g in quote.gateways if g["gateway"] == ARGO_ACCOUNT)
        assert argo["priced"] is True
        assert argo["dollars_point"] == pytest.approx(
            argo["input_tokens"] * 3.0 / 1e6 + argo["output_tokens_point"] * 15.0 / 1e6,
            abs=0.01,
        )
        assert argo["rate_provenance"] == self.MEASURED_RATE["provenance"]
        assert argo["is_floor"] is False

    def test_pricing_one_gateway_does_not_price_the_other(self, store: Any) -> None:
        """The headline stays incomplete while any gateway is unpriced."""
        plan, roster = _plan(), _roster()
        config = _config(rates={ARGO_ACCOUNT: self.MEASURED_RATE})
        quote = _quote(store, plan, roster, config)

        mag = next(g for g in quote.gateways if g["gateway"] == MAG_ACCOUNT)
        assert mag["priced"] is False
        assert quote.to_dict()["headline"]["dollars_complete"] is False
        assert quote.to_dict()["headline"]["unpriced_gateways"] == [MAG_ACCOUNT]

    def test_a_floor_rate_is_labelled_a_floor_and_warned_about(
        self, store: Any
    ) -> None:
        """A list-derived figure is a FLOOR; presenting it as a cost is the
        error this project already made once."""
        plan, roster = _plan(), _roster()
        config = _config(
            rates={
                ARGO_ACCOUNT: {
                    "input_per_mtok": 3.0,
                    "output_per_mtok": 15.0,
                    "provenance": "vendor list page, 2026-10-01",
                    "is_floor": True,
                }
            }
        )
        quote = _quote(store, plan, roster, config)

        argo = next(g for g in quote.gateways if g["gateway"] == ARGO_ACCOUNT)
        assert argo["is_floor"] is True
        assert any("FLOOR" in warning for warning in quote.warnings)

    def test_a_rate_with_no_provenance_is_refused_at_load_time(self) -> None:
        from kbutillib.domains.ai.council.types import ConfigError

        with pytest.raises(ConfigError, match="provenance"):
            _config(rates={ARGO_ACCOUNT: {"input_per_mtok": 1.0, "output_per_mtok": 2.0}})


class TestBalanceDelta:
    """Each completed run calibrates the next run's dollar figure."""

    def test_a_balance_block_exists_for_every_gateway(self, store: Any) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        block = _quote(store, plan, roster, config).balance_delta

        gateways = {entry["gateway"] for entry in block["gateways"]}
        assert gateways == {ARGO_ACCOUNT, MAG_ACCOUNT}

    def test_the_before_and_after_fields_are_present_and_empty(
        self, store: Any
    ) -> None:
        """Emitted EMPTY on purpose: an operator reads the balance and fills
        them in, because no gateway publishes a usage endpoint that answers."""
        plan, roster, config = _plan(), _roster(), _config()
        block = _quote(store, plan, roster, config).balance_delta

        for entry in block["gateways"]:
            for field in (
                "balance_before",
                "balance_after",
                "read_at_before",
                "read_at_after",
                "delta",
                "implied_input_per_mtok",
                "implied_output_per_mtok",
            ):
                assert field in entry, field
                assert entry[field] is None, field
            # The quoted side is filled in, so the delta has something to be
            # divided by when the operator comes back.
            assert entry["quoted_total_tokens_point"] >= 0


# ---------------------------------------------------------------------------
# Wall clock
# ---------------------------------------------------------------------------


class TestWallClock:
    """The slowest queue, never the sum."""

    def test_the_estimate_is_the_slowest_queue_not_the_sum(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        wall = _quote(store, plan, roster, config).wall_clock

        hours = [q["hours"] for q in wall["queues"] if q["hours"] is not None]
        assert len(hours) > 1, "the test bench must span more than one queue"
        assert wall["hours_point"] == pytest.approx(max(hours))
        assert wall["hours_point"] < sum(hours)

    def test_the_binding_queue_is_named(self, store: Any) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        wall = _quote(store, plan, roster, config).wall_clock

        assert wall["binding_queue"] in {q["queue"] for q in wall["queues"]}
        assert wall["binding_limit"] in ("concurrency", "requests_per_minute")

    def test_a_per_minute_ceiling_can_bind_instead_of_concurrency(
        self, store: Any
    ) -> None:
        """MAG enforces BOTH 20 parallel and 120 requests/minute, account-wide.

        Concurrency alone cannot express the second ceiling, so a queue whose
        requests-per-minute limit is the tighter of the two must report that
        as what binds it.
        """
        plan, roster = _plan(), _roster()
        config = _config()
        wall = _quote(store, plan, roster, config).wall_clock

        mag = next(q for q in wall["queues"] if MAG_ACCOUNT in q["gateways"])
        # 6 slots at the fallback 5.30 s is 1.13 calls/s; 120/min is 2.0/s,
        # so concurrency is the tighter one here -- and the quote says which.
        assert mag["requests_per_minute"] == 120
        assert mag["binding_limit"] == "concurrency"
        assert mag["calls_per_second"] == pytest.approx(
            min(6 / mag["service_time_s"], 2.0), abs=0.01
        )

    def test_declared_queues_are_used_when_present(self, store: Any) -> None:
        plan, roster = _plan(), _roster()
        config = _config(
            queues={
                "mag": {"seats": ["proposer_2", "proposer_3"], "concurrency": 3, "workers": 2},
                "argo": {"seats": ["proposer_1", "critic", "meta"], "concurrency": 24},
            }
        )
        wall = _quote(store, plan, roster, config).wall_clock

        names = {q["queue"] for q in wall["queues"]}
        assert names == {"mag", "argo"}
        assert all(q["declared"] for q in wall["queues"])
        # 3 concurrency x 2 workers = the 6 the measured configuration used.
        assert next(q for q in wall["queues"] if q["queue"] == "mag")["concurrency"] == 6

    def test_undeclared_queues_raise_the_account_wide_warning(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)

        assert any("ACCOUNT-WIDE" in warning for warning in quote.warnings)

    def test_the_upper_band_is_longer_than_the_point_estimate(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        wall = _quote(store, plan, roster, config).wall_clock

        assert wall["hours_upper"] > wall["hours_point"]


# ---------------------------------------------------------------------------
# The plan, and the hash an approval binds to
# ---------------------------------------------------------------------------


class TestPlan:
    """A quote binds to one plan, by hash."""

    @staticmethod
    def _payload(**overrides: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "query": "engine_test@1",
            "mapping_hash": "m" * 64,
            "profile": "test",
            "items": [
                {
                    "item_id": item.item_id,
                    "evidence": dict(item.evidence),
                    "menu": [dict(entry) for entry in item.menu],
                    "query_id": item.query_id,
                }
                for item in _plan_items(3)
            ],
        }
        payload.update(overrides)
        return payload

    def test_a_plan_loads_from_a_run_directory(self, tmp_path: Any) -> None:
        (tmp_path / "PLAN.json").write_text(json.dumps(self._payload()))
        plan = load_plan(tmp_path)

        assert plan.query_ref == "engine_test@1"
        assert len(plan.items) == 3
        assert plan.run_dir == str(tmp_path)

    def test_a_plan_loads_from_a_jsonl_items_file(self, tmp_path: Any) -> None:
        """47,341 functions at 6.9 kB each is a third of a gigabyte; nobody
        wants that inside a file an operator is expected to read."""
        payload = self._payload()
        items = payload.pop("items")
        (tmp_path / "items.jsonl").write_text(
            "\n".join(json.dumps(item) for item in items)
        )
        payload["items_file"] = "items.jsonl"
        (tmp_path / "PLAN.json").write_text(json.dumps(payload))

        plan = load_plan(tmp_path)
        assert [item.item_id for item in plan.items] == [i["item_id"] for i in items]

    def test_inline_and_file_items_together_are_refused(self, tmp_path: Any) -> None:
        payload = self._payload(items_file="items.jsonl")
        (tmp_path / "PLAN.json").write_text(json.dumps(payload))

        with pytest.raises(ValueError, match="BOTH"):
            load_plan(tmp_path)

    def test_a_query_with_no_version_is_refused(self, tmp_path: Any) -> None:
        """A version is part of the parameter set AND of every prompt hash."""
        (tmp_path / "PLAN.json").write_text(
            json.dumps(self._payload(query="engine_test"))
        )
        with pytest.raises(ValueError, match="names no VERSION"):
            load_plan(tmp_path)

    def test_a_repeated_item_id_is_refused(self, tmp_path: Any) -> None:
        payload = self._payload()
        payload["items"].append(dict(payload["items"][0]))
        (tmp_path / "PLAN.json").write_text(json.dumps(payload))

        with pytest.raises(ValueError, match="repeats item_id"):
            load_plan(tmp_path)

    def test_the_plan_hash_ignores_where_the_plan_was_written(
        self, tmp_path: Any
    ) -> None:
        """Or an approval could not bind across a re-plan into a new directory."""
        first, second = tmp_path / "a", tmp_path / "b"
        for directory in (first, second):
            directory.mkdir()
            (directory / "PLAN.json").write_text(json.dumps(self._payload()))

        assert load_plan(first).plan_hash == load_plan(second).plan_hash

    def test_the_plan_hash_moves_when_an_item_moves(self, tmp_path: Any) -> None:
        payload = self._payload()
        original = Plan(
            query_name="engine_test",
            query_version="1",
            items=tuple(_plan_items(3)),
            mapping_hash="m" * 64,
            profile="test",
        )
        payload["items"][0]["evidence"]["text"] = "a different function"
        (tmp_path / "PLAN.json").write_text(json.dumps(payload))

        assert load_plan(tmp_path).plan_hash != original.plan_hash

    def test_an_edited_plan_whose_declared_hash_no_longer_matches_is_refused(
        self, tmp_path: Any
    ) -> None:
        payload = self._payload(plan_hash="0" * 64)
        (tmp_path / "PLAN.json").write_text(json.dumps(payload))

        with pytest.raises(ValueError, match="edited since it was written"):
            load_plan(tmp_path)

    def test_the_quote_carries_the_plan_hash_it_was_computed_against(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)

        assert quote.plan_hash == plan.plan_hash
        assert quote.to_dict()["plan_hash"] == plan.plan_hash

    def test_an_item_with_no_query_id_is_warned_about(self, store: Any) -> None:
        """query_id is the clearinghouse join key. The council's own
        entity_hash matched 0 of 19,268 clearinghouse hashes."""
        items = list(_plan_items(3))
        items[0] = PlanItem(
            item_id=items[0].item_id,
            evidence=items[0].evidence,
            menu=items[0].menu,
            query_id=None,
        )
        quote = _quote(store, _plan(items=items), _roster(), _config())

        assert any("query_id" in warning for warning in quote.warnings)
        assert quote.plan["items_with_query_id"] == 2

    def test_quoting_one_spec_and_naming_another_is_refused(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(version="1"), _roster(), _config()

        with pytest.raises(ValueError, match="would make every reuse count"):
            _quote(
                store,
                plan,
                roster,
                config,
                query=make_query("engine_test", "9"),
            )


class TestParameterSet:
    """``{query, bench_fingerprint, mapping_hash}`` and nothing else."""

    def test_the_parameter_set_is_model_only(self, store: Any) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        parameter_set = _quote(store, plan, roster, config).parameter_set

        assert set(parameter_set) == {"query", "bench_fingerprint", "mapping_hash"}
        assert "roster_fingerprint" not in parameter_set

    def test_the_roster_fingerprint_is_recorded_as_provenance(
        self, store: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)

        assert quote.provenance["roster_fingerprint"] == roster.fingerprint()
        assert set(quote.provenance["route_fingerprints"]) == {
            seat.seat_name for _role, seat in roster.seats()
        }

    def test_the_parameter_set_hash_is_a_hash_of_exactly_that_set(
        self, store: Any
    ) -> None:
        from kbutillib.domains.ai.council.types import canonical_sha256

        plan, roster, config = _plan(), _roster(), _config()
        quote = _quote(store, plan, roster, config)

        assert quote.parameter_set_hash == canonical_sha256(dict(quote.parameter_set))


class TestWrittenFile:
    """``QUOTE.json`` is the artifact stage 3 approves."""

    def test_writing_to_a_directory_produces_QUOTE_json(
        self, store: Any, tmp_path: Any
    ) -> None:
        plan, roster, config = _plan(), _roster(), _config()
        written = _quote(store, plan, roster, config).write(tmp_path)

        assert written == tmp_path / "QUOTE.json"
        data = json.loads(written.read_text())
        for key in (
            "plan_hash",
            "parameter_set",
            "headline",
            "call_graph",
            "reuse",
            "wall_clock",
            "gateways",
            "balance_delta",
            "token_model",
        ):
            assert key in data, key

    def test_the_written_file_is_stable_json(self, store: Any, tmp_path: Any) -> None:
        """Sorted and indented, so two quotes can be diffed by a human."""
        plan, roster, config = _plan(), _roster(), _config()
        text = _quote(store, plan, roster, config).write(tmp_path).read_text()

        assert text.endswith("\n")
        assert json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n" == text
