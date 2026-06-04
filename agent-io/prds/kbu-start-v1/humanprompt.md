# kbu-start v1 — student-facing notebook workflow

## Problem Statement

Students attempting to use KBUtilLib to build analysis notebooks face a steep
on-ramp. KBUtilLib has two existing skills (`kbutillib-expert`,
`kbutillib-dev`) but both assume the user already knows what they're doing —
neither is a *front door*. Students don't know:

- where to start (no canonical workflow);
- how to set up the environment (venvman, jupyter, KBUtilLib editable install);
- how to structure their work so it's organised, reproducible, and reviewable;
- when to plan vs. execute vs. interpret (no staging discipline);
- how to use Claude Code effectively in the notebook context (no project-local
  slash commands tailored to the science workflow).

The result is wasted time, ad-hoc directory layouts, notebooks that mix
exploration and production, and a barrier to inviting collaborators (students,
postdocs) into KBUtilLib-based work.

Chris also wants a distributable system: a student should be able to clone
KBUtilLib, run `claude`, type one command, and get oriented — without needing
ClaudeCommands installed or any of the AIAssistant orchestration platform.

## Solution

A two-tier skill system shipped *inside KBUtilLib's own `.claude/`* (violating
the usual ClaudeCommands sync policy on purpose — KBUtilLib is a distributable
module):

**Tier 1** — `/kbu-start` lives in `KBUtilLib/.claude/commands/`. Run from
inside the KBUtilLib repo. Four menu items:

1. **Help** — explains KBUtilLib, the notebook workflow, and how the tier-2
   project model works.
2. **Initialize** — first-time machine setup. Creates a venv (venvman if
   present; else `.venv`), installs KBUtilLib editable, checks Cursor + the
   Claude extension, gives instructions if either is missing.
3. **New project** — creates a fresh student project at a path the user
   specifies. Copies a template tree, creates a per-project venv with
   KBUtilLib editable-installed from this source path, drops in a Cursor
   workspace file, gives the student exact instructions to open it and run
   `/kbu-start` there.
4. **Update** — pull updates into the local KBUtilLib install.

**Tier 2** — `/kbu-start` lives inside each student project (deployed by tier-1
"New project"). Run from inside the student project. Status-aware menu that
shows recent sessions and gates options against the current subproject state:

1. **Help** — workflow + subproject layout primer.
2. **Plan** (state: `plan`) — lean fork of `/ai-design` that writes a
   `RESEARCH_PLAN.md` into the subproject and advances state to `p-review`.
3. **Build** (state: `build`) — lean fork of `/ai-conductor` that scaffolds
   notebooks per the plan; advances to `b-review`.
4. **Run** (state: `run`) — deliberate notebook dashboard. Scans subprojects
   for notebooks, shows last-run timestamps + a ⚠ flag when the notebook has
   been modified since last execution. Student picks a notebook to run; Claude
   walks through cell-by-cell, narrating key results. On completion, advances
   to `synthesize`.
5. **Synthesize** (state: `synthesize`) — harvested from BERIL `/synthesize`.
   Reads notebook outputs, cross-references literature, drafts `REPORT.md`.
   Advances to `s-review`.
6. **Review** (states: `p-review` / `b-review` / `s-review`) — harvested from
   BERIL `/berdl-review`. Stage-aware independent review of plan / build /
   report. On pass: advances forward. On fail: returns to prior action state.
7. **Literature review** (any state) — harvested from BERIL
   `/literature-review`. Searches PubMed/bioRxiv/arXiv/Semantic Scholar with
   citation snowballing; appends to `references.md`.
8. **Diagnose** (any state) — lean fork of `/diagnose` for student debugging.
9. **Update** (any state) — pulls skill + template updates from the parent
   KBUtilLib install path (recorded in `kbu-project.toml` at student-repo
   creation). Clobber-with-warn for skill files.

**Subproject state machine** (linear, stored in
`subprojects/<name>/kbu-subproject.toml`):

```
plan → p-review → build → b-review → run → synthesize → s-review → complete
```

State transitions happen only via the `kbu` CLI. Each tier-2 skill calls
`kbu subproject advance <name>` at the end of its work. Review fails route
backward to the prior action state. Skills cannot be invoked in invalid
states — the menu greys them out.

**Subproject layout convention** (formalised in jupyter-dev too, even when
only one subproject exists):

```
subprojects/<name>/
├── kbu-subproject.toml       # status, authors, artifacts, notebook-tracking
├── RESEARCH_PLAN.md          # written by Plan
├── REPORT.md                 # written by Synthesize
├── REVIEW_<stage>_<n>.md     # written by Review
├── notebooks/
│   ├── 01_*.ipynb, 02_*.ipynb, ...
│   ├── util.py               # subproject-local helpers
│   └── nboutput/             # figures, intermediate data
├── data/                     # agent-derived data
├── user_data/                # student-supplied input
├── figures/                  # publication-ready outputs
├── references.md             # appended by Literature review
└── sessions/                 # local session YAMLs (when AIAssistant absent)
```

No root-level `util.py` or `nboutput/` directly under `notebooks/` — always
nested through a subproject.

**Sessions**: a `kbu session save` CLI command auto-routes:
- if `~/Dropbox/Projects/AIAssistant/state/sessions.db` exists, sessions go
  there (via `assistant.state.save_session`), with the subproject
  auto-registered as a kbu project in AIAssistant on first call;
- else, sessions are written as YAML to
  `subprojects/<name>/sessions/<timestamp>-<skill>.yaml`.

The tier-2 dashboard surfaces recent sessions in either mode (same UX).

**Distribution model**: KBUtilLib's `.claude/` is checked in. Students clone
the repo and the skills are immediately available — no ClaudeCommands sync
required. The template tree at `KBUtilLib/templates/student-project/` is
copied verbatim by `kbu new-project`, so derived student repos also work
zero-setup beyond `kbu init`.

## User Stories (selected)

1. As a new student, I want to clone KBUtilLib, run `claude` inside, type
   `/kbu-start`, pick "Initialize", and get a working environment without
   reading any docs.
2. As a new student, I want `/kbu-start` → "New project" to ask me for a
   project name and target path, then hand me back a path I can open in
   Cursor with a working venv and a `/kbu-start` already inside that project.
3. As a student starting an analysis, I want `/kbu-start` → "Plan" to grill
   me on my research question, write `RESEARCH_PLAN.md`, and refuse to let
   me jump to "Build" until I've reviewed the plan.
4. As a student who's run their notebooks, I want `/kbu-start` → "Run"
   to show me which notebooks have been modified since last run so I don't
   accidentally interpret stale outputs.
5. As a student who's debugging, I want `/kbu-start` → "Diagnose" to walk a
   structured reproduce-minimise-fix loop without pulling in the heavy
   AIAssistant infrastructure.
6. As a student who finishes an analysis, I want `/kbu-start` → "Synthesize"
   to read my notebook outputs and draft `REPORT.md` interpreting them.
7. As a student updating to a new KBUtilLib release, I want `/kbu-start` →
   "Update" to pull skill updates and warn me before clobbering anything.
8. As Chris using KBUtilLib himself, I want the tier-2 skills to be tight
   enough that I use them for my own subproject work in
   ADP1Notebooks / SalternsNotebooks / ModelingLOE.
9. As a tier-2 user, I want sessions from each `/kbu-*` skill to be saved
   automatically so my next session resumes where I left off.
10. As Chris on his primary-laptop, I want tier-2 sessions to route to
    AIAssistant's `state/sessions.db` so they show up in the regular
    AIAssistant dashboard.
11. As a tier-2 user, I want `/kbu-start` to refuse "Synthesize" if there's
    no `RESEARCH_PLAN.md` (state ≠ `synthesize`), instead of producing
    fabricated interpretations.
12. As a tier-2 user, I want "Review" to be stage-aware: reviewing a plan
    vs. a report should know what it's looking at and apply different
    criteria.
13. As a student new to notebook design, I want `/kbu-start` → "Literature
    review" to search the major bio-literature databases and add useful
    references to my `references.md`.
14. As a Cursor user, I want `/kbu-start` → "Initialize" to detect whether
    Cursor and the Claude extension are installed, and give me the exact
    install commands if not.

## Implementation Decisions

See `fullprompt.md` for the canonical decision list.

## Testing Decisions

See `fullprompt.md`.

## Out of Scope

- Auto-running notebooks during Build or Plan (the
  "don't auto-run notebooks" guidance still applies; only Run executes cells).
- Pitfall capture as a cross-cutting skill (deferred).
- Per-project `template_version` migration support (defer until first
  breaking template change forces it).
- `kbu update --from-github` fallback (deferred to v2; until then,
  `kbu update --set-source <path>` covers moved repos).
- Vendoring KBUtilLib into the student project (always editable-installed
  from the parent source path).
- Cursor auto-opening the Claude panel on workspace open (gave up; just
  give the student instructions).
- Tier-2 skills depending on AIAssistant `assistant.state` imports for
  anything other than session routing (must be file-based for portability).
- Backporting state-machine support to AIAssistant's own PRD lifecycle.
