"""Can THIS machine serve the seats a job would use?

Queues are per machine (review round 2: there is no shared store and nothing
coordinates across hosts), so a call row nobody here can run is a call row
nobody will EVER run.  That is the whole reason this module exists and the
reason the check runs at SUBMIT rather than being left to the worker: on a
shared queue an unreachable endpoint is somebody else's problem to pick up,
and on a per-machine queue it is a job that sits pending forever.

One implementation, three callers:

* ``CouncilUtils.doctor`` -- reports every seat of a profile.
* ``CouncilUtils.submit`` / ``rerun`` (and therefore ``kbu council submit``
  and ``kbu council run --query``) -- refuses a job whose seats this machine
  cannot serve, unless ``allow_unavailable``.
* ``CouncilWorker`` -- re-checks an endpoint a job was created against
  despite being unservable, so those calls wait rather than spend.

Everything here is FREE: :func:`probe_endpoint` lists models, runs
``--version`` and reads billing config files.  ``complete()`` is never called,
so no probe can spend a token.

The three states are deliberately distinct.  ``unknown`` ("I could not tell")
must never be rendered as ``unavailable`` ("I checked and it is broken"): they
drive different exit codes out of ``kbu council doctor`` and different
refusals out of submit, and conflating them would either refuse a working
plug-in api type or admit a dead endpoint.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import llm
from .types import CLI_KINDS, CouncilException

logger = logging.getLogger(__name__)

__all__ = [
    "SEAT_STATES",
    "SeatUnavailable",
    "probe_endpoint",
    "probe_roster",
    "seat_state",
    "unservable_message",
]

#: What one seat may be reported as.  ``unknown`` is NOT a milder
#: ``unavailable``; see the module docstring.
SEAT_STATES = ("ready", "unknown", "unavailable")


class SeatUnavailable(CouncilException):
    """A seat's endpoint cannot be served from this machine.

    Raised at submit, before a job row exists.  Carries :attr:`seats` -- the
    per-seat reports that caused the refusal -- so a caller that wants to act
    on them does not have to parse the message.
    """

    def __init__(self, message: str, seats: Optional[Sequence[Mapping[str, Any]]] = None) -> None:
        super().__init__(message)
        self.seats: List[Dict[str, Any]] = [dict(seat) for seat in seats or ()]


async def probe_endpoint(endpoint: str, kind: str, api: Any) -> Dict[str, Any]:
    """Everything that can be learned about ONE endpoint for free.

    Probed once per endpoint rather than once per seat: two seats on the same
    endpoint would otherwise mean two ``/models`` GETs and two ``--version``
    subprocesses, which is wasted work and -- for a CLI -- two child
    processes a reader of the doctor's output cannot explain.

    Nothing here can spend a token: ``models()`` is a free GET, ``version()``
    runs ``--version``, and ``preflight()`` reads config files.
    ``complete()`` is never called.

    Every optional member is reached through ``getattr`` rather than called
    directly.  An api type is only REQUIRED to provide ``complete`` and
    ``effective_params`` (``llm.protocol.REQUIRED_MEMBERS``), so a plug-in
    with neither probe must come back as a report saying so -- never as an
    ``AttributeError`` out of a readiness check (review round 3).
    """
    probe: Dict[str, Any] = {
        "endpoint": endpoint,
        "kind": kind,
        "backend": api,
        "can_preflight": callable(getattr(api, "preflight", None)),
        "can_list_models": callable(getattr(api, "models", None)),
    }
    if kind in CLI_KINDS:
        # ``version()`` is CLI-specific and deliberately not part of the
        # ApiType contract -- an HTTP gateway has no ``--version``.
        versioner = getattr(api, "version", None)
        probe["version"] = await versioner() if callable(versioner) else None
        probe["billing"] = await llm.preflight_api(api) if probe["version"] else {}
        return probe
    if probe["can_list_models"]:
        # The listing is the readiness answer for a non-CLI endpoint, so a
        # ``preflight`` is NOT also asked for: it would be a second probe
        # whose verdict nothing reads, and for an api type that opens a client
        # to answer it that is a cost with no buyer.
        listing = await llm.models_api(api)
        # EVERY id a gateway advertises for an entry, not just the first one
        # that happens to be present.  A gateway may carry more than one
        # spelling per model and accept a DIFFERENT one than it displays:
        # ARGO's listing is
        #   {"id": "Gemini 3.5 Flash", "internal_id": "gemini35flash", ...}
        # and as of 2026-10-20 its /chat/completions accepts ONLY
        # ``internal_id`` -- the display spelling returns HTTP 400 (Claude) or
        # HTTP 500 (Gemini).  Reading just ``id`` therefore made the readiness
        # test the exact inverse of the call: the only spelling that works is
        # the one that was reported unavailable, so every ARGO seat was
        # refused at submit while the call itself would have succeeded.
        # Collecting all of them makes "served" mean "the gateway names it",
        # which is the question a seat is actually asking.
        names = set()
        for entry in listing:
            if not isinstance(entry, Mapping):
                continue
            for key in ("id", "name", "internal_id"):
                value = entry.get(key)
                if value:
                    names.add(str(value))
        names.discard("")
        probe["models"] = names
        return probe
    if probe["can_preflight"]:
        # No listing to read, but something that can say whether it would
        # work.  This is the plug-in api type case; neither shipped HTTP
        # backend has a preflight.
        probe["billing"] = await llm.preflight_api(api)
    return probe


def seat_state(
    role: str, seat: Any, probe: Mapping[str, Any], allow_unverified: bool = False
) -> Dict[str, Any]:
    """Readiness of ONE seat, read off its endpoint's probe.

    HTTP readiness is the endpoint answering ``/models`` AND the served model
    appearing in that listing.  CLI readiness is ``--version`` having run plus
    the offline billing preflight saying ``ready``.  An unverified seat is
    ``unknown`` whatever the endpoint says: nobody has confirmed that endpoint
    serves the model the seat names, and a reachable endpoint is not evidence
    that it does.

    An api type that offers NEITHER ``models()`` nor ``preflight()`` is
    ``unknown`` rather than ``unavailable`` (review round 3): there is nothing
    to ask it, and "I could not tell" is the honest answer.  An EMPTY listing
    from an api type that does have ``models()`` stays ``unavailable`` -- it
    was asked and it served nothing.
    """
    backend = probe.get("backend")
    report: Dict[str, Any] = {
        "role": role,
        "seat": seat.seat_name,
        "endpoint": seat.endpoint,
        "endpoint_kind": seat.endpoint_kind,
        "model": seat.model,
        "canonical_model": seat.canonical_model,
        "verified": bool(seat.verified),
        "state": "unknown",
        "detail": "",
    }
    if not seat.verified and not allow_unverified:
        report["detail"] = (
            "the model is marked verified: false -- nobody has confirmed this "
            "endpoint serves it; confirm it and set verified: true"
        )
        return report

    if seat.endpoint_kind in CLI_KINDS:
        version = probe.get("version")
        if not version:
            report["state"] = "unavailable"
            report["detail"] = (
                f"{getattr(backend, 'executable', seat.endpoint)} --version did not "
                "run; the CLI is not installed or not on PATH"
            )
            return report
        report["cli_version"] = version
        return _with_billing(report, probe)

    if not probe.get("can_list_models"):
        if probe.get("can_preflight"):
            return _with_billing(report, probe)
        report["detail"] = (
            f"{type(backend).__name__} declares neither models() nor preflight(), "
            f"so this machine cannot tell whether {seat.endpoint!r} serves "
            f"{seat.model!r}; pass allow_unverified to use it anyway"
        )
        return report

    names = set(probe.get("models") or ())
    if not names:
        report["state"] = "unavailable"
        report["detail"] = (
            f"{getattr(backend, 'base_url', seat.endpoint)} listed no models: it is "
            "unreachable, refused the request, or does not implement /models"
        )
        return report
    report["models_listed"] = len(names)
    if seat.model in names:
        report["state"] = "ready"
        report["detail"] = f"{seat.model} is served by {seat.endpoint}"
    else:
        report["state"] = "unavailable"
        report["detail"] = (
            f"{seat.endpoint} is reachable but does not list {seat.model!r}; it "
            f"lists {sorted(names)[:8]}"
        )
    return report


def _with_billing(report: Dict[str, Any], probe: Mapping[str, Any]) -> Dict[str, Any]:
    """Fold a ``preflight()`` verdict into *report*'s state and detail."""
    billing = dict(probe.get("billing") or {})
    state = str(billing.get("state") or "unknown")
    report["billing"] = state
    report["detail"] = str(billing.get("detail") or "")
    report["state"] = {"ready": "ready", "refused": "unavailable"}.get(state, "unknown")
    return report


async def probe_roster(
    roster: Any, backends: Mapping[str, Any], allow_unverified: bool = False
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Probe each DISTINCT endpoint of *roster* once, then report every seat.

    Returns ``(probes keyed by endpoint, one report per seat in roster
    order)``.  An endpoint *backends* holds nothing for is probed with
    ``None``, which reports ``unknown`` rather than raising: a worker that
    cannot even build an api type for an endpoint has not learned that the
    endpoint is broken.
    """
    probes: Dict[str, Dict[str, Any]] = {}
    for _role, seat in roster.seats():
        if seat.endpoint in probes:
            continue
        probes[seat.endpoint] = await probe_endpoint(
            seat.endpoint, seat.endpoint_kind, backends.get(seat.endpoint)
        )
    seats = [
        seat_state(role, seat, probes[seat.endpoint], allow_unverified)
        for role, seat in roster.seats()
    ]
    return probes, seats


def unservable_message(reports: Sequence[Mapping[str, Any]], flag: str) -> str:
    """The refusal a submit raises, naming every affected seat.

    Names the seat, its endpoint and the doctor's own detail for each one,
    because "a seat is unavailable" sends nobody anywhere: the seat name says
    which part of the council is missing, the endpoint says where to look, and
    the detail is the thing that actually has to be fixed.
    """
    lines = [
        f"  - seat {report['seat']} ({report['role']}) on endpoint "
        f"{report['endpoint']}: {report['detail'] or 'no detail'}"
        for report in reports
    ]
    return (
        "this machine cannot serve "
        f"{len(reports)} seat(s) of the requested council:\n" + "\n".join(lines) + "\n"
        "The council queue is PER MACHINE -- no other machine will ever work a "
        "seat this one cannot reach, so these calls would sit pending forever. "
        f"Fix the endpoint(s), seat a different model, or pass {flag} to queue "
        "the job anyway (its calls will wait)."
    )
