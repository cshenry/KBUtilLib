# Getting Started with KBUtilLib

This guide walks a first-time user from a clean machine to "Claude is running inside a kbu project, and we can take it from there."

It's intentionally short. KBUtilLib's superpower is that once Claude is running in a kbu project, the AI agent can pick up the rest — pulling in deeper docs, scaffolding subprojects, running notebooks, etc. Your job here is just to get Claude into the room.

---

## What you'll end up with

```
your-project/
├── .claude/commands/   ← /kbu-start, /kbu-plan, /kbu-run, /kbu-review, ...
├── subprojects/         ← scientific work organized as kbu subprojects
├── kbu-project.toml     ← manifest tracked by `kbu` CLI
└── your-project.code-workspace
```

Plus a Python virtual environment with KBUtilLib installed editable, registered as a Jupyter kernel, and `claude` running in the project root with access to project-aware slash commands.

---

## Prerequisites

You'll need:

1. **git** — `git --version` should work.
2. **Python 3.11 or newer** — `python3 --version`. If you have 3.10 or older, install 3.11+ first. We recommend [`pyenv`](https://github.com/pyenv/pyenv) on macOS/Linux:
   ```bash
   curl -fsSL https://pyenv.run | bash
   pyenv install 3.11.14
   pyenv global 3.11.14
   ```
3. **Claude Code CLI** — `claude --version` should work. Install from [claude.com/claude-code](https://claude.com/claude-code) and sign in once before continuing.
4. **(Optional)** [`venvman`](https://github.com/cshenry/venvman) for per-project virtual environments. If absent, `kbu` falls back to a plain `.venv`.

---

## Step 1 — Install KBUtilLib

```bash
git clone https://github.com/cshenry/KBUtilLib.git
cd KBUtilLib
pip install -e .
```

Sanity-check:

```bash
kbu --help
kbu doctor
```

`kbu --help` should list `bootstrap`, `new-project`, `init`, `notebook`, `subproject`, and friends. `kbu doctor` prints one line per environment probe — if any line ends in `❌`, follow its suggestion before continuing.

---

## Step 2 — Get into a kbu project

Pick the path that matches your situation.

### Path A: Brand new project

If you're starting from scratch:

```bash
cd ~/where/you/keep/projects
kbu new-project my-project --name "my project"
cd my-project
git init && git add . && git commit -m "kbu new-project scaffold"
```

`kbu new-project` will prompt for author/affiliation/ORCID if not supplied, scaffold the directory layout above, create a venv, register a Jupyter kernel, and write `kbu-project.toml`.

### Path B: Existing git repository

If you already have a repo and want to retrofit it as kbu-aware:

```bash
cd your-existing-repo
kbu bootstrap --check     # dry-run: shows everything that would happen
kbu bootstrap             # apply
```

`kbu bootstrap` copies the `.claude/commands/`, `.vscode/`, `subprojects/`, and workspace files into your existing tree, prompting before overwriting anything (use `--force-overwrite` to skip prompts; conflicting files are saved as `*.bak.<UTC>`). It detects an existing venv if you have one, falls back to creating a `.venv` if you don't, installs KBUtilLib editable, registers a Jupyter kernel, and writes `kbu-project.toml` with `bootstrapped = true`.

Bootstrap does **not** auto-commit. Review the changes and commit them yourself:

```bash
git add .
git commit -m "kbu bootstrap"
```

---

## Step 3 — Open Claude in your project

```bash
cd my-project   # or your existing repo
claude
```

That's it. The `.claude/commands/` files added by `kbu new-project` or `kbu bootstrap` register project-aware slash commands inside Claude:

| Slash command | What it does |
|---|---|
| `/kbu-start` | Onboard Claude to the project and start a session |
| `/kbu-plan` | Plan a scientific or coding task before executing |
| `/kbu-run` | Execute a planned task |
| `/kbu-build` | Build/scaffold new functionality |
| `/kbu-review` | Review work before committing |
| `/kbu-diagnose` | Diagnose a failing test or broken workflow |
| `/kbu-literature-review` | Run a literature search and synthesis |
| `/kbu-synthesize` | Synthesize results across runs or sources |
| `/kbu-update` | Pull the latest template/`.claude/commands` updates from KBUtilLib |

From here, just say what you're trying to accomplish — Claude will pick the right slash command, ask clarifying questions, and drive the workflow.

---

## Common follow-ups

**Keep templates up to date.** When KBUtilLib publishes new slash commands or template improvements, pull them into your project:

```bash
cd my-project
kbu update --check        # see what would change
kbu update                # apply
```

By default `kbu update` only refreshes files your project already tracks — files you deliberately skipped at bootstrap stay skipped. Pass `--add-untracked` if you want to add newly available template files too.

**Add a subproject.** Most scientific work lives in subprojects (`subprojects/<name>/`):

```bash
kbu subproject create <name>
```

Or just ask Claude inside the project: *"create a subproject for my isotope-labeling experiment"* — `/kbu-start` knows how.

**Check project health.** From inside the project:

```bash
kbu doctor
```

The last line tells you whether the project was created via `new-project` or `bootstrap`, plus when.

---

## If you get stuck

- `kbu doctor` is the first stop — it diagnoses venv / kernel / Python / manifest problems.
- The deeper module-by-module API is in [`README.md`](README.md) and [`docs/`](docs/).
- Open an issue at https://github.com/cshenry/KBUtilLib/issues.
