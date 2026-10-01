# kbu model: auto-annotate unannotated genomes, and make GLPK the default solver

`kbu model reconstruct` silently produces a useless model, and `kbu model gapfill`
cannot run at all on kbhub. Both have single, measured root causes, and Chris has
already ruled on both fixes.

## What is broken

**Reconstruct returns a 5-reaction scaffold from a 4,285-protein genome.**
`MSGenome.from_fasta()` populates `MSFeature.id`, `.seq` and `.description` but
leaves `.ontology_terms` empty. `MSBuilder` maps genes to template reactions by
reading `feature.ontology_terms["RAST"]`, so with nothing there it matches nothing
and emits only the template biomass scaffold — at **exit 0**, which is how this
went unnoticed. Measured on kbhub 2026-09-30: 4,285 features, **0** carrying any
ontology term, `search_name_to_genes` size **0**.

**Gapfill dies with "Model too large for size-limited license".** Nothing in
KBUtilLib ever sets a solver, so optlang's default preference order picks a
commercial backend first — and the gurobi in kbhub's venv is a restricted,
size-capped licence. Measured on kbhub: `cobra.Configuration().solver` resolves to
`optlang.gurobi_interface`. Miles measured on h100 that GLPK solves the same
gapfill in 28.1s (437 reactions added, post-gapfill FBA objective 1.168) against
unrestricted gurobi's 27.4s / 438 / 1.168 — so the LP is not large, the licence cap
is the whole problem, and GLPK costs nothing.

## What we are doing

1. **Auto-annotate when, and only when, the genome is unannotated.** `reconstruct`
   gains `--annotate auto|always|never` (default `auto`). `auto` measures whether
   any feature carries a RAST term and calls RAST only if none does, so genomes
   that arrive already annotated are never re-sent over the network.
2. **Never return a silent scaffold again.** If annotation is needed and RAST is
   unreachable, `reconstruct` **fails** and names the missing annotation. The
   scaffold is still reachable, but only by explicitly asking for
   `--annotate never`, and even then the low reaction/gene counts are warned about
   on stderr.
3. **GLPK becomes the default solver, configurable.** All four model verbs gain
   `--solver`, resolved as `--solver` > `KBU_SOLVER` > `~/.kbutillib/config.yaml`
   `modeling.solver` > `glpk`. h100 overrides to gurobi in its own
   `~/.kbutillib/config.yaml`, which is per-machine by construction — no hostname
   detection anywhere.
4. **Restate the offline promise honestly.** `skill.md` currently promises every
   verb "runs fully offline (no KBase token, no network)". That is about to be
   false by default, and the docs say so precisely: annotation spends the
   *no-network* property and keeps the *no-KBase-token* property.

## What this is not

Not an optlang backend for the remote LP-solver service (that service takes LP text
over HTTP; it cannot be a cobra solver without writing an optlang interface, and
GLPK makes it unnecessary). Not wiring bakta/prokka into the build path — they emit
non-RAST namespaces and would need the SSO translation step. Not annotation
caching.
