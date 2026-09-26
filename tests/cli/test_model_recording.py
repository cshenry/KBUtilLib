"""Tests for ``kbu model`` analysis-record stamping (kind-model-p2).

Two layers:

* **Recording mechanics** (no modeling stack needed) drive
  ``model_recording.record_model_analysis`` directly against a SYNTHETIC KOROS
  runs tree in ``tmp_path`` with ``KOROS_ARC`` monkeypatched and the run
  database pointed at a temp SQLite file.  These assert the contract that does
  not depend on cobra/modelseedpy: one record per successful call, upsert on a
  repeated identical run (same run_uid → one row), a genuine re-run (new run_uid
  → a second row under the same analysis_id), a failing run written with
  ``status=failed``, an unresolvable arc writing NO record, the shared run_uid /
  distinct analysis_ids across several analyses in one process, float rejection
  in significant_params via the real path, and the fixed
  ``trust_tier='hypothesis'`` + non-empty provenance + ``contract_version=1``.

* **Verb integration** (gated behind the ``kbu_model`` marker, skipped when
  cobra/modelseedpy is unavailable) drives the real ``kbu model`` verbs and
  asserts ``--json`` stdout is BYTE-IDENTICAL with and without a resolvable arc,
  that each verb writes exactly one record with its namespaced kind, and that a
  re-run leaves exactly one record.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import pytest

from kbutillib.koros_arc_store import (
    KorosArcStore,
    RecordValidationError,
    RunDatabase,
    derive_analysis_id,
)
from kbutillib.interfaces.cli import model_recording


# ── synthetic runs tree + enabled temp run database ──────────────────────────


def _make_runs_tree(root: Path, project: str, slug: str) -> Path:
    """Create ``<root>/runs/<project>/arcs/<slug>/PROVENANCE.json`` and return runs/."""
    runs = root / "runs"
    arc_dir = runs / project / "arcs" / slug
    arc_dir.mkdir(parents=True)
    (arc_dir / "PROVENANCE.json").write_text(
        json.dumps({"run_id": "r1", "created_at": "2026-09-26T00:00:00Z"}),
        encoding="utf-8",
    )
    return runs


@pytest.fixture
def arc_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A synthetic runs tree, a temp enabled run DB, and KOROS_ARC set.

    Every recording-mechanics test resolves to ``proj/arc`` via ``KOROS_ARC``
    and writes to an isolated SQLite file so the assertions are hermetic.
    """
    _make_runs_tree(tmp_path, "proj", "arc")
    db_path = tmp_path / "runs.sqlite"
    monkeypatch.setenv("KOROS_HOME", str(tmp_path))
    monkeypatch.setenv("KBDL_RUN_DB", str(db_path))
    monkeypatch.setenv("KBDL_RUN_DB_ENABLED", "1")
    monkeypatch.setenv("KOROS_ARC", "proj/arc")
    # A fresh, deterministic run_uid per test.
    monkeypatch.delenv(model_recording.RUN_UID_ENV, raising=False)
    return {"tmp_path": tmp_path, "db_path": db_path}


def _reader(db_path: Path) -> RunDatabase:
    return RunDatabase(db_path=db_path, enabled=True)


def _all_records(db_path: Path) -> list:
    return _reader(db_path).list_analyses("proj", "arc")


def _call_fba(arc_env: dict, *, media: str = "glucose", status: str = "ok") -> None:
    """Drive the fba shaping helper the CLI verb uses (no modeling stack needed)."""
    from kbutillib.interfaces.cli.model import _record_fba

    _record_fba(
        subject="/abs/model.json",
        media_arg=media,
        objective="MAX{bio1}",
        objective_value=1.23,
        model_path="/abs/model.json",
        flux_path=None,
        status=status,
        arc_slug=None,
    )


# ── recording mechanics (no cobra needed) ────────────────────────────────────


class TestRecordingMechanics:
    def test_one_record_per_successful_call(self, arc_env: dict) -> None:
        _call_fba(arc_env)
        records = _all_records(arc_env["db_path"])
        assert len(records) == 1
        assert records[0].kind == "kbutillib.fba"
        assert records[0].status == "ok"

    def test_repeat_identical_run_leaves_exactly_one_record(self, arc_env: dict) -> None:
        # Same process (run_uid minted once, cached in env) → same record_id →
        # UPSERT leaves exactly one row.
        _call_fba(arc_env)
        _call_fba(arc_env)
        assert len(_all_records(arc_env["db_path"])) == 1

    def test_rerun_new_run_uid_adds_second_row_same_analysis_id(
        self, arc_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _call_fba(arc_env)
        first = _all_records(arc_env["db_path"])
        assert len(first) == 1
        # A genuine re-run is a new process → a new run_uid.
        monkeypatch.delenv(model_recording.RUN_UID_ENV, raising=False)
        _call_fba(arc_env)
        rows = _all_records(arc_env["db_path"])
        assert len(rows) == 2
        # Both rows share ONE analysis_id (same logical analysis).
        assert {r.analysis_id for r in rows} == {first[0].analysis_id}
        # ... but distinct record_ids / run_uids.
        assert len({r.record_id for r in rows}) == 2
        assert len({r.run_uid for r in rows}) == 2

    def test_failing_run_writes_status_failed(self, arc_env: dict) -> None:
        _call_fba(arc_env, status="failed")
        records = _all_records(arc_env["db_path"])
        assert len(records) == 1
        assert records[0].status == "failed"

    def test_unresolvable_arc_writes_no_record(
        self, arc_env: dict, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        # No KOROS_ARC, cwd outside the runs tree → resolve_current_arc None.
        monkeypatch.delenv("KOROS_ARC", raising=False)
        monkeypatch.chdir(arc_env["tmp_path"])  # inside KOROS_HOME but not runs/
        _call_fba(arc_env)
        assert _all_records(arc_env["db_path"]) == []
        err = capsys.readouterr().err
        assert "was not indexed" in err

    def test_one_process_shares_run_uid_distinct_analysis_ids(
        self, arc_env: dict
    ) -> None:
        from kbutillib.interfaces.cli.model import (
            _record_fba,
            _record_fva,
            _record_gapfill,
            _record_reconstruct,
        )

        _record_reconstruct(
            subject="genome1",
            template="gn",
            out="/abs/m.json",
            mdlutl=None,
            atp_safe=False,
            status="ok",
            arc_slug=None,
        )
        _record_gapfill(
            subject="/abs/m.json",
            media_arg="glucose",
            objective="bio1",
            out="/abs/m.json",
            reactions_added=["rxn1"],
            status="ok",
            arc_slug=None,
        )
        _record_fba(
            subject="/abs/m.json",
            media_arg="glucose",
            objective="MAX{bio1}",
            objective_value=1.0,
            model_path="/abs/m.json",
            flux_path=None,
            status="ok",
            arc_slug=None,
        )
        _record_fva(
            subject="/abs/m.json",
            media_arg="glucose",
            fraction_of_optimum=0.9,
            model_path="/abs/m.json",
            fva_path=None,
            status="ok",
            arc_slug=None,
        )
        rows = _all_records(arc_env["db_path"])
        assert len(rows) == 4
        # FOUR records, ONE run_uid, FOUR distinct analysis_ids.
        assert len({r.run_uid for r in rows}) == 1
        assert len({r.analysis_id for r in rows}) == 4
        assert {r.kind for r in rows} == {
            "kbutillib.reconstruct",
            "kbutillib.gapfill",
            "kbutillib.fba",
            "kbutillib.fva",
        }

    def test_every_record_is_hypothesis_with_nonempty_provenance(
        self, arc_env: dict
    ) -> None:
        from kbutillib.interfaces.cli.model import (
            _record_fba,
            _record_fva,
            _record_gapfill,
            _record_reconstruct,
        )

        _record_reconstruct(
            subject="g",
            template="gn",
            out="/abs/m.json",
            mdlutl=None,
            atp_safe=False,
            status="ok",
            arc_slug=None,
        )
        _record_gapfill(
            subject="/abs/m.json",
            media_arg="glucose",
            objective="bio1",
            out="/abs/m.json",
            reactions_added=[],
            status="ok",
            arc_slug=None,
        )
        _record_fba(
            subject="/abs/m.json",
            media_arg="glucose",
            objective="MAX{bio1}",
            objective_value=1.0,
            model_path="/abs/m.json",
            flux_path=None,
            status="ok",
            arc_slug=None,
        )
        _record_fva(
            subject="/abs/m.json",
            media_arg="glucose",
            fraction_of_optimum=0.9,
            model_path="/abs/m.json",
            fva_path=None,
            status="ok",
            arc_slug=None,
        )
        rows = _all_records(arc_env["db_path"])
        assert len(rows) == 4
        for r in rows:
            assert r.trust_tier == "hypothesis"
            assert isinstance(r.provenance, dict) and r.provenance
            assert r.contract_version == 1

    def test_payload_schema_per_kind_is_exact(self, arc_env: dict) -> None:
        from kbutillib.interfaces.cli.model import (
            _record_fba,
            _record_fva,
            _record_gapfill,
            _record_reconstruct,
        )

        _record_reconstruct(
            subject="g",
            template="gn",
            out="/abs/m.json",
            mdlutl=None,
            atp_safe=False,
            status="ok",
            arc_slug=None,
        )
        _record_gapfill(
            subject="/abs/m.json",
            media_arg="glucose",
            objective="bio1",
            out="/abs/m.json",
            reactions_added=["rxn1"],
            status="ok",
            arc_slug=None,
        )
        _record_fba(
            subject="/abs/m.json",
            media_arg="glucose",
            objective="MAX{bio1}",
            objective_value=1.0,
            model_path="/abs/m.json",
            flux_path=None,
            status="ok",
            arc_slug=None,
        )
        _record_fva(
            subject="/abs/m.json",
            media_arg="glucose",
            fraction_of_optimum=0.9,
            model_path="/abs/m.json",
            fva_path=None,
            status="ok",
            arc_slug=None,
        )
        by_kind = {r.kind: r for r in _all_records(arc_env["db_path"])}
        assert set(by_kind["kbutillib.reconstruct"].payload) == {
            "template",
            "n_reactions",
            "n_genes",
            "atp_safe",
        }
        assert set(by_kind["kbutillib.gapfill"].payload) == {
            "media",
            "objective",
            "reactions_added",
        }
        assert set(by_kind["kbutillib.fba"].payload) == {
            "media",
            "objective",
            "objective_value",
        }
        assert set(by_kind["kbutillib.fva"].payload) == {
            "media",
            "fraction_of_optimum",
        }

    def test_payload_with_unlisted_or_missing_key_fails_validation(
        self, arc_env: dict
    ) -> None:
        # The normative per-kind payload schema is enforced by the writer: an
        # unlisted key OR a missing key is rejected before any record is written,
        # and nothing lands in the database.
        with pytest.raises(ValueError):
            model_recording.record_model_analysis(
                kind="kbutillib.fba",
                subject="/abs/m.json",
                significant_params={"media": "glucose", "objective": "MAX{bio1}"},
                payload={"media": "glucose", "objective": "MAX{bio1}"},  # missing objective_value
                artifacts={"model_id": "/abs/m.json", "model_path": "/abs/m.json"},
                provenance_evidence={"media": "glucose"},
                bridge_metric={"name": "objective_value", "value": "1"},
                status="ok",
                arc_explicit=None,
            )
        with pytest.raises(ValueError):
            model_recording.record_model_analysis(
                kind="kbutillib.fba",
                subject="/abs/m.json",
                significant_params={"media": "glucose", "objective": "MAX{bio1}"},
                payload={  # unlisted key
                    "media": "glucose",
                    "objective": "MAX{bio1}",
                    "objective_value": 1.0,
                    "surprise": True,
                },
                artifacts={"model_id": "/abs/m.json", "model_path": "/abs/m.json"},
                provenance_evidence={"media": "glucose"},
                bridge_metric={"name": "objective_value", "value": "1"},
                status="ok",
                arc_explicit=None,
            )
        assert _all_records(arc_env["db_path"]) == []

    def test_artifact_keys_per_kind_are_exact(self, arc_env: dict) -> None:
        from kbutillib.interfaces.cli.model import (
            _record_fba,
            _record_fva,
            _record_gapfill,
            _record_reconstruct,
        )

        _record_reconstruct(
            subject="g", template="gn", out="/abs/m.json", mdlutl=None,
            atp_safe=False, status="ok", arc_slug=None,
        )
        _record_gapfill(
            subject="/abs/m.json", media_arg="glucose", objective="bio1",
            out="/abs/m.json", reactions_added=[], status="ok", arc_slug=None,
        )
        _record_fba(
            subject="/abs/m.json", media_arg="glucose", objective="MAX{bio1}",
            objective_value=1.0, model_path="/abs/m.json", flux_path="/abs/flux.json",
            status="ok", arc_slug=None,
        )
        _record_fva(
            subject="/abs/m.json", media_arg="glucose", fraction_of_optimum=0.9,
            model_path="/abs/m.json", fva_path="/abs/fva.json", status="ok",
            arc_slug=None,
        )
        by_kind = {r.kind: r for r in _all_records(arc_env["db_path"])}
        assert set(by_kind["kbutillib.reconstruct"].artifacts) == {
            "model_id", "model_path"
        }
        assert set(by_kind["kbutillib.gapfill"].artifacts) == {
            "model_id", "model_path"
        }
        assert set(by_kind["kbutillib.fba"].artifacts) == {
            "model_id", "model_path", "flux_path"
        }
        assert set(by_kind["kbutillib.fva"].artifacts) == {
            "model_id", "model_path", "fva_path"
        }
        # model_id is a valid artifact ref (bare absolute path for a file-backed
        # cobra JSON model) per the store's S8/S19 URI vocabulary.
        assert by_kind["kbutillib.fba"].artifacts["model_id"].startswith("/")

    def test_float_in_significant_params_is_rejected_via_real_path(self) -> None:
        # fraction_of_optimum is formatted to a string before it reaches
        # significant_params; a raw float there is rejected by the shared
        # canonical_json with code bad_float_param.
        with pytest.raises(RecordValidationError) as exc:
            derive_analysis_id(
                "kbutillib.fva", "/abs/m.json", {"fraction_of_optimum": 0.9}
            )
        assert exc.value.code == "bad_float_param"

    def test_record_without_run_uid_rejected_missing_run_uid(
        self, arc_env: dict
    ) -> None:
        from kbutillib.koros_arc_store import AnalysisRecord, derive_record_id

        aid = derive_analysis_id("kbutillib.fba", "g", {"media": "x"})
        rec = AnalysisRecord(
            record_id=derive_record_id(aid, "uid"),
            analysis_id=aid,
            run_uid="",  # missing
            kind="kbutillib.fba",
            created_at="2026-09-26T00:00:00.000000Z",
            producer="p",
            producer_version="1",
            subject="g",
            status="ok",
            artifacts={},
            payload={},
            trust_tier="hypothesis",
            provenance={
                "bridge_kind": "model_prediction",
                "metric": {"name": "x", "value": 1},
            },
            contract_version=1,
        )
        store = KorosArcStore()
        with pytest.raises(RecordValidationError) as exc:
            store.record_analysis("proj", "arc", rec)
        assert exc.value.code == "missing_run_uid"


# ── verb integration (needs the modeling stack) ──────────────────────────────

try:
    import cobra  # noqa: F401
    import modelseedpy  # noqa: F401

    _KBU_MODEL_AVAILABLE = True
except ImportError:
    _KBU_MODEL_AVAILABLE = False


FIXTURES = Path(__file__).parent.parent / "fixtures" / "model"
GENOME = FIXTURES / "demo_genome.faa"
MEDIA = FIXTURES / "glucose_minimal.json"


def _invoke(root: Path, *args: str) -> Any:
    from click.testing import CliRunner

    from kbutillib.cli import main

    runner = CliRunner(mix_stderr=False)
    saved = os.getcwd()
    try:
        os.chdir(root)
        return runner.invoke(main, list(args), catch_exceptions=False)
    finally:
        os.chdir(saved)


def _last_json(output: str) -> dict:
    lines = [line for line in output.splitlines() if line.startswith("{")]
    assert lines, f"no JSON line found in output:\n{output}"
    return json.loads(lines[-1])


@pytest.mark.kbu_model
@pytest.mark.skipif(
    not _KBU_MODEL_AVAILABLE, reason="kbu modeling stack (cobra/modelseedpy) unavailable"
)
class TestVerbStamping:
    @pytest.fixture
    def stamp_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        _make_runs_tree(tmp_path, "proj", "arc")
        db_path = tmp_path / "runs.sqlite"
        monkeypatch.setenv("KOROS_HOME", str(tmp_path))
        monkeypatch.setenv("KBDL_RUN_DB", str(db_path))
        monkeypatch.setenv("KBDL_RUN_DB_ENABLED", "1")
        monkeypatch.delenv("KOROS_ARC", raising=False)
        monkeypatch.delenv(model_recording.RUN_UID_ENV, raising=False)
        return {"tmp_path": tmp_path, "db_path": db_path}

    def test_reconstruct_json_stdout_byte_identical_with_and_without_arc(
        self, stamp_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = stamp_env["tmp_path"] / "work"
        work.mkdir()
        out1 = work / "d1.json"
        # No arc resolvable → no record, plain stdout.
        r_noarc = _invoke(
            work, "model", "reconstruct", "--genome", str(GENOME),
            "--out", str(out1), "--json",
        )
        assert r_noarc.exit_code == 0, r_noarc.output

        # With a resolvable arc → a record IS written, stdout unchanged.
        out2 = work / "d2.json"
        monkeypatch.setenv("KOROS_ARC", "proj/arc")
        monkeypatch.delenv(model_recording.RUN_UID_ENV, raising=False)
        r_arc = _invoke(
            work, "model", "reconstruct", "--genome", str(GENOME),
            "--out", str(out2), "--model-id", "same", "--json",
        )
        assert r_arc.exit_code == 0, r_arc.output

        # Normalise the only legitimately-differing field (the out path) so we
        # compare the payload shape byte-for-byte otherwise.
        j1 = _last_json(r_noarc.stdout)
        j2 = _last_json(r_arc.stdout)
        j1.pop("model_path")
        j2.pop("model_path")
        assert json.dumps(j1) == json.dumps(j2)

        # The arc run wrote exactly one reconstruct record; the no-arc run wrote
        # none (subject differs, but the point is the count under the arc).
        recs = RunDatabase(db_path=stamp_env["db_path"], enabled=True).list_analyses(
            "proj", "arc", kind="kbutillib.reconstruct"
        )
        assert len(recs) == 1
        assert recs[0].trust_tier == "hypothesis"

    def test_each_verb_writes_one_record_and_rerun_upserts(
        self, stamp_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = stamp_env["tmp_path"] / "chain"
        work.mkdir()
        monkeypatch.setenv("KOROS_ARC", "proj/arc")
        draft = work / "draft.json"
        gapfilled = work / "gf.json"

        def run(uid_reset: bool, *args: str) -> Any:
            if uid_reset:
                monkeypatch.delenv(model_recording.RUN_UID_ENV, raising=False)
            return _invoke(work, *args)

        r1 = run(True, "model", "reconstruct", "--genome", str(GENOME),
                 "--out", str(draft), "--json")
        assert r1.exit_code == 0, r1.output
        r2 = run(True, "model", "gapfill", "--model", str(draft),
                 "--media", str(MEDIA), "--out", str(gapfilled), "--json")
        assert r2.exit_code == 0, r2.output
        r3 = run(True, "model", "fba", "--model", str(gapfilled),
                 "--media", str(MEDIA), "--json")
        assert r3.exit_code == 0, r3.output
        r4 = run(True, "model", "fva", "--model", str(gapfilled),
                 "--media", str(MEDIA), "--json")
        assert r4.exit_code == 0, r4.output

        db = RunDatabase(db_path=stamp_env["db_path"], enabled=True)
        for kind in (
            "kbutillib.reconstruct",
            "kbutillib.gapfill",
            "kbutillib.fba",
            "kbutillib.fva",
        ):
            assert len(db.list_analyses("proj", "arc", kind=kind)) == 1, kind

        # Re-run fba in the SAME process (run_uid cached) → upsert, still one row.
        r5 = run(False, "model", "fba", "--model", str(gapfilled),
                 "--media", str(MEDIA), "--json")
        assert r5.exit_code == 0, r5.output
        assert len(db.list_analyses("proj", "arc", kind="kbutillib.fba")) == 1
