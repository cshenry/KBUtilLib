"""kbutillib.interfaces.cli — full KBUtilLib developer CLI.

This package contains all original CLI commands (moved from kbutillib.cli)
plus the WP6 capability-registry adapters.

Sub-modules
-----------
capabilities
    ``kbu cap list/info/run`` — capability registry introspection commands.
scaffold
    ``kbu new-capability`` — stub generator for new @capability-decorated methods.
"""

from __future__ import annotations

import click

# Original CLI commands (moved from kbutillib.cli by WP17)
from .beril import beril_cmd
from .bootstrap import bootstrap_command
from .buildplan import buildplan_cmd

# WP6: capability registry introspection + scaffolder
from .capabilities import cap_cmd
from .harness import harness_cmd
from .init import doctor_command, init_command
from .init_notebook import init_notebook_cmd
from .jobdaemon import jobdaemon_cmd
from .jobs import jobs_cmd
from .king import king_cmd
from .migrate import migrate_cmd
from .model import model_cmd
from .new_project import new_project_command
from .scaffold import new_capability_cmd
from .session import session_cmd
from .set_cmd import set_cmd
from .subproject import subproject_cmd
from .update import update_command
from .verab import verab_cmd


@click.group()
@click.version_option()
def main() -> None:
    """kbu -- KBUtilLib developer CLI."""


main.add_command(beril_cmd, name="beril")
main.add_command(harness_cmd, name="harness")
main.add_command(bootstrap_command, name="bootstrap")
main.add_command(buildplan_cmd, name="buildplan")
main.add_command(doctor_command, name="doctor")
main.add_command(init_command, name="init")
main.add_command(init_notebook_cmd, name="init-notebook")
main.add_command(jobs_cmd, name="jobs")
main.add_command(jobdaemon_cmd, name="jobdaemon")
main.add_command(king_cmd, name="king")
main.add_command(migrate_cmd, name="migrate")
main.add_command(model_cmd, name="model")
main.add_command(new_project_command, name="new-project")
main.add_command(session_cmd, name="session")
main.add_command(set_cmd, name="set")
main.add_command(subproject_cmd, name="subproject")
main.add_command(update_command, name="update")
# WP6: capability registry introspection + scaffolder
main.add_command(cap_cmd, name="cap")
main.add_command(new_capability_cmd, name="new-capability")

main.add_command(verab_cmd, name="verab")
