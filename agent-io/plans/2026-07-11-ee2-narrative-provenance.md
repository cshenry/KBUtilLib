# Extending KBUtilLib's EE2 API with (optional) KBase-Narrative provenance

**Date:** 2026-07-11
**Context:** Comparison of `narrative-connector`'s `narrative-caller` capability against KBUtilLib's EE2 app-runner, and a design for adding narrative-caller's "trace in the narrative itself" to KBUtilLib as an opt-in feature.
**Sources analyzed:** `~/king-stack/narrative-connector` (caller) and `~/Dropbox/Projects/KBUtilLib/src/kbutillib` (app runner).

---

## 1. TL;DR

- The **EE2 submission halves are nearly identical.** Both send a deliberately slim `run_job` (`method`, `app_id`, `service_ver`, `wsid`, `params`) under the user's own token, and **neither** attaches native Narrative app-cell metadata (`cell_id` / `run_id` / `narrative_id`). Porting the "run" behavior is a non-issue — KBUtilLib already does it.
- The distinctive, load-bearing piece in narrative-caller is a **provenance layer**: a local ledger (source of truth) that is **projected into the `KBaseNarrative.Narrative` object as markdown "audit" cells** via a Workspace `get_objects2` + `save_objects` read-modify-write, re-rendered idempotently on every run.
- **KBUtilLib already has the hard half of that** — a local job ledger (SQLite `~/.kbjobs/kbjobs.db`) with a *reserved-but-unused* `narrative_id` column, and a `get_provenance()` builder that already produces KBase prov-action shape. What's missing is (a) projecting the ledger into a Narrative object and (b) attaching object-level `prov_actions` on saved outputs.
- **Verdict: very feasible, and a clean opt-in.** Recommend shipping it as **two independent pieces** (object-level provenance, which is small and canonical; and the narrative audit-cell trace, which is the appealing nc feature).

---

## 2. Side-by-side diff

| Dimension | narrative-caller (nc) | KBUtilLib `AppRunner`/`KBJobUtils` |
|---|---|---|
| **EE2 submit params** | `method, app_id, service_ver, wsid, params`; asserts no `job_requirements` (user fair-share); user token, raw. `caller/run_app.py:666-677`, `clients/ee2.py:50-59` | `method, params` + optional `app_id, wsid, service_ver`. `kb_job_utils/utils.py:97-106`. **Essentially identical.** |
| **Native app-cell metadata** (`cell_id`/`run_id`/`narrative_id`/`source_ws_objects`) | **None** — deliberately not sent | **None** — not built anywhere |
| **Local record (source of truth)** | Markdown ledger files `ProvenanceEntry` (`schemas.py:289-345`) + fidelity registry | SQLite `~/.kbjobs/kbjobs.db` `JobRecord` (`kb_job_utils/store.py:27-40`, `state.py:39-67`) — richer/queryable; has **unused** `narrative_id` slot (`state.py:62`) |
| **Object-level Workspace provenance** (`prov_actions`) | **Not written** | **Not written** — `save_ws_object` hard-codes `"provenance": []` with `get_provenance()` commented out (`kb_ws_utils.py:247`). But `get_provenance()` **already builds the correct shape** (`kb_ws_utils.py:267`). |
| **Trace in the Narrative document** | **YES** — renders ledger → `KBaseNarrative.Narrative` `data.cells` as markdown cells tagged `metadata.kbase.audit=True`, via `get_objects2`+`save_objects` idempotent re-render (`narrative_append.py:463-686`) | **None** — never reads/writes `KBaseNarrative.Narrative` |
| **Concurrency strategy** | Render-**from-ledger** (strip all `kbase.audit` cells, re-insert complete block) because WS Narrative saves are last-writer-wins with no optimistic lock (`narrative_append.py:1-53`) | N/A (no narrative write) |
| **Identity** | KBase `user_id` from `whoami()`; no ORCID | KBase token |
| **Monitoring** | `job_monitor.poll_to_terminal` | `JobMonitor.wait_all` / background `Watcher` (`monitor.py:86`, `utils.py:523`) |

**The gap, in one line:** KBUtilLib runs the job and tracks it locally, but writes *nothing* back to KBase — no `prov_actions` on outputs, and no audit trail in the Narrative. narrative-caller adds exactly that projection.

---

## 3. What "narrative provenance" actually means — three flavors

Be precise about which of these you want, because they differ in value and cost:

1. **Object-level Workspace provenance (`prov_actions`)** — the canonical KBase lineage stamped on each *output object*: service, method, params, `input_ws_objects`, version. This is what makes an output show "created by app X from inputs Y" everywhere in KBase. **narrative-caller does NOT do this.** KBUtilLib is one wire away (its `get_provenance()` already exists).

2. **Narrative-document audit trail (markdown cells)** — what narrative-caller does. When you open the Narrative in the UI, you see the run recorded as markdown notes. This is the "trace in the narrative itself" you found appealing. **Caveat:** these are *markdown* cells, **not native KBase app cells** — nc deliberately does not fabricate `kbase.appCell` / `method.<id>` metadata (`compute_narrative_meta`, `narrative_append.py:738-785`), so the Navigator's "App cells" count honestly stays 0 and the runs are **not re-runnable from the UI**.

3. **Native, re-runnable app cells** — runs appear as real KBase app cells you can re-execute in the Narrative UI. This requires `kbase.appCell` cell metadata + EE2 `cell_id`/`run_id` linkage at submit time. **Neither codebase does this**; it's the hardest option and a larger project.

Recommendation: do **(1)** unconditionally (cheap, canonical, benefits every saved output), and **(2)** as the opt-in "narrative trace" feature. Treat **(3)** as a separate future question.

---

## 4. Extension design for KBUtilLib (opt-in, default-off)

Keeps default behavior unchanged; provenance is strictly opt-in — matching KBUtilLib's composable/capability-by-composition idiom.

### Piece A — object-level provenance (small, do regardless)
- Flip `save_ws_object`'s `"provenance": []` → `self.get_provenance()` when a run context is present (`kb_ws_utils.py:247` and the composition twin at `:919`).
- Populate via the existing `set_provenance(method, description, input_objects, params, service, version)` (`kb_ws_utils.py:256`) from the `AppRunner`/`JobRecord` at save time.
- Net: outputs saved through KBUtilLib carry real KBase `prov_actions`. ~a dozen lines.

### Piece B — narrative audit-cell trace (the nc feature, ported)
Copy nc's **render-from-ledger** pattern — it's the correct design and KBUtilLib already has a superior ledger to render from.

1. **Public flag** on the single choke point `AppRunner.run_app(...)` (`kb_app_runner/runner.py:95`), inherited by `run_app_if_missing` / `run_apps_parallel`:
   ```python
   run_app(..., record_narrative_provenance: bool = False, narrative_ref: str | None = None)
   ```
2. **Populate the reserved slot** — set `JobRecord.narrative_id` at submit (`kb_job_utils/utils.py:112`; column already exists at `store.py:34`) so the ledger→narrative scoping key exists.
3. **New capability method** on `KBWSUtilsImpl`, e.g. `append_app_run_audit(narrative_ref, workspace_id)`:
   - `get_objects2` the latest Narrative → strip all `kbase.audit==True` cells → re-render the **complete** audit block from the SQLite `jobs` table filtered by `workspace_id`/`narrative_id` → `save_objects` (mirrors `narrative_append._read_modify_write_once`, `narrative_append.py:596-686`). Idempotent, self-healing under last-writer-wins.
   - Reuse a stable per-run anchor (nc uses an HTML comment `<!-- nc:app-run:<id> -->`) so cells resolve by content, not index.
4. **Attach without touching the run path** — hang the render off the existing `JobMonitor.wait_all(on_progress=...)` terminal-success callback (`kb_app_runner/monitor.py:86`); `JobReport.result` already carries the raw EE2 output for the output UPA.
5. **Precondition to add:** a Narrative must exist. nc creates one per session via NarrativeService `create_new_narrative` (`clients/narrative_service.py:144-173`). KBUtilLib would need either a supplied `narrative_ref` or a small NarrativeService client to create/resolve one. This is the main net-new dependency.

### What you get to reuse for free
- The local ledger (SQLite) — richer than nc's markdown files; already the source of truth.
- `get_provenance()` / `set_provenance()` — object provenance builder already present.
- `BaseUtils` provenance attributes (`method`, `service`, `version`, `input_objects`, `obj_created`; `base_utils.py:44-58`) — already threaded for exactly this.
- `JobMonitor` `on_progress` and `Watcher` `on_change` callbacks — clean post-run seams, no core change.

---

## 5. Caveats / decisions for Chris

- **Markdown vs native app cells** (flavor 2 vs 3): the appealing nc trace is markdown-only, non-re-runnable. Confirm that's sufficient, or scope the harder native-app-cell path separately.
- **Narrative existence**: KBUtilLib apps often run against a bare workspace with no Narrative. Decide: require a `narrative_ref`, or auto-create one (adds a NarrativeService dependency + the "one narrative per what?" question — nc uses one-per-session).
- **Endpoint parity**: nc resolves NarrativeService dynamically via ServiceWizard; KBUtilLib's `kbase_endpoints.py` would need the NarrativeService/ServiceWizard entries.
- **Object provenance (Piece A) is independently valuable** and should probably land first regardless of the narrative-trace decision.

---

## 6. Suggested next step

If we want to build this, formalize as a KBUtilLib PRD (via `/ai-design`): Piece A as a standalone slice, Piece B as an opt-in `record_narrative_provenance` capability that ports nc's render-from-ledger projector onto KBUtilLib's SQLite ledger.
