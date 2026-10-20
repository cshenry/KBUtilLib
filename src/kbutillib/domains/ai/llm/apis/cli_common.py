"""Shared plumbing for the council's two headless-CLI backends.

A CLI seat is billed against an interactive **subscription**, not against an
API key, and that is the whole reason this module exists.  Anthropic's and
OpenAI's CLIs both accept API-key credentials from the environment and from
their own config files, and if one of those is picked up the call silently
moves onto metered API billing -- the opposite of what a subscription-billed
seat is for.  There is **no API-billing mode for CLI backends in v1**, so
this module's job is to make that impossible rather than merely unlikely:

* :func:`_child_env` builds the child environment from an **allowlist**,
  starting from an empty dict.  Nothing is inherited by default, so a
  credential variable cannot reach the CLI by being forgotten here -- only by
  being added on purpose.
* :func:`billing_preflight` answers "how would this CLI bill?" **offline**,
  by reading config files only.  It runs no inference, starts no process and
  never reads a secret's value.
* :func:`run_cli` runs the child in its own session and in a fresh temporary
  working directory that is deleted afterwards, so a CLI that looks for
  project context, a git repo or a session file finds nothing of the
  caller's.

Note that :func:`billing_preflight` is a **billing** check, not a liveness
check.  An ``auth.json`` holding expired ChatGPT tokens is still ``ready``:
how the account bills has not changed, and whether the token still works is
for the call itself to discover.

Verified against the CLIs INSTALLED ON THIS MACHINE, 2026-10-15
---------------------------------------------------------------

Everything below was read from ``--version`` and ``--help`` output and from
the on-disk config files.  **No prompt was sent through either real CLI** --
the test suite drives fake executables only.

Versions found (both exactly as the task expected):

* ``claude --version`` -> ``2.1.269 (Claude Code)``
* ``codex --version``  -> ``codex-cli 0.160.0``

``claude --help`` (every flag the Claude argv uses exists, with these
meanings):

* ``-p, --print`` -- "Print response and exit"; non-interactive mode.
* ``--output-format <format>`` -- choices ``text``, ``json``,
  ``stream-json``; "only works with --print".  We ask for ``json``.
* ``--json-schema <schema>`` -- "JSON Schema for structured output
  validation", passed as a JSON **string** on the command line.
* ``--model``, ``--system-prompt``.
* ``--no-session-persistence`` -- "sessions will not be saved to disk and
  cannot be resumed (only works with --print)".
* ``--tools <tools...>`` -- 'Use "" to disable all tools'.

Two Claude flags are deliberately NEVER passed, and the installed help
confirms why:

* ``--bare`` -- its own help says "Anthropic auth is strictly
  ANTHROPIC_API_KEY or apiKeyHelper via --settings (**OAuth and keychain are
  never read**)".  Passing it would bypass subscription OAuth, i.e. defeat
  the entire point of a CLI seat.
* ``--dangerously-skip-permissions`` -- "Bypass all permission checks."  A
  council seat answers one closed-menu question; it has no business running
  tools at all, which is what ``--tools ""`` says.

``codex exec --help`` (every flag the Codex argv uses exists):

* ``[PROMPT]`` -- "If not provided as an argument (or if ``-`` is used),
  instructions are read from stdin."  So ``-`` is the documented, installed
  spelling of "prompt on stdin"; the task's fallback note does not apply.
* ``-m, --model <MODEL>``.
* ``--output-schema <FILE>`` -- "Path to a JSON Schema file describing the
  model's final response shape".  A **file**, unlike Claude's inline string.
* ``-o, --output-last-message <FILE>`` -- "file where the last message from
  the agent should be written".
* ``--ephemeral`` -- "Run without persisting session files to disk".
* ``--ignore-user-config`` -- "Do not load ``$CODEX_HOME/config.toml``;
  **auth still uses CODEX_HOME**".  Exactly the property the council needs:
  a provider configured in ``config.toml`` (e.g. an ARGO route) cannot
  hijack the call, while subscription auth keeps working.
* ``-s, --sandbox <SANDBOX_MODE>`` -- possible values ``read-only``,
  ``workspace-write``, ``danger-full-access``.  We pass ``read-only``.
* ``--skip-git-repo-check`` -- "Allow running Codex outside a Git repository",
  needed because :func:`run_cli` runs in a bare temporary directory.

Web search, re-read off the SAME installed CLIs, 2026-10-20
------------------------------------------------------------

Opt-in only: ``LLMRequest.params["web_search"] = True``.  Both spellings
below were taken from the installed ``--help`` and confirmed by a
PARSE-ONLY probe -- a command line ending in a deliberately invalid flag, so
the CLI reports the first option it does not recognise and exits without
sending a prompt.  **No prompt was sent through either real CLI** and no
token was spent, so the claim made at the top of this docstring still holds.

``claude --help`` (``2.1.269``), the two flags the Claude web-search argv
adds:

* ``--tools <tools...>`` -- "Specify the list of available tools from the
  built-in set.  Use "" to disable all tools, "default" to use all tools, or
  specify tool names (e.g. "Bash,Edit,Read")."  So the COMMA-separated form
  is the documented spelling, and ``WebSearch,WebFetch`` replaces the ``""``
  that the default argv passes.
* ``--allowedTools, --allowed-tools <tools...>`` -- "Comma or space-separated
  list of tool names to allow (e.g. "Bash(git *) Edit")".  Variadic, which is
  why the two tool names are separate argv entries and why this pair is
  APPENDED LAST: the next token would otherwise be swallowed as a third tool
  name.
* Probe: ``claude -p ... --tools 'WebSearch,WebFetch' --allowedTools
  WebSearch WebFetch --zzz-not-a-real-flag`` exited 1 with
  ``error: unknown option '--zzz-not-a-real-flag'`` -- i.e. every option
  BEFORE the sentinel was recognised.  The control
  (``claude -p --definitely-not-a-flag 'say OK'``) exits 1 the same way and
  makes no model call, which is what makes the sentinel probe free.

``codex --help`` (``codex-cli 0.160.0``).  The switch is a **TOP-LEVEL**
option and is NOT listed by ``codex exec --help``:

* ``--search`` -- "Enable live web search.  When enabled, the native
  Responses ``web_search`` tool is available to the model (no per-call
  approval)".

  It must therefore come BEFORE the ``exec`` subcommand.  Measured on the
  installed version:

  * ``codex --search exec --help`` -> exit 0, prints the ``exec`` help.
  * ``codex exec --search --help`` -> exit **2**,
    ``error: unexpected argument '--search' found``.

  No ``-c`` config-override equivalent is documented in either help text, so
  the top-level flag is used rather than a guessed ``-c`` key.  The full
  web-search argv was parse-checked as well:
  ``codex --search exec --model gpt-5 --output-schema <f> -o <f> --ephemeral
  --ignore-user-config --sandbox read-only --skip-git-repo-check --help``
  -> exit 0.

``--sandbox read-only`` and ``--ignore-user-config`` are kept on the
web-search command line unchanged, and that is not an oversight.  Search is
the model reading the web through the provider's own tool; it is not the
agent running shell commands, so there is no reason to widen the sandbox,
and widening it is exactly the mistake this note exists to prevent.

Config-file locations and KEY NAMES (names only -- no value was printed):

* ``~/.claude/settings.json`` and ``~/.claude/settings.local.json`` both
  exist here.  Top-level keys observed: ``permissions``,
  ``skipDangerousModePermissionPrompt``, ``agentPushNotifEnabled``,
  ``hooks`` (and ``permissions`` alone in ``settings.local.json``).  Neither
  file has an ``env`` block and neither sets ``apiKeyHelper``, so this
  machine's real state is ``ready`` -- the refusal paths are exercised
  against fake home directories in the tests, never against ``$HOME``.
* ``CLAUDE_CONFIG_DIR`` is unset here; when it IS set, the same two file
  names are read from under it as well.
* ``~/.codex/auth.json`` exists.  Key names observed, with types:
  ``auth_mode`` (str), ``OPENAI_API_KEY`` (null on this machine),
  ``tokens`` (dict, holding ``id_token``, ``access_token``,
  ``refresh_token``, ``account_id``), ``last_refresh`` (str).  So the API-key
  field is spelled ``OPENAI_API_KEY`` and the ChatGPT token block is
  ``tokens``.
* ``CODEX_HOME`` is unset here, so the default ``~/.codex`` applies.

(The Codex login on this machine was reported expired at the time of
writing.  That is irrelevant to everything above: only ``--version``,
``--help`` and the auth.json key names were needed, all of which are
offline, and nothing under ``~/.codex`` was modified.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import tempfile
import time
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from ..types import (
    AuthError,
    BillingGuardRefused,
    CallError,
    CallTimeout,
    LLMError,
    LLMRequest,
    LLMResult,
    QuotaExhausted,
    TransportError,
    as_llm_error,
    as_llm_request,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AUTH_PATTERNS",
    "CHILD_ENV_ALLOWLIST",
    "CLAUDE_EXTRA_ENV",
    "CODEX_EXTRA_ENV",
    "DENIED_BILLING_VARS",
    "KILL_GRACE_S",
    "OUTPUT_EXCERPT_CHARS",
    "QUOTA_PATTERNS",
    "CliBackend",
    "_child_env",
    "billing_preflight",
    "classify_failure",
    "dropped_billing_vars",
    "kill_cli_children",
    "live_cli_children",
    "run_cli",
    "strip_fences",
]

#: The ONLY variables inherited by a CLI child, whatever the kind.  Starting
#: from an empty environment and copying these is what makes a credential
#: leak an act of commission rather than one of omission.  ``PATH`` and
#: ``HOME`` are load-bearing (the CLI finds its own node runtime and its own
#: credential store through them); the rest are locale/tempdir hygiene so the
#: child does not behave differently from the user's own shell.
CHILD_ENV_ALLOWLIST: Tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "SHELL",
)

#: Additionally copied for ``claude_cli``.  ``CLAUDE_CODE_OAUTH_TOKEN`` is a
#: SUBSCRIPTION credential, not an API key, so it is the one ``CLAUDE_CODE_*``
#: variable that may travel; ``CLAUDE_CONFIG_DIR`` relocates the config
#: directory and must agree with what :func:`billing_preflight` inspected.
CLAUDE_EXTRA_ENV: Tuple[str, ...] = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")

#: Additionally copied for ``codex_cli``.  ``--ignore-user-config`` stops
#: ``config.toml`` being read but, per its own help text, auth still comes
#: from ``CODEX_HOME`` -- so this is how a non-default Codex home stays usable.
CODEX_EXTRA_ENV: Tuple[str, ...] = ("CODEX_HOME",)

#: Variables that would move a CLI call onto metered API billing.  None of
#: them is ever copied into a child environment; :func:`dropped_billing_vars`
#: reports which were present so an operator can see why their API key
#: appeared to be ignored.  ``CLAUDE_CODE_*`` is additionally denied by
#: PREFIX (see :func:`dropped_billing_vars`), with the OAuth token excepted.
DENIED_BILLING_VARS: Tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CODEX_API_KEY",
)

#: Settings keys that, if present in a Claude settings ``env`` block, mean the
#: CLI would authenticate against the API rather than the subscription.
_ANTHROPIC_BILLING_KEYS: Tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)

#: Candidate spellings of the API-key field in ``auth.json``.  Only
#: ``OPENAI_API_KEY`` was observed on this machine (see the module docstring);
#: the others are accepted so an older or newer Codex layout is still refused
#: rather than silently read as "no API key".
_CODEX_API_KEY_FIELDS: Tuple[str, ...] = (
    "OPENAI_API_KEY",
    "openai_api_key",
    "api_key",
)

#: A non-zero exit (or an ``is_error`` envelope) whose text matches any of
#: these is a quota/rate refusal, which HALTS the endpoint: every other call
#: to the same subscription would be refused identically, so retrying is
#: just spending wall-clock to be told the same thing.
QUOTA_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"usage limit",
        r"rate limit",
        r"limit reached",
        r"quota",
        r"429",
        r"too many requests",
    )
)

#: ... and these mean the CLI is not logged in, which also halts the endpoint
#: because no amount of retrying logs it in.  Checked AFTER the quota
#: patterns: "usage limit reached" is a quota refusal even though a CLI may
#: mention logging in again in the same breath.
AUTH_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"not logged in",
        r"authentication",
        r"login",
    )
)

#: How long a child gets to die after ``SIGTERM`` before it is ``SIGKILL``ed.
KILL_GRACE_S = 5.0

#: The prompt file's name inside the per-call working directory.  It lives
#: INSIDE that directory (never beside it) so the same ``TemporaryDirectory``
#: cleanup that removes the cwd also removes the prompt -- it must never
#: outlive the call that wrote it.
STDIN_FILENAME = "prompt.stdin"

#: How much CLI output is quoted into an error message.  Enough to recognise
#: a provider banner in a call row weeks later, short enough that the store
#: does not fill up with stack traces.
OUTPUT_EXCERPT_CHARS = 500

# ```json ... ``` / ``` ... ``` fence markers, in any language-tag spelling.
_FENCE_RE = re.compile(r"```[A-Za-z0-9_+.-]*")


def strip_fences(text: str) -> str:
    """Remove markdown code-fence markers from *text*.

    Only the markers go, not the content between them: a CLI that wrapped its
    JSON in ```` ```json ```` answered correctly and must not be failed for
    the decoration.  Deliberately duplicated from ``http_common`` rather than
    imported, so the CLI backends do not drag ``httpx`` in through it.
    """
    return _FENCE_RE.sub("", str(text or "")).strip()


# ---------------------------------------------------------------------------
# The child environment
# ---------------------------------------------------------------------------


def _resolve_env(parent_env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    """Return *parent_env*, or the CURRENT ``os.environ`` when it is ``None``.

    The default is resolved here, at call time, and never as a default
    argument value: ``parent_env=os.environ`` in a signature is evaluated once
    at import, which would have pinned this module to the environment the
    process started with and made a later ``monkeypatch.setenv`` invisible.
    """
    return os.environ if parent_env is None else parent_env


def _resolve_home(home: Optional[Path]) -> Path:
    """Return *home*, or the CURRENT ``Path.home()`` when it is ``None``.

    Same reason as :func:`_resolve_env`: ``home=Path.home()`` as a default
    argument would be frozen at import time, so a changed ``HOME`` would be
    ignored.  ``Path.home()`` itself reads ``$HOME`` on each call.
    """
    return Path(home) if home is not None else Path.home()


def _child_env(
    kind: str,
    parent_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build the environment for a CLI child of *kind*.

    Starts **empty** and copies only the allowlisted names that are actually
    present: :data:`CHILD_ENV_ALLOWLIST` always, plus
    :data:`CLAUDE_EXTRA_ENV` for ``claude_cli`` and :data:`CODEX_EXTRA_ENV`
    for ``codex_cli``.

    Everything else is therefore absent by construction -- including every
    variable in :data:`DENIED_BILLING_VARS` and every ``CLAUDE_CODE_*``
    variable other than the OAuth token.  That is the point: a new credential
    variable invented by either vendor tomorrow is already excluded today.

    Args:
        kind: the backend kind, ``"claude_cli"`` or ``"codex_cli"``.  An
            unrecognised kind gets the bare allowlist, which is the safe
            reading of "I do not know what this CLI needs".
        parent_env: the environment to copy FROM.  ``None`` (the default)
            means the live ``os.environ``, read at call time.
    """
    source = _resolve_env(parent_env)
    names: Tuple[str, ...] = CHILD_ENV_ALLOWLIST
    if kind == "claude_cli":
        names = names + CLAUDE_EXTRA_ENV
    elif kind == "codex_cli":
        names = names + CODEX_EXTRA_ENV

    env: Dict[str, str] = {}
    for name in names:
        value = source.get(name)
        if value is not None:
            env[name] = str(value)
    return env


def dropped_billing_vars(
    parent_env: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """Return the sorted NAMES of denied billing variables present in the env.

    Names only -- a value is never read, returned or logged.  This exists so a
    user who exported ``ANTHROPIC_API_KEY`` and wondered why it had no effect
    gets told, rather than having to guess that the backend dropped it on
    purpose.

    Covers :data:`DENIED_BILLING_VARS` plus every ``CLAUDE_CODE_*`` variable
    EXCEPT ``CLAUDE_CODE_OAUTH_TOKEN``, which is a subscription credential
    and is deliberately passed through.
    """
    source = _resolve_env(parent_env)
    found = {name for name in DENIED_BILLING_VARS if name in source}
    for name in source:
        if name.startswith("CLAUDE_CODE_") and name != "CLAUDE_CODE_OAUTH_TOKEN":
            found.add(name)
    return sorted(found)


# ---------------------------------------------------------------------------
# The billing preflight (offline; no inference, no secret values)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Tuple[str, Any]:
    """Read *path* as JSON.  Returns ``(status, payload)``.

    ``status`` is ``"absent"``, ``"unreadable"`` (not JSON, or not an object,
    or an I/O error) or ``"ok"``.  Never raises and never returns a value out
    of the file to a caller that has not asked for a specific key.
    """
    try:
        if not path.is_file():
            return "absent", None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable", None
    if not isinstance(payload, Mapping):
        return "unreadable", None
    return "ok", payload


def _claude_settings_paths(
    parent_env: Mapping[str, str],
    home: Path,
) -> List[Path]:
    """The Claude settings files to inspect, in precedence-agnostic order.

    ``~/.claude/settings.json`` and ``~/.claude/settings.local.json`` always,
    plus the same two names under ``CLAUDE_CONFIG_DIR`` when that is set --
    because a relocated config directory is where the real settings then live,
    and checking only ``$HOME`` would pass an endpoint whose actual config
    pins an API key.
    """
    names = ("settings.json", "settings.local.json")
    paths = [home / ".claude" / name for name in names]
    config_dir = parent_env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        paths.extend(Path(config_dir) / name for name in names)
    return paths


def _claude_preflight(
    parent_env: Mapping[str, str],
    home: Path,
) -> Tuple[str, List[str], List[str]]:
    """Decide how the Claude CLI would bill.  Returns ``(state, reasons, checked)``."""
    reasons: List[str] = []
    checked: List[str] = []
    unreadable = False
    refused = False

    for path in _claude_settings_paths(parent_env, home):
        checked.append(str(path))
        status, payload = _read_json(path)
        if status == "absent":
            continue
        if status == "unreadable":
            unreadable = True
            reasons.append(f"{path}: not readable as a JSON object")
            continue

        env_block = payload.get("env")
        if env_block is not None and not isinstance(env_block, Mapping):
            unreadable = True
            reasons.append(f"{path}: 'env' is not an object")
        elif isinstance(env_block, Mapping):
            # NAMES only.  A settings env block that pins any of these is
            # exactly the API-billing configuration this guard exists to stop.
            offending = [k for k in _ANTHROPIC_BILLING_KEYS if env_block.get(k)]
            if offending:
                refused = True
                reasons.append(
                    f"{path}: 'env' sets {', '.join(sorted(offending))}, "
                    "which bills against the API rather than the subscription"
                )

        if payload.get("apiKeyHelper"):
            refused = True
            reasons.append(
                f"{path}: 'apiKeyHelper' is set, so the CLI would fetch an "
                "API key instead of using subscription OAuth"
            )

    if refused:
        # A refusal is decisive even if another file was also unreadable:
        # one file pinning an API key is enough to know how this would bill.
        return "refused", reasons, checked
    if unreadable:
        return "unknown", reasons, checked
    reasons.append(
        "no Claude settings file sets an Anthropic API credential, base URL "
        "or apiKeyHelper; the CLI will use subscription OAuth"
    )
    return "ready", reasons, checked


def _codex_preflight(
    parent_env: Mapping[str, str],
    home: Path,
) -> Tuple[str, List[str], List[str]]:
    """Decide how the Codex CLI would bill.  Returns ``(state, reasons, checked)``."""
    codex_home = parent_env.get("CODEX_HOME")
    auth_path = (Path(codex_home) if codex_home else home / ".codex") / "auth.json"
    checked = [str(auth_path)]

    status, payload = _read_json(auth_path)
    if status == "absent":
        return (
            "unknown",
            [f"{auth_path}: does not exist, so no login could be confirmed"],
            checked,
        )
    if status == "unreadable":
        return (
            "unknown",
            [f"{auth_path}: not readable as a JSON object"],
            checked,
        )

    # Key NAMES and emptiness only -- no value is read out of this file.
    tokens = payload.get("tokens")
    has_tokens = isinstance(tokens, Mapping) and any(tokens.get(k) for k in tokens)
    api_key_fields = [f for f in _CODEX_API_KEY_FIELDS if payload.get(f)]

    if has_tokens:
        reasons = [
            f"{auth_path}: holds a ChatGPT 'tokens' block, so calls bill "
            "against the ChatGPT subscription"
        ]
        if api_key_fields:
            # Both present: Codex prefers the ChatGPT session, so this still
            # bills against the subscription -- but say so, because an
            # operator who set an API key deserves to know it is inert.
            reasons.append(
                f"{auth_path}: also holds {', '.join(api_key_fields)}, which "
                "is not used while a ChatGPT token block is present"
            )
        return "ready", reasons, checked

    if api_key_fields:
        return (
            "refused",
            [
                f"{auth_path}: holds {', '.join(api_key_fields)} and no "
                "ChatGPT 'tokens' block, so calls would bill against the API"
            ],
            checked,
        )

    return (
        "unknown",
        [
            f"{auth_path}: holds neither a ChatGPT 'tokens' block nor an "
            "API key field, so how it would bill cannot be determined"
        ],
        checked,
    )


def billing_preflight(
    kind: str,
    parent_env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Answer "how would this CLI bill?" from config files alone.

    Entirely **offline**: no process is started, no prompt is run, no token is
    spent, and no secret's value is read -- only whether a named key is
    present and non-empty.

    Rules, per kind:

    * ``claude_cli`` -- reads the ``env`` block and ``apiKeyHelper`` key of
      ``~/.claude/settings.json``, ``~/.claude/settings.local.json`` and, when
      ``CLAUDE_CONFIG_DIR`` is set, the same two files under it.  ``refused``
      if any ``env`` block sets ``ANTHROPIC_API_KEY``,
      ``ANTHROPIC_AUTH_TOKEN`` or ``ANTHROPIC_BASE_URL``, or if
      ``apiKeyHelper`` is set.
    * ``codex_cli`` -- reads ``$CODEX_HOME/auth.json`` (default
      ``~/.codex/auth.json``).  ``refused`` if it holds a non-empty API key
      field and no ChatGPT token block; ``ready`` if it holds ChatGPT tokens.

    An unreadable file or an unrecognised structure is ``unknown``, never
    ``ready``: "I could not tell" must not be reported as "I checked and it
    is fine".

    This is a BILLING check, not a liveness check.  Expired ChatGPT tokens
    are still ``ready`` -- the account's billing mode has not changed, and
    whether the token works is the call's discovery to make.

    Args:
        kind: ``"claude_cli"`` or ``"codex_cli"``.
        parent_env: environment to read ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``
            from.  ``None`` (the default) means the live ``os.environ``, read
            at call time -- NOT frozen at import.
        home: home directory to resolve the config paths against.  ``None``
            (the default) means ``Path.home()``, read at call time, so a
            reassigned ``HOME`` is honoured.

    Returns:
        ``{"state": "ready"|"refused"|"unknown", "reasons": [str],
        "checked": [paths], "detail": str}``.  ``detail`` is the reasons
        joined into one line; it is not part of the documented shape but
        ``CouncilWorker._preflight`` reads a ``detail``/``message`` key when
        it builds its refusal message, and without it the operator is told
        only "no reason given".
    """
    env = _resolve_env(parent_env)
    base = _resolve_home(home)

    if kind == "claude_cli":
        state, reasons, checked = _claude_preflight(env, base)
    elif kind == "codex_cli":
        state, reasons, checked = _codex_preflight(env, base)
    else:
        state, reasons, checked = (
            "unknown",
            [f"{kind!r} is not a CLI backend kind, so it has no billing preflight"],
            [],
        )

    return {
        "state": state,
        "reasons": reasons,
        "checked": checked,
        "detail": "; ".join(reasons),
    }


def _stamp_paths(paths: Any) -> Dict[str, Optional[Tuple[int, int]]]:
    """``{path: (mtime_ns, size)}`` for *paths*; ``None`` where one is absent.

    The cache key of :meth:`CliBackend._billing_check`.  Two deliberate
    choices:

    * ``None`` for an absent file is load-bearing -- "there is no auth.json"
      is a fact the verdict rests on, so the file later APPEARING has to
      invalidate it just as a rewrite does.
    * the size travels with the mtime because a filesystem's mtime
      granularity is not guaranteed to be finer than a second (NFS, and
      ``/mnt/homes`` on this fleet is networked).  A ``claude login`` that
      rewrote ``settings.json`` inside one mtime tick would otherwise keep the
      stale billing verdict, which is the exact failure this invalidation
      exists to prevent.
    """
    stamps: Dict[str, Optional[Tuple[int, int]]] = {}
    for raw in paths or ():
        name = str(raw)
        try:
            stat = Path(name).stat()
        except OSError:
            stamps[name] = None
        else:
            stamps[name] = (stat.st_mtime_ns, stat.st_size)
    return stamps


# ---------------------------------------------------------------------------
# Running the child
# ---------------------------------------------------------------------------


async def _reap(proc: Any, timeout: float) -> bool:
    """Wait up to *timeout* for *proc* to exit.  ``True`` if it did."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (asyncio.TimeoutError, ProcessLookupError):
        return False
    return True


#: Every CLI child this process has spawned and not yet reaped, keyed by pid
#: (which is also its process-group id, because :func:`run_cli` spawns with
#: ``start_new_session=True``).
#:
#: It exists for ONE caller: the council daemon's stop deadline, which has to
#: kill and reap whatever is still running when the drain window expires.
#: ``asyncio`` cancellation alone cannot do that -- cancelling the task that
#: awaits ``proc.communicate()`` abandons the child, and a Claude or Codex
#: child abandoned mid-answer keeps its node runtime, its MCP servers and its
#: subscription slot. Process-global because the children are the PROCESS's,
#: not any one event loop's.
_LIVE_CHILDREN: "Dict[int, Any]" = {}


def live_cli_children() -> List[Any]:
    """Every CLI child process this process has spawned and not yet reaped."""
    return list(_LIVE_CHILDREN.values())


async def kill_cli_children() -> int:
    """Kill and reap every live CLI child's process GROUP.  Returns how many.

    The council daemon's last act before it releases the execution lock: a
    child still talking to a subscription when the drain deadline expires is
    spending against that subscription for an answer nobody will read, and
    ``SIGTERM`` to the daemon does not reach a child in its own session.

    Reaping matters as much as killing. An unreaped child becomes a zombie
    held by a process that is about to exit, and on a machine where the
    daemon is restarted in a loop that accumulates.
    """
    killed = 0
    for proc in list(_LIVE_CHILDREN.values()):
        if proc.returncode is not None:
            continue
        await _kill_group(proc)
        killed += 1
    return killed


async def _kill_group(proc: Any) -> None:
    """Kill the child's whole process group: ``SIGTERM``, then ``SIGKILL``.

    The GROUP, not the process.  Both CLIs spawn helpers (a node runtime, MCP
    servers, a sandbox shell), and killing only the parent leaves those
    holding the pipe and the subscription slot.  ``start_new_session=True`` in
    :func:`run_cli` is what makes ``proc.pid`` the group id here.
    """
    for sig, grace in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            # Already gone, or never a group leader.  Still try to reap.
            pass
        if await _reap(proc, grace):
            return


async def run_cli(
    argv: Optional[Sequence[str]],
    stdin_text: str,
    env: Mapping[str, str],
    timeout_s: float,
    *,
    prepare: Optional[Callable[[Path], Sequence[str]]] = None,
    collect: Optional[Callable[[Path], None]] = None,
    temp_root: Optional[Union[str, Path]] = None,
) -> Tuple[int, str, str, float]:
    """Run a CLI child and return ``(returncode, stdout, stderr, elapsed)``.

    Three properties the council depends on:

    * **Its own session.**  ``start_new_session=True`` puts the child in a new
      process group, so a timeout can take down its helpers too (see
      :func:`_kill_group`) instead of orphaning them.
    * **A fresh, empty working directory per call**, deleted afterwards.  Both
      CLIs read project context from the cwd -- ``CLAUDE.md``, a git repo, a
      session file -- and a council seat must answer from the prompt alone, so
      it is given a directory with nothing in it.  Deleting it afterwards also
      means a CLI that wrote scratch state leaves none behind.
    * **The prompt goes in on stdin**, never in ``argv``, so it cannot leak
      through ``ps`` to any other user on a shared host.

    The prompt is handed to the child as a FILE, not a pipe.  It used to be:
    ``proc.communicate(stdin_text.encode())`` pumps stdin from THIS coroutine,
    on the same asyncio event loop that also drives the council's synchronous
    store I/O.  When that loop stalled, the child gave up waiting for its
    prompt and exited with "Input must be provided through stdin" -- measured
    destroying 23 of 24 Claude attempts on a 19,276-item run, reported as
    generic transport failures.  Writing the prompt to a file INSIDE the
    per-call working directory and handing the child that file opened for
    reading removes the parent from the data path entirely: the child reads
    at its own pace, a child that never reads it does not block anyone
    writing to a pipe (because there is no pipe), and the file is deleted
    with the rest of the working directory, so the prompt never persists.
    The child's view is unchanged -- it still reads its prompt on stdin, which
    is the documented spelling both installed CLIs expect.

    Args:
        argv: the command line.  May be ``None`` when *prepare* is given.
        stdin_text: written to a file and handed to the child as stdin.
        env: the child's COMPLETE environment -- built by :func:`_child_env`,
            not merged with the parent's by this function.
        timeout_s: wall-clock budget.  On expiry the process group is killed
            and :class:`~kbutillib.domains.ai.council.types.CallTimeout` is
            raised (the one exception this function raises by design; the
            backends turn it into an ``ok=False`` result).
        prepare: called with the freshly created working directory, and
            returns the argv to run.  This is how a CLI that needs files
            INSIDE that directory names them -- Codex's ``--output-schema``
            and ``-o`` both take paths, and the directory does not exist
            until this function makes it.
        collect: called with the working directory after the child exits and
            BEFORE the directory is removed, so output written to a file in it
            can be read.  Called even when the child failed, because a partial
            answer file is evidence.
        temp_root: directory under which the per-call working directory is
            created (passed straight through to
            ``tempfile.TemporaryDirectory(dir=...)``).  ``None`` preserves
            today's default (the platform temp directory).

    Raises:
        CallTimeout: the child did not finish within *timeout_s*.
    """
    started = time.monotonic()
    temp_root_str = str(temp_root) if temp_root is not None else None
    with tempfile.TemporaryDirectory(
        prefix="kbu-council-cli-", dir=temp_root_str
    ) as tmp:
        cwd = Path(tmp)
        command = list(prepare(cwd)) if prepare is not None else list(argv or ())
        if not command:
            raise ValueError("run_cli needs a non-empty argv (or a prepare hook)")

        stdin_path = cwd / STDIN_FILENAME
        stdin_path.write_text(str(stdin_text or ""), encoding="utf-8")
        stdin_file = stdin_path.open("rb")
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=stdin_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=dict(env),
                start_new_session=True,
            )
        finally:
            # The child gets its own duplicated fd at exec time; this process
            # has no further use for the handle once the child exists.
            stdin_file.close()
        _LIVE_CHILDREN[proc.pid] = proc
        try:
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                await _kill_group(proc)
                raise CallTimeout(
                    f"{Path(command[0]).name}: no answer within {timeout_s:g}s "
                    "(process group killed)"
                ) from None
            except asyncio.CancelledError:
                # The awaiting task was cancelled -- the council daemon's stop
                # deadline, or a caller's timeout. Cancellation unwinds THIS
                # coroutine; it does nothing to the child, which would carry
                # on answering (and spending) with nobody listening. Kill its
                # group before letting the cancellation through.
                await _kill_group(proc)
                raise
        finally:
            _LIVE_CHILDREN.pop(proc.pid, None)

        if collect is not None:
            collect(cwd)

    elapsed = time.monotonic() - started
    return (
        int(proc.returncode or 0),
        (out or b"").decode("utf-8", "replace"),
        (err or b"").decode("utf-8", "replace"),
        elapsed,
    )


def classify_failure(name: str, returncode: Optional[int], text: str) -> CallError:
    """Map CLI failure output onto the council's taxonomy.

    Order matters and is deliberate: quota first, then auth, then transport.
    A message saying "usage limit reached -- run /login to switch accounts"
    is a quota refusal, and reading it as an auth failure would send an
    operator to re-authenticate an account that is merely out of quota.

    Both quota and auth HALT the endpoint: neither is fixed by trying again,
    and continuing to sweep calls at a subscription that has said no just
    burns the batch's wall-clock.  Anything else is a retryable
    :class:`TransportError` -- a crashed node runtime or a killed helper is
    exactly the kind of failure a second attempt fixes.
    """
    excerpt = str(text or "").strip()[:OUTPUT_EXCERPT_CHARS]
    where = f"{name} exited {returncode}" if returncode else name

    for pattern in QUOTA_PATTERNS:
        if pattern.search(excerpt):
            return QuotaExhausted(f"{where}: quota/rate refusal: {excerpt!r}")
    for pattern in AUTH_PATTERNS:
        if pattern.search(excerpt):
            return AuthError(f"{where}: not authenticated: {excerpt!r}")
    return TransportError(f"{where}: {excerpt!r}" if excerpt else f"{where} with no output")


# ---------------------------------------------------------------------------
# The shared backend
# ---------------------------------------------------------------------------


class CliBackend:
    """Common implementation of the ``Backend`` protocol over a headless CLI.

    Subclasses set :attr:`kind` and :attr:`default_executable` and implement
    :meth:`_run`, which is the only part that differs: Claude takes its schema
    inline and answers on stdout, Codex takes a schema file and answers into
    an output file.

    Everything else is here because it is identical and because getting it
    wrong is expensive: the billing gate (below), the never-raise guard in
    :meth:`complete`, and the two trivial protocol members
    (:meth:`models` is ``[]`` -- neither CLI lists models for free --
    and :meth:`effective_params` is just the model, because neither CLI
    accepts a temperature or a token ceiling).

    **The billing gate runs before the first call and again whenever the
    files it read have changed.**  Not per call, because four file reads per
    model per item would be pure syscall overhead; not once per process
    either, because the answer is only as current as the files it came from
    -- a user who runs ``claude login`` (or edits ``settings.json``) mid-batch
    has changed how the next call bills, and a gate cached for the life of the
    process would keep spending on the old answer.  So the gate caches its
    verdict together with the mtime of every path the preflight reported
    inspecting, and re-runs as soon as one of them moves (appears,
    disappears, or is rewritten).  Not at construction either: a backend is
    built eagerly by its caller, and refusing there would turn a billing
    misconfiguration into a constructor crash rather than the typed,
    endpoint-halting failure the caller already knows how to report.

    Args:
        executable: the CLI to run.  Resolved through ``PATH`` in the child.
        allow_unverified: let an ``unknown`` preflight through.  A ``refused``
            preflight is NEVER let through, whatever this says -- "I know this
            bills the wrong way" is not the same claim as "I could not tell".
        config: the endpoint's configuration mapping, as
            ``CouncilConfig.endpoint(alias)`` returns it.  Only ``name`` and
            ``executable`` are read; accepted so a CLI endpoint can be built
            the same way as an HTTP one.
        _home: TEST SEAT.  Home directory for the billing preflight, so the
            refusal paths can be exercised against fake home directories
            instead of the real ``$HOME``.  ``None`` in production, which
            means ``Path.home()`` read at call time.
        _parent_env: TEST SEAT.  Environment to build the child environment
            and resolve the preflight from.  ``None`` in production, which
            means the live ``os.environ`` read at call time.
    """

    kind: str = "claude_cli"
    default_executable: str = "claude"

    #: Optional ``ApiType`` member (see :mod:`..protocol`): can this API type
    #: run a call with live web search?  ``False`` here so a subclass that has
    #: not thought about it inherits the safe answer; both shipped CLI
    #: backends override it to ``True``.
    supports_web_search: bool = False

    def __init__(
        self,
        executable: Optional[str] = None,
        *,
        allow_unverified: bool = False,
        config: Optional[Mapping[str, Any]] = None,
        _home: Optional[Path] = None,
        _parent_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        cfg = dict(config or {})
        self.config = cfg
        self.executable = str(
            executable or cfg.get("executable") or self.default_executable
        )
        self.allow_unverified = bool(allow_unverified)
        self.name = str(cfg.get("name") or self.kind)
        self._home = Path(_home) if _home is not None else None
        self._parent_env = _parent_env
        #: ``None`` until the gate has run; then the refusal error, or ``False``
        #: for "checked, and cleared".
        self._billing_gate: Optional[Any] = None
        #: ``{path: (mtime_ns, size) or None}`` for every file the cached
        #: verdict was read from.  ``None`` records "did not exist", which is
        #: itself a fact the verdict depended on.
        self._billing_stamps: Dict[str, Optional[Tuple[int, int]]] = {}

    # -- protocol ---------------------------------------------------------

    async def preflight(self) -> Dict[str, Any]:
        """Report how this endpoint would bill.  Offline; spends nothing.

        ``CouncilWorker._preflight`` calls this once per CLI endpoint at job
        submission, before any token is spent, and refuses the whole job on a
        ``refused`` -- which is the right place for this question, because
        "which account is this going to charge?" must be answered before a
        batch runs rather than discovered in the middle of one.
        """
        return billing_preflight(
            self.kind,
            parent_env=self._parent_env,
            home=self._home,
        )

    async def complete(self, req: Any) -> LLMResult:
        """Run one invocation.  Never raises; failures come back typed.

        The outer ``except`` is not defensive decoration.  The ``ApiType``
        protocol's one hard rule is that a call failure cannot propagate: a
        caller runs N models per item, and an unforeseen ``OSError`` from the
        subprocess machinery would otherwise abort the whole item over one bad
        model.

        *req* is an :class:`~..types.LLMRequest` or anything
        :func:`~..types.as_llm_request` can read as one, converted inside the
        guard so even a request of the wrong shape comes back typed.
        """
        started = time.monotonic()
        try:
            payload = as_llm_request(req)
            gate = await self._billing_check()
            if gate is not None:
                return self._failed(gate, started)
            return await self._run(payload, started)
        except Exception as exc:  # noqa: BLE001 - a backend never raises
            logger.warning(
                "council %s backend %s: %s",
                self.kind,
                self.name,
                f"{type(exc).__name__}: {exc}",
            )
            return self._failed(as_llm_error(exc), started)

    async def models(self) -> List[Dict[str, Any]]:
        """``[]`` -- neither CLI enumerates its models without spending.

        Not an error: the ``Backend`` protocol documents ``[]`` as "this
        backend cannot enumerate models", and a council seat names its model
        explicitly anyway.
        """
        return []

    def effective_params(self, request: Any) -> Dict[str, Any]:
        """``{"model": <the model>}`` -- the only parameter a CLI takes.

        Neither CLI accepts a temperature or an output-token ceiling, so
        putting the requested values in here would make the caller's prompt
        hash depend on numbers that never reached the model: two calls
        differing only in ``temperature`` would then miss each other's cached
        answer for no reason.

        *request* may be an :class:`~..types.LLMRequest` or a council
        ``SeatSpec``; both name the served model the same way.
        """
        return {"model": as_llm_request(request).model}

    async def aclose(self) -> None:
        """Nothing to release: each call owns its own process and directory."""
        return None

    # -- the billing gate -------------------------------------------------

    async def _billing_check(self) -> Optional[LLMError]:
        """Run the billing preflight.  Returns the refusal, or ``None``.

        Cached on the instance and invalidated by the FILES the verdict came
        from: ``preflight()`` reports every path it inspected, and this keeps
        their mtimes beside its answer.  A hit costs one ``stat`` per path
        (cheap, and the only way to notice a ``claude login`` that happened
        after the batch started); a miss re-reads them.
        """
        if self._billing_gate is not None and not self._billing_files_changed():
            return self._billing_gate or None

        report = await self.preflight()
        self._billing_stamps = _stamp_paths(report.get("checked") or [])
        state = str(report.get("state") or "unknown")
        detail = report.get("detail") or "no detail"

        if state == "ready":
            self._billing_gate = False
        elif state == "refused":
            self._billing_gate = BillingGuardRefused(
                f"{self.name}: billing preflight refused: {detail}"
            ).to_error()
        elif self.allow_unverified:
            # Explicitly asked to spend against an endpoint whose billing mode
            # could not be confirmed.  Say so once, at WARNING, so the choice
            # is visible in the log of whatever it ends up charging.
            logger.warning(
                "council %s backend %s: billing mode unconfirmed (%s); "
                "proceeding because allow_unverified=True",
                self.kind,
                self.name,
                detail,
            )
            self._billing_gate = False
        else:
            self._billing_gate = BillingGuardRefused(
                f"{self.name}: could not confirm how this endpoint bills "
                f"({state}: {detail}); pass allow_unverified=True to spend "
                "against it anyway"
            ).to_error()

        return self._billing_gate or None

    # -- helpers for subclasses -------------------------------------------

    def _billing_files_changed(self) -> bool:
        """Has any file the cached billing verdict was read from moved?

        A path appearing, disappearing or being rewritten all count: each one
        can flip how the CLI bills, and a stale ``ready`` is the failure this
        exists to prevent.  With nothing recorded (an ``unknown`` verdict from
        a kind that inspects no files) there is nothing to invalidate.
        """
        if not self._billing_stamps:
            return False
        if _stamp_paths(self._billing_stamps) != self._billing_stamps:
            logger.info(
                "council %s backend %s: a billing config file changed; re-running "
                "the preflight",
                self.kind,
                self.name,
            )
            return True
        return False

    def _env(self) -> Dict[str, str]:
        """The child's complete environment, plus a note about what was dropped."""
        env = _child_env(self.kind, self._parent_env)
        dropped = dropped_billing_vars(self._parent_env)
        if dropped:
            # NAMES only, and at WARNING because an operator who exported an
            # API key is entitled to know it was deliberately ignored rather
            # than to conclude the backend is broken.
            logger.warning(
                "council %s backend %s: not passing %s to the CLI -- a CLI "
                "seat is billed against the subscription, and there is no "
                "API-billing mode for CLI backends",
                self.kind,
                self.name,
                ", ".join(dropped),
            )
        return env

    def _timeout(self, req: LLMRequest) -> float:
        """The wall-clock budget for *req*.

        One field now: :func:`~..types.as_llm_request` already folds a legacy
        ``CompletionRequest``'s ``deadline_s`` into ``timeout_s`` when the
        seat declared none, so the two-way fallback this used to do happens
        once, in the adapter.
        """
        return float(req.timeout_s or 180.0)

    async def _run(self, req: LLMRequest, started: float) -> LLMResult:
        """Run *req* through the CLI.  Implemented by each subclass."""
        raise NotImplementedError

    async def version(self) -> Optional[str]:
        """Return the CLI's own ``--version`` string, or ``None``.

        Free, offline and prompt-free -- used by ``kbu council doctor`` to
        report which CLI an endpoint would actually run.  A 5 s budget,
        because a ``--version`` that hangs is a broken install and the doctor
        should say so rather than wait.
        """
        try:
            code, out, err, _elapsed = await run_cli(
                [self.executable, "--version"],
                "",
                self._env(),
                5.0,
            )
        except (CallTimeout, OSError) as exc:
            logger.warning(
                "council %s backend %s: %s --version failed: %s",
                self.kind,
                self.name,
                self.executable,
                exc,
            )
            return None
        if code != 0:
            return None
        return (out or err or "").strip() or None

    # -- result builders --------------------------------------------------

    def _failed(
        self,
        error: LLMError,
        started: float,
        text: Optional[str] = None,
        truncated: bool = False,
    ) -> LLMResult:
        """Build the ``ok=False`` result for *error*, keeping what we learned."""
        return LLMResult(
            ok=False,
            text=text,
            parsed=None,
            elapsed_s=time.monotonic() - started,
            truncated=truncated,
            error=error,
        )
