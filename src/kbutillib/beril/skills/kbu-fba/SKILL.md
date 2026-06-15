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

---

## Stage 1: Reconstruction (Build)

```python
from kbutillib.ms_reconstruction_utils import MSReconstructionUtils

recon = MSReconstructionUtils(config_file=False, token_file=None, kbase_token_file=None)

# Build a draft metabolic model from a genome object.
# genome: MSGenome instance (from KBBERDLUtils or MSGenome.from_protein_sequences_hash)
# template: e.g. "grampos", "gramneg", "archaea" — determines template reactions.
model = recon.build_metabolic_model(
    genome=genome,
    template=template_key,  # "grampos" | "gramneg" | "archaea"
)
```

### BERDL access

```python
from kbutillib.kb_berdl_utils import KBBERDLUtils

berdl = KBBERDLUtils(token=session.kbu.env.kbase_token)
genome = berdl.get_genome(genome_ref)
media  = berdl.get_media(media_ref)
```

### 🟡 Sampling default (preferences key: `sampling.reconstruction_n`)

Default: 1 genome per sample run.

---

## Stage 2: Gapfilling

Two entry points depending on the `gapfill.comprehensive` preference:

### Targeted gapfill (default: `gapfill.comprehensive = false`)

```python
from kbutillib.ms_reconstruction_utils import MSReconstructionUtils

# Gapfill the model on a single medium.
# Returns the gapfilled MSModelUtil and the list of added reactions.
model_util, added_reactions = recon.gapfill_metabolic_model(
    model=model,          # cobra.Model or MSModelUtil
    media=media,          # MSMedia or KBase media ref
    max_solutions=1,      # sampling default from sampling.gapfill_max_solutions
)
```

### Comprehensive gapfill (`gapfill.comprehensive = true`)

```python
# Runs gapfill over a media panel and aggregates results.
result = recon.run_comprehensive_gapfill_on_model(
    model=model,
    media_list=[media1, media2, ...],
    max_solutions=1,
)
```

### 🟡 Sampling defaults

- `sampling.gapfill_media_n` — number of media per sample run (default 1)
- `sampling.gapfill_max_solutions` — max_solutions per gapfill call (default 1)

---

## Stage 3: FBA (pFBA)

```python
from kbutillib.ms_fba_utils import MSFBAUtils

fba = MSFBAUtils(config_file=False, token_file=None, kbase_token_file=None)

# Run parsimonious FBA.  Returns a cobra.Solution.
# Objective syntax: "MAX{<rxn_id>}" or "MIN{<rxn_id>}"
solution = fba.run_fba(
    model=model_util,          # MSModelUtil or cobra.Model
    objective="MAX{bio1}",     # or "MIN{<rxn_id>}"
)
```

### Setting media on the model

```python
# Apply a medium to the model before FBA.
fba.set_media(model_util, media)
```

### Setting a custom objective

```python
# Parse and apply an objective string to the model.
fba.set_objective_from_string(model_util, "MAX{rxn00001}")  # or MIN{...}
```

---

## Stage 4: FVA (MANDATORY)

**Always use `MSFBAUtils.run_fva`.**
**Never call `cobra.flux_variability_analysis` directly — it is broken
in this environment and will produce incorrect or no results.**

```python
# Run FVA.  Returns dict: {rxn_id: {"MIN": float, "MAX": float}}.
fva_results = fba.run_fva(
    model=model_util,             # MSModelUtil or cobra.Model
    media=media,                  # optional: apply this medium first
    fraction_of_optimum=1.0,      # strict at-optimum FVA
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
