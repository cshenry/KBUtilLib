"""Tests for kbutillib.notebook.helpers.compartment."""
from __future__ import annotations

from kbutillib.notebook.helpers.compartment import COMPARTMENT_MAP, normalize_compartment


class TestCompartment:
    def test_normalize_known(self):
        assert normalize_compartment("c") == "cytoplasm"
        assert normalize_compartment("e0") == "extracellular"
        assert normalize_compartment("p") == "periplasm"
        assert normalize_compartment("m0") == "mitochondria"

    def test_normalize_unknown(self):
        assert normalize_compartment("x") == "x"

    def test_map_contents(self):
        assert "c" in COMPARTMENT_MAP
        assert "e" in COMPARTMENT_MAP
