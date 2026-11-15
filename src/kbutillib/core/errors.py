"""Canonical error types for KBUtilLib.

This is the single source of truth for ``BackendUnavailableError`` and its
sibling core errors.  Any module that previously defined its own copy should
re-import from here so ``isinstance`` checks work across module boundaries.
"""

from __future__ import annotations

__all__ = [
    "KBUtilLibError",
    "BackendUnavailableError",
    "CapabilityError",
    "CapabilityNotFound",
    "ReconstructionEnvironmentError",
    "MissingSolverError",
    "MissingBiochemistryError",
    "ModelSEEDpyCapabilityError",
]


class KBUtilLibError(Exception):
    """Base class for all KBUtilLib-specific exceptions."""


class BackendUnavailableError(KBUtilLibError):
    """Raised when a required backend dependency is not installed or reachable.

    Catch this error to implement graceful degradation — an unavailable backend
    should never crash ``import kbutillib`` or the capability registry.

    Parameters
    ----------
    backend:
        Human-readable name of the missing backend (e.g. ``"rdkit"``,
        ``"equilibrator"``).
    reason:
        Optional detail message explaining why the backend is unavailable.
    """

    def __init__(self, backend: str = "", reason: str = "") -> None:
        self.backend = backend
        self.reason = reason
        parts = [backend] if backend else []
        if reason:
            parts.append(reason)
        super().__init__(": ".join(parts) if parts else "Backend unavailable")

    def __repr__(self) -> str:  # pragma: no cover
        return f"BackendUnavailableError(backend={self.backend!r}, reason={self.reason!r})"


class CapabilityError(KBUtilLibError):
    """Base class for capability-registry errors."""


class CapabilityNotFound(CapabilityError):
    """Raised when a requested capability name does not exist in the registry.

    Parameters
    ----------
    name:
        The dotted capability name that was not found.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Capability not found: {name!r}")


class ReconstructionEnvironmentError(KBUtilLibError):
    """Base class for reconstruction-environment faults.

    The reconstruction layer (``domains/modeling/ms_reconstruction_utils.py``)
    refuses to run against an environment it has not positively verified, and
    every refusal is one of the three leaf classes below.  They are defined
    here, rather than in that module, because ``domains/biochem`` must raise
    :class:`MissingBiochemistryError` too and ``domains/modeling`` imports
    ``domains/biochem`` (``kb_model_utils.py:11``) -- the reverse import would
    be a cycle.  ``ms_reconstruction_utils`` re-exports all four names, so
    ``isinstance`` checks work from either side (see this module's docstring).

    A bare :class:`ReconstructionEnvironmentError` is raised for a dependency
    that is neither the biochemistry database nor the solver -- currently only
    ``cb_annotation_ontology_api``.
    """


class MissingSolverError(ReconstructionEnvironmentError):
    """Raised when no LP backend completed a real solve.

    Never raised on the strength of an import or of
    ``optlang.available_solvers``: KBDL issue #35 (poplar's Gurobi floating
    licence) is invisible to both.  Only a solve that returns ``optimal`` with
    the expected objective value counts as a working backend.
    """


class MissingBiochemistryError(ReconstructionEnvironmentError):
    """Raised when the ModelSEED biochemistry database cannot be loaded.

    This is the classified form of the failure KBDL issue #41 describes.  The
    issue's stated cause is fork-specific -- the fork's ``config.cfg`` sets
    ``[biochem] path = /deps/ModelSEEDDatabase`` while upstream ``c0b94e3``
    sets the relative ``data/ModelSEEDDatabase`` -- so the error message must
    report the path actually tried rather than quoting the issue.
    """


class ModelSEEDpyCapabilityError(ReconstructionEnvironmentError):
    """Raised when the installed ModelSEEDpy lacks a capability the layer needs.

    Established BEHAVIOURALLY, never from a version string or a signature: the
    fork and upstream both report ``modelseedpy 0.4.2`` while
    ``MSBuilder.build()`` differs in arity and return type, so a version is not
    an identification of the code.
    """
