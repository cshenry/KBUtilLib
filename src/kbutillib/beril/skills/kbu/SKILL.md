---
name: kbu
description: >
  KBUtilLib metabolic-modeling primer for BERIL notebooks.
  Use when: starting a BERIL modeling session, opening any project that
  involves COBRA/MSModelUtil/KBase reconstruction or FBA, or any time
  kbu-notebook or kbu-fba guidance is needed and the session context is
  not yet loaded.
allowed-tools:
  - Read
  - Bash
user-invocable: true
---

# /kbu — KBUtilLib BERIL Primer

## Step 0: You are probably in a git worktree

Chris runs BERIL with a **primary-on-main, studies-in-worktrees** layout so
multiple modeling sessions run in parallel without colliding:

- The **primary checkout** (`~/Dropbox/Projects/BERIL-research-observatory`)
  stays parked on `main`. Don't expect study work to live there.
- **Each study runs in its own worktree** under
  `~/Dropbox/Projects/BERILWorktrees/<id>`, on branch `projects/<id>`. You are
  most likely inside one of these right now — `git branch --show-current` will
  read `projects/<id>`, not `main`. That is correct, not a mistake to fix.
- **Sibling worktrees are other live sessions.** Stay inside your own worktree
  dir; never edit, commit into, or `cd` into the primary checkout or a sibling.
- `.env`, `.venv-berdl`, and the `.claude/` skill bundles are **symlinks into
  the primary checkout** — intentional and shared. Do not "repair", replace, or
  commit them.
- **Never run `beril start` in a worktree** (it runs `_checkout_release` and
  detaches the branch). Use `kbu beril worktree start <id>` instead.

Worktrees are managed with `kbu beril worktree {new,ls,rm,open,start} <id>`.
If you're unsure which worktree you're in, run `git branch --show-current`.

## Step 1: Load modeling preferences

Read the active preferences file into the session context:

```
Read <BERIL_ROOT>/.claude/kbu/preferences.md
```

The YAML block in that file sets the active values for all `execution.*`,
`sampling.*`, `solver.*`, `gapfill.*`, `organism.*`, `media.*`, and
`version.*` keys.  Honour these values throughout the session.  If the
file does not exist, apply the defaults documented in
`src/kbutillib/beril/skills/kbu/preferences.md` (the template shipped
with KBUtilLib).

## Step 1a: NotebookSession import

The correct import for the session bootstrap is:

```python
from kbutillib.notebook import NotebookSession
```

> Note: `kbutillib.beril` is a **resources path** — it holds skill
> bundles (`skills/`) used by Claude Code.  It is NOT an importable
> Python API.  Always import from `kbutillib.notebook`.

## Step 1b: KBase authentication

Set `KB_AUTH_TOKEN` in your environment before starting:

```bash
export KB_AUTH_TOKEN="your-kbase-token-here"
```

KBUtilLib reads this variable automatically and uses it as the KBase
token, with precedence over any token file.  No `~/.kbase/token` stash
is needed.

## Step 2: Active modeling guidelines

The following guidelines apply for the duration of this session:

1. **Graduated execution policy.** Every computation is evaluated against
   the 🟢/🟡/🔴 tiers defined in the `kbu-notebook` and `kbu-fba`
   skills before running.  Do not run 🟡 or 🔴 work without explicit
   approval.

2. **Notebook discipline.** All interactive modeling notebooks follow
   the conventions in `kbu-notebook`.  One `util.py` per notebook dir,
   every cell starts `%run util.py`.

3. **FBA discipline.** All reconstruction and FBA work follows the arc
   in `kbu-fba`: build → gapfill → FBA (pFBA) → FVA.  Always use
   `session.kbu.fba.run_fva`; never call `cobra.flux_variability_analysis`
   directly (it is broken in this environment).

4. **BERDL access.** Use `KBBERDLUtils` for all KBase/BERDL data
   retrieval (media, genome-table databases).  Do not call KBase SDK
   clients directly unless `KBBERDLUtils` lacks the required accessor.

5. **Cache discipline.** Expensive computations must be cached in
   `.kbcache/` alongside `util.py`.  Retrieve from cache before
   re-running.  Never commit `.kbcache/` to git or allow BERIL to back
   it up (it is derived data and can be large).

## Step 3: Skill pointers

- **kbu-notebook** — notebook construction discipline (one `util.py`,
  `%run util.py`, graduated execution, `.kbcache/` layout, BERIL
  integration).
- **kbu-fba** — modeling arc (build → gapfill → FBA → FVA), function
  signatures, sampling defaults, FVA mandate.

Both skills are auto-discoverable.  Invoke them when the relevant
context is needed.

> Note: this primer does NOT modify or patch BERIL's `/berdl_start`
> skill.  It runs alongside the existing BERIL lifecycle, adding
> modeling discipline on top of BERIL's project/session management.
