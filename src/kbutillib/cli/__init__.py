"""kbu CLI — KBUtilLib developer CLI."""

from __future__ import annotations

import click

from .init_notebook import init_notebook_cmd


@click.group()
@click.version_option()
def main() -> None:
    """kbu -- KBUtilLib developer CLI."""


main.add_command(init_notebook_cmd, name="init-notebook")
