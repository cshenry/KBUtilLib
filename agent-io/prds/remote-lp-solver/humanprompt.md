# Remote LP-Solver Service — Human Prompt

## Origin

H100 has working CPLEX and Gurobi installations; primary-laptop will never get
them (license restriction / incompatibility). I want to offload LP/MILP solves
from the laptop to H100's commercial solvers.

Build:
1. A service on H100 that accepts an LP file, runs Gurobi or CPLEX, and returns
   the solution in this exact schema:

   ```
   {
     "status": "optimal" | "infeasible" | "unbounded" | "timeout" | "error",
     "objective_value": float | None,
     "variables": {"<var_name>": value, ...},   # every variable, keyed by the
                                                 # SAME names in the LP I sent
     "solver": "gurobi" | "cplex",
     "solve_time_s": float,
     "error": str | None,   # populated only when status == "error" --
                            # never silently drop a failure
   }
   ```

2. A KBUtilLib client API that submits an LP and polls for the result. The
   service runs asynchronously.

## Key question raised

"How do we communicate? Can we leverage SQS without pulling all of Courier in as
a dependency?"

## Answer reached (design session 2026-07-20/21)

**Not SQS — HTTP over an SSH tunnel.** Three converging reasons:

- SQS caps a message at 256KB; genome-scale LP files blow past that even
  gzipped (the response compresses fine, but the *request* does not). Using SQS
  would force an S3 large-payload offload we don't otherwise need.
- KBUtilLib has zero boto3/SQS footprint; adding it means net-new AWS creds +
  a reply-poller (Courier's RPC needs its daemon on both ends, so it isn't
  liftable) + a large-payload store.
- KBUtilLib already contains the exact client shape — `ArgoUtils` — an HTTP
  submit-then-poll client over a local tunnel. The new client mirrors it.

The commercial-solver licenses on H100 are unlimited but require callers to have
login capacity on the machine, which makes the SSH tunnel a *licensing-compliance
boundary*, not merely transport.

## Decisions locked

- Transport: HTTP-over-SSH-tunnel; v1 callers = interactive primary-laptop only.
- Both client and FastAPI service live in KBUtilLib (one repo, shared schema).
- Input: raw CPLEX-LP-format text from the caller (service is cobra-agnostic).
- Name fidelity: trust the solver's own variable names (LP-legal names
  guaranteed by caller); no defensive re-check in v1.
- Solver selection: default Gurobi; explicit honored; no cross-solver fallback;
  infra failure → error.
- Concurrency: up to 5 simultaneous solves; per-solve thread cap
  floor(cores / max_concurrent) to avoid oversubscribing H100.
- Timeouts: default 3600s, ceiling 7200s, +60s watchdog hard-kill, incumbent
  returned on clean timeout.
- Persistence: SQLite job store, LP on disk, 48h result TTL, orphan-on-restart
  → error.
- Deployment: user-level systemd unit on H100 (verified pattern), dedicated venv
  ~/venvs/lp-solver, bound 127.0.0.1:8091.
