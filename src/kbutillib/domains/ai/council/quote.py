"""The token model a council quote is priced from -- fitted, not tokenized.

A quote for a council run has to answer "how many tokens will this cost"
BEFORE a single call is made.  This module answers it from the council
store's own measured history: for each ``(seat, role)`` it fits

* ``input_tokens_per_char`` -- input tokens per character of the item's
  prompt material (:func:`item_prompt_chars`), and
* ``output_tokens_mean`` / ``output_tokens_upper`` -- the mean and a high
  percentile of the output tokens that seat actually emitted,

each carried back with the SAMPLE SIZE it was fitted over and a ``basis`` of
``measured`` or ``projected``.

WHY A FIT AND NOT A TOKENIZER.  The roster spans Google, OpenAI and Anthropic
models, so no single tokenizer is correct across it and carrying three would
be three dependencies.  The h100 store already holds ~20,000 real calls per
chair (measured 2026-10-22) carrying the gateway's own recorded token counts,
which is better calibrated than any third-party tokenizer and costs nothing
to read.  Measured on the 2026-10-20 ``function_to_go`` run (job
``477d6c53``, 19,276 items), the
per-attempt ratio of input tokens to prompt characters has a relative spread
(sd/mean) of 9-10% across the three proposers, 14% for the critic and 9% for
meta -- so a corpus-level total built on it is dominated by how many items
there are, not by the per-item spread.

WHY AN UPPER PERCENTILE AND NOT ``mean + k*sd``.  The output distributions are
right-skewed by long candidate menus: on that same run the critic's p95 was
2.7x its mean.  A normal band drawn around that mean understates the tail it
is supposed to cover, so the band here is an order statistic
(:data:`DEFAULT_UPPER_PERCENTILE`).

NOTHING IN THIS MODULE MAY OPEN A SOCKET.  "The quote spends nothing" is a
property the quote stage's tests assert, and the way it is held is that this
module's import graph never reaches a backend, an api type, the engine or the
service client -- it imports SQLAlchemy, :mod:`.store.schema` and
:mod:`.types`, all of which are pure data plus SQL.  Anything added here that
needs a network client belongs in a different module.

Example::

    from kbutillib.domains.ai.council.quote import fit_token_model

    model = fit_token_model(store, seats=roster)
    fit = model.fit_for("critic", "critic")
    fit.estimate(item_prompt_chars(evidence, menu))
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import sqlalchemy as sa

from .queries.spec import renderer_id_version
from .store import schema as sch
from .types import (
    CouncilTask,
    canonical_json,
    canonical_sha256,
    data_hash,
    default_reuse_namespace,
    entity_hash,
    prompt_hash,
    route_fingerprint,
    seat_generation_params,
)

__all__ = [
    "DEFAULT_UPPER_PERCENTILE",
    "PROJECTED_UPPER_FACTOR",
    "FALLBACK_PROMPT_CHARS",
    "SEAT_FALLBACKS",
    "ROLE_FALLBACKS",
    "SeatTokenFit",
    "TokenModel",
    "item_prompt_chars",
    "fit_token_model",
]

#: The percentile used for the output-token upper band.  p95 rather than a
#: mean + k*sd band: see the module docstring.
DEFAULT_UPPER_PERCENTILE = 95.0

#: Upper band for a PROJECTED seat, as a multiple of its fallback mean.  A
#: seat with no history has no distribution to take a percentile of, so the
#: factor is read off the seats that DO have one: on the 2026-10-20 run the
#: measured p95/mean ratios were 1.72 (proposer_1), 2.54 (proposer_2), 2.47
#: (proposer_3), 2.73 (critic) and 2.01 (meta).  2.5 sits at the top of that
#: observed range, so a projected band is conservative without pretending to
#: know a shape it has not measured.
PROJECTED_UPPER_FACTOR = 2.5

#: Prompt characters per item on the run the per-seat fallbacks below were
#: measured on (mean of :func:`item_prompt_chars` over the 19,276 items of
#: job ``477d6c53``, 2026-10-20).  The fallbacks are recorded as tokens PER
#: ITEM, and this is the divisor that turns one into the per-character ratio
#: this module reports, so a projected seat and a measured seat are quoted in
#: the same units.
FALLBACK_PROMPT_CHARS = 6856


@dataclass(frozen=True)
class _Fallback:
    """Per-item token figures for a seat with no history, and their source."""

    input_tokens: int
    output_tokens: int
    source: str


#: Measured per-seat, per-item token counts from the completed 2026-10-20
#: ``function_to_go`` run (19,268 verdicts).  Used ONLY when the store holds
#: no usable attempt for that seat, and always reported with ``basis =
#: "projected"`` and the ``source`` string below, because a quote that
#: silently returned zero -- or silently borrowed another seat's figures --
#: would be a cost estimate nobody could audit.
SEAT_FALLBACKS: Dict[str, _Fallback] = {
    "proposer_1": _Fallback(2226, 113, "proposer_1 (ARGO gemini35flash), measured 2026-10-20"),
    "proposer_2": _Fallback(2129, 219, "proposer_2 (MAG gpt-5.6-sol), measured 2026-10-20"),
    "proposer_3": _Fallback(2129, 292, "proposer_3 (MAG gpt-5.6-luna), measured 2026-10-20"),
    "critic": _Fallback(1416, 629, "critic (ARGO claudesonnet5), measured 2026-10-20"),
    "meta": _Fallback(2932, 493, "meta (ARGO claudeopus5), measured 2026-10-20"),
}

#: Fallback for a seat whose NAME is not in :data:`SEAT_FALLBACKS` either --
#: a bench seated under new seat names.  Per role, the WORST case over the
#: seats of that role on the same run, and the ``source`` string says so: a
#: quote may be conservative, but it may not quietly present one seat's
#: numbers as another's.
ROLE_FALLBACKS: Dict[str, _Fallback] = {
    "proposer": _Fallback(
        2226,
        292,
        "role default: worst case over proposer_1/2/3, measured 2026-10-20",
    ),
    "critic": _Fallback(
        1416, 629, "role default: critic (ARGO claudesonnet5), measured 2026-10-20"
    ),
    "meta": _Fallback(
        2932, 493, "role default: meta (ARGO claudeopus5), measured 2026-10-20"
    ),
}


def item_prompt_chars(
    evidence: Optional[Mapping[str, Any]],
    menu: Optional[Sequence[Any]],
) -> int:
    """Characters of prompt material one item contributes.

    THE REGRESSOR OF THE WHOLE MODEL, so it has to mean the same thing at fit
    time and at quote time.  Prompt text is never stored (see
    ``SqliteStore.record_attempt``, which refuses it), and re-rendering a
    historical prompt would mean importing the engine -- which imports the
    backends, which open sockets.  So the measure is taken from the two item
    columns the store DOES keep, in the council's own canonical JSON form:
    the item's ``evidence`` plus its candidate ``menu``.

    That is everything variable about a proposer prompt; the role template
    around it is a constant the per-seat fit absorbs, and the ballots a critic
    or meta prompt also carries scale with the same menu.  Measured on the
    2026-10-20 run, the resulting per-seat ratios are stable to 9-14%
    relative.

    The same two values are available for an item that has NOT run yet --
    they are what ``function_to_go`` puts in a task -- which is what makes a
    quote possible before anything is submitted.
    """
    return len(canonical_json(dict(evidence or {}))) + len(
        canonical_json(list(menu or []))
    )


@dataclass(frozen=True)
class SeatTokenFit:
    """The token model for one ``(seat, role)`` pair.

    Attributes:
        seat_name: the seat this fit is for.  Never another seat's.
        role: ``proposer``, ``critic`` or ``meta``.
        input_tokens_per_char: fitted input tokens per prompt character.
        output_tokens_mean: mean output tokens per call.
        output_tokens_upper: the :data:`DEFAULT_UPPER_PERCENTILE` percentile
            of output tokens (``mean * PROJECTED_UPPER_FACTOR`` when
            projected).
        n_attempts: SAMPLE SIZE -- how many successful attempts the fit was
            taken over.  Zero on a projected fit.
        basis: ``measured`` (fitted from this store) or ``projected`` (no
            usable history; see ``fallback``).
        fallback: names where a projected figure came from, and is ``None``
            on a measured fit.
        input_tokens_mean: mean input tokens per call over the same sample,
            carried as provenance so an operator can compare the fit against
            a known per-item figure without re-deriving it.
        prompt_chars_mean: mean :func:`item_prompt_chars` over the sample.
    """

    seat_name: str
    role: str
    input_tokens_per_char: float
    output_tokens_mean: float
    output_tokens_upper: float
    n_attempts: int
    basis: str
    fallback: Optional[str] = None
    input_tokens_mean: Optional[float] = None
    prompt_chars_mean: Optional[float] = None

    def estimate(self, prompt_chars: int) -> Dict[str, float]:
        """Projected tokens for ONE call of this seat on an item that size."""
        return {
            "input_tokens": self.input_tokens_per_char * float(prompt_chars),
            "output_tokens": self.output_tokens_mean,
            "output_tokens_upper": self.output_tokens_upper,
        }

    def to_dict(self) -> Dict[str, Any]:
        """The published shape, with the sample size always present."""
        return {
            "seat_name": self.seat_name,
            "role": self.role,
            "input_tokens_per_char": round(self.input_tokens_per_char, 6),
            "output_tokens_mean": round(self.output_tokens_mean, 1),
            "output_tokens_upper": round(self.output_tokens_upper, 1),
            "n_attempts": self.n_attempts,
            "basis": self.basis,
            "fallback": self.fallback,
            "input_tokens_mean": (
                None
                if self.input_tokens_mean is None
                else round(self.input_tokens_mean, 1)
            ),
            "prompt_chars_mean": (
                None
                if self.prompt_chars_mean is None
                else round(self.prompt_chars_mean, 1)
            ),
        }


@dataclass(frozen=True)
class TokenModel:
    """Every seat's fit, plus the band the upper figures were taken at."""

    fits: Tuple[SeatTokenFit, ...]
    upper_percentile: float = DEFAULT_UPPER_PERCENTILE

    def __iter__(self) -> Iterator[SeatTokenFit]:
        return iter(self.fits)

    def fit_for(self, seat_name: str, role: str) -> SeatTokenFit:
        """The fit for one pair.

        Raises:
            KeyError: that pair was not among the seats fitted.  Deliberately
                not a silent fallback: a quote asking about a seat the model
                was never built for is a bug in the caller, not a seat with
                no history.
        """
        for fit in self.fits:
            if fit.seat_name == seat_name and fit.role == role:
                return fit
        raise KeyError(f"no fit for seat {seat_name!r} in role {role!r}")

    def to_dict(self) -> Dict[str, Any]:
        """The published shape -- what ``kbu council token-model --json`` emits."""
        return {
            "upper_percentile": self.upper_percentile,
            "seats": [fit.to_dict() for fit in self.fits],
            "n_measured": sum(1 for fit in self.fits if fit.basis == "measured"),
            "n_projected": sum(1 for fit in self.fits if fit.basis == "projected"),
        }


# ---------------------------------------------------------------------------
# The fit
# ---------------------------------------------------------------------------


def fit_token_model(
    store: Any,
    *,
    seats: Any,
    upper_percentile: float = DEFAULT_UPPER_PERCENTILE,
) -> TokenModel:
    """Fit the token model for *seats* from *store*'s own measured history.

    Reads ``attempts.usage`` (per-attempt token counts) and, where an attempt
    row has none to give, ``calls.fresh_usage``, for every SUCCESSFUL call
    belonging to each named seat and role -- joined to the item the call was
    made about, which is where :func:`item_prompt_chars` comes from.

    Args:
        store: a council store.  Must expose ``.engine``: the fit reads the
            ``attempts`` x ``calls`` x ``items`` join, and the store contract
            publishes no reader for it.  The read takes no write lock (plain
            ``BEGIN``), because a quote must not contend with a running
            daemon.
        seats: a :class:`~.types.Roster`, or an iterable of ``(role, seat)``
            pairs where ``seat`` is a ``SeatSpec`` or a seat name.  A bare
            ``SeatSpec`` is refused: its role is not derivable from it, and
            guessing one from the seat name would attribute a fit to the
            wrong role.
        upper_percentile: percentile for the output-token upper band.

    Returns:
        A :class:`TokenModel` with one :class:`SeatTokenFit` per requested
        pair, in the order requested.

    Raises:
        TypeError: *store* exposes no ``engine``, or *seats* is not one of the
            accepted shapes.
        ValueError: a seat's role is not one of the three council roles.
    """
    pairs = _requested_pairs(seats)
    if not pairs:
        return TokenModel(fits=(), upper_percentile=upper_percentile)
    engine = getattr(store, "engine", None)
    if engine is None:
        raise TypeError(
            "fit_token_model needs a store exposing .engine (SqliteStore); "
            f"got {type(store).__name__}"
        )

    samples = _read_history(engine, {name for name, _role in pairs})
    return TokenModel(
        fits=tuple(
            _fit_one(name, role, samples.get((name, role)), upper_percentile)
            for name, role in pairs
        ),
        upper_percentile=upper_percentile,
    )


class _Sample:
    """Running totals for one ``(seat, role)`` pair.

    Sums, not retained rows, for the input ratio: the h100 store holds 20,654
    successful proposer_1 attempts (measured 2026-10-22), each joined to an
    item whose evidence and menu average 6.9 kB, so retaining the rows would
    be ~140 MB per chair.  The output tokens ARE retained, because a
    percentile needs the values -- one integer each.
    """

    __slots__ = ("n", "chars", "input_tokens", "output_tokens")

    def __init__(self) -> None:
        self.n = 0
        self.chars = 0
        self.input_tokens = 0
        self.output_tokens: List[int] = []

    def add(self, chars: int, prompt_tokens: int, completion_tokens: int) -> None:
        self.n += 1
        self.chars += chars
        self.input_tokens += prompt_tokens
        self.output_tokens.append(completion_tokens)


def _history_statement(seat_names: Iterable[str]) -> sa.Select:
    """Successful attempts of these seats, with the item each was about.

    Built here rather than in :mod:`.store.queries`: that module is the
    SQL of the QUEUE (what is claimable, what settles a call), and this is a
    read of finished history that the queue's meaning does not depend on.

    ``attempt_no >= 0`` drops the UNCHARGED attempts filed under a negative
    number (``SqliteStore.next_uncharged_attempt_no`` -- a stopped daemon, an
    interrupted invocation): they are audit rows for calls that were never
    answered, so they carry no usable token count.
    """
    attempts, calls, items = sch.attempts, sch.calls, sch.items
    join = attempts.join(calls, calls.c.call_id == attempts.c.call_id).join(
        items,
        sa.and_(
            items.c.job_id == calls.c.job_id, items.c.item_id == calls.c.item_id
        ),
    )
    return (
        sa.select(
            calls.c.seat_name,
            calls.c.role,
            calls.c.job_id,
            calls.c.item_id,
            calls.c.status,
            calls.c.reused_from,
            calls.c.fresh_usage,
            attempts.c.usage,
            items.c.evidence,
            items.c.menu,
        )
        .select_from(join)
        .where(
            attempts.c.outcome_kind == "ok",
            attempts.c.attempt_no >= 0,
            calls.c.seat_name.in_(sorted(seat_names)),
        )
    )


def _read_history(
    engine: Any, seat_names: Iterable[str]
) -> Dict[Tuple[str, str], _Sample]:
    """Accumulate per-``(seat, role)`` samples by streaming the join."""
    samples: Dict[Tuple[str, str], _Sample] = {}
    # Item prompt characters are the same for all five seats of an item, so
    # they are computed once per item rather than once per attempt -- five
    # times the JSON work on a 19k-item job, and the cache holds one integer
    # per item.
    chars_by_item: Dict[Tuple[str, str], int] = {}
    statement = _history_statement(seat_names)
    with engine.connect() as conn:
        result = conn.execution_options(
            stream_results=True, yield_per=2000
        ).execute(statement)
        for row in result:
            tokens = _usable_tokens(row)
            if tokens is None:
                continue
            key = (str(row.job_id), str(row.item_id))
            chars = chars_by_item.get(key)
            if chars is None:
                chars = item_prompt_chars(row.evidence, row.menu)
                chars_by_item[key] = chars
            if chars <= 0:
                continue
            prompt_tokens, completion_tokens = tokens
            samples.setdefault(
                (str(row.seat_name), str(row.role)), _Sample()
            ).add(chars, prompt_tokens, completion_tokens)
    return samples


def _usable_tokens(row: Any) -> Optional[Tuple[int, int]]:
    """``(prompt_tokens, completion_tokens)`` this row may be fitted on.

    ``attempts.usage`` first -- it is per attempt, so a call that was retried
    contributes each try's real cost -- and ``calls.fresh_usage`` second, for
    a successful call whose attempt row recorded no counts.  ``fresh_usage``
    is only consulted when ``reused_from`` is NULL: a reused call's usage is
    written as ZEROES (it spent nothing), and a zero would drag a fitted
    ratio toward zero while looking like data.

    Rows that cannot be fitted, and why:

    * no ``prompt_tokens`` -- the gateway reported none, or this is a
      non-anchor member of a batched invocation, whose usage carries the
      bookkeeping fields alone (``CouncilWorker._invocation_usage``).
    * ``batch_size`` above 1 -- the anchor's counts cover the WHOLE
      invocation's prompt, so dividing them by ONE item's characters would
      overstate the ratio by the batch size.
    * a non-positive ``prompt_tokens``.

    A batched call therefore contributes NOTHING, and the ``fresh_usage``
    fallthrough cannot smuggle it back in: ``CouncilWorker`` completes every
    batch member with ``fresh_usage=None`` (``_settle_batch_ok``), so the
    whole invocation's counts exist only on the anchor's attempt row, where
    the ``batch_size`` test above rejects them.
    """
    for usage in (row.usage, _fresh_usage(row)):
        if not isinstance(usage, Mapping):
            continue
        if int(usage.get("batch_size") or 1) > 1:
            continue
        prompt_tokens = usage.get("prompt_tokens")
        if prompt_tokens is None:
            continue
        try:
            prompt = int(prompt_tokens)
            completion = int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if prompt <= 0:
            continue
        return prompt, max(completion, 0)
    return None


def _fresh_usage(row: Any) -> Any:
    """``calls.fresh_usage``, but only where it records a real spend."""
    if row.reused_from is not None or str(row.status) != "ok":
        return None
    return row.fresh_usage


def _fit_one(
    seat_name: str,
    role: str,
    sample: Optional[_Sample],
    upper_percentile: float,
) -> SeatTokenFit:
    """One seat's fit, measured if there is history and projected if not."""
    if sample is None or sample.n == 0 or sample.chars <= 0:
        return _projected(seat_name, role)

    # Ratio of the SUMS, not the mean of the per-attempt ratios: a quote
    # multiplies a corpus-wide character count by this number, and
    # sum(tokens)/sum(chars) is the estimator that makes that product exact
    # over the fitted sample.  (On the 2026-10-20 run the two agree to
    # within 0.5%, so the choice is about which total is reproduced, not
    # about moving the number.)
    return SeatTokenFit(
        seat_name=seat_name,
        role=role,
        input_tokens_per_char=sample.input_tokens / sample.chars,
        output_tokens_mean=sum(sample.output_tokens) / sample.n,
        output_tokens_upper=_percentile(sample.output_tokens, upper_percentile),
        n_attempts=sample.n,
        basis="measured",
        fallback=None,
        input_tokens_mean=sample.input_tokens / sample.n,
        prompt_chars_mean=sample.chars / sample.n,
    )


def _projected(seat_name: str, role: str) -> SeatTokenFit:
    """The fit for a seat with no usable history, named rather than guessed."""
    fallback = SEAT_FALLBACKS.get(seat_name)
    if fallback is None:
        fallback = ROLE_FALLBACKS.get(role)
    if fallback is None:
        raise ValueError(
            f"seat {seat_name!r} has no history and role {role!r} has no "
            f"fallback; expected one of {sorted(ROLE_FALLBACKS)}"
        )
    return SeatTokenFit(
        seat_name=seat_name,
        role=role,
        input_tokens_per_char=fallback.input_tokens / FALLBACK_PROMPT_CHARS,
        output_tokens_mean=float(fallback.output_tokens),
        output_tokens_upper=float(fallback.output_tokens) * PROJECTED_UPPER_FACTOR,
        n_attempts=0,
        basis="projected",
        fallback=(
            f"{fallback.source}: {fallback.input_tokens} in / "
            f"{fallback.output_tokens} out per item over "
            f"{FALLBACK_PROMPT_CHARS} prompt chars"
        ),
        input_tokens_mean=float(fallback.input_tokens),
        prompt_chars_mean=float(FALLBACK_PROMPT_CHARS),
    )


def _percentile(values: Sequence[int], percentile: float) -> float:
    """Nearest-rank percentile of *values*.

    An order statistic of the sample itself rather than an interpolation
    between two of them: the band is quoted as "a value this seat actually
    emitted", which is the claim an operator approving a cost can check.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values to take a percentile of")
    rank = math.ceil(percentile / 100.0 * len(ordered)) - 1
    return float(ordered[min(max(rank, 0), len(ordered) - 1)])


# ---------------------------------------------------------------------------
# Seat argument
# ---------------------------------------------------------------------------


def _requested_pairs(seats: Any) -> List[Tuple[str, str]]:
    """``[(seat_name, role), ...]`` from a roster or an explicit pair list."""
    roster_seats = getattr(seats, "seats", None)
    pairs: List[Tuple[str, str]] = []
    if callable(roster_seats):
        pairs = [(_seat_name(seat), str(role)) for role, seat in roster_seats()]
    else:
        for entry in seats or ():
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                role, seat = entry
                pairs.append((_seat_name(seat), str(role)))
                continue
            raise TypeError(
                "seats must be a Roster or an iterable of (role, seat) pairs; "
                f"got a {type(entry).__name__}"
            )

    unknown = {role for _name, role in pairs} - set(ROLE_FALLBACKS)
    if unknown:
        raise ValueError(
            f"unknown council role(s) {sorted(unknown)}; "
            f"expected {sorted(ROLE_FALLBACKS)}"
        )
    # Deduplicated, order preserved: a roster may seat the same model twice
    # under ``allow_meta_overlap``, and fitting it twice would publish the
    # same row twice.
    seen: Set[Tuple[str, str]] = set()
    return [p for p in pairs if not (p in seen or seen.add(p))]


def _seat_name(seat: Any) -> str:
    """A seat's name, whether a ``SeatSpec`` or already a name, was passed."""
    name = getattr(seat, "seat_name", None)
    return str(name if name is not None else seat)


# ===========================================================================
# The quote
# ===========================================================================
#
# THE QUOTE MUST SPEND NOTHING.  Everything below renders prompts and reads
# the store; it never instantiates a backend, and the module docstring's rule
# -- this import graph may not reach a network client -- is what holds that
# up.  ``prompt_hash`` is a pure function of the seat spec, the reuse
# namespace, the effective params, the answer schema, the query identity, the
# renderer id version, the role and the two prompt strings; every one of
# those is available locally, which is the whole reason an EXACT reuse count
# is possible before a token is spent.


#: Fraction of submitted items that reach META, measured over the completed
#: 2026-10-20 ``function_to_go`` run: 24.1% of n = 19,268 verdicts.  Earlier
#: estimates of 16.7% (n=6), 19.2% (n=73) and 38.7% (n=475) are WITHDRAWN --
#: every one of them was a sample too small to price a corpus from, and they
#: disagree with each other by more than a factor of two.  The rate is always
#: published WITH :data:`META_FIRE_RATE_N` beside it, because a rate without
#: its sample size is the mistake those three withdrawn numbers are made of.
META_FIRE_RATE = 0.241

#: Sample size :data:`META_FIRE_RATE` was measured over.
META_FIRE_RATE_N = 19268

#: Paid-repair volume as a fraction of the run's paid calls, measured: of
#: 19,268 verdicts, 113 items failed with a silently truncated ARGO response
#: (``invalid_output``, which is NOT in ``RETRYABLE_KINDS`` -- only
#: ``transport`` and ``timeout`` are -- so the worker never retried them) and
#: 105 came back on a plain retry, for 0.30% of run cost.  The retry pass is
#: a first-class stage, so it is a first-class LINE: a quote that omitted it
#: would under-quote every run by this much and the approval ceiling would be
#: set below what the run actually needs.
PAID_REPAIR_FRACTION = 0.0030

#: Upper band for the repair line.  The repair pass can at most re-ask every
#: item that errored, and the measured error rate on the same run was 0.59%
#: (114 of 19,276 submitted).  So this is a true ceiling on the line rather
#: than a multiple of the point estimate.
PAID_REPAIR_FRACTION_UPPER = 0.0059

#: Mean seconds one call occupies a concurrency slot, used ONLY for a seat
#: the store holds no timing for.  Derived from the one measured end-to-end
#: fact and stated so it can be checked: the 2026-10-20 run took 9.46 h
#: (34,056 s) for 19,276 items with the MAG queues summing to 6 concurrent
#: and two MAG proposer seats, i.e. 38,552 MAG calls through 6 slots, giving
#: 6 * 34056 / 38552 = 5.30 s per call.  That assumes MAG was the binding
#: queue, which is why a wall clock resting on this value is reported
#: ``projected`` and names the assumption.
FALLBACK_SERVICE_TIME_S = 5.30

#: Schema version of the published ``QUOTE.json``.  Bumped only on a BREAKING
#: change to the object; additive fields do not bump it.
QUOTE_SCHEMA_VERSION = 1

#: Version of the ``PLAN.json`` contract :func:`load_plan` reads.
PLAN_SCHEMA_VERSION = 1

#: The file a plan is read from inside a run directory.
PLAN_FILENAME = "PLAN.json"

#: The file a quote is written to inside a run directory.
QUOTE_FILENAME = "QUOTE.json"

#: SQLite's default host-parameter ceiling is 999.  Reuse lookups are issued
#: as chunked ``IN`` queries against ``ix_calls_prompt_hash_status`` -- one
#: indexed probe per hash, just not one ROUND TRIP per hash: a 19k-item plan
#: is ~58k proposer hashes, and 58k statements would make the quote itself
#: the slowest stage of the pipeline.
_SQL_CHUNK = 900

#: The three reasons a prompt hash can MISS.  Reported separately because a
#: transport change makes a cheap run quote like a full corpus re-run, and
#: without the split a pure cost optimisation reads as a bug.
MISS_KINDS = ("new_item", "bench_change", "transport_change")

__all__ += [
    "META_FIRE_RATE",
    "META_FIRE_RATE_N",
    "PAID_REPAIR_FRACTION",
    "PAID_REPAIR_FRACTION_UPPER",
    "FALLBACK_SERVICE_TIME_S",
    "QUOTE_SCHEMA_VERSION",
    "PLAN_SCHEMA_VERSION",
    "PLAN_FILENAME",
    "QUOTE_FILENAME",
    "MISS_KINDS",
    "PlanItem",
    "Plan",
    "QuoteLine",
    "Quote",
    "load_plan",
    "quote_plan",
]


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanItem:
    """One un-run function the plan proposes to submit.

    ``query_id`` is THE JOIN KEY into the clearinghouse -- the clearinghouse's
    own ``entity_hash``, carried through from the ranked file.  It is carried
    here, unused by the quote itself, so that the quote can CHECK it is
    present on every item: measured 2026-10-21, the council's own
    ``items.entity_hash`` matched 0 of 19,268 clearinghouse hashes while the
    definition TEXT matched 19,268 of 19,268 -- so a run published without
    ``query_id`` would be joinable only on text, which is the one join this
    pipeline must never make.
    """

    item_id: str
    evidence: Mapping[str, Any]
    menu: Sequence[Mapping[str, Any]]
    query_id: Optional[str] = None

    def identity(self) -> Dict[str, Any]:
        """What this item contributes to the plan hash."""
        return {
            "item_id": self.item_id,
            "query_id": self.query_id,
            "evidence": dict(self.evidence or {}),
            "menu": [dict(entry) for entry in (self.menu or ())],
        }


@dataclass(frozen=True)
class Plan:
    """A run plan: which query, which bench, and which items to submit.

    Produced by stage 1 (``kbu ontomap council-plan``) and consumed here.
    The quote binds to :attr:`plan_hash`, so an approval can name a quote and
    the run can refuse a plan that has moved underneath it.
    """

    query_name: str
    query_version: str
    items: Tuple[PlanItem, ...]
    mapping_hash: str = ""
    profile: Optional[str] = None
    run_dir: Optional[str] = None
    source: str = ""

    @property
    def query_ref(self) -> str:
        """``"<name>@<version>"`` -- the ``query`` of the parameter set."""
        return f"{self.query_name}@{self.query_version}"

    @property
    def plan_hash(self) -> str:
        """Content address of this plan.

        Covers the query identity, the mapping hash and every item's content
        IN PLAN ORDER.  Deliberately excludes ``run_dir``, ``source`` and any
        generation timestamp: the same plan written to two directories, or
        regenerated unchanged, must hash the same or an approval could not
        bind to it across a re-plan.
        """
        return canonical_sha256(
            {
                "plan_version": PLAN_SCHEMA_VERSION,
                "query": self.query_ref,
                "mapping_hash": self.mapping_hash,
                "profile": self.profile,
                "items": [item.identity() for item in self.items],
            }
        )


def _plan_query_ref(raw: Mapping[str, Any]) -> Tuple[str, str]:
    """``(name, version)`` from a plan's ``query``, in either spelling."""
    query = raw.get("query")
    if isinstance(query, Mapping):
        name = str(query.get("name") or "").strip()
        version = str(query.get("version") or "").strip()
    else:
        text = str(query or "").strip()
        name, _, version = text.partition("@")
        name, version = name.strip(), version.strip()
    if not name:
        raise ValueError(
            "the plan declares no query; expected "
            '`"query": "<name>@<version>"` or `{"name": ..., "version": ...}`'
        )
    if not version:
        raise ValueError(
            f"the plan's query {name!r} names no VERSION. A query version is "
            "part of the clearinghouse parameter set and of every prompt "
            "hash, so a plan that leaves it to be guessed could be quoted "
            "against one spec and run against another."
        )
    return name, version


def _plan_items(raw: Mapping[str, Any], run_dir: Optional[Path]) -> Tuple[PlanItem, ...]:
    """The plan's items, inline or from the JSONL file it names.

    Both spellings exist because both are real: a few hundred items are
    readable inline, and the ``function_to_go`` corpus is 47,341 functions
    whose evidence and menus average 6.9 kB -- a third of a gigabyte that
    nobody wants inside a file an operator is expected to read.
    """
    rows: List[Any]
    inline = raw.get("items")
    items_file = raw.get("items_file")
    if inline is not None and items_file:
        raise ValueError(
            "the plan declares BOTH `items` and `items_file`; it must declare "
            "exactly one, because two item lists could disagree and the plan "
            "hash would then depend on which one was read"
        )
    if items_file:
        path = Path(str(items_file))
        if not path.is_absolute():
            if run_dir is None:
                raise ValueError(
                    f"the plan names a relative items_file {items_file!r} but "
                    "was not loaded from a directory, so there is nothing to "
                    "resolve it against"
                )
            path = run_dir / path
        rows = []
        with path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    rows.append(json.loads(text))
                except ValueError as exc:
                    raise ValueError(f"{path}:{number}: not valid JSON: {exc}") from exc
    elif inline is not None:
        rows = list(inline)
    else:
        raise ValueError("the plan declares neither `items` nor `items_file`")

    items: List[PlanItem] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(
                f"plan item {index} is a {type(row).__name__}, expected an "
                "object with item_id / evidence / menu"
            )
        item_id = str(row.get("item_id") or "").strip()
        if not item_id:
            raise ValueError(f"plan item {index} has no item_id")
        items.append(
            PlanItem(
                item_id=item_id,
                evidence=dict(row.get("evidence") or {}),
                menu=[dict(entry) for entry in (row.get("menu") or ())],
                query_id=(
                    str(row["query_id"]) if row.get("query_id") is not None else None
                ),
            )
        )
    if not items:
        raise ValueError("the plan holds no items; there is nothing to quote")

    duplicates = sorted(_duplicates([item.item_id for item in items]))
    if duplicates:
        raise ValueError(
            f"the plan repeats item_id(s) {duplicates[:5]}; item ids are the "
            "key the run and the publish stage join on, so a repeat would "
            "quote one item twice and publish one of two different answers"
        )
    return tuple(items)


def _duplicates(values: Sequence[str]) -> Set[str]:
    """The values appearing more than once in *values*."""
    seen: Set[str] = set()
    repeated: Set[str] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return repeated


def load_plan(source: Union[str, Path, Mapping[str, Any]]) -> Plan:
    """Load a :class:`Plan` from a run directory, a ``PLAN.json`` or a dict.

    Args:
        source: a run directory holding :data:`PLAN_FILENAME`, a path to the
            plan file itself, or an already-parsed mapping.

    Raises:
        FileNotFoundError: the directory holds no plan file.
        ValueError: the plan is unusable as written.
    """
    run_dir: Optional[Path] = None
    text_source = "<dict>"
    if isinstance(source, Mapping):
        raw: Mapping[str, Any] = source
    else:
        path = Path(source).expanduser()
        if path.is_dir():
            run_dir = path
            path = path / PLAN_FILENAME
        else:
            run_dir = path.parent
        if not path.exists():
            raise FileNotFoundError(
                f"no plan at {path}. Stage 1 writes it: `kbu ontomap council-plan`"
            )
        text_source = str(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"{text_source}: expected a JSON object, got {type(raw).__name__}")

    name, version = _plan_query_ref(raw)
    plan = Plan(
        query_name=name,
        query_version=version,
        items=_plan_items(raw, run_dir),
        mapping_hash=str(raw.get("mapping_hash") or ""),
        profile=(str(raw["profile"]) if raw.get("profile") else None),
        run_dir=str(run_dir) if run_dir is not None else None,
        source=text_source,
    )
    declared = str(raw.get("plan_hash") or "")
    if declared and declared != plan.plan_hash:
        raise ValueError(
            f"{text_source}: the plan declares plan_hash {declared[:12]}... but "
            f"its contents hash to {plan.plan_hash[:12]}.... The plan has been "
            "edited since it was written; re-run stage 1 rather than quoting a "
            "plan whose approval could not bind to it."
        )
    return plan


# ---------------------------------------------------------------------------
# The quote's line items
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteLine:
    """One line of the per-item CALL GRAPH.

    Five seats is not five identical calls, which is why the graph is
    enumerated rather than multiplied out:

    * three PROPOSER calls, unconditional, one per item;
    * ONE CRITIC call, ALSO unconditional -- ``engine/decide.py`` step 5
      returns ``need_critic`` with no guard whatsoever, so a critic call
      carries no information about whether the proposers disagreed and must
      not be quoted as if it did;
    * one META call on a FRACTION of items (:data:`META_FIRE_RATE`);
    * one paid-repair line for the retry pass.

    Attributes:
        basis: ``exact`` or ``projected`` -- THE TAG REQUIRED ON EVERY LINE.
            It describes the line's VOLUME: ``exact`` means every call on
            this line is enumerated and its prompt rendered, ``projected``
            means the call count itself is an estimate.  The token figures
            on an ``exact`` line still come from the fitted token model
            (``token_model_basis`` records that separately), because no
            tokenizer is correct across a roster spanning three vendors.
    """

    line: str
    role: str
    basis: str
    basis_reason: str
    calls_total: float
    calls_reused: float
    calls_paid: float
    input_tokens_point: float
    output_tokens_point: float
    output_tokens_upper: float
    seat_name: Optional[str] = None
    gateway: Optional[str] = None
    endpoint: Optional[str] = None
    endpoint_kind: Optional[str] = None
    canonical_model: Optional[str] = None
    reuse_basis: Optional[str] = None
    misses: Optional[Mapping[str, int]] = None
    rendered_prompt_chars: Optional[int] = None
    prompt_chars: Optional[int] = None
    token_model_basis: Optional[str] = None
    token_model_n: Optional[int] = None
    note: str = ""

    @property
    def total_tokens_point(self) -> float:
        return self.input_tokens_point + self.output_tokens_point

    @property
    def total_tokens_upper(self) -> float:
        # Input is NOT banded.  It is 86% of all tokens on the measured run
        # and it is the one quantity the quote genuinely knows: the prompts
        # are rendered here, so the only error in the input figure is the
        # tokens-per-character fit, not the volume.  The band is therefore
        # driven entirely by output, which is the part a model chooses.
        return self.input_tokens_point + self.output_tokens_upper

    def to_dict(self) -> Dict[str, Any]:
        """The published shape of one line."""
        payload: Dict[str, Any] = {
            "line": self.line,
            "role": self.role,
            "basis": self.basis,
            "basis_reason": self.basis_reason,
            "seat_name": self.seat_name,
            "gateway": self.gateway,
            "endpoint": self.endpoint,
            "endpoint_kind": self.endpoint_kind,
            "canonical_model": self.canonical_model,
            "calls_total": round(self.calls_total, 2),
            "calls_reused": round(self.calls_reused, 2),
            "calls_paid": round(self.calls_paid, 2),
            "reuse_basis": self.reuse_basis,
            "misses": dict(self.misses) if self.misses is not None else None,
            "rendered_prompt_chars": self.rendered_prompt_chars,
            "prompt_chars": self.prompt_chars,
            "input_tokens_point": round(self.input_tokens_point),
            "output_tokens_point": round(self.output_tokens_point),
            "output_tokens_upper": round(self.output_tokens_upper),
            "total_tokens_point": round(self.total_tokens_point),
            "total_tokens_upper": round(self.total_tokens_upper),
            "token_model_basis": self.token_model_basis,
            "token_model_n": self.token_model_n,
        }
        if self.note:
            payload["note"] = self.note
        return payload


@dataclass(frozen=True)
class Quote:
    """A priced plan, serialisable to ``QUOTE.json``.

    Carries the :attr:`plan_hash` it was computed against so the APPROVAL can
    bind to it: stage 3 records a human saying yes to one specific quote, and
    stage 4 refuses a plan whose hash has moved since.
    """

    plan_hash: str
    plan: Mapping[str, Any]
    parameter_set: Mapping[str, Any]
    provenance: Mapping[str, Any]
    lines: Tuple[QuoteLine, ...]
    reuse: Mapping[str, Any]
    wall_clock: Mapping[str, Any]
    gateways: Tuple[Mapping[str, Any], ...]
    balance_delta: Mapping[str, Any]
    token_model: Mapping[str, Any]
    meta_fire_rate: float
    meta_fire_rate_n: int
    generated_at: str
    notes: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    # -- totals -----------------------------------------------------------

    @property
    def parameter_set_hash(self) -> str:
        """The clearinghouse partition key for this run.

        ``{query, bench_fingerprint, mapping_hash}`` and nothing else.  NOT
        ``Roster.fingerprint()``: that covers the endpoint kind, the route
        spelling, the token ceiling, the temperature and the extras, so
        moving one seat from the codex CLI to MAG would move it -- and
        re-partitioning on a pure routing change would make all 47,341
        clearinghouse functions un-run and re-charge ~199M tokens for a cost
        optimisation.  The roster and route fingerprints are recorded in
        :attr:`provenance` instead, which is what they are for.
        """
        return canonical_sha256(dict(self.parameter_set))

    def totals(self) -> Dict[str, Any]:
        """Point and upper totals over every line, and what is projected.

        The PROJECTED FRACTION is published at the top of the output because
        it is the number that says how much of this quote is a measurement
        and how much is an extrapolation.
        """
        point = sum(line.total_tokens_point for line in self.lines)
        upper = sum(line.total_tokens_upper for line in self.lines)
        projected_point = sum(
            line.total_tokens_point for line in self.lines if line.basis == "projected"
        )
        return {
            "point": {
                "input_tokens": round(sum(l.input_tokens_point for l in self.lines)),
                "output_tokens": round(sum(l.output_tokens_point for l in self.lines)),
                "total_tokens": round(point),
            },
            "upper": {
                "input_tokens": round(sum(l.input_tokens_point for l in self.lines)),
                "output_tokens": round(sum(l.output_tokens_upper for l in self.lines)),
                "total_tokens": round(upper),
            },
            "calls_total": round(sum(l.calls_total for l in self.lines), 1),
            "calls_reused": round(sum(l.calls_reused for l in self.lines), 1),
            "calls_paid": round(sum(l.calls_paid for l in self.lines), 1),
            "projected_tokens": round(projected_point),
            "projected_fraction_of_total": (
                round(projected_point / point, 4) if point > 0 else 0.0
            ),
        }

    def to_dict(self) -> Dict[str, Any]:
        """The published ``QUOTE.json`` object."""
        totals = self.totals()
        priced = [g for g in self.gateways if g.get("priced")]
        unpriced = [str(g["gateway"]) for g in self.gateways if not g.get("priced")]
        return {
            "schema_version": QUOTE_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "plan_hash": self.plan_hash,
            "plan": dict(self.plan),
            "parameter_set": dict(self.parameter_set),
            "parameter_set_hash": self.parameter_set_hash,
            "provenance": dict(self.provenance),
            # The projected fraction is the FIRST thing in the headline: see
            # `totals`.
            "headline": {
                "projected_fraction_of_total": totals["projected_fraction_of_total"],
                "point": totals["point"],
                "upper": totals["upper"],
                "calls_total": totals["calls_total"],
                "calls_reused": totals["calls_reused"],
                "calls_paid": totals["calls_paid"],
                "wall_clock_hours_point": self.wall_clock.get("hours_point"),
                "wall_clock_hours_upper": self.wall_clock.get("hours_upper"),
                "dollars_point": (
                    round(sum(float(g["dollars_point"]) for g in priced), 2)
                    if priced
                    else None
                ),
                "dollars_upper": (
                    round(sum(float(g["dollars_upper"]) for g in priced), 2)
                    if priced
                    else None
                ),
                "dollars_complete": not unpriced,
                "unpriced_gateways": unpriced,
            },
            "meta_fire_rate": {
                "rate": self.meta_fire_rate,
                "n": self.meta_fire_rate_n,
                "basis": "projected",
                "source": (
                    "measured over the completed 2026-10-20 function_to_go run; "
                    "earlier estimates at n=6, n=73 and n=475 are withdrawn"
                ),
            },
            "call_graph": [line.to_dict() for line in self.lines],
            "reuse": dict(self.reuse),
            "wall_clock": dict(self.wall_clock),
            "gateways": [dict(g) for g in self.gateways],
            "balance_delta": dict(self.balance_delta),
            "token_model": dict(self.token_model),
            "notes": list(self.notes),
            "warnings": list(self.warnings),
        }

    def write(self, destination: Union[str, Path]) -> Path:
        """Write ``QUOTE.json`` to a run directory, or to a named file."""
        path = Path(destination).expanduser()
        if path.is_dir():
            path = path / QUOTE_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path


# ---------------------------------------------------------------------------
# Reuse: the exact count, and why each miss missed
# ---------------------------------------------------------------------------


def _hits(engine: Any, hashes: Sequence[str]) -> Set[str]:
    """Which of *hashes* already have an answered call.

    One indexed probe per hash against ``ix_calls_prompt_hash_status``
    (``prompt_hash``, ``status``), issued in chunks rather than one statement
    each -- see :data:`_SQL_CHUNK`.  A hit costs ZERO tokens: the worker will
    find the same row through ``find_reusable`` and copy the answer.
    """
    found: Set[str] = set()
    unique = sorted({h for h in hashes if h})
    if not unique:
        return found
    calls = sch.calls
    with engine.connect() as conn:
        for start in range(0, len(unique), _SQL_CHUNK):
            chunk = unique[start : start + _SQL_CHUNK]
            statement = (
                sa.select(calls.c.prompt_hash)
                .where(calls.c.prompt_hash.in_(chunk), calls.c.status == "ok")
                .distinct()
            )
            found.update(str(row[0]) for row in conn.execute(statement))
    return found


def _answered_models(
    engine: Any,
    keys: Sequence[Tuple[str, str]],
    role: str,
    query_name: str,
    query_version: str,
) -> Dict[Tuple[str, str], Set[str]]:
    """``{(entity_hash, data_hash): {canonical_model, ...}}`` already answered.

    THE MISS-ATTRIBUTION PROBE.  A prompt hash that missed tells you nothing
    about WHY, and the why is the whole point: a transport change makes a
    cheap run quote like a full corpus re-run, so without the split a pure
    cost optimisation reads as a bug.

    The probe deliberately narrows on the QUERY identity as well as the
    item's content, so a query-version bump reports its misses as
    ``new_item``: at a new version the prompt genuinely has never been
    asked, and calling that a bench or transport change would blame a
    configuration that did not move.
    """
    answers: Dict[Tuple[str, str], Set[str]] = {}
    unique = sorted({key for key in keys if key[0] and key[1]})
    if not unique:
        return answers
    calls = sch.calls
    # Keyed on entity_hash, which is indexed
    # (``ix_calls_query_model_entity``); data_hash is filtered in the same
    # statement and the pair is re-checked below, because one entity can
    # carry several menus and they are different questions.
    entities = sorted({key[0] for key in unique})
    wanted = set(unique)
    with engine.connect() as conn:
        for start in range(0, len(entities), _SQL_CHUNK):
            chunk = entities[start : start + _SQL_CHUNK]
            statement = sa.select(
                calls.c.entity_hash, calls.c.data_hash, calls.c.canonical_model
            ).where(
                calls.c.entity_hash.in_(chunk),
                calls.c.status == "ok",
                calls.c.role == role,
                calls.c.query_name == query_name,
                calls.c.query_version == query_version,
            ).distinct()
            for row in conn.execute(statement):
                key = (str(row[0]), str(row[1]))
                if key in wanted and row[2] is not None:
                    answers.setdefault(key, set()).add(str(row[2]))
    return answers


def _attribute(
    answered: Optional[Set[str]], canonical_model: str
) -> str:
    """Why one prompt hash missed: new item, bench change or transport change.

    * nothing has answered this item in this role at this query version ->
      ``new_item``;
    * something has, and THIS seat's model is among them -> the bench did not
      move, so the stored answer is unreusable only because the prompt hash
      keys on ``endpoint_kind`` (and on the reuse namespace and effective
      params) -> ``transport_change``.  That is the case this split exists
      for: the models are identical, the experiment is identical, and the
      run is about to pay again purely because the route changed;
    * something has, but not this model -> ``bench_change``.  This one is a
      LEGITIMATE re-charge: a different model is a different experiment, and
      :func:`~.types.bench_fingerprint` has re-partitioned the corpus.
    """
    if not answered:
        return "new_item"
    return "transport_change" if canonical_model in answered else "bench_change"


# ---------------------------------------------------------------------------
# Wall clock
# ---------------------------------------------------------------------------


def fit_service_times(store: Any, *, seats: Any) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Mean seconds a call of each ``(seat, role)`` occupies a slot.

    ``finished_at - started_at`` over successful attempts: SERVICE time, not
    queue wait (``started_at - queued_at``), because the wall clock below
    divides by a concurrency budget and would double-count the wait.

    Measured and relevant: the store's ``claim_calls`` is synchronous on the
    worker event loop and its cost is linear in the pending set -- 217 ms at
    19k pending rows against 10.4 ms at 2k -- which inflated service times
    6-9x on BOTH gateways at once with queue wait at 0.00 and collapsed
    throughput 10.8x.  So a service time fitted over a run that was chunked
    is the honest one, and a figure fitted over an unchunked 19k-row run
    overstates by that factor.  The chunking workaround is in the run stage
    (KBUtilLib issue 84); here it only means the fit's sample size matters,
    which is why it is reported.
    """
    pairs = _requested_pairs(seats)
    engine = getattr(store, "engine", None)
    if engine is None:
        raise TypeError(
            "fit_service_times needs a store exposing .engine (SqliteStore); "
            f"got {type(store).__name__}"
        )
    attempts, calls = sch.attempts, sch.calls
    statement = (
        sa.select(
            calls.c.seat_name,
            calls.c.role,
            sa.func.count().label("n"),
            sa.func.sum(
                sa.func.julianday(attempts.c.finished_at)
                - sa.func.julianday(attempts.c.started_at)
            ).label("days"),
        )
        .select_from(attempts.join(calls, calls.c.call_id == attempts.c.call_id))
        .where(
            attempts.c.outcome_kind == "ok",
            attempts.c.attempt_no >= 0,
            attempts.c.started_at.isnot(None),
            attempts.c.finished_at.isnot(None),
            calls.c.seat_name.in_(sorted({name for name, _role in pairs})),
        )
        .group_by(calls.c.seat_name, calls.c.role)
    )
    measured: Dict[Tuple[str, str], Dict[str, Any]] = {}
    with engine.connect() as conn:
        for row in conn.execute(statement):
            n = int(row.n or 0)
            seconds = float(row.days or 0.0) * 86400.0
            if n > 0 and seconds > 0:
                measured[(str(row.seat_name), str(row.role))] = {
                    "service_time_s": seconds / n,
                    "n": n,
                    "basis": "measured",
                }
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for pair in pairs:
        out[pair] = measured.get(
            pair,
            {
                "service_time_s": FALLBACK_SERVICE_TIME_S,
                "n": 0,
                "basis": "projected",
                "fallback": (
                    "6 concurrent MAG slots * 34,056 s / 38,552 MAG calls on the "
                    "2026-10-20 run = 5.30 s, assuming MAG was the binding queue"
                ),
            },
        )
    return out


def _queue_table(config: Any, roster: Any) -> Dict[str, Dict[str, Any]]:
    """``{queue_name: {seats, concurrency, rpm, gateway}}`` for this roster.

    Declared ``queues`` win where they cover a seat.  A seat no queue names
    falls into a synthetic per-GATEWAY queue, because that is what the engine
    does with it -- one worker, the endpoint's own ``max_concurrency``.

    The per-minute ceiling comes from the endpoint, and both ceilings are
    applied: MAG enforces 20 parallel AND 120 requests per minute,
    ACCOUNT-WIDE, and a config asking 24 parallel errored 427 of 500 pilot
    items over 1,925 transport attempts.  ARGO, by contrast, saturates at
    ~24 concurrent and showed zero rate limiting at 40 across three models.
    """
    declared = dict(getattr(config, "queues", {}) or {})
    seat_queue: Dict[str, str] = {
        seat_name: qname
        for qname, queue in declared.items()
        for seat_name in getattr(queue, "seats", ()) or ()
    }
    table: Dict[str, Dict[str, Any]] = {}
    for _role, seat in roster.seats():
        qname = seat_queue.get(seat.seat_name)
        cfg = _endpoint_cfg(config, seat)
        gateway = _gateway_of(config, seat)
        if qname is not None:
            queue = declared[qname]
            concurrency = int(queue.concurrency) * int(getattr(queue, "workers", 1) or 1)
            entry = table.setdefault(
                qname,
                {
                    "queue": qname,
                    "declared": True,
                    "seats": [],
                    "gateways": set(),
                    "concurrency": concurrency,
                    "requests_per_minute": None,
                },
            )
        else:
            qname = f"(undeclared:{gateway})"
            entry = table.setdefault(
                qname,
                {
                    "queue": qname,
                    "declared": False,
                    "seats": [],
                    "gateways": set(),
                    "concurrency": int(cfg.get("max_concurrency") or 0) or None,
                    "requests_per_minute": None,
                },
            )
        entry["seats"].append(seat.seat_name)
        entry["gateways"].add(gateway)
        rpm = cfg.get("max_requests_per_minute")
        if rpm:
            current = entry.get("requests_per_minute")
            entry["requests_per_minute"] = (
                float(rpm) if current is None else min(float(current), float(rpm))
            )
    return table


def _wall_clock(
    queue_table: Mapping[str, Mapping[str, Any]],
    paid_by_seat: Mapping[str, float],
    service_times: Mapping[Tuple[str, str], Mapping[str, Any]],
    seat_roles: Mapping[str, str],
) -> Dict[str, Any]:
    """Hours to drain the plan, as the SLOWEST QUEUE and never the sum.

    Queues run concurrently, so the run finishes when the last one does.
    Adding them would quote a council of five seats as if it ran them one
    after another, which is both wrong and wrong in the expensive direction:
    an approval ceiling set from a summed wall clock would be hours too
    generous and would not halt a run that had genuinely stalled.

    Each queue's throughput is the SMALLER of what its concurrency allows
    (``concurrency / service_time``) and what the account's per-minute
    ceiling allows -- the BINDING limit, whichever it is, is named in the
    output.
    """
    rows: List[Dict[str, Any]] = []
    for name in sorted(queue_table):
        queue = queue_table[name]
        calls = sum(float(paid_by_seat.get(seat, 0.0)) for seat in queue["seats"])
        times = [
            float(service_times[(seat, seat_roles[seat])]["service_time_s"])
            for seat in queue["seats"]
            if (seat, seat_roles.get(seat, "")) in service_times
        ]
        service = max(times) if times else FALLBACK_SERVICE_TIME_S
        projected = any(
            str(service_times[(seat, seat_roles[seat])].get("basis")) == "projected"
            for seat in queue["seats"]
            if (seat, seat_roles.get(seat, "")) in service_times
        )
        concurrency = queue.get("concurrency")
        by_concurrency = (
            (float(concurrency) / service) if concurrency and service > 0 else None
        )
        rpm = queue.get("requests_per_minute")
        by_rate = (float(rpm) / 60.0) if rpm else None
        candidates = [
            (value, label)
            for value, label in ((by_concurrency, "concurrency"), (by_rate, "requests_per_minute"))
            if value is not None
        ]
        if candidates:
            throughput, binding = min(candidates, key=lambda pair: pair[0])
        else:
            throughput, binding = None, "unknown"
        hours = (calls / throughput / 3600.0) if throughput else None
        rows.append(
            {
                "queue": name,
                "declared": bool(queue.get("declared")),
                "seats": sorted(queue["seats"]),
                "gateways": sorted(queue["gateways"]),
                "calls_paid": round(calls, 1),
                "concurrency": concurrency,
                "requests_per_minute": rpm,
                "service_time_s": round(service, 2),
                "binding_limit": binding,
                "calls_per_second": round(throughput, 3) if throughput else None,
                "hours": round(hours, 4) if hours is not None else None,
                "basis": "projected" if projected or hours is None else "exact",
            }
        )
    timed = [row for row in rows if row["hours"] is not None]
    slowest = max(timed, key=lambda row: row["hours"]) if timed else None
    hours_point = slowest["hours"] if slowest else None
    return {
        "binding_queue": slowest["queue"] if slowest else None,
        "binding_limit": slowest["binding_limit"] if slowest else None,
        "hours_point": hours_point,
        # The upper band scales the slowest queue only: the band is about how
        # long each call takes, and a queue that was not binding at the point
        # estimate does not become binding by the same multiple.
        "hours_upper": (
            round(hours_point * _WALL_CLOCK_UPPER_FACTOR, 4)
            if hours_point is not None
            else None
        ),
        "upper_factor": _WALL_CLOCK_UPPER_FACTOR,
        "basis": slowest["basis"] if slowest else "projected",
        "queues": rows,
        "rule": "the slowest queue, never the sum: queues run concurrently",
        "note": (
            "Assumes the run stage keeps ONE ~2,000-item chunk in flight. "
            "claim_calls is synchronous on the worker event loop and linear "
            "in the pending set (217 ms at 19k pending rows vs 10.4 ms at "
            "2k), which collapsed throughput 10.8x on the unchunked run. "
            "WORKAROUND for KBUtilLib issue 84."
        ),
    }


#: Multiple applied to the slowest queue's hours for the upper band.  Not a
#: guess: the measured run's own service times inflated 6-9x when the pending
#: set was 19k rows, and the chunking workaround is what keeps that from
#: recurring.  2.0 is the band for a run that is chunked as designed and
#: still runs slower than the fit -- it does NOT cover an unchunked run,
#: which is a configuration error rather than variance.
_WALL_CLOCK_UPPER_FACTOR = 2.0


# ---------------------------------------------------------------------------
# Gateways: dollars ONLY where a rate is configured
# ---------------------------------------------------------------------------


def _endpoint_cfg(config: Any, seat: Any) -> Dict[str, Any]:
    """The seat's endpoint config as a dict, with its kind filled in."""
    try:
        cfg = dict(config.endpoint(seat.endpoint))
    except Exception:  # noqa: BLE001 - an unknown endpoint is not a quote failure
        cfg = {}
    cfg.setdefault("kind", seat.endpoint_kind)
    return cfg


def _gateway_of(config: Any, seat: Any) -> str:
    """Which PAYER a seat's spend lands on.

    The endpoint's ``account`` when it declares one, else the endpoint alias.
    Account first because the limits and the bill are account-wide: MAG's 20
    parallel and 120 requests per minute are enforced across every endpoint
    on the account, so two aliases on one account are one gateway for both
    pricing and wall clock.
    """
    cfg = _endpoint_cfg(config, seat)
    return str(cfg.get("account") or seat.endpoint)


def _price_gateways(
    config: Any,
    lines: Sequence[QuoteLine],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Per-gateway token totals, priced ONLY where a rate is configured.

    NEVER substitutes a vendor list rate for a gateway's own.  The reason is
    measured, not squeamish: every ARGO and MAG usage, billing, pricing and
    quota path 404s, MAG's ``/models/<id>`` returns 401 "Only proxy admin",
    and a $350/month ARGO cap was exhausted in a week by ordinary traffic --
    which cannot happen at pass-through.  So ARGO's effective multiple over
    list is unknown and above 1.  A list-derived figure is a FLOOR, and
    presenting one as a cost is an error this project has already made once.

    A gateway with no rate is reported ``unpriced``, which is a usable answer
    (the operator knows what is missing) in a way that a confidently wrong
    dollar figure is not.
    """
    rates = dict(getattr(config, "rates", {}) or {})
    buckets: Dict[str, Dict[str, float]] = {}
    for line in lines:
        gateway = line.gateway
        if gateway is None:
            continue
        bucket = buckets.setdefault(
            gateway, {"input": 0.0, "output_point": 0.0, "output_upper": 0.0}
        )
        bucket["input"] += line.input_tokens_point
        bucket["output_point"] += line.output_tokens_point
        bucket["output_upper"] += line.output_tokens_upper

    out: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for gateway in sorted(buckets):
        bucket = buckets[gateway]
        entry: Dict[str, Any] = {
            "gateway": gateway,
            "input_tokens": round(bucket["input"]),
            "output_tokens_point": round(bucket["output_point"]),
            "output_tokens_upper": round(bucket["output_upper"]),
            "total_tokens_point": round(bucket["input"] + bucket["output_point"]),
            "total_tokens_upper": round(bucket["input"] + bucket["output_upper"]),
        }
        rate = rates.get(gateway)
        if rate is None:
            entry.update(
                {
                    "priced": False,
                    "dollars_point": None,
                    "dollars_upper": None,
                    "currency": None,
                    "rate": None,
                    "is_floor": None,
                    "rate_provenance": None,
                    "unpriced_reason": (
                        f"no rate configured for gateway {gateway!r} under "
                        "`ai_council.rates`. A vendor list rate is NOT "
                        "substituted: it would be a floor, not a cost."
                    ),
                }
            )
            warnings.append(
                f"gateway {gateway!r} is unpriced: "
                f"{entry['total_tokens_point']:,} tokens carry no dollar figure"
            )
        else:
            per_in = float(rate.input_per_mtok) / 1e6
            per_out = float(rate.output_per_mtok) / 1e6
            entry.update(
                {
                    "priced": True,
                    "dollars_point": round(
                        bucket["input"] * per_in + bucket["output_point"] * per_out, 2
                    ),
                    "dollars_upper": round(
                        bucket["input"] * per_in + bucket["output_upper"] * per_out, 2
                    ),
                    "currency": str(rate.currency),
                    "rate": {
                        "input_per_mtok": float(rate.input_per_mtok),
                        "output_per_mtok": float(rate.output_per_mtok),
                    },
                    "is_floor": bool(rate.is_floor),
                    "rate_provenance": str(rate.provenance),
                }
            )
            if rate.is_floor:
                warnings.append(
                    f"gateway {gateway!r} is priced from a rate its own "
                    f"provenance marks as a FLOOR ({rate.provenance}); the "
                    "dollar figure is a lower bound, not a cost"
                )
        out.append(entry)
    return out, warnings


def _balance_delta_block(gateways: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The operator-filled before/after balance record, one row per gateway.

    Every completed run is the only calibration this pipeline can get: the
    gateways publish no usage, billing, pricing or quota endpoint that
    answers (all 404, and MAG's ``/models/<id>`` returns 401 "Only proxy
    admin"), so the effective dollars-per-token is observable ONLY as the
    difference between two balances a human read either side of a run.

    The fields are emitted EMPTY and are meant to be filled in -- before the
    run and after it -- so that the next run's dollar figure can be derived
    from this run's measurement rather than from a vendor's list page.
    ``implied_input_per_mtok`` is what stage 5 computes once both balances
    are present, and what an operator should then write into
    ``ai_council.rates`` with ``provenance`` naming this run.
    """
    return {
        "purpose": (
            "Read each gateway's balance immediately before and after the "
            "run and record it here. The delta over this quote's token "
            "totals is the only measurement of effective cost available: "
            "every gateway usage/billing/pricing/quota path 404s."
        ),
        "gateways": [
            {
                "gateway": entry["gateway"],
                "balance_before": None,
                "balance_after": None,
                "read_at_before": None,
                "read_at_after": None,
                "currency": entry.get("currency"),
                "delta": None,
                "quoted_total_tokens_point": entry["total_tokens_point"],
                "quoted_input_tokens": entry["input_tokens"],
                "quoted_output_tokens_point": entry["output_tokens_point"],
                "actual_total_tokens": None,
                "implied_input_per_mtok": None,
                "implied_output_per_mtok": None,
            }
            for entry in gateways
        ],
    }


# ---------------------------------------------------------------------------
# quote_plan
# ---------------------------------------------------------------------------


def _upper_rate(rate: float, n: int) -> float:
    """Normal-approximation upper 95% bound on a proportion.

    The approval's ceiling derives from the BAND, not the mean.  A ceiling
    set at the mean halts roughly half of all runs partway, and a run halted
    at 60% has spent 60% of the tokens for none of the result -- the worst
    outcome the stage can produce, worse than quoting generously.
    """
    if n <= 0:
        return min(1.0, rate * 2.0)
    standard_error = math.sqrt(max(rate * (1.0 - rate), 0.0) / n)
    return min(1.0, rate + 1.96 * standard_error)


def quote_plan(
    plan: Union[Plan, str, Path, Mapping[str, Any]],
    *,
    store: Any,
    config: Any,
    token_model: TokenModel,
    meta_fire_rate: float = META_FIRE_RATE,
    roster: Any = None,
    query: Any = None,
    meta_fire_rate_n: int = META_FIRE_RATE_N,
    effective_params: Optional[Any] = None,
) -> Quote:
    """Price *plan* exactly where it can be, and projected where it cannot.

    THE QUOTE SPENDS NOTHING.  Not "avoids spending": it CANNOT spend,
    because this module's import graph never reaches a backend (the property
    is tested, and tested with the socket module poisoned rather than with a
    stubbed HTTP client -- a seam every test replaces is untested).

    What is EXACT and what is PROJECTED, and why the line between them falls
    where it does:

    * PROPOSERS -- exact.  Their prompts depend only on the item, so every
      one is rendered here through the same renderer the worker uses, hashed
      with the same :func:`~.types.prompt_hash`, and looked up in the store.
      Volume, reuse and the three miss reasons are all counted, not
      estimated.
    * CRITIC -- exact VOLUME, derived reuse.  One critic call per item,
      unconditional: ``engine/decide.py`` step 5 returns ``need_critic``
      with no guard, so a critic call carries no information about whether
      the proposers disagreed.  Its PROMPT, though, contains the proposers'
      ballots, which do not exist before the run, so it cannot be rendered
      here and its reuse is derived instead (see below).
    * META -- projected.  Volume is ``submit_count * meta_fire_rate``, and
      the rate is published with its sample size.
    * PAID REPAIR -- projected.  The retry pass is a first-class stage, so
      it is a first-class line.

    Args:
        plan: a :class:`Plan`, or anything :func:`load_plan` accepts.
        store: the council store.  Read only -- no claim, no write, and no
            write lock.
        config: a :class:`~.config.CouncilConfig`.  Supplies the endpoints
            (for reuse namespaces and gateways), the queues (for wall clock)
            and ``rates`` (for dollars, where they exist).
        token_model: the fit from :func:`fit_token_model`, over this roster.
        meta_fire_rate: fraction of items that reach meta.  Defaults to the
            measured :data:`META_FIRE_RATE`.
        roster: the bench.  Defaults to ``config.roster(plan.profile)``.
        query: the query type.  Defaults to the store's registered
            ``<name>@<version>`` from the plan.
        meta_fire_rate_n: sample size behind *meta_fire_rate*, published
            beside it.  A rate without its sample size is the mistake the
            three withdrawn estimates are made of.
        effective_params: ``(seat) -> params`` returning what the api type
            serving that seat would REALLY send.  Must be pure and must not
            make a call; ``LLMApi.effective_params`` is specified that way.
            Supplying it is what makes the reuse count match the worker's
            own prompt hashes -- see :func:`_effective_params` for what the
            default costs, and why the default is nonetheless local.

    Returns:
        A :class:`Quote`, carrying the :attr:`Plan.plan_hash` it was computed
        against so an approval can bind to it.
    """
    from .types import bench_fingerprint

    if not isinstance(plan, Plan):
        plan = load_plan(plan)
    if roster is None:
        roster = config.roster(plan.profile)
    if query is None:
        query = _registered_query(store, plan)

    engine = getattr(store, "engine", None)
    if engine is None:
        raise TypeError(
            "quote_plan needs a store exposing .engine (SqliteStore); "
            f"got {type(store).__name__}"
        )
    _check_query_matches_plan(query, plan)

    warnings: List[str] = []
    notes: List[str] = []
    n_items = len(plan.items)

    params_of = effective_params or _effective_params
    if effective_params is None:
        approximated = sorted(
            seat.seat_name
            for _role, seat in roster.seats()
            if seat.endpoint_kind in ("argo", "openai")
        )
        if approximated:
            warnings.append(
                _EFFECTIVE_PARAMS_WARNING.format(seats=", ".join(approximated))
            )

    missing_query_id = sum(1 for item in plan.items if not item.query_id)
    if missing_query_id:
        warnings.append(
            f"{missing_query_id} of {n_items} plan items carry no query_id. "
            "query_id is the clearinghouse join key (its entity_hash, carried "
            "from the ranked file); the council's own items.entity_hash "
            "matched 0 of 19,268 clearinghouse hashes. Those items cannot be "
            "published without joining on definition TEXT, which this "
            "pipeline must never do."
        )

    # -- per-item content, computed once ---------------------------------
    tasks: List[CouncilTask] = []
    content_keys: List[Tuple[str, str]] = []
    prompt_chars: List[int] = []
    entity_field = str(getattr(query, "entity_field", "") or "")
    for item in plan.items:
        evidence = dict(item.evidence or {})
        task = CouncilTask(
            item_id=item.item_id,
            evidence=evidence,
            menu=[dict(entry) for entry in (item.menu or ())],
        )
        tasks.append(task)
        rest = {k: v for k, v in evidence.items() if k != entity_field}
        # Hashed from the VALIDATED menu, exactly as ``CouncilWorker._item_row``
        # does, and not from the plan's raw dicts.  A MenuEntry dumps every
        # field including the defaults the plan omitted (``score``,
        # ``metadata``), so hashing the raw dicts would produce a data_hash
        # that matched no stored row and silently report a fully reusable
        # corpus as entirely new.
        menu = [entry.model_dump(mode="json") for entry in task.menu or []]
        content_keys.append(
            (entity_hash(evidence.get(entity_field, "")), data_hash(rest, task.menu))
        )
        # The same dumped form the store holds in ``items.menu``, which is
        # what the token model was fitted against.
        prompt_chars.append(item_prompt_chars(evidence, menu))

    # -- proposer prompts, rendered ONCE per item ------------------------
    # The renderer takes (role, task, context) and the proposer context is
    # empty, so the prompt is identical for every proposer SEAT.  Only the
    # hash differs between seats (canonical model, endpoint kind, reuse
    # namespace, effective params), so rendering per seat would be three
    # times the work for the same strings.
    proposer_schema = query.answer_schema("proposer")
    rendered: List[Tuple[str, str]] = [
        query.render("proposer", task, {}) for task in tasks
    ]
    rendered_chars = sum(len(system) + len(user) for system, user in rendered)
    rid_version = renderer_id_version(query)

    lines: List[QuoteLine] = []
    paid_by_seat: Dict[str, float] = {}
    seat_roles: Dict[str, str] = {
        seat.seat_name: role for role, seat in roster.seats()
    }
    reuse_by_seat: Dict[str, Dict[str, Any]] = {}
    # Per item: did EVERY proposer hit reuse?  This is what decides whether
    # the critic can reuse anything at all -- if one proposer must run, its
    # ballot may differ, so the critic prompt is not the stored one.
    all_proposers_reused = [True] * n_items

    for seat in roster.proposers:
        hashes = [
            prompt_hash(
                seat,
                _reuse_namespace(config, seat),
                params_of(seat),
                proposer_schema,
                plan.query_name,
                plan.query_version,
                rid_version,
                "proposer",
                system,
                user,
            )
            for system, user in rendered
        ]
        hit = _hits(engine, hashes)
        reused_flags = [h in hit for h in hashes]
        miss_keys = [
            content_keys[index] for index, ok in enumerate(reused_flags) if not ok
        ]
        answered = _answered_models(
            engine, miss_keys, "proposer", plan.query_name, plan.query_version
        )
        misses = {kind: 0 for kind in MISS_KINDS}
        for index, ok in enumerate(reused_flags):
            if ok:
                continue
            all_proposers_reused[index] = False
            misses[_attribute(answered.get(content_keys[index]), seat.canonical_model)] += 1

        paid_chars = sum(
            chars for chars, ok in zip(prompt_chars, reused_flags) if not ok
        )
        paid_rendered = sum(
            len(system) + len(user)
            for (system, user), ok in zip(rendered, reused_flags)
            if not ok
        )
        lines.append(
            _seat_line(
                config,
                seat,
                "proposer",
                token_model,
                calls_total=float(n_items),
                calls_reused=float(sum(reused_flags)),
                paid_chars=paid_chars,
                rendered_chars=paid_rendered,
                basis="exact",
                basis_reason=(
                    "one proposer call per item, unconditional; every prompt "
                    "rendered here and its prompt_hash looked up in the store"
                ),
                reuse_basis="prompt_hash",
                misses=misses,
            )
        )
        reuse_by_seat[seat.seat_name] = {
            "role": "proposer",
            "canonical_model": seat.canonical_model,
            "endpoint_kind": seat.endpoint_kind,
            "calls": n_items,
            "reused": sum(reused_flags),
            "paid": n_items - sum(reused_flags),
            "basis": "prompt_hash",
            "misses": misses,
        }

    # -- critic: exact volume, derived reuse ------------------------------
    critic = roster.critic
    critic_answered = _answered_models(
        engine, content_keys, "critic", plan.query_name, plan.query_version
    )
    critic_reused_flags: List[bool] = []
    critic_misses = {kind: 0 for kind in MISS_KINDS}
    for index in range(n_items):
        answered = critic_answered.get(content_keys[index])
        reusable = bool(all_proposers_reused[index]) and bool(
            answered and critic.canonical_model in answered
        )
        critic_reused_flags.append(reusable)
        if not reusable:
            if all_proposers_reused[index]:
                critic_misses[_attribute(answered, critic.canonical_model)] += 1
            else:
                # The critic prompt embeds the proposers' ballots, so a
                # proposer that has to run makes the critic's question new
                # whatever the critic seat is doing.  Attributed to the same
                # reason the upstream missed would double-count a bench or
                # transport change; "new_item" is the honest label for a
                # question whose text does not yet exist.
                critic_misses["new_item"] += 1
    critic_paid_chars = sum(
        chars for chars, ok in zip(prompt_chars, critic_reused_flags) if not ok
    )
    lines.append(
        _seat_line(
            config,
            critic,
            "critic",
            token_model,
            calls_total=float(n_items),
            calls_reused=float(sum(critic_reused_flags)),
            paid_chars=critic_paid_chars,
            rendered_chars=None,
            basis="exact",
            basis_reason=(
                "ONE critic call per item, UNCONDITIONAL: engine/decide.py "
                "step 5 returns need_critic with no guard, so a critic call "
                "carries no information about proposer disagreement"
            ),
            reuse_basis="upstream_derived",
            misses=critic_misses,
            note=(
                "Reuse is DERIVED, not hashed. The critic prompt embeds the "
                "proposers' ballots, which do not exist before the run, so it "
                "cannot be rendered here. A critic call is counted reusable "
                "only when every proposer for that item hit reuse (so the "
                "ballots will be byte-identical) AND an answered critic call "
                "exists for the same item content, query version and "
                "canonical model. That is a LOWER bound on critic reuse: the "
                "remaining prompt-hash axes (reuse namespace, effective "
                "params) are assumed unchanged."
            ),
        )
    )
    reuse_by_seat[critic.seat_name] = {
        "role": "critic",
        "canonical_model": critic.canonical_model,
        "endpoint_kind": critic.endpoint_kind,
        "calls": n_items,
        "reused": sum(critic_reused_flags),
        "paid": n_items - sum(critic_reused_flags),
        "basis": "upstream_derived",
        "misses": critic_misses,
    }

    # -- meta: projected volume -------------------------------------------
    rate = float(meta_fire_rate)
    if not 0.0 <= rate <= 1.0:
        raise ValueError(
            f"meta_fire_rate must be a fraction in [0, 1], got {meta_fire_rate!r}"
        )
    meta = roster.meta
    fully_reused = sum(
        1
        for index in range(n_items)
        if all_proposers_reused[index] and critic_reused_flags[index]
    )
    meta_total = n_items * rate
    # A fully reused item's meta call, if it fires, reuses too: same
    # ballots, same critic, same prompt.  So the PAID meta volume is driven
    # by the items that are actually going to run.
    meta_reused = fully_reused * rate
    meta_paid_items = n_items - fully_reused
    mean_chars = (sum(prompt_chars) / n_items) if n_items else 0.0
    lines.append(
        _seat_line(
            config,
            meta,
            "meta",
            token_model,
            calls_total=meta_total,
            calls_reused=meta_reused,
            paid_chars=mean_chars * meta_paid_items * rate,
            rendered_chars=None,
            basis="projected",
            basis_reason=(
                f"meta fires on {rate:.1%} of items (measured, n="
                f"{meta_fire_rate_n}); which items is not knowable before the "
                "proposers and the critic have answered"
            ),
            reuse_basis="projected",
            misses=None,
            upper_calls=n_items * _upper_rate(rate, meta_fire_rate_n)
            - meta_reused,
        )
    )

    # -- paid repair: projected -------------------------------------------
    paid_point = sum(line.total_tokens_point for line in lines)
    paid_input = sum(line.input_tokens_point for line in lines)
    paid_output = sum(line.output_tokens_point for line in lines)
    paid_calls = sum(line.calls_paid for line in lines)
    repair_input = paid_input * PAID_REPAIR_FRACTION
    repair_output = paid_output * PAID_REPAIR_FRACTION
    lines.append(
        QuoteLine(
            line="paid_repair",
            role="repair",
            basis="projected",
            basis_reason=(
                "the retry pass over silently truncated replies. "
                "invalid_output is NOT in RETRYABLE_KINDS (only transport and "
                "timeout are) and ARGO truncates with no length finish-reason, "
                "so the worker never retries them: 113 of 19,268 items failed "
                "that way and 105 came back on a plain retry, for 0.30% of run "
                "cost. A first-class stage, so a first-class line."
            ),
            calls_total=paid_calls * PAID_REPAIR_FRACTION,
            calls_reused=0.0,
            calls_paid=paid_calls * PAID_REPAIR_FRACTION,
            input_tokens_point=repair_input,
            output_tokens_point=repair_output,
            output_tokens_upper=paid_output * PAID_REPAIR_FRACTION_UPPER,
            gateway=None,
            note=(
                "Not attributed to a gateway: which seat truncates is not "
                "knowable in advance, so these tokens are deliberately absent "
                "from the per-gateway dollar figures and present in the "
                "total. The upper band is the measured 0.59% ERROR rate, "
                "which is a true ceiling on what a retry pass can re-ask."
            ),
        )
    )
    notes.append(
        f"paid_repair is {PAID_REPAIR_FRACTION:.2%} of the paid tokens of "
        f"every other line ({round(paid_point):,} tokens at the point estimate)."
    )

    for line in lines:
        if line.seat_name:
            paid_by_seat[line.seat_name] = line.calls_paid

    # -- wall clock, gateways, balances -----------------------------------
    service_times = fit_service_times(store, seats=roster)
    queue_table = _queue_table(config, roster)
    wall_clock = _wall_clock(queue_table, paid_by_seat, service_times, seat_roles)
    wall_clock["service_times"] = {
        f"{name}/{role}": dict(value) for (name, role), value in service_times.items()
    }
    if not any(queue.get("declared") for queue in queue_table.values()):
        warnings.append(
            "no `queues` are declared, so the wall clock rests on each "
            "endpoint's max_concurrency. MAG's limits are ACCOUNT-WIDE (20 "
            "parallel AND 120 requests/minute): a config asking 24 parallel "
            "errored 427 of 500 pilot items over 1,925 transport attempts. "
            "The working configuration is MAG queues summing to 6."
        )

    gateways, gateway_warnings = _price_gateways(config, lines)
    warnings.extend(gateway_warnings)

    return Quote(
        plan_hash=plan.plan_hash,
        plan={
            "source": plan.source,
            "run_dir": plan.run_dir,
            "query": plan.query_ref,
            "mapping_hash": plan.mapping_hash,
            "profile": plan.profile,
            "n_items": n_items,
            "submit_count": n_items,
            "items_with_query_id": n_items - missing_query_id,
            "rendered_prompt_chars": rendered_chars,
        },
        # THE PARTITION KEY, and it is model-only on purpose.
        parameter_set={
            "query": plan.query_ref,
            "bench_fingerprint": bench_fingerprint(roster),
            "mapping_hash": plan.mapping_hash,
        },
        provenance={
            "roster_fingerprint": roster.fingerprint(),
            "route_fingerprints": {
                seat.seat_name: route_fingerprint(
                    seat, _endpoint_cfg(config, seat), params_of(seat)
                )
                for _role, seat in roster.seats()
            },
            "renderer_id_version": rid_version,
            "note": (
                "roster_fingerprint and route_fingerprint are RECORDED, not "
                "keys. Both cover the transport, so partitioning on either "
                "would re-charge the whole corpus for a routing change."
            ),
        },
        lines=tuple(lines),
        reuse={
            "by_seat": reuse_by_seat,
            "misses": {
                kind: sum(
                    int(entry["misses"].get(kind, 0)) for entry in reuse_by_seat.values()
                )
                for kind in MISS_KINDS
            },
            "items_fully_reused": fully_reused,
            "miss_kinds": {
                "new_item": "this prompt was never asked",
                "bench_change": (
                    "this seat's canonical_model moved -- a different "
                    "experiment, and a legitimate re-charge"
                ),
                "transport_change": (
                    "same model, different route. prompt_hash keys on "
                    "endpoint_kind, so the stored answer is unreusable even "
                    "though the bench is unchanged and bench_fingerprint has "
                    "NOT moved. Without this split a pure cost optimisation "
                    "reads as a bug."
                ),
            },
        },
        wall_clock=wall_clock,
        gateways=tuple(gateways),
        balance_delta=_balance_delta_block(gateways),
        token_model=token_model.to_dict(),
        meta_fire_rate=rate,
        meta_fire_rate_n=int(meta_fire_rate_n),
        generated_at=datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        notes=tuple(notes),
        warnings=tuple(warnings),
    )


def _reuse_namespace(config: Any, seat: Any) -> str:
    """The seat's reuse namespace, computed WITHOUT instantiating a backend.

    Same inputs the worker uses (``CouncilWorker._reuse_namespace``): the
    endpoint config with its kind filled in, and the SERVED model.
    """
    return default_reuse_namespace(_endpoint_cfg(config, seat), seat.model)


def _effective_params(seat: Any) -> Dict[str, Any]:
    """The seat's generation params with NO api type consulted.

    The default resolver, and a deliberately POOR one for reuse counting --
    see :data:`_EFFECTIVE_PARAMS_WARNING` and pass ``effective_params`` to
    :func:`quote_plan` instead.

    :func:`~.types.seat_generation_params` returns the provider-independent
    spelling (``max_output_tokens``, ``temperature``).  An api type returns
    the WIRE spelling it would really send (``max_tokens``), and ARGO
    additionally forces ``temperature: 1`` on some models whatever was asked
    for.  Those are different dicts, so they are different prompt hashes,
    so a quote computed from this function alone finds NONE of the answers
    an HTTP seat has already paid for.

    This module still defaults to it, and does not reach for an api type
    itself, because the quote's whole guarantee is that it cannot spend:
    nothing here may import or construct a backend.  The accurate resolver
    is injected from above by
    :meth:`~kbutillib.domains.ai.council_utils.CouncilUtils.quote_plan`,
    which already owns api construction, and the shortfall is reported as a
    WARNING on the quote rather than silently miscounted.
    """
    return seat_generation_params(seat)


#: Warning emitted when the quote had to fall back to the provider-
#: independent parameter spelling for an HTTP seat.
_EFFECTIVE_PARAMS_WARNING = (
    "no effective_params resolver was supplied, so prompt hashes for HTTP "
    "seats were computed from the provider-independent parameter spelling "
    "(max_output_tokens) rather than the one an api type would really send "
    "(max_tokens, and ARGO forces temperature: 1 on some models). Those are "
    "different hashes, so REUSE IS UNDERCOUNTED for seats {seats} and this "
    "quote is an OVER-estimate. Quote through `kbu council quote` / "
    "CouncilUtils.quote_plan, which supplies the resolver."
)


def _registered_query(store: Any, plan: Plan) -> Any:
    """The plan's query type, out of the store's registry.

    Imported inside the function: :mod:`.queries.registry` is pure, but the
    quote's import graph is a tested property and this keeps the module's
    module-level imports to the two it genuinely needs.
    """
    from .queries import QueryRegistry

    return QueryRegistry(store).get(plan.query_name, plan.query_version)


def _check_query_matches_plan(query: Any, plan: Plan) -> None:
    """Refuse a query type that is not the one the plan named.

    A quote priced against one spec and run against another is worse than no
    quote: the prompts differ, so every reuse count in it is wrong and the
    approval binds to a number that was never going to happen.
    """
    name = str(getattr(query, "name", "") or "")
    version = str(getattr(query, "version", "") or "")
    if (name, version) != (plan.query_name, plan.query_version):
        raise ValueError(
            f"the plan names query {plan.query_ref!r} but the query handed to "
            f"quote_plan is {name}@{version}. Quoting one spec and running "
            "another would make every reuse count in the quote wrong."
        )


def _seat_line(
    config: Any,
    seat: Any,
    role: str,
    token_model: TokenModel,
    *,
    calls_total: float,
    calls_reused: float,
    paid_chars: float,
    rendered_chars: Optional[int],
    basis: str,
    basis_reason: str,
    reuse_basis: str,
    misses: Optional[Mapping[str, int]],
    note: str = "",
    upper_calls: Optional[float] = None,
) -> QuoteLine:
    """One seat's line of the call graph, priced through the token model.

    Input tokens come from the fit applied to :func:`item_prompt_chars` over
    the PAID calls only -- a reused call costs zero tokens, which is the
    whole reason the reuse count is worth computing exactly.

    *upper_calls* overrides the call count used for the OUTPUT upper band.
    Only meta passes it: its volume is itself uncertain, so its band has to
    cover both a longer answer and the fire rate's own sampling error, while
    an exact line's band covers the answer alone.
    """
    fit = token_model.fit_for(seat.seat_name, role)
    calls_paid = max(calls_total - calls_reused, 0.0)
    upper_paid = calls_paid if upper_calls is None else max(upper_calls, 0.0)
    return QuoteLine(
        line=seat.seat_name,
        role=role,
        basis=basis,
        basis_reason=basis_reason,
        seat_name=seat.seat_name,
        gateway=_gateway_of(config, seat),
        endpoint=seat.endpoint,
        endpoint_kind=seat.endpoint_kind,
        canonical_model=seat.canonical_model,
        calls_total=calls_total,
        calls_reused=calls_reused,
        calls_paid=calls_paid,
        reuse_basis=reuse_basis,
        misses=dict(misses) if misses is not None else None,
        rendered_prompt_chars=rendered_chars,
        prompt_chars=round(paid_chars),
        input_tokens_point=fit.input_tokens_per_char * float(paid_chars),
        output_tokens_point=fit.output_tokens_mean * calls_paid,
        output_tokens_upper=fit.output_tokens_upper * upper_paid,
        token_model_basis=fit.basis,
        token_model_n=fit.n_attempts,
        note=note,
    )
