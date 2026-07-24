# Changelog

All notable changes to KBUtilLib are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased] — Package reorganization (branch: feature/reorg-api-mcp-explore)

### Changed

- **Package structure rewritten** from a flat layout (`src/kbutillib/*.py`) to a
  domain-organized hierarchy. All implementations now live under `domains/`, shared
  infrastructure under `core/`, and transport adapters under `interfaces/`.
- **`toolkit.py`** updated to import all `*Impl` classes from canonical `domains/` and
  `core/` paths instead of the former flat modules.
- **`__init__.py`** updated to re-export all public classes from the new canonical paths;
  the top-level `from kbutillib import <ClassName>` surface remains stable.

### Added

- `core/` package: `CapabilityRegistry`, `@capability` decorator, pydantic v2 `Config`,
  `BackendUnavailableError`, `CapabilityError`, `BaseUtils`, `SharedEnvUtils`,
  `DependencyManager`.
- `domains/` with nine subpackages: `biochem`, `thermo` (incl. `thermo_predictors/`),
  `cheminformatics` (incl. `verab/`), `modeling`, `genome` (incl. `annotation/`),
  `external`, `kbase`, `ai`, `notebook`.
- `interfaces/` transport adapters: `mcp/server.py` (`kbu-mcp` stdio MCP server),
  `api/app.py` (`kbu-api` FastAPI HTTP server), `cli/` (Click root + `kbu cap` subcommands
  + `kbu new-capability`), `docs/` (mkdocs capability catalog generators).
- `pyproject.toml` console scripts: `kbu`, `kbu-mcp`, `kbu-api`.
- Optional dependency extras: `[mcp]`, `[api]`, `[apidocs]`, `[all]`.
- `environment.yml` for reproducible conda environment setup (`kbutillib-reorg`, Python 3.11).
- `agents/king_app/verab/` — Verab KING agent bundle (JSON + skill markdown).
- `deploy/poplar/` — systemd + nginx service definitions for `kbu-api`.
- `deploy/docker/` — docker-compose setup.
- Final predictive thermodynamics facade (`PredictiveThermoUtils`) and updated backends
  (`equilibrator`, `modelseed`, `modelseed_db`, `dgpredictor`, `molgpk`) landed in
  `domains/thermo/thermo_predictors/`.

### Removed

- All flat module files at `src/kbutillib/*.py` except the five infrastructure files
  (`__init__.py`, `__main__.py`, `toolkit.py`, `layout.py`, `compartments.py`).
- Temporary backward-compatibility shims that bridged the old flat paths during the
  migration phases (A–C).

### Migration

Flat submodule imports (`from kbutillib.ms_biochem_utils import ...`) no longer resolve
to standalone files. Use either:

```python
from kbutillib import MSBiochemUtils                              # stable top-level re-export
from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtils  # canonical domain path
```
