"""Loading and validating a council configuration.

A council configuration declares three tables and two scalars:

* ``endpoints`` -- WHERE models are reached (ARGO, an OpenAI-compatible
  server, the Claude CLI, the Codex CLI) plus the billing and concurrency
  facts that belong to the place rather than the model.
* ``models`` -- an ALIAS bound to one endpoint and one served name, with its
  canonical identity and generation params.
* ``profiles`` -- named seatings (proposers / critic / meta).
* ``default_profile`` and ``store``.

Two rules here exist because of specific ways this goes wrong:

1. **An alias named in a profile but absent from ``models`` is a
   ``ConfigError``.** It must never fall through to a same-named ARGO model:
   that turns a typo into a silent charge against a different model than the
   one the profile meant to seat.
2. **Canonical identity is required before a seat is taken.** It comes from
   the model's ``canonical`` key or from the versioned built-in
   :data:`DEFAULT_CANONICAL` table -- there is no
   ``"<endpoint>:<served_name>"`` fallback, because a synthesised identity
   makes two seats on the same real model look independent, which is exactly
   the comparison the council's independence invariant rests on.

Annotation note: pydantic evaluates field annotations, and this repo's CI
matrix still includes Python 3.9, so model fields use ``Optional[...]``
rather than PEP 604 unions (see the same note in :mod:`.types`).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Union

from pydantic import BaseModel, PrivateAttr, model_validator

from .types import (
    CLI_KINDS,
    ConfigError,
    Roster,
    RosterError,
    SeatSpec,
)

__all__ = [
    "BASE_URL_KINDS",
    "DEFAULT_CANONICAL",
    "DEFAULT_CANONICAL_BY_KIND",
    "DEFAULT_CANONICAL_VERSION",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_STORE_URL",
    "EndpointConfig",
    "ModelConfig",
    "ProfileConfig",
    "QueueConfig",
    "RateConfig",
    "CouncilConfig",
    "default_canonical",
    "expand_vars",
    "looks_like_credential",
    "load_council_config",
    "load_legacy_model_inference",
    "validate_static_headers",
]

logger = logging.getLogger(__name__)

#: Bump when :data:`DEFAULT_CANONICAL` changes meaning.  Jobs record it so a
#: later reader can tell which built-in identity table a verdict was decided
#: under; reuse across a version bump is the reader's decision, not ours.
DEFAULT_CANONICAL_VERSION = "1"

#: Built-in canonical identities, keyed by the alias (or served name) in
#: common use.  A canonical id is ``vendor/model``: it is what makes "the same
#: model via ARGO" and "the same model via the Claude CLI" compare equal.
DEFAULT_CANONICAL: Dict[str, str] = {
    "gemini35flash": "google/gemini-3.5-flash",
    "gemini31flashlite": "google/gemini-3.1-flash-lite",
    "gemini25flash": "google/gemini-2.5-flash",
    "gemini25pro": "google/gemini-2.5-pro",
    "gpt54": "openai/gpt-5.4",
    "gpt55": "openai/gpt-5.5",
    "claudesonnet5": "anthropic/claude-sonnet-5",
    "claudeopus45": "anthropic/claude-opus-4.5",
    "claudeopus5": "anthropic/claude-opus-5",
}

#: Identities that only hold on a particular endpoint kind.  ``sonnet`` and
#: ``opus`` are the Claude CLI's moving aliases for its current models; the
#: same two words mean nothing in particular anywhere else, so they are
#: resolved only on a ``claude_cli`` endpoint.
DEFAULT_CANONICAL_BY_KIND: Dict[str, Dict[str, str]] = {
    "claude_cli": {
        "sonnet": "anthropic/claude-sonnet-5",
        "opus": "anthropic/claude-opus-5",
    },
}

#: How many calls may be in flight per endpoint when the config is silent.
#: The CLIs are 1 because each call is a subprocess against a single
#: interactive subscription, not a pooled API.
DEFAULT_MAX_CONCURRENCY: Dict[str, int] = {
    "argo": 8,
    "openai": 16,
    "claude_cli": 1,
    "codex_cli": 1,
}

DEFAULT_STORE_URL = "sqlite:///~/.kbutillib/council/council.sqlite"

#: Built-in kinds whose endpoints are a URL, and so must declare ``base_url``.
#: Any OTHER kind -- a CLI, or a plug-in api type -- has no URL, which is why
#: it has to say how its answers may be reused (see
#: :meth:`EndpointConfig._check`).
BASE_URL_KINDS = ("argo", "openai")

_ENDPOINT_KEYS = frozenset(
    {
        "kind",
        "base_url",
        "executable",
        "user_env",
        "api_key",
        "api_key_env",
        "timeout",
        "extra_body",
        "public_url",
        "proxy",
        "billing",
        "account",
        "max_concurrency",
        "max_requests_per_minute",
        "max_total_tokens",
        "reuse_namespace",
        "deployment_revision",
        "options",
    }
)
_MODEL_KEYS = frozenset({"endpoint", "served_name", "canonical", "params", "verified"})
_PROFILE_KEYS = frozenset({"proposers", "critic", "meta", "allow_meta_overlap"})
_QUEUE_KEYS = frozenset(
    {
        "seats",
        "roles",
        "workers",
        "concurrency",
        "batch_size",
        "max_output_tokens_per_item",
    }
)
_TOP_KEYS = frozenset(
    {
        "endpoints",
        "models",
        "profiles",
        "queues",
        "rates",
        "default_profile",
        "store",
        "user",
        "query_dirs",
        "cli_temp_root",
    }
)

#: Keys one ``rates`` entry may carry (see :class:`RateConfig`).
_RATE_KEYS = frozenset(
    {
        "input_per_mtok",
        "output_per_mtok",
        "currency",
        "provenance",
        "is_floor",
    }
)

#: The batchable backends: one invocation may answer several items at once.
#: ``QueueConfig.batch_size > 1`` is refused for any seat whose endpoint kind
#: is not one of these (see :meth:`CouncilConfig._validate_queues`).
_BATCHABLE_KINDS = CLI_KINDS

_CLI_DEFAULT_EXECUTABLE = {"claude_cli": "claude", "codex_cli": "codex"}

# ${VAR} and ${VAR:-default}
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Keys whose VALUE is a credential.  Matched by exact name or by suffix, NOT
# by substring: a substring test on "token" also eats ``max_total_tokens``,
# which is a budget and belongs in a snapshot.  ``*_env`` keys name an
# environment variable rather than holding its contents, so they are not
# secrets and are kept -- a snapshot has to stay diagnosable.
_SECRET_EXACT = frozenset(
    {
        "api_key",
        "apikey",
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "credentials",
        "authorization",
    }
)
_SECRET_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_key",
    "_password",
    "_passwd",
    "_secret",
    "_token",
    "_credential",
    "_credentials",
)


#: Prefixes that only ever start a real credential.  Short, concrete and
#: vendor-specific on purpose: the question this answers is "has somebody
#: pasted a live key into a config file", and a heuristic wide enough to
#: catch ``api_key: EMPTY`` (vLLM's documented placeholder) or
#: ``api_key: REDACTED`` (how a config is shared) would refuse configurations
#: that are perfectly correct.
_CREDENTIAL_PREFIXES = (
    "sk-",
    "sk_",
    "pk-",
    "pk_",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "github_pat_",
    "glpat-",
    "xoxb-",
    "xoxp-",
    "xapp-",
    "AKIA",
    "ASIA",
    "ya29.",
    "AIza",
    "hf_",
    "Bearer ",
    "eyJ",  # the base64 of a JWT header
)

#: The other shape a credential comes in: one long opaque token.  32 is the
#: shortest length any of the vendors above issues, and the character class
#: excludes ``:`` and whitespace so a URL or a sentence cannot match.
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-+/=.]{32,}$")


def looks_like_credential(value: Any) -> bool:
    """Does *value* look like a live credential rather than a placeholder?

    Used to refuse a config that carries a secret's VALUE under a key that
    should only ever carry an environment variable's NAME (review round 3).
    Deliberately a value heuristic and not a key rule: ``api_key: EMPTY`` is
    vLLM's own placeholder, ``api_key: REDACTED`` is how a config gets shared
    for diagnosis, and refusing either would break working setups while
    protecting nobody.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if text.startswith(_CREDENTIAL_PREFIXES):
        return True
    return bool(_OPAQUE_TOKEN_RE.match(text))


def _refuse_credential_values(section: str, name: str, value: Any, path: str = "") -> None:
    """Raise :class:`ConfigError` for a credential-shaped value in *value*.

    Walks the whole entry, ``options`` included, because a plug-in field is
    exactly where somebody would put a key without thinking about it.  A
    ``*_env`` key is fine at any depth: it names a variable, and that is the
    whole point of the rule it is the answer to.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            here = f"{path}{key}"
            if (
                not lowered.endswith("_env")
                and (lowered in _SECRET_EXACT or lowered.endswith(_SECRET_SUFFIXES))
                and looks_like_credential(item)
            ):
                raise ConfigError(
                    f"{section} {name!r} sets {here!r} to what looks like a live "
                    "credential. A council configuration holds the NAME of an "
                    f"environment variable, never a secret's value: use "
                    f"{key}_env: <VARIABLE NAME> instead. (The store keeps a "
                    "snapshot of this configuration so a verdict can be "
                    "explained months later, and that snapshot must never be "
                    "able to hold a key.)"
                )
            _refuse_credential_values(section, name, item, f"{here}.")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _refuse_credential_values(section, name, item, f"{path}{index}.")


#: Header names an endpoint's ``options.headers`` may never set, matched by
#: exact name and case-insensitively.  Every one of them either IS the
#: credential channel or is routinely used as one, and the council has
#: exactly one credential channel: ``api_key_env``, which names an
#: environment variable rather than holding its contents.
_REFUSED_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "api-key",
    }
)

#: ... and the fragments that make any OTHER name a credential carrier.
#: Substring rather than suffix here, unlike :data:`_SECRET_SUFFIXES`: a
#: header name is freely invented by a gateway (``X-Gateway-Token-V2``) and
#: there is no budget-shaped counterexample among header names the way
#: ``max_total_tokens`` is one among config keys.
_REFUSED_HEADER_FRAGMENTS = ("token", "secret", "password", "key")

#: A field name as RFC 9110 defines it: one or more ``tchar``.  A name with a
#: space, a colon or a newline in it is how a header injection is spelled, so
#: it is refused at load rather than handed to ``httpx``.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def validate_static_headers(endpoint: str, raw: Any) -> Dict[str, str]:
    """Validate an endpoint's ``options.headers`` and return it as ``str: str``.

    Static headers exist for gateways that route on a non-secret header --
    the Genesis Model Access Gateway picks a provider from
    ``X-AmSC-MAG-Provider`` -- and they are sent on EVERY request an
    OpenAI-compatible endpoint makes.  Two things follow, and both are
    enforced here rather than at call time, because a config that cannot work
    must fail before any token is spent:

    * **They are non-secret.**  A name on :data:`_REFUSED_HEADER_NAMES`, or
      one containing any of :data:`_REFUSED_HEADER_FRAGMENTS`, is refused
      naming the endpoint, and so is a VALUE that
      :func:`looks_like_credential` recognises.  A credential goes through
      ``api_key_env`` and nowhere else -- the store keeps a snapshot of this
      configuration, and the route fingerprint hashes ``options`` verbatim,
      so a secret put here would be persisted in both.
    * **They are well-formed.**  Names must be RFC 9110 field names and
      values must already be strings; a config is not the place to discover
      that an integer or a newline reached the request line.

    Args:
        endpoint: the endpoint's name, for the error message.
        raw: whatever the config put under ``options.headers``.

    Returns:
        The headers as a plain ``{str: str}`` dict.  ``{}`` when *raw* is
        ``None`` or empty.

    Raises:
        ConfigError: *raw* is not a mapping, or any name or value is unusable.
    """
    if not raw:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"endpoint {endpoint!r} sets options.headers to a "
            f"{type(raw).__name__}; it must be a mapping of header name to "
            "string value."
        )

    headers: Dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)
        if not _HEADER_NAME_RE.match(name):
            raise ConfigError(
                f"endpoint {endpoint!r} sets options.headers entry {name!r}, "
                "which is not a valid HTTP header name (RFC 9110 allows only "
                "letters, digits and !#$%&'*+-.^_`|~)."
            )
        lowered = name.lower()
        if lowered in _REFUSED_HEADER_NAMES or any(
            fragment in lowered for fragment in _REFUSED_HEADER_FRAGMENTS
        ):
            raise ConfigError(
                f"endpoint {endpoint!r} sets options.headers entry {name!r}, "
                "which is a credential header. options.headers is for "
                "NON-SECRET routing headers only (it is hashed into the route "
                "fingerprint and kept in the store's config snapshot); the "
                "credential goes through api_key_env: <VARIABLE NAME>, which "
                "becomes the Authorization header and cannot be overridden "
                "from here."
            )
        if not isinstance(value, str):
            raise ConfigError(
                f"endpoint {endpoint!r} sets options.headers entry {name!r} to a "
                f"{type(value).__name__}; a header value must be a string "
                "(quote it in YAML if it looks like a number)."
            )
        # The name rule above cannot catch a gateway's own invented spelling
        # (``X-Forwarded-Auth``), so the VALUE is screened too, by the same
        # heuristic :func:`_refuse_credential_values` uses.  That function
        # only screens values under credential-SHAPED key names, which a
        # header name is not obliged to be.
        if looks_like_credential(value):
            raise ConfigError(
                f"endpoint {endpoint!r} sets options.headers entry {name!r} to "
                "what looks like a live credential. options.headers is for "
                "NON-SECRET routing headers only (it is hashed into the route "
                "fingerprint and kept in the store's config snapshot); the "
                "credential goes through api_key_env: <VARIABLE NAME>."
            )
        headers[name] = value
    return headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def expand_vars(value: Any) -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` in every string in *value*.

    Walks dicts and lists.  An unset variable with no default expands to the
    empty string, matching shell behaviour -- that is what lets
    ``proxy: ${KBU_ARGO_PROXY:-}`` mean "no proxy unless the operator sets
    one" in the shipped default config.
    """
    if isinstance(value, str):

        def _sub(match: "re.Match[str]") -> str:
            name, default = match.group(1), match.group(2)
            env = os.environ.get(name)
            if env is not None:
                return env
            if default is not None:
                return default
            logger.debug("council config: ${%s} is unset and has no default", name)
            return ""

        return _VAR_RE.sub(_sub, value)
    if isinstance(value, Mapping):
        return {k: expand_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_vars(v) for v in value]
    return value


def _warn_unknown(section: str, name: str, raw: Mapping[str, Any], known: frozenset) -> Dict[str, Any]:
    """Drop keys of *raw* that are not in *known*, with a logged warning.

    Unknown keys are ignored rather than rejected so a config written for a
    newer KBUtilLib still loads on an older one.
    """
    kept: Dict[str, Any] = {}
    for key, value in raw.items():
        if key in known:
            kept[key] = value
        else:
            logger.warning(
                "council config: ignoring unknown key %r in %s %r", key, section, name
            )
    return kept


def default_canonical(
    alias: str,
    served_name: Optional[str] = None,
    endpoint_kind: Optional[str] = None,
) -> Optional[str]:
    """Look an alias (or served name) up in the built-in identity tables.

    Tries the kind-specific table first so ``sonnet`` on a ``claude_cli``
    endpoint resolves, then the global table.  Returns ``None`` when nothing
    matches -- the caller decides whether that is fatal, which it is as soon
    as the alias is actually seated.
    """
    by_kind = DEFAULT_CANONICAL_BY_KIND.get(endpoint_kind or "", {})
    for key in (alias, served_name):
        if not key:
            continue
        if key in by_kind:
            return by_kind[key]
        if key in DEFAULT_CANONICAL:
            return DEFAULT_CANONICAL[key]
    return None


def _redact(value: Any) -> Any:
    """Recursively drop credential-bearing keys from *value*."""
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            name = str(key).lower()
            if not name.endswith("_env") and (
                name in _SECRET_EXACT or name.endswith(_SECRET_SUFFIXES)
            ):
                continue
            out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _resolve_store_url(url: Optional[str]) -> str:
    """Expand ``~`` in a sqlite store URL and refuse a synced location.

    A live SQLite database is a file plus a write-ahead log that a file
    syncer will happily upload mid-transaction and then "resolve" into a
    corrupt pair.  Dropbox is the syncer on this fleet, so a sqlite store
    inside a Dropbox directory is refused at load time rather than
    discovered as a corrupted council database later.
    """
    url = (url or DEFAULT_STORE_URL).strip()
    if not url.lower().startswith("sqlite"):
        return url

    scheme, sep, rest = url.partition(":///")
    if not sep:
        return url

    path_str = rest
    if path_str.startswith("/~"):
        path_str = path_str[1:]
    if path_str.startswith("~"):
        path_str = str(Path(path_str).expanduser())

    for part in Path(path_str).parts:
        lowered = part.lower()
        if lowered == "dropbox" or lowered.startswith("dropbox ("):
            raise ConfigError(
                f"refusing a sqlite council store inside a synced directory: {path_str!r} "
                f"(component {part!r}). Live SQLite WAL files corrupt under file sync; "
                "set `store:` to a path on local disk."
            )

    return f"{scheme}:///{path_str}"


def _resolve_query_dirs(raw: Any, source: str) -> List[str]:
    """Normalise ``query_dirs`` into a list of expanded path strings.

    A single string is accepted as a one-element list, because "one directory"
    is the common case and a config that spelled it as a scalar should not
    silently load nothing.  Expansion happens HERE rather than at use time so
    that :meth:`CouncilConfig.snapshot` -- which is what a job records and
    what a bug report quotes -- shows the paths that were really read.
    """
    from .queries.directories import expand_query_dirs

    if raw is None:
        return []
    entries = [raw] if isinstance(raw, (str, Path)) else raw
    if not isinstance(entries, (list, tuple)):
        raise ConfigError(
            f"query_dirs must be a list of directory paths, got "
            f"{type(raw).__name__} ({source})"
        )
    for entry in entries:
        if not isinstance(entry, (str, Path)):
            raise ConfigError(
                f"every query_dirs entry must be a path, got "
                f"{type(entry).__name__}: {entry!r} ({source})"
            )
    return [str(path) for path in expand_query_dirs(entries)]


# ---------------------------------------------------------------------------
# Config records
# ---------------------------------------------------------------------------


class EndpointConfig(BaseModel):
    """One place models are reached from, and the rules of that place.

    ``kind`` is a plain string rather than a ``Literal`` over the four
    shipped kinds (M1): since M2.1 an api type can arrive from another
    distribution through the ``kbutillib.llm_api_types`` entry-point group,
    and a Literal here would make a correctly installed plug-in unusable.
    The set of ACCEPTABLE kinds is therefore not a fact about this class --
    it is whatever is registered when the config is read, which is why the
    check lives in :func:`load_council_config`.
    """

    name: str
    kind: str
    base_url: Optional[str] = None
    executable: Optional[str] = None
    user_env: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    timeout: Optional[float] = None
    extra_body: Dict[str, Any] = {}
    public_url: Optional[str] = None
    proxy: Optional[str] = None
    billing: Literal["subscription", "api"] = "subscription"
    account: Optional[str] = None
    max_concurrency: Optional[int] = None
    #: Requests per minute this endpoint's ACCOUNT will accept, when that is
    #: a separate ceiling from ``max_concurrency``.  MAG enforces both and
    #: account-wide: 20 parallel requests AND 120 requests per minute
    #: (measured 2026-10-21 -- a config asking 24 parallel errored 427 of 500
    #: pilot items over 1,925 transport attempts).  Concurrency alone cannot
    #: express the second ceiling, and a wall-clock estimate that ignored it
    #: would promise a throughput the gateway refuses.  ``None`` means "no
    #: separate per-minute limit known", not "unlimited".
    max_requests_per_minute: Optional[float] = None
    max_total_tokens: Optional[int] = None
    reuse_namespace: Optional[str] = None
    deployment_revision: str = ""
    #: Fields a PLUG-IN api type defines for itself.  Preserved verbatim and
    #: handed to its factory, because this module cannot know what an api type
    #: nobody here wrote needs.  Non-secret by contract (a credential belongs
    #: in a ``*_env`` key, enforced in :func:`load_council_config`), and part
    #: of the route fingerprint, because an option that changes the answer has
    #: to change the identity of the route that produced it.
    #:
    #: One option is understood by a BUILT-IN kind: ``headers``, a mapping of
    #: static non-secret request headers an ``openai`` endpoint sends on every
    #: request (see :func:`validate_static_headers`).
    options: Dict[str, Any] = {}

    @model_validator(mode="after")
    def _check(self) -> "EndpointConfig":
        """Fill per-kind defaults and refuse combinations that cannot work."""
        # Checked for every kind, not only ``openai`` (which is the one kind
        # that SENDS them today): the rule being enforced is "no credential
        # in options", and that holds wherever the option was written.
        validate_static_headers(self.name, self.options.get("headers"))

        if self.kind in CLI_KINDS:
            if self.billing == "api":
                raise ConfigError(
                    f"endpoint {self.name!r} is {self.kind} with billing: api. "
                    "v1 drives the CLIs headless against an interactive subscription only; "
                    "there is no API-billed CLI path, so this would silently bill nothing "
                    "or bill the wrong account."
                )
            if self.kind == "codex_cli" and self.max_total_tokens is not None:
                raise ConfigError(
                    f"endpoint {self.name!r} sets max_total_tokens, but the Codex CLI "
                    "reports no token usage. An unmeasurable budget is unenforceable; "
                    "drop the key or use max_concurrency instead."
                )
            if not self.executable:
                self.executable = _CLI_DEFAULT_EXECUTABLE[self.kind]
        elif self.kind in BASE_URL_KINDS:
            if not self.base_url:
                raise ConfigError(
                    f"endpoint {self.name!r} is {self.kind} but declares no base_url"
                )
        elif not self.base_url and not self.reuse_namespace:
            # A plug-in kind with no URL.  ``default_reuse_namespace`` has
            # nothing to build a namespace from -- it would return the same
            # string for every such endpoint -- so two unrelated deployments
            # would share one answer cache and reuse each other's votes.  The
            # two built-in CLI kinds are exempt above because their namespace
            # is "<kind>:<served model>", which is a real identity.
            raise ConfigError(
                f"endpoint {self.name!r} is {self.kind}, which declares no base_url, "
                "so it must declare reuse_namespace: a reuse namespace is how two "
                "users' identical questions are recognised as the same question, and "
                "an endpoint with neither a URL nor a declared namespace would share "
                "one with every other endpoint of its kind."
            )

        if self.max_concurrency is None:
            self.max_concurrency = DEFAULT_MAX_CONCURRENCY.get(self.kind, 4)
        return self


class ModelConfig(BaseModel):
    """An alias bound to one endpoint and one served model name."""

    alias: str
    endpoint: str
    served_name: str = ""
    canonical: Optional[str] = None
    params: Dict[str, Any] = {}
    verified: bool = True

    @model_validator(mode="after")
    def _default_served_name(self) -> "ModelConfig":
        """Default the served name to the alias (the common case)."""
        if not self.served_name:
            self.served_name = self.alias
        return self


class ProfileConfig(BaseModel):
    """A named seating.

    ``proposers`` is normalised to ``{seat_name: alias}``: the list form in
    YAML yields ``proposer_1..n``, and the mapping form keeps the author's own
    seat names so overrides can address them.
    """

    name: str
    proposers: Dict[str, str] = {}
    critic: str
    meta: str
    allow_meta_overlap: bool = False


class QueueConfig(BaseModel):
    """One named worker queue: the seats it serves and its concurrency budget.

    Declaring ``queues`` on :class:`CouncilConfig` switches the engine from
    one worker process per store to one worker process **per queue**: a
    queue claims calls only for its own ``seats`` (and ``roles``, when that
    narrows it further), and ``concurrency`` is the number of calls that
    child is allowed to have in flight -- not the account's full
    ``max_concurrency`` (see :meth:`CouncilConfig._validate_queues` for why
    that distinction is load-bearing).

    ``batch_size`` lives here and nowhere else: there is no seat-level
    override, so a queue's value applies independently to each of its seats.
    It defaults to 1 (unbatched); 20 is a value an operator writes explicitly
    for a Claude queue, not a default this class picks for them.
    """

    seats: List[str]
    roles: Optional[List[str]] = None
    workers: int = 1
    concurrency: int
    batch_size: int = 1
    max_output_tokens_per_item: Optional[int] = None


class RateConfig(BaseModel):
    """What one GATEWAY charges per million tokens, and where that came from.

    OPT-IN AND NEVER GUESSED.  A gateway with no entry here is reported
    ``unpriced`` by the quote stage -- not priced at the vendor's public list
    rate -- and the reason is measured, not cautious: every ARGO and MAG
    usage, billing, pricing and quota path 404s, MAG's ``/models/<id>``
    returns 401 "Only proxy admin", and a $350/month ARGO cap was exhausted
    in a week by ordinary traffic, which cannot happen at pass-through.  So
    ARGO's effective multiple over list is unknown and above 1, and a
    list-derived figure is a FLOOR.  Presenting one as a cost is an error
    this project has already made once.

    ``provenance`` is therefore REQUIRED: a dollar figure with no stated
    source is the failure mode this class exists to prevent, so a rate that
    does not say where it came from is refused at load time rather than
    published unlabelled.  ``is_floor`` marks a rate known to understate (a
    vendor list price standing in for a gateway's own multiple); the quote
    carries it through to every figure derived from this rate.
    """

    #: Dollars per million INPUT tokens.  Input is 86% of all tokens on the
    #: measured ``function_to_go`` run, so this is the field that matters.
    input_per_mtok: float
    #: Dollars per million OUTPUT tokens.
    output_per_mtok: float
    currency: str = "USD"
    #: Where this number came from, in the operator's own words.  Required.
    provenance: str
    #: True when this rate is known to be a LOWER BOUND on what is charged.
    is_floor: bool = False

    @model_validator(mode="after")
    def _check(self) -> "RateConfig":
        """Refuse a rate that is unlabelled or negative."""
        if not str(self.provenance).strip():
            raise ConfigError(
                "a rate must declare its `provenance`: an unlabelled dollar "
                "figure is exactly the error the quote stage refuses to make. "
                "Say where the number came from (an invoice, a balance delta, "
                "a vendor list page) and whether it is a floor."
            )
        for field in ("input_per_mtok", "output_per_mtok"):
            if float(getattr(self, field)) < 0:
                raise ConfigError(
                    f"rate {field} is {getattr(self, field)!r}; a price per "
                    "million tokens cannot be negative"
                )
        return self


class CouncilConfig(BaseModel):
    """A validated council configuration.

    Loaded by :func:`load_council_config`; everything downstream reads seats
    out of :meth:`roster` and never re-interprets the raw YAML.
    """

    endpoints: Dict[str, EndpointConfig] = {}
    models: Dict[str, ModelConfig] = {}
    profiles: Dict[str, ProfileConfig] = {}
    #: Opt-in queue partitioning (see :class:`QueueConfig`). Empty means "one
    #: worker, every seat" -- today's behaviour, unchanged (point 6 of the
    #: design: this section is additive).
    queues: Dict[str, QueueConfig] = {}
    #: Per-GATEWAY token prices, keyed by the gateway a quote groups spend
    #: under: an endpoint's ``account`` when it declares one, else the
    #: endpoint name.  Empty is the normal state and means every gateway is
    #: reported ``unpriced`` (see :class:`RateConfig`).
    rates: Dict[str, RateConfig] = {}
    default_profile: Optional[str] = None
    store: str = DEFAULT_STORE_URL
    #: Root directory a CLI backend writes its prompt file under (story 17:
    #: delivering the Claude CLI's prompt by file rather than by stdin, so a
    #: busy event loop cannot cause a 3-second stdin timeout). Declared here
    #: only -- consumed by the CLI backend, not by this module.
    cli_temp_root: Optional[str] = None
    #: Per-queue account charged at load time by :meth:`_validate_queues`,
    #: keyed by queue name. Not a config field: it is what makes
    #: :meth:`check_roster_against_queues` able to refuse, at submission
    #: time, a roster/override whose seat resolves to an account this
    #: queue's budget was never checked against.
    _queue_accounts: Dict[str, str] = PrivateAttr(default_factory=dict)
    #: PROVENANCE label written to ``jobs.submitted_by``, nothing more.  It
    #: grants no access and gates nothing: there is no identity in the
    #: council (review round 2), budgets and the queue are the store's and
    #: not a person's, so changing this affects only the label on jobs
    #: submitted afterwards.  Empty means "use the OS user".
    user: Optional[str] = None
    #: Directories of query-type YAML files, loaded on every store open and
    #: by ``kbu council db init`` (see
    #: :mod:`.queries.directories`).  Stored expanded (``~`` and ``${VAR}``)
    #: so every consumer sees the same absolute paths.  NOTHING is ever
    #: written into one: they are expected to be in Dropbox, where a sidecar
    #: file would sync to the whole fleet.
    query_dirs: List[str] = []
    source: str = ""

    # -- accessors ----------------------------------------------------------

    def endpoint(self, name: str) -> Dict[str, Any]:
        """Return endpoint *name* as a plain dict.

        Dict rather than the model because this is what the hash helpers in
        :mod:`.types` and the backends consume.
        """
        try:
            return self.endpoints[name].model_dump()
        except KeyError:
            raise ConfigError(
                f"unknown endpoint {name!r}; declared: {sorted(self.endpoints)}"
            ) from None

    def snapshot(self) -> Dict[str, Any]:
        """Return the configuration with every credential removed.

        This is what a job stores, so a verdict can be explained months later
        without the store ever holding a key.  ``*_env`` keys survive (they
        name a variable, not a secret), which is what makes a snapshot useful
        for diagnosis.
        """
        data = self.model_dump()
        data["default_canonical_version"] = DEFAULT_CANONICAL_VERSION
        return _redact(data)

    # -- seating ------------------------------------------------------------

    def roster(
        self,
        profile: Optional[str] = None,
        overrides: Optional[Mapping[str, Union[str, Sequence[str]]]] = None,
    ) -> Roster:
        """Resolve a profile (plus *overrides*) into a validated :class:`Roster`.

        *overrides* is keyed by SEAT NAME, not by model: ``{"critic": alias}``,
        ``{"proposer_2": alias}``.  The one special key is ``"proposers"``,
        whose list value replaces the proposer bench entirely.

        Raises:
            ConfigError: unknown profile, or an override naming a seat the
                profile does not have.
            RosterError: a seated alias has no canonical identity, or the
                resulting seating is not independent.
        """
        name = profile or self.default_profile
        if not name:
            raise ConfigError("no profile requested and no default_profile is configured")
        prof = self.profiles.get(name)
        if prof is None:
            raise ConfigError(
                f"unknown profile {name!r}; configured: {sorted(self.profiles)}"
            )

        bench: Dict[str, str] = dict(prof.proposers)
        critic_alias = prof.critic
        meta_alias = prof.meta

        remaining = dict(overrides or {})
        if "proposers" in remaining:
            value = remaining.pop("proposers")
            aliases = [value] if isinstance(value, str) else list(value)
            bench = {f"proposer_{i}": alias for i, alias in enumerate(aliases, 1)}
        for key, value in remaining.items():
            if not isinstance(value, str):
                raise ConfigError(
                    f"override {key!r} must name a single model alias, got {value!r}"
                )
            if key == "critic":
                critic_alias = value
            elif key == "meta":
                meta_alias = value
            elif key in bench:
                bench[key] = value
            else:
                raise ConfigError(
                    f"override key {key!r} is not a seat in profile {name!r}; "
                    f"seats are {sorted(bench) + ['critic', 'meta']}"
                )

        proposers = [self._seat(seat, alias) for seat, alias in bench.items()]
        return Roster(
            proposers=proposers,
            critic=self._seat("critic", critic_alias),
            meta=self._seat("meta", meta_alias),
            allow_meta_overlap=prof.allow_meta_overlap,
        )

    def _seat(self, seat_name: str, alias: str) -> SeatSpec:
        """Resolve one ``(seat_name, alias)`` pair into a :class:`SeatSpec`."""
        model = self.models.get(alias)
        if model is None:
            raise ConfigError(
                f"seat {seat_name!r} names model alias {alias!r}, which is not declared "
                f"in `models`; declared: {sorted(self.models)}"
            )
        endpoint = self.endpoints.get(model.endpoint)
        if endpoint is None:
            raise ConfigError(
                f"model {alias!r} points at endpoint {model.endpoint!r}, which is not declared"
            )

        canonical = model.canonical or default_canonical(
            alias, model.served_name, endpoint.kind
        )
        if not canonical:
            raise RosterError(
                f"model alias {alias!r} (seat {seat_name!r}) has no canonical identity: "
                f"it declares no `canonical` and is not in the built-in table "
                f"(DEFAULT_CANONICAL v{DEFAULT_CANONICAL_VERSION}). Declare "
                f"canonical: <vendor>/<model> for it -- a council cannot check that two "
                "seats are different models without it."
            )

        params = dict(model.params)
        temperature = params.pop("temperature", 0)
        max_output_tokens = params.pop("max_output_tokens", None)
        legacy_max = params.pop("max_tokens", None)
        if max_output_tokens is None:
            max_output_tokens = legacy_max
        extra: Dict[str, Any] = {}
        if endpoint.extra_body:
            extra["extra_body"] = dict(endpoint.extra_body)
        extra.update(params)

        return SeatSpec(
            seat_name=seat_name,
            endpoint=endpoint.name,
            endpoint_kind=endpoint.kind,
            model=model.served_name,
            canonical_model=canonical,
            max_output_tokens=4000 if max_output_tokens is None else int(max_output_tokens),
            temperature=temperature,
            timeout_s=180.0 if endpoint.timeout is None else float(endpoint.timeout),
            extra=extra,
            verified=model.verified,
        )

    # -- queue partitioning ---------------------------------------------------

    def _account_for(self, endpoint_name: str) -> str:
        """The account a seat on *endpoint_name* charges concurrency against.

        Mirrors the worker's own fallback (``engine/worker.py``
        ``_build_limits``, around line 362): an endpoint that declares no
        ``account`` gets a synthetic one scoped to itself, so it never
        collides with another endpoint's budget.
        """
        endpoint = self.endpoints.get(endpoint_name)
        if endpoint is None or not endpoint.account:
            return f"endpoint:{endpoint_name}"
        return endpoint.account

    @model_validator(mode="after")
    def _validate_queues(self) -> "CouncilConfig":
        """Refuse a ``queues`` section that could over-subscribe an account.

        A no-op when ``queues`` is empty: queue mode is additive and opt-in
        (point 6 of the design) -- a config that declares none behaves
        exactly as it did before this validator existed.

        **A seat name does not bind to one account.** ``roster(profile,
        overrides)`` resolves seat names per job, and ``overrides["proposers"]``
        can replace the whole proposer bench, so the same seat name can reach a
        different account in two simultaneous jobs -- including a job
        persisted under a different, also-declared profile. Validating only
        ``default_profile`` would make the no-over-subscription promise
        false, so this resolves **every declared profile** (with no
        overrides -- overrides are a per-job, per-submission concern, which
        is exactly what :meth:`check_roster_against_queues` exists to police
        at submission/claim time) and charges each queue's full
        ``concurrency * workers`` reservation to every account any of those
        resolutions reaches.
        """
        if not self.queues:
            return self

        # -- no seat in two queues --------------------------------------
        seat_owner: Dict[str, str] = {}
        for qname, q in self.queues.items():
            for seat_name in q.seats:
                other = seat_owner.get(seat_name)
                if other is not None and other != qname:
                    raise ConfigError(
                        f"seat {seat_name!r} is declared in two queues, {other!r} and "
                        f"{qname!r}. A seat may belong to only one queue: the claim "
                        "protocol already prevents two workers claiming the same call, "
                        "but two queues on one seat would silently count that seat's "
                        "concurrency against its account twice."
                    )
                seat_owner[seat_name] = qname

        # -- resolve every declared profile, with no overrides -----------
        rosters: Dict[str, Roster] = {name: self.roster(name) for name in self.profiles}

        all_pairs: set = set()
        covered: set = set()
        queue_accounts: Dict[str, set] = {name: set() for name in self.queues}

        for roster in rosters.values():
            for role, seat_spec in roster.seats():
                pair = (seat_spec.seat_name, role)
                all_pairs.add(pair)
                qname = seat_owner.get(seat_spec.seat_name)
                if qname is None:
                    continue
                q = self.queues[qname]
                if q.roles is not None and role not in q.roles:
                    continue
                covered.add(pair)
                if q.batch_size > 1 and seat_spec.endpoint_kind not in _BATCHABLE_KINDS:
                    raise ConfigError(
                        f"queue {qname!r} sets batch_size={q.batch_size}, but seat "
                        f"{seat_spec.seat_name!r} is endpoint kind {seat_spec.endpoint_kind!r}, "
                        f"which is not a batchable backend (batchable kinds: {list(_BATCHABLE_KINDS)}). "
                        "batch_size > 1 is only valid for a seat reached through a CLI "
                        "endpoint."
                    )
                queue_accounts[qname].add(self._account_for(seat_spec.endpoint))

        # -- coverage is required, with no implicit fallback worker ------
        uncovered = sorted(all_pairs - covered)
        if uncovered:
            described = ", ".join(f"{seat_name!r} acting as {role!r}" for seat_name, role in uncovered)
            raise ConfigError(
                "queue mode requires exactly one queue to own every seat/role a "
                f"profile can submit; nothing covers: {described}. There is no "
                "implicit fallback worker -- add the seat to a queue's `seats` "
                "(and to `roles`, if that queue restricts them), or the job would "
                "silently never finish."
            )

        # -- every queue resolves to exactly one account -----------------
        for qname, accounts in queue_accounts.items():
            if len(accounts) > 1:
                raise ConfigError(
                    f"queue {qname!r} resolves to more than one account across the "
                    f"configured profiles: {sorted(accounts)}. A queue is one worker "
                    "process with one concurrency budget; split it into one queue per "
                    "account, or pin its seats so they cannot move accounts across "
                    "profiles."
                )

        # -- per account: sum(concurrency * workers) <= max_concurrency ---
        account_caps: Dict[str, int] = {}
        for endpoint in self.endpoints.values():
            account = endpoint.account or f"endpoint:{endpoint.name}"
            cap = int(endpoint.max_concurrency or 0)
            account_caps[account] = (
                cap if account not in account_caps else min(account_caps[account], cap)
            )

        account_sum: Dict[str, int] = {}
        account_queues: Dict[str, List[str]] = {}
        queue_accounts_final: Dict[str, str] = {}
        for qname, accounts in queue_accounts.items():
            if not accounts:
                continue
            (account,) = accounts
            queue_accounts_final[qname] = account
            contribution = self.queues[qname].concurrency * self.queues[qname].workers
            account_sum[account] = account_sum.get(account, 0) + contribution
            account_queues.setdefault(account, []).append(qname)

        for account, total in account_sum.items():
            cap = account_caps.get(account)
            if cap is not None and total > cap:
                queue_list = sorted(account_queues[account])
                raise ConfigError(
                    f"account {account!r} (max_concurrency {cap}) is over-subscribed: "
                    f"queues {queue_list} sum to concurrency {total}, which exceeds "
                    f"its cap of {cap}. Lower concurrency/workers on one of them, or "
                    "move a queue to a different account."
                )

        self._queue_accounts = queue_accounts_final
        return self

    def queue_account(self, queue: str) -> Optional[str]:
        """The account *queue* was validated against, or ``None``.

        :meth:`_validate_queues` already resolved every queue to exactly one
        account (an enforced invariant) and recorded it; this exposes that
        answer rather than letting a caller re-derive it from the queue's
        seats, which is the derivation that could disagree.

        The caller that needs it is the request pacer: a gateway's
        requests-per-minute is an ACCOUNT limit, the account's queue children
        are separate processes, and apportioning the rate between them means
        knowing which of them share one account (see
        ``daemon._queue_requests_per_minute``).
        """
        return self._queue_accounts.get(str(queue))

    def check_roster_against_queues(
        self, roster: Roster, *, job_id: Optional[str] = None
    ) -> None:
        """Refuse *roster* if it falls outside the validated queue set.

        This is the per-job rejection predicate point 3(e) of the design
        calls for: a roster or override assembled at submission time -- or
        read back for an existing job before claiming its calls -- whose
        ``(seat, account, backend)`` mapping was never covered by
        :meth:`_validate_queues` must be refused, with no mutation of any
        stored call. It is a no-op when ``queues`` is not configured (queue
        mode is opt-in).

        **This method is a predicate only; nothing in this module calls it.**
        Its natural call site is outside ``config.py`` -- in
        ``engine/worker.py``, at the top of ``submit()`` (before a job or its
        calls are written) and again in ``_claim()`` before an existing
        job's pending calls are claimed (see the module docstring there for
        the exact line numbers in this PRD phase). Wiring it in is out of
        scope for this task: see the work-record for ``t5-queue-config``.

        Args:
            roster: the fully resolved seating to check -- typically built
                from a submission's ``profile`` and ``overrides``.
            job_id: included in the message only, so the caller's log/refusal
                can name the job without this module knowing what a job is.

        Raises:
            ConfigError: a seat in *roster* is not covered by exactly one
                queue for the role it is acting in, or resolves to an
                account other than the one :meth:`_validate_queues` charged
                that queue's budget against.
        """
        if not self.queues:
            return
        job_part = f" (job {job_id!r})" if job_id else ""
        seat_owner: Dict[str, str] = {
            seat_name: qname for qname, q in self.queues.items() for seat_name in q.seats
        }
        for role, seat_spec in roster.seats():
            qname = seat_owner.get(seat_spec.seat_name)
            if qname is None:
                raise ConfigError(
                    f"seat {seat_spec.seat_name!r}{job_part} is not covered by any "
                    "configured queue; refusing rather than letting its calls sit "
                    "unclaimed by any worker process."
                )
            q = self.queues[qname]
            if q.roles is not None and role not in q.roles:
                raise ConfigError(
                    f"seat {seat_spec.seat_name!r}{job_part} is acting as {role!r}, but "
                    f"queue {qname!r} only serves roles {q.roles}; this roster is not "
                    "part of the validated queue set."
                )
            validated_account = self._queue_accounts.get(qname)
            account = self._account_for(seat_spec.endpoint)
            if validated_account is not None and account != validated_account:
                raise ConfigError(
                    f"seat {seat_spec.seat_name!r}{job_part} resolves to account "
                    f"{account!r}, but queue {qname!r} was validated against "
                    f"{validated_account!r}. An override that moves a seat to a "
                    "different account is exactly what config-load validation cannot "
                    "see -- refusing rather than silently over-subscribing the account."
                )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> Dict[str, Any]:
    """Load *path* as a YAML mapping."""
    import yaml

    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read council config {path}: {exc}") from exc
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"council config {path} must be a mapping, got {type(data).__name__}")
    return data


def _packaged_default() -> Dict[str, Any]:
    """Load the ``default_config.yaml`` shipped inside this package."""
    from importlib.resources import files

    resource = files(__package__).joinpath("default_config.yaml")
    import yaml

    data = yaml.safe_load(resource.read_text())
    return data or {}


def _user_overlay() -> Dict[str, Any]:
    """Read the ``ai_council`` section of the user's KBUtilLib config.

    Absent section, absent config file and an unreadable config all mean the
    same thing here -- no overlay -- because a fresh machine with no config at
    all must still get a working default council.
    """
    try:
        from ....core.shared_env_utils import SharedEnvUtils

        value = SharedEnvUtils().get_config_value("ai_council", default={})
    except Exception as exc:  # noqa: BLE001 - never let config discovery be fatal
        logger.warning("council config: could not read the user config section: %s", exc)
        return {}
    if not isinstance(value, dict):
        logger.warning(
            "council config: config section `ai_council` is %s, expected a mapping; ignoring",
            type(value).__name__,
        )
        return {}
    return value


def _merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    """Overlay a user config onto the packaged default.

    Merged per entry inside ``endpoints`` / ``models`` / ``profiles`` /
    ``queues``: the user's keys win within an entry they name, and entries
    they do not name are untouched.  Scalars are replaced outright.
    """
    merged: Dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in overlay.items():
        if key in ("endpoints", "models", "profiles", "queues") and isinstance(value, Mapping):
            section = dict(merged.get(key) or {})
            for name, entry in value.items():
                if isinstance(entry, Mapping) and isinstance(section.get(name), Mapping):
                    combined = dict(section[name])
                    combined.update(entry)
                    section[name] = combined
                else:
                    section[name] = entry
            merged[key] = section
        else:
            merged[key] = value
    return merged


def _normalise_proposers(name: str, raw: Any) -> Dict[str, str]:
    """Normalise a profile's ``proposers`` into ``{seat_name: alias}``."""
    if isinstance(raw, Mapping):
        return {str(seat): str(alias) for seat, alias in raw.items()}
    if isinstance(raw, (list, tuple)):
        return {f"proposer_{i}": str(alias) for i, alias in enumerate(raw, 1)}
    if isinstance(raw, str):
        return {"proposer_1": raw}
    raise ConfigError(
        f"profile {name!r}: proposers must be a list or a mapping, got {type(raw).__name__}"
    )


def _legacy_argo_endpoint(endpoints: Mapping[str, EndpointConfig]) -> Optional[EndpointConfig]:
    """Return the endpoint a legacy file's undeclared aliases belong to.

    Filipe Liu's ``model_inference.yaml`` names ARGO aliases in its profiles
    without declaring them in ``models`` -- the service looked them up live
    against ARGO's model list.  They all live on its one ``argo`` endpoint.
    """
    named = endpoints.get("argo")
    if named is not None and named.kind == "argo":
        return named
    argo = [ep for ep in endpoints.values() if ep.kind == "argo"]
    return argo[0] if len(argo) == 1 else None


def _require_registered_kind(name: str, kind: str) -> None:
    """Refuse an endpoint whose ``kind`` no installed api type answers to.

    Asked of the wrapper rather than of a list in this module, and asked
    AFTER entry-point discovery, so a kind that arrived from another
    distribution is accepted and a typo is still caught -- at load, before a
    token is spent, which is the whole reason this is not discovered at the
    first call.
    """
    from ...ai import llm

    registered = llm.api_types()
    if kind in registered:
        return
    raise ConfigError(
        f"endpoint {name!r} is kind {kind!r}, which no installed api type "
        f"answers to; registered kinds are {registered}. Declare one in the "
        f"{llm.ENTRY_POINT_GROUP!r} entry-point group, or call "
        "llm.register_api_type()."
    )


def load_council_config(
    path_or_dict: Optional[Union[str, Path, Mapping[str, Any]]] = None,
    *,
    legacy: Optional[bool] = None,
) -> CouncilConfig:
    """Load, expand, validate and return a :class:`CouncilConfig`.

    Args:
        path_or_dict: A path to a YAML file, an already-parsed mapping, or
            ``None`` to load the packaged default overlaid with the user's
            KBUtilLib config section ``ai_council`` (user keys win per
            endpoint / model / profile entry).
        legacy: Whether to apply the legacy ``model_inference.yaml``
            compatibility rules (endpoints default to kind ``openai``;
            profile aliases that are not declared in ``models`` are declared
            on the file's ARGO endpoint).  ``None`` auto-detects: a config
            with any endpoint that omits ``kind`` is a legacy file, since the
            native format always states it.  The design prompt did not say
            how legacy mode is selected; auto-detection on the missing-``kind``
            marker is used so that reading Filipe's real file needs no flag,
            while a native config keeps strict alias validation.

    Raises:
        ConfigError: the configuration cannot be used as written.
    """
    source = ""
    if path_or_dict is None:
        raw = _merge(_packaged_default(), _user_overlay())
        source = "packaged default + user ai_council section"
    elif isinstance(path_or_dict, Mapping):
        raw = dict(path_or_dict)
        source = "<dict>"
    else:
        path = Path(path_or_dict).expanduser()
        raw = _read_yaml(path)
        source = str(path)

    raw = expand_vars(raw)

    for key in list(raw):
        if key not in _TOP_KEYS:
            logger.warning("council config (%s): ignoring unknown top-level key %r", source, key)
            raw.pop(key)

    raw_endpoints = raw.get("endpoints") or {}
    raw_models = raw.get("models") or {}
    raw_profiles = raw.get("profiles") or {}
    if legacy is None:
        legacy = any(
            isinstance(entry, Mapping) and not entry.get("kind")
            for entry in raw_endpoints.values()
        )

    # -- endpoints ----------------------------------------------------------
    endpoints: Dict[str, EndpointConfig] = {}
    for name, entry in raw_endpoints.items():
        if not isinstance(entry, Mapping):
            raise ConfigError(f"endpoint {name!r} must be a mapping, got {type(entry).__name__}")
        fields = _warn_unknown("endpoint", name, entry, _ENDPOINT_KEYS)
        if not fields.get("kind"):
            if not legacy:
                raise ConfigError(f"endpoint {name!r} declares no kind")
            # Legacy: an endpoint with no kind is one of Filipe's local
            # OpenAI-compatible model_service containers.
            fields["kind"] = "openai"
        _refuse_credential_values("endpoint", name, fields)
        _require_registered_kind(name, str(fields["kind"]))
        endpoints[name] = EndpointConfig(name=name, **fields)

    # -- models -------------------------------------------------------------
    models: Dict[str, ModelConfig] = {}
    for alias, entry in raw_models.items():
        if entry is None:
            entry = {}
        if not isinstance(entry, Mapping):
            raise ConfigError(f"model {alias!r} must be a mapping, got {type(entry).__name__}")
        fields = _warn_unknown("model", alias, entry, _MODEL_KEYS)
        if not fields.get("endpoint"):
            raise ConfigError(f"model {alias!r} declares no endpoint")
        if fields["endpoint"] not in endpoints:
            raise ConfigError(
                f"model {alias!r} points at endpoint {fields['endpoint']!r}, which is not "
                f"declared; declared: {sorted(endpoints)}"
            )
        models[alias] = ModelConfig(alias=alias, **fields)

    # -- profiles -----------------------------------------------------------
    profiles: Dict[str, ProfileConfig] = {}
    for name, entry in raw_profiles.items():
        if not isinstance(entry, Mapping):
            raise ConfigError(f"profile {name!r} must be a mapping, got {type(entry).__name__}")
        fields = _warn_unknown("profile", name, entry, _PROFILE_KEYS)
        for required in ("critic", "meta"):
            if not fields.get(required):
                raise ConfigError(f"profile {name!r} declares no {required}")
        profiles[name] = ProfileConfig(
            name=name,
            proposers=_normalise_proposers(name, fields.get("proposers") or []),
            critic=fields["critic"],
            meta=fields["meta"],
            allow_meta_overlap=bool(fields.get("allow_meta_overlap", False)),
        )

    # -- queues ---------------------------------------------------------------
    raw_queues = raw.get("queues") or {}
    queues: Dict[str, QueueConfig] = {}
    for name, entry in raw_queues.items():
        if not isinstance(entry, Mapping):
            raise ConfigError(f"queue {name!r} must be a mapping, got {type(entry).__name__}")
        fields = _warn_unknown("queue", name, entry, _QUEUE_KEYS)

        seats = fields.get("seats")
        if not isinstance(seats, (list, tuple)) or not seats:
            raise ConfigError(
                f"queue {name!r} declares no seats; `seats` must be a non-empty list of "
                "seat names"
            )
        seats = [str(s) for s in seats]

        roles = fields.get("roles")
        if roles is not None:
            if not isinstance(roles, (list, tuple)) or not roles:
                raise ConfigError(
                    f"queue {name!r} sets `roles` to {roles!r}; it must be a non-empty "
                    "list of role names, or omitted entirely"
                )
            roles = [str(r) for r in roles]

        workers = int(fields.get("workers", 1))
        if workers < 1:
            raise ConfigError(f"queue {name!r} sets workers={workers}; workers must be >= 1")

        if "concurrency" not in fields or fields["concurrency"] is None:
            raise ConfigError(f"queue {name!r} declares no concurrency")
        concurrency = int(fields["concurrency"])
        if concurrency < 1:
            raise ConfigError(
                f"queue {name!r} sets concurrency={concurrency}; concurrency must be >= 1"
            )

        batch_size = int(fields.get("batch_size", 1))
        if not (1 <= batch_size <= 50):
            raise ConfigError(
                f"queue {name!r} sets batch_size={batch_size}; batch_size must be between "
                "1 and 50 (default 1)"
            )

        max_output_tokens_per_item = fields.get("max_output_tokens_per_item")
        if max_output_tokens_per_item is not None:
            max_output_tokens_per_item = int(max_output_tokens_per_item)

        queues[name] = QueueConfig(
            seats=seats,
            roles=roles,
            workers=workers,
            concurrency=concurrency,
            batch_size=batch_size,
            max_output_tokens_per_item=max_output_tokens_per_item,
        )

    cli_temp_root = raw.get("cli_temp_root")
    if cli_temp_root is not None:
        cli_temp_root = str(Path(str(cli_temp_root)).expanduser())

    # -- legacy backfill, then STRICT alias validation ----------------------
    referenced: List[str] = []
    for prof in profiles.values():
        referenced.extend(prof.proposers.values())
        referenced.extend([prof.critic, prof.meta])

    if legacy:
        argo_endpoint = _legacy_argo_endpoint(endpoints)
        if argo_endpoint is not None:
            for alias in referenced:
                if alias in models:
                    continue
                models[alias] = ModelConfig(
                    alias=alias,
                    endpoint=argo_endpoint.name,
                    served_name=alias,
                    canonical=default_canonical(alias, alias, argo_endpoint.kind),
                )
                logger.info(
                    "council config (%s): legacy import declared undeclared alias %r on "
                    "endpoint %r",
                    source,
                    alias,
                    argo_endpoint.name,
                )

    missing = sorted({alias for alias in referenced if alias not in models})
    if missing:
        raise ConfigError(
            f"profiles reference model aliases that are not declared in `models`: {missing}. "
            "Declare them explicitly -- an undeclared alias is never resolved against ARGO "
            "by name, because a typo would then silently seat a different model."
        )

    # -- rates ----------------------------------------------------------------
    # Keyed by GATEWAY, not by endpoint alias: MAG's limits and MAG's bill are
    # account-wide, so two endpoint aliases on one account are one payer.
    raw_rates = raw.get("rates") or {}
    rates: Dict[str, RateConfig] = {}
    for name, entry in raw_rates.items():
        if not isinstance(entry, Mapping):
            raise ConfigError(
                f"rate {name!r} must be a mapping, got {type(entry).__name__}"
            )
        fields = _warn_unknown("rate", name, entry, _RATE_KEYS)
        try:
            rates[str(name)] = RateConfig(**fields)
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad rate is a config error
            raise ConfigError(f"rate {name!r} is unusable: {exc}") from exc

    default_profile = raw.get("default_profile")
    if default_profile is not None and default_profile not in profiles:
        raise ConfigError(
            f"default_profile {default_profile!r} is not a configured profile; "
            f"configured: {sorted(profiles)}"
        )

    return CouncilConfig(
        endpoints=endpoints,
        models=models,
        profiles=profiles,
        queues=queues,
        rates=rates,
        default_profile=default_profile,
        store=_resolve_store_url(raw.get("store")),
        cli_temp_root=cli_temp_root,
        user=(str(raw["user"]).strip() or None) if raw.get("user") else None,
        query_dirs=_resolve_query_dirs(raw.get("query_dirs"), source),
        source=source,
    )


def load_legacy_model_inference(path: Union[str, Path]) -> CouncilConfig:
    """Load a legacy ``model_inference.yaml`` with the compatibility rules on.

    Equivalent to ``load_council_config(path, legacy=True)``; named so the
    legacy path is greppable from the call site.
    """
    return load_council_config(path, legacy=True)
