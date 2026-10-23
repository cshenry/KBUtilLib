"""Tests for the shipped ``function-to-go-council`` clearinghouse preset.

The preset is the PUBLISH step of the standing ``function_to_go`` council
pipeline: it maps the verdict parquet
(:data:`~kbutillib.domains.genome.ontology_mapping.verdicts.EXPORT_COLUMNS`,
written by stage 5) onto ``function_result``. Everything here runs off-pod
with no credentials -- the sample verdict parquet is written by the export's
own :func:`~...verdicts.write_verdict_parquet`, so these tests exercise the
real artifact shape rather than a hand-rolled approximation of it.

What is covered:

  - The preset RESOLVES BY NAME through
    :func:`clearinghouse_backfill.resolve_manifest_path`, and
    ``describe_preset`` reports its target tables and the one environment
    variable that re-points it at a real run's parquet.
  - It PLANS AND SHARDS UNTOUCHED, against the example parquet shipped beside
    it as package data -- the same arrangement ``file-example`` uses, and the
    only thing that makes the preset smoke-testable on a pod with no
    credentials and no fixture to fetch.
  - The declared shape: ``file``/``parquet``, ``entity_type = "function"``,
    ``kinds = ["result"]``, ``observed_at_column = "decided_at"``, and
    ``[source.hash] precomputed = "query_id"`` -- never ``raw_column`` on the
    text.
  - PLAN-TIME VALIDATION passes, through the ``kbu clearinghouse plan``
    command itself, against a sample verdict parquet. Plan time is the cheap
    gate and an ingest failure at 19k rows is not.
  - The parameter set is the RUN IDENTITY and is refused when it is ``{}``,
    when it names ``roster_fingerprint``, or when its ``query`` disagrees
    with the ``source`` partition it was rendered beside.
  - ``bench_fingerprint`` is the key; ``roster_fingerprint`` and the per-seat
    ``route_fingerprint`` are published in the PAYLOAD instead.
  - Every column the preset names exists in ``EXPORT_COLUMNS``. That is the
    anti-drift check: the preset and the export are one contract written in
    two files, and a payload naming a column the parquet does not carry would
    plan clean and publish nulls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pyarrow.parquet as pq
import pytest

from kbutillib.domains import identity
from kbutillib.domains.genome.ontology_mapping import verdicts as vx
from kbutillib.domains.kbase.berdl import clearinghouse_backfill as cb
from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
from kbutillib.domains.kbase.berdl import clearinghouse_parameters as cp
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

#: The preset under test. Named rather than globbed, so a preset that silently
#: stops being installed as package data fails here.
PRESET = "function-to-go-council"

#: The environment variable the preset's locator declares with NO default.
VERDICTS_ENV = "KBU_F2GO_VERDICTS"

#: The parameter-set hash of the preset AS SHIPPED, i.e. before the publish
#: step substitutes the run's real query version and digests. The preset's
#: header states this constant so a ``function_result`` row carrying it can be
#: recognised as a manifest that was loaded un-rendered; asserting it here is
#: what keeps that statement true.
UNRENDERED_PARAMETER_SET_HASH = (
    "7bccf01baa4f69e139545bf712247b8f49047c6569bad1e107b3e3417f960d49"
)

#: Two plausible clearinghouse function hashes, used as ``query_id``s. They
#: are the clearinghouse's OWN digests, carried through the pipeline -- which
#: is the whole point of the ``precomputed`` hash mode.
QUERY_IDS = ("a" * 64, "f" * 64)


# --------------------------------------------------------------------------
# A sample verdict parquet, written by the export's own writer
# --------------------------------------------------------------------------


def _decided_row(query_id: str) -> Dict[str, Any]:
    """One row the council decided, with a term selected."""
    row: Dict[str, Any] = {name: None for name in vx.EXPORT_COLUMNS}
    row.update(
        {
            "query_id": query_id,
            "function_text": "Glucokinase (EC 2.7.1.2)",
            "genomes": [],
            "go_ids": ["GO:0004340"],
            "go_confidences": ["high"],
            "final_confidence": "high",
            "decided_by": "unanimous",
            "no_functional_content": False,
            "decline_reason": None,
            "n_valid_proposers": 3,
            "votes": [{"go_id": "GO:0004340", "votes": 3}],
            "decided_at": "2026-10-22T01:02:03+00:00",
            "query_name": "go_mf",
            "query_version": "1",
            "roster_fingerprint": "b" * 64,
            "route_fingerprints": [
                {
                    "role": "critic",
                    "seat_name": "critic",
                    "route_fingerprint": "c" * 64,
                }
            ],
            "mapping_hash": "d" * 64,
            "job_ids": ["job-1"],
            "retried": False,
        }
    )
    return row


def _abstained_row(query_id: str) -> Dict[str, Any]:
    """One row retrieval declined: ``go_ids = []``, but still published.

    An ABSENT row is indistinguishable from "not yet run", so a decline has to
    be published or stage 1 re-selects it forever.
    """
    row = _decided_row(query_id)
    row.update(
        {
            "go_ids": [],
            "go_confidences": [],
            "final_confidence": None,
            "decided_by": None,
            "no_functional_content": None,
            "decline_reason": "abstained:no_candidates",
            "n_valid_proposers": 0,
            "votes": [],
        }
    )
    return row


@pytest.fixture()
def verdict_parquet(tmp_path: Path) -> Path:
    """A two-row verdict parquet: one decided, one abstained."""
    out = tmp_path / "verdicts.parquet"
    vx.write_verdict_parquet(
        [_decided_row(QUERY_IDS[0]), _abstained_row(QUERY_IDS[1])], out
    )
    return out


@pytest.fixture()
def preset_path() -> Path:
    presets = {preset.name: preset for preset in cb.available_presets()}
    assert PRESET in presets, f"shipped presets: {sorted(presets)}"
    return presets[PRESET].path


def _env(verdict_parquet: Path) -> Dict[str, str]:
    return {VERDICTS_ENV: str(verdict_parquet)}


def _manifest(preset_path: Path, verdict_parquet: Path) -> cm.Manifest:
    return cm.load_manifest(preset_path, env=_env(verdict_parquet))


def _only_source(manifest: cm.Manifest) -> cm.Source:
    assert len(manifest.sources) == 1, [s.name for s in manifest.sources]
    return manifest.sources[0]


def _variant(
    preset_path: Path, tmp_path: Path, old: str, new: str, *, name: str
) -> Path:
    """The shipped preset with ONE substitution, written to ``tmp_path``.

    Editing the real text rather than hand-building a near-miss manifest keeps
    the refusal tests honest: everything except the thing under test is
    exactly what ships.
    """
    text = preset_path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"anchor {old!r} appears {text.count(old)} times"
    path = tmp_path / f"{name}.toml"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


#: The ``parameter_set`` line as it ships -- the anchor every refusal variant
#: replaces.
_SHIPPED_PARAMETER_SET = (
    '  parameter_set       = { query = "go_mf@0", bench_fingerprint = '
    '"0000000000000000000000000000000000000000000000000000000000000000", '
    'mapping_hash = "0000000000000000000000000000000000000000000000000000000000000000" }'
)

_SHIPPED_SOURCE = '  source              = "@const(council/go_mf@0)"'


# --------------------------------------------------------------------------
# Resolution by name
# --------------------------------------------------------------------------


def test_the_preset_resolves_by_name(preset_path: Path) -> None:
    """A preset NAME, not a path: the publish step must work from a wheel."""
    assert cb.resolve_manifest_path(PRESET) == (preset_path, PRESET)
    assert preset_path.parent == cb.preset_root()


def test_the_preset_plans_and_shards_untouched_from_a_wheel(
    preset_path: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    """With nothing exported, the locator resolves to the shipped example.

    The same reason ``file-example`` ships its fixture: ``plan`` and
    ``backfill list`` have to work on any pod with no credentials, no network
    and no fixture to fetch, or the preset cannot be smoke-tested where it
    runs. ``${KBU_F2GO_VERDICTS}`` then re-points it at a real run's parquet
    without editing package data, which an operator cannot edit anyway -- it
    is inside the installed wheel.
    """
    monkeypatch.delenv(VERDICTS_ENV, raising=False)
    source = _only_source(cm.load_manifest(preset_path))
    example = preset_path.parent / "function-to-go-council-example.parquet"
    assert source.locator["path"] == str(example)
    assert example.is_file(), "the example parquet must ship as package data"

    reports = cs.shard_manifest(
        cm.load_manifest(preset_path),
        str(tmp_path / "out"),
        manifest_path=str(preset_path),
    )
    (result,) = [r for r in reports if r.table == "function_result"]
    # Five rows, one per published class -- including the three declines,
    # which carry go_ids = [] and are the behaviour most easily broken.
    assert result.rows == 5
    rows = pq.read_table(str(result.path)).to_pylist()
    reasons = {
        json.loads(row["payload"])["decline_reason"] for row in rows
    }
    assert reasons == {
        None,
        "no_candidate_matched",
        "contentless",
        "abstained:no_candidates",
    }
    # Shipped un-rendered, so the whole fixture lands at the inert hash -- the
    # row a promotion gate should refuse.
    assert {row["parameter_set_hash"] for row in rows} == {
        UNRENDERED_PARAMETER_SET_HASH
    }


def test_the_verdicts_path_is_overridable_and_declared_as_needs_env(
    preset_path: Path, verdict_parquet: Path, monkeypatch: Any
) -> None:
    """``needs_env`` is advisory, and it is what makes the override findable.

    A real publish MUST set it: the run's verdict parquet is a per-run
    artifact on local scratch, so no shipped default is correct for it.
    """
    manifest = cm.load_manifest(preset_path, env=_env(verdict_parquet))
    assert manifest.backfill is not None
    assert manifest.backfill.needs_env == (VERDICTS_ENV,)
    assert _only_source(manifest).locator["path"] == str(verdict_parquet)


def test_the_preset_describes_itself_as_writing_function_result(
    preset_path: Path, verdict_parquet: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv(VERDICTS_ENV, str(verdict_parquet))
    row = cb.describe_preset(cb.Preset(name=PRESET, path=preset_path))
    assert "error" not in row
    assert row["adapters"] == ["file"]
    # The registry table is reported FIRST: the sharder writes the parameter
    # set's canonical JSON before any result batch, because a result row keyed
    # by a hash with no registry entry is a partition nobody can explain.
    assert row["tables"] == [schema.PARAMETER_SET_TABLE, "function_result"]
    assert row["needs_env"] == [VERDICTS_ENV]
    assert row["pulls_from"]


# --------------------------------------------------------------------------
# The declared shape
# --------------------------------------------------------------------------


def test_the_declared_shape_is_a_parquet_file_of_function_results(
    preset_path: Path, verdict_parquet: Path
) -> None:
    source = _only_source(_manifest(preset_path, verdict_parquet))
    assert source.name == PRESET
    assert source.adapter == "file"
    assert source.locator["format"] == "parquet"
    assert source.locator["path"] == str(verdict_parquet)
    assert source.entity_type == "function"
    assert source.kinds == ("result",)
    # The council's own decision time, not the publication time. The shard
    # SKIPS a row whose observed_at is null, and observed_at is one of the two
    # columns the current-state window function orders by.
    assert source.observed_at_column == "decided_at"
    # No entity/content kinds: the 47,341 functions were READ from the
    # clearinghouse in stage 1, so re-declaring their identity from the
    # export's text would mint a second entity for a function that has one.
    assert "entity" not in source.kinds and "content" not in source.kinds


def test_the_join_key_is_the_precomputed_query_id_and_never_the_text(
    preset_path: Path, verdict_parquet: Path
) -> None:
    """``query_id`` is the clearinghouse's own entity_hash, carried through.

    Measured 2026-10-21: the council's own ``items.entity_hash`` matched 0 of
    19,268 clearinghouse hashes while the definition TEXT matched 19,268 of
    19,268 -- so a text join LOOKS like the one that works. It is still the
    wrong key: a text key makes identity depend on every whitespace and
    unicode normalisation in the chain, and a drifted text does not collide
    loudly, it silently fails to join and reads as un-run forever.
    """
    source = _only_source(_manifest(preset_path, verdict_parquet))
    assert source.hash_spec == {"precomputed": "query_id"}
    assert "raw_column" not in source.hash_spec
    assert "function_text" not in source.hash_spec.values()

    (plan,) = cm.shard_plan(_manifest(preset_path, verdict_parquet))
    assert plan.hash_source == ("precomputed", "query_id", "function")


def test_plan_time_validation_passes_against_a_sample_verdict_parquet(
    preset_path: Path, verdict_parquet: Path, monkeypatch: Any
) -> None:
    """Through the CLI, because plan time is the gate an operator actually runs."""
    from click.testing import CliRunner

    from kbutillib.interfaces.cli.clearinghouse import clearinghouse_cmd

    monkeypatch.setenv(VERDICTS_ENV, str(verdict_parquet))
    result = CliRunner().invoke(
        clearinghouse_cmd, ["plan", str(preset_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert '"function_result"' in result.output
    # estimate_rows is EXACT from the parquet footer, so the preview is the
    # real row count and not a guess.
    assert '"row_count": 2' in result.output
    assert '"estimate_method": "parquet-footer-num-rows"' in result.output


# --------------------------------------------------------------------------
# The parameter set: the run identity, and the three ways it is refused
# --------------------------------------------------------------------------


def test_the_parameter_set_is_the_run_identity_and_not_a_default_run(
    preset_path: Path, verdict_parquet: Path
) -> None:
    (plan,) = cm.shard_plan(_manifest(preset_path, verdict_parquet))
    assert plan.parameter_set is not None
    assert set(plan.parameter_set) == set(cp.COUNCIL_PARAMETER_KEYS)
    assert plan.parameter_set != {}
    assert plan.parameter_set_hash != identity.DEFAULT_PARAMETER_SET_HASH
    # The version is part of the partition, so the query carries one.
    assert "@" in plan.parameter_set["query"]


def test_the_bench_is_the_key_and_the_roster_is_not(
    preset_path: Path, verdict_parquet: Path
) -> None:
    """The bench partitions the corpus; the transport does not.

    ``Roster.fingerprint()`` covers endpoint_kind, the route spelling, the
    token ceiling, the temperature and the extras, so moving one seat from the
    codex CLI to MAG moves it -- which would re-partition all 47,341
    clearinghouse functions and re-charge ~199M tokens for a pure cost
    optimisation.
    """
    (plan,) = cm.shard_plan(_manifest(preset_path, verdict_parquet))
    assert "bench_fingerprint" in plan.parameter_set
    assert "roster_fingerprint" not in plan.parameter_set
    assert not set(plan.parameter_set) & cp.COUNCIL_REFUSED_KEYS


def test_the_unrendered_parameter_set_hash_is_the_documented_constant(
    preset_path: Path, verdict_parquet: Path
) -> None:
    """The preset's header names this hash so an un-rendered load is visible.

    The markers are legal -- the file as shipped must PLAN, or it could not be
    tested -- and inert. A ``function_result`` row at this hash means the
    publish step did not substitute, and ``PLAN.json`` records the run's real
    ``parameter_set_hash`` for the comparison that catches it.
    """
    (plan,) = cm.shard_plan(_manifest(preset_path, verdict_parquet))
    assert plan.parameter_set_hash == UNRENDERED_PARAMETER_SET_HASH
    assert UNRENDERED_PARAMETER_SET_HASH in preset_path.read_text(encoding="utf-8")


def test_an_empty_parameter_set_is_refused_at_plan_time(
    preset_path: Path, tmp_path: Path, verdict_parquet: Path
) -> None:
    """``{}`` is the one declaration this preset must never make.

    It hashes to DEFAULT_PARAMETER_SET_HASH, so every run would land in one
    slot and all 47,341 functions would read as answered by whichever bench
    got there first -- making "un-run at THIS version" a query that always
    returns nothing.
    """
    path = _variant(
        preset_path,
        tmp_path,
        _SHIPPED_PARAMETER_SET,
        "  parameter_set       = {}",
        name="empty-parameter-set",
    )
    with pytest.raises(cm.ManifestError) as exc:
        cm.shard_plan(cm.load_manifest(path, env=_env(verdict_parquet)))
    message = str(exc.value)
    assert "council" in message
    assert "bench_fingerprint" in message
    assert "not a default run" in message


def test_a_roster_fingerprint_parameter_key_is_refused(
    preset_path: Path, tmp_path: Path, verdict_parquet: Path
) -> None:
    """Provenance used as identity is the defect the bench fingerprint fixes."""
    path = _variant(
        preset_path,
        tmp_path,
        _SHIPPED_PARAMETER_SET,
        '  parameter_set       = { query = "go_mf@0", roster_fingerprint = '
        f'"{"b" * 64}", mapping_hash = "{"d" * 64}" }}',
        name="roster-as-key",
    )
    with pytest.raises(cm.ManifestError) as exc:
        cm.shard_plan(cm.load_manifest(path, env=_env(verdict_parquet)))
    # The REFUSED-key message, not the "bench_fingerprint is absent" one: this
    # set fails both checks and only the first names the actual mistake.
    assert "roster_fingerprint" in str(exc.value)
    assert "PROVENANCE" in str(exc.value)


def test_a_source_and_query_that_disagree_are_refused(
    preset_path: Path, tmp_path: Path, verdict_parquet: Path
) -> None:
    """The two halves of one rendered fact must not come apart.

    ``source`` is ``"council/<query>"`` and ``parameter_set.query`` is that
    same ``<query>``; a render that substituted one and missed the other would
    publish under a partition whose halves disagree about which query version
    answered.
    """
    path = _variant(
        preset_path,
        tmp_path,
        _SHIPPED_SOURCE,
        '  source              = "@const(council/go_mf@7)"',
        name="disagreeing-query",
    )
    with pytest.raises(cm.ManifestError) as exc:
        cm.shard_plan(cm.load_manifest(path, env=_env(verdict_parquet)))
    assert "go_mf@7" in str(exc.value)
    assert "go_mf@0" in str(exc.value)


def test_a_bare_bench_fingerprint_marker_is_refused(
    preset_path: Path, tmp_path: Path, verdict_parquet: Path
) -> None:
    """A truncated or placeholder digest fails the 64-lowercase-hex form.

    ``${VAR}`` is the spelling an author reaches for by habit, and
    ``load_manifest`` deliberately does NOT expand the mapping half -- so it
    would survive as a literal and hash the template. The digest form check is
    what stops it.
    """
    path = _variant(
        preset_path,
        tmp_path,
        _SHIPPED_PARAMETER_SET,
        '  parameter_set       = { query = "go_mf@0", bench_fingerprint = '
        f'"${{KBU_BENCH}}", mapping_hash = "{"d" * 64}" }}',
        name="unexpanded-marker",
    )
    with pytest.raises(cm.ManifestError) as exc:
        cm.shard_plan(cm.load_manifest(path, env=_env(verdict_parquet)))
    assert "bench_fingerprint" in str(exc.value)
    assert "sha256" in str(exc.value)


# --------------------------------------------------------------------------
# The payload: provenance, never a key
# --------------------------------------------------------------------------


def _payload_columns(plan: cm.TablePlan) -> List[str]:
    payload = plan.columns["payload"]
    assert payload.name == "json", payload.raw
    return list(payload.args)


def test_the_payload_publishes_the_roster_and_the_per_seat_route(
    preset_path: Path, verdict_parquet: Path
) -> None:
    """Both fingerprints ARE recorded -- they are simply not keys.

    ``route_fingerprint``'s own docstring puts it as "where did this come
    from", not "is this the same question", which is exactly the difference
    between the payload and the parameter set.
    """
    (plan,) = cm.shard_plan(_manifest(preset_path, verdict_parquet))
    payload = _payload_columns(plan)
    assert "roster_fingerprint" in payload
    assert "route_fingerprints" in payload
    # The verdict itself, and the text as published evidence rather than as a
    # key.
    for column in (
        "function_text",
        "go_ids",
        "go_confidences",
        "final_confidence",
        "decided_by",
        "no_functional_content",
        "decline_reason",
        "n_valid_proposers",
        "votes",
    ):
        assert column in payload, column


def test_every_column_the_preset_names_is_a_column_the_export_writes(
    preset_path: Path, verdict_parquet: Path
) -> None:
    """The anti-drift check between two files holding one contract.

    A payload naming a column the parquet does not carry PLANS CLEAN and
    publishes nulls -- plan time cannot see a source's columns -- so the only
    place this can be caught is here.
    """
    manifest = _manifest(preset_path, verdict_parquet)
    source = _only_source(manifest)
    (plan,) = cm.shard_plan(manifest)

    named = set(_payload_columns(plan))
    named.add(source.hash_spec["precomputed"])
    named.add(source.observed_at_column)
    for derivation in plan.columns.values():
        if derivation.name == "column":
            named.update(derivation.args)

    missing = sorted(named - set(vx.EXPORT_COLUMNS))
    assert not missing, (
        f"{PRESET} names {missing}, which verdicts.EXPORT_COLUMNS does not "
        "carry; the preset and the export are one contract in two files"
    )


# --------------------------------------------------------------------------
# End to end: the preset actually shards the real artifact
# --------------------------------------------------------------------------


def test_the_shard_publishes_one_function_result_row_per_verdict(
    preset_path: Path, verdict_parquet: Path, tmp_path: Path
) -> None:
    """Plan validation is necessary, not sufficient: run the mapping for real.

    One row per input ``query_id``, the DECLINED row included -- an absent row
    is indistinguishable from "not yet run", and stage 1 would re-select it
    forever.
    """
    manifest = _manifest(preset_path, verdict_parquet)
    reports = cs.shard_manifest(
        manifest, str(tmp_path / "out"), manifest_path=str(preset_path)
    )
    by_table = {report.table: report for report in reports}
    assert set(by_table) == {schema.PARAMETER_SET_TABLE, "function_result"}

    registry = pq.read_table(str(by_table[schema.PARAMETER_SET_TABLE].path)).to_pylist()
    assert [row["parameter_set_hash"] for row in registry] == [
        UNRENDERED_PARAMETER_SET_HASH
    ]

    rows = pq.read_table(str(by_table["function_result"].path)).to_pylist()
    assert [row["entity_hash"] for row in rows] == sorted(QUERY_IDS)
    for row in rows:
        assert row["entity_type"] == "function"
        assert row["result_type"] == "go_molecular_function"
        assert row["source"] == "council/go_mf@0"
        assert row["parameter_set_hash"] == UNRENDERED_PARAMETER_SET_HASH
        # observed_at is the COUNCIL's decision time, read off the column.
        assert row["observed_at"].isoformat().startswith("2026-10-22T01:02:03")
        assert '"roster_fingerprint":"' + "b" * 64 + '"' in row["payload"]
        assert '"route_fingerprint":"' + "c" * 64 + '"' in row["payload"]


def test_a_text_only_change_does_not_move_the_published_identity(
    preset_path: Path, tmp_path: Path
) -> None:
    """Why the key is ``query_id`` and not ``function_text``, demonstrated.

    The same function with its text re-normalised publishes under the SAME
    ``entity_hash``, because nothing in the chain recomputes the key. Under a
    ``raw_column`` on the text these two would be different entities and the
    second would read as un-run forever.
    """

    def _entity_hashes(text: str, out: str) -> List[str]:
        row = _decided_row(QUERY_IDS[0])
        row["function_text"] = text
        parquet = tmp_path / f"{out}.parquet"
        vx.write_verdict_parquet([row], parquet)
        manifest = cm.load_manifest(preset_path, env=_env(parquet))
        reports = cs.shard_manifest(
            manifest, str(tmp_path / out), manifest_path=str(preset_path)
        )
        (report,) = [r for r in reports if r.table == "function_result"]
        return [
            record["entity_hash"]
            for record in pq.read_table(str(report.path)).to_pylist()
        ]

    assert _entity_hashes("Glucokinase (EC 2.7.1.2)", "tight") == _entity_hashes(
        "  Glucokinase   (EC 2.7.1.2)\n", "loose"
    )


def test_the_preset_header_explains_both_decisions(preset_path: Path) -> None:
    """The presets' convention: a substantial header, not a bare mapping.

    The two decisions a reviewer has to be able to check from the file alone
    are why the hash is ``precomputed`` and how the parameter set is
    parameterised.
    """
    header = "\n".join(
        line
        for line in preset_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    )
    assert "precomputed" in header and "raw_column" in header
    assert "parameter_set_column" in header, "the rejected alternative is unexplained"
    assert "bench_fingerprint" in header and "roster_fingerprint" in header
    assert re.search(r"publish step", header)
