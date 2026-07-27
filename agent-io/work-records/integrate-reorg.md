# Work Record: integrate-reorg

## task_id
`integrate-reorg` (branch name; no separate Maestro task_id was supplied in
the envelope)

## branch
`integrate-reorg`

## commit_shas
- `4223797c9516b654b1104a90c9942d8ef37d1f24` — feat(integrate-reorg): re-land LP-solver + ARGO delta onto Vibhav's reorg base

Branch base (unmodified, adopted as the integration tip per STEP 0):
`e23caa5` (`vibhav/feature/reorg-api-mcp-explore`, fetched from
`https://github.com/VibhavSetlur/KBUtilLib`).

## summary

Per STEP 0 of the task prompt, fetched the third-party remote `vibhav`
(`https://github.com/VibhavSetlur/KBUtilLib`) branch
`feature/reorg-api-mcp-explore` in the Dropbox-parked KBUtilLib repo, then
created an isolated git worktree at `~/.maestro/worktrees/integrate-reorg`
on a new branch `integrate-reorg` rooted at that branch's tip (`e23caa5`).
Confirmed `src/kbutillib/domains/` exists on that tip before doing any
delta work (the required precondition assertion).

Re-landed the `main`/`wip` delta the reorg lacks, entirely inside that
worktree (the `wip` working tree on the Dropbox parking repo was never
touched):

1. **LP-solver service layer** — copied `services/lp_solver/app.py`,
   `worker.py`, and `deploy/{enable_lp_solver.sh,lp-solver.service}` from
   `main` verbatim (the shared `__init__.py`/`job_store.py`/
   `solver_backends.py` were already byte-identical between `main` and the
   reorg, confirmed by diff, so left untouched). Repointed `app.py`'s single
   flat import (`from kbutillib.shared_env_utils import SharedEnvUtils`) to
   `from kbutillib.core.shared_env_utils import SharedEnvUtils`; `worker.py`
   had no flat kbutillib imports to repoint.
2. **LP-solver client** — copied `ms_remote_solver_utils.py` and
   `ms_remote_solve_utils.py` from `main` into
   `src/kbutillib/domains/modeling/`, repointed
   `ms_remote_solver_utils.py`'s `from .shared_env_utils import
   SharedEnvUtils` to `from kbutillib.core.shared_env_utils import
   SharedEnvUtils` (matching the absolute-import convention used elsewhere
   in `domains/modeling/`), fixed the module's `print_docs()` docs-path
   traversal (`module_dir.parent.parent` → `.parent.parent.parent.parent`,
   accounting for the two extra directory levels), and updated stale
   flat-path docstring cross-references
   (`kbutillib.argo_utils`/`kbutillib.kb_berdl_utils` →
   `kbutillib.domains.ai.argo_utils`/`kbutillib.domains.kbase.kb_berdl_utils`).
   `ms_remote_solve_utils.py` had no internal kbutillib imports to repoint.
3. **`remote_solver`/`remote_solve` facade wiring** — added
   `self._remote_solver = None` to `KBUtilLib.__init__`, and a
   `remote_solver` lazy property + `remote_solve()` method to
   `toolkit.py`, mirroring the existing lazy-property pattern (see
   `berdl`/`argo`) and repointed to the new
   `domains.modeling.ms_remote_solver_utils`/`ms_remote_solve_utils` import
   paths. Used the reorg's `X | None` PEP 604 annotation style (the file
   has `from __future__ import annotations` and no `Optional` import)
   rather than reintroducing `typing.Optional`.
4. **Package exports** — added `RemoteSolveResult`/`remote_solve` as an
   eager top-level import in `__init__.py` (no external deps, same
   treatment as `model_directionality`/`model_helpers`), and
   `MSRemoteSolverUtils`/`MSRemoteSolverUtilsImpl` as
   try/except-`ImportError`-guarded legacy-class and Impl-class imports
   (matching every other domain re-export in that file), plus the four new
   `__all__` entries.
5. **ARGO httpx-0.28 fix** — applied the same `proxies={http://:..,
   https://:..}` → single `proxy="socks5://127.0.0.1:<port>"` fix (from
   `main`'s `ece70da`) to `src/kbutillib/domains/ai/argo_utils.py` (the
   renamed file; git's rename detection was not exercised since this
   worktree was cut fresh from the reorg tip rather than merged, so the fix
   was applied by hand as the task anticipated).

Reconciled the four conflict hotspots directly (no git merge conflicts
occurred, since the integration branch is a fresh checkout of the reorg
tip with the delta layered on by hand, not a merge of two diverged
histories):
- `domains/ai/argo_utils.py` — ARGO fix applied.
- `src/kbutillib/__init__.py` — `MSRemoteSolverUtils(Impl)` +
  `RemoteSolveResult`/`remote_solve` re-exports added.
- `src/kbutillib/toolkit.py` — `remote_solver`/`remote_solve` facade
  wiring added.
- `pyproject.toml` — new `ai` extra (`httpx[socks]>=0.28`) and `lp_solver`
  extra (`fastapi`/`uvicorn[standard]`) added; core `dependencies` list
  untouched.
- `config.yaml` — the `remote_solver:` block (present on `main`, absent
  from the reorg, which never touched `config.yaml`) appended verbatim.

**Dependency hygiene:** `httpx[socks]>=0.28` was on `wip` in the core
`dependencies` list (added by the wip-only ARGO fix commit `ece70da`); per
the task's explicit instruction this integration instead declares it
behind a new `ai` extra (both call sites — `ArgoUtils` module-level
`import httpx`, and `MSRemoteSolverUtils`'s `import httpx` — are reached
only through `try/except ImportError` re-exports in `__init__.py` or lazy
properties in `toolkit.py`, so a core install degrades gracefully instead
of raising). Cheminformatics deps (rdkit/pickaxe/retrorules) were already
lazily imported inside `try/except` blocks in the reorg with **no**
pyproject declaration at all (verified via grep — no rdkit/pickaxe/
retrorules exists anywhere in `pyproject.toml`), so no change was needed
there. `fastapi`/`uvicorn`/`mcp` were already behind the reorg's existing
`api`/`mcp`/`all` extras; added a parallel `lp_solver` extra (reusing the
`api` extra's version floors) for the service-side deps, per Implementation
Decision #6 ("preserve ... the lp_solver extra").

**Server bind host:** changed the `--host` default from `0.0.0.0` to
`127.0.0.1` in both `main()` argparse defaults (`interfaces/api/app.py`,
`interfaces/mcp/server.py`) and in `run_http()`'s function-signature default
in `server.py`, updating the accompanying docstrings/usage examples to
match (`--host 0.0.0.0` is now presented as the deliberate-widen option).
The `lp_solver` service (`services/lp_solver/app.py`) already defaulted to
`127.0.0.1` on `main` — no change needed there. The `deploy/poplar` systemd
unit (`kbutillib-api.service`) already hardcodes `--host 127.0.0.1` — no
change needed (out of scope per the task, which named only the two
`interfaces/*` entrypoints).

Explicitly did **not** touch downstream consumers, shipped skills, or the
flat-submodule deprecation shim — those are later phases per the task
prompt and the PRD's phasing.

## files_touched

Modified:
- `config.yaml` — appended the `remote_solver:` config block from `main`.
- `pyproject.toml` — added `ai` extra (`httpx[socks]>=0.28`) and
  `lp_solver` extra (`fastapi`, `uvicorn[standard]`).
- `src/kbutillib/__init__.py` — eager `RemoteSolveResult`/`remote_solve`
  import; guarded `MSRemoteSolverUtils`/`MSRemoteSolverUtilsImpl`
  re-exports; four new `__all__` entries.
- `src/kbutillib/domains/ai/argo_utils.py` — httpx-0.28 `proxy=` fix.
- `src/kbutillib/interfaces/api/app.py` — `--host` default `0.0.0.0` →
  `127.0.0.1`; docstring/usage updates.
- `src/kbutillib/interfaces/mcp/server.py` — `run_http()`'s `host` default
  and `--host` argparse default `0.0.0.0` → `127.0.0.1`; docstring/usage
  updates.
- `src/kbutillib/toolkit.py` — `remote_solver` lazy property,
  `remote_solve()` method, `_remote_solver` backing field, TYPE_CHECKING
  imports.

Added:
- `src/kbutillib/domains/modeling/ms_remote_solve_utils.py` — LP-solve
  glue (`RemoteSolveResult`, `remote_solve`), copied from `main` unchanged
  (no internal kbutillib imports needed repointing).
- `src/kbutillib/domains/modeling/ms_remote_solver_utils.py` — LP-solver
  HTTP client (`MSRemoteSolverUtils`, `MSRemoteSolverUtilsImpl`), copied
  from `main` with its `SharedEnvUtils` import and docstring cross-refs
  repointed.
- `src/kbutillib/services/lp_solver/app.py` — FastAPI surface, copied from
  `main` with its `SharedEnvUtils` import repointed.
- `src/kbutillib/services/lp_solver/worker.py` — worker pool, copied from
  `main` unchanged.
- `src/kbutillib/services/lp_solver/deploy/enable_lp_solver.sh` — H100
  systemd deploy script, copied from `main` unchanged.
- `src/kbutillib/services/lp_solver/deploy/lp-solver.service` — systemd
  unit template, copied from `main` unchanged.

## success_criteria_check

1. **`src/kbutillib/domains/` exists on the integration branch** — PASS.
   Present at the adopted reorg tip and unmodified since.
2. **`src/kbutillib/services/lp_solver/app.py` and `worker.py` exist** —
   PASS. Both added in this commit.
3. **`src/kbutillib/domains/modeling/ms_remote_solver_utils.py` exists** —
   PASS. Added in this commit.
4. **`domains/ai/argo_utils.py` contains `proxy=` and no `proxies=`
   kwarg** — PASS. Verified both by grep (`proxy=` present) and by an AST
   walk asserting no `ast.keyword(arg="proxies")` node exists anywhere in
   the file (the only remaining textual `proxies` occurrence is inside an
   explanatory code comment, not a live kwarg).
5. **`PYTHONPATH=src python3 -c 'import kbutillib; kbutillib.KBUtilLib'`
   exits 0** — PASS. Verified both in the ambient dev environment (which
   has cobra/fastapi/httpx installed) and in a from-scratch `pip install
   -e .` core venv with none of those packages (see criterion 7 below).
6. **`PYTHONPATH=src python3 -m kbutillib model --help` and `... cap list`
   exit 0** — PASS. Both verified in the ambient environment; `model
   --help` additionally verified in the clean core venv.
7. **A core install with no extras imports kbutillib and runs `kbu model
   --help` without rdkit/fastapi/mcp installed** — PASS. Built a fresh
   `python3 -m venv` + `pip install -e .` (no `[extras]`), confirmed via
   `pip list` that rdkit/fastapi/uvicorn/mcp/httpx are all absent, then ran
   `python3 -c 'import kbutillib; kbutillib.KBUtilLib'` (prints a
   "13 optional modules unavailable" degradation notice, including
   `ms_remote_solver_utils`, and returns 0) and `kbu model --help` (via the
   installed console script) and `kbu cap list`, both exit 0.

All seven listed success criteria PASS.

## tests_run

- `PYTHONPATH=src python3 -m pytest --collect-only -q` — **3005 tests
  collected, 0 collection errors**, both before and after the ruff
  auto-fix, confirming `toolkit.py`/`__init__.py` parse and import cleanly
  across the entire tree (this is the most conservative regression check
  available without the full ~2,500-test run the PRD defers to h100).
- `PYTHONPATH=src python3 -m pytest -k "argo or lp_solver or remote_solve
  or remote_solver" -q` — 36 passed, 5 skipped, 1 failed
  (`test_gurobi_inf_or_unbd_disambiguates_to_unbounded`). Confirmed this
  failure **pre-exists on the pristine, untouched reorg tip**
  (`e23caa5`) by running the same test in a separate scratch worktree
  before any of my edits — it is a local-Gurobi-behavior issue in
  `tests/modeling/test_lp_solver_solver_backends.py` (a file I never
  touched, byte-identical to `main`), not a regression from this task.
- `PYTHONPATH=src python3 -c "... ArgoUtils(model='gpt4o', env='dev',
  ...)"` — constructs successfully (the httpx-0.28 regression test
  pattern from `ece70da`, run inline since the reorg branch doesn't yet
  carry `tests/test_argo_utils_construct.py` — copying that test file was
  not in the task's explicit re-land list, so it was left for a later
  phase).
- `PYTHONPATH=src python3 -c "from kbutillib.services.lp_solver import
  app, worker; app.create_app(base_dir='/tmp/...')"` — FastAPI app builds
  successfully.
- `PYTHONPATH=src python3 -c "import kbutillib; kbu =
  kbutillib.KBUtilLib(); kbu.remote_solver; kbu.remote_solve"` — facade
  wiring resolves correctly to the new `domains.modeling.*` classes.
- `pytest tests/domains/ tests/core/` — 468 passed, 9 skipped, 1 xfailed, 5
  failed, 3 errors. All 8 failures/errors traced to a missing
  `src/ModelSEEDDatabase/Biochemistry/` data directory (a separate git
  clone/submodule this worktree never had populated) and a pre-existing
  stdout-logging contamination in `test_cli_cap.py`'s JSON-output test —
  neither is caused by, or related to, this task's delta.
- `ruff check` on all 9 touched/added Python files + `pyproject.toml` —
  clean after one auto-fix (`ruff check --fix`) to an import-formatting
  nit (`I001`) in the new `toolkit.py` code.
- Fresh core-only venv (`python3 -m venv` + `pip install -e .`, no
  extras) — `pip list` confirms no rdkit/fastapi/uvicorn/mcp/httpx;
  `import kbutillib`, `kbu model --help`, `kbu cap list` all exit 0. Run
  twice (before and after the ruff auto-fix) to be safe; both clean.
- **Not run:** the full ~2,500-test suite (explicitly deferred to h100 as
  next-stage verification per the PRD's "Minimum landing bar"), and no
  attempt was made to relocate `main`'s `tests/test_lp_solver_service.py`
  / `test_lp_solver_service.py`-equivalent worker tests into
  `tests/modeling/` (test relocation wasn't in this task's explicit
  five-item re-land list; the PRD's confront-hardened resolution #8 also
  says test relocation happens as a distinct post-merge step).

## caveats

- **No git merge occurred.** Per the task's own wording ("create an
  integration branch off its tip"), the branch was cut directly from
  `vibhav/feature/reorg-api-mcp-explore`'s tip rather than merged from a
  diverging `main`/`wip` history, so there were no git-level merge
  conflicts to resolve in the four "hotspot" files — the delta was
  layered on by hand, file by file, following the PRD's authoritative
  old→new mapping. This matches the task prompt's explicit STEP 0
  instruction and the PRD's "git mechanics ... are the developer's
  choice" language, but means the "conflict hotspots" language in the
  task description is descriptive of *risk*, not of actual merge markers
  encountered.
- **Landing to `main`/`wip` is out of scope for this task.** The task
  prompt only asked me to commit on `integrate-reorg`; I did not merge
  `integrate-reorg` into `main`, nor `main` into `wip`, nor push to
  `origin`/`vibhav`. That landing step (PRD Implementation Decision
  "Landing (parking-branch model)" and Acceptance Criterion 15) is
  presumably a separate, later task.
- **`lp_solver` extra version floors.** Main's `lp_solver` extra pinned
  `fastapi >=0.100` / `uvicorn >=0.23`; I raised those to match the
  reorg's existing `api` extra (`fastapi >=0.110` / `uvicorn[standard]
  >=0.29`) rather than introducing a second, looser version range for the
  same packages — a deliberate judgment call to keep the dependency graph
  consistent, not a literal "preserve main's pins" reading of
  Implementation Decision #6.
- **LP-solver service/worker tests not relocated.** `main` has
  `tests/test_lp_solver_service.py` covering `app.py`'s HTTP surface (the
  reorg tree only carries `job_store`/`solver_backends` tests, which were
  already present and passing). I left this uncopied since it wasn't in
  the task's explicit five-item list and the PRD frames test relocation as
  a distinct post-merge step (confront resolution #8) — flagging so a
  follow-up task doesn't assume `app.py`/`worker.py` are test-covered on
  this branch yet.
- **`tests/test_argo_utils_construct.py` not copied either**, for the same
  reason; I instead ran the equivalent construction check inline (see
  `tests_run`) to confirm the fix works, but there's no regression test
  pinning it on this branch yet.
- **Pre-existing, unrelated test failures** (documented in detail under
  `tests_run`) exist on this branch purely because of local-environment
  gaps (no `ModelSEEDDatabase` clone, a Gurobi license/version quirk, a
  stdout-logging leak into a JSON-output test) — all reproduced identically
  on the untouched reorg tip before I made any changes, so none are new
  regressions from this task, but a reviewer running the suite fresh will
  see the same red without any of my code being at fault.
