"""Tests for kbutillib.notebook.helpers.fva."""
from __future__ import annotations

import pandas as pd

from kbutillib.notebook.helpers.fva import classify_fva_flux, find_significant_differences


class TestFindSignificantDifferences:
    def test_find_significant_differences(self):
        df = pd.DataFrame(
            {"minimum": [0.0, -5.0, 0.0], "maximum": [0.0, 10.0, 0.0]},
            index=["rxn1", "rxn2", "rxn3"],
        )
        result = find_significant_differences(df)
        assert len(result) == 1
        assert "rxn2" in result.index

    def test_threshold(self):
        df = pd.DataFrame(
            {"minimum": [0.0, -1e-8], "maximum": [1e-8, 1e-8]},
            index=["rxn1", "rxn2"],
        )
        result = find_significant_differences(df, threshold=1e-6)
        assert len(result) == 0


class TestClassifyFvaFlux:
    def test_blocked(self):
        assert classify_fva_flux(0.0, 0.0) == "blocked"
        assert classify_fva_flux(1e-10, -1e-10) == "blocked"

    def test_fixed_nonzero(self):
        assert classify_fva_flux(5.0, 5.0) == "fixed_nonzero"
        assert classify_fva_flux(-3.0, -3.0) == "fixed_nonzero"

    def test_variable(self):
        assert classify_fva_flux(-5.0, 10.0) == "variable"
        assert classify_fva_flux(0.0, 5.0) == "variable"
        assert classify_fva_flux(-10.0, 0.0) == "variable"

    def test_edge_near_zero(self):
        assert classify_fva_flux(0.0, 1.0) == "variable"
