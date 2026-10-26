# Work record: t2-paperblast-local

## task_id
t2-paperblast-local

## prd
kbu-gene-literature-research-v1

## branch
`maestro/developer/base-kbutillib-main-b3dcb16-task-t2-paperblast-local`

## commit_shas
- `65b585c` — `feat(mmseqs): optional evalue, max_hits and subject coverage in search_proteins`
- `646ab1e` — `feat(literature): local PaperBLAST over the downloadable snapshot`
- (this work record is committed on top of those)

## base
`main` at `b3dcb16` — the commit named in the envelope, which is t1's Europe PMC
landing. `main` had not moved, so no rebase was needed.

## summary
Added **local PaperBLAST** to KBUtilLib: `PaperBLASTUtils`, which runs PaperBLAST
against the three files it publishes rather than scraping its web form (it has no
API). Three separately callable steps:

1. `ensure_snapshot()` — fetch/refresh `litsearch.db` (~3.1 GB), `uniq.faa`
   (~400 MB) and `stats` (47 B), resumable and validated.
2. `search()` — MMseqs2 the query sequences against `uniq.faa` through the
   existing `MMSeqsUtils`, caching hits per sequence.
3. `papers_for_hits()` — join those hits to papers in `litsearch.db`, opened
   read-only, after expanding `SeqToDuplicate`.

Supporting that required extending `MMSeqsUtils.search_proteins` with three
opt-in options, done so that the default call's command line and returned keys
are unchanged.

## files_touched

### Added — `src/kbutillib/domains/literature/paperblast.py` (1376 lines)

`PaperBLASTUtils(SharedEnvUtils)` at `:288`, following `europepmc.py` (the
in-package sibling t1 landed) for its shape: constructor-injectable
collaborators behind lazy properties, config read with
`get_config_value('section.key')`, and a `PaperBLASTUtilsImpl` composition
wrapper at `:1347` delegating via `__getattr__` (the `kb_uniprot_utils.py`
pattern the envelope named).

Injectable seams: `session` (HTTP), `cache` (`LiteratureCache`), `mmseqs`
(anything with `build_search_db`/`search_proteins`), plus `data_dir`.

**Snapshot** — `ensure_snapshot(data_dir=None, force=False, allow_dropbox=None)`
at `:449`:

- `resolve_data_dir` (`:397`) takes the explicit argument, else the
  constructor's, else config `paperblast.data_dir`, else
  `~/.kbutillib/paperblast` (joined to `Path.home()` at call time, not import
  time, so a relocated HOME is honoured). It `os.path.realpath()`s **before**
  the Dropbox check, so a symlink out of /tmp into Dropbox is caught; refusal is
  a `ValueError` naming the path and `allow_dropbox=True`.
- `_head` (`:568`) HEADs each file for `Content-Length`/`Last-Modified`.
  `_is_current` (`:586`) skips a file only when the manifest's size **and**
  `Last-Modified` equal the server's **and** the file's on-disk size equals the
  recorded size — so a truncated local file is re-fetched even though the
  manifest looks fine. A skipped file's recorded sha is carried forward rather
  than recomputed: an up-to-date 3.5 GB snapshot costs three HEADs and no
  hashing.
- `_download` (`:619`) stages to `<name>.part`, size-checks, validates, then
  `os.replace`s. The destination is only ever written by that rename, so a
  half-downloaded file cannot masquerade as a snapshot. A failed validation
  leaves the `.part` in place to resume from.
- `_stream_to_part` (`:658`) sends `Range: bytes=<n>-` when a `.part` exists and
  appends **only** on a 206. A server that ignores Range answers 200 with the
  whole body; that is detected from the status code and the download restarts
  from 0, because appending it would corrupt the file with a duplicated prefix.
  A `.part` that is already the published size skips the GET entirely.
- `_validate_download` (`:698`) dispatches on the file: SQLite gets
  `PRAGMA quick_check(1)`, FASTA gets its first header parsed, `stats` must
  parse to something containing `nSeq`. An HTML error page served in place of
  any of the three is rejected here rather than hours later mid-join.
- `snapshot.json` is written atomically: `{files: {name: {size, sha256,
  last_modified}}, stats: <parsed>, downloaded_at, data_dir}`.

**Search** — `search(queries, evalue=1e-3, max_hits=50, min_identity=0.30,
min_query_coverage=0.5, ...)` at `:858`:

- `ensure_mmseqs_db` (`:762`) builds the MMseqs2 database once, into
  `<data_dir>/mmseqs/<uniq.faa sha[:16]>/`. Binding the directory name to the
  sha is what makes a snapshot refresh build a new index instead of quietly
  searching a stale one.
- Calls `MMSeqsUtils.search_proteins(..., include_subject_coverage=True)` and
  reshapes each hit to exactly `subject_id, identity, query_coverage,
  subject_coverage, evalue, bitscore`, sorted by e-value then bitscore and
  truncated to `max_hits`.
- `_hits_cache_key` (`:825`) is `sha256(litsearch.db sha + uniq.faa sha +
  sha256(normalised sequence) + evalue + max_hits + min_identity +
  min_query_coverage + SEARCH_IMPL_VERSION)`, stored in the shared
  `LiteratureCache` table `paperblast_hits`. The key is **per sequence**, not
  per call, so a second call that adds one gene only searches that gene.
  Changing any threshold, either file sha, or `SEARCH_IMPL_VERSION` misses the
  cache — there is no cross-parameter reuse.

**Literature join** — `papers_for_hits(hits, ...)` at `:991`:

- Opens `litsearch.db` through a `file:...?mode=ro` URI (`pathname2url`-escaped,
  so a path with spaces works). The test asserts both that the URI carries
  `mode=ro` and that the resulting connection genuinely refuses a `DELETE`.
- `_check_schema` (`:1070`) reads the real schema with `PRAGMA table_info` for
  all seven tables in `REQUIRED_SCHEMA` (`:159`) and raises naming the table and
  the missing columns before any query runs. PaperBLAST's schema is upstream's
  to change; a clear error beats an `OperationalError` from inside a join.
- `_expand_duplicates` (`:1095`) closes the identical-sequence group over
  `SeqToDuplicate` in **both** directions (`sequence_id`→`duplicate_id` and
  back), bounded by `MAX_DUPLICATE_GROUP`. This is the step that matters:
  `uniq.faa` holds only representatives, so a paper attached to a folded-away
  duplicate is otherwise unreachable.
- `_links_for_hit` (`:1145`) joins `GenePaper`, `Snippet` and `GeneRIF` by
  `geneId`, and for a curated `db::protId` id (split by `split_db_id`, `:212`)
  also `CuratedGene`/`CuratedPaper` by `(db, protId)`. Links carry exactly
  `LINK_KEYS`, deduplicated on `(homolog_id, pmid, pmcid, doi, curated_db)` and
  sorted best-e-value first.

### Modified — `src/kbutillib/domains/genome/mmseqs_utils.py` (+66/-12)

`search_proteins` gained three keyword-only options (`:660`-`:662`):

- `evalue` → `mmseqs search -e` (`:826`), `max_hits` → `--max-seqs` (`:828`).
  Both are omitted from the command entirely when None.
- `include_subject_coverage=True` appends `tcov` to the `convertalis`
  `--format-output` columns (`:845`) and adds `subject_coverage` to each hit
  (`:963`). Default False returns exactly the six keys documented before.

`--format-output` deliberately remains the **last argument pair** of the
convertalis command, because the existing tests' subprocess fake locates the TSV
output at `cmd[-3]`. `_parse_search_tsv` (`:888`) raises `ValueError` rather than
guessing if subject coverage is requested and the `tcov` column is absent.

### Modified — `src/kbutillib/domains/literature/__init__.py` (+8)
Registered `PaperBLASTUtils`, `PaperBLASTUtilsImpl`, `parse_stats`,
`split_db_id` in `__all__` and the lazy `_MODULE_MAP`, so nothing is imported at
`import kbutillib` time.

### Added — `tests/literature/test_paperblast.py` (1301 lines, 60 tests)
Fully offline. `build_litsearch_db` builds a real but tiny `litsearch.db` in
tmp with all seven tables, **including columns the code never reads**
(`authors`, `isOpen`, `id2`, `comment`) so the schema check and queries run
against a realistic table. Fixture data is built around the two indirections
that are easy to get wrong:

- `repA` is in `uniq.faa`; `dupA` is **not**, and the only route to its paper
  (PMID 2002, plus its snippet) is `SeqToDuplicate`;
- `SwissProt::P12345` is curated, with its paper (PMID 4004) only in
  `CuratedPaper`.

Covered: duplicate expansion from both sides, curated links carrying
`curated_db`, the two coverages staying distinct from convertalis through to
each link, exact hit/link key sets, read-only opening, missing
column/table errors, snippet and GeneRIF links, link sort and dedupe, the
Dropbox refusal (including through a symlink, and on every entry point), the
snapshot's skip-when-unchanged / re-download-on-changed-Last-Modified /
re-download-when-truncated / force / Range resume / server-ignores-Range /
complete-`.part` / corrupt-SQLite / non-FASTA / bogus-stats / wrong-size paths,
per-sequence caching, no cross-parameter reuse, and snapshot-sha invalidation.

### Modified — `tests/external/test_mmseqs_utils.py` (+102, 5 tests)
`test_search_proteins_default_call_passes_no_evalue_or_max_seqs` pins that the
default command carries no `-e`/`--max-seqs` and the default
`--format-output` is exactly `query,target,pident,qcov,evalue,bits`; plus
tests for `-e`/`--max-seqs` plumbing, the tcov column and key, `tcov` parsed
into `subject_coverage` separately from `coverage`, and the missing-column
error.

## NO-INJECTION CHECK — it ran, and it passed

`TestRealMMseqsPath` injects **nothing**: no fake mmseqs, no faked search
output. `PaperBLASTUtils` builds its own `MMSeqsUtils`, which shells out to the
real binary (`createdb`/`createindex`/`search`/`convertalis`) over the fixture
`uniq.faa`, and the real hits feed the real join. Skipped only when
`shutil.which("mmseqs")` finds nothing or `mmseqs version` fails.

On this host (poplar, `mmseqs` = `/home/chenry/.local/bin/mmseqs`, version
`6f45232ac8daca14e354ae320a4359056ec524c2`) it **ran — not skipped — and both
tests passed**:

```
tests/literature/test_paperblast.py::TestRealMMseqsPath::test_identical_query_finds_its_homolog_and_its_papers PASSED
tests/literature/test_paperblast.py::TestRealMMseqsPath::test_the_built_database_is_reused_on_a_second_search PASSED
```

It asserts the identical query lands on `repA` with identity and query coverage
≥ 0.9, that `subject_coverage` really came back from `convertalis` as a fraction
in (0, 1], that the thresholds filtered the unrelated sequence out, and that
`papers_for_hits` then returns both PMID 1001 (direct) and PMID 2002 (through
`SeqToDuplicate`). This is the test that would catch a wrong convertalis column
order or a misread `tcov`; a faked search output cannot.

## verification

Baseline: `build-baselines/kbutillib.json` and
`build-baselines/kbutillib-h100-main-6310d61.json` both exist, so per the
developer protocol the full suite was **not** re-run — and the h100 baseline
records why in its own `WHY_DURATION_MATTERS` field: the full suite takes
**56m56s**, "5.7x the 600 s Bash-tool ceiling", and explicitly instructs that a
sub-agent be given a targeted selection instead. Neither baseline lists any
failing nodeid under `tests/external/test_mmseqs_utils.py` or
`tests/literature/`, so the areas this task touches had a clean base.

Targeted selection, with the baseline's own interpreter
(`/home/chenry/venvs/kbdl/bin/python`, cpython 3.11.15, pytest 9.1.1):

| command | result | exit |
|---|---|---|
| `pytest tests/literature tests/external/test_mmseqs_utils.py -q` | **191 passed, 2 skipped** | **0** |
| `pytest tests/guard tests/koros_arc_store/test_import_boundary.py tests/models_and_analyses/test_layering.py tests/test_python_floor_syntax.py tests/domains/test_genome_smoke.py tests/domains/test_agents_smoke.py tests/core/test_composition_smoke.py -q` | 244 passed, 6 skipped, 1 xfailed, **8 errors** (all pre-existing — see below) | 1 |
| `pytest ...test_search_proteins_success_returns_exact_keys_and_fraction_identity ...TestRealMMseqsPath -v` | **3 passed** | **0** |

The 2 skips are t1's `test_europepmc_live.py`, gated on `KBU_LIVE_TESTS=1`.

The architectural guards were run deliberately, because `paperblast.py` imports
across domains (`from ..genome.mmseqs_utils import MMSeqsUtils`):
`tests/guard/test_dependency_direction.py`,
`tests/koros_arc_store/test_import_boundary.py` and
`tests/models_and_analyses/test_layering.py` all pass with that import in place.
`tests/test_python_floor_syntax.py` (the 3.9 f-string-backslash guard) also
passes.

**The 8 errors are pre-existing and not a regression.** They are
`tests/core/test_composition_smoke.py` fixture errors from a missing
`ModelSEEDDatabase` checkout, which this worktree's sibling path does not have.
Proven rather than assumed: a detached worktree was cut at the base commit
`b3dcb16` and the same file run there produced **the same 8 errors** (`12
passed, 4 skipped, 1 xfailed, 8 errors`), with no part of this task's diff
present. The worktree was then removed.

`ruff check` (0.17.1, installed into this task's own `.task-venv`) is clean on
all four changed/added source and test files except one **pre-existing**
`F401 subprocess imported but unused` at `tests/external/test_mmseqs_utils.py:3`,
which is untouched by this diff and was left alone rather than folded into an
unrelated cleanup.

## reconciliations (prompt vs. tree)

The prompt was written against `wip` before t1 landed; these are the points
where it and the tree disagreed, and what was done.

1. **"New code goes in a NEW package `src/kbutillib/domains/literature/`
   (create `__init__.py`)"** — the package already exists; t1 landed it with a
   lazy-loading `__init__.py`. Added `paperblast` to that loader instead of
   creating anything.
2. **"if `cache.py` does not exist, create a small SQLite helper
   `LiteratureCache`"** — it exists (`cache.py`, t1). Reused it, as instructed;
   no second cache was created.
3. **"Tests go under `tests/literature/` (create it)"** — already exists, with
   t1's `test_europepmc.py`, `test_europepmc_live.py` and `fixtures/`. Added to
   it.
4. **Class name**: the task body says `MMseqsUtils`, the addendum says
   `MMSeqsUtils`. The tree has **`MMSeqsUtils`** (`mmseqs_utils.py:20`) — the
   addendum is right and was followed.
5. **Cited line numbers**: both of the addendum's citations checked out against
   the base commit — `search_proteins` at `:651` and the convertalis
   `--format-output` at `:823`. Symbols were still located by name rather than
   by line.
6. **Structural template**: the envelope names
   `domains/external/kb_uniprot_utils.py` as the template *and* says classes
   inherit `SharedEnvUtils` — but `KBUniProtUtils` actually inherits
   `BaseUtils`. Took the `XxxImpl`/`__getattr__` wrapper shape from it (which is
   what the prompt pointed at it for) and `SharedEnvUtils` inheritance from the
   prompt, which is also what the in-package sibling `EuropePMCClient` does.
7. **`build_search_db` takes a list of protein dicts, not a FASTA path.** Read
   its signature as instructed; there is no path-based entry point. So
   `ensure_mmseqs_db` stream-parses `uniq.faa` and materialises the records to
   hand over — ~842k sequences held in memory for the duration of a once-per-
   snapshot build. Documented at the method; flagged here because it is a real
   memory cost that a FASTA-path overload would avoid, and that overload is the
   obvious follow-up if it ever bites.
8. **"validate (sqlite `PRAGMA integrity_check` quick ...)"** — implemented as
   `PRAGMA quick_check(1)`, which is SQLite's documented reduced form of
   `integrity_check`. Read as the prompt's "quick" qualifier: a full
   `integrity_check` reads every index of a 3.1 GB file, which is minutes of
   I/O for a post-download sanity check that `quick_check` already satisfies
   (it catches truncation and page corruption, which are the actual failure
   modes here).
9. **`pydantic` models with `Optional[...]`** — the PRD-wide instruction. This
   task's own spec defines hits and links as dicts with named keys, and t1's
   `europepmc.py` returns plain dicts, so plain dicts were kept for consistency
   with the sibling, with `HIT_KEYS`/`LINK_KEYS` tuples as the contract and
   tests pinning the exact key sets. No pydantic model is introduced. The 3.9
   constraint behind that instruction is respected regardless: `typing.Optional`
   throughout, no PEP 604 unions, `from __future__ import annotations`, and the
   repo's own 3.9 f-string guard passes.

## decisions worth knowing

- **`search()` never downloads.** It reads `snapshot.json` and raises a
  `RuntimeError` naming `ensure_snapshot()` if there is none, rather than
  implicitly pulling 3.5 GB. This also keeps `search` offline-testable.
- **`homolog_id` is the `litsearch.db` id that carries the link**, which after
  `SeqToDuplicate` expansion may differ from the hit's `subject_id` (the two
  hold identical sequences). That is what lets a test prove expansion happened,
  and the alignment numbers on the link are still the hit's. Documented on
  `papers_for_hits`.
- **`GeneRIF` rows become their own links** (pmid + the comment as `snippet`),
  not just snippet decoration, since a GeneRIF is a paper reference for the
  gene. A paper appearing in both `GenePaper` and `GeneRIF` for the same
  homolog therefore yields two links, distinguishable by the richer metadata on
  the `GenePaper` one.
- **Paper ids are returned as PaperBLAST stores them** (bare `"12345"`,
  `"PMC12345"`), not canonicalised. The docstring points at
  `europepmc.canonical_paper_id`/`merge_ids` for merging with Europe PMC
  results, which is t3's concern rather than this module's.

## not verified
- No live download was attempted, so the real `papers.genomics.lbl.gov` HEAD
  response headers are inferred from the spec, not observed: the skip logic
  needs `Content-Length` and `Last-Modified`, and the resume path needs the
  server to honour `Range` with a 206. If that server omits `Last-Modified`,
  `_is_current` returns False and the file re-downloads — conservative, not
  wrong, but it would mean no skipping. If it ignores `Range`, the restart path
  (tested) handles it.
- The real 3.1 GB `litsearch.db` schema was not inspected; `REQUIRED_SCHEMA`
  comes from the task's reading of `bin/litsearch.sql`. That is exactly why the
  columns are checked at runtime with `PRAGMA table_info` and the error names
  what is missing.
- Performance at real scale (an 842k-sequence MMseqs2 index build, and joins
  against a 3 GB SQLite) is untested; only the tiny fixture was exercised.
