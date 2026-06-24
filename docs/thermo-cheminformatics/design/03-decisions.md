# Decisions log (ADR style) -- KBUtilLib thermo + cheminformatics

Henry protocol: every design choice recorded as context / decision / consequences.

## ADR-001: Stage the work in a planning folder before touching the KBUtilLib repo
- Context: Henry's ask spans two modules wrapping 4+ external tools, with a hard
  prerequisite (thermo before cheminformatics) and an explicit instruction to coordinate
  with Andrew before building thermo. Building straight into the repo before the design
  and the Andrew coordination are settled risks churn and fabricated adapters.
- Decision: capture the full design + decisions + Andrew coordination in
  /scratch/vsetlur/kbutillib-cheminformatics first. Code lands in KBUtilLib as a feature
  branch + PR (same flow as PR #44) once the design is confirmed and Andrew has answered.
- Consequences: short up-front planning step; no half-built code on the repo; reviewable
  design; clean separation between "what we can build now" and "what is blocked on Andrew."

## ADR-002: thermo as a SUBPACKAGE, not a single growing file
- Context: existing thermo_utils.py is one file doing ModelSEED-DB lookups. Adding three
  heavy external-tool backends (equilibrator, dGPredictor, molGPK) to one file would make
  it unmaintainable and couple unrelated install footprints.
- Decision: create src/kbutillib/thermo/ with base.py (umbrella) + backends/ (one file per
  tool). Keep thermo_utils.py as a back-compat shim re-exporting the public names.
- Consequences: each backend isolates its own optional deps and lazy imports; partial
  installs degrade gracefully; existing imports + test_ms_biochem_deltag.py stay green.
  Slightly more files; one extra shim to maintain.

## ADR-003: Unified, backend-agnostic API with provenance + auto backend selection
- Context: Henry wants "a common ... API" so KBase agent skills call one surface regardless
  of which underlying tool answers.
- Decision: ThermoUtils exposes compound_pka / predominant_ions / formation_energy /
  reaction_energy. Each accepts multiple identifier types, dispatches via a configurable
  backend_order with graceful skip of unavailable/incapable backends, and returns which
  backend produced the result (recorded through BaseUtils.initialize_call provenance).
- Consequences: agent skills are insulated from tool churn; results are traceable to a
  backend; adding a tool later = adding a backend file + an entry in backend_order, no
  call-site changes.

## ADR-004: Keep existing ModelSEED-DB thermo logic as a backend, do not replace
- Context: current thermo_utils.py provides DB formation-energy lookups + ion-transfer
  accounting that other code may already use. Henry is ADDING predictors, not removing the
  DB path.
- Decision: move the existing logic into backends/modelseed_db.py unchanged in behavior and
  register it as the lowest-priority fallback backend. Preserve ion-transfer methods on the
  umbrella.
- Consequences: zero behavior regression; DB lookups remain the offline/no-heavy-dep path;
  predictors layer on top.

## ADR-005: Optional-dependency extras + dependency_manager for install footprint
- Context: equilibrator pulls a large data cache; dGPredictor/molGPK are git research repos;
  rdkit is heavy. KBUtilLib must still import with none of these present.
- Decision: declare [thermo] / [thermo-full] / [chem] optional-dependency extras in
  pyproject.toml for pip-installable pieces; use dependencies.yaml + the existing
  dependency_manager for the git-based tools (pinned commits, sibling-dir clone), mirroring
  the ModelSEEDpy/cobrakbase precedent. Lazy imports + is_available() probes per backend.
- Consequences: `pip install KBUtilLib[thermo]` gives the equilibrator path immediately;
  full predictor stack is opt-in; base import stays light and never fails on a missing tool.

## ADR-006: Build equilibrator backend now; defer dGPredictor + molGPK until Andrew
- Context: equilibrator-api is MIT, documented, pip-installable with a stable public API.
  dGPredictor and molGPK are research codebases whose install + entry points are only known
  to Andrew; writing their adapters blind would fabricate APIs.
- Decision: implement + validate the equilibrator backend and the umbrella now (anchors the
  API shape); leave typed stubs for dGPredictor/molGPK that raise a clear "needs Andrew
  config" error until their real adapters are written.
- Consequences: real, testable progress without fabrication; the blocked pieces are clearly
  marked; integration resumes immediately once Andrew answers (see communication/andrew-ping.md).

## ADR-006b (resolved 2026-06-24): dGPredictor + molGPK adapters written to Andrew's real repos
- Context: Andrew sent the three repos (dGPredictor master, OPAM2 = MolGpKa fine-tuned on the
  MSDB, and his ModelSEED Database fork) and said none were designed to be installable, so
  the install/inspection work was ours to do.
- Decision: both backends are subprocess-isolated (KBUtilLib never imports torch / openbabel /
  the research code). molGPK calls OPAM2's predict_pka.predict + protonate.Opam_protonate_mol
  with the modelseed .pth weights. dGPredictor was rewritten to Andrew's ModelSEED fork API
  (ModelSEEDdGPredictor.predict_reaction / predict_from_equation): reactions are addressed by
  ModelSEED rxnNNNNN accession or cpd-stoichiometry (NOT KEGG), model is
  model/modelseed_M12_model_BR.pkl. The trained pkl was missing (LFS 404 on GitHub, never
  pushed); regenerated model-only from the in-repo training matrix + decompose/group files via
  retrain_modelseed's helpers (no full retrain, no ModelSEED-DB dir needed), R2=0.9997.
- Consequences: both backends validated end-to-end through the PredictiveThermoUtils facade
  with config-driven paths. molgpk: acetic acid pKa 2.01 -> CC(=O)[O-]; glycine zwitterion.
  dgpredictor: rxn00001 -15.76+/-3.63 kJ/mol; ATP hydrolysis -25.46 kJ/mol. Repo paths +
  interpreter set via thermo.{dgpredictor,molgpk}.{repo_path,python} (or *_REPO/*_PYTHON env).
  One conda env (rdkit+torch+torch-geometric+openbabel+sklearn/scipy/pandas/joblib) serves
  both, matching Henry's "common installation footprint" mandate.

## ADR-007 (resolved 2026-06-24): pickaxe = wrap the Tyo-NU MINE-Database
- Context: Henry said "implementing the pickaxe tool in python"; the canonical pickaxe
  (minedatabase) is already python/rdkit. It was ambiguous whether he wanted a wrapper or a
  dependency-light reimplementation, and which fork. Andrew (relaying the Tyo lab) confirmed
  the version to use: github.com/tyo-nu/MINE-Database.
- Decision: WRAP the Tyo-NU minedatabase package (not reimplement). The PyPI wheel ships code
  only, so we point the PickaxeBackend at the cloned repo's bundled rule TSVs via
  cheminformatics.pickaxe.data_dir (or env KBUTILLIB_PICKAXE_DATA_DIR), and make the package
  importable in the active interpreter. The repo pins python_requires <3.10 in setup.cfg but
  runs fine on 3.11; rather than edit Andrew's repo, we expose it via a site-packages .pth so
  `pip install -e` is not needed and their setup.cfg is untouched. Runtime deps added to the
  shared env: python-libsbml, lxml, pymongo (rdkit already present).
- Consequences: real Tyo pickaxe expansion works through the kbu.chem facade
  (NetworkExpansionUtils.expand). Validated: D-glucose, 1 generation, metacyc_generalized ->
  901 new compounds / 1696 new reactions. retrorules remains the secondary backend (needs a
  RetroRules dump TSV; optional). A dependency-light reimplementation was NOT pursued — the
  Tyo package is the lab-blessed, validated implementation, so wrapping it is correct and
  avoids drift from their rule semantics.
