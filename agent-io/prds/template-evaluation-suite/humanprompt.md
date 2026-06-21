# Template Evaluation Suite — original request

I'm thinking about more utility functions to support the energy loop work. This is
functionality that belongs in either `ms_fba_util` or `ms_reconstruction_util`, or a
new `ms_template_util`.

I want to develop a function designed to evaluate a template to assess the impact of
adding, removing, or changing a reaction.

First, I want a function to evaluate template quality. This function should build a full
template model, adding the gram positive and gram negative biomass reactions. Next, FVA
should be run in complete media to evaluate the number of dead and essential reactions.
Exchange fluxes should be set to zero and then FVA should be run to identify reactions
that function in a closed mode. Next, we should attempt to grow in all the Carbon-,
Nitrogen-, Sulphate-, and Phosphate- media in the KBaseMedia workspace (all the biolog
media formulations in KBase — we can and should make a local stash of this Biolog-style
simulation and offer it as a function in `ms_fba_util`). We should use MSGrowthPhenotypes
for these phenotype simulations. Lastly, in complete media AND glucose minimal media, we
should attempt to run flux through a drain flux consuming every compound in the template
one at a time. This assesses production potential. Next, in complete media, we should
attempt to run flux through a reaction producing every compound one by one (a degradation
test).

We should have separate utility functions for each of these tests, which take a model as
input.

When all this is done, we should make a report listing: all dead reactions, all essential
reactions, all forward only reactions, all reverse only reactions, all reverse(ible)
reactions (in minimal and rich media); all functional Biolog media; all producible and
consumable metabolites; all functional reactions when exchanges are set to zero.

Finally, we should have a diff function that repeats the test on the template after a
perturbation and contrasts the reports before/after the perturbation, then makes a report
linking each perturbation to all the observed changes.
