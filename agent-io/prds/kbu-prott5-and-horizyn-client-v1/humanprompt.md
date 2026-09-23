# kbu-prott5-and-horizyn-client-v1 — ProtT5 model-identity fix + `KBDLHorizyn` client

Three small changes that make the already-built `KBDLHorizyn` job type safe to
run: fix a silent model-identity bug between two merged repos, add the missing
client method, and prove the embedding path numerically against the measured
baseline.

## What changed since the first draft

The first draft of this PRD was written against a local KBUtilLib checkout that
was **116 commits behind `origin/main`**, and its central premise was false. It
proposed building `ProtT5Utils` from scratch. That module had already been
merged nine days earlier, on **2026-09-13** (`215625c`), complete with
residue-count batching, padding-safe pooling, OOM back-off, lazy imports, the
`ProtT5UtilsImpl` wrapper, lazy-loader registration, and a thorough test file.

Re-researching against `origin/main` removed roughly two thirds of the proposed
work and surfaced a defect nobody was looking for.

## The actual problem

**KBUtilLib and KBDL disagree, silently, about how the ProtT5 model is named.**

- KBUtilLib `main`: `ProtT5Utils.__init__(self, model_path=DEFAULT_MODEL_PATH, ...)`
- KBDL `main`, `job_types/horizyn.py:452`: `ProtT5Utils(model_name=model_name)`

This does not raise. `BaseUtils.__init__` ends with
`for key, value in kwargs.items(): setattr(self, key, value)`, so `model_name`
is absorbed as a dead attribute and `model_path` keeps its hardcoded default,
`Rostlab/prot_t5_xl_half_uniref50-enc`.

**The operator's configured `KBDL_PROT_T5_MODEL` is discarded with no error and
no log line.** The embeddings that come back are finite, 1024-dimensional and
entirely plausible — they are simply from the wrong model. Recall degrades and
nothing reports it.

It has not bitten yet only because `DEFAULT_PROT_T5_MODEL = None` and
`capabilities.py` refuses a `KBDLHorizyn` submission while that setting is
unset. It fires on the first real production run — the moment someone does the
ops wiring.

## The three deliverables

1. **Fix the model identity.** Correct the KBDL call site to pass `model_path=`,
   and pass the service's own `prot_t5_max_residues` through in the same change
   so the two independent truncation caps cannot drift apart. Add an explicit
   `model_name` parameter to `ProtT5Utils` whose only job is to raise a
   `TypeError` naming `model_path`, so this exact mistake cannot ship silently
   again.

2. **`KBDLHorizyn` client support.** A `JOB_TYPE_HORIZYN` constant and a
   `submit_horizyn(**params)` method in `KBDLServiceUtils`, mirroring
   `submit_skani`. (The client exposes nine job-type constants today, not five as
   the first draft said, and there is no enumerated job-type list in its
   docstrings to keep in sync.)

3. **Packaging + numeric proof.** A `prott5` optional-dependency extra carrying
   `torch`, `transformers` and `sentencepiece` — deliberately not folded into the
   `ai` extra, which is `httpx` only. Then re-run the 2026-09-21 evaluation with
   the embedding step routed through `ProtT5Utils`, pinned to `max_residues=1000`
   to match how the baseline was actually measured, and require recall@1/@5/@10
   within ±2pp of 44.6 / 64.0 / 66.3 %.

## Why the numeric check is pinned to 1000

The baseline was measured with `MAX_RESIDUES = 1000` (`run_eval.py:38`), while
both the module and the service default to 5000. Re-running at 5000 would embed
long enzymes more fully than the baseline did and change the numbers for an
entirely legitimate reason — which is indistinguishable from a real regression.
As originally written, that acceptance test could not have passed.

## What "done" looks like

The configured checkpoint is the checkpoint that loads; passing `model_name=` is
a loud `TypeError` rather than a silent wrong answer; a caller can reach the job
type through `submit_horizyn(...)` instead of a magic string; and routing the
2026-09-21 eval's proteins through `ProtT5Utils` reproduces the measured recall
within tolerance.

**Not** included: wiring `KBDL_PROT_T5_MODEL` / `KBDL_HORIZYN_CHECKPOINT` on the
live poplar deployment. That is an ops step with its own change control. It is
required for end-to-end Horizyn runs, but nothing in this PRD depends on it —
the numeric acceptance deliberately runs at library level so the build is not
blocked on an action this PRD does not own.
