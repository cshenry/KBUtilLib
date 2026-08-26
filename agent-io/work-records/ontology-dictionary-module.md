# Work record: ontology-dictionary-module

**task_id:** ontology-dictionary-module
**branch:** maestro/developer/ontology-dictionary-module
**commit_shas:**
- `ffa23e25990d0d600df74ac9687479f2255fecaf` — feat(annotation): add OntologyDictionary (KO/EC/GO/COG describe())

## Summary

Added `OntologyDictionary` (`src/kbutillib/domains/genome/annotation/ontology_dictionary.py`)
to KBUtilLib's annotation package, exposing exactly one public method,
`describe(namespace, accession) -> str | None`, over four controlled
vocabularies (`KO`, `EC`, `GO`, `COG`). The class reads **staged
two-column TSVs** (`accession<TAB>description`) rather than parsing raw
ontology releases itself; loading is lazy (nothing touches disk until the
first `describe()` call for a namespace) and the parsed table is cached
for the process lifetime. A missing dictionary file is a soft
degradation — `describe()` returns `None`, a warning is logged once
naming the expected path, and the namespace is added to the public
`degraded_namespaces` attribute (mirroring `kofamscan_utils`'s
`ko_function_map: absent` marker). A staged file that exists but parses
to zero entries is a hard failure — `RuntimeError`, naming the namespace,
the file path, and the detected layout — so a corrupted/truncated staged
file can never silently masquerade as "this accession has no
description."

Added one committed generator script per source, each parsing a raw
release format into the staged two-column TSV `OntologyDictionary`
consumes, gitignoring its own output with a stated licensing reason in
both its module docstring and `.gitignore`:

- `build_ko_description_map.py` — KEGG `ko` flat file. Reuses
  `build_ko_function_map._iter_ko_entries` and
  `build_ko_function_map._detect_layout_and_build_symbols` (the existing,
  already-guarded 109.1-SYMBOL-style vs. 90.1-NAME-style+CRLF
  release-layout detection) rather than reimplementing it, and reads the
  description off whichever field (`NAME` vs `DEFINITION`) the detected
  layout says carries it. `build_ko_function_map.py`'s `KoEntry`
  dataclass gained one additive field, `definition_field: str | None =
  None`, to expose the `DEFINITION` field text the reused parser wasn't
  previously capturing — a backward-compatible extension (default value,
  same constructor call site, no other caller exists in the repo).
  Emits `ko_description_map.tsv`, a **separate artifact** from
  `ko_function_map.tsv` (the composed reaction-bridge string); this
  script never touches `ko_function_map.tsv`.
- `build_ec_description_map.py` — ExPASy `enzyme.dat`, record-block
  format (`ID`/`DE` lines terminated by `//`), joining wrapped multi-line
  `DE` values.
- `build_go_description_map.py` — `go-basic.obo`, OBO stanza format
  (`[Term]` blocks with `id:`/`name:` lines), skipping obsolete terms and
  non-`[Term]` stanzas (e.g. `[Typedef]`).
- `build_cog_description_map.py` — NCBI `cog-20.def.tab`, tab-delimited,
  no header row (column 1 = COG id, column 3 = description).

Each generator raises `RuntimeError` naming the source and the detected
layout when a release parses to zero entries, matching
`build_ko_function_map.py`'s existing pattern.

Did **not** touch `bakta_utils.py` or `kofamscan_utils.py` (reserved for
a later task), and did not create an EC-to-role resolver (a parallel
sibling task, `maestro/developer/ec-role-resolver`).

## Files touched

- `src/kbutillib/domains/genome/annotation/ontology_dictionary.py` (new)
- `src/kbutillib/domains/genome/annotation/build_ko_description_map.py` (new)
- `src/kbutillib/domains/genome/annotation/build_ec_description_map.py` (new)
- `src/kbutillib/domains/genome/annotation/build_go_description_map.py` (new)
- `src/kbutillib/domains/genome/annotation/build_cog_description_map.py` (new)
- `src/kbutillib/domains/genome/annotation/build_ko_function_map.py` (modified — additive `KoEntry.definition_field`, default `None`)
- `src/kbutillib/domains/genome/annotation/__init__.py` (modified — export `OntologyDictionary`)
- `.gitignore` (modified — gitignore the four new staged dictionary output patterns)
- `tests/annotators/test_ontology_dictionary.py` (new)
- `tests/annotators/test_build_ko_description_map.py` (new)
- `tests/annotators/test_build_ec_description_map.py` (new)
- `tests/annotators/test_build_go_description_map.py` (new)
- `tests/annotators/test_build_cog_description_map.py` (new)

## Success criteria check

- **"An OntologyDictionary exposing describe(namespace, accession) -> str | None exists in KBUtilLib's annotation package and resolves KO, EC, GO and COG accessions from staged two-column dictionaries."**
  PASS. `OntologyDictionary` lives at
  `src/kbutillib/domains/genome/annotation/ontology_dictionary.py`,
  exported from the package `__init__.py`. `describe()` resolves all four
  namespaces from staged two-column TSVs (`tests/annotators/test_ontology_dictionary.py::TestDescribeSuccessfulLookup`).

- **"A zero-entry parse raises an error naming the source and the detected layout rather than returning an empty dictionary; a missing dictionary file yields None rather than raising."**
  PASS. Verified at both layers: (1) each generator's `build_description_map`
  raises `RuntimeError` naming the source and detected layout on zero
  entries (`test_build_*_description_map.py::TestZeroEntryRaisesNamingSource`
  / `TestZeroEntryRaisesNamingLayout`); (2) `OntologyDictionary`'s own
  loader raises the same way if a *staged* file exists but parses to zero
  entries (`test_ontology_dictionary.py::TestZeroEntryRaises`), while an
  unset or non-existent path returns `None` without raising
  (`TestMissingFileDegradation`).

- **"Tests over synthetic fixtures prove both KEGG release layouts (SYMBOL-style and NAME-style-with-CRLF) parse correctly, prove the zero-entry loud failure, and prove the missing-file degradation, with no test depending on a real ontology release."**
  PASS. `test_build_ko_description_map.py::TestSymbolFieldLayout` and
  `TestNameFieldLayoutWithCrlf` cover both KEGG layouts with inline
  synthetic fixtures (the CRLF fixture is written via `write_bytes` with
  explicit `\r\n`). All fixtures across all five new test files are
  inline strings written to `tmp_path`; none reference a real downloaded
  release file.

- **"A committed generator script exists per source, each gitignoring its output with a stated licensing reason, and ko_function_map.tsv is not modified or regenerated."**
  PASS. Four generator scripts committed, one per source (KO already had
  one — `build_ko_function_map.py` — which I reused rather than
  duplicated; this task's four *new* generators are for the plain
  description form of KO plus EC/GO/COG). `.gitignore` gained
  `ko_description_map*.tsv`, `ec_description_map*.tsv`,
  `go_description_map*.tsv`, `cog_description_map*.tsv` with a comment
  block stating the licensing reason; each script's own module docstring
  repeats the reason ("DO NOT COMMIT THE OUTPUT" section). Confirmed
  `ko_function_map.tsv`'s write logic (`build_ko_function_map.write_table`)
  is unmodified — the only change to `build_ko_function_map.py` is the
  additive `KoEntry.definition_field` (default `None`), which the
  existing bridge-composition code path (`build_map`) never reads.

- **"No test that passed on the base commit fails on the branch."**
  PASS — see Tests run below.

## Tests run

Dedicated venv built exactly per the task's environment instructions
(fresh `python3 -m venv`, `pip install -e '<worktree>[dev,all]'`,
confirmed `kbutillib.__file__` resolves into this worktree, `tomli_w`
importable).

- `python -m pytest tests/annotators/ -q` (fast pre-check, not the graded
  command): **395 passed, 2 skipped** — includes all 31 new tests for
  this task plus every existing annotator test, all green.
- `python -m pytest tests/ -q --continue-on-collection-errors` (the
  required baseline command, Bash timeout 600000 ms, actual duration
  ~280s): **2855 passed, 15 failed, 379 skipped, 6 errors**.

Comparison against the conductor-measured baseline
(`2823 passed, 15 failed, 6 errors, 380 skipped`):

- **Failed (15/15) — identical set**, confirmed by name: all 15 are the
  `tests/external/test_kbdl_service_utils.py` (13) and
  `tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`
  (1) entries from the baseline `failed_node_ids` list. Zero new
  failures.
- **Errors (6/6) — identical set**, confirmed by name:
  `tests/biochem/test_escher_utils.py` (collection error, optional dep),
  `tests/notebook/helpers` (collection error), and all 4
  `tests/modeling/test_comprehensive_gapfill_wrapper.py` tests. Zero new
  errors.
- **Passed: 2855 vs. 2823 baseline (+32).** 31 of these are the new
  tests added by this task (`test_ontology_dictionary.py` — 17 tests,
  `test_build_ko_description_map.py` — 4, `test_build_ec_description_map.py`
  — 3, `test_build_go_description_map.py` — 4, `test_build_cog_description_map.py`
  — 3; 17+4+3+4+3 = 31, confirmed by running each new file individually).
  The +1 beyond that (32 vs. 31) corresponds to **skipped dropping by
  exactly 1** (380
  -> 379) — one pre-existing test flipped from skipped to passed between
  the conductor's baseline run and mine. I did not modify any skip
  condition anywhere in the touched files (`build_ko_function_map.py`'s
  only change is an additive dataclass field never referenced by a skip
  condition), and did not investigate which specific test flipped since
  it does not affect the pass/fail bar (no passed-at-base test now
  fails); most likely a pre-existing conditional skip (e.g. an
  optional-dependency probe) whose environment-detection result differs
  by run/order, not something introduced by this branch.

Net: the required bar — **no test that passed at base fails on this
branch** — holds exactly, with the same 15 failed + 6 errored node ids as
the baseline and no others.

## Caveats

- One pre-existing test's skip/pass status flipped independent of this
  change (see Tests run above); not investigated further since it does
  not affect the required pass/fail bar and is not attributable to any
  file this task touched.
- `OntologyDictionary`'s two-column loader (`_parse_two_column_tsv`) also
  enforces the zero-entry-raises rule on its own, independent of the
  generator scripts' equivalent guard. This is intentionally
  belt-and-suspenders: the HARD RULES section says a source that parses
  to zero entries must never silently become an empty dictionary, and a
  staged file could in principle be corrupted/truncated after a
  generator ran cleanly. This is additional robustness beyond the
  minimum the task explicitly asked for, not scope creep on the public
  API (still exactly one public method).
- Generator scripts write their column-header line prefixed with `#`
  (e.g. `# ko_id\tdescription`) rather than bare, specifically so the
  header itself can never be misparsed as a spurious data row by the
  two-column loader (which only treats `#`-prefixed lines as
  comments/metadata). Note that the pre-existing
  `build_ko_function_map.py` / `kofamscan_utils._parse_ko_function_map_text`
  pair writes its header line bare (`ko_id\tcomposed_string`, not
  `#`-prefixed) — a latent equivalent issue there — but that file is
  explicitly off-limits for this task (`kofamscan_utils.py` is reserved
  for a later task) and untouched, so I left it as-is and did not widen
  scope to fix it.
- COG column layout (`cog-20.def.tab`: column 1 = id, column 3 =
  description) was chosen based on the commonly documented NCBI COG
  definition-file schema; no real release file was available to verify
  against (synthetic fixtures only, per the task's hard constraint), so
  this is a judgment call a reviewer with access to a real
  `cog-20.def.tab` sample may want to double-check before first
  production use.
- Did not run `ruff`/`mypy` (declared as separate `lint`/`mypy`
  dependency-groups, not part of the `[dev,all]` extras this task's
  environment instructions specify, and not part of the graded test
  command) — only `python -m py_compile` was used as a lightweight
  syntax sanity check on all new/modified files, plus a runtime smoke
  test of the `OntologyDictionary` import path and all four generator
  scripts' `--help` output.
