"""BERDL (BER Data Lakehouse) capability subpackage.

This package holds the pure-logic building blocks used by the BERDL skills
and, later, the ``BerdlCapability`` deep module described in
``agent-io/prds/berdl-lakehouse-skills/fullprompt.md``:

- :mod:`kbutillib.domains.kbase.berdl.naming` — dotted/underscored database
  name normalization and the ``my``/``{username}`` personal-catalog alias
  translation.
- :mod:`kbutillib.domains.kbase.berdl.membership` — ``ro``-suffix tenant
  membership decoding.
- :mod:`kbutillib.domains.kbase.berdl.tokens` — token resolution with fixed
  precedence (``KBASE_AUTH_TOKEN`` env var, then ``~/.kbase/token``, then
  config).

All three modules are pure logic: no network calls, no BERDL pod
dependency, and no imports of ``berdl_notebook_utils``. They are safe to run
in ordinary CI. Transports and the deep ``BerdlCapability`` module are out
of scope here and live elsewhere.
"""

from .membership import decode_memberships
from .naming import (
    NormalizedDatabase,
    normalize_databases,
    to_spark_alias,
    to_trino_alias,
)
from .tokens import NoTokenAvailableError, require_token, resolve_token

__all__ = [
    "NormalizedDatabase",
    "normalize_databases",
    "to_spark_alias",
    "to_trino_alias",
    "decode_memberships",
    "resolve_token",
    "require_token",
    "NoTokenAvailableError",
]
