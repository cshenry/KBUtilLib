# KBU Conductor Pipeline (human prompt)

## The story

I tested the v2 research pipeline — `/kbu-plan` then `/kbu-build` on a
subproject — and the build command just stubbed everything out. It wrote
`NotImplementedError` function bodies and `# TODO` notebook cells and stopped.
That's useless to me: the whole point is that the AI writes the code and I
explain what code I want, not that I get handed empty scaffolding to fill in.

The deeper issue is that `/kbu-plan` and `/kbu-build` were supposed to be the
research-repo versions of `/ai-design` and `/ai-conductor` — their provenance
headers even say so — but the fork lost the substance. `/ai-conductor` works
because it takes a real plan with per-task success criteria and actually
implements, reviews, and verifies until the code works. The kbu build does
none of that, and the kbu plan doesn't produce a rich enough plan for it to,
even if it wanted to.

I want them made truly parallel:

1. **`/kbu-plan` should produce a plan as rigorous as `/ai-design`** —
   grilled goals, a literature review, a grilled detailed plan, and then a
   grill where we **define exactly what the test cases should be** for each
   component of the project. The tests are the contract.

2. **`/kbu-build` should conduct, not scaffold** — write all the real code,
   verify it, and only stop to ask me when it hits a genuine algorithmic
   decision it can't make on its own. Because this is science, it won't be as
   fully autonomous as the software conductor — it'll sometimes need my input
   — but the default is that it writes everything.

The constraint I know from doing this before with Claude Code: you can't run
the whole notebook in the build loop, it's too slow. What works is building
the logic as helper functions and writing fast tests against them (on small or
synthetic data), running those tests, and assembling the notebook from the
verified pieces. The full real-data run happens later, when I drive it.

3. **The commands have to actually use the subagents.** Last time, the build
   and plan commands didn't realize they were supposed to call a subagent for
   things like the review — they just did it inline (or skipped it). The new
   versions need to be unmistakable that review, literature, per-notebook
   build, and diagnosis all run as subagents, with the explicit call written
   right where it happens.

And yes — write a unit test for the buildplan validator (Part A). That's the
one piece with real logic to get wrong.
