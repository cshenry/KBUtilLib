"""``kbu king`` — self-install the KBUtilLib-modeling app into a local KING.

Thin CLI facade over the vendored ``kbutillib.king_install`` module (see its
module docstring for the on-disk ``$KING_APPS_DIR`` contract and the
Acceptance Criteria it implements). Reads this repo's OWN packaged bundle
(``src/kbutillib/king_app/{bundle.json,skill.md}``) — no cross-repo
dependency; a checkout/install of KBUtilLib alone is enough to run
``kbu king install``.

Exit codes on ``status`` follow the CRAFT CLI convention already used
elsewhere in this CLI (``kbu researchos``/``kbu doctor``): 0 = green
(all-ok), 1 = amber (partial -- CLI missing), 2 = red (composed but
broken).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click

from .. import king_install


def _bundle_dir() -> Path:
    """This repo's own packaged KING-app bundle directory."""
    return Path(__file__).resolve().parent.parent / "king_app"


def _apps_dir_opt(apps_dir: Optional[str]) -> Optional[Path]:
    return Path(apps_dir).expanduser() if apps_dir else None


@click.group("king")
def king_cmd() -> None:
    """Self-install the KBUtilLib-modeling app into a local KING (`~/king-apps/`)."""


@king_cmd.command("install")
@click.option(
    "--apps-dir",
    default=None,
    metavar="PATH",
    help="Override $KING_APPS_DIR (default: ~/king-apps).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def install_cmd(apps_dir: Optional[str], as_json: bool) -> None:
    """Compose this app's bundle into $KING_APPS_DIR and wire serve-king.sh.

    Idempotent: re-running on an unchanged bundle is a no-op diff. Never
    fails just because `kbu` isn't found on PATH -- reports it instead.
    """
    result = king_install.install(_bundle_dir(), apps_dir=_apps_dir_opt(apps_dir))

    if as_json:
        click.echo(json.dumps(result))
        return

    click.echo(f"kbu king install: id={result['id']}  apps_dir={result['apps_dir']}")
    click.echo(f"  [{'PASS' if result['cli_on_path'] else 'FAIL'}] cli-on-path: {result['cli']}")
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
    click.echo(f"  CONTEXT.md: {result['context_md']}")
    click.echo(f"  serve-king.sh: {result['serve_script']}")


@king_cmd.command("uninstall")
@click.option(
    "--apps-dir",
    default=None,
    metavar="PATH",
    help="Override $KING_APPS_DIR (default: ~/king-apps).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def uninstall_cmd(apps_dir: Optional[str], as_json: bool) -> None:
    """Remove this app's id from $KING_APPS_DIR and recompose CONTEXT.md.

    A no-op if the app was never installed.
    """
    bundle = king_install.load_bundle(_bundle_dir())["bundle"]
    result = king_install.uninstall(bundle["id"], apps_dir=_apps_dir_opt(apps_dir))

    if as_json:
        click.echo(json.dumps(result))
        return

    click.echo(
        f"kbu king uninstall: id={result['id']}  removed={result['removed']}"
    )


@king_cmd.command("status")
@click.option(
    "--apps-dir",
    default=None,
    metavar="PATH",
    help="Override $KING_APPS_DIR (default: ~/king-apps).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def status_cmd(apps_dir: Optional[str], as_json: bool) -> None:
    """Static install-health check: green/amber/red (see module docs).

    No live-orientation API exists to confirm a running KING session
    actually sees the injected text -- that is a documented manual step.
    """
    result = king_install.status(_bundle_dir(), apps_dir=_apps_dir_opt(apps_dir))

    if as_json:
        click.echo(json.dumps(result))
    else:
        click.echo(f"kbu king status: id={result['id']}  [{result['color']}]")
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

    exit_code = {"green": 0, "amber": 1, "red": 2}[result["color"]]
    sys.exit(exit_code)
