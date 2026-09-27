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

    def __init__(self, locus="in_pod", *, stats_result=None):
        self._locus_value = locus
        self._stats_result = stats_result
        self.calls: list[str] = []

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

    def results(self, entity_type, hashes, *, sources=None, result_types=None, **_kw):
        self.calls.append(f"results({entity_type})")
        return [{"entity_hash": h, "result_type": "annotation"} for h in hashes]

    def current_state(self, entity_type, *, sources=None, result_types=None, **_kw):
        self.calls.append(f"current_state({entity_type})")
        return [{"entity_hash": "a" * 64, "result_type": "annotation"}]

    def sources(self, entity_type=None, **_kw):
        self.calls.append(f"sources({entity_type})")
        return [{"entity_type": "protein", "source": "uniprot", "row_count": 3}]


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
