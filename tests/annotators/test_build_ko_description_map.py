"""Offline unit tests for build_ko_description_map.py.

Test strategy
-------------
All fixtures are small synthetic KEGG ``ko`` flat-file snippets written to
``tmp_path`` -- no test depends on a real downloaded KEGG release. Covers
a well-formed 109.1-style (``SYMBOL``-field) release, a well-formed
90.1-style (``NAME``-field, CRLF) release -- the release-layout flip this
module's docstring calls out as the trap build_ko_function_map.py already
guards and this module reuses -- and a release that parses cleanly but
yields zero non-empty descriptions, which must raise naming the detected
layout rather than emit an empty dictionary.
"""

from __future__ import annotations

import pytest

from kbutillib.domains.genome.annotation.build_ko_description_map import (
    build_description_map,
)


class TestSymbolFieldLayout:
    """KEGG >=109.1-style: symbol in SYMBOL, description in NAME."""

    def test_parses_correctly(self, tmp_path):
        ko_file = tmp_path / "ko"
        ko_file.write_text(
            "ENTRY       K00001                      KO\n"
            "SYMBOL      E1.1.1.1, adh\n"
            "NAME        alcohol dehydrogenase [EC:1.1.1.1]\n"
            "///\n"
            "ENTRY       K00002                      KO\n"
            "SYMBOL      AKR1A1, adh\n"
            "NAME        alcohol dehydrogenase (NADP+) [EC:1.1.1.2]\n"
            "///\n",
            encoding="utf-8",
        )
        layout, descriptions = build_description_map(ko_file)
        assert layout.startswith("SYMBOL-field")
        assert descriptions == {
            "K00001": "alcohol dehydrogenase [EC:1.1.1.1]",
            "K00002": "alcohol dehydrogenase (NADP+) [EC:1.1.1.2]",
        }


class TestNameFieldLayoutWithCrlf:
    """KEGG <=90.1-style: symbol in NAME, description in DEFINITION, CRLF endings."""

    def test_parses_correctly(self, tmp_path):
        ko_file = tmp_path / "ko"
        content = (
            "ENTRY       K00001                      KO\r\n"
            "NAME        adh\r\n"
            "DEFINITION  alcohol dehydrogenase\r\n"
            "///\r\n"
            "ENTRY       K00002                      KO\r\n"
            "NAME        AKR1A1\r\n"
            "DEFINITION  alcohol dehydrogenase (NADP+)\r\n"
            "///\r\n"
        )
        ko_file.write_bytes(content.encode("utf-8"))
        layout, descriptions = build_description_map(ko_file)
        assert layout.startswith("NAME-field")
        assert descriptions == {
            "K00001": "alcohol dehydrogenase",
            "K00002": "alcohol dehydrogenase (NADP+)",
        }


class TestZeroEntryRaisesNamingLayout:
    def test_symbol_present_but_no_name_field_raises(self, tmp_path):
        # SYMBOL field is populated (detects SYMBOL-field layout) but NAME
        # -- where that layout expects the description -- never appears
        # anywhere in the release. A parser hard-coded to SYMBOL-field
        # would parse this cleanly and match nothing; must raise instead.
        ko_file = tmp_path / "ko"
        ko_file.write_text(
            "ENTRY       K00001                      KO\n"
            "SYMBOL      adh\n"
            "///\n"
            "ENTRY       K00002                      KO\n"
            "SYMBOL      AKR1A1\n"
            "///\n",
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError) as excinfo:
            build_description_map(ko_file)
        message = str(excinfo.value)
        assert "SYMBOL-field" in message
        assert str(ko_file) in message

    def test_file_with_no_entry_records_raises(self, tmp_path):
        ko_file = tmp_path / "ko"
        ko_file.write_text("this is not a KEGG flat file at all\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="zero parsed ENTRY records"):
            build_description_map(ko_file)
