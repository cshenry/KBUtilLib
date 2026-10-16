"""Re-export shim: the HTTP API type's shared plumbing moved to ``llm``.

M2.1 moved this module to
:mod:`kbutillib.domains.ai.llm.apis.http_common` so that one wrapper --
:mod:`kbutillib.domains.ai.llm` -- owns every way of reaching a model, and so
a non-council caller can use the same plumbing without importing the council.
Behaviour is unchanged; only ``complete()``'s argument is now an
``LLMRequest`` (a council ``CompletionRequest`` is still accepted, through
``llm.types.as_llm_request``).

This file exists so that every pre-M2 import path still resolves.  New code
should not import a provider class at all: call
``llm.get_api(kind, endpoint)`` or ``llm.acomplete(...)``.
"""

from __future__ import annotations

from ...llm.apis.http_common import (  # noqa: F401
    BODY_EXCERPT_CHARS,
    DEFAULT_TIMEOUT_S,
    ChatReply,
    HttpBackend,
    client_kwargs,
    extract_json_object,
    normalise_base_url,
    read_chat_envelope,
    redact,
    strip_fences,
    strip_think,
)

__all__ = [
    "BODY_EXCERPT_CHARS",
    "DEFAULT_TIMEOUT_S",
    "ChatReply",
    "HttpBackend",
    "client_kwargs",
    "extract_json_object",
    "normalise_base_url",
    "read_chat_envelope",
    "redact",
    "strip_fences",
    "strip_think",
]
