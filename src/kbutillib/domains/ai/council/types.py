"""Core data model for the domain-neutral LLM council.

A *council* asks N>=2 independent PROPOSER models to pick ids from a closed
candidate MENU for one item, has a CRITIC audit the resulting vote table, and
only invokes a META adjudicator when the proposers disagree or the critic
objects.  This module holds the vocabulary every other council module speaks:
the menu/task/roster/request/result records, the typed failure taxonomy, and
the content-addressing hash helpers that make answers reusable across users.

Nothing here performs I/O, imports a backend, or touches the store -- it is
pure data plus hashing, so it is safe to import from anywhere.

Annotation note: pydantic *evaluates* field annotations at class-creation
time, and this repository still builds on Python 3.9 (see the ci.yml matrix),
where ``str | None`` is a TypeError outside of a never-evaluated string
annotation.  Model fields therefore use ``Optional[...]`` / ``Union[...]``
rather than PEP 604 unions; plain functions are free to use either.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import (
    Any,
    ClassVar,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from pydantic import BaseModel, model_validator

from ..llm.types import (
    AuthError,
    BillingGuardRefused,
    CallError,
    CallTimeout,
    InvalidOutput,
    LLMError,
    LLMException,
    LLMResult,
    ModelUnavailable,
    QuotaExhausted,
    TransportError,
    as_llm_error,
)

#: The serialisable failure record and the invocation RESULT now live in the
#: domain-neutral wrapper (M2.1, ``kbutillib.domains.ai.llm``), because every
#: LLM call in KBUtilLib goes through it and not all of them are councils.
#: These aliases are not deprecation shims to be deleted later: they are the
#: council's own names for the same objects, and ``isinstance`` /
#: ``except`` relations are identical either way because they ARE the same
#: classes.
CouncilError = LLMError
CouncilException = LLMException
CompletionResult = LLMResult
as_council_error = as_llm_error

__all__ = [
    # Failure taxonomy
    "ERROR_KINDS",
    "CouncilError",
    "CouncilException",
    "ConfigError",
    "RosterError",
    "CallError",
    "TransportError",
    "CallTimeout",
    "QuotaExhausted",
    "AuthError",
    "BillingGuardRefused",
    "ModelUnavailable",
    "InvalidOutput",
    "BudgetExhausted",
    "BudgetUsageUnknown",
    "as_council_error",
    # Records
    "MenuEntry",
    "CouncilTask",
    "SeatSpec",
    "Roster",
    "CompletionRequest",
    "CompletionResult",
    "Pick",
    "Ballot",
    "CouncilVerdict",
    # Hashing / identity
    "canonical_json",
    "canonical_sha256",
    "entity_hash",
    "data_hash",
    "prompt_hash",
    "route_fingerprint",
    "default_reuse_namespace",
    "seat_generation_params",
    "ENDPOINT_KINDS",
    "CLI_KINDS",
    "HTTP_KINDS",
]

# Endpoint kinds a seat may be served from.  A self-hosted model is just an
# ``openai`` endpoint the user declared (review round 2 took model management
# out of the product); ``fake`` and ``replay`` exist for the test backends.
ENDPOINT_KINDS = (
    "argo",
    "openai",
    "claude_cli",
    "codex_cli",
    "fake",
    "replay",
)
HTTP_KINDS = ("argo", "openai")
CLI_KINDS = ("claude_cli", "codex_cli")

#: Every ``CouncilError.kind`` string the council may emit.  ``off_menu`` is a
#: non-fatal note (a proposer picked an id that is not on the menu); it is
#: recorded against the item but never fails a call.
ERROR_KINDS = (
    "transport",
    "timeout",
    "quota_exhausted",
    "auth",
    "billing_guard",
    "model_unavailable",
    "invalid_output",
    "budget_exhausted",
    "budget_usage_unknown",
    "off_menu",
)


# ---------------------------------------------------------------------------
# Canonical JSON + hashing
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def _jsonable(value: Any) -> Any:
    """Return *value* reduced to JSON-native types, recursively.

    Pydantic models become plain dicts so a hash is identical whether the
    caller passed a model or the dict it was built from.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_json(payload: Any) -> str:
    """Serialise *payload* to the council's canonical JSON form.

    ``sort_keys=True`` with no whitespace, so two dicts that differ only in
    key insertion order produce byte-identical output -- the property every
    hash in this module depends on.
    """
    return json.dumps(
        _jsonable(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def canonical_sha256(payload: Any) -> str:
    """SHA-256 hex digest of :func:`canonical_json` of *payload*."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def entity_hash(entity: str) -> str:
    """Hash of the item's entity text, whitespace-collapsed and stripped.

    Two spellings of the same entity that differ only in runs of whitespace
    are the same question, so they must reuse each other's answers.
    """
    normalised = _WHITESPACE_RE.sub(" ", str(entity)).strip()
    return canonical_sha256(normalised)


def data_hash(
    evidence_without_entity: Mapping[str, Any],
    menu: Sequence[Union["MenuEntry", Mapping[str, Any]]],
) -> str:
    """Hash of everything about the question except the entity itself.

    The entity is hashed separately (:func:`entity_hash`) so a store can index
    by entity and still distinguish two different menus for it.  Menu ORDER is
    significant and preserved: the menu is a ranked list, and reordering it is
    a different question.
    """
    return canonical_sha256(
        {
            "evidence": _jsonable(dict(evidence_without_entity or {})),
            "menu": [_jsonable(entry) for entry in (menu or [])],
        }
    )


def default_reuse_namespace(
    endpoint_config: Mapping[str, Any], served_model: str
) -> str:
    """Return the default reuse namespace for an endpoint.

    The namespace is the "same deployment" key inside :func:`prompt_hash`: two
    users asking the same question of the same deployment land on the same
    prompt hash and can reuse each other's answers, even though their endpoint
    *aliases* differ.

    * HTTP endpoints -> normalised ``base_url`` (lowercase host, no trailing
      slash) + ``"#"`` + ``deployment_revision`` (default ``""``).  The
      revision lets an operator invalidate reuse after a model swap behind an
      unchanged URL.
    * CLI endpoints -> ``"<kind>:<served model>"``, because a CLI has no URL
      and the subscription it bills against is not part of the question.

    An endpoint may set ``reuse_namespace`` explicitly to share answers with
    another endpoint; this function only supplies the default.
    """
    explicit = (endpoint_config or {}).get("reuse_namespace")
    if explicit:
        return str(explicit)

    kind = str((endpoint_config or {}).get("kind") or "")
    if kind in CLI_KINDS:
        return f"{kind}:{served_model}"

    base_url = str((endpoint_config or {}).get("base_url") or "")
    revision = str((endpoint_config or {}).get("deployment_revision") or "")
    return f"{_normalise_base_url(base_url)}#{revision}"


def _normalise_base_url(base_url: str) -> str:
    """Lowercase the scheme+host of *base_url* and drop any trailing slash."""
    url = (base_url or "").strip().rstrip("/")
    if "://" not in url:
        return url.lower()
    scheme, _, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}"


def seat_generation_params(seat: "SeatSpec") -> Dict[str, Any]:
    """Generation parameters a seat would send, independent of any backend.

    Used as the default ``effective_params`` for :func:`route_fingerprint` so
    the fingerprint can be computed without instantiating a backend.
    """
    params: Dict[str, Any] = {
        "temperature": seat.temperature,
        "max_output_tokens": seat.max_output_tokens,
    }
    params.update(dict(seat.extra or {}))
    return params


def prompt_hash(
    seat: "SeatSpec",
    reuse_namespace: str,
    effective_params: Mapping[str, Any],
    json_schema: Mapping[str, Any],
    query_name: str,
    query_version: str,
    renderer_id_version: str,
    role: str,
    system: str,
    user: str,
) -> str:
    """Content address of one model invocation, for cross-user answer reuse.

    Deliberately keyed on ``canonical_model`` and ``reuse_namespace`` and NOT
    on the endpoint alias: "gemini 3.5 flash via ARGO" is the same question
    whether the asking user calls their endpoint ``argo`` or ``anl-argo``, and
    reuse must cross that naming difference.  ``endpoint_kind`` IS included,
    because the same canonical model reached over a CLI and over HTTP is a
    different deployment with different defaults.
    """
    return canonical_sha256(
        {
            "canonical_model": seat.canonical_model,
            "endpoint_kind": seat.endpoint_kind,
            "reuse_namespace": reuse_namespace,
            "effective_params": _jsonable(dict(effective_params or {})),
            "json_schema": _jsonable(dict(json_schema or {})),
            "query_name": query_name,
            "query_version": query_version,
            "renderer_id_version": renderer_id_version,
            "role": role,
            "system": system,
            "user": user,
        }
    )


def route_fingerprint(
    seat: "SeatSpec",
    endpoint_config: Mapping[str, Any],
    effective_params: Optional[Mapping[str, Any]] = None,
) -> str:
    """Non-secret fingerprint of HOW a seat is reached.

    Stored per seat on jobs and on every call row, so a result can later be
    attributed to a concrete route (which endpoint, which account, which
    generation params) without ever persisting a credential.  Unlike
    :func:`prompt_hash` this DOES include the endpoint alias and account: it
    answers "where did this come from", not "is this the same question".

    *effective_params* defaults to :func:`seat_generation_params`, so the
    two-argument form in the design prompt works unchanged.

    An endpoint's ``options`` (a plug-in api type's own non-secret fields)
    are included when present; see the comment in the body for why only then.
    """
    cfg = dict(endpoint_config or {})
    if effective_params is None:
        effective_params = seat_generation_params(seat)
    payload: Dict[str, Any] = {
        "endpoint": seat.endpoint,
        "kind": seat.endpoint_kind,
        "target": cfg.get("base_url") or cfg.get("executable") or "",
        "model": seat.model,
        "account": cfg.get("account") or "",
        "effective_params": _jsonable(dict(effective_params)),
    }
    # A plug-in api type's own non-secret ``options`` change where and how a
    # call lands, so they belong in "where did this come from" (review round
    # 3).  Added only when there ARE options, so every endpoint configured
    # before M2.1 keeps the fingerprint already stored on its call rows --
    # an unconditional key would change every one of them and make every
    # queued call look like a configuration mismatch.
    options = cfg.get("options")
    if options:
        payload["options"] = _jsonable(dict(options))
    return canonical_sha256(payload)


# ---------------------------------------------------------------------------
# Typed failures
# ---------------------------------------------------------------------------


class ConfigError(CouncilException):
    """The council configuration is unusable (and no default can be guessed).

    Raised at load time -- a bad config must fail before any token is spent.
    """


class RosterError(CouncilException):
    """The requested seating is not a valid council.

    Raised at :class:`Roster` construction, i.e. before a job is created, so
    an invalid council never reaches the queue.
    """


# ``CallError`` and the seven provider-call failures it heads
# (``TransportError``, ``CallTimeout``, ``QuotaExhausted``, ``AuthError``,
# ``BillingGuardRefused``, ``ModelUnavailable``, ``InvalidOutput``) are
# imported from ``..llm.types`` at the top of this module: a provider failure
# is not a council concept, and the wrapper every call now goes through has to
# be able to report one without importing the council.  The two BUDGET
# failures below stay here, because a token budget is the council's own
# accounting and no API type knows about one.


class BudgetExhausted(CallError):
    """The endpoint's token budget is spent; halt the endpoint."""

    kind: ClassVar[str] = "budget_exhausted"
    halts_endpoint: ClassVar[bool] = True


class BudgetUsageUnknown(CallError):
    """A budget is configured but the endpoint reports no usage.

    Halts the endpoint rather than guessing: an unmeasurable budget is an
    unenforceable one, and silently spending past it is the worse failure.
    """

    kind: ClassVar[str] = "budget_usage_unknown"
    halts_endpoint: ClassVar[bool] = True


# ---------------------------------------------------------------------------
# Question records
# ---------------------------------------------------------------------------


class MenuEntry(BaseModel):
    """One candidate on the closed menu offered for an item."""

    id: str
    label: str
    description: Optional[str] = None
    rank: Optional[int] = None
    score: Optional[float] = None
    metadata: Dict[str, Any] = {}


class CouncilTask(BaseModel):
    """One item to decide: its evidence and the closed menu it may pick from.

    ``evidence`` is query-defined -- the GO query type uses
    ``{"text", "source"}`` -- so this model stays domain-neutral.
    """

    item_id: str
    evidence: Dict[str, Any] = {}
    menu: List[MenuEntry] = []


class SeatSpec(BaseModel):
    """A fully resolved seat: which model, reached how, with what limits.

    Resolved by :meth:`CouncilConfig.roster` from the endpoint/model/profile
    tables, and thereafter self-contained -- a worker on another machine can
    run a seat without re-reading the submitting user's config.
    """

    seat_name: str
    endpoint: str
    endpoint_kind: Literal[
        "argo", "openai", "claude_cli", "codex_cli", "fake", "replay"
    ]
    model: str
    canonical_model: str
    max_output_tokens: int = 4000
    temperature: Optional[float] = 0
    timeout_s: float = 180
    extra: Dict[str, Any] = {}
    verified: bool = True


class Roster(BaseModel):
    """The seating for one council: proposers, critic and meta.

    Invariants are enforced at construction (raising :class:`RosterError`, not
    a pydantic ``ValidationError``) because an invalid council is a design
    mistake to surface at submit time, not a runtime failure to discover after
    the tokens are spent.
    """

    proposers: List[SeatSpec]
    critic: SeatSpec
    meta: SeatSpec
    allow_meta_overlap: bool = False

    @model_validator(mode="after")
    def _check_independence(self) -> "Roster":
        """Reject seatings that are not independent votes.

        Independence is the whole premise of the protocol: two seats on the
        same canonical model are one opinion counted twice, however different
        their endpoints look.
        """
        if len(self.proposers) < 2:
            raise RosterError(
                f"a council needs at least 2 proposers, got {len(self.proposers)}"
            )

        seen: Dict[str, str] = {}
        for seat in self.proposers:
            if seat.canonical_model in seen:
                raise RosterError(
                    f"proposers {seen[seat.canonical_model]!r} and {seat.seat_name!r} "
                    f"are both {seat.canonical_model!r}; proposers must be independent models"
                )
            seen[seat.canonical_model] = seat.seat_name

        if self.critic.canonical_model in seen:
            raise RosterError(
                f"critic is {self.critic.canonical_model!r}, which is already seated as "
                f"proposer {seen[self.critic.canonical_model]!r}; a model cannot audit its own vote"
            )

        if not self.allow_meta_overlap:
            if self.meta.canonical_model in seen:
                raise RosterError(
                    f"meta is {self.meta.canonical_model!r}, which is already seated as "
                    f"proposer {seen[self.meta.canonical_model]!r}; "
                    "set allow_meta_overlap to permit this"
                )
            if self.meta.canonical_model == self.critic.canonical_model:
                raise RosterError(
                    f"meta and critic are both {self.meta.canonical_model!r}; "
                    "set allow_meta_overlap to permit this"
                )
        return self

    def seats(self) -> List[tuple]:
        """Return ``(role, seat)`` pairs in protocol order."""
        pairs: List[tuple] = [("proposer", seat) for seat in self.proposers]
        pairs.append(("critic", self.critic))
        pairs.append(("meta", self.meta))
        return pairs

    def fingerprint(self) -> str:
        """Stable identity of this seating, independent of endpoint naming.

        Covers each seat's role, endpoint kind, served and canonical model,
        token ceiling, temperature and extra params -- everything that can
        change an answer -- but not the endpoint alias, so the same council
        configured by two users fingerprints identically.
        """
        return canonical_sha256(
            [
                {
                    "role": role,
                    "endpoint_kind": seat.endpoint_kind,
                    "model": seat.model,
                    "canonical_model": seat.canonical_model,
                    "max_output_tokens": seat.max_output_tokens,
                    "temperature": seat.temperature,
                    "extra": _jsonable(dict(seat.extra or {})),
                }
                for role, seat in self.seats()
            ]
        )


# ---------------------------------------------------------------------------
# Invocation records
# ---------------------------------------------------------------------------


class CompletionRequest(BaseModel):
    """One rendered model invocation, ready for a backend."""

    seat: SeatSpec
    role: Literal["proposer", "critic", "meta"]
    system: str
    user: str
    json_schema: Dict[str, Any] = {}
    trace_id: str = ""
    deadline_s: float = 180


# ---------------------------------------------------------------------------
# Answer records
# ---------------------------------------------------------------------------


class Pick(BaseModel):
    """One menu id a seat voted for, with its stated confidence."""

    id: str
    confidence: Literal["high", "medium", "low"] = "medium"
    why: str = ""


class Ballot(BaseModel):
    """One seat's vote on one item, or the failure that produced no vote."""

    seat: str
    status: Literal["ok", "error"] = "ok"
    picks: List[Pick] = []
    overall_confidence: Optional[str] = None
    no_functional_content: bool = False
    notes: Optional[str] = None
    error: Optional[CouncilError] = None


class CouncilVerdict(BaseModel):
    """The council's decision for one item, with its full audit trail.

    Everything needed to explain or re-derive the decision travels with it:
    each ballot, the vote table, the critic and meta payloads, per-seat token
    usage, the query identity, the roster fingerprint, and any answer
    repairs :mod:`..council.engine.normalise` had to make for a reply to
    satisfy its role's schema.
    """

    item_id: str
    status: Literal["ok", "error"] = "ok"
    selected_ids: List[str] = []
    decline_reason: Optional[
        Literal["contentless", "no_candidate_matched", "no_candidates"]
    ] = None
    final_confidence: Optional[str] = None
    decided_by: Optional[Literal["unanimous", "meta", "deterministic_empty"]] = None
    ballots: List[Ballot] = []
    votes: Dict[str, int] = {}
    n_valid: int = 0
    critic: Optional[Dict[str, Any]] = None
    meta: Optional[Dict[str, Any]] = None
    meta_skipped: bool = False
    errors: List[Dict[str, Any]] = []
    usage: Dict[str, Dict[str, Any]] = {}
    #: Did any seat's reply have to be repaired before the answer schema
    #: accepted it?  False on a council where every model answered in the
    #: shape its query type asked for, which is the normal case.
    normalised: bool = False
    #: The repairs, per seat: ``{seat_name: [{"field", "rule", "original",
    #: "value"}, ...]}``.  Only repaired seats appear.  The raw reply text in
    #: the store is untouched, so this is the whole record of the difference
    #: between what the model said and what the council read.
    repairs: Dict[str, List[Dict[str, Any]]] = {}
    query: Dict[str, str] = {}
    roster_fingerprint: str = ""
    decided_at: str = ""
