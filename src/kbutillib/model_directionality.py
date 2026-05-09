"""Direction-analysis helpers extracted from multiple modules.

Consolidates direction-related logic from:
- ``kb_model_utils.model_reaction_directionality_analysis`` (undefined ``direction_conversion`` bug)
- ``ms_biochem_utils.reaction_directionality_from_bounds``
- ``ms_biochem_utils.reaction_biochem_directionality``
- ``model_standardization_utils.direction_conversion``

This module is the canonical home for all direction conversion and analysis.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Canonical direction_conversion mapping.
# Previously defined in model_standardization_utils.py.
direction_conversion: dict[str, str] = {
    "": "-",
    "forward": ">",
    "reverse": "<",
    "reversible": "=",
    "uncertain": "?",
    "blocked": "B",
}


def directionality_from_bounds(reaction: Any, tol: float = 1e-9) -> str:
    """Classify directionality from a Reaction's bounds only.

    Args:
        reaction: A COBRApy ``Reaction`` object (needs ``.lower_bound`` / ``.upper_bound``).
        tol: Tolerance for treating small bounds as zero.

    Returns:
        One of ``"forward"``, ``"reverse"``, ``"reversible"``, ``"blocked"``.
    """
    lb, ub = reaction.lower_bound, reaction.upper_bound

    if abs(lb) < tol:
        lb = 0.0
    if abs(ub) < tol:
        ub = 0.0

    if lb < 0 and ub > 0:
        return "reversible"
    if lb >= 0 and ub > 0:
        return "forward"
    if lb < 0 and ub <= 0:
        return "reverse"
    return "blocked"


def biochem_directionality(reaction_id: str, biochem: Any) -> Optional[str]:
    """Determine directionality of a reaction in the ModelSEED biochemistry DB.

    Args:
        reaction_id: ModelSEED reaction ID string (e.g. ``"rxn00001"``).
        biochem: An ``MSBiochemUtilsImpl`` or compatible object with
                 ``get_reaction_by_id``.

    Returns:
        Directionality string, or ``None`` if the reaction is not found.
    """
    rxnobj = biochem.get_reaction_by_id(reaction_id)
    if rxnobj is None:
        return None
    return directionality_from_bounds(rxnobj)


def combine_directionality_signals(
    model_dir: str,
    biochem_dir: Optional[str],
    ai_dir: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Combine multiple directionality signals into a summary dict.

    Args:
        model_dir: Directionality from model bounds.
        biochem_dir: Directionality from biochemistry DB (may be ``None``).
        ai_dir: Directionality from AI curation (may be ``None``).

    Returns:
        Dictionary with keys ``"model"``, ``"biochem"``, ``"ai"``, and
        ``"combined"`` (pipe-separated converted symbols).
    """
    result: Dict[str, Optional[str]] = {
        "model": model_dir,
        "biochem": biochem_dir,
        "ai": ai_dir,
    }
    parts = [
        direction_conversion.get(model_dir, "?"),
        direction_conversion.get(biochem_dir or "", "?"),
        direction_conversion.get(ai_dir or "", "?"),
    ]
    result["combined"] = "|".join(parts)
    return result
