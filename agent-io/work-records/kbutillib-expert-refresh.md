# Work Record: kbutillib-expert-refresh

**task_id:** kbutillib-expert-refresh
**branch:** kbutillib-expert-refresh
**date:** 2026-06-02

## commit_shas

- `e80d4c00711852e90ccf09c2ce5b0ca3d581b48e`

## summary

Refreshed the `kbutillib-expert` skill and its three bundled context files by applying all 8 gap-fixes documented in the `kbutillib-genome-skill-set` PRD (Implementation Decisions § "kbutillib-expert refresh"). Every fix was verified against the corresponding source file in `src/kbutillib/` before editing. The two mandatory verification greps both pass: no `kbu.env.get_config(` calls remain in the skill files (other than one deprecation note) and no `kbu.jobs.(submit|cancel|refresh)(` calls remain.

## files_touched

- `agent-io/skills/kbutillib-expert.md`
- `agent-io/skills/kbutillib-expert/context/module-reference.md`
- `agent-io/skills/kbutillib-expert/context/api-summary.md`
- `agent-io/skills/kbutillib-expert/context/patterns.md`
- `agent-io/work-records/kbutillib-expert-refresh.md` (this file)

## success_criteria_check

| Criterion | Status | Notes |
|---|---|---|
| All 8 gap-fixes applied to the 4 skill files | **PASS** | Verified below per fix |
| `grep -nE 'kbu\.env\.get_config\('` returns at most one match | **PASS** | Zero matches; deprecation note uses prose, not a code call |
| `grep -nE 'kbu\.jobs\.(submit|cancel|refresh)\('` returns zero matches | **PASS** | Zero matches |
| Related Skills section includes `/kbase-genome-expert` | **PASS** | Line 197 of kbutillib-expert.md |
| CLI chapter documents only subcommands present in `src/kbutillib/cli/` | **PASS** | All commands verified against jobs.py, jobdaemon.py, __init__.py |

### Per-fix status

**Fix #1 — Standardize config access on `get_config_value`** — PASS
Source verified: `shared_env_utils.py:195-244`. `get_config(section, key, default=None)` at L195 is marked deprecated; `get_config_value(key_path, default=None)` at L216 is the modern API taking dot-notation. Replaced `kbu.env.config.get("section.key")` (wrong — config is not a simple dict with that method) with `kbu.env.get_config_value("section.key")` in kbutillib-expert.md and api-summary.md. Added deprecation note in patterns.md and module-reference.md.

**Fix #2 — Reconcile KBJobUtils examples** — PASS
Source verified: `kb_job_utils/utils.py` and `kb_job_utils/__init__.py`. Real methods: `run_job`, `cancel_job`, `check_job`, `check_jobs`, `refresh_active`, `refresh_all`, `get_record`, `list_active`, `list_all`, `get_job_logs`, `cleanup`, `submit_chain`, `start_watcher`, `stop_watcher`. Fabricated methods removed: `submit`, `cancel`, `refresh`, `get`, `list`, `forget`, `summary`, `get_logs`. All examples in kbutillib-expert.md, api-summary.md, patterns.md updated to use real method names.

**Fix #3 — Document undocumented kb_genome_utils.py methods** — PASS
Source verified: `kb_genome_utils.py` method list via grep. Added to module-reference.md (KBGenomeUtilsImpl section) and api-summary.md (Genome Operations): `load_kbase_gene_container`, `object_to_features`, `get_ftr`, `ftr_to_aliases`, `alias_to_ftrs`, `object_to_proteins`, `add_annotations_to_object`, `load_genome_from_local_files`, `aggregate_taxonomies`, `create_synthetic_genome`, `calculate_gc_content`, plus new PRD methods by name: `save_genome_object`, `save_assembly_from_fasta`, `save_genome_with_assembly`, `validate_genome`, `build_genome_from_fasta_gff`.

**Fix #4 — Document kbu.callback.set_callback_client()** — PASS
Added subsection "Injecting SDK Clients in Notebook Contexts" to patterns.md after the configuration pattern, explaining the callback URL requirement and the `set_callback_client(name, client)` injection hook.

**Fix #5 — Flag installed_clients/ shipping constraint** — PASS
Added an "Installation note" paragraph near the top of kbutillib-expert.md (after the composition IMPORTANT note) flagging that `AssemblyUtilClient` and `GenomeFileUtilClient` are not shipped and require a separate KBase SDK install, with `ImportError` consequence for `kbu.callback.gfu_client()` / `afu_client()`.

**Fix #6 — Cross-reference kbase-genome-expert** — PASS
Added to Related Skills in kbutillib-expert.md: `/kbase-genome-expert - For saving, loading, and validating KBase Genome objects from notebooks`. Added a "See also" pointer at the end of the callback injection pattern in patterns.md.

**Fix #7 — KBGenomeUtilsImpl delegate note** — PASS
Added one sentence to KBGenomeUtilsImpl entry in module-reference.md: "Internally wraps a `KBGenomeUtils(KBWSUtils)` delegate (see source L805); inherited multi-utility methods reach via `__getattr__` passthrough."

**Fix #8 — CLI surface chapter** — PASS
Added a new top-level section "## CLI" to api-summary.md. Sourced from `src/kbutillib/cli/jobs.py` (verified all subcommands: status, list, summary, refresh, logs, cancel, forget, cleanup, chain submit/list/status/cancel/advance), `src/kbutillib/cli/jobdaemon.py` (kbu jobdaemon), `src/kbutillib/cli/__init__.py` (kbu init-notebook). The `util.py.tmpl` sys_paths bootstrap is documented under `kbu init-notebook`. No commands invented.

## tests_run

None run — this task is documentation only. The PRD explicitly states: "The kbutillib-expert refresh has no automated tests — it's documentation. Acceptance is by visual review of the diff: every API name referenced must exist in source (verify by grep), every example must be valid Python (verify by mental parse). The grep sanity-check listed in Implementation Decisions step 3 is the gate."

Grep verification output:
```
$ grep -nE 'kbu\.env\.get_config\(' agent-io/skills/kbutillib-expert.md agent-io/skills/kbutillib-expert/context/*.md
(no output — zero matches)

$ grep -nE 'kbu\.jobs\.(submit|cancel|refresh)\(' agent-io/skills/kbutillib-expert.md agent-io/skills/kbutillib-expert/context/*.md
(no output — zero matches)
```

## source_files_consulted

- `src/kbutillib/shared_env_utils.py` — verified `get_config_value` (L216) is modern; `get_config` (L195) is deprecated
- `src/kbutillib/kb_job_utils/utils.py` — enumerated all real public methods of `KBJobUtils`
- `src/kbutillib/kb_job_utils/__init__.py` — confirmed public exports
- `src/kbutillib/kb_genome_utils.py` — enumerated all `def ` lines to find real methods on `KBGenomeUtils` and `KBGenomeUtilsImpl`
- `src/kbutillib/cli/__init__.py` — confirmed top-level commands: `jobs`, `jobdaemon`, `init-notebook`
- `src/kbutillib/cli/jobs.py` — enumerated all subcommands and verified signatures
- `src/kbutillib/cli/jobdaemon.py` — verified `--interval`, `--kb-version`, `--log-level` options
- `src/kbutillib/cli/machine.py` — understood machine_configs layout
- `src/kbutillib/cli/templates/util.py.tmpl` — verified sys_paths bootstrap for init-notebook documentation

## caveats

1. **New PRD methods documented by name only.** `save_genome_object`, `save_assembly_from_fasta`, `save_genome_with_assembly`, `validate_genome`, `build_genome_from_fasta_gff` are documented in module-reference.md and api-summary.md based on PRD spec, not implemented source. They live on the sibling task's branch, which was not merged to main at the time of this task. Once the sibling task merges, no further skill edits are needed.
2. **Composition graph still shows `genome → KBGenomeUtilsImpl(env, ws)`.** The PRD says the sibling task will update this to `KBGenomeUtilsImpl(env, ws, jobs)`. The module-reference.md composition graph was left unchanged for this task (the graph edit is part of the sibling "new methods" task). When that merges, a follow-up skill edit should update the graph line from `genome → KBGenomeUtilsImpl(env, ws)` to `genome → KBGenomeUtilsImpl(env, ws, jobs)`.
3. **`kbu.jobs.store.delete(job_id)` for programmatic forget.** The `forget` command exists in the CLI but `KBJobUtils` has no `forget` Python method. The documented workaround (`kbu.jobs.store.delete(job_id)`) is a direct store access, which is internal API. If a public `forget` method is later added, update the api-summary note.
4. **`add_annotations_to_object` callback context note.** Marked as requiring callback context in the skill. This is a known limitation documented in the PRD's Out of Scope section; no refactor in this round.
