"""HTTP backends: ANL's ARGO gateway, and any OpenAI-compatible server.

Two concrete implementations of the
:class:`~kbutillib.domains.ai.council.protocols.Backend` protocol over the
shared machinery in :mod:`.http_common`:

* :class:`ArgoBackend` -- ARGO is OpenAI-compatible, authenticates by ANL
  username rather than an API key, insists on a different token-ceiling
  parameter for almost every model family, and has a monthly spend breaker
  that answers HTTP 200 with a fake assistant message.  All three are handled
  here.
* :class:`OpenAICompatibleBackend` -- self-hosted vLLM ports (the poplar
  servers), where a 503 or a refused connection means "the model is not
  loaded", not "the service is broken", so the call waits instead of failing.

The ARGO parameter mapping in :func:`argo_model_kwargs` is a port of
``concord/argo_client.py::model_kwargs`` from Jose Faria's CONCORDIA project;
the per-family rules (which models take ``max_tokens`` versus
``max_completion_tokens``, which refuse a temperature outright, which need a
token floor) are his findings against the live gateway, and the credit for
them is his.

Neither backend starts, stops or supervises a model, and neither ever sends a
request to a ``/service/`` path except the single optional ``GET
/service/status`` in :meth:`OpenAICompatibleBackend.models` -- lifecycle is a
different component's job and a council seat must never provoke it.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

import httpx

from ..types import (
    AuthError,
    LLMError,
    LLMRequest,
    ModelUnavailable,
    QuotaExhausted,
)
from .http_common import (
    BODY_EXCERPT_CHARS,
    HttpBackend,
    redact,
    strip_think,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ARGO_BASE_URL",
    "ARGO_FIXED_TEMPERATURE_MODELS",
    "ARGO_GEMINI_TOKEN_FLOOR",
    "ARGO_GEMINI_TOKEN_FLOOR_MODELS",
    "ARGO_NO_TEMPERATURE_MODELS",
    "ARGO_REFUSAL_SIGNATURES",
    "ARGO_USER_ENV",
    "ArgoBackend",
    "OpenAICompatibleBackend",
    "argo_model_kwargs",
    "argo_refusal_error",
]

#: ARGO's OpenAI-compatible base URL.
ARGO_BASE_URL = "https://apps.inside.anl.gov/argoapi/v1"

#: Environment variable holding the ANL username ARGO authenticates by, when
#: the endpoint config does not name a different one through ``user_env``.
ARGO_USER_ENV = "ARGO_USER"

#: Models that reject an explicit temperature; sending one fails the call.
ARGO_NO_TEMPERATURE_MODELS = frozenset({"claudeopus47", "claudeopus48", "claudesonnet5"})

#: Models that accept only ``temperature: 1``, whatever the seat asked for.
ARGO_FIXED_TEMPERATURE_MODELS = frozenset(
    {"gpt55", "gpt56sol", "gpt56terra", "gpt56luna"}
)

#: Gemini models that answer nothing useful below a token floor.
ARGO_GEMINI_TOKEN_FLOOR_MODELS = frozenset({"gemini35flash", "gemini31flashlite"})
ARGO_GEMINI_TOKEN_FLOOR = 1500

#: Phrases ARGO's own guard rails put in a **HTTP 200** assistant message, and
#: the ``LLMError`` kind each one means.  Matched case-insensitively.
#:
#: This table is the whole reason the breaker is survivable.  ARGO enforces a
#: $350/month ceiling by replying 200 OK with prose where the answer should be;
#: on 2026-10-05 that killed 64 scheduled wakes because nothing in the stack
#: looked at the content.  A usage notice is a spend halt (quota), an
#: authentication notice is a credential halt (auth), and both halt the
#: endpoint instead of being retried into the ground.
#:
#: Order matters only for reporting: the first phrase found names the error.
ARGO_REFUSAL_SIGNATURES: Tuple[Tuple[str, str], ...] = (
    ("IMPORTANT USAGE NOTICE FROM ARGO", "quota_exhausted"),
    ("ACCESS REVOKED", "quota_exhausted"),
    ("Monthly limit exceeded", "quota_exhausted"),
    ("AUTHENTICATION NOTICE", "auth"),
    ("msg_blocked_", "auth"),
)


def argo_model_kwargs(
    model: str, temperature: Optional[float], max_output_tokens: int
) -> Dict[str, Any]:
    """Return ARGO's generation parameters for *model*.

    A port of ``concord/argo_client.py::model_kwargs`` by Jose Faria
    (CONCORDIA).  ARGO fronts several vendors and passes their parameter
    quirks straight through, so the token ceiling is ``max_tokens`` for the
    Anthropic and Gemini families but ``max_completion_tokens`` for the
    OpenAI reasoning families, and a handful of models reject ``temperature``
    outright or accept only ``1``.  Sending the wrong one is a hard 400, which
    is why this is a table and not a guess.

    Args:
        model: ARGO's own served model name (e.g. ``gemini35flash``).
        temperature: The requested temperature; ``None`` means "do not send one".
        max_output_tokens: The requested token ceiling.

    Returns:
        The parameter dict to merge into the request body.
    """
    name = str(model or "")
    n = int(max_output_tokens)

    if name.startswith(("claudeopus", "claudesonnet", "claudehaiku")):
        params: Dict[str, Any] = {"max_tokens": n}
        if temperature is not None and name not in ARGO_NO_TEMPERATURE_MODELS:
            params["temperature"] = temperature
        return params

    if name in ARGO_FIXED_TEMPERATURE_MODELS:
        return {"max_completion_tokens": n, "temperature": 1}

    if name.startswith("gpto"):
        # o-series reasoning models: no temperature at all.
        return {"max_completion_tokens": n}

    if name.startswith(("gpt5", "gpt41")):
        params = {"max_completion_tokens": n}
        if temperature is not None:
            params["temperature"] = temperature
        return params

    if name.startswith("gemini"):
        ceiling = (
            max(n, ARGO_GEMINI_TOKEN_FLOOR)
            if name in ARGO_GEMINI_TOKEN_FLOOR_MODELS
            else n
        )
        params = {"max_tokens": ceiling}
        if temperature is not None:
            params["temperature"] = temperature
        return params

    params = {"max_tokens": n}
    if temperature is not None:
        params["temperature"] = temperature
    return params


def argo_refusal_error(text: str) -> Optional[LLMError]:
    """Return the typed error for an ARGO guard-rail reply, or ``None``.

    Callers must only reach this when the body could NOT be read as a JSON
    answer object: a legitimate answer that happens to quote ``"ACCESS
    REVOKED"`` in a ``why`` field is a vote, not a refusal.

    The first 500 characters of the matched body travel in the error message,
    because the next person to see this will be reading a call row and asking
    what ARGO actually said.
    """
    body = str(text or "")
    haystack = body.lower()
    for signature, kind in ARGO_REFUSAL_SIGNATURES:
        if signature.lower() not in haystack:
            continue
        excerpt = redact(body[:BODY_EXCERPT_CHARS])
        if kind == "auth":
            return AuthError(
                "ARGO replied HTTP 200 with an authentication notice instead of an "
                f"answer (matched {signature!r}); the endpoint is halted until the "
                f"credential is fixed. Body: {excerpt!r}"
            ).to_error()
        return QuotaExhausted(
            "ARGO replied HTTP 200 with a usage notice instead of an answer "
            f"(matched {signature!r}) -- this is the monthly spend breaker, not a "
            f"model answer; the endpoint is halted. Body: {excerpt!r}"
        ).to_error()
    return None


class ArgoBackend(HttpBackend):
    """Seats served through ANL's ARGO gateway.

    Authentication is an ANL username, not an API key: ARGO reads it from the
    ``Authorization: Bearer`` header.  A missing username does NOT raise at
    construction -- the worker builds every backend up front, and a config
    gap on one endpoint must not stop the others -- so each call returns an
    ``auth`` failure, which halts just this endpoint.
    """

    kind = "argo"
    default_base_url = ARGO_BASE_URL

    @property
    def user_env(self) -> str:
        """Name of the environment variable holding the ANL username."""
        return str(self.config.get("user_env") or ARGO_USER_ENV)

    def _bearer(self) -> Tuple[Optional[str], Optional[LLMError]]:
        """Resolve the ANL username, or return the ``auth`` failure.

        Read per call rather than cached at construction so that a user who
        exports ``ARGO_USER`` after starting a daemon is picked up on the next
        sweep instead of after a restart.
        """
        user = (os.environ.get(self.user_env) or "").strip()
        if not user:
            return None, AuthError(
                f"{self.name}: ARGO authenticates by ANL username and ${self.user_env} "
                "is unset or empty; export it (or point the endpoint's user_env at the "
                "variable that holds it). No request was sent."
            ).to_error()
        return user, None

    def _params(self, req: LLMRequest) -> Dict[str, Any]:
        """ARGO's per-family parameter mapping, plus the request's own params."""
        params = argo_model_kwargs(req.model, req.temperature, req.max_output_tokens)
        params.update(self._model_params(req))
        return params

    def _screen_text(self, text: str) -> Optional[LLMError]:
        """Check the body against :data:`ARGO_REFUSAL_SIGNATURES`."""
        return argo_refusal_error(text)


class OpenAICompatibleBackend(HttpBackend):
    """Seats served by any OpenAI-compatible server -- in practice vLLM.

    Two things set this apart from :class:`ArgoBackend`:

    * **Unavailability is not failure.**  A self-hosted port whose model is
      not loaded answers 503 or refuses the connection outright.  That is
      ``model_unavailable``, so the engine parks the call and retries later
      rather than burning an attempt or halting the endpoint.
    * **Thinking models talk to themselves.**  ``<think>...</think>`` blocks
      are stripped before the answer object is located, since a scratchpad
      routinely contains draft JSON that would otherwise be parsed as the
      answer.

    This backend never manages a model's lifecycle.  The only request it will
    ever send to a ``/service/`` path is one optional ``GET /service/status``
    from :meth:`models`, and only when the endpoint declares
    ``supervisor: true``; it never starts or stops anything.
    """

    kind = "openai"

    @property
    def control_url(self) -> str:
        """The port's control plane: the base URL without the trailing ``/v1``."""
        return re.sub(r"/v1$", "", self.base_url)

    @property
    def status_url(self) -> str:
        """The read-only lifecycle endpoint for a supervised port."""
        return f"{self.control_url}/service/status"

    def _bearer(self) -> Tuple[Optional[str], Optional[LLMError]]:
        """An explicit key, else the key named by ``api_key_env``, else ``EMPTY``.

        vLLM requires the header to be present but ignores its value unless it
        was started with ``--api-key``, and ``EMPTY`` is the convention every
        OpenAI client uses for that case.  There is therefore no
        missing-credential failure mode here.
        """
        explicit = self.config.get("api_key")
        if explicit:
            return str(explicit), None
        env_name = self.config.get("api_key_env")
        if env_name:
            from_env = (os.environ.get(str(env_name)) or "").strip()
            if from_env:
                return from_env, None
        return "EMPTY", None

    def _clean_content(self, content: str) -> str:
        """Drop ``<think>...</think>`` reasoning blocks."""
        return strip_think(content)

    def _unavailable(self, detail: str) -> LLMError:
        """Build the ``model_unavailable`` error, naming where to look."""
        where = f"; its lifecycle state is at {self.status_url}" if self.supervisor else ""
        return ModelUnavailable(
            f"{self.name}: {self.base_url} is not serving a model right now "
            f"({detail}){where}. The call waits rather than fails; this backend does "
            "not start models."
        ).to_error()

    def _connect_error(self, exc: BaseException) -> LLMError:
        """A refused connection means the port is down, i.e. not serving."""
        return self._unavailable(
            f"connection refused: {type(exc).__name__}: {redact(str(exc))}"
        )

    def _status_error(self, response: httpx.Response) -> LLMError:
        """503 means "model not loaded"; everything else is the shared mapping."""
        if response.status_code == 503:
            excerpt = redact(response.text[:BODY_EXCERPT_CHARS])
            return self._unavailable(f"HTTP 503: {excerpt!r}")
        return super()._status_error(response)

    async def _note_listing_failure(self) -> None:
        """Log the port's lifecycle state after ``/models`` failed.

        A read-only ``GET`` of the status endpoint, only for a supervised
        port, purely so the log says *why* the listing failed (starting,
        stopped, failed) instead of just that it did.  This is the one and
        only ``/service/`` request this backend makes anywhere.
        """
        if not self.supervisor:
            return None
        try:
            response = await self.client.get(self.status_url)
            logger.info(
                "council openai backend %s: %s reports %s",
                self.name,
                self.status_url,
                redact(response.text[:BODY_EXCERPT_CHARS]),
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics never fail a call
            logger.info(
                "council openai backend %s: control plane %s is unreachable too (%s)",
                self.name,
                self.status_url,
                redact(f"{type(exc).__name__}: {exc}"),
            )
        return None
