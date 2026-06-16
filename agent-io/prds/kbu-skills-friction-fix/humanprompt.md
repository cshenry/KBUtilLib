# Fix `kbu` skill-bundle friction (BERIL proving-ground audit)

Remediate the issues found in the first BERIL proving-ground run of the `kbu`
skill bundle (`/kbu`, `kbu-notebook`, `kbu-fba`), documented in
`BERIL-research-observatory/agent-io/audits/2026-06-15-kbu-skills-friction.md`.
The run never reached first compute: notebook/CLI venvs were missing declared
KBUtilLib dependencies, two divergent `util.py` templates exist, and the skills
contain doc-vs-code inconsistencies.

All fixes land in **KBUtilLib** (code, machine config, and the BERIL skill
sources all live here). Five tasks:

- **A — Venv provisioning + diagnostics.** Pin the heavy transitive deps in
  `notebook_deps`; extend the existing `kbu doctor` to import-check the FBA arc
  and flag missing deps; quiet the alarming per-module import-failure banner.
- **B — Unify the `util.py` template.** Collapse two divergent templates to one
  source of truth (`cli/templates/util.py.tmpl`), **FLAT** layout, guarded
  imports, `__file__` anchoring, smart-merge marker, **no** generic helpers
  block. Fix `init_notebook` path math; point the skill doc at the real file.
- **C — Skill-doc corrections.** State the `NotebookSession` import; declare
  `session.kbu.*` canonical; pin the FBA object-type contract and objective DSL;
  document the `/berdl_start` hand-back, the offline genome fallback, the
  `KB_AUTH_TOKEN` env var, and fix `preferences.md` shipping.
- **D — Reference exemplar.** Ship one tiny `KBUtilLib/examples/` project showing
  the canonical flat layout end-to-end (util.py + one cell + a cached artifact).
- **E — KBase token injection.** Make `SharedEnvUtils` promote a `KB_AUTH_TOKEN`
  env var into the kbase token with precedence over files, so BERIL can supply
  the token once instead of users stashing it in multiple places.

## Key decisions (made at design time)

- **Flat layout is canonical** — it matches BERIL's own project structure. One
  `notebooks/util.py` + one shared `.kbcache/` per project; `.ipynb` as siblings.
  `PROJECT_ROOT = NOTEBOOK_DIR.parent`.
- **Unified template drops the generic helpers/schema block** — notebooks import
  helpers explicitly. The block was unguarded and broke `%run util.py` in a
  minimal venv.
- **`session.kbu.*` is the canonical FBA idiom**; bare `MSFBAUtils(...)` is an
  escape hatch only.
- **BERIL integration is docs-only this round** — no `beril.yaml` writes.
- **`KB_AUTH_TOKEN` env var wins over files** (KBase SDK standard name).

## Out of scope

- CLI writing `beril.yaml`.
- The Cloudflare off-cluster challenge blocking BERDL (BERDL/infra issue).
- Re-running `energy_loop_analysis` (needs BERDL; manual follow-up).
- `claude-skills sync` to redeploy edited skills into BERIL (manual post-merge).
- The broken Homebrew `python@3.13` under the CLI venv (machine fix:
  `brew reinstall python@3.13`).
