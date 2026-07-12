# Optional KBase-Narrative Provenance for KBUtilLib's EE2 App Runner

Add opt-in, default-off KBase provenance to KBUtilLib's EE2 app runner, porting the
"trace in the narrative itself" behavior from `narrative-connector` (nc) onto KBUtilLib's
superior SQLite job ledger. Two independent slices, A then B.

## Piece A — object-level provenance (ships first, canonical)
Wire the existing `get_provenance()` into `save_ws_object` so saved outputs carry real
KBase `prov_actions` whenever a provenance context is set — with default behavior (empty
provenance) unchanged for callers who set none. Patch **both** `save_ws_object` sites
(`KBWSUtils` and its twin `KBWSUtilsImpl`). Fold in a latent bug fix: `get_provenance()`
must emit the `service` passed to `set_provenance()` rather than always using the class name.

## Piece B — narrative audit-cell trace (opt-in)
Add `record_narrative_provenance=False` + `narrative_ref=None` to `AppRunner.run_app`.
When enabled:
- Resolve the target Narrative — explicit `narrative_ref` > discover existing in the
  workspace > auto-create via NarrativeService (dynamic, via the existing ServiceWizard
  endpoint). Scope: **one Narrative per workspace**.
- Stamp `JobRecord.narrative_id` at submit; `run_app` stays submit-only.
- On job completion (via a new `AppRunner.audit_callback()` passed to
  `JobMonitor.wait_all(on_progress=...)`), render the **complete** audit block from the
  ledger into the Narrative as **markdown** cells (tagged `metadata.kbase.audit=True`,
  anchored by job_id), idempotently — strip-all-audit-cells-then-rerender, exactly like
  nc's render-from-ledger. Recompute the Navigator metadata under KBase's 900-byte cap.

New capability method: `KBWSUtilsImpl.append_app_run_audit(narrative_ref, workspace_id)`.

## Decisions (resolved)
- **Markdown, not native app cells** — native re-runnable cells need submit-time
  cell_id/run_id linkage that mutates the run path; separate future project.
- **One narrative per workspace** — KBase's own model; ledger filters by `workspace_id`.
- **Dynamic NarrativeService via ServiceWizard** — no new static endpoint.
- **Callback factory** (`AppRunner.audit_callback`) is the render seam — run path untouched;
  no `run_and_wait`.
- **A is independent of B and ships first**; B depends on A only to avoid a
  `kb_ws_utils.py` merge conflict.

## Out of scope
Native re-runnable app cells; `kb_annotation_utils.py:352`; one-per-session scoping;
backfilling historical jobs; ORCID identity; a combined run+wait entry point.

Prior analysis: `agent-io/plans/2026-07-11-ee2-narrative-provenance.md`.
nc reference (read-only, pattern source): `~/king-stack/narrative-connector`.
