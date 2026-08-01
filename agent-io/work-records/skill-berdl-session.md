# Work Record: skill-berdl-session

## task_id
skill-berdl-session

## branch
conductor/berdl-lakehouse-skills/skill-berdl-session

## commit_shas
- e7ce058c914b5ce148fd63fd7b42c7558b38dad5

## summary
Authored `agent-io/skills/berdl-session.md`, the foundation skill for the
BERDL lakehouse skill set. It encodes the five load-bearing facts from
`agent-io/prds/berdl-lakehouse-skills/fullprompt.md` that the published
`berdl_docs` guides get wrong or omit: the full verified import map (all
four `berdl_notebook_utils` submodule paths plus `data_lakehouse_ingest`),
locus detection specified as an importability test rather than an
environment-variable check, the five-step credential escalation ladder in
order with an explicit warning that the rotating step breaks other live
sessions, the access-denial taxonomy separating expected tenant isolation
from genuine credential drift (including the `kbase`/`kbaseincubator`
trailing-slash prefix-matching case), and the virtualenv/kernel procedure
covering the `pip install --user` shadowing hazard and its recovery. The
skill calls into the existing `BerdlCapability`/`InPodTransport` layer
(`src/kbutillib/domains/kbase/berdl/capability.py` and `transports.py`,
both already on `main`) rather than reimplementing any of that logic, and
follows the frontmatter/structure conventions of the six skills already in
`agent-io/skills/` (closest analog: `kbase-genome-expert.md`). No Python
module was modified.

## files_touched
- `agent-io/skills/berdl-session.md` (new)

## success_criteria_check
- **A berdl-session skill markdown file exists in KBUtilLib's
  agent-io/skills/ with frontmatter matching the conventions of the skills
  already there.** PASS — `agent-io/skills/berdl-session.md` uses the same
  `name` / `description` / `scope: domain` frontmatter block as
  `kbase-genome-expert.md`, `kbutillib-dev.md`, `kbutillib-expert.md`,
  `kb-sdk-dev.md`, and `msmodelutl-expert.md`.
- **Body contains the complete verified import map covering all four
  import paths.** PASS — §1 reproduces the full table for
  `berdl_notebook_utils` (top level), `.governance`, `.spark`, `.refresh`,
  plus the separate `data_lakehouse_ingest` row, verbatim in content from
  the PRD's "Solution" section, and adds the pointer that
  `InPodTransport.__init__` performs exactly these four imports.
- **Locus detection specified as an importability test.** PASS — §2 states
  detection must test importability of `berdl_notebook_utils`, not
  environment variables alone, names the specific pod env vars that can be
  set without the package being installed, and points at
  `BerdlCapability.locus()` / `berdl_notebook_utils_importable()` in
  `capability.py` as the canonical implementation.
- **The five-step credential ladder in the correct order with an explicit
  statement that the rotating fix breaks other live sessions and is never
  first.** PASS — §3 lists, in order: (1) non-rotating
  `get_credentials()` + `start_spark_connect_server(force_restart=True)` +
  fresh session, (2) kernel restart, (3) rotating
  `refresh_spark_environment()` with an explicit "will break other
  kernels, running scripts, and remote connections... never the first
  move" statement, (4) server restart + browser refresh, (5) escalate to
  the BERDL platform team. Includes a code snippet showing the transport
  calls for step 1 and a comment marking step 3 as rotating.
- **The access-denial taxonomy distinguishing expected isolation from
  genuine drift and naming the kbase/kbaseincubator trailing-slash case.**
  PASS — §4 lists the three expected-isolation cases (cross-tenant prefix
  `AccessDenied`, job-logs bucket `AccessDenied`, and the
  `aws s3 ls` no-trailing-slash-matching case naming `kbase` vs
  `kbaseincubator` explicitly and stating it affects both initial target
  tenants) against the four genuine-drift symptoms (tenant browser never
  renders, empty favorites, UI lag, 403 on paths the user does hold), with
  an explicit statement that only the second list warrants walking the
  credential ladder.
- **The venv/kernel procedure including the pip --user shadowing hazard
  and its recovery.** PASS — §5 explains the silent `--user` fallback
  outside an activated venv, that it shadows the base image for the whole
  server (not just the current kernel) and survives kernel/server/pod
  restarts, and gives a four-step recovery (locate via `pip show`, remove
  via `pip uninstall` or a direct `rm -rf ~/.local/...` fallback, restart
  the kernel, reinstall inside an activated venv).
- **No Python module is modified.** PASS — `git diff --stat main` for this
  branch shows a single new markdown file; no file under `src/` was
  touched.

## tests_run
None. Per the task's mandatory process rules, this task modifies no Python
and the full pytest suite was explicitly not to be run. No automated check
applies to a markdown-only skill file; the success-criteria table above is
the verification.

## caveats
- The pip `--user` shadowing hazard and its recovery steps are not sourced
  from the PRD document itself (a scan of `fullprompt.md`, `humanprompt.md`,
  `data.json`, and `api-reference.md` in
  `agent-io/prds/berdl-lakehouse-skills/` found no mention of `pip`/`venv`);
  they come directly from the task prompt's item 5, which stated the
  hazard and asked for "the recovery" without specifying its exact steps.
  I wrote a standard, conservative recovery procedure (locate via
  `pip show`, uninstall or manually remove from `~/.local`, restart
  kernel, reinstall inside an activated venv) consistent with how
  `--user`-install shadowing is generally resolved. This was not verified
  against a live `kbhub` session — if the actual recovery on this platform
  differs (e.g. a different `~/.local` path convention, or an additional
  step to clear a cached import), the skill should be corrected by
  whoever next verifies it in-pod.
- This skill was authored against `src/kbutillib/domains/kbase/berdl/`
  as it exists on `main` at commit `332ea64` (capability.py, transports.py,
  naming.py, membership.py, tokens.py all present). Three sibling tasks
  (`berdl-capability`, `berdl-pure-logic`, `berdl-transports`) were noted
  as editing files in the same directory concurrently on their own
  branches per the task instructions; this task touched none of those
  files and made no assumption about their in-flight state beyond what
  was already merged to `main`.
- `berdl-load`, `berdl-query`, and `berdl-tenant` (the three skills this
  one is the foundation for) are out of scope for this task and were not
  authored here.
