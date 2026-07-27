# KBUtilLib Reorg Integration — Human Summary

**Goal:** Adopt Vibhav's `feature/reorg-api-mcp-explore` branch as the new KBUtilLib
mainline (whole branch, as-is), re-land our LP-solver + ARGO delta on top, net downstream
consumers so nothing breaks silently, validate, and land it live.

## What the branch is (3 layers, all adopted)
1. **Structural reorg** — flat `*_utils.py` → `domains/` + `core/` + `interfaces/` +
   `agents/` (9 domains).
2. **Capability layer (the point)** — a `CapabilityRegistry` + `@capability` decorator +
   4 transports reading one registry: Python-import, CLI (`kbu cap …`), MCP (`kbu-mcp`),
   HTTP (`kbu-api`). New at base.
3. **Net-new science** — cheminformatics/verab, predictive-thermo, network-expansion.

## What survives (fears didn't materialize)
- `kbu` CLI + all verb groups preserved (`verab` is additive, not a rename) → **KING safe**.
- `session.kbu.*` idiom preserved (`toolkit.py` repointed in lockstep).
- Top-level re-exports preserved (`from kbutillib import MSFBAUtils` works).
- 5 package shims survive (`kbutillib.notebook`, `.cli`, `.cheminformatics`, `.researchos`,
  `.thermo_predictors`).

## What breaks (narrow)
- Flat **submodule** imports (`from kbutillib.ms_fba_utils import X`) — shims deleted.
- Known casualties: shipped `kbu-fba/SKILL.md:62`, and ModelingLOE/Watershed `util.py`.
- Silent-`None` hazard: missed `__init__.py` re-export degrades to `NoneType`, not
  `ImportError`.

## Our delta to re-land (small — mostly already on `main`)
`main` already has the full LP-solver stack; only the ARGO fix is `wip`-only. Re-apply into
new locations: LP-solver service layer (reorg lacks it), client → `domains/modeling/`,
`remote_solve` wiring, ARGO fix → `domains/ai/argo_utils.py`, keep `httpx[socks]>=0.28`.
Four conflict hotspots: `argo_utils.py` (renamed — highest risk), `__init__.py`,
`toolkit.py`, `pyproject.toml`.

## Decisions (this session)
- **Scope:** whole branch as-is.
- **Servers:** accept as-is; default-bind loopback (auth = later debt).
- **Migration:** fix known breaks + add flat-submodule **deprecation shim** (warn, not
  break) + grep sweep + consumer-migration report.

## The one new thing we build
A **table-driven flat-submodule deprecation shim** — legacy import paths warn and
re-export from the new domain path, generated from the old→new mapping. Plus a
silent-`None` guard test.

## Landing
Land on `main` → merge `main → wip` (live on local parking repo) → re-sync skills via
`claude-skills sync`. Recommend **interactive `/ai-conductor`** (held) given the stakes;
verify the landed tree by content, not status.
