"""Unit tests for kbutillib.domains.kbase.berdl.capability.

Pure logic, no network, no live BERDL: ``ingest`` config construction in
both source modes (DataFrame mode omits 'paths', bronze mode includes it),
create-vs-append write-mode selection given table existence, and
``BerdlCapability.load()`` refusing off-pod with actionable guidance
(pod requirement, the kbhub machine, and a concrete command). Per the PRD's
"Testing Decisions", the two transports and locus-detection's positive
(in-pod) branch require a live BERDL pod and are not exercised here.
"""

import pytest

from kbutillib.domains.kbase.berdl.capability import (
    POD_MACHINE,
    BerdlCapability,
    BerdlLoadRefusedError,
    BerdlMembershipUnavailableError,
    berdl_notebook_utils_importable,
    build_ingest_config,
    select_write_mode,
)


class TestBerdlNotebookUtilsImportable:
    def test_returns_false_when_package_is_not_installed(self):
        """In this dev/CI environment berdl_notebook_utils is not installed.

        This also documents that the check tests real importability rather
        than being hardcoded -- if the pod package were ever installed
        here, this assertion would need updating.
        """
        assert berdl_notebook_utils_importable() is False

    def test_locus_reports_off_pod_when_package_not_importable(self):
        """locus() must test importability, not environment variables alone.

        Setting all three pod environment variables without the package
        being installed must still report off_pod -- the variables alone
        are not sufficient evidence of being in-pod.
        """
        import os

        cap = BerdlCapability()
        env_backup = {}
        pod_env_vars = ["KBASE_AUTH_TOKEN", "SPARK_CONNECT_URL", "S3_ACCESS_KEY"]
        try:
            for var in pod_env_vars:
                env_backup[var] = os.environ.get(var)
                os.environ[var] = "fake-value-for-test"

            assert berdl_notebook_utils_importable() is False
            assert cap.locus() == "off_pod"
        finally:
            for var, value in env_backup.items():
                if value is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = value


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


class TestLoadOffPod:
    """BerdlCapability.load() must refuse off-pod with actionable guidance."""

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


class TestMembershipsOffPod:
    """memberships() has no governance surface off-pod."""

    def test_raises_membership_unavailable_off_pod(self):
        cap = BerdlCapability()
        assert cap.locus() == "off_pod"

        with pytest.raises(BerdlMembershipUnavailableError):
            cap.memberships()
