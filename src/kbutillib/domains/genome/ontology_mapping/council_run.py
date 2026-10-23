"""Stage 4 of the standing ``function_to_go`` pipeline: the approved RUN.

:func:`council_run` is the verb behind ``kbu ontomap council-run``.  It is
what the 2026-10-20 hand-driven run did without: a gate, a ceiling, chunked
submission with resume markers, a declared retry pass, and a record of what
each chunk actually cost.  It replaces the throwaway ``run_chunks.sh``.

**IT OWNS NO COUNCIL MACHINERY.**  Every step is a call into something that
already exists -- :func:`~kbutillib.domains.ai.council.approval.validate_approval`
for the gate, the store's ``endpoint_state`` for the ceiling, the store's
``set_halt`` for stopping, ``CouncilUtils.submit``/``run``/``job_status`` for
the work, and ``RUN.json``'s existing contract for the hand-off to stage 5.
What is left here is the ORDER those are called in and the bookkeeping, which
is the whole of stage 4.

THIS MODULE IMPORTS NO NUMPY, on purpose, exactly as :mod:`.unrun` and
:mod:`.verdicts` do not: :mod:`.ontology_mapping_utils` imports
:mod:`.go_text` (numpy / scikit-learn / joblib) because stage 1 RANKS, and
stage 4 does not rank anything.  A run stage living there would be untestable
-- and unrunnable -- on a host without the retrieval stack, for no reason at
all.  :func:`council_run` is re-exported from that module so a caller finds
it beside the other stages.

ONE CHUNK AT A TIME, AND THAT IS LOAD-BEARING.  The chunk boundaries are the
plan's own (:data:`~.ontology_mapping_utils.PLAN_CHUNK_SIZE`, ~2,000 items)
and this stage submits and drains them ONE AT A TIME
(:data:`~.ontology_mapping_utils.PLAN_CHUNKS_IN_FLIGHT`).  Submitting them all
up front would rebuild exactly the pending set the chunking exists to avoid,
because THE DAEMON DRAINS EVERY CLAIMABLE JOB -- it does not work one job and
stop.  This is a WORKAROUND FOR KBUtilLib ISSUE 84: the council store's
``claim_calls`` is synchronous on the worker event loop and its cost is linear
in the pending set, measured at 217 ms with 19k pending rows against 10.4 ms
at 2k.  That inflated service times 6-9x on BOTH gateways at once -- with
queue wait at 0.00, which is how it was identified -- and collapsed
throughput 10.8x.

THE CEILING IS THE STORE'S OWN COUNTER, NEVER A SECOND ONE.  ``endpoint_state``
already carries ``budget_epoch``, ``epoch_tokens``, ``lifetime_tokens``,
``halt_kind`` and ``halt_message`` per endpoint, and the worker already adds
every call's tokens to it.  So this stage OPENS AN EPOCH per gateway at the
start of the run (``reset_budget``) and then watches ``epoch_tokens``; when a
gateway's sum crosses the approved ceiling it writes the store's own HALT.  A
counter of its own here would be a parallel truth that disagreed with the
store's under exactly the conditions that matter -- a resumed run, a daemon
working alongside, a call settled while the runner was asleep.

VERIFIED, NOT ASSUMED: THE HALT STOPS A RUNNING WORKER.  The store's
claimable-call query excludes any call whose endpoint is halted
(``store/queries.py::select_claimable``, the ``~halted`` condition), and
``CouncilWorker.work``'s idle check reports a halted endpoint as a ``halted``
BLOCKER and returns rather than spinning
(``engine/worker.py::_scope_state``).  So a halt written mid-run stops the
next claim, lets the calls already in flight finish and be banked, and brings
the worker out of its loop.  It did NOT have to be made to do this and no
second stopping mechanism was added;
``tests/domains/test_ontomap_council_run.py::TestCeilingHaltsARunningWorker``
pins it against a real store and a real worker.

CREDENTIALS ARE RESOLVED HERE, AND AN UNRESOLVABLE ONE IS A REFUSAL.  Neither
``ARGO_USER`` nor the AMSC token is exported in any shell on this host, and a
council started without them DOES NOT FAIL LOUDLY: the ARGO api type reports
every call as an ``auth`` failure (which at least halts), while an
OpenAI-compatible endpoint with no key falls back to the literal ``EMPTY``
and the gateway answers 401.  So the run refuses to start rather than
discovering it 9 hours in.  This is what retires the ``kbu-env.sh`` wrapper:
the command resolves what it needs, from the environment first and then from
:data:`AMSC_TOKEN_PATH`, and says which.

LAUNCHING A RUN THAT OUTLIVES ITS SESSION.  :func:`council_run` runs in the
FOREGROUND and detaching is the operator's act, not this function's -- see
:data:`DETACH_NOTE`, which the CLI prints in its help.  On this host
``setsid``, ``nohup`` and ``disown`` do NOT escape the launching unit's
cgroup, and systemd kills everything in it without a trace.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "AMSC_TOKEN_PATH",
    "CHUNK_MARKER_DIR",
    "CREDENTIAL_CHECKS",
    "DEFAULT_CHUNK_TIMEOUT_S",
    "DEFAULT_DRAIN_GRACE_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DETACH_NOTE",
    "HALT_KINDS",
    "RETRY_ERROR_KIND",
    "RETRY_ROUND_CAP",
    "RUN_SCHEMA_VERSION",
    "STOP_FILENAME",
    "RunRefused",
    "council_run",
    "gateway_endpoints",
    "invalid_output_items",
    "resolve_credentials",
    "run_summary",
]

#: Schema version of the ``RUN.json`` this stage writes.  Bumped only on a
#: BREAKING change; additive fields do not bump it.  Stage 5 reads exactly
#: two things out of it -- ``chunks[].job_id`` and ``chunks[].retry`` (see
#: ``ontology_mapping_utils._run_job_ids``) -- so those two spellings are the
#: part of this contract that is not this stage's to change alone.
RUN_SCHEMA_VERSION = 1

#: Subdirectory of the run directory holding the per-chunk markers.
#:
#: Two files per chunk, and the pair is what makes a resume exact: a
#: ``.job`` marker is written BEFORE the chunk is drained and names the
#: council job it was submitted as, and a ``.done`` marker is written only
#: once that job has settled.  So a resume after a killed chunk re-executes
#: ONLY that chunk, against the job it already has rather than a duplicate
#: submission of the same 2,000 items.
CHUNK_MARKER_DIR = "chunks"

#: How many RETRY ROUNDS a run may make.  Two.
#:
#: The retry pass exists because ``invalid_output`` is NOT in the worker's
#: ``RETRYABLE_KINDS`` (which is only ``transport`` and ``timeout``) and ARGO
#: truncates silently with no length finish-reason, so a cut-off reply is
#: abandoned on its FIRST attempt.  113 items failed exactly that way in the
#: completed run and 105 of them came back on a plain retry, for 0.30% of run
#: cost.  A cap of two and not "until it stops helping": the second round's
#: yield is the first round's residue, and an uncapped loop over a
#: deterministically truncating seat would spend forever.
RETRY_ROUND_CAP = 2

#: The call failure kind the retry pass is over.  See
#: :data:`RETRY_ROUND_CAP` for why this kind and no other.
RETRY_ERROR_KIND = "invalid_output"

#: Per-chunk wall-clock budget, in seconds.  Six hours.
#:
#: A JUDGEMENT with a measurement behind it: the completed 19,276-item run
#: took 9.46 h in total, so one ~2,000-item chunk is ~1 h of honest work and
#: six is generous enough that a merely slow chunk is never cut off.  It
#: exists so ONE bad chunk -- a wedged CLI, a gateway that accepts and never
#: answers -- cannot hold the remaining chunks forever; the run halts, says
#: so, and resumes on that chunk.
DEFAULT_CHUNK_TIMEOUT_S = 6 * 60 * 60

#: How often the supervisor reads the ceiling, the halts and the stop file.
#:
#: Five seconds.  Each pass is a handful of indexed SQLite reads against a
#: local file, and the thing being bounded is OVERSHOOT: at the measured
#: 199.2M tokens over 9.46 h (~5.8M tokens/h) five seconds is ~8k tokens of
#: possible overshoot past the ceiling, against a ceiling that already
#: carries 25% headroom.
DEFAULT_POLL_INTERVAL_S = 5.0

#: How long a halted chunk is given to drain before the runner stops waiting.
#:
#: Calls already in flight are already being paid for, so the halt stops the
#: NEXT claim and this is how long their answers are still banked for.  Three
#: minutes covers the 180 s default seat timeout.
DEFAULT_DRAIN_GRACE_S = 200.0

#: A file in the run directory whose PRESENCE stops the run at the next
#: supervisor pass.  A file and not a signal, because the operator who needs
#: it is usually on another terminal (or another machine, through the synced
#: run directory) from the one holding the foreground run.
STOP_FILENAME = "STOP"

#: Where the AMSC token is read from when the endpoint's ``api_key_env`` is
#: not already set in the environment.
AMSC_TOKEN_PATH = "~/.config/amsc/token"

#: The credential checks :func:`resolve_credentials` makes, in order.  A
#: refusal always NAMES one of these, for the same reason an approval refusal
#: names its check: "credentials are missing" leaves an operator to guess
#: which of two unrelated things to export.
CREDENTIAL_CHECKS: Tuple[str, ...] = ("argo_user", "amsc_token")

#: The ``endpoint_state.halt_kind`` values THIS STAGE writes, as against the
#: ones the worker writes for a provider failure.
#:
#: ``budget_exhausted`` is deliberately the worker's own spelling, because it
#: means the same thing and ``store.reset_budget`` already clears exactly that
#: kind.  The other two are this stage's, and they are NOT cleared by a new
#: budget epoch: an operator stop and a wedged chunk are not fixed by giving
#: the run more tokens.
HALT_KINDS: Dict[str, str] = {
    "ceiling": "budget_exhausted",
    "operator_stop": "operator_stop",
    "chunk_timeout": "chunk_timeout",
}

#: What an operator must do to launch a run that outlives its session, on
#: this host.  Quoted verbatim into ``kbu ontomap council-run --help``.
DETACH_NOTE = (
    "THIS COMMAND RUNS IN THE FOREGROUND and detaching it is yours to do. "
    "On this host a run that must outlive its session has to be started "
    "under its own scope:\n"
    "\n"
    "    systemd-run --user --scope --collect kbu ontomap council-run "
    "--run-dir <dir>\n"
    "\n"
    "`setsid`, `nohup` and `disown` DO NOT WORK here: none of them escapes "
    "the launching unit's cgroup, and systemd kills everything in that "
    "cgroup when the unit goes away -- without a trace in the run's own log. "
    "A 9-hour run lost that way looks identical to a crash."
)


class RunRefused(Exception):
    """Stage 4 will not start, and :attr:`check` says why.

    Carries the failed check as a field as well as in the message, exactly as
    :class:`~kbutillib.domains.ai.council.approval.ApprovalRefused` does, so a
    caller can branch on WHICH check failed without parsing English.  An
    approval refusal is re-raised unchanged rather than wrapped -- its own
    ``.check`` is already one of the six the gate names.
    """

    def __init__(self, check: str, detail: str) -> None:
        self.check = check
        self.detail = detail
        super().__init__(f"council_run check {check!r} failed: {detail}")


def _now_iso(moment: Optional[datetime] = None) -> str:
    """*moment* (default: now) as an ISO-8601 UTC string with a ``Z``."""
    moment = moment or datetime.now(tz=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The gateway -> endpoint map, out of the plan
# ---------------------------------------------------------------------------


def gateway_endpoints(plan: Mapping[str, Any]) -> Dict[str, List[str]]:
    """``{gateway: [endpoint, ...]}`` from a plan's ``transport_allocation``.

    THE PLAN'S OWN RECORD AND NOT THE LIVE CONFIG.  The approved ceiling is
    keyed by GATEWAY (the payer: an endpoint's ``account``, else its alias)
    while the store's spend counter is keyed by ENDPOINT, so something has to
    join them -- and the join has to be the one the quote was priced against.
    Stage 1 writes that mapping per seat, per run, precisely because which
    gateway a seat is routed over is the thing this pipeline iterates on.

    An endpoint reachable from two gateways cannot happen (a gateway IS the
    endpoint's account), so each endpoint appears under exactly one key.
    """
    out: Dict[str, List[str]] = {}
    for row in plan.get("transport_allocation") or ():
        if not isinstance(row, Mapping):
            continue
        gateway = str(row.get("gateway") or "").strip()
        endpoint = str(row.get("endpoint") or "").strip()
        if not gateway or not endpoint:
            continue
        bucket = out.setdefault(gateway, [])
        if endpoint not in bucket:
            bucket.append(endpoint)
    return {gateway: sorted(endpoints) for gateway, endpoints in sorted(out.items())}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def resolve_credentials(
    plan: Mapping[str, Any],
    config: Any = None,
    *,
    environ: Optional[Dict[str, str]] = None,
    amsc_token_path: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Resolve every credential this run's seats need, or REFUSE.

    RESOLVES, not merely checks: an endpoint whose ``api_key_env`` is unset
    but whose token is on disk has that token EXPORTED into *environ*, which
    is what lets ``kbu ontomap council-run`` be typed into a bare shell and
    retires the ``kbu-env.sh`` wrapper.  Nothing is written to disk and no
    credential is returned -- each row reports the NAME of what was resolved
    and where it came from, never the value.

    Per endpoint kind:

    * ``argo`` -- ARGO authenticates by ANL username, read from
      ``$ARGO_USER`` (or the endpoint's own ``user_env``).  There is no file
      fallback: the username is not a secret and there is no conventional
      place for it.  Unset is ``argo_user``.
    * ``openai`` -- the bearer comes from ``api_key_env``.  Unset in the
      environment falls back to :data:`AMSC_TOKEN_PATH`; neither is
      ``amsc_token``.  An endpoint declaring NO ``api_key_env`` needs nothing
      (a local vLLM port ignores the header's value), and is reported as
      such rather than refused.
    * ``claude_cli`` / ``codex_cli`` -- an interactive subscription login,
      which lives in the CLI's own state and is checked by that api type's
      billing preflight.  Nothing here to resolve.
    * ``fake`` / ``replay`` -- test transports; nothing to resolve.

    Args:
        plan: the parsed ``PLAN.json``, for its ``transport_allocation``.
        config: a ``CouncilConfig`` (its ``endpoint(name)`` accessor is used)
            or a mapping of the same.  ``None`` falls back to the plan's own
            recorded ``endpoint_kind`` per seat, which is enough to know
            whether a credential is needed but not to know a non-default
            ``user_env``/``api_key_env``.
        environ: the environment to read and to export into.  Defaults to
            ``os.environ``.
        amsc_token_path: override :data:`AMSC_TOKEN_PATH`.

    Returns:
        One row per endpoint: ``{endpoint, kind, requires, name, source}``,
        where ``source`` is ``environment``, ``file:<path>`` or ``none``.

    Raises:
        RunRefused: a credential this run needs cannot be resolved.  ``.check``
            is one of :data:`CREDENTIAL_CHECKS`.
    """
    environ = os.environ if environ is None else environ
    token_path = Path(str(amsc_token_path or AMSC_TOKEN_PATH)).expanduser()

    def _endpoint_config(name: str) -> Dict[str, Any]:
        if config is None:
            return {}
        accessor = getattr(config, "endpoint", None)
        if callable(accessor):
            try:
                return dict(accessor(name))
            except Exception:  # noqa: BLE001 - an unknown endpoint is not fatal
                return {}
        if isinstance(config, Mapping):
            section = config.get("endpoints")
            section = section if isinstance(section, Mapping) else config
            value = section.get(name)
            return dict(value) if isinstance(value, Mapping) else {}
        return {}

    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for seat in plan.get("transport_allocation") or ():
        if not isinstance(seat, Mapping):
            continue
        endpoint = str(seat.get("endpoint") or "").strip()
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        cfg = _endpoint_config(endpoint)
        kind = str(cfg.get("kind") or seat.get("endpoint_kind") or "").strip()

        if kind == "argo":
            name = str(cfg.get("user_env") or "ARGO_USER")
            if not str(environ.get(name) or "").strip():
                raise RunRefused(
                    "argo_user",
                    f"endpoint {endpoint!r} is ARGO, which authenticates by ANL "
                    f"username, and ${name} is unset or empty in this process. "
                    "It is exported in NO shell on this host, and a council "
                    "started without it does not fail loudly -- the seats "
                    "report unavailable and the calls park. Export it "
                    f"(`export {name}=<anl-username>`) and start the run again.",
                )
            rows.append(
                {
                    "endpoint": endpoint,
                    "kind": kind,
                    "requires": "anl_username",
                    "name": name,
                    "source": "environment",
                }
            )
            continue

        if kind == "openai":
            name = str(cfg.get("api_key_env") or "").strip()
            if cfg.get("api_key"):
                rows.append(
                    {
                        "endpoint": endpoint,
                        "kind": kind,
                        "requires": "bearer_token",
                        "name": "api_key",
                        "source": "config",
                    }
                )
                continue
            if not name:
                # A local vLLM port requires the header to be PRESENT but
                # ignores its value, so there is genuinely nothing to resolve
                # -- and inventing a refusal here would refuse a working
                # endpoint.  Reported, not silently skipped.
                rows.append(
                    {
                        "endpoint": endpoint,
                        "kind": kind,
                        "requires": "none",
                        "name": "",
                        "source": "none",
                    }
                )
                continue
            if str(environ.get(name) or "").strip():
                rows.append(
                    {
                        "endpoint": endpoint,
                        "kind": kind,
                        "requires": "bearer_token",
                        "name": name,
                        "source": "environment",
                    }
                )
                continue
            token = ""
            if token_path.is_file():
                try:
                    token = token_path.read_text(encoding="utf-8").strip()
                except OSError as exc:
                    raise RunRefused(
                        "amsc_token",
                        f"endpoint {endpoint!r} needs ${name} and it is unset, "
                        f"so {token_path} was read as the fallback and could "
                        f"not be: {exc}",
                    ) from exc
            if not token:
                raise RunRefused(
                    "amsc_token",
                    f"endpoint {endpoint!r} needs a bearer token: ${name} is "
                    f"unset or empty and {token_path} "
                    + ("is empty" if token_path.is_file() else "does not exist")
                    + ". Neither is exported in any shell on this host, and an "
                    "OpenAI-compatible endpoint with no key does not fail "
                    "loudly -- it sends the literal 'EMPTY' and the gateway "
                    "answers 401 for every call of the run.",
                )
            environ[name] = token
            rows.append(
                {
                    "endpoint": endpoint,
                    "kind": kind,
                    "requires": "bearer_token",
                    "name": name,
                    "source": f"file:{token_path}",
                }
            )
            continue

        rows.append(
            {
                "endpoint": endpoint,
                "kind": kind,
                "requires": (
                    "interactive_subscription_login"
                    if kind in ("claude_cli", "codex_cli")
                    else "none"
                ),
                "name": "",
                "source": "none",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# The supervisor: the ceiling, the halts, the stop
# ---------------------------------------------------------------------------


class _Supervisor:
    """Watches the store's own counters and halts the run when it must.

    HOLDS NO COUNTER OF ITS OWN.  Spend is read out of
    ``endpoint_state.epoch_tokens`` -- the number the worker itself writes
    through ``add_tokens`` -- and summed per gateway over that gateway's
    endpoints.  :meth:`halt` writes the store's own halt, which is what
    actually stops a worker (see the module docstring).
    """

    def __init__(
        self,
        store: Any,
        *,
        ceiling_tokens: Mapping[str, int],
        endpoints_by_gateway: Mapping[str, Sequence[str]],
        run_dir: Path,
        stop_event: Optional[Any] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.store = store
        self.ceiling_tokens = {str(k): int(v) for k, v in dict(ceiling_tokens).items()}
        self.endpoints_by_gateway = {
            str(k): list(v) for k, v in dict(endpoints_by_gateway).items()
        }
        self.run_dir = Path(run_dir)
        self.stop_event = stop_event
        self._clock = clock or time.monotonic
        #: Halts THIS supervisor wrote, so a pre-existing or worker-written
        #: halt is still reported as a gateway halt rather than mistaken for
        #: our own.
        self.written: Dict[str, str] = {}

    # -- reading the store ------------------------------------------------

    def endpoints(self) -> List[str]:
        """Every endpoint of every gateway, once."""
        out: List[str] = []
        for names in self.endpoints_by_gateway.values():
            for name in names:
                if name not in out:
                    out.append(name)
        return out

    def spend(self) -> Dict[str, int]:
        """``{gateway: epoch_tokens}``, summed over the gateway's endpoints."""
        return {
            gateway: sum(
                int(self.store.get_endpoint_state(endpoint)["epoch_tokens"])
                for endpoint in endpoints
            )
            for gateway, endpoints in self.endpoints_by_gateway.items()
        }

    def open_epoch(self) -> Dict[str, Dict[str, int]]:
        """Start a fresh budget epoch on every endpoint of every gateway.

        ONCE PER RUN, and only on first use -- never on a resume.  The
        ceiling covers the WHOLE run, so what a resumed run already spent
        counts against it (which is exactly what
        ``validate_approval(resume=True)`` warns about); zeroing the counter
        on resume would hand the run its ceiling again.
        """
        epochs: Dict[str, Dict[str, int]] = {}
        for gateway, endpoints in self.endpoints_by_gateway.items():
            epochs[gateway] = {}
            for endpoint in endpoints:
                state = self.store.reset_budget(endpoint)
                epochs[gateway][endpoint] = int(state["budget_epoch"])
        return epochs

    # -- the four trips ---------------------------------------------------

    def over_ceiling(self) -> List[Dict[str, Any]]:
        """Every gateway whose epoch spend has reached its approved ceiling."""
        spent = self.spend()
        return [
            {
                "gateway": gateway,
                "spent_tokens": spent[gateway],
                "ceiling_tokens": ceiling,
                "overshoot_tokens": spent[gateway] - ceiling,
            }
            for gateway, ceiling in self.ceiling_tokens.items()
            if gateway in spent and spent[gateway] >= ceiling
        ]

    def foreign_halts(self) -> List[Dict[str, Any]]:
        """Halts on this run's endpoints that this supervisor did not write.

        A gateway halt -- the worker's own quota/auth/billing halt, or a
        SUSTAINED rate limit -- is a reason to stop the run and not a
        condition to wait out: every remaining call to that place would fail
        identically, and the operator has to clear it.
        """
        out: List[Dict[str, Any]] = []
        for endpoint in self.endpoints():
            state = self.store.get_endpoint_state(endpoint)
            kind = str(state.get("halt_kind") or "")
            if not kind or self.written.get(endpoint) == kind:
                continue
            out.append(
                {
                    "endpoint": endpoint,
                    "halt_kind": kind,
                    "halt_message": str(state.get("halt_message") or ""),
                }
            )
        return out

    def stop_requested(self) -> Optional[str]:
        """Why the operator asked this run to stop, or ``None``."""
        if self.stop_event is not None and self.stop_event.is_set():
            return "a stop was requested in-process"
        marker = self.run_dir / STOP_FILENAME
        if marker.exists():
            return f"{marker} exists"
        return None

    def check(
        self, *, started: Optional[float] = None, timeout_s: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """The first trip that fires, or ``None``.

        Order is deliberate: the ceiling first, because it is the one with a
        cost attached to every extra second; then a gateway halt, which has
        already stopped the work; then the operator; then the clock.
        """
        over = self.over_ceiling()
        if over:
            worst = max(over, key=lambda row: row["overshoot_tokens"])
            return {
                "reason": "ceiling",
                "detail": (
                    f"gateway {worst['gateway']!r} has spent "
                    f"{worst['spent_tokens']:,} tokens of its approved "
                    f"{worst['ceiling_tokens']:,} (overshoot "
                    f"{worst['overshoot_tokens']:,}). The ceiling is checked "
                    "AFTER each call settles -- the council cannot know what a "
                    "call will cost before making it -- so it stops the next "
                    "call, not that one."
                ),
                "gateways": over,
            }
        halts = self.foreign_halts()
        if halts:
            return {
                "reason": "gateway_halt",
                "detail": "; ".join(
                    f"{row['endpoint']} halted ({row['halt_kind']}): "
                    f"{row['halt_message'] or 'no message'}"
                    for row in halts
                ),
                "halts": halts,
            }
        stop = self.stop_requested()
        if stop is not None:
            return {"reason": "operator_stop", "detail": stop}
        if (
            started is not None
            and timeout_s
            and (self._clock() - started) > float(timeout_s)
        ):
            return {
                "reason": "chunk_timeout",
                "detail": (
                    f"the chunk has been draining for "
                    f"{self._clock() - started:.0f}s, past its "
                    f"{float(timeout_s):.0f}s budget. One wedged chunk must not "
                    "hold the rest of the run: this one is halted and recorded, "
                    "and a resumed run re-executes exactly it."
                ),
            }
        return None

    # -- stopping ---------------------------------------------------------

    def halt(self, trip: Mapping[str, Any]) -> List[str]:
        """Halt every endpoint of this run, recording *trip*'s reason.

        Every endpoint and not only the offending gateway's: a ceiling trip
        on one gateway stops the run, and leaving the other gateway claiming
        would spend its own ceiling finishing items whose other seats can no
        longer answer.

        A ``gateway_halt`` trip writes nothing -- the halt it is reporting is
        already in the store, and overwriting its kind would lose the reason
        the operator needs.
        """
        reason = str(trip.get("reason") or "")
        if reason == "gateway_halt":
            return []
        kind = HALT_KINDS.get(reason, reason or "operator_stop")
        message = str(trip.get("detail") or reason)
        halted: List[str] = []
        for endpoint in self.endpoints():
            self.store.set_halt(endpoint, kind, message)
            self.written[endpoint] = kind
            halted.append(endpoint)
        return halted


# ---------------------------------------------------------------------------
# RUN.json
# ---------------------------------------------------------------------------


def _read_run(run_dir: Path) -> Dict[str, Any]:
    """The run record on disk, or ``{}``."""
    from kbutillib.domains.ai.council.approval import RUN_FILENAME

    path = run_dir / RUN_FILENAME
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise RunRefused(
            "run_record",
            f"{path}: expected a JSON object, got {type(raw).__name__}",
        )
    return dict(raw)


def _write_run(run_dir: Path, record: Mapping[str, Any]) -> Path:
    """Write ``RUN.json`` atomically.

    Atomic for the same reason ``PLAN.json`` and ``APPROVED.json`` are: this
    file is in a Dropbox-synced directory and stage 5 reads it to find the
    run's job ids, so a reader must never see it half-written.
    """
    from kbutillib.domains.ai.council.approval import RUN_FILENAME

    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / RUN_FILENAME
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(
        json.dumps(dict(record), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
    return path


def _append_chunk(run_dir: Path, entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Append one chunk record to ``RUN.json``, replacing a re-run of it.

    Replacing and not duplicating: a resumed run re-executes the chunk it was
    killed in, and two records for chunk 7 would make stage 5 read the same
    job twice and an operator read the run's item count as too high.  Keyed
    on ``(retry, index)``, so retry round 1 and chunk 1 are different rows.
    """
    record = _read_run(run_dir)
    chunks = [dict(row) for row in record.get("chunks") or () if isinstance(row, Mapping)]
    key = (bool(entry.get("retry")), int(entry.get("index", -1)))
    chunks = [
        row
        for row in chunks
        if (bool(row.get("retry")), int(row.get("index", -1))) != key
    ]
    chunks.append(dict(entry))
    chunks.sort(key=lambda row: (bool(row.get("retry")), int(row.get("index", -1))))
    record["chunks"] = chunks
    _write_run(run_dir, record)
    return record


def _merge_run(run_dir: Path, **fields: Any) -> Dict[str, Any]:
    """Merge *fields* into ``RUN.json`` and write it back."""
    record = _read_run(run_dir)
    record.update(fields)
    _write_run(run_dir, record)
    return record


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


def _marker(run_dir: Path, *, retry: bool, index: int, suffix: str) -> Path:
    stem = f"retry-{index:04d}" if retry else f"chunk-{index:04d}"
    return run_dir / CHUNK_MARKER_DIR / f"{stem}.{suffix}"


def _write_marker(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
    return path


def _read_marker(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        # A marker written by a process that died mid-write is no marker at
        # all: treating it as absent re-executes the chunk, which is the
        # cheap, correct outcome.  Treating it as present would SKIP work
        # nobody can prove was done.
        return None
    return dict(raw) if isinstance(raw, Mapping) else None


# ---------------------------------------------------------------------------
# The retry pass
# ---------------------------------------------------------------------------


def invalid_output_items(store: Any, job_id: str) -> List[str]:
    """Item ids of *job_id* that an ``invalid_output`` call cost a verdict.

    BOTH conditions, and the second one matters: an item needs a call that
    failed :data:`RETRY_ERROR_KIND` **and** must not have settled ``ok``.  A
    council of three proposers reaches a verdict with two of them, so a seat
    that truncated on an item which was decided anyway has cost nothing --
    and re-asking it would re-spend for an answer the run already has.  The
    113 items the completed run lost were the ones with NO verdict.
    """
    settled_ok = {
        str(row["item_id"])
        for row in store.get_items(job_id)
        if str(row.get("status") or "") == "ok"
    }
    out: List[str] = []
    for call in store.get_calls(job_id):
        error = call.get("error")
        if not isinstance(error, Mapping):
            continue
        if str(error.get("kind") or "") != RETRY_ERROR_KIND:
            continue
        item_id = str(call.get("item_id") or "")
        if not item_id or item_id in settled_ok or item_id in out:
            continue
        out.append(item_id)
    return out


def _tasks_for(store: Any, job_id: str, item_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """The council tasks for *item_ids*, rebuilt from the stored item rows.

    From the STORE and not from the plan: the stored rows are the questions
    that were actually asked, so a retry asks the identical question -- which
    is what makes its answer a substitute for the one that was truncated
    rather than a different experiment.
    """
    wanted = set(item_ids)
    return [
        {
            "item_id": str(row["item_id"]),
            "evidence": dict(row.get("evidence") or {}),
            "menu": list(row.get("menu") or []),
        }
        for row in store.get_items(job_id)
        if str(row["item_id"]) in wanted
    ]


# ---------------------------------------------------------------------------
# Stage 4
# ---------------------------------------------------------------------------


def council_run(
    run_dir: Any,
    *,
    resume: bool = True,
    council: Any = None,
    profile: Optional[str] = None,
    allow_unverified: bool = False,
    allow_unavailable: bool = False,
    chunk_timeout_s: float = DEFAULT_CHUNK_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    drain_grace_s: float = DEFAULT_DRAIN_GRACE_S,
    retry_rounds: int = RETRY_ROUND_CAP,
    environ: Optional[Dict[str, str]] = None,
    amsc_token_path: Optional[Any] = None,
    stop_event: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Stage 4: execute an APPROVED plan, one chunk at a time, then retry.

    Spends LLM tokens -- it is the only stage of this pipeline that does --
    and refuses to spend the first one until the approval gate passes and
    every credential resolves.

    THE ORDER, each step a reuse of something that already exists:

    1. **Gate** -- :func:`~kbutillib.domains.ai.council.approval.validate_approval`,
       whose refusal NAMES one of its six checks.  On first use the consumed
       ``quote_hash`` is recorded into ``RUN.json``
       (``approval.mark_consumed``), which is what makes a second,
       undeclared run against the same approval a refusal.
    2. **Credentials** -- :func:`resolve_credentials`, which refuses an
       unresolvable ``ARGO_USER`` or AMSC token rather than letting the seats
       report unavailable nine hours in.
    3. **Epoch** -- one fresh budget epoch per gateway at the approved
       ceiling, using the store's own ``reset_budget``.  Skipped on a resume:
       the ceiling covers the whole run.
    4. **Chunks** -- submit and drain ONE AT A TIME in plan order, with
       ``.job``/``.done`` markers, a per-chunk timeout, and a supervisor pass
       every *poll_interval_s* that halts on the ceiling, on a gateway halt
       and on an operator stop.
    5. **Retry pass** -- up to *retry_rounds* rounds over items an
       ``invalid_output`` call cost a verdict.  THE RUN IS NOT COMPLETE UNTIL
       THIS HAS RUN OR BEEN DECLARED UNNECESSARY.

    WHO DRAINS.  If anything already holds the store's execution lock -- a
    daemon, a controller, a queue child -- this function submits and WATCHES,
    because that worker drains every claimable job and a second worker on one
    queue would claim the same calls.  If the lock is free it drains the
    chunk itself in a thread, supervised from here.  Both paths are watched
    identically; the only difference is who is making the calls.

    Args:
        run_dir: the run directory holding ``PLAN.json``, ``QUOTE.json`` and
            ``APPROVED.json``.
        resume: this invocation is a declared RESUME of the run this
            directory already records.  Passed straight to the gate's own
            ``resume`` (check 6) and is what lets the ``.done`` markers be
            honoured; ``False`` demands a fresh, unconsumed approval.
        council: the ``CouncilUtils`` to work through.  Default: a fresh one.
        profile: override the bench the chunk jobs are submitted under.
            Default: the plan's own -- and a MODEL change is refused by the
            gate, so this can only re-route.
        allow_unverified: admit seats whose model is unverified.
        allow_unavailable: queue a chunk even though an endpoint is
            unavailable on this machine.  Its calls WAIT.
        chunk_timeout_s: per-chunk wall-clock budget.
        poll_interval_s: how often the supervisor reads the store.
        drain_grace_s: how long a halted chunk's in-flight calls are waited
            for.
        retry_rounds: cap on retry rounds.  ``0`` declares the pass
            unnecessary without looking, which is recorded as such.
        environ: the environment credentials are resolved from and into.
        amsc_token_path: override :data:`AMSC_TOKEN_PATH`.
        stop_event: anything with ``is_set()``; an in-process operator stop.
        now: the clock, for tests.

    Returns:
        The run record as written to ``RUN.json``, plus ``run_file``.
        ``complete`` is true only when every chunk settled AND the retry pass
        ran or was declared unnecessary.

    Raises:
        RunRefused: a credential or the run record itself refuses the run.
        ~kbutillib.domains.ai.council.approval.ApprovalRefused: the gate
            refused, naming its check.
    """
    from kbutillib.domains.ai.council.approval import (
        CONSUMED_KEY,
        RUN_FILENAME,
        mark_consumed,
        validate_approval,
    )
    from kbutillib.domains.ai.council.quote import PLAN_FILENAME, load_plan

    directory = Path(run_dir).expanduser()
    plan_file = directory / PLAN_FILENAME
    if not plan_file.exists():
        raise RunRefused(
            "plan_present",
            f"no plan at {plan_file}. Stage 1 writes it: "
            "`kbu ontomap council-plan`",
        )
    plan = json.loads(plan_file.read_text(encoding="utf-8"))

    if council is None:
        from kbutillib.domains.ai.council_utils import CouncilUtils

        council = CouncilUtils(config_file=False, token_file=None, kbase_token_file=None)
    config = council.config()

    # -- 1. the gate --------------------------------------------------------
    #
    # Before anything else, including before the store is touched: an
    # unapproved run must not even open a budget epoch.
    approval = validate_approval(
        directory,
        profile=profile if profile is not None else plan.get("profile"),
        config=config,
        now=now,
        resume=resume,
    )
    warnings: List[str] = list(approval.warnings)

    # The plan's ITEMS, loaded only now.  ``load_plan`` refuses a plan whose
    # contents no longer hash to its declared ``plan_hash``, and that refusal
    # belongs to the GATE -- which converts it into an ``ApprovalRefused``
    # naming ``plan_hash``.  Loading it first would raise a bare ValueError
    # and leave the operator to work out which of six checks had failed.
    plan_object = load_plan(directory)

    # -- 2. credentials -----------------------------------------------------
    credentials = resolve_credentials(
        plan, config, environ=environ, amsc_token_path=amsc_token_path
    )

    store = council.require_schema()
    endpoints_by_gateway = gateway_endpoints(plan)
    unpriced = sorted(set(endpoints_by_gateway) - set(approval.ceiling_tokens))
    if unpriced:
        warnings.append(
            f"the approval sets no ceiling for gateway(s) {', '.join(unpriced)}, "
            "which this plan's transport routes seats over. Their spend is "
            "recorded but NOTHING HALTS ON IT -- re-approve with "
            "--ceiling-tokens for each, or accept that those gateways are "
            "uncapped for this run."
        )

    supervisor = _Supervisor(
        store,
        ceiling_tokens=approval.ceiling_tokens,
        endpoints_by_gateway=endpoints_by_gateway,
        run_dir=directory,
        stop_event=stop_event,
    )

    runner = _Runner(
        council=council,
        store=store,
        directory=directory,
        plan=plan,
        plan_object=plan_object,
        supervisor=supervisor,
        profile=profile,
        allow_unverified=allow_unverified,
        allow_unavailable=allow_unavailable,
        chunk_timeout_s=chunk_timeout_s,
        poll_interval_s=poll_interval_s,
        drain_grace_s=drain_grace_s,
    )

    # EVERY chunk block is validated HERE, before the approval is consumed
    # and before a budget epoch is opened. A plan whose chunk blocks do not
    # describe its items is unrunnable, and discovering that on chunk 7 would
    # have spent six chunks and burned the approval for nothing.
    runner.check_chunk_bounds()

    # -- 3. the epoch, on first use only -----------------------------------
    existing = _read_run(directory)
    first_use = not str(existing.get(CONSUMED_KEY) or "").strip()
    epochs: Dict[str, Dict[str, int]] = dict(existing.get("budget_epoch") or {})
    if first_use:
        mark_consumed(directory, approval, now=now)
        epochs = supervisor.open_epoch()
        warnings.append(
            "a fresh budget epoch was opened on "
            + ", ".join(supervisor.endpoints())
            + ". The store holds ONE epoch counter per endpoint, shared with "
            "any other work in this store, so this run's ceiling is enforced "
            "against the endpoint's spend since this moment -- not against "
            "this run's calls alone. One run at a time per store is the "
            "execution lock's rule anyway."
        )
    else:
        warnings.append(
            "resuming the run this directory already records; its budget "
            "epoch was NOT reset, because the approved ceiling covers the "
            "whole run and what it already spent counts against it."
        )

    started_at = _now_iso(now)
    _merge_run(
        directory,
        schema_version=RUN_SCHEMA_VERSION,
        run_id=str(plan.get("run_id") or directory.name),
        query=str(plan.get("query") or ""),
        plan_hash=str(plan.get("plan_hash") or ""),
        parameter_set_hash=str(plan.get("parameter_set_hash") or ""),
        source=str(plan.get("source") or ""),
        started_at=started_at,
        ceiling_tokens=dict(approval.ceiling_tokens),
        budget_epoch=epochs,
        endpoints_by_gateway=endpoints_by_gateway,
        credentials=credentials,
        chunk_size=int((plan.get("concurrency") or {}).get("chunk_size") or 0),
        chunks_in_flight=1,
        chunks_in_flight_issue=(
            "KBUtilLib issue 84. ONE chunk in flight, because the daemon "
            "drains every claimable job: submitting them all up front would "
            "rebuild the pending set the chunking exists to avoid. Measured: "
            "claim cost 217 ms at 19k pending rows against 10.4 ms at 2k, a "
            "10.8x throughput collapse. When issue 84 is fixed, this changes."
        ),
        detach=DETACH_NOTE,
    )

    # -- 4. the chunks, one at a time, in plan order ------------------------
    #
    # One supervisor pass BEFORE the first submission.  A halt left in the
    # store by an earlier run -- or an auth/quota halt the operator has not
    # cleared -- would otherwise be discovered one chunk in, with 2,000 items
    # queued against an endpoint nothing can claim for.  (A BUDGET halt of
    # this run's own is already gone: `open_epoch` clears exactly that kind.)
    halt: Optional[Dict[str, Any]] = supervisor.check()
    chunk_rows: List[Dict[str, Any]] = []
    if halt is None:
        for chunk in plan.get("chunks") or ():
            if not isinstance(chunk, Mapping):
                continue
            row, halt = runner.run_chunk(chunk, resume=resume)
            chunk_rows.append(row)
            if halt is not None:
                break

    # -- 5. the retry pass, a DECLARED stage --------------------------------
    retry = {
        "round_cap": int(retry_rounds),
        "error_kind": RETRY_ERROR_KIND,
        "why": (
            f"{RETRY_ERROR_KIND} is NOT in the worker's RETRYABLE_KINDS (only "
            "transport and timeout) and ARGO truncates silently with no "
            "length finish-reason, so a cut-off reply is abandoned on its "
            "first attempt. 113 items failed that way in the completed run "
            "and 105 came back on a plain retry, for 0.30% of run cost."
        ),
        "rounds": [],
        "ran": False,
        "declared_unnecessary": False,
        "reason": "",
    }
    if halt is not None:
        retry["reason"] = (
            f"not run: the chunk pass halted ({halt['reason']}), so the items "
            "that would be retried are not yet known. Clear the halt, resume "
            "the run, and the retry pass runs then."
        )
    elif int(retry_rounds) <= 0:
        retry["declared_unnecessary"] = True
        retry["reason"] = "declared unnecessary by the caller (retry_rounds=0)"
    else:
        rounds, halt = runner.run_retry_pass(
            [str(row["job_id"]) for row in chunk_rows if row.get("job_id")],
            rounds=int(retry_rounds),
            resume=resume,
        )
        retry["rounds"] = rounds
        retry["ran"] = bool(rounds)
        if not rounds:
            retry["declared_unnecessary"] = True
            retry["reason"] = (
                f"declared unnecessary: no item of this run lost a verdict to "
                f"a {RETRY_ERROR_KIND} call, so there is nothing to re-ask."
            )
        chunk_rows.extend(rounds)

    planned = len([c for c in plan.get("chunks") or () if isinstance(c, Mapping)])
    settled = [row for row in chunk_rows if row.get("outcome") == "settled"]
    complete = (
        halt is None
        and len([row for row in settled if not row.get("retry")]) == planned
        and (retry["ran"] or retry["declared_unnecessary"])
    )
    if not complete and halt is None:
        warnings.append(
            "the run is NOT complete and nothing halted it: "
            f"{len(settled)} of {planned} chunk(s) settled and the retry pass "
            f"{'ran' if retry['ran'] else 'did not run'}. Resume it."
        )
    if halt is not None:
        warnings.append(
            f"the run HALTED ({halt['reason']}): {halt['detail']} The halt is "
            "recorded on every endpoint of this run; clear it with `kbu "
            "council endpoints reset-halt <endpoint>` (and `reset-budget` for "
            "a ceiling halt) and resume."
        )

    # WHAT THIS INVOCATION DID, as against what the run has done.  A SKIPPED
    # chunk's row in ``chunks`` is the one its original execution wrote --
    # with that execution's duration, tokens and counts -- and overwriting it
    # with "skipped" would destroy the only record of the work.  So which
    # chunks this invocation skipped, adopted or submitted is reported here
    # instead, where it cannot be confused for the run's history.
    resumed: Dict[str, List[int]] = {"skipped": [], "adopted": [], "submitted": []}
    for row in chunk_rows:
        bucket = resumed.get(str(row.get("resumed") or ""))
        if bucket is not None:
            bucket.append(int(row.get("index", -1)))

    record = _merge_run(
        directory,
        finished_at=_now_iso(),
        spend_by_gateway=supervisor.spend(),
        halt=halt,
        retry=retry,
        complete=complete,
        this_invocation=resumed,
        warnings=warnings,
    )
    return {**record, "run_file": str(directory / RUN_FILENAME)}


class _Runner:
    """The chunk loop and the retry loop, around one supervisor.

    A class only so the dozen settings the two loops share are named once;
    it holds no state the run record does not.
    """

    def __init__(
        self,
        *,
        council: Any,
        store: Any,
        directory: Path,
        plan: Mapping[str, Any],
        plan_object: Any,
        supervisor: _Supervisor,
        profile: Optional[str],
        allow_unverified: bool,
        allow_unavailable: bool,
        chunk_timeout_s: float,
        poll_interval_s: float,
        drain_grace_s: float,
    ) -> None:
        self.council = council
        self.store = store
        self.directory = directory
        self.plan = plan
        self.plan_object = plan_object
        self.supervisor = supervisor
        self.profile = profile
        self.allow_unverified = bool(allow_unverified)
        self.allow_unavailable = bool(allow_unavailable)
        self.chunk_timeout_s = float(chunk_timeout_s)
        self.poll_interval_s = max(0.01, float(poll_interval_s))
        self.drain_grace_s = float(drain_grace_s)
        self.query = str(plan.get("query") or "")
        self.run_id = str(plan.get("run_id") or directory.name)

    # -- submission -------------------------------------------------------

    def _submit(self, tasks: Sequence[Mapping[str, Any]], label: str) -> str:
        return str(
            self.council.submit(
                self.query,
                list(tasks),
                label=label,
                profile=(
                    self.profile
                    if self.profile is not None
                    else self.plan.get("profile")
                ),
                allow_unverified=self.allow_unverified,
                allow_unavailable=self.allow_unavailable,
            )
        )

    def check_chunk_bounds(self) -> int:
        """Validate EVERY chunk block against the item list.  Returns the count.

        Called before the approval is consumed and before a budget epoch is
        opened, so a plan whose chunk blocks disagree with its items costs
        nothing: a refusal discovered on chunk 7 would have spent six chunks
        and burned the approval.

        The plan hash already guarantees the ITEMS have not moved; what this
        catches is a ``chunks`` block that does not describe them, which would
        silently submit the wrong 2,000 items.
        """
        count = 0
        for chunk in self.plan.get("chunks") or ():
            if isinstance(chunk, Mapping):
                self._chunk_items(chunk)
                count += 1
        return count

    def _chunk_items(self, chunk: Mapping[str, Any]) -> List[Any]:
        """The plan items this chunk covers, checked against its own bounds."""
        offset = int(chunk.get("offset") or 0)
        count = int(chunk.get("items") or 0)
        items = list(self.plan_object.items)[offset : offset + count]
        if not items:
            raise RunRefused(
                "chunk_bounds",
                f"chunk {chunk.get('index')} names offset {offset} and "
                f"{count} item(s), but the plan holds "
                f"{len(self.plan_object.items)} item(s) -- so this chunk "
                "indexes nothing. Re-run stage 1.",
            )
        for key, got in (
            ("first_item_id", items[0].item_id),
            ("last_item_id", items[-1].item_id),
        ):
            expected = str(chunk.get(key) or "")
            if expected and expected != got:
                raise RunRefused(
                    "chunk_bounds",
                    f"chunk {chunk.get('index')} declares {key}={expected!r} "
                    f"but offset {offset} + {count} items ends at {got!r}. The "
                    "chunk block and the item list disagree, so this chunk "
                    "would submit the wrong items. Re-run stage 1.",
                )
        return items

    def _chunk_tasks(self, chunk: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """The plan items this chunk covers, as council tasks.

        Built here and not at bounds-check time: the items of a 19k-item plan
        are ~130 MB of evidence and menus (6.9 kB each, measured), and only
        the chunk about to be submitted needs its own.
        """
        return [
            {
                "item_id": item.item_id,
                "evidence": dict(item.evidence),
                "menu": [dict(entry) for entry in item.menu],
            }
            for item in self._chunk_items(chunk)
        ]

    # -- draining ---------------------------------------------------------

    def _drain_mode(self) -> Tuple[str, Optional[Dict[str, Any]]]:
        """``("watch", holder)`` when something else drains, else
        ``("foreground", None)``.

        Decided by PROBING the store's execution lock rather than by asking
        the operator, because getting it wrong in either direction is bad in
        a specific way: a foreground worker beside a daemon would have two
        workers claiming out of one queue, and watching when nothing is
        draining would wait out the chunk timeout having made no calls.
        """
        try:
            holder = self.council.execution_lock().probe()
        except Exception:  # noqa: BLE001 - an unprobeable lock is "free"
            return "foreground", None
        return ("watch", dict(holder)) if holder else ("foreground", None)

    def _settled(self, job_id: str) -> bool:
        """Has *job_id* nothing outstanding left?"""
        status = self.council.job_status(job_id)
        if str(status.get("status") or "") in ("done", "cancelled"):
            return True
        by_status = dict(status.get("by_status") or {})
        n_items = int(status.get("n_items") or 0)
        done = sum(
            int(count)
            for name, count in by_status.items()
            if name in ("ok", "error", "cancelled")
        )
        return bool(n_items) and done >= n_items

    def _claim_probe(self, job_id: str) -> Dict[str, Any]:
        """The run's own pending-call set, and what it cost to read.

        A PROXY FOR THE CLAIM COST AND LABELLED AS ONE.  The cost that
        matters is ``claim_calls``'s, which only a worker may pay -- timing
        it here would steal claims from the worker draining the chunk.  What
        is measured instead is the quantity it is linear in (the pending set)
        and the wall time of a public read over it, which is enough to see
        the regression the chunking exists to avoid: issue 84 measured 217 ms
        at 19k pending rows against 10.4 ms at 2k.
        """
        started = time.monotonic()
        try:
            status = self.council.job_status(job_id)
            pending = sum(
                sum(int(n) for n in per_endpoint.values())
                for per_endpoint in (
                    status.get("pending_calls_by_role_endpoint") or {}
                ).values()
            )
        except Exception as exc:  # noqa: BLE001 - instrumentation never fails a run
            return {"pending_calls": None, "probe_ms": None, "error": str(exc)}
        return {
            "pending_calls": pending,
            "probe_ms": round((time.monotonic() - started) * 1000, 2),
            "what_this_is": (
                "A PROXY for the claim cost, not a measurement of claim_calls: "
                "pending calls of THIS run's job, and the wall time of one "
                "public read over them. KBUtilLib issue 84 -- claim cost is "
                "linear in the pending set, 217 ms at 19k rows against 10.4 ms "
                "at 2k."
            ),
        }

    def _drain(
        self, job_id: str, *, timeout_s: Optional[float] = None
    ) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Drain *job_id*, supervised.  ``(outcome, halt, drain)``.

        *outcome* is ``settled`` or ``halted``; *halt* is the trip that
        stopped it, or ``None``.
        """
        mode, holder = self._drain_mode()
        started = time.monotonic()
        error: Dict[str, Any] = {}
        thread: Optional[threading.Thread] = None

        if mode == "foreground":

            def _work() -> None:
                try:
                    self.council.run(job_id)
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    error["exc"] = exc

            thread = threading.Thread(
                target=_work, name=f"kbu-ontomap-chunk-{job_id}", daemon=True
            )
            thread.start()

        halt: Optional[Dict[str, Any]] = None
        while True:
            halt = self.supervisor.check(
                started=started,
                timeout_s=self.chunk_timeout_s if timeout_s is None else timeout_s,
            )
            if halt is not None:
                self.supervisor.halt(halt)
                break
            if thread is not None:
                if not thread.is_alive():
                    break
            elif self._settled(job_id):
                break
            time.sleep(self.poll_interval_s)

        if thread is not None:
            # The halt stops the next CLAIM; the calls already in flight are
            # already being paid for, so they are waited out and banked.
            thread.join(self.drain_grace_s if halt is not None else None)
        if "exc" in error:
            raise error["exc"]
        drain = {
            "mode": mode,
            "drained_by": holder,
            "seconds": round(time.monotonic() - started, 2),
        }
        return ("halted" if halt is not None else "settled"), halt, drain

    # -- one chunk --------------------------------------------------------

    def run_chunk(
        self, chunk: Mapping[str, Any], *, resume: bool, retry: bool = False
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """Submit (or re-adopt) and drain ONE chunk.  ``(record, halt)``."""
        index = int(chunk.get("index") or 0)
        done_path = _marker(self.directory, retry=retry, index=index, suffix="done")
        job_path = _marker(self.directory, retry=retry, index=index, suffix="job")

        done = _read_marker(done_path) if resume else None
        if done is not None:
            row = dict(done)
            row.update({"index": index, "retry": retry, "outcome": "settled",
                        "resumed": "skipped"})
            return row, None

        adopted = _read_marker(job_path) if resume else None
        job_id = str((adopted or {}).get("job_id") or "") or None
        if job_id is None:
            tasks = (
                list(chunk["tasks"])
                if retry
                else self._chunk_tasks(chunk)
            )
            label = (
                f"{self.run_id} retry {index}" if retry
                else f"{self.run_id} chunk {index}"
            )
            job_id = self._submit(tasks, label)
            _write_marker(
                job_path,
                {
                    "job_id": job_id,
                    "index": index,
                    "retry": retry,
                    "items": len(tasks),
                    "submitted_at": _now_iso(),
                },
            )

        before = self.supervisor.spend()
        # Probed with the chunk's calls still PENDING, which is the only
        # moment the number means anything: after the drain the pending set
        # is empty by construction and every reading would be 0.
        claim_probe = self._claim_probe(job_id)
        outcome, halt, drain = self._drain(job_id)
        after = self.supervisor.spend()

        status = self.council.job_status(job_id)
        by_status = dict(status.get("by_status") or {})
        row: Dict[str, Any] = {
            "index": index,
            "retry": retry,
            "job_id": job_id,
            "items": int(status.get("n_items") or 0),
            "ok": int(by_status.get("ok") or 0),
            "error": int(by_status.get("error") or 0),
            "duration_s": drain["seconds"],
            "tokens_by_gateway": {
                gateway: after.get(gateway, 0) - before.get(gateway, 0)
                for gateway in sorted(set(before) | set(after))
            },
            "claim_probe": claim_probe,
            "drain": drain,
            "outcome": outcome,
            "job_status": str(status.get("status") or ""),
            "percent_complete": status.get("percent_complete"),
            "blockers": list(status.get("blockers") or []),
            "resumed": "adopted" if adopted else "submitted",
        }
        if outcome == "settled":
            _write_marker(done_path, {**row, "settled_at": _now_iso()})
        _append_chunk(self.directory, row)
        return row, halt

    # -- the retry pass ---------------------------------------------------

    def run_retry_pass(
        self, job_ids: Sequence[str], *, rounds: int, resume: bool
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Up to *rounds* rounds over the items an ``invalid_output`` cost.

        Each round's SOURCE is the previous round's job (the first round's is
        every chunk job), so round 2 is over the residue of round 1 and not
        over the same list again.
        """
        out: List[Dict[str, Any]] = []
        sources = [str(jid) for jid in job_ids if str(jid)]
        for round_no in range(1, max(0, int(rounds)) + 1):
            tasks: List[Dict[str, Any]] = []
            seen: set = set()
            for source in sources:
                item_ids = invalid_output_items(self.store, source)
                for task in _tasks_for(self.store, source, item_ids):
                    if task["item_id"] in seen:
                        continue
                    seen.add(task["item_id"])
                    tasks.append(task)
            if not tasks:
                break
            row, halt = self.run_chunk(
                {"index": round_no, "tasks": tasks}, resume=resume, retry=True
            )
            row["retry_round"] = round_no
            row["retried_from"] = list(sources)
            out.append(row)
            if halt is not None:
                return out, halt
            sources = [str(row["job_id"])]
        return out, None


def run_summary(record: Mapping[str, Any]) -> Dict[str, Any]:
    """The headline numbers of a run record, for a renderer.

    Derived rather than stored, so the summary an operator reads cannot drift
    from the chunk rows it is a summary OF.
    """
    chunks = [row for row in record.get("chunks") or () if isinstance(row, Mapping)]
    work = [row for row in chunks if not row.get("retry")]
    retries = [row for row in chunks if row.get("retry")]
    tokens: Dict[str, int] = {}
    for row in chunks:
        for gateway, spent in dict(row.get("tokens_by_gateway") or {}).items():
            tokens[gateway] = tokens.get(gateway, 0) + int(spent or 0)
    return {
        "chunks": len(work),
        "chunks_settled": len([row for row in work if row.get("outcome") == "settled"]),
        "retry_rounds": len(retries),
        "items": sum(int(row.get("items") or 0) for row in work),
        "ok": sum(int(row.get("ok") or 0) for row in chunks),
        "error": sum(int(row.get("error") or 0) for row in chunks),
        "duration_s": round(sum(float(row.get("duration_s") or 0) for row in chunks), 2),
        "tokens_by_gateway": tokens,
    }
