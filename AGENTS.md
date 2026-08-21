# AGENTS.md — repository map for humans and AI agents

KBUtilLib is a domain-organized Python utility library for KBase bioinformatics
(biochemistry, metabolic modeling, thermodynamics, genome analysis, cheminformatics,
AI/LLM workflows). Every unit of functionality is registered once with the
`@capability` decorator and is then served unchanged by three transports — the `kbu`
CLI, an MCP stdio server, and a FastAPI HTTP API. Implementations live in
`src/kbutillib/domains/*`; transports live in `src/kbutillib/interfaces/*`; the glue
is the registry in `src/kbutillib/core/`. Read this file plus the one or two package
READMEs it points at — you should not need to read source to orient.

## Start here

| File | Why |
|------|-----|
| [README.md](README.md) | Architecture diagram, install, quick start, package structure |
| [GETTING_STARTED.md](GETTING_STARTED.md) | Fresh clone → working `kbu` command, step by step |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Repo layout rules, add a capability, add a domain, testing, style |
| [src/kbutillib/core/README.md](src/kbutillib/core/README.md) | The foundation layer and the dependency rule everything obeys |
| [src/kbutillib/domains/biochem/README.md](src/kbutillib/domains/biochem/README.md) | Reference `@capability` implementation to copy |
| [src/kbutillib/interfaces/cli/README.md](src/kbutillib/interfaces/cli/README.md) | The canonical `kbu` CLI |
| [docs/](docs/) | Rendered docs (`mkdocs serve`): usage, capability catalog, API reference |

## Repository map

Top level:

| Path | Purpose |
|------|---------|
| `src/kbutillib/` | The package (see next table) |
| `tests/` | pytest suite, mirrors the package layout (`tests/domains`, `tests/interfaces`, `tests/core`, …) |
| `docs/` | mkdocs sources; `docs/mcp-catalog.md` is generated |
| `deploy/` | Deployment assets — [`deploy/README.md`](deploy/README.md), `deploy/poplar/` (systemd + nginx), `deploy/docker/compose.yaml` |
| `examples/`, `notebooks/`, `templates/`, `scripts/`, `bin/` | Samples, notebooks, project templates, helper scripts |
| `data/`, `machine_configs/` | Static data and per-machine configuration |

Inside `src/kbutillib/`:

| Package | Purpose | Guide |
|---------|---------|-------|
| `core/` | Registry, `@capability`, errors, config, `BaseUtils`, `SharedEnvUtils` | [README](src/kbutillib/core/README.md) |
| `domains/` | All implementations, one sub-package per domain | per-domain READMEs below |
| `interfaces/` | Transport adapters only — CLI, MCP, API, docs generators | per-interface READMEs below |
| `agents/` | KING agent bundles + ResearchOS config | [README](src/kbutillib/agents/README.md) |
| `notebook/` | Provenanced notebook cache, vector store, experiment tracking | [README](src/kbutillib/notebook/README.md) |
| `services/lp_solver/` | Standalone LP solver service (app, worker, job store, backends) | — |
| `harness/`, `kb_job_utils/`, `kb_app_runner/`, `beril_worktree/` | Local execution harness, EE2 job tracking/submission, BERIL worktree manager | — |
| `installed_clients/`, `data/` | Generated KBase service clients; packaged data | — |
| `toolkit.py` | `KBUtilLib` facade — one lazy property per utility class | — |

Domains (`src/kbutillib/domains/<name>/README.md` for each):

| Domain | Contents |
|--------|----------|
| `biochem` | ModelSEED compound/reaction lookup, biochemistry search, reaction similarity |
| `modeling` | FBA, reconstruction, templates, standardization, KBase model I/O |
| `thermo` | ModelSEED thermodynamic data + multi-backend ΔG prediction |
| `cheminformatics` | Network expansion via reaction rules (Pickaxe, RetroRules), Verab |
| `genome` | Genome/annotation utilities, MMseqs2, skani, ontology mapping, PLMs |
| `kbase` | Workspace API, SDK calls, narrative audit, catalog, endpoints, BERDL |
| `ai` | Argo LLM gateway, AI-assisted curation |
| `external` | BVBRC/PATRIC, RCSB PDB, UniProt clients |
| `notebook` | Provenanced caching, vector storage, Escher rendering |

Interfaces: [`cli`](src/kbutillib/interfaces/cli/README.md) · [`mcp`](src/kbutillib/interfaces/mcp/README.md) · [`api`](src/kbutillib/interfaces/api/README.md) · [`docs`](src/kbutillib/interfaces/docs/README.md)

## Where the wiring lives

| Concern | Symbol | Location |
|---------|--------|----------|
| Facade with lazy properties | `class KBUtilLib` | `src/kbutillib/toolkit.py:66` |
| Capability decorator | `capability()` | `src/kbutillib/core/capability.py:92` |
| Populating the registry | `register_all()` | `src/kbutillib/core/capability.py:364` |
| Registry + spec | `CapabilityRegistry`, `CapabilitySpec`, `get_registry()` | `src/kbutillib/core/registry.py:129`, `:42`, `:270` |
| CLI (`kbu`) | Click group | `src/kbutillib/interfaces/cli/__init__.py` |
| MCP server (`kbu-mcp`) | `build_server()`, `main()` | `src/kbutillib/interfaces/mcp/server.py:202`, `:347` |
| HTTP API (`kbu-api`) | `build_app()`, `main()` | `src/kbutillib/interfaces/api/app.py:183`, `:389` |
| Docs generators | capability pages, MCP catalog | `src/kbutillib/interfaces/docs/gen_capabilities.py`, `.../mcp_catalog.py` |
| Console scripts | `kbu`, `kbu-mcp`, `kbu-api` | `pyproject.toml:154-157` |

## How to probe without reading many files

All commands assume the conda env below.

```bash
kbu --help                       # every CLI verb
kbu cap list                     # every registered capability + availability
kbu cap info <capability.name>   # signature, args, docstring
kbu doctor                       # optional-dependency and registry health, one line each
```

```bash
# the toolkit surface (one lazy property per utility class)
python -c "from kbutillib import KBUtilLib; print([p for p in dir(KBUtilLib) if not p.startswith('_')])"

# the registry, programmatically (register_all takes a KBUtilLib instance)
python -c "from kbutillib import KBUtilLib; from kbutillib.core.capability import register_all; \
from kbutillib.core.registry import get_registry; register_all(KBUtilLib()); \
print([s.name for s in get_registry().list()])"

# where a domain implementation lives
grep -rl "class MSBiochemUtils" src/kbutillib/domains
```

```bash
# regenerate the MCP catalog page after adding capabilities
python -m kbutillib.interfaces.docs.mcp_catalog --output docs/mcp-catalog.md
```

## Conventions

- **`domains/*` holds implementations; `interfaces/*` holds transports.** A transport must
  never contain domain logic; it reads the registry.
- **One registration point.** Decorate a method with `@capability` and all three transports
  expose it — no per-transport wiring.
- **Deprecated shims.** The ~40 flat `*.py` modules directly under `src/kbutillib/` (plus
  `cheminformatics/`, `cli/`, `researchos/`, `thermo_predictors/`) are auto-generated
  re-export shims kept for backward compatibility. Never add code there; import from
  `kbutillib.domains.*` / `kbutillib.interfaces.*`.
- **Optional dependencies are lazy.** Heavy/optional packages are imported inside
  `KBUtilLib` properties and grouped into pip extras (`api`, `mcp`, `ai`, `apidocs`,
  `lp_solver`, `reaction_similarity`, `reaction_similarity_extra`, `all`). A missing extra
  degrades one capability, never the import of `kbutillib`; `kbu doctor` and the
  `AVAILABLE` column of `kbu cap list` tell you what is missing.
- **Tests mirror the package.** A change in `src/kbutillib/domains/<d>/` belongs with tests
  in `tests/<d>/` or `tests/domains/`.

## Common tasks

| Task | Do this |
|------|---------|
| Add a capability | `kbu new-capability` scaffolds the stubs; rules in [CONTRIBUTING.md](CONTRIBUTING.md) → *Add a new domain capability* |
| Add a domain | [CONTRIBUTING.md](CONTRIBUTING.md) → *Add a new domain* |
| Set up from scratch | [GETTING_STARTED.md](GETTING_STARTED.md) |
| Run tests | `pytest -q tests/` (scope it: `pytest -q tests/biochem`) — see [CONTRIBUTING.md](CONTRIBUTING.md) → *Testing* |
| Lint / format | `ruff check <paths>` and `ruff format <paths>` — see [CONTRIBUTING.md](CONTRIBUTING.md) → *Code style* |
| Build docs | `mkdocs serve` (needs the `apidocs` extra) — see [CONTRIBUTING.md](CONTRIBUTING.md) → *Docs* |
| Deploy | [deploy/README.md](deploy/README.md) |

## Environment

Use the existing conda environment **`kbutillib`** — it is the only one with the test and
runtime dependencies installed:

```bash
conda run -n kbutillib pytest -q tests/
conda run -n kbutillib kbu doctor
```

Do not create, modify, or delete conda environments, and do not install or upgrade
packages on this shared machine without explicit approval.
