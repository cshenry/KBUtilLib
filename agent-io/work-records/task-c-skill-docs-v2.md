# Work Record: Task C — Skill-doc corrections (v2)

## task_id

kbu-friction/skill-docs-v2 (re-do of Task C; prior attempt on branch
`kbu-friction/skill-docs` did not actually make the edits)

## branch

`kbu-friction/skill-docs-v2`

## commit_shas

- `3d07496c7c5b3b6c6cffd2bfb8d40dd0ae5a2e8` (full: run `git rev-parse HEAD` on branch)

## summary

Rewrote four skill-doc files under `src/kbutillib/beril/skills/` to
reflect the verified live API of KBUtilLib/ModelSEEDpy.  The `kbu-fba`
SKILL.md was the largest change: it now documents the mandatory
`genome_classifier` second positional arg to `build_metabolic_model`
(confirmed at `ms_reconstruction_utils.py:176`), the correct
`gapfill_metabolic_model` 4-tuple return and pass-through of `mdlutl`
(confirmed at `ms_reconstruction_utils.py:685`), the `session.kbu.*`
canonical idiom backed by `toolkit.py:139+146`, the objective DSL
grammar from `objectivepkg.py` (MAX{}/MIN{} plus `|`-separated linear
combinations), the `MSGenome.from_fasta` offline fallback
(`modelseedpy/core/msgenome.py:212`), explicit statement that
`KBBERDLUtils.get_genome` does not exist (only
`get_genometables_from_kbase` at line 705), and `KB_AUTH_TOKEN`
documentation.  The `kbu/SKILL.md` primer gained the `NotebookSession`
import line, a note that `kbutillib.beril` is a resources path not an
importable API, and a `KB_AUTH_TOKEN` section.  The `kbu-notebook`
SKILL.md gained an explicit prose import statement and `kbutillib.beril`
note (the BERIL Lifecycle `/berdl_start` section was already present).
The `preferences.md` had its "Copy this file to ..." instruction header
replaced with a note and gained a `configured: false` sentinel.

## files_touched

- `src/kbutillib/beril/skills/kbu-fba/SKILL.md` (full rewrite)
- `src/kbutillib/beril/skills/kbu/SKILL.md` (added Steps 1a/1b)
- `src/kbutillib/beril/skills/kbu-notebook/SKILL.md` (added prose import + beril note)
- `src/kbutillib/beril/skills/kbu/preferences.md` (header + sentinel)

## success_criteria_check

**AC 7** — `kbu-fba` skill documents verified FBA contract with correct
types, 4-tuple return from gapfill, `mdlutl` pass-through, and explicit
build→gapfill→run handoff.
**PASS** — signatures confirmed against live source before writing.

**AC 8** — objective-string grammar documented near first use, derived
from `ObjectivePkg.build_package` (read at task time): MAX{}/MIN{} plus
linear combinations via `|` separator and optional coefficient prefix.
**PASS**

**AC 9** — `kbu-notebook` BERIL Lifecycle section instructs user to run
`/berdl_start` after `kbu init-notebook`.
**PASS** — section was already present; confirmed by grep.

**AC 10** — offline fallback uses `MSGenome.from_fasta` (verified at
`msgenome.py:212`); explicitly states `KBBERDLUtils.get_genome` does not
exist; names `get_genometables_from_kbase` as the real endpoint.
**PASS**

**AC 15** — `preferences.md` "Copy this file to ..." header removed;
`configured: false` sentinel added.
**PASS**

## tests_run

No automated tests cover pure doc files.  Per the PRD (Testing
Decisions, Tasks C/D): "doc/exemplar tasks; validation is the reviewer
reading the skill text".  Existing composition smoke tests were not
re-run in this worktree (no code changes were made; smoke tests are not
affected by markdown edits).

## caveats

- The BERIL Lifecycle (`/berdl_start`) section was already present in
  `kbu-notebook/SKILL.md` from a prior commit — this task confirmed its
  presence and did not duplicate it.
- `kbu-notebook/SKILL.md` section 7a appears before section 7 due to
  prior numbering — left as-is to avoid churning unrelated content;
  reviewer may want to renumber.
- `from_protein_sequences_hash` is documented as taking a dict (not a
  filename), per the live source at `msgenome.py:265`.  The task prompt
  required this explicit clarification.
- Worktree git metadata was corrupted by Dropbox sync racing against
  worktree creation; manually repaired the `.git/worktrees/` entry and
  reset the index before committing.  Branch HEAD and working tree were
  unaffected.
