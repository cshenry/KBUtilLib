"""Offline unit tests for build_go_description_map.py.

Test strategy
-------------
Fixtures are small synthetic ``go-basic.obo``-shaped snippets written to
``tmp_path`` -- no test depends on a real downloaded Gene Ontology
release. Covers a well-formed stanza release (including an obsolete term
that must be skipped and a non-``[Term]`` stanza that must be ignored),
and a release that parses cleanly (``[Term]`` stanzas with ``id:`` lines
present) but yields zero non-empty, non-obsolete descriptions, which must
raise naming the source and its expected layout rather than emit an empty
dictionary.
"""

from __future__ import annotations

import pytest

from kbutillib.domains.genome.annotation.build_go_description_map import (
    build_description_map,
)


class TestWellFormedRelease:
    def test_parses_correctly(self, tmp_path):
        go_obo = tmp_path / "go-basic.obo"
        go_obo.write_text(
            "format-version: 1.2\n"
            "data-version: releases/2026-01-01\n"
            "\n"
            "[Term]\n"
            "id: GO:0000001\n"
            "name: mitochondrion inheritance\n"
            "namespace: biological_process\n"
            "\n"
            "[Term]\n"
            "id: GO:0000002\n"
            "name: mitochondrial genome maintenance\n"
            "namespace: biological_process\n"
            "\n"
            "[Term]\n"
            "id: GO:0000003\n"
            "name: obsolete reproduction\n"
            "is_obsolete: true\n"
            "\n"
            "[Typedef]\n"
            "id: part_of\n"
            "name: part of\n"
            "\n",
            encoding="utf-8",
        )
        descriptions = build_description_map(go_obo)
        assert descriptions == {
            "GO:0000001": "mitochondrion inheritance",
            "GO:0000002": "mitochondrial genome maintenance",
        }
        # Obsolete term and the [Typedef] stanza must not leak in.
        assert "GO:0000003" not in descriptions
        assert "part_of" not in descriptions


class TestZeroEntryRaisesNamingSource:
    def test_id_lines_present_but_no_name_lines_raises(self, tmp_path):
        go_obo = tmp_path / "go-basic.obo"
        go_obo.write_text(
            "[Term]\n"
            "id: GO:0000001\n"
            "namespace: biological_process\n"
            "\n"
            "[Term]\n"
            "id: GO:0000002\n"
            "namespace: biological_process\n"
            "\n",
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError) as excinfo:
            build_description_map(go_obo)
        message = str(excinfo.value)
        assert "OBO stanza format" in message
        assert str(go_obo) in message

    def test_all_terms_obsolete_raises(self, tmp_path):
        go_obo = tmp_path / "go-basic.obo"
        go_obo.write_text(
            "[Term]\n"
            "id: GO:0000001\n"
            "name: obsolete term\n"
            "is_obsolete: true\n"
            "\n",
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="OBO stanza format"):
            build_description_map(go_obo)

    def test_file_with_no_term_stanzas_raises(self, tmp_path):
        go_obo = tmp_path / "go-basic.obo"
        go_obo.write_text(
            "format-version: 1.2\ndata-version: releases/2026-01-01\n",
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="zero parsed \\[Term\\] stanzas"):
            build_description_map(go_obo)
