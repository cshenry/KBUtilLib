"""The ``alcf`` api type: ALCF Inference Endpoints (Argonne Leadership Computing Facility).

ALCF's endpoints are OpenAI-compatible for ``POST .../chat/completions`` and
answer HTTP 404 on ``GET {base_url}/models``; the roster lives one level up at
``{service_root}/list-endpoints``.  This backend is
:class:`~.http.OpenAICompatibleBackend` with four differences:

* the base URL is DERIVED from ``options.cluster`` / ``options.framework``
  (never parsed back out of a URL at call time);
* :meth:`AlcfBackend.models_report` reads the roster, not ``/models``;
* :meth:`AlcfBackend._bearer` runs a token source per call (cached for a
  short TTL), because ALCF's Globus tokens live 48 hours;
* :meth:`AlcfBackend._body` adds a ``response_format`` for a request that
  carries a JSON schema.

No new dependency: ``httpx`` is already required and ``subprocess`` is stdlib.

Endpoint options (all under the endpoint's ``options:`` block)::

    cluster:        required, e.g. "tara"
    framework:      required, e.g. "api"
    service_root:   default https://inference-api.alcf.anl.gov/resource_server
    token_command:  argv LIST run without a shell (default
                    ["alcf-tokens", "get-token", "inference"])   -- OR --
    token_import:   "module:callable", called with "inference"
    token_timeout_s: default 30
    token_ttl_s:     default 300
    request_response_format: default true

Options are fingerprinted into route identity and persisted, so
``token_command`` argv and ``token_import`` identifiers must carry NO
credentials.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import httpx

from ..types import AuthError, LLMError, LLMRequest
from .http import OpenAICompatibleBackend
from .http_common import _listing, normalise_base_url

logger = logging.getLogger(__name__)

__all__ = [
    "ALCF_SERVICE_ROOT",
    "ALCF_TOKEN_SCOPE",
    "AlcfBackend",
    "DEFAULT_TOKEN_COMMAND",
    "is_strict_compatible",
]

#: Root of ALCF's inference gateway.
ALCF_SERVICE_ROOT = "https://inference-api.alcf.anl.gov/resource_server"

#: The scope name passed to a ``token_import`` callable.
ALCF_TOKEN_SCOPE = "inference"

#: Resolved on PATH.  The recommended source.
DEFAULT_TOKEN_COMMAND = ("alcf-tokens", "get-token", ALCF_TOKEN_SCOPE)

DEFAULT_TOKEN_TIMEOUT_S = 30.0
DEFAULT_TOKEN_TTL_S = 300.0

_FIX = "run `alcf-tokens login inference`"

# ---------------------------------------------------------------------------
# Token cache (process-wide, shared across backend instances)
# ---------------------------------------------------------------------------

#: Monotonic clock; tests replace it.
_clock: Callable[[], float] = time.monotonic

_CACHE_LOCK = threading.Lock()
#: ``(kind, normalised source config) -> (token, expires_at)``.  Successful
#: tokens only.  NOT keyed on account or endpoint name: two endpoints on one
#: source share one login.
_CACHE: Dict[Tuple[str, Any], Tuple[str, float]] = {}


def clear_token_cache() -> None:
    """Forget every cached token (tests, and a deliberate re-login)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# The child for ``token_import``.  It sees NO state of the parent process:
# a fresh interpreter, the same environment and working directory.  Anything
# the callable prints is routed to stderr (which is discarded) so that only
# the token reaches stdout.
_IMPORT_CHILD = r"""
import contextlib, importlib, sys
spec, scope = sys.argv[1], sys.argv[2]
real_out = sys.stdout
try:
    module_name, _, attr = spec.partition(":")
    with contextlib.redirect_stdout(sys.stderr):
        module = importlib.import_module(module_name)
        fn = getattr(module, attr)
except BaseException:
    sys.exit(12)
try:
    with contextlib.redirect_stdout(sys.stderr):
        token = fn(scope)
except BaseException:
    sys.exit(14)
if not isinstance(token, str) or not token.strip():
    sys.exit(13)
real_out.write(token.strip())
real_out.flush()
"""

_IMPORT_FAILURES = {
    12: "import failed",
    13: "returned an empty or non-string value",
    14: "callable raised",
}


def _run_child(argv: List[str], timeout_s: float) -> Tuple[Optional[int], str, bool]:
    """Run *argv* without a shell; return ``(exit status, stdout, timed_out)``.

    ``stdin`` is ``DEVNULL`` so a prompt can never wait on the daemon's
    terminal.  On timeout the child's whole process group is killed and the
    child is reaped.  stderr is captured only to keep it off the daemon's
    terminal and is DISCARDED: it may hold an opaque token that
    :func:`redact` cannot recognise.  Exit status is ``None`` if the child
    could not be started.
    """
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return None, "", False
    try:
        out, _err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
        try:
            proc.communicate(timeout=5)  # reap
        except Exception:  # noqa: BLE001
            proc.wait()
        return None, "", True
    return proc.returncode, out or "", False


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _truthy(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


# ---------------------------------------------------------------------------
# Strict-schema compatibility
# ---------------------------------------------------------------------------

#: Constructs a strict-mode schema compiler does not accept.  A schema using
#: any of them is sent WITHOUT ``strict``.
_STRICT_FORBIDDEN = frozenset(
    {
        "allOf", "oneOf", "not", "if", "then", "else", "patternProperties",
        "propertyNames", "dependentSchemas", "dependentRequired",
        "unevaluatedProperties", "unevaluatedItems", "minProperties",
        "maxProperties", "$ref", "$dynamicRef", "prefixItems",
    }
)  # fmt: skip


def is_strict_compatible(schema: Any) -> bool:
    """True when *schema* can be sent with ``strict: true``.

    Every object level must set ``additionalProperties`` to ``false`` and the
    schema must use none of :data:`_STRICT_FORBIDDEN`.  A schema that leaves
    ``additionalProperties`` open or true (go_mf@4's META schema does so on
    purpose) is NOT strict-compatible.  Optional properties are allowed: this
    is a gate on what the server would refuse, not on OpenAI's all-required
    rule, which ALCF's backends do not apply.
    """
    if isinstance(schema, list):
        return all(is_strict_compatible(item) for item in schema)
    if not isinstance(schema, Mapping):
        return True
    if any(key in _STRICT_FORBIDDEN for key in schema):
        return False
    is_object = schema.get("type") == "object" or "properties" in schema
    if is_object and schema.get("additionalProperties") is not False:
        return False
    for key, value in schema.items():
        if key == "properties" and isinstance(value, Mapping):
            if not all(is_strict_compatible(sub) for sub in value.values()):
                return False
        elif key == "$defs" and isinstance(value, Mapping):
            if not all(is_strict_compatible(sub) for sub in value.values()):
                return False
        elif key in ("items", "anyOf") and not is_strict_compatible(value):
            return False
    return True


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------


class AlcfBackend(OpenAICompatibleBackend):
    """Seats served by ALCF Inference Endpoints.

    See the module docstring for the options.  Everything else (error
    classification, ``<think>`` stripping, halt causes for HTTP statuses) is
    inherited unchanged; in particular there is NO ``_status_error`` override,
    so an ALCF HTTP 401/403 is classified by the shared classifier.
    """

    kind = "alcf"

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        cfg = dict(config or {})
        options = cfg.get("options")
        opts: Mapping[str, Any] = options if isinstance(options, Mapping) else {}
        self.cluster = str(opts.get("cluster") or "").strip()
        self.framework = str(opts.get("framework") or "").strip()
        self.service_root = normalise_base_url(
            str(opts.get("service_root") or ALCF_SERVICE_ROOT)
        )
        if not cfg.get("base_url") and self.cluster and self.framework:
            cfg["base_url"] = (
                f"{self.service_root}/{self.cluster}/{self.framework}/v1"
            )
        super().__init__(cfg, transport=transport)

    # -- options ------------------------------------------------------------

    def _options(self) -> Mapping[str, Any]:
        options = self.config.get("options")
        return options if isinstance(options, Mapping) else {}

    @property
    def roster_url(self) -> str:
        return f"{self.service_root}/list-endpoints"

    # -- roster -------------------------------------------------------------

    async def models_report(self) -> Dict[str, Any]:
        """This endpoint's models from ``{service_root}/list-endpoints``.

        Overrides :meth:`models_report`, NOT only ``models``:
        ``probe_endpoint`` prefers ``models_report``, so overriding just
        ``models`` would leave the base class's ``{base_url}/models`` (HTTP 404
        on ALCF) in charge of readiness.  Outcomes are classified exactly as
        the base class does.  A cluster or framework absent from an answered
        roster is ``answered`` with an empty list.  Free; never raises.
        """
        try:
            return await self._alcf_roster()
        except Exception as exc:  # noqa: BLE001 - listing never fails a call
            return _listing("malformed", f"unexpected {type(exc).__name__}: {exc}")

    async def _alcf_roster(self) -> Dict[str, Any]:
        if not (self.cluster and self.framework):
            return _listing(
                "malformed",
                f"{self.name}: options.cluster and options.framework are not both set",
            )
        bearer, auth_error = self._bearer()
        if auth_error is not None:
            logger.warning(
                "council %s backend %s: cannot list models: %s",
                self.kind,
                self.name,
                auth_error.message,
            )
            return _listing("credential_rejected", auth_error.message)
        url = self.roster_url
        try:
            response = await self.client.get(url, headers=self._headers(bearer))
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            return self._listing_failed("unreachable", exc)
        except Exception as exc:  # noqa: BLE001
            return self._listing_failed("malformed", exc)

        status = response.status_code
        if not 200 <= status < 300:
            if status in (404, 405):
                outcome = "not_implemented"
            elif status in (401, 403):
                outcome = "credential_rejected"
            else:
                outcome = "http_error"
            return _listing(outcome, f"HTTP {status} from {url}")
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            return self._listing_failed("malformed", exc, "invalid JSON")

        clusters = payload.get("clusters") if isinstance(payload, Mapping) else None
        if not isinstance(clusters, Mapping):
            return _listing("malformed", f"{url} body has no 'clusters' mapping")
        cluster = clusters.get(self.cluster)
        frameworks = cluster.get("frameworks") if isinstance(cluster, Mapping) else None
        framework = (
            frameworks.get(self.framework) if isinstance(frameworks, Mapping) else None
        )
        listed = framework.get("models") if isinstance(framework, Mapping) else None
        if listed is None:
            return _listing(
                "answered",
                f"{self.cluster}/{self.framework} is not in the roster at {url}",
            )
        if not isinstance(listed, list):
            return _listing("malformed", f"{url} 'models' for this endpoint is not a list")
        names = [m for m in listed if isinstance(m, str)]
        if len(names) != len(listed):
            return _listing(
                "malformed",
                f"{len(listed) - len(names)} entr(ies) in the roster at {url} are not strings",
                [{"id": n} for n in names],
            )
        return _listing(
            "answered",
            f"{len(names)} model(s) listed for {self.cluster}/{self.framework}",
            [{"id": n} for n in names],
        )

    # -- credential ---------------------------------------------------------

    def _bearer(self) -> Tuple[Optional[str], Optional[LLMError]]:
        """Resolve an ALCF access token PER CALL from the configured source.

        Exactly one of ``options.token_command`` (an argv list, no shell) and
        ``options.token_import`` (``"module:callable"``, called with
        ``"inference"``); neither means the default command.  Both run in a
        CHILD PROCESS with ``stdin=DEVNULL`` and ``options.token_timeout_s``
        (default 30); on timeout the child is killed and reaped.  A
        ``token_import`` callable runs in a fresh interpreter
        (``sys.executable``) and sees NO state of this process.

        Successful tokens are cached in-process for ``options.token_ttl_s``
        (default 300), keyed on the source kind plus its normalised
        configuration and shared by every backend instance, behind one lock so
        concurrent refreshes run the source once.  The TTL bounds how often
        the tool RUNS, not how fresh its answer is: a token the tool returns
        just before expiry is cached for the full TTL.

        THIS IS SYNCHRONOUS (the ``Backend`` protocol says so), so a slow
        source freezes the event loop for up to ``token_timeout_s``.  That is
        a known cost, not something fixed here.

        Any failure returns ``(None, AuthError)`` naming the source identity
        (executable or module, never the full argv), the exit status or
        failure class, and the fix.  Raw child output and raw exception text
        are DISCARDED: :func:`redact` does not recognise an opaque token alone
        on a line, so quoting it, even redacted, could leak.  The error
        carries ``halt_cause=None``: nothing structured says why, and the
        commonest cause (the 30-day Globus re-auth lapse) needs a human, so it
        must never be a resumable cause.  ``token_command`` argv and
        ``token_import`` identifiers must carry no credentials -- options are
        fingerprinted and persisted.
        """
        opts = self._options()
        has_command = opts.get("token_command") is not None
        has_import = opts.get("token_import") is not None
        if has_command and has_import:
            return None, self._auth_error(
                "token source", "options.token_command and options.token_import are both set"
            )
        timeout_s = _as_float(opts.get("token_timeout_s"), DEFAULT_TOKEN_TIMEOUT_S)
        ttl_s = _as_float(opts.get("token_ttl_s"), DEFAULT_TOKEN_TTL_S)

        if has_import:
            spec = opts.get("token_import")
            module, sep, attr = str(spec).partition(":")
            if not (isinstance(spec, str) and sep and module.strip() and attr.strip()):
                return None, self._auth_error(
                    "token_import", "options.token_import is not 'module:callable'"
                )
            spec = f"{module.strip()}:{attr.strip()}"
            identity = module.strip()
            key: Tuple[str, Any] = ("import", spec)
            argv = [sys.executable, "-c", _IMPORT_CHILD, spec, ALCF_TOKEN_SCOPE]
        else:
            command = opts.get("token_command", list(DEFAULT_TOKEN_COMMAND))
            if not (
                isinstance(command, (list, tuple))
                and command
                and all(isinstance(a, str) and a for a in command)
            ):
                return None, self._auth_error(
                    "token_command", "options.token_command is not a non-empty list of strings"
                )
            argv = [str(a) for a in command]
            identity = os.path.basename(argv[0]) or argv[0]
            key = ("command", tuple(argv))

        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit is not None and _clock() < hit[1]:
                return hit[0], None
            status, out, timed_out = _run_child(argv, timeout_s)
            if timed_out:
                return None, self._auth_error(identity, f"timed out after {timeout_s:g}s")
            if status is None:
                return None, self._auth_error(identity, "could not be started")
            if status != 0:
                if key[0] == "import" and status in _IMPORT_FAILURES:
                    return None, self._auth_error(
                        identity, f"{_IMPORT_FAILURES[status]} (exit status {status})"
                    )
                return None, self._auth_error(identity, f"exited with status {status}")
            token = out.strip()
            if not token:
                return None, self._auth_error(identity, "printed no token (exit status 0)")
            if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in token):
                return None, self._auth_error(
                    identity, "printed output that is not a single-line token (exit status 0)"
                )
            _CACHE[key] = (token, _clock() + ttl_s)
            return token, None

    def _auth_error(self, identity: str, failure: str) -> LLMError:
        # Built from fixed text and the source identity only; no child output.
        return AuthError(
            f"{self.name}: ALCF token source {identity!r} failed: {failure}. "
            f"To fix: {_FIX}. No request was sent."
        ).to_error()

    # -- request body -------------------------------------------------------

    def _body(self, req: LLMRequest) -> Dict[str, Any]:
        """The base body, plus ``response_format`` for a request with a schema.

        On when ``options.request_response_format`` is true (default) and
        ``req.json_schema`` is set.  ``strict: true`` is added only when
        :func:`is_strict_compatible`.  ``schema_enforced`` stays False on every
        result: ALCF enforces the schema for some models and ignores it for
        others, so the engine still validates.  The schema name is a digest
        because :class:`LLMRequest` carries no query name or version.
        """
        body = super()._body(req)
        schema = req.json_schema
        if schema is not None and _truthy(
            self._options().get("request_response_format"), True
        ):
            from ..council.types import canonical_sha256  # lazy: avoid import cycle

            spec: Dict[str, Any] = {
                "name": "kbu_" + canonical_sha256(schema)[:24],
                "schema": schema,
            }
            if is_strict_compatible(schema):
                spec["strict"] = True
            body["response_format"] = {"type": "json_schema", "json_schema": spec}
        return body
