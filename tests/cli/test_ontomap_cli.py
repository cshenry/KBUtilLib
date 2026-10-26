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
from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

import kbutillib.interfaces.cli.ontomap as ontomap
from kbutillib.cli import main

#: The envelope keys every verb emits -- the same set ``kbu council`` and
#: ``kbu clearinghouse`` publish. Pinned as a set so an additive field is a
#: deliberate edit here rather than a silent contract change.
ENVELOPE_KEYS = {"schema_version", "verb", "generated_at", "data", "warnings"}

#: The verbs still registered-but-not-implemented. **EMPTY, and that is the
#: END STATE rather than a gap**: every pipeline verb has LEFT this list, with
#: ``council-status`` the last to go (see :class:`TestCouncilStatus`). Stages
#: 1, 2, 3, 4 and both halves of 5's producer side have all landed.
#:
#: Kept as a list, and the stub machinery kept with it, so the next verb
#: designed ahead of its implementation has somewhere to go -- the convention
#: is what stops a rehearsal of the whole pipeline reporting success over a
#: stage that does not exist.
STUB_VERBS: List[str] = []

#: The verbs that ARE implemented, so the registration test can assert the
#: registered set is exactly the two lists together -- a new verb must land in
#: one of them rather than appearing unnoticed.
IMPLEMENTED_VERBS = [
    "version",
    # Stage 1a, the IN-POD half of stage 1: it reads the clearinghouse and
    # emits an immutable un-run bundle, so the scheduled off-pod planner
    # needs no BERDL credential.
    "council-unrun-bundle",
    "council-plan",
    "council-quote",
    "council-approve",
    "council-run",
    "council-status",
    "council-export",
    "council-publish",
    # Stage 5's producer half AGAIN, but sourced from the LAKEHOUSE rather
    # than from a finished run's directory: a pinned council snapshot plus the
    # campaign's publication bundle in, the same rendered preset and the same
    # shard run out. Registered here because it is implemented, not planned.
    "council-publish-lakehouse",
]

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


class TestCouncilPublishIsImplemented:
    """Stage 5's producer half landed, so ``council-publish`` must not refuse.

    The end-to-end behaviour is covered beside the function it delegates to
    (``tests/domains/test_ontomap_council_publish.py``); what is pinned here
    is only that this group no longer registers it as a stub, because a stub
    that quietly came back would make the whole pipeline un-runnable from the
    operator surface -- and a publish stage that exited 0 while publishing
    nothing is the exact failure the stub convention exists to prevent.
    """

    def test_it_is_not_registered_as_a_stub(self) -> None:
        assert "council-publish" not in [name for name, _ in ontomap._PLANNED_VERBS]
        assert "council-publish" in ontomap.ontomap_cmd.commands

    def test_it_does_not_answer_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ontomap", "council-publish", "--help"])
        assert result.exit_code == 0, result.output
        assert "not implemented yet" not in result.output

    def test_it_requires_a_run_dir_rather_than_refusing_outright(
        self, runner: CliRunner
    ) -> None:
        """Invoked bare it is a USAGE error (no --run-dir), not a refusal."""
        result = runner.invoke(main, ["ontomap", "council-publish"])
        assert result.exit_code != 0
        assert "not implemented yet" not in result.output
        assert "--run-dir" in result.output


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
            # None, because no --bundle was given: this invocation is the
            # original single-command path, which reads the clearinghouse
            # itself. The bundle path is covered beside the facade, in
            # tests/domains/test_ontomap_unrun_bundle.py.
            "bundle": None,
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


# ---------------------------------------------------------------------------
# council-approve -- stage 3
# ---------------------------------------------------------------------------


class TestCouncilApprove:
    """Stage 3 landed, so ``council-approve`` must no longer refuse.

    The gate itself -- the ceiling arithmetic and the six checks stage 4 makes
    -- is covered beside the module that owns it
    (``tests/domains/ai/council/test_approval.py``). What is pinned here is
    the OPERATOR SURFACE: that the verb writes the artifact, that the ceiling
    an operator reads comes off the quote's UPPER band rather than its point
    estimate, that the help says plainly what the artifact is NOT, and that an
    unapprovable run directory is a clean CLI error.

    Unlike the other verbs this one does not go through ``_ontomap()``: an
    approval is two files on disk and a clock, with no retrieval backend
    anywhere near it, so these tests run against REAL ``PLAN.json`` and
    ``QUOTE.json`` files.
    """

    #: Upper-band tokens the fixture quote attributes to each gateway.
    ARGO_UPPER = 120_000_000
    MAG_UPPER = 80_000_000

    #: The point estimate, deliberately well below the band, so a ceiling
    #: taken from the wrong column is visible in the number the CLI prints.
    ARGO_POINT = 100_000_000
    MAG_POINT = 60_000_000

    def _run_dir(self, tmp_path: Any) -> Any:
        """A run directory holding a real plan and a quote priced against it."""
        from kbutillib.domains.ai.council.quote import load_plan

        directory = tmp_path / "2026-10-23-adp1"
        directory.mkdir()
        plan = {
            "schema_version": 1,
            "query": "go_mf@1",
            "profile": "go_council_v3",
            "mapping_hash": "e" * 64,
            "bench_fingerprint": "b" * 64,
            "roster_fingerprint": "r" * 64,
            "items": [
                {
                    "item_id": "t0",
                    "query_id": "a" * 64,
                    "evidence": {"text": "hydrolase", "source": "ADP1"},
                    "menu": [{"id": "GO:0016787", "label": "hydrolase activity"}],
                }
            ],
        }
        (directory / "PLAN.json").write_text(json.dumps(plan), encoding="utf-8")
        plan["plan_hash"] = load_plan(directory).plan_hash
        (directory / "PLAN.json").write_text(json.dumps(plan), encoding="utf-8")
        (directory / "QUOTE.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "plan_hash": plan["plan_hash"],
                    "headline": {
                        "point": {"total_tokens": self.ARGO_POINT + self.MAG_POINT},
                        "upper": {"total_tokens": self.ARGO_UPPER + self.MAG_UPPER},
                    },
                    "gateways": [
                        {
                            "gateway": "argo",
                            "total_tokens_point": self.ARGO_POINT,
                            "total_tokens_upper": self.ARGO_UPPER,
                        },
                        {
                            "gateway": "mag",
                            "total_tokens_point": self.MAG_POINT,
                            "total_tokens_upper": self.MAG_UPPER,
                        },
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return directory

    def test_it_is_not_registered_as_a_stub(self) -> None:
        assert "council-approve" not in [name for name, _ in ontomap._PLANNED_VERBS]
        assert "council-approve" in ontomap.ontomap_cmd.commands

    def test_it_writes_approved_json_and_emits_one_envelope(
        self, runner: CliRunner, tmp_path: Any
    ) -> None:
        run_dir = self._run_dir(tmp_path)
        result = runner.invoke(
            main,
            [
                "ontomap",
                "council-approve",
                str(run_dir),
                "--approver",
                "chenry",
                "--note",
                "ADP1+PMI standing run",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert set(envelope) == ENVELOPE_KEYS
        assert envelope["verb"] == "council-approve"
        data = envelope["data"]
        for key in (
            "approved_at",
            "expires_at",
            "approver",
            "plan_hash",
            "quote_hash",
            "ceiling_tokens",
            "note",
        ):
            assert key in data, f"the envelope carries no {key}"
        assert data["approver"] == "chenry"
        assert data["note"] == "ADP1+PMI standing run"
        written = json.loads(
            (run_dir / "APPROVED.json").read_text(encoding="utf-8")
        )
        assert written["quote_hash"] == data["quote_hash"]

    def test_the_ceiling_is_the_quotes_upper_band_not_its_point_estimate(
        self, runner: CliRunner, tmp_path: Any
    ) -> None:
        run_dir = self._run_dir(tmp_path)
        result = runner.invoke(
            main, ["ontomap", "council-approve", str(run_dir), "--json"]
        )
        assert result.exit_code == 0, result.output
        ceiling = json.loads(result.stdout)["data"]["ceiling_tokens"]
        assert ceiling["argo"] > self.ARGO_UPPER > self.ARGO_POINT
        assert ceiling["mag"] > self.MAG_UPPER > self.MAG_POINT

    def test_ceiling_tokens_overrides_one_gateway(
        self, runner: CliRunner, tmp_path: Any
    ) -> None:
        run_dir = self._run_dir(tmp_path)
        result = runner.invoke(
            main,
            [
                "ontomap",
                "council-approve",
                str(run_dir),
                "--ceiling-tokens",
                "mag=250000000",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        ceiling = json.loads(result.stdout)["data"]["ceiling_tokens"]
        assert ceiling["mag"] == 250_000_000
        assert ceiling["argo"] != 250_000_000

    def test_a_malformed_ceiling_option_is_a_clean_cli_error(
        self, runner: CliRunner, tmp_path: Any
    ) -> None:
        run_dir = self._run_dir(tmp_path)
        result = runner.invoke(
            main,
            ["ontomap", "council-approve", str(run_dir), "--ceiling-tokens", "argo"],
        )
        assert result.exit_code != 0
        assert "GATEWAY=N" in result.output
        assert not (run_dir / "APPROVED.json").exists()

    def test_expires_in_is_passed_through(
        self, runner: CliRunner, tmp_path: Any
    ) -> None:
        run_dir = self._run_dir(tmp_path)
        result = runner.invoke(
            main,
            ["ontomap", "council-approve", str(run_dir), "--expires-in", "1", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert data["approved_at"][:4] == data["expires_at"][:4]
        assert data["approved_at"] != data["expires_at"]

    def test_no_write_approves_nothing_and_says_so(
        self, runner: CliRunner, tmp_path: Any
    ) -> None:
        run_dir = self._run_dir(tmp_path)
        result = _separated_runner().invoke(
            main, ["ontomap", "council-approve", str(run_dir), "--no-write", "--json"]
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["data"]["ceiling_tokens"]
        assert envelope["warnings"], "a run nobody approved must be warned about"
        assert not (run_dir / "APPROVED.json").exists()

    def test_the_help_says_it_is_not_an_authentication_boundary(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(main, ["ontomap", "council-approve", "--help"])
        assert result.exit_code == 0, result.output
        assert "not implemented yet" not in result.output
        assert "NOT AN AUTHENTICATION BOUNDARY" in result.output
        assert "AUDIT TRAIL" in result.output
        assert "UPPER BAND" in result.output

    def test_the_human_rendering_names_the_ceiling_and_the_caveat(
        self, runner: CliRunner, tmp_path: Any
    ) -> None:
        run_dir = self._run_dir(tmp_path)
        result = runner.invoke(main, ["ontomap", "council-approve", str(run_dir)])
        assert result.exit_code == 0, result.output
        assert "approval:" in result.output
        assert "ceiling_tokens" in result.output
        assert "argo" in result.output and "mag" in result.output
        assert "NOT AN AUTHENTICATION BOUNDARY" in result.output

    def test_a_run_directory_with_no_quote_is_a_cli_error_not_a_traceback(
        self, runner: CliRunner, tmp_path: Any
    ) -> None:
        result = runner.invoke(
            main, ["ontomap", "council-approve", str(tmp_path), "--json"]
        )
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "FileNotFoundError" in result.output
        assert "council-quote" in result.output
        assert "schema_version" not in result.output


class TestCouncilRun:
    """Stage 4 landed, so ``council-run`` must no longer refuse.

    The run itself -- the gate, the ceiling, the chunk loop, the retry pass --
    is covered beside the module that owns it
    (``tests/domains/test_ontomap_council_run.py``). What is pinned here is
    the OPERATOR SURFACE: that the verb is no longer a stub, that the help
    documents the ``systemd-run`` this host requires (a 9-hour run started
    with ``nohup`` is killed without a trace, which is the single most
    expensive thing an operator can get wrong about this command), that the
    options reach the library unchanged, and that a refusal is a clean CLI
    error naming its check rather than a traceback.

    Like ``council-approve``, this verb does NOT go through ``_ontomap()``:
    stage 4 ranks nothing, so it must not pay for the retrieval backend's
    numpy/scikit-learn/joblib stack. These tests therefore run on any host.
    """

    def test_it_is_not_registered_as_a_stub(self) -> None:
        assert "council-run" not in [name for name, _ in ontomap._PLANNED_VERBS]
        assert "council-run" in ontomap.ontomap_cmd.commands

    def test_it_does_not_answer_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ontomap", "council-run", "--help"])
        assert result.exit_code == 0, result.output
        assert "not implemented yet" not in result.output

    def test_invoked_bare_it_is_a_usage_error_not_a_refusal(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(main, ["ontomap", "council-run"])
        assert result.exit_code != 0
        assert "not implemented yet" not in result.output
        assert "--run-dir" in result.output

    def test_the_help_documents_the_systemd_run_requirement(
        self, runner: CliRunner
    ) -> None:
        """`nohup` on this host loses a 9-hour run without a trace."""
        result = runner.invoke(main, ["ontomap", "council-run", "--help"])
        assert result.exit_code == 0, result.output
        assert "systemd-run --user --scope --collect" in result.output
        for word in ("setsid", "nohup", "disown", "cgroup"):
            assert word in result.output, f"the help does not mention {word}"
        assert "FOREGROUND" in result.output

    def test_the_help_names_both_credential_checks(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(main, ["ontomap", "council-run", "--help"])
        assert "argo_user" in result.output
        assert "amsc_token" in result.output
        assert "ARGO_USER" in result.output

    def test_the_help_says_the_retry_pass_gates_completeness(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(main, ["ontomap", "council-run", "--help"])
        assert "RETRY PASS IS A DECLARED STAGE" in result.output
        assert "issue 84" in result.output

    def test_json_emits_exactly_one_envelope_carrying_the_run(
        self, runner: CliRunner, tmp_path: Any, monkeypatch: Any
    ) -> None:
        record = {
            "schema_version": 1,
            "run_id": "2026-10-23-adp1",
            "query": "go_mf@1",
            "complete": True,
            "chunks": [
                {
                    "index": 0,
                    "job_id": "job-1",
                    "items": 2,
                    "ok": 2,
                    "error": 0,
                    "duration_s": 1.5,
                    "tokens_by_gateway": {"argo": 10},
                    "outcome": "settled",
                }
            ],
            "retry": {"ran": False, "declared_unnecessary": True, "rounds": [],
                      "round_cap": 2, "reason": "nothing to re-ask"},
            "halt": None,
            "warnings": [],
            "run_file": str(tmp_path / "RUN.json"),
        }
        seen: Dict[str, Any] = {}
        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_run.council_run",
            lambda **kwargs: (seen.update(kwargs), record)[1],
        )
        result = runner.invoke(
            main, ["ontomap", "council-run", "--run-dir", str(tmp_path), "--json"]
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert set(envelope) == ENVELOPE_KEYS
        assert envelope["verb"] == "council-run"
        assert envelope["data"]["run_id"] == "2026-10-23-adp1"
        # The derived summary, so what an operator reads cannot drift from
        # the chunk rows it is a summary OF.
        assert envelope["data"]["summary"]["chunks"] == 1
        assert envelope["data"]["summary"]["ok"] == 2

    def test_the_operators_options_reach_the_library_unchanged(
        self, runner: CliRunner, tmp_path: Any, monkeypatch: Any
    ) -> None:
        seen: Dict[str, Any] = {}
        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_run.council_run",
            lambda **kwargs: (seen.update(kwargs), {"complete": True, "warnings": []})[1],
        )
        result = runner.invoke(
            main,
            [
                "ontomap", "council-run",
                "--run-dir", str(tmp_path),
                "--no-resume",
                "--profile", "go_council_v3",
                "--chunk-timeout", "60",
                "--poll-interval", "0.5",
                "--retry-rounds", "1",
                "--amsc-token", str(tmp_path / "tok"),
                "--allow-unverified",
                "--allow-unavailable",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert seen["run_dir"] == str(tmp_path)
        assert seen["resume"] is False
        assert seen["profile"] == "go_council_v3"
        assert seen["chunk_timeout_s"] == 60.0
        assert seen["poll_interval_s"] == 0.5
        assert seen["retry_rounds"] == 1
        assert seen["amsc_token_path"] == str(tmp_path / "tok")
        assert seen["allow_unverified"] is True
        assert seen["allow_unavailable"] is True

    def test_the_defaults_are_the_librarys_own(
        self, runner: CliRunner, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from kbutillib.domains.genome.ontology_mapping.council_run import (
            AMSC_TOKEN_PATH,
            DEFAULT_CHUNK_TIMEOUT_S,
            DEFAULT_POLL_INTERVAL_S,
            RETRY_ROUND_CAP,
        )

        seen: Dict[str, Any] = {}
        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_run.council_run",
            lambda **kwargs: (seen.update(kwargs), {"complete": True, "warnings": []})[1],
        )
        result = runner.invoke(
            main, ["ontomap", "council-run", "--run-dir", str(tmp_path), "--json"]
        )
        assert result.exit_code == 0, result.output
        assert seen["resume"] is True
        assert seen["chunk_timeout_s"] == DEFAULT_CHUNK_TIMEOUT_S
        assert seen["poll_interval_s"] == DEFAULT_POLL_INTERVAL_S
        assert seen["retry_rounds"] == RETRY_ROUND_CAP
        assert seen["amsc_token_path"] == AMSC_TOKEN_PATH
        assert seen["profile"] is None

    def test_a_refusal_is_a_clean_cli_error_naming_its_check(
        self, runner: CliRunner, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from kbutillib.domains.genome.ontology_mapping.council_run import RunRefused

        def _refuse(**_kwargs: Any) -> Any:
            raise RunRefused("argo_user", "$ARGO_USER is unset or empty")

        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_run.council_run", _refuse
        )
        result = runner.invoke(
            main, ["ontomap", "council-run", "--run-dir", str(tmp_path), "--json"]
        )
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "argo_user" in result.output
        assert "schema_version" not in result.output

    def test_an_approval_refusal_names_the_gate_check_it_failed(
        self, runner: CliRunner, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from kbutillib.domains.ai.council.approval import ApprovalRefused

        def _refuse(**_kwargs: Any) -> Any:
            raise ApprovalRefused("expires_at", "the approval expired")

        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_run.council_run", _refuse
        )
        result = runner.invoke(
            main, ["ontomap", "council-run", "--run-dir", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "expires_at" in result.output

    def test_the_human_rendering_is_the_same_dict(
        self, runner: CliRunner, tmp_path: Any, monkeypatch: Any
    ) -> None:
        record = {
            "run_id": "2026-10-23-adp1",
            "query": "go_mf@1",
            "complete": False,
            "ceiling_tokens": {"argo": 1500},
            "spend_by_gateway": {"argo": 2000},
            "endpoints_by_gateway": {"argo": ["argo"]},
            "chunks": [
                {
                    "index": 0,
                    "job_id": "job-1",
                    "items": 2,
                    "ok": 2,
                    "error": 0,
                    "duration_s": 1.0,
                    "outcome": "halted",
                    "tokens_by_gateway": {"argo": 2000},
                    "claim_probe": {"pending_calls": 2},
                }
            ],
            "retry": {"ran": False, "declared_unnecessary": False, "rounds": [],
                      "round_cap": 2, "reason": "not run: the chunk pass halted"},
            "halt": {"reason": "ceiling", "detail": "overshoot 500"},
            "warnings": ["the run HALTED (ceiling)"],
        }
        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_run.council_run",
            lambda **_kw: record,
        )
        captured: Dict[str, Any] = {}
        monkeypatch.setattr(ontomap, "_render", lambda env: captured.update(env))
        result = runner.invoke(
            main, ["ontomap", "council-run", "--run-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert captured["verb"] == "council-run"
        assert captured["data"]["halt"]["reason"] == "ceiling"

    def test_the_human_rendering_names_the_halt_and_the_ceiling(
        self, runner: CliRunner, tmp_path: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_run.council_run",
            lambda **_kw: {
                "run_id": "r",
                "query": "go_mf@1",
                "complete": False,
                "ceiling_tokens": {"argo": 1500},
                "spend_by_gateway": {"argo": 2000},
                "endpoints_by_gateway": {"argo": ["argo"]},
                "chunks": [],
                "retry": {"ran": False, "round_cap": 2, "rounds": [], "reason": "not run"},
                "halt": {"reason": "ceiling", "detail": "overshoot 500"},
                "warnings": [],
            },
        )
        result = runner.invoke(
            main, ["ontomap", "council-run", "--run-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert "complete:" in result.output
        assert "HALTED:" in result.output
        assert "ceiling" in result.output
        assert "1,500" in result.output and "2,000" in result.output
        assert "retry pass:" in result.output

    def test_an_incomplete_run_that_says_nothing_is_warned_about(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """``warnings`` is never empty for a degraded answer."""
        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_run.council_run",
            lambda **_kw: {"complete": False, "warnings": []},
        )
        # Separated streams: the warning goes to stderr and stdout must still
        # carry exactly one envelope (``_emit``'s stdout discipline).
        result = _separated_runner().invoke(
            main, ["ontomap", "council-run", "--run-dir", str(tmp_path), "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["warnings"], (
            "an incomplete run with no stated reason must not read as clean"
        )


class TestCouncilStatus:
    """``council-status`` landed, so this group registers NO stubs at all.

    Like ``council-approve``, ``council-run`` and ``council-publish``, this
    verb does NOT go through ``_ontomap()``: status ranks nothing, so it must
    not pay for -- or fail on -- the retrieval backend's
    numpy/scikit-learn/joblib stack. These tests therefore run on any host.
    """

    def test_no_verb_is_registered_as_a_stub_any_more(self) -> None:
        assert ontomap._PLANNED_VERBS == ()
        assert "council-status" in ontomap.ontomap_cmd.commands

    def test_it_does_not_answer_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ontomap", "council-status", "--help"])
        assert result.exit_code == 0, result.output
        assert "not implemented yet" not in result.output

    def test_invoked_bare_it_is_a_usage_error_naming_both_ways_to_scope_it(
        self, runner: CliRunner
    ) -> None:
        """Status is a report ACROSS runs, so it cannot be scoped to nothing."""
        result = runner.invoke(main, ["ontomap", "council-status"])
        assert result.exit_code != 0
        assert "not implemented yet" not in result.output
        assert "--runs-root" in result.output
        assert "--run-dir" in result.output

    def test_the_help_states_both_halves_of_the_report(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(main, ["ontomap", "council-status", "--help"])
        assert result.exit_code == 0, result.output
        for state in (
            "un-run",
            "planned-not-quoted",
            "quoted-not-approved",
            "approved-not-run",
            "running",
            "exported-not-loaded",
            "loaded",
        ):
            assert state in result.output, f"the help does not name {state}"
        assert "parameter_set_hash" in result.output
        assert "PAID FOR" in result.output
        assert "TOLERATED" in result.output

    def test_the_help_explains_why_a_second_concurrent_run_matters(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(main, ["ontomap", "council-status", "--help"])
        assert result.exit_code == 0, result.output
        assert "KBUtilLib issue 84" in result.output
        assert "STOP" in result.output

    def test_it_renders_both_halves_and_the_run_rows(
        self, tmp_path: Any, monkeypatch: Any, runner: CliRunner
    ) -> None:
        """One dict, rendered. The ``--json`` path serialises the same object."""
        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_status.council_status",
            lambda **_kw: _STATUS_RECORD,
        )
        result = runner.invoke(
            main, ["ontomap", "council-status", "--runs-root", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert "BY LIFECYCLE" in result.output
        assert "BY parameter_set_hash" in result.output
        assert "paid for, NOT landed: 20" in result.output
        assert "exported-not-loaded" in result.output
        assert "2026-10-23-one" in result.output
        assert "TWO RUNS IN STAGE 4" in result.output

    def test_under_json_it_emits_exactly_one_envelope(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_status.council_status",
            lambda **_kw: _STATUS_RECORD,
        )
        result = _separated_runner().invoke(
            main, ["ontomap", "council-status", "--runs-root", str(tmp_path), "--json"]
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert set(envelope) == ENVELOPE_KEYS
        assert envelope["verb"] == "council-status"
        assert envelope["data"]["lifecycle"]["paid_for_not_landed"] == 20

    def test_a_runs_root_holding_no_runs_is_warned_about_rather_than_read_as_empty(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """"No run directories here" is not "nothing is in flight".

        A run directory that has not synced yet holds work that is already
        running, and reporting zero for every middle state without saying so
        would be the exact invisibility this verb exists to remove.
        """
        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_status.council_status",
            lambda **_kw: {**_STATUS_RECORD, "runs": [], "concurrent_runs": []},
        )
        result = _separated_runner().invoke(
            main, ["ontomap", "council-status", "--runs-root", str(tmp_path), "--json"]
        )
        assert result.exit_code == 0, result.output
        warnings = json.loads(result.stdout)["warnings"]
        assert any("no run directories were found" in line for line in warnings)

    def test_a_refusal_is_a_clean_cli_error_and_not_a_traceback(
        self, tmp_path: Any, monkeypatch: Any, runner: CliRunner
    ) -> None:
        from kbutillib.domains.genome.ontology_mapping.council_status import (
            StatusRefused,
        )

        def _refuse(**_kw: Any) -> Any:
            raise StatusRefused("runs_root", "that is not a directory")

        monkeypatch.setattr(
            "kbutillib.domains.genome.ontology_mapping.council_status.council_status",
            _refuse,
        )
        result = runner.invoke(
            main, ["ontomap", "council-status", "--runs-root", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "StatusRefused" in result.output
        assert "Traceback" not in result.output


#: A status record shaped like the real one, for the renderer tests. Held as a
#: module constant rather than built per test so the renderer and the ``--json``
#: assertions are made against the SAME object -- which is the point of the
#: one-envelope contract.
_STATUS_RECORD: Dict[str, Any] = {
    "schema_version": 1,
    "generated_at": "2026-10-23T12:00:00Z",
    "query": "go_mf@1",
    "source": "council/go_mf@1",
    "parameter_set_hash": "c" * 64,
    "corpus": {"entity_rows": 100, "content_rows": 100, "functions": 100},
    "lifecycle": {
        "corpus": 100,
        "states": [
            {"state": "un-run", "functions": 60, "runs": None, "share": 0.6},
            {"state": "planned-not-quoted", "functions": 0, "runs": 0, "share": 0.0},
            {"state": "quoted-not-approved", "functions": 0, "runs": 0, "share": 0.0},
            {"state": "approved-not-run", "functions": 0, "runs": 0, "share": 0.0},
            {"state": "running", "functions": 15, "runs": 2, "share": 0.15},
            {"state": "exported-not-loaded", "functions": 5, "runs": 1, "share": 0.05},
            {"state": "loaded", "functions": 20, "runs": None, "share": 0.2},
        ],
        "paid_for_not_landed": 20,
        "overclaimed": 0,
        "un_run_is_a_floor": False,
    },
    "by_parameter_set": {
        "current": "c" * 64,
        "parameter_sets": [
            {"parameter_set_hash": "c" * 64, "current": True, "functions": 20,
             "share": 0.2},
            {"parameter_set_hash": "0" * 64, "current": False, "functions": 30,
             "share": 0.3},
        ],
        "covered_at_all": 45,
        "covered_only_by_an_older_parameter_set": 25,
        "uncovered": 55,
    },
    "runs": [
        {
            "run_id": "2026-10-23-one",
            "run_dir": "/runs/2026-10-23-one",
            "state": "running",
            "functions": 10,
            "complete": False,
            "published": False,
            "rows_at_parameter_set": 20,
            "parameter_set_hash": "c" * 64,
        },
        {
            "run_id": "2026-10-23-two",
            "run_dir": "/runs/2026-10-23-two",
            "state": "running",
            "functions": 5,
            "complete": False,
            "published": False,
            "rows_at_parameter_set": 20,
            "parameter_set_hash": "c" * 64,
        },
    ],
    "concurrent_runs": [
        {"run_id": "2026-10-23-one", "run_dir": "/runs/2026-10-23-one"},
        {"run_id": "2026-10-23-two", "run_dir": "/runs/2026-10-23-two"},
    ],
    "warnings": [],
}
