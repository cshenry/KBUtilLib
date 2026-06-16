---
name: kbu-notebook
description: >
  Notebook-construction discipline for local COBRA/MSModelUtil
  metabolic-modeling notebooks in BERIL projects.
  Use when: creating or editing a metabolic-modeling Jupyter notebook,
  scaffolding a util.py, deciding how to structure cells, managing the
  .kbcache/ layer, or integrating notebook outputs into a BERIL project.
  Supersedes the generic jupyter-dev skill for metabolic-modeling work.
allowed-tools:
  - Read
  - Bash
---

# kbu-notebook — Notebook-Construction Discipline

> This skill supersedes `jupyter-dev` for all COBRA/MSModelUtil
> metabolic-modeling notebooks.  Apply it whenever scaffolding or editing
> such a notebook inside a BERIL project.

## 1. One `util.py` per notebook directory

Every notebook directory contains exactly **one** `util.py` that holds:

- All Python imports (`cobra`, `modelseedpy`, `kbutillib`, `numpy`,
  `pandas`, etc.)
- All project-level helper functions and constants
- The `NotebookSession` bootstrap:
  ```python
  session: NotebookSession = NotebookSession.for_notebook(
      __file__,
      project_name="<project_id>",
  )
  ```

Never scatter imports or helpers across individual cells.

## 2. Every cell starts with `%run util.py`

The first (and only) import statement in every cell is:

```python
%run util.py
```

This makes every cell independently re-executable: if the kernel
restarts, any cell can be run in isolation without manually re-importing
anything.  Never add `import` statements to individual cells.

## 3. Cell independence and the cache-as-you-go pattern

Every cell must be independently executable (in any order, after a kernel
restart) by following this pattern:

```
load from cache / input files
→ analyze (compute)
→ save results to cache + write output files
```

Expensive intermediate results (model objects, FVA results, gapfill
solutions, etc.) must be saved to `.kbcache/` immediately after
computation so the next cell can load from cache rather than re-running.

## 4. System-path portability via `~/.kbu-sys-paths`

`util.py` must include the sys-path bootstrap so notebooks run on any
machine (local laptop, H100, Poplar hub) without hard-coded paths.  The
bootstrap reads `~/.kbu-sys-paths` (plain text, one path per line,
`#` comments allowed) and prepends each path to `sys.path`.

```python
import sys as _sys
from pathlib import Path as _Path

def _bootstrap_sys_paths() -> None:
    user_file = _Path.home() / ".kbu-sys-paths"
    if not user_file.exists():
        return
    try:
        for raw in user_file.read_text().splitlines():
            s = raw.split("#", 1)[0].strip()
            if not s:
                continue
            expanded = str(_Path(s).expanduser())
            if expanded and expanded not in _sys.path:
                _sys.path.insert(0, expanded)
    except Exception:
        pass

_bootstrap_sys_paths()
```

## 5. Canonical `util.py` skeleton

The unified template is at `src/kbutillib/cli/templates/util.py.tmpl`
(the file rendered by `kbu init-notebook`).  A project's `util.py`
extends this with project-specific constants and helpers below the marker.

Key invariants of the skeleton:

- `__file__`-anchored paths: all path constants are resolved relative to
  `Path(__file__).resolve().parent` so the notebook works from any cwd.
- **FLAT path math:** `NOTEBOOK_DIR = Path(__file__).resolve().parent`;
  `PROJECT_ROOT = NOTEBOOK_DIR.parent` (one level up — FLAT, not nested).
- `session` is a module-level name; cells use `session.cache.save(...)`,
  `session.kbu.fba.run_fba(...)`, etc.
- Heavy imports (`numpy`, `pandas`, `cobra`) are each wrapped in
  `try/except ImportError` and set to `None` so `%run util.py` succeeds
  even in a minimal or partially broken venv.
- There is a smart-merge marker `# === project-specific helpers below ===`
  that lets `kbu init-notebook --force` update the header while preserving
  project helpers below it.

```python
from kbutillib.notebook import NotebookSession
# Note: kbutillib.beril is a resources directory (it holds skills/),
# NOT an importable API.  Always import from kbutillib.notebook.

session: NotebookSession = NotebookSession.for_notebook(
    __file__,
    project_name="<project_id>",
)

NOTEBOOK_DIR: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = NOTEBOOK_DIR.parent          # one level up (FLAT)
DATA_DIR:     Path = PROJECT_ROOT / "data"
FIGURES_DIR:  Path = PROJECT_ROOT / "figures"
```

## 6. BERIL project layout (FLAT)

`kbu init-notebook` writes the **FLAT** shape: `notebooks/util.py` sits
directly under the project root, **not** inside a per-notebook sub-dir.
All `.ipynb` files are siblings of `util.py`.  One shared `.kbcache/`
lives inside `notebooks/`.

```
<project_root>/
  notebooks/
    util.py              ← the shared util.py (rendered from cli/templates/util.py.tmpl)
    .kbcache/            ← gitignored; anchored by __file__ in util.py
    <notebook_name>.ipynb
    ...
  data/                  ← curated input data (committed / BERIL-tracked)
  figures/               ← curated output figures (committed / BERIL-tracked)
```

Path constant math (matches the template exactly):

| Constant       | Value                              |
|----------------|------------------------------------|
| `NOTEBOOK_DIR` | `Path(__file__).resolve().parent`  |
| `PROJECT_ROOT` | `NOTEBOOK_DIR.parent`              |
| `DATA_DIR`     | `PROJECT_ROOT / "data"`            |
| `FIGURES_DIR`  | `PROJECT_ROOT / "figures"`         |

### `.kbcache/` rules

- Located at `notebooks/.kbcache/` alongside `util.py`.
- Must appear in `.gitignore` (and in BERIL's backup exclusion list if
  BERIL backs up the project directory).
- Contains only derived / re-computable data.  Never commit cache files.
- Named by computation key (e.g. `model_<org_id>.json`,
  `fva_complete.json`).

### Outputs

- Curated figures: save to `../figures/` relative to the notebooks dir
  (i.e. `FIGURES_DIR`).
- Curated data exports: save to `../data/` relative to the notebooks dir
  (i.e. `DATA_DIR`).
- Both `data/` and `figures/` are committed and BERIL-tracked.

## 7a. BERIL Lifecycle

After `kbu init-notebook` completes, run `/berdl_start` from the BERIL
project root to re-enter BERIL's lifecycle.  The scaffolded notebooks/
directory is the entry point; BERIL will register the project and track
subsequent notebook outputs from there.

## 7. Graduated execution policy

Before running any computation, evaluate it against these tiers:

### 🟢 Green — run freely (TDD)
- Estimated runtime < 5 seconds, AND
- No algorithmic uncertainty (deterministic, well-tested code path), AND
- Fan-out ≤ `execution.fanout_threshold` (default 5), AND
- No significant compute or cost intensity.

Run freely.  Write / extend tests as you go (TDD).

### 🟡 Yellow — sample, cache, pause, and consult

**Trigger ANY ONE of:**
- Estimated runtime ≥ 5 s and ≤ `execution.runtime_threshold_seconds`
  (default 60 s), OR
- Agent self-flags algorithmic uncertainty (novel code path, untested
  parameter space), OR
- Fan-out exceeds `execution.fanout_threshold`, OR
- Compute or cost intensity warrants review (e.g. model build over
  multiple genomes, gapfill over many media).

**Action:**
1. Run a sample at reduced scope (see `sampling.*` preferences):
   - Reconstruction: `sampling.reconstruction_n` genomes (default 1)
   - Gapfill: `sampling.gapfill_media_n` media, `sampling.gapfill_max_solutions`
     solutions (both default 1)
   - FVA: top `sampling.fva_reaction_n` reactions by |flux| (default 10)
2. Cache the sample result immediately.
3. **STOP and report findings to the user before proceeding.**
4. Wait for explicit go-ahead before running at full scope.

### 🔴 Red — full run only after user sign-off

- Estimated runtime > 60 s (i.e. above `execution.runtime_threshold_seconds`
  even if that value is customised above 60 s), OR any computation the user
  has explicitly flagged as 🔴.

**Action:**
1. Present scope, estimated runtime, and compute/cost summary to the user.
2. Wait for explicit sign-off.
3. The **user decides where the full run executes** (local, H100, Poplar
   hub, etc.).  Do not initiate full-scope runs on the user's behalf.

### Runtime rubric

| Estimated wall time | Default tier |
|---------------------|--------------|
| < 5 s               | 🟢 Green     |
| 5 s – 60 s          | 🟡 Yellow    |
| > 60 s              | 🔴 Red       |

The threshold value is overridable via `execution.runtime_threshold_seconds`
in `preferences.md`.  The 🔴 boundary is always the higher of 60 s and the
configured threshold.
