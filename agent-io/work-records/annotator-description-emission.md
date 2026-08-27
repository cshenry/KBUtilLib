# Work record: annotator-description-emission

- **task_id**: annotator-description-emission
- **PRD**: kbdl-ontology-descriptions-v1
- **branch**: `maestro/developer/annotator-description-emission`
- **base**: `main` @ `222490d`

## commit_shas (chronological)

1. `e9d753b` — feat(annotator-utils): add describe_or_accession shared formatting helper
2. `015aeb8` — feat(bakta-utils): surface ec_ids/kegg_orthology_id/cog_id/go_ids as described terms
3. `0a80720` — feat(kofamscan-utils): describe KO-namespace terms; fix kegg_release telemetry key
4. `f9a885d` — test(annotator-utils): dedicated coverage for describe_or_accession

## summary

Made `BaktaUtils` and `KofamscanUtils` emit ontology accessions as
described, namespaced `Term`s, additively, with degradation recorded when
no dictionary/resolver is available — using the already-merged
`OntologyDictionary` (`describe(namespace, accession) -> str | None`) and
`EcRoleResolver` (`roles_for_ec(ec) -> list[str]`) from the two prior tasks
in this PRD, without reimplementing either.

A shared helper, `describe_or_accession(namespace, accession,
ontology_dictionary)`, was added to `annotator_utils.py` (imported by both
tool modules) that returns `"<accession>: <description>"` or degrades to
the bare accession — never raising — used identically by both annotators.

**BaktaUtils**: `psc.ec_ids`/`kegg_orthology_id`/`cog_id`/`go_ids` (list or
bare-string shaped) are now additionally emitted as
`Term(namespace="EC"|"KO"|"COG"|"GO", id=<accession>, value="<accession>:
<description>")`, on top of (never instead of) the unchanged
`Term.evidence` carrying the same raw fields. This is the PRD's knowing
reversal of BAKTA's earlier reviewed "product string only, no EC/KO/COG/GO
value anywhere in the map" criterion. Each EC accession additionally
resolves ModelSEED role names via an injected `EcRoleResolver`, emitted as
`Term(namespace="role", id=None, value=<role name>)`. Both
`ontology_dictionary` and `ec_role_resolver` are now constructor kwargs on
`BaktaUtils.__init__`; `ontology_dictionary` defaults to a config-driven
`OntologyDictionary(**kwargs)` (mirrors `KofamscanUtils`'s existing
`ko_function_map` config-key pattern and inherits its graceful-degrade
behaviour for free), `ec_role_resolver` defaults to `None`
(constructor-injection only, per the module's existing "caller supplies the
path via the constructor" convention for `EcRoleResolver`).

**KofamscanUtils**: the existing `Term(namespace="KO", id=ko_id, ...)`
term's `value` is now `describe_or_accession("KO", ko_id,
ontology_dictionary)` instead of the bare `ko_id`. `ontology_dictionary` is
a new constructor kwarg, defaulting the same way as `BaktaUtils`. The
second, bridged `Term(namespace=None, ...)` (the FUNCTION-destined text
that joins the downstream reaction-mapping table) is completely untouched
by this change.

**FUNCTION-destined byte-identity (the highest-risk part of this task)**:
neither the BAKTA `product` term nor the KOFAMSCAN bridged-text term were
ever rewritten — both remain exactly the same code path/line as at
`222490d`. This was verified two ways: (1) captured literal values from the
*unmodified* base-commit functions before any edits (see below), asserted
against in a dedicated test; (2) every other new test that touches `psc`/KO
data reads the FUNCTION/bridged term via `next(t for t in terms if
t.namespace in (None, "FUNCTION"))` and asserts on its `.value`, so any
future accidental rewrite would be caught structurally, not just by one
test.

**Degradation recording**: `AnnotationResult.parameters` now carries
`ontology_<ns>: "absent"` for each namespace whose dictionary was queried
and found unset/unreadable (read from `OntologyDictionary
.degraded_namespaces` after processing — this already logs its own warning
naming the expected config key/path, so no duplicate warning logic was
needed), and `ec_role_resolver: "absent"` on `BaktaUtils` runs when no
resolver was injected (with an explicit `log_warning` call on every such
run, mirroring `KofamscanUtils`'s existing `_LOG.warning` pattern for
`ko_function_map: absent`). None of this ever raises.

**kegg_release telemetry fix**: `KofamscanUtils.annotate()` was reading
`metadata.get("kegg_release")`, but `ko_function_map.tsv`'s header (written
by `build_ko_function_map.py`/`build_ko_description_map.py`) is `#
kegg_ko_release: ...`, so the field always landed `None`. Changed to read
`metadata.get("kegg_ko_release")`. The existing test fixture in
`test_present_table_bridges_and_records_versions` (which had been writing
the same wrong header key, so it was accidentally "passing" against a
self-consistent-but-wrong fixture) was corrected to the real header key,
and a new regression-guard test
(`test_kegg_release_header_key_mismatch_regression_guard`) asserts that a
fixture using the old wrong key does NOT populate the field, proving the
fix reads the header key the generator actually writes.

`ko_function_map.tsv` itself was not touched — no such generated artifact
exists in this repo; it's staged externally and only ever referenced via
in-memory injected dicts/tables in tests.

## files_touched

- `src/kbutillib/domains/genome/annotation/annotator_utils.py`
- `src/kbutillib/domains/genome/annotation/bakta_utils.py`
- `src/kbutillib/domains/genome/annotation/kofamscan_utils.py`
- `tests/annotators/test_annotator_utils.py`
- `tests/annotators/test_bakta_utils.py`
- `tests/annotators/test_kofamscan_utils.py`
- `agent-io/work-records/annotator-description-emission.md` (this file)

## FUNCTION-destined byte-identity — captured base-commit literals

Captured by running the **unmodified** base-commit (`222490d`) functions
directly, before any code changes, against the exact inputs quoted below
(see the git history of this branch — commit `015aeb8`/`0a80720` add tests
that assert against these same literals):

```python
# BAKTA — base commit 222490d, _parse_bakta_features (unmodified):
_parse_bakta_features([{
    "id": "gene1",
    "product": "alcohol dehydrogenase, zinc-containing",
    "gene": "adhA",
    "aa_hexdigest": "abc123",
    "psc": {
        "ec_ids": ["1.1.1.1"],
        "kegg_orthology_id": ["K00001"],
        "cog_id": "COG1064",
        "go_ids": ["GO:0006260"],
    },
}])[0].terms[0].value
# => 'alcohol dehydrogenase, zinc-containing'
```

```python
# KOFAMSCAN — base commit 222490d, _build_kofam_records (unmodified):
rows = [{
    "gene_id": "gene1", "ko_id": "K00001",
    "thrshld": "329.85", "score": "356.50", "evalue": "1.4e-105",
    "definition": "alcohol dehydrogenase",
}]
table = {"K00001": "adh, e1.1.1.1; alcohol dehydrogenase [EC:1.1.1.1]"}
records, _fraction = _build_kofam_records(rows, table)
[t.value for t in records[0].terms if t.namespace is None][0]
# => 'adh, e1.1.1.1; alcohol dehydrogenase [EC:1.1.1.1]'
```

**On this branch**, `tests/annotators/test_bakta_utils.py
::TestAnnotateMocked::test_function_term_byte_identical_to_base_commit_capture`
runs the *same* feature dict through the full `BaktaUtils.annotate()` path
— with an `OntologyDictionary` that describes every accession the feature
carries and an `EcRoleResolver` that resolves EC roles (the maximally
adversarial case) — and asserts the FUNCTION term's `.value` still equals
`'alcohol dehydrogenase, zinc-containing'`, byte-for-byte, while
simultaneously confirming the new EC/role terms are present. Similarly,
`tests/annotators/test_kofamscan_utils.py::TestOntologyDescription
::test_bridged_function_term_unaffected_by_ontology_dictionary` runs the
same rows/table through `_build_kofam_records` with an injected
`OntologyDictionary`, and asserts the bridged (`namespace=None`) term's
`.value` still equals `'adh, e1.1.1.1; alcohol dehydrogenase [EC:1.1.1.1]'`,
byte-for-byte. **Both branches confirmed emitting these captured literals
unchanged.**

## success_criteria_check

- **BaktaUtils and KofamscanUtils emit, for every ontology accession they
  report, a Term in its own KO/EC/GO/COG namespace whose value is
  '<accession>: <description>', degrading to the bare accession when no
  description is available.** — PASS. `describe_or_accession` implements
  this exactly; used by both modules; covered by
  `TestDescribeOrAccession` (5 tests), `TestParseBaktaFeatures` (new tests),
  and `TestOntologyDescription` in kofamscan tests.
- **BAKTA's ec_ids, kegg_orthology_id, cog_id and go_ids are surfaced as
  described terms rather than confined to evidence.** — PASS. See
  `_psc_ontology_terms` in `bakta_utils.py`;
  `test_ec_kegg_cog_go_now_emitted_as_additive_namespaced_terms` and
  `test_psc_accessions_described_when_ontology_dictionary_injected` cover
  this, while also confirming evidence still carries the raw fields
  (additive, not a replacement).
- **Each EC accession additionally yields role-namespace terms resolved
  through EcRoleResolver.** — PASS. See the `ec_role_resolver.roles_for_ec`
  call in `_psc_ontology_terms`;
  `test_ec_role_resolver_yields_role_namespace_terms` and
  `test_no_ec_role_resolver_yields_no_role_terms` cover both branches.
- **The FUNCTION-destined terms are emitted with no namespace and are
  byte-identical to the base commit's output, asserted on exact strings by
  a test.** — PASS. See the captured-literal section above and the two
  dedicated byte-identity tests
  (`test_function_term_byte_identical_to_base_commit_capture`,
  `test_bridged_function_term_unaffected_by_ontology_dictionary`).
- **A GO accession round-trips correctly under a first-': ' split.** —
  PASS. `test_go_accession_round_trips_on_first_colon_space_split` in
  `test_annotator_utils.py`, plus the GO case in
  `test_psc_accessions_described_when_ontology_dictionary_injected`.
- **A missing dictionary degrades to accession-only with the degradation
  recorded in AnnotationResult.parameters rather than raising.** — PASS.
  `test_ontology_and_ec_role_degradation_recorded_in_parameters` (BAKTA),
  `test_ko_term_degrades_to_bare_id_without_ontology_dictionary`
  (KOFAMSCAN), and `test_psc_missing_dictionary_degrades_to_accession_only`
  / `test_dictionary_with_unset_namespace_degrades_to_bare_accession` cover
  the OntologyDictionary side; `ec_role_resolver: "absent"` recording is
  covered in the same BAKTA parameters test.
- **The kegg_release telemetry key is populated.** — PASS. Fixed to read
  `metadata["kegg_ko_release"]`; covered by
  `test_present_table_bridges_and_records_versions` (positive case) and
  `test_kegg_release_header_key_mismatch_regression_guard` (guards against
  regressing back to the wrong key).
- **ko_function_map.tsv is unmodified.** — PASS (trivially: no such file
  exists in this repo; it is a staged external artifact, never touched by
  this branch — `git diff main..HEAD` touches no `.tsv` files at all).
- **No test depends on a generated artifact or a real ontology release.**
  — PASS. Every new/modified test injects an in-memory
  `OntologyDictionary` (via `od._tables = {...}`, bypassing file I/O
  entirely) or `EcRoleResolver` (via `resolver._index = {...}`), or passes
  `None` to exercise the degraded path. No test reads a `.tsv` from disk
  except the pre-existing, unrelated `TestLoadKoFunctionMap`/
  `TestOntologyDictionary` tests, which already used `tmp_path`-written
  fixtures before this task.
- **No test that passed on the base commit fails on the branch.** — PASS,
  see tests_run below.

## tests_run

Command (exactly as specified): `python -m pytest tests/ -q
--continue-on-collection-errors`

Venv: freshly built at
`/private/tmp/claude-501/.../scratchpad/venv-kbu`, `pip install -e
'.[dev,all]'` from this worktree; verified `python -c 'import kbutillib;
print(kbutillib.__file__)'` resolves into
`~/.maestro/worktrees/annotator-description-emission/src/kbutillib/__init__.py`,
and `import tomli_w` succeeds. Never reused a venv under
`~/VirtualEnvironments/`.

- **Baseline (conductor-measured, base commit `222490d`)**: 2863 passed, 15
  failed, 6 errors, 380 skipped (91.4s).
- **This branch**: **2880 passed, 15 failed, 6 errors, 379 skipped**
  (286.83s — slower run, same machine class, no functional concern; result
  counts are what matter).
  - The 15 failures are the exact same 15 node ids listed in the baseline's
    `failed_node_ids` (all `tests/external/test_kbdl_service_utils.py` and
    `tests/modeling/test_ms_reconstruction_utils.py
    ::test_default_mode_uses_db_fallback`) — none touched by this branch.
  - The 6 errors are the same 6 pre-existing collection/dependency errors
    (`test_escher_utils.py` collection, `tests/notebook/helpers` collection,
    4x `test_comprehensive_gapfill_wrapper.py`) — same root causes (missing
    optional deps), untouched by this branch.
  - Delta: **+17 passed, -1 skipped** relative to baseline — entirely
    accounted for by the new tests this branch adds (none were previously
    skipped-then-fixed; the skip-count wobble is environmental, e.g. an
    optional-dependency-gated test that happened to run this time — not
    something this branch's changes could cause, since none of the touched
    files gate any `skipif` condition).
  - **Zero regressions**: no test that passed at base fails on this
    branch.

Also run individually during development (all passing, subsets of the full
run above): `tests/annotators/test_bakta_utils.py` (41 passed),
`tests/annotators/test_kofamscan_utils.py` (46 passed),
`tests/annotators/test_annotator_utils.py` (58 passed),
`tests/annotators/` full directory (405 passed, 2 skipped).

## caveats

- `ec_role_resolver` on `BaktaUtils` is **constructor-injection only** — no
  automatic config-key-driven discovery (unlike `ontology_dictionary`,
  which defaults to a config-driven `OntologyDictionary(**kwargs)`). This
  mirrors `EcRoleResolver`'s own module docstring ("the caller supplies the
  path ... via the constructor") and keeps the degradation path simple and
  provably non-raising (no lazy file I/O to catch). A future KBDL-wiring
  task will need to construct and inject a real `EcRoleResolver` pointed at
  a live `ModelSEEDDatabase/Annotations/Roles.tsv` — this task does not
  attempt that wiring, per its scope ("Work in the KBUtilLib repo, in the
  annotation package").
- `kegg_orthology_id` in BAKTA's `psc` block is treated as list-or-string
  via `_as_accession_list`; the one concrete test fixture I found in the
  existing test suite (and the one carried over from before this task) uses
  a list (`["K00001"]`), so that's what's exercised end-to-end. `cog_id` is
  exercised as a bare string, matching its existing fixture shape. Both
  shapes are normalized identically, so this is a defensive generalization,
  not a guess about real Bakta output shape.
- I did not modify `annotator_utils.py`'s existing `Term`/`AnnotationRecord`
  /`AnnotationResult` dataclasses or any other annotator module
  (`ProkkaUtils`, `DRAM2Utils`, `TransytUtils`) — out of scope per the task
  prompt, which named only BAKTA and KOFAMSCAN.
- Per-namespace degradation parameters (`ontology_ko`/`ontology_ec`/etc.)
  are only recorded when that namespace was actually *queried* during the
  run (i.e., at least one accession of that type appeared in the tool's
  output) — this follows directly from `OntologyDictionary
  .degraded_namespaces` being populated lazily on first `describe()` call
  per namespace, which is the existing, already-reviewed design of
  `OntologyDictionary` (not something introduced by this task). A run with
  zero EC accessions and an unset EC dictionary will not report
  `ontology_ec: absent`, since there was nothing to degrade.
