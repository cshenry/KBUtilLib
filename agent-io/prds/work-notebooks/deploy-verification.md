# Work-Notebooks Deploy Integration — Verification Note

**Task:** worknb-deploy-integration  
**Date:** 2026-06-16  
**Branch (KBUtilLib):** worknb/deploy-integration  
**Branch (ClaudeCommands):** worknb/deploy-integration

---

## 1. claude-skills invocation: confirmed NOT usable for arbitrary paths

After inspecting `claude_skills/repo_sync.py` and running `claude-skills sync-repos --help`:

`claude-skills sync-repos` deploys skills that have `deploys_to_repos` set in
`state/skill_registry.json` into repos registered in
`AIAssistant/state/project_registry.yaml`. It resolves target repo paths by:
1. Reading the AIAssistant project registry (`project_registry.yaml`)
2. Looking up each repo by name in that registry
3. Deploying only skills whose `deploys_to_repos` list includes that repo name

For a freshly created work-notebook repo at deploy time:
- The repo is not yet in `project_registry.yaml` (it may be registered in the
  same `kbu notebook-init` run, but after the deploy step)
- The three work-notebook skills (`jupyter-dev`, `kbu-run`, `synthesize`) have
  empty `deploys_to_repos` lists in the registry

**Conclusion:** `claude-skills sync-repos` cannot target an arbitrary path. The
direct-copy fallback is the **permanent deployment strategy**, not a temporary
workaround. No `claude-skills` invocation can replace it for this use case.

Equivalent (non-functional) invocation for reference only — this would NOT work
for a newly created repo:
```
claude-skills sync-repos --repo <repo-name> --apply
```

## 2. Deployment implementation: confirmed correct direct-copy

`kbu notebook-init` deploys via `_deploy_bundle()` in
`src/kbutillib/cli/notebook_init.py`. The function:
- Reads the three skill files from `ClaudeCommands/agent-io/skills/`:
  - `jupyter-dev.md`
  - `kbu-run.md`
  - `synthesize.md`
- Copies each to `<repo_root>/.claude/commands/`
- Also copies companion `<skill>/` context directories if present
- **Hard-limits** to exactly `_WORKNB_BUNDLE = ("jupyter-dev", "kbu-run", "synthesize")`
  — no BERIL skills can enter

Adjustment made in this task: added `KBUTILLIB_CLAUDECOMMANDS_ROOT` environment
variable to override the ClaudeCommands root path (default:
`~/Dropbox/Projects/ClaudeCommands`). This makes the deployment testable against
a specific git worktree (e.g., one where `kbu-run.md` and `synthesize.md` exist
on `main` before they land on `wip`).

## 3. Work-notebook collection definition

A `collections.work-notebook` entry was added to
`ClaudeCommands/state/skill_registry.json` (on branch
`worknb/deploy-integration`) containing exactly:
- `jupyter-dev`
- `kbu-run`
- `synthesize`

The `deploy_mechanism` field records `"direct-copy"` with a note explaining why
`claude-skills sync-repos` is not used.

## 4. End-to-end verification result

**Command run:**
```bash
PYTHONPATH=/Users/chenry/.maestro/worktrees/deploy-integration-kbu/src \
KBUTILLIB_CLAUDECOMMANDS_ROOT=/Users/chenry/.maestro/worktrees/deploy-integration-cc \
python3 -m kbutillib notebook-init /tmp/worknb-e2e-deploy --project "test notebook"
```

**Output summary:**
```
[info] Topic normalized: 'test notebook' -> 'test_notebook'
Creating new work-notebook repo at /private/tmp/worknb-e2e-deploy ...
  git init: /private/tmp/worknb-e2e-deploy
  Created: worknb-e2e-deploy.code-workspace
  Bundle deployed: jupyter-dev, kbu-run, synthesize -> /private/tmp/worknb-e2e-deploy/.claude/commands
  Created: notebooks/models/
  Created: notebooks/genomes/
  Created: notebooks/data/
  Created: notebooks/PRJ-test_notebook/
  Updated: .gitignore (work-notebook block)
  Registry: attached to existing entry 'worknb-worknb-e2e-deploy'
  Wrote: notebooks/.kbu-run.json (project_id='worknb-worknb-e2e-deploy')
Done.
```

**Assertions verified:**

| Assertion | Result |
|-----------|--------|
| `.claude/commands` contains exactly `jupyter-dev.md`, `kbu-run.md`, `synthesize.md` | PASS |
| No BERIL skills present (`kbu`, `kbu-notebook`, `kbu-fba`, `kbu-start`, `kbu-migrate`, `kbu-sub-*`) | PASS |
| `notebooks/.kbu-run.json` present with `project_id` field | PASS |
| `.gitignore` contains marker block with `notebooks/PRJ-*/NBCache/`, `notebooks/PRJ-*/NBOutput/`, `.ipynb_checkpoints/` | PASS |
| `notebooks/PRJ-test_notebook/util.py` created | PASS |
| `BERIL-research-observatory/.claude` unchanged (before/after diff clean) | PASS |

**Temp repo cleaned up:** yes (`rm -rf /tmp/worknb-e2e-deploy`)
