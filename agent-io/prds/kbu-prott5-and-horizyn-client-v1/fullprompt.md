# PRD: kbu-prott5-and-horizyn-client-v1 — ProtT5 model-identity fix + `KBDLHorizyn` client

**Repos:** KBUtilLib (owner), KBDLJobRunningPrototype (two-line consumer fix) ·
**Status:** ready · **Depends on:** `kbdl-horizyn-v1` (KBDL side landed;
reaction index registered `ref:a9f38994…`).

## Research Report

### Important findings

**1. `ProtT5Utils` already exists and is merged. The draft was written against a
stale clone.**
KBUtilLib `origin/main` carries `src/kbutillib/domains/ai/prott5_utils.py`,
merged **2026-09-13** as `215625c` ("feat(ai): add ProtT5Utils protein-embedding
surface"). It already implements everything the first draft of this PRD proposed
to build: UZOB→X mapping, space-separated tokenisation, residue-count batching,
padding-excluded mean-pooling, CUDA half-precision with CPU fallback, OOM
back-off by halving, lazy `torch`/`transformers` imports, a `ProtT5UtilsImpl`
wrapper, and registration in `domains/ai/__init__.py`'s `__all__` and
`_MODULE_MAP`. `tests/external/test_prott5_utils.py` covers all of it, including
the critical padding assertion against a sentinel-valued fake encoder and a real
model test behind the `prott5_model` + `slow` markers.
The earlier draft asserted the module "does not exist on any KBUtilLib branch".
That assertion came from reading `~/projects/KBUtilLib`, which was **116 commits
behind `origin/main`**. This PRD's scope shrank accordingly.

**2. The real defect is a silent model-identity break between two merged repos.**
KBUtilLib's constructor is `ProtT5Utils(model_path=DEFAULT_MODEL_PATH, ...)`.
KBDL's adapter calls `ProtT5Utils(model_name=model_name)`
(`src/kbdl_service/job_types/horizyn.py:452`).
This **does not raise**. `BaseUtils.__init__` ends with
`for key, value in kwargs.items(): setattr(self, key, value)`, so `model_name`
is absorbed into `**kwargs`, set as a dead attribute, and `model_path` keeps its
hardcoded default `Rostlab/prot_t5_xl_half_uniref50-enc`.
The operator's configured `KBDL_PROT_T5_MODEL` is therefore **discarded with no
error, no warning, and no log line**.
It is latent rather than live only because `DEFAULT_PROT_T5_MODEL = None` and
`kbdl_service/capabilities.py` refuses a `KBDLHorizyn` submission while that
setting is unset. **The bug fires on the first real production run** — i.e. the
moment the out-of-scope ops wiring is done.
This inverts the draft's pinned decision. The draft said "the merged consumer
calls `model_name=`, so the consumer wins." With the library also merged, and
merged *first*, tested, and documented against `model_path`, the consumer is
simply wrong. See Q1.

**3. The stated numeric acceptance criterion could not have passed as written.**
The 44.6 / 64.0 / 66.3 % recall baseline was measured with `MAX_RESIDUES = 1000`
(`run_eval.py:38`). The production path caps at
`DEFAULT_PROT_T5_MAX_RESIDUES = 5000`, and `ProtT5Utils` defaults
`max_residues=5000` too. Re-running through the module at its default embeds
long enzymes more fully and would legitimately fail to reproduce the baseline —
a green-looking test that was actually measuring a different thing. See Q4.

**4. The client half of the PRD stands, but its details were wrong.**
`JOB_TYPE_HORIZYN` and `submit_horizyn` are genuinely absent — `git grep -i
horizyn` over KBUtilLib `origin/main` `src/` and `tests/` returns nothing. But
the client enumerates **nine** job-type constants and eight `submit_*` methods,
not "exactly five". Both docstring edits the draft prescribed target text that
does not exist: the module docstring enumerates HTTP *endpoints*, and
`submit_and_wait` says "(`JOB_TYPE_GENOME_ANNOTATION`, etc.)" with no list to
extend. The real work is the constant, the method, and the test.

**5. Packaging work is still needed, and the obvious home is wrong.**
No `prott5` extra exists in `pyproject.toml`. The existing `ai` extra is
`httpx[socks]` only — it is the Argo/lp-solver *client* extra, not an ML extra —
so putting `torch` there would push multi-gigabyte wheels into every `ai`
install. See Q6.

**6. The live service has never been able to run this job type.** The poplar
`kbdl-worker@` units set neither `KBDL_PROT_T5_MODEL` nor
`KBDL_HORIZYN_CHECKPOINT`, and `KBDLHorizyn` is absent from the unit's
`KBDL_JOB_CONCURRENCY` list. Since `capabilities.py` refuses the job type while
`prot_t5_model` is unset, every submission is rejected at the gate. This is why
finding 2 is latent rather than actively corrupting, and it means `KBDLHorizyn`
is built, merged, indexed and unreachable. See Q8.1 and G1.

### Links to research output

The research for this round was a local-corpus source read plus a live-service
inspection, both performed in session; there is no separate Maestro external
scan to link. A cross-family confront WAS dispatched against this document
(`task-9a3f8d46`, codex backend on h100) and its committed stall report lives on
the task branch `maestro/researcher/you-are-the-autonomous-build-con-task-9a3f8d46`
at `agent-io/confront/stall-report.md`; its adjudication is recorded in the
Revision Log. See the quality report below for what was and was not searched.

### Quality report

**What was searched, and how well.** The local corpus was read directly and
thoroughly, against `origin/main` rather than any working tree — which is what
caught finding 1. Specifically read: the merged `prott5_utils.py` in full
(constructor, `_get_device`, `_load`, `_clean_sequence`, `_make_batches`,
`_embed_batch`, `_embed_batch_with_backoff`, `embed_proteins`);
`core/base_utils.py` and `core/shared_env_utils.py` constructors (which is what
established that the mismatch is *silent* rather than a `TypeError`);
`domains/ai/__init__.py`; `domains/external/kbdl_service_utils.py` constants,
`submit_*` methods and docstrings; `pyproject.toml` extras and pytest markers;
the KBDL adapter's `_embed_proteins` and its call site; `kbdl_service/config.py`
defaults; and the 2026-09-21 eval's `run_eval.py` and measurement report.

**Confidence: high on the code facts, high on the one deployment fact that
mattered.** Every claim about code is quoted from `origin/main` of both repos
and is checkable. The deployment question — what `KBDL_PROT_T5_MODEL` is set to
on poplar, which determines whether finding 2 is currently harmless or actively
corrupting — was open when this round began and was closed by reading the
running service directly (`systemctl --user cat kbdl-worker@1.service`): it is
unset, so the defect is latent. That is recorded as Q8.1.

The remaining deployment uncertainty is narrower and is not resolvable by
reading anything: whether the `/scratch` artifacts the numeric acceptance needs
will still exist when that task runs. It is recorded as Q8.2 with the assumption
stated.

**What was NOT searched, and why.** No external literature / prior-art scan was
dispatched to Maestro. This round's work is a two-line consumer fix, a
three-line guard, a constant-plus-method mirroring eight existing siblings, and
a packaging extra — all against interfaces already pinned by merged code. There
is no design space for a literature scan to inform, so the cost was not spent.
If a future round reopens the embedding *approach* (a different PLM, a
fine-tune, a caching layer), that scan becomes worth running and has not been
done.

**Known thin spots.** The equivalence of the module's pooling (`n_res` derived
from the cleaned string) and the reference's (`attention_mask.sum() - 1`) is
argued from the ProtT5 tokenizer's one-token-per-residue behaviour rather than
demonstrated from first principles. The confront round surfaced this as worth
testing directly, and Testing Decisions now specifies an assertion for it — but
that test needs the real tokenizer, so it runs only under `PROTT5_LIVE_TESTS=1`
and a routine CI run still would not catch a tokenizer-behaviour change. G3
records what remains open.

A second thin spot is unchanged: nothing in this round exercised the real ProtT5
model. Every claim about embedding behaviour rests on reading the merged code and
on the 2026-09-21 measurement, not on a fresh run. The numeric acceptance task is
what converts that from argument to evidence, and it has not run yet.

## Revision Log

**Round 1 — 2026-09-22.** First review round, run on h100. Re-researched the
draft against `origin/main` of both repos and found the draft's premise false:
`ProtT5Utils` had been merged nine days earlier (`215625c`), so the original
tasks 1.0, 2.0 and most of 5.0 were already complete. Scope re-centred from
"build the embedding module" onto the silent `model_path`/`model_name` break
between the two merged repos (Q1), which makes the module ignore the operator's
configured checkpoint. Added the missing canonical sections (Research Report,
Revision Log, User Stories, Open Questions, Gotchas, Sources Consulted),
corrected the numeric acceptance criterion to pin `max_residues=1000` (Q4),
decoupled that acceptance from the out-of-scope ops wiring by running it at
library level (Q3), and authored `taskplan.json`.

Also closed Q8.1 by measurement rather than assumption: the live poplar worker
units set neither Horizyn env var, so the defect is confirmed latent and no
stored result is tainted — which reframed G1 from "results may be suspect" to
"land the fix before the wiring". Ran a cross-family confront (`task-9a3f8d46`,
codex on h100); kept five of its six stall points — pinning the guard's exact
message, the extras' version-bound policy, the new members' placement, the
acceptance tolerance as arithmetic rather than prose, and an explicit note that
cross-repo landing order is safe in either direction — and adopted two free-
critique items, a tokenizer-coupling test that partly closes G3 and a CPU-only
torch install route.

## Problem Statement

The `KBDLHorizyn` job type is built and merged on the KBDL service, its
ModelSEED reaction index is built (32,422 × 512) and registered, and the
KBUtilLib protein-embedding module it depends on is merged too. It is still not
safely runnable end-to-end, for three reasons.

1. **The two merged halves do not agree on how the model is named, and the
   disagreement is silent.** The KBDL adapter passes `model_name=`; KBUtilLib's
   `ProtT5Utils` accepts `model_path=`. Because `BaseUtils` absorbs unknown
   keyword arguments with `setattr`, nothing raises: the operator's configured
   `KBDL_PROT_T5_MODEL` is discarded and the hardcoded default checkpoint is
   used instead. An operator who deliberately points the service at a specific
   ProtT5 checkpoint — the normal case, since the registered reaction index was
   built against one — gets embeddings from a different model with no indication
   anything is wrong. Recall degrades silently; the vectors remain finite,
   1024-d and plausible.

2. **The KBUtilLib KBDL client cannot express the job type.** `KBDLServiceUtils`
   exposes nine job-type constants and eight `submit_*` convenience methods.
   `KBDLHorizyn` is absent from both, so a caller can only reach it by
   hand-passing the raw string `"KBDLHorizyn"` to `submit_and_wait` — no
   constant, no helper, no test.

3. **The embedding path has never been validated end to end against the
   measured baseline.** The 2026-09-21 direction eval got its recall numbers by
   embedding proteins in a standalone `/scratch` script, bypassing `ProtT5Utils`
   entirely. Nothing has yet confirmed that routing the same proteins through
   the merged module reproduces those numbers — which is the only check that
   would catch a tokenisation or pooling drift away from the distribution the
   Horizyn checkpoint was trained on.

## Solution

Make the model identity honest, give the client a first-class way to submit the
job type, and prove the library's embedding path numerically against the
measured baseline.

1. **Fix the model-identity break at the call site, and make the confusable name
   fail loudly.** `model_path` stays canonical in KBUtilLib. The KBDL adapter is
   corrected to pass `model_path=`, and — in the same change — to pass its own
   `settings.prot_t5_max_residues` through explicitly so the service's truncation
   cap and the library's can never drift apart unnoticed. `ProtT5Utils` gains an
   explicit `model_name` parameter whose only job is to raise a `TypeError`
   naming `model_path`, so the mistake that shipped here can never ship silently
   again.

2. **Add the sixth-through-tenth job type to the client.** A `JOB_TYPE_HORIZYN`
   constant and a `submit_horizyn(**params)` method mirroring `submit_skani`,
   with a test copied from the `submit_skani` test.

3. **Declare the heavy dependencies as an opt-in extra.** A new `prott5` extra
   carrying `torch`, `transformers` and `sentencepiece`, deliberately not folded
   into `ai` or `all`.

4. **Validate numerically at library level.** Re-run the 2026-09-21 evaluation
   with the protein-embedding step routed through `ProtT5Utils` — pinned to
   `max_residues=1000` to match how the baseline was actually measured — and
   require recall@1/@5/@10 to land within tolerance of 44.6 / 64.0 / 66.3 %.

## User Stories

1. As an **operator** deploying the KBDL service, I want the `KBDL_PROT_T5_MODEL`
   I configure to be the model that is actually loaded, so that my deployment
   decision is not silently discarded.
2. As an **operator**, I want a run that cannot honour my configured checkpoint
   to fail loudly, so that I find out at submission time rather than from
   degraded science months later.
3. As a **scientist** submitting a `KBDLHorizyn` job, I want the embeddings to
   come from the same ProtT5 checkpoint the registered reaction index was built
   against, so that the cosine similarities I get back are meaningful.
4. As a **library caller**, I want `KBDLServiceUtils.submit_horizyn(...)` to
   exist, so that I can submit the job type without hand-writing the magic
   string `"KBDLHorizyn"`.
5. As a **library caller**, I want `JOB_TYPE_HORIZYN` as a named constant, so
   that a typo in the job type is a `NameError` at import rather than a 4xx from
   the service at runtime.
6. As a **library caller**, I want `submit_horizyn` to behave exactly like the
   eight sibling `submit_*` methods, so that I do not have to learn a second
   calling convention for one job type.
7. As a **developer** integrating `ProtT5Utils`, I want passing `model_name=` to
   raise a `TypeError` that names `model_path`, so that I am corrected in one
   second rather than debugging a wrong-model result.
8. As a **developer**, I want `import kbutillib` to stay fast and dependency-free,
   so that the core library remains installable without `torch`.
9. As a **developer** who does need embeddings, I want `pip install
   kbutillib[prott5]` to pull exactly the right heavy dependencies, so that I do
   not have to discover `sentencepiece` by reading a stack trace.
10. As a **developer** on a machine with no GPU, I want the embedding path to run
    on CPU rather than hard-fail, so that I can develop and test without GPU
    access.
11. As a **reviewer** of this work, I want a numeric regression check against the
    2026-09-21 baseline, so that a tokenisation or pooling drift is caught by a
    number rather than by inspection.
12. As a **reviewer**, I want the numeric check to be pinned to the same
    truncation cap the baseline used, so that a passing result means the pipeline
    matches rather than that the test was lenient.
13. As a **maintainer** of the KBDL service, I want the service's truncation cap
    passed explicitly into `ProtT5Utils`, so that the two independent caps cannot
    diverge silently and mis-report which proteins were truncated.
14. As a **maintainer**, I want the fix confined to two lines in the adapter's
    embedding seam, so that the blast radius on a live production service is as
    small as it can be.
15. As a **future reader** of this PRD, I want the stale-clone failure that
    produced the first draft recorded explicitly, so that the next design session
    researches against `origin/main` rather than a local checkout.

## Implementation Decisions

### D1 — `model_path` is canonical; the KBDL call site is what changes

`ProtT5Utils.__init__` keeps `model_path` as its model-identity parameter. It is
the name the merged, tested and documented module already uses, it is consistent
with the module's `DEFAULT_MODEL_PATH` constant, and it is accurate — the value
may be a HuggingFace id *or* a local filesystem path, and on poplar it is likely
to be the latter.

The correction is made in `kbdl_service/job_types/horizyn.py::_embed_proteins`,
which is the single function in the KBDL service that imports `ProtT5Utils`:

- `ProtT5Utils(model_name=model_name)` becomes
  `ProtT5Utils(model_path=model_name, max_residues=<the service's cap>)`.
- The local variable / parameter name inside `_embed_proteins` may stay
  `model_name`; it is fed from `ctx.settings.prot_t5_model` and renaming it is
  churn without benefit. Only the **keyword passed to the constructor** must
  change.

**Cross-repo landing order is safe in either direction, and this is not
obvious.** The KBDL fix lands first (taskplan phase 1) and the KBUtilLib guard
second (phase 2), but there is no window in which the two repos are
incompatible: `model_path` is *already* the real parameter on KBUtilLib `main`,
so a KBDL that has been fixed to pass `model_path=` works against both the
current and the guarded KBUtilLib. The guard only ever rejects `model_name=`,
which after phase 1 nothing passes. No feature flag, no coordinated release,
and no rollback coupling is needed.

### D2 — The confusable name becomes a loud failure, not a silent one

`ProtT5Utils.__init__` gains an explicit keyword-only `model_name` parameter
defaulting to `None`. When it is not `None`, the constructor raises `TypeError`
with a message naming `model_path` as the correct parameter.

**The message text is pinned**, so the implementation and its test cannot drift
apart, and so the criterion is checkable rather than a matter of taste:

```
ProtT5Utils has no 'model_name' parameter; use 'model_path' instead.
```

Tests assert on that exact string. If it is reworded later, the test moves with
it in the same commit.

This exists because of exactly what happened here: `BaseUtils.__init__` absorbs
every unrecognised keyword argument via `setattr`, so a mistyped or
wrongly-named constructor argument is not an error anywhere in KBUtilLib — it
becomes a dead attribute. Fixing that swallow behaviour repo-wide is out of
scope and would be a large, risky change. Guarding the one name that has already
caused a production-visible defect is cheap, targeted, and testable.

The guard is deliberately a hard `TypeError` rather than a silent alias. An alias
would make both names permanently valid and leave a two-name wart in a young API;
a deprecation warning would be discarded by the service's logging in practice.
Loud is correct here because the failure mode being prevented is *silence*.

### D3 — The service's truncation cap is passed through explicitly

`ProtT5Utils` owns a `max_residues` guard (default 5000) and reports
`truncated_ids`. The KBDL adapter separately truncates to
`ctx.settings.prot_t5_max_residues` (default 5000) before calling
`_embed_proteins`, and reports truncation per-protein into its own stats.

Today those two defaults coincide, so nothing misbehaves. They are nonetheless
independent knobs: an operator raising `KBDL_PROT_T5_MAX_RESIDUES` above 5000
would get a second, invisible truncation inside the library, and the adapter's
per-protein truncation report would understate what actually happened.

So `_embed_proteins` passes `max_residues=` through from the service settings.
After that the library's cap is never the effective one in service use, and the
adapter's truncation reporting is authoritative. This supersedes the earlier
draft's D4, which asserted that `ProtT5Utils` should not implement truncation at
all — the merged module does, and re-litigating that would mean editing shipped,
tested behaviour for no functional gain.

### D4 — Client: the tenth job type

In `src/kbutillib/domains/external/kbdl_service_utils.py`:

- Add `JOB_TYPE_HORIZYN = "KBDLHorizyn"` **immediately after
  `JOB_TYPE_UPLOAD_OBJECT`**, the last of the nine existing constants, so the
  block stays one contiguous run of assignments.
- Add `submit_horizyn(self, **params: Any) -> str` returning
  `self._submit(JOB_TYPE_HORIZYN, params)`, mirroring `submit_skani` exactly and
  placed **immediately after `submit_skani`**, keeping the `submit_*` methods
  together and the new one adjacent to the sibling it copies.
- **Do not** attempt the docstring edits the earlier draft prescribed. The module
  docstring enumerates HTTP endpoints, not job types, and `submit_and_wait`'s
  docstring says "(`JOB_TYPE_GENOME_ANNOTATION`, etc.)" — neither contains a
  five-item list to extend. There is no enumerated job-type list in this module
  to keep in sync.
- No change to `_submit`, `check_job`, `poll_until_terminal`, `get_job_result` or
  the error taxonomy. Horizyn's failure codes already flow through the existing
  `KBDLInvalidInputError` / `KBDLJobFailedError` mapping.

### D5 — Heavy dependencies are their own opt-in extra

Add to `[project.optional-dependencies]` in `pyproject.toml`:

```
prott5 = ["torch", "transformers", "sentencepiece"]
```

`sentencepiece` is required and easy to miss — the ProtT5 `T5Tokenizer` needs it,
and its absence surfaces as a confusing tokenizer error rather than a clean
`ImportError`.

The extra is deliberately **not** merged into the existing `ai` extra, which is
`httpx[socks]` only and serves the Argo / lp-solver HTTP clients; adding torch
there would impose multi-gigabyte wheels on every `ai` install. It is also
deliberately **not** added to `all`, which exists to install "everything needed
to run all transports + build docs" — a docs build has no business pulling a
CUDA-enabled torch.

**Version bounds: bare names, deliberately, and this is a decision rather than
an omission.** The sibling extras do carry lower bounds (`mcp >=1.27,<2`,
`fastapi >=0.110`, `httpx[socks] >=0.28`), so "match the siblings" is genuinely
ambiguous here and is resolved explicitly: list `torch`, `transformers` and
`sentencepiece` **unpinned**.

The reason is that a bound nobody has tested is worse than no bound. The
siblings' floors were set against versions the repo actually exercises in CI;
this extra is never installed in CI (see G7), so any floor written here would be
invented. `torch` additionally has a version/build matrix (CUDA vs CPU, platform
wheels) that a single floor cannot express correctly. An unpinned extra installs
the current release and fails visibly if that release is incompatible; an
invented floor fails obscurely and silently constrains users for years. **Do not
add version bounds to this extra without testing them.**

**CPU-only installs need a documented route.** The default `pip install
kbutillib[prott5]` pulls a CUDA-enabled `torch` on Linux, which is several
gigabytes and pointless on a CPU-only machine. Note in the extra's comment that
CPU-only users should install torch first from the CPU index
(`pip install torch --index-url https://download.pytorch.org/whl/cpu`) and then
install the extra, which will find the requirement already satisfied.

The existing lazy-import behaviour inside `_load()` already raises `ImportError`
with an install hint, and the KBDL adapter already maps `ImportError` to
`DEPENDENCY_UNAVAILABLE`. That path is the contract and needs no change; the
extra simply makes the correct install one command.

### D6 — Numeric acceptance runs at library level, pinned to the baseline's cap

The acceptance re-runs the 2026-09-21 evaluation
(`KBDLJobRunningPrototype/agent-io/research/horizyn-inverted-eval-b82ee05d/run_eval.py`,
tracked in git) with its inline ProtT5 block replaced by a `ProtT5Utils` call,
and compares recall@1/@5/@10 against 44.6 / 64.0 / 66.3 % on the 175-protein
scorable set.

Two decisions make this a valid regression check rather than a green-looking
one:

- **`max_residues=1000`**, matching `run_eval.py`'s `MAX_RESIDUES = 1000`. The
  module's 5000 default would embed long enzymes more fully than the baseline
  did and change the numbers for a legitimate reason, which is indistinguishable
  from a real regression.
- **Tolerance-based, not exact, and stated as arithmetic rather than prose.** The
  reference batches a fixed 16 sequences per forward pass; the module batches by
  residue count. Mean-pooling is per-sequence and padding-excluded, so batch
  composition should not change results in exact arithmetic — but fp16
  accumulation on CUDA is not associative, so small drift is expected.

  "Within 2 percentage points" is ambiguous between an absolute and a relative
  reading and flips near-boundary outcomes, so the comparison is pinned as a
  formula. Express each recall as a **fraction in [0, 1]** and require, for each
  of k = 1, 5, 10:

  ```
  abs(measured_k - baseline_k) <= 0.02
  ```

  with `baseline_1 = 0.446`, `baseline_5 = 0.640`, `baseline_10 = 0.663`.
  Compare the **unrounded** computed fractions — do not round to one decimal
  place and then compare, which would let a genuine 2.04-point drift pass. A
  tokenisation or pooling error produces a collapse of tens of points, not two.

- **The output artifact is `agent-io/research/prott5-utils-numeric-acceptance.md`**
  in KBDLJobRunningPrototype, recording the three measured fractions, the three
  absolute deltas, the model path, `max_residues`, the device, and a pass/fail
  verdict per k. The original `run_eval.py` is not modified — the variant is a
  new script beside it, because `run_eval.py` is the baseline record.

It runs **at library level, not through the KBDL service**. Going through the
service would require `KBDL_PROT_T5_MODEL` and `KBDL_HORIZYN_CHECKPOINT` to be
wired on the live poplar deployment, which is an ops step this PRD does not own.
Routing `run_eval.py`'s embedding step through `ProtT5Utils` tests exactly the
thing this PRD changes — the library embedding path — with no service dependency.

### D7 — Build the three KBUtilLib changes in sequence, not in parallel

The three KBUtilLib edits (the `model_name` guard, the client addition, the
packaging extra) touch three disjoint files and are logically independent. They
are nonetheless sequenced into separate phases rather than fanned out as a
parallel group.

The reason is a known, recorded failure of the build harness: when two disjoint
fixes are cut from the same base commit, the second one's reviewer compares
against the pre-first-fix base and returns a false `verdict=fail` that looks like
the second change reverts the first. Sequencing the phases means every task is
cut from a base that already contains its predecessor, and the false-fail cannot
arise. The wall-clock cost is small for a PRD this size; a spurious gate failure
on a live-service fix is not.

## Testing Decisions

Test external behaviour, not internals. The existing
`tests/external/test_prott5_utils.py` is the prior art to follow — it tests the
pure helpers (`_clean_sequence`, `_make_batches`, truncation) directly and the
pooling behaviour through a fake encoder whose padded positions carry a huge
sentinel value, so any pooling over padding is unmistakable rather than subtle.

- **The `model_name` guard.** A unit test asserting
  `ProtT5Utils(model_name="anything")` raises `TypeError` and that the message
  mentions `model_path`. A second asserting `ProtT5Utils(model_path="x")` still
  constructs normally and sets `.model_path`. These need no model and no torch.

- **The KBDL call site.** The adapter's tests already stub `_embed_proteins` as a
  seam, so they will not catch a constructor mismatch — that is precisely how
  this defect shipped. Add a test that exercises `_embed_proteins` itself with
  `ProtT5Utils` monkeypatched by a recording double, asserting it was constructed
  with `model_path=` set to the settings value and `max_residues=` set to
  `settings.prot_t5_max_residues`. This is the regression test for the actual
  bug and is the most important test in this PRD.

- **The pooling/tokenizer coupling (closes G3).** The module slices to `n_res`,
  the residue count of its own cleaned string; the reference slices to
  `attention_mask.sum() - 1`. These agree only while the ProtT5 tokenizer emits
  exactly one token per residue plus one trailing EOS — an assumption the design
  rests on and nothing currently checks. Add a test, behind the existing
  `prott5_model` + `slow` markers (it needs the real sentencepiece tokenizer, not
  the encoder weights), that tokenises a handful of sequences of differing
  lengths and asserts `attention_mask[i].sum() - 1 == n_res` for each. This is
  cheap, it runs only when `PROTT5_LIVE_TESTS=1`, and it converts an assumption
  into an assertion.

- **`submit_horizyn`.** Copy the existing `submit_skani` test in
  `tests/external/test_kbdl_service_utils.py`: assert the client POSTs
  `{"schema_version": "1", "job_type": "KBDLHorizyn", "params": {...}}` and
  returns the job id. Assert `JOB_TYPE_HORIZYN == "KBDLHorizyn"`.

- **The packaging extra.** No runtime test. A check that `pyproject.toml` parses
  and that `prott5` appears in `[project.optional-dependencies]` with all three
  names is sufficient; the install itself is not exercised in CI.

- **Numeric acceptance.** Not a pytest test — a one-off verification run
  producing a report artifact (see D6). It is GPU-bound, loads a 3B-parameter
  model, and depends on `/scratch` artifacts; it must never become part of the
  default suite.

**Regression framing.** Both repos carry pre-existing test failures, so no
criterion in this PRD requires a green suite. Every criterion is phrased as: the
tests this task adds pass, and no test that passed on the base commit fails on
the branch.

## Open Questions and Judgement Calls

Every entry below is **already decided and reflected in the PRD body and the
taskplan**. This PRD is buildable as it stands. Changing a decision means editing
its `DECIDED:` line and following the `If you disagree` consequences — it does
not mean the build is blocked.

### Q1. `model_path` or `model_name` — which name is canonical, and which repo changes? -- DECIDED: `model_path` is canonical; the KBDL call site is corrected

**Blast radius:** MEDIUM
**Why:** Both halves are merged, so "the consumer wins" (the earlier draft's
rule) has no force — the library merged first, on 2026-09-13, and is tested and
documented against `model_path`. `model_path` is also the more accurate name: the
value is frequently a local checkpoint path, and the module's own constant is
`DEFAULT_MODEL_PATH`. Correcting one keyword at one call site is a far smaller
change than renaming a public library parameter along with its tests, docstrings
and module constant.
**If you disagree:** the alternative is renaming `ProtT5Utils.__init__`'s
parameter to `model_name`, which means editing `prott5_utils.py`'s constructor,
the `DEFAULT_MODEL_PATH` constant, the class and method docstrings, the
`self.model_path` attribute and its three internal reads in `_load()`, and every
reference in `tests/external/test_prott5_utils.py` — then *also* deleting task
`kbdl-callsite-fix` from the taskplan and dropping KBDLJobRunningPrototype from
the `repos` union, making this a single-repo PRD again. Q2 and D2 both collapse
into that change.
**Confidence:** medium — high that one of the two names must become canonical and
that the fix is small either way; medium on which, because the KBDL service is
live on poplar and any change there carries deploy risk that a KBUtilLib-only
change does not.

### Q2. Does this PRD cross the repo boundary into KBDLJobRunningPrototype? -- DECIDED: yes, exactly two keyword arguments in one function

**Blast radius:** MEDIUM
**Why:** The earlier draft declared "any change to the KBDL service" out of
scope. That boundary was drawn under the false premise that KBUtilLib had nothing
built yet and could therefore absorb the whole fix. With the library merged
first, the defect is unambiguously on the consumer side, and a PRD that refuses
to touch the consumer cannot fix it. The crossing is kept as small as it can be:
two keyword arguments inside `_embed_proteins`, the single function in the KBDL
service that imports `ProtT5Utils`.
**If you disagree:** resolve Q1 the other way and this task disappears entirely —
`repos` drops to `["KBUtilLib"]`, the taskplan's first phase is deleted, and D3's
explicit `max_residues` pass-through is lost with it, so the two truncation caps
stay independently settable and G-level gotcha about divergent caps becomes
permanent.
**Confidence:** high — the call site is one function, it is quoted in this
document, and the change is two keywords.

### Q3. Does the numeric acceptance run end-to-end through the KBDL service, or at library level? -- DECIDED: library level, bypassing the service

**Blast radius:** MEDIUM
**Why:** An end-to-end run needs `KBDL_PROT_T5_MODEL` and
`KBDL_HORIZYN_CHECKPOINT` wired on the live poplar deployment plus a service
restart — an ops step this PRD explicitly does not own (see Out of Scope). Making
acceptance depend on it would make the PRD unbuildable until someone else acts,
which is the failure mode that left `kbdl-horizyn-v1` half-landed in the first
place. Routing `run_eval.py`'s embedding step through `ProtT5Utils` tests exactly
what this PRD changes, with no service dependency.
**If you disagree:** the numeric acceptance task's prompt must be rewritten to
submit through `KBDLServiceUtils.submit_horizyn` against the registered index
`ref:a9f38994…`, the ops wiring must move from Out of Scope into a blocking
prerequisite phase, and the PRD stops being buildable by the conductor alone.
**Confidence:** high — the library-level run is strictly a superset of what this
PRD's changes can affect, and end-to-end validation remains available later as a
separate, ops-gated check.

### Q4. What truncation cap does the numeric acceptance use? -- DECIDED: `max_residues=1000`, matching the baseline as measured

**Blast radius:** LOW
**Why:** `run_eval.py:38` sets `MAX_RESIDUES = 1000`; `ProtT5Utils` defaults to
5000 and KBDL's `DEFAULT_PROT_T5_MAX_RESIDUES` is 5000. Running the comparison at
5000 would embed long enzymes more fully than the baseline did, changing recall
for a legitimate reason that is indistinguishable from a genuine regression. The
acceptance must reproduce the baseline's conditions to mean anything.
**If you disagree:** re-running at 5000 requires first re-measuring the baseline
at 5000 — a second full GPU eval over the 175-protein set — and updating the
44.6 / 64.0 / 66.3 figures quoted in D6, in the numeric acceptance task's
`success_criteria`, and in `2026-09-21-horizyn-inverted-direction-measurement.md`.
**Confidence:** high — both numbers are read directly from source.

### Q5. Does `ProtT5Utils` keep its own truncation, contradicting the earlier draft's D4? -- DECIDED: keep it, and have the adapter pass its cap through explicitly

**Blast radius:** LOW
**Why:** The merged module already truncates and reports `truncated_ids`, with
tests covering it. Removing that to honour a superseded design note would mean
editing shipped, tested behaviour for no functional gain. The real risk is not
that the library truncates, but that its cap and the service's are independently
settable and can diverge silently; passing the service's cap through removes that
risk without touching the library.
**If you disagree:** drop the `max_residues=` argument from the call-site task and
the assertion from its test; the library's 5000 then silently applies whenever an
operator raises `KBDL_PROT_T5_MAX_RESIDUES` above it, and the adapter's
per-protein truncation report understates reality.
**Confidence:** high.

### Q6. Where do `torch` / `transformers` / `sentencepiece` live? -- DECIDED: a new `prott5` extra, not in `ai`, not in `all`

**Blast radius:** LOW
**Why:** The existing `ai` extra is `httpx[socks]` only — it serves the Argo and
lp-solver HTTP clients and has nothing to do with local ML. Folding torch in
there would impose multi-gigabyte wheels on every `ai` install. `all` is
documented as "everything needed to run all transports + build docs", and a docs
build has no reason to pull CUDA torch.
**If you disagree:** adding `prott5` to `all` changes what `pip install
kbutillib[all]` downloads by several gigabytes and will slow or break the
`apidocs` workflow; folding into `ai` additionally changes the dependency
footprint of every existing `ai` consumer.
**Confidence:** high — the extras and their stated purposes are quoted from
`pyproject.toml`.

### Q7. Are the three KBUtilLib changes built in parallel or in sequence? -- DECIDED: sequential phases

**Blast radius:** NONE (build orchestration only; no effect on shipped behaviour)
**Why:** They touch disjoint files and would be safe to parallelise on the merits.
But the build harness has a recorded failure where the second of two disjoint
same-base fixes draws a false `verdict=fail` whose diff appears to revert the
first. Sequencing means each task is cut from a base containing its predecessor,
so the condition cannot arise. For three small tasks the wall-clock cost is
minutes; a spurious gate failure on a live-service fix costs an investigation.
**If you disagree:** collapse phases 2–4 of the taskplan into one
`parallel_group` and accept the risk of a false reviewer failure on whichever
task lands second.
**Confidence:** high on the orchestration call; the underlying harness defect is
recorded separately and is not fixed by this PRD.

### Q8. What I could not decide and did not guess

One item was resolved by direct measurement during this round; one remains
genuinely undeterminable and was not guessed at.

1. **RESOLVED — what `KBDL_PROT_T5_MODEL` is set to on the live poplar
   deployment: it is not set at all.** This was open when the round began and was
   closed by reading the running service rather than reasoning about it. The live
   `kbdl-worker@` systemd units on poplar set neither `KBDL_PROT_T5_MODEL` nor
   `KBDL_HORIZYN_CHECKPOINT`, and `KBDLHorizyn` does not appear in the unit's
   `KBDL_JOB_CONCURRENCY` list either (verified 2026-09-22 via
   `systemctl --user cat kbdl-worker@1.service`). With `DEFAULT_PROT_T5_MODEL =
   None`, `capabilities.py` therefore rejects every `KBDLHorizyn` submission at
   the capability gate.

   **Consequence: Q1's defect is confirmed latent, and no production result is
   tainted.** No Horizyn job has ever run on the live service, so the silent
   wrong-model fallback has never produced a stored result. This lowers the
   urgency of the fix but *raises* the urgency of its ordering — see G1. It also
   means the job type is built, merged, indexed and **unreachable**, which is
   tracked outside this PRD as an ops item.

2. **UNRESOLVED — whether `/scratch/chenry/horizyn-artifacts/horizyn_v1_0_inf.ckpt`
   and the eval set will still exist when the numeric acceptance task runs.** They
   are present now (verified 2026-09-22), but `/scratch` is explicitly ephemeral
   on this host and nothing pins them. **Assumption made:** present at build time;
   the acceptance task is instructed to fail loudly naming the missing path rather
   than silently substituting a smaller set or reporting success on a skip.

## Gotchas and Unintuitive Consequences

**G1 — There is a window in which this bug becomes real, and it opens the moment
someone does the ops wiring.** As measured in Q8.1, `KBDL_PROT_T5_MODEL` is
currently unset on poplar, so `capabilities.py` rejects every Horizyn submission
and the silent wrong-model fallback has never executed. No stored result is
tainted.

That safety is incidental, not designed, and it ends the instant an operator
sets the variable. **If the ops wiring lands before this fix, every Horizyn job
run in between silently embeds with the hardcoded default checkpoint regardless
of what was configured** — and because the vectors stay finite, 1024-d and
plausible, nothing will flag it. The two changes are independent and are owned
by different people, which is exactly the condition under which they get
sequenced wrongly. **Land the fix first.** If the wiring somehow lands first,
treat every Horizyn result produced before the fix as having used the default
checkpoint irrespective of configuration.

On a deployment where the configured model is *not* the stock
`Rostlab/prot_t5_xl_half_uniref50-enc`, applying the fix will also change
rankings relative to any such interim runs. That is the fix working, not a
regression — but anyone diffing before and after without this context will read
it as one.

**G2 — The fix can expose a pre-existing index/model mismatch.** The registered
reaction index `ref:a9f38994…` was built against one specific ProtT5 checkpoint.
Until now the adapter always embedded with the hardcoded default. Once the
configured model is genuinely honoured, a deployment configured to a *different*
checkpoint than the index was built with will produce systematically poor recall
— and it will be the first time that mismatch has been visible. Making model
identity honest does not make it correct; it makes it checkable.

**G3 — The module's pooling and the reference's are equivalent only by
assumption — now asserted, but only under an opt-in marker.** `run_eval.py`
slices to `attention_mask[j].sum() - 1`; the merged module slices to `n_res`, the
residue count of its own cleaned string. These agree only while the ProtT5
tokenizer emits exactly one token per residue plus a single trailing EOS. That
holds for single-letter residues after UZOB→X mapping, which is why the module is
correct — but the two are not the *same* computation, and a tokenizer change
would break them differently.

This round added a test asserting the equivalence directly (Testing Decisions),
which is a real improvement over relying on the numeric acceptance to notice.
The catch: it needs the real sentencepiece tokenizer, so it sits behind the
`prott5_model` + `slow` markers and runs only when `PROTT5_LIVE_TESTS=1`. **A
routine CI run still will not catch a tokenizer-behaviour change**, so this
closes the gap for anyone who runs the live suite and leaves it open for anyone
who does not.

**G4 — Batch composition differs from the reference, so exact equality is the
wrong bar.** The reference uses a fixed 16 sequences per forward pass; the module
packs by residue count. With padding excluded from pooling, batch composition is
mathematically irrelevant — but fp16 accumulation on CUDA is not associative, so
identical inputs in different batches give slightly different floats. This is why
D6 specifies a ±2pp tolerance. Anyone "tightening" that to exact equality will
get a flaky test.

**G5 — `truncated_ids` is per-instance mutable state.** It is reset at the top of
each `embed_proteins` call and read afterwards. A single `ProtT5Utils` instance
shared across concurrent jobs will therefore report the wrong truncation set, and
the reporting is silently wrong rather than erroring. The KBDL adapter constructs
a fresh instance per call, so it is safe today; anyone introducing instance
caching to avoid the model-load cost must not share the instance across
concurrent work.

**G6 — The Q1 guard closes one name, not the class.** `BaseUtils.__init__`
`setattr`s every unrecognised keyword argument, across every utility class in
KBUtilLib. `model_name` gets an explicit guard because it has already cost a
production defect; every other mistyped kwarg to every other util is still
absorbed silently. That is a real, repo-wide hazard this PRD deliberately does
not fix.

The cheap general fix, if someone takes it up later, is a debug-mode warning
rather than a behaviour change: have `BaseUtils.__init__` log at warning level
when it absorbs a keyword that no class in the MRO declares, gated so it is off
by default. That surfaces the whole class of defect without breaking any caller
currently relying on the setattr behaviour — which, given it has been the
convention for a long time, some almost certainly are. Recorded here rather than
scoped in, because doing it properly means auditing those callers.

**G7 — A green CI run does not mean the embedding path works.** The real-model
test is behind the `prott5_model` and `slow` markers and requires
`PROTT5_LIVE_TESTS=1`; adding the `prott5` extra does not cause CI to install
torch. Everything CI exercises runs against a fake encoder. Only the numeric
acceptance task touches the real model.

**G8 — `/scratch` is ephemeral on this host.** The Horizyn checkpoint
(192 MB) and the eval set live under `/scratch/chenry/horizyn-artifacts/`. They
are present as of 2026-09-22, but nothing guarantees they survive to build time,
and their loss makes the numeric acceptance unrunnable rather than failing.

**G9 — Fixing the client does not make the job submittable.** `submit_horizyn`
will construct and POST correctly, and the service will still reject the
submission with a capability error until `KBDL_PROT_T5_MODEL` is set on the
deployment (`capabilities.py` refuses while it is unset). The client addition and
the ops wiring are independently necessary; neither alone produces a working
submission.

## Sources Consulted

**KBUtilLib, read at `origin/main` (not the local checkout, which was 116 commits
behind — this is what caught the draft's false premise):**

- `src/kbutillib/domains/ai/prott5_utils.py` — full read. Module docstring,
  `DEFAULT_MODEL_PATH`, `EMBEDDING_DIM`, `_UZOB`, `__init__` signature (the
  `model_path` finding), `_get_device`, `_load`, `_no_grad`, `_clean_sequence`,
  `_make_batches`, `_embed_batch` (the pooling `n_res` finding),
  `_embed_batch_with_backoff`, `embed_proteins`.
- `src/kbutillib/core/base_utils.py` — `__init__`; the
  `for key, value in kwargs.items(): setattr(...)` line is the direct cause of
  the defect being silent.
- `src/kbutillib/core/shared_env_utils.py` — `__init__`; confirmed it forwards
  `**kwargs` onward rather than validating them.
- `src/kbutillib/domains/ai/__init__.py` — `__all__` and `_MODULE_MAP`; confirmed
  `ProtT5Utils` / `ProtT5UtilsImpl` are already registered in the lazy loader.
- `src/kbutillib/domains/external/kbdl_service_utils.py` — the nine `JOB_TYPE_*`
  constants, the eight `submit_*` methods, the module docstring (endpoints, not
  job types), and `submit_and_wait`'s docstring. Confirmed `KBDLHorizyn` absent.
- `pyproject.toml` — `[project.optional-dependencies]` (`dev`, `mcp`, `api`,
  `ai`, `lp_solver`, `apidocs`, `all`) and `[tool.pytest.ini_options] markers`
  (including the pre-existing `prott5_model` marker).
- `tests/external/test_prott5_utils.py` — test class inventory and markers;
  established that the ProtT5 test surface is already comprehensive.
- `git log` for `prott5_utils.py` — commit `215625c`, 2026-09-13, full message.
- `git grep -i horizyn origin/main -- src/ tests/` — empty, confirming the client
  gap is real.

**KBDLJobRunningPrototype, read at `origin/main` (local == origin, verified):**

- `src/kbdl_service/job_types/horizyn.py` — `_embed_proteins` (lines ~428–460,
  the `ProtT5Utils(model_name=model_name)` call at 452) and its call site at 585.
- `src/kbdl_service/config.py` — `DEFAULT_PROT_T5_MODEL = None` (line 112),
  `DEFAULT_PROT_T5_MAX_RESIDUES = 5000` (line 120), and the `Settings` fields and
  env-var reads at 744–750 / 904–973.
- `src/kbdl_service/capabilities.py` — the `KBDLHorizyn` rejection while
  `prot_t5_model` is unset (lines ~278–305); this is why the defect is latent.
- `agent-io/research/horizyn-inverted-eval-b82ee05d/run_eval.py` — the reference
  embedding loop (lines ~135–175), `MAX_RESIDUES = 1000` (line 38), `CKPT` (line
  34). Confirmed tracked in git.
- `agent-io/research/2026-09-21-horizyn-inverted-direction-measurement.md` — the
  44.6 / 64.0 / 66.3 % baseline, n=175 scorable proteins, and its own caveats
  about EC-class variance and encoder-training contamination.

**Filesystem, this host, 2026-09-22:**

- `/scratch/chenry/horizyn-artifacts/` — `horizyn_v1_0_inf.ckpt` (192 MB),
  `genome_4000.faa`, `prott5_4000.npy` present.

**Read and found uninformative (recorded so the absence is visible):**

- The `ai` extra in `pyproject.toml` — checked as a candidate home for torch;
  it is `httpx[socks]` only and is the wrong home (D5).
- `kbdl_service_utils.py`'s module docstring and `submit_and_wait` docstring —
  read specifically to perform the earlier draft's prescribed "five → six" edits;
  neither contains the text described, so the instruction was dropped rather
  than approximated (D4).

**Prior design context:**

- `KBDLJobRunningPrototype/agent-io/prds/kbdl-horizyn-v1/` — the design ancestor
  whose D7 specified `ProtT5Utils` as a cross-repo deliverable and whose D8 owns
  adapter-side truncation.
- The earlier draft of this bundle (`humanprompt.md` / `fullprompt.md` /
  `data.json` as written 2026-09-22 by session `7ed8e324`), superseded by this
  round.

## Out of Scope

- **Live poplar env wiring** — setting `KBDL_HORIZYN_CHECKPOINT` and
  `KBDL_PROT_T5_MODEL` on the deployed service and restarting it. This is an ops
  step with its own change-control, and it is a prerequisite for *end-to-end*
  Horizyn runs but not for anything in this PRD (see Q3). It is tracked
  separately; the PRD is complete and verifiable without it.
- **Any other change to the KBDL service** beyond the two keyword arguments in
  `_embed_proteins` — no schema, adapter logic, scoring, index, or object-type
  changes.
- **Rebuilding or re-registering the reaction index** (`ref:a9f38994…`).
- **Fixing `BaseUtils`' swallow-unknown-kwargs behaviour repo-wide** (G6). Real,
  recorded, deliberately not attempted here.
- **A new or fine-tuned ProtT5 model**, and any caching or persistence of
  embeddings across jobs.
- **Renaming `ProtT5Utils`' public parameter** — settled as `model_path` (Q1).
- **End-to-end validation through the running service** — available later as an
  ops-gated check (Q3).

## Further Notes

- **Design ancestor:** `kbdl-horizyn-v1` (KBDLJobRunningPrototype), D7/D8/D9. This
  PRD supersedes D7's cross-repo `ProtT5Utils` deliverable (built and merged as
  `215625c`) and supersedes the earlier draft's D4 on truncation ownership
  (see Q5).
- **The stale-clone lesson is worth keeping.** The first draft of this bundle was
  written against `~/projects/KBUtilLib`, 116 commits behind `origin/main`, and
  confidently specified building a module that had existed for nine days. Design
  research must read `origin/main` — on this host the local clones are not
  automatically current, and nothing warns you.
- **Reference implementation:**
  `KBDLJobRunningPrototype/agent-io/research/horizyn-inverted-eval-b82ee05d/run_eval.py`,
  verified on poplar 2026-09-21, tracked in git.
- **Conventions to match:** `domains/ai/argo_utils.py`, `domains/ai/kb_plm_utils.py`
  and `domains/ai/__init__.py` for the public/Impl and lazy-loader pattern;
  `submit_skani` and its test for the client addition.

## Acceptance Criteria

1. `kbdl_service/job_types/horizyn.py::_embed_proteins` constructs `ProtT5Utils` with the keyword `model_path=`, fed from the service's `prot_t5_model` setting.
2. The same call passes `max_residues=` fed from the service's `prot_t5_max_residues` setting.
3. The string `model_name=` no longer appears as a `ProtT5Utils` constructor argument anywhere in the KBDL service.
4. A KBDL test exercises `_embed_proteins` with `ProtT5Utils` replaced by a recording double and asserts both keyword arguments and their values.
5. `ProtT5Utils.__init__` accepts an explicit keyword-only `model_name` parameter defaulting to `None`.
6. Supplying `model_name` raises `TypeError` with the exact message `ProtT5Utils has no 'model_name' parameter; use 'model_path' instead.`
7. `model_path` remains the canonical parameter and `ProtT5Utils(model_path="x")` still constructs and sets `.model_path == "x"`.
8. The two guard tests pass with neither `torch` nor `transformers` installed.
9. A test behind the `prott5_model` and `slow` markers asserts `attention_mask[i].sum() - 1 == n_res` for sequences of differing lengths, using the real tokenizer.
10. `kbdl_service_utils.py` defines `JOB_TYPE_HORIZYN = "KBDLHorizyn"` immediately after `JOB_TYPE_UPLOAD_OBJECT`.
11. `kbdl_service_utils.py` defines `submit_horizyn(**params)` delegating to `self._submit(JOB_TYPE_HORIZYN, params)`, placed immediately after `submit_skani`.
12. A client test asserts `submit_horizyn` posts `{"schema_version": "1", "job_type": "KBDLHorizyn", "params": {...}}` and returns the job id.
13. The `kbdl_service_utils.py` module docstring and the `submit_and_wait` docstring are byte-identical to the base commit.
14. `pyproject.toml` parses and `[project.optional-dependencies]` contains `prott5` listing `torch`, `transformers` and `sentencepiece`, all unpinned.
15. The `prott5` extra's comment documents the CPU-only install route via the PyTorch CPU index.
16. The `ai` and `all` extras are byte-identical to the base commit and contain no `torch`.
17. `src/kbutillib/domains/ai/prott5_utils.py` is unchanged by the packaging task.
18. The numeric acceptance run embeds via `ProtT5Utils` constructed with `model_path=` and `max_residues=1000`.
19. For each of k = 1, 5, 10, `abs(measured_k - baseline_k) <= 0.02` holds on unrounded fractions, with baselines 0.446, 0.640 and 0.663.
20. A report at `agent-io/research/prott5-utils-numeric-acceptance.md` records the three measured fractions, the three absolute deltas, the model path, `max_residues`, the device, and a per-k verdict.
21. The original `run_eval.py` is unmodified; the acceptance variant is a separate committed script.
22. If any prerequisite artifact is absent, the acceptance task fails naming the missing path rather than reporting success on a skipped run.
23. In every task, no test that passed on the base commit fails on the branch.
