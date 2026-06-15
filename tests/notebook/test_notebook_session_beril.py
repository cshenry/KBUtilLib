"""BERIL augmentation — Module 3 confirm-and-test.

Asserts that NotebookSession.for_notebook() anchors .kbcache/ beside a
util.py in a BERIL-style project layout (projects/<id>/notebooks/<nb>/)
with NO kbu-project.toml or other org files present, and that the Manifest
provenance reads (what_writes / what_reads / stale) work correctly in that
environment.

No source behaviour is changed by this module.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kbutillib.notebook.session import NotebookSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_beril_tree(base: Path, project_id: str, nb_name: str) -> Path:
    """Create a BERIL-style project tree and return the path to util.py.

    Layout::

        <base>/
          projects/
            <project_id>/
              notebooks/
                <nb_name>/
                  util.py        ← returned path

    Deliberately contains NO kbu-project.toml or any other org/run-state
    files anywhere in the tree.
    """
    nb_dir = base / "projects" / project_id / "notebooks" / nb_name
    nb_dir.mkdir(parents=True, exist_ok=True)
    util_py = nb_dir / "util.py"
    util_py.write_text("# BERIL utility notebook\n")
    return util_py


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBerilSessionAnchor:
    """for_notebook() places .kbcache/ beside the util.py (sibling)."""

    def test_kbcache_is_sibling_of_util_py(self, tmp_path: Path):
        project_id = "beril-project-42"
        util_py = _make_beril_tree(tmp_path, project_id, "analysis")

        session = NotebookSession.for_notebook(
            str(util_py), project_name=project_id
        )
        try:
            expected = util_py.parent / ".kbcache"
            assert session.kbcache_dir == expected, (
                f"Expected .kbcache at {expected}, got {session.kbcache_dir}"
            )
            assert session.kbcache_dir.exists(), ".kbcache directory must be created"
        finally:
            session.close()

    def test_no_kbu_project_toml_anywhere(self, tmp_path: Path):
        """Sanity-check: the tree must not contain kbu-project.toml."""
        project_id = "beril-project-42"
        util_py = _make_beril_tree(tmp_path, project_id, "analysis")

        toml_files = list(tmp_path.rglob("kbu-project.toml"))
        assert toml_files == [], (
            "BERIL fixture must have no kbu-project.toml; found: "
            + ", ".join(str(p) for p in toml_files)
        )

        # Session construction must still succeed without that file
        session = NotebookSession.for_notebook(
            str(util_py), project_name=project_id
        )
        session.close()

    def test_notebook_name_derived_from_stem(self, tmp_path: Path):
        """for_notebook sets notebook_name to the stem of the supplied file."""
        util_py = _make_beril_tree(tmp_path, "proj-99", "run_fba")
        session = NotebookSession.for_notebook(str(util_py), project_name="proj-99")
        try:
            assert session.notebook_name == "util"
        finally:
            session.close()

    def test_kbcache_not_at_project_root(self, tmp_path: Path):
        """Confirm .kbcache is NOT placed at the project or repo root."""
        project_id = "beril-nested"
        util_py = _make_beril_tree(tmp_path, project_id, "my_nb")
        project_root = tmp_path / "projects" / project_id

        session = NotebookSession.for_notebook(str(util_py), project_name=project_id)
        try:
            # .kbcache must NOT be at the project root or tmp_path root
            assert session.kbcache_dir != project_root / ".kbcache"
            assert session.kbcache_dir != tmp_path / ".kbcache"
            # It must be beside util.py
            assert session.kbcache_dir == util_py.parent / ".kbcache"
        finally:
            session.close()


class TestBerilManifestProvenance:
    """Manifest what_writes / what_reads / stale work with no org files present."""

    @pytest.fixture
    def beril_session(self, tmp_path: Path):
        """A session anchored at a BERIL notebooks/<nb>/util.py with no kbu-project.toml."""
        util_py = _make_beril_tree(tmp_path, "beril-exp-01", "proteomics")
        session = NotebookSession.for_notebook(
            str(util_py), project_name="beril-exp-01"
        )
        yield session
        session.close()

    def test_what_writes_returns_write_records(self, beril_session: NotebookSession):
        beril_session.cache.save("prot_data", {"protein_a": 1.5, "protein_b": 2.0})

        writes = beril_session.manifest.what_writes("prot_data")
        assert len(writes) >= 1, "Expected at least one write record"
        assert all(w.op == "write" for w in writes)

    def test_what_reads_returns_read_records(self, beril_session: NotebookSession):
        beril_session.cache.save("prot_data", {"protein_a": 1.5})
        beril_session.cache.load("prot_data")

        reads = beril_session.manifest.what_reads("prot_data")
        assert len(reads) >= 1, "Expected at least one read record"
        assert all(r.op == "read" for r in reads)

    def test_what_writes_empty_for_unknown_key(self, beril_session: NotebookSession):
        assert beril_session.manifest.what_writes("nonexistent") == []

    def test_what_reads_empty_for_unknown_key(self, beril_session: NotebookSession):
        assert beril_session.manifest.what_reads("nonexistent") == []

    def test_stale_detects_outdated_object(self, beril_session: NotebookSession):
        """stale() flags a derived object when its input is updated."""
        session = beril_session

        # Write input
        session.cache.save("raw_intensities", [1.0, 2.0, 3.0])

        # Write derived object that declares the input
        @session.cache.cached("normalised", inputs=["raw_intensities"])
        def normalise():
            return [0.1, 0.2, 0.3]

        normalise()

        # Not stale yet
        stale_ids = [o.id for o in session.manifest.stale()]
        assert "normalised" not in stale_ids

        # Refresh the input — derived object becomes stale
        time.sleep(0.01)
        session.cache.save("raw_intensities", [10.0, 20.0, 30.0])

        stale_ids = [o.id for o in session.manifest.stale()]
        assert "normalised" in stale_ids

    def test_stale_empty_when_no_inputs_declared(self, beril_session: NotebookSession):
        """Objects with no declared inputs are never stale."""
        beril_session.cache.save("standalone", {"result": 42})
        stale_objs = beril_session.manifest.stale()
        assert all(o.id != "standalone" for o in stale_objs)

    def test_manifest_works_without_org_files(self, tmp_path: Path):
        """End-to-end: full manifest cycle with zero org/run-state files in tree."""
        project_id = "beril-full-e2e"
        util_py = _make_beril_tree(tmp_path, project_id, "screening")

        # Confirm tree is free of org files
        for pattern in ("kbu-project.toml", "*.toml", "*.yaml", "*.json"):
            found = list(tmp_path.rglob(pattern))
            assert found == [], f"Unexpected file matching {pattern!r}: {found}"

        session = NotebookSession.for_notebook(str(util_py), project_name=project_id)
        try:
            # Write and read
            session.cache.save("hits", [{"gene": "geneA", "score": 0.9}])
            session.cache.load("hits")

            # Provenance reads
            writes = session.manifest.what_writes("hits")
            reads = session.manifest.what_reads("hits")
            stale = session.manifest.stale()

            assert len(writes) >= 1
            assert len(reads) >= 1
            assert stale == []  # single root object, never stale
        finally:
            session.close()
