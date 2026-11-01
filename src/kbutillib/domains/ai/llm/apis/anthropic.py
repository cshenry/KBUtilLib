"""The ``anthropic`` api type: Anthropic's native Messages API (POST /v1/messages).

Speaks the official ``anthropic`` Python SDK, which is an OPTIONAL dependency
(``pip install 'kbutillib[anthropic]'``, Python >= 3.10).  The SDK is imported
lazily inside :class:`AnthropicBackend`'s constructor, never at module scope,
so ``import kbutillib`` and every other api type work without it.

The SDK's real shape -- every attribute, parameter and exception name used
below -- was established by inspecting the installed package and is recorded
in ``agent-io/work-records/anthropic-sdk-smoke.md``.  Read that before
changing a name here.

Five rules this module exists to keep, each of which cost a design round:

* **One attempt per call.**  The SDK client is built with ``max_retries=0``
  (see :meth:`AnthropicBackend._build_client` for why that is not a tuning
  preference).
* **Usage is reported in the engine's vocabulary, as a SUM.**
  ``prompt_tokens = input_tokens + cache_creation_input_tokens +
  cache_read_input_tokens``; see :func:`usage_from_anthropic`.
* **A sampling parameter is sent only when it is not None.**  The current
  Claude models reject ``temperature``/``top_p``/``top_k`` with HTTP 400.
* **The two spend-limit refusals halt the endpoint**, and are matched before
  the generic status rules; see :func:`anthropic_status_error`.
* **A backend never raises for a call failure** and never puts a credential
  in a message (every escaping string goes through
  :func:`~.http_common.redact`).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..types import (
    WEB_SEARCH_PARAM,
    AuthError,
    CallTimeout,
    InvalidOutput,
    LLMError,
    LLMRequest,
    LLMResult,
    QuotaExhausted,
    TransportError,
    as_llm_error,
    as_llm_request,
    wants_web_search,
)
from .http_common import (
    BODY_EXCERPT_CHARS,
    DEFAULT_TIMEOUT_S,
    extract_json_object,
    normalise_base_url,
    redact,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ANTHROPIC_API_KEY_ENV",
    "ANTHROPIC_BASE_URL",
    "FORBIDDEN_EXTRA_KEYS",
    "INSTALL_HINT",
    "OPERATOR_SPEND_LIMIT_PREFIX",
    "ORG_SPEND_CAP_ERROR_CODE",
    "RESERVED_EXTRA_KEYS",
    "SAMPLING_PARAMS",
    "AnthropicBackend",
    "anthropic_status_error",
    "strip_anthropic_thinking",
    "usage_from_anthropic",
]

#: Where the Messages API lives when an endpoint does not say otherwise.
ANTHROPIC_BASE_URL = "https://api.anthropic.com"

#: The environment variable read for the key when ``api_key_env`` is unset.
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

#: The message a missing SDK raises.  It names the Python floor as well as the
#: extra because the extra carries a ``python_version >= '3.10'`` marker: on
#: Python 3.9 ``pip install 'kbutillib[anthropic]'`` succeeds and installs
#: NOTHING, so "install the extra" alone would be advice that cannot work.
INSTALL_HINT = (
    "the `anthropic` api type needs the official Anthropic SDK, which is not "
    "installed. Install it with `pip install 'kbutillib[anthropic]'` -- this "
    "requires Python >= 3.10 (the SDK does not support older Pythons, and on "
    "Python 3.9 that extra resolves to nothing)."
)

#: HTTP 400 message prefix for the OPERATOR-SET spend limit (console "usage
#: limits").  There is no machine-readable code on this one, so the prefix is
#: matched case-insensitively and the matched text is quoted in the halt
#: message: a future wording change then shows up as a halt that STOPPED
#: firing, which is findable, instead of a silent regression.
OPERATOR_SPEND_LIMIT_PREFIX = "You have reached your specified API usage limits"

#: ``error.details.error_code`` on the HTTP 429 that is the ORGANISATION
#: monthly spend cap.  It carries no ``retry-after`` and retrying it cannot
#: succeed, so it halts instead of counting as a rate blip.
ORG_SPEND_CAP_ERROR_CODE = "enforced_spend_limit_reached"

#: Request fields an extra may NOT overwrite.  A caller that overwrote
#: ``model`` would change which model was asked while the route fingerprint
#: still claimed the configured one.
RESERVED_EXTRA_KEYS = ("model", "messages", "max_tokens")

#: Request fields refused outright in this lane: each changes the response
#: shape this backend parses (a stream; tool-use blocks).
FORBIDDEN_EXTRA_KEYS = ("stream", "tools")

#: Sampling parameters.  Each is sent if and only if its value is not None --
#: the current Claude models (Sonnet 5, Opus 5, Opus 4.8, Opus 4.7) REMOVED
#: them and answer HTTP 400 to any of them.  No per-model reject-list is kept:
#: a hardcoded list rots on the next model release, and ``None`` already means
#: "do not send" in :class:`~..types.LLMRequest`'s own contract.
SAMPLING_PARAMS = ("temperature", "top_p", "top_k")

#: Response headers recorded (never acted on) after each call.
RATELIMIT_HEADER_PREFIX = "anthropic-ratelimit-"

# Reasoning-tag leakage.  BOTH spellings: ``<think>`` is what the open-weight
# thinking models emit, ``<thinking>`` is this provider's documented leakage.
# Deliberately a LOCAL cleaner -- the shared ``http_common.strip_think`` is
# used by ARGO and every OpenAI-compatible endpoint, and widening it to fix a
# provider none of them use would change their behaviour from a module they
# never import.
_THINKING_RE = re.compile(r"<(think|thinking)>.*?</\1>", re.DOTALL | re.IGNORECASE)


def strip_anthropic_thinking(text: str) -> str:
    """Remove ``<think>...</think>`` AND ``<thinking>...</thinking>`` blocks.

    Has to run before the JSON object is located: a leaked scratchpad that
    contains a brace would otherwise be taken as part of the answer, which
    fails intermittently -- on exactly the leaks that contain a brace.
    """
    return _THINKING_RE.sub("", str(text or ""))


def _count(value: Any) -> int:
    """A reported token count as ``int``; absent or unreadable reads as 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _field(obj: Any, name: str) -> Any:
    """``obj.name`` for an SDK model, ``obj[name]`` for a mapping."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def usage_from_anthropic(usage: Any) -> Optional[Dict[str, Optional[int]]]:
    """Map Anthropic's usage object onto the engine's vocabulary.

    THE MOST IMPORTANT FUNCTION IN THIS MODULE.  The engine's budget reads
    exactly ``prompt_tokens`` and ``completion_tokens`` and sums them
    (``council.engine.worker._token_total``).  Anthropic names its counts
    differently, and its ``input_tokens`` counts only the tokens AFTER the
    last cache breakpoint, so the true input total is the SUM of three
    fields.  A pass-through of Anthropic's own names reports ZERO spend: the
    budget ceiling never trips and the halt never fires.  That is a shipped
    defect elsewhere (Vercel AI SDK #9921, LiteLLM #28354); do not "simplify"
    this to ``input_tokens``.

    ``None`` is returned ONLY when the provider reported no usage object at
    all -- that is how the engine tells "free" from "unmeasurable", and the
    two must stay distinguishable.
    """
    if usage is None:
        return None
    prompt = (
        _count(_field(usage, "input_tokens"))
        + _count(_field(usage, "cache_creation_input_tokens"))
        + _count(_field(usage, "cache_read_input_tokens"))
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": _count(_field(usage, "output_tokens")),
    }


def _contains_key(value: Any, key: str) -> bool:
    """Is *key* a mapping key anywhere inside *value*, at any depth?"""
    if isinstance(value, Mapping):
        if key in value:
            return True
        return any(_contains_key(v, key) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_key(v, key) for v in value)
    return False


def _error_parts(body: Any) -> Tuple[str, Optional[str]]:
    """``(provider message, error_code)`` out of a parsed error body.

    The SDK exposes ``error.details.error_code`` ONLY through the body dict,
    not as an attribute on the exception (phase-1 record, "errors").
    """
    if not isinstance(body, Mapping):
        return "", None
    error = body.get("error")
    if not isinstance(error, Mapping):
        return str(body.get("message") or ""), None
    message = str(error.get("message") or "")
    details = error.get("details")
    code = details.get("error_code") if isinstance(details, Mapping) else None
    return message, (str(code) if code else None)


def anthropic_status_error(
    status: int, body: Any, *, name: str = "anthropic", base_url: str = ANTHROPIC_BASE_URL
) -> LLMError:
    """Map an HTTP error status (plus its parsed body) onto the taxonomy.

    ORDER IS THE CONTRACT -- the two spend-limit matches come BEFORE the
    generic status rules, or a 400 would read as "bad request" and a 429 as
    "slow down":

    1. 400 whose message begins (case-insensitively)
       :data:`OPERATOR_SPEND_LIMIT_PREFIX` -> ``quota_exhausted`` (halts).
       The operator-set console limit; the most likely way a fixed credit
       runs out.
    2. 429 carrying :data:`ORG_SPEND_CAP_ERROR_CODE` -> ``quota_exhausted``
       (halts).  The organisation monthly cap.
    3. 401 / 403 -> ``auth`` (halts).
    4. 429 otherwise -> retryable ``transport`` with ``rate_limited=True``,
       so the engine's sustained-rate halt counts it (and only it).
    5. >= 500 -> retryable ``transport``.
    6. any other 4xx -> ``invalid_output``: the request was wrong, not the
       credential.
    """
    message, code = _error_parts(body)
    excerpt = redact((message or str(body or ""))[:BODY_EXCERPT_CHARS])
    if status == 400 and message.lower().startswith(OPERATOR_SPEND_LIMIT_PREFIX.lower()):
        matched = redact(message[: len(OPERATOR_SPEND_LIMIT_PREFIX)])
        return QuotaExhausted(
            f"{name}: HTTP 400 operator-set API spend limit reached (matched "
            f"{matched!r}); the endpoint is halted -- raise the limit in the "
            f"console or stop. Provider said: {excerpt!r}"
        ).to_error()
    if status == 429 and code == ORG_SPEND_CAP_ERROR_CODE:
        return QuotaExhausted(
            f"{name}: HTTP 429 {ORG_SPEND_CAP_ERROR_CODE} -- the organisation's "
            "monthly spend cap; retrying cannot succeed, so the endpoint is "
            f"halted. Provider said: {excerpt!r}"
        ).to_error()
    if status in (401, 403):
        return AuthError(
            f"{name}: HTTP {status} from {base_url}; the credential was rejected "
            f"or is missing: {excerpt!r}"
        ).to_error()
    if status == 429:
        return TransportError(
            f"{name}: HTTP 429 (rate limited) from {base_url}: {excerpt!r}"
        ).to_error().model_copy(update={"rate_limited": True})
    if status >= 500:
        return TransportError(
            f"{name}: HTTP {status} from {base_url}: {excerpt!r}"
        ).to_error()
    return InvalidOutput(
        f"{name}: HTTP {status} from {base_url}; the request was rejected, not "
        f"the credential: {excerpt!r}"
    ).to_error()


def _import_sdk() -> Any:
    """Import and return the ``anthropic`` module, or raise naming the fix."""
    try:
        import anthropic  # noqa: PLC0415 - lazy by design (optional extra)
    except ImportError as exc:
        raise ImportError(INSTALL_HINT) from exc
    return anthropic


class AnthropicBackend:
    """Seats served by Anthropic's Messages API, through the official SDK.

    Args:
        config: one endpoint's configuration, as
            ``CouncilConfig.endpoint(alias)`` returns it; ``{}`` is valid.
            Keys read: ``base_url``, ``api_key``, ``api_key_env``,
            ``timeout``, ``proxy``, ``extra_body``, ``name``.
        transport: TEST SEAT ONLY -- an ``httpx2`` async transport (in
            practice ``httpx2.MockTransport``) handed to the SDK's HTTP
            client, so the full request path runs with no socket.  It must be
            an ``httpx2`` object: the SDK refuses ``httpx`` ones.

    Raises:
        ImportError: the SDK is not installed; the message names both
            ``kbutillib[anthropic]`` and the Python >= 3.10 requirement.
    """

    kind = "anthropic"
    DEFAULT_BASE_URL = ANTHROPIC_BASE_URL

    #: No server-side search tool is wired here (``tools`` is a forbidden
    #: extra), so a request asking for the live web is refused unsent.
    supports_web_search: bool = False

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        *,
        transport: Any = None,
    ) -> None:
        self._sdk = _import_sdk()
        cfg = dict(config or {})
        self.config = cfg
        self.name = str(cfg.get("name") or self.kind)
        self.base_url = normalise_base_url(cfg.get("base_url") or self.DEFAULT_BASE_URL)
        # The SDK ships its own HTTP stack (httpx2); it does NOT inherit the
        # project's httpx configuration, so the proxy is wired explicitly.
        self.proxy: Optional[str] = str(cfg.get("proxy")).strip() if cfg.get("proxy") else None
        self.timeout = float(cfg.get("timeout") or DEFAULT_TIMEOUT_S)
        self.endpoint_extra_body = dict(cfg.get("extra_body") or {})
        self._transport = transport
        #: Non-secret kwargs the SDK client is built with (no credential), so a
        #: test can assert a proxy or the retry setting reached the client.
        self.client_kwargs: Dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_retries": 0,
        }
        self.http_client_kwargs: Dict[str, Any] = {}
        if self.proxy:
            self.http_client_kwargs["proxy"] = self.proxy
        if transport is not None:
            self.http_client_kwargs["transport"] = transport
        #: Last observed ``anthropic-ratelimit-*`` response headers.  RECORD
        #: ONLY: nothing reads them for pacing.  Process-local.
        self.last_ratelimit: Dict[str, str] = {}
        #: The SDK client.  Built at construction when the credential
        #: resolves, otherwise on the first call that finds one; ``None``
        #: until then (an absent key is a per-call ``auth`` failure, not a
        #: construction error, so one misconfigured endpoint cannot stop the
        #: worker building the others).
        self.client: Any = None
        key, _ = self._credential()
        if key:
            self.client = self._build_client(key)

    # -- credential & client ---------------------------------------------

    @property
    def api_key_env(self) -> str:
        """Name of the environment variable the key is read from."""
        return str(self.config.get("api_key_env") or ANTHROPIC_API_KEY_ENV)

    def _credential(self) -> Tuple[Optional[str], str]:
        """``(key or None, NAME of where it came from)``.  Never logs the key.

        An explicit ``api_key`` wins, else ``$api_key_env``.  There is NO
        ``"EMPTY"`` fallback (unlike the OpenAI-compatible backend): this API
        cannot be called without a key.
        """
        explicit = self.config.get("api_key")
        if explicit:
            return str(explicit), "api_key"
        value = (os.environ.get(self.api_key_env) or "").strip()
        return (value or None), self.api_key_env

    def _build_client(self, api_key: str) -> Any:
        """Construct the SDK client.  The key is passed EXPLICITLY.

        Explicit ``api_key`` and ``base_url`` switch off the SDK's own
        resolution chain (``ANTHROPIC_API_KEY``/``ANTHROPIC_AUTH_TOKEN``/
        profiles/``ANTHROPIC_BASE_URL``), so a run cannot silently use a
        different credential or host than the config declares.

        RETRIES ARE DISABLED (``max_retries=0``) AND MUST STAY DISABLED.  This
        is not a tuning preference; do not "helpfully" re-enable it:

        (a) the SDK default is 2 retries and its ``_should_retry`` retries on
            HTTP status alone -- 429 included -- without reading the error
            body (confirmed against the installed source).  That breaks this
            repository's stated invariant, "Exactly one attempt per call"
            (``llm/__init__.py``), and turns ``CouncilWorker``'s own
            ``max_attempts`` into a multiplier instead of a limit;
        (b) a spend-cap 429 can NEVER succeed, so two internal retries are two
            guaranteed-wasted requests per attempt, each eating the
            per-minute allowance -- which is how the engine's sustained-rate
            halt then fires and reports a RATE problem for an EMPTY WALLET.

        The same client serves :meth:`models`, so enumeration is one attempt
        too.
        """
        http_client = self._sdk.DefaultAsyncHttpxClient(
            timeout=self.timeout, **self.http_client_kwargs
        )
        return self._sdk.AsyncAnthropic(
            api_key=api_key, http_client=http_client, **self.client_kwargs
        )

    def _ensure_client(self) -> Tuple[Any, Optional[LLMError]]:
        """The SDK client, or the ``auth`` failure if no key resolves."""
        if self.client is not None:
            return self.client, None
        key, source = self._credential()
        if not key:
            return None, AuthError(
                f"{self.name}: no Anthropic API key -- ${source} is unset or empty "
                "and the endpoint sets no api_key; export it (or point the "
                "endpoint's api_key_env at the variable that holds it). No "
                "request was sent."
            ).to_error()
        self.client = self._build_client(key)
        return self.client, None

    # -- protocol ---------------------------------------------------------

    async def complete(self, req: Any) -> LLMResult:
        """Run one invocation.  Never raises; failures come back typed.

        *req* is converted INSIDE the guard, so even a wrongly-shaped request
        returns a typed failure rather than raising.
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

    def effective_params(self, request: Any) -> Dict[str, Any]:
        """The generation parameters this backend would really send.

        Deterministic and generation-only: no credential, header, proxy or
        routing.  A temperature that will not be sent is ABSENT from the
        result (not ``None``, not ``0``), so the route fingerprint is honest
        about the omission.
        """
        req = as_llm_request(request)
        params: Dict[str, Any] = {"max_tokens": int(req.max_output_tokens)}
        params.update(self._extras(req))
        return dict(sorted(params.items()))

    async def preflight(self) -> Dict[str, Any]:
        """Free and offline: ``ready`` iff a credential resolves.

        Reports the NAME of what was resolved, never its value.
        """
        key, source = self._credential()
        if key:
            reasons = [
                f"Anthropic API key resolved from "
                f"{'the endpoint config (api_key)' if source == 'api_key' else '$' + source}"
            ]
            return {
                "state": "ready",
                "reasons": reasons,
                "checked": [source],
                "detail": "; ".join(reasons),
                "billing": "api",
            }
        reasons = [
            f"no Anthropic API key: ${source} is unset or empty and the endpoint "
            "sets no api_key"
        ]
        return {
            "state": "refused",
            "reasons": reasons,
            "checked": [source],
            "detail": "; ".join(reasons),
        }

    async def models(self) -> List[Dict[str, Any]]:
        """List the provider's models (``GET /v1/models``, free).

        ``[]`` on any failure: enumeration is a diagnostic, never a
        precondition for a call.
        """
        client, error = self._ensure_client()
        if error is not None:
            logger.warning(
                "council %s backend %s: cannot list models: %s",
                self.kind,
                self.name,
                error.message,
            )
            return []
        try:
            page = await client.models.list()
            entries = list(getattr(page, "data", None) or [])
            return [
                entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
                for entry in entries
            ]
        except Exception as exc:  # noqa: BLE001 - listing never fails a call
            logger.warning(
                "council %s backend %s: could not list models from %s: %s",
                self.kind,
                self.name,
                self.base_url,
                redact(f"{type(exc).__name__}: {exc}"),
            )
            return []

    async def aclose(self) -> None:
        """Release the SDK client (and its HTTP connection pool)."""
        client, self.client = self.client, None
        if client is not None:
            await client.close()

    # -- the request ------------------------------------------------------

    def _extras(self, req: LLMRequest) -> Dict[str, Any]:
        """Provider extras: endpoint ``extra_body``, then the request's own.

        Mirrors ``HttpBackend._model_params``/``_extra_body``: the request's
        ``params`` (minus ``extra_body`` and :data:`WEB_SEARCH_PARAM`, which
        are not generation parameters) and its nested ``extra_body`` are
        merged over the endpoint's ``extra_body``.  Then the sampling rule:
        ``temperature`` comes from ``req.temperature`` and EVERY sampling
        parameter is dropped when it is ``None``.

        ``req.temperature`` counts only when it was SET on the request
        (``model_fields_set``): ``LLMRequest``'s field default is ``0``, and
        forwarding a field default is exactly the bug that fails every call
        on every current Claude model.  A council seat always sets it
        explicitly (``as_llm_request`` copies ``SeatSpec.temperature``, which
        the config layer leaves ``None`` for an ``anthropic`` seat that did
        not configure one), so an explicit ``0`` is still sent.
        """
        params = dict(req.params or {})
        merged: Dict[str, Any] = dict(self.endpoint_extra_body)
        merged.update(dict(params.get("extra_body") or {}))
        merged.update(
            {k: v for k, v in params.items() if k not in ("extra_body", WEB_SEARCH_PARAM)}
        )
        if "temperature" in req.model_fields_set:
            merged["temperature"] = req.temperature
        for key in SAMPLING_PARAMS:
            if key in merged and merged[key] is None:
                del merged[key]
        return merged

    def _refusal(self, req: LLMRequest, extras: Mapping[str, Any]) -> Optional[str]:
        """Why *req* must not be sent at all, or ``None``."""
        if wants_web_search(req) and not self.supports_web_search:
            return f"{self.kind} has no web search; use context mode"
        reserved = sorted(k for k in RESERVED_EXTRA_KEYS if k in extras)
        if reserved:
            return (
                f"provider extras may not overwrite reserved request field(s) "
                f"{reserved}: model/messages/max_tokens come from the request "
                "itself, and overwriting model would change which model the "
                "route fingerprint claims"
            )
        forbidden = sorted(k for k in FORBIDDEN_EXTRA_KEYS if k in extras)
        if forbidden:
            return (
                f"provider extras {forbidden} are not allowed in this lane: they "
                "change the response shape this backend parses"
            )
        output_config = extras.get("output_config")
        if isinstance(output_config, Mapping) and "format" in output_config:
            return (
                "structured outputs (output_config.format) are not requested by "
                "this backend; the engine validates the answer itself"
            )
        # Recursive on purpose: a nested cache_control is exactly the one a
        # top-level key test would let through.
        if _contains_key(extras, "cache_control"):
            return (
                "cache_control is out of scope in v1: cache reads are billed at a "
                "fraction of base input, so cache-aware billing would make the "
                "dollar figure disagree with the token figure"
            )
        return None

    def _kwargs(self, req: LLMRequest, extras: Mapping[str, Any]) -> Dict[str, Any]:
        """Keyword arguments for ``client.messages.create``.

        ``temperature``/``top_p``/``top_k`` are NOT named parameters of the
        installed SDK's ``create`` (phase-1 record), so every extra travels in
        ``extra_body``, which the SDK merges into the JSON body verbatim.
        No ``thinking`` and no ``output_config.format`` are sent.
        """
        kwargs: Dict[str, Any] = {
            "model": req.model,
            "max_tokens": int(req.max_output_tokens),
            "messages": [{"role": "user", "content": req.user}],
            "timeout": float(req.timeout_s or self.timeout),
        }
        if req.system:
            kwargs["system"] = req.system
        if extras:
            kwargs["extra_body"] = dict(extras)
        return kwargs

    async def _complete(self, req: LLMRequest, started: float) -> LLMResult:
        """The body of :meth:`complete`, minus the never-raise guard."""
        extras = self._extras(req)
        refusal = self._refusal(req, extras)
        if refusal is not None:
            return self._failed(InvalidOutput(f"{self.name}: {refusal}").to_error(), started)

        client, auth_error = self._ensure_client()
        if auth_error is not None:
            return self._failed(auth_error, started)

        sdk = self._sdk
        try:
            raw = await client.messages.with_raw_response.create(**self._kwargs(req, extras))
        except sdk.APITimeoutError as exc:  # subclass of APIConnectionError: first
            return self._failed(
                CallTimeout(
                    f"{self.name}: no reply within {req.timeout_s}s ({type(exc).__name__})"
                ).to_error(),
                started,
            )
        except sdk.APIConnectionError as exc:
            return self._failed(
                TransportError(
                    f"{self.name}: could not reach {self.base_url} "
                    f"({type(exc).__name__}: {redact(str(exc))})"
                ).to_error(),
                started,
            )
        except sdk.APIStatusError as exc:
            self._record_ratelimit(getattr(getattr(exc, "response", None), "headers", None))
            return self._failed(
                anthropic_status_error(
                    int(exc.status_code), exc.body, name=self.name, base_url=self.base_url
                ),
                started,
            )

        self._record_ratelimit(raw.headers)
        message = await raw.parse()
        return self._read_message(message, req, started)

    def _read_message(self, message: Any, req: LLMRequest, started: float) -> LLMResult:
        """Turn a parsed ``Message`` into an :class:`LLMResult`."""
        usage = usage_from_anthropic(getattr(message, "usage", None))
        resolved = str(getattr(message, "model", "") or "") or req.model
        stop_reason = getattr(message, "stop_reason", None)
        stop_reason = str(stop_reason) if stop_reason is not None else None
        # Every text block, in order.  NOT block 0: with thinking on, a
        # non-text block legitimately comes first.
        text = "".join(
            str(getattr(block, "text", "") or "")
            for block in (getattr(message, "content", None) or [])
            if getattr(block, "type", None) == "text"
        )

        def failed(error: LLMError, truncated: bool = False) -> LLMResult:
            return self._failed(
                error,
                started,
                text=text or None,
                resolved_model=resolved,
                usage=usage,
                finish_reason=stop_reason,
                truncated=truncated,
            )

        if stop_reason == "max_tokens":
            # This API's vocabulary is ``max_tokens``, not OpenAI's ``length``.
            return failed(
                InvalidOutput(
                    f"{self.name}: truncated at the token ceiling "
                    f"({req.max_output_tokens}); a cut-off answer is not a vote"
                ).to_error(),
                truncated=True,
            )
        if stop_reason == "refusal":
            # stop_details is populated only for a refusal; guard the read.
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            return failed(
                InvalidOutput(
                    f"{self.name}: the model refused (stop_reason=refusal, "
                    f"category={category!r}); a refusal is not a vote"
                ).to_error()
            )
        if not text:
            return failed(
                InvalidOutput(f"{self.name}: reply carried no text block").to_error()
            )
        parsed = extract_json_object(strip_anthropic_thinking(text))
        if parsed is None:
            return failed(
                InvalidOutput(
                    f"{self.name}: reply contained no JSON object: "
                    f"{text[:BODY_EXCERPT_CHARS]!r}"
                ).to_error()
            )
        return LLMResult(
            ok=True,
            text=text,
            parsed=parsed,
            resolved_model=resolved,
            usage=usage,
            elapsed_s=time.monotonic() - started,
            finish_reason=stop_reason,
            truncated=False,
            # No schema was requested of the provider; the engine validates.
            schema_enforced=False,
        )

    def _record_ratelimit(self, headers: Any) -> None:
        """Keep the ``anthropic-ratelimit-*`` headers.  RECORD ONLY."""
        if headers is None:
            return
        try:
            observed = {
                str(k).lower(): str(v)
                for k, v in headers.items()
                if str(k).lower().startswith(RATELIMIT_HEADER_PREFIX)
            }
        except Exception:  # noqa: BLE001 - observation must never fail a call
            return
        if observed:
            self.last_ratelimit = observed
            logger.debug("council %s backend %s: rate limits %s", self.kind, self.name, observed)

    def _failed(
        self,
        error: LLMError,
        started: float,
        *,
        text: Optional[str] = None,
        resolved_model: Optional[str] = None,
        usage: Optional[Dict[str, Optional[int]]] = None,
        finish_reason: Optional[str] = None,
        truncated: bool = False,
    ) -> LLMResult:
        """The ``ok=False`` result for *error*, keeping what was learned."""
        return LLMResult(
            ok=False,
            text=text,
            parsed=None,
            resolved_model=resolved_model,
            usage=usage,
            elapsed_s=time.monotonic() - started,
            finish_reason=finish_reason,
            truncated=truncated,
            schema_enforced=False,
            error=error,
        )
