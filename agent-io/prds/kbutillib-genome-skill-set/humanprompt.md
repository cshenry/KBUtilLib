# KBUtilLib Skill Set: Expert Refresh + Genome Skill

Two deliverables, both homed in `KBUtilLib/agent-io/skills/`:

1. **Refresh `kbutillib-expert`** — apply approved gap fixes from the 2026-06-02 audit (API bugs, undocumented methods, missing context).
2. **New `kbase-genome-expert` skill** — task-oriented skill for saving, loading, validating, and manipulating KBase Genome objects from notebook contexts (no SDK callback). Requires extending `kb_genome_utils.py` with high-level save/load/validate methods.

## Why now

- The composition refactor (2026-05) landed and the expert was updated, but a fresh audit against `src/` surfaced factual bugs (e.g., `get_config_value` doesn't exist; `KBJobUtils` API names are wrong) and uncovered helpers.
- Notebook authors on primary-laptop / h100 need a clean save-genome flow. The existing path (`kbu.callback.save_genome_or_metagenome`) requires an SDK callback context they don't have. Direct Workspace `save_objects` works for the typed Genome, EE2 job-submission handles the assembly's shock upload.
- Chris approved all 8 audit gap-fixes and the broader genome-skill scope (save + load + validate + manipulations of KBase Genome objects).

## What's in the box

**Skill 1 — `kbutillib-expert` refresh** (apply fixes 1–8):

1. Fix `get_config_value` → `get_config(section, key)` in api-summary.md + patterns.md
2. Reconcile `KBJobUtils.run_job(method, params, *, app_id=...)` examples; fix wrong method names (`submit`, `refresh`, `cancel` → `run_job`, `refresh_active`, `cancel_job`)
3. Document existing `kb_genome_utils.py` methods (add_annotations_to_object, load_genome_from_local_files, aggregate_taxonomies, create_synthetic_genome, lookup-cache helpers)
4. Document `kbu.callback.set_callback_client(...)` injection hook
5. Flag `installed_clients/` shipping constraint (AssemblyUtil/GenomeFileUtil clients require external KBase SDK install)
6. Cross-reference new `kbase-genome-expert` skill in Related Skills + patterns.md save section
7. One-liner noting `KBGenomeUtilsImpl` wraps `KBGenomeUtils(KBWSUtils)` delegate
8. New CLI surface chapter (`kbu jobs`, `kbu jobdaemon`, `util.py` template + machine_configs sys_paths)

**Skill 2 — `kbase-genome-expert` (new)**, plus supporting code:

- Notebook-only execution context (no SDK callback assumed)
- New high-level methods on `KBGenomeUtilsImpl`:
  - `save_genome_object(genome_dict, workspace, name) -> str` — direct Workspace `save_objects` as `KBaseGenomes.Genome`
  - `save_assembly_from_fasta(fasta_path, workspace, name, *, wait=True, timeout=600) -> str` — EE2 job (`AssemblyUtil.save_assembly_from_fasta`)
  - `save_genome_with_assembly(fasta_path, genome_dict, workspace, base_name) -> tuple[str,str]` — orchestrates assembly-then-genome with assembly_ref splicing
  - `validate_genome(genome_dict) -> list[str]` — schema check (required fields, types, ref-resolvability, feature-ID uniqueness, location well-formedness); empty list = valid
  - `build_genome_from_fasta_gff(fasta_path, gff_path=None, *, scientific_name, taxonomy, genetic_code=11) -> dict` — constructor for the save flow
- `toolkit.py` wiring: `genome → KBGenomeUtilsImpl(env, ws, jobs)` (add `jobs` dep)
- Workspace is a **required parameter** on every save/load method — no implicit default

## Constraints

- Home repo: `KBUtilLib`
- Skill files saved under `KBUtilLib/agent-io/skills/` (sync-managed; runtime `.claude/commands/` is gitignored)
- EE2 access is assumed working (no fallback design for unreachable EE2)
- Existing `kb_genome_utils.py` methods stay backward-compatible; new methods only

## Out of scope

- claude-skills sync deployment (Chris runs separately after merge)
- Updates to `kbutillib-dev`
- Notebook-context refactor of `add_annotations_to_object` (still callback-dependent — documented as such)
- New reannotate / reroute-feature manipulation APIs (existing helpers documented; no new manipulation code)
- Genome formats beyond FASTA/GFF (no GenBank ingest this round)
