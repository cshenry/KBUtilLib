# kbu-beril-augmentation — human summary

**The pivot:** Stop building a notebook co-scientist inside KBUtilLib. Make
KBUtilLib a **science-method + skill-deployment layer** that augments KBase's
**BERIL-research-observatory** (the Microbial Discovery Forge). BERIL is the
brain (orientation, review, synthesis, shared corpus); KBUtilLib supplies the
modeling methods and the notebook discipline BERIL lacks.

**What we're building (PRD A of two):**

1. **`kbu beril` deployer** — `install` / `configure` / `doctor <BERIL_ROOT>`.
   Copies our skills into `<BERIL_ROOT>/.claude/skills/` as **untracked** dirs
   (so they survive `beril start`'s release re-checkout), pip-installs
   `kbutillib` into the BERIL env so functions are importable in-notebook, and
   renders an editable `preferences.md`. Modeled on CRAFT's deployer; pip/pipx
   installable so **other KBase users** can use it too. Separate from
   `claude-skills sync`.

2. **Three skills:**
   - **`/kbu`** — manual primer; loads preferences + briefs the guidelines.
   - **`kbu-notebook`** — the canonical notebook discipline: one `util.py` with
     all imports + the `NotebookSession`; every cell `%run util.py`; every cell
     independently executable with **cache-as-you-go**; portable project dirs.
     **Supersedes the broken `jupyter-dev`.**
   - **`kbu-fba`** — the full arc: reconstruct → gapfill (incl. comprehensive)
     → analyze (`run_fba`, `run_fva` — never the broken `cobra` FVA — media,
     objective, essentiality).

3. **Graduated execution policy** (in both skills): BERIL executes cheap/certain
   cells freely for TDD; for anything slow, uncertain, large fan-out, or
   compute-heavy it runs a **sample, caches it, and stops to consult**; the full
   run happens only on your sign-off, and you choose each time where it runs.

4. **`NotebookSession.for_notebook()`** mapped onto BERIL's `projects/{id}/`
   layout (`.kbcache/` beside `util.py`), keeping the provenance cache you value.

**Durability fact:** deployed skills are untracked and config is gitignored, so
nothing we add blocks or is reverted by BERIL's per-launch release checkout.
Verified against BERIL's `.gitignore` and checkout behavior.

**Tests:** deployer (against a temp fake BERIL root), NotebookSession BERIL
mapping, and a skill-bundle smoke test.

**Out of scope → PRD B (`kbu-harness`):** the per-project container repo, rsync
to/from BERIL, the design-deploy step, in-harness execution, and the MD dev-log
rule. Ships after A.

**Grounded by:** `agent-io/audits/2026-06-13-kbu-vs-beril-directive-audit.md`,
CRAFT (`~/Dropbox/Projects/craft`), and BERIL
(`~/Dropbox/Projects/BERIL-research-observatory`).
