"""kbutillib.researchos.manager — shim re-exporting from canonical location.

Canonical source: ``kbutillib.agents.researchos.manager``
"""

from kbutillib.agents.researchos.manager import *  # noqa: F401, F403
from kbutillib.agents.researchos.manager import (  # noqa: F401
    _NAME_PATTERN,
    ResearchOSProject,
    ResearchOSProjectInfo,
    _git_run,
    _open_cursor_workspace,
    _validate_name,
)
