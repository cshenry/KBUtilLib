"""beril_worktree.launch — start proxy for parallel BERIL sessions.

Replicates ``beril start``'s thin orchestration shell *minus* the
release-tag checkout (``_checkout_release``), importing the helpers
directly from ``beril_cli`` rather than reimplementing them.

The pre-``execvp`` assembly is factored into :func:`assemble_start_command`
— a pure function that returns ``(binary, argv, env_updates)`` — so it
is unit-testable without process replacement.

``beril_cli`` is NOT installed in the KBUtilLib dev/test environment.
All imports from ``beril_cli`` are deferred into functions (not at
module level) so that this module can be imported and tested without
the real package present.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Import helpers (deferred so beril_cli is optional at collection time)
# ---------------------------------------------------------------------------

_BERIL_CLI_SYMBOLS = (
    "get_default_agent",
    "get_vertex_config",
    "_sync_auth_token",
)


def _import_beril_cli():  # type: ignore[return]
    """Import and return the three borrowed symbols from beril_cli.

    Returns:
        Tuple ``(get_default_agent, get_vertex_config, _sync_auth_token)``.

    Raises:
        ImportError: If ``beril_cli`` is not importable.
        AttributeError: If any of the three symbols is missing from its
            expected module (naming the missing symbol).
    """
    try:
        from beril_cli import config as _beril_config  # type: ignore[import]
        from beril_cli import start as _beril_start  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            f"beril_cli is not importable: {exc}\n"
            "Ensure KBUtilLib is installed inside the BERIL venv where "
            "beril_cli is also installed."
        ) from exc

    missing = []
    for sym, mod in [
        ("get_default_agent", _beril_config),
        ("get_vertex_config", _beril_config),
        ("_sync_auth_token", _beril_start),
    ]:
        if not hasattr(mod, sym):
            missing.append(sym)

    if missing:
        raise AttributeError(
            f"beril_cli is missing expected symbol(s): {', '.join(missing)}\n"
            "The kbu proxy needs updating to match the current BERIL version."
        )

    return (
        _beril_config.get_default_agent,
        _beril_config.get_vertex_config,
        _beril_start._sync_auth_token,
    )


# ---------------------------------------------------------------------------
# Pure assembly function (unit-testable)
# ---------------------------------------------------------------------------


def assemble_start_command(
    worktree_path: Path,
    agent: Optional[str],
    extra_args: list[str],
    skip_onboard: bool,
    *,
    _get_default_agent=None,
    _get_vertex_config=None,
) -> tuple[str, list[str], dict[str, str]]:
    """Assemble (binary, argv, env_updates) for the agent launch.

    This is a **pure** function: it resolves paths and builds the command
    but does not mutate ``os.environ`` or launch any process.  The caller
    is responsible for applying ``env_updates`` and calling ``os.execvp``.

    No release-tag checkout is ever performed here (AC #15, #23).

    Args:
        worktree_path: Absolute path to the BERIL worktree directory.
        agent: Agent name (e.g. ``'claude'``, ``'codex'``).  ``None``
            means use the default from ``get_default_agent()``.
        extra_args: Extra arguments to forward to the agent (everything
            after ``--`` on the CLI).
        skip_onboard: When True, suppress the ``/berdl_start`` prompt
            injection.
        _get_default_agent: Injectable for testing (overrides the real
            ``beril_cli.config.get_default_agent``).
        _get_vertex_config: Injectable for testing (overrides the real
            ``beril_cli.config.get_vertex_config``).

    Returns:
        ``(binary, argv, env_updates)`` where:

        - *binary* is the resolved absolute path to the agent executable.
        - *argv* is the full ``argv`` list (``[agent_name, ...]``).
        - *env_updates* is a dict of environment variable overrides to
          apply before ``os.execvp``.

    Raises:
        RuntimeError: If the agent binary is not found on PATH.
    """
    # ------------------------------------------------------------------
    # Resolve agent name
    # ------------------------------------------------------------------
    if _get_default_agent is None:
        get_default_agent, get_vertex_config, _ = _import_beril_cli()
    else:
        get_default_agent = _get_default_agent
        get_vertex_config = _get_vertex_config  # type: ignore[assignment]

    resolved_agent = agent or get_default_agent()

    # ------------------------------------------------------------------
    # Resolve binary
    # ------------------------------------------------------------------
    binary = shutil.which(resolved_agent)
    if not binary:
        raise RuntimeError(
            f"Agent '{resolved_agent}' is not installed or not on PATH.\n"
            "Install it and try again, or choose a different agent with --agent."
        )

    # ------------------------------------------------------------------
    # Build argv — NO _checkout_release call here (AC #15, #23)
    # ------------------------------------------------------------------
    argv_extra = list(extra_args)

    if resolved_agent == "claude":
        # Inject /berdl_start onboard unless skipped or the user passed a prompt
        if not skip_onboard and not argv_extra:
            argv_extra = ["/berdl_start"]

        # Default to Opus model when --model was not explicitly supplied
        if "--model" not in argv_extra:
            argv_extra = ["--model", "opus", *argv_extra]

    # ------------------------------------------------------------------
    # Vertex env-key mapping (claude only, when enabled) — AC #16
    # Apply BERIL's EXACT key set from run_start; do NOT spread the whole
    # get_vertex_config() dict into the environment.
    # ------------------------------------------------------------------
    env_updates: dict[str, str] = {}

    if resolved_agent == "claude":
        vertex = get_vertex_config()
        if vertex.get("enabled"):
            creds = vertex.get("credentials_file", "")
            if creds and Path(creds).exists():
                env_updates["CLAUDE_CODE_USE_VERTEX"] = "1"
                env_updates["CLOUD_ML_REGION"] = vertex.get("region", "global")
                env_updates["ANTHROPIC_VERTEX_PROJECT_ID"] = vertex.get("project_id", "")
                env_updates["GOOGLE_APPLICATION_CREDENTIALS"] = creds
                env_updates["VERTEX_REGION_CLAUDE_HAIKU_4_5"] = "us-east5"
                env_updates["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = "claude-haiku-4-5@20251001"

    argv = [resolved_agent, *argv_extra]
    return binary, argv, env_updates


# ---------------------------------------------------------------------------
# Public launch function (calls os.execvp — not unit-tested beyond the guard)
# ---------------------------------------------------------------------------


def launch_start(
    worktree_path: Path,
    agent: Optional[str],
    extra_args: list[str],
    skip_onboard: bool,
) -> None:
    """Prepare and exec the agent inside *worktree_path*.

    1. Resolves the agent binary and builds ``(binary, argv, env_updates)``
       via :func:`assemble_start_command`.
    2. Changes the working directory to *worktree_path*.
    3. Calls ``_sync_auth_token`` on the worktree's ``.env``.
    4. Applies ``env_updates`` to ``os.environ``.
    5. Prints the "never run beril start inside a worktree" warning.
    6. Replaces the process via ``os.execvp`` (never returns on success).

    Args:
        worktree_path: Absolute path to the BERIL worktree directory.
        agent: Agent name or ``None`` (uses ``get_default_agent()``).
        extra_args: Extra arguments forwarded verbatim to the agent.
        skip_onboard: When True, suppress the ``/berdl_start`` injection.
    """
    get_default_agent, get_vertex_config, _sync_auth_token = _import_beril_cli()

    binary, argv, env_updates = assemble_start_command(
        worktree_path,
        agent,
        extra_args,
        skip_onboard,
        _get_default_agent=get_default_agent,
        _get_vertex_config=get_vertex_config,
    )

    os.chdir(worktree_path)
    _sync_auth_token(worktree_path / ".env")
    os.environ.update(env_updates)

    print(
        "Warning: do NOT run 'beril start' directly inside a worktree — it will "
        "run _checkout_release and detach your worktree off its project branch. "
        "Use 'kbu beril worktree start' instead."
    )

    os.execvp(binary, argv)  # pragma: no cover
