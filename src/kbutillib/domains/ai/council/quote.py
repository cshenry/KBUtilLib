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

import math
from dataclasses import dataclass
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
)

import sqlalchemy as sa

from .store import schema as sch
from .types import canonical_json

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
