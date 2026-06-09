# KBUtilLib 2.0 — research-project layout, plan workflow, and adoption (human prompt)

## The story

KBUtilLib's research-project template (introduced via `kbu new-project` and
`kbu bootstrap`) is now used enough that three structural problems are clear:

1. **The `/kbu-plan` skill is a single grilled pass** that produces one
   `RESEARCH_PLAN.md`. In real practice a good plan needs (a) goals grilled
   first, (b) a focused literature review producing per-topic syntheses,
   (c) a detailed plan grilled against those goals and literature, and
   (d) a task decomposition that feeds `/kbu-build` mechanically. Today
   steps b–d are either ad hoc or absent.

2. **The directory layout puts `data/` inside each subproject**, which
   forces duplication when two subprojects use the same input data and
   blurs the "shared inputs vs subproject scratch" line. Models and
   genomes need the same shared-at-root treatment that data wants.

3. **There is no way to onboard an existing notebook directory into a
   bootstrapped kbu repo.** Chris currently has `notebooks/fitness_loe/`
   sitting inside `ModelingLOE` and no command to convert it into
   `subprojects/fitness_loe/` with the canonical tree. Hand-migration is
   error-prone and won't get done.

A fourth, smaller problem surfaced while discussing these three: the
`.claude/` deployed skill set has no naming distinction between commands
the user invokes (`/kbu-plan`) and helpers called by other skills
(`/kbu-literature-review`, `/kbu-review`, `/kbu-diagnose`). The helpers
also pollute the main context because they run as slash commands in the
parent thread. The fix is structural — helpers move to the subagent
layer with their own context window — and worth landing alongside the
other three.

## What we want

### `/kbu-plan` as a 4-step grilled flow

`/kbu-plan` is still one skill, but internally walks four steps with a
grill at each step (Chris is explicit that step 3, the detailed plan,
must be grilled too — not just step 1):

1. **State goals + grill goals.** No literature, no plan, no tasks —
   just lock the goals.
2. **Literature review.** Invoke the `kbu-sub-literature-review`
   subagent (own context window — research content stays out of the
   primary thread). It writes one `subprojects/<name>/literature/<topic-slug>.md`
   per discrete topic, plus a `literature/index.md` listing topics with
   one-line summaries. The old `subprojects/<name>/references.md` file
   is retired — `literature/index.md` plays the same role with structure.
3. **Detailed plan + grill plan.** Hypothesis, success criteria, methods,
   data inputs, outputs, out-of-scope — all written into
   `RESEARCH_PLAN.md`. The structure is grilled with the user before
   the file is written.
4. **Decompose into tasks.** Populate the manifest's `notebooks: [...]`
   array (slug, purpose, last_run_at=None, modified_since_run=True) and
   render a parallel `TASKS.md` for human readability. The manifest is
   the source of truth that `/kbu-build` consumes.

### Root-level shared dirs + leaner subproject tree

The repo root gains three canonical shared dirs that are referenced by
every subproject: `data/`, `models/`, `genomes/`. These are extensible
via a new `[layout.shared_dirs]` table in `kbu-project.toml` (default
list seeds those three; users add e.g. `proteomes/` later).

The per-subproject tree drops `data/` and `user_data/` (folded into root
`data/`), and shrinks/clarifies to:

```
subprojects/<name>/
  notebooks/              # .ipynb + util.py
  nboutput/               # automated intermediates, gitignored by default
  figures/                # curated final figures, tracked
  literature/             # per-subproject lit syntheses (NEW)
    index.md
    <topic-slug>.md
  .cache/                 # KBase notebook tool cache (NEW, hidden, gitignored)
  sessions/
  RESEARCH_PLAN.md
  TASKS.md                # (NEW, by /kbu-plan step 4)
  REPORT.md               # (later, by /kbu-synthesize)
  kbu-subproject.toml
```

Policy taught explicitly by the skill prompts:

- *Shared products that any subproject would consume* → root `data/`,
  `models/`, `genomes/`, or whatever `[layout.shared_dirs]` lists.
- *Subproject intermediates consumed by later notebooks in the same
  subproject* → `nboutput/`.
- *Curated final figures* → `figures/`.
- *KBase tool fetches and workspace caches (machine-derived, regenerable)*
  → `.cache/`.

A new `kbu migrate` command does opt-in retrofit of existing bootstrapped
repos onto the new layout — explicit, user-driven, prompts for each move.

### `kbu subproject adopt` + `/kbu-migrate`

`kbu subproject adopt <path> --name <name>` runs from inside a kbu-bootstrapped
project to onboard an existing notebook directory. CLI is mechanical;
the agent does the integration.

The CLI:
1. Runs six pre-flight refusal checks (destination doesn't already exist,
   source exists and is a directory, source doesn't overlap destination,
   cwd is bootstrapped, source is not tracked by a *different* git repo,
   warn on zero-notebooks).
2. `mv <path>` → `subprojects/<name>/archive/` — single dumb move. No
   inspection, no smart routing. Internal structure preserved under
   `archive/`.
3. Scaffolds the canonical subproject tree alongside `archive/`: empty
   `notebooks/`, `figures/`, `nboutput/`, `.cache/`, `literature/`,
   `sessions/`, and a `notebooks/util.py` stub.
4. Writes `subprojects/<name>/kbu-subproject.toml` with `status=migrate`.
   No `--state` flag — adopted subprojects always land in `migrate`.
   `notebooks: []` is empty (populated by `/kbu-migrate` as it relocates
   files, not by the CLI).
5. Emits `subprojects/<name>/.adoption-notes.md` (gitignored) — the
   agent's worksheet listing notebooks found in archive/, subdirs with
   sizes, oversize files (>10MB), and a grep of in-notebook relative
   path references.

The state machine grows a `migrate` state alongside `plan`. Both transition
to `p-review` with `RESEARCH_PLAN.md` as the precondition. So `/kbu-migrate`
and `/kbu-plan` produce the same artifact contract downstream — `p-review`,
`build`, `run`, `synthesize` don't care which entered the lifecycle.

`/kbu-migrate` is a new skill that does the four design steps (same as
`/kbu-plan`) plus the migration-specific work:

- Reads existing artifacts from `archive/` and `.adoption-notes.md`.
- Infers-then-confirms hypothesis from existing notebooks ("the apparent
  hypothesis is X — confirm or correct") rather than asking blind.
- Path/data relocation pass: walks `.adoption-notes.md`, proposes moves
  for non-notebook content into the canonical tree (data → root `data/`
  or `data/<subproject>/`; figures → subproject `figures/`; intermediates
  → `nboutput/`); rewrites notebook-internal relative paths to match.
- `util.py` audit: scans adopted `util.py` for helpers that overlap
  KBUtilLib modules; proposes deletions/replacements; preserves
  project-specific helpers.
- NotebookSession migration: scans notebook first cells; flags missing
  `NotebookSession.kbu` initialization; recommends rewrites.
- Lit-review topic list seeded from existing notebook subject matter
  rather than freshly grilled.

`/kbu-build` learns a "notebooks already present" branch (no new skill,
just a branch): verify the existing notebooks match the manifest's
`notebooks: [...]` list, add net-new notebooks per the plan, warn if
manifest lists notebooks not present. For migrated projects `/kbu-build`
mostly verifies and standardizes; for virgin projects it scaffolds from
scratch as today.

`archive/` is transient — only created by `adopt`, ideally empty after
migration; user-tracked, not auto-deleted.

### Subagent layer + naming policy

A naming policy distinguishes user-invocable slash commands from
internal helpers:

- **`kbu-*`** = user-invocable slash commands in `.claude/commands/`.
  These are: `kbu-start`, `kbu-plan`, `kbu-build`, `kbu-run`,
  `kbu-synthesize`, `kbu-update`, **and the new `kbu-migrate`**.
- **`kbu-sub-*`** = subagents in `.claude/agents/` with their own
  context window. These are: `kbu-sub-literature-review` (renamed from
  `kbu-literature-review`), `kbu-sub-review` (renamed from `kbu-review`),
  `kbu-sub-diagnose` (renamed from `kbu-diagnose`).

Slash commands invoke subagents via the Agent tool (`Agent(subagent_type=
"kbu-sub-literature-review", prompt=…)`). Research content, review
findings, and diagnosis output stay in the subagent's context and
return to the parent as a short structured summary plus the file paths
written.

The `claude-skills` sync tool gains a `kind: command | agent` frontmatter
field on each source. Sources with `kind: command` land at
`.claude/commands/<name>.md`; sources with `kind: agent` land at
`.claude/agents/<name>.md`. This is the only `claude-skills` change —
inventory, drift, and per-machine deployment logic stay the same.

## Why now

KBUtilLib has just shipped two onboarding-flow PRDs back-to-back
(`kbu-start-v1` 2026-06-05, `kbu-bootstrap-v1` 2026-06-07). The bootstrap
PRD intentionally avoided changing the canonical layout so it could land
cleanly. Now is the time to fold in the structural improvements before
the next wave of bootstrapped repos cements the old layout.

The `ModelingLOE/notebooks/fitness_loe` migration is the specific
adoption case driving Feature 3 — without `kbu subproject adopt` it
would have to be done by hand, and the same hand-migration logic would
repeat for every other research repo Chris brings into the kbu lifecycle.

## Constraints + deferred work

- No backward-compatibility shims for `references.md` or per-subproject
  `data/`. We don't have heavy existing-artifact load (per the
  2026-06-07 session decisions), so retire cleanly.
- `kbu migrate` (the repo-level layout retrofit command) is opt-in only —
  no auto-migration on `kbu update`. Existing repos that don't run
  `kbu migrate` stay on the old layout.
- Skills that aren't being migrated to subagents (`/kbu-start`, `/kbu-plan`,
  `/kbu-build`, `/kbu-run`, `/kbu-synthesize`, `/kbu-update`) get only
  sweep-level updates for new path conventions — no behavior changes
  beyond what each feature requires.
- Confront round is recommended; we'll decide after the PRD is rendered.
