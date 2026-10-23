"""Tests for ``kbu ontomap`` -- the function-to-GO pipeline's operator surface.

Nothing here spends a token, makes a network call or needs a GPU. The
``version`` verb is driven two ways:

* against a **fake facade** (``ontomap._ontomap`` monkeypatched), which is what
  lets the envelope contract be asserted on a host that has not got the
  retrieval backend's numpy/sklearn/joblib stack;
* against the **real** :class:`OntologyMappingUtils`, skipped when that stack
  is absent, so the facade method the CLI calls is proved to exist and to
  return the three keys the envelope promises.

The four verbs whose stages have not landed are REGISTERED STUBS, and the
thing these tests are here to pin is that every one of them exits NON-ZERO. A
stub that exited 0 would let a rehearsal of the whole pipeline pass over a
missing stage and report success.

The published envelope (from ``ontomap.py``'s docstring)::

    {"schema_version": 1, "verb": ..., "generated_at": ..., "data": {...},
     "warnings": [...]}
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest
from click.testing import CliRunner

import kbutillib.interfaces.cli.ontomap as ontomap
from kbutillib.cli import main

#: The envelope keys every verb emits -- the same set ``kbu council`` and
#: ``kbu clearinghouse`` publish. Pinned as a set so an additive field is a
#: deliberate edit here rather than a silent contract change.
ENVELOPE_KEYS = {"schema_version", "verb", "generated_at", "data", "warnings"}

#: The verbs still registered-but-not-implemented. ``council-quote``,
#: ``council-plan`` and ``council-export`` have LEFT this list: stages 2, 1
#: and the first half of 5 landed, so each is now a real verb -- one prices a
#: plan, one writes one, one exports the verdicts (see
#: :class:`TestCouncilQuoteIsImplemented`,
#: :class:`TestCouncilPlanIsImplemented` and
#: :class:`TestCouncilExportIsImplemented`). A verb leaving this list is the
#: expected shape of progress through the five stages.
STUB_VERBS = [
    "council-approve",
    "council-run",
    "council-status",
    "council-publish",
]

#: The verbs that ARE implemented, so the registration test can assert the
#: registered set is exactly the two lists together -- a new verb must land in
#: one of them rather than appearing unnoticed.
IMPLEMENTED_VERBS = ["version", "council-plan", "council-quote", "council-export"]

#: What a real ``mapping_version()`` returns, shaped like the production value
#: measured on this tree (``ontomap-go-text-union@1.0.0``, GO 2026-07-26).
FAKE_VERSION: Dict[str, Any] = {
    "mapping_version": "ontomap-go-text-union@1.0.0 (go-text 2.0.0+4.0.0; GO releases/2026-07-26)",
    "mapping_hash": "f" * 64,
    "provenance": {
        "backend": "ontomap-go-text-union",
        "backend_version": "1.0.0",
        "models": [
            {"artifact": "v2", "ontomap_method_version": "2.0.0", "sha256": "a" * 64},
            {"artifact": "v4", "ontomap_method_version": "4.0.0", "sha256": "b" * 64},
        ],
        "go_release": "releases/2026-07-26",
        "go_metadata_sha256": "c" * 64,
        "placeholder_rules": "1.0.0",
        "params": {"export_top_n": 10, "per_model_top_k": 20},
    },
}


class _FakeOntomap:
    """A facade whose only job is to answer ``mapping_version()``."""

    def __init__(self, payload: Dict[str, Any] | None = None) -> None:
        self.payload = payload if payload is not None else FAKE_VERSION
        self.calls = 0

    def mapping_version(self) -> Dict[str, Any]:
        self.calls += 1
        return self.payload


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _separated_runner() -> CliRunner:
    """A runner that keeps stdout and stderr APART.

    Only for tests that parse ``result.stdout`` as JSON while a warning is
    also emitted (warnings go to stderr; see ``_emit``'s stdout discipline).
    The plain ``runner`` fixture above deliberately keeps click's default
    (mixed streams under click < 8.2), because the rest of this file's tests
    -- the ``ClickException`` and stub-verb cases -- check ``result.output``
    expecting the error text that ``click.ClickException`` writes to stderr
    to be folded into it.

    Click 8.2 removed ``mix_stderr`` and made separate streams the only
    behaviour, so it is probed rather than assumed (see the same pattern in
    ``tests/cli/test_clearinghouse_backfill_cli.py`` and
    ``tests/cli/test_council_cli.py``).
    """
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:  # pragma: no cover - depends on the installed click
        return CliRunner()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeOntomap:
    """Patch the ONE factory every verb builds its facade through."""
    util = _FakeOntomap()
    monkeypatch.setattr(ontomap, "_ontomap", lambda: util)
    return util


# ---------------------------------------------------------------------------
# The group and its help listing
# ---------------------------------------------------------------------------


class TestGroupRegistration:
    def test_ontomap_is_on_the_top_level_kbu_group(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0, result.output
        assert "ontomap" in result.output

    def test_help_lists_all_eight_commands(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ontomap", "--help"])
        assert result.exit_code == 0, result.output
        commands = result.output.split("Commands:", 1)[1]
        for verb in [*IMPLEMENTED_VERBS, *STUB_VERBS]:
            assert verb in commands, f"{verb} missing from the help listing"

    def test_help_describes_the_five_pipeline_stages(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ontomap", "--help"])
        assert result.exit_code == 0, result.output
        for stage in ("1.", "2.", "3.", "4.", "5."):
            assert stage in result.output
        assert "parameter_set_hash" in result.output

    def test_every_command_takes_json(self, runner: CliRunner) -> None:
        for verb in [*IMPLEMENTED_VERBS, *STUB_VERBS]:
            result = runner.invoke(main, ["ontomap", verb, "--help"])
            assert result.exit_code == 0, result.output
            assert "--json" in result.output, f"{verb} has no --json flag"


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


class TestVersion:
    def test_json_emits_one_envelope_with_the_three_identity_keys(
        self, runner: CliRunner, fake: _FakeOntomap
    ) -> None:
        result = runner.invoke(main, ["ontomap", "version", "--json"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert set(envelope) == ENVELOPE_KEYS
        assert envelope["schema_version"] == ontomap.SCHEMA_VERSION == 1
        assert envelope["verb"] == "version"
        assert envelope["generated_at"].endswith("Z")
        assert envelope["warnings"] == []
        data = envelope["data"]
        assert set(data) == {"mapping_version", "mapping_hash", "provenance"}
        assert data["mapping_version"] == FAKE_VERSION["mapping_version"]
        assert data["mapping_hash"] == FAKE_VERSION["mapping_hash"]
        assert data["provenance"] == FAKE_VERSION["provenance"]
        assert fake.calls == 1

    def test_json_stdout_is_exactly_one_object(
        self, runner: CliRunner, fake: _FakeOntomap
    ) -> None:
        result = runner.invoke(main, ["ontomap", "version", "--json"])
        assert result.exit_code == 0, result.output
        assert len([line for line in result.stdout.splitlines() if line.strip()]) == 1

    def test_human_output_renders_the_same_envelope(
        self, runner: CliRunner, fake: _FakeOntomap, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Dict[str, Any]] = []
        monkeypatch.setattr(ontomap, "_render", seen.append)
        result = runner.invoke(main, ["ontomap", "version"])
        assert result.exit_code == 0, result.output
        assert len(seen) == 1
        assert seen[0]["verb"] == "version"
        assert seen[0]["data"]["mapping_hash"] == FAKE_VERSION["mapping_hash"]

    def test_human_output_names_the_version_hash_and_models(
        self, runner: CliRunner, fake: _FakeOntomap
    ) -> None:
        result = runner.invoke(main, ["ontomap", "version"])
        assert result.exit_code == 0, result.output
        assert "mapping_version:" in result.output
        assert FAKE_VERSION["mapping_version"] in result.output
        assert FAKE_VERSION["mapping_hash"] in result.output
        assert "releases/2026-07-26" in result.output
        assert "ontomap_method_version" in result.output

    def test_a_backend_that_cannot_be_built_is_a_cli_error_not_a_traceback(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Any:
            raise ModuleNotFoundError("No module named 'numpy'")

        monkeypatch.setattr(ontomap, "_ontomap", _boom)
        result = runner.invoke(main, ["ontomap", "version", "--json"])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "ModuleNotFoundError" in result.output
        assert "numpy" in result.output

    def test_a_backend_that_raises_is_a_cli_error_not_a_traceback(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Broken:
            def mapping_version(self) -> Dict[str, Any]:
                raise RuntimeError("go metadata missing")

        monkeypatch.setattr(ontomap, "_ontomap", _Broken)
        result = runner.invoke(main, ["ontomap", "version"])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "RuntimeError: go metadata missing" in result.output


class TestVersionAgainstTheRealBackend:
    """Nothing patched: the verb must reach the real facade method.

    Skipped where the retrieval backend's optional stack is absent -- which is
    precisely why the CLI builds the facade through a lazy factory and turns an
    import failure into a clean CLI error.
    """

    def test_real_mapping_version_fills_the_envelope(self, runner: CliRunner) -> None:
        pytest.importorskip("numpy")
        pytest.importorskip("sklearn")
        pytest.importorskip("joblib")
        result = runner.invoke(main, ["ontomap", "version", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert data["mapping_version"].startswith("ontomap-go-text-union@")
        assert len(data["mapping_hash"]) == 64
        assert data["provenance"]["models"]


# ---------------------------------------------------------------------------
# The stubs -- the point of this test module
# ---------------------------------------------------------------------------


class TestStubsExitNonZero:
    @pytest.mark.parametrize("verb", STUB_VERBS)
    def test_stub_exits_non_zero_and_says_not_implemented(
        self, runner: CliRunner, verb: str
    ) -> None:
        result = runner.invoke(main, ["ontomap", verb])
        assert result.exit_code != 0, f"{verb} exited 0 -- a rehearsal would pass over it"
        assert "not implemented yet" in result.output

    @pytest.mark.parametrize("verb", STUB_VERBS)
    def test_stub_exits_non_zero_under_json_too(
        self, runner: CliRunner, verb: str
    ) -> None:
        result = runner.invoke(main, ["ontomap", verb, "--json"])
        assert result.exit_code != 0, f"{verb} --json exited 0"
        assert "not implemented yet" in result.output

    @pytest.mark.parametrize("verb", STUB_VERBS)
    def test_stub_never_emits_a_success_envelope(
        self, runner: CliRunner, verb: str
    ) -> None:
        # A consumer parsing `--json` output must not find an envelope here:
        # the only thing written is the refusal, and it is a click error (so
        # stderr), which `CliRunner` folds into ``output`` on click 8.1.
        result = runner.invoke(main, ["ontomap", verb, "--json"])
        for line in result.output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            assert not stripped.startswith("{"), f"{verb} emitted a JSON object"
        assert "schema_version" not in result.output

    def test_every_registered_verb_is_either_implemented_or_a_known_stub(
        self,
    ) -> None:
        registered = set(ontomap.ontomap_cmd.commands)
        assert registered == {*IMPLEMENTED_VERBS, *STUB_VERBS}
        assert [name for name, _ in ontomap._PLANNED_VERBS] == STUB_VERBS


class TestCouncilQuoteIsImplemented:
    """Stage 2 landed, so ``council-quote`` must no longer refuse.

    The end-to-end behaviour is covered beside the command it delegates to
    (``tests/cli/test_council_cli.py::TestOntomapCouncilQuote``); what is
    pinned here is only that this group no longer registers it as a stub,
    because a stub that quietly came back would make the whole pipeline
    un-runnable from the operator surface.
    """

    def test_it_is_not_registered_as_a_stub(self) -> None:
        assert "council-quote" not in [name for name, _ in ontomap._PLANNED_VERBS]
        assert "council-quote" in ontomap.ontomap_cmd.commands

    def test_it_does_not_answer_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ontomap", "council-quote", "--help"])
        assert result.exit_code == 0, result.output
        assert "not implemented yet" not in result.output

    def test_it_requires_a_plan_rather_than_refusing_outright(
        self, runner: CliRunner
    ) -> None:
        """Invoked bare it is a USAGE error (no --plan), not a refusal."""
        result = runner.invoke(main, ["ontomap", "council-quote"])
        assert result.exit_code != 0
        assert "not implemented yet" not in result.output
        assert "--plan" in result.output


class TestCouncilPlanIsImplemented:
    """Stage 1 landed, so ``council-plan`` must no longer refuse.

    The planning behaviour itself is covered beside the facade method that
    owns it (``tests/domains/test_ontomap_council_plan.py``); what is pinned
    here is the OPERATOR SURFACE -- that the verb is no longer a stub, that it
    emits the one envelope under ``--json``, that it passes the operator's
    options through to the facade unchanged, and that a planning failure is a
    clean CLI error rather than a traceback.
    """

    #: What a real ``council_plan()`` returns, shaped like the production
    #: value (see the facade's own tests for the full object).
    FAKE_PLAN: Dict[str, Any] = {
        "schema_version": 1,
        "run_dir": "/runs/2026-10-22-adp1",
        "plan_file": "/runs/2026-10-22-adp1/PLAN.json",
        "query": "go_mf@1",
        "source": "council/go_mf@1",
        "profile": "go_council_v3",
        "mapping_hash": "e" * 64,
        "bench_fingerprint": "b" * 64,
        "roster_fingerprint": "r" * 64,
        "parameter_set": {
            "query": "go_mf@1",
            "bench_fingerprint": "b" * 64,
            "mapping_hash": "e" * 64,
        },
        "parameter_set_hash": "0" * 64,
        "transport_allocation": [
            {
                "role": "proposer",
                "seat_name": "proposer_1",
                "gateway": "argo-anl",
                "endpoint_kind": "argo",
                "canonical_model": "gemini-3.5-flash",
                "route_fingerprint": "1" * 64,
            }
        ],
        "counts": {
            "universe": 20036,
            "done": 0,
            "unrun": 20036,
            "submit": 19276,
            "abstain": 760,
            "fanout": 19276,
        },
        "chunks": [{"index": 0, "offset": 0, "items": 2000}],
        "concurrency": {
            "chunk_size": 2000,
            "chunks_in_flight": 1,
            "issue": "KBUtilLib issue 84",
        },
        "abstain_file": "/scratch/function-to-go/2026-10-22-adp1/abstain.jsonl",
        "plan_hash": "f" * 64,
    }

    class _FakePlanner:
        """A facade whose only job is to answer ``council_plan()``."""

        def __init__(self, payload: Dict[str, Any], boom: Any = None) -> None:
            self.payload = payload
            self.boom = boom
            self.kwargs: Dict[str, Any] = {}

        def council_plan(self, run_dir: Any, **kwargs: Any) -> Dict[str, Any]:
            if self.boom is not None:
                raise self.boom
            self.kwargs = {"run_dir": run_dir, **kwargs}
            return dict(self.payload)

    def _planner(
        self, monkeypatch: pytest.MonkeyPatch, boom: Any = None
    ) -> "TestCouncilPlanIsImplemented._FakePlanner":
        planner = self._FakePlanner(self.FAKE_PLAN, boom)
        monkeypatch.setattr(ontomap, "_ontomap", lambda: planner)
        return planner

    def test_it_is_not_registered_as_a_stub(self) -> None:
        assert "council-plan" not in [name for name, _ in ontomap._PLANNED_VERBS]
        assert "council-plan" in ontomap.ontomap_cmd.commands

    def test_it_does_not_answer_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ontomap", "council-plan", "--help"])
        assert result.exit_code == 0, result.output
        assert "not implemented yet" not in result.output

    def test_invoked_bare_it_is_a_usage_error_not_a_refusal(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(main, ["ontomap", "council-plan"])
        assert result.exit_code != 0
        assert "not implemented yet" not in result.output
        assert "--run-dir" in result.output

    def test_json_emits_exactly_one_envelope_carrying_the_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._planner(monkeypatch)
        result = _separated_runner().invoke(
            main, ["ontomap", "council-plan", "--run-dir", "/runs/x", "--json"]
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert set(envelope) == ENVELOPE_KEYS
        assert envelope["verb"] == "council-plan"
        data = envelope["data"]
        assert data["counts"]["submit"] == 19276
        assert data["plan_hash"] == "f" * 64
        assert data["parameter_set"]["bench_fingerprint"] == "b" * 64
        # The partition key is the bench, never the roster.
        assert "roster_fingerprint" not in data["parameter_set"]
        assert data["roster_fingerprint"] == "r" * 64

    def test_the_abstained_set_is_warned_about_rather_than_passed_over(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """760 declined functions are not a silent 760-function shortfall."""
        self._planner(monkeypatch)
        result = _separated_runner().invoke(
            main, ["ontomap", "council-plan", "--run-dir", "/runs/x", "--json"]
        )
        (warning,) = json.loads(result.stdout)["warnings"]
        assert "760" in warning
        assert "abstained:*" in warning

    def test_an_empty_submit_set_is_a_warning_not_a_silent_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            **self.FAKE_PLAN,
            "counts": {
                "universe": 20036,
                "done": 20036,
                "unrun": 0,
                "submit": 0,
                "abstain": 0,
                "fanout": 0,
            },
            "chunks": [],
        }
        planner = self._FakePlanner(payload)
        monkeypatch.setattr(ontomap, "_ontomap", lambda: planner)
        result = _separated_runner().invoke(
            main, ["ontomap", "council-plan", "--run-dir", "/runs/x", "--json"]
        )
        assert result.exit_code == 0, result.output
        warnings = json.loads(result.stdout)["warnings"]
        assert any("NO submit items" in warning for warning in warnings)

    def test_the_human_rendering_is_the_same_dict(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._planner(monkeypatch)
        result = runner.invoke(main, ["ontomap", "council-plan", "--run-dir", "/runs/x"])
        assert result.exit_code == 0, result.output
        assert "/runs/2026-10-22-adp1/PLAN.json" in result.output
        assert "submit=19276" in result.output
        assert "KBUtilLib issue 84" in result.output
        assert "proposer_1" in result.output and "argo-anl" in result.output

    def test_the_operators_options_reach_the_facade_unchanged(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner = self._planner(monkeypatch)
        result = runner.invoke(
            main,
            [
                "ontomap",
                "council-plan",
                "--run-dir",
                "/runs/x",
                "--query",
                "go_mf@2",
                "--profile",
                "cheap",
                "--chunk-size",
                "500",
                "--scratch-dir",
                "/scratch/here",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert planner.kwargs == {
            "run_dir": "/runs/x",
            "query": "go_mf@2",
            "profile": "cheap",
            "chunk_size": 500,
            "scratch_dir": "/scratch/here",
        }

    def test_the_defaults_are_the_librarys_own(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kbutillib.domains.genome.ontology_mapping.ontology_mapping_utils import (
            DEFAULT_COUNCIL_QUERY,
            PLAN_CHUNK_SIZE,
        )

        planner = self._planner(monkeypatch)
        runner.invoke(main, ["ontomap", "council-plan", "--run-dir", "/runs/x", "--json"])
        assert planner.kwargs["query"] == DEFAULT_COUNCIL_QUERY
        assert planner.kwargs["chunk_size"] == PLAN_CHUNK_SIZE
        assert planner.kwargs["profile"] is None

    def test_a_planning_failure_is_a_cli_error_not_a_traceback(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._planner(monkeypatch, boom=ValueError("query 'go_mf' names no VERSION"))
        result = runner.invoke(
            main, ["ontomap", "council-plan", "--run-dir", "/runs/x", "--json"]
        )
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "ValueError: query 'go_mf' names no VERSION" in result.output
        assert "schema_version" not in result.output


class TestCouncilExportIsImplemented:
    """Stage 5's export landed, so ``council-export`` must no longer refuse.

    The export behaviour itself is covered beside the code that owns it
    (``tests/domains/test_verdict_export.py`` for the pure row derivation,
    ``tests/domains/test_ontomap_council_export.py`` for the facade method);
    what is pinned here is the OPERATOR SURFACE -- that the verb is no longer
    a stub, that it emits the one envelope, that the operator's options reach
    the facade unchanged, that withheld items are WARNED about rather than
    passed over, and that a failure is a clean CLI error.
    """

    #: What a real ``council_export()`` returns.
    FAKE_EXPORT: Dict[str, Any] = {
        "out_path": "/scratch/function-to-go/2026-10-22-adp1/verdicts.parquet",
        "rows": 20036,
        "bytes": 4194304,
        "counts": {
            "inputs": 20036,
            "rows": 20036,
            "withheld": 0,
            "abstained": 760,
            "decided": 19276,
            "with_go_ids": 9920,
            "retried": 105,
            "no_candidate_matched": 4893,
            "contentless": 4463,
            "no_candidates": 0,
        },
        "plan_file": "/runs/2026-10-22-adp1/PLAN.json",
        "plan_hash": "f" * 64,
        "query": "go_mf@1",
        "query_name": "go_mf",
        "query_version": "1",
        "source": "council/go_mf@1",
        "parameter_set_hash": "0" * 64,
        "mapping_hash": "e" * 64,
        "roster_fingerprint": "r" * 64,
        "job_ids": ["job-a", "job-retry"],
        "retry_job_ids": ["job-retry"],
        "warnings": [],
    }

    class _FakeExporter:
        """A facade whose only job is to answer ``council_export()``."""

        def __init__(self, payload: Dict[str, Any], boom: Any = None) -> None:
            self.payload = payload
            self.boom = boom
            self.kwargs: Dict[str, Any] = {}

        def council_export(self, **kwargs: Any) -> Dict[str, Any]:
            if self.boom is not None:
                raise self.boom
            self.kwargs = dict(kwargs)
            return dict(self.payload)

    def _exporter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: Dict[str, Any] | None = None,
        boom: Any = None,
    ) -> "TestCouncilExportIsImplemented._FakeExporter":
        exporter = self._FakeExporter(payload or self.FAKE_EXPORT, boom)
        monkeypatch.setattr(ontomap, "_ontomap", lambda: exporter)
        return exporter

    def test_it_is_not_registered_as_a_stub(self) -> None:
        assert "council-export" not in [name for name, _ in ontomap._PLANNED_VERBS]
        assert "council-export" in ontomap.ontomap_cmd.commands

    def test_it_does_not_answer_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ontomap", "council-export", "--help"])
        assert result.exit_code == 0, result.output
        assert "not implemented yet" not in result.output

    def test_invoked_bare_it_is_a_usage_error_not_a_refusal(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(main, ["ontomap", "council-export"])
        assert result.exit_code != 0
        assert "not implemented yet" not in result.output
        assert "--run-dir" in result.output

    def test_json_emits_exactly_one_envelope_carrying_the_export(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        self._exporter(monkeypatch)
        result = _separated_runner().invoke(
            main, ["ontomap", "council-export", "--run-dir", str(tmp_path), "--json"]
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert set(envelope) == ENVELOPE_KEYS
        assert envelope["verb"] == "council-export"
        assert envelope["warnings"] == []
        data = envelope["data"]
        assert data["counts"]["rows"] == data["counts"]["inputs"] == 20036
        assert data["counts"]["no_candidate_matched"] == 4893
        assert data["out_path"].endswith(".parquet")

    def test_withheld_items_are_warned_about_rather_than_passed_over(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """8 items with no verdict are not a silent 8-function shortfall."""
        payload = {
            **self.FAKE_EXPORT,
            "rows": 20028,
            "counts": {**self.FAKE_EXPORT["counts"], "rows": 20028, "withheld": 8},
            "warnings": ["8 of 20036 input function(s) were WITHHELD"],
        }
        self._exporter(monkeypatch, payload)
        result = _separated_runner().invoke(
            main, ["ontomap", "council-export", "--run-dir", str(tmp_path), "--json"]
        )
        assert result.exit_code == 0, result.output
        warnings = json.loads(result.stdout)["warnings"]
        assert any("WITHHELD" in warning for warning in warnings)

    def test_an_empty_export_is_a_warning_not_a_silent_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        payload = {
            **self.FAKE_EXPORT,
            "rows": 0,
            "counts": {**self.FAKE_EXPORT["counts"], "rows": 0, "withheld": 20036},
            "warnings": [],
        }
        self._exporter(monkeypatch, payload)
        result = _separated_runner().invoke(
            main, ["ontomap", "council-export", "--run-dir", str(tmp_path), "--json"]
        )
        assert result.exit_code == 0, result.output
        warnings = json.loads(result.stdout)["warnings"]
        assert any("NO rows" in warning for warning in warnings)

    def test_the_human_rendering_is_the_same_dict(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        self._exporter(monkeypatch)
        result = runner.invoke(
            main, ["ontomap", "council-export", "--run-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert "verdicts.parquet" in result.output
        assert "inputs=20036" in result.output
        assert "no_candidate_matched=4893" in result.output
        assert "job-retry" in result.output

    def test_the_operators_options_reach_the_facade_unchanged(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        exporter = self._exporter(monkeypatch)
        result = runner.invoke(
            main,
            [
                "ontomap",
                "council-export",
                "--run-dir",
                str(tmp_path),
                "--out",
                "/scratch/out.parquet",
                "--job-id",
                "job-a",
                "--job-id",
                "job-b",
                "--retry-job-id",
                "job-retry",
                "--scratch-dir",
                "/scratch/here",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert exporter.kwargs == {
            "run_dir": str(tmp_path),
            "out_path": "/scratch/out.parquet",
            "job_ids": ["job-a", "job-b"],
            "retry_job_ids": ["job-retry"],
            "scratch_dir": "/scratch/here",
        }

    def test_omitted_options_are_passed_as_none_so_the_library_defaults(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        exporter = self._exporter(monkeypatch)
        runner.invoke(
            main, ["ontomap", "council-export", "--run-dir", str(tmp_path), "--json"]
        )
        assert exporter.kwargs["out_path"] is None
        assert exporter.kwargs["job_ids"] is None
        assert exporter.kwargs["retry_job_ids"] is None

    def test_an_export_failure_is_a_cli_error_not_a_traceback(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        self._exporter(
            monkeypatch, boom=ValueError("council_export: no council job ids")
        )
        result = runner.invoke(
            main, ["ontomap", "council-export", "--run-dir", str(tmp_path), "--json"]
        )
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "ValueError: council_export: no council job ids" in result.output
        assert "schema_version" not in result.output
