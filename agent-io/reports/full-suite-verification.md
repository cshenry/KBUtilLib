# KBUtilLib full-suite verification (local, primary-laptop, off merged `main`)

**Branch**: `full-suite-verification-local`
**Base**: `main` @ `20e1e0c62dbc769eadfcc081c4f38eb2cfe42b81` (merge, parents `d1bcbcb` LP-solver/ARGO delta + `5dc3a87` integrate-reorg-skills)
**Run environment**: macOS (darwin/arm64), fresh throwaway venv at
`/private/tmp/claude-501/.../scratchpad/kbutillib-fullsuite-venv` (Python 3.11.14),
**not** `~/VirtualEnvironments`. `pip install -e .` was run only into that
throwaway venv — no shared venv was touched.

Precondition verified before starting: `git log -1 --format='%H %P'` showed
`20e1e0c` with two parents, and both `src/kbutillib/domains/` and
`src/kbutillib/services/lp_solver/app.py` existed in the worktree.

## Install outcome

Installed via `pip install -e ".[all]" --group dev --group lint`, then
`pip install -e ".[lp_solver]"`, then `--group notebook --group typeguard
--group xdoctest --group mypy`. All of these installed cleanly (`all` covers
`mcp`, `api`/fastapi+uvicorn, `ai`/httpx[socks], `apidocs`/mkdocs; `lp_solver`
adds fastapi+uvicorn again for the service side; `notebook` group brings in
pandas/jupyter/ipykernel/itables/tqdm).

Note: `pyproject.toml` has **no** `cheminformatics` or `server` extras group
by those names — the actual optional-dependency groups are `mcp`, `api`,
`ai`, `lp_solver`, `apidocs`, `all` (verified by reading
`[project.optional-dependencies]`). "server" transport deps are covered by
`api`/`mcp`/`all`; there is no declared "cheminformatics" extra because
`rdkit`/`pickaxe`(`minedatabase`)/`retrorules` are not pip dependencies of
this project at all — they're optional runtime imports guarded by
`BackendUnavailableError`/`pytest.importorskip`.

Additional ad-hoc installs attempted for maximal coverage, per the task's
"cheminformatics" and "kbu_model" instruction:

- `rdkit` — **installed successfully** (PyPI has arm64 macOS wheels,
  `rdkit==2026.3.4`). This unmasked 3 previously-always-skipped
  `tests/verab/test_verab.py::test_s7_*` tests (see below).
- `minedatabase` (the PyPI package providing the `pickaxe` expansion engine)
  — **failed to build** on this macOS/Python-3.11 environment:
  `numpy`'s legacy `numpy.distutils` build path errors with
  `NameError: name 'CCompiler' is not defined` while building a
  `scikit-learn` build-dependency, because `minedatabase`'s pin pulls an old
  numpy that is incompatible with the installed `setuptools`/Cython
  toolchain on this machine. This is an environmental/macOS-toolchain gap,
  not a code issue — `retrorules_backend.py` needs no external pip package
  (it's pure CSV/file-based), and the cheminformatics smoke tests
  (`tests/domains/test_cheminformatics_smoke.py`) are import-only and pass
  regardless (`PickaxeBackend`/`RetroRulesBackend` raise
  `BackendUnavailableError` at call time, not import time). No pickaxe-backed
  test in the suite requires a live `minedatabase` install (none use
  `pytest.importorskip("minedatabase")`), so this gap causes **zero**
  additional test failures — it's purely a maximal-effort install attempt
  that didn't pan out, noted for completeness.
- `cobra` + `modelseedpy` (PyPI `modelseedpy==0.4.2`) — **installed
  successfully**, but this exposed a large, single-root-cause class of
  environmental failures: PyPI's public `modelseedpy` release does not match
  the API kbutillib's `domains/biochem`, `domains/modeling`, and
  `domains/notebook/escher_utils.py` code expects (e.g.
  `ModelSEEDBiochem.get()` doesn't accept a `path=` kwarg on PyPI 0.4.2;
  `MSPackageManager` lacks `ObjectivePkg`; `MSGapfill.__init__()` doesn't
  accept `base_media`). This is the exact "missing local ModelSEEDDatabase
  checkout" pre-existing category named in the task brief — see below.
- `aiohttp` — installed (see "Fixed regressions" below; this one **was**
  declared as a project dependency by this branch, not just installed
  ad-hoc into the venv).

## Test results

Final full run (`pytest -q --no-header -ra`):

```
79 failed, 2714 passed, 247 skipped, 1 xfailed, 261 warnings, 62 errors in 124.23s
```

(First raw run, before any fixes: `82 failed, 2711 passed, 247 skipped, 1
xfailed, 62 errors`. The delta — 3 fewer failures, 3 more passes — is the
`aiohttp` fix below.)

## Fixed regressions (committed on this branch)

### 1. Relocated the re-landed LP-solver client tests into `tests/modeling/` (task item 4)

`tests/test_ms_remote_solve_utils.py` and `tests/test_ms_remote_solver_utils.py`
imported from the flat `kbutillib.ms_remote_solve_utils` /
`kbutillib.ms_remote_solver_utils` module paths, which were intentionally
deleted by the reorg landing (superseded by
`kbutillib.domains.modeling.ms_remote_solve_utils` /
`...ms_remote_solver_utils`, plus the `KBUtilLib` facade's
`.remote_solve()`/`.remote_solver` properties — no external consumers of the
flat path). Per the task's explicit instruction, this was an **expected**
consequence of the reorg, not a regression to preserve.

Fix: `git mv` both files into `tests/modeling/`, and updated their imports
to the canonical `kbutillib.domains.modeling.*` path (dropping the
now-nonexistent flat-path assertions; no test *logic* changed, only the
import surface under test).

- `tests/modeling/test_ms_remote_solve_utils.py` — imports
  `QP_CAPABLE_MODULE_PREFIXES`, `RemoteSolveResult`, `remote_solve` from
  `kbutillib.domains.modeling.ms_remote_solve_utils` (unchanged from
  `kbutillib.domains.modeling.ms_remote_solver_utils.MSRemoteSolverUtils`
  used in the opt-in live round-trip test).
- `tests/modeling/test_ms_remote_solver_utils.py` — imports
  `kbutillib.domains.modeling.ms_remote_solver_utils` module object (for
  `monkeypatch.setattr`) and `MSRemoteSolverUtils` from the same canonical
  path.

Result: `11 passed, 1 skipped` (the 1 skip is the opt-in live round-trip
test, gated on `KBUTILLIB_REMOTE_SOLVER_LIVE_TEST=1`).

### 2. Declared `aiohttp` as a core dependency (`pyproject.toml`)

`src/kbutillib/domains/external/rcsb_pdb_utils.py` unconditionally does
`import aiohttp` and has done so since the reorg landed it (`cf70f53`), but
`aiohttp` was never declared anywhere in `pyproject.toml`. This alone would
be a pre-existing, out-of-scope reorg gap — **except** that the deprecation-
shim commit (`fa13496`, part of the re-landed `integrate-reorg` side of this
merge, not present on the pristine `vibhav/feature/reorg-api-mcp-explore`
base) added two **new** test files that assert against exactly this surface
and did not exist before that commit:

- `tests/core/test_deprecation_shims.py` — asserts every `LEGACY_MODULE_MAP`
  entry (including `rcsb_pdb_utils`) imports cleanly with a
  `DeprecationWarning`.
- `tests/guard/test_silent_none_reexports.py` — asserts every class-shaped
  `kbutillib.__all__` entry (including `RCSBPDBUtils`/`RCSBPDBUtilsImpl`)
  resolves to non-`None`.

Both failed in a stock `pip install .` because `RCSBPDBUtils`/
`RCSBPDBUtilsImpl` degrade to `None` (caught `ImportError` in
`kbutillib/__init__.py`) when `aiohttp` is absent. Since these tests do not
exist on the pristine reorg base, this doesn't meet the task's "reproduces
on pristine base" bar for pre-existing — it's only observable because of
the shim delta's own new test surface, so I fixed it rather than
xfail-documenting it. `aiohttp` is a lightweight, portably-wheeled
dependency (no macOS build issues), so declaring it outright (vs. gating
behind a new "external" extra) was the minimal, low-risk fix.

Added `"aiohttp >=3.9",` to `[project].dependencies` in `pyproject.toml`.

Result: `tests/core/test_deprecation_shims.py` and
`tests/guard/test_silent_none_reexports.py` — `84 passed`.

## Documented pre-existing xfails / environmental gaps (NOT fixed — out of scope)

For every failure below, I confirmed the failure **reproduces identically**
on the pristine reorg base (`vibhav/feature/reorg-api-mcp-explore` @
`e23caa5`, fetched and checked out in a scratch worktree at
`/tmp/kbutillib-reorg-base-check`, removed after verification) — either by
diffing the relevant source/test file byte-for-byte between this branch and
the base (identical in every case below) and/or by re-running the specific
failing test file against the base with `PYTHONPATH` pointed at its `src/`
using the same venv. None of these are attributable to the re-landed
LP-solver delta or the deprecation shim.

1. **PyPI `modelseedpy` API mismatch / missing local ModelSEEDDatabase
   checkout** (the exact category named in the task brief). Root cause:
   `domains/biochem/ms_biochem_utils.py`, `domains/modeling/ms_fba_utils.py`,
   `domains/modeling/ms_reconstruction_utils.py`, and
   `domains/notebook/escher_utils.py` are all byte-identical between this
   branch and the pristine base, and all assume a modelseedpy dev/git
   version (or a local `ModelSEEDDatabase` checkout) that PyPI's public
   `modelseedpy==0.4.2` doesn't match
   (`ModelSEEDBiochem.get()` rejects `path=`; `MSPackageManager` has no
   `ObjectivePkg`; `MSGapfill.__init__()` has no `base_media` kwarg).
   Affects: `tests/biochem/test_escher_utils.py` (26 errors),
   `tests/biochem/test_ms_biochem_deltag.py` (17 failed),
   `tests/biochem/test_ms_fba_utils_eval.py` (19 failed),
   `tests/modeling/test_comprehensive_gapfill_wrapper.py` (4 failed),
   `tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback` (1 failed),
   `tests/cli/test_model.py::TestVerifiedVerbChain` (2 failed + 2 errors),
   `tests/core/test_composition_smoke.py::TestThermoUtils`/`TestMSBiochemUtils` (1 failed + 3 errors).

2. **Un-shimmed duplicate `kbutillib/notebook/{schema,serialization,storage,helpers}/*`
   subpackages.** The reorg (`cf70f53`) correctly converted the top-level
   flat `kbutillib/notebook/*.py` files (session.py, cache.py, manifest.py,
   vector_store.py, experiment_store.py, detect.py) into thin
   `from kbutillib.domains.notebook.X import *` shims, but left the nested
   `schema/`, `serialization/`, `storage/`, `helpers/` subpackages (22 files)
   as **full byte-identical duplicates** of their `domains/notebook/`
   counterparts rather than shims — verified with a pairwise `diff`. Because
   pydantic models compare by class identity, a `Sample`/`Computation`/etc.
   constructed via the flat `kbutillib.notebook.schema.experiment` path is a
   *different class* than the one `domains/notebook/experiment_store.py`
   expects, so `Experiment(payload=...)` raises a pydantic
   `ValidationError`. Confirmed present verbatim on the pristine base (same
   duplicate files, same tests, same failures reproduced 1:1 when run there).
   Affects: `tests/notebook/test_vector_store.py` (16 errors),
   `tests/notebook/test_experiment_store.py` (9 failed),
   `tests/notebook/test_manifest.py` (1 failed),
   `tests/notebook/test_schema.py` (1 failed),
   `tests/notebook/test_validate_entities.py` (3 failed).

3. **`kbutillib/researchos/config.py` shim doesn't re-export
   `_KBUTILLIB_DIR`.** The shim (`from kbutillib.agents.researchos.config
   import *` + an explicit named list of public functions) never re-exports
   the underscore-prefixed `_KBUTILLIB_DIR` module constant that
   `tests/researchos/test_researchos.py`'s `tmp_home` fixture
   `monkeypatch.setattr`s. Byte-identical file/test on the pristine base
   (present since `cf70f53`); reproduces identically there
   (`7 failed, 15 errors`, matching this branch exactly).

4. **Duplicate, un-shimmed `kbutillib/cli/init_notebook.py` vs.
   `kbutillib/interfaces/cli/init_notebook.py`.** Same full-duplicate
   pattern as #2, at the CLI layer. `tests/cli/test_init_notebook.py::
   test_contains_session_for` fails because the rendered template doesn't
   contain `def session_for`. Byte-identical source/test vs. base;
   reproduces there.

5. **Local Jupyter kernel-registry pollution on this specific machine.**
   `tests/cli/test_notebook.py::test_kernel_fallback_to_python3` expects
   `_select_kernel`'s fallback path to fire for a "nonexistent kernel"
   project, but this laptop has ~12 real project kernels (`kbutillib`,
   `modelingloe`, `anmenotebooks`, etc.) registered globally under
   `~/Library/Jupyter/kernels` from actual prior use, which short-circuits
   the code path the test exercises before the mocked
   `_select_kernel` is ever reached. Purely a per-machine environment
   artifact, unrelated to any branch; byte-identical source/test vs. base.

6. **`machine_configs/_default.yaml` drift vs. `pyproject.toml`
   dependencies.** `pyproject.toml`'s core `dependencies` already list
   `requests_toolbelt >=0.10.0` and `tomli-w >=1.0` (both pre-existing,
   present on the base too), but `machine_configs/_default.yaml`'s
   `notebook_deps` list was never updated to match, so
   `tests/cli/test_task_a_venv_doctor.py::TestDefaultYamlNotebookDeps`
   (4 tests) fails, and `TestProbeFbaImports::
   test_probe_reports_missing_dep_name` fails because the probe correctly
   reports PASS (the dependency genuinely *is* installed in this venv) where
   the test's docstring assumes it would be absent. Byte-identical
   `_default.yaml`/test vs. base; reproduces there.

7. **`domains/notebook/session.py` relative-import bug** — explicitly named
   in the task brief as a known pre-existing issue. `NotebookSession.kbu`
   does `from ..toolkit import KBUtilLib` from inside
   `kbutillib.domains.notebook.session`, which resolves to
   `kbutillib.domains.toolkit` (two dots = one level up = `domains`) instead
   of the real `kbutillib.toolkit` (needs three dots, or an absolute
   import). Raises `ModuleNotFoundError: No module named
   'kbutillib.domains.toolkit'`. Byte-identical file vs. base; reproduces
   there with the identical traceback. Affects:
   `tests/core/test_composition_smoke.py::TestNotebookSessionKbu::test_notebook_session_kbu_returns_facade`.

8. **`domains/cheminformatics/verab/facade.py` relative-import bug** (latent
   — only surfaces now that `rdkit` is installed, since these 3 tests are
   `@pytest.mark.skipif(not _RDKIT_PRESENT, ...)` and every prior run of
   this suite presumably lacked rdkit). `VerabUtils.discover_rules`/
   `enumerate_methoxy_aromatics` do `from .cheminformatics.verab.X import
   ...` from inside `kbutillib.domains.cheminformatics.verab.facade`, which
   resolves to the nonexistent
   `kbutillib.domains.cheminformatics.verab.cheminformatics.X` instead of
   `kbutillib.domains.cheminformatics.verab.X`. Byte-identical file/test vs.
   base; reproduces there identically once rdkit is present in that
   environment too (verified: `3 failed, 16 passed, 1 skipped, 62
   deselected`, matching this branch's 3 verab failures exactly). Affects:
   `tests/verab/test_verab.py::test_s7_discover_rules_with_fake_expander`,
   `::test_s7_discover_rules_via_verabutils_impl`,
   `::test_s7_enumerate_methoxy_aromatics_with_fake_biochem`.

9. `tests/core/test_composition_smoke.py::TestModelStandardizationUtils::
   test_model_standardization_runs_without_error` — already an explicit
   `XFAIL` in the repo (P0 bug tracked for "Task 2" per its own marker
   reason), not something this task touches.

## Statement on remaining regressions

**No failure attributable to the re-landed LP-solver delta or the
deprecation shim remains unaddressed.** Every one of the 79 failed / 62
errored tests in the final run was traced to one of the 9 root causes above,
and every root-cause source file (or, for #5/#6, the relevant fixture/config
data) was verified byte-identical between this branch and the pristine
`vibhav/feature/reorg-api-mcp-explore` reorg base, and — where the test
itself also predates the shim/LP-solver work — re-run against that base to
confirm the identical failure reproduces there. The only two genuinely
shim-delta-attributable issues found (the relocated flat-module tests, and
the `aiohttp`-dependent new guard/shim tests) are fixed and committed on
this branch.
