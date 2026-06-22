"""Tests for kbutillib.cli.registry_reader — read-only ranked candidate lookup."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kbutillib.cli.registry_reader import rank_candidates


# ── fixtures ──────────────────────────────────────────────────────────────────


def _write_registry(aia_root: Path, projects: dict) -> None:
    """Write a minimal project_registry.yaml into *aia_root*/state/."""
    state_dir = aia_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {"version": 2, "projects": projects}
    with open(state_dir / "project_registry.yaml", "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh)


# ── rank_candidates ────────────────────────────────────────────────────────────


class TestRankCandidates:
    def test_returns_empty_when_aia_root_none(self) -> None:
        """When aia_root is None, return [] without raising."""
        result = rank_candidates("ModelingLOE", aia_root=None)
        assert result == []

    def test_returns_empty_when_registry_missing(self, tmp_path: Path) -> None:
        """When project_registry.yaml is absent, return [] without raising."""
        aia_root = tmp_path / "AIAssistant"
        aia_root.mkdir()
        (aia_root / "state").mkdir()
        # No registry file written
        result = rank_candidates("ModelingLOE", aia_root=aia_root)
        assert result == []

    def test_returns_empty_when_registry_unreadable(self, tmp_path: Path) -> None:
        """When registry is not valid YAML, return [] without raising."""
        aia_root = tmp_path / "AIAssistant"
        state_dir = aia_root / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "project_registry.yaml").write_text(
            "this: is: invalid: yaml: :::", encoding="utf-8"
        )
        result = rank_candidates("ModelingLOE", aia_root=aia_root)
        assert result == []

    def test_close_title_ranks_first(self, tmp_path: Path) -> None:
        """A title close to one entry causes that entry to rank first."""
        aia_root = tmp_path / "AIAssistant"
        _write_registry(aia_root, {
            "modelingloe": {"name": "ModelingLOE", "type": "project"},
            "adp1notebooks": {"name": "ADP1Notebooks", "type": "project"},
            "aiassistant": {"name": "AIAssistant", "type": "project"},
        })
        results = rank_candidates("ModelingLOE", aia_root=aia_root)
        assert len(results) > 0
        assert results[0]["project_id"] == "modelingloe"

    def test_all_results_have_required_keys(self, tmp_path: Path) -> None:
        """Each result dict has project_id, name, score."""
        aia_root = tmp_path / "AIAssistant"
        _write_registry(aia_root, {
            "proj1": {"name": "Project One"},
            "proj2": {"name": "Project Two"},
        })
        results = rank_candidates("something", aia_root=aia_root)
        for r in results:
            assert "project_id" in r
            assert "name" in r
            assert "score" in r
            assert isinstance(r["score"], float)

    def test_unrelated_title_returns_results_without_error(self, tmp_path: Path) -> None:
        """An unrelated title still returns results (just all low-scored)."""
        aia_root = tmp_path / "AIAssistant"
        _write_registry(aia_root, {
            "genome-annotation": {"name": "GenomeAnnotation"},
            "flux-analysis": {"name": "FluxAnalysis"},
        })
        results = rank_candidates("zzzzunrelated", aia_root=aia_root)
        # Should not raise; returns all entries (capped at limit)
        assert isinstance(results, list)
        assert len(results) == 2

    def test_limit_is_respected(self, tmp_path: Path) -> None:
        """rank_candidates returns at most `limit` results."""
        aia_root = tmp_path / "AIAssistant"
        projects = {f"proj{i}": {"name": f"Project {i}"} for i in range(20)}
        _write_registry(aia_root, projects)
        results = rank_candidates("project", aia_root=aia_root, limit=5)
        assert len(results) <= 5

    def test_query_overrides_local_title(self, tmp_path: Path) -> None:
        """When query is provided, similarity is computed against query."""
        aia_root = tmp_path / "AIAssistant"
        _write_registry(aia_root, {
            "adp1notebooks": {"name": "ADP1Notebooks"},
            "modelingloe": {"name": "ModelingLOE"},
        })
        # Local title is unrelated; query targets adp1
        results = rank_candidates("something unrelated", aia_root=aia_root, query="ADP1")
        assert len(results) > 0
        assert results[0]["project_id"] == "adp1notebooks"

    def test_scores_descending_order(self, tmp_path: Path) -> None:
        """Results are sorted by score descending."""
        aia_root = tmp_path / "AIAssistant"
        _write_registry(aia_root, {
            "modelingloe": {"name": "ModelingLOE"},
            "unrelated-xyz": {"name": "Unrelated XYZ"},
        })
        results = rank_candidates("ModelingLOE", aia_root=aia_root)
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_empty_registry_returns_empty(self, tmp_path: Path) -> None:
        """An empty projects dict returns []."""
        aia_root = tmp_path / "AIAssistant"
        _write_registry(aia_root, {})
        results = rank_candidates("anything", aia_root=aia_root)
        assert results == []
