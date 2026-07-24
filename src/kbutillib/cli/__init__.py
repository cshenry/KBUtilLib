"""kbutillib.cli — backward-compat shim.

The full CLI implementation has moved to ``kbutillib.interfaces.cli`` (WP17).
This shim re-exports everything from the new location so that existing code
using ``from kbutillib.cli import main`` or ``from kbutillib.cli import X``
continues to work unchanged.
"""

from __future__ import annotations

# Re-export the entire public surface of the new location.
from kbutillib.interfaces.cli import *  # noqa: F401, F403
from kbutillib.interfaces.cli import (  # noqa: F401
    beril_cmd,
    bootstrap_command,
    buildplan_cmd,
    cap_cmd,
    doctor_command,
    harness_cmd,
    init_command,
    init_notebook_cmd,
    jobdaemon_cmd,
    jobs_cmd,
    king_cmd,
    main,
    migrate_cmd,
    model_cmd,
    new_capability_cmd,
    new_project_command,
    notebook_cmd,
    notebook_init_cmd,
    researchos_cmd,
    session_cmd,
    set_cmd,
    subproject_cmd,
    update_command,
)
