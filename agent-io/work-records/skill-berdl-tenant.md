# Work Record: skill-berdl-tenant

## task_id
skill-berdl-tenant

## branch
conductor/berdl-lakehouse-skills/skill-berdl-tenant

## commit_shas
- 2f288f43ab6030e0dc3010fc03b678978dd2bab4 — docs(skills): add berdl-tenant skill for membership, ACLs, and admin ops

## summary
Authors `agent-io/skills/berdl-tenant.md`, the fourth of four BERDL skills
described in `agent-io/prds/berdl-lakehouse-skills/fullprompt.md`
("Skills" section). It covers the governance surface — membership
decoding, access requests, sharing doctrine, namespace ACLs/stewardship,
and admin operations — for both loci, and calls into the already-merged
capability layer (`BerdlCapability`, `InPodTransport`,
`decode_memberships`) rather than reimplementing any of its logic. No
Python module was touched.

## files_touched
- `agent-io/skills/berdl-tenant.md` — new skill file (frontmatter +
  9 sections: overview/locus-awareness, membership decoding, access
  requests, sharing doctrine, namespace ACLs/stewardship, admin
  operations, privacy, a consolidated import-path quick-reference table,
  related skills)

## success_criteria_check

- **berdl-tenant skill markdown file exists in `agent-io/skills/` with
  conforming frontmatter** — PASS. File created with `name:`,
  `description:`, `scope: domain` frontmatter, matching the convention in
  the five sibling skill files already in that directory
  (`kbutillib-dev.md`, `kbase-genome-expert.md`, etc.).
- **ro-suffix membership decode with naive endswith explicitly rejected**
  — PASS. §2 states the suffix decode, gives the `"enigmaro"` example, and
  explicitly calls out that a naive `str.endswith("ro")` check "is wrong
  and must never be used on its own," with the `"cairo"`-style
  misclassification example, and points at
  `kbutillib.domains.kbase.berdl.membership.decode_memberships` as the
  already-correct implementation rather than telling the reader to
  re-derive it.
- **access requests described as asynchronous human Slack approval with
  no polling** — PASS. §3 states same-day/business-hours Slack approval
  explicitly and says "do not poll for approval," with a worked
  `request_tenant_access` example and a closing instruction to tell the
  user to wait rather than loop-checking.
- **deprecated sharing family named and forbidden, tenant catalogs +
  namespace ACLs given as replacement** — PASS. §4 names all four
  (`share_table`, `unshare_table`, `make_table_public`,
  `make_table_private`), states they must not be emitted/suggested/used as
  example basis, and gives the replacement (create in a tenant catalog,
  refine with namespace ACLs) with a concrete "if asked to share a table"
  redirection.
- **always-available inspection ops separated from confirmation-gated
  mutation ops** — PASS. §5 has two subsections with distinct headers
  ("Inspection — always available, no confirmation needed" /
  "Mutation — requires explicit confirmation before calling"), each with
  its own code block and import-path table, listing exactly the two sets
  named in the task prompt (`list_namespace_access`, `get_tenant_stewards`,
  `get_tenant_members`, `list_tenants`, `get_my_groups`,
  `show_my_tenants` vs. `grant_namespace_access`,
  `revoke_namespace_access`, `assign_steward`, `remove_steward`,
  `add_tenant_member`, `remove_tenant_member`).
- **CDM_JUPYTERHUB_ADMIN requirement for admin operations** — PASS. §6
  states the role requirement up front, notes calls fail with an
  authentication error (not a governance-denied error) without it, lists
  the five admin governance calls, and notes the tenancy v2 API
  (`list_tenants`, `get_tenant_detail`, `get_tenant_members`,
  `add_tenant_member`, `update_tenant_metadata`) supersedes
  `create_tenant_and_assign_users` for create/inspect.
- **privacy constraint on routing tenant contents to external services**
  — PASS. §7 states the AI-ALE/collaborator-material framing, cites
  BERDL's own MCP warning, and extends the constraint explicitly to
  governance data (ACL/membership listings), not just table contents.
- **all governance functions referenced via `.governance` path rather
  than bare/top-level** — PASS with one accuracy caveat, see below. Every
  function that is genuinely bound under `berdl_notebook_utils.governance`
  in the shipped `transports.py` is described with that exact path
  throughout (§3, §5, §6, and the consolidated table in §8), and no
  function anywhere in the file is presented as a bare, unqualified call
  — the file explicitly warns against that in §8's closing note. The one
  function the task singled out by name, `get_tenant_stewards`, is called
  out specifically in §8 with the exact typo/correction the task
  described (fullprompt.md's prose table misgroups it as top-level;
  `api-reference.md` and `transports.py` both place it under
  `.governance`; this skill follows the code).
- **no Python module modified** — PASS. `git diff --stat` against `main`
  shows only `agent-io/skills/berdl-tenant.md`; `git status` is clean
  after the commit.

## tests_run
None. Per the task's mandatory process rules (rule 2), this task modifies
no Python, so no test run was required or attempted. `ruff`/`pytest` were
not invoked.

## caveats
- **Accuracy note on "all governance functions via `.governance`":** not
  every tenancy/stewardship function the task prompt lists is actually
  bound under `berdl_notebook_utils.governance` in the shipped
  `transports.py` — `list_tenants`, `get_tenant_detail`,
  `get_tenant_members`, `add_tenant_member`, `remove_tenant_member`,
  `update_tenant_metadata`, `show_my_tenants`, `assign_steward`,
  `remove_steward`, and `get_my_steward_tenants` are bound at the
  **top level** of `berdl_notebook_utils` (verified by reading
  `InPodTransport`'s method bodies, which call `self._bnu.*` for these and
  `self._governance.*` only for the true governance-submodule set). I
  judged that literally forcing all of these into a false `.governance`
  path would contradict the shipped, tested transport code and the task's
  own instruction to "reference the correct submodule import paths" and
  to follow `transports.py`/`api-reference.md` over a table known to
  contain at least one typo — so the skill states the correct path for
  each function individually (both tables in §5/§6 and the consolidated
  §8 table have an explicit "Import path" column distinguishing
  `berdl_notebook_utils.governance` from `berdl_notebook_utils` top
  level), while still (a) never presenting any of them as a bare/notebook-
  style call and (b) explicitly resolving the one named discrepancy
  (`get_tenant_stewards`) exactly as instructed. If the reviewer intended
  a literal, blanket "path all of these through `.governance`" reading of
  the success criterion regardless of what the shipped code does, that
  would require either changing the skill's claims to be factually wrong
  relative to `transports.py`, or changing `transports.py` itself (out of
  scope for this task, which touches no Python).
- No sibling skill (`berdl-session`, `berdl-load`, `berdl-query`) exists
  yet in this worktree or on `main` at the time of writing — this task's
  worktree was created fresh off `main` (no pre-existing
  `skill-berdl-tenant` worktree was found), and the three sibling
  `skill-berdl-*` worktrees on disk are all still parked at their
  pre-task base commit with no skill file written. The "Related Skills"
  section (§9) references them by the slash-command names given in the
  PRD (`/berdl-session`, `/berdl-load`, `/berdl-query`) on the assumption
  those sibling tasks will land those exact file/command names; this was
  not verified against their (not-yet-existent) output.
