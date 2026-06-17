# kbu-beril-worktree

## Problem Statement

I want to work on several BERIL Research Observatory projects in parallel — one
Claude session on project `foo`, another on `bar`, a third on `baz` — without
them clobbering each other. Today I can't: BERIL uses a branch-per-project model
(each project lives in `projects/<id>/` on its own branch `projects/<id>`), and a
single working tree can only have one branch checked out at a time. Two sessions
in the same directory on different project branches yank files out from under each
other on checkout and collide on the shared git index.

`beril start` does not solve this — it only launches an agent in the current
directory and has no worktree management. Worse, `beril start` runs an
*unconditional release-tag checkout* before launching, which would detach a
worktree off its project branch entirely.

I drive my own work through the Cursor IDE and want the full Claude Code
*extension* (inline diffs, the rich panel) in each parallel session. Other KBase
users run BERIL on cloud JupyterHub with no Cursor at all and launch their agent
with `beril start`. Both audiences need a parallel-session path; the launch
surface differs but the underlying worktree mechanics are identical.

## Solution

Add a `kbu beril worktree` command group that manages one git worktree per
concurrent BERIL project. Each worktree is its own working directory on its own
`projects/<id>` branch, all backed by the primary BERIL checkout's single `.git`.
Because project directories are disjoint, parallel work is conflict-free.

- **`new <id>`** creates (or re-adopts) a worktree under a configurable root,
  symlinks the gitignored env plumbing into it, and writes a per-worktree Cursor
  workspace file.
- **`open <id>`** launches Cursor on that worktree (Claude *extension* per
  window — my flow).
- **`start <id>`** launches a CLI agent in the worktree for JupyterHub users —
  a faithful proxy of `beril start` that skips only the release-checkout.
- **`rm <id>`** removes the worktree *directory only* and keeps the branch in the
  primary repo, so I can recreate it later or open a PR to main from it.
- **`ls`** shows live worktrees and reopenable branches.

Getting a project's work to BERIL `main` stays BERIL's job: recreate a worktree,
open Claude in it, and run `/submit` (the reviewed-PR path). `kbu` never touches
`main`.

## User Stories

1. As a researcher, I want to spawn a new parallel BERIL session for project
   `foo` with one command, so that I can work on `foo` while another session
   works on `bar`.
2. As a researcher, I want each worktree placed under a single configurable root
   directory (`WORKING_BERIL_DIRECTORY`), so that all my parallel sessions live
   in one predictable place.
3. As Chris on primary-laptop, I want that root to default to
   `~/Dropbox/Projects/WorkingBERIL/`, so that my worktrees sync across machines
   and I can run BERIL operations on other machines if needed.
4. As a researcher, I want the worktree root to be a **kbu** setting persisted in
   kbu's own config, so that it is not entangled with BERIL's config.
5. As a researcher, I want `new` to create a fresh `projects/<id>` branch off
   `main` when the project is new, but **adopt** an existing `projects/<id>`
   branch when one already exists, so that recreating a worktree resumes prior
   work instead of erroring.
6. As a researcher, I want every new worktree to have working BERDL access
   immediately, so that I do not hit `Spark Connect server ... is not reachable`
   because the gitignored `.env` and `.venv-berdl/` are missing.
7. As Chris, I want `new --open` (or `open <id>`) to launch Cursor on a
   per-worktree workspace file, so that I get the full Claude Code extension in a
   window rooted at that worktree.
8. As Chris, I want each worktree to open as its **own Cursor window** (one
   window per worktree), so that the Claude extension binds cleanly to a single
   workspace per session.
9. As a JupyterHub user with no Cursor, I want `start <id>` to launch my
   configured agent (claude/codex/gemini) inside the worktree, so that I get a
   parallel session without Cursor.
10. As a JupyterHub user, I want `start` to behave like `beril start` —
    refreshing my `KBASE_AUTH_TOKEN`, applying the shared-Anthropic-key Vertex
    config, defaulting Claude to the opus model, and auto-running
    `/berdl_start` — but **without** the release-tag checkout that would break my
    worktree.
11. As a researcher, I want `rm <id>` to delete only the worktree directory and
    **keep** the `projects/<id>` branch in the primary repo, so that all my
    project branches accumulate in one git and I can reopen any of them later.
12. As a researcher, I want `rm <id>` to be idempotent — a no-op when the worktree
    is already gone (e.g. after BERIL cleared it) — so that teardown never errors
    on an absent worktree.
13. As a researcher, I want `rm <id>` to refuse when the worktree has uncommitted
    changes, with a `--force` to override, so that I never silently lose work.
14. As a researcher, I want `ls` to show both my live worktrees (path + branch)
    and the `projects/*` branches that have no worktree (reopenable), so that I
    can see what I can resume.
15. As a researcher, I want to take a finished project branch to `main` by
    recreating its worktree, opening Claude, and running `/submit`, so that the
    reviewed-PR gate is honored and `kbu` never bypasses review.
16. As a KBase user installing kbu, I want this capability to ship as part of the
    `kbu` CLI I already use, so that I do not need a separate tool.
17. As a maintainer, I want the `start` proxy to *reuse* BERIL's own launch
    helpers rather than reimplement them, so that the proxy tracks upstream BERIL
    behavior and does not silently drift.
18. As a maintainer, I want `kbu beril worktree doctor` to verify that the BERIL
    launch helpers the proxy imports still resolve, so that a BERIL rename surfaces
    as a loud, actionable error rather than a runtime crash.
19. As a researcher, I want a clear warning never to run `beril start` directly
    inside a worktree, so that I do not detach my worktree onto a release tag.
20. As a researcher running heavy Spark scans in two sessions at once, I want to
    understand that compute is shared (slower, not broken), so that I stagger
    large queries deliberately.

## Implementation Decisions

**Home & framework.** Add a nested `@click.group("worktree")` under the existing
`beril_cmd` group in the kbu CLI (`src/kbutillib/cli/beril.py`), registered with
`beril_cmd.add_command(worktree_cmd, name="worktree")`. CLI commands stay thin and
delegate to a new library package.

**Library package — the deep module.** New package `kbutillib/beril_worktree/`,
mirroring the `kbutillib/harness/` precedent (CLI wrapper + library):

- `manager.py` — `BerilWorktree(beril_root: Path, worktree_root: Path)` with the
  small stable interface:
  - `new(project_id, *, from_branch="main", open_cursor=False) -> Path`
  - `remove(project_id, *, force=False) -> bool` (True if something was removed)
  - `list() -> list[WorktreeInfo]` (live worktrees + reopenable `projects/*` branches)
  - `open(project_id) -> Path` (recreate the worktree if missing, return its path)
  - Private helpers: `_add_worktree`, `_symlink_env`, `_write_workspace`,
    `_branch_exists`, `_worktree_path`, `_prune`.
- `launch.py` — the `start` proxy (see "Launch proxy" below).
- `config.py` — root/path resolution (see "Configuration" below).

**Worktree git mechanics.**
- `new`: if branch `projects/<id>` exists → `git worktree add <wt> projects/<id>`
  (adopt); else `git worktree add <wt> -b projects/<id> <from_branch>`. Branching
  off the `main` *ref* works regardless of what the primary checkout currently has
  checked out, so the main checkout being parked on a project branch is not a
  blocker.
- Error clearly if the target worktree dir already exists, or if the requested
  branch is already checked out in another worktree (git refuses).
- `remove`: `git worktree remove <wt>`; **never** `git branch -d` — the branch is
  the durable artifact. Run `git worktree prune` to clear stale admin entries left
  by manual deletions. Idempotent: if the worktree path does not exist (per
  `git worktree list`), return False with a "nothing to remove" message, exit 0.
- `--force` passes `--force` to `git worktree remove` (discards changes).

**The branch lives in primary `.git` the whole time.** Worktrees share one object
store; commits in a worktree advance `projects/<id>` in the primary repo directly.
There is no merge-back step — `rm` keeps the branch; recreation re-adopts it.

**Symlink plumbing (mandatory on every `new`).** Inside the worktree create
`.env → <beril_root>/.env` and `.venv-berdl → <beril_root>/.venv-berdl` as
symlinks. Both match BERIL's `.gitignore` (`.env`, `*.env`, `.venv-berdl/`), so
they are never committed. Symlinking (not copying) means one place to refresh the
auth token and no re-bootstrapping of the heavy venv, which points at the same
shared backend anyway.

**Workspace file (Cursor / Shape B).** Write the per-worktree workspace file at
`<worktree_root>/<id>.code-workspace` — *outside* any git working tree, so BERIL's
tree stays pristine and nothing new appears as untracked. Content mirrors the
existing `BERIL.code-workspace`: a single `folders` entry
`{"name": "BERIL: <id>", "path": "./<id>"}` plus BERIL's settings/extensions
blocks. `python.defaultInterpreterPath` =
`${workspaceFolder}/.venv-berdl/bin/python` resolves per-folder through the
symlink. One workspace file per worktree → one Cursor window per worktree.

**Cursor launch.** `open` / `new --open` run `cursor <worktree_root>/<id>.code-workspace`
via subprocess. If `cursor` is not on PATH, print the workspace path and a manual
"open this in Cursor" instruction (do not fail hard).

**Launch proxy (`start`, JupyterHub) — import, do not reimplement.** kbu is
pip-installed into the BERIL env, so `beril_cli` is importable. The proxy:
- Imports `get_default_agent`, `get_vertex_config` from `beril_cli.config` and
  `_sync_auth_token` from `beril_cli.start`.
- Replicates *only* `run_start`'s thin orchestration shell **minus**
  `_checkout_release`: resolve agent (arg or `get_default_agent()`), `shutil.which`
  the binary, `os.chdir(worktree)`, `_sync_auth_token(worktree/.env)`, the Vertex
  env block when agent is `claude`, the opus-model default for `claude`,
  `/berdl_start` onboard injection unless `--skip-onboard` or the user passed a
  prompt, then `os.execvp`.
- Heavy/secret-bearing logic (token refresh, Vertex key) stays *imported*, so it
  tracks upstream; only ~15 lines of stable glue are replicated.
- Factor the pre-`execvp` assembly into a pure function returning
  `(binary, argv, env_updates)` so it is unit-testable without process
  replacement.

**Drift tripwire.** `kbu beril worktree doctor` asserts `beril_cli` is importable
and that the three borrowed symbols (`get_default_agent`, `get_vertex_config`,
`_sync_auth_token`) resolve; on failure it reports exactly which symbol is missing
and that the kbu proxy needs updating to match BERIL. A unit test asserts the same
imports.

**Configuration.** Persist a `beril` section in kbu's own config
(`~/.kbutillib/config.yaml`, via `SharedEnvUtils`):
```yaml
beril:
  root: /Users/chenry/Dropbox/Projects/BERIL-research-observatory
  worktree_root: /Users/chenry/Dropbox/Projects/WorkingBERIL
```
Resolution order, each independently:
- `beril_root`: `--beril-root` flag > env `BERIL_ROOT` > config `beril.root` >
  error with guidance.
- `worktree_root`: `--root` flag > env `WORKING_BERIL_DIRECTORY` >
  config `beril.worktree_root` > default `<beril_root>/../WorkingBERIL` (a sibling,
  which is exactly Chris's path and sane for any user).
- `kbu beril worktree set-root <path>` (and `--beril-root` on it) persists these to
  kbu config. Never write into BERIL's `~/.config/beril/config.toml`.

**Confront round 1 resolutions (folded — all binding).**
- **CLI option placement:** `--beril-root PATH` and `--root PATH` (alias
  `--worktree-root`) are group-level options on `kbu beril worktree`, available to
  every subcommand. `set-root` accepts both. `start` accepts `--agent NAME` and
  `--skip-onboard` and forwards everything after `--` verbatim to the agent.
- **Exact paths:** the worktree directory for `<id>` is exactly
  `<worktree_root>/<id>`; the branch is exactly `projects/<id>`.
- **Project-ID validation:** `<id>` must match `[A-Za-z0-9._-]+`; reject slashes
  and anything else with a clear error (branch-name + path safety).
- **Workspace content source:** copy the `settings` and `extensions` top-level
  keys from `<beril_root>/BERIL.code-workspace` when present; if it is missing,
  write `{"folders":[{"name":"BERIL: <id>","path":"./<id>"}],"settings":{},"extensions":{}}`.
- **`open` when branch missing:** error and direct the user to `new <id>`; `open`
  only recreates the *worktree directory* for an existing branch, it never creates
  a branch.
- **Missing symlink targets:** always create the symlinks; if `<beril_root>/.env`
  or `.venv-berdl` is absent, warn and continue (non-fatal).
- **Pre-existing directory on `new`:** if `<worktree_root>/<id>` exists and is not
  a registered git worktree, abort and change nothing.
- **`rm` when unregistered:** if the path is not in `git worktree list`, print
  "nothing to remove", delete no files, return False, exit 0 (idempotent; never
  delete an untracked folder).
- **`ls` output schema:** human-readable two sections by default; `--json` emits a
  stable array `[{"id","branch","path","live"}]` sorted by `id`.
- **`start` argv/env contract (corrects the adversary's suggestion):** assemble
  the agent command with NO release checkout. For `claude`, append `--model opus`
  when the user did not pass `--model`, and set the initial prompt to
  `/berdl_start` when no prompt was passed and `--skip-onboard` is false. Apply
  the Vertex environment by replicating `run_start`'s **exact env-key mapping**
  (the specific keys it sets — `CLAUDE_CODE_USE_VERTEX`, `CLOUD_ML_REGION`,
  `ANTHROPIC_VERTEX_PROJECT_ID`, `GOOGLE_APPLICATION_CREDENTIALS`, and the
  Haiku-model keys), gated on `get_vertex_config()["enabled"]`, only for `claude`.
  Do **not** `os.environ.update(get_vertex_config())` — that dict is config, not
  env vars. Refresh the token via the imported `_sync_auth_token` before launch.
- **`doctor` exit codes:** 0 on success; 1 when `beril_cli` import fails or any
  borrowed symbol is missing (naming it); never 2+.
- **`doctor` symlink check (adopted from free critique):** also report whether each
  configured worktree's `.env`/`.venv-berdl` symlink targets exist and are
  readable, so a stale link surfaces early.
- **`set-root` normalization:** expand `~`, resolve to absolute paths, and create
  `~/.kbutillib/config.yaml` and its parent directory if missing before writing.
- **`beril start` warning placement:** print the "never run `beril start` inside a
  worktree" warning after a successful `new`, `open`, and `start`.
- **Git scoping:** every git command runs with `git -C <beril_root>`; never rely on
  the current working directory.

## Testing Decisions

Test external behavior against a scratch temp git repo (init, commit on `main`,
seed `projects/<id>/` dirs), not git internals. Mirror the existing
`kbutillib/harness/` test suite and the `cli/beril.py` install/doctor tests as
prior art.

- **Manager core:** `new` creates the worktree + `projects/<id>` branch off main;
  `new` on an existing branch adopts it (no `-b`, no error); `remove` deletes the
  directory and the branch still exists afterward; `remove` is idempotent (second
  call no-ops, exit 0); `remove` refuses on a dirty worktree and `--force`
  overrides; `.env`/`.venv-berdl` symlinks are created and are gitignored; the
  workspace file is well-formed JSON, lives outside the worktree, and points its
  folder at `./<id>`.
- **Config resolution:** flag > env > config > default precedence for both
  `beril_root` and `worktree_root`; `set-root` persists to `~/.kbutillib/config.yaml`
  and not to BERIL's config.
- **`ls` parsing:** parse canned `git worktree list --porcelain` output plus
  `git branch --list 'projects/*'` into live vs reopenable sets.
- **Launch proxy:** the *import-resolution guard* (the drift tripwire) — assert
  `beril_cli` symbols import; the argv/env assembly function omits any checkout,
  includes the opus default and the `/berdl_start` onboard, and respects
  `--skip-onboard`. Do **not** test `os.execvp` itself (process replacement).
- **doctor matrix:** `beril_cli` importable vs not; each borrowed symbol present
  vs renamed.

Skip testing actual Cursor/agent process launch; assert the assembled command and
the not-on-PATH fallback message instead.

## Out of Scope

- **Any BERIL-repo change.** The release-checkout fix lives entirely in the kbu
  proxy; `beril start` is not modified (the BERIL-side `--no-checkout`/auto-detect
  guard was explicitly rejected to avoid a shared-repo PR).
- **Merging project branches to `main`.** BERIL's `/submit` reviewed-PR path owns
  it; `kbu` never touches `main`.
- **Cross-machine worktree orchestration.** Dropbox sync moves files; no active
  coordination, locking, or remote launch is built.
- **Spark/BERDL contention management.** Provide documented guidance (stagger
  heavy scans); do not build scheduling.
- **IDEs other than Cursor/VS Code.** The `.code-workspace` file serves both;
  other editors are unsupported.
- **A multi-root single-window mode (Shape A).** Decided against; one window per
  worktree only.

## Further Notes

- **Dropbox conflict risk is accepted.** Chris consciously keeps `WorkingBERIL/`
  on Dropbox for cross-machine BERIL operations, accepting potential conflict
  copies; the heavy `.venv-berdl` is symlinked, not synced.
- **`beril start` hazard.** Running `beril start` inside a worktree triggers
  `_checkout_release` (`git checkout <release-tag>`), detaching the worktree off
  `projects/<id>`. Document prominently: use `kbu beril worktree start` instead.
- **Auth-token freshness is shared.** Because each worktree's `.env` is a symlink
  to the main checkout's, refreshing the token once (main checkout `beril start`,
  or the proxy's `_sync_auth_token`) updates all worktrees at once.
- **Spark Connect is one per-user server.** Parallel heavy queries contend on the
  same Spark cluster → slowdowns, not failures.
- **Grounding:** `~/Dropbox/Projects/BERIL-research-observatory`
  (`beril_cli/start.py`, `beril_cli/config.py`, `beril_cli/cli.py`,
  `BERIL.code-workspace`, `PROJECT.md`, `.gitignore`);
  `AIAssistant/agent-io/plans/2026-06-16-beril-parallel-worktree-sessions.md`;
  the `kbu-beril-augmentation` and `kbu-harness` PRDs (precedent for kbu↔BERIL
  augmentation and the library+CLI module shape).
- **Constraint:** worker IS Claude (Max plan) — no Anthropic API / no subprocess
  `claude`; the proxy uses `os.execvp` to hand off the terminal to the agent, it
  does not call any model API.

## Acceptance Criteria

1. All `kbu beril worktree` subcommands accept group-level options `--beril-root PATH` and `--root PATH` (alias `--worktree-root`).
2. `set-root` accepts `--beril-root` and a worktree-root path and persists `beril.root` and/or `beril.worktree_root` to `~/.kbutillib/config.yaml`.
3. `start` accepts `--agent NAME` and `--skip-onboard`, and forwards all arguments after `--` verbatim to the launched agent.
4. The worktree directory for project `<id>` is exactly `<worktree_root>/<id>` and the branch is exactly `projects/<id>`.
5. Project IDs must match `[A-Za-z0-9._-]+`; IDs containing `/` or other characters are rejected with a clear error and no side effects.
6. `new <id>` creates branch `projects/<id>` off `main` when it does not exist, and adopts the existing branch (no `-b`) when it does.
7. `new` creates `.env` and `.venv-berdl` symlinks in the worktree pointing at `<beril_root>/.env` and `<beril_root>/.venv-berdl`; a missing target produces a warning and non-fatal continuation.
8. `new` writes `<worktree_root>/<id>.code-workspace` with a single folder `{"name":"BERIL: <id>","path":"./<id>"}`, copying the `settings` and `extensions` keys from `<beril_root>/BERIL.code-workspace` when present and writing empty objects otherwise.
9. If `<worktree_root>/<id>` exists and is not a registered git worktree, `new` aborts with an error and makes no changes.
10. `open <id>` recreates the worktree from an existing `projects/<id>` branch if its directory is missing, and errors (pointing to `new`) when the branch does not exist.
11. `rm <id>` removes only the worktree directory, never deletes the `projects/<id>` branch, and runs `git worktree prune`.
12. `rm <id>` is idempotent: when the worktree is not registered in `git worktree list` it prints "nothing to remove", deletes no files, and exits 0.
13. `rm <id>` refuses when the worktree has uncommitted changes unless `--force` is given.
14. `ls` prints a human-readable listing by default and a stable JSON array `[{"id","branch","path","live"}]` sorted by `id` under `--json`.
15. `start` assembles the agent command with no release-tag checkout; for `claude` it appends `--model opus` when `--model` was not supplied and sets the initial prompt to `/berdl_start` when no prompt is passed and `--skip-onboard` is false.
16. `start` applies BERIL's exact Vertex env-key mapping from `run_start` (not `os.environ.update(get_vertex_config())`), gated on `get_vertex_config()["enabled"]` and only for `claude`, and refreshes the auth token via the imported `_sync_auth_token` before launch.
17. `kbu beril worktree doctor` exits 0 on success and 1 when `beril_cli` import fails or any of `get_default_agent`, `get_vertex_config`, `_sync_auth_token` is missing, naming the missing symbol.
18. `doctor` reports whether each configured worktree's `.env` and `.venv-berdl` symlink targets exist and are readable.
19. `set-root` expands `~`, resolves to absolute paths, and creates `~/.kbutillib/config.yaml` and its parent directory if missing before writing.
20. Config resolution precedence is flag > env (`BERIL_ROOT` / `WORKING_BERIL_DIRECTORY`) > config (`beril.root` / `beril.worktree_root`) > default `<beril_root>/../WorkingBERIL` for the worktree root; `beril_root` has no silent default and errors if unresolved.
21. All git operations run with `git -C <beril_root>` and do not depend on the current working directory.
22. A warning that `beril start` must not be run inside a worktree is printed after a successful `new`, `open`, and `start`.
23. The `start` proxy's pre-`execvp` assembly is a pure function returning `(binary, argv, env)` that is unit-tested to omit any checkout; a unit test and `doctor` both assert `beril_cli` and the three borrowed symbols import.
