"""Re-export shim: the CLI API types' shared plumbing moved to ``llm``.

M2.1 moved this module to
:mod:`kbutillib.domains.ai.llm.apis.cli_common`.  See
:mod:`kbutillib.domains.ai.council.backends.http_common` for why.

``_child_env`` is re-exported despite its leading underscore because the CLI
backend test suite imports it by name to prove the child environment is built
from an allowlist; dropping it here would break that proof, which is the one
test standing between a CLI seat and metered API billing.
"""

from __future__ import annotations

from ...llm.apis.cli_common import (  # noqa: F401
    AUTH_PATTERNS,
    CHILD_ENV_ALLOWLIST,
    CLAUDE_EXTRA_ENV,
    CODEX_EXTRA_ENV,
    DENIED_BILLING_VARS,
    KILL_GRACE_S,
    OUTPUT_EXCERPT_CHARS,
    QUOTA_PATTERNS,
    CliBackend,
    _child_env,
    billing_preflight,
    classify_failure,
    dropped_billing_vars,
    run_cli,
    strip_fences,
)

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
    "run_cli",
    "strip_fences",
]
