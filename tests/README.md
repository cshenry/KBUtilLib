# KBUtilLib Test Suite

This directory contains all tests for KBUtilLib, organized into themed subdirectories.

## Running Tests

### Run all tests
```bash
source /path/to/anaconda3/etc/profile.d/conda.sh && conda activate kbutillib
python -m pytest tests/ -v
```

### Run a specific subdirectory
```bash
python -m pytest tests/verab/ -v
python -m pytest tests/modeling/ -v
python -m pytest tests/core/ -v
```

### Run quickly (no verbose output)
```bash
python -m pytest tests/ -q --tb=no
```

### Run with short tracebacks on failures
```bash
python -m pytest tests/ -q --tb=short
```

## Test Directory Structure

| Directory | Files | What It Covers |
|-----------|-------|----------------|
| `annotators/` | 4 | Genome annotation tools (PROKKA, RAST, COG, etc.) |
| `beril_worktree/` | 4 | Beril deployer and skill bundle management |
| `biochem/` | 5 | Biochemistry tools: FBA templates, biochem delta-G, escher visualization, registry |
| `cli/` | 28 | CLI commands: adopt-inventory, doctor, venv-doctor, and the full command set |
| `core/` | 9 | Core framework: capability registry, config, layout, composition, docs generators |
| `domains/` | 11 | Domain-level tests (WP16 reorganization) |
| `external/` | 2 | External tool integrations: MMseqs2, OntomapUtils |
| `guard/` | 1 | Guard/safety layer tests |
| `harness/` | 2 | Test harness utilities |
| `interfaces/` | 2 | API and MCP server interface tests |
| `kb_app_runner/` | 1 | KBase app runner tests |
| `kbase/` | 11 | KBase platform integration: workspace, models, genomes, auth, BEr-DL, PLM, narrative provenance |
| `kb_job_utils/` | 6 | Job management: job store, LP solver backends, job list/dict utilities |
| `modeling/` | 7 | Metabolic modeling: gapfilling, FBA, network expansion, thermo, flux loops |
| `notebook/` | 11 | Jupyter notebook utilities |
| `researchos/` | 1 | ResearchOS integration tests |
| `verab/` | 6 | verAB lignin-degradation pathway: rule discovery, CLI, screening, KING workflow |

## Fixture Discovery

The root `tests/conftest.py` is automatically discovered by pytest for all subdirectories, so shared fixtures are available everywhere without per-subdirectory `conftest.py` files.

## Optional-Dependency Tests

Some tests require optional packages and will be **skipped** if those packages are not installed:

- **RDKit** — required by `tests/verab/` (substructure matching, SMARTS transforms), some `tests/biochem/` tests.
- **cobra / modelseedpy** — required by `tests/modeling/` (FBA, gapfilling). Tests skip gracefully if absent.
- **requests_toolbelt** — required by `tests/kbase/` upload streaming tests.
- **escher** — required by `tests/biochem/test_escher_utils.py`.
- **KBase tokens / live services** — tests requiring real KBase credentials are marked and skip without a valid token.

## Collection Errors vs Failures

- **Collection errors** = a test file cannot even be imported (import error). These should be zero.
- **Test failures** = the test ran but produced wrong results or raised an unexpected exception.
- **Skips** = the test was intentionally skipped due to missing optional deps or marks.

To check collection health:
```bash
python -m pytest tests/ --collect-only -q 2>&1 | tail -3
```

## Adding New Tests

1. Place new test files in the appropriate subdirectory.
2. Follow the naming convention: `test_<module_name>.py`.
3. Use fixtures from `tests/conftest.py` or add a local `conftest.py` if a fixture is subdir-specific.
4. Optional-dep tests should guard imports with `pytest.importorskip()` or `@pytest.mark.skipif`.
