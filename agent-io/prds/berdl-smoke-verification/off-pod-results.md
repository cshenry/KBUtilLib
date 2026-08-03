# Off-pod smoke verification — results

**Machine:** primary-laptop · **Date:** 2026-08-03 · **Session:** `/ai-design`
**Spec:** [`humanprompt.md`](humanprompt.md)
**Python:** `/Users/chenry/.pyenv/versions/3.11.14/bin/python3`, `kbutillib` from
the editable Dropbox checkout (`src/kbutillib/__init__.py`), branch `wip` @ `b46a02b`.

Run with `KBASE_AUTH_TOKEN` explicitly unset (`env -u`) so file-fallback
resolution was genuinely exercised rather than shadowed by the env var.

---

## Summary

| # | Off-pod item | Result |
|---|---|---|
| 1 | Token resolution works off-pod | **PASS** — and the recorded blocker was stale |
| 2 | REST exposes the personal catalog? | **BLOCKED** — see the finding below |
| 3 | Subpackage imports without the pod package | **PASS** (clean) |
| 4 | `load()` refuses correctly, for real | **PASS** (clean, all 5 checks) |
| 5 | Dual-name disambiguation | **BLOCKED** — same cause as #2 |

Items 3 and 4 are clean results and are recorded as such deliberately — the spec
asks for that explicitly.

---

## The headline finding: off-pod is not pod-independent

**The off-pod REST surface is a thin remote client to the user's own per-user
Spark Connect server in the pod.**

`POST https://hub.berdl.kbase.us/apis/mcp/delta/databases/list` with a valid
Bearer token returns **HTTP 500**, body:

```
{"error":null,"error_type":null,
 "message":"Spark Connect server for user 'chenry' did not respond to a
            session-create RPC within 15s. Restart your notebook pod to recover."}
```

All four `OffPodTransport` methods — `databases`, `tables`, `table_schema`,
`query` — delegate to `KBBERDLUtils` against that same `/apis/mcp/delta`
service. So the **entire off-pod read path** depends on a healthy *in-pod* Spark
Connect session.

The design framing "REST/MCP = read-only, works without Spark" is true only
about **local** Spark. It requires a **live pod** Spark Connect session. Off-pod
is therefore *not* a pod-independent fallback, and the documented kbhub Spark
Connect zombie state takes the laptop's read path down with it.

This is why items 2 and 5 are blocked, not failed: they need a successful
`databases/list`, and that call cannot succeed while the pod's Spark Connect is
zombied. Re-run the probe from primary-laptop once the in-pod session completes
its repair step.

---

## Item 1 — token resolution off-pod: PASS

- `KBASE_AUTH_TOKEN` absent from env (forced).
- `~/.kbase/token` exists, **32 chars** stripped.
- `tokens.resolve_token()` → returns the file value; `require_token()` succeeds.
- `KBBERDLUtils._get_headers()` → returns `['Authorization', 'Content-Type', 'accept']`.

**The 2026-07-31 recorded blocker is stale.** It said the laptop "loads
`~/.kbase/token` (32 bytes) but `KBBERDLUtils._get_headers()` raises *No KBase
token available*". That no longer reproduces — headers resolve fine.

**The token is also genuinely valid**, proven by discrimination rather than
assumed:

| Request | Result |
|---|---|
| Real token, `Bearer` scheme | HTTP **500** — the chenry-specific Spark Connect message |
| Bogus token, `Bearer` scheme | HTTP **401** `{"error":10020,...,"message":"KBase auth server reported token is invalid."}` |
| Real token, **no** `Bearer` prefix | HTTP 401 `{"error":10030,...,"Authorization header requires Bearer scheme"}` |
| No `Authorization` header | HTTP 401 `{"error":10010,...,"User not authenticated"}` |

Two things follow. First, a 32-byte token is **not** a staleness signal — that
is normal KBase token length, and the same assumption should be re-tested rather
than inherited for kbhub's copy. Second, the 500 is unambiguously a server-side
pod condition, not an auth failure: a bad token produces 401 at the same endpoint.

---

## Item 3 — subpackage imports without `berdl_notebook_utils`: PASS

`import berdl_notebook_utils` → `ModuleNotFoundError`, i.e. this is a true
off-pod environment, not a fixture. With it genuinely absent, all six modules
imported cleanly:

```
kbutillib.domains.kbase.berdl
kbutillib.domains.kbase.berdl.tokens
kbutillib.domains.kbase.berdl.naming
kbutillib.domains.kbase.berdl.transports
kbutillib.domains.kbase.berdl.capability
kbutillib.domains.kbase.berdl.membership
```

This closes the `berdl-transports` success criterion that by construction
**cannot** be verified in-pod. It is now verified against the real environment,
not only via the `force_off_pod` fixture.

---

## Item 4 — `load()` refusal quality, against the real environment: PASS

`BerdlCapability().locus()` → `'off_pod'`.
`load(dataset="smoke_scratch", tables=[{"name": "t1"}])` → `BerdlLoadRefusedError`:

```
BerdlCapability.load() requires a Spark session, and Spark only exists inside the
BERDL JupyterHub pod (kbhub). This process is off-pod ('berdl_notebook_utils' is
not importable here), so nothing has been staged and no write has been attempted.
Run this load from inside the pod instead, e.g. from a kbhub notebook or terminal:
  python -c "from kbutillib.domains.kbase.berdl.capability import BerdlCapability; BerdlCapability().load(dataset='smoke_scratch', tables=[...])"
```

| Check | Result |
|---|---|
| Names the pod requirement (Spark) | PASS |
| Names kbhub | PASS |
| States nothing was staged | PASS |
| Gives a concrete command | PASS |
| Echoes the caller's dataset name | PASS |

---

## Items 2 and 5 — BLOCKED, not failed

Both need a successful `databases/list`. Blocked by the pod-side Spark Connect
condition above. Reruns are cheap once the pod is repaired; the probe is written
and checks both at once:

- Item 2 checks whether any returned entry belongs to the personal (`chenry`)
  catalog, and reports whether the design-time observation ("tenant catalogs
  only") is confirmed or contradicted.
- Item 5 checks that no dotted name and its underscored twin appear as two
  separate entries — i.e. that `normalize_databases` folded them, preferring the
  dotted Iceberg form and recording the underscored form as `legacy_alias`.

Note the decided behavior for item 2 either way: `kbu-dlquery` must **warn and
continue naming kbhub** — never hard-fail (tenant reads work off-pod) and never
silently return nothing (that silence is the defect).

---

## Defect filed

`OffPodTransport` **discards the diagnostic 500 body**. The server returns a
precise, actionable message; `transports.py` raises
`RuntimeError("BERDL REST databases/list failed: 500 Server Error: Internal
Server Error for url: ...")`, throwing the useful part away. All four delegating
methods share the failure mode. Filed as a decision in AIAssistant project state
under project id `kbutillib`, with the fix: detect this condition and translate
it into guidance naming kbhub and the non-rotating repair
(`get_credentials()` → `start_spark_connect_server(force_restart=True)`,
escalating to `refresh_spark_environment()` only on failure).

---

## Decisions applied this session (spec's open questions, all closed)

1. **Form** — smoke test stays a documented manual procedure; no `kbu dl smoke`
   script this round.
2. **Write tenant** — `kbaseincubator`, not `aiale`.
3. **Personal-catalog gap** — warn and continue, naming kbhub.
4. **Read-only refusal test** — run it, against `refdata`. Safe on evidence: the
   preflight is a client-side membership-dict lookup that raises before Spark,
   before existence checks, and before any staging.

Recorded in AIAssistant project state under project id `kbase`.
