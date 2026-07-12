"""Tests for the opt-in narrative-provenance feature (PRD Piece B).

See ``agent-io/prds/ee2-narrative-provenance/fullprompt.md``. Ports
narrative-connector's render-from-ledger pattern (read-only reference:
``~/king-stack/narrative-connector``) onto KBUtilLib's SQLite job ledger.

All tests run offline against fakes -- no network, no real KBase.

Coverage (Testing Decisions, Piece B):
- Render idempotency: re-rendering the same ledger twice yields an
  identical audit-cell set, no duplicates.
- Anchor / non-audit preservation: intro (cell[0]) and user-authored
  non-audit markdown survive a render; an audit cell resolves by its
  anchor, not by position.
- Bounded Navigator meta: ``data_dependencies`` stays under KBase's
  900-byte key+value cap; ``cell_count`` / ``is_temporary`` recompute.
- Narrative resolution precedence: explicit ref bypasses discovery/create;
  an existing narrative is discovered without calling create; a bare
  workspace triggers exactly one ``create_new_narrative``.
- Callback wiring: a terminal report renders; a non-terminal report is a
  no-op; ``record_narrative_provenance=False`` never resolves, creates, or
  renders (the default-off invariant), at both submit time and render time.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from kbutillib.kb_job_utils.state import JobRecord, JobState
from kbutillib.kb_job_utils.store import JobStore
from kbutillib.kb_narrative_audit import app_run_cell_anchor
from kbutillib.kb_ws_utils import KBWSUtilsImpl
from kbutillib.shared_env_utils import SharedEnvUtils

# ── Fakes ────────────────────────────────────────────────────────────────


class FakeWorkspace:
    """In-memory fake of the slice of the Workspace API that narrative
    resolution + ``append_app_run_audit`` touch: ``get_workspace_info``,
    ``list_objects``, ``get_objects2``, ``save_objects``."""

    def __init__(self):
        # (wsid, objid) -> list of successive `data` dicts (index 0 = v1)
        self._versions: dict[tuple[int, int], list[dict]] = {}
        self._names: dict[tuple[int, int], str] = {}
        self._ws_meta: dict[int, dict] = {}
        self._listed_narratives: dict[int, list[tuple]] = {}
        self.save_objects_calls: list[dict] = []

    # -- test setup helpers ------------------------------------------------

    def seed_narrative(self, wsid: int, objid: int, cells: list, name: str = "MyNarrative"):
        self._versions[(wsid, objid)] = [{"cells": cells}]
        self._names[(wsid, objid)] = name

    def set_ws_meta(self, wsid: int, meta: dict) -> None:
        self._ws_meta[wsid] = meta

    def set_discoverable_narrative(self, wsid: int, objid: int, ver: int = 1):
        """Make ``list_objects`` (the discovery fallback scan) surface an
        existing Narrative even though no ``narrative`` ws-meta pointer is
        set."""
        info = (objid, self._names.get((wsid, objid), "MyNarrative"),
                "KBaseNarrative.Narrative-4.0", "date", ver, "user", wsid,
                f"ws{wsid}", "chsum", 100, {})
        self._listed_narratives.setdefault(wsid, []).append(info)

    # -- Workspace API surface ---------------------------------------------

    def get_workspace_info(self, params):
        wsid = params["id"]
        return [wsid, f"ws{wsid}", "user", "date", 0, "a", "n", "unlocked",
                self._ws_meta.get(wsid, {})]

    def list_objects(self, args):
        wsid = None
        if "ids" in args:
            wsid = args["ids"][0]
        return list(self._listed_narratives.get(wsid, []))

    def get_objects2(self, args):
        ref = args["objects"][0]["ref"]
        parts = ref.split("/")
        wsid, objid = int(parts[0]), int(parts[1])
        versions = self._versions[(wsid, objid)]
        ver = len(versions)
        data = versions[-1]
        info = [objid, self._names[(wsid, objid)], "KBaseNarrative.Narrative-4.0",
                "date", ver, "user", wsid, f"ws{wsid}", "chsum", 100, {}]
        return {"data": [{"info": info, "data": data}]}

    def save_objects(self, params):
        self.save_objects_calls.append(params)
        wsid = params["id"]
        obj = params["objects"][0]
        objid = None
        for (w, o), name in self._names.items():
            if w == wsid and name == obj["name"]:
                objid = o
                break
        if objid is None:
            existing_ids = [o for (w, o) in self._versions if w == wsid]
            objid = (max(existing_ids) + 1) if existing_ids else 1
            self._names[(wsid, objid)] = obj["name"]
            self._versions[(wsid, objid)] = []
        self._versions[(wsid, objid)].append(obj["data"])
        ver = len(self._versions[(wsid, objid)])
        info = [objid, obj["name"], "KBaseNarrative.Narrative-4.0", "date",
                ver, "user", wsid, f"ws{wsid}", "chsum", 100, obj.get("meta", {})]
        return [info]


def _make_ws_impl(temp_dir: str) -> tuple[KBWSUtilsImpl, FakeWorkspace]:
    """Build a KBWSUtilsImpl wired to a fresh FakeWorkspace."""
    fake_ws = FakeWorkspace()
    with patch("kbutillib.kb_ws_utils.Workspace", return_value=fake_ws):
        with patch("kbutillib.kb_ws_utils.HandleService"):
            env = SharedEnvUtils(
                token_file=f"{temp_dir}/tokens",
                kbase_token_file=f"{temp_dir}/kbase_token",
                token="fake-token",
            )
            impl = KBWSUtilsImpl(env, kb_version="prod")
            impl._ws_client = fake_ws
            return impl, fake_ws


def _make_job_store() -> JobStore:
    tmp = tempfile.mktemp(suffix=".db")
    return JobStore(db_path=Path(tmp))


def _make_record(
    job_id: str,
    workspace_id: int,
    *,
    state: JobState = JobState.COMPLETED,
    app_id: str = "kb_module/run_thing",
    method: str = "kb_module.run_thing",
    service_ver: str = "abc123",
    params: list | None = None,
    output_upa: str | None = None,
    error_message: str | None = None,
    when: datetime | None = None,
) -> JobRecord:
    when = when or datetime.now(timezone.utc)
    ee2_raw: dict = {"status": state.value, "job_id": job_id}
    if output_upa:
        ee2_raw["result"] = [{"report_ref": output_upa}]
    return JobRecord(
        job_id=job_id,
        method=method,
        params={"method": method, "app_id": app_id, "service_ver": service_ver,
                "params": params or [{"input": "x"}]},
        state=state,
        workspace_id=workspace_id,
        created_at=when,
        updated_at=when + timedelta(seconds=30),
        ee2_raw=ee2_raw,
        error_message=error_message,
    )


# ── Render idempotency + anchor/preservation ───────────────────────────────


class TestAppendAppRunAuditRender:
    def test_render_idempotent_no_duplicates(self, temp_dir):
        """Rendering twice from the same ledger yields an identical
        audit-cell set -- no duplicates (strip-and-rerender)."""
        impl, fake_ws = _make_ws_impl(temp_dir)
        store = _make_job_store()
        store.upsert(_make_record("job-1", 500, output_upa="500/10/1"))
        store.upsert(_make_record("job-2", 500, output_upa="500/11/1"))

        intro_cell = {"cell_type": "markdown", "source": "# Intro", "metadata": {}}
        fake_ws.seed_narrative(500, 42, [intro_cell])

        ref1 = impl.append_app_run_audit("500/42", 500, job_store=store)
        cells_after_first = fake_ws._versions[(500, 42)][-1]["cells"]
        audit_cells_first = [c for c in cells_after_first if c["metadata"].get("kbase", {}).get("audit")]
        assert len(audit_cells_first) == 2

        ref2 = impl.append_app_run_audit("500/42", 500, job_store=store)
        cells_after_second = fake_ws._versions[(500, 42)][-1]["cells"]
        audit_cells_second = [c for c in cells_after_second if c["metadata"].get("kbase", {}).get("audit")]

        assert len(audit_cells_second) == 2  # not 4 -- no duplicates
        assert audit_cells_first == audit_cells_second  # byte-identical re-render
        assert ref1.startswith("500/42/")
        assert ref2.startswith("500/42/")
        # Two independent save_objects calls (v2, v3) both converged to the
        # same 2-audit-cell content.
        assert len(fake_ws.save_objects_calls) == 2

    def test_intro_and_user_cells_preserved_anchor_resolves(self, temp_dir):
        """cell[0] intro and user-authored non-audit markdown survive a
        render; an audit cell is identified by its anchor regardless of
        position."""
        impl, fake_ws = _make_ws_impl(temp_dir)
        store = _make_job_store()
        store.upsert(_make_record("job-anchor-1", 700, output_upa="700/5/1"))

        intro_cell = {"cell_type": "markdown", "source": "# Project intro", "metadata": {}}
        user_cell = {"cell_type": "markdown", "source": "my own notes", "metadata": {}}
        # A stale audit cell (wrong content) that MUST be stripped on render.
        stale_audit_cell = {
            "cell_type": "markdown",
            "source": "stale content " + app_run_cell_anchor("job-anchor-1"),
            "metadata": {"kbase": {"audit": True}},
        }
        fake_ws.seed_narrative(700, 9, [intro_cell, user_cell, stale_audit_cell])

        impl.append_app_run_audit("700/9", 700, job_store=store)

        new_cells = fake_ws._versions[(700, 9)][-1]["cells"]
        assert new_cells[0] == intro_cell  # intro preserved verbatim, position 0
        assert user_cell in new_cells  # user cell preserved (order after audit block)

        anchor = app_run_cell_anchor("job-anchor-1")
        matches = [c for c in new_cells if anchor in c["source"]]
        assert len(matches) == 1  # exactly one cell carries this anchor
        assert "stale content" not in matches[0]["source"]  # stale content replaced
        assert matches[0]["metadata"]["kbase"]["audit"] is True

    def test_only_terminal_jobs_rendered(self, temp_dir):
        """A still-running (non-terminal) job produces no audit cell yet."""
        impl, fake_ws = _make_ws_impl(temp_dir)
        store = _make_job_store()
        store.upsert(_make_record("job-done", 800, output_upa="800/1/1"))
        store.upsert(_make_record("job-running", 800, state=JobState.RUNNING))

        fake_ws.seed_narrative(800, 3, [{"cell_type": "markdown", "source": "intro", "metadata": {}}])
        impl.append_app_run_audit("800/3", 800, job_store=store)

        cells = fake_ws._versions[(800, 3)][-1]["cells"]
        audit_cells = [c for c in cells if c["metadata"].get("kbase", {}).get("audit")]
        assert len(audit_cells) == 1
        assert app_run_cell_anchor("job-done") in audit_cells[0]["source"]
        assert app_run_cell_anchor("job-running") not in "".join(c["source"] for c in cells)

    def test_error_job_rendered_with_error_state(self, temp_dir):
        """A terminal error/terminated job IS rendered (failures visible)."""
        impl, fake_ws = _make_ws_impl(temp_dir)
        store = _make_job_store()
        store.upsert(_make_record("job-fail", 810, state=JobState.ERROR, error_message="boom"))
        fake_ws.seed_narrative(810, 4, [{"cell_type": "markdown", "source": "intro", "metadata": {}}])

        impl.append_app_run_audit("810/4", 810, job_store=store)

        cells = fake_ws._versions[(810, 4)][-1]["cells"]
        audit_cells = [c for c in cells if c["metadata"].get("kbase", {}).get("audit")]
        assert len(audit_cells) == 1
        assert "error" in audit_cells[0]["source"]
        assert "boom" in audit_cells[0]["source"]

    def test_workspace_filter_excludes_other_workspaces(self, temp_dir):
        """Only jobs in the target workspace_id are rendered."""
        impl, fake_ws = _make_ws_impl(temp_dir)
        store = _make_job_store()
        store.upsert(_make_record("job-in-900", 900, output_upa="900/1/1"))
        store.upsert(_make_record("job-in-901", 901, output_upa="901/1/1"))
        fake_ws.seed_narrative(900, 7, [{"cell_type": "markdown", "source": "intro", "metadata": {}}])

        impl.append_app_run_audit("900/7", 900, job_store=store)

        cells = fake_ws._versions[(900, 7)][-1]["cells"]
        rendered_source = "".join(c.get("source", "") for c in cells)
        assert app_run_cell_anchor("job-in-900") in rendered_source
        assert app_run_cell_anchor("job-in-901") not in rendered_source

    def test_latest_version_read_via_stripped_ref(self, temp_dir):
        """A ref carrying a stale version suffix is stripped -- the LATEST
        version is always fetched (append_app_run_audit is idempotent
        under a version-bearing ref too)."""
        impl, fake_ws = _make_ws_impl(temp_dir)
        store = _make_job_store()
        store.upsert(_make_record("job-v", 950, output_upa="950/1/1"))
        fake_ws.seed_narrative(950, 2, [{"cell_type": "markdown", "source": "intro", "metadata": {}}])

        impl.append_app_run_audit("950/2/1", 950, job_store=store)  # version-suffixed ref

        cells = fake_ws._versions[(950, 2)][-1]["cells"]
        assert any(app_run_cell_anchor("job-v") in c.get("source", "") for c in cells)


# ── Bounded Navigator metadata ──────────────────────────────────────────────


class TestBoundedNavigatorMeta:
    def test_data_dependencies_bounded_under_900_bytes(self, temp_dir):
        """A workspace with many referenced objects produces a
        data_dependencies value under the 900-byte cap; cell_count /
        is_temporary recompute correctly."""
        impl, fake_ws = _make_ws_impl(temp_dir)
        store = _make_job_store()
        for i in range(200):
            store.upsert(_make_record(f"job-many-{i}", 1000, output_upa=f"1000/{i}/1"))
        fake_ws.seed_narrative(1000, 6, [{"cell_type": "markdown", "source": "intro", "metadata": {}}])

        impl.append_app_run_audit("1000/6", 1000, job_store=store)

        saved_meta = fake_ws.save_objects_calls[-1]["objects"][0]["meta"]
        key_value_bytes = len("data_dependencies".encode("utf-8")) + len(
            saved_meta["data_dependencies"].encode("utf-8")
        )
        assert key_value_bytes < 900
        assert saved_meta["is_temporary"] == "false"
        cells = fake_ws._versions[(1000, 6)][-1]["cells"]
        assert saved_meta["cell_count"] == str(len(cells))
        assert int(saved_meta["data_object_count"]) == 200

    def test_small_workspace_meta_not_truncated(self, temp_dir):
        impl, fake_ws = _make_ws_impl(temp_dir)
        store = _make_job_store()
        store.upsert(_make_record("job-small", 1100, output_upa="1100/2/1"))
        fake_ws.seed_narrative(1100, 8, [{"cell_type": "markdown", "source": "intro", "metadata": {}}])

        impl.append_app_run_audit("1100/8", 1100, job_store=store)

        saved_meta = fake_ws.save_objects_calls[-1]["objects"][0]["meta"]
        deps = json.loads(saved_meta["data_dependencies"])
        assert "1100/2/1" in deps
        assert "more" not in " ".join(deps)  # nothing truncated for one object


# ── Narrative resolution precedence ─────────────────────────────────────────


class TestNarrativeResolutionPrecedence:
    def test_explicit_ref_bypasses_discovery_and_create(self, temp_dir):
        impl, fake_ws = _make_ws_impl(temp_dir)
        with patch.object(impl, "find_narrative_in_workspace") as mock_find, \
             patch.object(impl, "create_narrative") as mock_create:
            result = impl.resolve_narrative(1200, narrative_ref="1200/99")

        assert result == "1200/99"
        mock_find.assert_not_called()
        mock_create.assert_not_called()

    def test_existing_narrative_discovered_without_create(self, temp_dir):
        impl, fake_ws = _make_ws_impl(temp_dir)
        fake_ws.set_ws_meta(1300, {"narrative": "55"})

        with patch.object(impl, "create_narrative") as mock_create:
            result = impl.resolve_narrative(1300)

        assert result == "1300/55"
        mock_create.assert_not_called()

    def test_bare_workspace_triggers_exactly_one_create(self, temp_dir):
        impl, fake_ws = _make_ws_impl(temp_dir)
        # No ws-meta 'narrative' pointer, and list_objects (discovery
        # fallback scan) returns nothing -> bare workspace.
        fake_narrative_service = MagicMock()
        fake_narrative_service.create_new_narrative.return_value = {
            "workspaceInfo": [1400, "new_ws"],
            "narrativeInfo": [77, "Narrative", "KBaseNarrative.Narrative-4.0",
                               "date", 1, "user", 1400],
        }
        impl._narrative_service = fake_narrative_service

        result = impl.resolve_narrative(1400)

        assert result == "1400/77"
        fake_narrative_service.create_new_narrative.assert_called_once()

    def test_discovery_via_type_scan_when_no_meta_pointer(self, temp_dir):
        """Discovery falls back to a KBaseNarrative.Narrative type scan
        when the workspace has no 'narrative' meta pointer."""
        impl, fake_ws = _make_ws_impl(temp_dir)
        fake_ws.set_discoverable_narrative(1500, 33)

        with patch.object(impl, "create_narrative") as mock_create:
            result = impl.resolve_narrative(1500)

        assert result == "1500/33/1"
        mock_create.assert_not_called()

    def test_find_narrative_returns_none_for_truly_bare_workspace(self, temp_dir):
        impl, fake_ws = _make_ws_impl(temp_dir)
        assert impl.find_narrative_in_workspace(1600) is None


# ── AppRunner submit-time resolution + narrative_id stamping ───────────────


def _make_ee2_mock(job_id: str = "job-narrative-001") -> MagicMock:
    ee2 = MagicMock()
    ee2.run_job.return_value = job_id
    return ee2


def _make_job_utils(ee2_mock) -> "object":
    import tempfile as _tempfile

    from kbutillib.kb_job_utils.utils import KBJobUtils
    from kbutillib.shared_env_utils import SharedEnvUtils as _SEU

    env = _SEU(config_file=False, token_file=None, kbase_token_file=None, token="fake-token")
    db_path = Path(_tempfile.mktemp(suffix=".db"))
    jutils = KBJobUtils.__new__(KBJobUtils)
    jutils._env = env
    jutils._kb_version = "prod"
    jutils._token = "fake-token"
    jutils._ee2 = ee2_mock
    jutils._store = JobStore(db_path=db_path)
    return jutils


FASTQC_SPEC_RAW_PATH = Path(__file__).parent / "fixtures" / "nms" / "nms_runFastQC.json"


def _load_fastqc_spec() -> dict:
    return json.loads(FASTQC_SPEC_RAW_PATH.read_text())


def _make_nms_mock(spec_raw: dict) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"result": [[spec_raw]]}
    return MagicMock(return_value=mock_resp)


class TestRunAppNarrativeSubmitTime:
    def _make_runner(self, ee2_mock, ws_mock):
        from kbutillib.kb_app_runner.nms import NMSSpecCache
        from kbutillib.kb_app_runner.runner import AppRunner

        cache = NMSSpecCache()
        jutils = _make_job_utils(ee2_mock)
        runner = AppRunner(ws=ws_mock, nms_cache=cache, job_store=jutils)
        return runner, jutils

    def test_default_off_never_resolves_narrative(self):
        """record_narrative_provenance=False (default) never calls
        resolve_narrative -- the default-off invariant at submit time."""
        ee2 = _make_ee2_mock()
        ws = MagicMock()
        ws.ws_id = 2000
        ws.ws_name = "narrative_2000"
        ws.set_ws = MagicMock()
        runner, jutils = self._make_runner(ee2, ws)

        spec_raw = _load_fastqc_spec()
        with patch("kbutillib.kb_app_runner.nms.requests.post", _make_nms_mock(spec_raw)):
            handle = runner.run_app("kb_fastqc/runFastQC", {"input_file_ref": "2000/2/1"}, 2000)

        ws.resolve_narrative.assert_not_called()
        assert "record_narrative_provenance" not in handle.meta
        assert "narrative_ref" not in handle.meta
        record = jutils.get_record(handle.job_id)
        assert record.narrative_id is None

    def test_opt_in_resolves_and_stamps_narrative_id(self):
        """record_narrative_provenance=True resolves the Narrative at
        submit time, stamps JobRecord.narrative_id, and stashes
        record_narrative_provenance/narrative_ref/narrative_id on
        handle.meta."""
        ee2 = _make_ee2_mock()
        ws = MagicMock()
        ws.ws_id = 2100
        ws.ws_name = "narrative_2100"
        ws.set_ws = MagicMock()
        ws.resolve_narrative.return_value = "2100/88"
        runner, jutils = self._make_runner(ee2, ws)

        spec_raw = _load_fastqc_spec()
        with patch("kbutillib.kb_app_runner.nms.requests.post", _make_nms_mock(spec_raw)):
            handle = runner.run_app(
                "kb_fastqc/runFastQC", {"input_file_ref": "2100/2/1"}, 2100,
                record_narrative_provenance=True,
            )

        ws.resolve_narrative.assert_called_once_with(2100, None)
        assert handle.meta["record_narrative_provenance"] is True
        assert handle.meta["narrative_ref"] == "2100/88"
        assert handle.meta["narrative_id"] == 88
        record = jutils.get_record(handle.job_id)
        assert record.narrative_id == 88

    def test_explicit_narrative_ref_forwarded_to_resolve(self):
        ee2 = _make_ee2_mock()
        ws = MagicMock()
        ws.ws_id = 2200
        ws.ws_name = "narrative_2200"
        ws.set_ws = MagicMock()
        ws.resolve_narrative.return_value = "2200/50"
        runner, jutils = self._make_runner(ee2, ws)

        spec_raw = _load_fastqc_spec()
        with patch("kbutillib.kb_app_runner.nms.requests.post", _make_nms_mock(spec_raw)):
            runner.run_app(
                "kb_fastqc/runFastQC", {"input_file_ref": "2200/2/1"}, 2200,
                record_narrative_provenance=True, narrative_ref="2200/50",
            )

        ws.resolve_narrative.assert_called_once_with(2200, "2200/50")

    def test_run_apps_parallel_forwards_narrative_fields(self):
        from kbutillib.kb_app_runner.runner import AppCall

        ee2 = _make_ee2_mock()
        ws = MagicMock()
        ws.ws_id = 2300
        ws.ws_name = "narrative_2300"
        ws.set_ws = MagicMock()
        ws.resolve_narrative.return_value = "2300/60"
        runner, jutils = self._make_runner(ee2, ws)

        spec_raw = _load_fastqc_spec()
        call = AppCall(
            app_id="kb_fastqc/runFastQC",
            params={"input_file_ref": "2300/2/1"},
            workspace=2300,
            record_narrative_provenance=True,
        )
        with patch("kbutillib.kb_app_runner.nms.requests.post", _make_nms_mock(spec_raw)):
            handles = runner.run_apps_parallel([call])

        assert handles[0].meta["record_narrative_provenance"] is True
        ws.resolve_narrative.assert_called_once_with(2300, None)

    def test_run_app_if_missing_forwards_narrative_fields(self):
        ee2 = _make_ee2_mock()
        ws = MagicMock()
        ws.ws_id = 2400
        ws.ws_name = "narrative_2400"
        ws.set_ws = MagicMock()
        ws.list_ws_objects.return_value = {}
        ws.resolve_narrative.return_value = "2400/70"
        runner, jutils = self._make_runner(ee2, ws)

        spec_raw = _load_fastqc_spec()
        with patch("kbutillib.kb_app_runner.nms.requests.post", _make_nms_mock(spec_raw)):
            handle = runner.run_app_if_missing(
                "kb_fastqc/runFastQC", {"input_file_ref": "2400/2/1"}, 2400,
                output_name="out.obj", output_type="KBaseReport.Report",
                record_narrative_provenance=True,
            )

        ws.resolve_narrative.assert_called_once_with(2400, None)
        assert handle.meta["narrative_ref"] == "2400/70"


# ── Callback wiring (audit_callback) ────────────────────────────────────────


class TestAuditCallback:
    def _make_runner_with_ws(self, ws_mock):
        from kbutillib.kb_app_runner.nms import NMSSpecCache
        from kbutillib.kb_app_runner.runner import AppRunner

        jutils = _make_job_utils(_make_ee2_mock())
        return AppRunner(ws=ws_mock, nms_cache=NMSSpecCache(), job_store=jutils)

    def _make_report(self, state: str, wsid: int, meta: dict):
        from kbutillib.kb_app_runner.monitor import JobHandle, JobReport

        handle = JobHandle(job_id="job-cb-1", app_id="kb_x/run", wsid=wsid, meta=meta)
        return JobReport(handle=handle, state=state, result={"result": []})

    def test_terminal_success_renders_with_correct_wsid(self):
        ws = MagicMock()
        runner = self._make_runner_with_ws(ws)
        callback = runner.audit_callback()

        report = self._make_report("completed", 3000, {"narrative_ref": "3000/1"})
        callback(report)

        ws.append_app_run_audit.assert_called_once_with("3000/1", 3000)

    def test_terminal_error_also_renders(self):
        """Failures are visible too -- error/terminated is also terminal."""
        ws = MagicMock()
        runner = self._make_runner_with_ws(ws)
        callback = runner.audit_callback()

        report = self._make_report("error", 3100, {"narrative_ref": "3100/2"})
        callback(report)

        ws.append_app_run_audit.assert_called_once_with("3100/2", 3100)

    def test_non_terminal_report_is_noop(self):
        ws = MagicMock()
        runner = self._make_runner_with_ws(ws)
        callback = runner.audit_callback()

        for state in ("queued", "running"):
            report = self._make_report(state, 3200, {"narrative_ref": "3200/1"})
            callback(report)

        ws.append_app_run_audit.assert_not_called()

    def test_record_narrative_provenance_false_never_renders(self):
        """A handle submitted with record_narrative_provenance=False (no
        narrative_ref stashed in meta) is a no-op -- nothing is resolved,
        created, or rendered, even on a terminal report."""
        ws = MagicMock()
        runner = self._make_runner_with_ws(ws)
        callback = runner.audit_callback()

        report = self._make_report("completed", 3300, {})  # no narrative_ref
        callback(report)

        ws.append_app_run_audit.assert_not_called()
        ws.resolve_narrative.assert_not_called()
        ws.create_narrative.assert_not_called()
        ws.find_narrative_in_workspace.assert_not_called()

    def test_explicit_narrative_ref_overrides_handle_meta(self):
        """An explicit narrative_ref passed to audit_callback() wins over
        whatever is stashed on the handle's meta."""
        ws = MagicMock()
        runner = self._make_runner_with_ws(ws)
        callback = runner.audit_callback(narrative_ref="9999/1")

        report = self._make_report("completed", 3400, {"narrative_ref": "3400/5"})
        callback(report)

        ws.append_app_run_audit.assert_called_once_with("9999/1", 3400)
