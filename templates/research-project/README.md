# {{project_name}}

A scientific research project built on KBUtilLib.

This guide gets you from "I just cloned the repo" or "I just ran `kbu new-project`" to "Claude is driving my workflow." Once you're inside Claude with `/kbu-start` open, the AI agent handles the rest — planning, building notebooks, running them, reviewing, synthesizing results.

---

## Quick start

```bash
cd {{project_name}}
source activate.sh        # activate the per-project venv
claude                    # open Claude Code in the project root
# then in Claude:
/kbu-start
```

If you'd rather work in Cursor (recommended on macOS):

```bash
cursor {{project_name}}.code-workspace
# inside Cursor, open the integrated terminal:
claude
/kbu-start
```

That's it. `/kbu-start` is a status-aware dashboard that knows where each of your subprojects is in its workflow and routes you to the right next action.

---

## What you'll see in `/kbu-start`

| Menu item | When to pick |
|---|---|
| **Help** | First time using kbu — single-screen explainer of the workflow |
| **Plan** | Start a new subproject — `/kbu-plan` grills you on the research question, writes `RESEARCH_PLAN.md` |
| **Build** | Plan approved — scaffold analysis notebooks per the plan |
| **Run** | Notebooks built — execute them and capture outputs |
| **Synthesize** | Notebooks ran clean — interpret results, draft `REPORT.md` |
| **Review** | At any stage — get an independent AI review of plan / build / report |
| **Literature review** | Anytime — search PubMed / bioRxiv / arXiv / Semantic Scholar, append to `references.md` |
| **Diagnose** | When something is broken or surprising — structured debug loop |
| **Update** | Pull the latest KBUtilLib templates + slash commands into this project |

The menu is *state-aware*: if a subproject is in `build`, the **Plan** item is disabled. If no subprojects exist yet, only **Help**, **Plan**, **Literature review**, **Update** are enabled.

---

## Subproject layout

Each subproject lives under `subprojects/<name>/`. Create one with `kbu subproject create <name>` (or just ask `/kbu-start` to plan one):

```
subprojects/<name>/
├── RESEARCH_PLAN.md         # written by /kbu-plan, approved via /kbu-review
├── notebooks/
│   ├── 01_*.ipynb           # analysis notebooks scaffolded by /kbu-build
│   ├── util.py              # shared helpers for the subproject
│   └── nboutput/            # notebook execution outputs (data products)
├── data/
│   └── user_data/           # user-supplied data files
├── figures/                 # generated figures
├── references.md            # appended by /kbu-literature-review
├── REPORT.md                # written by /kbu-synthesize, approved via /kbu-review
├── sessions/                # local session YAMLs (not committed by default)
└── kbu-subproject.toml      # state machine + artifact tracking
```

---

## The state machine

Each subproject moves through 8 linear states. Reviews can route back on failure:

```
plan → p-review → build → b-review → run → synthesize → s-review → complete
            ↺           ↺                            ↺
        (review fail loops back to the prior state)
```

`kbu subproject status <name>` tells you where any subproject is. `kbu subproject list --json` gives a machine-readable summary across all subprojects.

---

## Useful CLI commands

| Command | Purpose |
|---|---|
| `kbu doctor` | Probe the project environment — venv, kernel, kbu version, Cursor/Claude integration |
| `kbu subproject create <name>` | Scaffold a new subproject's directory tree + manifest |
| `kbu subproject list [--json]` | List all subprojects + their state |
| `kbu subproject status <name> [--json]` | Detailed status for one subproject |
| `kbu session list [--limit N] [--json]` | Recent sessions (any subproject, any skill) |
| `kbu notebook list` | List notebooks under all subprojects + their run state |
| `kbu update [--check]` | Refresh `.claude/commands/` and `.vscode/` from the parent KBUtilLib install |

If `kbu doctor` reports any FAIL, it'll point you at the fix. The common ones:

- **`venv_python` no longer resolves** — your venv moved or was deleted. Re-run `kbu init` from the parent KBUtilLib clone, then come back here.
- **`jupyter-kernel` missing** — re-run kernel registration: `python -m ipykernel install --user --name=<your-kernel-name>`.
- **`cursor-on-path` FAIL** but Cursor.app is installed — symlink the shim: `ln -s /Applications/Cursor.app/Contents/Resources/app/bin/cursor ~/bin/cursor` (or use Cursor's command palette → "Shell Command: Install 'cursor' command in PATH").

---

## Keeping templates fresh

KBUtilLib evolves — when new slash commands ship or existing ones get better, run:

```bash
/kbu-start    # → Update
# or directly:
kbu update --check    # see what would change
kbu update            # apply
```

By default `kbu update` only touches files this project already tracks — files you (or `kbu bootstrap`) deliberately skipped stay skipped. Use `--add-untracked` to bring in newly-available template files too.

---

## Where to read more

- **KBUtilLib library API** — `<kbutillib-clone>/README.md` and `<kbutillib-clone>/docs/`.
- **First-time install** — `<kbutillib-clone>/GETTING_STARTED.md`.
- **Project hierarchy registry** (if your lab uses AIAssistant orchestration) — `~/Dropbox/Projects/AIAssistant/state/project_registry.yaml`.
- **Issues / bugs** — https://github.com/cshenry/KBUtilLib/issues.
