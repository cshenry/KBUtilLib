"""Canonical compartment types mapping and normalization.

This is the single source of truth for compartment name/code mappings,
replacing duplicate definitions that previously existed in
``ms_biochem_utils.py`` and ``model_standardization_utils.py``.
"""

from __future__ import annotations

# Complete mapping from ms_biochem_utils.py (the broader version).
compartment_types: dict[str, str] = {
    "cytosol": "c",
    "extracellar": "e",
    "extracellular": "e",
    "extraorganism": "e",
    "periplasm": "p",
    "membrane": "m",
    "mitochondria": "m",
    "environment": "e",
    "env": "e",
    "c": "c",
    "c0": "c",
    "p": "p",
    "p0": "p",
    "e": "e",
    "e0": "e",
    "m": "m",
    "m0": "m",
}


def normalize_compartment(compartment: str) -> str:
    """Normalize a compartment identifier to its canonical single-letter code.

    Args:
        compartment: A compartment name or code (e.g. ``"cytosol"``, ``"c0"``, ``"e"``).

    Returns:
        Single-letter compartment code, or the original string if unrecognized.
    """
    return compartment_types.get(compartment.lower(), compartment)
