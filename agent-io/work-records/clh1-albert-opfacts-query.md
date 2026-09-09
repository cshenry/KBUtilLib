# Work record: clh1-albert-opfacts-query

## task_id
clh1-albert-opfacts-query

## branch
maestro/developer/clh1-albert-opfacts-query

## commit_shas
- e6aa1b903b11225b80214b3dec7120cb15aabfe3 -- docs: record trigger-wake dispatch to Albert for clearinghouse OP1 facts

(Verify with `git log 9066c99..HEAD --format=%H` in the worktree if this
needs re-checking; this second commit's own sha is necessarily omitted
from the list above, same as the precedent in clh1-current-state-sql.md.)

## summary

This is the final task of PRD `clearinghouse-lake-1-schema`. Everything else
in the PRD is off-pod KBUtilLib code; the three clearinghouse tables it
defines live inside the BERDL JupyterHub pod (`kbhub`), which no build agent
can reach and which is not a Maestro or AgentForge worker. The operator
runbook (`agent-io/docs/clearinghouse-schema-operator-runbook.md`, added by
task `clh1-operator-runbook`) names OP1 (getting one kbhub environment that
imports `kbutillib`, `berdl_notebook_utils` and `data_lakehouse_ingest`
together) as blocking, but the facts needed to know what OP1 actually means
on the real pod are not available to any off-pod session.

This task emitted one triggered-wake QUERY envelope to Albert (steward on
kbhub) asking six read-only fact-finding questions covering: how `kbutillib`
reaches `~/venvs/kbu-modeling` on the pod (Q1), whether a KBUtilLib checkout
exists on kbhub at all (Q2), what is writable for building a combined
environment and whether it persists across pod restarts (Q3, Q5), where
`berdl_notebook_utils` and `data_lakehouse_ingest` live (Q4), and whether the
`kbaseincubator.clearinghouse` namespace and its tables already exist (Q6 --
the safety-relevant question, since `select_write_mode()` still promotes a
requested `append` to `OVERWRITE` when a table appears absent and
`BerdlCapability.load()`'s `namespace` parameter still defaults to
`"default"`, so knowing the true starting state removes the one scenario in
which a first OP2 run could destroy something). The payload carries an
explicit `do_not` list forbidding all lakehouse writes, running the
bootstrap, mutating any Python environment, restarting the pod, or running
an expensive scan, and asks for a FILE reply (this session is a caller, not
a steward -- nothing polls a name for it) rather than an envelope back. No
lakehouse or pod operation was performed by this task itself. No production
code was changed; the only artifact this task adds is its own work-record.

**Approval**: Chris approved this wake in-session before dispatch. The
conductor put the cost and irrevocability to him explicitly (a paid Claude
session on kbhub, unrecallable once Dropbox syncs) and he answered "Yes,
wake albert." That approval was obtained by the conductor before this
developer sub-agent was dispatched; this sub-agent has no channel to ask
anyone anything and proceeded on that basis per its dispatch contract.

## files_touched

- `agent-io/work-records/clh1-albert-opfacts-query.md` (new -- this file)

No source, test, or documentation files were modified. This task's only
action was emitting a trigger-wake envelope (an out-of-repo Dropbox write)
and recording it here.

## success_criteria_check

- **A single trigger-wake query envelope addressed `--to albert
  --to-machine kbhub` was emitted with `PERSISTENTAI_MACHINE` set, its
  `trigger_id` captured and recorded** -- pass. `PERSISTENTAI_MACHINE=primary-laptop`
  was exported before the emit call. `emitted: 98c109ba-b121-40ac-8510-a0ff277533e0`,
  RC=0. Verified on disk: the envelope at
  `~/Dropbox/Projects/AIAssistant/trigger-inbox/kbhub/98c109ba-b121-40ac-8510-a0ff277533e0.trigger`
  has `to_agent: albert`, `task_type: query`, `depth: 0`, and is sitting in
  the `kbhub` directory (the two axes -- `--to`/`to_agent` and
  `--to-machine`/directory -- both checked correct, not assumed).
- **Reply-file path recorded** -- pass. See "Trigger details" below:
  `~/Dropbox/Projects/AIAssistant/trigger-inbox/replies/98c109ba-b121-40ac-8510-a0ff277533e0.json`.
- **Payload asks only the six read-only questions and carries an explicit
  `do_not` list forbidding all lakehouse writes, the bootstrap, environment
  mutation and pod restarts** -- pass. The payload's `questions` array has
  exactly Q1-Q6 as specified in the task prompt (reworded for clarity but
  substantively identical, plus the Q6 safety-context paragraph verbatim in
  substance per the task's "one piece of context" section). The `do_not`
  array explicitly forbids: writing to any table/namespace/file, running
  `bootstrap()`, `pip install`/`conda install`/creating any env, restarting
  the pod or any service, and running `COUNT(*)`/expensive scans -- plus an
  instruction to answer "refused" rather than violate any of these.
- **The payload requests a FILE reply and does not ask for an envelope
  back** -- pass. The `reply` key opens "Do NOT emit an envelope back" and
  names the exact reply-file path, matching the caller pattern from the
  skill (this session has no gate polling for it).
- **No lakehouse or pod operation was performed by this task itself** --
  pass. This session ran no command against kbhub, the BERDL lakehouse, or
  any pod resource; the only action taken was emitting the envelope from
  primary-laptop and inspecting local files.
- **No test that passed on the base commit fails on the branch** -- pass.
  See tests_run below: identical counts and identical failing/erroring node
  IDs to the stated baseline.

## tests_run

This task adds no production code, so no code-level verification was
expected beyond confirming the suite is unchanged. Ran the exact baseline
command, with the shared conductor-baseline venv repointed to this worktree
first:

- `pip install -e . --no-deps -q` (into
  `/Users/chenry/VirtualEnvironments/kbutillib-conductor-baseline`), then
  confirmed `import kbutillib; print(kbutillib.__file__)` resolved to
  `/Users/chenry/.maestro/worktrees/clh1-albert-opfacts-query/src/kbutillib/__init__.py`
  -- i.e. this worktree, not a stale install.
- `/Users/chenry/VirtualEnvironments/kbutillib-conductor-baseline/bin/python
  -m pytest tests/ -q --ignore=tests/biochem/test_escher_utils.py
  --ignore=tests/notebook/helpers` (600000 ms Bash timeout; actual runtime
  269.27s, within the stated ~250s baseline). **Before** (stated baseline on
  `9066c99`): 2878 passed, 1 failed, 4 errors, 380 skipped. **After** (this
  branch): 2878 passed, 1 failed, 4 errors, 380 skipped -- identical counts.
  The 1 failure and 4 errors are exactly the five pre-existing, named-as-
  not-mine node IDs from the task envelope
  (`tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`
  and the four `tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_*`
  collection errors). No test that passed on base fails on branch.

## Trigger details (for the next session to act on)

- **trigger_id**: `98c109ba-b121-40ac-8510-a0ff277533e0`
- **Envelope location**:
  `~/Dropbox/Projects/AIAssistant/trigger-inbox/kbhub/98c109ba-b121-40ac-8510-a0ff277533e0.trigger`
  (will move to `.../kbhub/done/` once Albert acks it -- that is the only
  observable confirmation of delivery from this side).
- **Reply-file path to poll**:
  `~/Dropbox/Projects/AIAssistant/trigger-inbox/replies/98c109ba-b121-40ac-8510-a0ff277533e0.json`
- **Emitted from**: primary-laptop, `PERSISTENTAI_MACHINE=primary-laptop`,
  `--from maestro-developer-clh1-albert-opfacts-query --to albert
  --to-machine kbhub --task query`, 2026-09-09T13:38:38Z (envelope's own
  `created_at`).
- **Expected latency**: typically 5-20 minutes (Albert's gate polls on his
  resident timer, not a fixed 300s launchd job -- see below), per Dropbox
  sync (3-15 min) plus poll interval. Do not treat this as a low-latency
  channel.
- **If the envelope stalls**: the likeliest cause is Albert's resident timer
  (`~/albert/bin/kbhub_waker.py`, started from the Jupyter boot list) not
  running, rather than anything about this envelope -- kbhub is a rootless
  JupyterHub pod with no launchd and no systemd, so this timer is the only
  mechanism that polls his inbox, and it is the least battle-tested of the
  four stewards' scheduling. **A pod restart is the first thing to ask
  about if it stalls.** `persistentai schedule --install` does **NOT** work
  on that pod (it branches launchd/systemd only) and must not be used to
  "fix" a stalled Albert.
- **Delivery is proven only when the envelope moves to `done/`** in
  `~/Dropbox/Projects/AIAssistant/trigger-inbox/kbhub/`, not when `emit`
  reports success (which only means "written", confirmed above).

## caveats

- This task performs no lakehouse or pod work and adds no production code
  by design -- it is a fact-gathering dispatch only, per the task prompt.
  The next session in this PRD's chain needs to poll the reply-file path
  above and, once Albert's answer lands, use it to decide what OP1 actually
  requires (update the runbook, or hand a concrete OP1 remedy to a human
  operator).
- The payload's `questions` array phrasing was tightened from the task
  prompt's Q1-Q6 wording for a single-session reader with no shared context,
  but is substantively identical in scope, ordering, and intent -- nothing
  was added or dropped.
- I did not wait for or poll the reply. Per the task prompt ("You are DONE
  once the envelope is emitted and recorded... do not block this phase
  waiting for the answer"), this session ends here.
- The shared conductor-baseline venv
  (`/Users/chenry/VirtualEnvironments/kbutillib-conductor-baseline`) was
  repointed to this worktree via `pip install -e . --no-deps` before the
  test run, per the task's instructions; no other worktree's editable
  install was touched.
