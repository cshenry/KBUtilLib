# PRD — Optional KBase-Narrative Provenance for KBUtilLib's EE2 App Runner

**Repo:** KBUtilLib
**Prior analysis (read first):** `agent-io/plans/2026-07-11-ee2-narrative-provenance.md`
**Reference implementation (read-only, for pattern only):** `~/king-stack/narrative-connector` (nc) — specifically `src/narrative_connector/narrative_append.py` (render-from-ledger) and `src/narrative_connector/clients/narrative_service.py` (create_new_narrative).

---

## Problem Statement

KBUtilLib runs KBase apps through EE2 (`AppRunner` + `KBJobUtils`) and tracks every job in a durable local SQLite ledger (`~/.kbjobs/kbjobs.db`), but it writes **nothing** back to KBase. Two consequences:

1. **Outputs carry no lineage.** Objects saved through KBUtilLib are stamped with `"provenance": []` — the `save_ws_object` provenance line is hard-coded empty with `get_provenance()` commented out — even though `get_provenance()` already builds the correct KBase prov-action shape. So an output saved via KBUtilLib does not show "created by app X from inputs Y" anywhere in KBase.
2. **There is no trace of the run in the Narrative itself.** Adam's `narrative-connector` (nc) additionally projects its run ledger into the `KBaseNarrative.Narrative` object as a markdown audit trail, so opening the Narrative in the KBase UI shows what was run. KBUtilLib has the *superior* ledger (queryable SQLite, with a reserved-but-unused `narrative_id` column) but never projects it.

Chris wants that "trace in the narrative itself" in KBUtilLib — **opt-in, default-off** — without disturbing the existing run path or KBUtilLib's composable `*UtilsImpl` idiom.

## Solution

Add provenance as two strictly-independent, opt-in-respecting slices:

- **Piece A — object-level provenance (canonical, unconditional-by-default-when-context-present).** Wire the existing `get_provenance()` into `save_ws_object` so outputs saved through KBUtilLib carry real KBase `prov_actions` whenever a provenance context has been set. Default behavior for callers who never set provenance is unchanged (empty provenance). Fold in a latent-bug fix in `get_provenance()`.

- **Piece B — narrative audit-cell trace (the nc feature, ported).** A new opt-in `record_narrative_provenance=False` flag on `AppRunner.run_app`. When enabled, KBUtilLib resolves (or creates) a Narrative for the target workspace, stamps the ledger's `narrative_id`, and — on job completion — renders the complete audit block **from the SQLite ledger** into the Narrative object as markdown cells, idempotently and self-healingly, exactly mirroring nc's render-from-ledger design. The run path is untouched: the render hangs off the existing post-terminal `JobMonitor.wait_all(on_progress=...)` callback via a callback factory.

Both slices honor the composable idiom (new behavior lives on `KBWSUtilsImpl` / `AppRunner`, composed not inherited) and keep provenance strictly opt-in: nothing new fires unless the caller sets a provenance context (A) or passes `record_narrative_provenance=True` (B).

## User Stories

1. As a KBUtilLib user running an app that saves an output object, I want that object stamped with real KBase provenance (service, method, params, input objects, version), so that the output's lineage is visible everywhere in KBase — without my doing anything beyond the provenance context KBUtilLib already tracks.
2. As a KBUtilLib user who has *not* set any provenance context, I want `save_ws_object` to behave exactly as before (empty provenance), so this change is non-breaking.
3. As a maintainer, I want `get_provenance()` to emit the `service` value I passed to `set_provenance()`, so the prov-action `service` field is correct rather than silently falling back to the util class name.
4. As a KBUtilLib user running an app, I want an opt-in `record_narrative_provenance=True` flag on `run_app`, so that by default nothing is written to any Narrative and my current workflows are unaffected.
5. As a user who opts in, I want the run recorded as a markdown audit cell in the workspace's Narrative, so that opening the Narrative in the KBase UI shows what app ran, when, with what params, and what output UPA it produced.
6. As a user who opts in and runs the same app-set repeatedly (retries, re-runs, `run_apps_parallel`), I want the audit block to be idempotent and self-healing — re-rendering replaces the audit cells rather than duplicating them — so the Narrative never accumulates stale or doubled audit entries, even though Workspace Narrative saves are last-writer-wins with no optimistic lock.
7. As a user opting in against a **bare workspace with no Narrative**, I want KBUtilLib to auto-create one Narrative for that workspace, so the feature works without my manually pre-creating a Narrative.
8. As a user who already has a Narrative (or a specific one I want to target), I want to pass an explicit `narrative_ref` that takes precedence over discovery/auto-create, so I control exactly where the trace lands.
9. As a user, I want the audit cells clearly tagged (`metadata.kbase.audit=True`) and anchored by a stable per-run marker, so my own markdown/intro cells are never stripped and audit cells resolve by content, not by index.
10. As a user, I want the Narrative-object metadata the KBase Navigator reads (cell counts, `is_temporary=false`) recomputed on each render and kept under KBase's 900-byte metadata cap, so a Narrative filled with real audit work does not display stale "0 cells / temporary" counts and no save fails from metadata overflow.
11. As a user monitoring jobs myself via `JobMonitor.wait_all`, I want a ready-made `on_progress` callback I can pass in, so I can wire the audit render into my existing monitoring loop without adopting a new run/monitor entry point.
12. As a maintainer, I want Piece A to land first and independently of any Piece B decision, so the canonical object-provenance win ships regardless of the narrative-trace work.
13. As a maintainer, I want NarrativeService resolved dynamically through the existing ServiceWizard endpoint, so I don't hard-code a URL that breaks on redeploy.

## Implementation Decisions

### Scope split & ordering
- **Piece A ships first** (Phase 1). **Piece B depends on Piece A** (Phase 2) — not for logic reasons (they are behaviorally independent) but because both edit `kb_ws_utils.py` (A patches `save_ws_object`; B adds `append_app_run_audit` to `KBWSUtilsImpl`), so sequencing avoids a merge conflict.

### Piece A — object-level provenance
- **Two edit sites, one behavior.** `save_ws_object` exists on both `KBWSUtils` and its composition twin `KBWSUtilsImpl`, each hard-coding `"provenance": []`. Both must emit `self.get_provenance()` **when a provenance context is present**, else keep `[]`.
- **"Context present" guard.** Emit `get_provenance()` only when `self.method` has been set to a real value (i.e. `self.method not in (None, "", "Unknown")`); otherwise emit `[]`. This preserves current behavior for callers who never call `set_provenance()` / `initialize_call()`, satisfying the non-breaking requirement (stories 2). Do **not** add a separate boolean flag — the existing `method` sentinel is the context signal.
- **Latent-bug fix (story 3).** `set_provenance(service=...)` stores `self.service`, but `get_provenance()` currently emits `"service": self.name` and ignores `self.service`. Fix `get_provenance()` so the prov-action `service` field is `self.service` when it has been set to a real value (`not in (None, "", "Unknown")`), falling back to `self.name` otherwise. Leave `service_ver` sourced from `self.version`. Do not change the prov-action key names (they match the KBase Workspace `ProvenanceAction` shape).
- **Prov-action shape is already correct** (`description`, `input_ws_objects`, `method`, `script_command_line`, `method_params`, `service`, `service_ver`) — do not restructure it, only fix the `service` value source.
- **Out of scope for A:** the third bare `"provenance": []` in `kb_annotation_utils.py` is a different call site (not `save_ws_object`) and is explicitly not touched.

### Piece B — narrative audit-cell trace

**Cell format — markdown (decided):** Audit cells are **markdown**, tagged `metadata.kbase.audit=True`. KBUtilLib does **not** fabricate native `kbase.appCell` / `method.<id>` metadata; the runs are intentionally not re-runnable from the UI, and the Navigator's "App cells" count honestly stays 0. Native re-runnable cells are out of scope.

**Narrative resolution & scope — one-per-workspace (decided):** When `record_narrative_provenance=True`, resolve the target Narrative in this precedence:
1. Explicit `narrative_ref` argument (wins outright).
2. Else discover an existing Narrative in the target workspace (the workspace's `narrative` metadata pointer / a `KBaseNarrative.Narrative` object in that ws).
3. Else auto-create one via NarrativeService `create_new_narrative`.

Scope is **one Narrative per workspace** — KBase's own model — not one-per-session. `JobRecord` already carries `workspace_id`; the render filters the ledger by it.

**Submit-time resolution + narrative_id stamping:** Narrative resolution/creation happens **at submit time** inside `run_app` when the flag is set, so `JobRecord.narrative_id` (the reserved column) is populated with the resolved Narrative's object id before the job even runs. The flag, the `narrative_ref`, and the resolved `narrative_id` are also stashed on `JobHandle.meta` so a downstream monitor callback can find them. `run_app` itself remains **submit-only** — it does not wait or render.

**Flag propagation:** Add `record_narrative_provenance: bool = False` and `narrative_ref: str | None = None` to `AppRunner.run_app`. `run_app_if_missing` and `run_apps_parallel` forward them (add the two fields to `AppCall`).

**Render seam — callback factory (decided):** Add `AppRunner.audit_callback(narrative_ref: str | None = None) -> Callable[[JobReport], None]`. It returns an `on_progress` function the caller passes to `JobMonitor.wait_all(on_progress=...)`. On each **terminal** report, the callback (for success, and also for error/terminated so failures are visible) calls `append_app_run_audit(narrative_ref_or_from_handle_meta, report.handle.wsid)`. Non-terminal reports are ignored. This keeps the run path untouched and matches the plan's "hang off the existing on_progress" intent while remaining fully composable. (A combined `run_and_wait` convenience is explicitly NOT part of this slice.)

**New capability method — `KBWSUtilsImpl.append_app_run_audit(narrative_ref, workspace_id)`:** A single read-modify-write, ported from nc's `_read_modify_write_once`:
1. `get_objects2` the **latest** version of the Narrative (strip any stale version suffix from the ref first).
2. Preserve cell[0] (intro). From the remaining cells, **strip every cell whose `metadata.kbase.audit` is truthy**.
3. Re-render the **complete** audit block from the SQLite `jobs` table filtered by `workspace_id` (newest-relevant ordering), one markdown cell per run, each carrying `metadata.kbase.audit=True` and a stable anchor.
4. Reassemble `cells = intro + rendered_audit_cells + other_non_audit_cells` and `save_objects`, recomputing the Navigator metadata (below).

Idempotent and self-healing: because it strips-then-rerenders the full block from the ledger every time, concurrent/last-writer-wins saves converge rather than duplicate.

**Stable per-run anchor:** Each audit cell embeds an invisible HTML-comment anchor keyed on the EE2 `job_id`, e.g. `<!-- kbu:app-run:<job_id> -->`, so cells resolve by content, not index. (nc uses `<!-- nc:app-run:<id> -->`; use a `kbu:` prefix to avoid confusion with nc-authored cells.)

**Audit cell content (per run):** app_id, fully-qualified method, `service_ver`, job_id, terminal state (completed / error), submit + finish timestamps, a compact params summary, the output UPA(s) extracted from `JobReport.result`, and the workspace. All sourced from `JobRecord` (ledger) + the terminal `JobReport`.

**Navigator metadata (port nc's `compute_narrative_meta`):** On every save recompute and write the string→string object `meta`: `cell_count`, `jupyter.markdown`, `data_object_count`, a **bounded** `data_dependencies` sample, and `is_temporary="false"`. Honor KBase's **900-byte key+value cap** on `data_dependencies` (KBUtilLib must not overflow it — port nc's bounded-sample logic). Do **not** fabricate native `method.<id>` app-cell keys.

**NarrativeService client (new, small):** A minimal client that (a) resolves the NarrativeService URL **dynamically via ServiceWizard** and (b) calls `create_new_narrative` (wire param `includeIntroCell`). ServiceWizard is already available: `kbase_endpoints.py` already has a `service_wizard` suffix. **No new static endpoint entry is required** and none should be added.

**Prototype-derived decision shapes** (trimmed to the decision-rich parts):

```python
# Audit cell (markdown), tagged + anchored:
{
  "cell_type": "markdown",
  "source": "### App run: <app_id>\n... \n<!-- kbu:app-run:<job_id> -->",
  "metadata": {"kbase": {"audit": True}},
}

# Audit-cell discriminator (strip rule):
def _is_audit_cell(cell) -> bool:
    return bool(((cell.get("metadata") or {}).get("kbase") or {}).get("audit"))

# Render seam:
AppRunner.audit_callback(narrative_ref=None) -> Callable[[JobReport], None]
#   on terminal report r: append_app_run_audit(ref_or_handle_meta, r.handle.wsid)

# run_app additions (submit-only; resolves+stamps narrative_id at submit):
run_app(..., record_narrative_provenance: bool = False, narrative_ref: str | None = None)
```

## Testing Decisions

Test **external behavior**, not implementation details. Use fakes for the WorkspaceClient, NarrativeService, and an in-memory / temp-file `JobStore`; do not hit live KBase.

**Piece A (required):**
- `save_ws_object` emits a non-empty `prov_actions` with correct `service`/`method`/`method_params`/`input_ws_objects` **when a provenance context is set** (`method != "Unknown"`), captured from a fake `ws_client.save_objects` call. Cover **both** `KBWSUtils` and `KBWSUtilsImpl`.
- `save_ws_object` emits `[]` when **no** context is set (non-breaking guard).
- `get_provenance()` emits the `service` passed to `set_provenance()` (bug-fix regression), and falls back to `self.name` when service is unset.

**Piece B (required):**
- **Render idempotency:** rendering twice from the same ledger yields an identical audit-cell set — no duplicates (strip-and-rerender).
- **Anchor / non-audit preservation:** cell[0] intro and user-authored non-audit markdown survive a render; an audit cell is identified by its anchor/tag regardless of position.
- **Bounded Navigator meta:** a workspace with many referenced objects produces a `data_dependencies` value under the 900-byte cap; `cell_count` / `is_temporary` recompute correctly.
- **Narrative resolution precedence:** explicit `narrative_ref` bypasses discovery + create; an existing narrative is discovered without calling create; a bare workspace triggers exactly one `create_new_narrative` (assert on the fake NarrativeService).
- **Callback wiring:** a terminal-success `JobReport` triggers `append_app_run_audit` with the correct `workspace_id`; a non-terminal report is a no-op; `record_narrative_provenance=False` never resolves/creates a narrative and never renders (default-off invariant).

**Prior art to mirror:** existing `kb_job_utils` store tests (fake/in-memory store patterns) and any existing `kb_ws_utils` tests for the save path.

## Out of Scope

- **Native, re-runnable `kbase.appCell` cells** (flavor 3) — requires `cell_id`/`run_id`/`narrative_id` linkage injected at EE2 submit time, mutating the run path. Separate future PRD.
- **`kb_annotation_utils.py` provenance site** — a different call site, not `save_ws_object`.
- **One-narrative-per-session** scoping — rejected in favor of one-per-workspace.
- **Backfilling** Narratives for jobs run before the flag existed — only jobs submitted with `record_narrative_provenance=True` get a `narrative_id`.
- **ORCID / richer identity** — KBase token identity only.
- **A combined `run_and_wait`** entry point — the callback factory is the only render seam this slice ships.
- **New static NarrativeService endpoint** — dynamic ServiceWizard resolution only.

## Further Notes

- The load-bearing invariant to preserve from nc: **render the complete audit block from the ledger on every write** (strip-all-audit-cells then re-insert). Do NOT design an append path — Workspace Narrative saves are last-writer-wins with no optimistic lock, and only render-from-ledger is convergent under that.
- KBUtilLib's ledger is richer than nc's markdown files — it is the source of truth; the Narrative is a projection.
- Reuse for free: `get_provenance()`/`set_provenance()`, `BaseUtils` provenance attributes (`method`/`service`/`version`/`params`/`input_objects`/`obj_created`, all reset by `reset_attributes()`), the reserved `JobRecord.narrative_id` column, and the `JobMonitor.wait_all(on_progress=...)` seam.
- nc reference files are read-only pattern sources; do not import from or depend on nc.
