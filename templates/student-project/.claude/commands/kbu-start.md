<!--
kbu skill provenance
type: net-new
created: 2026-06-05
-->

# /kbu-start — project dashboard

Use this command as your entry point each time you open the project. It shows
current subproject state and recent sessions, then offers a navigation menu so
you can jump directly to the right workflow step.

## Step 1 — load the dashboard

Run both queries in parallel:

```bash
kbu subproject list --json
kbu session list --limit 5 --json
```

Parse the JSON output from each command.

### Render: subprojects table

Print a Markdown table from the `kbu subproject list` output:

| Subproject | Status | Next action |
|------------|--------|-------------|
| ... | ... | ... |

If the JSON array is empty, note: "No subprojects yet. Use the **Help** option
to learn how to create one."

### Render: recent sessions table

Print a Markdown table from the `kbu session list` output:

| Session ID | At | Subproject | Skill | Summary |
|------------|----|------------|-------|---------|
| ... | ... | ... | ... | ... |

If the array is empty, note: "No sessions recorded yet."

## Step 2 — determine menu item availability

For each subproject returned by `kbu subproject list`, fetch its detailed
state:

```bash
kbu subproject status <name> --json
```

Use the returned `status` field to gate the menu items below. When no
subprojects exist at all, every workflow action (Plan through Diagnose) is
disabled with reason `no-subprojects`.

### Disabled-item reason strings (canonical)

The following five reason strings are used verbatim in disabled menu labels:

- `wrong-state` — the action is defined but the subproject's current state
  does not match the prerequisite state for this skill.
- `missing-artifact` — a required artifact (research plan, notebooks, report)
  does not yet exist in the subproject directory.
- `no-subprojects` — no subprojects have been created in this project yet.
- `notebooks-stale` — at least one notebook has never been run, or has been
  modified since its last run; the run step is not complete.
- `review-pending` — a review file exists but no passing verdict has been
  recorded yet (`<!-- kbu-review:verdict: pass -->`).

### Gating rules per menu item

Apply these rules across all subprojects. If any subproject satisfies the
action's state prerequisite, the item is enabled.

| Menu item | Enabled when any subproject is in state(s) |
|-----------|---------------------------------------------|
| Plan | `plan` |
| Build | `build` |
| Run | `run` |
| Synthesize | `synthesize` |
| Review | `p-review`, `b-review`, or `s-review` |
| Literature review | any state (always available if subprojects exist) |
| Diagnose | any state (always available if subprojects exist) |
| Update | always available (no subproject state required) |
| Help | always available |

When an item is disabled, append the reason in the label:

```
Run (not available: wrong-state)
Plan (not available: no-subprojects)
Build (not available: missing-artifact)
Synthesize (not available: notebooks-stale)
Review (not available: review-pending)
```

Use the most specific reason: if no subprojects exist, use `no-subprojects`
for all workflow actions. If subprojects exist but none is in the matching
state, use `wrong-state`. If a subproject is in the right state but a
precondition artifact is absent, use `missing-artifact`. For the run→synthesize
transition, use `notebooks-stale` when notebooks have not all been run cleanly.
For review transitions, use `review-pending` when no passing verdict file
exists yet.

## Step 3 — present the menu

Use AskUserQuestion with the following items (including availability labels
derived above):

```
Help
Plan          (or: Plan (not available: <reason>))
Build         (or: Build (not available: <reason>))
Run           (or: Run (not available: <reason>))
Synthesize    (or: Synthesize (not available: <reason>))
Review        (or: Review (not available: <reason>))
Literature review  (or: Literature review (not available: <reason>))
Diagnose      (or: Diagnose (not available: <reason>))
Update
```

## Step 4 — route to the selected skill

When the student picks an enabled item, invoke the corresponding slash command:

| Pick | Action |
|------|--------|
| Help | Explain the kbu workflow briefly in plain language, then re-show the menu. |
| Plan | `/kbu-plan` |
| Build | `/kbu-build` |
| Run | `/kbu-run` |
| Synthesize | `/kbu-synthesize` |
| Review | `/kbu-review` |
| Literature review | `/kbu-literature-review` |
| Diagnose | `/kbu-diagnose` |
| Update | `/kbu-update` |

When the student picks a disabled item, explain why it is unavailable (using
the reason string and the subproject's current state), then re-display the
dashboard menu so they can pick a valid action instead.

## Notes for Help text

The kbu workflow follows this sequence per subproject:

1. **Plan** — write a research plan (`RESEARCH_PLAN.md`).
2. **Review (plan)** — a reviewer approves the plan.
3. **Build** — create analysis notebooks under `subprojects/<name>/notebooks/`.
4. **Review (build)** — a reviewer approves the notebooks.
5. **Run** — execute notebooks via `/kbu-run`; all must run clean.
6. **Synthesize** — write `REPORT.md` summarising results.
7. **Review (synthesis)** — a reviewer approves the report.
8. **Complete** — subproject is done.

Use `kbu subproject create <name>` to add a new subproject.
