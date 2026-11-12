# Work record: docs-opc5-and-h3

- task_id: docs-opc5-and-h3
- branch: maestro/developer/docs-opc5-and-h3
- commit_shas: 9a05a6b266e98634324f6c090f3f42b3bed58442 (runbook); work-record commit follows
- base: bb426ef14496b290e68dd0133b5c0004e2a1513a (main did not move)

## Summary
Added one new `##` chapter to the operator runbook, immediately before "Backfill: filling the clearinghouse from a source" (after OP-C4 and its run record, NOT under the parameter-set chapter, per dispatch correction C1). It holds OP-C5 (C5.0 precondition, C5.1 dry run, C5.2 real run, C5.3 acceptance reading live columns and partition spec of each of five tables, C5.4 all_alias view, C5.5 closing check) with its own empty run-record table; the rule A / rule B assembly-hash section (Q11.1 settled, n=3 of 47); the genome_id = content_hash[:16] chain; the reader's alias_id/entity_hash warning; and the corrected obj:// prefix rule (standalone joins in both directions, no one-expression archive-member join). A short pointer note was added in section 2.4 saying the union views are now four.

## files_touched
- agent-io/docs/clearinghouse-schema-operator-runbook.md
- agent-io/work-records/docs-opc5-and-h3.md

## success_criteria_check
- OP-C5 after OP-C4, dry/real/acceptance structure: pass (as a new ## chapter, per C1)
- acceptance reads live columns AND partition spec, says why existence is insufficient: pass
- closes on health: pass, but per C2 as three independent checks; exit zero does NOT imply orphan_total 0 (stated explicitly). The criterion's literal "exit zero with orphan total 0" was deliberately not written as one claim.
- rule A / rule B, fitness declares neither, genome_id chain: pass
- "entity_alias one of two non-entity-typed tables, in no union view": deliberately NOT satisfied (C3/C4); doc says five per-type tables, parameter_set the only non-entity-typed one, all_alias exists, views now four
- self-join warning: pass
- no src/ change: pass (diff touches only agent-io/)
- obj:// corrected form: pass
- fitness.assembly_hash is rule A, documentation gap: pass
- five tables + all_alias, 16 -> 21, why per-type: pass

## tests_run
- /Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python3 -m pytest -o pythonpath=src tests/ -q (run from the worktree, at the runbook commit)
- Result: 3 failed, 7778 passed, 575 skipped in 432s. Expected 5/7776/575. The 3 failures are the known MCP unavailable-handler ones; the 2 LLM import-isolation tests passed this run. The diff touches no code, so this is environmental/order-dependent flakiness of those two tests, not a change of mine. Not investigated further.

## caveats
- build_genome.py lines (484, 1404, 1549, 1690, 1720) are attributed to KBDLJobRunningPrototype wip c676443 as of 2026-11-12, as supplied by the dispatcher; I could not open that repo.
- The SQL snippets and DESCRIBE loop are unexecuted documentation (no pod access); the join SQL omits latest-observation reduction (noted in the text).
- No line numbers for in-repo files are written into the runbook; functions are named instead.
