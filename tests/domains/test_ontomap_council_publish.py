"""``council_publish`` -- stage 5's PRODUCER half, end to end over real files.

Spends nothing and reaches nothing. The run these tests publish is built by
the REAL :meth:`OntologyMappingUtils.council_plan` and the REAL
:meth:`OntologyMappingUtils.council_export` -- the collaborators injected are
the clearinghouse read, the council configuration and the retrieval stage, and
nothing else -- so what is exercised is the file contract BETWEEN stage 1,
the export and the publish, rather than a hand-written ``PLAN.json`` that
could drift from what stage 1 actually emits.

THE COLLABORATORS ARE IMPORTED FROM ``test_ontomap_council_export``, not
copied. There is exactly one definition in this suite of "what stage 1 emits
and what the council store hands back", and a second copy of it here would be
a second thing that could disagree with stage 1 -- with this file passing
against a plan shape nothing produces. The same reason the preset imports its
column list from ``verdicts.EXPORT_COLUMNS`` instead of restating it.

WHAT IS PINNED HERE. The render of the preset's four per-run markers, the
plan-time gate and the partition refusals, the shard run and its completeness
contract, the item accounting the load receipt reconciles against, and the
fact that NONE of it is in-pod: sharding runs anywhere, "no pod and no
credentials", so a publish that reached for the BERDL capability would be
wrong on h100 and this file proves it does not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from kbutillib.domains.genome.ontology_mapping import council_publish as cpub  # noqa: E402
from kbutillib.domains.genome.ontology_mapping import verdicts as vx  # noqa: E402
from kbutillib.domains.kbase.berdl import clearinghouse_ingest as ci  # noqa: E402
from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm  # noqa: E402
from tests.domains.test_ontomap_council_export import (  # noqa: E402
    QUERY,
    _FakeCouncil,
    _rows,
    _Stage,
)


# ---------------------------------------------------------------------------
# The fixture: a real plan, a real export, then a publish
# ---------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs" / "2026-10-23-publish"


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    return tmp_path / "scratch"


def _exported(run_dir: Path, scratch: Path) -> tuple:
    """Stage 1 and the export, both for real. Returns ``(plan, export)``."""
    stage = _Stage(_FakeCouncil({"job-chunk-0": _rows()}))
    plan = stage.council_plan(run_dir, query=QUERY, scratch_dir=scratch)
    export = stage.council_export(
        run_dir, job_ids=["job-chunk-0"], scratch_dir=scratch
    )
    return plan, export


def _published(run_dir: Path, scratch: Path, **kwargs: Any) -> tuple:
    plan, export = _exported(run_dir, scratch)
    return plan, export, cpub.council_publish(run_dir, **kwargs)


def _sharded_rows(
    record: Dict[str, Any], table: str = "function_result"
) -> List[Dict[str, Any]]:
    """Every row of one target table in the published shard run.

    Filtered by table because the run also carries the PARAMETER-SET REGISTRY
    batch, which has no ``entity_hash`` at all -- the registry is a hash-keyed
    dictionary, not an entity-typed table.
    """
    rows: List[Dict[str, Any]] = []
    for batch in record["batches"]:
        if batch["table"] != table:
            continue
        rows.extend(
            pq.read_table(str(Path(record["shard_dir"]) / batch["file"])).to_pylist()
        )
    return rows


# ---------------------------------------------------------------------------
# The default verdict path, and the refusal when it is not there
# ---------------------------------------------------------------------------


class TestResolvingTheExport:
    def test_the_default_verdict_path_is_the_one_the_plan_recorded(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """Recovered from PLAN.json's own absolute items_file, not re-derived.

        The plan already wrote down where it put its artefacts; re-resolving
        ``$KBU_SCRATCH_DIR`` here could answer differently from the stage that
        actually wrote the file, and a publish that looked in the wrong place
        would report "not exported yet" about an export that exists.
        """
        _plan, export, record = _published(run_dir, scratch)
        assert record["verdicts_file"] == str(Path(export["out_path"]).resolve())

    def test_an_absent_export_is_refused_naming_the_verb_that_writes_it(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage = _Stage(_FakeCouncil({"job-chunk-0": _rows()}))
        stage.council_plan(run_dir, query=QUERY, scratch_dir=scratch)
        with pytest.raises(cpub.PublishRefused) as excinfo:
            cpub.council_publish(run_dir)
        assert excinfo.value.check == "verdicts"
        assert "council-export" in excinfo.value.detail

    def test_an_absent_plan_is_refused_naming_stage_one(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()
        with pytest.raises(cpub.PublishRefused) as excinfo:
            cpub.council_publish(bare)
        assert excinfo.value.check == "plan"
        assert "council-plan" in excinfo.value.detail


# ---------------------------------------------------------------------------
# The render
# ---------------------------------------------------------------------------


class TestTheRender:
    def test_the_four_markers_are_substituted_from_the_plan(
        self, run_dir: Path, scratch: Path
    ) -> None:
        plan, export, record = _published(run_dir, scratch)
        text = Path(record["manifest_file"]).read_text(encoding="utf-8")
        assert record["manifest_file"] == str(run_dir / cpub.MANIFEST_FILENAME)

        source = cm.load_manifest(Path(record["manifest_file"])).sources[0]
        # 1. the locator, absolute and self-contained: the rendered manifest
        #    must not depend on an environment the load never sees.
        assert source.locator["path"] == str(Path(export["out_path"]).resolve())
        assert "${" not in source.locator["path"]
        # 2 and 3. source and parameter_set.query are two spellings of ONE
        #    fact, and validate_council_parameter_set checks they agree -- so
        #    both come from the same plan["query"].
        assert f'@const(council/{plan["query"]})' in text
        (table_plan,) = cm.shard_plan(cm.load_manifest(Path(record["manifest_file"])))
        assert table_plan.parameter_set["query"] == plan["query"]
        # 4. the two digests.
        assert table_plan.parameter_set["bench_fingerprint"] == plan["bench_fingerprint"]
        assert table_plan.parameter_set["mapping_hash"] == plan["mapping_hash"]

    def test_a_marker_that_moved_refuses_rather_than_publishing_the_template(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """An edited preset must fail LOUDLY here, not silently render nothing."""
        plan, _export = _exported(run_dir, scratch)
        shipped = "x" * 10  # a preset text with none of the three anchors
        with pytest.raises(cpub.PublishRefused) as excinfo:
            cpub.render_manifest(shipped, plan=plan, verdicts_path="/tmp/v.parquet")
        assert excinfo.value.check == "marker"
        assert "appears 0 time(s)" in excinfo.value.detail

    def test_a_plan_missing_a_partition_field_is_refused_by_name(self) -> None:
        with pytest.raises(cpub.PublishRefused) as excinfo:
            cpub.render_manifest(
                "anything",
                plan={"query": "go_mf@1", "source": "council/go_mf@1"},
                verdicts_path="/tmp/v.parquet",
            )
        assert excinfo.value.check == "plan_fields"
        assert "bench_fingerprint" in excinfo.value.detail


# ---------------------------------------------------------------------------
# The plan gate and the partition refusals
# ---------------------------------------------------------------------------


class TestThePlanGate:
    def test_the_gate_reports_the_target_table_and_the_footer_row_count(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """The same numbers ``kbu clearinghouse plan`` reports, from the same place.

        ``estimate_rows`` is metadata-only by contract -- here the parquet
        footer -- so the gate never scans the source to produce a count it
        then throws away.
        """
        _plan, export, record = _published(run_dir, scratch)
        (gate,) = record["plan_gate"]
        assert gate["table"] == "function_result"
        assert gate["kind"] == "result"
        assert gate["entity_type"] == "function"
        assert gate["row_count"] == export["rows"]
        assert gate["estimate_method"] == "parquet-footer-num-rows"

    def test_the_rows_land_at_the_plans_parameter_set_hash(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """Stage 1 defined "un-run" as the absence of a row at THIS hash."""
        plan, _export, record = _published(run_dir, scratch)
        (gate,) = record["plan_gate"]
        assert gate["parameter_set_hash"] == plan["parameter_set_hash"]
        assert gate["parameter_set_hash"] != cpub.UNRENDERED_PARAMETER_SET_HASH
        landed = {row["parameter_set_hash"] for row in _sharded_rows(record)}
        assert landed == {plan["parameter_set_hash"]}
        # And the registry row that explains it, written from the same set --
        # a result whose parameter_set_hash the registry cannot explain is an
        # orphan, so the publish has to carry both.
        registry = _sharded_rows(record, "parameter_set")
        assert {row["parameter_set_hash"] for row in registry} == landed

    def test_a_plan_hash_that_disagrees_with_the_render_refuses(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """Publishing at any other hash leaves the whole plan still un-run.

        And would charge the next run for all of it again -- ~199M tokens for
        the measured corpus.
        """
        _plan, _export = _exported(run_dir, scratch)
        plan_file = run_dir / "PLAN.json"
        plan = json.loads(plan_file.read_text())
        plan["parameter_set_hash"] = "9" * 64
        plan_file.write_text(json.dumps(plan))
        with pytest.raises(cpub.PublishRefused) as excinfo:
            cpub.council_publish(run_dir)
        assert excinfo.value.check == "parameter_set_hash"
        assert "still un-run" in excinfo.value.detail

    def test_a_plan_carrying_the_inert_markers_is_refused_by_name(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """The documented inert hash is a GATE, not a comment.

        A ``function_result`` row at that hash would make all 47,341
        clearinghouse functions read as answered by whichever bench got there
        first, and ``current_state()`` would make "un-run at THIS version" a
        query that always returns nothing.
        """
        _plan, _export = _exported(run_dir, scratch)
        plan_file = run_dir / "PLAN.json"
        plan = json.loads(plan_file.read_text())
        plan.update(
            {
                "query": "go_mf@0",
                "bench_fingerprint": "0" * 64,
                "mapping_hash": "0" * 64,
                "parameter_set_hash": cpub.UNRENDERED_PARAMETER_SET_HASH,
            }
        )
        plan_file.write_text(json.dumps(plan))
        with pytest.raises(cpub.PublishRefused) as excinfo:
            cpub.council_publish(run_dir)
        assert excinfo.value.check == "unrendered"
        assert cpub.UNRENDERED_PARAMETER_SET_HASH in excinfo.value.detail


# ---------------------------------------------------------------------------
# The shard run: the completeness contract, which already exists
# ---------------------------------------------------------------------------


class TestTheShardRun:
    def test_run_json_carries_a_sha256_row_count_and_hash_range_per_batch(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """THE COMPLETENESS CONTRACT, and there is no second manifest format.

        The sharder already publishes exactly this, and the consumer's ingest
        gate already verifies every field of it, so a second inventory would
        only be a second description of one directory that could disagree.
        """
        _plan, _export, record = _published(run_dir, scratch)
        assert record["shard_dir"] == str(run_dir / cpub.SHARD_SUBDIR)
        run = json.loads(Path(record["run_manifest"]).read_text())
        assert run["run_id"] == record["shard_run_id"]
        assert run["batches"]
        for batch in run["batches"]:
            assert len(batch["sha256"]) == 64
            assert batch["rows"] > 0
            assert len(batch["hash_min"]) == len(batch["hash_max"]) == 64
            assert batch["hash_min"] <= batch["hash_max"]
            # The byte count, so the consumer's arrival check can report
            # expected versus observed SIZE and not only a digest.
            assert batch["bytes"] > 0

    def test_the_published_bytes_match_the_published_digest(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """What the consumer checks on arrival, checked where it was produced."""
        _plan, _export, record = _published(run_dir, scratch)
        root = Path(record["shard_dir"])
        for batch in record["batches"]:
            path = root / batch["file"]
            assert ci.file_sha256(path) == batch["sha256"]
            assert path.stat().st_size == batch["bytes"]
            assert pq.ParquetFile(path).metadata.num_rows == batch["rows"]

    def test_the_join_key_is_the_query_id_and_never_the_text(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """Measured 2026-10-21: text matched 19,268/19,268 and is STILL wrong.

        A text key makes identity depend on every whitespace and unicode
        normalisation in a five-stage chain, and a drifted text does not
        collide loudly -- it silently fails to join and reads as un-run
        forever.
        """
        _plan, export, record = _published(run_dir, scratch)
        exported = pq.read_table(export["out_path"]).to_pylist()
        expected = {row["query_id"] for row in exported}
        assert {row["entity_hash"] for row in _sharded_rows(record)} == expected

    def test_a_shard_run_over_an_empty_export_is_a_producer_failure(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """Not a clean load: it shards, syncs, appends nothing and exits zero."""
        _plan, _export = _exported(run_dir, scratch)
        empty = scratch / "empty.parquet"
        pa.parquet.write_table(
            pa.Table.from_pylist([], schema=vx.export_schema()), empty
        )
        with pytest.raises(cpub.PublishRefused) as excinfo:
            cpub.council_publish(run_dir, verdicts=empty)
        assert excinfo.value.check == "shard_run"
        assert "PRODUCER failure" in excinfo.value.detail
        # The parameter-set registry batch IS written -- it comes from the
        # manifest's declared set, not from the data -- so counting batches
        # rather than payload rows is exactly how this would slip through.
        assert "registry batch" in excinfo.value.detail


# ---------------------------------------------------------------------------
# PUBLISH.json: the item accounting the receipt reconciles against
# ---------------------------------------------------------------------------


class TestThePublishRecord:
    def test_items_are_counted_separately_from_rows(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """One item can carry several GO terms, so a row count is not an item count."""
        plan, export, record = _published(run_dir, scratch)
        counts = record["counts"]
        assert counts["items_submitted"] == (
            plan["counts"]["abstain"] + plan["counts"]["fanout"]
        )
        assert counts["items_submitted"] == export["counts"]["inputs"]
        assert counts["items_with_verdict"] == export["rows"]
        assert counts["items_unresolved"] == export["counts"]["withheld"]
        assert (
            counts["items_with_verdict"] + counts["items_unresolved"]
            == counts["items_submitted"]
        )
        # The PARAMETER-SET REGISTRY batch is excluded: it carries a row per
        # declared parameter set, from the manifest rather than from the data,
        # so counting it would put the receipt's row reconciliation off by the
        # size of the registry.
        assert counts["result_rows_sharded"] == sum(
            batch["rows"]
            for batch in record["batches"]
            if batch["table"] == "function_result"
        )
        assert counts["result_rows_sharded"] == len(_sharded_rows(record))
        assert counts["result_rows_sharded"] < sum(
            batch["rows"] for batch in record["batches"]
        )

    def test_a_withheld_item_is_counted_and_warned_about(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """The residual items are the interesting number, and only we can count them.

        An item whose every call failed produced NO row, so it is invisible to
        the shard run and to everything downstream of the export.
        """
        stage = _Stage(_FakeCouncil({"job-chunk-0": _rows(error_item="t1")}))
        stage.council_plan(run_dir, query=QUERY, scratch_dir=scratch)
        export = stage.council_export(
            run_dir, job_ids=["job-chunk-0"], scratch_dir=scratch
        )
        record = cpub.council_publish(run_dir)
        assert export["counts"]["withheld"] == record["counts"]["items_unresolved"]
        assert record["counts"]["items_unresolved"] > 0
        assert any("no published row" in line for line in record["warnings"])

    def test_the_declines_are_tallied_from_the_published_parquet(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """From the artefact being published, so the tally cannot disagree with it."""
        _plan, export, record = _published(run_dir, scratch)
        published = pq.read_table(export["out_path"]).to_pylist()
        expected: Dict[str, int] = {}
        for row in published:
            if row["decline_reason"] is not None:
                expected[row["decline_reason"]] = (
                    expected.get(row["decline_reason"], 0) + 1
                )
        assert record["declines_by_reason"] == expected
        assert expected, "the fixture must publish at least one decline"

    def test_the_record_lands_where_the_receipt_looks_for_it(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """The producer and the consumer must agree on ONE location and name."""
        assert cpub.PUBLISH_FILENAME == ci.PUBLISH_RECORD_FILENAME
        _plan, _export, record = _published(run_dir, scratch)
        assert record["publish_file"] == str(run_dir / cpub.PUBLISH_FILENAME)
        # read_publish_record looks BESIDE the shard dir, which is where this
        # wrote it. Beside and not inside: the shard dir's inventory is
        # run.json and this must not add a second manifest to it.
        found = ci.read_publish_record(record["shard_dir"])
        assert found["counts"] == record["counts"]
        assert not (Path(record["shard_dir"]) / cpub.PUBLISH_FILENAME).exists()

    def test_the_hand_off_declares_where_the_receipt_goes(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """NOTHING HERE RUNS THE CONSUMER'S VERBS -- it declares them.

        ``load`` and ``verify`` are in-pod and run on the consumer machine
        after Dropbox syncs the shard directory; this repository's producer
        half only names them, and names where the receipt should land so the
        producer can read it back off the synced surface.
        """
        _plan, _export, record = _published(run_dir, scratch)
        hand_off = record["hand_off"]
        assert hand_off["receipt_file"] == str(run_dir / ci.RECEIPT_FILENAME)
        assert hand_off["load"].startswith("kbu clearinghouse load ")
        assert record["shard_dir"] in hand_off["load"]
        assert "--receipt" in hand_off["verify"]
        # Declared, not executed: the shard directory holds parquet and
        # run.json and nothing that could have come from a pod.
        assert sorted(
            path.name for path in Path(record["shard_dir"]).iterdir()
        ) == ["function_result", "parameter_set", "run.json"]

    def test_the_receipt_reconciles_against_what_this_record_published(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """The two halves of stage 5, joined on the one file they share."""
        _plan, export, record = _published(run_dir, scratch)
        receipt = ci.build_receipt(
            {
                "execution_id": "01EXECUTION00000000000000",
                "namespace": "kbaseincubator.clearinghouse",
                "ok": True,
                "batches": [
                    {
                        "name": "function_result",
                        "ledger_rows": record["counts"]["result_rows_sharded"],
                    }
                ],
                "tables": [{"name": "function_result", "snapshot_ids": ["1001"]}],
                "parameter_set_hashes": [record["parameter_set_hash"]],
                "discrepancies": [],
            },
            publish_record=ci.read_publish_record(record["shard_dir"]),
        )
        assert receipt["items_submitted"] == export["counts"]["inputs"]
        assert receipt["items_with_verdict"] == export["rows"]
        assert receipt["result_rows_written"] == record["counts"][
            "result_rows_sharded"
        ]
        assert receipt["parameter_set_hashes"] == [record["parameter_set_hash"]]
        assert receipt["reconciled"] is True


# ---------------------------------------------------------------------------
# Nothing here is in-pod, and nothing here runs on kbhub
# ---------------------------------------------------------------------------


class TestItRunsAnywhere:
    def test_publishing_never_touches_the_berdl_capability(
        self, run_dir: Path, scratch: Path, monkeypatch: Any
    ) -> None:
        """Sharding runs anywhere -- "no pod and no credentials".

        Only ``load`` and ``verify`` are in-pod and
        ``_require_in_pod_or_refuse`` guards exactly those two, so a publish
        that reached for the capability would refuse on h100, which is the one
        machine it has to work on. Proven by making construction fatal.
        """
        from kbutillib.domains.kbase.berdl import clearinghouse_capability as cc

        _plan, _export = _exported(run_dir, scratch)

        class _Forbidden:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise AssertionError(
                    "council_publish reached for the BERDL capability; the "
                    "publish half makes no transport call and needs no pod"
                )

        monkeypatch.setattr(cc, "ClearinghouseCapability", _Forbidden)
        record = cpub.council_publish(run_dir)
        assert record["batches"]


# ---------------------------------------------------------------------------
# Through the CLI
# ---------------------------------------------------------------------------


class TestTheCliVerb:
    def test_one_envelope_and_a_zero_exit(self, run_dir: Path, scratch: Path) -> None:
        from click.testing import CliRunner

        from kbutillib.interfaces.cli.ontomap import ontomap_cmd

        _exported(run_dir, scratch)
        result = CliRunner().invoke(
            ontomap_cmd, ["council-publish", "--run-dir", str(run_dir), "--json"]
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output.strip())
        assert envelope["verb"] == "council-publish"
        assert envelope["schema_version"] == 1
        assert envelope["data"]["shard_dir"] == str(run_dir / cpub.SHARD_SUBDIR)
        assert (run_dir / cpub.PUBLISH_FILENAME).is_file()

    def test_the_verb_is_no_longer_a_not_implemented_stub(self) -> None:
        from kbutillib.interfaces.cli import ontomap as om

        assert "council-publish" not in [name for name, _ in om._PLANNED_VERBS]

    def test_a_refusal_is_a_clean_nonzero_exit_and_not_a_traceback(
        self, run_dir: Path, scratch: Path
    ) -> None:
        from click.testing import CliRunner

        from kbutillib.interfaces.cli.ontomap import ontomap_cmd

        stage = _Stage(_FakeCouncil({"job-chunk-0": _rows()}))
        stage.council_plan(run_dir, query=QUERY, scratch_dir=scratch)
        result = CliRunner().invoke(
            ontomap_cmd, ["council-publish", "--run-dir", str(run_dir), "--json"]
        )
        assert result.exit_code != 0
        assert "council-export" in result.output
        assert "Traceback" not in result.output
