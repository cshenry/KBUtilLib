"""Backward-compatibility shim — thermo_utils.

The canonical implementation has moved to::

    kbutillib.domains.thermo.thermo_utils

All public names are re-exported here so existing code continues to work
without any modifications.
"""

from kbutillib.domains.thermo.thermo_utils import (  # noqa: F401
    ThermoUtils,
    ThermoUtilsImpl,
)

__all__ = [
    "ThermoUtils",
    "ThermoUtilsImpl",
]
