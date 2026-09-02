# Work record: checkm2-kbutillib-wrapper

- **task_id**: checkm2-kbutillib-wrapper
- **branch**: `maestro/developer/checkm2-kbutillib-wrapper`
- **base**: `main` @ `7b9c02e4921f4c266ae5a32e91a76a0a3f432970`

## commit_shas (chronological)

1. `e006e6719cc67e01bd1bbd744557a194dc789bc0` — feat(genome): add CheckM2Utils Docker wrapper for genome quality prediction

## files_touched

- `src/kbutillib/domains/genome/checkm2_utils.py` (new)
- `tests/annotators/test_checkm2_utils.py` (new)
- `src/kbutillib/domains/genome/__init__.py` (registered `CheckM2Utils` in the lazy-import map and `__all__`, matching the existing convention for `SKANIUtils`/`OntomapUtils`/etc.)

## summary

Added `CheckM2Utils` at `src/kbutillib/domains/genome/checkm2_utils.py` (a
sibling of `skani_utils.py`, not inside `annotation/`) wrapping CheckM2
v1.1.0's `predict` entry point via Docker. It follows the established
Dockerised-tool-wrapper convention from `bakta_utils.py`/
`kofamscan_utils.py`: a `checkm2.docker_image` config key (empty ⇒
unavailable, never hardcoded), a `docker image inspect` availability probe
(`check_availability()`), and `ToolUnavailableError` — imported from
`annotation/annotator_utils.py`, not a new exception class — raised by
`get_version()`/`predict()` when the image is absent. The container's
entrypoint (`micromamba run -n checkm2 checkm2`) is overridden to
`micromamba` (mirroring `bakta_utils.py`'s `--entrypoint bash` override) so
the inner `checkm2 predict`/`checkm2 --version` command can be built
explicitly, with `--user <uid>:<gid>`, `--network none`, the work dir
bind-mounted to `/work`, and the database directory bind-mounted read-only
to `/db`.

Because CheckM2 does not produce `Term`/`AnnotationRecord` output (it scores
whole genomes for completeness/contamination, not per-gene function),
`CheckM2Utils` subclasses `SharedEnvUtils` directly rather than
`AnnotatorUtils` — the same choice already made for `SKANIUtils`, its
nearest sibling in `domains/genome/`.

`predict(genome_paths, db_path, outdir, *, threads, extension,
remove_intermediates=True)` copies every genome into a per-run scratch work
dir (copy, not symlink — a symlink's target lives outside the bind-mounted
view and would be unreadable in the container), runs one batched
`checkm2 predict --input <all genomes>` invocation, copies the container's
full (flat) output tree into the caller-supplied `outdir`, and returns
`quality_report.tsv` parsed by **header name** into a dict keyed by the
report's `Name` column. A missing or unexpected column raises `ValueError`
rather than falling back to positional parsing.

**Judgment call — `db_path` is a file, not a directory.** The task spec
gives the docker invocation shape (`-v <db_dir>:/db:ro`, `--database_path
<db arg>`) but leaves the exact meaning of "db arg" open. CheckM2's real
database artifact is a single diamond `.dmnd` file (produced by `checkm2
database --download`, e.g. `uniref100.KO.1.dmnd`), so I treated `db_path`
as that file's path: its **parent directory** is what gets bind-mounted
read-only to `/db`, and `--database_path /db/<db_path.name>` is passed
inside the container. This is documented in the module docstring and is the
one place where the spec's generic "<db arg>" placeholder required a
concrete decision — a reviewer who intended `db_path` to be a pre-existing
CheckM2 database *directory* (with a fixed-name file inside it) would need
to flag this for adjustment, since no such fixed filename was given in the
task.

## success_criteria_check

- **`CheckM2Utils` defines `check_availability`, `get_version`, `predict`** — PASS. All three are implemented with the documented signatures; `check_availability()` runs `docker image inspect <checkm2.docker_image>` and returns `False` (never raises) when unset, docker is missing, or the probe times out.
- **Docker argv includes `--user <uid>:<gid>`, `--network none`, a `:ro` database mount, `--threads`, `--extension`, and the full `--input` list** — PASS. Verified directly in `TestPredictArgvConstruction.test_argv_has_user_network_none_and_ro_db_mount` by mocking `subprocess.run` and inspecting the captured argv (`checkm2_utils.py:_run_checkm2_predict`).
- **`quality_report.tsv` parsed by header name into a `Name`-keyed dict; raises on a missing column** — PASS. `_parse_quality_report()` parses via `csv.reader` + header lookup (never positional index) and raises `ValueError` on both a missing expected column and an unexpected extra column (`TestParseQualityReport::test_missing_column_raises` / `test_extra_column_raises`). The two verbatim fixture rows (including the low-confidence, non-`None`-`Additional_Notes` row) parse fully and are never dropped (`test_low_confidence_row_parses_fully_and_keeps_its_note`).
- **`tests/test_checkm2_utils.py` exists and includes a real-dependency test that invokes the image and is skipped when docker or the image is absent** — PASS, with a placement note. The test file was placed at `tests/annotators/test_checkm2_utils.py`, not the literal `tests/test_checkm2_utils.py` path named in the task's TESTS section, because the task's own "Anchors" section explicitly instructs: "tests are organised into subdirectories … pick the directory consistent with where the sibling tool-wrapper tests live." `tests/annotators/` is where `test_bakta_utils.py`/`test_kofamscan_utils.py`/`test_prokka_utils.py` live — the pattern `CheckM2Utils` actually shares (Docker-wrapper argv shape, `ToolUnavailableError`), as opposed to `tests/domains/test_genome_skani_hardening.py`, which covers a native-binary wrapper with neither. `TestRealDockerGuard::test_get_version_against_real_image` constructs `CheckM2Utils()` with default config discovery (not `config_file=False`) so a deployment host's real `checkm2.docker_image` config value is picked up, calls `check_availability()`, and calls `pytest.skip(...)` — not error, not silent pass — when it is `False`. Ran on this machine and **confirmed the skip actually fires** (see tests_run below): `SKIPPED [1] tests/annotators/test_checkm2_utils.py:438: checkm2 docker image not available locally …`.
- **Tests added pass; nothing that passed on base fails** — PASS. See tests_run.
- **`ToolUnavailableError` imported from `annotator_utils`, no new exception class defined** — PASS. `checkm2_utils.py` imports `from .annotation.annotator_utils import ToolUnavailableError` and raises it from `_require_available()`; no CheckM2-specific exception class exists anywhere in the module.
- **No image digest/ID/tag hardcoded anywhere in the module or its tests** — PASS. `_docker_image` is read exclusively via `self.get_config_value("checkm2.docker_image", default="")`; every test that needs a non-empty image sets `cu._docker_image = "kbutillib/checkm2:1.1.0"` on the instance as a test fixture value (never referenced as a real registry image, never baked into source), and `TestImageIsConfigurationNotConstant` explicitly asserts the default is `""` and that the value flows through the `checkm2` config key.

## tests_run

- `~/VirtualEnvironments/checkm2-wrapper-env/bin/python -m pytest tests/annotators/test_checkm2_utils.py -v` → **21 passed, 1 skipped** (the real-dependency guard; confirmed via `-rs` that the skip reason is "checkm2 docker image not available locally …", i.e. the skip actually fired rather than silently passing or erroring).
- `~/VirtualEnvironments/checkm2-wrapper-env/bin/python -m pytest tests/ -q --continue-on-collection-errors` (exact baseline command, own venv) → **2894 passed, 15 failed, 399 skipped, 6 errors** in ~281s.
  - Baseline (base commit, measured by the dispatcher): 2873 passed, 15 failed, 398 skipped, 6 errors.
  - Delta: **+21 passed** (this task's new tests), **+1 skipped** (this task's real-dependency guard skip), **0 change** in failed/errors.
  - Verified the failed+errored node-id set is byte-identical to the 21 pre-existing ids listed in the dispatch envelope (`tests/external/test_kbdl_service_utils.py` ×14, `tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`, `tests/biochem/test_escher_utils.py` + `tests/notebook/helpers` collection errors, `tests/modeling/test_comprehensive_gapfill_wrapper.py` ×4) — none of this task's changes touched any of those files, and no new failure/error appeared. **No regression.**

## caveats

- `db_path` is treated as the CheckM2 diamond database *file* path (parent dir bind-mounted read-only, file basename referenced inside the container) rather than a pre-populated directory with a fixed internal filename — see the judgment-call note in `summary` above. If the deployment host's actual CheckM2 database layout differs (e.g. a directory CheckM2 auto-discovers a file within), this is the one place to reconcile against real usage before first production run.
- `ruff`/`mypy` were not run: they live in this repo's `lint`/`mypy` dependency-groups, not the `dev` group the dispatch envelope's `pip install -e '.[dev]'` installs, so neither tool is present in the dedicated venv built for this task. No lint/type-check requirement was listed in the success criteria.
- Did not touch `tests/external/test_kbdl_service_utils.py` (the stale-port-assertion failures) or `config.yaml` (no `checkm2:` section was added there, consistent with `bakta`/`prokka`/`kofamscan` also having no entry in the shipped `config.yaml` despite each having a `<tool>.docker_image` config key — the key is read on demand via `get_config_value` and is expected to be set in a deployment host's own `~/.kbutillib/config.yaml`, not in this repo's example config).
- Built a dedicated venv at `~/VirtualEnvironments/checkm2-wrapper-env` per the dispatch envelope's instructions, editable-installed against this worktree only (never touched `~/VirtualEnvironments/checkm2-baseline-kbutillib`).
