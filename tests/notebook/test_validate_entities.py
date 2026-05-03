"""Tests for session.validate_entities() — Phase 2 entity resolution validation."""

from datetime import datetime, timezone

import pytest

from kbutillib.notebook.schema.entity import EntityKind, EntityRef
from kbutillib.notebook.schema.experiment import Computation, Sample
from kbutillib.notebook.schema.media import Media
from kbutillib.notebook.schema.strain import Mutation, Strain
from kbutillib.notebook.session import NotebookSession


class TestValidateEntities:
    def test_validate_entities_clean_repo(self, tmp_session: NotebookSession):
        """Empty catalog should produce ok=True."""
        report = tmp_session.validate_entities()
        assert report.ok
        assert report.checked_experiments == 0
        assert report.checked_strains == 0
        assert report.checked_vectors == 0
        assert report.checked_entity_refs == 0

    def test_validate_entities_missing_namespace(self, tmp_session: NotebookSession):
        """Strain with parent_genome not in cache -> missing_namespace."""
        strain = Strain(
            id="ko1",
            parent_genome="nonexistent_genome",
            mutations=[
                Mutation(
                    kind="knockout",
                    target=EntityRef(kind=EntityKind.GENE, id="g1", namespace="nonexistent_genome"),
                )
            ],
        )
        tmp_session.strains.register(strain)

        report = tmp_session.validate_entities()
        assert not report.ok
        assert report.checked_strains == 1
        ns_issues = [i for i in report.issues if i.kind == "missing_namespace"]
        assert len(ns_issues) == 1
        assert "nonexistent_genome" in ns_issues[0].detail

    def test_validate_entities_missing_entity(self, tmp_session: NotebookSession):
        """Gene ID not found in namespace blob -> missing_entity."""
        # Save a genome blob with features g1, g2
        tmp_session.cache.save(
            "ecoli_genome",
            {"features": [{"id": "g1"}, {"id": "g2"}]},
        )
        strain = Strain(
            id="ko1",
            parent_genome="ecoli_genome",
            mutations=[
                Mutation(
                    kind="knockout",
                    target=EntityRef(kind=EntityKind.GENE, id="g3", namespace="ecoli_genome"),
                )
            ],
        )
        tmp_session.strains.register(strain)

        report = tmp_session.validate_entities()
        assert not report.ok
        missing = [i for i in report.issues if i.kind == "missing_entity"]
        assert len(missing) == 1
        assert "g3" in missing[0].detail

    def test_validate_entities_resolved_clean(self, tmp_session: NotebookSession):
        """Gene ID found in namespace blob -> ok, checked_entity_refs incremented."""
        tmp_session.cache.save(
            "ecoli_genome",
            {"features": [{"id": "g1"}, {"id": "g2"}]},
        )
        strain = Strain(
            id="ko1",
            parent_genome="ecoli_genome",
            mutations=[
                Mutation(
                    kind="knockout",
                    target=EntityRef(kind=EntityKind.GENE, id="g1", namespace="ecoli_genome"),
                )
            ],
        )
        tmp_session.strains.register(strain)

        report = tmp_session.validate_entities()
        assert report.ok
        assert report.checked_entity_refs >= 1

    def test_validate_entities_missing_parent_experiment(self, tmp_session: NotebookSession):
        """Sample whose parents list a nonexistent experiment -> missing_parent_experiment."""
        sample = Sample(
            id="s1",
            media=Media(id="glucose"),
            strains={"wt": 1.0},
        )
        # Register the experiment without parents first
        tmp_session.experiments.register_sample(sample)

        # Manually insert a bogus parent link (bypass FK by disabling enforcement)
        conn = tmp_session._get_catalog().conn
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO experiment_parents (child_id, parent_id) VALUES (?, ?)",
            ("s1", "nonexistent_id"),
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

        report = tmp_session.validate_entities()
        assert not report.ok
        parent_issues = [i for i in report.issues if i.kind == "missing_parent_experiment"]
        assert len(parent_issues) == 1
        assert "nonexistent_id" in parent_issues[0].detail

    def test_validate_entities_missing_derived_sample(self, tmp_session: NotebookSession):
        """Computation.derived_from_sample references nonexistent experiment -> missing_derived_sample."""
        comp = Computation(
            id="c1",
            model_ref="iML1515",
            media=Media(id="glucose"),
            derived_from_sample="bogus",
        )
        tmp_session.experiments.register_computation(comp)

        report = tmp_session.validate_entities()
        assert not report.ok
        ds_issues = [i for i in report.issues if i.kind == "missing_derived_sample"]
        assert len(ds_issues) == 1
        assert "bogus" in ds_issues[0].detail

    def test_validate_entities_derived_sample_wrong_kind(self, tmp_session: NotebookSession):
        """derived_from_sample points at a Computation (not Sample) -> wrong_kind."""
        # Register a computation as the "derived from" target
        other_comp = Computation(
            id="c_target",
            model_ref="iML1515",
            media=Media(id="glucose"),
        )
        tmp_session.experiments.register_computation(other_comp)

        # Register a computation that references c_target as derived_from_sample
        comp = Computation(
            id="c1",
            model_ref="iML1515",
            media=Media(id="glucose"),
            derived_from_sample="c_target",
        )
        tmp_session.experiments.register_computation(comp)

        report = tmp_session.validate_entities()
        assert not report.ok
        wk_issues = [i for i in report.issues if i.kind == "wrong_kind"]
        assert len(wk_issues) == 1
        assert "sample" in wk_issues[0].detail
