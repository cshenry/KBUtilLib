# Work record: cli-aliases-and-resolve

- task_id: cli-aliases-and-resolve
- branch: maestro/developer/cli-aliases-and-resolve
- commit_shas: 8cfb07c94353cbb2b5d18638ddbb195d8f8665a3 (feature), plus the work-record commit on top

## Summary
Added `aliases` and `resolve` verbs to src/kbutillib/interfaces/cli/clearinghouse.py via `_build_aliases` / `_build_resolve`, renderer branches, docs, and tests. SCHEMA_VERSION stays 1 (tested). `--type` omitted reaches the capability as `entity_type=None` (all_alias view) on both verbs. Unpassed `--alias-source`/`--alias-id` use the house idiom `list(x) or None` (tested: capability receives None, and repeated values arrive as lists).

## Exit contract (five cases)
valid-but-unknown alias -> success envelope, exit 0 (`found:false`, human "not known"); AliasAmbiguousError -> one failure envelope, exit 1; AliasNotInvertibleError -> one failure envelope, exit 1, message names the `aliases` verb; invalid alias_source/type (ValueError) -> one failure envelope, exit 1; other (transport) errors propagate, non-zero. Failure envelope is emitted via _emit then SystemExit(1); `data.error = {kind, message}`, message also in `warnings`.

**AliasNotInvertibleError:** this fifth case came from the conductor, not the plan; the exit-1 contract (naming aliases()) is the conductor's inference, not plan text. It is caught by name because it is a RuntimeError, not ValueError. Tests parametrised over ncbi_taxon and arc_analysis.

## Verification
- kbutillib.__file__ = /Users/chenry/.maestro/worktrees/cli-aliases-and-resolve/src/kbutillib/__init__.py (checked via sys.path.insert from the worktree; pytest run with -o pythonpath=src). No test asserts this.
- Command: /Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python3 -m pytest -o pythonpath=src tests/cli tests/berdl -q
- Result: 2637 passed, 17 skipped, 0 failed (95 s). Full suite not run (conductor runs it).

## Files touched
- src/kbutillib/interfaces/cli/clearinghouse.py
- tests/cli/test_clearinghouse_aliases.py (new)
- agent-io/docs/kbu-clearinghouse-cli.md

## Caveats
- aliases also converts a capability ValueError (bad --type) into a ClickException (exit 1, no envelope); the spec only required one-envelope for resolve.
- Docs exit-codes wording: resolve note placed at the top of the existing Exit codes section.
