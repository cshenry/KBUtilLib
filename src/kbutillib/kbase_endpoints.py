"""KBase service endpoint URL helpers.

Centralizes all KBase environment-to-URL mapping so that every module
resolves endpoints the same way.  The only public entry points are
:func:`base_url`, :func:`service_url`, and :func:`narrative_url`.
"""

from typing import Optional
from urllib.parse import urlparse

# ── environment → base URL mapping ──────────────────────────────────────────

_BASE_URLS = {
    "prod":   "https://kbase.us/services",
    "appdev": "https://appdev.kbase.us/services",
    "ci":     "https://ci.kbase.us/services",
}

_NARRATIVE_URLS = {
    "prod":   "https://narrative.kbase.us",
    "appdev": "https://appdev.kbase.us",
    "ci":     "https://ci.kbase.us",
}

# ── well-known service suffixes (relative to base URL) ──────────────────────

_SERVICE_SUFFIXES = {
    "workspace":      "/ws",
    "ws":             "/ws",
    "shock":          "/shock-api",
    "handle_service": "/handle_service",
    "ee2":            "/ee2",
    "auth":           "/auth/api/legacy/KBase/Sessions/Login",
    "catalog":        "/catalog",
    "service_wizard": "/service_wizard",
    "njs_wrapper":    "/njs_wrapper",
}


def base_url(env: str = "prod") -> str:
    """Return the services base URL for a KBase environment.

    Args:
        env: One of ``"prod"``, ``"appdev"``, or ``"ci"``.

    Returns:
        Base services URL (no trailing slash).

    Raises:
        ValueError: If *env* is not a recognised environment name.
    """
    env = env.lower()
    if env not in _BASE_URLS:
        raise ValueError(
            f"Unknown KBase environment {env!r}; "
            f"expected one of {sorted(_BASE_URLS)}"
        )
    return _BASE_URLS[env]


def service_url(service: str, env: str = "prod") -> str:
    """Return the full URL for a named KBase service.

    Args:
        service: Service name (e.g. ``"workspace"``, ``"ee2"``, ``"shock"``).
        env: KBase environment name.

    Returns:
        Fully-qualified service URL.

    Raises:
        ValueError: If *service* or *env* is not recognised.
    """
    service_lower = service.lower()
    if service_lower not in _SERVICE_SUFFIXES:
        raise ValueError(
            f"Unknown KBase service {service!r}; "
            f"expected one of {sorted(_SERVICE_SUFFIXES)}"
        )
    return base_url(env) + _SERVICE_SUFFIXES[service_lower]


def narrative_url(env: str = "prod") -> str:
    """Return the Narrative UI base URL for a KBase environment.

    Args:
        env: KBase environment name.

    Returns:
        Narrative base URL (no trailing slash).

    Raises:
        ValueError: If *env* is not recognised.
    """
    env = env.lower()
    if env not in _NARRATIVE_URLS:
        raise ValueError(
            f"Unknown KBase environment {env!r}; "
            f"expected one of {sorted(_NARRATIVE_URLS)}"
        )
    return _NARRATIVE_URLS[env]


def env_from_url(url: str) -> str:
    """Infer the KBase environment from an arbitrary service URL.

    Inspects the hostname to decide whether the URL points at *ci*,
    *appdev*, or *prod*.

    Args:
        url: Any KBase service URL.

    Returns:
        One of ``"ci"``, ``"appdev"``, or ``"prod"``.
    """
    hostname = urlparse(url).hostname or ""
    if hostname.startswith("ci."):
        return "ci"
    if hostname.startswith("appdev."):
        return "appdev"
    return "prod"
