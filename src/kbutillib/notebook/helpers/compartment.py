"""Compartment normalization utilities."""
from __future__ import annotations


COMPARTMENT_MAP: dict[str, str] = {
    "c": "cytoplasm",
    "c0": "cytoplasm",
    "e": "extracellular",
    "e0": "extracellular",
    "p": "periplasm",
    "p0": "periplasm",
    "m": "mitochondria",
    "m0": "mitochondria",
}


def normalize_compartment(compartment_id: str) -> str:
    """Map a compartment suffix/id to a canonical human-readable name.

    Returns the original id unchanged if no mapping is found.
    """
    return COMPARTMENT_MAP.get(compartment_id, compartment_id)
