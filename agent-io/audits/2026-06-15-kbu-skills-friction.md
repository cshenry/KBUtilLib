# kbu Skill Bundle — Friction & Inconsistency Report

**Date:** 2026-06-15
**Context:** First proving-ground run of the `kbu` skill bundle (`/kbu`,
`kbu-notebook`, `kbu-fba`) inside BERIL, driven by the `energy_loop_analysis`
project. Goal was to exercise the metabolic-modeling and notebook-construction
directives end-to-end while doing a real modeling study.
**Verdict:** The run **did not reach first compute.** The bundle is blocked by
environment/installation gaps and undermined by significant template drift and
doc-vs-code inconsistencies. None of the blockers are about the science — they
are about the toolchain and the skill documentation. Details below, each with
evidence and a suggested fix, ordered by severity.

**Machine:** primary-laptop, off-cluster. **Repos:** BERIL-research-observatory
(test site), KBUtilLib at `~/Dropbox/Projects/KBUtilLib` (skill + code home).

---

## TL;DR — what must be fixed before retrying

1. **The `kbu` CLI does not run** (missing `tomli_w`) → the canonical
   scaffolder `kbu init-notebook` is unusable. (Blocker)
2. **Notebook venvs are missing `requests_toolbelt`**, a *declared* KBUtilLib
   dependency → `ms_fba_utils` and `ms_reconstruction_utils` (the two core
   kbu-fba modules) fail to import → the FBA arc cannot load. (Blocker)
3. **Two divergent `util.py` templates exist.** The one the CLI renders and the
   one the skill documents are materially different and mutually incompatible
   (the smart-merge marker exists in only one). (High)
4. **The skills never state the correct import line** for the mandated
   `NotebookSession` bootstrap, and gesture at a `kbutillib.beril` location that
   is not the import path. (High)
5. **No BERIL lifecycle integration** and **no offline/fallback story** when
   BERDL is unreachable (which it currently is, via a Cloudflare challenge). (Medium)

---

## A. Environment / installation blockers

### A1 — `kbu` CLI crashes on import: missing `tomli_w`  🔴 BLOCKER
Running `kbu --help` (via `~/bin/kbu` → `KBUtilLib-py3.13` venv) crashes:

```
File ".../kbutillib/cli/manifest.py", line 15, in <module>
    import tomli_w
ModuleNotFoundError: No module named 'tomli_w'
```

The same venv also emits import failures for `requests_toolbelt`, `cobra`,
`httpx`, and `aiohttp` on every invocation. Because the crash is at CLI module
load, **no `kbu` subcommand works** — including `kbu init-notebook` (the
canonical notebook scaffolder), `kbu doctor`, and `kbu new-project`.

- **Impact:** The entire documented scaffolding path is dead on this machine.
  An agent or user following the skills cannot create a notebook the intended way.
- **Suggested fix:** Add `tomli_w` (and audit the full CLI dependency closure)
  to the CLI's own install. The `kbu` CLI venv should be provisioned from
  KBUtilLib's declared deps, not hand-curated. Consider making `kbu doctor`
  importable even when optional util modules are absent, so it can self-diagnose.

### A2 — Notebook venv missing `requests_toolbelt` breaks the FBA arc  🔴 BLOCKER
In `~/VirtualEnvironments/kbu.nb-adp1notebooks-py3.11` (a representative
notebook venv), `kbutillib`, `cobra 0.31.1`, `modelseedpy 0.4.2`, numpy, and
pandas all import — **but**:

```
[KBUtilLib] Failed to import ms_reconstruction_utils: ModuleNotFoundError: No module named 'requests_toolbelt'
[KBUtilLib] Failed to import ms_fba_utils:            ModuleNotFoundError: No module named 'requests_toolbelt'
```

`ms_fba_utils` (`MSFBAUtils`) and `ms_reconstruction_utils`
(`MSReconstructionUtils`) are exactly the modules the kbu-fba arc is built on
(build → gapfill → FBA → FVA). The failure chains from
`kbutillib/kb_ws_utils.py:14` (`from requests_toolbelt.multipart.encoder import
MultipartEncoder`).

- **Root cause:** `requests_toolbelt >=0.10.0` **is** declared in
  `KBUtilLib/pyproject.toml:25`, yet it is absent from the venv. The editable
  install (`pip install -e ~/Dropbox/Projects/KBUtilLib`) did not resolve
  KBUtilLib's dependencies — either it was run with `--no-deps`, or the dep was
  added to `pyproject.toml` after the venv was built and never reconciled.
  `machine_configs/_default.yaml` `notebook_deps` does **not** pin
  `requests_toolbelt`, so nothing else backfills it.
- **Impact:** Even a venv that looks healthy (cobra + modelseedpy present)
  cannot load the FBA arc. This is the single most damaging gap for kbu-fba.
- **Suggested fix:** (a) Ensure notebook venv creation does a full dependency
  resolution of the editable installs (no `--no-deps`); (b) add a
  `kbu doctor` check that imports `ms_fba_utils`/`ms_reconstruction_utils` and
  flags missing transitive deps; (c) optionally pin the known-heavy transitive
  deps in `notebook_deps` as a belt-and-suspenders measure.

### A3 — Loud, alarming import-failure banner on every import  🟡 UX
Every `python -c "import kbutillib"` prints ~13 lines of
`[KBUtilLib] Failed to import <module>: ModuleNotFoundError: ...` for optional
modules (`aiohttp`, `httpx`, `requests_toolbelt`-dependent modules). In a
notebook, this noise prints on the first `%run util.py` of every cell.

- **Impact:** Hard to distinguish a real blocker (A2) from benign optional-module
  skips. Reads as "everything is broken."
- **Suggested fix:** Demote optional-module import failures to a single
  collapsed summary or a debug-level log; surface only modules the current
  workflow actually needs.

---

## B. Template drift — the two `util.py` templates disagree  🔴 HIGH

There are **two** `util.py` templates, and they have materially diverged:

| | `cli/templates/util.py.tmpl` (what `kbu init-notebook` renders) | `beril/skills/kbu-notebook/util.py.tmpl` (what the skill doc cites as canonical) |
|---|---|---|
| Placeholder | Jinja `{{ project_name }}` | literal `<project_id>` (manual edit) |
| `numpy`/`pandas` imports | **unguarded** (`import numpy as np`) | guarded in `try/except ImportError` |
| Session bootstrap | `NotebookSession.for_notebook(None, project_name=...)` (auto-detect) | `NotebookSession.for_notebook(__file__, project_name=...)` |
| Path constants | none | `NOTEBOOK_DIR`, `PROJECT_ROOT`, `DATA_DIR`, `FIGURES_DIR` |
| Generic helper imports | imports a large block from `kbutillib.notebook.helpers` and `kbutillib.notebook.schema` | none |
| Smart-merge marker `# === project-specific helpers below ===` | **present (1)** | **absent (0)** |
| `session_for()` back-compat shim | present | absent |

Consequences:

- **B1 — `--force` smart-merge will refuse on skill-template files.**
  `init_notebook.py:48` (`_smart_merge_util`) returns `None` when the marker is
  missing, and the command then raises. A user who hand-creates `util.py` from
  the **skill** template (no marker) and later runs `kbu init-notebook --force`
  gets a hard error telling them to "add the marker manually." The two officially
  shipped templates are not round-trip compatible.
- **B2 — The CLI template violates the skill's own stated invariant.**
  kbu-notebook §5 says: *"Imports that may not be installed are wrapped in
  `try/except ImportError` … so util.py loads even in a minimal environment."*
  The CLI template imports `numpy`/`pandas` **unguarded** and pulls a large
  `kbutillib.notebook.helpers` / `.schema` block — so in a minimal or partially
  broken venv (exactly our A2 situation), `%run util.py` will raise immediately.
- **B3 — `__file__` vs `None` anchoring diverge.** The skill template anchors
  `.kbcache/` to `__file__` (deterministic, cwd-independent — as the skill
  prose promises). The CLI template passes `None` and relies on runtime
  auto-detection, which is the less robust path the skill explicitly argues
  against.

- **Suggested fix:** Collapse to a **single source-of-truth template**. Decide
  whether the marker + jinja are part of it, and make the skill doc point at the
  exact file the CLI renders. If two audiences truly need two templates, generate
  one from the other and test that round-trip (create-from-skill → `init-notebook
  --force`) in CI.

---

## C. Skill-doc vs code/CLI inconsistencies  🔴 HIGH / 🟡 MED

### C1 — The correct `NotebookSession` import is never stated; `kbutillib.beril` misleads  🔴
The `/kbu` primer and kbu-notebook skill mandate
`NotebookSession.for_notebook(__file__, project_name=...)` but **never give the
import line.** The primer repeatedly references `kbutillib.beril` paths
(`src/kbutillib/beril/skills/...`), which naturally suggests
`from kbutillib.beril import NotebookSession`. That is wrong:

- `NotebookSession` is defined at `kbutillib/notebook/session.py:21` and exported
  as `from kbutillib.notebook import NotebookSession`.
- `kbutillib.beril` is a **resources** directory (it holds `skills/`), not a
  Python API surface.

I followed the doc's framing and guessed `kbutillib.beril` — it failed — and I
briefly (and wrongly) concluded `for_notebook` didn't exist. Both `for_notebook`
and the class are fine; the **doc just never names the import**, and its
`kbutillib.beril` framing actively points the wrong way. A first-time user/agent
will hit the same wall.

- **Suggested fix:** State the exact import in the skill
  (`from kbutillib.notebook import NotebookSession`). Disambiguate that
  `kbutillib.beril` is a resource path, not an importable API.

### C2 — Notebook directory layout: `notebooks/<name>/util.py` vs `notebooks/util.py`  🟡
kbu-notebook §6 documents the layout as:

```
notebooks/<notebook_name>/util.py   ← one util.py per notebook subdirectory
notebooks/<notebook_name>/.kbcache/
notebooks/<notebook_name>/<notebook_name>.ipynb
```

But `kbu init-notebook` (`init_notebook.py:232`) writes a **single**
`notebooks/util.py` for the whole project, with all `.ipynb` as siblings and one
shared `.kbcache/`. §1 says "every notebook **directory** contains exactly one
util.py," leaving "directory" ambiguous (project `notebooks/` vs per-notebook
subdir). The doc and the shipped tool describe two different filesystem shapes.

- **Impact:** `.kbcache/` location, `PROJECT_ROOT` depth (`parent.parent`), and
  cross-notebook cache sharing all depend on which shape is real. The skill
  template's own `PROJECT_ROOT = NOTEBOOK_DIR.parent.parent` comment assumes the
  nested `<name>/` shape, but the CLI produces the flat shape (where
  `parent.parent` overshoots).
- **Suggested fix:** Pick one layout, make the CLI and the skill agree, and fix
  the `PROJECT_ROOT` math to match.

### C3 — No in-repo exemplar of the new discipline  🟡
The only existing ADP1 notebook project (`acinetobacter_adp1_explorer`) predates
the kbu-notebook discipline: flat `notebooks/*.ipynb`, **no `util.py`**, no
`%run util.py`, no `.kbcache/`. So a first user has no worked example in the repo
to copy, and the closest-looking project actively models the *old* pattern.

- **Suggested fix:** Ship one tiny reference project that demonstrates the
  canonical layout end-to-end (util.py + one cell + a cached artifact).

---

## D. API / usage ambiguities in kbu-fba  🟡 MED

### D1 — Two parallel idioms for the same call, no stated preference
kbu-fba shows direct construction
(`MSFBAUtils(config_file=False, token_file=None, kbase_token_file=None)`), while
the kbu-notebook skeleton uses the session facade
(`session.kbu.fba.run_fba(...)`). `session.kbu` lazily builds
`KBUtilLib(env=SharedEnvUtils())` (`session.py:137`). Which is canonical? When
does `session.kbu` share auth/env vs. the bare constructor? Unstated.

- **Suggested fix:** Declare one canonical idiom (likely `session.kbu.*`, so env
  and token flow through one place) and show the other only as an escape hatch.

### D2 — Object-type handoff through the arc is unpinned
`build_metabolic_model` returns "model" (cobra.Model **or** MSModelUtil?);
`gapfill_metabolic_model` returns `(model_util, added_reactions)`; `run_fba`/
`run_fva` accept "MSModelUtil or cobra.Model." The exact type at each hop — and
whether you pass the model or the `MSModelUtil` wrapper into the next stage — is
left to the reader. For a skill whose whole point is a clean arc, the type
contract should be explicit.

- **Suggested fix:** Annotate each stage's input/output type and show the handoff
  explicitly (e.g., "gapfill returns `MSModelUtil`; pass it directly to
  `run_fba`").

### D3 — `MAX{bio1}` objective DSL is used before it is explained
`run_fba(objective="MAX{bio1}")` introduces a custom objective mini-language.
It's only later implied that `set_objective_from_string` parses it. The grammar
(`MAX{}`/`MIN{}`, single reaction only? linear combos?) isn't specified.

- **Suggested fix:** Document the objective-string grammar once, near first use.

---

## E. BERIL integration & operating-context concerns  🟡 MED

### E1 — No BERIL lifecycle integration
`energy_loop_analysis` has **no `beril.yaml`** (manifest-less); its README status
is the free-text "PLANNED," which only loosely maps to BERIL's `proposed`.
`kbu init-notebook` does not create or update `beril.yaml`, set `artifacts`
flags, or touch the BERIL status machine. The `/kbu` primer explicitly says it
runs "alongside" BERIL without modifying `/berdl_start` — but in practice that
means a kbu-driven project silently drifts out of BERIL's lifecycle (status,
artifacts, submit gating).

- **Suggested fix:** Have the kbu notebook flow either (a) write/refresh
  `beril.yaml` (status `active`, `artifacts.*`) or (b) document the explicit
  hand-back point to `/berdl_start` so the project re-enters the lifecycle.

### E2 — No offline / BERDL-unreachable story
kbu-fba Stage 1 begins `berdl.get_genome(genome_ref)` via `KBBERDLUtils`, and the
plan also leans on `berdl-query`/`modelseeddb` to extend the currency panel. In
this session **BERDL was unreachable**: off-cluster access to
`hub.berdl.kbase.us` is intercepted by a Cloudflare *managed challenge*
(`cf-mitigated: challenge`, HTTP 403 on the kernel WebSocket), which the
`spark_connect_remote` / `berdl-remote` clients cannot satisfy (no browser to
solve the JS challenge). Tunnels, pproxy, auth, JupyterHub server, and kernel
were all healthy; only the Cloudflare layer blocked programmatic access — and a
browser login did **not** transfer to the CLI connection.

The kbu skills assume BERDL data retrieval just works. They give no guidance on:
whether `get_genome` routes through the challenged hub or a separate KBase
service; whether a cached genome can seed the arc offline; or how to proceed when
the lakehouse is down. For a study that is otherwise pure local COBRA compute,
the single genome fetch becomes an undocumented hard dependency.

- **Suggested fix:** Document a fallback (cache a genome to `.kbcache/`, or load
  from a local GBK/FAA via `MSGenome.from_protein_sequences_hash`), and state
  which network endpoint `get_genome` actually uses so users know what a BERDL
  outage does and does not block. (The Cloudflare challenge itself is a BERDL/infra
  issue to raise with that team — it likely breaks *all* off-cluster programmatic
  access, not just kbu.)

### E3 — `preferences.md` is shipped as un-edited template text
`.claude/kbu/preferences.md` is the verbatim template — its header literally
reads *"Copy this file to `<BERIL_ROOT>/.claude/kbu/preferences.md` and edit the
values."* All project-specific keys (`organism.focus`, `media.default`,
`solver.name`) are blank. So the file both *exists* and is *unconfigured
boilerplate* — ambiguous whether it was intentionally set up. The primer says
"if the file does not exist, apply defaults," but here it exists yet still says
"copy me."

- **Suggested fix:** When the primer/CLI installs preferences, strip the
  "copy this file" header and either fill defaults or leave a clearly-marked
  "configured: false" sentinel so tooling and users can tell template from
  configured state.

---

## F. Documentation pointer / packaging notes  🟢 LOW

- **F1** — The skill cites `src/kbutillib/beril/skills/kbu-notebook/util.py.tmpl`
  as the template, but the CLI actually renders
  `src/kbutillib/cli/templates/util.py.tmpl` (see §B). The doc points at a file
  the tool doesn't use.
- **F2** — `kbu-fba` warns (correctly and usefully) to **always** use
  `MSFBAUtils.run_fva` and never `cobra.flux_variability_analysis`. This is a
  good, specific directive — keep it, and consider having `run_fva` be the only
  exported path so the broken one isn't reachable by accident.

---

## What worked (for fairness)

- The three skills loaded cleanly and the graduated-execution policy (🟢/🟡/🔴)
  is clear and sensible.
- The kbu-fba arc (build → gapfill → FBA → FVA), the `run_fva` mandate, and the
  sampling/preferences table are well-specified at the conceptual level.
- The skill `util.py.tmpl` (the skills-side one) is a good template *in
  isolation* — guarded imports, `__file__` anchoring, path constants.
- `NotebookSession` itself is a clean API once you find the right import; cache
  save/load is straightforward.
- The diagnosis was tractable: every blocker traced to a concrete missing dep or
  a concrete doc/template divergence, not to anything mysterious.

---

## Recommended remediation order

1. **Make `kbu` run** — add `tomli_w` and fully resolve the CLI venv (A1).
2. **Fix notebook-venv dependency resolution** so `requests_toolbelt` and other
   declared KBUtilLib deps are present; add a `kbu doctor` import check for
   `ms_fba_utils`/`ms_reconstruction_utils` (A2).
3. **Unify the `util.py` template** to one source of truth and make the skill doc
   reference the exact file the CLI renders; verify the smart-merge round-trip (B).
4. **State the `NotebookSession` import** explicitly and de-conflate
   `kbutillib.beril` (resources) from the import path (C1).
5. **Reconcile the notebook directory layout** between doc and CLI, and fix
   `PROJECT_ROOT` depth (C2).
6. **Pin the kbu-fba object-type contract and objective DSL** (D2, D3); choose a
   canonical construction idiom (D1).
7. **Define BERIL lifecycle hand-off and an offline/BERDL-down fallback** (E1, E2).
8. **Quiet the optional-import banner** and clean up `preferences.md` shipping
   (A3, E3).

After 1–4 are done, re-run this same `energy_loop_analysis` proving-ground; it is
a good end-to-end exercise of the full bundle and will confirm whether the FBA
arc, caching, and notebook discipline operate smoothly for a first-time user.

---

## Evidence index (file:line)

- `KBUtilLib/src/kbutillib/cli/manifest.py:15` — `import tomli_w` (CLI crash, A1)
- `KBUtilLib/src/kbutillib/kb_ws_utils.py:14` — `requests_toolbelt` import (A2)
- `KBUtilLib/pyproject.toml:25` — `requests_toolbelt >=0.10.0` declared (A2)
- `KBUtilLib/machine_configs/_default.yaml:8` — `notebook_deps` (no requests_toolbelt) (A2)
- `KBUtilLib/src/kbutillib/cli/templates/util.py.tmpl` — CLI-rendered template (B)
- `KBUtilLib/src/kbutillib/beril/skills/kbu-notebook/util.py.tmpl` — skill-cited template (B)
- `KBUtilLib/src/kbutillib/cli/init_notebook.py:43,232` — smart-merge + util.py write (B1, C2)
- `KBUtilLib/src/kbutillib/notebook/session.py:21,55` — `NotebookSession`, `for_notebook` (C1)
- `KBUtilLib/src/kbutillib/notebook/__init__.py` — `from kbutillib.notebook import NotebookSession` (C1)
- `projects/acinetobacter_adp1_explorer/notebooks/` — pre-discipline exemplar (C3)
- `.claude/kbu/preferences.md` — un-edited template text (E3)
