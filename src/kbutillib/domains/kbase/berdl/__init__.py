"""BERDL (BER Data Lakehouse) capability subpackage.

This package holds the pure-logic building blocks used by the BERDL skills,
the two locus-specific transports, and the ``BerdlCapability`` deep module
described in ``agent-io/prds/berdl-lakehouse-skills/fullprompt.md``:

- :mod:`kbutillib.domains.kbase.berdl.naming` — dotted/underscored database
  name normalization and the ``my``/``{username}`` personal-catalog alias
  translation.
- :mod:`kbutillib.domains.kbase.berdl.membership` — ``ro``-suffix tenant
  membership decoding.
- :mod:`kbutillib.domains.kbase.berdl.tokens` — token resolution with fixed
  precedence (``KBASE_AUTH_TOKEN`` env var, then ``~/.kbase/token``, then
  config).
- :mod:`kbutillib.domains.kbase.berdl.transports` — :class:`InPodTransport`
  (wraps the pod-only ``berdl_notebook_utils`` package) and
  :class:`OffPodTransport` (pure-``requests`` REST client, read-only by
  construction) behind a common :class:`BerdlTransport` interface.
- :mod:`kbutillib.domains.kbase.berdl.capability` — :class:`BerdlCapability`,
  the deep module: locus detection, ``databases()``, ``memberships()``,
  ``load()`` (in-pod only, routed through ``data_lakehouse_ingest.ingest``),
  and ``query()``.

``naming``, ``membership``, and ``tokens`` are pure logic: no network
calls, no BERDL pod dependency, and no imports of ``berdl_notebook_utils``.
They are safe to run in ordinary CI. ``transports`` and ``capability`` import
``berdl_notebook_utils``/``data_lakehouse_ingest`` only lazily -- never at
module scope -- so importing this package never requires the pod package to
be installed, even though ``InPodTransport`` cannot be *constructed*, and
``BerdlCapability.load()`` cannot succeed, off-pod. ``capability``'s pure
config-building and mode-selection helpers (:func:`~capability.build_ingest_config`,
:func:`~capability.select_write_mode`) are unit-tested the same way.
"""

from .capability import (
    BerdlCapability,
    BerdlLoadRefusedError,
    BerdlMembershipUnavailableError,
    build_ingest_config,
    select_write_mode,
)
from .membership import decode_memberships
from .naming import (
    NormalizedDatabase,
    normalize_databases,
    to_spark_alias,
    to_trino_alias,
)
from .tokens import NoTokenAvailableError, require_token, resolve_token
from .transports import BerdlTransport, InPodTransport, OffPodTransport

__all__ = [
    "NormalizedDatabase",
    "normalize_databases",
    "to_spark_alias",
    "to_trino_alias",
    "decode_memberships",
    "resolve_token",
    "require_token",
    "NoTokenAvailableError",
    "BerdlTransport",
    "InPodTransport",
    "OffPodTransport",
    "BerdlCapability",
    "BerdlLoadRefusedError",
    "BerdlMembershipUnavailableError",
    "build_ingest_config",
    "select_write_mode",
]
