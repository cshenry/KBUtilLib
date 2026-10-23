"""Stage 3 of the standing pipeline: the approval gate, and its validation.

A quoted run becomes a RUNNING run when a human writes ``APPROVED.json`` into
the run directory beside ``PLAN.json`` and ``QUOTE.json``.  This module owns
that artifact: :func:`approve` writes one, :func:`validate_approval` is the
function STAGE 4 CALLS before it spends a token.

BE CLEAR ABOUT WHAT THIS ARTIFACT IS.  ``APPROVED.json`` is an AUDIT TRAIL
AND A BINDING CEILING.  IT IS NOT AN AUTHENTICATION BOUNDARY.  There is no
key material on these machines that would make an approval
cryptographically anyone's, and an agent with a filesystem can write the
file as easily as an operator can.  A signature scheme nothing on the fleet
could verify would add ceremony and no security, so there is none.

What the gate does buy is real, and it is what the 2026-10-20 hand-driven run
did not have:

* **The ceiling is explicit and per gateway.**  Stage 4 has a number to halt
  at, recorded before the run started rather than argued about during it.
* **The binding to plan and quote is checkable.**  ``quote_hash`` is the hash
  of the quote on disk and ``plan_hash`` is the hash of the plan the quote was
  priced against, so an approval cannot drift onto a different job.
* **A stale approval against a changed job is refused**, naming the check that
  failed -- not "invalid approval".

THE CEILING DEFAULTS TO THE QUOTE'S UPPER BAND TIMES
:data:`DEFAULT_HEADROOM`, NEVER TO THE POINT ESTIMATE.  A ceiling set at the
mean halts roughly half of all runs partway, and a run halted at 60% has spent
60% of the tokens for none of the result -- the most expensive outcome
available.  The headroom also has to cover the RETRY PASS, which happens after
the approval and is ~0.3% of run cost (measured: 113 items truncated silently,
105 recovered on a plain retry), and the repair line is not attributed to a
gateway at all, so :func:`ceiling_from_quote` distributes the unattributed
tokens across the gateways pro rata before applying the headroom.

THE APPROVAL BINDS TO THE BENCH, NOT TO THE ROSTER (Chris, 2026-10-22).  The
staleness check is on ``bench_fingerprint`` -- ``{role, canonical_model}`` per
seat and nothing else -- because the bench partitions the corpus and the
transport does not:

* A **model swap** under a standing approval is a REFUSAL.  Approving one
  experiment and running another is exactly what the gate exists to stop.
* A **transport change** -- the same models reached over a different gateway,
  CLI or GPU -- is PERMITTED and must not invalidate an approval; re-routing
  for cost is the thing we iterate on.  It does invalidate the quote's PRICE,
  so :func:`validate_approval` WARNS and names the re-routed seats.

See :func:`~.types.bench_fingerprint` for why ``Roster.fingerprint()`` is not
the key, and :func:`~.types.prompt_hash` for the half of this that is
deliberately NOT changed to match.

Example::

    from kbutillib.domains.ai.council.approval import approve, validate_approval

    approve(run_dir, approver="chenry", note="ADP1+PMI standing run")
    approval = validate_approval(run_dir)   # raises, naming the failed check
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from .quote import PLAN_FILENAME, QUOTE_FILENAME, load_plan
from .types import CouncilException, bench_fingerprint, canonical_sha256

__all__ = [
    "APPROVAL_FILENAME",
    "APPROVAL_SCHEMA_VERSION",
    "APPROVAL_CHECKS",
    "CONSUMED_KEY",
    "DEFAULT_EXPIRES_IN_DAYS",
    "DEFAULT_HEADROOM",
    "RUN_FILENAME",
    "Approval",
    "ApprovalRefused",
    "approve",
    "ceiling_from_quote",
    "live_bench",
    "mark_consumed",
    "quote_hash",
    "read_approval",
    "read_quote",
    "validate_approval",
]

#: The file an approval is written to inside a run directory.
APPROVAL_FILENAME = "APPROVED.json"

#: Schema version of the published ``APPROVED.json``.  Bumped only on a
#: BREAKING change to the object; additive fields do not bump it.
APPROVAL_SCHEMA_VERSION = 1

#: The file stage 4 records its council jobs and its CONSUMED approval in.
#: Stage 4 owns that file's contract (see
#: ``ontology_mapping_utils.RUN_FILENAME``, the same name); this module reads
#: and writes exactly one field of it, :data:`CONSUMED_KEY`.  The name is
#: spelled here rather than imported because the council package must not
#: import the genome domain -- and a top-level import of it would pull
#: numpy/sklearn into every approval check.
RUN_FILENAME = "RUN.json"

#: The ``RUN.json`` field naming the approval a run CONSUMED.  Stage 4 writes
#: it on first use (:func:`mark_consumed`), and :func:`validate_approval`
#: refuses a second, non-resume run against it: an approval is a ceiling for
#: ONE run, and silently re-spending it would double the approved cost.
CONSUMED_KEY = "approved_quote_hash"

#: Multiple applied to the quote's UPPER band to get the default ceiling.
#:
#: Not applied to the point estimate, and that is the whole point: a ceiling
#: at the mean halts about half of all runs partway, and a halted run has
#: spent its tokens for none of the result.  1.25 over the p95-derived upper
#: band also covers the retry pass (~0.3% of run cost, upper-bounded by the
#: measured 0.59% error rate), which is charged AFTER this approval is
#: written and so cannot be in the quote the operator approved.
DEFAULT_HEADROOM = 1.25

#: How long an approval is good for, in days, absent ``--expires-in``.
#:
#: Seven days, as the PRD specifies.  A JUDGEMENT rather than a measurement:
#: the run itself took 9.46 h, so a week is ample for a planned run and short
#: enough that an approval left lying in a run directory from last month
#: cannot authorise today's corpus.  An approval with NO expiry would be a
#: standing authorisation to spend, which is not what "approve this run"
#: means.  The content checks are what actually catch a changed job; the
#: expiry catches the case where nothing checkable changed but the approval is
#: simply too old to still represent a decision anyone made.
DEFAULT_EXPIRES_IN_DAYS = 7

#: Every check :func:`validate_approval` makes, in the order it makes them.
#: A refusal always names one of these, because "the approval is invalid"
#: leaves an operator to guess which of six things moved.
APPROVAL_CHECKS: Tuple[str, ...] = (
    "approval_present",
    "quote_hash",
    "plan_hash",
    "bench_fingerprint",
    "expires_at",
    "consumed",
)


class ApprovalRefused(CouncilException):
    """An approval cannot authorise this run, and :attr:`check` says why.

    Carries the failed check as a field as well as in the message, so a
    caller (stage 4, the CLI, a test) can branch on WHICH check failed
    without parsing English.
    """

    def __init__(self, check: str, detail: str) -> None:
        self.check = check
        self.detail = detail
        super().__init__(f"approval check {check!r} failed: {detail}")


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _now_iso(moment: Optional[datetime] = None) -> str:
    """*moment* (default: now) as an ISO-8601 UTC string with a ``Z``."""
    moment = moment or datetime.now(tz=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any, *, what: str) -> datetime:
    """Parse an ISO-8601 timestamp, UTC-naive read as UTC.

    Raises:
        ValueError: *value* is not a timestamp.  The caller turns that into an
            :class:`ApprovalRefused` naming its own check -- an unparseable
            ``expires_at`` is an expiry failure, not a generic one.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{what} is empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{what} {text!r} is not an ISO-8601 timestamp: {exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# The quote on disk
# ---------------------------------------------------------------------------


def read_quote(source: Union[str, Path]) -> Tuple[Path, Dict[str, Any]]:
    """``(path, quote)`` for a run directory or a ``QUOTE.json`` path.

    Raises:
        FileNotFoundError: there is no quote there.
        ValueError: the quote is not a JSON object.
    """
    path = Path(source).expanduser()
    if path.is_dir():
        path = path / QUOTE_FILENAME
    if not path.exists():
        raise FileNotFoundError(
            f"no quote at {path}. Stage 2 writes it: `kbu ontomap council-quote`"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path}: expected a JSON object, got {type(raw).__name__}")
    return path, dict(raw)


def quote_hash(quote: Union[str, Path, Mapping[str, Any]]) -> str:
    """Content address of a quote: :func:`~.types.canonical_sha256` of it.

    The STORE'S canonicalisation rule and not a second one, so a quote hashes
    identically however it was written out -- indentation, key order and
    trailing newline are not part of the quote.  Takes a run directory, a
    ``QUOTE.json`` path, or the parsed object.
    """
    if isinstance(quote, Mapping):
        payload: Mapping[str, Any] = quote
    else:
        _path, payload = read_quote(quote)
    return canonical_sha256(dict(payload))


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------


def _gateway_uppers(quote: Mapping[str, Any]) -> Dict[str, float]:
    """Per-gateway upper-band token totals, out of the quote's own block."""
    out: Dict[str, float] = {}
    for entry in quote.get("gateways") or ():
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("gateway") or "").strip()
        if not name:
            continue
        out[name] = out.get(name, 0.0) + float(entry.get("total_tokens_upper") or 0)
    return out


def ceiling_from_quote(
    quote: Mapping[str, Any],
    *,
    headroom: float = DEFAULT_HEADROOM,
    overrides: Optional[Mapping[str, int]] = None,
) -> Tuple[Dict[str, int], Dict[str, Any], List[str]]:
    """The per-gateway token ceiling this quote implies, and how it was got.

    THE UPPER BAND TIMES *headroom*, never the point estimate -- see the
    module docstring for why a mean-shaped ceiling is the most expensive
    possible setting.

    The quote's repair line carries ``gateway = None`` on purpose (which seat
    truncates is not knowable in advance), so its tokens are in the quote's
    headline upper total but in no gateway's.  They are distributed across the
    gateways PRO RATA here rather than dropped: a ceiling that silently
    excluded the retry pass would halt the run during the stage this pipeline
    treats as first-class.

    Args:
        quote: the parsed ``QUOTE.json``.
        headroom: multiple applied to the upper band.  Must be >= 1 -- a
            ceiling below the quote's own upper band is a halt scheduled in
            advance.
        overrides: ``{gateway: tokens}`` from the operator, replacing the
            derived ceiling for those gateways exactly as given.

    Returns:
        ``(ceiling_tokens, basis, warnings)``.  *basis* records the headroom,
        the derived uppers and which gateways the operator overrode, so the
        number in the artifact can be re-derived from the artifact.

    Raises:
        ValueError: *headroom* is below 1, the quote prices no gateway and no
            override names one, or an override is not a positive integer.
    """
    if float(headroom) < 1.0:
        raise ValueError(
            f"headroom={headroom} is below 1, so the ceiling would sit UNDER "
            "the quote's own upper band -- that is a halt scheduled in "
            "advance, and a run halted partway has spent its tokens for none "
            "of the result"
        )
    headroom = float(headroom)
    uppers = _gateway_uppers(quote)
    headline = quote.get("headline") or {}
    total_upper = float((headline.get("upper") or {}).get("total_tokens") or 0)
    attributed = sum(uppers.values())
    # Everything the quote banded but could not attribute to a gateway: the
    # paid-repair line, and anything else a later quote adds with no gateway.
    unattributed = max(total_upper - attributed, 0.0)

    warnings: List[str] = []
    derived: Dict[str, int] = {}
    for gateway in sorted(uppers):
        share = (uppers[gateway] / attributed) if attributed > 0 else 0.0
        derived[gateway] = int(
            math.ceil((uppers[gateway] + unattributed * share) * headroom)
        )

    ceiling = dict(derived)
    overridden: List[str] = []
    for gateway, value in dict(overrides or {}).items():
        name = str(gateway).strip()
        if not name:
            raise ValueError("a ceiling override names no gateway")
        tokens = int(value)
        if tokens <= 0:
            raise ValueError(
                f"ceiling override for gateway {name!r} is {tokens}; a ceiling "
                "of zero or less approves nothing and would halt the run on "
                "its first call"
            )
        if name not in derived:
            # NOT an error: a transport change after the approval is
            # permitted, so an operator may legitimately cap a gateway this
            # quote never priced. It is still worth saying out loud.
            warnings.append(
                f"ceiling override names gateway {name!r}, which this quote "
                f"does not price (it prices {sorted(derived) or 'none'}). The "
                "ceiling is recorded as given."
            )
        if tokens < derived.get(name, 0):
            warnings.append(
                f"the ceiling for gateway {name!r} is {tokens:,} tokens, BELOW "
                f"the {derived[name]:,} this quote's upper band and "
                f"{headroom}x headroom imply. The run will halt partway if the "
                "quote is right."
            )
        ceiling[name] = tokens
        overridden.append(name)

    if not ceiling:
        raise ValueError(
            "the quote prices no gateway and no --ceiling-tokens was given, "
            "so there is no ceiling to approve. A run approved with no "
            "ceiling is the 2026-10-20 run again."
        )

    basis = {
        "headroom": headroom,
        "source": (
            "the quote's per-gateway UPPER band times the headroom, with the "
            "quote's unattributed (repair-pass) tokens distributed pro rata. "
            "NOT the point estimate: a ceiling at the mean halts about half "
            "of all runs partway."
        ),
        "gateway_upper_tokens": {name: int(round(uppers[name])) for name in uppers},
        "unattributed_upper_tokens": int(round(unattributed)),
        "overridden_gateways": sorted(overridden),
    }
    return ceiling, basis, warnings


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Approval:
    """One operator's yes to one specific quote, with a ceiling on it.

    AN AUDIT TRAIL AND A BINDING CEILING, NOT AN AUTHENTICATION BOUNDARY --
    see the module docstring.  :attr:`approver` is whoever the writing process
    said it was; nothing proves it, and nothing pretends to.

    Attributes:
        warnings: populated by :func:`approve` and :func:`validate_approval`
            and deliberately NOT persisted.  A warning is about the state of
            the world when the approval was checked (a seat was re-routed, a
            ceiling was set below the quote), not about the decision the file
            records.
    """

    approved_at: str
    expires_at: str
    approver: str
    plan_hash: str
    quote_hash: str
    ceiling_tokens: Mapping[str, int]
    note: str = ""
    ceiling_basis: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """The published ``APPROVED.json`` object."""
        return {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "approved_at": self.approved_at,
            "expires_at": self.expires_at,
            "approver": self.approver,
            "plan_hash": self.plan_hash,
            "quote_hash": self.quote_hash,
            "ceiling_tokens": {
                str(key): int(value) for key, value in dict(self.ceiling_tokens).items()
            },
            "note": self.note,
            "ceiling_basis": dict(self.ceiling_basis),
            "what_this_is": (
                "This file is an AUDIT TRAIL and a BINDING CEILING. It is NOT "
                "AN AUTHENTICATION BOUNDARY: it is unsigned and cannot be "
                "signed -- there is no key material on these machines, and an "
                "agent can write it. What it buys is an explicit per-gateway "
                "ceiling, a checkable binding to this plan and this quote, "
                "and a refusal when the job has changed under it."
            ),
        }

    def write(self, destination: Union[str, Path]) -> Path:
        """Write ``APPROVED.json`` to a run directory, or to a named file.

        Atomic: written to a ``.partial`` sibling and renamed, so stage 4
        never reads a half-written approval (the same discipline stage 1 uses
        for ``PLAN.json``).
        """
        path = Path(destination).expanduser()
        if path.is_dir():
            path = path / APPROVAL_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")
        tmp.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        return path


def _default_approver() -> str:
    """Who to record as the approver when the caller did not say.

    ``$KBU_APPROVER`` first, then the login name.  This is a LABEL FOR AN
    AUDIT TRAIL and is not authenticated anywhere -- see the module docstring.
    """
    named = str(os.environ.get("KBU_APPROVER") or "").strip()
    if named:
        return named
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no pwd entry, no controlling terminal
        return "unknown"


def approve(
    run_dir: Union[str, Path],
    *,
    approver: Optional[str] = None,
    ceiling_tokens: Optional[Mapping[str, int]] = None,
    headroom: float = DEFAULT_HEADROOM,
    expires_in_days: float = DEFAULT_EXPIRES_IN_DAYS,
    note: str = "",
    now: Optional[datetime] = None,
    write: bool = True,
) -> Approval:
    """Record an approval of the quote in *run_dir*, and write it there.

    SPENDS NOTHING and reaches no network: it reads ``QUOTE.json``, hashes it
    and writes one small JSON file.

    Args:
        run_dir: the run directory holding ``QUOTE.json``.
        approver: who is approving.  Default: ``$KBU_APPROVER``, else the
            login name.  A LABEL, not an authenticated identity.
        ceiling_tokens: ``{gateway: tokens}`` overrides.  Gateways not named
            keep the derived ceiling.
        headroom: multiple on the quote's upper band (:data:`DEFAULT_HEADROOM`).
        expires_in_days: lifetime of the approval
            (:data:`DEFAULT_EXPIRES_IN_DAYS`).
        note: free text recorded in the artifact -- why this run, for whom.
        now: the clock, for tests.
        write: write ``APPROVED.json``.  ``False`` computes the artifact
            without writing it, so an operator can see the ceiling first.

    Returns:
        The :class:`Approval`, with ``warnings`` set where the ceiling is
        degraded (an override below the quote, or naming an unpriced gateway)
        and ``source`` naming the file it was written to.

    Raises:
        FileNotFoundError: *run_dir* holds no ``QUOTE.json``.
        ValueError: the quote is unusable, or the ceiling cannot be derived.
    """
    quote_path, quote = read_quote(run_dir)
    plan_hash = str(quote.get("plan_hash") or "")
    if not plan_hash:
        raise ValueError(
            f"{quote_path} declares no plan_hash, so an approval could not "
            "bind to the plan it prices. Re-run stage 2."
        )
    ceiling, basis, warnings = ceiling_from_quote(
        quote, headroom=headroom, overrides=ceiling_tokens
    )
    if float(expires_in_days) <= 0:
        raise ValueError(
            f"expires_in_days={expires_in_days}; an approval that expires at "
            "or before the moment it is written authorises nothing"
        )
    moment = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    approval = Approval(
        approved_at=_now_iso(moment),
        expires_at=_now_iso(moment + timedelta(days=float(expires_in_days))),
        approver=str(approver or _default_approver()),
        plan_hash=plan_hash,
        # The hash of the quote ON DISK, which is what stage 4 will re-hash.
        quote_hash=quote_hash(quote),
        ceiling_tokens=ceiling,
        note=str(note or ""),
        ceiling_basis=basis,
        warnings=tuple(warnings),
    )
    if write:
        target = Path(run_dir).expanduser()
        written = approval.write(target if target.is_dir() else quote_path.parent)
        approval = replace(approval, source=str(written))
    return approval


def read_approval(source: Union[str, Path]) -> Approval:
    """Load an :class:`Approval` from a run directory or an ``APPROVED.json``.

    Raises:
        ApprovalRefused: the file is absent, unparseable, or missing a field
            the gate rests on.  Check ``approval_present`` -- this IS that
            check, so a caller never has to translate an ``OSError``.
    """
    path = Path(source).expanduser()
    if path.is_dir():
        path = path / APPROVAL_FILENAME
    if not path.exists():
        raise ApprovalRefused(
            "approval_present",
            f"no approval at {path}. Stage 4 does not run unapproved: write "
            "one with `kbu ontomap council-approve`, having read the quote.",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ApprovalRefused(
            "approval_present", f"{path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ApprovalRefused(
            "approval_present",
            f"{path}: expected a JSON object, got {type(raw).__name__}",
        )
    missing = [
        key
        for key in ("approved_at", "expires_at", "approver", "plan_hash", "quote_hash")
        if not str(raw.get(key) or "").strip()
    ]
    if missing:
        raise ApprovalRefused(
            "approval_present",
            f"{path} carries no {', '.join(missing)}. An approval missing any "
            "of those binds to nothing.",
        )
    ceiling_raw = raw.get("ceiling_tokens")
    if not isinstance(ceiling_raw, Mapping) or not ceiling_raw:
        raise ApprovalRefused(
            "approval_present",
            f"{path} carries no per-gateway ceiling_tokens. An approval with "
            "no ceiling is the 2026-10-20 run again: nothing to halt at.",
        )
    try:
        ceiling = {str(key): int(value) for key, value in ceiling_raw.items()}
    except (TypeError, ValueError) as exc:
        raise ApprovalRefused(
            "approval_present", f"{path}: ceiling_tokens is not integral: {exc}"
        ) from exc
    return Approval(
        approved_at=str(raw["approved_at"]),
        expires_at=str(raw["expires_at"]),
        approver=str(raw["approver"]),
        plan_hash=str(raw["plan_hash"]),
        quote_hash=str(raw["quote_hash"]),
        ceiling_tokens=ceiling,
        note=str(raw.get("note") or ""),
        ceiling_basis=dict(raw.get("ceiling_basis") or {}),
        source=str(path),
    )


# ---------------------------------------------------------------------------
# The live bench, and the re-route warning
# ---------------------------------------------------------------------------


def live_bench(
    profile: Optional[str] = None,
    *,
    config: Any = None,
    roster: Any = None,
) -> Dict[str, Any]:
    """What the LIVE configuration seats for *profile*, as the plan records it.

    Returns ``{"bench_fingerprint", "roster_fingerprint",
    "transport_allocation"}`` -- the same three keys ``PLAN.json`` carries, so
    :func:`validate_approval` compares like with like.  ``bench_fingerprint``
    is the only one of them it refuses on.

    Args:
        profile: the bench to resolve.  Default: the configured default.
        config: a :class:`~.config.CouncilConfig`.  Default: the loaded one.
        roster: an already-resolved roster, which skips the config entirely.
    """
    if roster is None:
        if config is None:
            from .config import load_council_config

            config = load_council_config()
        roster = config.roster(profile)
    from .types import route_fingerprint, seat_generation_params

    rows: List[Dict[str, Any]] = []
    for role, seat in roster.seats():
        row: Dict[str, Any] = {
            "role": role,
            "seat_name": seat.seat_name,
            "endpoint": seat.endpoint,
            "endpoint_kind": seat.endpoint_kind,
            "model": seat.model,
            "canonical_model": seat.canonical_model,
        }
        if config is not None:
            # Imported from the quote stage rather than copied: which PAYER a
            # seat's spend lands on is one rule (the account, else the alias),
            # and a second copy of it here could disagree with the gateway
            # names the ceiling is keyed on.
            from .quote import _gateway_of

            row["gateway"] = _gateway_of(config, seat)
            try:
                row["route_fingerprint"] = route_fingerprint(
                    seat, config.endpoint(seat.endpoint), seat_generation_params(seat)
                )
            except Exception:  # noqa: BLE001 - an unknown endpoint is still comparable
                row["route_fingerprint"] = ""
        rows.append(row)
    return {
        "bench_fingerprint": bench_fingerprint(roster),
        "roster_fingerprint": roster.fingerprint(),
        "transport_allocation": rows,
    }


def _by_seat(rows: Iterable[Any]) -> Dict[str, Mapping[str, Any]]:
    """``{seat_name: row}`` for a transport-allocation block."""
    out: Dict[str, Mapping[str, Any]] = {}
    for row in rows or ():
        if isinstance(row, Mapping) and row.get("seat_name"):
            out[str(row["seat_name"])] = row
    return out


def _bench_delta(planned: Iterable[Any], live: Iterable[Any]) -> List[str]:
    """Human descriptions of every MODEL change between two seatings."""
    before, after = _by_seat(planned), _by_seat(live)
    out: List[str] = []
    for seat_name in sorted(set(before) | set(after)):
        was = before.get(seat_name)
        now = after.get(seat_name)
        if was is None:
            out.append(f"{seat_name} is newly seated on {now.get('canonical_model')}")
        elif now is None:
            out.append(
                f"{seat_name} ({was.get('canonical_model')}) is no longer seated"
            )
        elif str(was.get("canonical_model")) != str(now.get("canonical_model")):
            out.append(
                f"{seat_name}: {was.get('canonical_model')} -> "
                f"{now.get('canonical_model')}"
            )
    return out


def _route_delta(planned: Iterable[Any], live: Iterable[Any]) -> List[str]:
    """Human descriptions of every seat re-ROUTED without changing model."""
    before, after = _by_seat(planned), _by_seat(live)
    out: List[str] = []
    for seat_name in sorted(set(before) & set(after)):
        was, now = before[seat_name], after[seat_name]
        if str(was.get("canonical_model")) != str(now.get("canonical_model")):
            continue  # a model change; the bench check owns that one
        moved = [
            f"{key} {was.get(key)!r} -> {now.get(key)!r}"
            for key in ("gateway", "endpoint", "endpoint_kind", "model")
            if was.get(key) is not None
            and now.get(key) is not None
            and str(was.get(key)) != str(now.get(key))
        ]
        if not moved:
            # Same spelling everywhere, but the route fingerprint moved --
            # the endpoint's own target, account or params changed under it.
            was_fp, now_fp = (
                str(was.get("route_fingerprint") or ""),
                str(now.get("route_fingerprint") or ""),
            )
            if was_fp and now_fp and was_fp != now_fp:
                moved = [f"route_fingerprint {was_fp[:12]}... -> {now_fp[:12]}..."]
        if moved:
            out.append(f"{seat_name} ({was.get('canonical_model')}): " + ", ".join(moved))
    return out


# ---------------------------------------------------------------------------
# The gate stage 4 calls
# ---------------------------------------------------------------------------


def _consumed_quote_hash(run_dir: Path) -> Tuple[Optional[str], Dict[str, Any]]:
    """``(consumed quote_hash, RUN.json)`` for a run directory.

    ``(None, {})`` when the run has not started.  Stage 4 owns the rest of
    that file's contract; this reads one field.
    """
    path = run_dir / RUN_FILENAME
    if not path.exists():
        return None, {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ApprovalRefused(
            "consumed", f"{path} is not valid JSON, so whether this approval "
            f"has already been spent cannot be established: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ApprovalRefused(
            "consumed",
            f"{path}: expected a JSON object, got {type(raw).__name__}",
        )
    consumed = str(raw.get(CONSUMED_KEY) or "").strip()
    return (consumed or None), dict(raw)


def validate_approval(
    run_dir: Union[str, Path],
    *,
    live: Optional[Mapping[str, Any]] = None,
    profile: Optional[str] = None,
    config: Any = None,
    roster: Any = None,
    now: Optional[datetime] = None,
    resume: bool = False,
) -> Approval:
    """The gate stage 4 calls before it spends a token.  Raises, or returns.

    REFUSES unless ALL SIX of :data:`APPROVAL_CHECKS` hold, and every refusal
    NAMES THE CHECK (:attr:`ApprovalRefused.check`):

    1. ``approval_present`` -- ``APPROVED.json`` exists, parses, and carries
       the fields the gate rests on.
    2. ``quote_hash`` -- the approval's ``quote_hash`` equals the recomputed
       hash of the ``QUOTE.json`` on disk.  An edited quote is a different
       price than the one approved.
    3. ``plan_hash`` -- the quote's ``plan_hash`` equals the recomputed hash
       of ``PLAN.json``, and the approval's matches the quote's.  A re-plan
       after approval is a different job.
    4. ``bench_fingerprint`` -- the bench ``PLAN.json`` was built against is
       still what the live config resolves (and so is the query, when the
       caller supplies the one it resolved).  THE BENCH, NOT THE ROSTER: a
       model swap is refused (that is approving one experiment and running
       another), while a TRANSPORT change is PERMITTED and comes back as a
       WARNING naming the re-routed seats -- the quote's price is stale, but
       the experiment is the one that was approved.
    5. ``expires_at`` -- the approval has not expired.
    6. ``consumed`` -- no OTHER run has already spent this approval.  Stage 4
       records the hash it consumed in ``RUN.json`` (:func:`mark_consumed`);
       a second run against that record is refused unless *resume* says this
       is a declared resume of the same run.

    REMEMBER WHAT THIS IS.  An audit trail and a ceiling, and
    NOT AN AUTHENTICATION BOUNDARY.  Passing these six checks does not
    establish that a human approved anything -- the file is unsigned and an
    agent can write it (see the module docstring).  It establishes that the
    approval on disk is bound to THIS plan, THIS quote and THIS bench, has not
    expired, and has not already been spent.

    Args:
        run_dir: the run directory holding the stage files.
        live: an already-resolved :func:`live_bench` block.  Default: resolved
            from the live configuration, using the plan's own ``profile``.  A
            ``query`` key on it (``"<name>@<version>"``, which stage 4 has
            already resolved out of the store's registry) is checked against
            the plan's too; without one, the plan's query is covered only by
            ``plan_hash``, which is what it is part of.
        profile: override the profile used to resolve the live bench.
        config: a :class:`~.config.CouncilConfig` to resolve against.
        roster: an already-resolved roster to resolve against.
        now: the clock, for tests.
        resume: this invocation is a declared RESUME of the run that already
            consumed this approval.

    Returns:
        The :class:`Approval`, with :attr:`Approval.warnings` carrying the
        re-route warning when the transport moved under it.

    Raises:
        ApprovalRefused: one of the six checks failed; ``.check`` says which.
    """
    directory = Path(run_dir).expanduser()
    approval = read_approval(directory)  # check 1

    # -- check 2: the quote on disk is the quote that was approved ---------
    try:
        quote_path, quote = read_quote(directory)
    except (OSError, ValueError) as exc:
        raise ApprovalRefused(
            "quote_hash",
            f"the quote this approval names cannot be read: {exc}",
        ) from exc
    recomputed_quote = quote_hash(quote)
    if recomputed_quote != approval.quote_hash:
        raise ApprovalRefused(
            "quote_hash",
            f"{quote_path} hashes to {recomputed_quote[:12]}... but the "
            f"approval names {approval.quote_hash[:12]}.... The quote has "
            "changed since it was approved, so the approved price is not this "
            "run's price. Re-quote and re-approve.",
        )

    # -- check 3: the plan on disk is the plan that was quoted -------------
    quoted_plan_hash = str(quote.get("plan_hash") or "")
    if quoted_plan_hash != approval.plan_hash:
        raise ApprovalRefused(
            "plan_hash",
            f"the approval names plan_hash {approval.plan_hash[:12]}... but "
            f"the quote it approves was priced against "
            f"{quoted_plan_hash[:12] or '(none)'}.... The approval and the "
            "quote do not agree about which plan this is.",
        )
    try:
        plan = load_plan(directory)
    except (OSError, ValueError) as exc:
        raise ApprovalRefused(
            "plan_hash",
            f"the plan the quote was priced against cannot be read: {exc}",
        ) from exc
    if plan.plan_hash != quoted_plan_hash:
        raise ApprovalRefused(
            "plan_hash",
            f"{directory / PLAN_FILENAME} hashes to {plan.plan_hash[:12]}... "
            f"but the approved quote was priced against "
            f"{quoted_plan_hash[:12]}.... The plan has been re-derived since "
            "it was quoted -- different items, or a different query or "
            "mapping_hash -- so this is a different job. Re-quote and "
            "re-approve.",
        )

    # -- check 4: the BENCH is still the approved bench -------------------
    plan_raw = json.loads((directory / PLAN_FILENAME).read_text(encoding="utf-8"))
    planned_bench = str(plan_raw.get("bench_fingerprint") or "")
    if live is None:
        try:
            live = live_bench(
                profile if profile is not None else plan_raw.get("profile"),
                config=config,
                roster=roster,
            )
        except Exception as exc:  # noqa: BLE001 - an unresolvable bench is a refusal
            raise ApprovalRefused(
                "bench_fingerprint",
                "the live configuration cannot be resolved into a bench, so "
                "whether the approved experiment is the one about to run "
                f"cannot be established: {type(exc).__name__}: {exc}",
            ) from exc
    live_fingerprint = str((live or {}).get("bench_fingerprint") or "")
    live_query = str((live or {}).get("query") or "")
    planned_query = str(plan_raw.get("query") or "")
    if live_query and planned_query and live_query != planned_query:
        raise ApprovalRefused(
            "bench_fingerprint",
            f"the plan was approved for query {planned_query!r} but the live "
            f"configuration resolves {live_query!r}. The query version is part "
            "of the clearinghouse parameter set and of every prompt hash, so "
            "this is a different experiment under the same approval.",
        )
    if not planned_bench:
        raise ApprovalRefused(
            "bench_fingerprint",
            f"{directory / PLAN_FILENAME} records no bench_fingerprint, so "
            "the approved experiment cannot be identified. Re-run stage 1.",
        )
    warnings: List[str] = []
    if live_fingerprint != planned_bench:
        delta = _bench_delta(
            plan_raw.get("transport_allocation") or (),
            (live or {}).get("transport_allocation") or (),
        )
        raise ApprovalRefused(
            "bench_fingerprint",
            f"the plan was approved against bench {planned_bench[:12]}... but "
            f"the live configuration seats {live_fingerprint[:12] or '(none)'}"
            "..."
            + (f" ({'; '.join(delta)})" if delta else "")
            + ". A MODEL change is a different experiment: running it under "
            "this approval would approve one thing and run another. "
            "Re-plan, re-quote and re-approve. (Re-ROUTING the same models "
            "is permitted and does not come here.)",
        )
    reroutes = _route_delta(
        plan_raw.get("transport_allocation") or (),
        (live or {}).get("transport_allocation") or (),
    )
    if reroutes:
        warnings.append(
            "the BENCH is unchanged but the TRANSPORT has moved since the "
            "quote, for " + "; ".join(reroutes) + ". The approval stands -- "
            "re-routing the same models is permitted and is how token cost is "
            "allocated -- but the quote's PRICE and wall clock are stale for "
            "those seats, and prompt_hash keys on endpoint_kind so their "
            "stored answers will not be reused."
        )
    planned_roster = str(plan_raw.get("roster_fingerprint") or "")
    live_roster = str((live or {}).get("roster_fingerprint") or "")
    if planned_roster and live_roster and planned_roster != live_roster and not reroutes:
        warnings.append(
            f"roster_fingerprint moved ({planned_roster[:12]}... -> "
            f"{live_roster[:12]}...) with no seat's model, gateway or endpoint "
            "changing: a token ceiling, temperature or extra param was edited. "
            "That is provenance, not a partition key, so the approval stands."
        )

    # -- check 5: the approval has not expired ----------------------------
    moment = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    try:
        expires = _parse_iso(approval.expires_at, what="expires_at")
    except ValueError as exc:
        raise ApprovalRefused("expires_at", str(exc)) from exc
    if expires <= moment:
        raise ApprovalRefused(
            "expires_at",
            f"the approval expired at {approval.expires_at} and it is now "
            f"{_now_iso(moment)}. The corpus, the config and the price all "
            "move; re-quote and re-approve rather than running a decision "
            "nobody has looked at since.",
        )

    # -- check 6: nobody else has already spent this approval -------------
    consumed, run_record = _consumed_quote_hash(directory)
    if consumed is not None:
        run_id = str(run_record.get("run_id") or "") or directory.name
        if consumed != approval.quote_hash:
            raise ApprovalRefused(
                "consumed",
                f"{directory / RUN_FILENAME} records run {run_id!r} as having "
                f"consumed approval {consumed[:12]}..., not this one "
                f"({approval.quote_hash[:12]}...). This run directory is "
                "already committed to a different approved quote; a fresh run "
                "needs a fresh run directory.",
            )
        if not resume:
            raise ApprovalRefused(
                "consumed",
                f"approval {approval.quote_hash[:12]}... has already been "
                f"consumed by run {run_id!r} ({directory / RUN_FILENAME}). An "
                "approval is a ceiling for ONE run, so re-spending it would "
                "double the approved cost. Pass the resume flag if this is a "
                "declared resume of that same run, or re-approve for a new "
                "one.",
            )
        warnings.append(
            f"resuming run {run_id!r} against the approval it already "
            f"consumed ({approval.quote_hash[:12]}...). The ceiling covers "
            "the WHOLE run, so what the run already spent counts against it."
        )

    return replace(approval, warnings=tuple(warnings))


def mark_consumed(
    run_dir: Union[str, Path],
    approval: Union[Approval, str],
    *,
    run_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Path:
    """Record in ``RUN.json`` that this run has consumed *approval*.

    Stage 4 calls this ONCE, on first use, before it submits anything.  It
    exists here, beside the check that reads it, so the field has exactly one
    spelling (:data:`CONSUMED_KEY`) -- a run that recorded consumption under a
    name the gate did not read would make check 6 unenforceable while looking
    implemented.

    Merges into whatever ``RUN.json`` already holds (stage 4 owns the rest of
    that file) and writes atomically.

    Returns:
        The path written.
    """
    directory = Path(run_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / RUN_FILENAME
    record: Dict[str, Any] = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"{path}: expected a JSON object, got {type(raw).__name__}"
            )
        record = dict(raw)
    digest = approval if isinstance(approval, str) else approval.quote_hash
    record[CONSUMED_KEY] = str(digest)
    record.setdefault("run_id", str(run_id or directory.name))
    record.setdefault("approval_consumed_at", _now_iso(now))
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
