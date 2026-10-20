# Work Record: t5-gene-facts

## task_id
t5-gene-facts (PRD kbu-gene-literature-research-v1)

## branch
maestro/developer/base-kbutillib-main-ec0dc1c-task-t5-gene-facts

## base
KBUtilLib `main` @ `ec0dc1c` ("docs(work-record): t4-annotate-genes-with-papers")

## summary

Layer 2 of the gene-literature stack: `GeneFactsUtils` in
`src/kbutillib/domains/literature/facts.py`. Layer 1 answers *which papers
talk about this gene*; this answers *what do they say*, as typed statements
each carrying at least one citation that has been checked outside the model.

`gene_facts(gene, organism, papers=None, *, model, api_type, endpoint=None,
web_search='auto', max_papers=20, context_token_budget=24000, force=False)`
returns a `GeneFacts`. `genes_facts(...)` does the same for many genes under
an `asyncio.Semaphore`, one `GeneFacts` per gene, failures isolated.

Four properties the implementation is built around:

- **Mode is decided for free, before anything is spent.** `web_search='off'`
  is context mode; `'auto'` is web mode iff
  `llm.supports_web_search(api_type, endpoint)`; `'required'` is web mode or
  `CapabilityError`, raised before the model is called and before a paper is
  even looked up (asserted: `Script.count() == 0` *and*
  `europepmc.get_calls == []`).
- **No ungrounded call.** In context mode the model is asked only if at least
  one *selected* paper carries a non-empty abstract or a layer-1 snippet.
  With nothing readable the answer is `status='no_evidence'` with zero model
  calls, and it is cached. If the reason there is nothing readable is that
  *retrieval raised*, the status is `'error'` and it is **not** cached — a
  ten-second outage must not answer for that gene for ever.
- **Nothing is trusted because the model said it.** `verify_facts` is a
  plain function: no model, and in context mode no network. Every citation id
  is normalised through `normalise_paper_id` and must land in the trusted set
  — which is built from the papers *actually sent*, plus every alias of each
  (so a model handed a DOI may cite the PMID). In web mode an id outside the
  set is accepted only if `EuropePMCClient.resolve` finds it. A fact left
  with no citations is rejected `unresolved_citations`; a type outside
  `FACT_TYPES` is rejected `bad_type` before its citations are read; an entry
  that is not readable as a fact at all is rejected `malformed` **on its
  own**, so one broken entry costs that entry. A malformed answer is never
  retried.
- **The cache key is over what was SENT.** Not over the paper ids: over a
  SHA-256 of the exact rendered context text, beside the normalised gene
  mapping (sequence as its own hash), the organism dump, `max_papers`, the
  supplied evidence records, model, api_type, `llm.endpoint_key(endpoint)`,
  mode, `PROMPT_VERSION` and `SCHEMA_VERSION`. A key over the ids alone would
  answer a call that had full abstracts with one made when Europe PMC
  returned stubs.

Selection is deterministic: papers are resolved through
`EuropePMCClient.get` **before** the context is built (only a publication
Europe PMC knows about becomes citable), folded onto one canonical id per
paper through `merge_ids`, then ranked `direct > curated > homolog (identity
desc) > mention > no-evidence`, ties in supplied order. `max_papers` then the
token budget (`len(text) // 4`) drop the lowest priority first and record
what they dropped in `papers_used.dropped_for_budget`. The highest-priority
paper is kept even against a budget it alone exceeds — a budget is not a
reason to ask with no context, and truncating mid-abstract would make the
cache key depend on where the cut landed. Curated evidence whose
`homolog_organism` is not this organism is described to the model as
`"curated (homolog-derived: another organism's protein)"`.

## files_touched

- `src/kbutillib/domains/literature/facts.py` — **new, 1564 lines.** Pydantic
  models `Citation`, `Fact`, `RejectedFact`, `PapersUsed`, `GeneFacts` and the
  internal `_Plan`; `CapabilityError`; `verify_facts`; `GeneFactsUtils`
  (`gene_facts`, `genes_facts`, `_resolve_mode`, `_plan`, `_retrieve`,
  `_finalise`, lazy `europepmc` / `cache` properties); module helpers
  `_read_papers`, `_priority`, `_has_text`, `_fit_budget`, `_trusted_map`,
  `_system_prompt`, `_user_prompt`, `_paper_block`, `_evidence_description`,
  `_cache_key`, `_storable`; `GeneFactsUtilsImpl`. Constants
  `PROMPT_VERSION='1'`, `SCHEMA_VERSION='1'`, `FACT_TYPES` (9),
  `REJECTION_REASONS`, `CONFIDENCE_LEVELS`, `DEFAULT_MAX_PAPERS=20`,
  `CHARS_PER_TOKEN=4`, `DEFAULT_CONTEXT_TOKEN_BUDGET=24000`,
  `GENE_FACTS_TABLE='gene_facts'`, `FACTS_JSON_SCHEMA`.
- `tests/literature/test_facts.py` — **new, 1660 lines, 85 tests**, all
  offline.
- `src/kbutillib/domains/literature/__init__.py:33-100` — 14 names added to
  `__all__` and to `_MODULE_MAP` (→ `"facts"`), plus a docstring example.
- `src/kbutillib/toolkit.py:51,131,399-404` — `TYPE_CHECKING` import, the
  `self._gene_facts = None` slot, and the lazy `gene_facts` property
  returning `GeneFactsUtilsImpl`.
- `src/kbutillib/__init__.py:266-278,582-585` — guarded import of
  `Citation`, `Fact`, `GeneFacts`, `GeneFactsUtils` (own `try/except
  ImportError` + `_import_error("gene_facts", e)` block, each name set to
  `None` on failure), and the four names in `__all__`.

No existing function signature, model field or constant changed. The only
edits to existing files are additive.

## NO-INJECTION CHECK: it ran, and it passed

`tests/literature/test_facts.py::TestNoInjection` (2 tests). `GeneFactsUtils`
is constructed with **no seams** — `GeneFactsUtils(token_file=None,
kbase_token_file=None)`, nothing else; `token_file`/`kbase_token_file` are not
seams of this class and are silenced only to keep the real HOME's token files
out of the test.

- **Real cache, on disk.** HOME is relocated to `tmp_path`, a
  `~/.kbutillib/config.yaml` there names the cache path (and
  `europepmc_rps: 50` so the real throttle's real sleep is negligible), and
  `shared_env_utils.DEFAULT_CONFIG_FILE` is pointed at it (that location is
  bound at import time). Asserted: `utils.cache.path == cache_path`, the file
  exists, and `sqlite3` opened directly on it shows **exactly one row** in
  table `gene_facts` whose key **is** `first.cache_key`, whose stored
  `status` is `"ok"`, and which carries **no** `from_cache` field.
- **Real Europe PMC client.** `isinstance(utils.europepmc, EuropePMCClient)`.
  Only `requests.adapters.HTTPAdapter.send` is intercepted — a socket, not a
  seam — replaying `tests/literature/fixtures/get_batch.json` (reused from
  t1). One batched lookup for three ids.
- **Real llm dispatch.** No monkeypatching of `llm.complete` / `acomplete`,
  and none of `GeneFactsUtils`. The api type is registered by the test with
  `llm.register_api_type` and resolved **by name** through
  `llm.complete → get_api`. Asserted: the fake received an `LLMRequest` whose
  `json_schema == FACTS_JSON_SCHEMA` (and whose fact-type enum is
  `list(FACT_TYPES)`), whose `user` carries `[PMID:15210978]`, and that
  `"web_search" not in request.params` in context mode — while the second
  test, same wiring against the api type that reports
  `supports_web_search = True`, gets `mode == "web"` and
  `params["web_search"] is True`.
- **Real round trip through the registry's capability answer.** Mode
  resolution is not stubbed: `'auto'` resolves to context on the searchless
  type and `'required'` resolves to web on the searching one, both through
  `llm.supports_web_search`.
- **The second call is served from the row on disk.** A *separately
  constructed* `GeneFactsUtils` makes the same call: `from_cache is True`,
  same `cache_key`, same statements, `Script.count()` still `1` (no model
  call) and `len(sent)` still `1` (the Europe PMC lookup came from the shared
  `http_cache` in the same file).

The recorded fixture pays a bonus: its third supplied id
(`10.1038/nsmb.1583`) is not in the response, so the test also pins that an
unresolvable id lands in `papers_used.unresolved` and never becomes citable.

## success_criteria_check

| criterion | where |
|---|---|
| `gene_facts`/`genes_facts` return `GeneFacts` with typed facts and verified citations **through `kbutillib.domains.ai.llm`** | `facts.py` `_plan`/`_finalise` call `llm.LLMRequest` + `llm.complete`/`llm.acomplete`; `TestNoInjection` proves the dispatch is real |
| context/web mode resolved per the api type's `supports_web_search` | `_resolve_mode`; `TestModeResolution` (6 tests, both api types) |
| `'required'` refused **before any call** | `_resolve_mode` raises `CapabilityError`; `test_required_raises_BEFORE_the_api_type_is_called` asserts 0 model calls *and* 0 Europe PMC lookups; `test_batch_refuses_required_before_any_gene_runs` |
| no call when ungrounded | `TestNoCallWhenUngrounded` (6 tests: no papers, no text, snippet-only *is* grounding, retrieval failure, web mode still calls) |
| caching on the specified input hash | `TestCacheKey` (19 cases) + `TestCaching` (5) |
| per-gene failure isolation | `TestBatch::test_one_failing_gene_does_not_abort_the_others` |
| offline tests with fake registered api types cover each behaviour | 85 tests, zero network, zero subprocess, zero real model |
| NO-INJECTION CHECK | `TestNoInjection`, above |

## tests_run

Commands, verbatim, all foreground. `pytest` is not installed in this task's
`.task-venv`, so the suite was run with the machine's `kbdl` venv
**read-only** (`PYTHONPATH=$PWD/src`, nothing installed into it) — the same
interpreter and the same invocation shape as
`build-baselines/kbutillib-h100-main-6310d61.json` records.

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest tests/literature/test_facts.py \
  -q -p no:cacheprovider --no-header
# 85 passed in 2.76s                                            exit 0
```

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest tests/literature \
  tests/domains/ai/llm -q -p no:cacheprovider --no-header
# 1 failed, 399 passed, 2 skipped in 19.10s                      exit 1
# the 1 failure is pre-existing and machine-local -- see below
```

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest tests/literature \
  tests/domains/ai/llm tests/core -q -p no:cacheprovider --no-header
# 4 failed, 762 passed, 6 skipped, 1 xfailed, 8 errors in 34.88s  exit 1
```

The full `tests/` tree was **not** run: the h100 baseline measures it at
2665-3416 s, 4.4-5.7x the 600 s sub-agent Bash ceiling, and records that only
a conductor may run it backgrounded. The selection above is the union of
everything this change can reach — the literature package, the `llm` wrapper
it calls, and `tests/core` for the toolkit property and the top-level guarded
exports.

## regression framing

`build-baselines/kbutillib.json` exists (2026-09-13 @ `0f84803`) and
`build-baselines/kbutillib-h100-main-6310d61.json` beside it, so **no new
baseline file was written and the full suite was not re-run to establish
one**. Both cached baselines were captured with different commands in
different environments, so neither is name-comparable against this
selection — the h100 one says so itself.

A **targeted baseline was therefore measured directly**, which is the only
honest comparison available: a scratch worktree at the base commit
(`git worktree add --detach /scratch/chenry/t5-base ec0dc1c`), the identical
selection, the identical interpreter, removed afterwards with
`git worktree remove`.

```
base   @ ec0dc1c : 4 failed, 677 passed, 6 skipped, 1 xfailed, 8 errors
branch           : 4 failed, 762 passed, 6 skipped, 1 xfailed, 8 errors
```

Compared **by nodeid**, the 12 non-passing names are **identical**: new on
branch `(none)`, fixed-vs-base `(none)`. `677 → 762` is exactly `+85`, the
85 tests this task adds — every one of which passes.

The 12 pre-existing non-passing nodeids:

- `tests/domains/ai/llm/test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint` — the h100 user config at `/home/chenry/.kbutillib/config.yaml` leaks past the test's monkeypatched HOME and contributes a `mag` endpoint. Documented in the h100 baseline's `MACHINE_LOCAL_FAILURES_NOT_PRODUCT_DEFECTS` block as a known non-hermetic test, not a product defect.
- `tests/core/test_cli_cap.py::TestCapList::{test_json_output_is_valid, test_json_output_has_required_keys, test_json_filter_by_tag}` — capability-registry CLI, missing optional scientific deps.
- 8 setup **errors** in `tests/core/test_composition_smoke.py` (`TestMSBiochemUtils`, `TestMSFBAUtils`, `TestKBModelUtils`, `TestEscherUtils`) — all `Failed to load ModelSEED database: No such file or directory: '/scratch/chenry/ModelSEEDDatabase/Biochemistry/'`, plus the worktree-relative data-path trap the h100 baseline documents under `WORKTREE_MEASUREMENT_TRAP`.

None of the twelve is touched by this change, and all twelve fail identically
at `ec0dc1c` in the same location with the same interpreter.

## lint

```
.task-venv/bin/python -m ruff check --no-cache \
  src/kbutillib/domains/literature/facts.py \
  src/kbutillib/domains/literature/__init__.py \
  src/kbutillib/__init__.py tests/literature/test_facts.py
# All checks passed!                                              exit 0
```

`ruff` was installed into **this task's own** `.task-venv` (not into any
service venv) because no `ruff` exists on this machine. Two `F401`s it
caught (unused `Gene`, `Organism` in the test imports) were fixed.

`src/kbutillib/toolkit.py` reports **1 `I001`** (import block un-sorted) —
and reported the same single `I001` at the base commit, verified by linting
`git show ec0dc1c:src/kbutillib/toolkit.py`. t4 placed
`from .domains.literature.annotate import GeneLiteratureUtilsImpl` between
the two `kbase` imports; the new `facts` import was placed beside it rather
than in sorted position, so the two literature imports stay together and the
lint count is unchanged (1 → 1). Re-sorting the whole 48-line
`TYPE_CHECKING` block would be an unrequested cleanup across ~45 unrelated
lines.

## divergences reconciled (prompt vs. tree)

The prompt was written against `wip` and is wrong in six places. Each was
read out of the worktree and adapted; none was a reason to stop.

1. **"New package `src/kbutillib/domains/literature/` (create `__init__.py`)"**
   — the package **already exists** with `annotate.py`, `europepmc.py`,
   `paperblast.py`, `cache.py` and a lazy-loader `__init__.py` from t1/t2/t4.
   Adapted: added 14 names to the existing `__all__` and `_MODULE_MAP`.
2. **"Tests go under `tests/literature/` (create it)"** — already exists with
   four test modules. Adapted: added `test_facts.py` beside them and reused
   `test_europepmc.load_fixture` and `fixtures/get_batch.json`.
3. **"If `cache.py` does not exist, create a small SQLite helper
   `LiteratureCache(path)` with `get(table,key)`/`put(table,key,value_json)`"**
   — it exists, with that exact API, WAL mode, an `RLock`, the
   `gene_literature.cache_path` config key and `resolve_cache_path`. Nothing
   was created; `facts.py` uses it as-is, table `gene_facts`.
4. **"There is NO existing `CapabilityError` anywhere in KBUtilLib: define it
   in facts.py (subclass of `ValueError`)"** — **false.**
   `kbutillib.core.errors.CapabilityError` has existed as the
   capability-registry base error (a `KBUtilLibError`, exported from
   `kbutillib.core`, used by `tests/core/test_registry.py`) since before this
   PRD. A second, unrelated class of the same name would be a live trap:
   `except kbutillib.core.CapabilityError` would silently fail to catch it.
   Reconciled by defining
   `class CapabilityError(ValueError, _RegistryCapabilityError)` in
   `facts.py` — literally a `ValueError` subclass as instructed, *and*
   catchable under the pre-existing name. It is exported from
   `kbutillib.domains.literature` but deliberately **not** added to the
   top-level `kbutillib.__all__`, which is the one namespace where the two
   names could collide. Pinned by
   `test_capability_error_is_catchable_as_either_name`.
5. **"`EVIDENCE_CLASSES = ('direct','homolog','curated','mention')`" in
   facts.py** — `annotate.py` already defines `EVIDENCE_CLASSES` with the
   same four members (ordered strongest-claim-first:
   `('curated','direct','homolog','mention')`), and the package's lazy loader
   can map a name to only one module. A second tuple would drift and the
   loader would advertise whichever module imported first. Reconciled:
   `facts.py` **imports** it, so `facts.EVIDENCE_CLASSES is
   annotate.EVIDENCE_CLASSES`, pinned by
   `test_evidence_classes_is_ONE_tuple_shared_with_layer_one`. The prompt's
   *prioritisation* order (`direct > curated > homolog > mention`) is a
   different thing and is implemented as specified, in `_CLASS_PRIORITY`.
6. **C6 vs. the base prompt's mode design.** C6 reads "call the model only if
   at least one selected paper has a non-empty abstract or supplied snippet".
   Taken literally that forbids every web-mode call with no papers — which
   contradicts the base prompt's own rule ("**if mode=='context'** and there
   are no papers → `no_evidence`") and would make `papers=None` web mode,
   the entire point of web mode, impossible. Read as the coherent
   requirement it plainly is — the gate is about *context* grounding — so the
   short-circuit applies in context mode only, and a web-mode call proceeds
   because the live web is its grounding. This matches the success criterion
   as worded ("making no call when ungrounded"): in web mode the model is not
   ungrounded. A web-mode call shaped by a retrieval failure is still never
   cached (`test_a_partial_retrieval_failure_is_reported_and_not_cached`).

Two additions the prompt did not list, both small and both flagged here
rather than buried:

- **`GeneFacts.notes: str = ""`.** `FACTS_JSON_SCHEMA` carries a `notes`
  field (as specified) but the listed `GeneFacts` fields had nowhere to put
  it, so the model's note would have been generated and thrown away.
- **`context_token_budget` as a keyword-only parameter** on both public
  methods, defaulting to `DEFAULT_CONTEXT_TOKEN_BUDGET = 24000`. C2 names the
  parameter and its default but the prompt's signature line predates it.

## inferred rather than verified

- **No live model was called and no live Europe PMC request was made** —
  per the NO PAID CALLS constraint. Every api type in the suite is registered
  by the test; HTTP is intercepted at `HTTPAdapter.send`. So the *shape* of
  `facts.py`'s `LLMRequest` is verified against the `ApiType` contract and
  against two conforming fakes, but **not** against ARGO, OpenAI, the Claude
  CLI or the Codex CLI. In particular: whether a given provider honours
  `json_schema` strictly enough that `LLMResult.parsed` is populated is
  untested here — which is why `_finalise` falls back to `json.loads(text)`
  before giving up.
- **Web mode's prompt discipline is untested against a real searching
  model.** That an actual `codex --search` or `claude` run returns PMIDs
  rather than bare URLs is an assumption; `verify_facts` is written so that
  the assumption failing costs the citation (dropped) rather than the answer
  (`resolve` is the gate).
- **`CHARS_PER_TOKEN = 4`** is the conventional English estimate, not a
  measurement against any tokeniser. It only has to be deterministic (it
  feeds the cache key through the context text); it is not a safety margin
  against a provider's hard context limit.
- **`EuropePMCClient.get` returning the same record object under several
  keys** is relied on by `_retrieve` (it groups by `id(record)`). That is
  what `get` does today — `found.setdefault(tokens[0], hit)` then
  `found.setdefault(requested, hit)`, same `hit` — and it is exercised by
  `TestNoInjection` against the real method, but it is an internal property
  of that method, not a documented contract.
- **Python 3.9.** CI runs 3.9 and every model field uses `Optional[...]`, not
  PEP 604; `asyncio.to_thread` (used by `genes_facts`) is 3.9+. This was
  verified by reading, not by running: no 3.9 interpreter exists on this
  machine (the suite ran on 3.11.15).
