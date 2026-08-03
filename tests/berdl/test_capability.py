"""Unit tests for kbutillib.domains.kbase.berdl.capability.

Pure logic, no network, no live BERDL: ``ingest`` config construction in
both source modes (DataFrame mode omits 'paths', bronze mode includes it),
create-vs-append write-mode selection given table existence, and
``BerdlCapability.load()`` refusing off-pod with actionable guidance
(pod requirement, the kbhub machine, and a concrete command). Per the PRD's
"Testing Decisions", the two transports require a live BERDL pod and are
not exercised here.

Locus is always forced via the ``force_off_pod`` / ``force_in_pod``
fixtures rather than inferred from whether ``berdl_notebook_utils``
happens to be installed on the host. These tests must behave identically
off-pod and in-pod: relying on ambient absence made them pass on a laptop,
fail in-pod, and -- because the refusal branch was then never taken --
open real Spark Connect sessions inside a suite that promises no network.
"""

import pytest

from kbutillib.domains.kbase.berdl import capability as capability_module
from kbutillib.domains.kbase.berdl.capability import (
    POD_MACHINE,
    BerdlCapability,
    BerdlLoadRefusedError,
    BerdlMembershipUnavailableError,
    berdl_notebook_utils_importable,
    build_ingest_config,
    select_write_mode,
)


@pytest.fixture
def force_off_pod(monkeypatch):
    """Make locus detection report off_pod regardless of the host machine.

    The off-pod tests must not depend on whether ``berdl_notebook_utils``
    happens to be installed where the suite runs. Relying on its ambient
    absence passes on a dev laptop but fails in-pod, and worse, lets the
    in-pod branch run for real -- which is how these tests previously
    attempted live Spark Connect sessions during a "pure logic, no
    network" suite.
    """
    monkeypatch.setattr(
        capability_module, "berdl_notebook_utils_importable", lambda: False
    )


@pytest.fixture
def force_in_pod(monkeypatch):
    """Make locus detection report in_pod regardless of the host machine."""
    monkeypatch.setattr(
        capability_module, "berdl_notebook_utils_importable", lambda: True
    )


class TestBerdlNotebookUtilsImportable:
    def test_agrees_with_find_spec(self):
        """The check must reflect real importability on whichever machine runs it.

        Asserted against ``find_spec`` rather than a hardcoded expectation,
        so this passes both off-pod (package absent) and in-pod (package
        present) without needing to be edited per locus.
        """
        import importlib.util

        expected = importlib.util.find_spec("berdl_notebook_utils") is not None
        assert berdl_notebook_utils_importable() is expected

    def test_returns_false_when_the_package_cannot_be_found(self, monkeypatch):
        """A missing package reports False."""
        monkeypatch.setattr(
            capability_module.importlib.util, "find_spec", lambda name: None
        )
        assert berdl_notebook_utils_importable() is False

    def test_returns_false_when_find_spec_raises(self, monkeypatch):
        """A malformed parent package is not importable either."""

        def _boom(name):
            raise ValueError("malformed parent package")

        monkeypatch.setattr(
            capability_module.importlib.util, "find_spec", _boom
        )
        assert berdl_notebook_utils_importable() is False

    def test_locus_reports_off_pod_when_package_not_importable(
        self, force_off_pod
    ):
        """locus() must test importability, not environment variables alone.

        Setting all three pod environment variables while the package is
        not importable must still report off_pod -- the variables alone
        are not sufficient evidence of being in-pod.
        """
        monkey = pytest.MonkeyPatch()
        try:
            for var in ("KBASE_AUTH_TOKEN", "SPARK_CONNECT_URL", "S3_ACCESS_KEY"):
                monkey.setenv(var, "fake-value-for-test")
            assert BerdlCapability().locus() == "off_pod"
        finally:
            monkey.undo()

    def test_locus_reports_in_pod_when_package_importable(self, force_in_pod):
        """The positive branch, exercised deterministically on either locus.

        Previously untested because it was assumed to need a live pod; it
        only needs the importability check to report True.
        """
        assert BerdlCapability().locus() == "in_pod"


class TestBuildIngestConfig:
    """ingest-config construction for both source modes."""

    def test_dataframe_mode_omits_paths(self):
        """DataFrame mode short-circuits the bronze read: no 'paths' key."""
        config = build_ingest_config(
            "aiale",
            [{"name": "dataset1"}],
            dataframes={"dataset1": object()},
        )

        assert "paths" not in config
        assert config["dataset"] == "aiale"
        assert config["tables"] == [{"name": "dataset1"}]

    def test_dataframe_mode_drops_paths_even_if_caller_passed_one(self):
        """A caller-supplied 'paths' is dropped in DataFrame mode, not merged in."""
        config = build_ingest_config(
            "aiale",
            [{"name": "dataset1"}],
            dataframes={"dataset1": object()},
            paths={"bronze_base": "s3a://cdm-lake/bronze/"},
        )

        assert "paths" not in config

    def test_bronze_mode_includes_paths_bronze_base(self):
        """Bronze mode (no dataframes) requires and includes paths.bronze_base."""
        config = build_ingest_config(
            "aiale",
            [
                {
                    "name": "dataset1",
                    "bronze_path": "s3a://cdm-lake/bronze/d1.parquet",
                    "format": "parquet",
                }
            ],
            paths={"bronze_base": "s3a://cdm-lake/bronze/"},
        )

        assert config["paths"] == {"bronze_base": "s3a://cdm-lake/bronze/"}
        assert config["tables"][0]["bronze_path"] == "s3a://cdm-lake/bronze/d1.parquet"

    def test_bronze_mode_without_paths_raises(self):
        """Bronze mode with no 'paths' at all is a config error, not a silent gap."""
        with pytest.raises(ValueError, match="bronze_base"):
            build_ingest_config("aiale", [{"name": "dataset1"}])

    def test_bronze_mode_without_bronze_base_raises(self):
        """A 'paths' section missing 'bronze_base' is still a config error."""
        with pytest.raises(ValueError, match="bronze_base"):
            build_ingest_config(
                "aiale",
                [{"name": "dataset1"}],
                paths={"silver_base": "s3a://cdm-lake/silver/"},
            )

    def test_requires_dataset(self):
        with pytest.raises(ValueError, match="dataset"):
            build_ingest_config(
                "", [{"name": "dataset1"}], dataframes={"dataset1": object()}
            )

    def test_requires_at_least_one_table(self):
        with pytest.raises(ValueError, match="table"):
            build_ingest_config("aiale", [], dataframes={})

    def test_table_missing_name_raises(self):
        with pytest.raises(ValueError, match="name"):
            build_ingest_config(
                "aiale", [{"comment": "no name here"}], dataframes={"x": object()}
            )

    def test_optional_top_level_keys_included_only_when_given(self):
        config = build_ingest_config(
            "aiale",
            [{"name": "dataset1"}],
            dataframes={"dataset1": object()},
            tenant="aiale",
            is_tenant=True,
            pipeline_name="gaa-pipeline",
            defaults={"parquet": {"inferSchema": True}},
        )

        assert config["tenant"] == "aiale"
        assert config["is_tenant"] is True
        assert config["pipeline_name"] == "gaa-pipeline"
        assert config["defaults"] == {"parquet": {"inferSchema": True}}

        minimal = build_ingest_config(
            "aiale", [{"name": "dataset1"}], dataframes={"dataset1": object()}
        )
        for key in ("tenant", "is_tenant", "pipeline_name", "defaults"):
            assert key not in minimal

    def test_does_not_mutate_caller_table_dicts(self):
        """build_ingest_config copies table dicts rather than mutating them."""
        original = {"name": "dataset1"}
        build_ingest_config("aiale", [original], dataframes={"dataset1": object()})
        assert original == {"name": "dataset1"}


class TestSelectWriteMode:
    """create-vs-append mode selection given table existence."""

    def test_append_to_nonexistent_table_becomes_overwrite(self):
        """A re-runnable loader must not fail on first execution.

        ``ingest`` itself raises ValueError for append-to-nonexistent; the
        loader must select 'overwrite' before ever calling ingest.
        """
        assert select_write_mode("append", table_exists=False) == "overwrite"

    def test_append_to_existing_table_stays_append(self):
        assert select_write_mode("append", table_exists=True) == "append"

    def test_overwrite_is_unaffected_by_existence(self):
        assert select_write_mode("overwrite", table_exists=False) == "overwrite"
        assert select_write_mode("overwrite", table_exists=True) == "overwrite"

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="overwrite.*append|append.*overwrite"):
            select_write_mode("upsert", table_exists=True)


@pytest.mark.usefixtures("force_off_pod")
class TestLoadOffPod:
    """BerdlCapability.load() must refuse off-pod with actionable guidance.

    Locus is forced via ``force_off_pod`` so these run identically on a dev
    laptop and in-pod. Without it, in-pod the refusal branch is never taken
    and ``load()`` proceeds to open a real Spark Connect session.
    """

    def test_raises_berdl_load_refused_error(self):
        cap = BerdlCapability()
        assert cap.locus() == "off_pod"

        with pytest.raises(BerdlLoadRefusedError):
            cap.load(dataset="aiale", tables=[{"name": "dataset1"}])

    def test_error_names_the_pod_requirement(self):
        cap = BerdlCapability()
        with pytest.raises(BerdlLoadRefusedError) as excinfo:
            cap.load(dataset="aiale", tables=[{"name": "dataset1"}])

        message = str(excinfo.value)
        assert "spark" in message.lower()
        assert "pod" in message.lower()

    def test_error_names_kbhub(self):
        cap = BerdlCapability()
        with pytest.raises(BerdlLoadRefusedError) as excinfo:
            cap.load(dataset="aiale", tables=[{"name": "dataset1"}])

        assert POD_MACHINE in str(excinfo.value)
        assert "kbhub" in str(excinfo.value)

    def test_error_gives_a_concrete_command(self):
        cap = BerdlCapability()
        with pytest.raises(BerdlLoadRefusedError) as excinfo:
            cap.load(dataset="aiale", tables=[{"name": "dataset1"}])

        message = str(excinfo.value)
        # A concrete, runnable command -- not just a vague pointer.
        assert "BerdlCapability" in message
        assert "load(" in message

    def test_off_pod_load_does_not_touch_ingest(self, monkeypatch):
        """Off-pod, load() must not stage data or dispatch any work.

        If it reached data_lakehouse_ingest.ingest this would raise
        ModuleNotFoundError (the package is not installed off-pod) rather
        than BerdlLoadRefusedError -- this test pins that the refusal
        happens strictly before that point.
        """
        cap = BerdlCapability()

        with pytest.raises(BerdlLoadRefusedError):
            cap.load(
                dataset="aiale",
                tables=[{"name": "dataset1"}],
                dataframes={"dataset1": object()},
            )


@pytest.mark.usefixtures("force_off_pod")
class TestMembershipsOffPod:
    """memberships() has no governance surface off-pod."""

    def test_raises_membership_unavailable_off_pod(self):
        cap = BerdlCapability()
        assert cap.locus() == "off_pod"

        with pytest.raises(BerdlMembershipUnavailableError):
            cap.memberships()
