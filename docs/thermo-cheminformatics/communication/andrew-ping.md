# Andrew coordination

Henry was explicit: "For this, you will want to ping Andrew." Andrew has been running
equilibrator, dGPredictor, and molGPK in SEPARATE modules. We need his concrete setup to
wrap dGPredictor and molGPK without inventing their APIs. equilibrator we can do without him.

## What we specifically need from Andrew (the 3 things that unblock the build)

For dGPredictor AND molGPK each:
  1. REPO + COMMIT/BRANCH he actually runs (URL + pinned ref). For molGPK especially,
     since it is not a mainstream package.
  2. INSTALL method that works for him (clean conda env? requirements file? vendored model
     files? any non-pip deps like openbabel/ChemAxon/rdkit version pin?).
  3. The ENTRY POINT he calls: the import path + function signature that takes a compound
     (SMILES/InChI/ID) and returns the result --
       - dGPredictor: -> deltaG of formation (and ideally reaction) with units + uncertainty
       - molGPK:      -> pKa values + predominant ion / protonation state at a given pH

Nice-to-have: a tiny working example (one compound in, expected number out) so we can
validate our wrapper against his result.

## Why this matters / what is already settled (so Andrew isn't re-explaining everything)
- equilibrator-api is MIT and pip-installable; we are wiring that backend ourselves now.
- The plan is ONE KBUtilLib module (umbrella API + per-tool backends) so all three live
  under a single install footprint, exactly as Henry asked, and become available to KBase
  agent skills. Andrew's separate modules become backends behind a common API.
- We only need the install + call details for his two tools; we are not asking him to
  rewrite anything.

## Status
- [ ] Slack draft for Vibhav -> Andrew prepared (see ../README of this dir / sent to Vibhav)
- [ ] Andrew responded with repo/commit + install + entry point for dGPredictor
- [ ] Andrew responded with same for molGPK
- [ ] Backends written + validated against Andrew's example outputs
