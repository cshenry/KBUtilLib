# KBUtilLib Skill Set: Expert Refresh + Genome Skill

## Problem Statement

Two related gaps in the KBUtilLib skill set, both surfaced during a 2026-06-02 design session:

**Gap A — `kbutillib-expert` has factual bugs and missing coverage.** The skill was last updated 2026-05-25 for the composition refactor. A fresh audit found:

- API names that don't exist (e.g., `kbu.env.get_config_value(...)` — the actual method is `get_config(section, key=None)`).
- Inconsistent and partly-wrong `KBJobUtils` examples. The skill references `submit`, `refresh`, `cancel` but the module exposes `run_job`, `refresh_active`, `cancel_job`.
- Multiple `kb_genome_utils.py` helpers undocumented (annotations, BV-BRC local-file ingest, taxonomy aggregation, synthetic-genome creation, feature lookup cache).
- No mention of `kbu.callback.set_callback_client()` injection (the only sane notebook escape hatch for callback-dependent SDK clients).
- No flag that the repo's `installed_clients/` does NOT ship `AssemblyUtilClient` / `GenomeFileUtilClient`; they require an external KBase SDK install. A user calling `kbu.callback.gfu_client()` without that install gets `ImportError`.
- No coverage of the `kbu` CLI surface (`kbu jobs`, `kbu jobdaemon`) at a top-level chapter.

**Gap B — No skill or supporting API for saving a Genome from a notebook.** Notebook authors on primary-laptop / h100 typically don't run inside an SDK callback context. The existing `kbu.callback.save_genome_or_metagenome(...)` requires a callback URL. There's no high-level path that says "given a FASTA and a Genome dict, give me an assembly_ref and a genome_ref in this workspace."

## Solution

Two deliverables, both saved under `KBUtilLib/agent-io/skills/`:

1. **Refresh `kbutillib-expert`** in place — apply all 8 approved gap-fixes from the audit. The skill remains a reference-oriented "expert" with three bundled context files (module-reference, api-summary, patterns); structure and tone unchanged.

2. **Create `kbase-genome-expert`** — a new task-oriented skill focused on KBase Genome objects: how to load, validate, manipulate, and save them. Targets notebook authors. Includes supporting source-code extensions to `kb_genome_utils.py` so the documented API actually exists.

The new skill's notebook-only execution model means save flows split:
- **Genome typed object** → direct Workspace `save_objects` (KBaseGenomes.Genome). No callback needed; the existing `kbu.ws.save_ws_object(...)` already implements this transport.
- **Assembly** → EE2 job submission of `AssemblyUtil.save_assembly_from_fasta` via `kbu.jobs.run_job(...)`. The SDK callback server, running on KBase's side, handles the FASTA upload to shock and the handle plumbing. We wait for the job to terminal and return the assembly_ref.

## User Stories

1. As a notebook author on primary-laptop, I want to call `kbu.genome.save_genome_with_assembly(fasta_path, genome_dict, workspace, base_name)` and get back `(assembly_ref, genome_ref)` so I don't have to know about shock, handle refs, AssemblyUtil, GenomeFileUtil, or callback URLs.
2. As a notebook author, I want to call `kbu.genome.save_assembly_from_fasta(fasta_path, workspace, name)` standalone and get an assembly_ref so I can splice it into a genome I build myself.
3. As a notebook author, I want to call `kbu.genome.save_genome_object(genome_dict, workspace, name)` standalone (when I already have the assembly_ref) so I don't pay for an EE2 job round-trip.
4. As a notebook author, I want to call `kbu.genome.validate_genome(genome_dict)` before save and see a list of validation errors (empty list = ready to save), so failures happen locally with a useful error instead of as opaque server-side `save_one_genome` rejections.
5. As a notebook author, I want to call `kbu.genome.build_genome_from_fasta_gff(fasta_path, gff_path=None, scientific_name=..., taxonomy=..., genetic_code=11)` and get a Genome dict skeleton populated with contigs/lengths/MD5/GC/features, so building a Genome from scratch isn't pages of plumbing.
6. As a developer reading the skill, I want every public API named in the skill to actually exist in the source, so I can copy-paste examples and they run.
7. As a developer trying to access config, I want the skill to show me the correct `kbu.env.get_config(section, key)` call, not the non-existent `get_config_value`.
8. As a developer submitting an EE2 job, I want the skill to show me the actual `kbu.jobs.run_job(method=..., params=[...])` shape, not a fabricated `submit({"app_id":...})` shape.
9. As a developer needing GenomeFileUtil from a non-SDK context, I want the skill to tell me that `kbu.callback.set_callback_client("GenomeFileUtil", my_client)` is how to inject a pre-built client.
10. As a developer trying to install KBUtilLib, I want the skill to flag that `installed_clients/` doesn't ship `AssemblyUtilClient`/`GenomeFileUtilClient` so I know to install KBase SDK separately.
11. As a CLI user, I want a top-level CLI chapter in api-summary covering `kbu jobs ...`, `kbu jobdaemon`, and the `util.py` template's machine_configs sys_paths handling.
12. As a future skill author working on KBase Genome workflows, I want the `kbase-genome-expert` skill cross-referenced from `kbutillib-expert`'s patterns chapter so users discover it.

## Implementation Decisions

### Deep modules and the new public API surface

The deep module is `kb_genome_utils.py`. Public interface grows by **5 methods** on both `KBGenomeUtils` (legacy class) and `KBGenomeUtilsImpl` (composition wrapper). Implementation hides Workspace/EE2/AssemblyUtil/GenomeFileUtil plumbing.

#### `save_genome_object(genome_dict, workspace, name) -> str`

Direct Workspace transport:

```python
# Prototype-derived decision encoding (do NOT copy literally — confirm via source):
def save_genome_object(self, genome_dict, workspace, name):
    info = self.ws.save_ws_object(name, workspace, genome_dict, "KBaseGenomes.Genome")
    # info shape: [obj_id, name, type, save_date, version, saved_by, ws_id, ws_name, chsum, size, meta]
    return f"{info[6]}/{info[0]}/{info[4]}"  # ws_id/obj_id/version
```

Returns the full `ws_id/obj_id/version` ref. Raises whatever `save_ws_object` raises on type-validation failure (server-side).

#### `save_assembly_from_fasta(fasta_path, workspace, name, *, wait=True, timeout=600) -> str`

EE2 transport:

```python
def save_assembly_from_fasta(self, fasta_path, workspace, name, *, wait=True, timeout=600):
    record = self.jobs.run_job(
        method="AssemblyUtil.save_assembly_from_fasta",
        params=[{
            "file": {"path": str(fasta_path)},
            "workspace_name": workspace,
            "assembly_name": name,
        }],
    )
    if not wait:
        return record.job_id
    # Poll to terminal state; raise on failure/timeout; return assembly_ref from job result.
    final = self._wait_for_job_terminal(record.job_id, timeout=timeout)
    return final.result["assembly_ref"]  # exact key TBD by AssemblyUtil contract
```

`_wait_for_job_terminal` is a small private helper that polls `self.jobs.refresh(job_id)` until `JobState.is_terminal`, with a timeout.

**Authoritative result key (from 2026-06-02 confront stall #3):** `AssemblyUtil.save_assembly_from_fasta` returns `{"assembly_ref": "<ws_id/obj_id/version>"}`. Implementation must read this key. Before implementing, the developer MUST verify against the AssemblyUtil KIDL spec (`AssemblyUtil/AssemblyUtil.spec` in the public KBase AssemblyUtil repo, or via the catalog client) — if the spec has changed since 2026-06-02 (e.g., the key is now `upa` or `ref`), raise to the human before proceeding rather than silently picking a different key.

#### `save_genome_with_assembly(fasta_path, genome_dict, workspace, base_name, *, assembly_suffix="_assembly") -> tuple[str, str]`

Orchestrator. Save assembly, splice the returned `assembly_ref` into `genome_dict["assembly_ref"]`, save genome, return both refs:

```python
def save_genome_with_assembly(self, fasta_path, genome_dict, workspace, base_name, *, assembly_suffix="_assembly"):
    assembly_ref = self.save_assembly_from_fasta(
        fasta_path, workspace, base_name + assembly_suffix
    )
    genome_dict = dict(genome_dict)  # shallow-copy; don't mutate caller's input
    genome_dict["assembly_ref"] = assembly_ref
    genome_ref = self.save_genome_object(genome_dict, workspace, base_name)
    return assembly_ref, genome_ref
```

#### `validate_genome(genome_dict, *, require_assembly_ref=True) -> list[str]`

Schema-only validation against `KBaseGenomes.Genome` required fields. Returns a list of human-readable error strings; empty list means valid. Does NOT mutate input. Does NOT do content checks (no codon-table verification, no MD5 recomputation, no protein/DNA agreement check) — those would belong to a separate `validate_genome_content` if ever needed.

**`require_assembly_ref` kwarg (added per 2026-06-02 confront stall #9):** the build-then-save flow constructs a Genome dict before the assembly exists; calling `validate_genome` on that intermediate dict with `require_assembly_ref=True` would always fail. Callers in pre-assembly contexts (e.g., `build_genome_from_fasta_gff` immediately after building) MUST call with `require_assembly_ref=False`; pre-save callers (the default) leave it True.

Required top-level fields checked: `id`, `scientific_name`, `domain`, `genetic_code` (int), `dna_size` (int>0), `num_contigs` (int>0), `contig_ids` (list[str]), `contig_lengths` (list[int], same length as contig_ids), `gc_content` (float 0..1), `md5` (str), `molecule_type`, `source`, `source_id`, `assembly_ref` (non-empty str **iff `require_assembly_ref=True`**), `features` (list), `cdss` (list), `mrnas` (list), `non_coding_features` (list), `feature_counts` (dict), `taxonomy`.

Per-feature checks: `id` (str, unique across all features+cdss+mrnas+non_coding_features), `type` (str), `location` (list of `[contig_id, start_int, strand_str, length_int]` tuples — contig_id must appear in genome's `contig_ids`).

#### `build_genome_from_fasta_gff(fasta_path, gff_path=None, *, scientific_name, taxonomy, genetic_code=11, source="User", source_id=None) -> dict`

Constructor. Reuses existing `_parse_fasta` (already in kb_genome_utils.py:526). Adds a new private `_parse_gff(gff_path) -> list[feature_dict]` helper that emits KBase-shaped features from a GFF3 file. When `gff_path` is None, returns a Genome dict with empty features. Computes contig metadata (ids, lengths, total dna_size), GC content, MD5, builds `cdss` via existing `_create_cds_features`, derives `domain` from taxonomy first segment.

Behaviorally similar to the existing `load_genome_from_local_files` but cleaner inputs (one FASTA path + one optional GFF path, rather than a BV-BRC-shaped multi-file layout).

**GFF3→KBase feature mapping (added per 2026-06-02 confront stall #8):**

For each GFF3 record where `type` is in `{"CDS", "gene", "tRNA", "rRNA", "ncRNA", "mRNA"}`:

- **Feature ID:** prefer attribute `ID`, fall back to `locus_tag`, fall back to `Name`, fall back to synthesized `{seqid}_{1-based-counter-for-type}`. IDs must be unique across the returned feature set; on collision, suffix with `_2`, `_3`, etc.
- **Type mapping:** GFF `gene` → KBase `feature.type = "gene"`. GFF `CDS` → emit as a separate entry in `cdss` list (with `parent_gene` set to the parent gene's id when GFF `Parent` attribute is present). GFF `tRNA`/`rRNA`/`ncRNA` → KBase `non_coding_features` with `type` matching the GFF type. GFF `mRNA` → KBase `mrnas` list with `parent_gene` from `Parent`.
- **Location tuple:** `[[seqid, start, strand, length]]` where `seqid` is the GFF column 1 (must appear in `contig_ids`; if not, log a warning and skip the feature), `start` is **1-based inclusive** (GFF's natural convention), `strand` is `"+"` or `"-"` from GFF column 7, `length = abs(end - start) + 1`. Multi-segment features (joined CDSs) emit one tuple per segment, ordered as they appear in the GFF.
- **Functions:** if GFF attribute `product` is present, use as the sole entry in `feature["functions"]` list. Otherwise empty list.
- **Aliases:** GFF attribute `Dbxref` → split on comma, each entry parsed as `dbname:value` → appended to `feature["aliases"]` as `[dbname, value]`. `locus_tag` (if present and different from ID) → appended as `["locus_tag", value]`.
- **DNA sequence:** populate `feature["dna_sequence"]` by slicing the matching contig from the FASTA (using start/length/strand; reverse-complement when strand is `"-"`). Populate `dna_sequence_length` accordingly.
- **Protein translation:** for CDS features only, translate `dna_sequence` using the genome's `genetic_code` (call existing `translate_sequence`); store as `protein_translation` and `protein_translation_length`. Trim trailing stop codon (`*`).
- **MD5 fields:** `feature["md5"]` = MD5 of `dna_sequence`. For CDS: `protein_md5` = MD5 of `protein_translation`.
- **CDS linking on parent gene:** when a CDS has `Parent` pointing to a gene, append the CDS's id to the parent gene's `feature["cdss"]` list.

`feature_counts` is computed at the end as `{type: count}` across `features + cdss + mrnas + non_coding_features`.

### Composition wiring update

`KBGenomeUtilsImpl` currently composes `env, ws`. To call EE2 from `save_assembly_from_fasta`, it needs `jobs`. Update `toolkit.py` so the genome facade property constructs `KBGenomeUtilsImpl(env, ws, jobs)`. Update the module-reference and the composition graph diagram. The `jobs` dependency is lazy on the facade — first access still triggers the chain.

The `KBGenomeUtilsImpl.__init__` signature becomes `(env, ws, jobs, **kwargs)`. The legacy `KBGenomeUtils(KBWSUtils)` class does NOT get a `jobs` dependency (it's a multi-inheritance leftover; jobs access goes through the facade only). If a legacy-class caller calls `save_assembly_from_fasta` on the bare `KBGenomeUtils` instance, raise `RuntimeError("save_assembly_from_fasta requires the composition-facade path; use kbu = KBUtilLib(); kbu.genome.save_assembly_from_fasta(...)")`.

### Workspace parameter convention

Every new save/load/build method takes `workspace` as a **required positional/keyword parameter**. No default from session or config. Workspace can be either a workspace ID (int) or a workspace name (str); pass-through to `kbu.ws.save_ws_object` which handles both via the existing `set_ws` logic.

### Skill file layout — `kbase-genome-expert`

Single skill markdown at `KBUtilLib/agent-io/skills/kbase-genome-expert.md`. No bundled context files in this initial round (keep simple). If the skill grows past ~400 lines, future work splits out a `context/` bundle. Frontmatter shape mirrors the other expert skills (`name`, `description`, `scope: domain`).

Skill body sections:
1. **What this skill covers** — KBase Genome objects in notebook contexts.
2. **Quick reference: the save flow** — `save_genome_with_assembly` end-to-end example.
3. **Loading genomes** — `kbu.genome.get_genome` from workspace + `build_genome_from_fasta_gff` from local files.
4. **Validating** — `validate_genome` usage; what it does and doesn't check.
5. **Common manipulations** — pointers to existing helpers (`get_features_by_type`, `get_features_by_function`, `translate_features`, `add_annotations_to_object`, `aggregate_taxonomies`, `create_synthetic_genome`, feature lookup cache via `_check_for_object`/`get_ftr`/`ftr_to_aliases`/`alias_to_ftrs`).
6. **Notebook-vs-SDK callback note** — what works without a callback URL (direct WS, EE2 jobs) and what doesn't (`kbu.callback.gfu_client()`, `kbu.callback.afu_client()`); link to `kbu.callback.set_callback_client()` for the injection escape hatch.
7. **Related skills** — `kbutillib-expert`, `kb-sdk-dev`, `modelseedpy-expert`.

### `kbutillib-expert` refresh — exact gap fixes (all 8 approved 2026-06-02)

1. **Standardize config access on `get_config_value` (CORRECTED FROM ORIGINAL AUDIT — direction was reversed).** `shared_env_utils.py:198` explicitly marks `get_config(section, key, default=None)` as deprecated in favor of `get_config_value(key_path, default=None)` (`shared_env_utils.py:216`), which takes a dot-notation key path. The 2026-06-02 confront caught this inversion (stall #4). The correct fix is: ensure every example in `kbutillib-expert.md`, `api-summary.md`, and `patterns.md` uses `kbu.env.get_config_value("section.key")` with dot notation. Where `kbu.env.get_config("section", "key")` appears, replace it. Add a one-liner noting `get_config(section, key)` exists only for INI compatibility and is deprecated. Verify the exact `get_config_value` signature in source before editing.

2. **Reconcile `KBJobUtils` examples.** Both main skill and api-summary reference `kbu.jobs.submit(...)`, `kbu.jobs.refresh(...)`, `kbu.jobs.cancel(...)`, etc. The actual public API per `src/kbutillib/kb_job_utils/utils.py` is:
   - `run_job(method, params, *, app_id=None, workspace_id=None, service_ver=None, meta=None) -> JobRecord`
   - `cancel_job(job_id) -> JobRecord`
   - `submit_chain(steps, ...)` for linear pipelines
   - `cleanup(...)`, `start_watcher(interval=...)`
   
   Refresh-related methods (`refresh`, `refresh_active`, `refresh_all`), read methods (`get`, `list`, `summary`), and management methods (`forget`, `get_logs`) — verify each exists in `utils.py` and `__init__.py` for `KBJobUtils` before keeping or removing the documentation. Replace every `submit({...dict...})` example with `run_job(method=..., params=[...])`. Replace `cancel(...)` with `cancel_job(...)`.

3. **Document undocumented `kb_genome_utils.py` methods.** Add to `module-reference.md` under the `KBGenomeUtilsImpl` section, and add appropriate examples to `api-summary.md` under Genome Operations:
   - `load_kbase_gene_container(id_or_ref_or_filename, ws=None, localname=None)` — cache load
   - `object_to_features(name)` — cached feature list
   - `get_ftr(name, ftrid)` / `ftr_to_aliases(name, ftrid)` / `alias_to_ftrs(name, alias)` — lookup helpers
   - `object_to_proteins(ref)` — protein extraction
   - `add_annotations_to_object(reference, suffix, annotations)` — annotation update via the SDK annotation client (NOTE: requires callback context)
   - `load_genome_from_local_files(genome_id, features_dir, genomes_dir, metadata_dir, ...)` — BV-BRC local-file ingest
   - `aggregate_taxonomies(genomes, asv_id, output_dir=None)` — taxonomy consensus
   - `create_synthetic_genome(asv_id, genomes, ...)` — synthetic-merge genome
   - `calculate_gc_content(sequence)` — pure-string helper
   - And the new methods added in this PRD: `save_genome_object`, `save_assembly_from_fasta`, `save_genome_with_assembly`, `validate_genome`, `build_genome_from_fasta_gff`

4. **Document `kbu.callback.set_callback_client(name, client)`.** Add a short subsection to `patterns.md` (right after the configuration pattern) titled "Injecting SDK clients in notebook contexts" explaining that callbacks like `gfu_client()`/`afu_client()` normally require an SDK callback URL, and that `set_callback_client("GenomeFileUtil", my_client)` is the injection hook to plug in a pre-built client.

5. **Flag `installed_clients/` shipping constraint.** Add a one-paragraph note (in the main skill body, near the top, ideally in a "Repository Layout" or "Installation" sentence) stating: the repo ships only `Workspace`, `EE2`, `AbstractHandle`, `baseclient`, and `authclient` under `installed_clients/`. `AssemblyUtilClient` and `GenomeFileUtilClient` are imported lazily inside `kb_callback_utils.py` but expected from a separate KBase SDK install. A user without that install will get `ImportError` on `kbu.callback.gfu_client()` / `afu_client()`.

6. **Cross-reference `kbase-genome-expert`.** In `kbutillib-expert.md` Related Skills (around L186), add `/kbase-genome-expert - For saving + loading + validating KBase Genome objects from notebooks`. In `patterns.md`, add a short "See also" pointer at the end of any genome-related pattern.

7. **`KBGenomeUtilsImpl` delegate note.** In `module-reference.md` at the `KBGenomeUtilsImpl` entry (L132), add one sentence: "Internally wraps a `KBGenomeUtils(KBWSUtils)` delegate (see source L805); inherited multi-utility methods reach via `__getattr__` passthrough."

8. **CLI surface chapter.** Add a new top-level section to `api-summary.md` titled "## CLI" covering: `kbu jobs status/list/refresh/logs/cancel`, `kbu jobs chain submit/status`, `kbu jobdaemon --interval N`, and the `util.py` template's machine_configs sys_paths handling. Source of truth: `src/kbutillib/cli/` and `src/kbutillib/__main__.py`. Verify exact subcommand names by reading source — do not invent.

### Order of work (one task, internally ordered)

For the developer task on the new skill + supporting code:
1. Add the 5 new methods to `KBGenomeUtils` legacy class (with the legacy-raise for `save_assembly_from_fasta`).
2. Update `KBGenomeUtilsImpl.__init__` to accept `jobs`; pass through to delegate as needed.
3. Update `toolkit.py` `genome` property to construct `KBGenomeUtilsImpl(env, ws, jobs)`.
4. Add unit tests (see Testing Decisions).
5. Write `kbase-genome-expert.md` skill against the actual implemented API.
6. Sanity-check: every code example in the skill imports + parses; every API name referenced exists in source.

For the developer task on `kbutillib-expert` refresh:
1. Read current `kbutillib-expert.md` and all 3 bundled context files.
2. Apply each of the 8 fixes in order; for each, verify the underlying source-of-truth claim by reading the relevant `src/` file BEFORE writing the fix.
3. Sanity-check: grep the updated skill files for `get_config_value`, `kbu.jobs.submit(`, `kbu.jobs.cancel(`, `kbu.jobs.refresh(` — should be zero matches except where deliberately preserved.

## Testing Decisions

### Tests to write

Module: `kb_genome_utils.py` new methods, in `tests/test_kb_genome_utils_save.py` (new file).

- **`test_validate_genome_passes_on_minimal_valid_genome`** — fixture: smallest Genome dict that satisfies all required fields → `validate_genome` returns `[]`.
- **`test_validate_genome_flags_missing_required_field`** — drop `assembly_ref` → expect error string containing `assembly_ref`.
- **`test_validate_genome_flags_contig_length_mismatch`** — `contig_ids` length 3, `contig_lengths` length 2 → expect error.
- **`test_validate_genome_flags_feature_with_unknown_contig`** — feature location references a contig not in `contig_ids` → expect error.
- **`test_validate_genome_flags_duplicate_feature_ids`** — two features with same id → expect error.
- **`test_build_genome_from_fasta_only`** — fixture FASTA with 2 contigs → returns Genome dict with correct `contig_ids`, `contig_lengths`, `num_contigs=2`, computed `gc_content` and `md5`, empty `features`/`cdss`/`mrnas`.
- **`test_build_genome_from_fasta_with_gff`** — fixture FASTA + minimal GFF (1 CDS) → returns Genome dict with 1 feature with correct location and 1 entry in `cdss`.
- **`test_save_assembly_from_fasta_legacy_class_raises`** — instantiate bare `KBGenomeUtils()` (no jobs), call `save_assembly_from_fasta(...)` → expect `RuntimeError` with the documented message.
- **`test_save_genome_object_returns_ref_format`** — mock `self.ws.save_ws_object` to return a known info-tuple → assert returned ref is `"ws_id/obj_id/version"`.

The EE2-job round-trip in `save_assembly_from_fasta` (with `wait=True`) and the orchestration in `save_genome_with_assembly` are **integration-test-only** — they require live EE2 access and a real KBase workspace. Mark them as `@pytest.mark.integration` and skip by default; document the manual smoke-test command in the new skill's body.

### Prior art

- `tests/test_composition_smoke.py` already exercises facade wiring; pattern-match its style.
- Existing genome-related tests live in `tests/test_kb_genome_utils.py` (if present) — keep new save-flow tests in a separate file to avoid bloating one file past ~200 lines.

### What we deliberately don't test

- The wire format of EE2's `run_job` response (we'd be testing EE2, not us).
- AssemblyUtil's FASTA-to-shock upload (KBase SDK app, not our code).
- GenomeFileUtil's `save_one_genome` content validation (server-side, not our code).
- Visual rendering of the skill markdown.

The `kbutillib-expert` refresh has **no automated tests** — it's documentation. Acceptance is by visual review of the diff: every API name referenced must exist in source (verify by grep), every example must be valid Python (verify by mental parse). The grep sanity-check listed in Implementation Decisions step 3 is the gate.

## Out of Scope

- **Deployment via `claude-skills sync`.** Source edits land on the `wip` branch in `KBUtilLib/agent-io/skills/`; Chris runs `claude-skills sync <machine> --apply` separately after merge to land on each machine.
- **Updates to `kbutillib-dev`.** That skill targets contributors extending KBUtilLib; this PRD touches the consumer skill only.
- **Notebook-context refactor of `add_annotations_to_object`.** Method still requires SDK callback context. The skill documents this limitation; no refactor in this round.
- **New manipulation APIs (reannotate, reroute features, gene-feature surgery).** Chris listed these as examples of "common manipulations" but they're not required deliverables. Existing helpers (`add_annotations_to_object`, `alias_to_ftrs`, `aggregate_taxonomies`, `create_synthetic_genome`) are documented; new code only for save/load/validate.
- **GenBank ingest.** `genbank_to_genome` via GenomeFileUtil would be a separate add to `build_genome_from_*` family. Not this round.
- **Bundled context files for `kbase-genome-expert`.** Single-file skill until it grows past ~400 lines.
- **Fallback design for unreachable EE2.** Per Chris 2026-06-02: assume EE2 works.
- **Direct-WS Assembly save.** Building a typed Assembly object directly (without AssemblyUtil) requires reimplementing shock upload + handle creation in pure Python — out of scope; EE2-via-AssemblyUtil is the canonical path.

## Further Notes

- **PRD lives in `KBUtilLib/agent-io/prds/kbutillib-genome-skill-set/`** (not AIAssistant). The registered `taskplan_path` must therefore be an **absolute** path or the conductor's `load_taskplan` from the AIAssistant working dir will fail. Registration step takes care of this.
- **Skill source convention:** Both skills are sync-managed runtime artifacts. The canonical source lives under `KBUtilLib/agent-io/skills/`. Do not edit anything under `.claude/commands/` directly — `claude-skills sync` overwrites it. See `~/.claude/CLAUDE.md` "Skills and slash commands" for the contract.
- **Skill registry entries** in `~/Dropbox/Projects/ClaudeCommands/state/skill_registry.json` will be auto-discovered by `claude-skills inventory` once the new skill file exists at the home path.
- **Composition wiring change is backward-incompatible** for any caller that explicitly constructs `KBGenomeUtilsImpl(env, ws)` without the `jobs` arg. Search for such call sites before the change; expect zero (facade path is the only documented constructor).
- **EE2 job result parsing.** The `AssemblyUtil.save_assembly_from_fasta` return key (`assembly_ref` vs `upa` vs other) is the one place the implementer must read the AssemblyUtil spec; do not guess from this PRD. Same for `GenomeFileUtil.save_one_genome` if we ever fall back to EE2 for the genome save (not in this PRD's scope).
- **Audit was performed in-session 2026-06-02 against commit `51b5916`.** If the source has drifted significantly by implementation time, the auditor should re-verify each fix's premise before applying.
- **Confront round 1 was performed 2026-06-02** by GPT-5 codex on h100 (Maestro task-330ce54d). The codex worktree turned out to be on a stale base (commit `d632fbc`, pre-composition-refactor) so 6 of 10 stalls were rejected as based on non-existent source. The 4 surviving findings (stalls #3, #4, #8, #9) are folded above. **Stall #4 caught a critical inversion in the original audit** — `get_config_value` is the modern API, not `get_config`. A separate platform issue (h100 Maestro worktree base lagging Dropbox-synced commits) is worth filing as a follow-up; it did not block this PRD.

## Acceptance Criteria

1. `kbutillib-expert.md`, `api-summary.md`, and `patterns.md` contain zero references to `get_config_value(` being replaced by `get_config(` — instead every config-access example uses `kbu.env.get_config_value("section.key")` with dot notation, and `get_config(section, key)` is documented exactly once as deprecated/INI-compatibility-only.
2. `grep -nE "kbu\.jobs\.(submit|cancel|refresh)\(" agent-io/skills/kbutillib-expert*` returns zero matches after refresh (all replaced with `run_job`, `cancel_job`, `refresh_active`, etc., or removed).
3. `KBGenomeUtilsImpl.__init__` signature accepts `(env, ws, jobs, **kwargs)`; `toolkit.py`'s `genome` property constructs with the `jobs` dependency; `tests/test_composition_smoke.py` passes (or its analogue covers genome wiring).
4. `kbu.genome.save_genome_object(genome_dict, workspace, name)` returns a string ref of the form `"<ws_id>/<obj_id>/<version>"` (verifiable by mocking `self.ws.save_ws_object` and asserting the returned format).
5. `kbu.genome.save_assembly_from_fasta(fasta_path, workspace, name)` submits an EE2 job via `self.jobs.run_job(method="AssemblyUtil.save_assembly_from_fasta", params=[{...}])`, polls to terminal, and returns the value of the `assembly_ref` key from the job result. Integration test marked `@pytest.mark.integration` and skipped by default.
6. `kbu.genome.save_genome_with_assembly(fasta_path, genome_dict, workspace, base_name)` orchestrates assembly-then-genome and returns `(assembly_ref, genome_ref)` tuple; does not mutate the caller's `genome_dict`.
7. `kbu.genome.validate_genome(genome_dict)` returns `[]` for a minimal valid genome, and a non-empty list with specific error strings for: missing `assembly_ref` (default `require_assembly_ref=True`), mismatched `contig_ids`/`contig_lengths` lengths, feature with `location` referencing an unknown contig, and duplicate feature IDs across features+cdss+mrnas+non_coding_features.
8. `kbu.genome.validate_genome(genome_dict, require_assembly_ref=False)` accepts a genome dict with `assembly_ref=""` or missing, used by `build_genome_from_fasta_gff` callers.
9. `kbu.genome.build_genome_from_fasta_gff(fasta_path, gff_path, scientific_name="...", taxonomy="...")` produces a Genome dict that (a) passes `validate_genome(..., require_assembly_ref=False)`, (b) has `contig_ids` matching FASTA, (c) has `contig_lengths` summing to `dna_size`, (d) features have 1-based-inclusive start in their location tuples, (e) CDS features have non-empty `protein_translation` translated under `genetic_code=11` by default.
10. Calling `save_assembly_from_fasta` on a bare legacy `KBGenomeUtils()` instance (no jobs) raises `RuntimeError` whose message instructs the caller to use the facade.
11. `agent-io/skills/kbase-genome-expert.md` exists, has frontmatter (`name`, `description`, `scope: domain`), and contains the 7 sections enumerated in Implementation Decisions § "Skill file layout".
12. `agent-io/skills/kbutillib-expert.md` Related Skills section includes a line for `/kbase-genome-expert`.
13. The phrase "callback URL" appears at least once in the new `kbase-genome-expert.md` explaining what notebook contexts lack and pointing at `kbu.callback.set_callback_client()` as the injection escape hatch.
14. Every API name referenced in `kbase-genome-expert.md` resolves via `grep` to a real symbol in `src/kbutillib/` (no fabricated methods).
15. `pytest tests/test_kb_genome_utils_save.py -q` passes locally (unit tests only, integration tests skipped).

