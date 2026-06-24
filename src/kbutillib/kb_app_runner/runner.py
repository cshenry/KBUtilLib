"""AppRunner: submit any KBase app as an EE2 job.

:class:`AppRunner` hides NMS spec discovery, UI→service parameter
translation, workspace resolution, and EE2 submission behind a small
interface.  Callers compose it with :class:`~.monitor.JobMonitor` when
they need to wait for results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import AmbiguousParams, SpecNotFound
from .monitor import JobHandle
from .nms import NMSSpecCache

if TYPE_CHECKING:
    from ..kb_job_utils import KBJobUtils
    from ..kb_ws_utils import KBWSUtils

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExistingObject:
    """Returned by :meth:`AppRunner.run_app_if_missing` when the output
    object already exists in the workspace.

    Attributes:
        ref: Workspace ref of the existing object (``"wsid/objid/ver"``).
    """

    ref: str


@dataclass
class AppCall:
    """A single pending app invocation (for :meth:`AppRunner.run_apps_parallel`).

    Attributes:
        app_id: KBase app identifier.
        params: UI-shape or service-shape params.
        workspace: Numeric wsid or workspace name.
        pin_version: Optional service version override.
        meta: Caller metadata forwarded to the :class:`JobHandle`.
    """

    app_id: str
    params: dict | list
    workspace: int | str
    pin_version: str | None = None
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# AppRunner
# ---------------------------------------------------------------------------


class AppRunner:
    """Submit KBase apps as EE2 jobs with automatic spec discovery.

    Args:
        ws: A :class:`~kbutillib.kb_ws_utils.KBWSUtils` instance used
            for workspace resolution and idempotency checks.
        kb_version: KBase environment name (``"prod"``, ``"appdev"``, ``"ci"``).
        nms_cache: Optional pre-built :class:`~.nms.NMSSpecCache`.  If
            *None*, a new cache is created (sharing it across callers
            reduces NMS round-trips).
        job_store: A :class:`~kbutillib.kb_job_utils.KBJobUtils` instance
            used for EE2 submission.
    """

    def __init__(
        self,
        ws: "KBWSUtils",
        kb_version: str = "prod",
        nms_cache: NMSSpecCache | None = None,
        job_store: "KBJobUtils | None" = None,
    ) -> None:
        self._ws = ws
        self._kb_version = kb_version
        self._nms = nms_cache if nms_cache is not None else NMSSpecCache()
        self._jobs = job_store

    # ── public API ────────────────────────────────────────────────────────

    def run_app(
        self,
        app_id: str,
        params: dict | list,
        workspace: int | str,
        *,
        pin_version: str | None = None,
        tag: str | None = None,
        meta: dict | None = None,
    ) -> JobHandle:
        """Submit an EE2 job for *app_id*.

        **Param-shape detection (in order):**

        1. ``params`` is a ``list[dict]`` → service-shape; pass through
           unchanged.
        2. ``params`` is a ``dict`` with both UI keys (matching an
           ``input_parameter`` narrative_name in the NMS spec) and non-UI
           keys → raise :exc:`~.errors.AmbiguousParams`.
        3. ``params`` is a ``dict`` and every key is a UI key → UI→service
           remap via ``kb_service_input_mapping``; wrapped into a
           one-element list.
        4. ``params`` is a ``dict`` and no key is a UI key → service-shape;
           wrapped into a one-element list.

        Args:
            app_id: KBase app identifier (e.g. ``"kb_fastqc/runFastQC"``).
            params: UI-shape dict or service-shape dict/list.
            workspace: Numeric wsid or workspace name string.
            pin_version: Override the NMS-supplied ``service_ver``.
            tag: Release tag (``"release"``/``"beta"``/``"dev"``) for the NMS
                spec lookup.  Required for beta/dev-only apps; ``None`` uses the
                released spec.
            meta: Arbitrary metadata forwarded to the :class:`JobHandle`.

        Returns:
            A :class:`JobHandle` wrapping the submitted EE2 job_id.

        Raises:
            SpecNotFound: If NMS does not recognise *app_id* (for the given tag).
            AmbiguousParams: If *params* contains both UI and service keys.
        """
        if self._jobs is None:
            raise RuntimeError("AppRunner requires a job_store (KBJobUtils) to submit jobs.")

        spec = self._nms.get(app_id, tag)
        ui_names = spec.narrative_names()

        params_list = self._build_params_list(app_id, params, ui_names, spec)

        wsid = self._resolve_wsid(workspace)
        service_ver = pin_version or spec.service_ver

        logger.info(
            "Submitting app=%s method=%s ws=%d ver=%s",
            app_id,
            spec.method,
            wsid,
            service_ver,
        )

        record = self._jobs.run_job(
            spec.method,
            params_list,
            app_id=app_id,
            workspace_id=wsid,
            service_ver=service_ver,
            meta=meta or {},
        )

        return JobHandle(
            job_id=record.job_id,
            app_id=app_id,
            wsid=wsid,
            meta=meta or {},
        )

    def run_app_if_missing(
        self,
        app_id: str,
        params: dict | list,
        workspace: int | str,
        *,
        output_name: str,
        output_type: str,
        pin_version: str | None = None,
        meta: dict | None = None,
    ) -> JobHandle | ExistingObject:
        """Idempotency wrapper around :meth:`run_app`.

        Checks whether an object named *output_name* of type *output_type*
        already exists in the workspace.  If it does, returns an
        :class:`ExistingObject` and does **not** submit any EE2 job.
        Otherwise submits and returns a :class:`JobHandle`.

        The caller can treat both return types uniformly via the ``.ref``
        attribute (for :class:`ExistingObject`) or by waiting on the
        :class:`JobHandle` and using its result.

        Args:
            app_id: KBase app identifier.
            params: UI-shape or service-shape params.
            workspace: Numeric wsid or workspace name string.
            output_name: Canonical output object name to check.
            output_type: Expected KBase type (e.g. ``"KBaseReport.Report"``).
            pin_version: Optional service version override.
            meta: Caller metadata.

        Returns:
            :class:`ExistingObject` if the output already exists, or
            :class:`JobHandle` if a new job was submitted.
        """
        wsid = self._resolve_wsid(workspace)

        # list_ws_objects returns a dict keyed by object name.
        existing = self._ws.list_ws_objects(wsid, type=output_type)
        if output_name in existing:
            obj_info = existing[output_name]
            # KBase object_info tuple: (objid, name, type, date, ver, owner, wsid, ...)
            ws_ref = f"{obj_info[6]}/{obj_info[0]}/{obj_info[4]}"
            logger.info("SKIP %s: %s already exists at %s", app_id, output_name, ws_ref)
            return ExistingObject(ref=ws_ref)

        return self.run_app(
            app_id,
            params,
            workspace,
            pin_version=pin_version,
            meta=meta,
        )

    def run_apps_parallel(self, calls: list[AppCall]) -> list[JobHandle]:
        """Submit multiple app calls and return their handles.

        Each call is submitted independently in sequence (EE2 itself queues
        them for parallel execution).  Returns one :class:`JobHandle` per
        :class:`AppCall`, in the same order.
        """
        handles: list[JobHandle] = []
        for call in calls:
            handle = self.run_app(
                call.app_id,
                call.params,
                call.workspace,
                pin_version=call.pin_version,
                meta=call.meta,
            )
            handles.append(handle)
        return handles

    # ── internals ─────────────────────────────────────────────────────────

    def _resolve_wsid(self, workspace: int | str) -> int:
        """Return a numeric workspace ID.

        If *workspace* is already an ``int`` return it directly; otherwise
        call :meth:`~kbutillib.kb_ws_utils.KBWSUtils.set_ws` to resolve
        the name and retrieve the numeric ID.
        """
        if isinstance(workspace, int):
            return workspace
        try:
            workspace_int = int(workspace)
            return workspace_int
        except (ValueError, TypeError):
            pass
        self._ws.set_ws(workspace)
        return self._ws.ws_id

    def _build_params_list(
        self,
        app_id: str,
        params: dict | list,
        ui_names: frozenset[str],
        spec: Any,
    ) -> list:
        """Apply the 4-branch param detection rule and return a params list."""
        # Branch 1: already a list → service-shape passthrough.
        if isinstance(params, list):
            return params

        # Dict-based branches (2-4).
        param_keys = set(params.keys())
        ui_keys = param_keys & ui_names
        service_keys = param_keys - ui_names

        # Branch 2: mixed UI and service keys → ambiguous.
        if ui_keys and service_keys:
            raise AmbiguousParams(app_id, params, sorted(ui_keys), sorted(service_keys))

        # Branch 3: all keys are UI keys → remap through input_mapping.
        if ui_keys and not service_keys:
            remapped = _apply_input_mapping(params, spec.input_mapping)
            return [remapped]

        # Branch 4: no UI keys → service-shape dict; wrap in list.
        return [params]


# ---------------------------------------------------------------------------
# UI→service parameter remapping
# ---------------------------------------------------------------------------


def _apply_input_mapping(
    ui_params: dict[str, Any],
    input_mapping: tuple,
) -> dict[str, Any]:
    """Translate UI-shape params through the NMS ``kb_service_input_mapping``.

    Each mapping entry has the form::

        {
            "input_parameter": "<ui_param_name>",  # or "narrative_system_variable"
            "target_property": "<service_param_name>",
            "target_type_transform": "<optional>",  # ignored at this layer
        }

    Entries without ``input_parameter`` (e.g. ``narrative_system_variable``)
    are system-injected values the Narrative UI populates automatically;
    they are skipped here because the ``workspace=`` argument handles
    workspace injection at the EE2 call site.

    Args:
        ui_params: Caller-supplied UI-shape params dict.
        input_mapping: The ``kb_service_input_mapping`` tuple from
            :class:`~.nms.AppSpec`.

    Returns:
        Service-shape params dict with keys renamed per the mapping.
    """
    service_params: dict[str, Any] = {}
    for entry in input_mapping:
        if "input_parameter" not in entry:
            # System-variable (e.g. workspace) — skip; caller supplies workspace separately.
            continue
        ui_key: str = entry["input_parameter"]
        target: str = entry["target_property"]
        if ui_key in ui_params:
            service_params[target] = ui_params[ui_key]
    return service_params
