# Work Record: kbase-genome-expert-and-utils

## task_id
`kbase-genome-expert-and-utils`

## branch
`kbase-genome-expert-and-utils`

## commit_shas
_(populated after commit)_

## summary

Added five new methods to `KBGenomeUtils` (legacy class) in `src/kbutillib/kb_genome_utils.py`: `save_genome_object` (direct Workspace transport returning `ws_id/obj_id/version`), `save_assembly_from_fasta` (raises `RuntimeError` on the legacy class to direct callers to the facade), `save_genome_with_assembly` (orchestrator: saves assembly then genome), `validate_genome` (schema-only validation with configurable `require_assembly_ref` kwarg), and `build_genome_from_fasta_gff` (constructs a full Genome dict from FASTA + optional GFF3). Also added a private `_parse_gff` helper implementing the full GFF3→KBase feature mapping. Updated `KBGenomeUtilsImpl.__init__` to accept `(env, ws, jobs, **kwargs)` and store `self._jobs`; added an explicit `save_assembly_from_fasta` override on the Impl class that uses `self._jobs.run_job` and polls to terminal state. Updated `toolkit.py` `genome` property to construct `KBGenomeUtilsImpl(self.env, self.ws, self.jobs)`. Also added missing imports (`hashlib`, `logging`, `time`, `Counter`, `defaultdict`, `datetime`, `Path`) that the existing methods in the file were already using but not importing. Created `tests/test_kb_genome_utils_save.py` with 24 unit tests and 2 integration stubs. Created `agent-io/skills/kbase-genome-expert.md` with YAML frontmatter and 7 body sections.

## files_touched

- `src/kbutillib/kb_genome_utils.py` — added missing imports; added 5 new methods on `KBGenomeUtils` + private `_parse_gff`; updated `KBGenomeUtilsImpl.__init__` to accept `jobs`; added explicit `save_assembly_from_fasta` on `KBGenomeUtilsImpl`
- `src/kbutillib/toolkit.py` — updated `genome` property to pass `self.jobs` to `KBGenomeUtilsImpl`
- `tests/test_kb_genome_utils_save.py` — new test file, 24 unit tests + 2 integration stubs
- `agent-io/skills/kbase-genome-expert.md` — new skill file
- `agent-io/work-records/kbase-genome-expert-and-utils.md` — this file

## success_criteria_check

| Criterion | Status | Justification |
|---|---|---|
| 5 new methods on `KBGenomeUtils` | PASS | `save_genome_object`, `save_assembly_from_fasta`, `save_genome_with_assembly`, `validate_genome`, `build_genome_from_fasta_gff` all present at lines ~784–1080 |
| `KBGenomeUtilsImpl.__init__` signature is `(self, env, ws, jobs, **kwargs)` | PASS | Updated at line ~1175 |
| `toolkit.py` `genome` property passes `jobs` | PASS | Now constructs `KBGenomeUtilsImpl(self.env, self.ws, self.jobs)` |
| `pytest tests/test_kb_genome_utils_save.py -q` zero failures | PASS | 24 passed, 2 deselected (integration) |
| `kbase-genome-expert.md` exists with frontmatter and 7 body sections | PASS | Sections 1–7 present |
| `kbu.genome.save_genome_object(...)` returns `'ws_id/obj_id/version'` | PASS | `test_returns_ref_format` confirms format `99/42/3` |
| `kbu.genome.validate_genome(genome_dict, require_assembly_ref=False)` accepts missing assembly_ref | PASS | `test_require_assembly_ref_false_accepts_missing` and `_accepts_empty_string` both pass |
| `kbu.genome.build_genome_from_fasta_gff(...)` produces dict passing `validate_genome(..., require_assembly_ref=False)` | PASS | `test_passes_validate_genome` and `test_passes_validate_genome_with_gff` both pass |
| `save_assembly_from_fasta` on bare `KBGenomeUtils()` raises `RuntimeError` mentioning facade | PASS | `test_bare_legacy_class_raises_runtime_error` passes; message contains "KBUtilLib" |
| Every API name in `kbase-genome-expert.md` resolves via grep to real symbol | PASS | 23/23 API names found (see grep verification below) |

## tests_run

```
pytest tests/test_kb_genome_utils_save.py -q -m "not integration"
Result: 24 passed, 2 deselected in 0.98s

pytest tests/test_composition_smoke.py -q -m "not kbase and not integration"
Result: 9 passed, 14 skipped, 1 deselected in 1.33s (no regressions)
```

Integration tests (`TestIntegration`) are marked `@pytest.mark.integration` and skipped by default. They require `KBASE_LIVE_TESTS=1` and a real KBase workspace name in `KBASE_TEST_WORKSPACE`.

## grep verification (API names in skill file)

All 23 API names referenced in `kbase-genome-expert.md` resolved:

```
OK: build_genome_from_fasta_gff
OK: validate_genome
OK: save_genome_with_assembly
OK: save_assembly_from_fasta
OK: save_genome_object
OK: get_object
OK: load_kbase_gene_container
OK: object_to_features
OK: get_ftr
OK: ftr_to_aliases
OK: alias_to_ftrs
OK: object_to_proteins
OK: reverse_complement
OK: translate_sequence
OK: calculate_gc_content
OK: aggregate_taxonomies
OK: create_synthetic_genome
OK: load_genome_from_local_files
OK: add_annotations_to_object
OK: set_callback_client
OK: gfu_client
OK: afu_client
OK: save_ws_object
```

## AssemblyUtil KIDL spec check

No local `AssemblyUtil.spec` KIDL file was found in the repository or Dropbox projects. The implementation uses the `assembly_ref` key confirmed by PRD § Implementation Decisions → Authoritative result key (from 2026-06-02 confront stall #3). The code has a defensive check: if `assembly_ref` is absent from the job result dict, a `RuntimeError` is raised with the raw result attached so the caller can diagnose. If the key has changed in a later AssemblyUtil release, the error message will contain the actual result dict for debugging.

## caveats

1. **Missing imports added as side effect:** The existing methods `load_genome_from_local_files`, `aggregate_taxonomies`, and `create_synthetic_genome` were already using `hashlib`, `Path`, `Counter`, `defaultdict`, and `datetime` without importing them. These would have caused `NameError` at runtime. The imports were added as a necessary fix — the new methods also need them.

2. **`get_features_by_type`, `get_features_by_function`, `translate_features` not referenced in skill.** The PRD skill layout lists these as "pointers to existing helpers (get_features_by_type, get_features_by_function, translate_features)" but these methods do not exist in source. The skill section was written to reference only the real helpers that were verified via grep. This is conservative and correct; fabricating method names in a skill file was the exact bug the PRD was fixing.

3. **`save_genome_with_assembly` on legacy class still routes through `save_assembly_from_fasta`.** The legacy class's `save_genome_with_assembly` calls `self.save_assembly_from_fasta(...)` which raises `RuntimeError`. So `save_genome_with_assembly` on a bare `KBGenomeUtils` also raises. This is correct behavior per the design (the method should only be called via the facade), but it means the legacy class's `save_genome_with_assembly` is effectively non-functional — it exists to make the method available via the facade's `__getattr__` delegation only if the Impl doesn't override it. In practice, the Impl inherits it through `__getattr__`, and when the Impl calls `self.save_assembly_from_fasta(...)`, it calls the Impl's own override (not the legacy raise). This is correct Python MRO behavior.

4. **AssemblyUtil KIDL spec not found locally** — see AssemblyUtil KIDL spec check above. Using PRD-specified `assembly_ref` key with defensive error on mismatch.
