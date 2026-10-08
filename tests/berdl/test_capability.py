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

import sys
import types

import pytest

from kbutillib.domains.kbase.berdl import capability as capability_module
from kbutillib.domains.kbase.berdl.capability import (
    POD_MACHINE,
    BerdlCapability,
    BerdlLoadRefusedError,
    BerdlMembershipUnavailableError,
    _quote_fqn,
    berdl_notebook_utils_importable,
    build_ingest_config,
    select_write_mode,
)
from kbutillib.domains.kbase.berdl.transports import InPodTransport


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


class _FakeSpark:
    """A Spark stand-in that records SQL and returns empty results."""

    def __init__(self):
        self.statements: list[str] = []

    def sql(self, statement):
        self.statements.append(statement)
        return _FakeCollectable()


class _FakeCollectable:
    def collect(self):
        return []


class _FakeInPodTransport(InPodTransport):
    """An ``InPodTransport`` that never touches the pod.

    Subclasses the real class (so ``load()``'s ``isinstance`` assertion
    passes) but bypasses ``__init__`` -- which imports ``berdl_notebook_utils``
    -- and records the namespaces its probes and namespace-resolver see.

    ``create_namespace_if_not_exists`` mirrors the real function's contract:
    it resolves and RETURNS ``f"{tenant_name}.{namespace}"`` (personal
    catalog ``f"my.{namespace}"`` when no tenant), which is exactly the
    namespace ``data_lakehouse_ingest.ingest`` writes to.
    """

    def __init__(self, *, exists=True, resolved_namespace=None):
        # Deliberately does NOT call super().__init__(): that imports the
        # pod-only package. We only need the recorded-call surface below.
        self._spark = _FakeSpark()
        self._exists = exists
        self._resolved_override = resolved_namespace
        self.table_exists_calls: list[tuple] = []
        self.create_namespace_calls: list[tuple] = []

    def spark_session(self):
        return self._spark

    def create_namespace_if_not_exists(
        self, spark, namespace="default", tenant_name=None, iceberg=True
    ):
        self.create_namespace_calls.append((namespace, tenant_name))
        if self._resolved_override is not None:
            return self._resolved_override
        prefix = tenant_name if tenant_name is not None else "my"
        return f"{prefix}.{namespace}"

    def table_exists(self, spark, table_name, namespace="default"):
        self.table_exists_calls.append((table_name, namespace))
        return self._exists


class _RecordingIngest:
    """Captures the config each ``ingest`` call receives so tests can read
    the effective per-table write mode.
    """

    def __init__(self):
        self.configs: list[dict] = []

    def __call__(self, config, **kwargs):
        self.configs.append(config)
        return {"status": "ok"}


@pytest.fixture
def fake_ingest(monkeypatch):
    """Install a fake ``data_lakehouse_ingest`` module (pod-only, absent
    off-pod) so ``load()``'s deferred ``from data_lakehouse_ingest import
    ingest`` resolves to a recording double.
    """
    recorder = _RecordingIngest()
    module = types.ModuleType("data_lakehouse_ingest")
    module.ingest = recorder
    monkeypatch.setitem(sys.modules, "data_lakehouse_ingest", module)
    return recorder


@pytest.mark.usefixtures("force_in_pod")
class TestLoadNamespaceResolution:
    """dev 1206 / defect D1: the existence probe must query the namespace
    the write actually targets, so a requested ``append`` against an
    existing table is never silently promoted to a destructive ``overwrite``.
    """

    def _capability(self, transport, monkeypatch):
        cap = BerdlCapability()
        cap._transport = transport
        # rw membership on any tenant/dataset -- the membership gate is not
        # what these tests exercise.
        monkeypatch.setattr(
            cap, "memberships", lambda: {"kbaseincubator": "rw"}
        )
        return cap

    def test_append_to_existing_table_stays_append_not_overwrite(
        self, monkeypatch, fake_ingest
    ):
        """AC 1 + AC 4: append against an existing table performs an append.

        The table exists at the WRITE-TARGET namespace
        ('kbaseincubator.clearinghouse'), not at the caller's 'default'.
        With the defect, the probe queried 'default', missed, and the write
        mode became 'overwrite'. This asserts it stays 'append'.
        """
        transport = _FakeInPodTransport(exists=True)
        cap = self._capability(transport, monkeypatch)

        report = cap.load(
            dataset="clearinghouse",
            tenant="kbaseincubator",
            tables=[{"name": "t1", "mode": "append"}],
            dataframes={"t1": object()},
            namespace="default",  # the destructive default the defect used
        )

        # AC 2: the probe queried the resolved write-target namespace.
        assert transport.table_exists_calls == [
            ("t1", "kbaseincubator.clearinghouse")
        ]
        # AC 4: the config handed to ingest requested 'append', never
        # 'overwrite'.
        assert len(fake_ingest.configs) == 1
        modes = [t["mode"] for t in fake_ingest.configs[0]["tables"]]
        assert modes == ["append"]
        table_report = report["tables"][0]
        assert table_report["effective_mode"] == "append"
        assert table_report["operation"] == "append"
        assert table_report["existed_before"] is True

    def test_probe_ignores_the_default_namespace_parameter(
        self, monkeypatch, fake_ingest
    ):
        """AC 2: the caller's ``namespace`` no longer steers the probe.

        Even a wildly wrong ``namespace`` cannot redirect the existence
        probe away from the write target.
        """
        transport = _FakeInPodTransport(exists=True)
        cap = self._capability(transport, monkeypatch)

        cap.load(
            dataset="clearinghouse",
            tenant="kbaseincubator",
            tables=[{"name": "t1", "mode": "append"}],
            dataframes={"t1": object()},
            namespace="a_wrong_namespace",
        )

        assert transport.table_exists_calls[0][1] == "kbaseincubator.clearinghouse"

    def test_resolution_happens_once_and_matches_ingest_target(
        self, monkeypatch, fake_ingest
    ):
        """The write-target namespace is resolved exactly once, via
        create_namespace_if_not_exists, from dataset + tenant.
        """
        transport = _FakeInPodTransport(exists=True)
        cap = self._capability(transport, monkeypatch)

        cap.load(
            dataset="clearinghouse",
            tenant="kbaseincubator",
            tables=[{"name": "t1", "mode": "append"}],
            dataframes={"t1": object()},
        )

        # Called once, with the BARE dataset as namespace and the tenant as
        # tenant_name (the real function prepends the tenant as the catalog).
        assert transport.create_namespace_calls == [
            ("clearinghouse", "kbaseincubator")
        ]

    def test_unresolvable_namespace_raises_not_overwrite(
        self, monkeypatch, fake_ingest
    ):
        """AC 3: if the write-target namespace cannot be determined, load()
        raises rather than probing (and thus falling into the destructive
        overwrite path). No ingest call is made.
        """
        transport = _FakeInPodTransport(exists=True, resolved_namespace="")
        cap = self._capability(transport, monkeypatch)

        with pytest.raises(BerdlLoadRefusedError, match="write-target namespace"):
            cap.load(
                dataset="clearinghouse",
                tenant="kbaseincubator",
                tables=[{"name": "t1", "mode": "append"}],
                dataframes={"t1": object()},
            )

        # It refused BEFORE any probe or ingest -- no destructive path taken.
        assert transport.table_exists_calls == []
        assert fake_ingest.configs == []

    def test_append_to_missing_table_still_becomes_overwrite(
        self, monkeypatch, fake_ingest
    ):
        """Non-regression: when the table genuinely does not exist at the
        write target, append-to-nonexistent still resolves to 'overwrite'
        (ingest itself rejects append-to-nonexistent). The fix only stops
        the FALSE-negative probe, it does not change this legitimate case.
        """
        transport = _FakeInPodTransport(exists=False)
        cap = self._capability(transport, monkeypatch)

        cap.load(
            dataset="clearinghouse",
            tenant="kbaseincubator",
            tables=[{"name": "t1", "mode": "append"}],
            dataframes={"t1": object()},
        )

        modes = [t["mode"] for t in fake_ingest.configs[0]["tables"]]
        assert modes == ["overwrite"]


@pytest.mark.usefixtures("force_off_pod")
class TestMembershipsOffPod:
    """memberships() has no governance surface off-pod."""

    def test_raises_membership_unavailable_off_pod(self):
        cap = BerdlCapability()
        assert cap.locus() == "off_pod"

        with pytest.raises(BerdlMembershipUnavailableError):
            cap.memberships()


class TestQuoteFqn:
    """load()'s postflight fully-qualified name is quoted per segment.

    The production namespace is dotted (``kbaseincubator.clearinghouse``).
    Quoting a dotted namespace as a SINGLE identifier
    (``` `kbaseincubator.clearinghouse`.`t` ```) yields a two-part name
    whose first part contains a dot, which Spark does not resolve --
    measured in-pod raising ``TABLE_OR_VIEW_NOT_FOUND`` while the
    per-segment shape resolved. Each namespace segment must get its own
    backtick pair. This mirrors the ``_quote_fqn`` rule in
    clearinghouse_bootstrap_adapter / _derivation / _schema.
    """

    def test_two_segment_namespace_quotes_each_segment(self):
        assert (
            _quote_fqn("protein_result", "kbaseincubator.clearinghouse")
            == "`kbaseincubator`.`clearinghouse`.`protein_result`"
        )

    def test_single_segment_namespace(self):
        assert (
            _quote_fqn("protein_result", "clearinghouse")
            == "`clearinghouse`.`protein_result`"
        )

    def test_table_name_is_never_split_on_a_dot(self):
        # A dot inside a table name is part of the name, not a level
        # separator: the name is quoted as one identifier.
        assert (
            _quote_fqn("my.table", "kbaseincubator.clearinghouse")
            == "`kbaseincubator`.`clearinghouse`.`my.table`"
        )

    def test_empty_namespace_segments_are_dropped(self):
        assert _quote_fqn("t", "a..b") == "`a`.`b`.`t`"


# --------------------------------------------------------------------------
# bf-write-target: the write-target RESOLVERS the clearinghouse guard needs.
#
# ClearinghouseCapability._assert_write_target refuses unless the wrapped
# capability exposes resolve_write_namespace/resolve_probe_namespace. Before
# this task those methods existed only in test doubles, so register(),
# ingest_shards() and verify_run() refused on EVERY production call. The
# resolvers below are the real implementation, and they are only worth
# anything if they return the SAME value load() probes -- which is why they
# and load() share one pure function.
# --------------------------------------------------------------------------


class TestWriteTargetResolvers:
    """The resolvers are pure, agree with each other, and agree with load()."""

    def test_resolvers_agree_with_tenant_set(self):
        cap = BerdlCapability()
        write = cap.resolve_write_namespace(dataset="clearinghouse", tenant="kbaseincubator")
        probe = cap.resolve_probe_namespace(dataset="clearinghouse", tenant="kbaseincubator")
        assert write == probe == "kbaseincubator.clearinghouse"

    def test_resolvers_agree_with_tenant_unset(self):
        """No tenant -> the Spark personal-catalog alias, not a bare dataset."""
        cap = BerdlCapability()
        write = cap.resolve_write_namespace(dataset="clearinghouse", tenant=None)
        probe = cap.resolve_probe_namespace(dataset="clearinghouse", tenant=None)
        assert write == probe == "my.clearinghouse"

    def test_resolvers_do_no_io_and_work_off_pod(self, monkeypatch):
        """Pure: no transport is constructed, so they answer off-pod too.

        ``_get_transport`` is replaced with a boom so any I/O attempt fails
        loudly rather than silently working through a real transport.
        """
        cap = BerdlCapability()

        def _boom():
            raise AssertionError("a resolver constructed a transport")

        monkeypatch.setattr(cap, "_get_transport", _boom)
        monkeypatch.setattr(
            "kbutillib.domains.kbase.berdl.capability.berdl_notebook_utils_importable",
            lambda: False,
        )
        assert cap.locus() == "off_pod"
        assert cap.resolve_write_namespace(dataset="d", tenant="t") == "t.d"
        assert cap.resolve_probe_namespace(dataset="d", tenant="t") == "t.d"
        assert cap._transport is None

    @pytest.mark.usefixtures("force_in_pod")
    @pytest.mark.parametrize(
        "tenant", ["kbaseincubator", None], ids=["tenant_set", "tenant_unset"]
    )
    def test_resolver_value_is_exactly_what_load_probes(
        self, monkeypatch, fake_ingest, tenant
    ):
        """The published namespace IS the probed namespace, both loci of tenancy.

        This is the whole point of the shared function: the clearinghouse
        guard confirms ``resolve_*``'s answer, so that answer has to be the
        one ``load()`` reads table existence against.
        """
        transport = _FakeInPodTransport(exists=True)
        cap = BerdlCapability()
        cap._transport = transport
        monkeypatch.setattr(
            cap, "memberships", lambda: {"kbaseincubator": "rw", "clearinghouse": "rw"}
        )

        cap.load(
            dataset="clearinghouse",
            tenant=tenant,
            tables=[{"name": "t1", "mode": "append"}],
            dataframes={"t1": object()},
            namespace="default",
        )

        published = cap.resolve_probe_namespace(dataset="clearinghouse", tenant=tenant)
        assert transport.table_exists_calls == [("t1", published)]

    @pytest.mark.usefixtures("force_in_pod")
    def test_load_refuses_when_platform_resolves_a_different_namespace(
        self, monkeypatch, fake_ingest
    ):
        """A platform/resolver disagreement refuses with NOTHING written.

        If ``create_namespace_if_not_exists`` returns a namespace the shared
        function did not compute, then the resolvers advertise one namespace
        while the write would land in another -- dev 1206 with a different
        pair of names. load() must refuse before probing or ingesting.
        """
        transport = _FakeInPodTransport(
            exists=True, resolved_namespace="somewhere.else"
        )
        cap = BerdlCapability()
        cap._transport = transport
        monkeypatch.setattr(cap, "memberships", lambda: {"kbaseincubator": "rw"})

        with pytest.raises(BerdlLoadRefusedError) as excinfo:
            cap.load(
                dataset="clearinghouse",
                tenant="kbaseincubator",
                tables=[{"name": "t1", "mode": "append"}],
                dataframes={"t1": object()},
            )

        # The message names BOTH values, so an operator can see the drift.
        message = str(excinfo.value)
        assert "somewhere.else" in message
        assert "kbaseincubator.clearinghouse" in message
        # Nothing staged, nothing probed, no ingest.
        assert transport.table_exists_calls == []
        assert fake_ingest.configs == []
