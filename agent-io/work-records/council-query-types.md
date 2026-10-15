# Work Record — council-query-types

## task_id
council-query-types

## branch
maestro/developer/background-reference-only-agent--council-query-types

## base commit
b084561 (`docs(work-record): fill in the record's own commit sha`)

## commit_shas
- cf02ceb — feat(council): query types, the template renderer and the two builtins
- 55dcbdd — docs(work-record): record the council query-types milestone

## summary
Adds `src/kbutillib/domains/ai/council/queries/` — the only place in the
council where a domain vocabulary lives. `QueryType` (the pydantic spec that
IS the `QueryRenderer` protocol implementation), the `string.Template`
renderer with its closed binding list, `QueryRegistry`, and the two BUILTIN
query types `go_mf@1` and `reaction_pickup@1`, both verbatim ports of Jose
Faria's CONCORDIA councils. Tests pin rendering by BYTE IDENTITY against text
the reference modules themselves produced.

`src/kbutillib/domains/ai/argo_utils.py`,
`src/kbutillib/domains/ai/ai_curation_utils.py`, and the four council modules
the task said not to edit (`council/__init__.py`, `types.py`, `protocols.py`,
`config.py`) are byte-identical to the base commit — `git diff --stat b084561 --`
over all six is empty. The only pre-existing file touched is `pyproject.toml`,
and only to add three package-data globs.

## files_touched
- `src/kbutillib/domains/ai/council/queries/__init__.py` (new)
- `src/kbutillib/domains/ai/council/queries/spec.py` (new — `QueryType`,
  `RoleSpec`, the generic schemas, `generic_postprocess`)
- `src/kbutillib/domains/ai/council/queries/template.py` (new — the
  `string.Template` renderer and the block renderers)
- `src/kbutillib/domains/ai/council/queries/registry.py` (new —
  `QueryRegistry`, `validate_spec`)
- `src/kbutillib/domains/ai/council/queries/errors.py` (new)
- `src/kbutillib/domains/ai/council/queries/generic_schemas.json` (new)
- `src/kbutillib/domains/ai/council/queries/querytype.schema.json` (new)
- `src/kbutillib/domains/ai/council/queries/examples/custom_example.yaml` (new)
- `src/kbutillib/domains/ai/council/queries/builtin/__init__.py` (new —
  `BUILTIN_RENDERERS`, `BUILTIN_ADAPTERS`, `resolve_renderer`,
  `resolve_adapter`, `load_builtin_spec`)
- `src/kbutillib/domains/ai/council/queries/builtin/renderers.py` (new)
- `src/kbutillib/domains/ai/council/queries/builtin/adapters.py` (new)
- `src/kbutillib/domains/ai/council/queries/builtin/go_mf.yaml` (new, package data)
- `src/kbutillib/domains/ai/council/queries/builtin/reaction_pickup.yaml` (new, package data)
- `tests/domains/ai/council/test_queries.py` (new — 51 tests)
- `tests/domains/ai/council/fixtures/queries/*.json` (new — 3 input cases)
- `tests/domains/ai/council/fixtures/queries/*.txt` (new — 14 expected prompts)
- `pyproject.toml` (modified — three package-data globs, nothing else)

## PORTED SOURCES — path, line range, SHA-256 measured on this machine

All digests measured with `sha256sum` on poplar (h100) on **2026-10-15**.
Every named source was present and readable; none was missing or unreadable,
and the stated fallback
(`~/Dropbox/Projects/AIAssistant/go-council-run-2026-09-10/go_council.py`) was
NOT needed and was not used.

| File | SHA-256 (measured) | Lines ported |
|---|---|---|
| `/scratch/chenry/go_council_v3/go_council_kit/go_council.py` | `7a567823a1765f7fee93dd29a7a73a5c7f3c9509952cc72f36f43a1f26ca5ea1` | 50–147 (the six prompt constants), 152–159 (`render_candidates`), 296–362 (candidate blocks, vote table, reasoning block in `run_one`), 364–379 (critic assembly), 431–448 (meta assembly + the 1200-char JSON truncation) |
| `/scratch/jplfaria/CONCORDIA-live/scripts/go_council_pilot.py` | `3563e97c9b32dce7934da341ce5b3ad2d072a215eabbfa5b4ff9306bc804559d` | cross-check only — see below |
| `/scratch/jplfaria/CONCORDIA-live/concord/pickup_labeler_v4.py` | `a6ecbe25217ae60fd01bd0227c70578797914999a79b5f4393328d01b364f36a` | 29–95 (`SYSTEM_PROMPT_BASE`, `PROMISCUITY_HINT`, `USER_TEMPLATE`), 98–130 (`_is_promiscuous`, `_shuffled_candidates`, `_render_candidates`), 133–158 (`_call_one` prompt assembly, 5→12 cap) |
| `/scratch/jplfaria/CONCORDIA-live/concord/pickup_critic.py` | `6d5d84440f5a4b9391bf7bf56378d4bae6ed4610497b69a9b8006058007ce6e4` | 47–113 (prompts), 116–169 (`_render_candidates_block`, `_render_vote_block`, `_render_rationale_block`), 249–265 (`council_critique` assembly) |
| `/scratch/jplfaria/CONCORDIA-live/concord/pickup_meta.py` | `f35ec296e045719fd600355ccfa94df004f4c6c61692cec2c2456cba972c2d97` | 26–89 (prompts), 92–174 (the four block renderers incl. `_render_critic_block`), 267–280 (`meta_reviewer` assembly) |
| `/scratch/jplfaria/CONCORDIA-live/concord/pickup_schema.py` | `4e18d27d908dcc98f09522b210d013b7768561f88e42d6aca68aeb611762cfe4` | 29–81 (`Candidate`, `PickupInput`, `PickedReaction`, `PickupOutput` — the field names, enums and caps the YAML answer schemas encode) |
| `/scratch/jplfaria/CONCORDIA-live/scripts/run_concordia_pickup_council_v4.py` | `84101d14655502fcd991c5add772f615e0e8981d8531a25558e3785188e06951` | 93–151 (`council_pick_v2`: the vote table with zeros included, `n_council_valid`, the `council_picks` payload shape) |

Also read for context (not ported, but `_parse_response` is where the
reference's pick cleaning lives):
`/scratch/jplfaria/CONCORDIA-live/concord/pickup_labeler.py`.

### `go_council_pilot.py` DID match its pinned SHA-256 — and still diverges

The task pinned `3563e97c9b32dce7934da341ce5b3ad2d072a215eabbfa5b4ff9306bc804559d`
for `/scratch/jplfaria/CONCORDIA-live/scripts/go_council_pilot.py`. **The
measured digest is exactly that. It matched.**

The cross-check nevertheless found a real divergence, and the port follows the
file the task names as the SOURCE (the V3 kit), not the cross-check:

* `PROPOSER_USER`, `CRITIC_SYSTEM`, `CRITIC_USER`, `META_SYSTEM` and
  `META_USER` are **byte-identical** between the two files.
* `PROPOSER_SYSTEM` is **not**. The live pilot (mtime 2026-09-18, vs. the V3
  kit's 2026-09-11) carries four extra rules about candidate PROVENANCE —
  `"retrieval"` / `"EC <number> lookup"` / `"name match"` — together with a
  `render_candidates()` that labels every candidate with its `origin` and
  prints `"  exact  "` instead of a score for the lookup-derived ones.

That is a later generation of the query, not V3: it changes both the prompt
and the menu rendering, and porting half of it would produce a prompt that
exists nowhere. `go_mf@1` therefore ports the V3 kit verbatim. The divergence
is recorded in three places so it cannot be rediscovered as a bug: the
`attribution` key of `go_mf.yaml`, the module docstring of
`builtin/renderers.py`, and here.

**No prompt in this change was written by me.** Every word a model will be
shown came out of a reference module through `yaml.safe_dump` and was
round-tripped back through `yaml.safe_load` before the YAML was accepted —
except `examples/custom_example.yaml`, which is a WORKED EXAMPLE of the
template renderer and says so in its own `attribution`; it is not registered
by `seed_builtins` and no builtin uses it.

## How the expected fixtures were produced

`tests/domains/ai/council/fixtures/queries/` holds three input cases
(`*_case.json`) and fourteen expected prompt texts (`*.system.txt`,
`*.user.txt`). The texts were generated ONCE, during development, by a
throwaway script under `/tmp` that imported the CONCORDIA modules (with
`openai` and `concord.argo_client` stubbed, since no call is made) and used:

* the reference prompt CONSTANTS directly;
* the reference BLOCK RENDERERS directly — `go_council.render_candidates`,
  `pickup_critic._render_{candidates,vote,rationale}_block`,
  `pickup_meta._render_critic_block`, `pickup_labeler_v4._is_promiscuous`,
  `_shuffled_candidates`, `_render_candidates`;
* and, for the three assemblies the reference performs INLINE inside an async
  driver that would make a network call (`go_council.run_one`,
  `pickup_labeler_v4._call_one`), the assembly expressions copied verbatim out
  of the cited line ranges. The surrounding call was not copied.

**The committed tests do not import the reference.** Those modules live on
`/scratch` on one machine and import `openai`; a test that needed them would
only ever run on poplar. The fixtures are the contract.

Every one of the fourteen matched my renderers on the first run.

## environment

SQLAlchemy, psycopg, pytest, pandas, duckdb and httpx were not in a fresh task
venv. Installed into THIS TASK'S OWN venv only
(`.task-venv/` inside the worktree — no shared or service venv was touched):

```
python -m pip install -e '.[council]'
python -m pip install -e '.[dev,ai]'
python -m pip install pytest pytest-asyncio pytest-cov ruff
```

Interpreter CPython 3.12.3, SQLAlchemy 2.1.4, pytest 9.1.1, pydantic 2.14.1.
A reviewer must do the same in its own venv; an ImportError here is an
environment gap, not a verdict.

`jsonschema` 4.10.3 is present in this venv as a transitive dependency. It is
NOT declared in `pyproject.toml` and nothing under `src/` imports it; the three
tests that validate a schema against its metaschema use
`pytest.importorskip("jsonschema")`, so they skip rather than fail where it is
absent.

## verification

| Command | Exit | Result |
|---|---|---|
| `python -m pytest -q -p no:cacheprovider tests/domains/ai/council/test_queries.py` | 0 | **51 passed** |
| `python -m pytest -q -p no:cacheprovider tests/domains/ai/council/` | 0 | 343 passed, 190 skipped |
| `python -m ruff check --output-format=concise src/kbutillib/domains/ai/council tests/domains/ai` | 0 | All checks passed |
| full suite at base `b084561`, clean tree, same venv, chunk 1 + chunk 2 | 1 | **26 failed**, 4447 passed, 619 skipped |
| same two commands on this branch | 1 | **26 failed**, 4498 passed, 619 skipped |

The 190 skips in the council directory are the PostgreSQL parametrisations of
the pre-existing store contract (`KBU_COUNCIL_TEST_PG_DSN is not set`); none is
in `test_queries.py`.

### Regression comparison — BY NAME, against a base run in this venv

`build-baselines/kbutillib.json` is stale (captured 2026-09-13 at `0f84803`
on CPython 3.11 in a more minimal venv) and the dispatcher said so, so it was
not used to adjudicate. A base run was taken FIRST, on a clean tree at
`b084561`, before any file was written, in the venv above.

The suite was run in two chunks to stay inside the 600 s per-call limit, with
the cached baseline's own `--ignore` list split across them:

```
chunk 1: tests/annotators tests/arc_context tests/berdl tests/beril_worktree
         tests/biochem tests/cli tests/core tests/domains
         --ignore=tests/berdl/test_clearinghouse_derivation.py
         --ignore=tests/biochem/test_escher_utils.py
         --ignore=tests/biochem/test_ms_biochem_deltag.py
chunk 2: tests/external tests/guard tests/harness tests/interfaces
         tests/kb_app_runner tests/kbase tests/kb_job_utils
         tests/koros_arc_store tests/modeling tests/models_and_analyses
         tests/verab tests/test_argo_utils_construct.py
         tests/test_lp_solver_service.py tests/test_python_floor_syntax.py
         --ignore=tests/kbase/test_kb_annotation_utils_parquet.py
         --ignore=tests/modeling/test_comprehensive_gapfill_wrapper.py
         --ignore=tests/modeling/test_ms_reconstruction_utils.py
```

**The two FAILED sets are the same 26 nodeids; `comm -13` and `comm -23` over
the sorted lists are both empty.** All 26 are `tests/cli/test_kind.py`,
`tests/cli/test_clearinghouse*.py` and one `tests/core/test_composition_smoke.py`
case — untouched by this change. The +51 passes are exactly this task's new
tests. Skips are unchanged at 619.

I did not rewrite `build-baselines/kbutillib.json`: one exists, the
instruction only requires writing it when absent, and overwriting a shared
cache with numbers from a differently-provisioned venv would not help the next
task any more than it helped this one.

## success_criteria_check

**Criterion 1.** PASS.
- `queries/` provides `QueryType` (`spec.py`), the `string.Template` renderer
  (`template.py`), `QueryRegistry` (`registry.py`) and the builtins `go_mf@1`
  and `reaction_pickup@1` with the renderers `go_mf_v3` and
  `reaction_pickup_v4`, whose module docstring records every ported source
  file, line range and SHA-256 (table above).
- `test_queries.py::TestBuiltinRenderingIsByteIdentical` asserts byte-identical
  `(system, user)` for **every role of both builtins** against the checked-in
  fixtures, plus a fourth case for the pickup proposer's non-promiscuous
  branch (the 5-pick cap, which the promiscuous case never exercises).
- User-YAML rendering, validation and postprocessing:
  `TestUserQueryType` (9 tests, against the shipped `custom_example.yaml`).
- Registry immutability and seed idempotence: `TestRegistry` (7 tests, against
  a real `SqliteStore`).
- No test that passed at `b084561` fails on this branch (comparison above).

**Criterion 2 (no-injection check).** PASS.
`TestSpecSurvivesTheDatabase::test_seeded_specs_render_byte_identically_after_a_round_trip`
uses no monkeypatch, no stub and no fixture but `tmp_path`. It calls
`open_store("sqlite:///<tmp_path>/roundtrip.sqlite")`, `init_schema()`,
`QueryRegistry(store).seed_builtins()`; then opens a **second, separate** store
object on the same file (asserting `reader is not writer`), builds a **new**
`QueryRegistry` over it, does `get("go_mf")` and `get("reaction_pickup")`, and
renders **every role of both** — comparing against the same checked-in
fixtures. It also re-renders the plain pickup proposer, which is the only path
that reads `roles.proposer.fragments`, so a fragment lost in the JSON round
trip fails here. A second test,
`test_the_stored_spec_is_byte_equal_to_the_shipped_one`, compares the stored
`spec` column against `QueryType.to_spec()` of the shipped YAML.

**Criterion 3 (provenance).** PASS — the table above gives, for each ported
file, its path, the line range ported and the SHA-256 measured on this
machine, and states explicitly that `go_council_pilot.py` **matched** the
pinned `3563e97c9b32dce7934da341ce5b3ad2d072a215eabbfa5b4ff9306bc804559d`
(and what diverges in it anyway).

## reconciliation — where the prompt and the tree disagreed

1. **`candidate_templates` is REQUIRED by the task's field list, but the two
   builtins ship it empty.** The reference candidate blocks use fixed-width
   numeric columns (`{rank:>2}`, `{pos:>3}`), `:.3f` score formatting, two
   different truncation lengths, and a segment that appears only when the
   entry has a definition. A `string.Template` cannot express any of those, so
   shipping an approximate template beside a builtin renderer would ship a
   lie. `candidate_templates` is therefore required **only when `renderer` is
   unset** (enforced in `QueryType._check`, tested by
   `test_a_template_rendered_spec_must_ship_candidate_templates`), and the two
   builtins render candidates in code.

2. **`adapter` is not in the task's `QueryType` field list, but item 3 says
   builtins name `renderer` AND `adapter`.** Added as
   `adapter: Optional[str]`, resolved exactly like `renderer` (bare name, or
   `name@version` with a version mismatch refusing the query).

3. **`RoleSpec` needed a home for `PROMISCUITY_HINT`.** It is prompt text the
   pickup proposer's system prompt gets conditionally, so it belongs in the
   versioned, stored spec rather than in a Python constant. Added
   `RoleSpec.fragments: Dict[str, str]`; `reaction_pickup.yaml` carries it as
   `roles.proposer.fragments.promiscuity_hint`.

4. **The pickup meta prompt needs the RAW critic reply, but the engine only
   holds the generic one.** `pickup_meta._render_critic_block` reads
   `per_reaction_critique` / `missed_picks` / `overall_note`, none of which
   survives the mapping to the generic critic shape. The pickup adapter
   therefore keeps the reference reply under a `"raw"` key, and
   `renderers.pickup_critic_block` reads it from there. Without this the meta
   prompt could not be a verbatim port at all. Pinned by
   `test_seeded_specs_render_byte_identically_after_a_round_trip` (meta role)
   and by `test_the_fixture_critique_maps_to_the_generic_critic_shape`.

5. **`schema` cannot be a pydantic field name** — it shadows
   `BaseModel.schema` and pydantic refuses the class. The Python attribute is
   `RoleSpec.json_schema` with `alias="schema"`; the YAML, the stored spec and
   `querytype.schema.json` all spell it `schema`, and `to_spec()` dumps
   `by_alias=True`.

6. **`str(SkosPredicate.X)` vs `.value`.** The reference renders
   `c.skos.value.replace("skos:", "")`, which always strips the prefix because
   `skos` is always the enum. A menu entry carries the plain string, so
   `renderers._skos` strips the prefix directly. Verified by byte identity
   against the reference-generated fixture.

7. **A ballot's error is a `CouncilError`, not a Python exception.** The
   reference interpolates `f"{type(e).__name__}: {str(e)[:160]}"`; the
   renderers interpolate `f"{kind}: {message}"`. Documented in
   `template._error_text`; the GO fixture includes an errored seat so the line
   is covered.

8. **The critic's "every voted reaction is rated consistent" clause.** Judged
   over the rows the critic actually returned. A voted reaction the critic
   omitted entirely is not evidence of approval, but it is also not a rating,
   and synthesising one would be the adapter making the critic's judgement for
   it. Stated in `pickup_critic_approved`'s docstring.

## things I inferred rather than verified

- **The engine's `context` shapes.** `protocols.QueryRenderer` names the keys
  (`votes`, `ballots`, `critic`) but not their types, and the engine that will
  fill them is a later milestone. The renderers accept `votes` as
  `Mapping[str, int]` and `ballots` as either `types.Ballot` models or the
  plain dicts a store round trip produces (`template.normalise_ballots` takes
  both), and read `critic` as the POSTPROCESSED generic dict. If the engine
  lands with a different shape, `normalise_ballots` is the one place to change.
- **Tie order in the GO vote table.** `render_votes` sorts by descending count
  with a stable sort, which reproduces `collections.Counter.most_common` only
  if the engine inserts ids in first-vote order — which is what the reference
  does and what the fixture asserts (`assert dict(votes) == case["votes"]` in
  the generator). A future engine that built the dict differently would change
  tie order.
- **The pickup answer schemas.** The reference has pydantic models, not JSON
  Schema. The three schemas in `reaction_pickup.yaml` were written from
  `pickup_schema.py` lines 29–81 and the three `USER_TEMPLATE` JSON shapes;
  field names, enums and the 15/5 caps are transcribed, but the reference
  never validated against a JSON Schema, so this is a faithful translation
  rather than a copy.
- **`menu_fields` as the declaration of a candidate template's `$meta_<key>`
  bindings.** The task names the field but not its use; binding validation at
  registration time needs a declared key set, and this is the only field that
  could supply one.
