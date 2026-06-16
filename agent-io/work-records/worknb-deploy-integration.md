# Work Record: worknb-deploy-integration

**task_id:** worknb-deploy-integration  
**role:** developer

## branch

- KBUtilLib: `worknb/deploy-integration`
- ClaudeCommands: `worknb/deploy-integration`

## commit_shas

**KBUtilLib:**
- `481640251a7838ea10007a742252c56f5358e405` — feat(worknb): confirm deploy mechanism and add KBUTILLIB_CLAUDECOMMANDS_ROOT override

**ClaudeCommands:**
- `326cb2940c4d2619352376aa85f18699d87928e0` — feat(registry): add work-notebook skill collection to skill_registry.json

## summary

Completed the final integration task for the work-notebooks PRD. (1) Added a
`work-notebook` collection entry to `ClaudeCommands/state/skill_registry.json`
documenting that the bundle contains exactly `jupyter-dev`, `kbu-run`, and
`synthesize`. (2) Confirmed via source code analysis that `claude-skills sync-repos`
cannot target an arbitrary newly-created repo path — it requires the target to
be registered in `AIAssistant/state/project_registry.yaml` AND the skill's
`deploys_to_repos` to include the repo name; neither condition holds at deploy
time for a fresh work-notebook repo. The direct-copy fallback in
`_deploy_bundle()` is therefore the permanent deployment strategy.
(3) Added a `KBUTILLIB_CLAUDECOMMANDS_ROOT` env var override to
`notebook_init.py` so the ClaudeCommands source can be pointed at any checkout
(including a `main`-branch worktree) for testing, and updated the test fixtures
accordingly (all 44 tests pass). (4) Ran a live E2E verification on a throwaway
temp dir: the created repo's `.claude/commands/` contained exactly the three
work-notebook skills and zero BERIL skills; the BERIL repo's `.claude` was
unchanged before/after. (5) Wrote a verification note at
`agent-io/prds/work-notebooks/deploy-verification.md`.

## files_touched

**KBUtilLib:**
- `src/kbutillib/cli/notebook_init.py` — added `KBUTILLIB_CLAUDECOMMANDS_ROOT`
  env var override (`_claudecommands_root()` function), renamed
  `_CLAUDECOMMANDS_ROOT` to `_CLAUDECOMMANDS_ROOT_DEFAULT`, updated docstrings
  to document the deploy mechanism decision and env var
- `tests/cli/test_notebook_init.py` — updated `no_claudecommands` and
  `fake_claudecommands` fixtures to use `monkeypatch.setenv` (env var) instead
  of patching the now-removed module constant
- `agent-io/prds/work-notebooks/deploy-verification.md` — E2E verification note
- `agent-io/work-records/worknb-deploy-integration.md` — this file

**ClaudeCommands:**
- `state/skill_registry.json` — added `collections.work-notebook` entry
- `agent-io/work-records/worknb-deploy-integration.md` — repo-local work record

## success_criteria_check

| Criterion | Status | Justification |
|-----------|--------|---------------|
| `work-notebook` collection defined in ClaudeCommands containing exactly jupyter-dev/kbu-run/synthesize | PASS | `collections.work-notebook.skills` in `skill_registry.json` |
| Confirmed exact `claude-skills` invocation for arbitrary repo deploy | PASS | Confirmed: `sync-repos` cannot do it; documented why |
| `notebook-init` deployment uses confirmed invocation or correct direct-copy fallback | PASS | Direct-copy kept; `_deploy_bundle()` is isolated; source path correct |
| Direct-copy hard-limited to exactly the three skills (no BERIL) | PASS | `_WORKNB_BUNDLE = ("jupyter-dev", "kbu-run", "synthesize")` enforces the allowlist |
| E2E: `.claude/commands` contains exactly jupyter-dev, kbu-run, synthesize | PASS | Verified; see verification note |
| E2E: No BERIL skills present | PASS | Verified; zero BERIL skills in test repo `.claude` |
| E2E: No BERIL repo's `.claude` altered | PASS | BERIL-research-observatory `.claude/skills` unchanged (before/after diff clean) |
| E2E temp repo cleaned up | PASS | `rm -rf /tmp/worknb-e2e-deploy` |
| Verification note written | PASS | `agent-io/prds/work-notebooks/deploy-verification.md` |

## tests_run

```
cd /Users/chenry/.maestro/worktrees/deploy-integration-kbu
PYTHONPATH=src python3 -m pytest tests/cli/test_notebook_init.py -v

Result: 44 passed in 0.98s
```

Live E2E:
```
PYTHONPATH=/Users/chenry/.maestro/worktrees/deploy-integration-kbu/src \
KBUTILLIB_CLAUDECOMMANDS_ROOT=/Users/chenry/.maestro/worktrees/deploy-integration-cc \
python3 -m kbutillib notebook-init /tmp/worknb-e2e-deploy --project "test notebook"

Result: exit 0, all structure assertions pass (see deploy-verification.md)
```

## caveats

- The `collections` key in `skill_registry.json` is informational — no
  `claude-skills` subcommand reads or acts on it. It is a documentation
  record, not a functional hook.

- `claude-skills sync-repos` is NOT the deployment mechanism and cannot become
  one without (a) a new `--path` flag or (b) requiring every work-notebook repo
  to be pre-registered in `project_registry.yaml` before `notebook-init` runs.
  Neither constraint is acceptable per the PRD's portability requirement (AC 13).

- The `KBUTILLIB_CLAUDECOMMANDS_ROOT` env var applies only to the direct-copy
  path. If `claude-skills` ever gains an `--path` flag for `sync-repos`, the
  `_deploy_bundle()` isolation makes it easy to add that path.

- The E2E was run against a ClaudeCommands `main` worktree (not the Dropbox
  `wip` tree) because the `wip` tree does not yet have `kbu-run.md` or
  `synthesize.md`. In production (once `main` merges to `wip`), the default
  `~/Dropbox/Projects/ClaudeCommands` path will have all three files.

- The project_id in the E2E output shows `worknb-worknb-e2e-deploy` (double
  prefix) because the test repo was named `worknb-e2e-deploy`. Real repos will
  not have `worknb-` in their basename; the generated id will be of the correct
  form `worknb-<repo_basename>`.
