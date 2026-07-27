# KBUtilLib reorg-livability fixes

**Branch**: `reorg-livability-fixes`
**Base**: `full-suite-verification-local` @ `4c2a10233b4c312c3288416e2e645be3d5f4f4fd`
**Run environment**: macOS (darwin/arm64), fresh throwaway venv at
`/private/tmp/claude-501/.../scratchpad/kbu-reorg-venv` (Python 3.11.14),
**not** `~/VirtualEnvironments`. `pip install -e '.[all]' --group dev --group
lint --group notebook` was run into that throwaway venv only, followed by an
ad-hoc `pip install rdkit` and `pip install cobra` (both PyPI wheels, no
`minedatabase`/dev-`modelseedpy`) to exercise the `verab` and
`notebook/helpers` subsets — matching the precedent set by
`agent-io/reports/full-suite-verification.md`. No shared venv was touched.

This task fixes 4 pre-existing reorg-base bugs that blocked the notebook/
researchos/verab surfaces from actually working after `integrate-reorg-*`
landed. It does **not** touch the unrelated modelseedpy/ModelSEEDDatabase
API-mismatch failures (see "Out of scope" below) — those are an environment
gap, not a code bug, and are left exactly as they were.

## Bug 1 — notebook nested-subpackage duplicates (class-identity break)

**Symptom**: `kbutillib/notebook/{schema,serialization,storage,helpers}/`
(26 files, including 4 `__init__.py`) were byte-identical **copies** of
`kbutillib/domains/notebook/{schema,serialization,storage,helpers}/`, not
shims. Anything built via `kbutillib.notebook.schema.X` was therefore a
*different class object* than the one
`kbutillib/domains/notebook/experiment_store.py` (and friends) expect,
triggering pydantic `ValidationError` (`model_type`) whenever a
flat-path-constructed object was passed to a domains-path consumer.

**Fix**: converted every file under
`kbutillib/notebook/{schema,serialization,storage,helpers}/*.py` into a
one-line shim (`from kbutillib.domains.notebook.<subpath> import *`),
mirroring the style already used for the flat `kbutillib/notebook/*.py`
files (`cache.py`, `manifest.py`, etc.). For the two `__init__.py` files that
already had an explicit `__all__` in the domains counterpart
(`schema/__init__.py`, `helpers/__init__.py`), the shim also re-exports
`__all__` explicitly, matching the pattern used by the top-level
`kbutillib/notebook/__init__.py` shim. `serialization/__init__.py` and
`storage/__init__.py` have no `__all__` in the domains counterpart, so a
plain `import *` is sufficient (this also correctly re-runs
`serialization/__init__.py`'s `_boot()` self-registration exactly once,
into the single shared `_REGISTRY`/`_DISPATCH_ORDER` living in the domains
module — the two modules cannot diverge into separate registries now
because every serializer submodule under `kbutillib/notebook/serialization/`
is itself now a shim that resolves to the domains submodule via
`from . import register_serializer` inside the domains files).

**Verified** (fresh venv, `PYTHONPATH=src`):
```
kbutillib.notebook.schema.experiment.Sample is kbutillib.domains.notebook.schema.experiment.Sample  -> True
kbutillib.notebook.schema.strain.Strain is kbutillib.domains.notebook.schema.strain.Strain           -> True
kbutillib.notebook.schema.manifest.NotebookEntry is kbutillib.domains.notebook.schema.manifest.NotebookEntry -> True
kbutillib.notebook.schema.vector.Vector is kbutillib.domains.notebook.schema.vector.Vector           -> True
kbutillib.notebook.serialization.register_serializer is kbutillib.domains.notebook.serialization.register_serializer -> True
kbutillib.notebook.helpers.normalize_compartment is kbutillib.domains.notebook.helpers.normalize_compartment -> True
```

**Status: fully resolved.** All previously pydantic-ValidationError-failing
tests in `tests/notebook/test_experiment_store.py`,
`tests/notebook/test_vector_store.py`, `tests/notebook/test_manifest.py`,
`tests/notebook/test_schema.py`, `tests/notebook/test_validate_entities.py`
now pass (see counts below).

## Bug 2 — `notebook/session.py` `..toolkit` relative-import bug

**Symptom**: `kbutillib/domains/notebook/session.py` (package
`kbutillib.domains.notebook`) did `from ..toolkit import KBUtilLib` inside
the `NotebookSession.kbu` property. `..` from that package resolves to
`kbutillib.domains`, so this looked for the nonexistent
`kbutillib.domains.toolkit` module. The real module is the top-level
`kbutillib/toolkit.py` (`kbutillib.toolkit`).

**Fix**: changed the import to the absolute form
`from kbutillib.toolkit import KBUtilLib`, matching the absolute-import
style already used two lines above it in the same function
(`from kbutillib.core.shared_env_utils import SharedEnvUtils`).

**Verified**:
```python
import tempfile
from kbutillib.domains.notebook.session import NotebookSession
s = NotebookSession(kbcache_dir=tempfile.mkdtemp())
type(s.kbu)  # -> <class 'kbutillib.toolkit.KBUtilLib'>
```
`tests/core/test_composition_smoke.py::TestNotebookSessionKbu::test_notebook_session_kbu_returns_facade`
went from FAILED (`ModuleNotFoundError: No module named
'kbutillib.domains.toolkit'`) to PASSED.

**Status: fully resolved.**

## Bug 3 — `verab/facade.py` relative-import bugs (rdkit-gated)

**Symptom**: `kbutillib/domains/cheminformatics/verab/facade.py` (package
`kbutillib.domains.cheminformatics.verab`) had **four** broken deferred
imports that all repeated the package's own path as a prefix instead of
using a plain sibling-relative import:

```python
from .cheminformatics.verab.rule_discovery import discover_verab_rules   # in discover_rules()
from .cheminformatics.verab.king_artifacts import emit_king_workflow     # in emit_king_workflow()
from .cheminformatics.verab import screening                             # in screen()
from .cheminformatics.verab.substructure import MethoxyAromaticFilter    # in enumerate_methoxy_aromatics()
```

Each of these resolved to the nonexistent
`kbutillib.domains.cheminformatics.verab.cheminformatics.verab.*`. These
were previously invisible because `rdkit` (an optional/lazy dependency) was
not installed in the verification venv, so the RDKit-gated methods
(`enumerate_methoxy_aromatics`) short-circuited via `BackendUnavailableError`
before reaching the bad import, and the other three methods weren't
exercised by the always-skipped-without-rdkit `tests/verab/test_verab.py::test_s7_*`
tests.

**Fix**: all four import statements corrected to plain sibling-relative
imports (`.rule_discovery`, `.king_artifacts`, `from . import screening`,
`.substructure`), matching the already-correct `.models`/`.smarts` imports
at the top of the same file and the pattern used throughout the rest of the
`verab` package (`rule_discovery.py`, `screening.py`, `king_artifacts.py`,
`substructure.py` all import their siblings with plain `.` imports).

**Verified** (with `rdkit==2026.3.4` installed ad hoc in the venv):
```
tests/verab/  -> 193 passed, 13 skipped   (was: 190 passed, 3 failed, 13 skipped)
```
The 3 resolved failures were exactly
`test_s7_discover_rules_with_fake_expander`,
`test_s7_discover_rules_via_verabutils_impl`,
`test_s7_enumerate_methoxy_aromatics_with_fake_biochem` — the three `rdkit`-
gated tests that exercise the broken imports.

**Status: fully resolved** (verified with rdkit installed; without rdkit
these code paths are unreachable in normal operation, same as before).

## Bug 4 — `researchos/config.py` shim gap (`_KBUTILLIB_DIR` etc.)

**Symptom**: `kbutillib/researchos/config.py` is a shim re-exporting from
`kbutillib.agents.researchos.config` via `from ... import *` plus an
explicit named list of the four public `resolve_*`/`set_root` functions.
`import *` skips underscore-prefixed names by design, so the shim's
attribute surface was missing `_KBUTILLIB_DIR`, `_DEFAULT_CONFIG_FILE`,
`_DEFAULT_ROOT`, `_DEFAULT_TOOLING_VENV`, `_DEFAULT_AIASSISTANT_ROOT` — all
of which `tests/researchos/test_researchos.py`'s `tmp_home` fixture
`monkeypatch.setattr()`s directly (`raising=True` by default), so every test
using that fixture errored at fixture setup with `AttributeError` before
even running.

**Fix**: added an explicit second import block re-exporting the five
underscore-prefixed module constants the tests touch
(`_KBUTILLIB_DIR`, `_DEFAULT_CONFIG_FILE`, `_DEFAULT_ROOT`,
`_DEFAULT_TOOLING_VENV`, `_DEFAULT_AIASSISTANT_ROOT`), merged into the
shim's single named-import statement (kept ruff `I001`-clean).

Note: this fix removes the `AttributeError` that was blocking the fixture,
but it does **not** make the monkeypatched values flow into
`resolve_researchos_root()`/`resolve_tooling_venv()`/`resolve_aiassistant_root()`
behavior — those functions are defined in (and read their defaults from)
`kbutillib.agents.researchos.config`'s own module globals, not the shim's,
so patching the shim's copy of e.g. `_DEFAULT_ROOT` doesn't change what the
real function returns. This is out of scope for bug 4 as specified (the task
asked only to close the shim re-export gap, not to redesign the
monkeypatch-target contract), and none of the currently-passing assertions
in `test_researchos.py` depend on the patched value actually being read by
the resolver (e.g. `test_root_default_when_nothing_configured` only asserts
`"ResearchOS" in str(result)`, true regardless of which `_DEFAULT_ROOT` copy
is read).

There is a second, structurally identical shim-gap bug in
`kbutillib/researchos/registry.py` (missing `_slug`, referenced by
`tests/researchos/test_researchos.py::TestSlug::*`) and one behavioral
failure in `TestCLISetRoot::test_set_root_persists_with_root_option`. Both
are **out of scope** — not part of the 4 bugs this task was scoped to — and
are left untouched; before/after counts below confirm they're unchanged (7
failures, same set, in both runs).

**Verified**:
```
tests/researchos/test_researchos.py -> 74 passed, 7 failed  (was: 59 passed, 7 failed, 15 errors)
```
The 15 resolved `ERROR`s are exactly the `AttributeError` fixture-setup
failures; the 7 `FAILED` are the pre-existing, out-of-scope `_slug`/
`set_root persists` issues, unchanged before/after.

**Status: fully resolved** (the shim-gap AttributeError bug specifically
called out in the task; the two adjacent out-of-scope issues are documented
above but intentionally untouched).

## Out of scope (untouched, confirmed unchanged before/after)

Per the task's explicit scope boundary, none of the following were touched,
and the full-suite diff (below) confirms zero regressions and these exact
failures/errors persist identically before and after:

- `tests/biochem/test_ms_biochem_deltag.py` (17) — modelseedpy/ModelSEEDDatabase API mismatch (PyPI 0.4.2 vs dev).
- `tests/biochem/test_escher_utils.py` (26 errors) — same root cause, escher/modelseedpy.
- `tests/modeling/test_comprehensive_gapfill_wrapper.py` (4 errors) — gapfill, same root cause.
- `tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback` (1) — same root cause.
- `tests/cli/test_task_a_venv_doctor.py` (5) — unrelated CLI/venv-doctor probe tests.
- `tests/cli/test_notebook.py::test_kernel_fallback_to_python3` (1) — unrelated jupyter-kernel probe.
- `tests/cli/test_init_notebook.py::test_contains_session_for` (1) — unrelated template-rendering test.
- `tests/researchos/test_researchos.py::TestSlug::*` (6) and `TestCLISetRoot::test_set_root_persists_with_root_option` (1) — adjacent shim/behavior gaps not in the 4-bug scope (see Bug 4 note above).

## Before/after test counts

### Targeted subsets (task's VERIFY list)

| Subset | Before | After |
|---|---|---|
| `tests/notebook/test_experiment_store.py` | 1 passed, 9 failed | **10 passed** |
| `tests/notebook/test_vector_store.py` | 3 passed, 16 errors | **19 passed** |
| `tests/notebook/test_manifest.py` | 8 passed, 1 failed | **9 passed** |
| `tests/notebook/test_schema.py` | 34 passed, 1 failed | **35 passed** |
| `tests/notebook/test_validate_entities.py` | 4 passed, 3 failed | **7 passed** |
| `tests/researchos/test_researchos.py` | 59 passed, 7 failed, 15 errors | **74 passed**, 7 failed (unchanged, out-of-scope) |
| `tests/verab/` (rdkit installed) | 190 passed, 3 failed, 13 skipped | **193 passed**, 13 skipped |
| `tests/notebook/helpers/` (cobra installed) | 26 passed | 26 passed (unaffected — confirms no regression) |
| `tests/core/test_deprecation_shims.py` + `tests/guard` | 77 passed, 8 failed | 77 passed, 8 failed (unchanged — pre-existing `escher_utils`/`ms_fba_utils`/`ms_template_utils` modelseedpy-env gap, out of scope) |
| Regression guard: `PYTHONPATH=src python3 -c 'import kbutillib; kbutillib.KBUtilLib'` | exit 0 | exit 0 |
| Regression guard: `PYTHONPATH=src python3 -m kbutillib model --help` | exit 0 | exit 0 |

### Full suite (`pytest tests/ -q --tb=no`)

| | Before (base `4c2a102`) | After (`reorg-livability-fixes`) |
|---|---|---|
| passed | 2658 | **2707** |
| failed | 50 | **32** |
| errors | 61 | **30** |
| skipped | 325 | 325 (unchanged) |

`comm`-diff of the FAILED/ERROR id sets: **0 new failures/errors** introduced
by this branch; **49** previously-failing/erroring tests now pass, all
attributable to the 4 bug fixes above (class-identity, `session.kbu`
composition-smoke test, `verab` rdkit-gated tests, `researchos` fixture
AttributeErrors). The remaining 32 failed + 30 errors are exactly the
out-of-scope set enumerated above, byte-for-byte identical before and after.

## Ruff

`ruff check` on all touched files: clean (`All checks passed!`).
