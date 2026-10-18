"""Re-export shim: the ARGO and OpenAI-compatible API types moved to ``llm``.

M2.1 moved this module to :mod:`kbutillib.domains.ai.llm.apis.http`.
See :mod:`kbutillib.domains.ai.council.backends.http_common` for why, and
``llm.get_api`` / ``llm.acomplete`` for what new code should call instead.
"""

from __future__ import annotations

from ...llm.apis.http import (  # noqa: F401
    ARGO_BASE_URL,
    ARGO_FIXED_TEMPERATURE_MODELS,
    ARGO_GEMINI_TOKEN_FLOOR,
    ARGO_GEMINI_TOKEN_FLOOR_MODELS,
    ARGO_NO_TEMPERATURE_MODELS,
    ARGO_REFUSAL_SIGNATURES,
    ARGO_USER_ENV,
    ArgoBackend,
    OpenAICompatibleBackend,
    argo_model_kwargs,
    argo_refusal_error,
)

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
