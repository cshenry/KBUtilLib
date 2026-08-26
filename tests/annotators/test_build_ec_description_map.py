"""Offline unit tests for build_ec_description_map.py.

Test strategy
-------------
Fixtures are small synthetic ExPASy ``enzyme.dat``-shaped snippets written
to ``tmp_path`` -- no test depends on a real downloaded ExPASy release.
Covers a well-formed record-block release (including a wrapped multi-line
``DE``), and a release that parses cleanly (``ID`` lines present) but
yields zero non-empty descriptions, which must raise naming the source
and its expected layout rather than emit an empty dictionary.
"""

from __future__ import annotations

import pytest

from kbutillib.domains.genome.annotation.build_ec_description_map import (
    build_description_map,
)


class TestWellFormedRelease:
    def test_parses_correctly(self, tmp_path):
        enzyme_dat = tmp_path / "enzyme.dat"
        enzyme_dat.write_text(
            "ID   1.1.1.1\n"
            "DE   Alcohol dehydrogenase.\n"
            "AN   Aldehyde reductase.\n"
            "CA   An alcohol + NAD+ = an aldehyde or ketone + NADH.\n"
            "//\n"
            "ID   1.1.1.2\n"
            "DE   Alcohol dehydrogenase\n"
            "DE   (NADP+).\n"
            "//\n",
            encoding="utf-8",
        )
        descriptions = build_description_map(enzyme_dat)
        assert descriptions == {
            "1.1.1.1": "Alcohol dehydrogenase",
            "1.1.1.2": "Alcohol dehydrogenase (NADP+)",
        }


class TestZeroEntryRaisesNamingSource:
    def test_id_lines_present_but_no_de_lines_raises(self, tmp_path):
        enzyme_dat = tmp_path / "enzyme.dat"
        enzyme_dat.write_text(
            "ID   1.1.1.1\n"
            "AN   Aldehyde reductase.\n"
            "//\n"
            "ID   1.1.1.2\n"
            "AN   Something else.\n"
            "//\n",
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError) as excinfo:
            build_description_map(enzyme_dat)
        message = str(excinfo.value)
        assert "record-block format" in message
        assert str(enzyme_dat) in message

    def test_file_with_no_id_lines_raises(self, tmp_path):
        enzyme_dat = tmp_path / "enzyme.dat"
        enzyme_dat.write_text("this is not an enzyme.dat file at all\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="zero parsed ID records"):
            build_description_map(enzyme_dat)
