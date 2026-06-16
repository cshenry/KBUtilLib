# kbu-harness — human summary

**PRD B of two.** PRD A (`kbu-beril-augmentation`, shipped 2026-06-14) taught
BERIL to *build* lab-discipline modeling notebooks and to **sample-then-consult**
on anything slow, uncertain, large-fan-out, or compute-heavy. PRD B builds the
place where the **full expensive run** actually happens — and the rsync that
moves a project there and back.

**The pain it solves:** BERIL runs on cloud JupyterHub (BERDL/Spark), ephemeral
pods, and **forbids programmatic notebook execution** ("must use web UI"). Our
modeling notebooks are local COBRA/MSModelUtil work that needs a solver, not
Spark. So full runs have nowhere automated to go. The **harness** is that place:
a per-project, local, programmatic-execution container.

**What we're building:**

1. **`kbu harness` CLI + `kbutillib/harness/` library** — one deep module:
   - `init <BERIL_ROOT> <project-id>` — scaffold a **sibling git repo** under a
     harness root (e.g. `~/Dropbox/Projects/kbu-harness/<id>/`): venv, BERIL-mirror
     dirs, `.claude/skills/` with the `kbu-run` skill, empty `DEVLOG.md`,
     `harness.toml` (records the BERIL source), and an initial pull.
   - `pull` / `push` — rsync the whole `projects/{id}/` tree **both ways**
     (`.kbcache/` included, so PRD A's sampled work carries into the full run and
     back), excluding `.git/.venv/__pycache__`.
   - `run [nb…] [--on local|h100]` — `local`: `nbconvert --execute --inplace`,
     capture exit/traceback, verify outputs. `h100`: write an ai-cowork task at
     the Dropbox-synced harness path (results flow back via Dropbox).
   - `doctor` — venv / `import kbutillib` / `harness.toml` / BERIL-reachable.

2. **`kbu-run` skill** (deployed into the harness, auto-discoverable +
   user-invocable): drives the loop — pull (the "design-deploy" step) → classify
   the run (graduated-execution, reads pulled `preferences.md`) → choose local vs
   h100 → `kbu harness run` → verify → append `DEVLOG.md`. **Success: stop, confirm,
   push back to BERIL** (so BERIL can commit notebooks-with-outputs). **Failure:
   stop and report the traceback — never edits code.**

**Strip deferred:** the confront round established the old-co-scientist strip is
*load-bearing* (kept `bootstrap`/`update`/`new-project` import `layout.py` +
`manifest.py`; ~11 test files touch the targets), so it is **out of scope for PRD
B** and moves to its own follow-up PRD where the full org-surface removal can be
scoped coherently. PRD B is harness-only.

**Why all-harness-side:** BERIL stays at its three PRD-A skills and never knows
about the harness. You `cd` into the harness; its own skill does pull/run/push.
Self-contained and pip-distributable to other KBase users (local execution is the
portable default; the h100 ai-cowork path is Chris-lab-specific).

**Tests:** library core (init scaffold, pull/push round-trip, doctor, harness.toml)
against temp dirs; the runner on a tiny real notebook (clean + throwing); dev-log
append-only; skill-bundle smoke + post-strip assertions.

**Grounded by:** `agent-io/prds/kbu-beril-augmentation/` (PRD A),
`agent-io/audits/2026-06-13-kbu-vs-beril-directive-audit.md`, BERIL
(`~/Dropbox/Projects/BERIL-research-observatory`), CRAFT
(`~/Dropbox/Projects/craft`, CLI-surface convention only — it has no harness
pattern).
