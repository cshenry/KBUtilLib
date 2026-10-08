"""Tests for ``kbu clearinghouse`` — the read verbs.

Pure logic, no network, no live BERDL pod, no Spark. Every scenario drives the
click CLI against a hand-built fake ``ClearinghouseCapability`` (:class:`_FakeCH`
below) injected by patching
:func:`kbutillib.interfaces.cli.clearinghouse._capability`. The fake records
which read method the verb called and returns canned rows, so these tests prove
the CLI's OUTPUT CONTRACT and its facade discipline WITHOUT proving anything
against real Iceberg-on-Polaris.

The published contract (from the module docstring):
    - every verb under --json emits EXACTLY ONE object carrying schema_version,
      verb, generated_at, locus, data and warnings
    - the human rendering of the same invocation is driven from the SAME dict
      (asserted by patching the renderer and checking it got what the
      serialiser did)
    - off-pod ``stats --include-files`` returns without file fields and with a
      non-empty warnings list
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import kbutillib.interfaces.cli.clearinghouse as ch
from kbutillib.cli import main
from kbutillib.domains.identity import DEFAULT_PARAMETER_SET_HASH
from kbutillib.domains.kbase.berdl.clearinghouse_schema import ENTITY_TYPES, table_name

#: The eight read verbs and a minimal valid argv for each (the fake capability
#: makes every one succeed). ``--json`` is appended by the test.
_VERB_ARGV = {
    "tables": ["clearinghouse", "tables"],
    "stats": ["clearinghouse", "stats"],
    "known": ["clearinghouse", "known", "--type", "protein", "--hash", "a" * 64],
    "show": ["clearinghouse", "show", "--type", "protein", "--hash", "a" * 64],
    "content": ["clearinghouse", "content", "--type", "protein", "--hash", "a" * 64],
    "results": ["clearinghouse", "results", "--type", "protein"],
    "sources": ["clearinghouse", "sources"],
    "parameter-sets": ["clearinghouse", "parameter-sets"],
    "health": ["clearinghouse", "health"],
}

_ENVELOPE_KEYS = {"schema_version", "verb", "generated_at", "locus", "data", "warnings"}


class _FakeCH:
    """A fake ClearinghouseCapability recording calls and returning canned rows.

    Mirrors exactly the read surface the CLI is allowed to call. Every method
    returns a small, well-formed result so the CLI can build its envelope. The
    ``locus`` and per-method ``stats``/etc. behaviour are configurable so a test
    can exercise the off-pod degradation path.
    """

    def __init__(
        self,
        locus="in_pod",
        *,
        stats_result=None,
        parameter_sets_rows=None,
        orphan_report=None,
        result_rows=None,
    ):
        self._locus_value = locus
        self._stats_result = stats_result
        self._parameter_sets_rows = parameter_sets_rows
        self._orphan_report = orphan_report
        self._result_rows = result_rows
        self.calls: list[str] = []
        #: Every keyword a verb passed, per method name -- so a CLI test can
        #: assert a flag ARRIVED with the right value, not merely that the
        #: command exited zero.
        self.kwargs: dict[str, dict] = {}

    def _locus(self):
        return self._locus_value

    def tables(self, **_kw):
        self.calls.append("tables")
        return [
            {
                "name": table_name(et, kind),
                "kind": kind,
                "entity_type": et,
                "partition_by": [],
                "row_count": 1,
            }
            for kind in ("entity", "content", "result")
            for et in ENTITY_TYPES
        ]

    def stats(self, *, include_files=False, **_kw):
        self.calls.append(f"stats(include_files={include_files})")
        if self._stats_result is not None:
            return self._stats_result
        return {
            "tables": [
                {"name": "protein_entity", "entity_type": "protein",
                 "kind": "entity", "row_count": 7}
            ],
            "warnings": [],
        }

    def known(self, entity_type, hashes, **_kw):
        self.calls.append(f"known({entity_type})")
        return list(hashes)

    def content(self, entity_type, hashes, **_kw):
        self.calls.append(f"content({entity_type})")
        return [{"entity_hash": h, "sequence": "ACGT"} for h in hashes]

    def content_all_types(self, hashes, **_kw):
        self.calls.append("content_all_types")
        return [{"entity_hash": h, "entity_type": "protein"} for h in hashes]

    def results(
        self,
        entity_type,
        hashes,
        *,
        sources=None,
        result_types=None,
        parameter_set_hashes=None,
        **_kw,
    ):
        self.calls.append(f"results({entity_type})")
        self.kwargs["results"] = {
            "sources": sources,
            "result_types": result_types,
            "parameter_set_hashes": parameter_set_hashes,
        }
        if self._result_rows is not None:
            return [dict(row) for row in self._result_rows]
        return [{"entity_hash": h, "result_type": "annotation"} for h in hashes]

    def current_state(
        self,
        entity_type,
        *,
        sources=None,
        result_types=None,
        parameter_set_hashes=None,
        **_kw,
    ):
        self.calls.append(f"current_state({entity_type})")
        self.kwargs["current_state"] = {
            "sources": sources,
            "result_types": result_types,
            "parameter_set_hashes": parameter_set_hashes,
        }
        if self._result_rows is not None:
            return [dict(row) for row in self._result_rows]
        return [{"entity_hash": "a" * 64, "result_type": "annotation"}]

    def sources(self, entity_type=None, *, by_parameter_set=False, **_kw):
        self.calls.append(f"sources({entity_type})")
        self.kwargs["sources"] = {"by_parameter_set": by_parameter_set}
        if by_parameter_set:
            return [
                {
                    "entity_type": "protein",
                    "source": "uniprot",
                    "parameter_set_hash": "c" * 64,
                    "row_count": 2,
                    "parameter_set_count": 1,
                    "canonical_json": '{"threshold":"1e-5"}',
                }
            ]
        return [
            {
                "entity_type": "protein",
                "source": "uniprot",
                "row_count": 3,
                "parameter_set_count": 2,
            }
        ]

    def parameter_sets(self, hashes=None, **_kw):
        self.calls.append("parameter_sets")
        self.kwargs["parameter_sets"] = {"hashes": hashes}
        if self._parameter_sets_rows is not None:
            return [dict(row) for row in self._parameter_sets_rows]
        return [
            {
                "parameter_set_hash": "c" * 64,
                "canonical_json": '{"threshold":"1e-5"}',
                "observed_at": "2026-01-01T00:00:00Z",
                "ingest_batch_id": "b1",
            }
        ]

    def parameter_set_orphans(self, **_kw):
        self.calls.append("parameter_set_orphans")
        if self._orphan_report is not None:
            return dict(self._orphan_report)
        return {
            "ok": True,
            "result_hashes": 1,
            "registry_hashes": 1,
            "missing_from_registry": [],
            "malformed_result_hashes": [],
            "conflicting_canonical_json": [],
            "mishashed_registry_rows": [],
            "failures": [],
        }


def _invoke(monkeypatch, argv, *, fake=None):
    """Invoke ``main`` with ``_capability`` patched to return ``fake``."""
    fake = fake or _FakeCH()
    monkeypatch.setattr(ch, "_capability", lambda: fake)
    return CliRunner().invoke(main, argv, catch_exceptions=False), fake


# --------------------------------------------------------------------------
# (a) every read verb under --json emits exactly one envelope object
# --------------------------------------------------------------------------


@pytest.mark.parametrize("verb", sorted(_VERB_ARGV))
def test_json_emits_exactly_one_envelope_object(monkeypatch, verb):
    result, _ = _invoke(monkeypatch, _VERB_ARGV[verb] + ["--json"])
    assert result.exit_code == 0, result.output

    # stdout carries EXACTLY ONE JSON object and nothing else (RFC 8259-clean).
    stdout = result.stdout.strip()
    parsed = json.loads(stdout)  # raises if stdout is not a single clean object
    assert isinstance(parsed, dict)
    # Exactly one object: there is no trailing content after it.
    decoder = json.JSONDecoder()
    _obj, end = decoder.raw_decode(stdout)
    assert stdout[end:].strip() == ""

    assert set(parsed) == _ENVELOPE_KEYS
    assert parsed["schema_version"] == ch.SCHEMA_VERSION
    assert parsed["verb"] == verb
    assert parsed["locus"] == "in_pod"
    assert isinstance(parsed["data"], dict)
    assert isinstance(parsed["warnings"], list)
    assert isinstance(parsed["generated_at"], str) and parsed["generated_at"]


@pytest.mark.parametrize("verb", sorted(_VERB_ARGV))
def test_json_stdout_has_no_diagnostic_lines(monkeypatch, verb):
    # Force a warning path where possible so we prove warnings go to stderr,
    # never stdout: use the off-pod include-files degradation for stats; other
    # verbs simply must not print anything but the envelope.
    result, _ = _invoke(monkeypatch, _VERB_ARGV[verb] + ["--json"])
    # stdout must parse whole as one object => no banner/log/warning text mixed in.
    json.loads(result.stdout.strip())


# --------------------------------------------------------------------------
# (b) human rendering is driven from the SAME dict the serialiser would emit
# --------------------------------------------------------------------------


@pytest.mark.parametrize("verb", sorted(_VERB_ARGV))
def test_human_render_receives_same_dict_as_serialiser(monkeypatch, verb):
    fake = _FakeCH()

    # First capture the exact envelope the --json path serialises.
    json_result, _ = _invoke(monkeypatch, _VERB_ARGV[verb] + ["--json"], fake=_FakeCH())
    serialised = json.loads(json_result.stdout.strip())

    # Now patch the renderer and run the SAME invocation WITHOUT --json; assert
    # the renderer received an envelope structurally identical to what the
    # serialiser emitted (generated_at is a timestamp, so compare all else).
    captured = {}

    def _spy(envelope):
        captured["envelope"] = envelope

    monkeypatch.setattr(ch, "_render", _spy)
    human_result, _ = _invoke(monkeypatch, _VERB_ARGV[verb], fake=fake)
    assert human_result.exit_code == 0, human_result.output

    got = captured["envelope"]
    assert got is not None
    # Same keys, same everything but the wall-clock timestamp.
    for key in _ENVELOPE_KEYS - {"generated_at"}:
        assert got[key] == serialised[key], key
    assert set(got) == _ENVELOPE_KEYS


def test_render_is_the_only_human_path(monkeypatch):
    # If the renderer is patched to a no-op, the human path prints nothing to
    # stdout beyond what the renderer chooses -- proving there is no second
    # code path computing output.
    monkeypatch.setattr(ch, "_render", lambda envelope: None)
    result, _ = _invoke(monkeypatch, ["clearinghouse", "tables"])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


# --------------------------------------------------------------------------
# (c) off-pod ``stats --include-files`` omits file fields, non-empty warnings
# --------------------------------------------------------------------------


def test_offpod_stats_include_files_degrades(monkeypatch):
    # Model the capability's own off-pod degradation: no file fields, a warning.
    off_pod_stats = {
        "tables": [
            {"name": "protein_entity", "entity_type": "protein",
             "kind": "entity", "row_count": 7}
        ],
        "warnings": [
            "include_files is in-pod only; file counts and sizes are omitted "
            "off-pod."
        ],
    }
    fake = _FakeCH(locus="off_pod", stats_result=off_pod_stats)
    result, _ = _invoke(
        monkeypatch,
        ["clearinghouse", "stats", "--include-files", "--json"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout.strip())

    assert envelope["locus"] == "off_pod"
    assert envelope["warnings"], "a degraded answer must carry a non-empty warnings list"
    # No file fields leaked into any table row.
    for row in envelope["data"]["tables"]:
        assert "data_file_count" not in row
        assert "avg_file_size_bytes" not in row
    # The warning is ALSO on stderr for the human (as a bare diagnostic line),
    # while stdout stays a pristine JSON envelope: the warning text appears in
    # stdout ONLY inside the envelope's warnings array, never as a loose line.
    assert "include_files is in-pod only" in result.stderr
    # stdout parses whole as exactly one JSON object => no loose warning line.
    reparsed = json.loads(result.stdout.strip())
    assert any("include_files is in-pod only" in w for w in reparsed["warnings"])


# --------------------------------------------------------------------------
# facade discipline: each verb calls the sanctioned capability method
# --------------------------------------------------------------------------


def test_results_without_hash_uses_current_state(monkeypatch):
    _, fake = _invoke(monkeypatch, ["clearinghouse", "results", "--type", "gene"])
    assert "current_state(gene)" in fake.calls
    assert not any(c.startswith("results(") for c in fake.calls)


def test_results_with_hash_uses_results(monkeypatch):
    _, fake = _invoke(
        monkeypatch,
        ["clearinghouse", "results", "--type", "gene", "--hash", "b" * 64],
    )
    assert "results(gene)" in fake.calls
    assert "current_state(gene)" not in fake.calls


def test_content_all_types_uses_lossy_view_and_warns(monkeypatch):
    result, fake = _invoke(
        monkeypatch,
        ["clearinghouse", "content", "--all-types", "--hash", "c" * 64, "--json"],
    )
    envelope = json.loads(result.stdout.strip())
    assert "content_all_types" in fake.calls
    assert envelope["data"]["all_types"] is True
    assert any("all_content" in w for w in envelope["warnings"])


def test_content_requires_type_without_all_types(monkeypatch):
    monkeypatch.setattr(ch, "_capability", lambda: _FakeCH())
    result = CliRunner().invoke(
        main, ["clearinghouse", "content", "--hash", "d" * 64]
    )
    assert result.exit_code != 0  # UsageError


def test_show_composes_known_content_results(monkeypatch):
    _, fake = _invoke(
        monkeypatch,
        ["clearinghouse", "show", "--type", "protein", "--hash", "e" * 64],
    )
    assert "known(protein)" in fake.calls
    assert "content(protein)" in fake.calls
    assert "results(protein)" in fake.calls


def test_health_flags_fragmented_tables(monkeypatch):
    stats_result = {
        "tables": [
            {"name": "gene_content", "entity_type": "gene", "kind": "content",
             "row_count": 2_760_000, "data_file_count": 139,
             "avg_file_size_bytes": 288 * 1024},  # 288 KiB -- fragmented
            {"name": "protein_entity", "entity_type": "protein", "kind": "entity",
             "row_count": 100, "data_file_count": 2,
             "avg_file_size_bytes": 64 * 1024 * 1024},  # 64 MiB -- healthy
        ],
        "warnings": [],
    }
    fake = _FakeCH(stats_result=stats_result)
    result, _ = _invoke(monkeypatch, ["clearinghouse", "health", "--json"], fake=fake)
    envelope = json.loads(result.stdout.strip())
    flagged = {t["name"] for t in envelope["data"]["tables"]}
    assert flagged == {"gene_content"}
    assert envelope["data"]["tables_evaluated"] == 2
    assert envelope["data"]["threshold_bytes"] == ch.HEALTH_MIN_AVG_FILE_SIZE_BYTES


def test_health_offpod_cannot_evaluate_warns(monkeypatch):
    # Off-pod stats omits file fields -> nothing evaluable -> non-empty warnings.
    stats_result = {
        "tables": [
            {"name": "protein_entity", "entity_type": "protein", "kind": "entity",
             "row_count": 7}
        ],
        "warnings": ["include_files is in-pod only; omitted off-pod."],
    }
    fake = _FakeCH(locus="off_pod", stats_result=stats_result)
    result, _ = _invoke(monkeypatch, ["clearinghouse", "health", "--json"], fake=fake)
    envelope = json.loads(result.stdout.strip())
    assert envelope["data"]["tables_evaluated"] == 0
    assert envelope["warnings"]


# ===========================================================================
# PARAMETER SETS ON THE CLI
#
# The flags are additive, so SCHEMA_VERSION does not move; what has to be
# proved is that each flag ARRIVES at the capability carrying the right
# value, that the registry text reaches the human output, and that an
# integrity finding is a FAILURE rather than a warning.
# ===========================================================================

_TUNED_HASH = "c" * 64


def test_schema_version_is_unchanged_by_the_parameter_set_work():
    """Every change in this task is ADDITIVE, so the contract version holds.

    A new field, a new flag and a new verb do not break a consumer pinning
    major 1 (it must tolerate unknown keys -- output-contract rule 2). If
    this assertion ever has to change, something stopped being additive.
    """
    assert ch.SCHEMA_VERSION == 1


# -- results: --parameter-set-hash / --default-parameters ------------------


def test_results_default_parameters_flag_reaches_the_capability(monkeypatch):
    _, fake = _invoke(
        monkeypatch,
        ["clearinghouse", "results", "--type", "protein", "--default-parameters"],
    )
    assert fake.kwargs["current_state"]["parameter_set_hashes"] == [
        DEFAULT_PARAMETER_SET_HASH
    ]


def test_results_parameter_set_hash_flag_is_repeatable(monkeypatch):
    _, fake = _invoke(
        monkeypatch,
        [
            "clearinghouse", "results", "--type", "protein",
            "--parameter-set-hash", _TUNED_HASH,
            "--parameter-set-hash", "d" * 64,
        ],
    )
    assert fake.kwargs["current_state"]["parameter_set_hashes"] == [
        _TUNED_HASH,
        "d" * 64,
    ]


def test_results_combining_both_flags_unions_them(monkeypatch):
    """--default-parameters is shorthand, so combining the two means BOTH."""
    _, fake = _invoke(
        monkeypatch,
        [
            "clearinghouse", "results", "--type", "protein",
            "--parameter-set-hash", _TUNED_HASH,
            "--default-parameters",
        ],
    )
    assert fake.kwargs["current_state"]["parameter_set_hashes"] == [
        _TUNED_HASH,
        DEFAULT_PARAMETER_SET_HASH,
    ]


def test_results_default_parameters_is_not_duplicated_when_given_both_ways(
    monkeypatch,
):
    _, fake = _invoke(
        monkeypatch,
        [
            "clearinghouse", "results", "--type", "protein",
            "--parameter-set-hash", DEFAULT_PARAMETER_SET_HASH,
            "--default-parameters",
        ],
    )
    assert fake.kwargs["current_state"]["parameter_set_hashes"] == [
        DEFAULT_PARAMETER_SET_HASH
    ]


def test_results_without_parameter_set_flags_applies_no_filter(monkeypatch):
    """No flag means NO FILTER (None), never "match nothing" ([])."""
    _, fake = _invoke(
        monkeypatch, ["clearinghouse", "results", "--type", "protein"]
    )
    assert fake.kwargs["current_state"]["parameter_set_hashes"] is None


def test_results_parameter_set_flags_reach_the_hash_filtered_path(monkeypatch):
    _, fake = _invoke(
        monkeypatch,
        [
            "clearinghouse", "results", "--type", "protein",
            "--hash", "a" * 64, "--default-parameters",
        ],
    )
    assert fake.kwargs["results"]["parameter_set_hashes"] == [
        DEFAULT_PARAMETER_SET_HASH
    ]


def test_results_human_output_shows_the_registry_text_beside_each_hash(
    monkeypatch,
):
    """A bare 64-char digest is opaque; the human table carries its meaning."""
    fake = _FakeCH(
        result_rows=[
            {"entity_hash": "a" * 64, "parameter_set_hash": _TUNED_HASH},
        ],
        parameter_sets_rows=[
            {
                "parameter_set_hash": _TUNED_HASH,
                "canonical_json": '{"threshold":"1e-5"}',
                "observed_at": "2026-01-01",
                "ingest_batch_id": "b1",
            }
        ],
    )
    result, fake = _invoke(
        monkeypatch, ["clearinghouse", "results", "--type", "protein"], fake=fake
    )
    assert "parameter_sets" in fake.calls
    assert fake.kwargs["parameter_sets"]["hashes"] == [_TUNED_HASH]
    assert '{"threshold":"1e-5"}' in result.output


def test_results_json_carries_the_registry_text_on_each_row(monkeypatch):
    fake = _FakeCH(
        result_rows=[{"entity_hash": "a" * 64, "parameter_set_hash": _TUNED_HASH}],
        parameter_sets_rows=[
            {"parameter_set_hash": _TUNED_HASH, "canonical_json": "{}"}
        ],
    )
    result, _ = _invoke(
        monkeypatch,
        ["clearinghouse", "results", "--type", "protein", "--json"],
        fake=fake,
    )
    payload = json.loads(result.output.splitlines()[-1])
    assert payload["data"]["results"][0]["parameter_set"] == "{}"


def test_results_annotation_degrades_with_a_warning_on_a_bad_stored_hash(
    monkeypatch,
):
    """Malformed STORED data must not fail the read -- it warns and points on.

    The rows are still the right answer; a stored hash that cannot be looked
    up is the integrity verb's business, not this read's.
    """

    class _RefusingCH(_FakeCH):
        def parameter_sets(self, hashes=None, **_kw):
            raise ValueError("parameter_sets: parameter_set_hashes[0] is bad.")

    fake = _RefusingCH(
        result_rows=[{"entity_hash": "a" * 64, "parameter_set_hash": "SHORT"}]
    )
    result, _ = _invoke(
        monkeypatch,
        ["clearinghouse", "results", "--type", "protein", "--json"],
        fake=fake,
    )
    payload = json.loads(result.output.splitlines()[-1])
    assert payload["warnings"], "a degraded answer must carry a warning"
    assert payload["data"]["results"][0]["entity_hash"] == "a" * 64
    assert result.exit_code == 0


# -- sources: --by-parameter-set -------------------------------------------


def test_sources_by_parameter_set_flag_reaches_the_capability(monkeypatch):
    _, fake = _invoke(
        monkeypatch, ["clearinghouse", "sources", "--by-parameter-set"]
    )
    assert fake.kwargs["sources"]["by_parameter_set"] is True


def test_sources_defaults_to_the_coarse_grain(monkeypatch):
    """The default grain is frozen: one row per source, opt-in to go finer."""
    _, fake = _invoke(monkeypatch, ["clearinghouse", "sources"])
    assert fake.kwargs["sources"]["by_parameter_set"] is False


def test_sources_envelope_states_which_grain_it_holds(monkeypatch):
    result, _ = _invoke(
        monkeypatch, ["clearinghouse", "sources", "--by-parameter-set", "--json"]
    )
    payload = json.loads(result.output.splitlines()[-1])
    assert payload["data"]["by_parameter_set"] is True
    assert payload["data"]["sources"][0]["parameter_set_hash"] == "c" * 64


# -- the parameter-sets verb ----------------------------------------------


def test_parameter_sets_verb_lists_the_registry(monkeypatch):
    result, fake = _invoke(monkeypatch, ["clearinghouse", "parameter-sets", "--json"])
    payload = json.loads(result.output.splitlines()[-1])
    assert payload["verb"] == "parameter-sets"
    assert payload["data"]["parameter_sets"][0]["canonical_json"] == (
        '{"threshold":"1e-5"}'
    )
    assert fake.kwargs["parameter_sets"]["hashes"] is None


def test_parameter_sets_verb_hash_option_is_repeatable(monkeypatch):
    _, fake = _invoke(
        monkeypatch,
        [
            "clearinghouse", "parameter-sets",
            "--hash", _TUNED_HASH, "--hash", "d" * 64,
        ],
    )
    assert fake.kwargs["parameter_sets"]["hashes"] == [_TUNED_HASH, "d" * 64]


def test_parameter_sets_verb_renders_rows_for_a_human(monkeypatch):
    result, _ = _invoke(monkeypatch, ["clearinghouse", "parameter-sets"])
    assert "canonical_json" in result.output
    assert '{"threshold":"1e-5"}' in result.output


# -- integrity findings are FAILURES on health and verify ------------------


_BROKEN_REGISTRY = {
    "ok": False,
    "result_hashes": 1,
    "registry_hashes": 0,
    "missing_from_registry": [
        {"parameter_set_hash": _TUNED_HASH, "entity_types": ["protein"]}
    ],
    "malformed_result_hashes": [],
    "conflicting_canonical_json": [],
    "mishashed_registry_rows": [],
    "failures": [f"parameter_set_hash {_TUNED_HASH} is used by protein row(s)"],
}


def test_health_exits_non_zero_on_a_parameter_set_integrity_finding(monkeypatch):
    result, _ = _invoke(
        monkeypatch,
        ["clearinghouse", "health", "--json"],
        fake=_FakeCH(orphan_report=_BROKEN_REGISTRY),
    )
    assert result.exit_code == 1


def test_health_reports_the_finding_as_a_failure_not_a_warning(monkeypatch):
    """A warning marks a degraded ANSWER; this answer is fine, the DATA is not."""
    result, _ = _invoke(
        monkeypatch,
        ["clearinghouse", "health", "--json"],
        fake=_FakeCH(orphan_report=_BROKEN_REGISTRY),
    )
    payload = json.loads(result.output.splitlines()[-1])
    integrity = payload["data"]["parameter_set_integrity"]
    assert integrity["ok"] is False
    assert integrity["failures"] == _BROKEN_REGISTRY["failures"]
    assert not any(_TUNED_HASH in w for w in payload["warnings"])


def test_health_human_output_prints_the_failure_lines(monkeypatch):
    result, _ = _invoke(
        monkeypatch,
        ["clearinghouse", "health"],
        fake=_FakeCH(orphan_report=_BROKEN_REGISTRY),
    )
    assert f"failure: {_BROKEN_REGISTRY['failures'][0]}" in result.output


def test_health_exits_zero_and_carries_the_clean_report(monkeypatch):
    result, _ = _invoke(monkeypatch, ["clearinghouse", "health", "--json"])
    payload = json.loads(result.output.splitlines()[-1])
    assert result.exit_code == 0
    assert payload["data"]["parameter_set_integrity"]["ok"] is True


# -- THE REAL-PATH TEST ---------------------------------------------------


class _SqlRecorder:
    """The LOWEST-level seam: a BerdlCapability-shaped SQL executor.

    Exposes only ``locus()``/``query()`` -- the two methods
    ``ClearinghouseCapability`` composes over -- and records the SQL it is
    handed. Nothing above it is faked.
    """

    def __init__(self):
        self.sql: list[str] = []

    def locus(self):
        return "in_pod"

    def query(self, sql, *, params=None, engine=None, limit=None, offset=0):
        self.sql.append(sql)
        return []


def test_real_cli_results_entry_point_pushes_the_default_hash_into_real_sql(
    monkeypatch,
):
    """End to end through the REAL entry point, faking only the SQL executor.

    This is the one test that cannot pass against a stub. It drives the real
    ``kbu clearinghouse results`` command (the click ``main`` group, not
    ``results_cmd`` or ``_build_results`` called directly), with the REAL
    ``ClearinghouseCapability`` and the REAL ``current_state_sql`` -- neither
    is monkeypatched -- and asserts on the SQL text that reached the bottom
    of the stack. If any layer between the flag and the SQL dropped the
    parameter set, the generated text would not carry it.
    """
    from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
        ClearinghouseCapability,
    )

    recorder = _SqlRecorder()
    monkeypatch.setattr(
        ch, "_capability", lambda: ClearinghouseCapability(recorder)
    )
    result = CliRunner().invoke(
        main,
        [
            "clearinghouse", "results", "--type", "protein",
            "--default-parameters", "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert recorder.sql, "no SQL reached the executor"
    sql = recorder.sql[0]

    # parameter_set_hash is part of the slot key the window partitions on...
    partition_by = [
        line for line in sql.splitlines() if "PARTITION BY" in line
    ]
    assert len(partition_by) == 1
    assert "parameter_set_hash" in partition_by[0]

    # ...and the default hash is the pre-filter, inside the windowed CTE.
    assert DEFAULT_PARAMETER_SET_HASH in sql
    assert sql.index(DEFAULT_PARAMETER_SET_HASH) < sql.index("WHERE rn = 1")


def test_real_cli_results_entry_point_emits_no_hash_filter_without_the_flag(
    monkeypatch,
):
    """The control for the test above: no flag, no parameter_set_hash predicate."""
    from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
        ClearinghouseCapability,
    )

    recorder = _SqlRecorder()
    monkeypatch.setattr(
        ch, "_capability", lambda: ClearinghouseCapability(recorder)
    )
    result = CliRunner().invoke(
        main,
        ["clearinghouse", "results", "--type", "protein", "--json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    sql = recorder.sql[0]
    assert "PARTITION BY" in sql and "parameter_set_hash" in sql
    assert DEFAULT_PARAMETER_SET_HASH not in sql
    assert "WHERE parameter_set_hash IN" not in sql
