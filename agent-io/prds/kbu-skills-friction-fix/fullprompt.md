# PRD: Fix `kbu` skill-bundle friction (BERIL proving-ground audit)

## Problem Statement

The first BERIL proving-ground run of the `kbu` skill bundle (`/kbu`,
`kbu-notebook`, `kbu-fba`) — driven by the `energy_loop_analysis` project —
**never reached first compute.** The blockers were not scientific; they were
toolchain and documentation defects, captured in
`BERIL-research-observatory/agent-io/audits/2026-06-15-kbu-skills-friction.md`:

- The `kbu` CLI and notebook virtualenvs are missing **declared** KBUtilLib
  dependencies (`tomli_w`, `requests_toolbelt`), so the canonical scaffolder
  (`kbu init-notebook`) and the entire FBA arc (`ms_fba_utils`,
  `ms_reconstruction_utils`) fail to load.
- Two divergent `util.py` templates exist and are mutually incompatible.
- The skills never state the correct `NotebookSession` import, describe a
  notebook directory layout the CLI does not produce, and leave the FBA API
  contract and objective DSL unspecified.
- A first-time BERIL user must stash a KBase token in `~/.kbase/token` even
  though BERIL already holds one.

These were verified against the live KBUtilLib code at design time (see
**Implementation Decisions → Verified findings**). Two of the audit's stated
root causes were corrected: `tomli-w` and `requests_toolbelt` **are** declared
in `pyproject.toml`; the failures are **venv-reconciliation drift** (venvs built
at different times got different dependency closures), not missing declarations.

## Solution

Land five fixes in **KBUtilLib** (which owns the code, the machine config, and
the BERIL skill sources):

1. **Harden venv provisioning + diagnostics** so a notebook/CLI venv reliably
   carries the deps the FBA arc needs, and `kbu doctor` detects when it does not.
2. **Collapse the two `util.py` templates to one source of truth** with a single,
   FLAT, minimal-venv-safe layout.
3. **Correct the skill docs** so a first-time user/agent follows accurate import
   lines, a single canonical FBA idiom, a pinned type contract, and a documented
   token/offline story.
4. **Ship a worked reference exemplar** of the canonical notebook layout.
5. **Let BERIL inject the KBase token via `KB_AUTH_TOKEN`** so the token lives in
   one place.

## User Stories

1. As a first-time BERIL user, I want `kbu init-notebook` to run, so that I can
   scaffold a notebook the documented way.
2. As a modeler, I want `ms_fba_utils` / `ms_reconstruction_utils` to import in a
   freshly provisioned notebook venv, so that the build→gapfill→FBA→FVA arc loads.
3. As a modeler, I want `kbu doctor` to tell me when the FBA arc cannot import and
   which transitive dep is missing, so that I can self-diagnose before debugging.
4. As a notebook user, I want `import kbutillib` to print a single clear summary
   instead of ~13 alarming per-module failure lines, so that I can distinguish a
   real blocker from benign optional-module skips.
5. As a maintainer, I want exactly one `util.py` template, so that the CLI and the
   skill doc cannot drift apart again.
6. As a user who hand-creates `util.py` from the documented template, I want
   `kbu init-notebook --force` smart-merge to round-trip, so that re-scaffolding
   never hard-errors on a missing marker.
7. As a user in a minimal or partially broken venv, I want `%run util.py` to load
   (heavy imports guarded), so that a missing optional dep does not abort the cell.
8. As a notebook user, I want `.kbcache/` and path constants anchored to the
   notebook directory regardless of cwd, so that caching is deterministic.
9. As a reader of the skills, I want the exact `NotebookSession` import line, so
   that I do not guess `kbutillib.beril` and wrongly conclude the API is missing.
10. As a modeler, I want one canonical way to construct the FBA utilities
    (`session.kbu.*`), so that auth and env flow through a single place.
11. As a modeler, I want the FBA arc's object-type handoffs pinned (what each
    stage returns and what to pass next), so that I am not guessing types.
12. As a modeler, I want the `MAX{}` / `MIN{}` objective DSL grammar documented at
    first use, so that I know what objective strings are valid.
13. As a new user, I want one worked reference project demonstrating the canonical
    layout end-to-end, so that I have something correct to copy.
14. As a BERIL operator, I want to supply the KBase token via `KB_AUTH_TOKEN`, so
    that I do not have to stash it in `~/.kbase/token` in addition to BERIL.
15. As a kbu-driven project owner, I want the skills to state the `/berdl_start`
    hand-back point, so that my project re-enters BERIL's lifecycle.
16. As an off-cluster user when BERDL is unreachable, I want a documented offline
    fallback (load `MSGenome` from a local FAA, or seed from cache), so that pure
    local COBRA compute is not blocked by a single genome fetch.
17. As a user, I want shipped `preferences.md` to clearly indicate whether it is
    configured, so that template boilerplate is not mistaken for real config.

## Implementation Decisions

### Verified findings (design-time, against live code)

- `tomli_w` imported at `cli/manifest.py:15`; declared as `tomli-w >=1.0`
  (`pyproject.toml:27`). The CLI venv (`KBUtilLib-py3.13`) lacks it — and that
  venv's base Homebrew `python@3.13` is itself broken (`pyexpat`/libexpat symbol
  mismatch), so `pip` cannot install into it. **CLI-venv repair is a machine fix,
  out of scope.**
- `requests_toolbelt >=0.10.0` declared (`pyproject.toml:25`); absent from
  `machine_configs/_default.yaml` `notebook_deps`. Notebook venvs are built by
  **venvman** (`cli/_template_ops.py:125`), which installs `editable_installs` +
  `notebook_deps`. Drift confirmed empirically: the `adp1notebooks` venv lacked
  `requests_toolbelt` (FBA arc broken) while the `modelingloe` venv had it (arc
  OK). The `adp1notebooks` venv was repaired at design time as an immediate
  unblock; the durable fix is below.
- Two templates exist: `cli/templates/util.py.tmpl` (jinja, unguarded
  numpy/pandas, **unguarded** `kbutillib.notebook.helpers`/`.schema` block, `None`
  anchoring, smart-merge marker present, `session_for` shim) and
  `beril/skills/kbu-notebook/util.py.tmpl` (literal placeholders, guarded imports,
  `__file__` anchoring, path constants, **no** marker, **no** helpers block).
- `helpers` and `schema` are real packages under `kbutillib/notebook/` and import
  fine in a healthy venv; the defect is that the template imports them *unguarded*.
- Correct import is `from kbutillib.notebook import NotebookSession`
  (`notebook/__init__.py:12`). `kbutillib.beril` is a resources dir, not an API.
- `kbu doctor` already exists (`cli/init.py`, `cli/beril.py`) — Task A extends it.
- `SharedEnvUtils.__init__` already accepts a `token=` param that injects without
  writing files and beats file tokens (`shared_env_utils.py:34,87-94`), but
  `session.kbu` constructs `SharedEnvUtils()` with no token (`session.py:139`),
  and `load_environment_variables()` captures `KB_*`/`KBASE_*` into `_env_vars`
  **without promoting any of them into `_token_hash`** — so an env-var token does
  not authenticate today.

### Task A — Venv provisioning + diagnostics

- **`machine_configs/_default.yaml`:** add `requests_toolbelt` and `tomli-w` to
  `notebook_deps` (belt-and-suspenders so the transitive closure lands even if an
  editable install resolves no deps). Audit the rest of KBUtilLib's declared
  runtime closure and pin any other heavy transitive deps the FBA/recon arc needs.
- **`kbu doctor`:** add checks that (a) attempt `import kbutillib.ms_fba_utils`
  and `import kbutillib.ms_reconstruction_utils` and report the specific missing
  transitive dep on failure; (b) report whether the *current* interpreter is a CLI
  venv missing `tomli_w`. `doctor` must remain importable/runnable even when
  optional util modules are absent (so it can self-diagnose).
- **Import banner:** demote the per-module `"[KBUtilLib] Failed to import
  <module>: ..."` lines (emitted on every `import kbutillib`) to a single collapsed
  summary line (e.g. `"[KBUtilLib] N optional modules unavailable: a, b, c (set
  KBUTILLIB_VERBOSE_IMPORTS=1 for detail)"`) or a debug-level log. Real,
  workflow-needed import failures should still surface.

### Task B — Unify the `util.py` template (FLAT)

- **One source of truth:** `cli/templates/util.py.tmpl` (the file the CLI actually
  renders). **Delete** `beril/skills/kbu-notebook/util.py.tmpl`; the skill doc
  references the CLI-rendered file by path.
- **Template body** (decision-encoded shape):
  - jinja `{{ project_name }}` placeholder.
  - `~/.kbu-sys-paths` bootstrap block (unchanged, both templates already have it).
  - **guarded** heavy imports: numpy, pandas, cobra each in `try/except
    ImportError`.
  - `from kbutillib.notebook import NotebookSession`.
  - `session = NotebookSession.for_notebook(__file__, project_name="{{ project_name }}")`
    — **`__file__` anchoring** (deterministic, cwd-independent).
  - path constants anchored to `__file__`, **FLAT** math:
    `NOTEBOOK_DIR = Path(__file__).resolve().parent`;
    `PROJECT_ROOT = NOTEBOOK_DIR.parent`; `DATA_DIR = PROJECT_ROOT / "data"`;
    `FIGURES_DIR = PROJECT_ROOT / "figures"`.
  - smart-merge marker `# === project-specific helpers below ===` present (so
    `--force` round-trip works).
  - **NO** generic `kbutillib.notebook.helpers` / `.schema` import block.
  - drop the `session_for()` back-compat shim (new canonical template).
- **`cli/init_notebook.py`:** confirm it writes the FLAT shape
  (`notebooks/util.py`, sibling `.ipynb`, one shared `notebooks/.kbcache/`) and
  that `_smart_merge_util` finds the marker. No layout change (it already writes
  flat); ensure the rendered template matches the new file.
- **`kbu-notebook` skill (`beril/skills/kbu-notebook/`):** rewrite the layout
  section to the FLAT shape; fix the `PROJECT_ROOT` description to `parent` (one
  level), not `parent.parent`; point the "template" reference at
  `cli/templates/util.py.tmpl`.

### Task C — Skill-doc corrections (depends on B)

- **Import (C1):** in `/kbu` primer and `kbu-notebook`, state
  `from kbutillib.notebook import NotebookSession`; add a one-liner that
  `kbutillib.beril` is a *resources* path (it holds `skills/`), not an importable
  API.
- **Canonical FBA idiom (D1):** declare `session.kbu.*` (e.g.
  `session.kbu.fba.run_fba(...)`) the canonical idiom in `kbu-fba`; document bare
  `MSFBAUtils(config_file=False, ...)` only as an escape hatch and say when it
  applies (no session in scope).
- **Object-type contract (D2):** in `kbu-fba`, pin each hop:
  `build_metabolic_model` → (state exact return type); `gapfill_metabolic_model`
  → `(MSModelUtil, added_reactions)`; `run_fba` / `run_fva` accept `MSModelUtil`
  or `cobra.Model`. Show the explicit handoff (pass the `MSModelUtil` wrapper from
  gapfill straight into `run_fba`). Confirm the exact return types against the
  live `ms_fba_utils` / `ms_reconstruction_utils` source while editing.
- **Objective DSL (D3):** document the `MAX{...}` / `MIN{...}` grammar near first
  use in `kbu-fba` — single reaction vs linear combination, what
  `set_objective_from_string` accepts. Confirm grammar against the parser source.
- **Token (E doc half):** document that BERIL/users can set `KB_AUTH_TOKEN` and
  KBU will use it (precedence over files) — no `~/.kbase/token` stashing required.
- **BERIL lifecycle (E1):** document the explicit hand-back to `/berdl_start`
  after `kbu init-notebook`, so a kbu-driven project re-enters BERIL's lifecycle.
- **Offline fallback (E2):** document that when BERDL is unreachable, a genome can
  be loaded from a local GBK/FAA (`MSGenome.from_protein_sequences_hash` or
  equivalent) or seeded from `.kbcache/`; state which endpoint `get_genome` uses
  so users know what an outage blocks. Note the Cloudflare challenge is a BERDL
  infra issue, not a kbu defect.
- **`preferences.md` (E3):** when shipped/installed, strip the "Copy this file …"
  header and add a clear `configured: false` sentinel (or fill defaults), so
  tooling and users can tell template from configured state.

### Task D — Reference exemplar (depends on B)

- Create `KBUtilLib/examples/kbu_notebook_reference/` demonstrating the canonical
  FLAT layout end-to-end: `notebooks/util.py` (rendered from the unified template),
  one small `.ipynb` with a single cell that does `%run util.py`, computes a
  trivial artifact, and saves it via the session cache, plus the resulting
  `.kbcache/` artifact committed. Keep it tiny and offline (no BERDL dependency).

### Task E — KBase token injection (`KB_AUTH_TOKEN`, env wins)

- In `SharedEnvUtils` (`shared_env_utils.py`), after reading token files, promote
  `KB_AUTH_TOKEN` from the environment into `_token_hash['kbase']` **with
  precedence over** any file-sourced kbase token. (KBase SDK standard env var
  name.) Keep the existing `token=` constructor param working; env var and
  explicit param both beat files.
- Net effect: `session.kbu`, bare `MSFBAUtils(...)`, and the CLI all authenticate
  from `KB_AUTH_TOKEN` when set, with zero file stashing. BERIL exports the var
  before invoking KBU.
- Add a focused unit test (Task E owns it): with `KB_AUTH_TOKEN` set and a dummy
  `~/.kbase/token`, `SharedEnvUtils().get_token('kbase')` returns the env value;
  with the env var unset, it falls back to the file.

### Confront round 1 — folded resolutions (GPT-5 adversary, task-eeed6ea5)

These corrections supersede the audit where they conflict (verified against live
code at design time):

- **FBA object-type contract was wrong in the audit.** The real signatures:
  `build_metabolic_model(genome: MSGenome, ...) -> (current_output: dict,
  mdlutl: MSModelUtil)`; `gapfill_metabolic_model(mdlutl: MSModelUtil,
  genome: MSGenome, ...) -> (current_output, solutions, output_solution,
  output_solution_media)` — `mdlutl` is passed **in** and mutated, it is **not**
  the return (the audit's `(model_util, added_reactions)` is incorrect);
  `run_fba(model: MSModelUtil, ...)` and `run_fva(model: MSModelUtil, ...)`.
  Canonical handoff: `out, mdlutl = kbu.recon.build_metabolic_model(genome)` →
  `kbu.recon.gapfill_metabolic_model(mdlutl, genome, ...)` →
  `kbu.fba.run_fba(mdlutl, objective="MAX{bio1}")`. (Confirm property names on
  the `KBUtilLib` facade in `toolkit.py` while editing.)
- **`session.kbu.fba` exists** (`toolkit.py:138`, `@property def fba ->
  MSFBAUtilsImpl`). The canonical idiom is documentable as-is; no code change
  needed to expose it.
- **`KBBERDLUtils.get_genome` does NOT exist** — only `get_genometables_from_kbase`
  (`kb_berdl_utils.py:705`). The offline-fallback doc must use the MSGenome path
  (`from modelseedpy.core.msgenome import MSGenome`, build from a local FAA, pass
  to `build_metabolic_model`), and must not reference a `get_genome` method.
- **Objective DSL** is parsed by modelseedpy's `ObjectivePkg.build_package`
  (`set_objective_from_string` → `model.pkgmgr.getpkg("ObjectivePkg")`). Internal
  usage is single-reaction (`MAX{bio1}`); document the full grammar by reading
  ObjectivePkg at build time.
- **notebook_deps:** add only the missing transitive deps (`requests_toolbelt`,
  `tomli-w`); `cobra`/`modelseedpy`/`cobrakbase` arrive via `editable_installs`
  and must NOT be added to `notebook_deps`.
- **Exemplar cache:** committing a SQLite `.kbcache` catalog conflicts with the
  "never commit `.kbcache`" guidance; the exemplar commits only a tiny
  non-SQLite cached artifact (and the catalog DB stays ignored).
- **preferences.md** source path is `src/kbutillib/beril/skills/kbu/preferences.md`.

The precise, checkable form of every folded resolution is in **Acceptance
Criteria** below.

## Testing Decisions

Test **external behavior**, not implementation details. Per the recent
`.gitignore` gotcha, new `test_*.py` files may need `git add -f` (the repo's
`.gitignore` ignores `test_*.py`); the developer must ensure new tests are
actually tracked.

- **Task A:** a test (or `kbu doctor` self-check exercised in a test) asserting
  that `doctor` reports FBA-arc import status and names a missing dep; a check that
  `import kbutillib` emits at most one summary line for optional-module skips.
- **Task B:** render `util.py` from the unified template into a temp flat layout
  and assert: the marker is present; numpy/pandas/cobra imports are guarded; no
  `kbutillib.notebook.helpers` import; `PROJECT_ROOT` resolves to the project dir
  (one level up). Round-trip test: hand-write a `util.py` from the template, run
  the smart-merge path, assert it does not raise and preserves user helpers below
  the marker. Prior art: existing notebook/session tests under `tests/`.
- **Task E:** the env-var precedence unit test described above.
- **Tasks C, D:** doc/exemplar tasks; validation is the reviewer reading the
  skill text and confirming the exemplar renders/loads. No unit test required,
  but Task D's notebook should execute its single cell without error in a healthy
  venv.
- Existing composition smoke tests (`tests/test_composition_smoke.py`) must stay
  green across all tasks.

## Out of Scope

- CLI writing or refreshing `beril.yaml` (BERIL lifecycle is docs-only this round).
- Fixing the Cloudflare managed-challenge blocking off-cluster BERDL access (a
  BERDL/infra issue to raise with that team; affects all off-cluster programmatic
  access, not just kbu).
- Re-running the `energy_loop_analysis` proving ground (needs BERDL; a manual
  follow-up once 1–4 land and BERDL is reachable).
- `claude-skills sync` to redeploy the edited skills into BERIL and other machines
  (manual post-merge step).
- Repairing the CLI venv's broken Homebrew `python@3.13` (machine fix:
  `brew reinstall python@3.13`).
- Threading an explicit `token=` param through `NotebookSession.for_notebook`
  (the `KB_AUTH_TOKEN` env-var path makes it unnecessary; the existing
  `SharedEnvUtils(token=...)` param remains as the programmatic escape hatch).

## Further Notes

- All five tasks target the **KBUtilLib** repo only.
- Skills edited under `beril/skills/` are the canonical sources; redeploying them
  to BERIL is the manual `claude-skills sync` follow-up noted above.
- Flat layout was chosen specifically because it matches BERIL's own project
  structure; an earlier subproject layout prototyped in KBU diverged from BERIL
  and is abandoned.

## Acceptance Criteria

1. `machine_configs/_default.yaml` `notebook_deps` includes `requests_toolbelt >=0.10.0` and `tomli-w >=1.0`, plus any other KBUtilLib `pyproject.toml` runtime dependency verified absent from a freshly resolved notebook venv; `cobra`, `modelseedpy`, and `cobrakbase` are NOT added to `notebook_deps` (they arrive via `editable_installs`).
2. `kbu doctor` wraps `import kbutillib.ms_fba_utils` and `import kbutillib.ms_reconstruction_utils` in try/except: on `ModuleNotFoundError` it prints `[FAIL] fba-import: missing dependency: {e.name}`; on any other exception it prints the exception type and first message line; `kbu doctor` remains runnable when optional util modules are absent.
3. On `import kbutillib`, optional-module import failures are collected and emitted as a single stderr line of the form `[KBUtilLib] {N} optional modules unavailable: {comma-separated} (set KBUTILLIB_VERBOSE_IMPORTS=1 for detail)`; when `KBUTILLIB_VERBOSE_IMPORTS=1`, the per-module failure lines are emitted as before.
4. `cli/templates/util.py.tmpl` contains the unified body: `~/.kbu-sys-paths` bootstrap; guarded `numpy`/`pandas`/`cobra` imports (each in `try/except ImportError`); `from kbutillib.notebook import NotebookSession`; `session = NotebookSession.for_notebook(__file__, project_name="{{ project_name }}")`; flat path constants; the smart-merge marker `# === project-specific helpers below ===`; NO `kbutillib.notebook.helpers`/`.schema` import block; NO `session_for()` shim. `beril/skills/kbu-notebook/util.py.tmpl` is deleted. Any test asserting the old template body (e.g. `tests/test_beril_skill_bundle.py`) is updated to the new body.
5. The rendered `util.py` and the `kbu-notebook` skill both define `PROJECT_ROOT = NOTEBOOK_DIR.parent` (one level); no `parent.parent` remains in either.
6. `kbu init-notebook` writes the FLAT shape (`notebooks/util.py`, sibling `.ipynb`, one shared `notebooks/.kbcache/`); running `kbu init-notebook --force` against a project whose `util.py` was created from the unified template succeeds (smart-merge finds the marker) and preserves user helpers below the marker.
7. The `kbu-fba` skill documents the verified FBA contract: `build_metabolic_model(genome: MSGenome, ...) -> (current_output: dict, mdlutl: MSModelUtil)`; `gapfill_metabolic_model(mdlutl: MSModelUtil, genome: MSGenome, ...) -> (current_output, solutions, output_solution, output_solution_media)` with `mdlutl` passed in and mutated; `run_fba(model: MSModelUtil, ...)` and `run_fva(model: MSModelUtil, ...)`; and shows the explicit build→gapfill→run handoff passing `mdlutl`. Types are confirmed against live `ms_fba_utils`/`ms_reconstruction_utils` source while editing.
8. The `kbu-fba` skill documents the objective-string grammar near first use, derived from modelseedpy `ObjectivePkg.build_package` (read at build time): at minimum `MAX{<rxn_id>}` and `MIN{<rxn_id>}`, and states whether linear combinations / coefficients are supported per the parser.
9. The `kbu-notebook` skill has a "BERIL Lifecycle" section instructing the user to run `/berdl_start` from the BERIL project root after `kbu init-notebook` to re-enter BERIL's lifecycle.
10. The skills document the offline fallback using verified APIs only: `from modelseedpy.core.msgenome import MSGenome`, build a genome from a local FAA, pass it to `build_metabolic_model`; the docs explicitly state that `KBBERDLUtils.get_genome` does not exist (use the MSGenome path) and name the endpoint used by the genome-table retrieval. No reference to a `get_genome` method.
11. In `SharedEnvUtils.__init__`, after token files and env vars are loaded, if `KB_AUTH_TOKEN` is present in `os.environ` it is set into `_token_hash['kbase']` WITHOUT persisting to disk, with precedence over file-sourced tokens; `get_token('kbase')` returns the env value when set and falls back to the file value when unset. The existing `token=` constructor param continues to take precedence as before.
12. New tests are placed under `tests/` and committed with `git add -f` (the repo `.gitignore` ignores `test_*.py`); `.gitignore` is NOT modified by this PRD.
13. The Task D exemplar (`examples/kbu_notebook_reference/`) commits a tiny NON-SQLite cached artifact demonstrating session cache save/load; it does NOT commit a SQLite `.kbcache` catalog DB (which stays ignored). The "never commit `.kbcache`" guidance is reconciled (either via an `artifacts/` location or a noted one-file exemplar exception).
14. `kbu doctor` attempts `import tomli_w` on the current interpreter and, on failure, warns that the kbu CLI venv needs dependency reconciliation; no elaborate "is this the CLI venv" auto-detection is required.
15. `src/kbutillib/beril/skills/kbu/preferences.md` has the "Copy this file …" header removed and a top-level `configured: false` sentinel added.
16. Existing composition smoke tests (`tests/test_composition_smoke.py`) remain green after all tasks.
17. The `kbu doctor` FBA-import and `tomli_w` checks (criteria 2 and 14) run on Linux as well as macOS — they are NOT gated behind a macOS-only guard — because BERIL runs on Linux (h100); confirm the checks execute on a Linux interpreter.
