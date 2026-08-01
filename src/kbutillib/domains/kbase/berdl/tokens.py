"""BERDL auth-token resolution with fixed precedence.

``KBBERDLUtils`` (``kb_berdl_utils.py``) today resolves a token only from
``~/.kbase/token`` (via ``SharedEnvUtils.get_token``) and raises
``"No KBase token available"`` even **in-pod**, where the BERDL JupyterHub
environment sets ``KBASE_AUTH_TOKEN`` and it works. This module fixes that
gap by resolving in this fixed order (see
``agent-io/prds/berdl-lakehouse-skills/fullprompt.md``, "Token
resolution"):

1. The ``KBASE_AUTH_TOKEN`` environment variable.
2. The contents of a token file, ``~/.kbase/token`` by default.
3. A pre-resolved config-supplied value.

A resolved token value must never be logged, printed, included in a
``repr()``, or written to any artifact by this module. Functions here only
ever *return* the value; they never format it into a message or pass it to
a logger.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

#: The environment variable BERDL sets in-pod, checked first.
KBASE_AUTH_TOKEN_ENV_VAR = "KBASE_AUTH_TOKEN"

#: Default on-disk token file location, checked second.
DEFAULT_TOKEN_FILE = Path.home() / ".kbase" / "token"


class NoTokenAvailableError(RuntimeError):
    """Raised when no token could be resolved from any configured source.

    The message deliberately names only the *sources checked*, never a
    value -- there is never a token value to include when this is raised.
    """

    def __init__(self, sources_checked: tuple[str, ...]) -> None:
        self.sources_checked = sources_checked
        checked = ", ".join(sources_checked)
        super().__init__(
            f"No BERDL/KBase token available. Checked, in order: {checked}. "
            f"Set {KBASE_AUTH_TOKEN_ENV_VAR} in the environment, write a "
            f"token to the token file, or supply one via config."
        )


def resolve_token(
    env: Mapping[str, str] | None = None,
    token_file: str | Path | None = DEFAULT_TOKEN_FILE,
    config_token: str | None = None,
) -> str | None:
    """Resolve a BERDL/KBase auth token using fixed precedence.

    Precedence, first non-empty value wins:

    1. ``env[KBASE_AUTH_TOKEN_ENV_VAR]`` (``KBASE_AUTH_TOKEN`` by default).
    2. The contents of ``token_file`` (``~/.kbase/token`` by default),
       stripped of surrounding whitespace.
    3. ``config_token``, an already-resolved value from the caller's config
       layer (e.g. ``get_config_value("berdl.token")``). This function does
       not read config itself, so as to stay dependency-free and pure.

    Args:
        env: Environment mapping to read from. Defaults to ``os.environ``.
        token_file: Path to a token file to read as a fallback. Pass
            ``None`` to skip the file source entirely. Defaults to
            ``~/.kbase/token``.
        config_token: A pre-resolved config value to fall back to last.

    Returns:
        The resolved token string, or ``None`` if no source yielded one.
        Never logs, prints, or otherwise emits the value.
    """
    active_env = os.environ if env is None else env
    env_value = active_env.get(KBASE_AUTH_TOKEN_ENV_VAR)
    if env_value:
        return env_value

    if token_file is not None:
        try:
            file_value = Path(token_file).read_text().strip()
        except OSError:
            file_value = ""
        if file_value:
            return file_value

    if config_token:
        return config_token

    return None


def require_token(
    env: Mapping[str, str] | None = None,
    token_file: str | Path | None = DEFAULT_TOKEN_FILE,
    config_token: str | None = None,
) -> str:
    """Resolve a token, raising if none is available.

    Same precedence and arguments as :func:`resolve_token`.

    Returns:
        The resolved token string.

    Raises:
        NoTokenAvailableError: If no source (env, file, config) yielded a
            token. The error message names the sources checked, never a
            value.
    """
    token = resolve_token(env=env, token_file=token_file, config_token=config_token)
    if not token:
        raise NoTokenAvailableError(
            (
                f"env:{KBASE_AUTH_TOKEN_ENV_VAR}",
                f"file:{token_file}",
                "config",
            )
        )
    return token
