"""Shared plumbing for the council's HTTP backends.

Both HTTP backends -- ARGO and any OpenAI-compatible server -- speak the same
``POST {base_url}/chat/completions`` protocol and differ only in four places:
how they authenticate, which generation parameters they are allowed to send,
how they clean a reply before parsing it, and what a connection failure or a
503 *means* (a dead gateway for ARGO, an unloaded model for a self-hosted
vLLM port).  :class:`HttpBackend` holds the common path and exposes exactly
those four as hooks, so neither concrete backend re-implements the request,
the taxonomy mapping or the result shape.

Three rules this module exists to enforce:

* **A backend never raises for a call failure.**  Every exit returns a
  :class:`~kbutillib.domains.ai.council.types.LLMResult` with
  ``ok=False`` and ``error`` set -- the council runs N seats per item and one
  raise would abort the caller's whole item over one bad model.
* **No credential is ever logged, stored or put in an error message.**  The
  bearer token is built per call, lives in a local, and :func:`redact` scrubs
  anything that looks like one out of any text that escapes into a message.
* **``response_format`` is never requested.**  Servers disagree about it and
  several silently reject the whole request; the engine validates the answer
  against the query's JSON Schema instead, which is why
  ``schema_enforced`` is ``False`` on every result from here.

Nothing in this module reads the council configuration or the store: a
backend is constructed from one plain endpoint-config mapping (the dict
``CouncilConfig.endpoint(alias)`` returns) and is thereafter self-contained.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

import httpx

from ..halt_causes import (
    NETWORK_ERROR,
    classify_http_halt_cause,
    with_halt_cause,
)
from ..types import (
    WEB_SEARCH_PARAM,
    AuthError,
    CallTimeout,
    InvalidOutput,
    LLMError,
    LLMRequest,
    LLMResult,
    TransportError,
    as_llm_error,
    as_llm_request,
    wants_web_search,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BODY_EXCERPT_CHARS",
    "DEFAULT_TIMEOUT_S",
    "ChatReply",
    "HttpBackend",
    "client_kwargs",
    "EMPTY_OBJECT_LABEL",
    "JsonScan",
    "describe_ambiguity",
    "extract_json_object",
    "scan_json_objects",
    "normalise_base_url",
    "read_chat_envelope",
    "redact",
    "LISTING_OUTCOMES",
    "strip_fences",
    "strip_think",
]

#: How much of a suspicious reply body is kept in an error message.  Enough to
#: recognise a provider banner when someone reads the call row weeks later,
#: short enough that the store does not fill up with HTML error pages.
BODY_EXCERPT_CHARS = 500

#: Client-level timeout when the endpoint config does not set one.  Per-call
#: timeouts come from ``req.timeout_s`` and override this.
DEFAULT_TIMEOUT_S = 180.0

# ```json ... ``` / ``` ... ``` fence markers, in any language-tag spelling.
_FENCE_RE = re.compile(r"```[A-Za-z0-9_+.-]*")

# A reasoning block as emitted by the open-weight thinking models served on
# poplar.  DOTALL because the block spans lines, and non-greedy so two blocks
# in one reply do not swallow the answer between them.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Anything that looks like a credential in free text.  Applied to every string
# that reaches an error message or a log line, because a transport exception's
# own text can quote the request headers back at us.
_REDACT_RES = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+"),
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"(?i)((?:api[_-]?key|x-api-key|token|password)\s*[:=]\s*)\S+"),
)


def _parsed_body(response: httpx.Response) -> Any:
    """The response body as JSON, or ``None`` if it is not JSON at all.

    Only used to read a provider's STRUCTURED error object for halt-cause
    classification, so an unparseable body is simply no signal -- never an
    error of its own.  The answer path parses the body separately and must
    keep its own failure behaviour.
    """
    try:
        return response.json()
    except ValueError:
        return None


def redact(text: str) -> str:
    """Return *text* with anything credential-shaped replaced by ``***``.

    Error messages from this module end up in call rows and in logs, and an
    ``httpx`` exception can carry the request headers in its own string form.
    This is the single chokepoint that keeps a bearer token out of both.
    """
    out = str(text)
    for pattern in _REDACT_RES:
        out = pattern.sub(r"\1***", out)
    return out


def normalise_base_url(base_url: str) -> str:
    """Strip whitespace and any trailing slashes from *base_url*."""
    return str(base_url or "").strip().rstrip("/")


def strip_think(text: str) -> str:
    """Remove ``<think>...</think>`` reasoning blocks from *text*.

    Thinking models put their scratchpad in front of the answer; the
    scratchpad frequently contains braces and even draft JSON, so it has to go
    before the answer object is located rather than after.
    """
    return _THINK_RE.sub("", str(text or ""))


def strip_fences(text: str) -> str:
    """Remove markdown code-fence markers from *text*.

    Only the markers are removed, not the content between them: a model that
    wrapped its JSON in ```` ```json ```` answered correctly and must not be
    failed for the decoration.
    """
    return _FENCE_RE.sub("", str(text or "")).strip()


#: Most candidate key labels kept on a :class:`JsonScan` (and so in a message).
MAX_CANDIDATE_LABELS = 5

#: Longest key excerpt put in a diagnostic.  A hostile reply can carry a
#: megabyte-long key; the message must stay small whatever the reply holds.
MAX_KEY_LABEL_CHARS = 40

#: Label for a decoded object that has no keys at all.
EMPTY_OBJECT_LABEL = "<empty object>"

_NON_PRINTABLE_RE = re.compile(r"[^\x20-\x7e]")


@dataclasses.dataclass(frozen=True)
class JsonScan:
    """Immutable result of scanning one reply for top-level JSON objects.

    The count travels in this return value -- never in a module global or an
    attribute on a backend -- because several calls are in flight at once on
    one cached per-endpoint api type.

    ``obj`` is the decoded object when ``count == 1`` and ``None`` otherwise:
    two or more objects are AMBIGUOUS and nothing is guessed.  ``labels``
    holds a bounded, sanitised first-key summary for up to
    :data:`MAX_CANDIDATE_LABELS` objects.
    """

    obj: Optional[Dict[str, Any]]
    count: int
    labels: Tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return self.count >= 2


def _key_label(obj: Dict[str, Any]) -> str:
    """Bounded, printable-ASCII label for *obj*'s first key."""
    if not obj:
        return EMPTY_OBJECT_LABEL
    key = str(next(iter(obj)))
    clean = _NON_PRINTABLE_RE.sub("?", key[: MAX_KEY_LABEL_CHARS + 1])
    if len(clean) > MAX_KEY_LABEL_CHARS:
        clean = clean[:MAX_KEY_LABEL_CHARS] + "..."
    return repr(clean)


def scan_json_objects(text: str) -> JsonScan:
    """Enumerate the top-level JSON objects in *text*.

    Fences are stripped, then ``raw_decode`` is tried at the FIRST ``{`` only.
    If that fails the answer is zero objects -- later and inner braces are NOT
    tried, because that would salvage a nested object out of a broken
    enclosing answer (a truncated ballot silently becoming a vote).  If it
    succeeds, scanning continues from where the decoder stopped: the next
    ``{`` decoding successfully means another object; a failure after at
    least one success is tolerated as trailing prose.  Jumping past each
    decoded object makes braces inside its strings unreachable.

    Known limitation: a malformed brace BETWEEN two objects stops the scan,
    so the second, competing object is never seen.
    """
    candidate = strip_fences(text)
    decoder = json.JSONDecoder()
    first: Optional[Dict[str, Any]] = None
    labels: List[str] = []
    count = 0
    pos = candidate.find("{")
    while pos >= 0:
        try:
            value, end = decoder.raw_decode(candidate, pos)
        except (ValueError, TypeError, RecursionError):
            break
        if not isinstance(value, dict):  # cannot happen at a '{', kept defensive
            break
        count += 1
        if count == 1:
            first = value
        if len(labels) < MAX_CANDIDATE_LABELS:
            labels.append(_key_label(value))
        pos = candidate.find("{", end)
    return JsonScan(obj=first if count == 1 else None, count=count, labels=tuple(labels))


def describe_ambiguity(scan: JsonScan) -> str:
    """Message fragment naming the object count and each object's first key."""
    shown = ", ".join(scan.labels)
    more = "" if scan.count <= len(scan.labels) else ", ..."
    return (
        f"reply contained {scan.count} JSON objects (first keys: {shown}{more}); "
        "the reply is ambiguous and nothing was guessed"
    )


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Return the single JSON object in *text*, or ``None``.

    Thin wrapper over :func:`scan_json_objects`.  ``None`` means "no JSON
    object here" or "two or more, ambiguous"; callers that must tell those
    apart use :func:`scan_json_objects`.  Every caller turns ``None`` into a
    typed failure -- never an exception.
    """
    return scan_json_objects(text).obj


def client_kwargs(
    proxy: Optional[str],
    timeout: float,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Dict[str, Any]:
    """Build the exact kwargs handed to :class:`httpx.AsyncClient`.

    Deliberately a separate, non-secret function: the backend keeps the result
    on :attr:`HttpBackend.client_kwargs` and constructs the client with
    ``**kwargs``, so a test can assert that a configured proxy really reached
    the client without opening a connection through it.  Credentials are not
    here at all -- the ``Authorization`` header is built per call.
    """
    kwargs: Dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    if proxy:
        kwargs["proxy"] = proxy
    if transport is not None:
        kwargs["transport"] = transport
    return kwargs


class ChatReply:
    """The four fields the council reads out of a chat-completions envelope."""

    __slots__ = ("content", "finish_reason", "resolved_model", "usage")

    def __init__(
        self,
        content: Optional[str],
        finish_reason: Optional[str],
        resolved_model: Optional[str],
        usage: Optional[Dict[str, Optional[int]]],
    ) -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.resolved_model = resolved_model
        self.usage = usage


def _int_or_none(value: Any) -> Optional[int]:
    """Coerce a reported token count to ``int``, or ``None`` if it is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_chat_envelope(payload: Any) -> ChatReply:
    """Read ``choices[0]``, ``usage`` and the resolved model out of *payload*.

    Tolerant by design: a missing field becomes ``None`` rather than an
    exception, and the caller decides what a missing field means.  ``usage``
    stays ``None`` when the server reported none at all, which is the signal
    the worker's budget accounting needs to tell "free" from "unmeasurable".
    """
    if not isinstance(payload, Mapping):
        return ChatReply(None, None, None, None)

    content: Optional[str] = None
    finish_reason: Optional[str] = None
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        choice = choices[0]
        message = choice.get("message")
        if isinstance(message, Mapping):
            raw = message.get("content")
            if isinstance(raw, str):
                content = raw
        reason = choice.get("finish_reason")
        finish_reason = str(reason) if reason is not None else None

    usage: Optional[Dict[str, Optional[int]]] = None
    raw_usage = payload.get("usage")
    if isinstance(raw_usage, Mapping):
        usage = {
            "prompt_tokens": _int_or_none(raw_usage.get("prompt_tokens")),
            "completion_tokens": _int_or_none(raw_usage.get("completion_tokens")),
        }

    model = payload.get("model")
    resolved = str(model) if model else None
    return ChatReply(content, finish_reason, resolved, usage)


#: Why a model listing is what it is; see :meth:`HttpBackend.models_report`.
LISTING_OUTCOMES = (
    "answered",
    "not_implemented",
    "credential_rejected",
    "http_error",
    "unreachable",
    "malformed",
)


def _listing(
    outcome: str, detail: str, models: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """One ``models_report()`` result; *detail* is always redacted."""
    return {"models": list(models or []), "outcome": outcome, "detail": redact(detail)}


class HttpBackend:
    """Common implementation of the ``Backend`` protocol over HTTP.

    Subclasses set :attr:`kind` and :attr:`default_base_url` and override the
    hooks: :meth:`_bearer` (authentication), :meth:`_params` (what generation
    parameters this server accepts), :meth:`_clean_content` (what to remove
    from a reply before looking for JSON), :meth:`_screen_text` (whether a
    non-JSON body is a recognisable provider refusal) and
    :meth:`_connect_error` / :meth:`_status_error` (what a failed connection
    or an HTTP status means here).

    Args:
        config: One endpoint's configuration, as
            ``CouncilConfig.endpoint(alias)`` returns it.  ``{}`` is valid and
            falls back to the subclass defaults.
        transport: TEST SEAT ONLY.  An :class:`httpx.AsyncBaseTransport` --
            in practice :class:`httpx.MockTransport` -- handed straight to the
            client so the whole suite can exercise the real request path with
            no socket.  Production call sites never pass it.
    """

    kind: str = "openai"
    default_base_url: Optional[str] = None

    #: Optional ``ApiType`` member (see :mod:`..protocol`): can this API type
    #: run a call with live web search?  ``False`` for every HTTP backend
    #: this repository ships -- neither ARGO nor a local OpenAI-compatible
    #: server exposes a search tool -- and a request that asks for one is
    #: refused by :meth:`_complete` BEFORE anything is sent.
    supports_web_search: bool = False

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        cfg = dict(config or {})
        self.config = cfg
        self.name = str(cfg.get("name") or self.kind)
        self.base_url = normalise_base_url(
            cfg.get("base_url") or self.default_base_url or ""
        )
        # An empty proxy string is how the packaged config spells "no proxy"
        # (``proxy: ${KBU_ARGO_PROXY:-}``), so falsy means None, not "".
        self.proxy: Optional[str] = str(cfg.get("proxy")).strip() if cfg.get("proxy") else None
        self.timeout = float(cfg.get("timeout") or DEFAULT_TIMEOUT_S)
        self.endpoint_extra_body = dict(cfg.get("extra_body") or {})
        #: The exact kwargs the client was built with; no credentials among them.
        self.client_kwargs = client_kwargs(self.proxy, self.timeout, transport)
        self.client = httpx.AsyncClient(**self.client_kwargs)

    # -- protocol ---------------------------------------------------------

    async def complete(self, req: Any) -> LLMResult:
        """Run one invocation.  Never raises; failures come back typed.

        The outer ``except`` is not defensive decoration: the protocol's one
        hard rule is that a call failure cannot propagate, and an unforeseen
        exception from ``httpx`` or from a hook would otherwise abort the
        caller's whole item.

        *req* is an :class:`~..types.LLMRequest`, or anything
        :func:`~..types.as_llm_request` can read as one -- which is how the
        council's own ``CompletionRequest`` still works unchanged.  The
        conversion happens INSIDE the guard so that even a request of the
        wrong shape comes back as a typed failure rather than a raise.
        """
        started = time.monotonic()
        try:
            return await self._complete(as_llm_request(req), started)
        except Exception as exc:  # noqa: BLE001 - a backend never raises
            logger.warning(
                "council %s backend %s: unexpected failure: %s",
                self.kind,
                self.name,
                redact(f"{type(exc).__name__}: {exc}"),
            )
            error = as_llm_error(exc)
            error.message = redact(error.message)
            return self._failed(error, started)

    async def models(self) -> List[Dict[str, Any]]:
        """List the server's models.  Free, and ``[]`` on any failure.

        Listing is a diagnostic, never a precondition for a call, so an
        unreachable or unsupported ``/models`` is logged and reported as "I
        cannot enumerate", not as an error.  Use :meth:`models_report` to learn
        WHY a listing is empty.
        """
        return (await self.models_report())["models"]

    async def models_report(self) -> Dict[str, Any]:
        """The listing plus the reason it is what it is.  Never raises.

        Returns ``{"models": [...], "outcome": ..., "detail": ...}`` where
        ``models`` is exactly what :meth:`models` returns and ``outcome`` is
        one of :data:`LISTING_OUTCOMES`.  ``detail`` is a short human string,
        passed through :func:`redact`; no credential and no response body is
        ever put in it.
        """
        try:
            return await self._models_report()
        except Exception as exc:  # noqa: BLE001 - listing never fails a call
            return _listing("malformed", f"unexpected {type(exc).__name__}: {exc}")

    async def _models_report(self) -> Dict[str, Any]:
        bearer, auth_error = self._bearer()
        if auth_error is not None:
            logger.warning(
                "council %s backend %s: cannot list models: %s",
                self.kind,
                self.name,
                auth_error.message,
            )
            return _listing("credential_rejected", auth_error.message)
        try:
            response = await self.client.get(
                f"{self.base_url}/models", headers=self._headers(bearer)
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            return self._listing_failed("unreachable", exc)
        except Exception as exc:  # noqa: BLE001 - listing never fails a call
            # Some other transport or protocol fault: the server did not give a
            # usable answer, but "unreachable" is reserved for a refused
            # connection and a timeout.
            return self._listing_failed("malformed", exc)

        status = response.status_code
        if not 200 <= status < 300:
            if status in (404, 405):
                outcome = "not_implemented"
            elif status in (401, 403):
                outcome = "credential_rejected"
            else:
                outcome = "http_error"
            return _listing(outcome, f"HTTP {status} from {self.base_url}/models")
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            return self._listing_failed("malformed", exc, "invalid JSON")

        entries = payload.get("data") if isinstance(payload, Mapping) else payload
        if not isinstance(entries, list):
            return _listing(
                "malformed",
                f"{self.base_url}/models body is not a list or a 'data' list",
            )
        models = [dict(entry) for entry in entries if isinstance(entry, Mapping)]
        bad = len(entries) - len(models) + sum(
            1
            for entry in models
            for key in ("id", "name", "internal_id")
            if entry.get(key) is not None and not isinstance(entry.get(key), str)
        )
        if bad:
            return _listing(
                "malformed",
                f"{bad} entr(ies)/field(s) in the listing from {self.base_url}/models "
                "are not mappings or strings",
                models,
            )
        return _listing("answered", f"{len(models)} model(s) listed", models)

    def _listing_failed(
        self, outcome: str, exc: BaseException, what: str = ""
    ) -> Dict[str, Any]:
        text = redact(f"{type(exc).__name__}: {exc}")
        logger.warning(
            "council %s backend %s: could not list models from %s: %s",
            self.kind,
            self.name,
            self.base_url,
            text,
        )
        return _listing(outcome, f"{what + ': ' if what else ''}{text}")

    def effective_params(self, request: Any) -> Dict[str, Any]:
        """The generation parameters this backend would really send.

        Goes into the caller's prompt hash and route fingerprint, so it is
        deterministic and holds generation parameters ONLY -- no header, no
        credential, no proxy, nothing about how the request is routed.
        ``extra_body`` is included (nested) because it travels in the request
        body and can change the answer; leaving it out would let two different
        questions share one cache entry.

        *request* may be an :class:`~..types.LLMRequest` or a council
        ``SeatSpec`` -- the generation fields are the same either way, and
        existing callers pass the seat.
        """
        req = as_llm_request(request)
        params = dict(self._params(req))
        extra_body = self._extra_body(req)
        if extra_body:
            params["extra_body"] = extra_body
        return params

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()

    # -- the request ------------------------------------------------------

    async def _complete(self, req: LLMRequest, started: float) -> LLMResult:
        """The real body of :meth:`complete`, minus the never-raise guard.

        The web-search refusal comes FIRST, before the credential is even
        resolved, because the point of it is that nothing is sent: a caller
        that asked for the live web and was quietly served the model's
        training data has no way to tell it happened, so the only honest
        answer from a backend with no search tool is ``ok=False``.  Reported
        as ``invalid_output`` and NOT retryable -- a second identical attempt
        would be refused identically, and the fix is on the caller's side
        (put what you found in the prompt, i.e. context mode).
        """
        if wants_web_search(req) and not self.supports_web_search:
            return self._failed(
                InvalidOutput(
                    f"{self.kind} has no web search; use context mode"
                ).to_error(),
                started,
            )

        bearer, auth_error = self._bearer()
        if auth_error is not None:
            return self._failed(auth_error, started)

        body = self._body(req)
        url = f"{self.base_url}/chat/completions"
        try:
            response = await self.client.post(
                url,
                json=body,
                headers=self._headers(bearer),
                timeout=float(req.timeout_s or self.timeout),
            )
        except httpx.TimeoutException as exc:
            return self._failed(
                with_halt_cause(
                    CallTimeout(
                        f"{self.name}: no reply within {req.timeout_s}s "
                        f"({type(exc).__name__})"
                    ).to_error(),
                    NETWORK_ERROR,
                ),
                started,
            )
        except httpx.ConnectError as exc:
            return self._failed(self._connect_error(exc), started)
        except httpx.HTTPError as exc:
            return self._failed(
                with_halt_cause(
                    TransportError(
                        f"{self.name}: {type(exc).__name__}: {redact(str(exc))}"
                    ).to_error(),
                    NETWORK_ERROR,
                ),
                started,
            )

        if response.status_code >= 400:
            return self._failed(self._status_error(response, req), started)

        try:
            payload: Any = response.json()
        except ValueError:
            # Not even a JSON envelope.  Before calling this a transport
            # failure, let the subclass check for a provider banner: ARGO's
            # budget breaker answers 200 with prose, and misreading that as a
            # retryable blip is exactly how 64 wakes were burned on
            # 2026-10-05.
            screened = self._screen_text(response.text)
            if screened is not None:
                return self._failed(screened, started, text=response.text)
            return self._failed(
                TransportError(
                    f"{self.name}: reply was not JSON "
                    f"(HTTP {response.status_code}): "
                    f"{redact(response.text[:BODY_EXCERPT_CHARS])!r}"
                ).to_error(),
                started,
                text=response.text,
            )

        reply = read_chat_envelope(payload)
        if reply.content is None:
            # A well-formed envelope with no assistant message is not an
            # answer, so the refusal screen applies to the raw body here too.
            screened = self._screen_text(response.text)
            if screened is not None:
                return self._failed(screened, started, reply=reply, text=response.text)
            if reply.finish_reason == "length":
                # A reasoning model that spent its whole ceiling on a separate
                # reasoning field leaves content null: that is a truncation.
                return self._failed(
                    InvalidOutput(
                        f"{self.name}: truncated at the token ceiling "
                        f"({req.max_output_tokens}) with no content (reasoning "
                        "budget exhausted); a cut-off answer is not a vote"
                    ).to_error(),
                    started,
                    reply=reply,
                    text=response.text,
                    truncated=True,
                )
            return self._failed(
                InvalidOutput(
                    f"{self.name}: reply carried no choices[0].message.content: "
                    f"{redact(response.text[:BODY_EXCERPT_CHARS])!r}"
                ).to_error(),
                started,
                reply=reply,
                text=response.text,
            )

        if reply.finish_reason == "length":
            return self._failed(
                InvalidOutput(
                    f"{self.name}: truncated at the token ceiling "
                    f"({req.max_output_tokens}); a cut-off answer is not a vote"
                ).to_error(),
                started,
                reply=reply,
                text=reply.content,
                truncated=True,
            )

        scan = scan_json_objects(self._clean_content(reply.content))
        parsed = scan.obj
        if parsed is None:
            screened = self._screen_text(reply.content)
            if screened is not None:
                return self._failed(screened, started, reply=reply, text=reply.content)
            if scan.ambiguous:
                detail = describe_ambiguity(scan)
            else:
                detail = "reply contained no JSON object"
            return self._failed(
                InvalidOutput(
                    f"{self.name}: {detail}: "
                    f"{reply.content[:BODY_EXCERPT_CHARS]!r}"
                ).to_error(),
                started,
                reply=reply,
                text=reply.content,
            )

        return LLMResult(
            ok=True,
            text=reply.content,
            parsed=parsed,
            resolved_model=reply.resolved_model or req.model,
            usage=reply.usage,
            elapsed_s=time.monotonic() - started,
            finish_reason=reply.finish_reason,
            truncated=False,
            # No ``response_format`` was requested (servers differ), so the
            # engine -- not the server -- validates against the query schema.
            schema_enforced=False,
        )

    def _body(self, req: LLMRequest) -> Dict[str, Any]:
        """The chat-completions request body for *req*."""
        body: Dict[str, Any] = {
            "model": req.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
        }
        body.update(self._params(req))
        body.update(self._extra_body(req))
        return body

    def _headers(self, bearer: Optional[str]) -> Dict[str, str]:
        """Request headers, built per call so no credential is ever stored.

        Order is the guarantee: ``Content-Type``, then whatever
        :meth:`_static_headers` contributes, then ``Authorization`` LAST.  A
        static header can therefore never displace the credential, whatever
        a configuration tried to call it.
        """
        headers = {"Content-Type": "application/json"}
        headers.update(self._static_headers())
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    def _failed(
        self,
        error: LLMError,
        started: float,
        reply: Optional[ChatReply] = None,
        text: Optional[str] = None,
        truncated: bool = False,
    ) -> LLMResult:
        """Build the ``ok=False`` result for *error*, keeping what we learned."""
        return LLMResult(
            ok=False,
            text=text,
            parsed=None,
            resolved_model=reply.resolved_model if reply is not None else None,
            usage=reply.usage if reply is not None else None,
            elapsed_s=time.monotonic() - started,
            finish_reason=reply.finish_reason if reply is not None else None,
            truncated=truncated,
            schema_enforced=False,
            error=error,
        )

    # -- hooks ------------------------------------------------------------

    def _bearer(self) -> Tuple[Optional[str], Optional[LLMError]]:
        """Return ``(bearer value, error)``; exactly one is not ``None``.

        A credential problem is returned, never raised: an endpoint with no
        usable credential must halt that endpoint through the normal failure
        path rather than crash the worker at construction.
        """
        return None, None

    def _static_headers(self) -> Dict[str, str]:
        """Non-secret headers this backend adds to EVERY request.  None here.

        Empty by default so a subclass that does not opt in sends exactly the
        header dict it sent before this hook existed -- ARGO's requests are
        byte-identical, which is the point of the default.
        """
        return {}

    def _params(self, req: LLMRequest) -> Dict[str, Any]:
        """Generation parameters this server accepts for *req*."""
        params: Dict[str, Any] = {"max_tokens": int(req.max_output_tokens)}
        if req.temperature is not None:
            params["temperature"] = req.temperature
        params.update(self._model_params(req))
        return params

    def _model_params(self, req: LLMRequest) -> Dict[str, Any]:
        """The request's own provider params, minus the two that are not params.

        ``CouncilConfig._seat`` puts a model's ``params:`` block on the seat,
        which the request carries here, so dropping them would silently
        discard a configured ``top_p``.

        Two keys are dropped because they are not generation parameters at
        all: ``extra_body`` (raw body additions, merged separately by
        :meth:`_extra_body`) and :data:`~..types.WEB_SEARCH_PARAM`, which is
        an instruction to the API TYPE about how to make the call.  Dropping
        ``web_search`` matters even though a truthy one is refused above:
        a caller that writes ``web_search=False`` on every request must not
        have ``{"web_search": false}`` posted into a chat-completions body,
        where a strict server answers 400 for an unknown field.
        """
        dropped = ("extra_body", WEB_SEARCH_PARAM)
        return {
            k: v for k, v in dict(req.params or {}).items() if k not in dropped
        }

    def _extra_body(self, req: LLMRequest) -> Dict[str, Any]:
        """Raw body additions: the endpoint's, overridden by the request's."""
        request_extra = dict(req.params or {}).get("extra_body") or {}
        merged = dict(self.endpoint_extra_body)
        merged.update(dict(request_extra))
        return merged

    def _clean_content(self, content: str) -> str:
        """Clean a reply before the JSON object is located.  Identity here."""
        return content

    def _screen_text(self, text: str) -> Optional[LLMError]:
        """Recognise a provider refusal in a body that is not an answer.

        Called ONLY when no JSON answer object could be read, so a phrase
        quoted inside a legitimate answer can never trip it.
        """
        return None

    def _connect_error(self, exc: BaseException) -> LLMError:
        """What a refused/failed connection means for this backend.

        The cause is ``network_error`` on the EXCEPTION CLASS, which is the
        only structured signal a request that never arrived leaves behind --
        there is no status and no error object to read.
        """
        return with_halt_cause(
            TransportError(
                f"{self.name}: could not connect to {self.base_url} "
                f"({type(exc).__name__}: {redact(str(exc))})"
            ).to_error(),
            NETWORK_ERROR,
        )

    def _status_error(
        self, response: httpx.Response, req: Optional[LLMRequest] = None
    ) -> LLMError:
        """Map an HTTP error status onto the council's failure taxonomy.

        *req* is the request that produced *response*, for a subclass that
        needs to know which model was asked for -- the OpenAI-compatible
        server answers 404 naming the model when it is not loaded.  Optional
        so a caller (or a test) may ask what a bare status means.
        """
        status = response.status_code
        excerpt = redact(response.text[:BODY_EXCERPT_CHARS])
        # The cause code rides on whichever error is built below.  Classified
        # HERE and only here, because this is the one place holding both the
        # status and the provider's structured error object -- by the time the
        # worker halts the endpoint it has the LLMError and nothing else.
        cause = classify_http_halt_cause(status, _parsed_body(response))
        if status in (401, 403):
            return with_halt_cause(
                AuthError(
                    f"{self.name}: HTTP {status} from {self.base_url}; the credential "
                    f"was rejected or is missing: {excerpt!r}"
                ).to_error(),
                cause,
            )
        if status == 429:
            # Still a retryable ``transport`` failure -- one 429 against a
            # windowed limit is normal and is retried -- but FLAGGED, so a
            # caller pacing the account can apply its own SUSTAINED rule
            # without matching on this message (see ``LLMError.rate_limited``).
            return with_halt_cause(
                TransportError(
                    f"{self.name}: HTTP 429 (rate limited) from {self.base_url}: "
                    f"{excerpt!r}"
                ).to_error().model_copy(update={"rate_limited": True}),
                cause,
            )
        if status >= 500:
            return with_halt_cause(
                TransportError(
                    f"{self.name}: HTTP {status} from {self.base_url}: {excerpt!r}"
                ).to_error(),
                cause,
            )
        # Any other 4xx is the request itself being wrong -- a bad model name,
        # an unsupported parameter.  Retrying cannot fix it and it must not
        # halt the endpoint, so it fails this call and surfaces the mistake.
        return with_halt_cause(
            InvalidOutput(
                f"{self.name}: HTTP {status} from {self.base_url}; the request was "
                f"rejected, not the credential: {excerpt!r}"
            ).to_error(),
            cause,
        )

