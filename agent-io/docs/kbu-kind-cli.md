# `kbu kind` CLI reference

`kbu kind` self-installs this repo's KIND apps into a local KIND checkout,
without ever modifying KIND's own repository. It is a thin CLI facade over
the vendored `kbutillib.kind_install` module, which reads this repo's OWN
packaged bundles — no cross-repo dependency; a checkout/install of
KBUtilLib alone is enough to run `kbu kind install`.

## The two bundles this repo ships

| `--app` | package dir | app id | `cli` |
|---|---|---|---|
| `modeling` | `src/kbutillib/kind_app/` | `kbutillib-modeling` | `kbu` |
| `wake` | `src/kbutillib/kind_app_wake/` | `persistentai-wake` | `persistentai` |

`modeling` is App-2 in the [king-integration-apps](../prds/) PRD — the
metabolic-modeling verbs.

`wake` gives a KING/KOROS session the ability to fire a **triggered wake**
at one of Chris's persistent agents (`luna` on primary-laptop, `miles` on
h100) by writing an envelope onto the `persistentai` trigger rail. It is
deliberately fire-and-forget: 3–15 minute latency, no return value, and no
reply channel, because an originating envelope cannot request one. The
skill prose says so in those words, since it is the only thing an isolated
KOROS session will ever know about the rail.

Its `cli` (`persistentai`) lives in another repo. That is fine and
expected — `install` never fails on a missing CLI, it reports
`cli_on_path: false`, and `status` colors the app amber until the CLI
arrives. **The wake app is only usable on primary-laptop**: a KIND session
on the BERDL pod has neither `persistentai` nor the Dropbox-synced trigger
inbox.

With no `--app`, every verb acts on **all** of this repo's bundles. A
per-app default would silently ship a subset.

Built for [king-integration-apps](../prds/) Module C/D: composing this
app's `skill.md` into KIND's injected `KING_CONTEXT` orientation and
wiring the launch env, per Acceptance Criteria #13-#21.

## Verbs

```
kbu kind install   [--app modeling|wake] [--apps-dir PATH] [--json]
kbu kind uninstall [--app modeling|wake] [--apps-dir PATH] [--json]
kbu kind status    [--app modeling|wake] [--apps-dir PATH] [--json]
```

`--apps-dir` overrides `$KIND_APPS_DIR` (default `~/kind-apps`).
`--app` narrows to one bundle; omitting it acts on all of them.

**`--json` always emits a LIST** of per-app result objects, one per app
acted on — a list even when `--app` selects exactly one. It was a bare
object before this repo shipped a second bundle; an output shape that
changes with the number of apps is the kind of thing that breaks a caller
months later.

### `install`

Idempotent (AC #17): re-running on an unchanged bundle is a no-op diff —
no file under `$KIND_APPS_DIR` is touched (mtimes unchanged) if nothing
changed. Never fails just because `kbu` isn't on PATH; reports the state
instead (`cli_on_path: false`) so the install can complete and the app can
be verified later once the CLI is installed.

Actions, all confined to `$KIND_APPS_DIR` (never `~/king-stack/king/`):

1. Verify the hand: `shutil.which("kbu")` plus the bundle's `verify` probe
   (`kbu model --help`, checked for exit 0 and the `ok_text` substring
   `"Metabolic-modeling verbs"`).
2. Copy `skill.md` to `$KIND_APPS_DIR/kbutillib-modeling/skill.md`; record
   the app in `$KIND_APPS_DIR/registry.json` keyed by id
   `kbutillib-modeling`.
3. **Union-recompose** `$KIND_APPS_DIR/CONTEXT.md` from every id currently
   in `registry.json` (ids sorted lexicographically, one
   `# [KIND App] <title> (id: <id>)` header per id) — this is what lets
   this installer and a sibling tool's own `<tool> king install` (e.g.
   AIAssistant's `assistant kind install`) coexist without clobbering each
   other's fragment, in either install order.
4. Generate/update `$KIND_APPS_DIR/serve-kind.sh`, which exports
   `KING_CONTEXT=$KIND_APPS_DIR/CONTEXT.md` (plus `KING_PLUGINS_DIR` only
   when some registered app declares `manifests`), then `exec`s KIND's own
   `<KING_STACK_DIR>/king/scripts/serve.sh`. `KING_STACK_DIR` defaults to
   `~/king-stack`. The user launches KIND via this wrapper instead of
   KIND's own `serve.sh` directly.

`--json` output:

```json
{"id": "kbutillib-modeling", "apps_dir": "...", "app_dir": "...", "cli": "kbu",
 "cli_on_path": true, "verify_probe_ran": true, "verify_probe_ok": true,
 "verify_output": "...", "changed": true,
 "context_md": "...", "serve_script": "...", "registry": "..."}
```

### `uninstall`

Removes `$KIND_APPS_DIR/kbutillib-modeling/` and its `registry.json` entry,
then recomposes `CONTEXT.md` (other apps' fragments are preserved). A
no-op if the app was never installed (AC #16).

### `status`

Static coloring (AC #18) — there is no live-orientation API to confirm a
running KIND session actually sees the injected text (none exists; a live
check is a documented **manual** step: launch via `serve-kind.sh`, start a
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
- **LLM route** (AC #21): reads KIND's own persisted
  `<KING_STACK_DIR>/king/runs/settings.json` directly (read-only; no
  `king_backend` import — that would be a cross-repo dependency). Absent
  settings means KIND's default route (`anthropic`, direct/local, reported
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

## The `~/kind-apps/` on-disk contract

`kbu kind` and any sibling tool's own vendored `<tool>.kind_install`
module (each self-contained, no cross-repo import) interoperate purely
through this on-disk layout:

```
$KIND_APPS_DIR/                  # default ~/kind-apps
├── registry.json                 # {"<id>": {id, title, description, cli,
│                                  #           verify, manifests, bundle_hash,
│                                  #           installed_at, updated_at}, ...}
├── CONTEXT.md                     # union-recomposed from every registry.json entry
├── serve-kind.sh                   # generated launch wrapper
└── kbutillib-modeling/
    └── skill.md                    # this app's injected orientation prose
```

Whichever installer runs last recomposes `CONTEXT.md` as a correct
superset — installing (or uninstalling) one app never drops another's
fragment. See `src/kbutillib/kind_install.py` for the implementation and
`tests/cli/test_king.py::TestInstall::
test_install_union_recompose_keeps_other_apps_fragment` (and the matching
`TestUninstall` case) for the automated proof, using an independent
fixture bundle standing in for a sibling installer's app.

## Bundle schema (AC #14)

`src/kbutillib/kind_app/bundle.json` (shipped as package data — see
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

`src/kbutillib/kind_app/skill.md` is authored from the frozen `kbu model
--help` surface (Module B, already merged) — see
[`kbu-model-cli.md`](kbu-model-cli.md) for the CLI reference it summarizes
for the agent. It is injected into KIND sessions via `KING_CONTEXT` only
and is **not** registered with `claude-skills` (AC #22) — KIND sessions
have no `Skill` tool to invoke a registered skill with.
