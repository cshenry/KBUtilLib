"""kbu CLI — KBUtilLib developer CLI."""

from __future__ import annotations

import click

from .beril import beril_cmd
from .bootstrap import bootstrap_command
from .buildplan import buildplan_cmd
from .init import doctor_command, init_command
from .init_notebook import init_notebook_cmd
from .jobdaemon import jobdaemon_cmd
from .jobs import jobs_cmd
from .migrate import migrate_cmd
from .new_project import new_project_command
from .notebook import notebook_cmd
from .session import session_cmd
from .subproject import subproject_cmd
from .update import update_command


@click.group()
@click.version_option()
def main() -> None:
    """kbu -- KBUtilLib developer CLI."""


main.add_command(beril_cmd, name="beril")
main.add_command(bootstrap_command, name="bootstrap")
main.add_command(buildplan_cmd, name="buildplan")
main.add_command(doctor_command, name="doctor")
main.add_command(init_command, name="init")
main.add_command(init_notebook_cmd, name="init-notebook")
main.add_command(jobs_cmd, name="jobs")
main.add_command(jobdaemon_cmd, name="jobdaemon")
main.add_command(migrate_cmd, name="migrate")
main.add_command(new_project_command, name="new-project")
main.add_command(notebook_cmd, name="notebook")
main.add_command(session_cmd, name="session")
main.add_command(subproject_cmd, name="subproject")
main.add_command(update_command, name="update")
