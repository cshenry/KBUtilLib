# `kbu king` CLI reference

`kbu king` self-installs the KBUtilLib-modeling app (App-2 in the
[king-integration-apps](../prds/) PRD) into a local KING checkout, without
ever modifying KING's own repository. It is a thin CLI facade over the
vendored `kbutillib.king_install` module, which reads this repo's OWN
packaged bundle (`src/kbutillib/king_app/{bundle.json,skill.md}`) — no
cross-repo dependency; a checkout/install of KBUtilLib alone is enough to
run `kbu king install`.

Built for [king-integration-apps](../prds/) Module C/D: composing this
app's `skill.md` into KING's injected `KING_CONTEXT` orientation and
wiring the launch env, per Acceptance Criteria #13-#21.

## Verbs

```
kbu king install [--apps-dir PATH] [--json]
kbu king uninstall [--apps-dir PATH] [--json]
kbu king status [--apps-dir PATH] [--json]
```

`--apps-dir` overrides `$KING_APPS_DIR` (default `~/king-apps`).

### `install`

Idempotent (AC #17): re-running on an unchanged bundle is a no-op diff —
no file under `$KING_APPS_DIR` is touched (mtimes unchanged) if nothing
changed. Never fails just because `kbu` isn't on PATH; reports the state
instead (`cli_on_path: false`) so the install can complete and the app can
be verified later once the CLI is installed.

Actions, all confined to `$KING_APPS_DIR` (never `~/king-stack/king/`):

1. Verify the hand: `shutil.which("kbu")` plus the bundle's `verify` probe
   (`kbu model --help`, checked for exit 0 and the `ok_text` substring
   `"Metabolic-modeling verbs"`).
2. Copy `skill.md` to `$KING_APPS_DIR/kbutillib-modeling/skill.md`; record
   the app in `$KING_APPS_DIR/registry.json` keyed by id
   `kbutillib-modeling`.
3. **Union-recompose** `$KING_APPS_DIR/CONTEXT.md` from every id currently
   in `registry.json` (ids sorted lexicographically, one
   `# [KING App] <title> (id: <id>)` header per id) — this is what lets
   this installer and a sibling tool's own `<tool> king install` (e.g.
   AIAssistant's `assistant king install`) coexist without clobbering each
   other's fragment, in either install order.
4. Generate/update `$KING_APPS_DIR/serve-king.sh`, which exports
   `KING_CONTEXT=$KING_APPS_DIR/CONTEXT.md` (plus `KING_PLUGINS_DIR` only
   when some registered app declares `manifests`), then `exec`s KING's own
   `<KING_STACK_DIR>/king/scripts/serve.sh`. `KING_STACK_DIR` defaults to
   `~/king-stack`. The user launches KING via this wrapper instead of
   KING's own `serve.sh` directly.

`--json` output:

```json
{"id": "kbutillib-modeling", "apps_dir": "...", "app_dir": "...", "cli": "kbu",
 "cli_on_path": true, "verify_probe_ran": true, "verify_probe_ok": true,
 "verify_output": "...", "changed": true,
 "context_md": "...", "serve_script": "...", "registry": "..."}
```

### `uninstall`

Removes `$KING_APPS_DIR/kbutillib-modeling/` and its `registry.json` entry,
then recomposes `CONTEXT.md` (other apps' fragments are preserved). A
no-op if the app was never installed (AC #16).

### `status`

Static coloring (AC #18) — there is no live-orientation API to confirm a
running KING session actually sees the injected text (none exists; a live
check is a documented **manual** step: launch via `serve-king.sh`, start a
session, ask the agent what capabilities it has):

- **green**: CLI on PATH AND verify probe passes AND `CONTEXT.md` contains
  the app header AND `$KING_CONTEXT` resolves to that exact file.
- **amber**: CLI missing (remediation printed: `pip install -e <repo
  containing kbu>`), regardless of composition state.
- **red**: CLI present but the verify probe fails, or `KING_CONTEXT` isn't
  wired to this app's `CONTEXT.md`.

Exit codes follow the CRAFT CLI convention already used by `kbu
researchos`/`kbu doctor`: 0 = green, 1 = amber, 2 = red.

`status` also reports (best-effort, never fatal):

- **Versions** (AC #20): `kbutillib`/`cobra`/`modelseedpy` versions, probed
  via a `python -c` import in a subprocess against the same interpreter
  `kbu` runs under.
- **LLM route** (AC #21): reads KING's own persisted
  `<KING_STACK_DIR>/king/runs/settings.json` directly (read-only; no
  `king_backend` import — that would be a cross-repo dependency). Absent
  settings means KING's default route (`anthropic`, direct/local, reported
  as local). Route `cborg` (LBNL's hosted gateway) is reported as
  non-local and produces a WARNING, never a block — this app is intended
  local-only.

`--json` output:

```json
{"id": "kbutillib-modeling", "color": "green", "cli_on_path": true,
 "verify_probe_ran": true, "verify_probe_ok": true, "composed": true,
 "context_has_header": true, "king_context_wired": true, "remediation": null,
 "versions": {"kbutillib": "0.1.0", "cobra": "0.30.0", "modelseedpy": "0.4.2"},
 "llm_route": "anthropic", "llm_route_is_local": true, "llm_route_warning": null}
```

## The `~/king-apps/` on-disk contract

`kbu king` and any sibling tool's own vendored `<tool>.king_install`
module (each self-contained, no cross-repo import) interoperate purely
through this on-disk layout:

```
$KING_APPS_DIR/                  # default ~/king-apps
├── registry.json                 # {"<id>": {id, title, description, cli,
│                                  #           verify, manifests, bundle_hash,
│                                  #           installed_at, updated_at}, ...}
├── CONTEXT.md                     # union-recomposed from every registry.json entry
├── serve-king.sh                   # generated launch wrapper
└── kbutillib-modeling/
    └── skill.md                    # this app's injected orientation prose
```

Whichever installer runs last recomposes `CONTEXT.md` as a correct
superset — installing (or uninstalling) one app never drops another's
fragment. See `src/kbutillib/king_install.py` for the implementation and
`tests/cli/test_king.py::TestInstall::
test_install_union_recompose_keeps_other_apps_fragment` (and the matching
`TestUninstall` case) for the automated proof, using an independent
fixture bundle standing in for a sibling installer's app.

## Bundle schema (AC #14)

`src/kbutillib/king_app/bundle.json` (shipped as package data — see
`[tool.setuptools.package-data]` in `pyproject.toml`):

```json
{
  "id": "kbutillib-modeling",
  "title": "KBUtilLib Metabolic Modeling",
  "description": "...",
  "cli": "kbu",
  "verify": {"cmd": ["kbu", "model", "--help"], "ok_text": "Metabolic-modeling verbs"}
}
```

`id`/`title`/`description`/`cli` are required; `verify`/`manifests` are
optional. The verify probe passes when the command exits 0 and (if
`ok_text` is given) that text appears in its stdout.

`src/kbutillib/king_app/skill.md` is authored from the frozen `kbu model
--help` surface (Module B, already merged) — see
[`kbu-model-cli.md`](kbu-model-cli.md) for the CLI reference it summarizes
for the agent. It is injected into KING sessions via `KING_CONTEXT` only
and is **not** registered with `claude-skills` (AC #22) — KING sessions
have no `Skill` tool to invoke a registered skill with.
