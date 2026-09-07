"""Shared click group that resolves retired command names silently.

Used by the top-level ``kbu`` group and by subgroups that renamed a verb.
Aliases resolve but are never listed in ``--help`` -- the new name is the
only one advertised -- so a rename does not break a caller's muscle memory,
a script, or a skill that has not been re-synced yet.

Lives in its own module rather than in ``interfaces/cli/__init__``: subgroups
need it, and ``__init__`` imports the subgroups, so importing it from there
would be a cycle.
"""

from __future__ import annotations

import click


class AliasedGroup(click.Group):
    """Click group resolving the retired names in its ``aliases`` map."""

    #: retired name -> current name. Set on each subclass.
    aliases: dict[str, str] = {}

    def get_command(self, ctx: click.Context, cmd_name: str):
        return super().get_command(ctx, self.aliases.get(cmd_name, cmd_name))
