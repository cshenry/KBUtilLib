---
name: kbu-fba
description: >
  Metabolic-modeling arc for local COBRA/MSModelUtil FBA in BERIL projects.
  Use when: performing genome-scale metabolic reconstruction, gapfilling, FBA,
  or FVA with KBUtilLib/ModelSEEDpy on a BERIL project genome; choosing which
  functions to call; setting media or objectives; interpreting FBA/FVA output.
allowed-tools:
  - Read
  - Bash
---

# kbu-fba — Metabolic-Modeling Arc

## Overview

The canonical modeling arc is:

```
Genome → Build → Gapfill → FBA (pFBA) → FVA
```

Each stage is described below with the authoritative KBUtilLib function
signatures.  Always read preferences from `/kbu` before starting.

**Canonical idiom:** use `session.kbu.*` for all FBA and reconstruction
work.  The session toolkit (`toolkit.py`) exposes:

- `session.kbu.fba`   → `MSFBAUtils` instance (`run_fba`, `run_fva`,
  `set_media`, `set_objective_from_string`)
- `session.kbu.recon` → `MSReconstructionUtils` instance
  (`build_metabolic_model`, `gapfill_metabolic_model`)

Auth and environment (including `KB_AUTH_TOKEN`) flow through the
session automatically.  Constructing bare `MSFBAUtils(...)` or
`MSReconstructionUtils(...)` is an escape hatch for contexts where no
session is in scope (e.g. a standalone script).

## Authentication

Set the environment variable `KB_AUTH_TOKEN` to your KBase token before
starting the session.  KBUtilLib reads it automatically — no
`~/.kbase/token` file is needed.

```bash
export KB_AUTH_TOKEN="your-kbase-token-here"
```

---

## Stage 1: Obtain a genome (`MSGenome`)

### Online path (BERDL reachable)

`KBBERDLUtils` provides `get_genometables_from_kbase` for fetching
genome-table databases from KBase workspace.  **There is no
`get_genome` method on `KBBERDLUtils`** — genome objects must be
constructed from a local file or fetched via the genome-table path.

```python
from kbutillib.kb_berdl_utils import KBBERDLUtils

berdl = KBBERDLUtils(token=session.kbu.env.kbase_token)

# Download genome-table SQLite databases from a KBase workspace object.
# Endpoint: get_genometables_from_kbase (kb_berdl_utils.py:705)
result = berdl.get_genometables_from_kbase(
    ref="76990/TestDB",         # workspace ref to a GenomeDataLakeTables object
    output_path="data/dbs",
    kb_version="prod",
)
media = berdl.get_media(media_ref)
```

### Offline fallback (BERDL unreachable)

When BERDL is not reachable (e.g. off-cluster, Cloudflare challenge),
load the genome from a local protein FASTA file:

```python
from modelseedpy.core.msgenome import MSGenome

# from_fasta reads a protein FASTA file (one sequence per feature).
# Confirmed at modelseedpy/core/msgenome.py:212.
genome = MSGenome.from_fasta("path/to/genome.faa")

# Alternatively, build from a dict of {seq_id: protein_sequence}:
# genome = MSGenome.from_protein_sequences_hash({"gene1": "MKTLL...", ...})
# Note: from_protein_sequences_hash takes a dict, NOT a filename.
```

Once you have an `MSGenome`, pass it directly to `build_metabolic_model`
below.  If you have cached a prior result, load it from `.kbcache/`
instead of re-running.

---

## Stage 2: Reconstruction (Build)

**Signature** (confirmed at `ms_reconstruction_utils.py:176`):

```python
def build_metabolic_model(
    self,
    genome: MSGenome,
    genome_classifier: Any,         # MANDATORY second positional arg
    base_model=None,
    model_id=None,
    model_name=None,
    gs_template: str = "auto",      # "gp"|"gn"|"ar"|"auto"
    ...
) -> tuple:                         # (current_output: dict, mdlutl: MSModelUtil)
```

`genome_classifier` is a mandatory positional argument — always supply
it.  Returns a 2-tuple `(current_output: dict, mdlutl: MSModelUtil)`.

**Canonical call:**

```python
out, mdlutl = session.kbu.recon.build_metabolic_model(
    genome,
    genome_classifier,              # mandatory; determines template type
    gs_template="auto",
)
# mdlutl is an MSModelUtil wrapping the draft model.
```

### 🟡 Sampling default (preferences key: `sampling.reconstruction_n`)

Default: 1 genome per sample run.

---

## Stage 3: Gapfilling

**Signature** (confirmed at `ms_reconstruction_utils.py:685`):

```python
def gapfill_metabolic_model(
    self,
    mdlutl: MSModelUtil,            # MSModelUtil passed IN and mutated
    genome: MSGenome,
    media_objs: List[MSMedia],
    templates: List[...],
    core_template=None,
    source_models=None,
    additional_tests=None,
    expression_obj=None,
    atp_safe: bool = True,
    reaction_exclusion_list=None,
    objective: str = "bio1",
    minimum_objective: float = 0.01,
    gapfilling_mode: str = "Sequential",
    base_media=None,
    base_media_target_element: str = "C",
    reaction_scores=None,
) -> tuple:   # (current_output, solutions, output_solution, output_solution_media)
```

**Key contract:** `mdlutl` is passed **in** and mutated in place.  The
return is the 4-tuple `(current_output, solutions, output_solution,
output_solution_media)`, not an `(mdlutl, added_reactions)` pair.

**Canonical call — continuing from Stage 2:**

```python
gapfill_out, solutions, best_solution, best_media = \
    session.kbu.recon.gapfill_metabolic_model(
        mdlutl,                     # pass the MSModelUtil from build
        genome,
        media_objs=[media],
        templates=[template],
    )
# mdlutl is now gapfilled; pass it directly to run_fba below.
```

### Two entry points depending on the `gapfill.comprehensive` preference

#### Targeted gapfill (default: `gapfill.comprehensive = false`)

Single medium, returns the 4-tuple described above.

#### Comprehensive gapfill (`gapfill.comprehensive = true`)

```python
# Runs gapfill over a media panel and aggregates results.
result = session.kbu.recon.run_comprehensive_gapfill_on_model(
    model=mdlutl,
    media_list=[media1, media2, ...],
    max_solutions=1,
)
```

### 🟡 Sampling defaults

- `sampling.gapfill_media_n` — number of media per sample run (default 1)
- `sampling.gapfill_max_solutions` — max_solutions per gapfill call (default 1)

---

## Stage 4: FBA (pFBA)

**Signature** (confirmed at `ms_fba_utils.py:75`):

```python
def run_fba(self, model: MSModelUtil, media=None, objective=None,
            run_pfba=True) -> cobra.Solution:
```

### Objective DSL

`set_objective_from_string` and `run_fba`'s `objective` param both
accept strings parsed by modelseedpy's `ObjectivePkg.build_package`
(`objectivepkg.py:106`).  Grammar:

```
MAX{<rxn_id>}          — maximise the named reaction
MIN{<rxn_id>}          — minimise the named reaction

# Linear combinations are supported via |-separated terms, each
# optionally prefixed with a coefficient in parentheses and a
# direction (+/-) for forward/reverse variables:
MAX{(2.0)+rxn00001|(1.0)-rxn00002}
#   ↑ coefficient  ↑ direction (+ = forward var, - = reverse var)
#   omit direction to use both forward and reverse (net flux)
```

Single-reaction objectives are the common case; multi-term objectives
work but are rare.  Always quote reaction IDs exactly as they appear in
the model.

**Canonical call:**

```python
solution = session.kbu.fba.run_fba(
    mdlutl,                         # MSModelUtil from gapfill stage
    objective="MAX{bio1}",
)
# solution is a cobra.Solution; solution.objective_value is the growth rate.
```

### Setting media on the model

```python
session.kbu.fba.set_media(mdlutl, media)
```

### Setting a custom objective

```python
# Parse and apply an objective string to the model.
session.kbu.fba.set_objective_from_string(mdlutl, "MAX{rxn00001}")
```

---

## Stage 5: FVA (MANDATORY)

**Always use `session.kbu.fba.run_fva`.**
**Never call `cobra.flux_variability_analysis` directly — it is broken
in this environment and will produce incorrect or no results.**

**Signature** (confirmed at `ms_fba_utils.py:86`):

```python
def run_fva(self, model: MSModelUtil, media=None, objective=None,
            fraction_of_optimum=0.9) -> dict:
    # Returns dict: {rxn_id: {"MIN": float, "MAX": float}}
```

**Canonical call:**

```python
fva_results = session.kbu.fba.run_fva(
    mdlutl,                         # MSModelUtil (same wrapper from build/gapfill)
    media=media,                    # optional: apply this medium first
    fraction_of_optimum=1.0,        # strict at-optimum FVA
)
```

### 🟡 Sampling default (preferences key: `sampling.fva_reaction_n`)

Default: top 10 reactions by |flux| (sorted descending by
`max(|MIN|, |MAX|)`).  Pass `reaction_ids=top10_ids` to scope the run.

### Interpreting FVA output

```python
# A reaction is blocked if its flux range is within numerical tolerance of zero.
def is_blocked(rxn_id, fva_results, eps=1e-9):
    entry = fva_results.get(rxn_id, {})
    fmin = entry.get("MIN", 0.0) or 0.0
    fmax = entry.get("MAX", 0.0) or 0.0
    return max(abs(fmin), abs(fmax)) <= eps
```

---

## Complete build → gapfill → FBA handoff

```python
# Step 1: get genome (online or offline — see Stage 1 above)
genome = MSGenome.from_fasta("genome.faa")   # offline example

# Step 2: build
out, mdlutl = session.kbu.recon.build_metabolic_model(
    genome, genome_classifier, gs_template="auto"
)

# Step 3: gapfill (mutates mdlutl in place)
_, _, _, _ = session.kbu.recon.gapfill_metabolic_model(
    mdlutl, genome, media_objs=[media], templates=[template]
)

# Step 4: FBA
solution = session.kbu.fba.run_fba(mdlutl, objective="MAX{bio1}")

# Step 5: FVA
fva = session.kbu.fba.run_fva(mdlutl, fraction_of_optimum=1.0)
```

---

## Applying a constraint package to an existing model

The build → gapfill arc is not always the starting point.  A common
alternative is to load a finished model from a JSON file and apply a
thermodynamic constraint package before solving.

### Interaction pattern

```python
import cobra
from modelseedpy.core.msmodelutl import MSModelUtil

# 1. Load a finished model from a saved JSON file and wrap it.
cobra_model = cobra.io.load_json_model("my_model.json")
mdlutl = MSModelUtil.get(cobra_model)

# 2. Build the constraint package against the wrapped model.
#    FullThermoPkg adds thermodynamic free-energy constraints (TFBA).
#    Required parameter: modelseed_path pointing to your ModelSEED database.
mdlutl.pkgmgr.getpkg("FullThermoPkg").build_package({
    "modelseed_path": "path/to/ModelSEEDDatabase",
    "temperature": 298,          # K (default)
    "default_max_conc": 0.02,    # M (default)
    "default_min_conc": 1e-6,    # M (default)
})

# 3. Run FBA or FVA — the constraint package already applied in step 2
#    constrains the solve automatically.
solution = session.kbu.fba.run_fba(mdlutl, objective="MAX{bio1}")
fva     = session.kbu.fba.run_fva(mdlutl, fraction_of_optimum=1.0)
```

### In-place contract

`MSFBAUtils.run_fba` and `MSFBAUtils.run_fva` operate on the
`MSModelUtil` object **in place** — they call
`configure_fba_formulation` to set media and objective, then solve the
model in its current LP state.  They do **not** copy or reset the model
before solving.  Any constraint package (e.g. `FullThermoPkg`) built
externally on the same `mdlutl` before the call therefore survives into
the solve and the thermodynamic constraints remain active.

Key consequence: build the package once, then call `run_fba` or
`run_fva` as many times as needed — the package variables and
constraints persist across calls on the same `mdlutl`.

---

## Graduated execution policy

Applies to every stage of this arc.

### 🟢 Green — run freely (TDD)
- Estimated runtime < 5 seconds, AND
- No algorithmic uncertainty, AND
- Fan-out ≤ `execution.fanout_threshold` (default 5), AND
- No significant compute or cost intensity.

### 🟡 Yellow — sample, cache, pause, and consult

**Trigger ANY ONE of:**
- Estimated runtime ≥ 5 s and ≤ `execution.runtime_threshold_seconds`
  (default 60 s), OR
- Agent self-flags algorithmic uncertainty, OR
- Fan-out exceeds `execution.fanout_threshold`, OR
- Compute or cost intensity warrants review.

**Action:**
1. Run a sample at reduced scope using the `sampling.*` defaults.
2. Cache the sample result immediately.
3. **STOP and report findings to the user before proceeding.**

### 🔴 Red — full run only after user sign-off
- Estimated runtime > 60 s (above `execution.runtime_threshold_seconds`).

**Action:**
1. Present scope, estimated runtime, and resource summary to the user.
2. Wait for explicit sign-off.
3. The **user decides where the full run executes**.

### Runtime rubric

| Estimated wall time | Default tier |
|---------------------|--------------|
| < 5 s               | 🟢 Green     |
| 5 s – 60 s          | 🟡 Yellow    |
| > 60 s              | 🔴 Red       |

The threshold is overridable via `execution.runtime_threshold_seconds`
in `preferences.md`.

---

## Preferences quick-reference

| Key | Default | Purpose |
|-----|---------|---------|
| `execution.runtime_threshold_seconds` | 60 | 🟡/🔴 boundary |
| `execution.fanout_threshold` | 5 | Max parallel sub-tasks before 🟡 |
| `sampling.reconstruction_n` | 1 | Genomes per 🟡 sample |
| `sampling.gapfill_media_n` | 1 | Media per 🟡 gapfill sample |
| `sampling.gapfill_max_solutions` | 1 | max_solutions in 🟡 gapfill |
| `sampling.fva_reaction_n` | 10 | Reactions in 🟡 FVA sample |
| `solver.name` | (COBRA default) | LP solver backend |
| `gapfill.comprehensive` | false | Use comprehensive gapfill |
