"""Tests for kbutillib.koros_arc_store (read-only KOROS runs-tree access).

Covers the runs-root resolution chain, PROVENANCE.json parsing (including the
common empty-collections case), zero-arc projects, lineage preservation, and
defensive handling of unparseable provenance during enumeration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbutillib.koros_arc_store import (
    ArcProvenance,
    ArcRecord,
    KorosArcStore,
    ProjectRecord,
    RecordNotFound,
    RunsRootResolutionError,
    parse_provenance,
    resolve_runs_root,
)
from kbutillib.koros_arc_store.records import (
    FILE_ABSENT,
    JSON_PARSE_ERROR,
    MISSING_CREATED_AT,
    MISSING_RUN_ID,
    NOT_AN_OBJECT,
)

# ── fixture helpers ──────────────────────────────────────────────────────────

# A minimal but realistic PROVENANCE.json matching the live shape: required
# run_id/created_at present, inputs/tool_versions/compute_targets EMPTY (the
# common case on all live arcs).
EMPTY_COLLECTIONS_PROVENANCE = {
    "run_id": "run-0001",
    "run_name": "first run",
    "created_at": "2026-01-02T03:04:05.678901Z",
    "created_by": "someone",
    "inputs": [],
    "tool_versions": {},
    "compute_targets": [],
    "role": "main",
}


def _write_arc(runs_root: Path, project: str, slug: str, provenance) -> Path:
    """Create <runs_root>/<project>/arcs/<slug>/ and optionally write PROVENANCE.json.

    ``provenance`` may be a dict (written as JSON), a str (written verbatim, for
    malformed-JSON tests), or None (no PROVENANCE.json written).
    """
    arc_dir = runs_root / project / "arcs" / slug
    arc_dir.mkdir(parents=True, exist_ok=True)
    if provenance is not None:
        content = provenance if isinstance(provenance, str) else json.dumps(provenance)
        (arc_dir / "PROVENANCE.json").write_text(content, encoding="utf-8")
    return arc_dir


# ── resolve_runs_root: the resolution chain ──────────────────────────────────


class TestResolveRunsRoot:
    def test_explicit_runs_root_wins(self, tmp_path, monkeypatch):
        # Even with env vars set, an explicit argument takes priority.
        monkeypatch.setenv("KOROS_HOME", str(tmp_path / "other"))
        monkeypatch.setenv("KING_KOROS_RUNS", str(tmp_path / "yetanother"))
        explicit = tmp_path / "explicit_runs"
        explicit.mkdir()
        assert resolve_runs_root(explicit) == explicit
        # str form works too
        assert resolve_runs_root(str(explicit)) == explicit

    def test_explicit_runs_root_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KOROS_HOME", raising=False)
        monkeypatch.delenv("KING_KOROS_RUNS", raising=False)
        with pytest.raises(RunsRootResolutionError):
            resolve_runs_root(tmp_path / "does-not-exist")

    def test_koros_home_is_workspace_root_runs_from_subdir(self, tmp_path, monkeypatch):
        # $KOROS_HOME is the WORKSPACE ROOT; runs live at $KOROS_HOME/runs.
        workspace = tmp_path / "workspace"
        runs = workspace / "runs"
        runs.mkdir(parents=True)
        monkeypatch.setenv("KOROS_HOME", str(workspace))
        monkeypatch.delenv("KING_KOROS_RUNS", raising=False)
        resolved = resolve_runs_root()
        assert resolved == runs
        # Not the workspace root itself.
        assert resolved != workspace

    def test_koros_home_without_runs_subdir_falls_through(self, tmp_path, monkeypatch):
        # $KOROS_HOME set but no runs/ subdir → fall through to $KING_KOROS_RUNS.
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        king_runs = tmp_path / "king_runs"
        king_runs.mkdir()
        monkeypatch.setenv("KOROS_HOME", str(workspace))
        monkeypatch.setenv("KING_KOROS_RUNS", str(king_runs))
        assert resolve_runs_root() == king_runs

    def test_king_koros_runs_used_when_koros_home_absent(self, tmp_path, monkeypatch):
        king_runs = tmp_path / "king_runs"
        king_runs.mkdir()
        monkeypatch.delenv("KOROS_HOME", raising=False)
        monkeypatch.setenv("KING_KOROS_RUNS", str(king_runs))
        assert resolve_runs_root() == king_runs

    def test_raises_when_nothing_resolves(self, monkeypatch):
        monkeypatch.delenv("KOROS_HOME", raising=False)
        monkeypatch.delenv("KING_KOROS_RUNS", raising=False)
        # No hardcoded fallback: with nothing set, resolution RAISES.
        with pytest.raises(RunsRootResolutionError):
            resolve_runs_root()

    def test_koros_store_delegates_to_resolution(self, tmp_path, monkeypatch):
        runs = tmp_path / "runs"
        runs.mkdir()
        monkeypatch.delenv("KOROS_HOME", raising=False)
        monkeypatch.delenv("KING_KOROS_RUNS", raising=False)
        store = KorosArcStore(runs_root=runs)
        assert store.runs_root == runs
        # And with nothing supplied, constructing raises the same named error.
        with pytest.raises(RunsRootResolutionError):
            KorosArcStore()


# ── contract guardrails (grep-verifiable in source, asserted here too) ────────


class TestContractGuardrails:
    def test_no_kind_koros_runs_reference_in_source(self):
        src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "kbutillib"
            / "koros_arc_store"
        )
        for py in src.glob("*.py"):
            assert "KIND_KOROS_RUNS" not in py.read_text(encoding="utf-8"), py

    def test_no_king_backend_import_in_source(self):
        src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "kbutillib"
            / "koros_arc_store"
        )
        for py in src.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            assert "king_backend" not in text, py

    def test_no_hardcoded_dropbox_science_fallback(self):
        src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "kbutillib"
            / "koros_arc_store"
        )
        for py in src.glob("*.py"):
            assert "Dropbox/Science" not in py.read_text(encoding="utf-8"), py


# ── parse_provenance ─────────────────────────────────────────────────────────


class TestParseProvenance:
    def test_empty_collections_parse_as_valid(self):
        # THE COMMON CASE: inputs empty list, tool_versions empty dict.
        prov, reason = parse_provenance(EMPTY_COLLECTIONS_PROVENANCE)
        assert reason is None
        assert isinstance(prov, ArcProvenance)
        assert prov.run_id == "run-0001"
        assert prov.created_at == "2026-01-02T03:04:05.678901Z"
        assert prov.inputs == []
        assert prov.tool_versions == {}
        assert prov.compute_targets == []

    def test_missing_run_id_is_invalid(self):
        data = dict(EMPTY_COLLECTIONS_PROVENANCE)
        del data["run_id"]
        prov, reason = parse_provenance(data)
        assert prov is None
        assert reason == MISSING_RUN_ID

    def test_missing_created_at_is_invalid(self):
        data = dict(EMPTY_COLLECTIONS_PROVENANCE)
        del data["created_at"]
        prov, reason = parse_provenance(data)
        assert prov is None
        assert reason == MISSING_CREATED_AT

    def test_non_object_is_invalid(self):
        prov, reason = parse_provenance(["not", "an", "object"])
        assert prov is None
        assert reason == NOT_AN_OBJECT

    def test_unknown_keys_preserved_in_raw(self):
        data = dict(EMPTY_COLLECTIONS_PROVENANCE)
        data["some_future_field"] = {"nested": 1}
        data["another"] = "x"
        prov, reason = parse_provenance(data)
        assert reason is None
        assert prov.raw == {"some_future_field": {"nested": 1}, "another": "x"}
        # Documented keys never leak into raw.
        assert "run_id" not in prov.raw
        assert "inputs" not in prov.raw

    def test_lineage_fields_preserved(self):
        data = dict(EMPTY_COLLECTIONS_PROVENANCE)
        data["role"] = "leg"
        data["leg_of"] = "run-main-42"
        data["parent"] = "run-parent-7"
        prov, reason = parse_provenance(data)
        assert reason is None
        assert prov.role == "leg"
        assert prov.leg_of == "run-main-42"
        assert prov.parent == "run-parent-7"


# ── enumeration: projects and arcs ───────────────────────────────────────────


class TestEnumeration:
    def test_list_projects_ordered_by_name(self, tmp_path):
        (tmp_path / "zeta").mkdir()
        (tmp_path / "alpha").mkdir()
        (tmp_path / "mid").mkdir()
        store = KorosArcStore(runs_root=tmp_path)
        names = [p.name for p in store.list_projects()]
        assert names == ["alpha", "mid", "zeta"]
        assert all(isinstance(p, ProjectRecord) for p in store.list_projects())

    def test_project_without_arcs_dir_enumerates_zero_arcs(self, tmp_path):
        # A project directory with NO arcs/ subdirectory: zero arcs, never raise.
        bare = tmp_path / "bare_project"
        bare.mkdir()
        store = KorosArcStore(runs_root=tmp_path)
        project = store.get_project("bare_project")
        assert project.arc_count == 0
        assert store.list_arcs("bare_project") == []

    def test_empty_arcs_dir_indistinguishable_from_missing(self, tmp_path):
        # A project with an EMPTY arcs/ dir also reports arc_count == 0.
        with_empty_arcs = tmp_path / "empty_arcs_project"
        (with_empty_arcs / "arcs").mkdir(parents=True)
        store = KorosArcStore(runs_root=tmp_path)
        assert store.get_project("empty_arcs_project").arc_count == 0

    def test_list_arcs_ordered_by_slug(self, tmp_path):
        _write_arc(tmp_path, "proj", "z-arc", EMPTY_COLLECTIONS_PROVENANCE)
        _write_arc(tmp_path, "proj", "a-arc", EMPTY_COLLECTIONS_PROVENANCE)
        _write_arc(tmp_path, "proj", "m-arc", EMPTY_COLLECTIONS_PROVENANCE)
        store = KorosArcStore(runs_root=tmp_path)
        slugs = [a.slug for a in store.list_arcs("proj")]
        assert slugs == ["a-arc", "m-arc", "z-arc"]

    def test_arc_count_matches_listed_arcs(self, tmp_path):
        _write_arc(tmp_path, "proj", "arc1", EMPTY_COLLECTIONS_PROVENANCE)
        _write_arc(tmp_path, "proj", "arc2", EMPTY_COLLECTIONS_PROVENANCE)
        store = KorosArcStore(runs_root=tmp_path)
        assert store.get_project("proj").arc_count == 2

    def test_slug_is_verbatim_case_sensitive(self, tmp_path):
        _write_arc(tmp_path, "proj", "MixedCase-Arc", EMPTY_COLLECTIONS_PROVENANCE)
        store = KorosArcStore(runs_root=tmp_path)
        arc = store.read_arc("proj", "MixedCase-Arc")
        assert arc.slug == "MixedCase-Arc"

    def test_leg_arc_lineage_survives_enumeration(self, tmp_path):
        data = dict(EMPTY_COLLECTIONS_PROVENANCE)
        data["role"] = "leg"
        data["leg_of"] = "run-main-42"
        data["parent"] = "run-parent-7"
        _write_arc(tmp_path, "proj", "leg-arc", data)
        store = KorosArcStore(runs_root=tmp_path)
        arc = store.read_arc("proj", "leg-arc")
        assert arc.valid
        assert arc.provenance.role == "leg"
        assert arc.provenance.leg_of == "run-main-42"
        assert arc.provenance.parent == "run-parent-7"

    def test_unparseable_provenance_does_not_break_enumeration(self, tmp_path):
        # A malformed arc must not break the listing of its siblings.
        _write_arc(tmp_path, "proj", "good", EMPTY_COLLECTIONS_PROVENANCE)
        _write_arc(tmp_path, "proj", "broken", "{ this is not valid json ")
        store = KorosArcStore(runs_root=tmp_path)
        arcs = {a.slug: a for a in store.list_arcs("proj")}
        assert set(arcs) == {"good", "broken"}
        assert arcs["good"].valid is True
        assert arcs["broken"].valid is False
        assert arcs["broken"].invalid_reason == JSON_PARSE_ERROR
        assert arcs["broken"].provenance is None

    def test_absent_provenance_file_marks_invalid(self, tmp_path):
        _write_arc(tmp_path, "proj", "no-prov", None)  # no PROVENANCE.json
        store = KorosArcStore(runs_root=tmp_path)
        arc = store.read_arc("proj", "no-prov")
        assert arc.valid is False
        assert arc.invalid_reason == FILE_ABSENT
        assert arc.provenance is None


# ── read_arc / get_project error semantics ───────────────────────────────────


class TestLookupErrors:
    def test_read_arc_missing_arc_raises_record_not_found(self, tmp_path):
        (tmp_path / "proj" / "arcs").mkdir(parents=True)
        store = KorosArcStore(runs_root=tmp_path)
        with pytest.raises(RecordNotFound):
            store.read_arc("proj", "nope")

    def test_get_project_missing_raises_record_not_found(self, tmp_path):
        store = KorosArcStore(runs_root=tmp_path)
        with pytest.raises(RecordNotFound):
            store.get_project("nope")

    def test_list_arcs_missing_project_raises(self, tmp_path):
        store = KorosArcStore(runs_root=tmp_path)
        with pytest.raises(RecordNotFound):
            store.list_arcs("nope")

    def test_read_arc_returns_record_never_none_on_invalid(self, tmp_path):
        # read_arc on an invalid arc returns a record, never None, never raises.
        _write_arc(
            tmp_path, "proj", "invalid", {"created_at": "2026-01-01T00:00:00.000000Z"}
        )
        store = KorosArcStore(runs_root=tmp_path)
        arc = store.read_arc("proj", "invalid")
        assert isinstance(arc, ArcRecord)
        assert arc.valid is False
        assert arc.invalid_reason == MISSING_RUN_ID
