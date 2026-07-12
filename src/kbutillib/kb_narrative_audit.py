"""Render-from-ledger projector: EE2 job ledger -> ``KBaseNarrative.Narrative``
audit cells.

Ports narrative-connector's (nc) render-from-ledger pattern onto
KBUtilLib's SQLite job ledger (PRD ``ee2-narrative-provenance``, Piece B).
nc's ``_read_modify_write_once`` always strips every audit-tagged cell and
re-renders the COMPLETE block from the ledger on every write; that
invariant is what makes concurrent/last-writer-wins ``save_objects`` calls
converge instead of duplicate. See the nc reference (read-only pattern
source, not a dependency of KBUtilLib)::

    ~/king-stack/narrative-connector/src/narrative_connector/narrative_append.py

This module holds only **pure** functions -- no network I/O. The
read-modify-write orchestration (``get_objects2`` / ``save_objects``) lives
on :meth:`kbutillib.kb_ws_utils.KBWSUtilsImpl.append_app_run_audit`, which
imports these helpers.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, List, Optional

from .kb_job_utils.state import JobRecord, JobState

# ── stable per-run anchor ──────────────────────────────────────────────────

#: nc uses ``<!-- nc:app-run:<id> -->``; KBUtilLib uses a ``kbu:`` prefix so
#: its own audit cells are never confused with nc-authored ones in a shared
#: Narrative (PRD Piece B, "Stable per-run anchor").
_CELL_ANCHOR_PREFIX = "<!-- kbu:app-run:"
_CELL_ANCHOR_SUFFIX = " -->"


def app_run_cell_anchor(job_id: str) -> str:
    """The stable-anchor marker string for an app-run cell, keyed on the
    EE2 ``job_id`` -- embedded in the cell markdown so the cell resolves by
    content (scan), not by a positional index that Narrative edits would
    invalidate."""
    return f"{_CELL_ANCHOR_PREFIX}{job_id}{_CELL_ANCHOR_SUFFIX}"


def is_audit_cell(cell: dict) -> bool:
    """A cell is an "audit cell" iff its ``metadata.kbase.audit`` is
    truthy. Locks the discriminator the strip-then-rerender step uses;
    user-authored markdown and the Tier-1 intro (cell[0]) never carry the
    tag, so they survive every render."""
    return bool(((cell.get("metadata") or {}).get("kbase") or {}).get("audit"))


def find_audit_cell_index(cells: list, job_id: str) -> Optional[int]:
    """Resolve a rendered audit cell BY ITS ANCHOR (content), not by
    position. Returns the index of the cell carrying
    :func:`app_run_cell_anchor` for *job_id*, or ``None`` if absent."""
    anchor = app_run_cell_anchor(job_id)
    for idx, cell in enumerate(cells):
        src = cell.get("source") if isinstance(cell, dict) else None
        if isinstance(src, (list, tuple)):
            src = "".join(src)
        if isinstance(src, str) and anchor in src:
            return idx
    return None


# ── per-run cell rendering ─────────────────────────────────────────────────

#: Cap a single rendered param value (D54-style: "the cell is human
#: provenance, not a data store") so one oversized value can't balloon the
#: cell or trip the Workspace object-size ceiling.
_PARAM_VALUE_MAX = 200


def _render_value(value: Any) -> str:
    """One value -> its cell display string. Strings render verbatim;
    everything else renders as compact JSON (falling back to ``str()``);
    an oversized rendering is truncated with an explicit note."""
    if isinstance(value, str):
        rendered = value
    else:
        try:
            rendered = json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError):
            rendered = str(value)
    if len(rendered) > _PARAM_VALUE_MAX:
        kept = rendered[:_PARAM_VALUE_MAX]
        return f"{kept}… (truncated; {len(rendered)} chars total)"
    return rendered


def _params_summary(run_params: Any) -> str:
    """A compact one-line params summary (PRD: "compact params summary").

    ``run_params`` is ``JobRecord.params["params"]`` -- the EE2-shape
    positional params, normally a one-element ``list[dict]``. Keys are
    sorted for a deterministic, byte-identical re-render.
    """
    if not run_params:
        return ""
    if isinstance(run_params, list):
        run_params = run_params[0] if run_params else {}
    if not isinstance(run_params, dict):
        return _render_value(run_params)
    if not run_params:
        return ""
    return ", ".join(f"{k}={_render_value(v)}" for k, v in sorted(run_params.items()))


def _looks_like_upa(value: str) -> bool:
    """Honest UPA shape check: ``wsid/objid`` or ``wsid/objid/ver``, all
    numeric segments. Never fabricated -- just a pattern match over
    strings already present in the raw EE2 response."""
    parts = value.split("/")
    if len(parts) not in (2, 3):
        return False
    return all(part.isdigit() for part in parts)


def extract_output_upas(ee2_raw: Optional[dict]) -> List[str]:
    """Best-effort output UPA(s) from a terminal job's raw EE2 payload
    (``JobRecord.ee2_raw`` / the terminal ``JobReport.result``). Walks the
    raw structure for UPA-shaped strings; de-duplicates, preserves first-
    seen order. Never fabricates -- returns ``[]`` when nothing UPA-shaped
    is found."""
    if not ee2_raw:
        return []
    found: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            if _looks_like_upa(node):
                found.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v)

    _walk(ee2_raw)
    seen: set = set()
    ordered: List[str] = []
    for upa in found:
        if upa not in seen:
            seen.add(upa)
            ordered.append(upa)
    return ordered


def render_app_run_cell_markdown(record: JobRecord) -> str:
    """Build the markdown audit-cell source for one terminal ``JobRecord``.

    Content per the PRD: app_id, method, service_ver, job_id, terminal
    state, submit + finish timestamps, a compact params summary, output
    UPA(s), and the workspace -- all sourced from the ledger (no native
    ``kbase.appCell`` / ``method.<id>`` metadata is fabricated). The
    trailing line is the stable anchor (:func:`app_run_cell_anchor`).
    """
    run_params = record.params if isinstance(record.params, dict) else {}
    app_id = str(run_params.get("app_id") or record.method or "")
    service_ver = run_params.get("service_ver")
    outcome = "completed" if record.state == JobState.COMPLETED else record.state.value

    lines = [
        f"### App run: {app_id or record.method}",
        "",
        f"- **Method:** `{record.method}`",
    ]
    if service_ver:
        lines.append(f"- **Service version:** `{service_ver}`")
    lines.append(f"- **Job ID:** `{record.job_id}`")
    lines.append(f"- **State:** {outcome}")
    lines.append(f"- **Submitted:** {record.created_at.isoformat()}")
    lines.append(f"- **Finished:** {record.updated_at.isoformat()}")

    params_summary = _params_summary(run_params.get("params"))
    if params_summary:
        lines.append(f"- **Params:** {params_summary}")

    if record.state == JobState.ERROR and record.error_message:
        lines.append(f"- **Error:** {_render_value(record.error_message)}")

    output_upas = extract_output_upas(record.ee2_raw)
    if output_upas:
        lines.append(f"- **Output:** {', '.join(f'`{u}`' for u in output_upas)}")

    if record.workspace_id is not None:
        lines.append(f"- **Workspace:** {record.workspace_id}")

    lines.append("")
    lines.append(app_run_cell_anchor(record.job_id))
    return "\n".join(lines) + "\n"


def render_audit_cells(records: Iterable[JobRecord]) -> List[dict]:
    """The COMPLETE, freshly-rendered set of audit cells for *records*
    (already scoped to one workspace by the caller). Only TERMINAL jobs
    are rendered (a still-running job has nothing to report yet); sorted
    oldest-first by ``updated_at`` so re-renders are deterministic."""
    terminal = [r for r in records if r.state.is_terminal]
    terminal.sort(key=lambda r: (r.updated_at, r.job_id))
    return [
        {
            "cell_type": "markdown",
            "source": render_app_run_cell_markdown(r),
            "metadata": {"kbase": {"audit": True}},
        }
        for r in terminal
    ]


def data_dependencies_from_records(records: Iterable[JobRecord]) -> List[str]:
    """The Navigator's ``data_dependencies`` sample: every output UPA
    produced by a terminal job in *records*. Built from the SAME ledger
    slice the audit cells render from, so it self-heals + converges with
    the audit section on every render -- nothing fabricated."""
    deps: List[str] = []
    for record in records:
        if not record.state.is_terminal:
            continue
        deps.extend(extract_output_upas(record.ee2_raw))
    return deps


# ── Navigator metadata (ported from nc's compute_narrative_meta) ──────────

#: The KBase Workspace caps a metadata **key + value** at 900 BYTES. The
#: ``data_dependencies`` key is 17 bytes; leave headroom for JSON framing +
#: a trailing "+N more" marker so the serialized VALUE never trips the cap
#: regardless of how many objects a workspace's job ledger accrues.
_DATA_DEPS_KEY = "data_dependencies"
_DATA_DEPS_VALUE_BUDGET = 900 - len(_DATA_DEPS_KEY) - 40


def _bounded_data_dependencies(deps: List[str]) -> str:
    """A JSON list of object UPAs BOUNDED to stay under the KBase 900-byte
    key+value metadata cap. Includes as many as fit, then a final
    ``"+N more"`` marker carrying the remainder. Empty list -> ``"[]"``.
    Deterministic (*deps* is pre-sorted) so re-renders converge
    byte-identically."""
    if not deps:
        return "[]"
    chosen: List[str] = []
    for i, name in enumerate(deps):
        remaining = len(deps) - i
        marker = f"+{remaining} more"
        candidate = json.dumps(chosen + [name, marker])
        if len(candidate.encode("utf-8")) > _DATA_DEPS_VALUE_BUDGET:
            return json.dumps(chosen + [f"+{remaining} more"])
        chosen.append(name)
    return json.dumps(chosen)


def compute_narrative_meta(cells: list, data_dependencies: List[str]) -> dict:
    """The Narrative-object ``meta`` the KBase Navigator reads, recomputed
    on every render so a Narrative filled with real audit work never shows
    stale "0 cells / temporary" counts. All values are strings (the
    Workspace metadata contract is string->string). We write ONLY what can
    be legitimately computed from the cells + referenced objects -- no
    native ``method.<id>`` app-cell keys are fabricated (KBUtilLib records
    runs as audit markdown cells, not native KBase app cells).

    Keys:
      - ``cell_count`` -- total cells.
      - ``jupyter.markdown`` -- markdown-cell count.
      - ``data_object_count`` -- honest count of referenced objects.
      - ``data_dependencies`` -- a BOUNDED JSON sample (900-byte cap).
      - ``is_temporary`` -- ``"false"`` once real content exists.
    """
    cell_count = len(cells)
    markdown_count = sum(1 for c in cells if c.get("cell_type") == "markdown")
    deps = sorted(set(data_dependencies))
    return {
        "cell_count": str(cell_count),
        "jupyter.markdown": str(markdown_count),
        "data_object_count": str(len(deps)),
        "data_dependencies": _bounded_data_dependencies(deps),
        "is_temporary": "false",
    }


def to_latest_ref(upa: str) -> str:
    """Strip the version suffix from a ``ws/obj[/ver]`` UPA so
    ``get_objects2`` returns the LATEST version of the Narrative. The
    render always reads the head: a stale snapshot could lose cells that
    landed between a prior save and this call."""
    parts = upa.split("/")
    if len(parts) < 2:
        raise ValueError(f"invalid narrative ref {upa!r}; expected wsid/objid or wsid/objid/ver")
    return f"{parts[0]}/{parts[1]}"
