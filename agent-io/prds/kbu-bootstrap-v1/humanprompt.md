# kbu bootstrap v1 — human prompt

Design a `kbu bootstrap` subcommand that retrofits an existing research repo
as a kbu-aware project. Complement of `kbu new-project` (greenfield) which
just landed in kbu-start-v1.

## Why

We have three real research repos that need kbu skills and the subproject
state machine applied retroactively:
- `~/Dropbox/Projects/ModelingLOE` (notebooks under `notebooks/`, venvman
  `activate.sh` already present)
- `~/Dropbox/Projects/ADP1PhenotypeAnalysis` (bare, only README + agent-io)
- `~/Dropbox/Projects/MappingEmbeddings` (src + scripts + tests + requirements.txt)

`kbu new-project` refuses if the target path exists (by design — it creates
the directory + does `git init`). It can't be coerced into operating on an
existing repo.

## What

A new subcommand `kbu bootstrap` that runs in cwd and:
- Refuses unless cwd is a git repo with no existing `kbu-project.toml`.
- Copies the templates/student-project/ tree into cwd **additively**:
  `.claude/commands/`, `.vscode/`, `subprojects/.gitkeep`, gitignore lines
  appended via a marker block, README left alone.
- Detects an existing venv (`$VIRTUAL_ENV`, `activate.sh`, `.venv/`, `venv/`)
  and reuses it; refuses to install into Python <3.11 unless `--force`.
- pip-installs KBUtilLib editable into the (re)used venv.
- Registers a Jupyter kernel named after the project.
- Writes `kbu-project.toml` with `[project].bootstrapped=true` and
  `[update.file_hashes]` covering only the files actually written.
- Does NOT git commit. Tells the user to review + commit.

After bootstrap: `kbu update`, `kbu subproject create`, `kbu notebook list`,
the `/kbu-start` tier-2 dashboard, and all tier-2 skills work in the repo.

## Constraints inherited from kbu-start-v1

- macOS-only (`KBU_PLATFORM_OVERRIDE=force` opens non-Darwin best-effort)
- TOML schemas + ISO-8601 UTC `Z` timestamps + SHA-256 file hashing all
  per kbu-start-v1
- venvman invocation is `venvman create --project NAME --dir DIR --python 3.11`,
  activation via the generated `activate.sh` (no `venvman use`)

## Out of scope (v1)

- Adopting scattered notebooks into a `subprojects/legacy/` (deferred to a
  future `kbu adopt`)
- A `kbu unbootstrap` command (manual removal is small and documented)
- Tier-1 `/kbu-start` menu changes (separate follow-up)
- Linux/Windows
- Cross-machine bootstrap

## Open questions answered in design (2026-06-06)

| # | Question | Decision |
|---|---|---|
| 1 | Collision on `.claude/commands/kbu-*.md` | Identical-hash skip; different-hash prompt overwrite-with-`.bak` (default y); `--force` skips prompt |
| 2 | Should `kbu update` need a bootstrap-aware flag | No flag; `file_hashes` already keys what update touches. Surgical fix to update: filter "added" candidates to recorded paths; `--add-untracked` opts in |
| 3 | Incompatible existing venv (py<3.11, missing tools) | Refuse-with-message; `--force` overrides; `--no-venv` skips venv work entirely |
| 4 | Jupyter kernel naming collision | Reuse the name; `ipykernel install --user --name=X` replaces in-place — that's canonical jupyter behavior |
| 5 | Adopt existing notebooks into `subprojects/legacy/` | Out of scope; future `kbu adopt` |
| 6 | `kbu unbootstrap` for clean removal | Out of scope; "delete `kbu-project.toml` + `.claude/commands/kbu-*.md` + revert gitignore block" documented in success message |
