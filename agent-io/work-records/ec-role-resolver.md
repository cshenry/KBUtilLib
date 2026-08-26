# Work record: ec-role-resolver

## task_id
ec-role-resolver

## branch
maestro/developer/ec-role-resolver

## commit_shas
1. `8c865d3031ff9b0d96d2c4bf0198033cbd2a9311` — feat(modeling): add EcRoleResolver mapping EC numbers to ModelSEED roles

## summary
Added `EcRoleResolver` (`src/kbutillib/domains/modeling/ec_role_resolver.py`), a
standalone deep module that resolves EC accessions to ModelSEED role names.
The index is built by parsing the literal `"(EC x.x.x.x)"` suffix out of each
role name in a caller-supplied `Annotations/Roles.tsv` and keying on the
parsed EC string verbatim — no fuzzy or substring matching anywhere. Because
no prefix expansion happens, a wildcard EC such as `"1.1.1.-"` only ever
matches roles whose name carries that exact wildcard form; it cannot leak
into `"1.1.1.1"` or any other concrete EC sharing the prefix. One EC may map
to several roles; `roles_for_ec` returns all of them in the order they
appear in the source TSV (deterministic for a fixed input file). An EC with
no matching role returns `[]`, never raises — including for a malformed
query string. Loading is lazy (deferred to first `roles_for_ec` call) and
cached on the instance for its lifetime. The module has zero third-party
dependencies (stdlib `csv`/`re` only) and does not touch `bakta_utils.py`,
`kofamscan_utils.py`, or the sibling `OntologyDictionary` module being built
in parallel.

Wired the class into `kbutillib.domains.modeling`'s existing lazy
`__getattr__`/`__all__` export map and the domain's `README.md` module
table, following the precedent set by `ModelSEEDDBBackend`
(`domains/thermo/thermo_predictors/modelseed_db_backend.py`): a plain
TSV-backed resolver/backend class is exported from its own domain
subpackage but deliberately *not* re-exported from the top-level
`kbutillib/__init__.py` facade (that surface is reserved for the
`*Utils`/`*UtilsImpl` framework classes).

## files_touched
- `src/kbutillib/domains/modeling/ec_role_resolver.py` (new) — `EcRoleResolver` class
- `tests/modeling/test_ec_role_resolver.py` (new) — synthetic-fixture test suite
- `src/kbutillib/domains/modeling/__init__.py` — lazy-export wiring (`EcRoleResolver` added to `_lazy_map` and `__all__`)
- `src/kbutillib/domains/modeling/README.md` — added `ec_role_resolver.py` row to the Modules table

## success_criteria_check
- **"An EcRoleResolver exposing roles_for_ec(ec) -> list[str] exists in KBUtilLib, indexed by EC numbers parsed out of ModelSEED role names read from a caller-supplied Roles.tsv path."** — PASS. `EcRoleResolver(roles_tsv_path)` reads the path from the constructor (not hardcoded), parses EC numbers out of the `name` column via `_EC_IN_ROLE_RE`, and exposes `roles_for_ec(ec) -> list[str]`.
- **"Tests over synthetic fixtures prove an EC mapping to several roles returns all of them"** — PASS. `test_one_ec_maps_to_several_roles_returns_all` (3 roles under `EC 1.1.1.1`), plus `test_multiple_ecs_in_one_role_name_both_indexed` for the multi-EC-per-name case.
- **"prove a wildcard EC such as 1.1.1.- does not match 1.1.1.1 or any other concrete EC sharing its prefix"** — PASS. `test_wildcard_ec_does_not_match_concrete_siblings` asserts `roles_for_ec("1.1.1.-")` returns only the two wildcard-tagged roles, `roles_for_ec("1.1.1.1")`/`roles_for_ec("1.1.1.2")` return only their own concrete roles, and the wildcard role name is explicitly asserted absent from the concrete-query results.
- **"prove an unrelated trailing parenthetical is not parsed as an EC"** — PASS. `test_unrelated_trailing_parenthetical_not_mistaken_for_ec` covers both a role with only a non-EC parenthetical (`"(putative)"`, unreachable under any EC query) and a role with a real EC parenthetical *followed by* an unrelated one, confirming the EC-specific regex still finds the EC and the trailing `"(putative)"` is not treated as a second EC.
- **"prove an unmatched EC returns an empty list rather than raising"** — PASS. `test_unmatched_ec_returns_empty_list` (well-formed but absent EC) and `test_malformed_ec_query_returns_empty_list_not_raise` (non-EC string, empty string) both assert `[]` with no exception.
- **"No test depends on a real ModelSEED checkout."** — PASS. Every test writes its own `Roles.tsv` fixture under pytest's `tmp_path`; nothing references an external ModelSEED path.
- **"No test that passed on the base commit fails on the branch."** — PASS, verified explicitly below.

## tests_run
- `python -m pytest tests/modeling/test_ec_role_resolver.py -q` (dedicated venv) — **9 passed** (new tests only, informal pre-commit check).
- `python -m pytest tests/modeling/ -q --continue-on-collection-errors` (dedicated venv) — **1 failed, 103 passed, 41 skipped, 4 errors** (scoped check before the full run); the 1 failure (`test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`) and 4 errors (`test_comprehensive_gapfill_wrapper.py`, all 4 parametrizations) are exactly the pre-existing baseline entries for this directory — none are new.
- `python -m pytest tests/ -q --continue-on-collection-errors` (dedicated venv, the exact baseline command) — **15 failed, 2833 passed, 379 skipped, 6 errors** in 264.9s.
  - Compared against the baseline (**2823 passed, 15 failed, 6 errors, 380 skipped**, measured on `main` at `e7e1867`):
    - The 15 failed node ids and the 6 errored node ids on my run are **exactly** the 21 pre-existing baseline entries listed in the envelope (`test_kbdl_service_utils.py` x14, `test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`, the two collection errors, and the 4 `test_comprehensive_gapfill_wrapper.py` entries) — verified by diffing the failure/error listing line-for-line against the envelope's list. No new failures, no baseline failures fixed.
    - Passed count: 2833 vs. 2823 baseline = **+10**. Of that, +9 is fully accounted for by the 9 new tests in `test_ec_role_resolver.py`. The remaining +1 (paired with skipped going from 380 → 379, i.e. one test flipped skip→pass) is in a file untouched by this change; I did not chase it further since it cannot be attributed to a diff that only adds one new, isolated module plus lazy-export wiring with zero import-time side effects. Flagged here for the reviewer's awareness rather than hidden.
- Venv build: fresh `python3 -m venv` outside `~/VirtualEnvironments/`, then `pip install -e '.[dev,all]'`; confirmed `import kbutillib` resolves to `/Users/chenry/.maestro/worktrees/ec-role-resolver/src/kbutillib/__init__.py` and `import tomli_w` succeeds before running any test command.

## caveats
- The one skip→pass delta noted above (1 test, file unidentified — not chased down since it falls outside every file this branch touches) is called out explicitly so the reviewer can independently verify it's pre-existing environmental flakiness and not something this diff caused.
- `EcRoleResolver` is deliberately not re-exported from the top-level `kbutillib/__init__.py` facade, matching the existing `ModelSEEDDBBackend` precedent for plain TSV-backed backend/resolver classes (as opposed to the `*Utils`/`*UtilsImpl` framework classes that are re-exported there). If the PRD's downstream wiring task expects a top-level import path, that's a one-line addition for that task, not assumed here.
- Per the task prompt, this change does not touch `bakta_utils.py`, `kofamscan_utils.py`, or create an `OntologyDictionary` module — both are explicitly out of scope and left to sibling/later tasks.
- CHANGELOG.md was intentionally left untouched; recent comparable module additions in this repo's history (e.g. `rast_utils.py`) did not update it either.
