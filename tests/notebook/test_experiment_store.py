"""Tests for ExperimentStore — register Sample/Computation/ExternalDataset, parents lineage."""

import pytest

from kbutillib.notebook.schema.experiment import Computation, ExternalDataset, Sample
from kbutillib.notebook.schema.media import Media
from kbutillib.notebook.session import NotebookSession


class TestRegisterSample:
    def test_register_and_get(self, tmp_session: NotebookSession):
        store = tmp_session.experiments
        sample = Sample(
            id="s1",
            media=Media(id="glucose"),
            strains={"wt": 1.0},
            replicates=["rep1", "rep2"],
        )
        exp = store.register_sample(sample)
        assert exp.id == "s1"
        assert exp.kind == "sample"

        retrieved = store.get("s1")
        assert retrieved.id == "s1"
        assert retrieved.kind == "sample"
        assert isinstance(retrieved.payload, Sample)
        assert retrieved.payload.strains == {"wt": 1.0}

    def test_register_sample_idempotent(self, tmp_session: NotebookSession):
        store = tmp_session.experiments
        sample = Sample(id="s2", media=Media(id="glucose"), strains={"wt": 1.0})
        store.register_sample(sample)
        # Re-register should not raise
        store.register_sample(sample)
        items = store.list(kind="sample")
        ids = [e.id for e in items]
        assert ids.count("s2") == 1


class TestRegisterComputation:
    def test_register_and_get(self, tmp_session: NotebookSession):
        store = tmp_session.experiments
        comp = Computation(
            id="c1",
            model_ref="iML1515",
            media=Media(id="glucose"),
            parameters={"objective": "biomass"},
        )
        exp = store.register_computation(comp)
        assert exp.kind == "computation"

        retrieved = store.get("c1")
        assert isinstance(retrieved.payload, Computation)
        assert retrieved.payload.model_ref == "iML1515"


class TestRegisterExternalDataset:
    def test_register_and_get(self, tmp_session: NotebookSession):
        store = tmp_session.experiments
        ext = ExternalDataset(
            id="e1",
            source="literature",
            citation="Smith et al. 2024",
            organism="E. coli",
        )
        exp = store.register_external(ext)
        assert exp.kind == "external"

        retrieved = store.get("e1")
        assert isinstance(retrieved.payload, ExternalDataset)
        assert retrieved.payload.citation == "Smith et al. 2024"


class TestParentsLineage:
    def test_single_parent(self, tmp_session: NotebookSession):
        store = tmp_session.experiments
        sample = Sample(id="parent_s", media=Media(id="glucose"), strains={"wt": 1.0})
        store.register_sample(sample)

        comp = Computation(id="child_c", model_ref="model1", media=Media(id="glucose"))
        exp = store.register_computation(comp, parents=("parent_s",))
        assert exp.parents == ["parent_s"]

        retrieved = store.get("child_c")
        assert "parent_s" in retrieved.parents

    def test_multiple_parents(self, tmp_session: NotebookSession):
        store = tmp_session.experiments
        s1 = Sample(id="p1", media=Media(id="glucose"), strains={"wt": 1.0})
        s2 = Sample(id="p2", media=Media(id="glucose"), strains={"ko": 1.0})
        store.register_sample(s1)
        store.register_sample(s2)

        comp = Computation(id="derived", model_ref="m1", media=Media(id="glucose"))
        exp = store.register_computation(comp, parents=("p1", "p2"))
        retrieved = store.get("derived")
        assert set(retrieved.parents) == {"p1", "p2"}

    def test_parents_updated_on_re_register(self, tmp_session: NotebookSession):
        store = tmp_session.experiments
        s1 = Sample(id="pp1", media=Media(id="glucose"), strains={"wt": 1.0})
        s2 = Sample(id="pp2", media=Media(id="glucose"), strains={"wt": 1.0})
        store.register_sample(s1)
        store.register_sample(s2)

        comp = Computation(id="child", model_ref="m", media=Media(id="glucose"))
        store.register_computation(comp, parents=("pp1",))
        # Re-register with different parents
        store.register_computation(comp, parents=("pp2",))
        retrieved = store.get("child")
        assert retrieved.parents == ["pp2"]


class TestExperimentList:
    def test_list_all(self, tmp_session: NotebookSession):
        store = tmp_session.experiments
        store.register_sample(
            Sample(id="ls1", media=Media(id="glucose"), strains={"wt": 1.0})
        )
        store.register_computation(
            Computation(id="lc1", model_ref="m", media=Media(id="glucose"))
        )
        all_exps = store.list()
        assert len(all_exps) >= 2

    def test_list_by_kind(self, tmp_session: NotebookSession):
        store = tmp_session.experiments
        store.register_sample(
            Sample(id="flt1", media=Media(id="glucose"), strains={"wt": 1.0})
        )
        store.register_computation(
            Computation(id="flt2", model_ref="m", media=Media(id="glucose"))
        )
        samples = store.list(kind="sample")
        assert all(e.kind == "sample" for e in samples)


class TestExperimentGetMissing:
    def test_get_missing(self, tmp_session: NotebookSession):
        with pytest.raises(KeyError, match="not found"):
            tmp_session.experiments.get("nonexistent")
