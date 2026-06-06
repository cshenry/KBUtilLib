"""Exceptions for the kb_app_runner module."""

from __future__ import annotations

from typing import Any


class AppRunnerError(Exception):
    """Base exception for kb_app_runner errors."""


class SpecNotFound(AppRunnerError):
    """Raised when NMS cannot find the requested app spec.

    Args:
        app_id: The KBase app identifier that was not found.
        detail: Additional detail from the NMS response.
    """

    def __init__(self, app_id: str, detail: str = "") -> None:
        self.app_id = app_id
        self.detail = detail
        msg = f"NMS spec not found for app '{app_id}'"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class AmbiguousParams(AppRunnerError):
    """Raised when a params dict contains both UI keys and service keys.

    Mixed shapes are never silently translated — the caller must pass a
    clean UI dict, a clean service dict, or a service list.

    Args:
        app_id: The KBase app identifier.
        params: The offending params dict.
        ui_keys: Keys that matched NMS narrative_name entries.
        service_keys: Keys that did not match any narrative_name.
    """

    def __init__(
        self,
        app_id: str,
        params: dict,
        ui_keys: list[str],
        service_keys: list[str],
    ) -> None:
        self.app_id = app_id
        self.params = params
        self.ui_keys = ui_keys
        self.service_keys = service_keys
        super().__init__(
            f"Ambiguous params for '{app_id}': "
            f"ui_keys={ui_keys!r}, service_keys={service_keys!r}. "
            "Pass either all UI keys or all service keys, not both."
        )


class JobFailed(AppRunnerError):
    """Raised when a monitored job terminates with an error state.

    Args:
        job_id: The EE2 job identifier.
        error: Error message from EE2.
        tail: Last N log lines from the job container.
    """

    def __init__(self, job_id: str, error: str = "", tail: list[str] | None = None) -> None:
        self.job_id = job_id
        self.error = error
        self.tail = tail or []
        msg = f"EE2 job '{job_id}' failed"
        if error:
            msg += f": {error}"
        super().__init__(msg)
