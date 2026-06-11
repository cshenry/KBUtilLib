# Comprehensive Gapfill — original ask

I want to add a function to `ms_reconstruction_utils` called
`run_comprehensive_gapfill_on_model`. We could add a related function to
`MSGapfill` as well (or make it an addon option to existing gapfill functions).

This starts by building a standard gapfill object from an input model, but then
we add `ReactionActivationPkg` constraints with no filter, adding reaction
activation for all model reactions. Then we run an FBA that maximizes all
`ReactionActivationPkg` variables to force on as many reactions as possible at
the same time in complete media. Then we change the bounds on the
`ReactionActivationPkg` to lock this solution in place. Then we run the standard
gapfilling objective minimizing gapfilled reactions. We then return this model
as the comprehensively gapfilled model.

## Design-session decisions (grilled 2026-06-11)

1. **Activation scope** — apply reaction activation to the *original model's own
   reactions only*, not the merged gapfill-candidate reactions. ("No filter" was
   reconciled to mean "all of the model's own reactions.") Candidate database
   reactions are recruited only as the *means* to unblock model reactions; they
   are never locked on.
2. **Growth constraint** — enforce `biomass >= minimum_obj` in *both* stages, so
   the comprehensively gapfilled model still grows.
3. **API location** — fold it into `MSGapfill.run_multi_gapfill` as a new
   `gapfilling_mode = "Comprehensive"`, dispatching to a dedicated
   `MSGapfill.run_comprehensive_gapfilling` helper. The KBUtilLib
   `run_comprehensive_gapfill_on_model` is a thin wrapper.
4. **Complete media** — use `KBaseMedia/Complete` via `KBaseMediaPkg`.
5. **Lock semantics** — keep `ReactionActivationPkg`'s default `max_value=0.001`;
   after Stage 1, raise the lower bound of each `fra`/`rra` variable that reached
   the cap to `0.001` (direction-preserving). Non-activated reactions stay free.
6. **RevBin is required** — without it a reaction can set `forward=reverse=0.001`
   (net flux 0) and activate both directions for free; RevBin forces genuine net
   flux. This is an inherent MILP cost, bounded by filtering to model reactions.
7. **Process** — render full PRD + validated taskplan, register to /ai-conductor.
