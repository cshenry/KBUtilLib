# KBUtilLib cheminformatics + thermodynamics work

Working folder for the Henry-requested expansion of KBUtilLib (https://github.com/cshenry/KBUtilLib)
with (1) a thermodynamics module and (2) a cheminformatics module.

This is a PLANNING + STAGING folder, not the build target itself. The actual code lands
in the KBUtilLib repo as a feature branch + PR (same flow as PR #44, rxnsim). Nothing here
gets pushed to cshenry/KBUtilLib until the design below is confirmed and the Andrew
coordination (see communication/) is resolved.

## Why this is staged and not built straight away

Henry's ask has a hard ordering baked into it:

  "before you do that though, I think it's firstly important to establish thermodynamic
   utility modules. For this, you will want to ping Andrew."

So the dependency chain is:

  thermo module (equilibrator + dGPredictor + molGPK, Andrew's existing code)
      -> cheminformatics module (pickaxe, retrorules, Tyo-lab pickaxe, etc.)
      -> test the rxnsim tools already added in PR #44

The thermo module wraps THREE external tools Andrew has been running in separate modules.
Two of them (dGPredictor, molGPK) are research codebases with non-trivial installs and
non-obvious public APIs. Wiring them blind would mean inventing import paths and call
signatures, which is exactly the kind of fabrication to avoid. So the safe, validated
path is: design the umbrella now, wire the one tool we can verify (equilibrator-api,
MIT, pip-installable), and get the concrete integration details for the other two from
Andrew before writing their adapters.

## Contents

- design/00-overview.md          high-level plan + module layout
- design/01-thermo-module.md     thermo module design (the prerequisite)
- design/02-cheminformatics.md   cheminformatics module design (the follow-on)
- design/03-decisions.md         ADR-style decisions log (Henry protocol)
- communication/                 Slack drafts + Andrew coordination
- scratch/                       throwaway experiments, never the source of truth

## Status

- [x] Reviewed KBUtilLib architecture (module pattern, toolkit facade, external-tool wrap pattern)
- [x] Reviewed existing thermo_utils.py (ModelSEED-DB deltaG lookups only; NOT the predictors Henry wants)
- [x] Confirmed equilibrator-api: MIT, actively maintained, pip-installable
- [x] Designed the thermo umbrella API + module layout
- [ ] BLOCKED: dGPredictor + molGPK integration details (need Andrew) -- see communication/andrew-ping.md
- [ ] Build thermo module on KBUtilLib feature branch (after Andrew)
- [ ] Build cheminformatics module (pickaxe first)
- [ ] Test rxnsim tools from PR #44
