# task_id

ee2-piece-b-narrative-audit

# branch

ee2-piece-b-narrative-audit

# commit_shas

1. `8e400eb9f78f64e27645ab0c206258cc753de3eb` — feat(kb_app_runner): add opt-in narrative audit-cell provenance (EE2 Piece B)

# summary

Implements Piece B of `agent-io/prds/ee2-narrative-provenance/fullprompt.md`: an
opt-in, default-off narrative audit-cell trace on top of KBUtilLib's EE2
`AppRunner`, ported from narrative-connector's (nc) render-from-ledger pattern
(read-only reference: `~/king-stack/narrative-connector`). `AppRunner.run_app`
gained `record_narrative_provenance: bool = False` and `narrative_ref: str |
None = None`, forwarded through `run_app_if_missing` and `run_apps_parallel`
(both fields added to `AppCall`). When the flag is set, `run_app` resolves the
target Narrative **at submit time** with precedence explicit-ref >
discover-existing (workspace `narrative` meta pointer, else a
`KBaseNarrative.Narrative` type scan) > auto-create (a new small
`NarrativeService` client that resolves its URL dynamically via ServiceWizard
— no new static endpoint was added to `kbase_endpoints.py`), stamps the
resolved object id onto the reserved `JobRecord.narrative_id` column, and
stashes `record_narrative_provenance` / `narrative_ref` / `narrative_id` on
`JobHandle.meta`. `run_app` itself stays submit-only. The actual render lives
behind `AppRunner.audit_callback(narrative_ref=None)`, which returns an
`on_progress` function for `JobMonitor.wait_all`; on a terminal report
(success or error/terminated) it calls the new
`KBWSUtilsImpl.append_app_run_audit(narrative_ref, workspace_id)` — a
read-modify-write ported from nc's `_read_modify_write_once` that fetches the
latest Narrative version, preserves cell[0] (intro), strips every cell whose
`metadata.kbase.audit` is truthy, re-renders the complete audit block from the
SQLite `jobs` table filtered by `workspace_id` (one markdown cell per
terminal job, anchored by `<!-- kbu:app-run:<job_id> -->`), reassembles
`intro + audit + other`, and saves with recomputed Navigator metadata
(`cell_count`, `jupyter.markdown`, `data_object_count`, a `data_dependencies`
sample bounded under KBase's 900-byte key+value cap, `is_temporary="false"`).
Strip-and-rerender-from-ledger makes concurrent/last-writer-wins Narrative
saves converge instead of duplicating cells. No native `kbase.appCell` /
`method.<id>` metadata is fabricated. Along the way, fixed a pre-existing
`IndexError` in `list_ws_objects` (`output[-1]` on an empty result page) that
blocked the bare-workspace auto-create discovery path this feature depends
on.

# files_touched

- `src/kbutillib/kb_app_runner/runner.py` — `AppCall` gains
  `record_narrative_provenance` / `narrative_ref`; `run_app` resolves +
  stamps narrative provenance at submit time (opt-in, forwards through
  `run_app_if_missing` / `run_apps_parallel`); new `audit_callback()` method
  and `_narrative_objid_from_ref` helper.
- `src/kbutillib/kb_ws_utils.py` — `KBWSUtilsImpl` gains
  `_narrative_service_client()` (lazy, dynamic ServiceWizard lookup),
  `find_narrative_in_workspace`, `create_narrative`, `resolve_narrative`,
  `append_app_run_audit`; fixed the `list_ws_objects` empty-page `IndexError`
  in both `KBWSUtils` and `KBWSUtilsImpl`.
- `src/kbutillib/kb_narrative_audit.py` (new) — pure render-from-ledger
  helpers ported from nc's `narrative_append.py`: stable anchor, audit-cell
  discriminator, per-run markdown cell renderer, output-UPA extraction,
  bounded `data_dependencies` / Navigator `compute_narrative_meta`.
- `src/kbutillib/installed_clients/NarrativeServiceClient.py` (new) — small
  vendored client for the dynamic `NarrativeService` module
  (`create_new_narrative`), resolved via `BaseClient(lookup_url=True)` +
  the existing `service_wizard` endpoint suffix.
- `src/kbutillib/kb_job_utils/store.py` — new `JobStore.list_by_workspace`.
- `src/kbutillib/kb_job_utils/utils.py` — `KBJobUtils.run_job` gains an
  optional `narrative_id` param, stamped on the created `JobRecord`.
- `tests/test_kb_narrative_provenance.py` (new) — 23 tests covering render
  idempotency, anchor/preservation, bounded Navigator meta, resolution
  precedence, and callback wiring (see below).

# success_criteria_check

- **`run_app` accepts `record_narrative_provenance` (default False) and
  `narrative_ref`, default-off byte-for-byte unchanged** — PASS. Verified by
  `TestRunAppNarrativeSubmitTime::test_default_off_never_resolves_narrative`
  (asserts `ws.resolve_narrative` never called, `handle.meta` carries no
  narrative keys, `JobRecord.narrative_id is None`) plus the full pre-existing
  `test_kb_app_runner.py` suite passing unmodified (33/33 green, same
  assertions as before this change).
- **When True against a workspace, a `KBaseNarrative.Narrative` gains
  markdown audit cells tagged `metadata.kbase.audit=True` rendered from the
  job ledger** — PASS. `TestAppendAppRunAuditRender` renders against a fake
  Workspace and asserts the saved cells carry the tag and per-job content.
- **Idempotent across repeated renders (strip-and-rerender, no duplicate
  cells)** — PASS. `test_render_idempotent_no_duplicates` renders twice
  against the same ledger; asserts 2 audit cells both times (not 4) and
  byte-identical content.
- **Intro/user cells preserved, cells anchored by job_id** — PASS.
  `test_intro_and_user_cells_preserved_anchor_resolves` seeds a stale
  wrong-content audit cell under the correct anchor plus a user cell; asserts
  the user cell survives, the stale cell's content is replaced, and exactly
  one cell carries the anchor post-render (position-independent).
  `test_only_terminal_jobs_rendered` and
  `test_workspace_filter_excludes_other_workspaces` confirm scoping.
- **Resolution: explicit-ref > discover-existing > auto-create; no new
  static endpoint** — PASS. `TestNarrativeResolutionPrecedence` covers all
  three branches directly (`test_explicit_ref_bypasses_discovery_and_create`,
  `test_existing_narrative_discovered_without_create` [both the ws-meta
  pointer and the type-scan-fallback discovery paths],
  `test_bare_workspace_triggers_exactly_one_create` — asserts
  `create_new_narrative` called exactly once). `kbase_endpoints.py` was not
  modified — `NarrativeServiceClient` reuses the existing `service_wizard`
  suffix via `BaseClient(lookup_url=True)`, confirmed by a standalone
  scratch-run showing two RPC round-trips (`ServiceWizard.get_service_status`
  then `NarrativeService.create_new_narrative`) against the *same* configured
  URL.
- **`JobRecord.narrative_id` stamped at submit; render hangs off
  `AppRunner.audit_callback` passed to `JobMonitor.wait_all`; `run_app`
  stays submit-only** — PASS. `test_opt_in_resolves_and_stamps_narrative_id`
  asserts the stored `JobRecord.narrative_id` after `run_app` (no wait/render
  call in between); `TestAuditCallback` exercises the callback in isolation
  from `run_app`, confirming they are decoupled seams.
- **Navigator meta recomputes with `data_dependencies` bounded under the
  900-byte cap** — PASS. `test_data_dependencies_bounded_under_900_bytes`
  seeds 200 output UPAs and asserts the saved `data_dependencies` key+value
  byte length stays under 900 while `data_object_count` still reports the
  true total (200) and `cell_count` matches the actual rendered cell count.
- **Tests for render idempotency, anchor/preservation, bounded meta,
  resolution precedence, callback wiring pass** — PASS. All 23 new tests
  green; see `tests_run` below.

# tests_run

- `python3 -m pytest tests/test_kb_narrative_provenance.py -q` → **23 passed**
  (this task's new suite: render idempotency, anchor/preservation, bounded
  Navigator meta, resolution precedence, submit-time wiring, callback
  wiring).
- `python3 -m pytest tests/kb_app_runner/test_kb_app_runner.py
  tests/test_kb_ws_utils.py tests/test_kb_job_utils.py tests/kb_job_utils/ -q`
  → **159 passed, 2 failed (pre-existing, unrelated caplog-propagation
  issue in `TestListAllTypes`/`TestGetTypeSpecs` logging tests — confirmed
  present identically on the Piece-A base commit before this task's changes),
  2 skipped**.
- `python3 -m pytest -q` (full repo suite) → **2014 passed, 45 failed, 22
  skipped, 1 xfailed, 26 errors**. Diffed against a full-suite baseline run
  on the pre-Piece-B `main` (Piece A) worktree: **identical 45
  failed / 26 errors** (unrelated pre-existing breakage in
  `test_kb_berdl_utils.py`, `test_escher_utils.py`, `test_ms_biochem_deltag.py`,
  `test_task_a_venv_doctor.py`, `test_composition_smoke.py`,
  `test_ms_reconstruction_utils.py`, plus the 2 known logging tests) — the
  +23 delta in `passed` count is exactly this task's new test file, with
  zero new failures introduced.

# caveats

- **`NarrativeService.create_new_narrative`'s real wire API has no
  workspace-targeting parameter.** Per the live `NarrativeService.spec`
  (documented in the nc reference client's docstring), the call always mints
  a *fresh* Workspace alongside the new Narrative — it cannot attach a
  Narrative to a pre-existing workspace. So on the auto-create branch (no
  existing Narrative found in the target workspace), the resolved Narrative
  physically lives in a **different, freshly-minted workspace**, not the
  app's actual run workspace. This is a genuine constraint of the real
  KBase API, not a simplification I introduced — nc's own auto-create has
  the identical one-per-session (not one-per-target-workspace) behavior for
  the same reason. Functionally this doesn't break anything: `JobRecord`
  still stamps the run's real `workspace_id` for ledger filtering, and
  `append_app_run_audit` addresses the Narrative purely by its own UPA
  (`narrative_ref`), independent of where it lives — but a user should be
  aware that "auto-create" does not mean "a Narrative appears in *my*
  target workspace." Flagging this because the PRD's "one Narrative per
  workspace" framing could otherwise read as universally true; it only
  strictly holds on the discover-existing branch. If this needs Chris's
  attention (e.g. adding a workspace-copy step after create to force
  attachment), that would be a follow-up PRD — I judged it out of this
  slice's stated scope (Out of Scope explicitly excludes new engineering
  around NarrativeService beyond the documented `create_new_narrative`
  call).
- **`KBWSUtilsImpl.append_app_run_audit`'s `job_store` parameter is an
  addition beyond the PRD's literal two-positional-arg signature.** The PRD
  states `append_app_run_audit(narrative_ref, workspace_id)` but also
  requires querying "the SQLite jobs table" — `KBWSUtilsImpl` doesn't
  otherwise hold a `JobStore` reference. I added `job_store: Any = None` as
  a **keyword-only** override (defaulting to a fresh
  `JobStore()` pointed at the standard `~/.kbjobs/kbjobs.db`), which
  preserves the documented two-positional-arg call shape
  (`append_app_run_audit(ref, wsid)`) while satisfying the Testing
  Decisions' explicit ask for injectable "in-memory / temp-file `JobStore`"
  fakes. Judgment call, documented here per the instructions.
- **`list_ws_objects` bug fix is a pre-existing latent bug**, not scoped to
  Piece B, but required for the bare-workspace discovery path (user story 7)
  to work at all — a workspace with zero objects of the queried type would
  otherwise crash with `IndexError` on `output[-1]`. Fixed identically in
  both `KBWSUtils` and `KBWSUtilsImpl` for consistency (both had the
  identical bug). This is the only change outside the PRD's direct scope.
- **Audit-cell markdown content format is my own rendering**, not a literal
  port of nc's `render_app_run_cell_markdown` (nc's shape is keyed to its
  own `AppRunCellSpec`/ledger schema, which doesn't exist in KBUtilLib).
  I built an analogous renderer (`kb_narrative_audit.render_app_run_cell_markdown`)
  sourced directly from `JobRecord` fields, covering every field the PRD
  lists (app_id, method, service_ver, job_id, terminal state, submit/finish
  timestamps, compact params summary, output UPA(s), workspace) with the
  same anchor-comment convention (just `kbu:` prefixed instead of `nc:`, as
  the PRD explicitly directs).
- Did not modify `kb_annotation_utils.py`'s separate bare `"provenance":
  []"` (explicitly Out of Scope, shared with Piece A).
- No native `kbase.appCell` / `method.<id>` app-cell metadata is fabricated
  anywhere — audit cells are markdown-only, matching the PRD's explicit
  decision (Piece B, "Cell format").
