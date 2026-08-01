---
name: BERDL Tenant Governance
description: Tenant membership, access requests, namespace ACLs, stewardship, and admin operations on the BER Data Lakehouse (BERDL) platform
scope: domain
---

# BERDL Tenant Governance

## 1. What This Skill Covers

This skill covers everything about **who can do what on a BERDL tenant** —
membership level, requesting access, sharing data the right way, namespace
ACLs, stewardship, and platform admin operations. It does not cover loading
or querying data; see the related skills in §8 for those.

It is **locus-aware** and works from both places Chris runs BERDL work from:

- **in-pod** — inside the BERDL JupyterHub pod (`kbhub` / `jupyter-chenry`).
  Full governance surface: membership, access requests, ACL/stewardship
  inspection and mutation, admin operations.
- **off-pod** — laptop, h100, or anywhere else. `berdl_notebook_utils` is a
  pod-only package and is not importable, so the governance surface this
  skill wraps is not reachable. The correct behavior off-pod is to detect
  that plainly and say so — with the exact next step (run it from the pod) —
  not to guess, retry, or emulate a governance call over some other path.

The deep behavior (locus detection, membership decoding) lives in
`src/kbutillib/domains/kbase/berdl/` in KBUtilLib: `capability.py`
(`BerdlCapability`), `membership.py` (`decode_memberships`), and
`transports.py` (`InPodTransport`, which binds every governance function
below to its verified import path). This skill calls into that layer — it
does not reimplement the decode logic or the import bindings.

```python
from kbutillib.domains.kbase.berdl import BerdlCapability, InPodTransport

cap = BerdlCapability()
cap.locus()   # 'in_pod' | 'off_pod' — check this before anything else
```

`BerdlCapability` is **not** yet wired onto the `KBUtilLib` facade (`kbu.*`)
— it is an additive subpackage alongside the legacy `kbu.berdl`
(`KBBERDLUtils`). Import it directly, as above.

---

## 2. Membership: Who Am I, and At What Level?

`get_my_groups()` returns a **flat list** of tenant/group names. Read-only
membership is encoded as a **name suffix**, not a structured field:
`"enigmaro"` means read-only membership on the tenant `"enigma"`, not
membership on a tenant literally named `"enigmaro"`.

**The safe decode:** strip a trailing `"ro"` and test whether the
*remainder* is a real, known tenant name (i.e. it appears in
`list_available_groups()`). Only then is the suffix genuine.

**A naive `str.endswith("ro")` check is wrong and must never be used on its
own.** It misclassifies any tenant whose real, full name happens to end in
those two letters as read-only membership on a truncated, non-existent
tenant — e.g. a hypothetical tenant named `"cairo"` would be misread as
read-only on `"cai"`. `kbutillib.domains.kbase.berdl.membership.decode_memberships`
already implements the correct check (strip, then validate the remainder
against the available-groups list); do not re-derive it inline.

Get decoded memberships through `BerdlCapability`:

```python
cap = BerdlCapability()
memberships = cap.memberships()   # {tenant: 'rw' | 'ro'} — in-pod only
```

Read-write is the precondition for any load — report the permission level
per tenant before assuming a write will succeed, and treat `'ro'` (or a
tenant that's simply absent from the mapping) as "cannot write here yet."

**Off-pod**, `cap.memberships()` raises
`BerdlMembershipUnavailableError`: there is no governance surface to decode
membership from outside the pod. Report that plainly — do not attempt to
infer membership from a prior in-pod session or from database visibility.

---

## 3. Requesting Tenant Access

Requesting access to a tenant is an **asynchronous human approval step**,
typically approved same-day during business hours via Slack. Set that
expectation explicitly to the user and **do not poll for approval** — no
retry loop, no repeated `get_my_groups()` calls waiting for the group to
appear.

```python
transport = InPodTransport()   # governance surface is in-pod only
transport.request_tenant_access(
    "enigma",
    permission="read_write",       # or "read_only"; default is "read_only"
    justification="Why you need access",
)
```

`berdl_notebook_utils.governance.request_tenant_access(tenant_name,
permission='read_only', justification=None)`. Tell the user the request has
been filed and that approval will come through Slack — the correct next
action is to wait, not to check back in a loop.

---

## 4. Sharing Doctrine — a Deprecated Family You Must Not Emit

The published guides (and a model trained on them) reach for a family of
sharing functions: **`share_table`, `unshare_table`, `make_table_public`,
`make_table_private`**. This family is **deprecated and must not be
emitted** — do not call these, do not suggest them, do not use them as the
basis for an example. `InPodTransport` deliberately does not wrap them.

**The replacement:** sharing is achieved by *creating the table in a tenant
catalog in the first place* (any table created under a tenant's namespace is
already visible to that tenant's members at whatever the tenant's default
posture is), and access is then **refined with namespace ACLs** — see §5. If
a table currently lives in a personal catalog and needs to become shared,
the fix is to load or copy it into the tenant catalog, not to "share" it in
place.

If asked to "share a table" or "make a table public," treat that as a
request to either (a) load into the correct tenant catalog (`berdl-load`),
or (b) grant a specific namespace ACL (§5) — never as a cue to look for a
`share_*` or `make_table_*` function.

---

## 5. Namespace ACLs and Stewardship

These exist in the installed `berdl_notebook_utils` package but appear in
**none** of the published user guides — this skill is the only place they
are documented. They are the correct, non-deprecated mechanism for
controlling who can see or write a specific namespace within a tenant.

### Inspection — always available, no confirmation needed

```python
transport = InPodTransport()

transport.namespace_access(tenant_name="aiale")        # list_namespace_access
transport.tenant_stewards("aiale")                      # get_tenant_stewards
transport.tenant_members("aiale")                        # get_tenant_members
transport.list_tenants()                                 # list_tenants
transport.my_groups()                                     # get_my_groups
transport.show_my_tenants()                               # show_my_tenants
```

| Call | Import path |
|---|---|
| `list_namespace_access(tenant_name=None, namespace=None)` | `berdl_notebook_utils.governance` |
| `get_tenant_stewards(tenant_name)` | `berdl_notebook_utils.governance` |
| `get_tenant_members(tenant_name)` | `berdl_notebook_utils` (top level) |
| `list_tenants(force_refresh=False)` | `berdl_notebook_utils` (top level) |
| `get_my_groups(force_refresh=False)` | `berdl_notebook_utils.governance` |
| `show_my_tenants(...)` | `berdl_notebook_utils` (top level) |

Use these freely to answer "who has access to this namespace" or "who are
the stewards of this tenant" — they carry no destructive risk and need no
confirmation before calling.

### Mutation — requires explicit confirmation before calling

Access changes on shared tenants are hard to notice and hard to reverse —
that is exactly why they are gated. Before calling any of these, confirm
explicitly with the user what is about to change and on which tenant.

```python
transport.grant_namespace_access(
    "aiale", "someuser", "default", access_level="read",
)   # governance.grant_namespace_access
transport.revoke_namespace_access(...)   # governance.revoke_namespace_access
transport.assign_steward(...)             # top-level assign_steward
transport.remove_steward(...)             # top-level remove_steward
transport.add_tenant_member(...)          # top-level add_tenant_member
transport.remove_tenant_member(...)       # top-level remove_tenant_member
```

| Call | Import path |
|---|---|
| `grant_namespace_access(tenant_name, username, namespace, access_level='read', show_refresh_hint=True)` | `berdl_notebook_utils.governance` |
| `revoke_namespace_access(...)` | `berdl_notebook_utils.governance` |
| `assign_steward(...)` | `berdl_notebook_utils` (top level) |
| `remove_steward(...)` | `berdl_notebook_utils` (top level) |
| `add_tenant_member(...)` | `berdl_notebook_utils` (top level) |
| `remove_tenant_member(...)` | `berdl_notebook_utils` (top level) |

Note the split: the namespace-ACL mutations (`grant_namespace_access`,
`revoke_namespace_access`) live under `.governance`; the tenant-membership
and stewardship mutations (`assign_steward`, `remove_steward`,
`add_tenant_member`, `remove_tenant_member`) are top-level
`berdl_notebook_utils` calls. Neither guess nor default to `.governance` for
all of them — `InPodTransport` in `transports.py` is the verified source for
which is which.

---

## 6. Admin Operations

A separate tier of operations manages users, groups, and tenant creation
platform-wide. These require the **`CDM_JUPYTERHUB_ADMIN`** role, granted
via the KBase Auth Server — without it, every call in this section fails
with an authentication error, not a governance-denied error. Do not attempt
to work around an auth failure here by retrying or by falling back to a
non-admin call; report that the role is missing.

```python
transport = InPodTransport()
transport.list_users(...)                     # governance.list_users
transport.list_groups(...)                    # governance.list_groups
transport.add_group_member(...)               # governance.add_group_member
transport.remove_group_member(...)            # governance.remove_group_member
transport.create_tenant_and_assign_users(...) # governance.create_tenant_and_assign_users
```

All five are `berdl_notebook_utils.governance` calls. Every mutation here
(`add_group_member`, `remove_group_member`, `create_tenant_and_assign_users`)
requires the same explicit-confirmation gate as §5's mutations, on top of
the `CDM_JUPYTERHUB_ADMIN` requirement.

**`create_tenant_and_assign_users` is superseded by the tenancy v2 API**
for tenant creation and inspection: `list_tenants`, `get_tenant_detail`,
`get_tenant_members`, `add_tenant_member`, `update_tenant_metadata` (all
top-level `berdl_notebook_utils` calls, not `.governance`). Prefer the v2
calls for anything they cover; reach for
`create_tenant_and_assign_users` only for the one-shot
create-and-assign path v2 doesn't offer directly.

---

## 7. Privacy

AI-ALE tenant contents include collaborator material. BERDL's own
documentation warns that query results routed through its MCP server reach
external servers. Treat everything read from a tenant — table contents,
membership lists, ACL entries — with the same care as other sensitive
project material: **do not route tenant contents through external
services, diagram renderers, or pastebins.** This applies to governance
data (who has access to what) as much as to table data — a namespace-ACL
listing is itself information about who can see collaborator material and
should be handled with the same discretion.

---

## 8. Quick Reference: All Governance Import Paths

| Function | Import path | Gate |
|---|---|---|
| `get_my_groups` | `berdl_notebook_utils.governance` | inspection |
| `list_available_groups` | `berdl_notebook_utils.governance` | inspection |
| `get_my_workspace` | `berdl_notebook_utils.governance` | inspection |
| `get_credentials` | `berdl_notebook_utils.governance` | inspection |
| `request_tenant_access` | `berdl_notebook_utils.governance` | async human approval |
| `list_namespace_access` | `berdl_notebook_utils.governance` | inspection |
| `get_tenant_stewards` | `berdl_notebook_utils.governance` | inspection |
| `grant_namespace_access` | `berdl_notebook_utils.governance` | mutation — confirm first |
| `revoke_namespace_access` | `berdl_notebook_utils.governance` | mutation — confirm first |
| `list_users` | `berdl_notebook_utils.governance` | admin (`CDM_JUPYTERHUB_ADMIN`) |
| `list_groups` | `berdl_notebook_utils.governance` | admin (`CDM_JUPYTERHUB_ADMIN`) |
| `add_group_member` | `berdl_notebook_utils.governance` | admin, mutation |
| `remove_group_member` | `berdl_notebook_utils.governance` | admin, mutation |
| `create_tenant_and_assign_users` | `berdl_notebook_utils.governance` | admin, mutation — superseded by v2 for create/inspect |
| `list_tenants` | `berdl_notebook_utils` (top level) | inspection |
| `get_tenant_detail` | `berdl_notebook_utils` (top level) | inspection |
| `get_tenant_members` | `berdl_notebook_utils` (top level) | inspection |
| `add_tenant_member` | `berdl_notebook_utils` (top level) | mutation — confirm first |
| `remove_tenant_member` | `berdl_notebook_utils` (top level) | mutation — confirm first |
| `update_tenant_metadata` | `berdl_notebook_utils` (top level) | mutation — confirm first |
| `show_my_tenants` | `berdl_notebook_utils` (top level) | inspection |
| `assign_steward` | `berdl_notebook_utils` (top level) | mutation — confirm first |
| `remove_steward` | `berdl_notebook_utils` (top level) | mutation — confirm first |
| `get_my_steward_tenants` | `berdl_notebook_utils` (top level) | inspection |

**A note on `get_tenant_stewards` specifically:** the PRD's prose import-map
table (`agent-io/prds/berdl-lakehouse-skills/fullprompt.md`, "The import
map") groups it with the other top-level stewardship functions
(`assign_steward`, `remove_steward`, `get_my_steward_tenants`). That is a
known typo in that table. The harvested signatures in `api-reference.md`
and the shipped `InPodTransport.tenant_stewards()` in `transports.py` both
bind it under `.governance`. Follow the code, not the prose table, when the
two disagree: `get_tenant_stewards` is a `berdl_notebook_utils.governance`
call.

**Never call any of the above bare** (as if auto-imported with no module
qualification) outside a notebook kernel — see `berdl-session` for why that
raises `ImportError` and the full locus-detection procedure.

**Never emit** `share_table`, `unshare_table`, `make_table_public`, or
`make_table_private` — see §4.

---

## 9. Related Skills

- `/berdl-session` — locus detection, the full import map, the credential
  escalation ladder, and the access-denial taxonomy. Read this first if you
  are not already sure whether you are in-pod or off-pod.
- `/berdl-load` — in-pod loading: preflight (including the read-write
  membership check this skill's §2 documents), schema enforcement, write,
  verify, and namespace teardown.
- `/berdl-query` — discovery, Trino/Spark routing, alias translation,
  cross-catalog joins, and time travel, in both loci.
