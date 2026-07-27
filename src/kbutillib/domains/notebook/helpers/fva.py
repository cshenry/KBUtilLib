"""FVA (Flux Variability Analysis) helper utilities."""
from __future__ import annotations

import pandas as pd


def find_significant_differences(
    fva_df: pd.DataFrame,
    threshold: float = 1e-6,
) -> pd.DataFrame:
    """Filter an FVA result DataFrame to rows where min and max flux differ significantly.

    Expects columns 'minimum' and 'maximum' (standard cobra FVA output).
    """
    span = (fva_df["maximum"] - fva_df["minimum"]).abs()
    return fva_df[span > threshold].copy()


def classify_fva_flux(
    minimum: float,
    maximum: float,
    tol: float = 1e-6,
) -> str:
    """Classify flux variability for a single reaction.

    Returns one of:
        'fixed_zero'     — both bounds effectively zero
        'fixed_nonzero'  — flux is fixed at a nonzero value
        'variable'       — flux can vary (min != max)
        'blocked'        — both bounds are exactly zero (same as fixed_zero but used
                           when there is genuinely no flux possible)
    """
    min_zero = abs(minimum) <= tol
    max_zero = abs(maximum) <= tol
    span = abs(maximum - minimum)

    if min_zero and max_zero:
        return "blocked"
    elif span <= tol:
        return "fixed_nonzero"
    else:
        return "variable"
