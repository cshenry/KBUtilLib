"""Unit tests for ``clearinghouse_schema.bootstrap()``.

Pure logic, no network, no live BERDL pod, no Spark: every scenario here
drives ``bootstrap()`` against a hand-built fake capability (``_FakeCapability``
below), never a real ``BerdlCapability``. That fake stands in for the
lakehouse boundary -- it lets these tests prove ``bootstrap()``'s control
flow (mode selection, the partition-spec guard, off-pod refusal
propagation, indeterminate-existence handling, dry-run's no-write
guarantee) but they do **not** prove any of this actually works against
real Iceberg-on-Polaris. See this module's docstring note above
``bootstrap()`` in ``clearinghouse_schema.py`` and the task's work-record
for that limitation stated explicitly.
"""

from __future__ import annotations

import pytest

from kbutillib.domains.kbase.berdl.capability import BerdlLoadRefusedError
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    BootstrapIndeterminateStateError,
    BootstrapPartitionSpecMismatchError,
    bootstrap,
    table_configs,
)

_NAMESPACE = "clearinghouse"


class _FakeCapability:
    """A minimal fake satisfying ``bootstrap()``'s required capability
    interface: ``table_exists``, ``table_partition_spec``, and ``load``.

    ``_tables`` maps table name -> live partition spec (``list[str]``,
    ``[]`` for unpartitioned) for every table that "already exists".
    Tables not present in ``_tables`` are reported as not existing.
    """

    def __init__(
        self,
        existing: dict[str, list[str]] | None = None,
        *,
        existence_error: Exception | None = None,
        partition_spec_error: Exception | None = None,
        load_error: Exception | None = None,
    ) -> None:
        self._tables = dict(existing) if existing else {}
        self._existence_error = existence_error
        self._partition_spec_error = partition_spec_error
        self._load_error = load_error
        self.load_calls: list[dict] = []

    def table_exists(self, name, *, namespace):
        self.seen_namespaces = getattr(self, "seen_namespaces", set())
        self.seen_namespaces.add(namespace)
        if self._existence_error is not None:
            raise self._existence_error
        return name in self._tables

    def table_partition_spec(self, name, *, namespace):
        self.seen_namespaces = getattr(self, "seen_namespaces", set())
        self.seen_namespaces.add(namespace)
        if self._partition_spec_error is not None:
            raise self._partition_spec_error
        return self._tables[name]

    def load(self, **kwargs):
        self.load_calls.append(kwargs)
        if self._load_error is not None:
            raise self._load_error
        # Record every table this call would have created/appended, so a
        # second bootstrap() run against the same fake sees them as
        # existing with the partition spec it requested.
        for table in kwargs["tables"]:
            self._tables[table["name"]] = (
                [table["partition_by"]]
                if isinstance(table.get("partition_by"), str)
                else list(table.get("partition_by") or [])
            )
        return {"ingest_result": "ok", "tables": kwargs["tables"]}


class TestFirstRunCreatesAllThree:
    def test_empty_namespace_creates_all_three_tables(self):
        cap = _FakeCapability()
        result = bootstrap(cap, namespace=_NAMESPACE)

        assert result["dry_run"] is False
        assert [t["name"] for t in result["tables"]] == [
            "entity",
            "canonical_content",
            "result",
        ]
        assert all(t["action"] == "create" for t in result["tables"])
        assert len(cap.load_calls) == 1
        loaded_names = [t["name"] for t in cap.load_calls[0]["tables"]]
        assert loaded_names == ["entity", "canonical_content", "result"]
        # bootstrap() itself never requests 'overwrite' -- it always
        # requests 'append' and lets load()'s own select_write_mode()
        # promote it on first creation.
        assert all(
            t["mode"] == "append" for t in cap.load_calls[0]["tables"]
        )

    def test_explicit_namespace_is_passed_to_every_call(self):
        cap = _FakeCapability()
        bootstrap(cap, namespace=_NAMESPACE)
        assert cap.load_calls[0]["namespace"] == _NAMESPACE

    def test_namespace_is_required(self):
        cap = _FakeCapability()
        with pytest.raises(ValueError, match="namespace"):
            bootstrap(cap, namespace="")


class TestSecondRunNeverRequestsOverwrite:
    def test_second_run_against_existing_tables_requests_append_only(self):
        cap = _FakeCapability(
            existing={
                "entity": ["entity_type"],
                "canonical_content": [],
                "result": ["source", "entity_type"],
            }
        )

        result = bootstrap(cap, namespace=_NAMESPACE)

        assert all(t["action"] == "append" for t in result["tables"])
        assert len(cap.load_calls) == 1
        requested_modes = {
            table["name"]: table["mode"] for table in cap.load_calls[0]["tables"]
        }
        assert requested_modes == {
            "entity": "append",
            "canonical_content": "append",
            "result": "append",
        }
        assert "overwrite" not in requested_modes.values()

    def test_two_runs_back_to_back_never_request_overwrite(self):
        """Run once against an empty namespace, then again -- the second
        run's fake now has all three tables 'existing' (see
        ``_FakeCapability.load``) and must request 'append' for all of
        them, never 'overwrite'.
        """
        cap = _FakeCapability()
        bootstrap(cap, namespace=_NAMESPACE)
        bootstrap(cap, namespace=_NAMESPACE)

        assert len(cap.load_calls) == 2
        second_call_modes = [t["mode"] for t in cap.load_calls[1]["tables"]]
        assert second_call_modes == ["append", "append", "append"]


class TestPartitionSpecGuard:
    def test_mismatch_against_live_table_refuses_and_names_both_specs(self):
        cap = _FakeCapability(existing={"result": ["standardizer_version"]})
        tables = [t for t in table_configs() if t["name"] == "result"]

        with pytest.raises(BootstrapPartitionSpecMismatchError) as excinfo:
            bootstrap(cap, namespace=_NAMESPACE, tables=tables)

        message = str(excinfo.value)
        assert "['source', 'entity_type']" in message  # expected (config) spec
        assert "['standardizer_version']" in message  # actual (live) spec
        assert cap.load_calls == []

    def test_mismatch_blocks_the_whole_batch_not_just_the_bad_table(self):
        """A mismatch on one table must refuse before writing *any* table
        in the same bootstrap() call -- including tables that were fine.
        """
        cap = _FakeCapability(existing={"result": ["standardizer_version"]})

        with pytest.raises(BootstrapPartitionSpecMismatchError):
            bootstrap(cap, namespace=_NAMESPACE)

        assert cap.load_calls == []

    def test_matching_live_spec_appends_without_refusing(self):
        cap = _FakeCapability(existing={"result": ["source", "entity_type"]})
        tables = [t for t in table_configs() if t["name"] == "result"]

        result = bootstrap(cap, namespace=_NAMESPACE, tables=tables)

        assert result["tables"][0]["action"] == "append"
        assert cap.load_calls[0]["tables"][0]["mode"] == "append"

    def test_unpartitioned_table_with_no_live_partition_matches_none(self):
        # canonical_content is the table that carries no partition_by --
        # entity now partitions on entity_type, so it no longer serves as
        # the "deliberately unpartitioned" example here.
        cap = _FakeCapability(existing={"canonical_content": []})
        tables = [t for t in table_configs() if t["name"] == "canonical_content"]

        result = bootstrap(cap, namespace=_NAMESPACE, tables=tables)

        assert result["tables"][0]["action"] == "append"


class TestOffPodRefusalPropagates:
    def test_berdl_load_refused_error_propagates_untouched(self):
        cap = _FakeCapability(load_error=BerdlLoadRefusedError("run this in-pod"))

        with pytest.raises(BerdlLoadRefusedError, match="run this in-pod"):
            bootstrap(cap, namespace=_NAMESPACE)

    def test_nothing_is_recorded_as_written_when_load_refuses(self):
        cap = _FakeCapability(load_error=BerdlLoadRefusedError("off-pod"))

        with pytest.raises(BerdlLoadRefusedError):
            bootstrap(cap, namespace=_NAMESPACE)

        # The fake only mutates its 'existing' state inside load(), after
        # a successful call -- an off-pod refusal must leave it untouched.
        assert cap._tables == {}

    def test_off_pod_refusal_is_reached_via_load_not_short_circuited(self):
        """bootstrap() must not swallow/re-wrap the error into something
        less informative, or invent a fallback write path -- it must
        genuinely call load() and let the real error surface.
        """
        cap = _FakeCapability(load_error=BerdlLoadRefusedError("off-pod, go to kbhub"))

        with pytest.raises(BerdlLoadRefusedError) as excinfo:
            bootstrap(cap, namespace=_NAMESPACE)

        assert "kbhub" in str(excinfo.value)
        assert len(cap.load_calls) == 1


class TestIndeterminateExistenceRefuses:
    def test_existence_check_raising_causes_refusal_not_overwrite(self):
        cap = _FakeCapability(existence_error=RuntimeError("catalog lookup timed out"))

        with pytest.raises(BootstrapIndeterminateStateError, match="entity"):
            bootstrap(cap, namespace=_NAMESPACE)

        assert cap.load_calls == []

    def test_indeterminate_existence_blocks_the_whole_batch(self):
        cap = _FakeCapability(existence_error=ValueError("ambiguous"))

        with pytest.raises(BootstrapIndeterminateStateError):
            bootstrap(cap, namespace=_NAMESPACE)

        assert cap.load_calls == []

    def test_partition_spec_lookup_raising_also_refuses(self):
        cap = _FakeCapability(
            existing={"result": ["source"]},
            partition_spec_error=RuntimeError("could not read table metadata"),
        )
        tables = [t for t in table_configs() if t["name"] == "result"]

        with pytest.raises(BootstrapIndeterminateStateError):
            bootstrap(cap, namespace=_NAMESPACE, tables=tables)

        assert cap.load_calls == []


class TestDryRun:
    def test_dry_run_never_calls_load(self):
        cap = _FakeCapability()
        bootstrap(cap, namespace=_NAMESPACE, dry_run=True)
        assert cap.load_calls == []

    def test_dry_run_reports_create_for_an_empty_namespace(self):
        cap = _FakeCapability()
        result = bootstrap(cap, namespace=_NAMESPACE, dry_run=True)

        assert result["dry_run"] is True
        assert result["load_result"] is None
        assert all(t["action"] == "create" for t in result["tables"])

    def test_dry_run_would_create_carries_a_namespace_resolution_warning(self):
        cap = _FakeCapability()
        result = bootstrap(cap, namespace=_NAMESPACE, dry_run=True)

        for table in result["tables"]:
            assert table["action"] == "create"
            assert "namespace" in table["namespace_warning"].lower()

    def test_namespace_warning_absent_on_a_real_run(self):
        cap = _FakeCapability()
        result = bootstrap(cap, namespace=_NAMESPACE, dry_run=False)
        for table in result["tables"]:
            assert "namespace_warning" not in table

    def test_dry_run_reports_append_for_existing_matching_tables(self):
        cap = _FakeCapability(
            existing={
                "entity": ["entity_type"],
                "canonical_content": [],
                "result": ["source", "entity_type"],
            }
        )
        result = bootstrap(cap, namespace=_NAMESPACE, dry_run=True)

        assert all(t["action"] == "append" for t in result["tables"])
        assert cap.load_calls == []

    def test_dry_run_reports_refuse_on_partition_mismatch_without_raising(self):
        cap = _FakeCapability(existing={"result": ["standardizer_version"]})
        tables = [t for t in table_configs() if t["name"] == "result"]

        result = bootstrap(cap, namespace=_NAMESPACE, tables=tables, dry_run=True)

        assert result["tables"][0]["action"] == "refuse"
        assert "['source', 'entity_type']" in result["tables"][0]["reason"]
        assert cap.load_calls == []

    def test_dry_run_reports_refuse_on_indeterminate_existence_without_raising(self):
        cap = _FakeCapability(existence_error=RuntimeError("timed out"))

        result = bootstrap(cap, namespace=_NAMESPACE, dry_run=True)

        assert all(t["action"] == "refuse" for t in result["tables"])
        assert all(t["exists"] is None for t in result["tables"])
        assert cap.load_calls == []

    def test_dry_run_performs_no_write_of_any_kind(self):
        """The overarching dry-run guarantee, checked across a mixed batch
        (one creatable table, one matching-existing table, one mismatched
        table) -- load() must never be called regardless of what any
        individual table's plan is.
        """
        cap = _FakeCapability(existing={"result": ["standardizer_version"]})
        bootstrap(cap, namespace=_NAMESPACE, dry_run=True)
        assert cap.load_calls == []
        # And the fake's own state (which only load() mutates) is
        # unchanged.
        assert cap._tables == {"result": ["standardizer_version"]}


class TestNoHardcodedNamespaceRule:
    def test_namespace_is_passed_through_verbatim_never_transformed(self):
        """bootstrap() must not invent a tenant-qualified/'my.'-prefix
        resolution rule of its own -- whatever the caller passes is what
        reaches table_exists/table_partition_spec/load, unchanged.
        """
        cap = _FakeCapability()
        weird_namespace = "my.kbaseincubator.clearinghouse"
        result = bootstrap(cap, namespace=weird_namespace)

        assert result["namespace"] == weird_namespace
        assert cap.load_calls[0]["namespace"] == weird_namespace
        assert cap.seen_namespaces == {weird_namespace}
