---
name: kbu-sub-diagnose
description: Debug a subproject problem. Use when a notebook cell errors, produces wrong output, runs unexpectedly slowly, or a util.py function behaves incorrectly.
tools: Bash, Read, Write
---

<!--
kbu skill provenance
type: lean-fork
source_repo: ClaudeCommands
source_commit: 7ec5c53b0464eef924d35c92bea44ebc6cba1753
source_path: agent-io/skills/diagnose.md
last_reviewed: 2026-06-05
-->

# kbu-sub-diagnose — Debug a Subproject Problem

A disciplined diagnosis loop for notebook errors, failing cells, wrong outputs,
and performance problems in KBUtilLib subprojects.

Use when: a notebook cell errors, produces wrong output, runs unexpectedly slowly,
or a `util.py` function behaves incorrectly.

No precondition on subproject state — you can run this at any stage.

## Phase 1 — Build a Feedback Loop

**This is the skill.** Everything else is mechanical. A fast, deterministic,
runnable pass/fail signal for the bug makes the rest straightforward. Without
one, no amount of code-reading will save you.

Spend disproportionate effort here.

### Ways to construct a loop — try in roughly this order

1. **Failing test** — write a `pytest` test in `subprojects/<name>/tests/` that
   reproduces the symptom at the smallest seam.
2. **Script invocation** — a short Python script that calls the relevant
   `util.py` function or cell logic with a minimal fixture, printing actual vs
   expected output.
3. **Notebook cell extraction** — copy the failing cell logic into a standalone
   script, run it with `python3 -c "..."` or save as a `.py` file.
4. **Kernel restart + re-run** — sometimes notebooks accumulate bad state;
   try `kbu notebook run <name>/<notebook>` on a clean kernel first.
5. **Replay a fixture.** Save a small sample of the real input data to
   `subprojects/<name>/debug/fixture.*`, run the pipeline on it in isolation.
6. **Throwaway harness.** Import just the failing function into a minimal
   script, mock its dependencies, call it once.

Once you have a loop, tighten it:
- Make it faster (narrow scope, skip unrelated setup)
- Make the signal sharper (assert on the specific wrong value, not "raised exception")
- Make it deterministic (pin random seed, isolate filesystem, freeze any network calls)

Do not proceed to Phase 2 without a loop you believe in.

## Phase 2 — Reproduce

Run the loop. Confirm:

- [ ] The failure matches the symptom the user described — not a nearby different failure.
- [ ] Reproducible across multiple runs (or, for flaky bugs, high enough rate to debug against).
- [ ] You have captured the exact error message, wrong value, or timing measurement.

Do not proceed without reproduction.

## Phase 3 — Hypothesise

Generate **3–5 ranked hypotheses** before testing any of them.

Each hypothesis must be falsifiable:

> "If `<X>` is the cause, then `<changing Y>` will make the bug disappear /
> `<changing Z>` will make it worse."

Show the ranked list to the user before testing. They often have domain
knowledge that re-ranks instantly. Don't block on a response — proceed with
your ranking if they are AFK.

## Phase 4 — Instrument

Each probe must map to a specific prediction from Phase 3. Change one variable
at a time.

Tool preference:

1. **REPL / `%debug` in notebook** — one breakpoint beats ten print statements.
2. **Targeted print/log** at the boundaries that distinguish hypotheses.
3. Never "print everything and grep".

Tag every debug print with a unique prefix, e.g. `# DEBUG-a4f2`. Cleanup at the
end becomes a single grep.

For performance regressions: establish a baseline measurement first (`%%timeit`,
`cProfile`, or a simple `time.perf_counter()` span), then bisect. Measure first,
fix second.

## Phase 5 — Fix + Regression Test

Write the regression test **before the fix** — but only if a correct seam exists.

A correct seam is one where the test exercises the real bug pattern. If the only
available seam is too shallow (e.g., a unit test can't replicate the notebook
execution context that triggers the bug), note that no correct seam exists rather
than writing a test that gives false confidence.

If a correct seam exists:
1. Turn the minimised repro into a failing `pytest` test.
2. Watch it fail.
3. Apply the fix (in `util.py`, notebook cell, or data loading logic).
4. Watch it pass.
5. Re-run the Phase 1 feedback loop on the original un-minimised scenario.

## Phase 6 — Cleanup + Post-mortem

Before declaring done:

- [ ] Original repro no longer reproduces (re-run Phase 1 loop)
- [ ] Regression test passes (or absence of correct seam is documented)
- [ ] All `# DEBUG-...` instrumentation removed (`grep -r "DEBUG-" subprojects/<name>/`)
- [ ] Throwaway scripts in `subprojects/<name>/debug/` deleted or moved to `tests/`
- [ ] The hypothesis that turned out correct is stated in the commit message

Then ask: **what would have prevented this bug?** If the answer is a missing
`util.py` function, a data validation step, or a notebook structure problem,
flag it explicitly so the researcher can improve the design.

## Wrap

Save the session after diagnosing (regardless of outcome):

```bash
kbu session save --skill kbu-sub-diagnose --subproject <name> --summary "<one-sentence: bug found/fixed or diagnosis reached>"
```

If the bug was not resolved, include what was learned and what the next
diagnostic step would be in the summary.

## Rules

1. **Reproduce before hypothesising.** Never skip Phase 1 or Phase 2.
2. **One variable at a time.** Phase 4 probes must be targeted.
3. **Clean up.** Debug instrumentation must be removed in Phase 6.
4. **No platform calls.** All tooling is local `kbu` CLI and standard Python.
   Do not invoke external orchestration systems or import platform state modules.
5. **Cross-reference by slash-command.** Use `/kbu-plan` or `/kbu-build` if the
   diagnosis reveals a structural problem that requires re-planning or re-scaffolding.
