# BERDL Lakehouse Skills — original request

> Session: `/ai-design`, 2026-07-31 → 2026-08-01. Requested by Chris Henry.

## The ask, as stated

> I want to design a set of skills to know how to load data to a tenant on the
> lakehouse, as well as other relevant capabilities described in
> https://github.com/KBaseDataLakehouse/berdl_docs/tree/main/user_guides.
> Review these guides and explain what skills need.

## Answers given during the design grill

1. **Execution locus** — "I suggest we build in-pod and off-pod skills. We
   operate in both places."
2. **Owning repo** — "KBUtilLib makes sense."
3. **Pod deployment channel** — "I think ClaudeCommands sync can read pod today."
   (Verified true in-session.)
4. **Is the off-pod REST surface Iceberg-aware?** — "I don't know. We should
   check." (Checked in-session; answered empirically.)
5. **Initial tenant targets** — "ALALE and KBase Incubator both are my initial
   targets."

## Decisions taken during the grill

- One **locus-aware** skill set, not two parallel in-pod/off-pod families.
- Off-pod writes **hard-fail with guidance** rather than generating notebooks or
  dispatching through Maestro.
- Module breakdown: one deep `BerdlCapability` module behind two transports,
  plus four pure-logic helpers.
- Spark **cluster provisioning deferred** out of v1.
- Namespace ACLs and stewardship: **inspection always, mutation behind explicit
  confirmation**.
- Tests cover the **pure-logic modules only**.
