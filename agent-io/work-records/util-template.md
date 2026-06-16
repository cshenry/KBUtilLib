# Work Record — util-template

## task_id
util-template

## branch
worknb/util-template

## commit_shas
- fe0cff3ad5c5dc3c23f8df7de9f12aa06c3fa851

## summary
Added the work-notebook `util.py` template (PRD Module 4) as a Jinja2
template at `src/kbutillib/cli/templates/worknb_util.py.tmpl`, together
with a render/smart-merge module at `src/kbutillib/cli/worknb_util.py`
that exposes `render_worknb_util_template(repo_basename, topic)` and
`smart_merge_worknb_util(existing_content, new_header)`. The template
produces a `%run`-loadable file exposing six path constants anchored to
the PRJ layout (`PROJECT_ROOT`, `NOTEBOOKS_DIR`, `MODELS_DIR`,
`GENOMES_DIR`, `DATA_DIR`, `NBOUTPUT_DIR`) and a `session =
NotebookSession.for_notebook(__file__, project_name=<repo_basename>,
cache_dir="NBCache")` instance. The helpers marker `# === project-specific
helpers below ===` is preserved by `smart_merge_worknb_util()` so a
re-render on `--update` does not clobber hand-written code. No BERIL code
was modified.

## files_touched
- `src/kbutillib/cli/templates/worknb_util.py.tmpl` (new — Jinja2 template)
- `src/kbutillib/cli/worknb_util.py` (new — render + smart-merge module)
- `tests/cli/test_worknb_util_template.py` (new — 22 tests)

## success_criteria_check

**Criterion (from task prompt):** A work-notebook `util.py` template renders
a `%run`-loadable file exposing `PROJECT_ROOT`/`NOTEBOOKS_DIR`/`MODELS_DIR`/
`GENOMES_DIR`/`DATA_DIR`/`NBOUTPUT_DIR` and a session via
`NotebookSession.for_notebook(__file__, project_name=<repo_basename>,
cache_dir='NBCache')`; it preserves the helpers marker for smart-merge.

- **Six path constants** — PASS. Tests `test_contains_*` verify each name
  is present in the rendered text; `test_path_constants_resolve_correctly`
  execs the rendered file and asserts each `Path` value matches the
  expected PRJ-relative location.
- **`NotebookSession.for_notebook(__file__, project_name=..., cache_dir='NBCache')`**
  — PASS. `test_cache_dir_nbcache` checks the literal string; `test_session_receives_nbcache_cache_dir` captures kwargs at exec time.
- **`%run`-loadable** — PASS. `TestRenderedUtilExecutable` execs the rendered
  source with a patched `for_notebook` and confirms no import error.
- **Helpers marker preserved on re-render** — PASS. `test_preserves_helpers_below_marker`,
  `test_idempotent_double_merge`, and `test_merge_preserves_multiline_helpers`
  cover the smart-merge behavior; `test_returns_none_when_marker_absent_*`
  cover the safety-valve behavior.
- **BERIL template untouched** — PASS. `src/kbutillib/cli/templates/util.py.tmpl`
  and `src/kbutillib/cli/init_notebook.py` were not modified.

**AC 17 (PRD):** PASS — verified by `TestRenderedUtilExecutable` suite.

## tests_run

```
python -m pytest tests/cli/test_worknb_util_template.py -v
# Result: 22 passed, 0 failed

python -m pytest tests/cli/test_init_notebook.py tests/test_layout.py tests/notebook/test_cache_dir_param.py -v
# Result: 105 passed, 1 failed (pre-existing failure)
```

Pre-existing failure: `tests/cli/test_init_notebook.py::TestRenderUtilTemplate::test_contains_session_for`
asserts `"def session_for" in rendered` but the BERIL template uses
`NotebookSession.for_notebook(...)` directly (no `def session_for` wrapper).
This failure exists on `main` before this branch. It is a BERIL test bug
unrelated to Module 4; not fixed here per the constraint not to modify
BERIL code.

## caveats

1. **`worknb_util.py` is not wired into any CLI command yet.** It is a
   standalone render/merge module ready for `kbu notebook-init` (Module 1)
   to call when scaffolding a new `PRJ-<topic>/`. The CLI wiring is out of
   scope for this task per the task envelope.

2. **`worknb_util.py` is not registered in `src/kbutillib/cli/__init__.py`**
   because it is not a Click command; it is a library module. The Module 1
   task will import it directly.

3. **Pre-existing test failure** (`test_contains_session_for`) is documented
   above — it was failing on `main` before this branch and is not caused by
   any change made here.

4. **Template variables substituted are `repo_basename` and `topic`.** The
   `topic` value is used only in the rendered doc-comment (for readability);
   it is not load-bearing for path computation. The path constants are all
   anchored to `__file__` at `%run` time, so the template works correctly
   regardless of the topic string passed at render time.
