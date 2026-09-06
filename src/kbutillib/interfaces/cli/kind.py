"""``kbu kind`` — self-install this repo's KING apps into a local KING.

Thin CLI facade over the vendored ``kbutillib.agents.king_install`` module
(see its module docstring for the on-disk ``$KING_APPS_DIR`` contract and
the Acceptance Criteria it implements). Reads this repo's OWN packaged
bundles — no cross-repo dependency; a checkout/install of KBUtilLib alone
is enough to run ``kbu kind install``.

This repo ships more than one bundle, selected with ``--app``:

* ``modeling`` (``src/kbutillib/king_app/``, id ``kbutillib-modeling``) —
  the metabolic-modeling verbs.
* ``wake`` (``src/kbutillib/king_app_wake/``, id ``persistentai-wake``) —
  firing a triggered wake at luna/miles over the ``persistentai`` trigger
  rail.

With no ``--app``, every bundle is acted on. That is deliberate: the point
of ``kbu kind install`` is "make what this repo offers visible to KING",
and a per-app default would silently ship a subset.

Exit codes on ``status`` follow the CRAFT CLI convention already used
elsewhere in this CLI (``kbu doctor``): 0 = green
(all-ok), 1 = amber (partial -- CLI missing), 2 = red (composed but
broken). With several apps, the WORST color across them wins — a green
app does not mask a red sibling.

``--json`` always emits a LIST of per-app result objects, one per app
acted on, even when ``--app`` selects exactly one. It was a bare object
before this repo shipped a second bundle; a shape that changes with the
number of apps is the kind of thing that breaks a caller months later.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click

from ...agents import king_install

#: CLI ``--app`` name -> package subdirectory holding that bundle.
APPS: dict[str, str] = {
    "modeling": "king_app",
    "wake": "king_app_wake",
}

#: Worst-wins ordering for the aggregate ``status`` exit code.
_COLOR_RANK = {"green": 0, "amber": 1, "red": 2}
_EXIT_CODE = {"green": 0, "amber": 1, "red": 2}


def _package_root() -> Path:
    """The ``kbutillib`` package root (interfaces/cli -> interfaces -> kbutillib)."""
    return Path(__file__).resolve().parent.parent.parent


def _bundle_dirs(app: Optional[str]) -> list[Path]:
    """Bundle directories to act on: just *app*, or all of them if None."""
    names = [app] if app else list(APPS)
    return [_package_root() / APPS[name] for name in names]


def _apps_dir_opt(apps_dir: Optional[str]) -> Optional[Path]:
    return Path(apps_dir).expanduser() if apps_dir else None


def _app_option(fn):
    return click.option(
        "--app",
        type=click.Choice(sorted(APPS)),
        default=None,
        help="Act on one app only (default: all of this repo's apps).",
    )(fn)


def _apps_dir_option(fn):
    return click.option(
        "--apps-dir",
        default=None,
        metavar="PATH",
        help="Override $KING_APPS_DIR (default: ~/king-apps).",
    )(fn)


def _json_option(fn):
    return click.option(
        "--json", "as_json", is_flag=True, default=False, help="Emit JSON."
    )(fn)


@click.group("kind")
def kind_cmd() -> None:
    """Self-install this repo's KING apps into a local KING (`~/king-apps/`)."""


@kind_cmd.command("install")
@_app_option
@_apps_dir_option
@_json_option
def install_cmd(app: Optional[str], apps_dir: Optional[str], as_json: bool) -> None:
    """Compose this repo's bundle(s) into $KING_APPS_DIR and wire serve-king.sh.

    Idempotent: re-running on an unchanged bundle is a no-op diff. Never
    fails just because the app's CLI isn't found on PATH -- reports it
    instead.
    """
    resolved = _apps_dir_opt(apps_dir)
    results = [
        king_install.install(bundle_dir, apps_dir=resolved)
        for bundle_dir in _bundle_dirs(app)
    ]

    if as_json:
        click.echo(json.dumps(results))
        return

    for result in results:
        click.echo(
            f"kbu kind install: id={result['id']}  apps_dir={result['apps_dir']}"
        )
        click.echo(
            f"  [{'PASS' if result['cli_on_path'] else 'FAIL'}] "
            f"cli-on-path: {result['cli']}"
        )
        if result["verify_probe_ran"]:
            click.echo(
                f"  [{'PASS' if result['verify_probe_ok'] else 'FAIL'}] verify-probe"
            )
        if not result["cli_on_path"]:
            click.echo(
                f"  ! '{result['cli']}' not found on PATH. Install it: "
                f"pip install -e <repo containing {result['cli']}>"
            )
        click.echo(f"  changed: {result['changed']}")
    click.echo(f"  CONTEXT.md: {results[-1]['context_md']}")
    click.echo(f"  serve-king.sh: {results[-1]['serve_script']}")


@kind_cmd.command("uninstall")
@_app_option
@_apps_dir_option
@_json_option
def uninstall_cmd(app: Optional[str], apps_dir: Optional[str], as_json: bool) -> None:
    """Remove this repo's app id(s) from $KING_APPS_DIR and recompose CONTEXT.md.

    A no-op for any app that was never installed.
    """
    resolved = _apps_dir_opt(apps_dir)
    results = []
    for bundle_dir in _bundle_dirs(app):
        bundle = king_install.load_bundle(bundle_dir)["bundle"]
        results.append(king_install.uninstall(bundle["id"], apps_dir=resolved))

    if as_json:
        click.echo(json.dumps(results))
        return

    for result in results:
        click.echo(
            f"kbu kind uninstall: id={result['id']}  removed={result['removed']}"
        )


@kind_cmd.command("status")
@_app_option
@_apps_dir_option
@_json_option
def status_cmd(app: Optional[str], apps_dir: Optional[str], as_json: bool) -> None:
    """Static install-health check per app: green/amber/red (see module docs).

    No live-orientation API exists to confirm a running KING session
    actually sees the injected text -- that is a documented manual step.
    """
    resolved = _apps_dir_opt(apps_dir)
    results = [
        king_install.status(bundle_dir, apps_dir=resolved)
        for bundle_dir in _bundle_dirs(app)
    ]

    if as_json:
        click.echo(json.dumps(results))
    else:
        for result in results:
            click.echo(f"kbu kind status: id={result['id']}  [{result['color']}]")
            click.echo(
                f"  [{'PASS' if result['cli_on_path'] else 'FAIL'}] cli-on-path"
            )
            click.echo(
                f"  [{'PASS' if result['verify_probe_ok'] else 'FAIL'}] verify-probe"
            )
            click.echo(
                f"  [{'PASS' if result['context_has_header'] else 'FAIL'}] "
                "context-md-has-header"
            )
            click.echo(
                f"  [{'PASS' if result['king_context_wired'] else 'FAIL'}] "
                "king-context-wired"
            )
            if result["remediation"]:
                click.echo(f"  ! {result['remediation']}")
            click.echo(f"  versions: {result['versions']}")
            click.echo(
                f"  llm-route: {result['llm_route']} "
                f"({'local' if result['llm_route_is_local'] else 'NON-LOCAL'})"
            )
            if result["llm_route_warning"]:
                click.echo(f"  ! {result['llm_route_warning']}")

    worst = max(results, key=lambda r: _COLOR_RANK[r["color"]])["color"]
    sys.exit(_EXIT_CODE[worst])
