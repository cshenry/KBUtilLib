"""kbutillib.researchos.config — shim re-exporting from canonical location.

Canonical source: ``kbutillib.agents.researchos.config``
"""

from kbutillib.agents.researchos.config import *  # noqa: F401, F403
from kbutillib.agents.researchos.config import (  # noqa: F401
    resolve_aiassistant_root,
    resolve_researchos_root,
    resolve_tooling_venv,
    set_root,
)
