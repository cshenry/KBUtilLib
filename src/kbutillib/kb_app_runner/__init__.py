"""KBase app-runner: generic EE2 job submission and monitoring.

This package provides a high-level interface for running any KBase app
via the Execution Engine 2 (EE2) without hand-coding method names,
service versions, or parameter remapping.

Public API
----------
:class:`AppRunner`
    Submit an EE2 job for any KBase app, with automatic spec discovery
    and UI→service parameter translation.

:class:`NMSSpecCache`
    In-process cache of NMS ``get_method_spec`` responses.

:class:`AppSpec`
    Frozen dataclass representing a single NMS app spec.

:class:`JobMonitor`
    Poll EE2 in batches and return :class:`JobReport` objects once all
    submitted handles reach a terminal state.

:class:`JobHandle`
    Lightweight reference to a submitted EE2 job.

:class:`JobReport`
    Result of monitoring a single :class:`JobHandle`.

:class:`ExistingObject`
    Returned by :meth:`AppRunner.run_app_if_missing` when the named
    output object already exists in the workspace.

:exc:`AmbiguousParams`
    Raised when a params dict contains both UI and service keys.

:exc:`SpecNotFound`
    Raised when NMS cannot find the requested app spec.

:exc:`JobFailed`
    Raised when a monitored job terminates in an error state.
"""

from .errors import AmbiguousParams, AppRunnerError, JobFailed, SpecNotFound
from .monitor import JobHandle, JobMonitor, JobReport
from .nms import AppSpec, NMSSpecCache
from .runner import AppCall, AppRunner, ExistingObject

__all__ = [
    # Core classes
    "AppRunner",
    "NMSSpecCache",
    "JobMonitor",
    # Data types
    "JobHandle",
    "JobReport",
    "AppSpec",
    "ExistingObject",
    "AppCall",
    # Errors
    "AppRunnerError",
    "AmbiguousParams",
    "SpecNotFound",
    "JobFailed",
]
