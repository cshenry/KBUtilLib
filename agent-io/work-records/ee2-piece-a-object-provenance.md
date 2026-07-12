# Work Record: ee2-piece-a-object-provenance

- **task_id:** ee2-piece-a-object-provenance
- **branch:** ee2-piece-a-object-provenance
- **commit_shas:**
  - `2fcd3d6` — feat(ws-utils): wire get_provenance() into save_ws_object (EE2 Piece A)

## summary

Implemented Piece A of `agent-io/prds/ee2-narrative-provenance/fullprompt.md`. In both `save_ws_object` definitions in `src/kbutillib/kb_ws_utils.py` (the inheritance-based `KBWSUtils` and its composition twin `KBWSUtilsImpl`), replaced the hard-coded `"provenance": []` with a context-guarded call to `self.get_provenance()`: it now emits real prov_actions whenever `self.method not in (None, "", "Unknown")` (i.e. a caller has called `set_provenance()` / `initialize_call()`), and keeps `[]` otherwise, preserving default (non-breaking) behavior for callers who never establish a provenance context. Also fixed the latent bug in both `get_provenance()` methods: the prov-action `service` field previously emitted `self.name` unconditionally and ignored `self.service`; it now emits `self.service` when set to a real value (`not in (None, "", "Unknown")`), falling back to `self.name` otherwise. `service_ver` remains sourced from `self.version`, and no prov-action keys were renamed (they still match the KBase Workspace `ProvenanceAction` shape). The unrelated `"provenance": []` literal in `kb_annotation_utils.py` was left untouched, as instructed. Added unit tests in `tests/test_kb_ws_utils.py` using a fake `ws_client` (`MagicMock`) that captures `save_objects()` params, covering: non-empty prov_actions with correct service/method/method_params/input_ws_objects for both classes when a context is set; empty `[]` for both classes when no context is set; and `get_provenance()` emitting the configured `service` vs. falling back to `self.name`, again for both classes.

## files_touched

- `src/kbutillib/kb_ws_utils.py` — `KBWSUtils.save_ws_object`, `KBWSUtils.get_provenance`, `KBWSUtilsImpl.save_ws_object`, `KBWSUtilsImpl.get_provenance`
- `tests/test_kb_ws_utils.py` — new fixtures (`fake_ws_client`, `ws_utils_prov`, `ws_utils_impl_prov`) and new test classes (`TestSaveWsObjectProvenance`, `TestGetProvenanceServiceField`)

## success_criteria_check

- **Objects saved via save_ws_object (on BOTH KBWSUtils and KBWSUtilsImpl) carry a non-empty prov_actions array with correct service/method/method_params/input_ws_objects when a provenance context is set (self.method != 'Unknown'):** PASS — verified by `test_save_ws_object_carries_provenance_when_context_set`, parametrized over both classes; asserts `service`, `method`, `method_params`, `input_ws_objects` on the captured `save_objects()` params.
- **...and remain [] otherwise (default behavior unchanged):** PASS — verified by `test_save_ws_object_empty_provenance_when_no_context`, parametrized over both classes; asserts `provenance == []` when no `set_provenance()`/`initialize_call()` has been called (default `self.method == "Unknown"`).
- **get_provenance() emits the service value set via set_provenance():** PASS — verified by `test_get_provenance_uses_configured_service` (KBWSUtils) and `test_get_provenance_impl_uses_configured_service` (KBWSUtilsImpl); also confirms fallback to `self.name` when service is unset via `test_get_provenance_falls_back_to_name_when_service_unset` and the Impl equivalent.
- **New unit tests cover all three branches and the full suite passes:** PASS (with caveat) — all new tests pass (10/10). Ran the full repo suite (`pytest tests/ -q`): 1991 passed, 45 failed, 22 skipped, 1 xfailed, 26 errors — all failures/errors are in unrelated modules (`test_kb_berdl_utils.py`, `test_ms_biochem_deltag.py`, `test_ms_reconstruction_utils.py`, `test_task_a_venv_doctor.py`, `test_escher_utils.py`, and two pre-existing `test_kb_ws_utils.py` logging-propagation failures) and were confirmed to reproduce identically on `main` before this change (see Caveats).

## tests_run

- `python -m pytest tests/test_kb_ws_utils.py -v` (in worktree) — 22 passed, 2 skipped, 2 failed (pre-existing logging-propagation failures, unrelated to this change; confirmed reproducing on `main`).
- `python -m pytest tests/ -q` (in worktree, full suite) — 1991 passed, 45 failed, 22 skipped, 1 xfailed, 26 errors, 227.97s. `git diff --stat` confirms this change touches only `src/kbutillib/kb_ws_utils.py` and `tests/test_kb_ws_utils.py`; spot-checked two of the failing tests (`test_kb_berdl_utils.py::TestQueryContigs::test_query_contigs_basic`, `test_ms_biochem_deltag.py::TestCompoundDeltaG::test_get_compound_deltag_valid_value`) directly against the `main`-tracked working copy at `~/Dropbox/Projects/KBUtilLib` and confirmed they fail there too (pre-existing, unrelated to Piece A).

## caveats

- Two pre-existing failures in `tests/test_kb_ws_utils.py` itself (`TestListAllTypes::test_list_all_types_logging`, `TestGetTypeSpecs::test_get_type_specs_logging`) were NOT introduced by this change — they fail identically on `main` (verified directly) and are caused by an unrelated logger-propagation ordering issue with `caplog`, not by the provenance edits.
- The broader suite has 45 failures / 26 errors across `kb_berdl_utils`, `ms_biochem_deltag`, `ms_reconstruction_utils`, `task_a_venv_doctor`, and `escher_utils` test files. These are all outside the scope of Piece A (no code in those modules was touched) and were spot-checked to reproduce on `main`.
- Piece B (narrative audit-cell trace / `AppRunner` / narrative audit) was explicitly out of scope for this task and was not touched.
- The `"provenance": []` literal in `kb_annotation_utils.py` (a different, non-`save_ws_object` call site) was explicitly left untouched per the task instructions.
- Not pushed to origin; this repo is Dropbox-synced and committed-unpushed is the expected state per repo convention.
