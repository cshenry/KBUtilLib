# Slack draft: Vibhav -> Andrew

Drafted in Vibhav's voice (no AI-tone, no em-dashes, first-person, direct).
This is a DRAFT for Vibhav to send. Not sent automatically.

---

Hey Andrew, Chris wants me to build a thermodynamics module into KBUtilLib that puts
equilibrator, dGPredictor, and molGPK under one install footprint and one API, so they're
available to the KBase agent skills. He pointed me to you since you've been running these in
separate modules. equilibrator I can wire myself (it's the MIT pip package), but for
dGPredictor and molGPK I need your actual setup so I don't guess at the APIs. For each of
those two, can you send me:

1. the repo and the exact commit or branch you run
2. how you install it (conda env, requirements, any vendored model files or non-pip deps
   like openbabel/rdkit version pins)
3. the function you actually call: the import path and what it takes in (SMILES/InChI/ID)
   and returns. For dGPredictor I want deltaG of formation/reaction with units, for molGPK I
   want pKa plus the predominant ion at a given pH.

A one-compound example with the expected number would be perfect so I can check my wrapper
against your output. Once I have that I can finish the module. Thanks!
