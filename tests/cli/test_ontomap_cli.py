"""Tests for ``kbu ontomap`` -- the function-to-GO pipeline's operator surface.

Nothing here spends a token, makes a network call or needs a GPU. The
``version`` verb is driven two ways:

* against a **fake facade** (``ontomap._ontomap`` monkeypatched), which is what
  lets the envelope contract be asserted on a host that has not got the
  retrieval backend's numpy/sklearn/joblib stack;
* against the **real** :class:`OntologyMappingUtils`, skipped when that stack
  is absent, so the facade method the CLI calls is proved to exist and to
  return the three keys the envelope promises.

The other seven verbs are REGISTERED STUBS, and the thing these tests are here
to pin is that every one of them exits NON-ZERO. A stub that exited 0 would
let a rehearsal of the whole pipeline pass over a missing stage and report
success.

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

#: The seven verbs registered but not implemented by this task.
STUB_VERBS = [
    "council-plan",
    "council-quote",
    "council-approve",
    "council-run",
    "council-status",
    "council-export",
    "council-publish",
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
        for verb in ["version", *STUB_VERBS]:
            assert verb in commands, f"{verb} missing from the help listing"

    def test_help_describes_the_five_pipeline_stages(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ontomap", "--help"])
        assert result.exit_code == 0, result.output
        for stage in ("1.", "2.", "3.", "4.", "5."):
            assert stage in result.output
        assert "parameter_set_hash" in result.output

    def test_every_command_takes_json(self, runner: CliRunner) -> None:
        for verb in ["version", *STUB_VERBS]:
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

    def test_every_registered_verb_is_either_version_or_a_known_stub(self) -> None:
        registered = set(ontomap.ontomap_cmd.commands)
        assert registered == {"version", *STUB_VERBS}
        assert [name for name, _ in ontomap._PLANNED_VERBS] == STUB_VERBS
