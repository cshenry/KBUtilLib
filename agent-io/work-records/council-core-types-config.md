# Work Record — council-core-types-config

## task_id
council-core-types-config

## branch
maestro/developer/background-reference-only-agent--council-core-types-config

## commit_shas
- 8b30684 — feat(council): core types, protocols, config and test backends
- (second commit, this record + the asyncio-neutral test helper — sha below)

## summary
Created the foundation of the domain-neutral LLM council at
`src/kbutillib/domains/ai/council/`: the data vocabulary and hash helpers
(`types.py`), the three structural interfaces (`protocols.py`), the
configuration loader with the shipped default (`config.py` +
`default_config.yaml`), the lazy name loader (`__init__.py`), and the two
non-spending test backends (`backends/testing.py`). 94 new tests in
`tests/domains/ai/council/test_types_config.py`. Nothing in the change makes
a network call, spends a token, needs a CLI login or needs a GPU.

`src/kbutillib/domains/ai/argo_utils.py` and
`src/kbutillib/domains/ai/ai_curation_utils.py` are byte-identical to the
base commit (`git diff --stat HEAD -- <both>` is empty).

## files_touched
- `src/kbutillib/domains/ai/council/__init__.py` (new — lazy `_MODULE_MAP`
  covering every name in the task prompt, including those whose modules
  arrive in later milestones, so no later task edits this file)
- `src/kbutillib/domains/ai/council/types.py` (new — records, failure
  taxonomy, canonical-JSON hashing)
- `src/kbutillib/domains/ai/council/protocols.py` (new — `Backend`,
  `QueryRenderer`, `CouncilStore`)
- `src/kbutillib/domains/ai/council/config.py` (new — loader, validation,
  `DEFAULT_CANONICAL`, roster resolution, legacy import)
- `src/kbutillib/domains/ai/council/default_config.yaml` (new — package data)
- `src/kbutillib/domains/ai/council/backends/__init__.py` (new)
- `src/kbutillib/domains/ai/council/backends/testing.py` (new — `FakeBackend`,
  `ReplayBackend`)
- `pyproject.toml` (new optional extra `council`; `domains/ai/council/*.yaml`
  added to `[tool.setuptools.package-data]`)
- `tests/domains/ai/__init__.py`, `tests/domains/ai/council/__init__.py` (new)
- `tests/domains/ai/council/test_types_config.py` (new — 94 tests)
- `tests/domains/ai/council/fixtures/filipe_model_inference.yaml` (new —
  verbatim copy of Filipe Liu's real file, api_key values redacted)

## verification

| Command | Exit | Result |
|---|---|---|
| `python -m pytest tests/domains/ai/council/ -q` | 0 | 94 passed |
| `python -m ruff check --output-format=concise src/kbutillib/domains/ai/council tests/domains/ai` | 0 | All checks passed |
| `python -m pytest -q -p no:cacheprovider <baseline ignore list>` (branch) | 1 | 26 failed, 4249 passed, 429 skipped |
| same command at base `520338f` in a detached `/tmp` worktree | 1 | 26 failed, 4155 passed, 429 skipped |

**Regression comparison was done by NAME against a base run, not against the
cached baseline.** `build-baselines/kbutillib.json` (captured 2026-09-13 at
`0f84803`) could not adjudicate this branch: it was captured in a different
minimal venv, and in this task venv 237 of its "new" names were simply
`No module named pandas` / `tomli_w` / `httpx` / `duckdb`. I installed those
four declared dependencies into this task's own `.task-venv` (all are in the
repo's own `dependencies` / `[dev]` / `[ai]` lists) and then ran the full
suite on both sides. **Result: the branch and base failure sets are the same
26 nodeids; the diff in both directions is empty.** The +94 passes are
exactly this task's new tests. I did NOT rewrite the cached baseline, since
one already exists — but it is stale relative to a properly provisioned env
and a future task should expect to re-provision rather than trust its counts.

One real regression was found and fixed before the second commit: my test
helper used `asyncio.run()`, which leaves the thread with no current event
loop, and `tests/interfaces/test_mcp_server.py::TestUnavailableHandler` (3
tests) still calls the deprecated `asyncio.get_event_loop()`. Because
`tests/domains` collects first, my tests broke theirs from a distance. Fixed
in my helper (own a local loop, leave process-global asyncio state as found)
rather than by touching their test.

## success_criteria_check

**Criterion 1 — package exists with the listed files; the test module covers
the listed cases and passes; the packaged default yields Rosters for `argo`,
`mixed-subscription` and `poplar-local`; a critic sharing a `canonical_model`
with a proposer on a DIFFERENT endpoint raises; `argo_utils.py` and
`ai_curation_utils.py` byte-identical; no test that passed on base fails on
the branch.** — PASS.
- Every listed file exists; `__init__.py`'s `_MODULE_MAP` lists all 24
  prompt-named targets plus the extra public names of the modules written
  here (`load_legacy_model_inference`, the error classes, the hash helpers),
  so later milestones need not edit it.
- `TestPackagedDefaultConfig::test_all_three_profiles_build_rosters` builds
  all three profiles.
- `TestRosterInvariants::test_critic_sharing_canonical_with_proposer_refused_across_endpoints`
  seats the critic as `openai/gpt-5.4` on a `codex_cli` endpoint while a
  proposer holds it on `argo`, and asserts `RosterError`.
- Byte-identity and the base-vs-branch name diff are shown above.

**Criterion 2 (no-injection check) — a test calls `load_council_config()`
with NO argument and NOTHING monkeypatched, under a temporary HOME with no
KBUtilLib config, and asserts all three profiles build Rosters.** — PASS.
`TestPackagedDefaultConfig::test_on_a_fresh_machine_with_no_kbutillib_config`
sets `HOME`/`USERPROFILE` to an empty `tmp_path`, deletes `XDG_CONFIG_HOME`,
and `chdir`s into a subdirectory of that HOME — the last part matters,
because `SharedEnvUtils._find_config_file` walks up from the CWD looking for
a `config.yaml` and would otherwise find the repo's own. It asserts no
`config.yaml` exists anywhere under that HOME, then loads with no argument
and no stub of any kind (no `SharedEnvUtils` patch, no packaged-data patch)
and builds all three Rosters. It additionally asserts the default store path
expanded against the temporary HOME, which proves the packaged default — not
a cached or injected config — was the source.

## reconciliation (prompt vs tree)

The prompt is a claim about the code; where they disagreed I followed the
tree and carried on. Each divergence, and what I did:

1. **`SharedEnvUtils` has moved.** The prompt says
   `SharedEnvUtils().get_config_value("ai_council", default={})` without a
   module. It no longer lives at `kbutillib.shared_env_utils` (that path is
   an auto-generated deprecation shim that emits a `DeprecationWarning`); the
   real module is `kbutillib.core.shared_env_utils`. `config.py` imports
   `from ....core.shared_env_utils import SharedEnvUtils`, matching how
   `domains/ai/argo_utils.py:13` reaches it.
2. **The "STORE CONTRACT below" section is missing from the prompt.**
   `protocols.py` was told to declare `CouncilStore` "with exactly those
   method names and signatures", but no such section was included. I did not
   stop: I declared a compact store protocol reconstructed from the queue
   behaviour the prompt *does* describe (jobs, call rows as the queue,
   claim/record, reuse lookup by prompt hash, verdicts, endpoint halts) and
   labelled it **PROVISIONAL** in its docstring, naming the missing section,
   so the store milestone refines it instead of guessing what happened here.
   This is the one place a later task is expected to edit a file I wrote;
   `__init__.py` is not affected.
3. **The prompt does not say how legacy mode is selected.** It specifies the
   legacy rules and also specifies the conflicting strict rule (undeclared
   alias → `ConfigError`), which cannot both apply unconditionally. I made
   `load_council_config(path, legacy=None)` auto-detect on the prompt's own
   marker — "an endpoint without `kind` is openai" means a file with such an
   endpoint IS a legacy file — and added explicit `legacy=True/False` plus
   `load_legacy_model_inference(path)` for call sites that want to be
   unambiguous. Filipe's real file therefore loads with no flag, while a
   native config (every endpoint declaring `kind`) keeps strict validation.
   `TestConfigValidation::test_undeclared_alias_is_a_config_error_even_with_an_argo_endpoint`
   pins the distinction on a config that *has* an ARGO endpoint.
4. **Filipe's `oss` / `oss-critic` / `oss-next` profiles cannot resolve, by
   design.** The prompt asks the test to assert that "argo/oss/oss-next
   profiles resolve", with the stated alternative that the test documents
   which raise `RosterError`. His nine locally-served models are declared in
   `models` with no `canonical` key, and local vLLM deployments are
   deliberately absent from `DEFAULT_CANONICAL` (the prompt's own table lists
   only vendor models). Seating one therefore raises `RosterError`. I took
   the documented-failure branch:
   `test_locally_served_profiles_raise_roster_error_as_documented` asserts
   and explains it, and
   `test_the_same_profile_resolves_once_identities_are_declared` demonstrates
   the fix (add `canonical: poplar/<alias>`, exactly what KBUtilLib's own
   default config does for the same nine models). `argo` resolves in full
   through the legacy backfill.
5. **`tests/domains/ai/` did not exist.** The tests tree uses `__init__.py`
   packages (`tests/domains/__init__.py` is present), so I created
   `tests/domains/ai/__init__.py` and `tests/domains/ai/council/__init__.py`.
   `tests/domains/test_ai_smoke.py` continues to sit beside the new package.
6. **Python 3.9 is still in the CI matrix** (`.github/workflows/ci.yml`), and
   pydantic *evaluates* field annotations, so PEP 604 (`str | None`) in a
   model field is a `TypeError` there. All model fields use `Optional[...]`;
   the reason is stated in both module docstrings so nobody "modernises" it
   back.

## design decisions not dictated by the prompt

- **`Pick` is a pydantic model, not a bare dict** (`Ballot.picks:
  list[Pick]`). The prompt wrote the shape as `{id, confidence, why}`; a
  model validates the confidence enum and still accepts dicts on input
  (`TestRecordShapes::test_ballot_coerces_pick_dicts`). `CouncilVerdict`'s
  `errors` and `usage` stay plain dicts, as written.
- **`route_fingerprint` takes an optional third argument** (`effective_params`,
  defaulting to `seat_generation_params(seat)`), so the prompt's two-argument
  call works unchanged while a worker can pass the params a backend really
  sent.
- **`FakeBackend` takes a keyword-only `usage=`**, defaulting to zeroed token
  counts rather than `None`, so budget accounting in later milestones sees a
  measured-but-free call instead of tripping `BudgetUsageUnknown` in every
  fake test. The positional signature `(script, kind="fake")` is as specified.
- **Secret redaction in `snapshot()` matches exact names and suffixes, not
  substrings.** A substring test on `"token"` also eats `max_total_tokens`,
  which is a budget and belongs in a snapshot
  (`test_snapshot_drops_credentials_but_keeps_env_var_names` pins both
  directions). `*_env` keys are kept: they name a variable, not a secret.
- **The Dropbox refusal also matches `Dropbox (...)` directory names**, since
  macOS business installs are named e.g. `Dropbox (Argonne National
  Laboratory)` and the hazard is identical.
- **CLI endpoints default their `executable`** to `claude` / `codex` by kind,
  and HTTP endpoints without a `base_url` are a `ConfigError`.
- **`council` is deliberately NOT added to the `all` extra**, on the same
  rationale the existing `backfill` extra records: `all` is "every transport
  plus the docs build", and a database driver is neither.

## inferred rather than verified

- **The Codex CLI served names `gpt-5.4` / `gpt-5.5` are unverified**, as the
  prompt states. They carry `verified: false` and the YAML comment pointing
  at `kbu council doctor` (operator step OP-2); a test asserts those two
  seats come back unverified. Nothing in this task could confirm them without
  running the CLI.
- **The nine poplar ports and model names** are transcribed from the prompt.
  No endpoint was contacted — the whole test suite is offline by constraint.
- **`DEFAULT_CANONICAL`'s vendor strings** are the prompt's values. They are
  identity keys, not API model ids, so nothing here validates them against a
  provider.
- **The `council` extra's version floors** (`SQLAlchemy >=2.0`,
  `psycopg[binary] >=3.1`) follow the prompt's "SQLAlchemy Core 2.x + psycopg
  3". Neither package is installed in this task venv and no store code exists
  yet, so the floors are declared, not exercised.
