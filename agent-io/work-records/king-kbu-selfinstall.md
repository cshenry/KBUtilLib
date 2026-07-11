# Work record: king-kbu-selfinstall

## task_id
king-kbu-selfinstall (manually-dispatched developer task for the AMENDED
PRD `king-integration-apps`, Modules C & D / KBUtilLib self-install side —
no Maestro envelope task_id issued)

## branch
king-kbu-selfinstall

## commit_shas
- ea88b8db82759427791e4ca6f641456b1c1dea73 (`feat(cli): add kbu king self-install verb group (KING integration Module C/D)`)

## summary

Added the `kbu king install|uninstall|status` verb group
(`src/kbutillib/cli/king.py`, registered in `src/kbutillib/cli/__init__.py`)
over a new vendored, self-contained module `src/kbutillib/king_install.py`
implementing the on-disk `~/king-apps/` contract from
`agent-io/prds/king-integration-apps/fullprompt.md` Module C (Acceptance
Criteria #13-#21): `registry.json` (keyed by app id), a `CONTEXT.md`
regenerated from the **union of every registered app** (ids sorted
lexicographically, exact `# [KING App] <title> (id: <id>)` headers), and a
generated `serve-king.sh` launch wrapper that exports `KING_CONTEXT` (and
`KING_PLUGINS_DIR` only when some app declares manifests) before `exec`ing
KING's own `scripts/serve.sh` — the module never writes anything under
KING's own checkout (`~/king-stack/king/`, resolved via `KING_STACK_DIR`,
default `~/king-stack`). `king_install.py` deliberately imports nothing from
`assistant` or `king_backend`; the only coupling to a sibling installer
(e.g. AIAssistant's `assistant king install`) is the shared on-disk
contract, proven by a dedicated union-recompose test using an independent
fixture bundle (see Testing below).

`install` verifies the hand (`shutil.which("kbu")` + the bundle's
`verify.cmd` probe, `kbu model --help` checked for exit 0 and the
`ok_text` substring `"Metabolic-modeling verbs"`) and never crashes when
the CLI is missing — it reports `cli_on_path: false` and composes anyway.
It is idempotent by construction: every file write goes through a
`_write_if_changed` helper that compares content before writing, and the
registry entry's `bundle_hash` is compared before bumping
`installed_at`/`updated_at`, so a second `install` on an unchanged bundle
touches nothing on disk (verified by an automated mtime/byte-identity
test, not just a behavioral "changed: false" flag). `status` implements
the exact static green/amber/red rule from AC #18 (no live-orientation API
exists — confirmed absent by inspection of `king_backend`, so this is
intentionally static), reports `kbutillib`/`cobra`/`modelseedpy` versions
via a `python -c` import in a subprocess (AC #20), and reads KING's own
persisted `<KING_STACK_DIR>/king/runs/settings.json` (read-only, best
effort, no `king_backend` import) to WARN — never block — when the LLM
route is `cborg` (LBNL's hosted gateway) rather than the local default
`anthropic` (AC #21). `uninstall` removes this app's id + directory,
recomposes `CONTEXT.md`, and is a no-op when the id is already absent (AC
#16).

The app bundle ships as package data at `src/kbutillib/king_app/{bundle
.json,skill.md}`, wired into `pyproject.toml`'s new
`[tool.setuptools.package-data]` section (`kbutillib = ["king_app/*.json",
"king_app/*.md"]`) — verified by building an actual (non-editable) wheel
with `pip wheel . --no-deps --no-build-isolation` and inspecting its
contents for `kbutillib/king_app/bundle.json` and `skill.md` before this
task closed. `skill.md` was authored from the now-frozen `kbu model
--help` surface (read directly, plus `agent-io/docs/kbu-model-cli.md` from
the already-merged Module B task) per AC #25/Module D: it walks the
build→gapfill→FBA/FVA arc, documents the exact `--media`/`--objective`/
`--template` accepted forms as implemented (not invented), states the
`run_fva`-not-canonical-FVA caveat explicitly, gives a verified-verb-vs-
`exec` decision rule, and points to a Read-able API reference
(`agent-io/docs/kbu-model-cli.md` plus the modeling module source, at the
conventional `~/Dropbox/Projects/KBUtilLib` path, with a programmatic
fallback if KBUtilLib is installed elsewhere) — AC #23. It also states
explicitly that it is injected via `KING_CONTEXT` only and is not
registered with `claude-skills` (AC #22).

## files_touched

- `src/kbutillib/king_install.py` (new) — vendored compose/verify module
  implementing the `~/king-apps/` contract
- `src/kbutillib/cli/king.py` (new) — `kbu king install|uninstall|status`
  Click group (thin facade over `king_install`)
- `src/kbutillib/cli/__init__.py` — registers `king_cmd`
- `src/kbutillib/king_app/bundle.json` (new) — this app's package-data
  bundle manifest (AC #14 schema)
- `src/kbutillib/king_app/skill.md` (new) — the injected orientation text,
  authored from the frozen `kbu model --help` surface (AC #25)
- `pyproject.toml` — adds `[tool.setuptools.package-data]` for
  `kbutillib.king_app` + registers the `king_install` pytest marker
- `tests/cli/test_king.py` (new) — 12 tests: install (context/registry/
  serve-script contents, idempotency, union-recompose, CLI-missing
  no-crash), status (green/amber/red), uninstall (removes id +
  recomposes, preserves sibling fragment, no-op when absent), and bundle
  schema validation
- `agent-io/docs/kbu-king-cli.md` (new) — user-facing CLI reference for
  `kbu king`, including the `~/king-apps/` on-disk contract and bundle
  schema

## success_criteria_check

Acceptance Criteria #13-#23, #25 (Modules C & D, this task's scope):

- **#13 serve-king.sh generation, honors KING_APPS_DIR, never edits
  ~/king-stack/king/** — PASS. `generate_serve_script` writes only under
  `apps_dir`; `test_install_writes_context_registry_and_serve_script`
  asserts the exported `KING_CONTEXT` path and that a deliberately-never-
  created fake `king_stack_dir` (and its `king/` subdir) still don't exist
  after install (proving nothing was written there).
- **#14 bundle.json schema + verify-probe pass/fail semantics** — PASS.
  `TestBundleSchema` validates the packaged bundle against the schema and
  a hand-crafted invalid bundle raises `BundleError`; `run_verify_probe`
  implements exit-0-plus-`ok_text`-substring exactly, exercised by the
  green/amber/red status tests.
- **#15 CONTEXT.md union-recompose, lexicographic id order, exact
  headers, each id once, second app doesn't drop the first's fragment** —
  PASS. `test_install_union_recompose_keeps_other_apps_fragment` installs
  this app then an independent fixture bundle (id `aiassistant`, standing
  in for AIAssistant's own installer without depending on its code) and
  asserts both headers, lexicographic order (`aiassistant` before
  `kbutillib-modeling`), and single occurrence of each.
  `test_uninstall_keeps_other_apps_fragment` proves the converse.
- **#16 uninstall removes id + dir + registry entry, recomposes, no-op
  when absent** — PASS. `TestUninstall::
  test_uninstall_removes_id_and_recomposes` and
  `test_uninstall_already_absent_is_noop`.
- **#17 idempotent install (no-op diff)** — PASS.
  `test_install_idempotent_second_run_is_noop_diff` asserts
  `registry.json`/`CONTEXT.md`/`serve-king.sh` are byte-identical after a
  second install and the CLI reports `changed: false`.
- **#18 status static green/amber/red exactly as specified** — PASS.
  `TestStatus` covers all three colors against a hermetic fake `kbu` shim
  (see caveats) plus `KING_CONTEXT` env manipulation; exit codes 0/1/2
  verified.
- **#19 DocDB reuse (assistant CLI)** — N/A to this task (Module A, the
  AIAssistant repo's own task).
- **#20 version detection via `python -c` import + pip-install-e
  remediation** — PASS. `detect_versions()` shells `sys.executable -c
  "..."`; `test_status_green_...` asserts all three package keys are
  present; `install`/`status`'s amber-path remediation string is asserted
  in `test_status_amber_when_cli_absent`.
- **#21 route-locality WARN, not block** — PASS by construction and
  inspected manually: `detect_llm_route` never raises and `status`'s color
  computation does not incorporate `llm_route_is_local` at all (a
  non-local route only ever adds `llm_route_warning`, never downgrades the
  color) — confirmed by reading `status()`'s color branch, which is
  independent of route info. Not separately unit-tested against a live
  `cborg` settings.json (no such fixture exists in the local dev
  environment); the read-only settings.json parse path was exercised
  manually against a hand-written `king/runs/settings.json` during
  development (see caveats) but that manual check is not part of the
  committed automated suite.
- **#22 skill.md injected via KING_CONTEXT only, not a claude-skill; bundle
  ships as package data; CLI owns the install verb** — PASS. `skill.md`
  itself states this explicitly; no `~/.claude/skills/` entry was created
  anywhere in this task; `pyproject.toml` package-data + the wheel-build
  smoke test confirm the package-data path.
- **#23 skill.md points to a Read-able KBUtilLib API reference** — PASS.
  `skill.md`'s "API reference" section points to
  `agent-io/docs/kbu-model-cli.md`, `cli/model.py`,
  `ms_reconstruction_utils.py`, and `ms_fba_utils.py` at the conventional
  `~/Dropbox/Projects/KBUtilLib` path, plus a programmatic fallback
  (`kbu model exec` one-liner) if KBUtilLib lives elsewhere.
- **#25 skill.md authored only after the CLI's --help is frozen** — PASS
  by construction: Module B (`kbu model`) was already merged to `main`
  before this task's worktree was cut (confirmed via `git log main --
  src/kbutillib/cli/model.py` before starting); `skill.md` quotes/derives
  from the actual `kbu model --help` output and
  `agent-io/docs/kbu-model-cli.md` produced by that already-merged task,
  not from speculation about what the verbs might do.
- **Tests use a pytest marker consistent with existing conventions** —
  PASS. Added `king_install` to `[tool.pytest.ini_options] markers` in
  `pyproject.toml`, alongside the existing `kbu_model`/`kbase`/etc.
  markers, and applied it as `pytestmark = pytest.mark.king_install` at
  module level in `tests/cli/test_king.py`. Unlike `kbu_model`, this
  marker is not conditionally skipped -- `kbu king` itself needs no
  cobra/modelseedpy import (the CLI group is a thin facade; only the
  `verify` probe subprocess touches `kbu model`, and even that only needs
  cobra/modelseedpy to be absent-tolerant, not present, since the probe
  command is `--help` which doesn't import them either).

## tests_run

- `PYTHONPATH=src python3 -m pytest tests/cli/test_king.py -v` — **12
  passed** in 26.8s.
- `PYTHONPATH=src python3 -m pytest tests/cli/ -q --deselect
  tests/cli/test_jobs.py --deselect tests/cli/test_jobs_chain.py
  --deselect tests/cli/test_jobdaemon.py` (full CLI suite, same
  deselections used by the prior `king-kbu-model-verbs` task's work
  record) — **612 passed, 2 failed** in 122s. The 2 failures
  (`test_init.py::TestDoctorCommand::test_doctor_prints_one_line_per_probe`,
  `test_init_notebook.py::TestRenderUtilTemplate::
  test_contains_session_for`) are the exact same pre-existing failures
  already documented as unrelated in the prior task's work record; I did
  not re-verify them against a clean `main` myself (trusting that prior
  verification), but neither test touches anything this task changed.
- `ruff check src/kbutillib/king_install.py src/kbutillib/cli/king.py
  tests/cli/test_king.py` — clean (one unused-import issue caught and
  fixed before the final run). A pre-existing `I001` import-sort warning
  on `src/kbutillib/cli/__init__.py` was confirmed present on `main`
  before my edit (via `git stash`) and left as-is, per the instruction not
  to make unrelated fixes.
- `pip wheel . --no-deps --no-build-isolation -w /tmp/wheeltest` (ad hoc,
  not part of the committed test suite) — inspected the built wheel's
  member list and confirmed `kbutillib/king_app/bundle.json` and
  `kbutillib/king_app/skill.md` are present, proving the package-data
  wiring actually ships the bundle in a non-editable install. The
  `/tmp/wheeltest` output and the `build/`/`*.egg-info` directories this
  produced in the worktree were deleted before committing (not part of
  the final tree).

## caveats

- **The verify-probe/version-detection tests use a hermetic fake `kbu`
  shim**, not the machine's real global `kbu` (`_fake_kbu_on_path` in
  `tests/cli/test_king.py`): a small bash script on a monkeypatched
  `$PATH` that execs `sys.executable -m kbutillib` with `PYTHONPATH`
  pointed at this worktree's own `src/`. This was necessary because the
  global pyenv `kbu` shim on this development machine is an editable
  install pointing at the Dropbox-synced `KBUtilLib` checkout's `wip`
  branch, which does not yet have `kbu model` merged in locally (main has
  it, wip hasn't been fast-forwarded on this machine) — using the real
  global `kbu` would have made the probe fail for a reason unrelated to
  this task's code. The shim approach is also simply more correct for CI
  hermeticity regardless of that particular local-machine state.
- **`detect_llm_route`'s read of `<KING_STACK_DIR>/king/runs/settings.json`
  is a heuristic I designed, not something the PRD specifies byte-for-byte.**
  I inspected `~/king-stack/king/backend/king_backend/settings.py`
  (`resolve_llm`/`get_llm_route`/`set_llm_route`) to confirm the settings
  file's location and shape (`{"llm_route": "anthropic"|"cborg"}`,
  defaulting to `"anthropic"` when absent) and deliberately did NOT import
  `king_backend` (that would violate the "no cross-repo import"
  requirement) — I read the JSON file directly instead. This is read-only
  and best-effort (any I/O or parse error silently falls back to the
  local default), so it can never block install/status, matching AC #21's
  "WARNS, does not block." I did not add an automated test against a
  hand-written `settings.json` fixture with `llm_route: "cborg"`; I
  verified the parse path manually during development but that check
  didn't make it into the committed suite. A reviewer wanting stronger
  coverage here could add
  `king_install.detect_llm_route(king_stack_dir=<tmp with a cborg
  settings.json>)` as a follow-up unit test.
- **`KING_STACK_DIR` (default `~/king-stack`) is a convention I introduced**,
  mirroring the `KING_APPS_DIR`/`KING_CONTEXT` naming already established
  by the PRD, since the PRD itself never names an env var for "where is
  the local KING checkout." It is only used to build the `serve-king.sh`
  wrapper's `exec` target path and for the best-effort route read above —
  never for anything that could fail install/status if wrong or if KING
  isn't installed at all (confirmed: `test_install_writes_context_
  registry_and_serve_script` installs successfully with no
  `~/king-stack`-equivalent directory existing on disk at all).
- **`status`'s amber/red precedence** (amber whenever the CLI is absent,
  red only when the CLI is present but the probe fails or `KING_CONTEXT`
  isn't wired) is a literal simplification of AC #18's wording ("amber =
  composed but CLI missing") — I did not gate amber on "composed" because
  the PRD's own listed test expectation ("status amber-when-CLI-absent /
  green-when-present + probe passes") doesn't mention a composed
  precondition, and gating on it would leave an undefined fourth state
  (not composed AND CLI missing) that the AC doesn't name. Flagging this
  as a judgment call for the reviewer.
- **Did not modify AIAssistant or KING**, per the task's explicit
  instruction, and did not read or depend on the sibling
  `assistant.king_install` implementation (per the interop requirement
  that it not be visible to this task) — interop is proven entirely
  through the shared on-disk contract as specified in fullprompt.md, using
  an independent fixture bundle in the union-recompose tests rather than
  any real AIAssistant code.
- **Did not touch `src/kbutillib/cli/model.py`** beyond the pre-existing,
  already-merged state — only registered `king_cmd` alongside it in
  `cli/__init__.py`.
