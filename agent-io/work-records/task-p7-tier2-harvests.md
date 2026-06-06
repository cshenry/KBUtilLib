# Work Record: task-p7-tier2-harvests

## task_id
task-p7-tier2-harvests

## branch
task-p7-tier2-harvests

## commit_shas
- 053b969b1c7d3b49ab6a7c51f04a00e0dc0c4f24

## summary
Replaced the three Phase 6 placeholder files (`kbu-synthesize.md`, `kbu-review.md`,
`kbu-literature-review.md`) under `templates/student-project/.claude/commands/` with
full skill files harvested from BERIL-research-observatory at commit 940c3b0. Each file
was stripped of all BERIL-specific concepts (lakehouse, MinIO, Spark, beril.yaml,
BERDL-specific paths) and adapted to the kbu subproject layout
(`subprojects/<name>/{notebooks,data,figures,references.md}`). Provenance headers,
correct stage preconditions, `kbu subproject advance` wiring, and session-save calls
were added per the task requirements.

## files_touched
- `templates/student-project/.claude/commands/kbu-synthesize.md` (215 lines)
- `templates/student-project/.claude/commands/kbu-review.md` (189 lines)
- `templates/student-project/.claude/commands/kbu-literature-review.md` (210 lines)

## success_criteria_check

- **Three files exist** — PASS. All three files are present and non-empty.
- **Each contains `kbu skill provenance` header with `type: harvested`** — PASS. All three have the provenance block at file top.
- **Each contains `source_commit: 940c3b0ee7bbf63bc576bd6e8c25210ad692df8e`** — PASS. Verified via grep.
- **`grep -c 'pubmed.mcp.claude.com' kbu-literature-review.md` returns at least 1** — PASS. Returns 1.
- **`grep -c 'paper-search-mcp' kbu-literature-review.md` returns at least 1** — PASS. Returns 2 (appears in JSON snippet args and secondary section header).
- **`grep -c 'kbu-review:verdict' kbu-review.md` returns at least 1** — PASS. Returns 4 (intro, template, verdict rules, notes).
- **`grep -lE 'lakehouse|MinIO|beril.yaml|spark' ...` returns 0 files** — PASS. grep exits 1 (no matches) on all three files.
- **Each file is ≤ 300 lines** — PASS. Counts: kbu-synthesize 215, kbu-review 189, kbu-literature-review 210.

## tests_run
- `wc -l` on all three files — pass (215, 189, 210 lines; all ≤ 300).
- `grep -c 'pubmed.mcp.claude.com' kbu-literature-review.md` — 1 (pass).
- `grep -c 'paper-search-mcp' kbu-literature-review.md` — 2 (pass).
- `grep -c 'kbu-review:verdict' kbu-review.md` — 4 (pass).
- `grep -lE 'lakehouse|MinIO|beril\.yaml|[Ss]park' ...` — exit 1, no files matched (pass).
- No unit tests applicable (skill markdown files).

## caveats
- The `kbu-literature-review.md` MCP setup section embeds the `.mcp.json` snippet verbatim from the BERIL repo at 940c3b0. The snippet includes `"SEMANTIC_SCHOLAR_API_KEY": "${SEMANTIC_SCHOLAR_API_KEY:-}"` which uses bash-style parameter expansion — this is valid inside JSON only if the MCP client supports env var interpolation (Claude desktop and Code both do).
- `kbu-review.md` uses three stage identifiers (`p-review`, `b-review`, `s-review`) that must match the stage names implemented by the `p7-tier2-lean-forks` and `p7-tier2-net-new` developers and the CLI's stage machine. If the CLI uses different stage names, the auto-detect table will need updating.
- The `kbu-synthesize.md` session-save block uses `from assistant.state import save_session` — this assumes AIAssistant is installed in the student environment. If not available, the session save step will silently fail (non-blocking for the workflow itself).
