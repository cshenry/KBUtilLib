# KBUtilLib Modeling Preferences

```yaml
# Sentinel: set to true once you have reviewed and configured this file.
configured: false

# ---------------------------------------------------------------------------
# Execution control
# ---------------------------------------------------------------------------

# Seconds.  Any computation whose estimated runtime exceeds this threshold
# is escalated to 🟡 (sample-and-pause) rather than run freely.
# Default: 60  (runtime rubric: <5s 🟢 / 5-60s 🟡 / >60s 🔴)
execution:
  runtime_threshold_seconds: 60

  # Max number of independent sub-tasks (e.g. organisms, media, notebooks)
  # that may fan out in a single step before requiring 🟡 review.
  fanout_threshold: 5

# ---------------------------------------------------------------------------
# Sampling defaults (apply at the 🟡 tier)
# ---------------------------------------------------------------------------
sampling:
  # Number of genomes to reconstruct in a sample run.
  reconstruction_n: 1

  # Number of media conditions to gapfill in a sample run.
  gapfill_media_n: 1

  # max_solutions passed to gapfill in a sample run.
  gapfill_max_solutions: 1

  # Number of reactions to include in a sample FVA (sorted by |flux|,
  # descending).
  fva_reaction_n: 10

# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------
solver:
  # LP/MILP solver backend passed to COBRA.  Typical values: glpk, cplex,
  # gurobi.  Leave blank to use the COBRA default.
  name:  # unset — no code-level default; COBRA picks solver automatically

# ---------------------------------------------------------------------------
# Gapfill
# ---------------------------------------------------------------------------
gapfill:
  # If true, run_comprehensive_gapfill_on_model is used; if false, use the
  # targeted gapfill_metabolic_model call.
  comprehensive: false

# ---------------------------------------------------------------------------
# Organism defaults
# ---------------------------------------------------------------------------
organism:
  # Free-text hint used in session summaries (e.g. "Methanothrix soehngenii").
  focus:  # unset — project-specific; no code-level default

# ---------------------------------------------------------------------------
# Media defaults
# ---------------------------------------------------------------------------
media:
  # Default medium ID (KBase workspace ref or ModelSEED media ID) used when
  # no medium is specified explicitly.
  default:  # unset — project-specific; no code-level default

# ---------------------------------------------------------------------------
# Schema version (do not edit manually)
# ---------------------------------------------------------------------------
version: "1.0"
```
