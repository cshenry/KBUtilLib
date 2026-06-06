"""NMS (Narrative Method Store) spec cache for kb_app_runner.

Fetches the NMS ``get_method_spec`` RPC once per app_id per process and
caches the result in-memory.  No disk persistence — specs change rarely
and an in-process cache is sufficient for notebook and pipeline use.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import requests

from .errors import SpecNotFound

logger = logging.getLogger(__name__)

_NMS_RPC = "https://kbase.us/services/narrative_method_store/rpc"


@dataclass(frozen=True)
class AppSpec:
    """Parsed representation of a single NMS app spec.

    Attributes:
        app_id: KBase app identifier (e.g. ``"kb_fastqc/runFastQC"``).
        method: Fully-qualified EE2 method name (e.g. ``"kb_fastqc.runFastQC"``).
        service_ver: Git commit hash pinned by NMS.
        input_mapping: Raw ``kb_service_input_mapping`` list from the NMS spec.
        parameter_groups: Raw ``parameter_groups`` list from the NMS spec.
        raw: The full NMS ``get_method_spec`` response dict.
    """

    app_id: str
    method: str
    service_ver: str
    input_mapping: tuple  # list[dict] frozen as tuple
    parameter_groups: tuple  # list[dict] frozen as tuple
    raw: dict

    def narrative_names(self) -> frozenset[str]:
        """Return the set of UI parameter names from input_mapping.

        Each entry with an ``input_parameter`` key corresponds to a user-
        supplied UI field.  Entries with ``narrative_system_variable`` are
        system-injected (e.g. workspace) and are not caller-supplied.
        """
        names: set[str] = set()
        for entry in self.input_mapping:
            if "input_parameter" in entry:
                names.add(entry["input_parameter"])
        return frozenset(names)


class NMSSpecCache:
    """In-process cache of NMS ``get_method_spec`` responses.

    Usage::

        cache = NMSSpecCache()
        spec = cache.get("kb_fastqc/runFastQC")
        # spec.method, spec.service_ver, spec.input_mapping, ...

    Args:
        nms_rpc: Override the NMS JSON-RPC endpoint URL.
    """

    NMS_RPC = _NMS_RPC

    def __init__(self, nms_rpc: str | None = None) -> None:
        self._url = nms_rpc or self.NMS_RPC
        self._cache: dict[str, AppSpec] = {}

    # ── public API ────────────────────────────────────────────────────────

    def get(self, app_id: str) -> AppSpec:
        """Return the :class:`AppSpec` for *app_id*, using the in-process cache.

        Issues exactly one NMS ``get_method_spec`` RPC per *app_id* across
        the lifetime of this :class:`NMSSpecCache` instance.

        Raises:
            SpecNotFound: If NMS returns an error or an empty spec list.
        """
        if app_id not in self._cache:
            self._cache[app_id] = self.get_uncached(app_id)
        return self._cache[app_id]

    def get_uncached(self, app_id: str) -> AppSpec:
        """Fetch and parse the NMS spec for *app_id*, bypassing the cache.

        Raises:
            SpecNotFound: If NMS returns an error or an empty spec list.
        """
        logger.debug("NMS get_method_spec: %s", app_id)
        payload = {
            "version": "1.1",
            "method": "NarrativeMethodStore.get_method_spec",
            "params": [{"ids": [app_id]}],
        }
        try:
            resp = requests.post(self._url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise SpecNotFound(app_id, str(exc)) from exc

        if "error" in data:
            err = data["error"]
            raise SpecNotFound(app_id, str(err.get("message", err)))

        try:
            specs = data["result"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise SpecNotFound(app_id, f"unexpected NMS response shape: {exc}") from exc

        if not specs:
            raise SpecNotFound(app_id, "NMS returned empty spec list")

        raw_spec = specs[0]
        return _parse_spec(app_id, raw_spec)

    def clear(self) -> None:
        """Evict all cached entries."""
        self._cache.clear()


# ── internal helpers ──────────────────────────────────────────────────────────


def _parse_spec(app_id: str, raw: dict) -> AppSpec:
    """Parse a raw NMS spec dict into an :class:`AppSpec`.

    Extracts ``method``, ``service_ver``, ``input_mapping``, and
    ``parameter_groups`` from the NMS spec JSON structure.
    """
    behavior: dict[str, Any] = raw.get("behavior", {})

    module_name: str = behavior.get("kb_service_name", "")
    method_name: str = behavior.get("kb_service_method", "")
    if not module_name or not method_name:
        # Fallback: derive from info block
        info: dict[str, Any] = raw.get("info", {})
        raw_id: str = info.get("id", app_id)
        parts = raw_id.replace("/", ".").split(".")
        if len(parts) >= 2:
            module_name = module_name or parts[0]
            method_name = method_name or parts[-1]

    method = f"{module_name}.{method_name}"

    service_ver: str = behavior.get(
        "kb_service_version",
        raw.get("info", {}).get("git_commit_hash", ""),
    )

    input_mapping: list[dict] = behavior.get("kb_service_input_mapping", [])
    parameter_groups: list[dict] = raw.get("parameter_groups", [])

    return AppSpec(
        app_id=app_id,
        method=method,
        service_ver=service_ver,
        input_mapping=tuple(input_mapping),
        parameter_groups=tuple(parameter_groups),
        raw=raw,
    )
