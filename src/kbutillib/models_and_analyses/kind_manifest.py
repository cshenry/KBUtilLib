"""KIND manifest registration for the Models and Analyses app (CAC I1).

The app SELF-REGISTERS its KIND manifest — this is CAC invariant I1, not a
convenience. :func:`register_manifest` is called on ``serve`` (unless
``--no-king``) and is ALSO invoked from ``kbu kind install`` so an install-time
registration path exists; both write the SAME file through this one module.

Where the manifest goes (binding, corrected 2026-09-24)
-------------------------------------------------------
The manifest is a REAL FILE at::

    $KING_PLUGINS_DIR/<APP_ID>.json      when $KING_PLUGINS_DIR is set
    ~/kind-apps/plugins/<APP_ID>.json    otherwise

and NEVER at a path derived from ``$KING_STATE``. The superseded formula
``$KING_STATE/kind-apps/plugins`` resolved to a DOUBLED ``~/kind-apps/kind-apps/
plugins`` on the pod (where ``$KING_STATE`` already ends in ``kind-apps``), so
:func:`resolve_manifest_dir` here does NOT reuse
:func:`kbutillib.models_and_analyses.service.resolve_app_state_dir`. It reads
``$KING_PLUGINS_DIR`` first because that is the variable KIND's
``_resolve_plugins()`` (``king_backend/config.py``) reads first.

Never write into, and never commit in, ``king/plugins/``. KIND is consume-only.

The union symlink farm (binding, gotcha G3/G25)
-----------------------------------------------
``_resolve_plugins()`` REPLACES the plugin directory rather than unioning it:
pointing ``$KING_PLUGINS_DIR`` at ``~/kind-apps/plugins`` HIDES every plugin
KIND ships unless each upstream manifest under ``$KING_ROOT/king/plugins/*.json``
is symlinked into the union dir first. :func:`refresh_plugin_union` does that,
without clobbering real files already present and skipping ``.disabled`` entries.

``$KING_ROOT`` is resolved for BOTH layouts by :func:`resolve_king_root`:
``~/king-stack`` on the laptop, and a flat ``$HOME`` holding the five repos on
the kbhub pod (the pod is the PRIMARY environment). Neither is hardcoded.

The identity string (CAC I4)
----------------------------
The manifest ``id``, the manifest BASENAME, the console command and the
distribution name are ONE string, taken from
:data:`kbutillib.arc_context.APP_ID`. It is never spelled a second time here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..arc_context import APP_ID
from ..koros_arc_store import CONTRACT_VERSION

#: The manifest ``type`` KIND filters on (``plugins.py:33`` keeps only
#: ``type == "app"``; a manifest without it is loaded and silently ignored).
MANIFEST_TYPE = "app"


def manifest_filename() -> str:
    """Return the manifest basename ``<APP_ID>.json`` (CAC I4).

    The basename is derived from :data:`APP_ID`, never spelled by hand, so the
    id, the filename, the console command and the distribution name stay ONE
    string.
    """
    return f"{APP_ID}.json"


def build_manifest() -> Dict[str, Any]:
    """Return the KIND manifest for this app as a plain dict.

    ``type`` is MANDATORY (``"app"``) — ``king_backend/plugins.py`` filters on
    it and a type-less manifest is loaded then silently dropped. ``id`` comes
    from :data:`APP_ID` (CAC I4). ``contract_version`` is REQUIRED from v1 (CAC
    §F) and is the integer :data:`CONTRACT_VERSION`. ``{port}`` and
    ``{proxy_path}`` are filled by KIND's launcher — they stay LITERAL here.
    ``ready_probe`` points at ``/health`` (the app serves it).
    """
    return {
        "type": MANIFEST_TYPE,
        "id": APP_ID,
        "title": "Models and Analyses",
        "description": (
            "Metabolic models and their analyses across your projects and arcs."
        ),
        "contract_version": CONTRACT_VERSION,
        "launch": {
            "cmd": [APP_ID, "serve", "{port}", "--root-path", "{proxy_path}"],
            "ready_probe": {"path": "/health", "timeout_s": 45},
        },
        "embed": "iframe",
        "port_strategy": "allocate",
        "singleton": True,
    }


def resolve_manifest_dir() -> Path:
    """Resolve the plugin-union directory the manifest is written into.

    Precedence (binding, corrected 2026-09-24):

      1. ``$KING_PLUGINS_DIR`` when set — the variable KIND's
         ``_resolve_plugins()`` reads first;
      2. otherwise ``~/kind-apps/plugins``.

    NEVER derived from ``$KING_STATE`` (that formula doubled the path on the
    pod). The environment and home directory are read at CALL time. The
    directory is created if missing.
    """
    king_plugins = os.environ.get("KING_PLUGINS_DIR")
    if king_plugins:
        plugins_dir = Path(king_plugins)
    else:
        plugins_dir = Path.home() / "kind-apps" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    return plugins_dir


def resolve_king_root(explicit: Optional[Path] = None) -> Optional[Path]:
    """Resolve ``$KING_ROOT`` — the directory CONTAINING the ``king`` checkout.

    Both deployment layouts are supported and NEITHER is hardcoded:

      * the laptop, where the ``king`` checkout lives under ``~/king-stack``;
      * the kbhub pod (the PRIMARY environment), where the five repos sit flat
        in ``$HOME``, so ``king`` is ``$HOME/king``.

    Resolution order (first whose ``king/plugins`` directory exists wins):

      1. an explicit argument;
      2. ``$KING_ROOT`` if set;
      3. ``~/king-stack``;
      4. flat ``$HOME``.

    Returns ``None`` when no layout resolves — a machine with no KIND checkout
    is normal, and the honest answer is "no upstream plugins to union".
    """
    candidates: List[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    env_root = os.environ.get("KING_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path.home() / "king-stack")
    candidates.append(Path.home())
    for root in candidates:
        if (root / "king" / "plugins").is_dir():
            return root
    return None


def refresh_plugin_union(
    plugins_dir: Optional[Path] = None,
    king_root: Optional[Path] = None,
) -> List[str]:
    """Symlink upstream KIND manifests into the union dir (binding, G3/G25).

    ``_resolve_plugins()`` REPLACES rather than unions, so pointing
    ``$KING_PLUGINS_DIR`` at our directory hides every plugin KIND ships unless
    each ``$KING_ROOT/king/plugins/*.json`` is linked in. This links them
    WITHOUT clobbering a real file already present in the union dir, and SKIPS
    ``.disabled`` entries.

    Returns the sorted basenames of the manifests newly linked (a real file, an
    already-correct symlink, or a ``.disabled`` entry is not counted). A no-op
    when ``$KING_ROOT`` does not resolve.
    """
    if plugins_dir is None:
        plugins_dir = resolve_manifest_dir()
    if king_root is None:
        king_root = resolve_king_root()
    if king_root is None:
        return []

    upstream_dir = king_root / "king" / "plugins"
    if not upstream_dir.is_dir():
        return []

    # Enumerate KIND's plugins through the KIND-install helper rather than
    # globbing here: this app module must never walk a directory itself (its
    # runs-tree layering invariant, tests/models_and_analyses/test_layering.py).
    from ..agents.kind_install import list_plugin_manifests

    linked: List[str] = []
    for src in list_plugin_manifests(upstream_dir):
        # Skip disabled entries — never resurrect a plugin the operator turned
        # off. A manifest is disabled either by a ``.disabled`` suffix or by
        # sitting next to a matching ``.disabled`` marker.
        if src.name.endswith(".disabled"):
            continue
        if src.with_suffix(src.suffix + ".disabled").exists():
            continue

        dest = plugins_dir / src.name
        if dest.is_symlink():
            # An existing symlink already pointing at the right upstream file is
            # left alone; a stale one is repointed.
            try:
                if dest.resolve() == src.resolve():
                    continue
            except OSError:
                pass
            dest.unlink()
            dest.symlink_to(src)
            linked.append(src.name)
            continue
        if dest.exists():
            # A REAL file already here (e.g. our own app manifest, or an
            # operator's hand-placed file) is NEVER clobbered.
            continue
        dest.symlink_to(src)
        linked.append(src.name)
    return linked


def write_manifest(plugins_dir: Optional[Path] = None) -> Path:
    """Write this app's KIND manifest as a real file and return its path.

    The file is ``<plugins_dir>/<APP_ID>.json`` (a REAL file, never a symlink),
    where ``plugins_dir`` defaults to :func:`resolve_manifest_dir`. Written
    with a trailing newline and sorted keys so re-registration is a byte-stable
    no-op diff.
    """
    if plugins_dir is None:
        plugins_dir = resolve_manifest_dir()
    manifest_path = plugins_dir / manifest_filename()
    content = json.dumps(build_manifest(), indent=2, sort_keys=True) + "\n"
    manifest_path.write_text(content, encoding="utf-8")
    return manifest_path


def register_manifest(
    plugins_dir: Optional[Path] = None,
    king_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Write the manifest and refresh the union symlink farm (CAC I1).

    This is the single entry point both ``serve`` and ``kbu kind install`` call.
    It writes the manifest to the union dir ($KING_PLUGINS_DIR else
    ~/kind-apps/plugins) and then links every upstream KIND manifest in beside
    it so installing this app never hides KIND's shipped plugins.

    Returns a small result dict (``manifest_path``, ``plugins_dir``,
    ``king_root``, ``linked_upstream``) so a caller can report what it did.
    """
    if plugins_dir is None:
        plugins_dir = resolve_manifest_dir()
    manifest_path = write_manifest(plugins_dir)
    linked = refresh_plugin_union(plugins_dir=plugins_dir, king_root=king_root)
    resolved_root = king_root if king_root is not None else resolve_king_root()
    return {
        "manifest_path": str(manifest_path),
        "plugins_dir": str(plugins_dir),
        "king_root": str(resolved_root) if resolved_root is not None else None,
        "linked_upstream": linked,
    }
