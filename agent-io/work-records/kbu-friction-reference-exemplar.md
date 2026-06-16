# Work Record: kbu-friction/reference-exemplar

## task_id
kbu-friction-reference-exemplar

## branch
kbu-friction/reference-exemplar

## commit_shas
- 9c34abf12a1c06ee56bc4bf10d7bf997a910ece7

## summary

Created `examples/kbu_notebook_reference/` as the Task D reference exemplar for
the kbu-skills-friction-fix PRD (Acceptance Criterion 13). The exemplar
demonstrates the canonical FLAT KBUtilLib notebook layout end-to-end:
`notebooks/util.py` is a verbatim render of `cli/templates/util.py.tmpl` with
`project_name="kbu_notebook_reference"` (verified zero-diff via jinja2); a
single-cell `reference.ipynb` that runs `%run util.py`, computes 6+7=13
offline, saves the result dict via `session.cache.save()`, and round-trip
verifies via `session.cache.load()`. One tiny non-SQLite blob
(`265fc4c1...083d0.json`, 114 bytes) is committed as the exemplar artifact;
`catalog.sqlite` and WAL files are excluded by a `.gitignore` inside
`.kbcache/`. The notebook was executed with `jupyter nbconvert --execute` in
the `kbu.nb-modelingloe-py3.11` venv and produced clean output with all
assertions passing.

## files_touched
- `examples/kbu_notebook_reference/README.md` (new)
- `examples/kbu_notebook_reference/notebooks/util.py` (new — rendered from `cli/templates/util.py.tmpl`)
- `examples/kbu_notebook_reference/notebooks/reference.ipynb` (new)
- `examples/kbu_notebook_reference/notebooks/.kbcache/.gitignore` (new)
- `examples/kbu_notebook_reference/notebooks/.kbcache/blobs/265fc4c1c457d3a0e6adfe2b6d81196325ed4480d56504a77b31efdeef1083d0.json` (new — 114-byte committed artifact)
- `agent-io/work-records/kbu-friction-reference-exemplar.md` (this file)

## success_criteria_check

### AC 13: examples/kbu_notebook_reference/ commits a tiny non-SQLite artifact; no SQLite .kbcache catalog committed; "never commit .kbcache" reconciled.
**PASS.** The blob `.kbcache/blobs/<sha256>.json` (114 bytes, pure JSON) is
committed. `catalog.sqlite`, `catalog.sqlite-wal`, and `catalog.sqlite-shm` are
excluded by `.kbcache/.gitignore`. The reconciliation approach is: blob files
are content-addressed and immutable (the hash in the filename IS the integrity
check), so committing them is safe and useful as worked examples; the catalog is
regenerated at runtime and must not be committed. This policy is documented in
both `.kbcache/.gitignore` and `README.md`.

### Notebook cell runs without error in a healthy venv.
**PASS.** Executed `jupyter nbconvert --execute reference.ipynb` in
`kbu.nb-modelingloe-py3.11`. Output: "Saved artifact: blobs/265fc4c1...json
(114 bytes)", "Round-trip OK: ...", "All checks passed." No errors or
exceptions. One pre-existing `[KBUtilLib] Failed to import rcsb_pdb_utils:
ModuleNotFoundError: No module named 'aiohttp'` warning is unrelated (the
optional aiohttp module is not installed in that venv, which is expected and
guarded in kbutillib's __init__).

### notebooks/util.py matches the unified template (flat PROJECT_ROOT = NOTEBOOK_DIR.parent, guarded imports, smart-merge marker).
**PASS.** Jinja2 render of `src/kbutillib/cli/templates/util.py.tmpl` with
`project_name="kbu_notebook_reference"` produces zero diff against
`notebooks/util.py`. Template contains guarded numpy/pandas/cobra imports,
`PROJECT_ROOT = NOTEBOOK_DIR.parent` (flat, one level), and smart-merge marker
`# === project-specific helpers below ===`.

### Fully offline (no BERDL dependency).
**PASS.** The cell computes `6 + 7 = 13` using only Python builtins and
`session.cache.save/load`. No network calls; no KBase/BERDL imports.

## tests_run
- `jupyter nbconvert --execute --ExecutePreprocessor.timeout=60 reference.ipynb` in `kbu.nb-modelingloe-py3.11`: PASS (all assertions, correct blob hash).
- Manual jinja2 render + `diff` against `notebooks/util.py`: PASS (zero diff).
- SHA-256 of committed blob file == filename hash: PASS (265fc4c1...083d0, 114 bytes).
- `git status` after `git add examples/kbu_notebook_reference/`: confirmed catalog.sqlite and WAL files excluded by .gitignore.

## caveats
- The `kbu.nb-modelingloe-py3.11` venv was used for execution (it has
  kbutillib + notebook + nbconvert installed). The cell would also work in any
  venv with kbutillib + pandas (pandas is needed by the notebook session's
  vector store import at module load time). A venv that's missing pandas will
  fail to import `NotebookSession` due to `storage/vectors.py` importing pandas
  unconditionally — this is a pre-existing issue tracked under Task A (not Task D).
- The `data/` and `figures/` directories exist on disk but are empty; git does
  not track empty directories, so they do not appear in the commit. The README
  documents them as placeholders. A `.gitkeep` was deliberately not added to
  keep the exemplar minimal.
- Blob artifact is pre-committed. When a user re-runs the cell, `session.cache.save()`
  detects the same content hash (skip-write optimization in Cache.save) and does
  not overwrite the blob, but does recreate `catalog.sqlite`. This means the
  round-trip works correctly on a fresh clone even without the SQLite catalog.
  The README's "Re-running the cell" note documents this behavior.
