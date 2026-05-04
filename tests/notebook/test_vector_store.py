"""Tests for VectorStore — from_dataframe, aggregate, fold_change, get/get_many."""

import math

import pandas as pd
import pytest

from kbutillib.notebook.schema.entity import EntityKind
from kbutillib.notebook.schema.vector import VectorType
from kbutillib.notebook.session import NotebookSession


@pytest.fixture
def experiment_id(tmp_session: NotebookSession) -> str:
    """Register a sample experiment for vectors to reference."""
    from kbutillib.notebook.schema.experiment import Sample
    from kbutillib.notebook.schema.media import Media

    sample = Sample(id="exp1", media=Media(id="glucose"), strains={"wt": 1.0})
    tmp_session.experiments.register_sample(sample)
    return "exp1"


VTYPE = VectorType(domain="transcriptomics", scale="log2")


class TestFromDataFrame:
    def test_basic_ingest(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        df = pd.DataFrame(
            {"rep1": [1.0, 2.0, 3.0], "rep2": [4.0, 5.0, 6.0]},
            index=["g1", "g2", "g3"],
        )
        vec = vs.from_dataframe(
            df,
            id="v1",
            experiment_id=experiment_id,
            type=VTYPE,
            entity_kind=EntityKind.GENE,
            entity_namespace="ecoli",
        )
        assert vec.id == "v1"
        assert vec.columns == ["rep1", "rep2"]
        assert vec.content_hash

    def test_n_entities_stored_correctly(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        df = pd.DataFrame(
            {"rep1": [1.0, 2.0, 3.0, 4.0]},
            index=["g1", "g2", "g3", "g4"],
        )
        vs.from_dataframe(
            df,
            id="v_ent",
            experiment_id=experiment_id,
            type=VTYPE,
            entity_kind=EntityKind.GENE,
            entity_namespace="ecoli",
        )
        row = tmp_session._get_catalog().conn.execute(
            "SELECT n_entities FROM vectors WHERE id='v_ent'"
        ).fetchone()
        assert row["n_entities"] == 4

    def test_column_subset(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        df = pd.DataFrame(
            {"a": [1.0], "b": [2.0], "c": [3.0]},
            index=["g1"],
        )
        vec = vs.from_dataframe(
            df,
            id="v_sub",
            experiment_id=experiment_id,
            type=VTYPE,
            entity_kind=EntityKind.GENE,
            entity_namespace="ecoli",
            columns=["a", "c"],
        )
        assert vec.columns == ["a", "c"]

    def test_get_roundtrip(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        df = pd.DataFrame({"val": [10.0, 20.0]}, index=["g1", "g2"])
        vs.from_dataframe(
            df,
            id="v_rt",
            experiment_id=experiment_id,
            type=VTYPE,
            entity_kind=EntityKind.GENE,
            entity_namespace="ecoli",
        )
        vec, loaded_df = vs.get("v_rt")
        assert vec.id == "v_rt"
        assert list(loaded_df.index) == ["g1", "g2"]


class TestAggregate:
    def test_mean_aggregate(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        df1 = pd.DataFrame({"val": [2.0, 4.0]}, index=["g1", "g2"])
        df2 = pd.DataFrame({"val": [6.0, 8.0]}, index=["g1", "g2"])
        vs.from_dataframe(
            df1, id="a1", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.from_dataframe(
            df2, id="a2", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        agg_vec = vs.aggregate(["a1", "a2"], "mean", id="agg_mean")
        assert agg_vec.derivation == "mean"
        assert agg_vec.parents == ["a1", "a2"]

        _, agg_df = vs.get("agg_mean")
        assert agg_df.loc["g1", "aggregated"] == pytest.approx(4.0)
        assert agg_df.loc["g2", "aggregated"] == pytest.approx(6.0)

    def test_n_entities_in_aggregate(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        df = pd.DataFrame({"val": [1.0, 2.0, 3.0]}, index=["g1", "g2", "g3"])
        vs.from_dataframe(
            df, id="src", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.aggregate(["src"], "sum", id="agg_sum")
        row = tmp_session._get_catalog().conn.execute(
            "SELECT n_entities FROM vectors WHERE id='agg_sum'"
        ).fetchone()
        assert row["n_entities"] == 3


class TestFoldChange:
    def test_basic_fold_change(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        num_df = pd.DataFrame({"val": [8.0, 4.0]}, index=["g1", "g2"])
        den_df = pd.DataFrame({"val": [2.0, 1.0]}, index=["g1", "g2"])
        vs.from_dataframe(
            num_df, id="num", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.from_dataframe(
            den_df, id="den", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        fc = vs.fold_change("num", "den", id="fc1")
        assert fc.derivation == "log2_fold_change"
        assert fc.parents == ["num", "den"]

        _, fc_df = vs.get("fc1")
        # log2(8/2) = 2.0, log2(4/1) = 2.0
        assert fc_df.loc["g1"].values[0] == pytest.approx(2.0)
        assert fc_df.loc["g2"].values[0] == pytest.approx(2.0)

    def test_fold_change_no_log(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        num_df = pd.DataFrame({"val": [6.0]}, index=["g1"])
        den_df = pd.DataFrame({"val": [3.0]}, index=["g1"])
        vs.from_dataframe(
            num_df, id="num2", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.from_dataframe(
            den_df, id="den2", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        fc = vs.fold_change("num2", "den2", id="fc_nolog", log_base=None)
        assert fc.derivation == "fold_change"
        _, fc_df = vs.get("fc_nolog")
        assert fc_df.iloc[0, 0] == pytest.approx(2.0)

    def test_fold_change_rejects_multi_column(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        multi_df = pd.DataFrame({"a": [1.0], "b": [2.0]}, index=["g1"])
        single_df = pd.DataFrame({"val": [1.0]}, index=["g1"])
        vs.from_dataframe(
            multi_df, id="multi", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.from_dataframe(
            single_df, id="single", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        with pytest.raises(ValueError, match="single-column"):
            vs.fold_change("multi", "single", id="fc_bad")
        with pytest.raises(ValueError, match="single-column"):
            vs.fold_change("single", "multi", id="fc_bad2")


class TestFoldChangeAggregate:
    """fold_change with aggregate= auto-reduces multi-column inputs."""

    def test_aggregate_mean_both_multi(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        num_df = pd.DataFrame({"r1": [8.0, 4.0], "r2": [12.0, 6.0]}, index=["g1", "g2"])
        den_df = pd.DataFrame({"r1": [2.0, 1.0], "r2": [4.0, 3.0]}, index=["g1", "g2"])
        vs.from_dataframe(
            num_df, id="agg_num", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.from_dataframe(
            den_df, id="agg_den", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        fc = vs.fold_change("agg_num", "agg_den", id="fc_agg", aggregate="mean")
        assert fc.derivation == "log2_fold_change"
        # Intermediate vectors created
        num_agg_vec = vs.metadata("fc_agg__num_agg")
        assert num_agg_vec.derivation == "mean"
        assert num_agg_vec.parents == ["agg_num"]
        den_agg_vec = vs.metadata("fc_agg__den_agg")
        assert den_agg_vec.derivation == "mean"
        assert den_agg_vec.parents == ["agg_den"]
        # FC parents point to the intermediate vectors
        assert fc.parents == ["fc_agg__num_agg", "fc_agg__den_agg"]

    def test_aggregate_median(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        num_df = pd.DataFrame({"r1": [4.0], "r2": [8.0], "r3": [16.0]}, index=["g1"])
        den_df = pd.DataFrame({"val": [1.0]}, index=["g1"])
        vs.from_dataframe(
            num_df, id="med_num", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.from_dataframe(
            den_df, id="med_den", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        fc = vs.fold_change("med_num", "med_den", id="fc_med", aggregate="median")
        _, fc_df = vs.get("fc_med")
        # median of [4, 8, 16] = 8; log2(8/1) = 3.0
        assert fc_df.iloc[0, 0] == pytest.approx(3.0)

    def test_aggregate_single_col_passthrough(self, tmp_session: NotebookSession, experiment_id: str):
        """When aggregate is set but inputs are already single-column, no intermediate is created."""
        vs = tmp_session.vectors
        num_df = pd.DataFrame({"val": [8.0]}, index=["g1"])
        den_df = pd.DataFrame({"val": [2.0]}, index=["g1"])
        vs.from_dataframe(
            num_df, id="pt_num", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.from_dataframe(
            den_df, id="pt_den", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        fc = vs.fold_change("pt_num", "pt_den", id="fc_pt", aggregate="mean")
        # No intermediate vectors since inputs were already single-column
        assert fc.parents == ["pt_num", "pt_den"]
        _, fc_df = vs.get("fc_pt")
        assert fc_df.iloc[0, 0] == pytest.approx(2.0)

    def test_aggregate_max(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        num_df = pd.DataFrame({"r1": [2.0], "r2": [8.0]}, index=["g1"])
        den_df = pd.DataFrame({"val": [1.0]}, index=["g1"])
        vs.from_dataframe(
            num_df, id="max_num", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.from_dataframe(
            den_df, id="max_den", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        fc = vs.fold_change("max_num", "max_den", id="fc_max", aggregate="max")
        _, fc_df = vs.get("fc_max")
        # max of [2, 8] = 8; log2(8/1) = 3.0
        assert fc_df.iloc[0, 0] == pytest.approx(3.0)

    def test_aggregate_still_rejects_without_flag(self, tmp_session: NotebookSession, experiment_id: str):
        """Multi-column inputs without aggregate= still raise ValueError."""
        vs = tmp_session.vectors
        multi_df = pd.DataFrame({"a": [1.0], "b": [2.0]}, index=["g1"])
        single_df = pd.DataFrame({"val": [1.0]}, index=["g1"])
        vs.from_dataframe(
            multi_df, id="noagg_m", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.from_dataframe(
            single_df, id="noagg_s", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        with pytest.raises(ValueError, match="single-column"):
            vs.fold_change("noagg_m", "noagg_s", id="fc_noagg")


class TestGetMany:
    def test_get_many(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        df1 = pd.DataFrame({"val": [1.0, 2.0]}, index=["g1", "g2"])
        df2 = pd.DataFrame({"val": [3.0, 4.0]}, index=["g1", "g2"])
        vs.from_dataframe(
            df1, id="m1", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.from_dataframe(
            df2, id="m2", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        wide = vs.get_many(["m1", "m2"])
        assert "m1:val" in wide.columns
        assert "m2:val" in wide.columns
        assert len(wide) == 2

    def test_get_many_empty(self, tmp_session: NotebookSession):
        wide = tmp_session.vectors.get_many([])
        assert wide.empty


class TestVectorGetMissing:
    def test_get_missing_raises(self, tmp_session: NotebookSession):
        with pytest.raises(KeyError, match="not found"):
            tmp_session.vectors.get("nonexistent")


class TestVectorDelete:
    def test_delete(self, tmp_session: NotebookSession, experiment_id: str):
        vs = tmp_session.vectors
        df = pd.DataFrame({"val": [1.0]}, index=["g1"])
        vs.from_dataframe(
            df, id="to_del", experiment_id=experiment_id,
            type=VTYPE, entity_kind=EntityKind.GENE, entity_namespace="ecoli",
        )
        vs.delete("to_del")
        with pytest.raises(KeyError):
            vs.get("to_del")

    def test_delete_missing_raises(self, tmp_session: NotebookSession):
        with pytest.raises(KeyError):
            tmp_session.vectors.delete("nope")
