"""kbutillib.core — lightweight foundation (no heavy optional deps).

Public API exported from this package:

* :class:`~kbutillib.core.errors.BackendUnavailableError` — canonical error class.
* :class:`~kbutillib.core.errors.CapabilityError` — base registry error.
* :class:`~kbutillib.core.errors.CapabilityNotFound` — unknown capability name.
* :class:`~kbutillib.core.errors.KBUtilLibError` — root exception base class.
* :class:`~kbutillib.core.registry.CapabilitySpec` — per-capability descriptor.
* :class:`~kbutillib.core.registry.CapabilityRegistry` — store + filter + status.
* :func:`~kbutillib.core.registry.get_registry` — global registry singleton accessor.
* :func:`~kbutillib.core.capability.capability` — ``@capability`` decorator.
* :func:`~kbutillib.core.capability.register_all` — collector + registry binder.
* :func:`~kbutillib.core.capability.collect_capabilities` — introspect single object.
* :func:`~kbutillib.core.config.load_config` — load + validate ``config.yaml``.
* :class:`~kbutillib.core.config.Config` — top-level pydantic config model.
* :class:`~kbutillib.core.base_utils.BaseUtils` — base class for all utility modules.
* :class:`~kbutillib.core.shared_env_utils.SharedEnvUtils` — shared environment + config.
* :class:`~kbutillib.core.dependency_manager.DependencyManager` — external dep paths.

No heavy dependencies (rdkit, fastapi, mcp, …) are imported here.  Pydantic v2
is allowed and expected to be present.
"""

from kbutillib.core.base_utils import BaseUtils
from kbutillib.core.capability import (
    CapabilityDraft,
    capability,
    collect_capabilities,
    register_all,
)
from kbutillib.core.config import Config, load_config
from kbutillib.core.dependency_manager import DependencyManager
from kbutillib.core.errors import (
    BackendUnavailableError,
    CapabilityError,
    CapabilityNotFound,
    KBUtilLibError,
)
from kbutillib.core.registry import (
    CapabilityRegistry,
    CapabilitySpec,
    get_registry,
)
from kbutillib.core.shared_env_utils import SharedEnvUtils

__all__ = [
    # errors
    "KBUtilLibError",
    "BackendUnavailableError",
    "CapabilityError",
    "CapabilityNotFound",
    # registry
    "CapabilitySpec",
    "CapabilityRegistry",
    "get_registry",
    # capability decorator + collector
    "capability",
    "register_all",
    "collect_capabilities",
    "CapabilityDraft",
    # config
    "Config",
    "load_config",
    # base stack (WP10)
    "BaseUtils",
    "SharedEnvUtils",
    "DependencyManager",
]
