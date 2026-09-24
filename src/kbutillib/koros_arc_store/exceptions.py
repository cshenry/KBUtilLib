"""Exception hierarchy for the KOROS arc store.

Consumers of :mod:`kbutillib.koros_arc_store` catch these by name, so the
class names and their meanings are part of the module's stable contract.

The whole reason there is no hard-coded runs-root fallback is that an app
must be able to tell "the workspace is misconfigured" (a
:class:`RunsRootResolutionError`) apart from "the workspace is empty" (a
successful enumeration that yields nothing). Guessing a directory would
collapse that distinction.
"""

from __future__ import annotations


class KorosArcStoreError(Exception):
    """Base class for every error raised by :mod:`kbutillib.koros_arc_store`."""


class RunsRootResolutionError(KorosArcStoreError):
    """No KOROS runs root could be resolved from the resolution chain.

    Raised when none of ``--runs-root``, ``$KOROS_HOME/runs`` or
    ``$KING_KOROS_RUNS`` yields an existing directory. Apps catch this to
    render "misconfigured" distinctly from "empty".
    """


class RecordValidationError(KorosArcStoreError):
    """A record failed validation.

    Carries a stable ``code`` so callers can branch on the reason without
    parsing prose. This is *not* raised during enumeration — enumeration and
    :meth:`KorosArcStore.read_arc` mark invalid records with an
    ``invalid_reason`` instead of raising. It exists for callers that want an
    explicit, raising validation path.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class RecordNotFound(KorosArcStoreError):
    """A requested project or arc does not exist in the runs tree."""


class ContractVersionMismatch(KorosArcStoreError):
    """The on-disk contract version differs from the version this code expects."""

    def __init__(self, expected: int, found: int) -> None:
        self.expected = expected
        self.found = found
        super().__init__(
            f"contract version mismatch: expected {expected}, found {found}"
        )
