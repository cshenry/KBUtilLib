"""The request/result vocabulary every LLM call in KBUtilLib speaks.

One wrapper, one request record, one result record, one failure taxonomy.
:class:`LLMRequest` is deliberately flat and provider-neutral: a model name,
two prompts, an optional answer schema, the generation limits, a bag of
provider extras, a timeout and a trace id.  Nothing in it knows about
councils, seats, roles or query types -- which is what lets a second caller
(a curation pass, an agent, a notebook) use the same wrapper without
importing the council.

:class:`LLMResult` and the typed failures were MOVED here from
``kbutillib.domains.ai.council.types`` with their fields unchanged;
``council.types`` keeps ``CompletionResult = LLMResult`` and an alias per
error class, so every existing import still resolves.  The hard rule travels
with them: an API type NEVER raises for a call failure, it returns
``ok=False`` with ``error`` set, because a caller running N models per item
cannot have one bad model abort the item.

Annotation note: pydantic *evaluates* field annotations at class-creation
time and this repository still builds on Python 3.9 (see the ci.yml matrix),
where ``str | None`` is a TypeError outside a never-evaluated string
annotation.  Model fields therefore use ``Optional[...]`` rather than PEP 604
unions; plain functions are free to use either.
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, Optional

from pydantic import BaseModel

from ....core.errors import KBUtilLibError

__all__ = [
    "LLM_ERROR_KINDS",
    "AuthError",
    "BillingGuardRefused",
    "CallError",
    "CallTimeout",
    "InvalidOutput",
    "LLMError",
    "LLMException",
    "LLMRequest",
    "LLMResult",
    "ModelUnavailable",
    "QuotaExhausted",
    "TransportError",
    "as_llm_error",
    "as_llm_request",
    "generation_params",
]

#: Every ``LLMError.kind`` an API type may report.  A caller may define more
#: kinds of its own (the council adds ``budget_exhausted``,
#: ``budget_usage_unknown`` and ``off_menu``), but these seven are the ones a
#: provider call itself can produce, and they are what the wrapper promises.
LLM_ERROR_KINDS = (
    "transport",
    "timeout",
    "quota_exhausted",
    "auth",
    "billing_guard",
    "model_unavailable",
    "invalid_output",
)


# ---------------------------------------------------------------------------
# Typed failures
# ---------------------------------------------------------------------------


class LLMError(BaseModel):
    """Serialisable form of one model call's failure.

    A record rather than an exception because it is persisted (the council
    writes it onto a call row) and has to survive a round trip through a
    database.  Aliased as ``council.types.CouncilError``.
    """

    kind: str
    message: str = ""
    retryable: bool = False
    halts_endpoint: bool = False


class LLMException(KBUtilLibError):
    """Base class for every exception raised by the LLM layer.

    Aliased as ``council.types.CouncilException``, which is what the council's
    own ``ConfigError`` / ``RosterError`` / store and query errors derive
    from, so ``except CouncilException`` still catches a :class:`CallError`
    exactly as it did before the move.
    """


class CallError(LLMException):
    """Base class for the typed failures of a single model invocation.

    Subclasses carry the three facts a caller needs to route a failure: its
    ``kind`` (what to persist), ``retryable`` (try this call again) and
    ``halts_endpoint`` (stop using this endpoint for every call, not just
    this one).
    """

    kind: ClassVar[str] = "invalid_output"
    retryable: ClassVar[bool] = False
    halts_endpoint: ClassVar[bool] = False

    def to_error(self) -> LLMError:
        """Return the serialisable :class:`LLMError` for this exception."""
        return LLMError(
            kind=self.kind,
            message=str(self),
            retryable=self.retryable,
            halts_endpoint=self.halts_endpoint,
        )


class TransportError(CallError):
    """Network or protocol failure reaching the endpoint; retry the call."""

    kind: ClassVar[str] = "transport"
    retryable: ClassVar[bool] = True


class CallTimeout(CallError):
    """The call exceeded its deadline; retry the call."""

    kind: ClassVar[str] = "timeout"
    retryable: ClassVar[bool] = True


class QuotaExhausted(CallError):
    """The provider refused for quota/rate reasons; halt the endpoint."""

    kind: ClassVar[str] = "quota_exhausted"
    halts_endpoint: ClassVar[bool] = True


class AuthError(CallError):
    """Credentials are missing, expired or rejected; halt the endpoint."""

    kind: ClassVar[str] = "auth"
    halts_endpoint: ClassVar[bool] = True


class BillingGuardRefused(CallError):
    """The billing guard refused the call (wrong billing mode or account).

    Halts the endpoint: a guard refusal is a configuration fact, so every
    other call to the same endpoint would be refused identically.
    """

    kind: ClassVar[str] = "billing_guard"
    halts_endpoint: ClassVar[bool] = True


class ModelUnavailable(CallError):
    """The model is not loaded/serving yet; the call WAITS rather than fails.

    ``retryable`` is true but this is not a transport retry: a caller
    re-queues the call for a later sweep instead of burning an attempt.
    """

    kind: ClassVar[str] = "model_unavailable"
    retryable: ClassVar[bool] = True


class InvalidOutput(CallError):
    """The model answered, but not in the shape the schema requires."""

    kind: ClassVar[str] = "invalid_output"


def as_llm_error(exc: BaseException) -> LLMError:
    """Coerce any exception into an :class:`LLMError`.

    Known :class:`CallError` subclasses keep their taxonomy; anything else is
    reported as a retryable transport failure, which is the safe reading of an
    unexpected exception out of a provider client.
    """
    if isinstance(exc, CallError):
        return exc.to_error()
    if isinstance(exc, LLMError):  # pragma: no cover - defensive
        return exc
    return LLMError(
        kind="transport",
        message=f"{type(exc).__name__}: {exc}",
        retryable=True,
        halts_endpoint=False,
    )


# ---------------------------------------------------------------------------
# The request and the result
# ---------------------------------------------------------------------------


class LLMRequest(BaseModel):
    """One rendered model invocation, ready for any API type.

    Args:
        model: the name the PROVIDER knows the model by (ARGO's
            ``gemini35flash``, a vLLM port's ``qwen3-32b``, the Claude CLI's
            ``sonnet``) -- not a canonical identity and not a config alias.
        system: the system prompt.
        user: the user prompt.
        json_schema: the JSON Schema the answer must satisfy, or ``None`` for
            "no schema".  Whether the provider can *enforce* it differs by
            API type, which is what :attr:`LLMResult.schema_enforced` reports.
        max_output_tokens: the output-token ceiling.  Which parameter name it
            travels under is the API type's business (ARGO alone needs three
            different ones), which is the whole reason this field is neutral.
        temperature: ``None`` means "do not send a temperature at all", which
            is not the same as sending zero -- several models reject an
            explicit temperature outright.
        params: provider extras, merged over the API type's own mapping.  A
            model's configured ``top_p`` arrives here.
        timeout_s: wall-clock budget for this one call.
        trace_id: an opaque caller-side label.  Never sent to a provider; it
            is how a reply is matched back to what asked for it.
    """

    model: str
    system: str = ""
    user: str = ""
    json_schema: Optional[Dict[str, Any]] = None
    max_output_tokens: int = 4000
    temperature: Optional[float] = 0
    params: Dict[str, Any] = {}
    timeout_s: float = 180.0
    trace_id: Optional[str] = None


class LLMResult(BaseModel):
    """The outcome of one model invocation.

    An API type NEVER raises for a call failure; it returns ``ok=False`` with
    ``error`` set, so a single bad model cannot abort a batch.  Aliased as
    ``council.types.CompletionResult``; the fields are unchanged from the M1
    record of that name.
    """

    ok: bool
    text: Optional[str] = None
    parsed: Optional[Dict[str, Any]] = None
    resolved_model: Optional[str] = None
    usage: Optional[Dict[str, Optional[int]]] = None
    elapsed_s: float = 0.0
    finish_reason: Optional[str] = None
    truncated: bool = False
    schema_enforced: Optional[bool] = None
    error: Optional[LLMError] = None
    from_cache: bool = False


# ---------------------------------------------------------------------------
# Compatibility adapter
# ---------------------------------------------------------------------------


def as_llm_request(obj: Any) -> LLMRequest:
    """Return *obj* as an :class:`LLMRequest`, whatever shape it arrived in.

    Three shapes are accepted, and the second two are why this function
    exists at all -- the council and its test suites were written against the
    M1 records and must keep working unchanged:

    * an :class:`LLMRequest` -- returned as-is.
    * a council ``CompletionRequest`` (anything carrying a ``seat``) -- the
      seat's served model, token ceiling, temperature, ``extra`` params and
      ``timeout_s`` become the matching request fields, and ``deadline_s`` is
      the timeout fallback.  ``role`` is deliberately dropped: it is the
      council's own vocabulary and no provider has ever been sent it.
    * a bare ``SeatSpec`` -- a request with no prompts, which is all
      :meth:`ApiType.effective_params` needs.  Existing tests call
      ``backend.effective_params(seat)`` directly, so this is the shape that
      keeps them green.

    Duck-typed rather than ``isinstance``-typed on purpose: this module is
    domain-neutral and must not import the council to recognise its records.

    Raises:
        TypeError: *obj* is none of the three.
    """
    if isinstance(obj, LLMRequest):
        return obj

    seat = getattr(obj, "seat", None)
    if seat is not None:
        schema = getattr(obj, "json_schema", None)
        return LLMRequest(
            model=str(getattr(seat, "model", "") or ""),
            system=str(getattr(obj, "system", "") or ""),
            user=str(getattr(obj, "user", "") or ""),
            json_schema=dict(schema) if schema is not None else None,
            max_output_tokens=int(getattr(seat, "max_output_tokens", 4000) or 4000),
            temperature=getattr(seat, "temperature", 0),
            params=dict(getattr(seat, "extra", None) or {}),
            timeout_s=float(
                getattr(seat, "timeout_s", 0)
                or getattr(obj, "deadline_s", 0)
                or 180.0
            ),
            trace_id=getattr(obj, "trace_id", None) or None,
        )

    if hasattr(obj, "model") and hasattr(obj, "max_output_tokens"):
        # A bare seat: enough for effective_params, nothing to say to a model.
        return LLMRequest(
            model=str(getattr(obj, "model", "") or ""),
            max_output_tokens=int(getattr(obj, "max_output_tokens", 4000) or 4000),
            temperature=getattr(obj, "temperature", 0),
            params=dict(
                getattr(obj, "params", None) or getattr(obj, "extra", None) or {}
            ),
            timeout_s=float(getattr(obj, "timeout_s", 0) or 180.0),
        )

    raise TypeError(
        f"cannot read {type(obj).__name__} as an LLMRequest: it is neither an "
        "LLMRequest, nor a record carrying a `seat`, nor a seat with a `model` "
        "and a `max_output_tokens`"
    )


def generation_params(spec: Any) -> Dict[str, Any]:
    """Provider-independent generation parameters for *spec*.

    The default ``effective_params`` for an API type that applies no mapping
    of its own, and the fallback a caller uses when it has no API type to ask.
    Accepts an :class:`LLMRequest` or a council ``SeatSpec`` (through
    :func:`as_llm_request`), so one helper serves both.
    """
    request = as_llm_request(spec)
    params: Dict[str, Any] = {
        "temperature": request.temperature,
        "max_output_tokens": request.max_output_tokens,
    }
    params.update(dict(request.params or {}))
    return params
