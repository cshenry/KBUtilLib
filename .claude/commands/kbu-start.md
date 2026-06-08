<!--
kbu skill provenance
type: tier-1 dashboard (KBUtilLib repo entry point)
created: 2026-06-08
design source: agent-io/prds/kbu-start-v1 (tier-1 spec)
-->

# /kbu-start — KBUtilLib root dashboard

Use this command as the entry point when you've cloned KBUtilLib and want to
either (a) set up your machine, (b) start a new scientific project, or
(c) retrofit an existing repo to use kbu workflows.

It is the **tier-1** dashboard — runs from inside the KBUtilLib repo itself.
The **tier-2** `/kbu-start` lives inside each kbu-scaffolded project and is a
different command (workflow state-machine dashboard).

If the user runs `/kbu-start` from somewhere that isn't a KBUtilLib clone, that
will resolve to the project-side variant (or nothing). This file only loads
when invoked from KBUtilLib itself.

## Step 1 — load the dashboard

Run these in parallel and parse:

```bash
kbu --version 2>&1                    # is kbu installed at all?
kbu doctor 2>&1                       # full environment probe
ls ~/.config/kbu/init_done.json 2>&1  # has kbu init completed?
```

### Render: environment status table

Print a compact status block at the top:

```
KBUtilLib root dashboard
========================
kbu CLI:        {version or "NOT INSTALLED"}
kbu init done:  {yes — venv at <path> / no}
doctor:         {N/M probes PASS}
```

If `kbu --version` fails because the CLI itself isn't installed, **stop**. Tell
the user this skill assumes the CLI is on PATH and direct them to
[GETTING_STARTED.md](../../GETTING_STARTED.md) Step 1 (`pip install -e .`).

### Render: doctor table (only if any probe is not PASS)

```
| Probe | Status | Note |
|-------|--------|------|
| init-done       | PASS/FAIL/SKIP | ... |
| cursor-on-path  | PASS/FAIL/SKIP | ... |
| claude-extension| PASS/FAIL/SKIP | ... |
| kbu-version     | PASS/FAIL/SKIP | ... |
| jupyter-kernel  | PASS/FAIL/SKIP | ... |
```

## Step 2 — present the menu

Use AskUserQuestion. Menu items + availability gating:

| Menu item | Always enabled? | Disabled-reason string |
|-----------|-----------------|-------------------------|
| Help | yes | — |
| Doctor | yes | — |
| Initialize | yes (but redundant if init-done = PASS — show as `Initialize (already done — re-run anyway)`) | — |
| New project | yes | — |
| Bootstrap | yes (but only useful from inside an existing git repo; the action handles missing-git cleanly) | — |
| List projects | yes | — |
| Update | only if init-done = PASS | `not-initialized` |

## Step 3 — route to the selected action

### Help

Print, in ≤1 screen:

- What KBUtilLib is (composable utility framework for KBase/ModelSEED + scientific workflows).
- The two-tier slash command model: tier-1 (you are here) sets up the machine and creates projects; tier-2 lives inside each project and drives the per-subproject workflow.
- The five user actions: Doctor (health check), Initialize (one-time machine setup), New project (scaffold from scratch), Bootstrap (retrofit existing repo), Update (pull latest templates).
- Where to read more: `GETTING_STARTED.md` for the human guide, `README.md` for the library API.

Then re-show the menu.

### Doctor

Run `kbu doctor` and re-render the table. For each FAIL line, offer a fix:

- **`cursor-on-path` FAIL** — Cursor.app may be installed but its CLI shim isn't on `$PATH`. Ask: *"Install the cursor shim into `~/bin/cursor` now? (Or do it yourself via Cursor command palette → 'Shell Command: Install cursor command in PATH'.)"* If yes:
  ```bash
  test -x /Applications/Cursor.app/Contents/Resources/app/bin/cursor || { echo "Cursor.app not at /Applications/Cursor.app"; exit 1; }
  mkdir -p ~/bin
  ln -sf /Applications/Cursor.app/Contents/Resources/app/bin/cursor ~/bin/cursor
  which cursor
  ```
  Then re-run `kbu doctor`.

- **`claude-extension` FAIL** — the Anthropic Claude Code extension isn't installed in Cursor. Tell user: *"Open Cursor → Extensions panel (Cmd+Shift+X) → search 'Claude' → install Anthropic's official Claude Code extension. Then re-run Doctor."*

- **`jupyter-kernel` FAIL** — the `kbutillib` Jupyter kernel isn't registered. This usually means `kbu init` either hasn't run or crashed during the kernel-registration step. Ask: *"Re-run `kbu init` now to register the kernel?"* If yes, route to **Initialize**.

- **`init-done` FAIL** — no marker. Route to **Initialize**.

- **`kbu-version` FAIL** — kbu is not on PATH. Direct user to `pip install -e .` from this repo.

After fixes, re-render the dashboard.

### Initialize

This is the most bug-prone action — the existing `kbu init` will fail in three
known ways. Walk the user through them proactively.

1. **Check venvman state first.** Run:
   ```bash
   command -v venvman && echo "VED=${VIRTUAL_ENVIRONMENT_DIRECTORY:-<unset>}"
   ```
   - If venvman is installed but `VIRTUAL_ENVIRONMENT_DIRECTORY` is unset in the current process, the user probably set it in their shell profile but hasn't re-sourced. Ask them to open a new terminal (or `source ~/.zshrc` / `source ~/.bash_profile`) and re-run. (`kbu init` itself has no `--no-venv` flag — only `kbu bootstrap` does — so the fallback path requires that the venvman probe genuinely fail, e.g. `venvman` not on PATH.)
   - If venvman is not installed at all, that's fine — `kbu init` will fall back to plain `.venv`. No action needed.

2. **Run `kbu init`** and capture both stdout and stderr.
   - If it succeeds: confirm via `kbu doctor` and route back to the menu.
   - If it fails with `No module named ipykernel`: this is a known bug in older KBUtilLib (pre-`a6fb33d`); pull latest:
     ```bash
     cd ~/Dropbox/Projects/KBUtilLib && git pull
     pip install -e .
     ```
     then retry. If the user's KBUtilLib clone is up-to-date and the bug still occurs, surface the venv python path and have them manually run:
     ```bash
     <venv_python> -m pip install ipykernel
     <venv_python> -m ipykernel install --user --name=kbutillib --display-name="KBUtilLib (kbu)"
     ```
     and write the marker via:
     ```bash
     PYTHONPATH=src python3 -c "from kbutillib.cli.init import _write_marker, _kbutillib_commit; from pathlib import Path; repo=Path('.'); _write_marker(kbutillib_repo_path=str(repo.resolve()), kbutillib_commit=_kbutillib_commit(repo), venv_manager='venvman', venv_python='<venv_python>', jupyter_kernel_name='kbutillib')"
     ```
   - If it warns `venvman succeeded but VIRTUAL_ENV could not be resolved from activate.sh`: again, a pre-`21b3758` bug. Pull and reinstall as above.

3. After init, if `cursor-on-path` is still FAIL, offer the symlink fix from the Doctor branch.

4. Re-render the dashboard.

### New project

Collect via AskUserQuestion:

1. **Target path** (required). Default suggestion: `~/Dropbox/Projects/<name>` if `~/Dropbox/Projects` exists, else `~/projects/<name>`.
2. **Project name** (default: basename of path).
3. **Author name / affiliation / ORCID** — try `git config user.name` / `git config user.email` for defaults. ORCID has no default.
4. **First subproject name** (optional — can skip and create later).

Then run:

```bash
kbu new-project <path> --name <name> --author <author> --affiliation <aff> --orcid <orcid> [--first-subproject <sp>]
```

If it succeeds, print exact next-step commands the user can copy-paste:

```
✓ Project created at <path>.

Open it in Cursor:
  cursor <path>/<name>.code-workspace

Then in Cursor's integrated terminal:
  cd <path>
  claude
  /kbu-start
```

Re-render the dashboard.

### Bootstrap

Use this when the user wants to add kbu workflows to an existing git repo that
already lives somewhere they like.

1. Ask for the **target repo path** (default: current working directory if it
   contains a `.git/` — detect with `test -d <path>/.git`).
2. If the path does not contain `.git/`, surface the error: *"`kbu bootstrap`
   requires the target to be inside a git repository. Either `cd` to an
   existing repo, or use `New project` to scaffold a fresh one."*
3. If the path already contains a `kbu-project.toml`, surface: *"This repo is
   already kbu-aware. Use `Update` to refresh templates, or
   `cd <path> && claude` to start using the existing project's `/kbu-start`."*
4. Otherwise, offer to **dry-run first**:
   ```bash
   cd <path> && kbu bootstrap --check
   ```
   Show output; ask for confirmation.
5. On confirm, run:
   ```bash
   cd <path> && kbu bootstrap [--author ... --affiliation ... --orcid ... --first-subproject ...]
   ```
   If the user has uncommitted changes mixed in, remind them that bootstrap
   does **not** auto-commit — review the diff and commit yourself.
6. Print next-step commands (same as New project's success block, swapping
   target path).

### List projects

There is no central kbu project registry yet (TODO: track via state file).
For now, scan likely roots for directories containing `kbu-project.toml`:

```bash
find ~/Dropbox/Projects ~/projects -maxdepth 3 -name kbu-project.toml -type f 2>/dev/null
```

For each hit, parse the manifest and print:

| Path | Name | Origin | Last pulled |
|------|------|--------|-------------|
| ... | `[project].name` | `bootstrap (<bootstrapped_at>)` or `new-project (<created_at>)` | `[update].last_pulled_at` |

If zero hits: tell the user no kbu-aware projects were found under the default
roots, and suggest `New project` or `Bootstrap`.

After listing, re-render the dashboard.

### Update

This refreshes the KBUtilLib clone itself (the source-of-truth for templates
and slash commands). It does not run `kbu update` inside individual projects —
that's a per-project action available from the tier-2 dashboard.

The canonical verb is `kbu init --update`, which pulls + reinstalls the
editable install in the venv `kbu init` originally provisioned.

1. Derive the KBUtilLib clone path from `pwd` at skill invocation (it should
   be the repo root containing `pyproject.toml` and `src/kbutillib/`).
2. Check working tree:
   ```bash
   git -C <kbutillib-clone> status --porcelain
   ```
   If non-empty, refuse and tell the user to commit or stash first.
3. Run:
   ```bash
   kbu init --update
   ```
   This pulls main and reinstalls KBUtilLib editable. Capture output and
   surface any errors.
4. As a fallback (e.g. if `kbu init` hasn't been run yet on this machine,
   so `--update` errors with no marker), run the manual two-step:
   ```bash
   cd <kbutillib-clone> && git pull
   pip install -e .
   ```
5. After update, recommend the user open each kbu-aware project and run
   `/kbu-start` → Update inside it (tier-2) to pull the latest templates.

Re-render the dashboard.

## Notes / known bug surface

This skill exists in part because `kbu init` and `kbu bootstrap` have failed
during onboarding in ways the bare CLI doesn't help with. Specifically:

- **`venvman` activate.sh format** changed (late-2025) to compose the venv
  path from `${VIRTUAL_ENVIRONMENT_DIRECTORY}/${VENV_SUBDIR}` rather than
  writing a literal `VIRTUAL_ENV=` line. Fixed in commit `21b3758`. Older
  clones will silently fall back to plain `.venv`.
- **`ipykernel` not installed by `pip install -e .`** — it's in the
  `[notebook]` extra, not base. Fixed in commit `a6fb33d` by adding
  `ipykernel` to the pip install command in both `kbu init` and `kbu bootstrap`.
- **`cursor` shim not on PATH** despite Cursor.app being installed. macOS
  installs Cursor.app to `/Applications`, but the `cursor` shell shim at
  `/Applications/Cursor.app/Contents/Resources/app/bin/cursor` only gets
  symlinked onto PATH when the user runs Cursor → Cmd+Shift+P → "Shell
  Command: Install 'cursor' command in PATH".

If you suspect any of these, check `git log --oneline -10` in the KBUtilLib
clone for the relevant commits, and offer to `git pull` + `pip install -e .`
before retrying.
