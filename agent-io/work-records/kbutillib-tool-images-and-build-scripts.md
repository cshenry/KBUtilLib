# kbutillib-tool-images-and-build-scripts

## task_id
kbutillib-tool-images-and-build-scripts

## branch
maestro/developer/kbutillib-tool-images-and-build-scripts

## commit_shas
- aff8e10 -- feat(docker,kofamscan): add Bakta/KOfamScan images + KEGG bridge-table and guard-set builders

## summary

Authors the two Docker images and two generator scripts scoped to this task
by PRD `kbdl-local-bakta-kofamscan-v1` (D6, D6.1, D6.2, D10): `docker/bakta/Dockerfile`
and `docker/kofamscan/Dockerfile` (siblings of the existing `docker/prokka/`
and `docker/transyt/`, each carrying a provenance header naming
`Fxe/bioseed_tools@a9c22ca` and defining no worker entrypoint), plus
`build_ko_function_map.py` (composes the KOFAMSCAN reaction-bridge table
from a KEGG `ko` release + `ko_list`, with an explicit fail-loud guard
against the release-schema trap) and `build_guard_kofam_profiles.py`
(builds the small `<name>/` + `<name>.txt` guard profile set for the fast
KOFAMSCAN real-dependency guard). No existing Python module was touched, no
`bakta_utils.py`/`kofamscan_utils.py` were created, and the annotation
package's `__init__.py` was not edited, per the scope fence for the
sibling task running in parallel on this same repo/base.

## files_touched

- `docker/bakta/Dockerfile` (new)
- `docker/kofamscan/Dockerfile` (new)
- `src/kbutillib/domains/genome/annotation/build_ko_function_map.py` (new)
- `src/kbutillib/domains/genome/annotation/build_guard_kofam_profiles.py` (new)
- `.gitignore` (added generated-artifact patterns for both scripts' output,
  plus a `.venv-task/` ignore for the throwaway dev venv this task created)

## success_criteria_check

Restated from the envelope's "SUCCESS CRITERIA" section:

1. **`docker/bakta/Dockerfile` and `docker/kofamscan/Dockerfile` exist, each
   with a provenance header naming `Fxe/bioseed_tools@a9c22ca`, and neither
   defines a worker entrypoint.** PASS. Both files exist; both headers name
   the repo and commit explicitly; neither file contains an `ENTRYPOINT`
   instruction of any kind (grep-verified: only `FROM`, `LABEL`, `RUN`,
   `ENV`, `WORKDIR`, `CMD`), so no worker script (upstream's
   `/worker/run_worker.sh`) can run unoverridden. **Caveat** (see below):
   these are not byte-for-byte ports of the literal upstream Dockerfile,
   which this task could not read.

2. **`build_ko_function_map.py` and the guard-profile-set generator are
   committed, their outputs are gitignored and absent from the tree.**
   PASS. Both scripts are committed at
   `src/kbutillib/domains/genome/annotation/`. `.gitignore` now has
   `ko_function_map*.tsv`, `/kofam-guard/`, and `/guard.txt` entries, and
   both module docstrings state prominently that their output must never
   be committed and why (KEGG redistribution terms). No generated artifact
   is present in the tree (`git status --porcelain` is clean; the smoke-test
   runs below were done under `/private/tmp/.../scratchpad`, entirely
   outside the repo, and deleted afterward).

3. **The KEGG parser fails loudly naming the detected layout when a release
   yields no symbols, rather than emitting an empty table.** PASS,
   demonstrated. `build_map()` raises `RuntimeError` naming the detected
   layout (`"NAME-field (KEGG <=90.1-style)"` in the test case, since no
   `SYMBOL` field was present) when zero non-empty symbols result. Also
   verified the two positive worked examples from the PRD reproduce
   exactly: `K00003` (new/109.1-style `SYMBOL` field) composes to
   `"hom; homoserine dehydrogenase"`, and `K00001` (old/90.1-style, symbol
   living in `NAME`) composes to `"e1.1.1.1, adh; alcohol dehydrogenase"`
   -- both match the PRD's D6 worked example and D6.4 failure example
   verbatim.

4. **No test that passed on the base commit fails on the branch.** PASS.
   Ran the exact baseline command; result was `1 failed, 2759 passed, 379
   skipped, 6 errors in 270.69s` -- identical counts to the stated baseline,
   and the failed test plus all 6 errors are exactly the pre-declared
   `failed_node_ids` (none of my changes are exercised by the existing
   suite, since nothing imports the two new scripts yet -- they are
   standalone CLI modules with no importers in this task's scope).

## tests_run

- `python -m pytest tests/ -q --continue-on-collection-errors` (repo's
  documented baseline command, run verbatim, 270.69s) --
  `1 failed, 2759 passed, 379 skipped, 6 errors` -- matches the stated
  baseline exactly; no regression.
- `ruff check` on both new scripts -- clean.
- `python -m py_compile` on both new scripts -- clean.
- Manual smoke tests of both scripts against small synthetic fixtures
  (built and deleted under the scratchpad, never committed):
  - `build_ko_function_map.py` against a synthetic 109.1-style `ko` file
    (has a `SYMBOL` field) and a synthetic 90.1-style `ko` file (no
    `SYMBOL` field, symbol lives in `NAME`) both paired with a synthetic
    `ko_list` -- both reproduced the PRD's worked/failure examples
    verbatim (see criterion 3 above).
  - Same script against a synthetic `ko` file with neither `SYMBOL` nor
    `NAME` populated -- raised `RuntimeError` naming the detected layout,
    as required.
  - `build_guard_kofam_profiles.py` against a synthetic 3-row `ko_list`
    (two ribosomal-protein rows, one unrelated hypothetical-protein row)
    and matching stub `.hmm` files -- correctly selected only the two
    matching rows, copied only their `.hmm` files into `<output>/guard/`,
    and wrote a `guard.txt` slice preserving the original header and raw
    row text.
  - Same script against a `ko_list` with zero matching rows -- raised
    `RuntimeError` rather than silently writing an empty guard set.
  - No Docker build or run was attempted, per explicit instruction in the
    dispatch envelope (image build/verification is a separate, later,
    host-side task).

## caveats

- **Dockerfiles are reconstructions, not verified ports.** This task ran
  on primary-laptop, which has no network route to poplar or to
  `github.com/Fxe/bioseed_tools`, so I could not read or diff against the
  literal upstream Dockerfile text. Each Dockerfile's header says this
  explicitly and names exactly which measured facts (from
  `agent-io/research/2026-08-23-filipe-bakta-kofamscan-recon.md` in
  KBDLJobRunningPrototype, quoted inline in the dispatch envelope) it was
  reconstructed from: `bakta_proteins` as the entry point, `/opt/conda/bin`
  needed for AMRFinderPlus, Bakta 1.11.4 / DB major 6, KOfamScan 1.3.0 with
  HMMER 3.4. I chose `condaforge/miniforge3:24.9.2-0` as the base image and
  installed both tools from bioconda/conda-forge into the base conda env
  (landing on `/opt/conda/bin` directly, matching the stated PATH
  requirement) -- this is a reasonable, standard way to get these tools
  running, but it is *my* choice of base image and install mechanism, not
  a copy of Filipe's actual recipe. **Whoever next has poplar access should
  diff these files against the literal upstream Dockerfile and correct any
  drift**, as both files say in their own headers. Neither image was built
  or run (explicitly out of scope for this task).

- **The guard-profile-set default selection is unverified against a real
  KOfamScan run.** "Chosen to actually hit E. coli" is an empirical claim
  I cannot verify without running KOfamScan against a real E. coli genome,
  which this task's environment cannot do. Rather than hard-coding a fixed
  list of ~50 KO ids from memory (which would risk silently wrong or
  nonexistent K-numbers presented with false precision), I designed
  `build_guard_kofam_profiles.py` to select KOs by matching a curated list
  of ~90 case-insensitive text-fragment patterns (ribosomal proteins,
  RNA/DNA polymerase subunits, DNA gyrase, translation factors,
  chaperonins, central-metabolism enzymes, aminoacyl-tRNA synthetases --
  all single-copy, essential, near-universal bacterial gene families)
  against whatever `ko_list` the script is actually pointed at on the
  host, at generation time. This is self-correcting across KEGG releases
  (it can never select a KO id that doesn't exist in the given release) and
  transparent (unmatched patterns are logged), but it does **not**
  guarantee every selected KO actually scores above KOfamScan's
  significance threshold against E. coli protein sequences -- that
  requires an actual run, which is out of scope here and should happen as
  part of the later host-side image-build/verification task. This is
  flagged prominently in the script's own docstring as well as here.

- **Neither the two Dockerfiles nor the two scripts were exercised inside
  a container or against a real KEGG/KOfam dataset** (only against
  hand-built synthetic fixtures matching the documented file formats).
  Full verification -- Docker build, `bakta_proteins --version`,
  `exec_annotation -h`, and running both generator scripts against the
  real registered KOfam/KEGG data on poplar -- is explicitly reserved for
  the separate, later, compute-host task per the dispatch envelope.

- `docker/kofamscan/Dockerfile`'s sanity-check layer uses
  `exec_annotation -h` rather than `--version`, because `exec_annotation`
  (per general knowledge of the tool's CLI, not verified against this
  specific image) has no dedicated version flag; this is noted inline in
  the Dockerfile's own comment.
