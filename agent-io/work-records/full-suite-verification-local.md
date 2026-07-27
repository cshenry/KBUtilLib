# task_id

full-suite-verification-local (Maestro developer task run locally on primary-laptop, h100 unavailable)

# branch

full-suite-verification-local

# commit_shas

1. 63062f050fd0413470babc0ddfeb9dc842c215aa — test(modeling): relocate LP-solver client tests to tests/modeling/
2. 9e7e0335bceee973a43a66b443a4ddeaa759fb50 — chore(tests): remove flat-path LP-solver client test originals
3. 3bbddd964e3734c08cdb1a25ab012a87c0916abf — fix(deps): declare aiohttp as a core dependency
4. 04a598691d135d36292c0079144cf9af847c0b5c — docs(reports): add full-suite-verification report

HEAD = `04a598691d135d36292c0079144cf9af847c0b5c`

# summary

Ran the full KBUtilLib pytest suite off merged `main` (20e1e0c, the merge of
the re-landed LP-solver/ARGO delta with the reorg base + deprecation shims +
skill netting) in a fresh throwaway venv on macOS/arm64, since h100 was
unavailable and the user explicitly authorized running this normally-h100
task locally. Installed all declared optional-dependency groups (`all`,
`lp_solver`, plus dev/lint/notebook/typeguard/xdoctest/mypy dependency
groups) plus ad-hoc `rdkit`, `cobra`, `modelseedpy`, and `aiohttp` for
maximal coverage; `minedatabase` (the pickaxe backend) failed to build on
this macOS toolchain (legacy numpy/setuptools incompatibility) but causes no
test failures since nothing in the suite requires a live pickaxe install.
The first full run produced 82 failed / 2711 passed / 247 skipped / 1
xfailed / 62 errors. Every one of the 144 failing/erroring tests was
individually traced to a root cause and verified (via byte-identical
source/test diffs against, and in most cases direct re-execution on, the
pristine `vibhav/feature/reorg-api-mcp-explore` reorg base in a scratch
worktree) to be either pre-existing on that base or a genuine
shim/LP-solver-delta regression. Two genuine regressions were found and
fixed: (1) two LP-solver client test files still imported the intentionally
deleted flat `kbutillib.ms_remote_solve_utils`/`ms_remote_solver_utils`
paths — relocated to `tests/modeling/` against the canonical
`kbutillib.domains.modeling.*` path per the task's explicit instruction; (2)
`aiohttp` was never declared as a dependency despite being a hard
requirement of `domains/external/rcsb_pdb_utils.py`, which broke two new
test files added by the deprecation-shim commit itself
(`tests/core/test_deprecation_shims.py`,
`tests/guard/test_silent_none_reexports.py`) that don't exist on the
pristine base — fixed by declaring `aiohttp >=3.9` in `pyproject.toml`. The
final run is 79 failed / 2714 passed / 247 skipped / 1 xfailed / 62 errors,
with every remaining failure documented in
`agent-io/reports/full-suite-verification.md` as a pre-existing/environmental
xfail, none attributable to the LP-solver delta or deprecation shim.

# files_touched

- `tests/modeling/test_ms_remote_solve_utils.py` (moved from `tests/test_ms_remote_solve_utils.py`, imports repointed to `kbutillib.domains.modeling.ms_remote_solve_utils`)
- `tests/modeling/test_ms_remote_solver_utils.py` (moved from `tests/test_ms_remote_solver_utils.py`, imports repointed to `kbutillib.domains.modeling.ms_remote_solver_utils`)
- `pyproject.toml` (added `"aiohttp >=3.9",` to `[project].dependencies`)
- `agent-io/reports/full-suite-verification.md` (new — full report)

# success_criteria_check

- **Assert worktree is off merged main (20e1e0c, two parents) with domains/ and services/lp_solver/app.py present** — PASS. Verified via `git log -1 --format='%H %P'` before starting and confirmed both paths exist.
- **Install extras for the full suite (cheminformatics, server), noting macOS-uninstallable deps explicitly** — PASS with a caveat: `pyproject.toml` has no extras literally named "cheminformatics"/"server" (verified by reading `[project.optional-dependencies]`: only `mcp`, `api`, `ai`, `lp_solver`, `apidocs`, `all` exist); installed `all` + `lp_solver` + dev/lint/notebook/typeguard/xdoctest/mypy groups (covers the "server" transports), plus ad-hoc `rdkit`/`cobra`/`modelseedpy`/`aiohttp`. `minedatabase` (pickaxe) failed to build on macOS — documented explicitly in the report with the exact error, and confirmed it causes zero additional test failures since nothing imports it eagerly or gates on it via `importorskip`.
- **Run pytest, capture pass/fail/skip/xfail counts** — PASS. Final: `79 failed, 2714 passed, 247 skipped, 1 xfailed, 62 errors` (from `82 failed, 2711 passed` before the aiohttp fix).
- **Classify every failure as regression (fix) or pre-existing (xfail-document)** — PASS. All 144 original failures/errors classified into 9 root-cause groups in the report; each verified against the pristine reorg base (byte-diff and/or direct re-run in a scratch worktree, now removed). 2 genuine regressions fixed (test relocation, aiohttp dependency); remaining 141 (79 failed + 62 errors post-fix) documented as pre-existing/environmental.
- **Relocate re-landed LP-solver client tests into tests/modeling/, treat their flat-path failure as expected** — PASS. Done exactly as specified; both files now pass (`11 passed, 1 skipped`, the skip being the opt-in live round-trip test).
- **Write agent-io/reports/full-suite-verification.md with counts, install outcome, fixed regressions (with SHAs), documented xfails** — PASS. See file for full detail.
- **Every failure either FIXED or documented as pre-existing/env-gap xfail** — PASS.
- **No unaddressed regression from LP-solver delta or deprecation shim** — PASS, to the best of my verification. I traced every failing test file to a root cause and checked source/test identity against the pristine base rather than assuming; I'm confident in this classification but it rests on the assumption that `vibhav/feature/reorg-api-mcp-explore` @ `e23caa5` is in fact the correct "pristine reorg base" referenced by the task (it matched the task's own named examples — "notebook/session.py relative-import bug" — exactly, which corroborates this).
- **Commit fixes + report on branch full-suite-verification-local** — PASS. 4 commits, working tree clean.

# tests_run

- `pytest -q --no-header` (initial partial runs while installing dependencies) — see report for install-outcome iteration.
- `pytest -q --no-header -ra` (full suite, before fixes) — `82 failed, 2711 passed, 247 skipped, 1 xfailed, 261 warnings, 62 errors in 142.76s`.
- `pytest -q tests/modeling/test_ms_remote_solve_utils.py tests/modeling/test_ms_remote_solver_utils.py` (after relocation fix) — `11 passed, 1 skipped`.
- `pytest -q tests/core/test_deprecation_shims.py tests/guard/test_silent_none_reexports.py` (after aiohttp fix) — `84 passed`.
- `pytest -q --no-header -ra` (full suite, after both fixes) — `79 failed, 2714 passed, 247 skipped, 1 xfailed, 261 warnings, 62 errors in 124.23s`.
- `ruff check tests/modeling/test_ms_remote_solve_utils.py tests/modeling/test_ms_remote_solver_utils.py pyproject.toml` — `All checks passed!`
- Comparative runs against a scratch worktree of the pristine reorg base (`vibhav/feature/reorg-api-mcp-explore` @ `e23caa5`, `/tmp/kbutillib-reorg-base-check`, removed after use) for: `tests/notebook/` (vector_store/experiment_store/manifest/schema/validate_entities/session/catalog/serializers/helpers) — `14 failed, 114 passed, 16 errors`, matching this branch's notebook failures exactly; `tests/researchos/` — `7 failed, 59 passed, 15 errors`, matching exactly; `tests/cli/test_task_a_venv_doctor.py tests/cli/test_init_notebook.py tests/core/test_composition_smoke.py` — `8 failed, 46 passed, 10 skipped, 1 xfailed, 3 errors`, matching exactly; `tests/verab/test_verab.py -k s7` — `3 failed, 16 passed, 1 skipped, 62 deselected`, matching exactly.

# caveats

- This task normally targets h100; it was run locally on primary-laptop per explicit user instruction because h100 is currently broken. The venv used (`/private/tmp/claude-501/.../scratchpad/kbutillib-fullsuite-venv`) is throwaway and will not persist past this session — a future run (whether on h100 or here) will need to reprovision it (or a durable equivalent) the same way: `pip install -e ".[all]" --group dev --group lint`, `pip install -e ".[lp_solver]"`, `pip install --group notebook --group typeguard --group xdoctest --group mypy`, plus `pip install rdkit cobra modelseedpy`.
- 9 distinct pre-existing/environmental root causes remain undocumented-as-code (by design — out of scope per the task's own classification rule) but are fully written up in `agent-io/reports/full-suite-verification.md` with exact affected test lists, so a future task that wants to actually fix any of them (e.g. the un-shimmed `kbutillib/notebook/{schema,serialization,storage,helpers}/*` duplication, or the two relative-import bugs in `domains/notebook/session.py` and `domains/cheminformatics/verab/facade.py`) has a ready-made root-cause list instead of having to re-derive it.
- The `minedatabase`/pickaxe build failure is a real macOS/Python-3.11 toolchain gap (numpy legacy-distutils vs. modern setuptools) that would need a different Python version or a conda-based install to resolve; not attempted further since it causes zero test impact.
- `machine_configs/_default.yaml` and `pyproject.toml`'s `requests_toolbelt`/`tomli-w` dependency drift (root cause #6 in the report) would be a genuinely easy, low-risk fix (add two lines to the YAML) if a future task wants to pick it up — I left it as pre-existing/documented per the task's strict scope rule (it reproduces identically on the pristine base, so it doesn't meet the "shim/LP-solver-delta regression" bar the same way the aiohttp gap did), but flagging it here since it's the cheapest of the 9 to actually close out.
- I made one judgment call worth flagging explicitly: I fixed the `aiohttp` dependency gap (root cause of `test_deprecation_shims.py`/`test_silent_none_reexports.py` failures) even though the underlying missing-declaration predates both the LP-solver delta and the shim commit, because the *tests* asserting against it are new and don't exist on the pristine base. I judged this in-scope ("regression from the deprecation shim" — observable only because of that delta's own new test surface) rather than pre-existing. This is a defensible but not unique reading of the task's classification rule; a stricter reading could argue for leaving it as a documented gap instead. I believe the fix is correct and low-risk either way.
