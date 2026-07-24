"""Backward-compat shim — real implementation at kbutillib.agents.king_install."""
from kbutillib.agents.king_install import *  # noqa: F401,F403
from kbutillib.agents.king_install import (  # noqa: F401
    BundleError,
    compose_context,
    detect_versions,
    install,
    load_bundle,
    uninstall,
)
