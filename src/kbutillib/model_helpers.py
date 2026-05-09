"""Canonical model helper functions.

Consolidates duplicated helpers from ``kb_model_utils``, ``ms_biochem_utils``,
and ``thermo_utils`` into one authoritative implementation.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional, Tuple

from .compartments import compartment_types

logger = logging.getLogger(__name__)


def _parse_id(object_or_id: Any) -> Tuple[str, Optional[str], Optional[str]]:
    """Parse a compound/reaction ID to extract base ID, compartment, and index.

    Supports three notation styles:
    - Bracket notation: ``"adp[c]"``, ``"h[e]"``, ``"cpd00001[c]"``
    - Underscore notation: ``"cpd01024_c0"``, ``"rxn00001_c"``
    - Plain ID: ``"cpd00001"`` (returns compartment ``None``)

    This is the canonical implementation that replaces the triplicate copies
    in ``kb_model_utils._parse_id``, ``ms_biochem_utils._parse_id``, and
    ``thermo_utils._parse_id``.

    Args:
        object_or_id: Either a string ID or an object with an ``.id`` attribute.

    Returns:
        Tuple of ``(base_id, compartment, index)`` where *compartment* is a
        single-letter code (or ``None``) and *index* is a digit string (or ``None``).
    """
    if isinstance(object_or_id, str):
        obj_id = object_or_id
    else:
        obj_id = object_or_id.id

    # Try bracket notation first (e.g., "adp[c]" or "h[e]")
    bracket_match = re.search(r"(.+)\[([a-zA-Z]+)(\d*)\]$", obj_id)
    if bracket_match:
        baseid = bracket_match[1]
        compartment = bracket_match[2]
        index = bracket_match[3] if bracket_match[3] else None
        # Normalize compartment to single-letter code
        lower_comp = compartment.lower()
        if lower_comp in compartment_types:
            compartment = compartment_types[lower_comp]
        else:
            logger.warning("Compartment type '%s' not recognized in bracket notation.", compartment)
        return (baseid, compartment, index)

    # Try underscore notation (e.g., "cpd01024_c0")
    underscore_match = re.search(r"(.+)_([a-zA-Z]+)(\d*)$", obj_id)
    if underscore_match:
        baseid = underscore_match[1]
        compartment = underscore_match[2]
        index = underscore_match[3] if underscore_match[3] else None
        lower_comp = compartment.lower()
        if lower_comp in compartment_types:
            compartment = compartment_types[lower_comp]
        else:
            # If compartment not recognized, re-attach to base_id
            logger.warning(
                "Compartment type '%s' not recognized. Re-adding to base ID.",
                compartment,
            )
            baseid = baseid + "_" + compartment
            compartment = None
        return (baseid, compartment, index)

    # Also check object.compartment attribute
    if hasattr(object_or_id, "compartment") and object_or_id.compartment:
        return (obj_id, object_or_id.compartment, None)

    # No compartment found
    return (obj_id, None, None)


def _check_and_convert_model(model: Any) -> Any:
    """Ensure *model* is an ``MSModelUtil`` instance.

    If *model* is already an ``MSModelUtil``, returns it unchanged.
    Otherwise wraps it.

    Args:
        model: A COBRApy ``Model`` or ``MSModelUtil`` instance.

    Returns:
        ``MSModelUtil`` wrapping the model.
    """
    from modelseedpy.core.msmodelutl import MSModelUtil

    if not isinstance(model, MSModelUtil):
        model = MSModelUtil(model)
    return model
