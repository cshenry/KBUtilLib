"""Tests for the Manifest API (Phase 3)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from kbutillib.notebook.schema.entity import EntityKind
from kbutillib.notebook.schema.experiment import Sample
from kbutillib.notebook.schema.manifest import AccessRecord, NotebookEntry, ObjectEntry
from kbutillib.notebook.schema.media import Media
from kbutillib.notebook.schema.vector import VectorType
from kbutillib.notebook.session import NotebookSession


@pytest.fixture
def manifest_session(tmp_path: Path):
    """Create a session with some objects and access history."""
    kbcache = tmp_path / ".kbcache"
    session = NotebookSession(
        kbcache_dir=kbcache,
        notebook_name="nb_alpha",
        project_name="test_project",
    )
    yield session
    session.close()


def test_manifest_empty_repo(manifest_session: NotebookSession):
    """Fresh session: manifest queries return empty results without error."""
    session = manifest_session

    assert session.manifest.notebooks() == []
    assert session.manifest.objects() == []
    assert session.manifest.stale() == []

    # info() raises KeyError on missing object
    with pytest.raises(KeyError):
        session.manifest.info("nothing_here")

    # what_writes / what_reads return empty lists for unknown objects
    assert session.manifest.what_writes("nothing_here") == []
    assert session.manifest.what_reads("nothing_here") == []

    # dot() should still emit a valid (but empty) DOT graph
    dot_str = session.manifest.dot()
    assert dot_str.startswith("digraph manifest {")
    assert dot_str.rstrip().endswith("}")


def test_notebooks_lists_active_notebooks(manifest_session: NotebookSession):
    """notebooks() returns entries for all notebooks that appear in the access log."""
    session = manifest_session
    session.cache.save("obj_a", {"hello": "world"})
    session.cache.load("obj_a")

    entries = session.manifest.notebooks()
    assert len(entries) == 1
    assert entries[0].name == "nb_alpha"
    assert entries[0].write_count >= 1
    assert entries[0].read_count >= 1
    assert entries[0].last_run is not None


def test_objects_lists_cache_objects(manifest_session: NotebookSession):
    """objects() returns ObjectEntry for cache + vector objects with correct
    kind/type fields. Spec: 2 cache + 1 vector → 3 entries.
    """
    session = manifest_session
    session.cache.save("data_x", [1, 2, 3])
    session.cache.save("data_y", {"key": "val"})

    # Register an experiment + vector to exercise the vector branch.
    session.experiments.register_sample(
        Sample(id="exp_v", media=Media(id="m1"), strains={"wt": 1.0})
    )
    df = pd.DataFrame({"rep1": [1.0, 2.0]}, index=["g1", "g2"])
    session.vectors.from_dataframe(
        df,
        id="vec_z",
        experiment_id="exp_v",
        type=VectorType(domain="transcriptomics", scale="log2"),
        entity_kind=EntityKind.GENE,
        entity_namespace="ecoli",
    )

    objs = session.manifest.objects()
    ids = [o.id for o in objs]
    assert "data_x" in ids
    assert "data_y" in ids
    assert "vec_z" in ids
    assert len(objs) == 3

    data_x = next(o for o in objs if o.id == "data_x")
    assert data_x.kind == "cache"
    assert data_x.type == "json"
    assert data_x.write_count >= 1

    vec_z = next(o for o in objs if o.id == "vec_z")
    assert vec_z.kind == "vector"
    # Vector type is assembled as "{type_scale}-{type_domain}"
    assert vec_z.type == "log2-transcriptomics"


def test_info_returns_single_object(manifest_session: NotebookSession):
    """info(name) returns the ObjectEntry for a specific object."""
    session = manifest_session
    session.cache.save("my_obj", "some text", type_hint="text")

    entry = session.manifest.info("my_obj")
    assert entry.id == "my_obj"
    assert entry.kind == "cache"

    with pytest.raises(KeyError):
        session.manifest.info("nonexistent")


def test_what_writes_and_reads(manifest_session: NotebookSession):
    """what_writes/what_reads return correct AccessRecords."""
    session = manifest_session
    session.cache.save("tracked", {"a": 1})
    session.cache.load("tracked")

    writes = session.manifest.what_writes("tracked")
    assert len(writes) >= 1
    assert all(w.op == "write" for w in writes)
    assert all(w.notebook == "nb_alpha" for w in writes)

    reads = session.manifest.what_reads("tracked")
    assert len(reads) >= 1
    assert all(r.op == "read" for r in reads)


def test_stale_detects_outdated_objects(manifest_session: NotebookSession):
    """stale() flags objects whose inputs have a newer created_at."""
    session = manifest_session

    # Create the "input" object first
    session.cache.save("input_obj", [1, 2, 3])

    # Create a dependent object with inputs declared via cached decorator
    @session.cache.cached("derived_obj", inputs=["input_obj"])
    def compute():
        return [4, 5, 6]

    compute()

    # At this point derived_obj is NOT stale (input_obj was created before it)
    stale_objs = session.manifest.stale()
    assert all(o.id != "derived_obj" for o in stale_objs)

    # Now update the input — making derived_obj stale
    # Need a small delay to ensure timestamp ordering
    time.sleep(0.01)
    session.cache.save("input_obj", [10, 20, 30])

    stale_objs = session.manifest.stale()
    stale_ids = [o.id for o in stale_objs]
    assert "derived_obj" in stale_ids


def test_dot_produces_valid_graphviz(manifest_session: NotebookSession):
    """dot() returns a string that starts with 'digraph' and contains object nodes."""
    session = manifest_session
    session.cache.save("parent_a", "hello")

    @session.cache.cached("child_b", inputs=["parent_a"])
    def compute():
        return "derived"

    compute()

    dot_str = session.manifest.dot()
    assert dot_str.startswith("digraph manifest {")
    assert '"parent_a"' in dot_str
    assert '"child_b"' in dot_str
    assert '"parent_a" -> "child_b"' in dot_str


def test_render_creates_manifest_notebook(manifest_session: NotebookSession, tmp_path: Path):
    """render() creates a valid .ipynb file with the spec'd cell layout."""
    session = manifest_session
    session.cache.save("some_data", {"x": 1})

    output = session.manifest.render()
    assert output.exists()
    assert output.name == "Manifest.ipynb"

    # Verify it's valid nbformat
    import nbformat

    with open(output) as f:
        nb = nbformat.read(f, as_version=4)

    # Should have markdown and code cells
    cell_types = [c.cell_type for c in nb.cells]
    assert "markdown" in cell_types
    assert "code" in cell_types

    md_sources = [c.source for c in nb.cells if c.cell_type == "markdown"]
    code_sources = [c.source for c in nb.cells if c.cell_type == "code"]

    # Title cell should mention the project name and a generation timestamp.
    title_cell = md_sources[0]
    assert "test_project" in title_cell
    assert "Generated:" in title_cell
    assert "fullprompt.md" in title_cell  # PRD link

    # Setup code cell must invoke for_notebook(__file__) so .kbcache resolves
    # next to the notebook file rather than the user's CWD.
    setup_code = code_sources[0]
    assert "NotebookSession.for_notebook(__file__)" in setup_code
    assert "manifest = session.manifest" in setup_code

    # Required section headers, in order.
    expected_headers = [
        "## Notebooks",
        "## Cache objects",
        "## Vectors",
        "## Stale objects",
        "## Dependency DAG",
    ]
    seen_idx = -1
    for header in expected_headers:
        matches = [i for i, src in enumerate(md_sources) if src.strip().startswith(header)]
        assert matches, f"Missing required section header: {header}"
        # Ensure ordering
        assert matches[0] > seen_idx, f"Section header {header!r} out of order"
        seen_idx = matches[0]

    # Required: notebooks rendered as DataFrame; cache and vectors filtered separately.
    assert any("manifest.notebooks()" in s and "DataFrame" in s for s in code_sources)
    assert any("kind == 'cache'" in s for s in code_sources)
    assert any("kind != 'vector'" in s or "kind == 'vector'" in s for s in code_sources)
    assert any("manifest.stale()" in s for s in code_sources)
    assert any("manifest.dot()" in s for s in code_sources)

    # Trailer markdown
    assert any("Re-run this notebook to refresh." in s for s in md_sources)

    # Test custom output path
    custom_path = tmp_path / "custom" / "MyManifest.ipynb"
    custom_path.parent.mkdir(parents=True, exist_ok=True)
    result = session.manifest.render(output_path=custom_path)
    assert result == custom_path
    assert custom_path.exists()


def test_dot_escapes_quotes_in_object_ids(manifest_session: NotebookSession):
    """dot() must emit a parseable graph even when object IDs contain quotes."""
    session = manifest_session
    weird_id = 'has"quote'
    session.cache.save(weird_id, "value")

    dot_str = session.manifest.dot()
    # The unescaped quote must NOT appear inside a node label.
    assert f'"{weird_id}"' not in dot_str
    # Properly escaped form must appear.
    assert 'has\\"quote' in dot_str
