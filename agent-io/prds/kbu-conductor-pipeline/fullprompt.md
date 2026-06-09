# KBU Conductor Pipeline — make /kbu-plan and /kbu-build truly parallel /ai-design and /ai-conductor

## Problem Statement

A researcher who runs `/kbu-plan` then `/kbu-build` on a subproject today
gets a directory of **empty stub notebooks** — `util.py` functions that
`raise NotImplementedError`, notebook cells that are structure plus `# TODO`,
and nothing executed. When Chris tested the v2 build, "all it did was stub
things out." That is not a defect of the implementation; it is what
`/kbu-build` is currently *designed* to do (its own Rule #2: "Stubs only.
Scientific logic belongs to the researcher").

Both skills carry provenance headers declaring them lean-forks of the
platform's gold-standard pair — `kbu-plan.md ← ai-design.md`,
`kbu-build.md ← ai-conductor.md` — but the fork **inverted the conductor
into a scaffolder** and left the plan side too thin to drive anything
better. Three concrete failures:

1. **The plan→build contract is too thin to conduct against.** The only
   machine-readable handoff is the manifest's `[[notebooks]]` entry, which
   carries `slug · purpose · last_run_at · modified_since_run` and nothing
   else — no success criteria, no helper specs, no test definitions, no
   dependency edges. `/ai-conductor` works because `taskplan.json` gives
   each task a `prompt` + `success_criteria` + `depends_on`; the kbu pipeline
   has no equivalent, so `/kbu-build` re-derives everything from
   `RESEARCH_PLAN.md` prose. (The v2 PRD itself flagged this prose-derivation
   as the original sin and did not fully fix it.)

2. **`/kbu-build` does not implement, execute, or verify anything.** It
   writes stubs and advances. There is no dev→review→verify loop, no
   execution, no notion of "this code actually works."

3. **Delegated steps are not wired as subagents — and two subagents are
   orphaned entirely.** Only `kbu-sub-literature-review` is ever spawned via
   an explicit `Agent(...)` call. `kbu-sub-review` and `kbu-sub-diagnose`
   exist as agent definitions but **no command invokes them anywhere**, even
   though the subproject state machine's `p-review` and `b-review` gates
   block on a review file with a `pass` verdict existing. In practice the
   main thread either skipped review or hand-wrote the verdict file inline —
   the exact behavior Chris observed where "the build and plan commands
   didn't realize they needed to run a subagent."

The net effect: the research pipeline has the *shape* of design→conduct but
none of the *substance*. The AI is not writing the researcher's code; it is
producing empty scaffolding the researcher must fill in by hand.

## Solution

Make `/kbu-plan` and `/kbu-build` genuinely parallel `/ai-design` and
`/ai-conductor`, so that **the AI writes all the code and the human only
explains what code they want and resolves genuine algorithmic forks.**

The governing constraint, established empirically by Chris from prior
Claude Code notebook builds: **a research notebook is too slow to execute
end-to-end inside an autonomous build loop.** Therefore the conductor
cannot verify by running the notebook. Instead:

- **The science logic lives in `util.py` helper functions** — the testable
  units. The notebook becomes a thin orchestration layer that calls verified
  helpers in sequence.
- **`/kbu-build` conducts at the helper level:** for each helper it writes
  the real implementation plus a **fast test** (against sampled-real or
  synthetic data), **runs the test** (never the full notebook), and verifies
  it passes. This is the conductor's dev→review→verify loop, with "execute"
  meaning *run the fast test*.
- **Full, slow, real-data execution stays in the `run` state**, human-driven,
  exactly where it is today. Build and run therefore operate at different
  speeds and granularities and do not collide.

This maps onto the platform's own template more tightly than the v2 fork
did, because `ai-design`'s PRD already separates **Implementation Decisions**
(modules + interfaces) from **Testing Decisions** (what makes a good test,
which modules tested):

| ai-design / ai-conductor            | kbu-plan / kbu-build                                  |
|-------------------------------------|------------------------------------------------------|
| PRD (prose, human)                  | `RESEARCH_PLAN.md` (prose, human)                    |
| `taskplan.json` (machine, validated)| `buildplan.json` (machine, validated)               |
| `load_taskplan` / `TaskPlanError`   | `kbu buildplan validate`                            |
| task `prompt` + `success_criteria`  | helper `signature`/`contract` + `test` (the criterion)|
| `depends_on` (strictly-earlier)     | notebook `depends_on` (strictly-earlier)            |
| in_context developer subagent       | `kbu-sub-build` developer subagent                  |
| reviewer subagent → verdict         | `kbu-sub-review` → verdict vs buildplan tests       |
| escalation/blocking phase           | BLOCKED-fork escalation to the researcher           |

The state machine is unchanged
(`plan → p-review → build → b-review → run → synthesize → s-review → complete`);
only the depth of `plan` and `build` increases.

## User Stories

1. As a researcher, I want `/kbu-plan` to grill my goals, run a literature
   review, grill the detailed plan, **and grill the exact test cases for each
   component**, so that the plan it produces is as rigorous as an `/ai-design`
   PRD.
2. As a researcher, I want `/kbu-plan` to emit a machine-readable
   `buildplan.json` alongside the prose `RESEARCH_PLAN.md`, so that the build
   step has a precise contract to conduct against instead of re-reading prose.
3. As a researcher, I want the planning step to **work with me to define
   exactly what each test case asserts and what data it runs against**, so
   that the tests faithfully encode what "correct" means for my science.
4. As a researcher, I want `kbu buildplan validate` to hard-fail a malformed
   or inconsistent buildplan at plan time, so that build never starts against
   a broken contract.
5. As a researcher, I want `/kbu-build` to **write the real implementation of
   every helper function**, not stubs, so that I am not left hand-filling
   `NotImplementedError` bodies.
6. As a researcher, I want `/kbu-build` to **write a fast test for each helper
   and run it**, so that the code is verified to work before it is handed to
   me — without the cost of executing the whole notebook.
7. As a researcher, I want `/kbu-build` to **assemble each notebook as a thin
   orchestration of verified helpers**, so that when I open it in JupyterLab
   the structure is real working code I can run on full data.
8. As a researcher, I want `/kbu-build` to **fan each notebook out to a
   developer subagent and a reviewer subagent**, so that implementation
   detail stays out of my main conversation and independent notebooks build
   in parallel.
9. As a researcher, I want a build subagent that hits a **genuine algorithmic
   fork** (e.g. two equally valid normalization methods) to **stop and ask
   me**, present the options, and continue with my answer — so that I stay in
   the loop only for real scientific decisions, not boilerplate.
10. As a researcher, I want a failing test to trigger a **bounded
    diagnose-and-retry loop** via the diagnose subagent, so that transient
    coding errors self-heal without my intervention.
11. As a researcher, I want every review to be performed by the
    `kbu-sub-review` **subagent** and written to a verdict file the state gate
    checks, so that review actually happens and cannot be faked by an inline
    hand-written verdict.
12. As a maintainer, I want every delegated step (literature, build, review,
    diagnose) invoked through an **explicit `Agent(...)` call at the exact
    point it runs**, with a hard rule forbidding inline execution, so that the
    skills never again silently collapse a subagent step into the main thread.
13. As a researcher, I want the slow full-notebook real-data execution to
    remain in the `run` state where I drive it, so that the autonomous build
    loop stays fast.
14. As a maintainer, I want a unit test for `kbu buildplan validate`, so that
    the one pure-logic correctness surface in this change is regression-proof.

## Implementation Decisions

### Module A — `buildplan.json` schema + `kbu buildplan validate` (net-new CLI)

The machine-readable build contract, `subprojects/<name>/buildplan.json`,
distinct from the prose `RESEARCH_PLAN.md`. It is the `taskplan.json` analog.
Shape (prototype-derived; decision-rich parts only):

```json
{
  "subproject": "<name>",
  "notebooks": [
    {
      "slug": "01_load_fitness",
      "purpose": "<one-sentence purpose from the plan>",
      "depends_on": [],
      "helpers": [
        {
          "name": "load_rbtnseq",
          "signature": "load_rbtnseq(path: str) -> pd.DataFrame",
          "contract": "<what the function must do, in prose>",
          "test": {
            "data_source": "sampled-real | synthetic",
            "data_spec": "<e.g. 'data/raw.tsv head -200' or '10x5 random matrix'>",
            "assertions": ["<exact checkable assertion>", "..."]
          }
        }
      ]
    }
  ]
}
```

`kbu buildplan validate <path>` is the hard gate, mirroring
`load_taskplan` / `TaskPlanError`. It is a **deep module** — tiny interface
`validate(path) -> ok | [errors]`, real logic behind it. It must reject:

- Missing or malformed required fields at any level.
- `depends_on` referencing a notebook that is not **strictly earlier** in the
  `notebooks` list (no same-notebook, no forward, no cyclic edges).
- A helper whose `test` has an empty `assertions` list (a test that asserts
  nothing is not a success criterion).
- A `test.data_source` outside the enum `{sampled-real, synthetic}`.
- Duplicate notebook slugs or duplicate helper names within a notebook.

It surfaces **every** error (not just the first), like `TaskPlanError`.
`/kbu-plan` must run it and pass before `kbu subproject advance`.

### Module B — `/kbu-plan` (modify)

Add two things to the existing four-step flow:

- **Test-design grill (new mandatory step, after the detailed-plan grill).**
  The AI proposes, per helper, the exact `assertions` and `data_source`/
  `data_spec`, and grills the researcher one question per round until each
  test case is pinned down. This is the linchpin: the tests are the success
  criteria the conductor verifies against, so a vague test means build stalls
  or "passes" against a weak check.
- **`buildplan.json` emission + validate gate.** After the test grill,
  `/kbu-plan` writes `buildplan.json`, runs `kbu buildplan validate`, and
  only advances on success. `RESEARCH_PLAN.md` remains the prose artifact; the
  manifest `[[notebooks]]` entries remain the lightweight run-ledger
  (`last_run_at` / `modified_since_run`) and are **not** overloaded with build
  spec.

### Module C — `/kbu-build` (rewrite)

From scaffolder to conductor. New control flow:

1. Load `buildplan.json`; run `kbu buildplan validate` as a re-check; refuse
   to build on failure.
2. For each notebook in `depends_on` topological order (independent notebooks
   in parallel): spawn the `kbu-sub-build` developer subagent (Module D) with
   that notebook's buildplan entry.
3. On the subagent returning a **BLOCKED** signal, surface the decision and
   options to the researcher, get the answer, and re-dispatch the subagent
   with the decision appended.
4. On the subagent returning a normal work-record, spawn `kbu-sub-review`
   (Module E) to verify against the buildplan test cases. On a `fail` verdict,
   spawn `kbu-sub-diagnose`, then retry the developer subagent — **bounded to
   2 retries**, after which escalate to the researcher.
5. Assemble each notebook as a thin orchestration layer calling the verified
   helpers. **Never execute the full notebook.**
6. `b-review` (the existing state) becomes the **final gate**: confirm every
   buildplan helper has a passing test and every notebook is assembled, via
   one closing `kbu-sub-review` pass. Per-notebook review already happened
   inside step 4.

All "stubs only" / "scientific logic belongs to the researcher" language is
**removed**. The researcher's input is solicited only at genuine algorithmic
forks (step 3).

### Module D — `kbu-sub-build` developer subagent (net-new)

The `in_context developer` analog. Inputs: subproject path + one notebook's
buildplan entry. Behavior: write the helper implementations into
`util.py`, write their tests into `test_util.py`, run the fast tests
(`pytest`), iterate until green. Returns a structured work-record, **or** a
BLOCKED signal when it hits a genuine algorithmic decision it cannot make.

**BLOCKED protocol (exact):** the subagent returns a final message beginning
with the literal token `BLOCKED:` followed by the decision statement and a
labelled option list, e.g.:

```
BLOCKED: normalize_fitness — two valid normalizations, plan does not specify.
options:
  A) per-condition z-score
  B) quantile-normalize across conditions
```

The conductor re-dispatches with `DECISION: <chosen option>` appended to the
original prompt. A subagent must use BLOCKED **only** for genuine scientific/
algorithmic forks — never for ordinary coding errors (those are diagnosed and
retried, not escalated).

### Module E — `kbu-sub-review` (modify)

Point its verdict at the **buildplan test cases**: does each helper's test
exist and pass, and are the notebook's named outputs produced/declared? It
writes the verdict file (with the existing `<!-- kbu-review:verdict: pass|fail -->`
marker) that the state gate reads.

### Cross-cutting — mandatory explicit subagent delegation

This is the requirement that prevents regression to inline behavior, and it
applies to **all** of `/kbu-plan`, `/kbu-build`, and `/kbu-migrate`:

- Every delegated step is invoked through an explicit
  `Agent(subagent_type="kbu-sub-…", prompt=…)` call written into the skill at
  the exact point it runs. Never a prose instruction ("review the build") and
  never a `/slash` cross-reference that the model can satisfy inline.
- Each such skill carries a hard rule with the tell-sign:
  *"These steps run as subagents. If you are reading papers, writing review
  prose, building notebook code, or diagnosing a failure in the main thread,
  STOP — you skipped the subagent."*
- **Closed-loop wiring:** the skill spawns the reviewer subagent, the reviewer
  writes the verdict file, and the skill confirms a `pass` verdict file exists
  on disk before calling `kbu subproject advance`. The gate cannot be
  satisfied by an inline hand-written verdict because the skill never writes
  that file itself.

### Test layout

Helper tests live in `subprojects/<name>/notebooks/test_util.py`, co-located
with `util.py`, importable, and runnable fast via `pytest`. The
research-project template seeds an empty `test_util.py` alongside `util.py`.

## Testing Decisions

A good test here checks **external behavior of the conductor pipeline and the
one pure-logic module**, not prompt wording.

- **Module A (`kbu buildplan validate`) gets a real unit test** — it is the
  hard gate and the only pure-logic correctness surface. The test feeds the
  validator: a valid buildplan (asserts `ok`); a buildplan with a forward/
  cyclic `depends_on` edge (asserts the specific error); a helper with empty
  `assertions` (asserts rejected); an out-of-enum `data_source` (asserts
  rejected); duplicate slugs and duplicate helper names (asserts rejected);
  and confirms **all** errors surface, not just the first. Prior art: the
  `load_taskplan` / `TaskPlanError` test pattern in AIAssistant, and existing
  pytest tests in `KBUtilLib/tests/`.
- **Modules B–E are skills/prompts**, validated by a dry-run of the flow on a
  throwaway subproject (does `/kbu-plan` emit a passing buildplan? does
  `/kbu-build` produce real, test-passing helpers and assembled notebooks?
  does an injected algorithmic ambiguity produce a BLOCKED escalation? does an
  injected test failure trigger diagnose+retry? is `kbu-sub-review` actually
  spawned and the verdict file written?) — not unit tests.

## Out of Scope

- Cross-family confront for `/kbu-plan`. It is codex/h100-dependent and
  heavyweight; the buildplan is validated mechanically by `kbu buildplan
  validate` and reviewed at `p-review`. (Omission flagged explicitly.)
- Changing the subproject state machine names or transition table. Unchanged.
- Auto-executing the full notebook on real data during build. Stays in `run`.
- The `--scaffold-missing` flag for adopted-branch verify-and-extend mode
  (separate, already-deferred concern).
- Changes to `/kbu-run`, `/kbu-migrate` beyond the cross-cutting subagent
  delegation rule.

## Further Notes

- This is effectively kbutillib-v2.1, building on the registered-and-done
  `kbutillib-v2` PRD. The naming policy (`kbu-*` commands vs `kbu-sub-*`
  subagents in `.claude/agents/`) from v2 is the substrate this relies on.
- Chris's blind-spot note ("trusts status when underlying state is broken")
  is directly served by the closed-loop wiring (verdict file on disk, not a
  status claim) and by build verifying via running tests, not by asserting it
  wrote cells.
- The skills are templates under
  `templates/research-project/.claude/{commands,agents}/` and deploy into each
  research repo via `kbu bootstrap` / `new-project`. Changes land in the
  template; existing bootstrapped repos pick them up via `kbu update`.
