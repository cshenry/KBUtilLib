"""Tests for ``kbu model`` — reconstruct/gapfill/fba/fva + exec.

All tests exercise the real modeling algorithms (build_metabolic_model,
gapfill_metabolic_model, run_fba, run_fva) against a tiny committed genome
fixture and a local JSON media file — no KBase auth, no network.  Skipped
entirely (``kbu_model`` marker) when the modeling stack (cobra/modelseedpy)
is unavailable, per Acceptance Criterion #24.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.manifest import now_utc_iso, write_project_manifest

try:
    import cobra  # noqa: F401
    import modelseedpy  # noqa: F401

    _KBU_MODEL_AVAILABLE = True
except ImportError:
    _KBU_MODEL_AVAILABLE = False

pytestmark = pytest.mark.kbu_model

if not _KBU_MODEL_AVAILABLE:
    pytest.skip(
        "kbu modeling stack (cobra/modelseedpy) unavailable", allow_module_level=True
    )


FIXTURES = Path(__file__).parent.parent / "fixtures" / "model"
GENOME = FIXTURES / "demo_genome.faa"
MEDIA = FIXTURES / "glucose_minimal.json"


# ── helpers ──────────────────────────────────────────────────────────────────


def _invoke(root: Path, *args: str) -> Any:
    runner = CliRunner()
    saved = os.getcwd()
    try:
        os.chdir(root)
        return runner.invoke(main, list(args), catch_exceptions=False)
    finally:
        os.chdir(saved)


def _last_json(output: str) -> dict:
    """Return the parsed JSON from the last line of *output* that looks like JSON.

    Verb commands may emit informational log lines (e.g. biochem DB load
    notices) before the JSON payload; the JSON payload is always the last
    ``{...}`` line.
    """
    lines = [line for line in output.splitlines() if line.startswith("{")]
    assert lines, f"no JSON line found in output:\n{output}"
    return json.loads(lines[-1])


def _make_project(root: Path) -> None:
    now = now_utc_iso()
    write_project_manifest(
        root,
        {
            "project": {"name": "modelproj", "title": "modelproj", "created_at": now},
            "kbutillib": {"source_path": "/fake", "source_commit": "abc"},
            "update": {"last_pulled_at": now, "last_pulled_commit": "abc"},
        },
    )


# ── reconstruct -> gapfill -> fba -> fva chain ──────────────────────────────


@pytest.fixture(scope="module")
def gapfilled_model_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build+gapfill once and share the result across the FVA-focused tests.

    Cuts redundant ~30s gapfill MILP solves; the dedicated
    ``test_full_chain_reconstruct_gapfill_fba_fva`` test still exercises its
    own independent reconstruct->gapfill->fba->fva run end-to-end.
    """
    root = tmp_path_factory.mktemp("shared_model")
    draft = root / "draft.json"
    gapfilled = root / "gapfilled.json"
    r1 = _invoke(
        root, "model", "reconstruct", "--genome", str(GENOME), "--out", str(draft)
    )
    assert r1.exit_code == 0, r1.output
    r2 = _invoke(
        root,
        "model",
        "gapfill",
        "--model",
        str(draft),
        "--media",
        str(MEDIA),
        "--out",
        str(gapfilled),
    )
    assert r2.exit_code == 0, r2.output
    return gapfilled


class TestVerifiedVerbChain:
    def test_reconstruct_writes_model_and_emits_schema(self, tmp_path: Path) -> None:
        out = tmp_path / "draft.json"
        r = _invoke(
            tmp_path,
            "model",
            "reconstruct",
            "--genome",
            str(GENOME),
            "--out",
            str(out),
            "--json",
        )
        assert r.exit_code == 0, r.output
        data = _last_json(r.output)
        assert set(data) == {"model_path", "reactions", "metabolites", "genes"}
        assert data["model_path"] == str(out.resolve())
        assert data["reactions"] > 0
        assert data["metabolites"] > 0
        assert isinstance(data["genes"], int)
        assert out.is_file()

    def test_full_chain_reconstruct_gapfill_fba_fva(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        draft = tmp_path / "draft.json"
        gapfilled = tmp_path / "gapfilled.json"

        r1 = _invoke(
            tmp_path,
            "model",
            "reconstruct",
            "--genome",
            str(GENOME),
            "--out",
            str(draft),
            "--json",
        )
        assert r1.exit_code == 0, r1.output

        r2 = _invoke(
            tmp_path,
            "model",
            "gapfill",
            "--model",
            str(draft),
            "--media",
            str(MEDIA),
            "--out",
            str(gapfilled),
            "--json",
        )
        assert r2.exit_code == 0, r2.output
        gf_data = _last_json(r2.output)
        assert set(gf_data) == {"model_in", "media", "model_out", "reactions_added"}
        assert gf_data["model_in"] == str(draft.resolve())
        assert gf_data["model_out"] == str(gapfilled.resolve())
        assert gf_data["media"] == str(MEDIA)
        assert isinstance(gf_data["reactions_added"], list)
        assert len(gf_data["reactions_added"]) > 0
        assert gapfilled.is_file()

        r3 = _invoke(
            tmp_path,
            "model",
            "fba",
            "--model",
            str(gapfilled),
            "--media",
            str(MEDIA),
            "--json",
        )
        assert r3.exit_code == 0, r3.output
        fba_data = _last_json(r3.output)
        assert set(fba_data) == {"objective_value", "fluxes", "solver_status"}
        # A plausible non-zero FBA objective (biomass growth after gapfill).
        assert fba_data["objective_value"] > 0.01
        assert fba_data["solver_status"] == "optimal"
        assert len(fba_data["fluxes"]) > 0
        for flux in fba_data["fluxes"]:
            assert set(flux) == {"id", "value"}
            assert isinstance(flux["id"], str)
            assert isinstance(flux["value"], float)

        r4 = _invoke(
            tmp_path,
            "model",
            "fva",
            "--model",
            str(gapfilled),
            "--media",
            str(MEDIA),
            "--json",
        )
        assert r4.exit_code == 0, r4.output
        fva_data = _last_json(r4.output)
        assert set(fva_data) == {"reactions"}
        assert len(fva_data["reactions"]) > 0
        for rxn in fva_data["reactions"]:
            assert set(rxn) == {"id", "min", "max"}
            assert rxn["min"] <= rxn["max"]

        # bio1 (biomass) must be present and its FVA range must bracket the
        # FBA growth rate we just observed -- proof the two verbs are
        # consistent (same model, same media).
        bio_entries = [r for r in fva_data["reactions"] if r["id"] == "bio1"]
        assert bio_entries, "bio1 missing from fva output"
        bio = bio_entries[0]
        assert bio["min"] <= fba_data["objective_value"] <= bio["max"] + 1e-6

    def test_fva_routes_through_run_fva_not_cobra_native(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        gapfilled_model_path: Path,
    ) -> None:
        """FVA must call MSFBAUtils.run_fva, never cobra.flux_variability_analysis."""
        gapfilled = gapfilled_model_path

        from kbutillib import ms_fba_utils

        calls = {"run_fva": 0, "native_fva": 0}
        original_run_fva = ms_fba_utils.MSFBAUtils.run_fva

        def _spy_run_fva(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls["run_fva"] += 1
            return original_run_fva(self, *args, **kwargs)

        def _forbidden_native_fva(*args: Any, **kwargs: Any) -> Any:
            calls["native_fva"] += 1
            raise AssertionError(
                "kbu model fva must not call cobra.flux_variability_analysis"
            )

        monkeypatch.setattr(ms_fba_utils.MSFBAUtils, "run_fva", _spy_run_fva)
        monkeypatch.setattr(
            "cobra.flux_analysis.flux_variability_analysis", _forbidden_native_fva
        )

        r = _invoke(
            tmp_path,
            "model",
            "fva",
            "--model",
            str(gapfilled),
            "--media",
            str(MEDIA),
            "--json",
        )
        assert r.exit_code == 0, r.output
        assert calls["run_fva"] == 1
        assert calls["native_fva"] == 0

    def test_fva_reactions_filter(
        self, tmp_path: Path, gapfilled_model_path: Path
    ) -> None:
        gapfilled = gapfilled_model_path
        r = _invoke(
            tmp_path,
            "model",
            "fva",
            "--model",
            str(gapfilled),
            "--media",
            str(MEDIA),
            "--reactions",
            "bio1",
            "--json",
        )
        assert r.exit_code == 0, r.output
        data = _last_json(r.output)
        assert [rxn["id"] for rxn in data["reactions"]] == ["bio1"]


# ── exec ─────────────────────────────────────────────────────────────────────


class TestExec:
    def test_exec_success_envelope_and_run_dir(self, tmp_path: Path) -> None:
        script = tmp_path / "ok_script.py"
        script.write_text(
            "import kbutillib\n"
            "print('hello-' + kbutillib.__version__)\n"
            "with open('relative_out.txt', 'w') as fh:\n"
            "    fh.write('captured')\n"
        )

        r = _invoke(tmp_path, "model", "exec", str(script), "--json")
        assert r.exit_code == 0, r.output
        data = _last_json(r.output)
        assert set(data) == {"stdout", "stderr", "exit_code", "run_dir"}
        assert data["exit_code"] == 0
        assert "hello-" in data["stdout"]

        run_dir = Path(data["run_dir"])
        assert run_dir.is_dir()
        assert (run_dir / script.name).is_file()
        assert (run_dir / "stdout.txt").is_file()
        assert (run_dir / "stderr.txt").is_file()
        run_json_path = run_dir / "run.json"
        assert run_json_path.is_file()

        run_record = json.loads(run_json_path.read_text())
        assert set(run_record) >= {
            "script_hash",
            "exit_code",
            "started_at",
            "finished_at",
            "versions",
            "argv",
            "cwd",
        }
        assert run_record["exit_code"] == 0
        assert set(run_record["versions"]) == {"kbutillib", "cobra", "modelseedpy"}
        assert run_record["cwd"] == str(run_dir)

        # Relative output written by the script must land in run_dir, not be
        # lost to a throwaway temp cwd.
        assert (run_dir / "relative_out.txt").is_file()
        assert (run_dir / "relative_out.txt").read_text() == "captured"

    def test_exec_failure_yields_nonzero_exit_code_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        script = tmp_path / "bad_script.py"
        script.write_text("import sys\nprint('boom')\nsys.exit(7)\n")

        r = _invoke(tmp_path, "model", "exec", str(script), "--json")
        assert r.exit_code == 0, r.output  # kbu model exec itself must not crash
        data = _last_json(r.output)
        assert data["exit_code"] == 7
        assert "boom" in data["stdout"]

        run_record = json.loads((Path(data["run_dir"]) / "run.json").read_text())
        assert run_record["exit_code"] == 7

    def test_exec_passthrough_args(self, tmp_path: Path) -> None:
        script = tmp_path / "args_script.py"
        script.write_text("import sys\nprint(','.join(sys.argv[1:]))\n")

        r = _invoke(
            tmp_path, "model", "exec", str(script), "--json", "--", "foo", "bar"
        )
        assert r.exit_code == 0, r.output
        data = _last_json(r.output)
        assert data["stdout"].strip() == "foo,bar"

    def test_exec_records_kbu_session_when_project_context_exists(
        self, tmp_path: Path
    ) -> None:
        _make_project(tmp_path)
        script = tmp_path / "ok_script.py"
        script.write_text("print('hi')\n")

        r = _invoke(tmp_path, "model", "exec", str(script), "--json")
        assert r.exit_code == 0, r.output
        data = _last_json(r.output)
        # The durable run_dir must live under the project's runs/ dir, not
        # ~/.kbcache, when a kbu-project.toml is present.
        assert str(tmp_path / "runs" / "kbu-model-exec") in data["run_dir"]

        r2 = _invoke(tmp_path, "session", "list", "--json")
        assert r2.exit_code == 0, r2.output
        sessions = json.loads(r2.output)
        assert any(s["command"] == "kbu-model-exec" for s in sessions)

    def test_exec_falls_back_to_kbcache_without_project_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        script = work_dir / "ok_script.py"
        script.write_text("print('hi')\n")

        r = _invoke(work_dir, "model", "exec", str(script), "--json")
        assert r.exit_code == 0, r.output
        data = _last_json(r.output)
        assert str(fake_home / ".kbcache" / "kbu-model-exec") in data["run_dir"]

    def test_exec_missing_script_errors_cleanly(self, tmp_path: Path) -> None:
        r = _invoke(tmp_path, "model", "exec", str(tmp_path / "nope.py"))
        assert r.exit_code != 0
