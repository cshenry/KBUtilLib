"""``kbu council`` -- the LLM council's command surface.

Every verb is a thin facade over
:class:`~kbutillib.domains.ai.council_utils.CouncilUtils`.  This module
contains **no protocol logic, no prompt assembly, no SQL and no backend
construction of its own**: it parses argv, calls one facade method, shapes the
answer into the published output contract, and renders it.  A verb that needed
to know how a roster becomes backends would be a second place that could
disagree with the library, so there is none.

THE OUTPUT CONTRACT.  Under ``--json`` every verb emits EXACTLY ONE object::

    {"schema_version": 1, "verb": "doctor", "generated_at": "...",
     "data": {...}, "warnings": [...]}

Three structural rules, the same three ``kbu clearinghouse`` publishes:

1. HUMAN OUTPUT IS A RENDERING OF THE SAME DICT.  Each verb builds one
   envelope (via a ``_build_*`` helper) and then either serialises it
   (``--json``) or hands that very object to :func:`_render`.  There is no
   second code path that computes anything.
2. ``schema_version`` is bumped only on a BREAKING change to ``data``.
   Additive fields do not bump it; a consumer pinning major ``1`` must
   tolerate unknown keys.
3. ``warnings`` is never empty for a degraded answer.  A doctor seat that is
   not ready, a store with no schema and a dropped billing variable all
   appear there as well as in ``data``.

STDOUT DISCIPLINE.  Under ``--json``, stdout carries the envelope and nothing
else; warnings are mirrored to stderr for the human.

TWO RULES SPECIFIC TO THE COUNCIL:

* **Nothing here creates a database schema.**  Every verb except ``db *`` and
  ``doctor`` calls ``CouncilUtils.require_schema()`` first, so a store with no
  tables fails with "run ``kbu council db init``" rather than quietly becoming
  a second, empty council because a ``--store`` was mistyped.
* **``doctor`` spends nothing.**  It never calls a backend's ``complete()``
  and never sends a prompt: HTTP readiness is the free ``/models`` GET, CLI
  readiness is ``--version`` plus the offline billing preflight.

EXIT CODES.  ``doctor`` exits 1 when a seat is unavailable and 2 when none is
but some are unknown -- "I could not tell" is a different answer from "it is
broken", and a CI gate wants to distinguish them.  ``run`` exits 1 when the
job it worked ended blocked.  Everything else exits 0 or raises.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import click

#: The output-contract schema version.  Bumped ONLY on a breaking change to a
#: verb's ``data`` shape (see module docstring rule 2).
SCHEMA_VERSION = 1

#: Verbs that may run against a store with no schema.  Everything else is
#: refused with the ``db init`` instruction.
SCHEMALESS_VERBS = ("db", "doctor")


# ---------------------------------------------------------------------------
# Envelope + emit
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """The current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jsonable(value: Any) -> str:
    """Last-resort encoder for a value ``json`` cannot serialise itself.

    Store rows are plain dicts straight out of SQLAlchemy, so they carry
    ``datetime`` columns (``submitted_at``, ``decided_at``, ``not_before``).
    Those become ISO-8601 strings rather than ``str(datetime)``, because the
    envelope is a published contract and ``2026-10-15T21:30:36+00:00`` is a
    timestamp a consumer can parse while ``2026-10-15 21:30:36+00:00`` is one
    it has to guess at.
    """
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _envelope(
    verb: str,
    *,
    data: Dict[str, Any],
    warnings: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build the one published envelope object every verb emits."""
    return {
        "schema_version": SCHEMA_VERSION,
        "verb": verb,
        "generated_at": _now_iso(),
        "data": data,
        "warnings": list(warnings or []),
    }


def _emit(envelope: Dict[str, Any], *, as_json: bool) -> None:
    """Emit one built envelope, honouring the stdout discipline."""
    warnings = envelope.get("warnings", [])
    if as_json:
        for warning in warnings:
            click.echo(f"warning: {warning}", err=True)
        click.echo(json.dumps(envelope, default=_jsonable))
        return
    _render(envelope)
    for warning in warnings:
        click.echo(f"warning: {warning}", err=True)


def _rows(rows: Sequence[Any], columns: Sequence[str]) -> None:
    """Print *rows* as a column-aligned table of *columns*."""
    if not rows:
        click.echo("(none)")
        return
    table = [[str(row.get(col, "")) for col in columns] for row in rows]
    widths = [
        max(len(col), *(len(line[i]) for line in table)) for i, col in enumerate(columns)
    ]
    click.echo("  ".join(col.ljust(widths[i]) for i, col in enumerate(columns)).rstrip())
    for line in table:
        click.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip())


def _render(envelope: Dict[str, Any]) -> None:
    """Render a built envelope on stdout.

    Takes the SAME dict the ``--json`` path serialises -- there is no second
    computation path, which is what lets a test patch this function and assert
    it received exactly what the serialiser would have emitted.
    """
    verb = str(envelope.get("verb") or "")
    data = envelope.get("data") or {}

    if verb == "doctor":
        store = data.get("store") or {}
        click.echo(f"profile: {data.get('profile')}   user: {data.get('user')}")
        click.echo(
            f"store:   {store.get('url')} (schema {store.get('schema_version')}, "
            f"reachable={store.get('reachable')})"
        )
        _rows(data.get("seats") or [], ("role", "seat", "endpoint", "model", "state", "detail"))
        summary = data.get("summary") or {}
        click.echo(
            "seats: "
            + ", ".join(f"{state}={summary.get(state, 0)}" for state in ("ready", "unknown", "unavailable"))
        )
        if data.get("dropped_billing_vars"):
            click.echo("dropped billing variables: " + ", ".join(data["dropped_billing_vars"]))
        return

    if verb == "submit":
        click.echo(data.get("job_id", ""))
        return

    if verb in ("run", "jobs status", "cancel"):
        click.echo(
            f"job {data.get('job_id')}: {data.get('status')} "
            f"{data.get('percent_complete')}% of {data.get('n_items')} item(s)"
        )
        click.echo(f"items: {data.get('by_status')}")
        pending = data.get("pending_calls_by_role_endpoint") or {}
        if pending:
            click.echo(f"pending calls: {pending}")
        for blocker in data.get("blockers") or []:
            click.echo(f"blocked: {blocker.get('kind')} {blocker.get('endpoint')} {blocker.get('detail')}")
        return

    if verb == "jobs":
        _rows(
            data.get("jobs") or [],
            ("job_id", "label", "query_name", "status", "n_items", "submitted_by", "submitted_at"),
        )
        return

    if verb == "jobs results":
        out = data.get("out")
        if out:
            click.echo(f"{data.get('n_rows')} row(s) -> {out}")
            return
        _rows(data.get("results") or [], ("item_id", "status", "decided_at"))
        return

    if verb == "jobs delete":
        click.echo(f"job {data.get('job_id')}: {data.get('status')} (tombstoned)")
        return

    if verb == "jobs rerun":
        click.echo(data.get("job_id", ""))
        return

    if verb == "query list":
        _rows(
            data.get("query_types") or [],
            ("name", "version", "status", "source", "source_file", "created_by"),
        )
        return

    if verb == "query show":
        click.echo(json.dumps(data.get("spec") or {}, indent=2, sort_keys=True, default=_jsonable))
        return

    if verb in ("query register", "query retire"):
        click.echo(f"{data.get('name')}@{data.get('version')}: {data.get('result')}")
        return

    if verb == "calls search":
        _rows(
            data.get("calls") or [],
            ("call_id", "role", "canonical_model", "status", "query_name", "entity_hash"),
        )
        return

    if verb in ("daemon start", "daemon stop", "daemon status"):
        holder = data.get("holder") or {}
        click.echo(
            f"store:  {data.get('store')}\nlock:   {data.get('lock')}\n"
            f"log:    {data.get('log')}"
        )
        if holder:
            click.echo(
                f"holder: {holder.get('kind')} pid {holder.get('pid')} "
                f"started {holder.get('started_at')}"
            )
        else:
            click.echo("holder: (none -- no council process is executing)")
        if data.get("detail"):
            click.echo(str(data["detail"]))
        if data.get("passes") is not None:
            # A --foreground daemon that has finished: this is its own tally,
            # and it is the only place the interrupted count is reported.
            click.echo(
                f"served: {data.get('passes')} pass(es), "
                f"{data.get('claimed')} claimed, {data.get('finished')} finished, "
                f"{data.get('interrupted')} interrupted, {data.get('resumes')} resume(s)"
            )
        if verb == "daemon status":
            _rows(
                data.get("workers") or [],
                ("worker_id", "kind", "host", "pid", "state", "heartbeat_age_s", "endpoints"),
            )
        return

    if verb == "endpoints status":
        _rows(
            data.get("endpoints") or [],
            ("endpoint", "kind", "halted", "halt_kind", "epoch_tokens", "lifetime_tokens"),
        )
        return

    if verb in ("endpoints reset-halt", "endpoints reset-budget"):
        click.echo(f"{data.get('endpoint')}: {data.get('result')}")
        return

    if verb in ("db init", "db migrate", "db check"):
        for key, value in data.items():
            click.echo(f"{key}: {value}")
        return

    if verb == "models list":
        rows = data.get("models") or []
        if data.get("endpoint"):
            _rows(rows, ("id", "object", "owned_by"))
        else:
            _rows(rows, ("alias", "endpoint", "kind", "served_name", "canonical", "verified"))
        return

    if verb == "quote":
        headline = data.get("headline") or {}
        point = headline.get("point") or {}
        upper = headline.get("upper") or {}
        click.echo(
            f"plan: {data.get('plan_hash', '')[:12]}   "
            f"query: {(data.get('parameter_set') or {}).get('query')}   "
            f"items: {(data.get('plan') or {}).get('n_items')}"
        )
        click.echo(
            "bench_fingerprint (the PARTITION key): "
            f"{str((data.get('parameter_set') or {}).get('bench_fingerprint'))[:12]}"
        )
        # The projected fraction FIRST: it says how much of what follows is a
        # measurement and how much is an extrapolation.
        click.echo(
            f"PROJECTED FRACTION OF TOTAL: "
            f"{float(headline.get('projected_fraction_of_total') or 0.0):.1%}"
        )
        click.echo(
            f"tokens: point {int(point.get('total_tokens') or 0):,}   "
            f"upper {int(upper.get('total_tokens') or 0):,}   "
            f"(input {int(point.get('input_tokens') or 0):,})"
        )
        click.echo(
            f"calls: {headline.get('calls_total')} total, "
            f"{headline.get('calls_reused')} reused, "
            f"{headline.get('calls_paid')} paid"
        )
        _rows(
            data.get("call_graph") or [],
            (
                "line",
                "role",
                "basis",
                "calls_total",
                "calls_reused",
                "calls_paid",
                "input_tokens_point",
                "output_tokens_point",
                "total_tokens_upper",
            ),
        )
        misses = (data.get("reuse") or {}).get("misses") or {}
        click.echo(
            "reuse misses: "
            f"new_item {misses.get('new_item', 0)}, "
            f"bench_change {misses.get('bench_change', 0)}, "
            f"transport_change {misses.get('transport_change', 0)}"
        )
        wall = data.get("wall_clock") or {}
        click.echo(
            f"wall clock: {wall.get('hours_point')} h point / "
            f"{wall.get('hours_upper')} h upper, set by the SLOWEST queue "
            f"{wall.get('binding_queue')!r} (limit: {wall.get('binding_limit')})"
        )
        _rows(
            data.get("gateways") or [],
            ("gateway", "total_tokens_point", "priced", "dollars_point", "dollars_upper"),
        )
        for gateway in data.get("gateways") or []:
            if not gateway.get("priced"):
                click.echo(f"{gateway.get('gateway')}: unpriced")
            elif gateway.get("is_floor"):
                click.echo(
                    f"{gateway.get('gateway')}: FLOOR, not a cost "
                    f"({gateway.get('rate_provenance')})"
                )
        rate = data.get("meta_fire_rate") or {}
        click.echo(
            f"meta fire rate: {float(rate.get('rate') or 0.0):.1%} (n={rate.get('n')})"
        )
        return

    if verb == "token-model":
        click.echo(
            f"profile: {data.get('profile')}   bench: {str(data.get('bench_fingerprint'))[:12]}"
            f"   roster: {str(data.get('roster_fingerprint'))[:12]}"
        )
        _rows(
            data.get("seats") or [],
            (
                "seat_name",
                "role",
                "input_tokens_per_char",
                "output_tokens_mean",
                "output_tokens_upper",
                "n_attempts",
                "basis",
            ),
        )
        click.echo(
            f"output band: p{data.get('upper_percentile')}   "
            f"measured seats: {data.get('n_measured')}   "
            f"projected seats: {data.get('n_projected')}"
        )
        for seat in data.get("seats") or []:
            if seat.get("fallback"):
                click.echo(f"{seat.get('seat_name')} fallback: {seat['fallback']}")
        return

    click.echo(json.dumps(data, indent=2, sort_keys=True, default=_jsonable))


# ---------------------------------------------------------------------------
# The facade, and the one guard every verb shares
# ---------------------------------------------------------------------------


def _council(ctx: click.Context) -> Any:
    """Build the :class:`CouncilUtils` this invocation works through.

    One factory, so a test can patch exactly one name, and so the group's
    ``--config`` / ``--store`` options reach every verb without each verb
    repeating them.  File discovery is off: the council's own configuration
    route is ``load_council_config`` (the packaged default overlaid with the
    user's ``ai_council`` section), not ``SharedEnvUtils``' config hunt.
    """
    from ...domains.ai.council_utils import CouncilUtils

    options = ctx.obj or {}
    return CouncilUtils(
        council_config=options.get("config_path"),
        store_url=options.get("store_url"),
        allow_unverified=bool(options.get("allow_unverified")),
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )


def _ready(ctx: click.Context) -> Any:
    """The facade, with its store proved to carry a schema.

    Every verb except ``db *`` and ``doctor`` goes through here, which is the
    single place the "run ``kbu council db init``" instruction is produced.
    """
    util = _council(ctx)
    try:
        util.require_schema()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    return util


def _fail(exc: BaseException) -> "click.ClickException":
    """A council failure as a clean CLI error rather than a traceback."""
    return click.ClickException(f"{type(exc).__name__}: {exc}")


def _parse_seats(seat: Sequence[str]) -> Dict[str, Any]:
    """``--seat critic=opus-cli`` pairs as roster overrides.

    Keyed by SEAT NAME, which is what ``CouncilConfig.roster`` takes.  The one
    special key is ``proposers``, whose comma-separated value replaces the
    whole proposer bench -- the one override that is a list rather than a
    single alias.
    """
    overrides: Dict[str, Any] = {}
    for item in seat or ():
        if "=" not in item:
            raise click.BadParameter(
                f"--seat expects SEAT_NAME=ALIAS, got {item!r}", param_hint="--seat"
            )
        name, _, alias = item.partition("=")
        name, alias = name.strip(), alias.strip()
        if not name or not alias:
            raise click.BadParameter(
                f"--seat expects SEAT_NAME=ALIAS, got {item!r}", param_hint="--seat"
            )
        overrides[name] = [part.strip() for part in alias.split(",")] if name == "proposers" else alias
    return overrides


def _read_tasks(path: str) -> List[Dict[str, Any]]:
    """Read a JSONL file of ``CouncilTask`` dicts.

    One item per line (``item_id``, ``evidence``, ``menu``).  Validation is
    the library's -- this only refuses lines that are not JSON objects, and
    names the line number when it does, because a 20k-line input with one bad
    line should not report "invalid input".
    """
    tasks: List[Dict[str, Any]] = []
    text = Path(path).read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except ValueError as exc:
            raise click.ClickException(f"{path}:{number}: not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise click.ClickException(
                f"{path}:{number}: expected a JSON object (a CouncilTask: item_id, "
                f"evidence, menu), got {type(payload).__name__}"
            )
        tasks.append(payload)
    if not tasks:
        raise click.ClickException(f"{path} holds no items")
    return tasks


def _json_option(function: Any) -> Any:
    """``--json``, spelled once."""
    return click.option(
        "--json", "as_json", is_flag=True, default=False, help="Emit the JSON envelope."
    )(function)


# ---------------------------------------------------------------------------
# ``kbu council`` group
# ---------------------------------------------------------------------------


@click.group(name="council")
@click.option(
    "--config",
    "config_path",
    envvar="KBU_COUNCIL_CONFIG",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Council configuration YAML. Default: the packaged default overlaid "
    "with the `ai_council` section of the user config.",
)
@click.option(
    "--store",
    "store_url",
    envvar="KBU_COUNCIL_STORE",
    default=None,
    help="Override the store URL the configuration names (a sqlite:/// URL or "
    "a plain path; the store is always a local SQLite file).",
)
@click.pass_context
def council_cmd(ctx: click.Context, config_path: Optional[str], store_url: Optional[str]) -> None:
    """Run a closed-menu LLM council: N proposers, a critic, a meta adjudicator.

    Work is organised as jobs against a saved query type; every model
    invocation is a row in the store, and `run` is the foreground worker that
    claims those rows. Start with `kbu council doctor` -- it spends nothing
    and tells you whether the seats of your profile can be reached at all.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["store_url"] = store_url


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _build_doctor(util: Any, *, profile: Optional[str]) -> Dict[str, Any]:
    """Build the ``doctor`` envelope: per-seat readiness, store, user, drops."""
    report = dict(util.doctor(profile))
    warnings = list(report.pop("warnings", []))
    return _envelope("doctor", data=report, warnings=warnings)


@council_cmd.command(name="doctor")
@click.option("--profile", default=None, help="Profile to check. Default: the configured default.")
@_json_option
@click.pass_context
def doctor_cmd(ctx: click.Context, profile: Optional[str], as_json: bool) -> None:
    """Check every seat of a profile WITHOUT spending anything.

    Exits 1 if any seat is unavailable, 2 if none is but some are unknown.
    """
    util = _council(ctx)
    try:
        envelope = _build_doctor(util, profile=profile)
    except Exception as exc:  # noqa: BLE001 - a config failure is a CLI error
        raise _fail(exc) from exc
    _emit(envelope, as_json=as_json)
    code = int(envelope["data"].get("exit_code") or 0)
    if code:
        raise SystemExit(code)


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


def _submit(
    util: Any,
    *,
    query: str,
    input_file: str,
    label: Optional[str],
    priority: int,
    profile: Optional[str],
    seat: Sequence[str],
    allow_unverified: bool,
    allow_unavailable: bool = False,
) -> Dict[str, Any]:
    """Build the ``submit`` envelope: the new job's id and what it was seated with.

    The library refuses a seat this machine cannot serve
    (``SeatUnavailable``); that arrives here as a clean CLI error naming the
    seats.  With ``--allow-unavailable`` the job is created and its blockers
    appear in the envelope's ``warnings``, because a job queued behind an
    endpoint that does not work here has to say so at submit and not only
    when somebody asks for its status.
    """
    tasks = _read_tasks(input_file)
    try:
        job_id = util.submit(
            query,
            tasks,
            label=label,
            priority=priority,
            profile=profile,
            allow_unverified=allow_unverified,
            allow_unavailable=allow_unavailable,
            **_parse_seats(seat),
        )
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 - a refusal is a CLI error
        raise _fail(exc) from exc
    status = util.job_status(job_id)
    blockers = [
        blocker
        for blocker in status.get("blockers") or []
        if blocker.get("kind") == "unservable"
    ]
    return _envelope(
        "submit",
        data={
            "job_id": job_id,
            "query": query,
            "input": input_file,
            "n_items": status.get("n_items"),
            "status": status.get("status"),
            "profile": profile,
            "blockers": blockers,
        },
        warnings=[
            f"unservable: {blocker.get('endpoint')}: {blocker.get('detail')}"
            for blocker in blockers
        ],
    )


def _submit_options(function: Any) -> Any:
    """The options ``submit`` and the submitting form of ``run`` share."""
    for decorator in reversed(
        (
            click.option("--query", required=True, help="Query type NAME or NAME@VERSION."),
            click.option(
                "--input",
                "input_file",
                required=True,
                type=click.Path(exists=True, dir_okay=False),
                help="JSONL file of items (one CouncilTask per line: item_id, evidence, menu).",
            ),
            click.option("--label", default=None, help="Free-text label for the job."),
            click.option("--priority", default=100, show_default=True, type=int, help="Lower runs first."),
            click.option("--profile", default=None, help="Seating profile."),
            click.option(
                "--seat",
                multiple=True,
                metavar="SEAT_NAME=ALIAS",
                help="Override one seat (repeatable). `proposers=a,b,c` replaces the bench.",
            ),
            click.option(
                "--allow-unverified",
                is_flag=True,
                default=False,
                help="Permit unverified seats and an unconfirmed CLI billing mode.",
            ),
            click.option(
                "--allow-unavailable",
                is_flag=True,
                default=False,
                help="Queue the job even though an endpoint is unavailable HERE. Its "
                "calls wait: the queue is per machine, so no other host works them.",
            ),
        )
    ):
        function = decorator(function)
    return function


@council_cmd.command(name="submit")
@_submit_options
@_json_option
@click.pass_context
def submit_cmd(
    ctx: click.Context,
    query: str,
    input_file: str,
    label: Optional[str],
    priority: int,
    profile: Optional[str],
    seat: Sequence[str],
    allow_unverified: bool,
    allow_unavailable: bool,
    as_json: bool,
) -> None:
    """Queue a batch of items against a saved query type. Prints the job id."""
    envelope = _submit(
        _ready(ctx),
        query=query,
        input_file=input_file,
        label=label,
        priority=priority,
        profile=profile,
        seat=seat,
        allow_unverified=allow_unverified,
        allow_unavailable=allow_unavailable,
    )
    _emit(envelope, as_json=as_json)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _build_run(util: Any, job_id: str) -> Dict[str, Any]:
    """Build the ``run`` envelope: the job's status after the worker went idle."""
    try:
        status = dict(util.run(job_id))
    except Exception as exc:  # noqa: BLE001 - a worker refusal is a CLI error
        raise _fail(exc) from exc
    warnings = [
        f"blocked: {blocker.get('kind')} {blocker.get('endpoint')}: {blocker.get('detail')}"
        for blocker in status.get("blockers") or []
    ]
    return _envelope("run", data=status, warnings=warnings)


@council_cmd.command(name="run")
@click.option("--job", "job_id", default=None, help="Work an existing job.")
@click.option("--query", default=None, help="Submit first: query type NAME or NAME@VERSION.")
@click.option(
    "--input",
    "input_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Submit first: JSONL file of items.",
)
@click.option("--label", default=None, help="Label for a job submitted here.")
@click.option("--priority", default=100, show_default=True, type=int, help="Lower runs first.")
@click.option("--profile", default=None, help="Seating profile.")
@click.option("--seat", multiple=True, metavar="SEAT_NAME=ALIAS", help="Override one seat (repeatable).")
@click.option("--allow-unverified", is_flag=True, default=False, help="Permit unverified seats.")
@click.option(
    "--allow-unavailable",
    is_flag=True,
    default=False,
    help="Submit even though an endpoint is unavailable here (its calls wait).",
)
@_json_option
@click.pass_context
def run_cmd(
    ctx: click.Context,
    job_id: Optional[str],
    query: Optional[str],
    input_file: Optional[str],
    label: Optional[str],
    priority: int,
    profile: Optional[str],
    seat: Sequence[str],
    allow_unverified: bool,
    allow_unavailable: bool,
    as_json: bool,
) -> None:
    """Work a job in the foreground until it is idle.

    Either `--job ID`, or `--query ... --input ...` to submit and immediately
    work the result. Exits 1 if the job ended blocked.
    """
    if bool(job_id) == bool(query or input_file):
        raise click.UsageError("give either --job ID, or --query with --input, but not both")
    util = _ready(ctx)
    if job_id is None:
        if not (query and input_file):
            raise click.UsageError("--query and --input must be given together")
        job_id = _submit(
            util,
            query=query,
            input_file=input_file,
            label=label,
            priority=priority,
            profile=profile,
            seat=seat,
            allow_unverified=allow_unverified,
            allow_unavailable=allow_unavailable,
        )["data"]["job_id"]
    envelope = _build_run(util, job_id)
    _emit(envelope, as_json=as_json)
    if envelope["data"].get("blockers"):
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


def _daemon(ctx: click.Context) -> Any:
    """The :class:`CouncilDaemon` for this invocation's store.

    Through the facade's own factory, so this module still constructs nothing
    of the council's: ``start``, ``stop`` and ``status`` are three calls on one
    object and the daemon's clocks stay the library's.
    """
    return _ready(ctx).daemon()


@council_cmd.group(name="daemon", invoke_without_command=True)
@click.pass_context
def daemon_cmd(ctx: click.Context) -> None:
    """Run ONE long-running worker for this machine's store.

    The daemon drains every job in the store, not one, and holds the store's
    execution lock for its whole life -- so `kbu council run`, `evaluate` and
    `db migrate` refuse while it is up, and it refuses while one of them is.
    With no subcommand this is `daemon status`.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(daemon_status_cmd, as_json=False)


@daemon_cmd.command(name="start")
@click.option(
    "--foreground",
    is_flag=True,
    default=False,
    help="Run the loop in THIS process (for systemd, launchd, tmux or nohup) "
    "instead of spawning a detached child.",
)
@_json_option
@click.pass_context
def daemon_start_cmd(ctx: click.Context, foreground: bool, as_json: bool) -> None:
    """Start the daemon. Detached by default; --foreground runs it here.

    Exits 1 naming the holder's kind, pid and start time if another council
    process (a daemon, a `run`, an `evaluate` or a `db migrate`) is already
    executing against this store.
    """
    daemon = _daemon(ctx)
    try:
        data = dict(daemon.start(foreground=foreground))
    except Exception as exc:  # noqa: BLE001 - a refusal is a CLI error
        raise _fail(exc) from exc
    data.setdefault("store", daemon.council.store_url())
    data.setdefault("lock", str(daemon.lock.path))
    data.setdefault("log", str(daemon.log_path))
    _emit(_envelope("daemon start", data=data), as_json=as_json)


@daemon_cmd.command(name="stop")
@_json_option
@click.pass_context
def daemon_stop_cmd(ctx: click.Context, as_json: bool) -> None:
    """Stop the daemon holding this store's lock, and wait for it to go.

    Signals only a holder whose recorded kind is `daemon`: a `run` in
    somebody's terminal and a half-finished `db migrate` are lock holders too,
    and this verb reports them rather than killing them.
    """
    daemon = _daemon(ctx)
    try:
        data = dict(daemon.stop())
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    data.setdefault("store", daemon.council.store_url())
    data.setdefault("lock", str(daemon.lock.path))
    data.setdefault("log", str(daemon.log_path))
    warnings = [] if data.get("stopped") or not data.get("running") else [str(data.get("detail"))]
    _emit(_envelope("daemon stop", data=data, warnings=warnings), as_json=as_json)


@daemon_cmd.command(name="status")
@_json_option
@click.pass_context
def daemon_status_cmd(ctx: click.Context, as_json: bool) -> None:
    """Who holds this store's execution lock, and what its workers rows say."""
    daemon = _daemon(ctx)
    try:
        data = dict(daemon.status())
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    warnings = [
        f"worker {row['worker_id']} is stale (last heartbeat "
        f"{row.get('heartbeat_age_s')}s ago)"
        for row in data.get("workers") or []
        if row.get("state") == "stale"
    ]
    _emit(_envelope("daemon status", data=data, warnings=warnings), as_json=as_json)


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


@council_cmd.group(name="jobs", invoke_without_command=True)
@click.option("--status", default=None, help="Only jobs in this status.")
@click.option("--include-deleted", is_flag=True, default=False, help="Include tombstoned jobs.")
@_json_option
@click.pass_context
def jobs_cmd(
    ctx: click.Context,
    status: Optional[str],
    include_deleted: bool,
    as_json: bool,
) -> None:
    """List every job in the store; with a subcommand, act on one.

    There is no ``--all-users``: a store is one machine's single-user file, so
    every job in it is already yours.
    """
    if ctx.invoked_subcommand is not None:
        return
    util = _ready(ctx)
    try:
        rows = util.jobs(status=status, include_deleted=include_deleted)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(
        _envelope(
            "jobs",
            data={
                "user": util.user(),
                "status": status,
                "include_deleted": include_deleted,
                "n_jobs": len(rows),
                "jobs": [dict(row) for row in rows],
            },
        ),
        as_json=as_json,
    )


@jobs_cmd.command(name="status")
@click.argument("job_id")
@_json_option
@click.pass_context
def jobs_status_cmd(ctx: click.Context, job_id: str, as_json: bool) -> None:
    """Progress, outstanding work by role and endpoint, and blockers."""
    try:
        status = _ready(ctx).job_status(job_id)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(_envelope("jobs status", data=dict(status)), as_json=as_json)


@jobs_cmd.command(name="results")
@click.argument("job_id")
@click.option("--out", "out", default=None, type=click.Path(dir_okay=False), help="Write rows to a file.")
@click.option(
    "--format",
    "out_format",
    type=click.Choice(["jsonl", "parquet"]),
    default="jsonl",
    show_default=True,
    help="Format for --out.",
)
@click.option("--include-pending", is_flag=True, default=False, help="Include undecided items.")
@_json_option
@click.pass_context
def jobs_results_cmd(
    ctx: click.Context,
    job_id: str,
    out: Optional[str],
    out_format: str,
    include_pending: bool,
    as_json: bool,
) -> None:
    """A job's decided items with their verdicts."""
    util = _ready(ctx)
    try:
        rows = util.results(job_id, include_pending=include_pending)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    rows = [dict(row) for row in rows]
    data: Dict[str, Any] = {
        "job_id": job_id,
        "include_pending": include_pending,
        "n_rows": len(rows),
        "out": out,
        "format": out_format if out else None,
    }
    if out:
        _write_results(rows, out, out_format)
    else:
        data["results"] = rows
    _emit(_envelope("jobs results", data=data), as_json=as_json)


def _write_results(rows: Sequence[Dict[str, Any]], out: str, out_format: str) -> None:
    """Write result rows to *out*.

    ``jsonl`` is written here (``default=str`` so a datetime column survives);
    ``parquet`` goes through pandas, which is optional, so a missing pandas is
    reported as the install it needs rather than as an ImportError traceback.
    """
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    if out_format == "jsonl":
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, default=_jsonable) + "\n")
        return
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise click.ClickException(
            "--format parquet needs pandas and a parquet engine: pip install pandas pyarrow"
        ) from exc
    frame = pd.DataFrame(
        [
            {
                # A verdict is a nested object and parquet has no column type
                # for one, so it travels as its JSON text rather than being
                # flattened into columns whose names would depend on the query
                # type.
                key: json.dumps(value, default=_jsonable)
                if isinstance(value, (dict, list))
                else value
                for key, value in row.items()
            }
            for row in rows
        ]
    )
    try:
        frame.to_parquet(path)
    except Exception as exc:  # noqa: BLE001 - a missing engine is a CLI error
        raise click.ClickException(f"could not write parquet: {type(exc).__name__}: {exc}") from exc


@jobs_cmd.command(name="cancel")
@click.argument("job_id")
@_json_option
@click.pass_context
def jobs_cancel_cmd(ctx: click.Context, job_id: str, as_json: bool) -> None:
    """Cancel a job. Terminal: every pending call is superseded."""
    try:
        status = _ready(ctx).cancel(job_id)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(_envelope("cancel", data=dict(status)), as_json=as_json)


@jobs_cmd.command(name="delete")
@click.argument("job_id")
@_json_option
@click.pass_context
def jobs_delete_cmd(ctx: click.Context, job_id: str, as_json: bool) -> None:
    """Tombstone a cancelled or done job. No row is physically removed."""
    try:
        data = _ready(ctx).delete(job_id)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(_envelope("jobs delete", data=dict(data)), as_json=as_json)


@jobs_cmd.command(name="rerun")
@click.argument("job_id")
@click.option("--profile", default=None, help="Re-seat the rerun with this profile.")
@click.option("--seat", multiple=True, metavar="SEAT_NAME=ALIAS", help="Override one seat (repeatable).")
@click.option("--query-version", default=None, help="Rerun against a different version of the query type.")
@click.option("--label", default=None, help="Label for the new job.")
@click.option("--allow-unverified", is_flag=True, default=False, help="Permit unverified seats.")
@_json_option
@click.pass_context
def jobs_rerun_cmd(
    ctx: click.Context,
    job_id: str,
    profile: Optional[str],
    seat: Sequence[str],
    query_version: Optional[str],
    label: Optional[str],
    allow_unverified: bool,
    as_json: bool,
) -> None:
    """Submit the SAME items again, optionally re-seated or re-versioned."""
    util = _ready(ctx)
    try:
        new_id = util.rerun(
            job_id,
            profile=profile,
            query_version=query_version,
            label=label,
            allow_unverified=allow_unverified,
            **_parse_seats(seat),
        )
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(
        _envelope("jobs rerun", data={"job_id": new_id, "rerun_of": job_id, "profile": profile}),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


@council_cmd.group(name="query")
def query_cmd() -> None:
    """The saved query types: the only place a domain vocabulary lives."""


@query_cmd.command(name="list")
@click.option("--include-retired", is_flag=True, default=False, help="Also show retired versions.")
@_json_option
@click.pass_context
def query_list_cmd(ctx: click.Context, include_retired: bool, as_json: bool) -> None:
    """List the saved query types."""
    try:
        rows = _ready(ctx).queries(include_retired=include_retired)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(
        _envelope(
            "query list",
            data={"n_query_types": len(rows), "query_types": [dict(row) for row in rows]},
        ),
        as_json=as_json,
    )


@query_cmd.command(name="show")
@click.argument("ref")
@_json_option
@click.pass_context
def query_show_cmd(ctx: click.Context, ref: str, as_json: bool) -> None:
    """Show one query type's spec, by NAME or NAME@VERSION."""
    try:
        spec = _ready(ctx).query(ref)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(
        _envelope(
            "query show",
            data={"name": spec.name, "version": spec.version, "spec": spec.to_spec()},
        ),
        as_json=as_json,
    )


@query_cmd.command(name="register")
@click.argument("spec_file", type=click.Path(exists=True, dir_okay=False))
@_json_option
@click.pass_context
def query_register_cmd(ctx: click.Context, spec_file: str, as_json: bool) -> None:
    """Validate a query-type YAML and save it. Immutable once saved."""
    try:
        spec = _ready(ctx).register_query(spec_file)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(
        _envelope(
            "query register",
            data={"name": spec.name, "version": spec.version, "source": spec_file, "result": "registered"},
        ),
        as_json=as_json,
    )


@query_cmd.command(name="retire")
@click.argument("ref")
@_json_option
@click.pass_context
def query_retire_cmd(ctx: click.Context, ref: str, as_json: bool) -> None:
    """Hide a query type from lookups, without removing it. Takes NAME@VERSION."""
    if "@" not in ref:
        raise click.BadParameter(
            "retire needs an exact NAME@VERSION: a query type is retired one version "
            "at a time, because a finished job's verdicts only mean anything against "
            "the exact spec that produced them",
            param_hint="REF",
        )
    name, _, version = ref.partition("@")
    try:
        retired = _ready(ctx).retire_query(name, version)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(
        _envelope(
            "query retire",
            data={
                "name": name,
                "version": version,
                "result": "retired" if retired else "no such active version",
            },
        ),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# calls
# ---------------------------------------------------------------------------


@council_cmd.group(name="calls")
def calls_cmd() -> None:
    """The call rows: every model invocation this installation has made."""


@calls_cmd.command(name="search")
@click.option("--query", "query_name", default=None, help="Only calls of this query type.")
@click.option("--model", "canonical_model", default=None, help="Only calls to this canonical model.")
@click.option("--entity", default=None, help="The entity TEXT (hashed here for you).")
@click.option("--entity-hash", "entity_hash_value", default=None, help="The entity hash directly.")
@click.option("--data-hash", "data_hash_value", default=None, help="The item data hash.")
@click.option("--status", default=None, help="Only calls in this status.")
@click.option("--limit", default=100, show_default=True, type=int, help="Maximum rows.")
@_json_option
@click.pass_context
def calls_search_cmd(
    ctx: click.Context,
    query_name: Optional[str],
    canonical_model: Optional[str],
    entity: Optional[str],
    entity_hash_value: Optional[str],
    data_hash_value: Optional[str],
    status: Optional[str],
    limit: int,
    as_json: bool,
) -> None:
    """Find call rows across every job and user."""
    if entity and entity_hash_value:
        raise click.UsageError("give either --entity or --entity-hash, not both")
    filters: Dict[str, Any] = {"limit": limit}
    if query_name:
        filters["query_name"] = query_name
    if canonical_model:
        filters["canonical_model"] = canonical_model
    if entity:
        filters["entity"] = entity
    if entity_hash_value:
        filters["entity_hash"] = entity_hash_value
    if data_hash_value:
        filters["data_hash"] = data_hash_value
    if status:
        filters["status"] = status
    try:
        rows = _ready(ctx).search_calls(**filters)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(
        _envelope(
            "calls search",
            data={
                "filters": {k: v for k, v in filters.items() if k != "limit"},
                "limit": limit,
                "n_calls": len(rows),
                "calls": [dict(row) for row in rows],
            },
        ),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@council_cmd.group(name="endpoints")
def endpoints_cmd() -> None:
    """Per-endpoint budget and halt state, for this user."""


@endpoints_cmd.command(name="status")
@_json_option
@click.pass_context
def endpoints_status_cmd(ctx: click.Context, as_json: bool) -> None:
    """Budget and halt state for every configured endpoint."""
    util = _ready(ctx)
    try:
        rows = util.endpoint_status()
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    warnings = [
        f"endpoint {row['endpoint']} is halted ({row.get('halt_kind')}): {row.get('halt_message')}"
        for row in rows
        if row.get("halted")
    ]
    _emit(
        _envelope(
            "endpoints status",
            data={"user": util.user(), "endpoints": [dict(row) for row in rows]},
            warnings=warnings,
        ),
        as_json=as_json,
    )


@endpoints_cmd.command(name="reset-halt")
@click.argument("endpoint")
@_json_option
@click.pass_context
def endpoints_reset_halt_cmd(ctx: click.Context, endpoint: str, as_json: bool) -> None:
    """Clear an endpoint's halt. Never touches the token counters."""
    try:
        state = _ready(ctx).reset_halt(endpoint)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    data = dict(state)
    data["endpoint"] = endpoint
    data["result"] = "halt cleared"
    _emit(_envelope("endpoints reset-halt", data=data), as_json=as_json)


@endpoints_cmd.command(name="reset-budget")
@click.argument("endpoint")
@_json_option
@click.pass_context
def endpoints_reset_budget_cmd(ctx: click.Context, endpoint: str, as_json: bool) -> None:
    """Start a new budget epoch, keeping the lifetime total."""
    try:
        state = _ready(ctx).reset_budget(endpoint)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    data = dict(state)
    data["endpoint"] = endpoint
    data["result"] = "new budget epoch"
    _emit(_envelope("endpoints reset-budget", data=data), as_json=as_json)


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


@council_cmd.group(name="db")
def db_cmd() -> None:
    """Create and check the council store's schema.

    SQLite only at this milestone. A PostgreSQL store is opened by the same
    `open_store` dispatch, so these verbs reach it without a second code path
    once that implementation lands.
    """


@db_cmd.command(name="init")
@_json_option
@click.pass_context
def db_init_cmd(ctx: click.Context, as_json: bool) -> None:
    """Create every table and seed the builtin query types. Idempotent."""
    try:
        data = _council(ctx).db_init()
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(_envelope("db init", data=dict(data)), as_json=as_json)


@db_cmd.command(name="migrate")
@_json_option
@click.pass_context
def db_migrate_cmd(ctx: click.Context, as_json: bool) -> None:
    """Bring the store up to the current schema version."""
    try:
        data = _council(ctx).db_migrate()
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    _emit(_envelope("db migrate", data=dict(data)), as_json=as_json)


@db_cmd.command(name="check")
@_json_option
@click.pass_context
def db_check_cmd(ctx: click.Context, as_json: bool) -> None:
    """Report the store's reachability and schema version."""
    data = dict(_council(ctx).db_check())
    warnings = [data["error"]] if data.get("error") else []
    _emit(_envelope("db check", data=data, warnings=warnings), as_json=as_json)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


@council_cmd.group(name="models")
def models_cmd() -> None:
    """What this installation can seat, and what an endpoint actually serves."""


@models_cmd.command(name="list")
@click.option(
    "--endpoint",
    default=None,
    help="Ask THIS endpoint what it serves (a free GET). Default: the configured aliases.",
)
@_json_option
@click.pass_context
def models_list_cmd(ctx: click.Context, endpoint: Optional[str], as_json: bool) -> None:
    """List the configured model aliases, or one endpoint's live model list."""
    try:
        rows = _council(ctx).models(endpoint)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    warnings = []
    if endpoint and not rows:
        warnings.append(
            f"{endpoint} listed no models: it is unreachable, or (for a CLI endpoint) "
            "it cannot enumerate its models without spending"
        )
    _emit(
        _envelope(
            "models list",
            data={"endpoint": endpoint, "n_models": len(rows), "models": [dict(r) for r in rows]},
            warnings=warnings,
        ),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# token-model
# ---------------------------------------------------------------------------


def _build_token_model(
    util: Any, *, profile: Optional[str], seat: Sequence[str], upper_percentile: Optional[float]
) -> Dict[str, Any]:
    """Build the ``token-model`` envelope: the fit, its sample sizes, its gaps.

    Every PROJECTED seat is also a warning, per the module's third structural
    rule: a quote priced off a seat this store has never run is a degraded
    answer, and an operator approving the cost has to see that before the
    number, not after it.
    """
    data = dict(
        util.token_model(
            profile, upper_percentile=upper_percentile, **_parse_seats(seat)
        )
    )
    warnings = [
        f"{seat_fit.get('seat_name')} ({seat_fit.get('role')}) has no measured history "
        f"in this store; projected from {seat_fit.get('fallback')}"
        for seat_fit in data.get("seats") or []
        if seat_fit.get("basis") == "projected"
    ]
    return _envelope("token-model", data=data, warnings=warnings)


@council_cmd.command(name="token-model")
@click.option("--profile", default=None, help="Profile whose bench to fit. Default: the configured default.")
@click.option("--seat", multiple=True, metavar="SEAT_NAME=ALIAS", help="Override one seat (repeatable).")
@click.option(
    "--upper-percentile",
    default=None,
    type=float,
    help="Percentile for the output-token upper band. Default: 95.",
)
@_json_option
@click.pass_context
def token_model_cmd(
    ctx: click.Context,
    profile: Optional[str],
    seat: Sequence[str],
    upper_percentile: Optional[float],
    as_json: bool,
) -> None:
    """Show the fitted token model a quote is priced from, with sample sizes.

    Reads this store's own measured history and SPENDS NOTHING -- it contacts
    no gateway and makes no call. A seat with no history is reported
    ``projected`` against a named fallback, and as a warning.
    """
    util = _ready(ctx)
    try:
        envelope = _build_token_model(
            util,
            profile=profile,
            seat=seat,
            upper_percentile=upper_percentile,
        )
    except Exception as exc:  # noqa: BLE001 - a config or store failure is a CLI error
        raise _fail(exc) from exc
    _emit(envelope, as_json=as_json)


# ---------------------------------------------------------------------------
# quote
# ---------------------------------------------------------------------------


def build_quote(
    util: Any,
    *,
    plan: str,
    profile: Optional[str] = None,
    seat: Sequence[str] = (),
    meta_fire_rate: Optional[float] = None,
    out: Optional[str] = None,
    write: bool = True,
) -> Dict[str, Any]:
    """Build the ``quote`` envelope, and write ``QUOTE.json`` beside the plan.

    PUBLIC because ``kbu ontomap council-quote`` delegates to this very
    function rather than reimplementing the stage: two code paths that both
    priced a plan would be two numbers that could disagree, and the one an
    operator approved would be whichever CLI they happened to type.

    Every projected seat in the token model is a warning, and so is every
    unpriced gateway: both are degraded answers, and the third structural
    rule of this module's output contract is that a degraded answer never
    has an empty ``warnings``.
    """
    quote = util.quote_plan(
        plan, profile=profile, meta_fire_rate=meta_fire_rate, **_parse_seats(seat)
    )
    data = quote.to_dict()
    warnings = list(data.get("warnings") or [])
    for seat_fit in (data.get("token_model") or {}).get("seats") or []:
        if seat_fit.get("basis") == "projected":
            warnings.append(
                f"{seat_fit.get('seat_name')} ({seat_fit.get('role')}) has no "
                f"measured history in this store; its tokens are projected "
                f"from {seat_fit.get('fallback')}"
            )
    if write:
        destination = out or (quote.plan.get("run_dir") if quote.plan else None)
        if not destination:
            raise click.ClickException(
                "the plan was not loaded from a directory, so there is nowhere "
                "to write QUOTE.json; pass --out"
            )
        data["written_to"] = str(quote.write(destination))
    return _envelope("quote", data=data, warnings=warnings)


@council_cmd.command(name="quote")
@click.option(
    "--plan",
    required=True,
    type=click.Path(exists=True),
    help="Run directory holding PLAN.json, or the plan file itself.",
)
@click.option("--profile", default=None, help="Bench to quote. Default: the plan's own, then the configured default.")
@click.option("--seat", multiple=True, metavar="SEAT_NAME=ALIAS", help="Override one seat (repeatable).")
@click.option(
    "--meta-fire-rate",
    default=None,
    type=float,
    help="Fraction of items reaching meta. Default: 0.241, measured over n=19,268.",
)
@click.option(
    "--out",
    default=None,
    type=click.Path(),
    help="Where to write QUOTE.json. Default: the run directory the plan came from.",
)
@click.option(
    "--no-write",
    "no_write",
    is_flag=True,
    default=False,
    help="Compute and print the quote without writing QUOTE.json.",
)
@_json_option
@click.pass_context
def quote_cmd(
    ctx: click.Context,
    plan: str,
    profile: Optional[str],
    seat: Sequence[str],
    meta_fire_rate: Optional[float],
    out: Optional[str],
    no_write: bool,
    as_json: bool,
) -> None:
    """Price a run plan and write QUOTE.json. SPENDS NOTHING.

    Stage 2 of the standing pipeline. Every proposer prompt is rendered
    locally and hashed with the same `prompt_hash` the worker uses, so reuse
    is COUNTED rather than guessed; only meta's volume and the repair pass
    are projected, and every line says which it is.

    \b
    What the quote will not do:
      - it will not price a gateway that has no rate in `ai_council.rates`;
        it reports `unpriced`. A vendor list rate is a FLOOR, not a cost.
      - it will not add the queues together for a wall clock. Queues run
        concurrently, so the estimate is the slowest one.
    """
    util = _ready(ctx)
    try:
        envelope = build_quote(
            util,
            plan=plan,
            profile=profile,
            seat=seat,
            meta_fire_rate=meta_fire_rate,
            out=out,
            write=not no_write,
        )
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 - a bad plan or store is a CLI error
        raise _fail(exc) from exc
    _emit(envelope, as_json=as_json)


# ---------------------------------------------------------------------------
# disposition -- which jobs are a RESULT
# ---------------------------------------------------------------------------
#
# The store cannot answer this. `jobs.status` and `jobs.deleted_at` do not
# distinguish a published chunk from a cancelled 19,276-item look-alike, and
# the biggest, most authoritative-looking job in the production store is one
# of the two that must never be loaded. These verbs are the curated layer:
# `seed` and `declare` write the append-only declaration ledger, `publish`
# projects it into `council.job_disposition`, and `check` is the gate a
# publisher calls -- it exits NON-ZERO and NAMES every job that is not
# declared `published`, including one that has no declaration at all.


def _declared_by(ctx: click.Context, declared_by: Optional[str]) -> str:
    """Who is declaring.  The flag, else the facade's provenance user."""
    if declared_by:
        return declared_by
    return str(_council(ctx).user())


def _ledger(store_id: str, root: Optional[str]) -> Any:
    from ...domains.kbase.berdl.council_disposition import (
        DispositionLedger,
        resolve_ledger_path,
    )

    return DispositionLedger(resolve_ledger_path(store_id, root=root))


def _sink(lakehouse: Optional[str]) -> Any:
    from ...domains.kbase.berdl.council_sink import open_sink

    return open_sink(lakehouse)


def _store_id_option(function: Any) -> Any:
    return click.option(
        "--store-id",
        required=True,
        help="The store's DECLARED archive identity. Never derived from a "
        "path: a store that moves must keep its id or every archived row "
        "becomes unjoinable.",
    )(function)


def _ledger_root_option(function: Any) -> Any:
    return click.option(
        "--ledger-root",
        default=None,
        type=click.Path(file_okay=False),
        help="Root for declaration ledgers. Default: $KBU_COUNCIL_DISPOSITION_DIR, "
        "else ~/.kbu/council/dispositions.",
    )(function)


def _lakehouse_option(function: Any) -> Any:
    return click.option(
        "--lakehouse",
        default=None,
        help="Where council.job_disposition lives: a local SQLite staging "
        "file, or 'berdl' for the live namespace (in-pod only). Default: berdl.",
    )(function)


@council_cmd.group(name="disposition")
def disposition_cmd() -> None:
    """Declare and resolve what each job's output is FOR.

    \b
    A job with NO declaration is NOT publishable. Absence never reads as an
    implicit `published`, because the two cancelled 19,276-item jobs in the
    production store look authoritative by label, by size and by status, and
    a clearinghouse slot is frozen on first write.

    A `published` disposition is NECESSARY BUT NOT SUFFICIENT: the publisher
    also requires the job to be listed in its campaign's publication bundle.
    """


@disposition_cmd.command(name="seed")
@click.argument("provenance", type=click.Path(exists=True, dir_okay=False))
@_store_id_option
@click.option("--declared-by", default=None, help="Who is declaring. Default: the config/OS user.")
@_ledger_root_option
@_json_option
@click.pass_context
def disposition_seed_cmd(
    ctx: click.Context,
    provenance: str,
    store_id: str,
    declared_by: Optional[str],
    ledger_root: Optional[str],
    as_json: bool,
) -> None:
    """Seed declarations from a provenance JSON file. IDEMPOTENT.

    Every disposition is DERIVED from the file's own structure -- its
    `LOAD_THESE` list and each job's prose -- and a shape that classifies to
    nothing is refused rather than guessed at. Re-seeding the same file
    appends nothing and restamps nothing.
    """
    from ...domains.kbase.berdl.council_disposition import seed_from_provenance

    try:
        report = seed_from_provenance(
            _ledger(store_id, ledger_root),
            provenance,
            declared_by=_declared_by(ctx, declared_by),
        )
    except Exception as exc:  # noqa: BLE001 - a bad seed file is a CLI error
        raise _fail(exc) from exc
    report["store_id"] = store_id
    _emit(_envelope("disposition seed", data=report), as_json=as_json)


@disposition_cmd.command(name="declare")
@click.argument("job_id")
@click.argument("disposition")
@_store_id_option
@click.option("--reason", required=True, help="Why. Stored verbatim.")
@click.option("--declared-by", default=None, help="Who is declaring. Default: the config/OS user.")
@_ledger_root_option
@_json_option
@click.pass_context
def disposition_declare_cmd(
    ctx: click.Context,
    job_id: str,
    disposition: str,
    store_id: str,
    reason: str,
    declared_by: Optional[str],
    ledger_root: Optional[str],
    as_json: bool,
) -> None:
    """Declare ONE job's disposition, recording who said so and when.

    A change keeps the superseded declaration: the ledger is append-only, so
    "who said this was a pilot before somebody published it" stays
    answerable.
    """
    from ...domains.kbase.berdl.council_disposition import declare

    try:
        report = declare(
            _ledger(store_id, ledger_root),
            job_id=job_id,
            disposition=disposition,
            reason=reason,
            declared_by=_declared_by(ctx, declared_by),
        )
    except Exception as exc:  # noqa: BLE001 - a bad disposition is a CLI error
        raise _fail(exc) from exc
    report["store_id"] = store_id
    _emit(_envelope("disposition declare", data=report), as_json=as_json)


@disposition_cmd.command(name="list")
@_store_id_option
@_ledger_root_option
@_json_option
def disposition_list_cmd(store_id: str, ledger_root: Optional[str], as_json: bool) -> None:
    """List the CURRENT declaration per job, from the ledger."""
    ledger = _ledger(store_id, ledger_root)
    current = ledger.current()
    rows = [
        {
            "job_id": job_id,
            "disposition": record.get("disposition"),
            "declared_by": record.get("declared_by"),
            "declared_at": record.get("declared_at"),
            "reason": record.get("reason"),
        }
        for job_id, record in sorted(current.items())
    ]
    data = {
        "store_id": store_id,
        "ledger": str(ledger.path),
        "jobs": len(rows),
        "declarations": rows,
        "published": [r["job_id"] for r in rows if r["disposition"] == "published"],
    }
    _emit(_envelope("disposition list", data=data), as_json=as_json)


@disposition_cmd.command(name="publish")
@_store_id_option
@click.option("--snapshot-id", required=True, help="The VERIFIED digest of the snapshot these rows belong to.")
@click.option("--snapshot-seq", required=True, type=int, help="The explicit monotonic sequence for this snapshot.")
@click.option("--load-batch-id", required=True, help="This load's batch id (idempotency + provenance).")
@click.option("--store-host", default=None, help="Where the snapshot was taken. Default: this host.")
@_ledger_root_option
@_lakehouse_option
@_json_option
def disposition_publish_cmd(
    store_id: str,
    snapshot_id: str,
    snapshot_seq: int,
    load_batch_id: str,
    store_host: Optional[str],
    ledger_root: Optional[str],
    lakehouse: Optional[str],
    as_json: bool,
) -> None:
    """Write the ledger's current projection into council.job_disposition.

    REFUSES a second write for the same (store_id, snapshot_id): the table is
    keyed one-disposition-per-job-per-snapshot, so a second projection would
    duplicate every job.
    """
    import socket

    from ...domains.kbase.berdl.council_disposition import write_dispositions

    try:
        report = write_dispositions(
            _sink(lakehouse),
            _ledger(store_id, ledger_root),
            store_id=store_id,
            snapshot_id=snapshot_id,
            snapshot_seq=snapshot_seq,
            store_host=store_host or socket.gethostname(),
            load_batch_id=load_batch_id,
        )
    except Exception as exc:  # noqa: BLE001 - a refused write is a CLI error
        raise _fail(exc) from exc
    _emit(_envelope("disposition publish", data=report), as_json=as_json)


@disposition_cmd.command(name="check")
@click.argument("job_ids", nargs=-1, required=True)
@click.option("--store-id", default=None, help="Restrict to one store's dispositions.")
@click.option("--snapshot-id", default=None, help="Read one snapshot instead of the current one.")
@_lakehouse_option
@_json_option
def disposition_check_cmd(
    job_ids: Sequence[str],
    store_id: Optional[str],
    snapshot_id: Optional[str],
    lakehouse: Optional[str],
    as_json: bool,
) -> None:
    """THE GATE. Exit non-zero, naming every job that is not published.

    Goes through the real resolver against the real table. An undeclared job
    fails here by name -- which is the whole point, because nothing else in
    the store would have stopped it.
    """
    from ...domains.kbase.berdl.council_disposition import require_published

    try:
        resolved = require_published(
            _sink(lakehouse),
            job_ids,
            store_id=store_id,
            snapshot_id=snapshot_id,
        )
    except Exception as exc:  # noqa: BLE001 - the refusal IS this verb's answer
        raise _fail(exc) from exc
    _emit(
        _envelope("disposition check", data={"published": resolved, "jobs": len(resolved)}),
        as_json=as_json,
    )
