# Work Record: t6-maestro-batches

## task_id
t6-maestro-batches (PRD kbu-gene-literature-research-v1)

## branch
maestro/developer/base-kbutillib-main-06214d3-task-t6-maestro-batches

## base
KBUtilLib `main` @ `06214d3` ("docs(work-record): t5-gene-facts")

## summary

Layer 3 of the gene-literature stack:
`src/kbutillib/domains/literature/maestro_batch.py`. Layers 1 and 2 search
and call a model *here*; layer 3 does neither. It hands a batch of genes to a
Maestro **researcher** task on another machine, which reads with its own
tools and network, writes a JSON result file into the repository and commits
it on its own task branch. This module is the two ends of that handoff.

- `submit_gene_facts_batches(genes, organism, papers_by_gene=None, *, repo,
  worker_env, machine='h100', backend='codex', batch_size=25,
  out_dir='agent-io/gene-facts', timeout=3600, maestro_exe='maestro')
  -> list[BatchHandle]`
- `collect_gene_facts_batches(handles, *, repo_path, organism=None,
  papers_by_gene=None) -> {gene_id: GeneFacts}`

Both exist as methods of `MaestroBatchUtils(SharedEnvUtils)` and as
module-level functions of the signatures above (the module-level form builds
a default instance and forwards `**utils_kwargs` to its constructor, which is
the seam a caller or a test uses instead of monkeypatching the module).
`MaestroBatchUtilsImpl` is the composition wrapper, same shape as
`GeneFactsUtilsImpl` and `KBUniProtUtilsImpl`.

Four properties the implementation is built around:

- **The prompt is self-contained, and oversize is refused before anything is
  submitted.** The worker can read neither the PRD nor this module, so
  `build_batch_prompt` carries everything: the environment it must verify
  first, the batch genes and their layer-1 slices as JSON, the output JSON
  schema (`FACTS_JSON_SCHEMA`, verbatim) and `FACT_TYPES`, the evidence-class
  rules, the citation discipline, which KBUtilLib tools exist on the worker
  *and which do not*, the exact document shape, and the write-and-commit
  instruction. Every batch of a call is assembled and size-checked **before
  the first `maestro submit` runs**, so a call that would fail on its fourth
  batch does not leave three paid tasks live. Over 100 KB raises `ValueError`
  naming the limit and saying "Nothing was submitted".
- **`maestro status` is never consulted.** Collection asks git:
  `git -C <repo_path> show <branch>:<output_path>`. A task that reported
  success without committing has produced nothing, and looks identical to one
  still running — hence one status, `pending_or_failed`, for both.
- **Nothing the worker says is trusted because the worker said it.** The
  document is structurally validated against `FACTS_JSON_SCHEMA`; a foreign
  gene id is rejected; a gene id written twice fails *that gene*; each absent
  assigned gene is reported on its own; and every citation goes through
  layer 2's `verify_facts` in **web mode with an EMPTY trusted set**, so each
  cited id has to resolve against Europe PMC. We never handed the worker
  paper text, so there is no closed set of ids it could legitimately have
  copied from — resolution is the only check that means anything here.
- **The cache row is layer 2's row, under layer 2's key function.**
  `_cache_key`, `_storable`, `_read_papers` and `GENE_FACTS_TABLE` are
  **imported** from `facts.py` rather than re-implemented: "cached under the
  same key scheme" is the requirement, and two copies of a hash recipe drift
  the first time either is touched. The Maestro key is that function over the
  gene + organism + layer-1 evidence, with `api_type='maestro'`,
  `model=<backend>`, `mode='web'`, `endpoint={'machine':…, 'repo':…}` and a
  `context` string that is the manifest-plus-output provenance (manifest
  version, prompt sha256, worker_env, batch/task/branch/output path, output
  sha256). A re-run with a changed prompt, a changed worker environment or a
  different committed document is a different key.

## files_touched

- `src/kbutillib/domains/literature/maestro_batch.py` — **new, 1792 lines.**
  Pydantic models `BatchManifest`, `BatchHandle`; `MaestroBatchUtils`
  (`submit_gene_facts_batches`, `collect_gene_facts_batches`, `_submit_one`,
  `_submit_failure`, `_collect_one`, `_read_output`, `_gene_result`, lazy
  `europepmc` / `cache` properties); `build_batch_prompt`; module functions
  `submit_gene_facts_batches`, `collect_gene_facts_batches`; helpers
  `_normalise_genes`, `_validated_worker_env`, `_slice_for`,
  `_ids_from_canonical`, `_schema_problems`, `_json_with_duplicate_keys`,
  `_error_facts`, `_gene_block`, `_output_provenance`, `_submit_payload`,
  `_subprocess_runner`; `MaestroBatchUtilsImpl`. Constants
  `MAESTRO_PROMPT_VERSION='1'`, `MANIFEST_VERSION='1'`,
  `BATCH_SCHEMA_VERSION='1'`, `MAESTRO_API_TYPE='maestro'`,
  `VERIFICATION_MODE='web'`, `DEFAULT_MACHINE='h100'`,
  `DEFAULT_BACKEND='codex'`, `DEFAULT_BATCH_SIZE=25`,
  `DEFAULT_OUT_DIR='agent-io/gene-facts'`, `DEFAULT_TIMEOUT=3600`,
  `DEFAULT_MAESTRO_EXE='maestro'`, `SUBMIT_CALL_TIMEOUT=300`,
  `WORKER_ENV_KEYS` (5), `FORBIDDEN_MAESTRO_FLAGS` (3),
  `MAX_PROMPT_BYTES=102400`, `COLLECT_ERROR_KINDS` (6).
- `tests/literature/test_maestro_batch.py` — **new, 1442 lines, 76 tests**,
  all offline.
- `tests/literature/conftest.py` — **new, 87 lines.** The PATH guard (see
  "no real maestro", below). Autouse for every test in `tests/literature`.
- `src/kbutillib/domains/literature/__init__.py:33-130` — 9 names added to
  `__all__` and to `_MODULE_MAP` (→ `"maestro_batch"`): `MAESTRO_API_TYPE`,
  `WORKER_ENV_KEYS`, `BatchHandle`, `BatchManifest`, `MaestroBatchUtils`,
  `MaestroBatchUtilsImpl`, `build_batch_prompt`,
  `collect_gene_facts_batches`, `submit_gene_facts_batches`. Nothing else in
  that file changed.

No existing function signature, model field or constant was changed.
`toolkit.py` and the top-level `kbutillib/__init__.py` were deliberately
**not** touched: this layer is an orchestration entry point, the task did not
ask for toolkit exposure, and `GeneFactsUtilsImpl` is already the toolkit's
literature-facts surface.

## NO REAL MAESTRO WAS INVOKED, and here is how that was guaranteed

This host has a real, working submitter at **`/home/chenry/.local/bin/maestro`**
which queues real paid tasks onto this very fleet, and the module's
`maestro_exe` default is the bare name `"maestro"` — resolved on PATH by
`subprocess`, exactly as a shell would. Three independent mechanisms, two of
them tested:

1. **Every test passes `maestro_exe` explicitly**, pointing at a script
   `tests/literature/test_maestro_batch.py::fake_maestro` writes under
   `tmp_path`. It is named `fake-maestro`, never `maestro`, so no test can be
   satisfied by a PATH lookup by accident.
2. **A refusing fake named `maestro` is first on PATH for the whole package.**
   `tests/literature/conftest.py` writes a session-scoped script that records
   its argv to a marker file, prints "REFUSED: this is the tests/literature
   PATH guard, not the real maestro" to stderr and exits **97**; the autouse
   `guard_maestro_on_path` fixture prepends its directory to `PATH` for every
   test in `tests/literature`. `TestPathGuard` asserts BOTH halves:
   `shutil.which("maestro")` **is** that script and is **not**
   `/home/chenry/.local/bin/maestro`; and a `submit_gene_facts_batches` call
   with **no** `maestro_exe` raises `RuntimeError` carrying `exited 97` and
   `PATH guard`, with the guard's marker file one line longer than before. So
   the one test that exercises the default argument proves the default cannot
   reach the real binary rather than proving it can.
3. **The code refuses the dangerous flags itself.**
   `submit_gene_facts_batches` scans the argv it just built for
   `FORBIDDEN_MAESTRO_FLAGS` and raises before running anything, so a later
   edit that adds `--auto-merge` fails locally instead of merging unreviewed
   model output.

**Verified against Maestro's own database, read-only**
(`sqlite3 file:/home/chenry/.maestro/master.sqlite?mode=ro`), after the full
test run:

```
SELECT COUNT(*) FROM tasks WHERE prompt LIKE '%Gene literature research batch%';
-- 0
```

Zero. Every prompt this module can build begins with that exact heading, so a
single real submission from any test or any hand-run would be a row there.
The only task submitted on this machine during this session's window
(`submitted_at >= 19:43Z`) is **this task itself**, `t6-maestro-batches`,
role `developer` — no `researcher` task was submitted at all.

Correction, because it would otherwise read as evidence: an earlier check of
mine, `find ~/.maestro -newermt '-120 minutes'`, printed nothing — but it
printed nothing because `find` on this host is `bfs`, which **rejects**
relative timestamps and errored to stderr. That probe proved nothing and is
not the basis of the claim above; the sqlite query is. (The `.maestro`
databases *are* being written continuously, by the daemon running this very
task, so their mtimes could never have been evidence either way.)

## success_criteria_check

| criterion | where |
|---|---|
| one Maestro researcher task per batch of `batch_size` genes | `TestSubmission::test_one_submit_per_batch_and_batch_size_is_honoured` (7 genes / 3 → 3 real subprocesses, `[g1 g2 g3][g4 g5 g6][g7]`), `test_the_default_batch_size_is_25` |
| the documented argv, with `--role researcher`, `--success-criterion output-signal`, `--expected-output`, `--timeout` | `test_the_argv_is_exactly_the_documented_submit_call` asserts `argv[:-1]` **element by element** |
| **no** auto-review / auto-merge flags | `test_no_auto_review_or_auto_merge_flag_is_ever_passed` (membership *and* no `--auto` substring in any argv), plus the in-code `FORBIDDEN_MAESTRO_FLAGS` refusal |
| a self-contained prompt (schema, genes JSON, fact types, evidence rules, tools, citation rules) | `TestPrompt`, 11 tests, one per contract; `test_the_argv_is_exactly_…` pins that the submitted prompt **is** `build_batch_prompt`'s output, so those 11 are about what was sent |
| JSON-serialisable `BatchHandle`s | `test_a_handle_round_trips_through_json` (via `model_dump_json` *and* via a plain mapping), `test_the_batch_id_is_twelve_hex_characters_and_names_the_output` |
| `collect` reads outputs with `git show` from the task branch | `test_the_git_argv_reads_the_task_branch_and_nothing_else` (exact argv), `test_maestro_status_is_never_consulted`, and `TestNoInjection` with real git |
| citations verified with the layer-2 verifier | `test_an_unresolvable_citation_rejects_the_fact` (one fact kept, one rejected `unresolved_citations`, **every** cited id resolved), `test_a_fact_type_outside_FACT_TYPES_is_rejected_on_its_own` (`bad_type`) |
| `GeneFacts` cached with `api_type 'maestro'` | `test_the_result_is_cached_under_a_maestro_key`, `test_an_error_result_is_never_cached`, `test_the_cache_key_separates_genes_machines_and_outputs`, and the on-disk sqlite assertion in `TestNoInjection` |
| missing outputs marked `pending_or_failed` | `test_one_valid_and_one_missing_output`, `test_an_unparseable_or_shapeless_document_is_pending_or_failed` (6 cases), `TestNoInjection::test_a_task_that_committed_nothing_reads_as_pending_or_failed` |
| offline tests with a fake maestro executable and fake git | the whole module: 76 tests, no network, no real submitter, no model |
| C8 `worker_env` required and complete | `TestWorkerEnv`, 9 tests (one per key, plus empty-value, non-mapping, required-keyword, and manifest contents) |
| C9 one entry per gene; foreign/duplicate rejected; absent reported individually | `test_an_absent_assigned_gene_is_reported_on_its_own`, `test_a_foreign_gene_id_is_rejected`, `test_the_same_gene_twice_in_one_document_is_refused`, `test_a_worker_reported_error_is_carried_through`, `test_an_entry_that_does_not_match_the_schema_is_invalid` (5 cases) |
| C10 manifest carries everything; collect needs only handles + repo_path | `test_the_manifest_carries_everything_collection_needs`; every collect test passes **only** handles and `repo_path` |
| 100 KB ceiling, before submission | `test_an_oversized_prompt_fails_before_ANY_batch_is_submitted` — the *second* batch is oversize and the fake records **zero** calls |
| NO-INJECTION CHECK | `TestNoInjection`, below |

## NO-INJECTION CHECK: it ran, and it passed

`tests/literature/test_maestro_batch.py::TestNoInjection` (2 tests).
`MaestroBatchUtils(token_file=None, kbase_token_file=None)` and nothing else —
no `runner`, no `cache`, no `europepmc`, no `config` dict
(`token_file`/`kbase_token_file` are not seams of this class; they are
silenced only to keep the real HOME's token files out of the test).

- **Real subprocess to a fake maestro SCRIPT**, not a Python-level runner
  stub: `fake_maestro` writes an executable file under `tmp_path` with
  `sys.executable` as its shebang, and the default `_subprocess_runner`
  (`subprocess.run`) executes it. The test reads the argv back out of the
  file the script wrote, so what is asserted is what a real process really
  received.
- **A real temporary git repository.** `git init --initial-branch=main`, a
  real initial commit, then the worker's half done exactly as the prompt
  instructs it: `git checkout -b <branch>` (the branch name the fake
  submitter returned), write `agent-io/gene-facts/<batch_id>.json`,
  `git add`, `git commit`, `git checkout main`. The test then **asserts the
  file is absent from the working tree**, so collection cannot accidentally
  be reading a file off disk — it has to go through `git show
  <branch>:<path>`, which it does, with the real `git` on PATH.
- **Real verification through a real `EuropePMCClient`.**
  `isinstance(collector.europepmc, EuropePMCClient)`; only
  `requests.adapters.HTTPAdapter.send` is intercepted (a socket, not a seam),
  replaying `tests/literature/fixtures/get_batch.json` (reused from t1). The
  committed document cites `15210978` (in the fixture) **and**
  `PMID:99999999` (not in it): the first survives, normalised to its
  canonical `PMID:15210978`, and the second takes its fact down with it as
  `unresolved_citations`. The test asserts a request was actually sent, so a
  verification that silently did nothing fails.
- **A real cache file on disk.** HOME is relocated to `tmp_path`, a
  `~/.kbutillib/config.yaml` there names the cache path (and
  `europepmc_rps: 50`, so the real throttle's real sleep is negligible), and
  `shared_env_utils.DEFAULT_CONFIG_FILE` is pointed at it (that location is
  bound at import time). Asserted: `utils.cache.path == cache_path`, and
  `sqlite3` opened **directly** on the file shows exactly **one** row in
  table `gene_facts`, whose key **is** `answer.cache_key`, whose stored
  `api_type` is `"maestro"`, whose `mode` is `"web"`, which carries **no**
  `from_cache` field, and which re-validates as a `GeneFacts` whose first
  fact cites `PMID:15210978`.
- **The second test is the unhappy path through the same real wiring**: the
  task branch does not exist at all, and the result is `status='error'` with
  `error` starting `pending_or_failed:` and naming the task id.

## tests_run

Commands, verbatim, all **foreground**. `pytest` is not installed in this
task's `.task-venv`, so the suite ran on the machine's `kbdl` venv
**read-only** (`PYTHONPATH=$PWD/src`, nothing installed into it) — the same
interpreter and invocation shape as
`build-baselines/kbutillib-h100-main-6310d61.json` records.

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/literature/test_maestro_batch.py -q -p no:cacheprovider --no-header
# 76 passed in 5.60s                                             exit 0
```

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest tests/literature \
  -q -p no:cacheprovider --no-header
# 349 passed, 2 skipped in 15.87s                                exit 0
```

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest tests/literature tests/core \
  -q -p no:cacheprovider --no-header
# 3 failed, 711 passed, 6 skipped, 1 xfailed, 8 errors in 31.86s  exit 1
# the 3 failures and 8 errors are pre-existing -- see below
```

The full `tests/` tree was **not** run: the h100 baseline measures it at
3416 s, 5.7x the 600 s sub-agent Bash ceiling, and records that only a
conductor may run it, backgrounded. The selection above is the union of what
this change can reach — the literature package (the only package touched)
plus `tests/core`, which is where the package-level import and composition
smoke tests live. No test outside `tests/literature` references
`domains.literature` (checked by grep; the hits in `tests/cli` and
`tests/notebook` are the word "literature" as a project directory name and a
sub-agent name, unrelated).

## regression framing

`build-baselines/kbutillib.json` (2026-09-13 @ `0f84803`) and
`build-baselines/kbutillib-h100-main-6310d61.json` (2026-10-19 @ `6310d61`)
both exist, so **no new baseline file was written and the full suite was not
re-run to establish one**. Neither is name-comparable against this targeted
selection — both were captured with different commands in different
environments, and the h100 one says so itself.

A **targeted baseline was therefore measured directly**, which is the only
honest comparison available: a scratch worktree at the base commit
(`git worktree add --detach /scratch/chenry/t6-base 06214d3`), the identical
selection, the identical interpreter, removed afterwards with
`git worktree remove`.

```
base   @ 06214d3 : 3 failed, 635 passed, 6 skipped, 1 xfailed, 8 errors
branch           : 3 failed, 711 passed, 6 skipped, 1 xfailed, 8 errors
```

Compared **by nodeid**, the 11 non-passing names are **identical**: new on
branch `(none)`, fixed-vs-base `(none)`. `635 → 711` is exactly `+76`, the 76
tests this task adds — every one of which passes.

The 11 pre-existing non-passing nodeids, all of them also non-passing at
`06214d3` in the same location with the same interpreter:

- `tests/core/test_cli_cap.py::TestCapList::{test_json_output_is_valid,
  test_json_output_has_required_keys, test_json_filter_by_tag}` —
  capability-registry CLI, missing optional scientific deps.
- 8 setup **errors** in `tests/core/test_composition_smoke.py`
  (`TestMSBiochemUtils`, `TestMSFBAUtils`, `TestKBModelUtils`,
  `TestEscherUtils`) — all `Failed to load ModelSEED database: No such file
  or directory: '/scratch/chenry/ModelSEEDDatabase/Biochemistry/'`, the
  worktree-relative data-path trap the h100 baseline documents under
  `WORKTREE_MEASUREMENT_TRAP`.

Note that `tests/literature` is **349 passed, 2 skipped** on the branch and
had no failures at base either: the new `tests/literature/conftest.py`
(autouse PATH prepend) does not disturb the 273 pre-existing literature
tests.

## lint

```
.task-venv/bin/ruff check --no-cache \
  src/kbutillib/domains/literature/maestro_batch.py \
  src/kbutillib/domains/literature/__init__.py \
  tests/literature/test_maestro_batch.py tests/literature/conftest.py
# All checks passed!                                             exit 0
```

`ruff` 0.17.1 was installed into **this task's own** `.task-venv` (not into
any service venv) because no `ruff` exists on this machine. Two findings it
caught were fixed with `--fix` on `maestro_batch.py` only: one `I001`
(`typing` import wrapped, `verify_facts` re-sorted in the `.facts` block) and
one `F401` (unused `Evidence` import).

## divergences reconciled (prompt vs. tree)

The prompt's re-checked claims about `facts.py` were, this time, **all
correct**: `FACT_TYPES` at line 127, `GeneFacts` at 330, `verify_facts` at
392, `_cache_key` at 1468, `CapabilityError` at 220, and `EVIDENCE_CLASSES`
at `annotate.py:112` re-exported by `facts.py:89`. Verified by reading, and
nothing was located by line number anyway. Six other things did diverge:

1. **"New code goes in a NEW package `src/kbutillib/domains/literature/`
   (create `__init__.py`)"** — the package **already exists** with
   `annotate.py`, `europepmc.py`, `paperblast.py`, `cache.py`, `facts.py` and
   a lazy-loader `__init__.py` from t1-t5. Adapted: `maestro_batch.py` added
   beside them, 9 names added to the existing `__all__`/`_MODULE_MAP`.
2. **"Tests go under `tests/literature/` (create it)"** — already exists with
   five test modules. Adapted: `test_maestro_batch.py` added beside them,
   reusing `test_europepmc.load_fixture` and `fixtures/get_batch.json`. A new
   `conftest.py` **was** created there, for the PATH guard.
3. **"if `cache.py` does not exist, create `LiteratureCache`"** — it exists,
   with exactly `get(table,key)` / `put(table,key,value_json)`, the
   `gene_literature.cache_path` config key and `resolve_cache_path`. Nothing
   was created; rows go in table `gene_facts`, the same table layer 2 uses,
   under layer 2's own key function.
4. **The signature in the TASK line has no `worker_env`; addendum C8 makes it
   required.** C8 is the later binding, so `worker_env` is a **required
   keyword-only** parameter placed after `repo`, validated by
   `_validated_worker_env` (not a mapping, or any of the five keys missing or
   blank → `ValueError`, nothing submitted). It is in the manifest and in the
   prompt's STEP 0.
5. **`collect_gene_facts_batches`'s `organism` is required in the TASK line
   and unnecessary under C10** ("collect needs only the handles +
   repo_path"). Reconciled by keeping `organism` and `papers_by_gene` as
   **optional** keyword parameters that are accepted and ignored — the
   handle's `input_manifest` is the authority, because it records what was
   actually submitted and cannot be contradicted by a caller passing
   something different a week later. Passing either logs a debug line saying
   so. Every collect test passes neither.
6. **"a missing or unparseable output -> status 'error', `error.kind`
   'pending_or_failed'"** — `GeneFacts.error` is `Optional[str]`; there is no
   error object with a `.kind` field anywhere in layer 2. Rendered as the
   string form layer 2's `_finalise` already produces for a failed model
   call: `error = "<kind>: <message>"`, so `error.startswith(
   "pending_or_failed: ")` is the test and a caller that already parses that
   prefix needs no second rule.
7. **"the instruction to use ... `EuropePMCClient` incl. full text via
   `.../fullTextXML` and supplementary files" vs. addendum C7** ("KBUtilLib
   v1 has NO full-text or supplement methods"). C7 wins and the two are
   reconciled in the prompt text: it states plainly that **no** full-text or
   supplementary-file method exists in the package and names the methods the
   worker must not assume (`full_text`, `fulltext`, `get_full_text`,
   `supplementary_files`), then gives the `fullTextXML` and
   `supplementaryFiles` URLs as things it **may fetch ad hoc with its own
   network tools**, and requires unavailable material to be recorded in the
   gene's `notes`. Verified against the tree: `europepmc.py` exposes
   `search`, `get` and `resolve`, and nothing else.

Four things the prompt did not list, each flagged here rather than buried:

- **`BatchManifest` and `build_batch_prompt` are public.** C10 requires the
  handle to carry an `input_manifest`; making it a model rather than a bare
  dict is what makes the handle round-trip through JSON with its paper slices
  intact. `build_batch_prompt` is exported so the prompt's contents can be
  tested without submitting anything.
- **Five error kinds beyond `pending_or_failed`**: `batch_mismatch`,
  `missing_from_output`, `duplicate_in_output`, `invalid_result`,
  `worker_error`. C9 requires foreign/duplicate ids to be rejected and each
  absent assigned gene to be reported *individually* — which are different
  conditions from "the output is missing or unparseable", and collapsing them
  into `pending_or_failed` would tell a caller to resubmit a batch when eight
  named genes is what needs resubmitting.
- **The document validator deliberately does not enforce `enum`.**
  `_schema_problems` checks `type`/`required`/`properties`/`items` — the
  whole of `FACTS_JSON_SCHEMA`'s structure — but leaves the fact-type and
  evidence-class enumerations to `verify_facts`, which rejects the one
  offending fact and keeps the rest. Enforcing them at document level would
  make one bad `type` discard every good fact beside it. No new dependency:
  `jsonschema` is importable in this venv but is **not** a declared
  dependency of this repository, so it was not used.
- **`SUBMIT_CALL_TIMEOUT = 300`** bounds the `maestro submit` call itself
  (and the `git show`), which is distinct from the task's own `--timeout`
  that is passed through.

One non-divergence worth recording, because the prompt's wording invites the
wrong implementation: **the structural template was followed.**
`domains/external/kb_uniprot_utils.py` does carry a `KBUniProtUtilsImpl`
composition wrapper delegating via `__getattr__` (line 677), exactly as the
prompt says — though the class it wraps extends `BaseUtils`, while the prompt
separately (and this package uniformly) requires `SharedEnvUtils`.
`MaestroBatchUtils` extends `SharedEnvUtils` and reads config through
`get_config_value('gene_literature.cache_path')`; `MaestroBatchUtilsImpl`
mirrors `GeneFactsUtilsImpl`, the in-package instance of the same pattern.

## inferred rather than verified

- **The real `maestro submit` CLI contract is taken from the task prompt, not
  from Maestro.** The argv (`submit --role researcher --backend … --machine …
  --repo … --success-criterion output-signal --expected-output … --timeout …
  <prompt>`) and the stdout keys (`task_id`, `branch_name`) are what the
  prompt specifies and what the fake executable implements. They were **not**
  checked against the installed `maestro` — not by running it (forbidden, and
  it would submit), and not by reading its source either, which lives outside
  this worktree. If the real CLI spells a flag differently, every test here
  still passes and the first real submission fails at the argv. The parser is
  written defensively around that: `branch_name` falls back to `branch`,
  stdout is read whole and then line-by-line from the end, and a payload with
  no `task_id` **or** no branch raises rather than returning a handle that
  could never be collected.
- **No real Maestro task has ever consumed one of these prompts**, so whether
  a `codex`-backed researcher actually honours the output contract — one
  entry per gene, ids not URLs, `homolog` not `direct`, committed before exit
  — is untested. The collection path is written on the assumption that it
  sometimes will not: that is what `missing_from_output`,
  `duplicate_in_output`, `invalid_result` and the empty trusted set are for.
- **`git show <branch>:<path>` is exercised against a repository this test
  built**, not against a Maestro task branch in a real repo. The two differ
  in how the branch got there, which is the part collection does not touch.
- **Python 3.9**: CI runs 3.9 and every model field uses `Optional[...]`, not
  PEP 604. Verified by reading, not by running — no 3.9 interpreter exists on
  this machine (the suite ran on 3.11.15).
- **`genome_annotation_aggregator` was never imported** (prompt constraint);
  the module's imports are `collections.abc`, `hashlib`, `json`,
  `subprocess`, `uuid`, `datetime`, `typing`, `pydantic`, and four in-package
  modules.
