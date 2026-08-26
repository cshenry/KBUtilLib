"""Offline unit tests for build_cog_description_map.py.

Test strategy
-------------
Fixtures are small synthetic ``cog-20.def.tab``-shaped snippets written to
``tmp_path`` -- no test depends on a real downloaded NCBI COG release.
Covers a well-formed tab-delimited release (no header row, matching the
real NCBI file), and a release that "parses" under a naive
``line.split("\\t")`` but has fewer than the expected 3 columns per row
(column 1 = id, column 3 = description) and therefore yields zero
non-empty descriptions -- which must raise naming the source and its
expected layout rather than emit an empty dictionary.
"""

from __future__ import annotations

import pytest

from kbutillib.domains.genome.annotation.build_cog_description_map import (
    build_description_map,
)


class TestWellFormedRelease:
    def test_parses_correctly(self, tmp_path):
        cog_def_tab = tmp_path / "cog-20.def.tab"
        cog_def_tab.write_text(
            "COG0001\tH\tGlutamate-1-semialdehyde aminotransferase\tgene1\t426\tpath1\n"
            "COG0002\tE\tN-acetyl-gamma-glutamylphosphate reductase\tgene2\t340\tpath2\n",
            encoding="utf-8",
        )
        descriptions = build_description_map(cog_def_tab)
        assert descriptions == {
            "COG0001": "Glutamate-1-semialdehyde aminotransferase",
            "COG0002": "N-acetyl-gamma-glutamylphosphate reductase",
        }


class TestZeroEntryRaisesNamingSource:
    def test_rows_with_fewer_than_three_columns_raises(self, tmp_path):
        # Only 2 tab-separated columns per row -- a naive split parses
        # this cleanly (no exception from `line.split("\t")` itself) but
        # there is no column 3 to read a description from.
        cog_def_tab = tmp_path / "cog-20.def.tab"
        cog_def_tab.write_text(
            "COG0001\tH\n"
            "COG0002\tE\n",
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError) as excinfo:
            build_description_map(cog_def_tab)
        message = str(excinfo.value)
        assert "tab-delimited" in message
        assert str(cog_def_tab) in message

    def test_empty_file_raises(self, tmp_path):
        cog_def_tab = tmp_path / "cog-20.def.tab"
        cog_def_tab.write_text("", encoding="utf-8")
        with pytest.raises(RuntimeError, match="tab-delimited"):
            build_description_map(cog_def_tab)
