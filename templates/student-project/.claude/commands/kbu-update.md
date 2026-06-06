<!--
kbu skill provenance
type: net-new
created: 2026-06-05
-->

# /kbu-update — pull template updates from KBUtilLib

Use this command to update the `.claude/commands/` skill files and `.vscode/`
settings in this project from the parent KBUtilLib installation. Always run
this from the project root (the directory containing `kbu-project.toml`).

## Step 1 — dry-run diff

```bash
kbu update --check
```

`kbu update --check` prints a diff summary of which template files would be
added, modified, or deleted without making any changes. Capture and display
this output to the student.

If the output is `Already up-to-date.`, tell the student the project is
already at the latest template version and exit (no further action needed).
Still save a session record noting "No update needed."

## Step 2 — present the summary

Show the diff output exactly as printed. Explain briefly what each status
means:

- `[ADDED]` — new file from the template that does not yet exist in the project.
- `[MODIFIED]` — template file changed since the last update.
- `[DELETED]` — file removed from the template and will be deleted from the project.

If any `[MODIFIED]` files are listed, note that `kbu update` tracks whether
the student has edited those files locally. If a locally-modified file appears
in the diff, `kbu update` will normally prompt for confirmation; using
`--yes` skips that prompt because confirmation is obtained here first.

## Step 3 — ask for confirmation

Use AskUserQuestion:

```
Apply these template updates?
  Yes — apply all changes listed above
  No  — cancel, keep the project as-is
```

If the student selects No, confirm the update was cancelled and exit. Save a
session record noting "Update cancelled by student."

## Step 4 — apply the update

```bash
kbu update --yes
```

`--yes` bypasses the interactive overwrite-confirmation prompt inside
`kbu update` because confirmation was already obtained in Step 3.

Report the output from `kbu update --yes` to the student. The command prints
which commit the templates were pulled from.

If `kbu update --yes` fails (non-zero exit), show the error output and advise
the student to check that:

1. The `[kbutillib].source_path` in `kbu-project.toml` points to a valid
   KBUtilLib installation.
2. They can reach that path from their current machine.
3. They can run `kbu update --set-source <path>` to correct the source path
   if it has moved.

## Step 5 — save session

```bash
kbu session save \
  --skill kbu-update \
  --subproject project \
  --summary "<one sentence: what changed or why update was skipped>"
```

Use `project` as the subproject name when the update is project-wide (not
specific to one subproject). Include the number of files changed in the
summary when an update was applied, e.g. "Updated 3 template files to commit
abc123def456."
