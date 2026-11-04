#!/usr/bin/env python3
"""The ONE COMMAND that runs the `anthropic` api type's LIVE acceptance rehearsal.

    export ANTHROPIC_API_KEY=...            # never put it on the command line
    python scripts/council/anthropic_acceptance_rehearsal.py \
        --work-root /scratch/$USER/anthropic-rehearsal \
        --confirm-spend

SPENDS REAL MONEY, A LITTLE. It makes one small real call per seated model
through the shipped council engine, into an ISOLATED council store it creates
under ``--work-root``, plus one more call against a deliberately tiny budget.
The estimated ceiling is printed BEFORE anything is sent, and nothing is sent
without ``--confirm-spend`` (or ``KBU_ANTHROPIC_REHEARSAL_CONFIRM=yes``).

IT NEVER FALLS BACK TO A FIXTURE. No key -> it refuses (exit 2). A rehearsal
that quietly passed offline would report success for a check that did not
happen, which is worse than no rehearsal: the defect this guards against -- a
backend reporting zero token spend because the provider names its counts
differently -- is invisible to any test built on a fixture written from the
same documentation as the code.

THE SIX CHECKS (each reported ASSERTED / FAILED / NOT ASSERTED, with the
observed value quoted):

  1. every stored call row carries non-zero prompt_tokens AND completion_tokens
     -- read OUT OF THE STORE (``calls.fresh_usage``), not off the in-memory
     result and not from a log line;
  2. ``council.engine.worker._token_total`` on that stored usage is non-zero
     (the real function, imported);
  3. the ``anthropic-ratelimit-requests-limit`` header observed on THIS
     process's calls, compared with the configured max_requests_per_minute
     (a mismatch is a FINDING, not a failure);
  4. the free offline preflight reports ``ready`` for every endpoint;
  5. a quote over the rehearsal roster has one gateway row per rate-limit
     bucket, by_model figures summing to each gateway total, and ``unpriced``
     / ``partial`` (never a number) where no rate is configured;
  6. an endpoint with a tiny max_total_tokens halts ``budget_exhausted`` after
     the budget is passed, and the halt message quotes spend and budget.

NOT COVERED, DELIBERATELY: "an endpoint whose provider reports no usage object
halts budget_usage_unknown". There is no supported way to make the real API
omit its usage object, so that criterion is the offline engine test
``tests/domains/ai/council/test_anthropic_engine_budget.py``.

Needs no GPU, pod or lakehouse transport: a key, network access to
api.anthropic.com, ``pip install 'kbutillib[anthropic,council]'`` (Python >=
3.10), and a scratch directory.

EXIT CODES. ``0`` only when no check FAILED. ``1`` when a check failed. ``2``
when the rehearsal refused to start (no key, no SDK, no confirmation, a
non-empty work root). ``--strict`` also exits 1 on a NOT ASSERTED check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:  # run from a checkout without install
    sys.path.insert(0, str(REPO_ROOT / "src"))

from kbutillib.domains.kbase.berdl.council_rehearsal import (  # noqa: E402
    ASSERTED,
    FAILED,
    NOT_ASSERTED,
    Step,
    _fmt,
)

#: The served model ids this rehearsal may seat -- exactly the ones confirmed
#: during design (council.config.DEFAULT_CANONICAL_BY_KIND["anthropic"]).
ALLOWED_MODELS = (
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-opus-4-8",
)

DEFAULT_PROPOSERS = ("claude-haiku-4-5", "claude-sonnet-4-6")
DEFAULT_CRITIC = "claude-sonnet-5"
DEFAULT_META = "claude-opus-5"

#: The tiny budget check 6 runs against. One real reply is far more than this.
TINY_BUDGET_TOKENS = 1

#: ASSUMED price CEILINGS for the pre-spend estimate, in USD per million
#: tokens. NOT prices: deliberately pessimistic upper bounds so the printed
#: figure is a ceiling the run will not exceed, not a quote. Override with
#: --assume-input-per-mtok / --assume-output-per-mtok.
ASSUMED_INPUT_PER_MTOK = 30.0
ASSUMED_OUTPUT_PER_MTOK = 150.0
#: Generous upper bound on one rendered prompt (the packaged EC example query).
ASSUMED_INPUT_TOKENS_PER_CALL = 3000

#: The query type every call asks: the worked example that ships with the
#: package, so the rehearsal registers nothing a user wrote.
EXAMPLE_QUERY = (
    REPO_ROOT / "src/kbutillib/domains/ai/council/queries/examples/custom_example.yaml"
)

CONFIRM_ENV = "KBU_ANTHROPIC_REHEARSAL_CONFIRM"
WALLET = "anthropic-rehearsal-wallet"
BUDGET_ENDPOINT = "rehearsal-budget"


class Refused(Exception):
    """The rehearsal will not start; the message says why."""


# ---------------------------------------------------------------------------
# arguments, refusals, estimate
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--work-root", required=True,
                        help="NEW or EMPTY scratch directory; the isolated store lives here")
    parser.add_argument("--api-key-env", default="ANTHROPIC_API_KEY",
                        help="variable holding the key (default ANTHROPIC_API_KEY)")
    parser.add_argument("--proposers", default=",".join(DEFAULT_PROPOSERS))
    parser.add_argument("--critic", default=DEFAULT_CRITIC)
    parser.add_argument("--meta", default=DEFAULT_META)
    parser.add_argument("--max-output-tokens", type=int, default=2000,
                        help="per-seat ceiling; must cover thinking plus the answer")
    parser.add_argument("--rpm", action="append", default=[], metavar="BUCKET=RPM",
                        help="configured max_requests_per_minute per bucket, compared in check 3")
    parser.add_argument("--rates", default="",
                        help="optional YAML/JSON file of `rates` entries for check 5")
    parser.add_argument("--confirm-spend", action="store_true",
                        help=f"spend real money (or set {CONFIRM_ENV}=yes)")
    parser.add_argument("--assume-input-per-mtok", type=float, default=ASSUMED_INPUT_PER_MTOK)
    parser.add_argument("--assume-output-per-mtok", type=float, default=ASSUMED_OUTPUT_PER_MTOK)
    stamp = date.today().isoformat()
    parser.add_argument("--report",
                        default=str(REPO_ROOT / f"agent-io/reports/{stamp}-anthropic-acceptance-rehearsal.txt"))
    parser.add_argument("--json",
                        default=str(REPO_ROOT / f"agent-io/reports/{stamp}-anthropic-acceptance-rehearsal.json"))
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero on a NOT ASSERTED check as well as a FAILED one")
    return parser.parse_args(argv)


def seated_models(args: argparse.Namespace) -> Dict[str, Any]:
    proposers = [m.strip() for m in str(args.proposers).split(",") if m.strip()]
    plan = {"proposers": proposers, "critic": args.critic.strip(), "meta": args.meta.strip()}
    every = proposers + [plan["critic"], plan["meta"]]
    unknown = [m for m in every if m not in ALLOWED_MODELS]
    if unknown:
        raise Refused(
            f"model id(s) {unknown} are not among the design-confirmed ids "
            f"{list(ALLOWED_MODELS)}; never guess an id (GET /v1/models lists the live ones)"
        )
    if len(proposers) < 2 or len(set(every)) != len(every):
        raise Refused("seat at least two proposers, and a different model in every seat "
                      "(a council refuses one model in two seats)")
    return plan


def estimate(args: argparse.Namespace, plan: Mapping[str, Any]) -> Dict[str, Any]:
    """An UPPER BOUND on what the run can spend, from assumed price ceilings."""
    calls = len(plan["proposers"]) + 2 + 1  # proposers, critic, meta (may not fire), budget call
    tokens_in = calls * ASSUMED_INPUT_TOKENS_PER_CALL
    tokens_out = calls * int(args.max_output_tokens)
    dollars = (tokens_in * args.assume_input_per_mtok + tokens_out * args.assume_output_per_mtok) / 1e6
    return {
        "calls_at_most": calls,
        "input_tokens_at_most": tokens_in,
        "output_tokens_at_most": tokens_out,
        "assumed_input_per_mtok": args.assume_input_per_mtok,
        "assumed_output_per_mtok": args.assume_output_per_mtok,
        "dollars_ceiling": round(dollars, 2),
        "basis": "upper bound from ASSUMED price ceilings, not a price list",
    }


def preconditions(args: argparse.Namespace) -> Tuple[Dict[str, Any], Path]:
    """Every refusal that must happen BEFORE a byte is sent."""
    if not (os.environ.get(args.api_key_env) or "").strip():
        raise Refused(
            f"${args.api_key_env} is unset or empty. This rehearsal makes REAL calls "
            "and never substitutes a fixture or a recorded response; export the key "
            f"(`export {args.api_key_env}=...`) and run it again."
        )
    try:
        import anthropic  # noqa: F401
    except ImportError as exc:
        raise Refused(
            "the anthropic SDK is not installed: pip install 'kbutillib[anthropic,council]' "
            f"(requires Python >= 3.10; this is {platform.python_version()})"
        ) from exc
    plan = seated_models(args)
    work_root = Path(args.work_root).expanduser()
    if work_root.exists() and any(work_root.iterdir()):
        raise Refused(f"--work-root {work_root} is not empty; the store must be isolated and new")
    return plan, work_root


# ---------------------------------------------------------------------------
# the rehearsal configuration
# ---------------------------------------------------------------------------


def _rpm_table(args: argparse.Namespace) -> Dict[str, float]:
    table: Dict[str, float] = {}
    for entry in args.rpm:
        bucket, _, value = str(entry).partition("=")
        table[bucket.strip()] = float(value)
    return table


def build_config(args: argparse.Namespace, plan: Mapping[str, Any], store_url: str) -> Dict[str, Any]:
    from kbutillib.domains.ai.council.config import (
        ANTHROPIC_RATE_LIMIT_BUCKETS,
        DEFAULT_CANONICAL_BY_KIND,
    )

    canonical = DEFAULT_CANONICAL_BY_KIND["anthropic"]
    rpm = _rpm_table(args)
    endpoints: Dict[str, Any] = {}
    models: Dict[str, Any] = {}
    every = list(plan["proposers"]) + [plan["critic"], plan["meta"]]
    for served in every:
        bucket = ANTHROPIC_RATE_LIMIT_BUCKETS[canonical[served]]
        endpoints.setdefault(bucket, {
            "kind": "anthropic",
            "api_key_env": args.api_key_env,
            "billing": "api",
            "account": bucket,
            "billing_wallet": WALLET,
            "max_concurrency": 2,
            **({"max_requests_per_minute": rpm[bucket]} if bucket in rpm else {}),
        })
        models[served] = {"endpoint": bucket, "served_name": served,
                          "params": {"max_output_tokens": int(args.max_output_tokens)}}
    first = plan["proposers"][0]
    first_bucket = ANTHROPIC_RATE_LIMIT_BUCKETS[canonical[first]]
    # Check 6: same bucket (so account == bucket), one call at a time, and a
    # budget a single real reply overshoots.
    endpoints[BUDGET_ENDPOINT] = {
        "kind": "anthropic", "api_key_env": args.api_key_env, "billing": "api",
        "account": first_bucket, "billing_wallet": WALLET, "max_concurrency": 1,
        "max_total_tokens": TINY_BUDGET_TOKENS,
    }
    models[f"{first}@budget"] = {"endpoint": BUDGET_ENDPOINT, "served_name": first,
                                 "params": {"max_output_tokens": int(args.max_output_tokens)}}
    raw: Dict[str, Any] = {
        "endpoints": endpoints,
        "models": models,
        "profiles": {
            "rehearsal": {"proposers": list(plan["proposers"]), "critic": plan["critic"],
                          "meta": plan["meta"]},
            "budget": {"proposers": [f"{first}@budget"] + list(plan["proposers"][1:]),
                       "critic": plan["critic"], "meta": plan["meta"]},
        },
        "default_profile": "rehearsal",
        "store": store_url,
    }
    if args.rates:
        import yaml

        raw["rates"] = yaml.safe_load(Path(args.rates).expanduser().read_text(encoding="utf-8")) or {}
    return raw


def _item(item_id: str, annotation: str, ec: str, label: str) -> Dict[str, Any]:
    return {
        "item_id": item_id,
        "evidence": {"annotation": annotation, "organism": "Escherichia coli K-12"},
        "menu": [
            {"id": ec, "label": label, "rank": 1, "score": 0.91,
             "description": label, "definition_source": "rehearsal"},
            {"id": "2.7.1.1", "label": "hexokinase", "rank": 2, "score": 0.12,
             "description": "hexokinase", "definition_source": "rehearsal"},
        ],
    }


RUN_ITEMS = [_item("rehearsal-1", "alcohol dehydrogenase AdhE", "1.1.1.1", "alcohol dehydrogenase")]
BUDGET_ITEMS = [
    _item("budget-1", "glucose-6-phosphate isomerase Pgi", "5.3.1.9", "glucose-6-phosphate isomerase"),
    _item("budget-2", "citrate synthase GltA", "2.3.3.16", "citrate synthase"),
]
QUOTE_ITEMS = [
    _item("quote-1", "malate dehydrogenase Mdh", "1.1.1.37", "malate dehydrogenase"),
    _item("quote-2", "fumarase FumA", "4.2.1.2", "fumarate hydratase"),
]


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace, plan: Mapping[str, Any], work_root: Path) -> Dict[str, Any]:
    from kbutillib.domains.ai import llm
    from kbutillib.domains.ai.council.engine.worker import CouncilWorker, _token_total
    from kbutillib.domains.ai.council.quote import Plan, PlanItem, fit_token_model, quote_plan
    from kbutillib.domains.ai.council_utils import CouncilUtils

    work_root.mkdir(parents=True, exist_ok=True)
    store_url = f"sqlite:///{work_root / 'council.sqlite'}"
    raw = build_config(args, plan, store_url)
    (work_root / "rehearsal_config.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")

    util = CouncilUtils(council_config=raw, store_url=store_url)
    config = util.config()
    util.db_init()
    store = util.store()
    spec = util.register_query(EXAMPLE_QUERY)
    roster = config.roster("rehearsal")
    budget_roster = config.roster("budget")
    steps: Dict[str, Step] = {
        n: Step(n, name) for n, name in (
            ("1", "stored call rows carry non-zero prompt_tokens and completion_tokens"),
            ("2", "the engine's own _token_total on the stored usage is non-zero"),
            ("3", "anthropic-ratelimit-requests-limit observed vs configured max_requests_per_minute"),
            ("4", "free offline preflight reports ready for every endpoint"),
            ("5", "quote: one gateway row per rate-limit bucket, by_model sums to the total"),
            ("6", "a tiny max_total_tokens halts budget_exhausted, quoting spend and budget"),
        )
    }
    timings: Dict[str, float] = {}

    # Backends: built once and KEPT, so check 3 can read the header the calls
    # in THIS process observed (a backend attribute is process-local; a queue
    # worker in another process would never see it).
    budget_and_main = list(roster.seats()) + list(budget_roster.seats())
    backends = util.build_backends(_RosterView(budget_and_main))
    try:
        # -- 4: preflight (free, before anything is spent) ------------------
        started = time.monotonic()
        for endpoint, api in sorted(backends.items()):
            report = await llm.preflight_api(api)
            steps["4"].check(f"preflight({endpoint}) is ready", "ready", report.get("state"),
                             evidence=str(report.get("detail") or ""))
            listing = await llm.models_api(api)
            ids = sorted(str(e.get("id")) for e in listing if isinstance(e, Mapping))
            served = sorted({s.model for _r, s in budget_and_main if s.endpoint == endpoint})
            missing = [m for m in served if m not in ids]
            steps["4"].note(f"GET /v1/models via {endpoint}: {len(ids)} ids")
            if missing:
                steps["4"].find(
                    f"{endpoint}: served_name(s) {missing} are not literally in the model "
                    "listing, so `kbu council doctor`/submit would report these seats "
                    f"unavailable (listing sample: {ids[:8]})"
                )
        timings["4"] = time.monotonic() - started

        # -- the live run: one real call per seat that fires ----------------
        started = time.monotonic()
        main_backends = {e: b for e, b in backends.items() if e != BUDGET_ENDPOINT}
        worker = CouncilWorker(store, config, main_backends, util.user())
        job_id = await worker.asubmit(spec, util._as_tasks(RUN_ITEMS), roster,
                                      label="anthropic-acceptance-rehearsal",
                                      profile_name="rehearsal")
        summary = await worker.work(job_id=job_id, until_idle=True)
        rows = list(store.get_calls(job_id))
        timings["run"] = time.monotonic() - started

        # -- 1 & 2: read usage OUT OF THE STORE ------------------------------
        fired = {}
        for row in rows:
            label = f"{row['role']} {row['seat_name']} ({row.get('served_model')} via {row['endpoint']})"
            usage = row.get("fresh_usage")
            if row.get("status") != "ok":
                steps["1"].check(f"{label}: call completed", "ok", row.get("status"),
                                 evidence=json.dumps(row.get("error"), default=str)[:400])
                continue
            fired[row["seat_name"]] = row
            prompt = int((usage or {}).get("prompt_tokens") or 0)
            completion = int((usage or {}).get("completion_tokens") or 0)
            steps["1"].check(f"{label}: stored prompt_tokens > 0 and completion_tokens > 0",
                             "both > 0", usage, ok=prompt > 0 and completion > 0)
            total = _token_total(usage)
            steps["2"].check(f"{label}: _token_total(stored usage) > 0", "> 0", total, ok=total > 0)
        for role, seat in roster.seats():
            if seat.seat_name not in fired and not any(r["seat_name"] == seat.seat_name for r in rows):
                note = (f"{role} {seat.seat_name} ({seat.model}) made no call: "
                        + ("meta fires only on a split decision" if role == "meta" else "not reached"))
                steps["1"].note(note)
                steps["2"].note(note)
        steps["1"].note(f"job {job_id}; worker summary: {json.dumps(summary, default=str)[:300]}")

        # -- 3: the header, from THIS process's backends ---------------------
        configured = {e: config.endpoint(e).get("max_requests_per_minute") for e in main_backends}
        called = {row["endpoint"] for row in rows if row.get("status") == "ok"}
        for endpoint, api in sorted(main_backends.items()):
            if endpoint not in called:
                steps["3"].note(f"{endpoint}: made no completed call in this run, so no header to read")
                continue
            observed = dict(getattr(api, "last_ratelimit", {}) or {})
            limit = observed.get("anthropic-ratelimit-requests-limit")
            steps["3"].check(f"{endpoint}: anthropic-ratelimit-requests-limit observed",
                             "present", limit, ok=limit is not None,
                             evidence=json.dumps(observed, sort_keys=True))
            if limit is not None and configured.get(endpoint) is not None:
                if float(limit) != float(configured[endpoint]):
                    steps["3"].find(
                        f"{endpoint}: header says {limit} requests/min, config says "
                        f"{configured[endpoint]} -- the header reports the MOST RESTRICTIVE "
                        "limit in effect (a workspace limit can shadow the model's)"
                    )
            elif limit is not None:
                steps["3"].find(f"{endpoint}: header says {limit} requests/min; no "
                                "max_requests_per_minute configured (pass --rpm BUCKET=N)")

        # -- 5: a real quote over NEW items, fitted from this store ----------
        started = time.monotonic()
        try:
            qplan = Plan(
                query_name=spec.name, query_version=str(spec.version),
                items=tuple(PlanItem(item_id=i["item_id"], evidence=i["evidence"],
                                     menu=i["menu"], query_id=i["item_id"]) for i in QUOTE_ITEMS),
                mapping_hash="rehearsal", profile="rehearsal",
            )
            quote = quote_plan(qplan, store=store, config=config,
                               token_model=fit_token_model(store, seats=roster),
                               roster=roster, query=spec)
            published = quote.to_dict()
            buckets = sorted({config.endpoint(s.endpoint)["account"] for _r, s in roster.seats()})
            gateways = {g["gateway"]: g for g in published["gateways"]}
            steps["5"].check("one gateway row per rate-limit bucket", buckets, sorted(gateways))
            for name, gw in sorted(gateways.items()):
                by_model = gw.get("by_model") or []
                if gw.get("priced") is True:
                    total = round(sum(float(m["dollars_point"]) for m in by_model), 2)
                    steps["5"].check(f"{name}: by_model dollars sum to the gateway total",
                                     gw.get("dollars_point"), total)
                else:
                    state = "partial" if gw.get("partially_priced") else "unpriced"
                    steps["5"].check(f"{name}: no rate -> reported {state}, no dollar figure",
                                     None, gw.get("dollars_point"),
                                     evidence=str(gw.get("unpriced_reason") or ""))
            steps["5"].note(f"headline: {json.dumps(published['headline'], default=str)[:400]}")
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            steps["5"].fail(f"quote_plan raised {type(exc).__name__}: {exc}")
            steps["5"].note(traceback.format_exc()[-1500:])
        timings["5"] = time.monotonic() - started

        # -- 6: the budget bites ---------------------------------------------
        started = time.monotonic()
        submitter = CouncilWorker(store, config, backends, util.user())
        budget_job = await submitter.asubmit(spec, util._as_tasks(BUDGET_ITEMS), budget_roster,
                                             label="anthropic-rehearsal-budget",
                                             profile_name="budget")
        # A worker serves ONLY the endpoints it is handed: this one spends
        # nothing anywhere but the budget endpoint.
        budget_worker = CouncilWorker(store, config, {BUDGET_ENDPOINT: backends[BUDGET_ENDPOINT]},
                                      util.user())
        await budget_worker.work(job_id=budget_job, until_idle=True)
        state = store.get_endpoint_state(BUDGET_ENDPOINT)
        spent = int(state.get("epoch_tokens") or 0)
        message = str(state.get("halt_message") or "")
        steps["6"].check("halt_kind", "budget_exhausted", state.get("halt_kind"))
        steps["6"].check("spend passed the budget", f"> {TINY_BUDGET_TOKENS}", spent,
                         ok=spent > TINY_BUDGET_TOKENS)
        steps["6"].check("halt message quotes the spend and the budget",
                         [f"spent {spent}", f"{TINY_BUDGET_TOKENS}-token budget"], message,
                         ok=f"spent {spent}" in message and f"{TINY_BUDGET_TOKENS}-token budget" in message)
        budget_rows = [r for r in store.get_calls(budget_job) if r["endpoint"] == BUDGET_ENDPOINT]
        completed = [r for r in budget_rows if r.get("status") == "ok"]
        steps["6"].check("the halt stopped the NEXT call on that endpoint", 1, len(completed),
                         evidence=f"{len(budget_rows)} call row(s) on {BUDGET_ENDPOINT}")
        timings["6"] = time.monotonic() - started
    finally:
        await util._aclose_backends(backends)

    for number, seconds in timings.items():
        if number in steps:
            steps[number].seconds = seconds
    return {"steps": [steps[n].as_dict() for n in sorted(steps)], "job_ids": [job_id, budget_job],
            "store_url": store_url}


class _RosterView:
    """``build_backends`` wants something with ``.seats()``; two rosters' worth."""

    def __init__(self, seats: List[Any]) -> None:
        self._seats = seats

    def seats(self) -> List[Any]:
        return list(self._seats)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


LIVE_ACCEPTANCE = {
    "state": "LIVE-ACCEPTANCE-PENDING until a human runs this script and reads the report",
    "pending_criteria": ["1", "2", "3", "6"],
    "operator_supplies": [
        "a real ANTHROPIC_API_KEY (exported, never in a file or on the command line)",
        "network access to api.anthropic.com and the models seated",
        "an isolated, new council store (--work-root)",
        "an explicit spending cap (a console spend limit) and --confirm-spend",
    ],
    "offline_only": (
        "criterion 4 (no usage object -> budget_usage_unknown) cannot be produced live; "
        "it is tests/domains/ai/council/test_anthropic_engine_budget.py"
    ),
}


def render(report: Mapping[str, Any]) -> str:
    out: List[str] = []
    summary = report.get("summary") or {}
    out.append("=" * 72)
    out.append("ANTHROPIC API TYPE -- LIVE ACCEPTANCE REHEARSAL")
    out.append("=" * 72)
    for key in ("started_at", "finished_at", "seconds", "host", "interpreter", "store_url",
                "work_root"):
        if report.get(key) is not None:
            out.append(f"{key:<16} {_fmt(report[key])}")
    est = report.get("estimate") or {}
    out.append(f"{'estimate':<16} <= ${est.get('dollars_ceiling')} ({est.get('basis')})")
    out.append("")
    out.append(
        f"VERDICT: {summary.get('verdict', 'unknown')}  "
        f"({summary.get(ASSERTED, 0)} asserted, {summary.get(FAILED, 0)} failed, "
        f"{summary.get(NOT_ASSERTED, 0)} not asserted; "
        f"{summary.get('assertions_held', 0)}/{summary.get('assertions_total', 0)} assertions held)"
    )
    out.append("")
    for step in report.get("steps") or ():
        out.append("-" * 72)
        out.append(f"CHECK {step['number']}  {step['name']}\n  STATE: {step['state']}   ({step['seconds']}s)")
        if step.get("reason"):
            out.append(f"  REASON: {step['reason']}")
        for a in step.get("assertions") or ():
            out.append(f"  [{'ok  ' if a['ok'] else 'FAIL'}] {a['claim']}")
            out.append(f"         expected={_fmt(a['expected'])}  measured={_fmt(a['measured'])}")
            if a.get("evidence"):
                out.append(f"         evidence: {a['evidence']}")
        for note in step.get("notes") or ():
            out.append(f"  note: {note}")
        for finding in step.get("findings") or ():
            out.append(f"  FINDING: {finding}")
    out.append("-" * 72)
    live = report.get("live_acceptance") or {}
    out.append(f"ACCEPTANCE: {live.get('state')}")
    out.append(f"  live-only criteria: {', '.join(live.get('pending_criteria') or [])}")
    for item in live.get("operator_supplies") or ():
        out.append(f"  - operator supplies: {item}")
    out.append(f"  offline only: {live.get('offline_only')}")
    out.append("=" * 72)
    return "\n".join(out)


def _summarise(steps: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    tally = {ASSERTED: 0, FAILED: 0, NOT_ASSERTED: 0}
    held = total = 0
    for step in steps:
        tally[step["state"]] = tally.get(step["state"], 0) + 1
        total += len(step["assertions"])
        held += sum(1 for a in step["assertions"] if a["ok"])
    verdict = ("FAILED" if tally[FAILED] else "PARTIAL -- see NOT ASSERTED checks"
               if tally[NOT_ASSERTED] else "ASSERTED")
    return {**tally, "assertions_held": held, "assertions_total": total, "verdict": verdict}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        plan, work_root = preconditions(args)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    est = estimate(args, plan)
    print(
        f"seats: proposers={plan['proposers']} critic={plan['critic']} meta={plan['meta']} "
        f"(+1 budget call on {plan['proposers'][0]})\n"
        f"ESTIMATED COST CEILING: <= ${est['dollars_ceiling']} for at most {est['calls_at_most']} "
        f"calls ({est['basis']}: ${est['assumed_input_per_mtok']}/MTok in, "
        f"${est['assumed_output_per_mtok']}/MTok out)",
        file=sys.stderr,
    )
    confirmed = args.confirm_spend or (os.environ.get(CONFIRM_ENV) or "").strip().lower() == "yes"
    if not confirmed:
        print(f"REFUSED: nothing was sent. Re-run with --confirm-spend (or {CONFIRM_ENV}=yes) "
              "to spend.", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    t0 = time.monotonic()
    result = asyncio.run(_run(args, plan, work_root))
    steps = result["steps"]
    report: Dict[str, Any] = {
        "schema_version": 1,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.monotonic() - t0, 2),
        "host": platform.node(),
        "interpreter": sys.executable,
        "work_root": str(work_root),
        "store_url": result["store_url"],
        "job_ids": result["job_ids"],
        "seats": plan,
        "estimate": est,
        "steps": steps,
        "summary": _summarise(steps),
        "live_acceptance": LIVE_ACCEPTANCE,
    }
    # JSON FIRST, before rendering (same rule as acceptance_rehearsal.py: the
    # measurement is the artifact; the text is a view of it).
    target = Path(args.json).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(f"report JSON written to {target}", file=sys.stderr)
    text = render(report)
    print(text)
    target = Path(args.report).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    print(f"\nreport written to {target}", file=sys.stderr)

    summary = report["summary"]
    if summary[FAILED]:
        return 1
    if args.strict and summary[NOT_ASSERTED]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
