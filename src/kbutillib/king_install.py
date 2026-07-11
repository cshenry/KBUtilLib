"""Vendored KING self-install compose/verify logic for KBUtilLib's ``kbu king`` verbs.

**Self-contained: no cross-repo import.** This module does not import
``assistant`` (AIAssistant) or KING's own ``king_backend`` package — the only
coupling to either is the on-disk ``~/king-apps/`` contract documented below,
which any other tool's own vendored ``<tool>.king_install`` module (e.g.
AIAssistant's ``assistant.king_install``) implements independently. See
``agent-io/prds/king-integration-apps/fullprompt.md`` (Module C, Acceptance
Criteria #13-#21) in the AIAssistant repo for the binding spec this module
implements.

On-disk contract (``$KING_APPS_DIR``, default ``~/king-apps``)
----------------------------------------------------------------
::

    $KING_APPS_DIR/
    ├── registry.json      # {"<id>": {id, title, description, cli, verify,
    │                       #           manifests, bundle_hash,
    │                       #           installed_at, updated_at}, ...}
    ├── CONTEXT.md          # union-recomposed from ALL registered app dirs
    ├── serve-king.sh        # generated launch wrapper (exports KING_CONTEXT)
    └── <id>/
        └── skill.md         # this app's injected orientation prose

``CONTEXT.md`` is regenerated deterministically from the union of every
``registry.json`` entry (ids sorted lexicographically, one
``# [KING App] <title> (id: <id>)`` header per id) every time ANY app
installs or uninstalls — this is what lets independently-installed apps
(this one, AIAssistant's, and any future one) coexist without clobbering
each other's fragment, regardless of install order (AC #15).

Writes are confined to ``$KING_APPS_DIR`` and the generated
``serve-king.sh`` wrapper; this module never writes anything under KING's
own checkout (conventionally ``~/king-stack/king/``) — it only *reads* KING's
persisted LLM-route settings (best-effort, for the AC #21 warning) and
references KING's own ``scripts/serve.sh`` path inside the generated
wrapper, without touching it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── paths ────────────────────────────────────────────────────────────────


def resolve_apps_dir(explicit: Optional[str] = None) -> Path:
    """Resolve ``$KING_APPS_DIR`` (env var, else default ``~/king-apps``)."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("KING_APPS_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / "king-apps"


def resolve_king_stack_dir(explicit: Optional[str] = None) -> Path:
    """Resolve the local KING checkout root (env ``KING_STACK_DIR``, else
    default ``~/king-stack``).

    Used ONLY to point the generated ``serve-king.sh`` wrapper at KING's own
    ``scripts/serve.sh`` and (best-effort, read-only) at its persisted
    settings for the LLM-route warning -- never to write anything there.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("KING_STACK_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / "king-stack"


def now_utc_iso() -> str:
    """Return the current UTC time as ISO-8601 with a ``Z`` suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── bundle loading (package data shipped inside this repo) ─────────────────


class BundleError(ValueError):
    """Raised when a ``bundle.json``/``skill.md`` pair fails validation (AC #14)."""


def load_bundle(bundle_dir: Path) -> dict:
    """Load + schema-validate ``{bundle.json, skill.md}`` from *bundle_dir*.

    Schema (AC #14): ``{id, title, description, cli, verify:{cmd:[...],
    ok_text?}, manifests?}``; ``id``/``title``/``description``/``cli`` are
    required, ``verify``/``manifests`` are optional.

    Returns ``{"bundle": <parsed bundle.json dict>, "skill_md": <str>}``.
    """
    bundle_json_path = bundle_dir / "bundle.json"
    skill_md_path = bundle_dir / "skill.md"
    if not bundle_json_path.is_file():
        raise BundleError(f"bundle.json not found at {bundle_json_path}")
    if not skill_md_path.is_file():
        raise BundleError(f"skill.md not found at {skill_md_path}")
    try:
        bundle = json.loads(bundle_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"bundle.json is not valid JSON: {exc}") from exc
    if not isinstance(bundle, dict):
        raise BundleError("bundle.json must contain a JSON object")
    for field in ("id", "title", "description", "cli"):
        if not bundle.get(field):
            raise BundleError(f"bundle.json missing required field '{field}'")
    skill_md = skill_md_path.read_text(encoding="utf-8")
    return {"bundle": bundle, "skill_md": skill_md}


# ── verify probe (AC #14) ───────────────────────────────────────────────────


def run_verify_probe(bundle: dict) -> dict:
    """Run the bundle's ``verify`` probe, if any.  Never raises.

    "The verify probe passes when it exits 0 and (if ``ok_text`` given)
    ``ok_text`` appears in its stdout" (AC #14).  Absent CLI or a missing
    probe command are reported as state, not exceptions -- callers (``king
    install``/``king status``) must never crash on a missing hand.
    """
    cli = bundle.get("cli")
    cli_on_path = bool(cli) and shutil.which(cli) is not None
    verify = bundle.get("verify") or {}
    cmd = verify.get("cmd")

    if not cmd:
        # No probe declared: "on PATH" is the whole verification.
        return {
            "cli_on_path": cli_on_path,
            "probe_ran": False,
            "probe_ok": cli_on_path,
            "output": "",
        }
    if not cli_on_path:
        return {
            "cli_on_path": False,
            "probe_ran": False,
            "probe_ok": False,
            "output": "",
        }
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "cli_on_path": True,
            "probe_ran": True,
            "probe_ok": False,
            "output": str(exc),
        }
    ok_text = verify.get("ok_text")
    ok = proc.returncode == 0 and (ok_text is None or ok_text in proc.stdout)
    return {
        "cli_on_path": True,
        "probe_ran": True,
        "probe_ok": ok,
        "output": (proc.stdout + proc.stderr)[:2000],
    }


# ── registry.json + write-if-changed (idempotency, AC #17) ─────────────────


def _registry_path(apps_dir: Path) -> Path:
    return apps_dir / "registry.json"


def read_registry(apps_dir: Path) -> dict:
    """Read ``$KING_APPS_DIR/registry.json``; ``{}`` if absent/unparseable."""
    p = _registry_path(apps_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_if_changed(path: Path, content: str) -> bool:
    """Write *content* to *path* only if it differs from what's already there.

    Guarantees AC #17 ("re-running install on an unchanged bundle is a
    no-op diff"): an untouched file keeps its untouched mtime/content.
    Returns True iff a write happened.
    """
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def write_registry(apps_dir: Path, registry: dict) -> bool:
    content = json.dumps(registry, indent=2, sort_keys=True) + "\n"
    return _write_if_changed(_registry_path(apps_dir), content)


# ── CONTEXT.md union-recompose (AC #15) ─────────────────────────────────────


def _context_md_path(apps_dir: Path) -> Path:
    return apps_dir / "CONTEXT.md"


def compose_context(apps_dir: Path) -> bool:
    """Regenerate ``CONTEXT.md`` from the union of ALL registered app dirs.

    Deterministic: ids sorted lexicographically; each app's header is
    exactly ``# [KING App] <title> (id: <id>)``; each id appears exactly
    once.  This is what lets independently-installed apps coexist --
    whichever installer runs last recomposes a correct superset (AC #15).
    Only writes when the composed content actually changed.
    """
    registry = read_registry(apps_dir)
    sections = []
    for app_id in sorted(registry):
        entry = registry[app_id]
        skill_path = apps_dir / app_id / "skill.md"
        skill_text = (
            skill_path.read_text(encoding="utf-8") if skill_path.is_file() else ""
        )
        header = f"# [KING App] {entry.get('title', app_id)} (id: {app_id})"
        sections.append(f"{header}\n\n{skill_text.strip()}\n")
    content = "\n".join(sections)
    if content:
        content += "\n"
    return _write_if_changed(_context_md_path(apps_dir), content)


# ── serve-king.sh launch wrapper (AC #13) ───────────────────────────────────


def _serve_script_path(apps_dir: Path) -> Path:
    return apps_dir / "serve-king.sh"


def generate_serve_script(
    apps_dir: Path, king_stack_dir: Optional[Path] = None
) -> bool:
    """Generate/update ``$KING_APPS_DIR/serve-king.sh`` (AC #13).

    Exports ``KING_CONTEXT`` (always) and ``KING_PLUGINS_DIR`` (only when at
    least one registered app declares a non-empty ``manifests`` list), then
    ``exec``s KING's own ``scripts/serve.sh``.  NEVER writes anything under
    ``king_stack_dir`` itself -- only references its ``scripts/serve.sh``
    path.
    """
    king_stack_dir = king_stack_dir or resolve_king_stack_dir()
    registry = read_registry(apps_dir)
    has_manifests = any(entry.get("manifests") for entry in registry.values())
    context_path = _context_md_path(apps_dir)
    king_serve = king_stack_dir / "king" / "scripts" / "serve.sh"

    lines = [
        "#!/usr/bin/env bash",
        "# Generated by `king install` (kbutillib.king_install) -- do NOT edit",
        "# KING's own repo; this wrapper only wires env vars before delegating",
        "# to KING's own launcher. Regenerate via `kbu king install`.",
        "set -euo pipefail",
        f'export KING_CONTEXT="{context_path}"',
    ]
    if has_manifests:
        lines.append(f'export KING_PLUGINS_DIR="{apps_dir / "plugins"}"')
    lines.append(f'exec "{king_serve}" "$@"')
    lines.append("")
    content = "\n".join(lines)
    changed = _write_if_changed(_serve_script_path(apps_dir), content)
    script_path = _serve_script_path(apps_dir)
    script_path.chmod(script_path.stat().st_mode | 0o111)
    return changed


# ── install / uninstall (AC #15, #16, #17) ──────────────────────────────────


def install(
    bundle_dir: Path,
    apps_dir: Optional[Path] = None,
    king_stack_dir: Optional[Path] = None,
) -> dict:
    """Install (or idempotently re-install) this app's bundle into ``$KING_APPS_DIR``.

    Never raises for a missing CLI / failed verify probe -- reports state,
    does not crash (per Module C step 1: "Report state, don't crash.").
    """
    apps_dir = apps_dir or resolve_apps_dir()
    loaded = load_bundle(bundle_dir)
    bundle, skill_md = loaded["bundle"], loaded["skill_md"]
    app_id = bundle["id"]

    verify_state = run_verify_probe(bundle)

    app_dir = apps_dir / app_id
    skill_changed = _write_if_changed(app_dir / "skill.md", skill_md)

    registry = read_registry(apps_dir)
    bundle_hash = _sha256_text(
        json.dumps(bundle, sort_keys=True) + "\x00" + skill_md
    )
    existing = registry.get(app_id)
    registry_changed = False
    if not existing or existing.get("bundle_hash") != bundle_hash:
        now = now_utc_iso()
        registry[app_id] = {
            "id": app_id,
            "title": bundle["title"],
            "description": bundle["description"],
            "cli": bundle["cli"],
            "verify": bundle.get("verify"),
            "manifests": bundle.get("manifests"),
            "bundle_hash": bundle_hash,
            "installed_at": existing["installed_at"] if existing else now,
            "updated_at": now,
        }
        registry_changed = write_registry(apps_dir, registry)

    context_changed = compose_context(apps_dir)
    serve_changed = generate_serve_script(apps_dir, king_stack_dir)

    return {
        "id": app_id,
        "apps_dir": str(apps_dir),
        "app_dir": str(app_dir),
        "cli": bundle["cli"],
        "cli_on_path": verify_state["cli_on_path"],
        "verify_probe_ran": verify_state["probe_ran"],
        "verify_probe_ok": verify_state["probe_ok"],
        "verify_output": verify_state["output"],
        "changed": bool(
            skill_changed or registry_changed or context_changed or serve_changed
        ),
        "context_md": str(_context_md_path(apps_dir)),
        "serve_script": str(_serve_script_path(apps_dir)),
        "registry": str(_registry_path(apps_dir)),
    }


def uninstall(app_id: str, apps_dir: Optional[Path] = None) -> dict:
    """Remove *app_id*'s ``$KING_APPS_DIR/<id>/`` dir + registry entry.

    Recomposes ``CONTEXT.md`` afterward.  An already-absent id is a no-op
    (AC #16).
    """
    apps_dir = apps_dir or resolve_apps_dir()
    registry = read_registry(apps_dir)
    app_dir = apps_dir / app_id
    existed = app_id in registry or app_dir.is_dir()

    if app_id in registry:
        del registry[app_id]
        write_registry(apps_dir, registry)
    if app_dir.is_dir():
        shutil.rmtree(app_dir)

    compose_context(apps_dir)
    generate_serve_script(apps_dir)

    return {
        "id": app_id,
        "removed": existed,
        "apps_dir": str(apps_dir),
        "context_md": str(_context_md_path(apps_dir)),
    }


# ── status (static, no live-orientation API; AC #18, #20, #21) ─────────────


def _context_has_header(apps_dir: Path, title: str, app_id: str) -> bool:
    p = _context_md_path(apps_dir)
    if not p.is_file():
        return False
    header = f"# [KING App] {title} (id: {app_id})"
    return header in p.read_text(encoding="utf-8")


def _king_context_env_resolves(apps_dir: Path) -> bool:
    """Static check of AC #18's "``KING_CONTEXT`` resolves to it".

    Checked primarily against the generated ``serve-king.sh`` wrapper, not
    the calling process's own environment: ``king status`` is normally run
    from an ordinary shell (e.g. right after ``king install``, per Module
    E's orchestrator skill), not from inside a KING launch, so the
    process's live ``KING_CONTEXT`` is almost never set even when
    everything is correctly wired. A ``KING_CONTEXT`` already set correctly
    in the current process env also counts. (Mirrors
    ``assistant.king_install._serve_script_wires_context`` so both
    installers' ``status`` verbs agree on what "wired" means.)
    """
    context_path = _context_md_path(apps_dir)
    expected = f'export KING_CONTEXT="{context_path}"'
    script_path = _serve_script_path(apps_dir)
    try:
        if expected in script_path.read_text(encoding="utf-8"):
            return True
    except OSError:
        pass

    king_context = os.environ.get("KING_CONTEXT")
    if not king_context:
        return False
    try:
        return Path(king_context).expanduser().resolve() == context_path.resolve()
    except OSError:
        return False


def detect_versions() -> dict:
    """kbutillib/cobra/modelseedpy versions via a ``python -c`` import print (AC #20).

    Runs in a subprocess against ``sys.executable`` so this module stays
    lightweight (it never hard-imports cobra/modelseedpy itself); checks the
    same interpreter ``kbu`` runs under.
    """
    code = (
        "import json\n"
        "out = {}\n"
        "for pkg in ('kbutillib', 'cobra', 'modelseedpy'):\n"
        "    try:\n"
        "        mod = __import__(pkg)\n"
        "        out[pkg] = getattr(mod, '__version__', 'unknown')\n"
        "    except Exception:\n"
        "        out[pkg] = None\n"
        "print(json.dumps(out))\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
        if lines:
            return json.loads(lines[-1])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return {"kbutillib": None, "cobra": None, "modelseedpy": None}


def detect_llm_route(king_stack_dir: Optional[Path] = None) -> dict:
    """Best-effort, READ-ONLY detection of KING's chosen LLM route (AC #21).

    Self-contained: reads KING's own persisted ``runs/settings.json``
    directly (never imports ``king_backend`` -- that would be a cross-repo
    dependency).  Absent settings (the common, un-configured case) means
    KING's own default route (``"anthropic"`` -- direct/local), which we
    report as local.  ``"cborg"`` is LBNL's hosted gateway -- non-local,
    reported for a WARN, never a block (per AC #21 and the PRD's "local-only
    intended" note).
    """
    king_stack_dir = king_stack_dir or resolve_king_stack_dir()
    settings_path = king_stack_dir / "king" / "runs" / "settings.json"
    route = "anthropic"
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                route = data.get("llm_route", "anthropic")
        except (OSError, json.JSONDecodeError):
            pass
    is_local = route != "cborg"
    return {"route": route, "is_local": is_local, "settings_path": str(settings_path)}


def status(
    bundle_dir: Path,
    apps_dir: Optional[Path] = None,
    king_stack_dir: Optional[Path] = None,
) -> dict:
    """Static install-health check (AC #18) -- no live-orientation API exists.

    - green: CLI on PATH AND verify probe passes AND ``CONTEXT.md`` contains
      the app header AND ``KING_CONTEXT`` resolves to that file.
    - amber: CLI missing (remediation reported) -- whether or not composed.
    - red: CLI present but the probe fails or ``KING_CONTEXT`` isn't wired.
    """
    apps_dir = apps_dir or resolve_apps_dir()
    loaded = load_bundle(bundle_dir)
    bundle = loaded["bundle"]
    app_id = bundle["id"]

    verify_state = run_verify_probe(bundle)
    registry = read_registry(apps_dir)
    composed = app_id in registry
    context_ok = _context_has_header(apps_dir, bundle["title"], app_id)
    king_context_wired = _king_context_env_resolves(apps_dir)

    if (
        verify_state["cli_on_path"]
        and verify_state["probe_ok"]
        and context_ok
        and king_context_wired
    ):
        color = "green"
    elif not verify_state["cli_on_path"]:
        color = "amber"
    else:
        color = "red"

    remediation = None
    if not verify_state["cli_on_path"]:
        remediation = (
            f"'{bundle['cli']}' not found on PATH. Install it: "
            f"pip install -e <repo containing {bundle['cli']}>"
        )

    route_info = detect_llm_route(king_stack_dir)
    llm_route_warning = None
    if not route_info["is_local"]:
        llm_route_warning = (
            f"KING's LLM route is '{route_info['route']}' (non-local). "
            "This app is intended local-only -- consider switching back to "
            "the direct Anthropic route in KING Settings."
        )

    return {
        "id": app_id,
        "color": color,
        "cli_on_path": verify_state["cli_on_path"],
        "verify_probe_ran": verify_state["probe_ran"],
        "verify_probe_ok": verify_state["probe_ok"],
        "composed": composed,
        "context_has_header": context_ok,
        "king_context_wired": king_context_wired,
        "remediation": remediation,
        "versions": detect_versions(),
        "llm_route": route_info["route"],
        "llm_route_is_local": route_info["is_local"],
        "llm_route_warning": llm_route_warning,
    }


__all__: list[str] = [
    "BundleError",
    "compose_context",
    "detect_llm_route",
    "detect_versions",
    "generate_serve_script",
    "install",
    "load_bundle",
    "read_registry",
    "resolve_apps_dir",
    "resolve_king_stack_dir",
    "run_verify_probe",
    "status",
    "uninstall",
    "write_registry",
]
