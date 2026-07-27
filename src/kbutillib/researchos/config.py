"""kbutillib.researchos.config — shim re-exporting from canonical location.

Canonical source: ``kbutillib.agents.researchos.config``
"""

from kbutillib.agents.researchos.config import *  # noqa: F401, F403

# `from ... import *` skips underscore-prefixed names, but tests monkeypatch
# these module-level constants directly (e.g.
# ``monkeypatch.setattr("kbutillib.researchos.config._KBUTILLIB_DIR", ...)``),
# so re-export them explicitly to keep the shim's attribute surface complete.
from kbutillib.agents.researchos.config import (  # noqa: F401
    _DEFAULT_AIASSISTANT_ROOT,
    _DEFAULT_CONFIG_FILE,
    _DEFAULT_ROOT,
    _DEFAULT_TOOLING_VENV,
    _KBUTILLIB_DIR,
    resolve_aiassistant_root,
    resolve_researchos_root,
    resolve_tooling_venv,
    set_root,
)
