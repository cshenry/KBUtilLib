# Remote LP-Solver Service (KBUtilLib)

## Problem Statement

Chris develops metabolic models on primary-laptop, but the commercial LP/MILP
solvers he needs — Gurobi and CPLEX — will never install there (license
restriction / platform incompatibility). Only the H100 machine has working
Gurobi and CPLEX. Today, laptop-side FBA/FVA falls back to GLPK (slow) or the
broken `cobra.flux_variability_analysis` workaround, and large genome-scale or
community MILP problems are effectively unsolvable on the laptop. There is no
way to hand an LP off to H100's fast commercial solvers and get the solution
back programmatically.

## Solution

A small asynchronous **LP-solver service on H100** that accepts an LP file,
solves it with Gurobi or CPLEX, and returns the solution — plus a **KBUtilLib
client** that submits an LP and polls for the result over an SSH tunnel.

From the caller's perspective:

```python
kbu = KBUtilLib(...)
result = kbu.remote_solver.solve_lp(lp_text_or_path, solver=None, time_limit=None)
# result == {
#   "status": "optimal" | "infeasible" | "unbounded" | "timeout" | "error",
#   "objective_value": float | None,
#   "variables": {"<var_name>": value, ...},   # every variable, keyed by the
#                                               # SAME names in the LP text sent
#   "solver": "gurobi" | "cplex",
#   "solve_time_s": float,
#   "error": str | None,   # populated only when status == "error"
# }
```

The caller generates the LP text (from its own optlang/cobra model); the service
is solver-only and never imports cobra. Communication is HTTP over an SSH tunnel
(not SQS): the client `POST`s a gzipped LP, receives a `job_id`, and polls
`GET /result/{job_id}` until the job reaches a terminal state.

## User Stories

1. As a modeler on primary-laptop, I want to submit an LP file to H100's Gurobi
   and get the optimal solution back, so I can solve problems my laptop's GLPK
   can't handle.
2. As a modeler, I want the returned `variables` dict keyed by the *exact*
   variable names in the LP I sent, so I can map fluxes back to my reactions
   with no name translation.
3. As a modeler, I want to choose Gurobi or CPLEX explicitly (or accept a
   default), and know which engine actually produced a number, so my results are
   reproducible.
4. As a modeler, I want an infeasible or unbounded problem reported *as such*
   (never silently retried on the other solver), so I trust the answer is a real
   property of my model.
5. As a modeler, I want a solve that exceeds its time limit to return the best
   incumbent found (clearly labeled `timeout`), so a near-optimal MILP result
   isn't thrown away.
6. As a modeler, I never want a failure silently dropped: any unrecognized
   solver outcome or crash must come back as `status: "error"` with a populated
   `error` string, not a wrong `optimal`.
7. As a modeler, I want to fire several solves close together and have them
   handled concurrently (up to a bounded pool), so I'm not serialized behind one
   long solve when the machine has capacity.
8. As a modeler, I want the client call to block-and-poll transparently and
   return a plain dict, so the async service feels synchronous at my call site.
9. As a modeler, I want a solve to survive a service restart gracefully — if the
   service bounces mid-solve, my poll returns a definitive `error`, never an
   eternal "running".
10. As the platform operator, I want the service to run as a supervised systemd
    unit on H100 that survives logout and restarts on failure, so it's always
    available without manual babysitting.
11. As the platform operator, I want the service bound to localhost and reachable
    only through the SSH tunnel, so access is gated by H100 login (which also
    satisfies the solver licenses' login-capacity requirement).
12. As the platform operator, I want per-solve thread caps so that 5 concurrent
    solves can't oversubscribe H100's cores and starve other platform workloads.
13. As a modeler, I want large LP files to transfer efficiently, so submitting a
    multi-MB genome-scale LP over the tunnel is fast.

## Implementation Decisions

### Transport & topology
- **HTTP over an SSH tunnel**, not SQS. SQS's 256KB message cap fails on
  genome-scale LP text even after gzip (the request, not the response, is the
  problem), which would force an S3 large-payload offload we don't otherwise
  need. KBUtilLib also has zero boto3/SQS footprint, whereas it already contains
  the exact submit-then-poll client shape (`ArgoUtils`).
- v1 caller set = **interactive primary-laptop only**. h100-local callers (if
  ever added) hit `127.0.0.1` with no tunnel; emailmac-headless callers (not in
  v1) would need their own tunnel — an additive concern, not a redesign.
- The commercial-solver licenses on H100 are **unlimited**, but their terms
  require callers to have login capacity on the machine. The SSH tunnel is
  therefore a **licensing-compliance boundary**: reachability implies H100 login.
  This is *why* the service may bind localhost-only and keep auth lightweight.

### Repo & module layout
- Client and service both live in **KBUtilLib** (one repo, shared result schema,
  no cross-repo drift).
- **Server** package `kbutillib/services/lp_solver/`:
  - `solver_backends.py` — deepest module. Stable interface
    `solve(lp_path, solver, time_limit, threads) -> SolveResult`. Two impls
    (Gurobi, CPLEX) behind one interface. Owns: reading the LP, setting the
    solver's time limit and thread count, running, the INF_OR_UNBD re-solve, and
    extracting `{var_name: value}` for **every** variable via the solver's own
    name attribute (Gurobi `Var.VarName`, CPLEX `variables.get_names()`).
  - `job_store.py` — deep module over SQLite. Interface:
    `create(lp) -> job_id`, `claim_next()`, `mark_running/mark_done/mark_error()`,
    `get(job_id)`, `sweep_expired()`, `reap_orphans_on_startup()`. Hides SQL,
    per-job LP temp-file handling, the 48h TTL, and orphan reaping.
  - `worker.py` — bounded async pool (≤ `max_concurrent_solves`) that pulls
    `queued` jobs, invokes `solver_backends.solve` with the thread cap, enforces
    the watchdog hard-kill, and writes results back.
  - `app.py` — FastAPI surface (thin): `POST /solve`, `GET /result/{job_id}`,
    `GET /healthz`. HTTP glue, gunzip, queue-depth 503, optional bearer check.
- **Client** module `kbutillib/ms_remote_solver_utils.py`, mirroring `ArgoUtils`:
  - `class MSRemoteSolverUtils(SharedEnvUtils)` with
    `solve_lp(lp_text_or_path, solver=None, time_limit=None) -> dict`.
  - Companion `MSRemoteSolverUtilsImpl` (holds `env`, lazy delegate, token copy),
    registered as a guarded import in `kbutillib/__init__.py` and a lazy
    `@property remote_solver` on `KBUtilLib` in `kbutillib/toolkit.py`.
  - Config-driven (dot-notation via `SharedEnvUtils.get_config_value`):
    `remote_solver.base_url` (default `http://127.0.0.1:8091`),
    `remote_solver.timeout`, `remote_solver.poll_interval` (default 2.0s),
    `remote_solver.api_key` (optional).

### Result schema & status mapping
- Return exactly the schema above. `variables` contains **every** variable the
  solver reports, keyed by the solver's own name (verbatim to the LP for
  LP-legal names, which the caller guarantees). Solver choice never changes the
  returned names.
- Explicit whitelist mapping; **anything unrecognized ⇒ `error`**:
  - Gurobi `OPTIMAL` / CPLEX `optimal`,`MIP_optimal` → `optimal`
  - `INFEASIBLE` → `infeasible`
  - `UNBOUNDED` → `unbounded`
  - `TIME_LIMIT` → `timeout`
  - anything else (numeric trouble, interrupted, suboptimal, license error,
    exception) → `error`, with the raw solver status/message in `error`.
- **Gurobi `INF_OR_UNBD` (status 4):** re-solve that model once with
  `DualReductions=0` to definitively distinguish `infeasible` vs `unbounded`.
  CPLEX lacks this quirk.
- `objective_value` and `variables` are populated only for `optimal` and for
  `timeout` **with a feasible incumbent**; otherwise `None` / `{}`.
- `error` is populated **only** when `status == "error"`. `solve_time_s` is
  always the wall-clock solve time.

### Solver selection
- `solver=None` → service default **Gurobi**; `solver="gurobi"|"cplex"` honored
  exactly; returned `solver` field reflects the engine that actually ran.
- **No cross-solver fallback.** Infeasible/unbounded are real answers, returned
  as-is. Infrastructure failure (license unavailable, solver crash) → `error`,
  never a silent engine switch. Rationale: scientific reproducibility.

### Concurrency, threads, timeouts
- `max_concurrent_solves` default **5** (bounded worker pool). Overflow submits
  are accepted and FIFO-queued (return `job_id`, status `queued`); the client
  polls through the wait. `max_queue_depth` default **50** backstop → `POST
  /solve` returns HTTP 503 beyond that.
- `threads_per_solve` default **floor(physical_cores / max_concurrent_solves)**;
  applied per solve via Gurobi `Threads` / CPLEX `threads` so 5 concurrent
  solves don't oversubscribe cores. Both knobs are service config.
- Caller `time_limit` → Gurobi `TimeLimit` / CPLEX `timelimit` (clean solver
  stop). `default_time_limit` **3600s** when omitted; `max_time_limit` ceiling
  **7200s** (caller values clamped). **Watchdog hard-kill** at
  `time_limit + 60s` grace terminates a wedged solver subprocess → job `error`.

### Persistence & lifecycle
- **SQLite** job store: `job_id`, `status`, `solver`, `submit_ts`, `start_ts`,
  `end_ts`, `result_json`, `error`. LP payloads written as **per-job temp files
  on disk** (solver reads natively; keeps the DB small), deleted on completion or
  by the TTL sweep.
- Job states `queued → running → done | error`, mapped into the result-schema
  `status` on retrieval.
- **Orphan-on-restart:** at startup, any job left `running` (its solver
  subprocess died with the old process) is marked `error` with
  `error: "service restarted during solve"` — a definitive terminal answer,
  never an eternal `running`.
- **Result TTL:** completed results swept after **48h**.

### Wire format & polling
- `POST /solve` body = **gzipped LP text** (`Content-Encoding: gzip`; service
  inflates). Result responses uncompressed (small).
- Client polls `GET /result/{job_id}` every **2s**; client overall wait =
  `min(requested_time_limit, max_time_limit) + 90s` then raises a client-side
  `TimeoutError`. The client always outlasts the server watchdog, so the normal
  terminal path is a real `error`/`timeout` result, not an early client give-up.

### Auth & network binding
- Service binds **`127.0.0.1:8091`** on H100 (never the external interface).
  Port 8091 is free in the 8090-8099 band; there is **no port registry** on
  H100, so the deploy step must `ss -tln` to confirm it's still free before
  enabling.
- Optional **bearer token** `remote_solver.api_key` (checked only if set),
  mirroring `ArgoUtils`'s `x-api-key`. May be left unset in v1 given the
  localhost bind.
- TunnelManager adds a direct local port-forward
  `laptop 127.0.0.1:<local_port> → H100 127.0.0.1:8091`; the client
  `base_url` points at the local forward (plain forward, not SOCKS).

### Deployment on H100 (verified pattern — see Testing prior-art note)
- **User-level systemd unit** `~/.config/systemd/user/lp-solver.service`. H100
  has no sudo; **linger is already enabled**, so the unit survives logout with no
  extra step. Template = `maestro-bridge.service` (localhost-only Python sidecar):
  `Type=simple`, `Restart=on-failure`, `RestartSec=10s`, explicit `PATH`
  prepending `%h/.local/bin`, no `User=` line.
- **Dedicated venv `~/venvs/lp-solver`** with `fastapi` + `uvicorn` + the solver
  Python packages. ExecStart runs uvicorn against `lp_solver.app:app --host
  127.0.0.1 --port 8091` (or `-m` module form, matching the package entrypoint).
- **File logging** to `~/.lp-solver/logs/` (matching the Maestro units), not
  journald.
- Code reaches H100 via Dropbox sync + a `~/projects/<Repo>` clone `git pull`;
  restart after a code pull is `systemctl --user restart lp-solver.service`
  (`daemon-reload` only when the unit file itself changes).
- DocDB is **not** a deployment template (it runs in Docker); the Maestro/Courier
  user units are.

### Confront-hardened specifics (round 1 — GPT-5 cross-family)

These resolve every binding stall point an autonomous builder would hit. All are
binding.

- **`SolveResult` type (S1).** `solver_backends.solve(...)` returns a plain dict
  whose keys are exactly the external result schema: `status`, `objective_value`,
  `variables`, `solver`, `solve_time_s`, `error`. No divergent internal type.
- **Variables on timeout (S2).** On `timeout` with a feasible incumbent, return
  **all** variables the solver reports (solvers populate every variable's value
  from the incumbent), keyed by name — consistent with the "every variable"
  requirement. Only when there is genuinely no incumbent are `objective_value`
  and `variables` `None`/`{}`. Never partially omit variables that have values.
- **INF_OR_UNBD re-solve (S3).** On Gurobi status 4, set `DualReductions=0` on the
  existing model and re-optimize **within the original `time_limit`** (no extra
  slack); `solve_time_s` includes both runs.
- **Solver imports (S4).** Server depends on `gurobipy` and the native `cplex`
  Python package — **not** `docplex`.
- **job_store paths & types (S5).** SQLite DB at `~/.lp-solver/jobs.sqlite`;
  `job_id` is a UUIDv4 string primary key; per-job LP temp files at
  `~/.lp-solver/tmp/{job_id}.lp`. All under `~/.lp-solver/` on H100 (a local path,
  never inside Dropbox).
- **Solve isolation & watchdog (S6).** Each solve runs in a **separate worker
  subprocess** (gurobipy/cplex are in-process C APIs that can't be interrupted
  cleanly in-thread). The watchdog sends `SIGTERM` at `time_limit + 60s`, then
  `SIGKILL` 5s later; a killed solve → job `error` ("solver exceeded time limit +
  grace").
- **SQLite concurrency (promoted from free critique).** Because multiple solve
  subprocesses plus the API process touch the DB, open SQLite in **WAL mode** with
  a **busy-timeout (retry-on-busy)**; writes go through the store's own connection
  handling. This is binding, not advisory.
- **Auth header (S7).** Optional token is checked as header `x-api-key: <token>`
  (no `Bearer` prefix), matching `ArgoUtils`. Checked only if `remote_solver.api_key`
  is set.
- **Result HTTP codes (S8).** `GET /result/{job_id}` returns **200 + JSON** for any
  known job — the `status` field carries `queued`/`running` for non-terminal jobs
  and the terminal status otherwise; **404** for an unknown `job_id`. `POST /solve`
  returns 200 with `{job_id}` normally, **503** when the queue is full.
- **gzip request headers (S9).** Client sets `Content-Type: text/plain;
  charset=utf-8` and `Content-Encoding: gzip`; no filename metadata is sent.
- **Poll cadence (S10).** Client polls at a fixed `poll_interval` (default 2.0s)
  with ±10% jitter; no backoff.
- **Thread computation (S11).** `threads_per_solve = max(1, floor(os.cpu_count() /
  max_concurrent_solves))` using logical cores from `os.cpu_count()`.
- **TTL sweep cadence (S12).** `sweep_expired()` runs on each `POST /solve` and each
  `GET /result/{job_id}` (opportunistic); no background cron/timer.
- **systemd unit exactness (S13).** `ExecStart=%h/venvs/lp-solver/bin/python -m
  kbutillib.services.lp_solver.app --host 127.0.0.1 --port 8091`,
  `WorkingDirectory=%h/projects/KBUtilLib`. KBUtilLib is installed editable in the
  `~/venvs/lp-solver` venv, so **no `PYTHONPATH` override is needed**; the `app`
  module exposes a `__main__`/CLI entrypoint that starts uvicorn. Commit the unit
  file template and a short deploy/enable script into the repo (e.g. under
  `kbutillib/services/lp_solver/deploy/`) so operator steps aren't ad hoc.
- **Test solver availability (S14).** `solver_backends` correctness tests run only
  where `gurobipy`/`cplex` import successfully (the H100 runner); elsewhere they
  **skip with a logged note** rather than fail. Non-solver tests (job_store,
  client, e2e-with-stub) run everywhere.
- **Config surface (S15).** Both client and service read `~/.kbutillib/config.yaml`
  under `remote_solver.*`: `base_url`, `timeout`, `poll_interval`, `api_key`,
  `max_concurrent_solves`, `max_queue_depth`, `threads_per_solve`,
  `default_time_limit`, `max_time_limit`. Missing keys fall back to the documented
  defaults.
- **Client base_url/tunnel (S16).** Client default `base_url` is
  `http://127.0.0.1:8091`. If TunnelManager forwards to a different local port, the
  caller sets `remote_solver.base_url` in config accordingly.
- **Queue-depth definition (S17).** `max_queue_depth` counts only `queued` jobs;
  `running` jobs are excluded from the admission limit.

## Testing Decisions

Test external behavior, not implementation details. Two deep modules are the
prime targets:

- **`solver_backends`** — the correctness core. Feed small LPs with known
  outcomes and assert:
  - a known-optimal LP → `status: "optimal"`, correct `objective_value`, and
    `variables` keyed by the **exact** submitted names (name round-trip);
  - a known-infeasible LP → `infeasible`; a known-unbounded LP → `unbounded`;
  - a tiny problem with a near-zero `time_limit` → `timeout` (incumbent behavior
    where applicable);
  - the Gurobi `INF_OR_UNBD` path disambiguates correctly;
  - an unrecognized/forced-failure path → `status: "error"` with a populated
    `error` (never a silent `optimal`).
  Run against whichever of Gurobi/CPLEX is importable in the test environment;
  skip the other with a marker if unavailable.
- **`job_store`** — state machine over SQLite: `create → claim → running →
  done/error` transitions; `sweep_expired` honors the 48h TTL; `reap_orphans_on_startup`
  turns a `running` row into `error` with the restart message. These are
  filesystem/DB behaviors that are easy to get wrong and worth pinning.
- **Client (`MSRemoteSolverUtils`)** — one integration test against a stubbed
  HTTP server: submit → poll → returns the expected dict; verifies gzip on
  submit and the poll-until-terminal loop, including a `TimeoutError` when the
  server never terminates.
- **End-to-end** — one test that boots the FastAPI app in-process (TestClient or
  a live uvicorn on an ephemeral port) with a stub/real backend, submits a small
  LP, and polls to an `optimal` result — exercising `app.py` + `worker.py` glue
  without dedicated unit tests for those thin layers.

Prior art in-repo: `ArgoUtils` is the closest existing pattern for the client
(submit/poll, retry/backoff) and for its test approach; `KBBERDLUtils` shows the
config-driven-endpoint + structured-return-dict convention. Follow the existing
KBUtilLib test layout/conventions for these.

## Out of Scope

- SQS / Courier / any AWS transport (explicitly rejected).
- S3 or Dropbox large-payload offload (HTTP has no size cap, so unneeded).
- Headless callers (emailmac/h100-local Maestro workers) — v1 is
  interactive-laptop-only; adding them later is additive tunnel provisioning,
  not a redesign.
- LP-file *generation* from cobra/optlang models — the caller owns this; the
  service takes raw LP text. (A thin client-side `model_to_lp` convenience could
  be a later add but is not part of v1.)
- Cross-solver automatic fallback on solve outcome.
- MPS input format (LP format chosen); other solvers beyond Gurobi/CPLEX.
- Defensive server-side variable-name validation (caller guarantees LP-legal
  names in v1).
- Multi-user auth/quotas beyond the optional single bearer token.
- Exposing the service beyond localhost / the SSH tunnel.

## Further Notes

- The immediate consumer is laptop-side FBA/FVA where GLPK is too slow and
  `cobra.flux_variability_analysis` is broken (`ms_fba_utils.run_fva` is the
  known workaround). This service unblocks fast genome-scale/community MILP solves
  from the laptop.
- Compression asymmetry established during design: solutions gzip well under
  256KB in all realistic cases; genome-scale LP *requests* do not (1–3MB raw →
  200–500KB gzipped → over the SQS cap after base64). This asymmetry is the core
  reason HTTP-over-tunnel wins over SQS.
- Name-legality landmine to keep in mind if the input contract ever loosens:
  CPLEX LP format reserves `[ ] + - * / < > =` and whitespace, and forbids
  leading digits. Compartment brackets (`glc[c]`) are the classic break. FBA
  reaction variables are LP-legal; the caller guarantees this in v1.
- H100 supervision facts were verified live via Maestro researcher task
  `task-1da6c8ff`; full evidence report committed at
  `KBUtilLib:agent-io/research/h100-service-supervision.md` (on that task's
  branch, unmerged).

## Acceptance Criteria

1. `kbu.remote_solver.solve_lp(lp_text_or_path, solver=None, time_limit=None)` returns a dict with exactly the keys `status`, `objective_value`, `variables`, `solver`, `solve_time_s`, `error` and no others.
2. Submitting a known-optimal LP returns `status == "optimal"`, the correct `objective_value`, and a `variables` dict whose keys are byte-identical to the variable names in the submitted LP text.
3. Submitting a known-infeasible LP returns `status == "infeasible"`; a known-unbounded LP returns `status == "unbounded"`; neither is silently retried on the other solver.
4. A Gurobi `INF_OR_UNBD` result is disambiguated by a single in-place re-solve with `DualReductions=0` within the original time budget, yielding a definitive `infeasible` or `unbounded`, and `solve_time_s` reflects both runs.
5. Any solver outcome not in the whitelist (numeric trouble, interrupted, license failure, crash, exception) returns `status == "error"` with a populated `error` string and never a wrong `optimal`; `error` is `None` for all non-error statuses.
6. `solver=None` runs Gurobi; `solver="gurobi"|"cplex"` runs exactly that engine; the returned `solver` field names the engine that actually ran.
7. A solve that hits its time limit returns `status == "timeout"` with the incumbent's `objective_value` and full `variables` when a feasible incumbent exists, else `None`/`{}`.
8. `time_limit` is clamped to `[0, max_time_limit]` (default ceiling 7200s); when omitted, `default_time_limit` (3600s) is applied and passed to the solver's native time limit.
9. A solve still running at `time_limit + 60s` is hard-killed (SIGTERM then SIGKILL after 5s) in its own subprocess and its job is marked `error`.
10. `POST /solve` accepts a gzip-encoded LP body (`Content-Encoding: gzip`, `Content-Type: text/plain; charset=utf-8`) and returns `{job_id}`; a multi-MB LP transfers and solves without a size-limit error.
11. `GET /result/{job_id}` returns 200 + JSON for any known job (with `status` = `queued`/`running`/terminal) and 404 for an unknown job_id.
12. With `max_concurrent_solves` (default 5) solves in flight, additional submits are accepted and FIFO-queued as `queued`; `POST /solve` returns HTTP 503 once `queued` jobs (running excluded) exceed `max_queue_depth` (default 50).
13. Each solve is capped at `threads_per_solve = max(1, floor(os.cpu_count() / max_concurrent_solves))` via the solver's native thread parameter.
14. Jobs and results persist in `~/.lp-solver/jobs.sqlite` (WAL mode, busy-timeout retry); `job_id` is a UUIDv4 string; LP temp files live at `~/.lp-solver/tmp/{job_id}.lp` and are removed on completion or TTL sweep.
15. On service startup, any job left in `running` is transitioned to `error` with `error == "service restarted during solve"`, so no job polls forever.
16. Completed results older than 48h are removed by `sweep_expired()`, invoked opportunistically on each `POST /solve` and `GET /result/{job_id}` (no background cron).
17. The client polls `GET /result/{job_id}` every 2.0s (±10% jitter) and raises a client-side `TimeoutError` only after `min(requested_time_limit, max_time_limit) + 90s`, i.e. always after the server's own watchdog would have produced a terminal result.
18. The optional `x-api-key` header is required only when `remote_solver.api_key` is configured; when set, requests without the matching header are rejected.
19. Client and service both read configuration from `~/.kbutillib/config.yaml` under `remote_solver.*`, and every unset key falls back to its documented default.
20. The service is deployable on H100 as a user-level systemd unit (`~/.config/systemd/user/lp-solver.service`) with the pinned `ExecStart`/`WorkingDirectory`, bound to `127.0.0.1:8091`, and a repo-committed unit template + enable script exist under the service package.
21. `solver_backends` correctness tests run where `gurobipy`/`cplex` import and skip-with-note elsewhere; `job_store`, client-integration, and end-to-end (stub-backend) tests run in any environment.
