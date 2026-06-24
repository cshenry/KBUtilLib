"""Unit tests for the kb_app_runner module.

All tests run offline: ``requests`` is mocked for NMS HTTP calls and
the EE2 client is replaced with a :class:`~unittest.mock.MagicMock`.
No test reaches the network.

Coverage:
- AC#2: run_app submits correct EE2 call shape (method, service_ver, app_id, wsid, params).
- AC#3: AmbiguousParams raised for mixed UI/service keys.
- AC#4: list[dict] params passed through as service-shape without remap.
- AC#5: pin_version overrides NMS-supplied service_ver.
- AC#6: NMSSpecCache issues exactly one NMS RPC per app_id.
- AC#7: run_app_if_missing returns ExistingObject when output already exists.
- AC#8: run_app_if_missing submits and returns JobHandle when output absent.
- AC#9: JobMonitor.wait_all maps EE2 states correctly.
- AC#10: wait_all calls get_job_logs once for errored jobs.
- AC#11: wait_all uses check_jobs (batch), not per-job check_job.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Fixtures directory path
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "nms"


def _load_fixture(filename: str) -> dict:
    return json.loads((FIXTURES_DIR / filename).read_text())


# ── Shared fixture data ────────────────────────────────────────────────────────

FASTQC_SPEC_RAW = _load_fixture("nms_runFastQC.json")
SRA_SPEC_RAW = _load_fixture("nms_import_sra_as_reads_from_web.json")
METASPADES_SPEC_RAW = _load_fixture("nms_run_metaSPAdes.json")
QUAST_SPEC_RAW = _load_fixture("nms_run_QUAST_app.json")


def _nms_response_for(spec_raw: dict) -> dict:
    """Build a fake NMS JSON-RPC response wrapping *spec_raw*."""
    return {"result": [[spec_raw]]}


def _make_nms_mock(spec_raw: dict) -> MagicMock:
    """Return a mock requests.post that returns *spec_raw* wrapped in NMS shape."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = _nms_response_for(spec_raw)
    mock_post = MagicMock(return_value=mock_resp)
    return mock_post


def _make_ee2_mock(job_id: str = "job-abc123") -> MagicMock:
    """Return a minimal EE2 client mock that can submit a job."""
    ee2 = MagicMock()
    ee2.run_job.return_value = job_id
    return ee2


def _make_job_utils(ee2_mock: MagicMock | None = None, db_path: Path | None = None) -> "KBJobUtils":
    """Build a KBJobUtils with a mocked EE2 client and a temp SQLite store."""
    import tempfile

    from kbutillib.kb_job_utils.store import JobStore
    from kbutillib.kb_job_utils.utils import KBJobUtils
    from kbutillib.shared_env_utils import SharedEnvUtils

    if ee2_mock is None:
        ee2_mock = _make_ee2_mock()

    env = SharedEnvUtils(config_file=False, token_file=None, kbase_token_file=None,
                         token="fake-token")
    if db_path is None:
        tmp = tempfile.mktemp(suffix=".db")
        db_path = Path(tmp)

    jutils = KBJobUtils.__new__(KBJobUtils)
    jutils._env = env
    jutils._kb_version = "prod"
    jutils._token = "fake-token"
    jutils._ee2 = ee2_mock
    jutils._store = JobStore(db_path=db_path)
    return jutils


def _make_ws_mock(wsid: int = 261700) -> MagicMock:
    """Return a mock KBWSUtils that resolves workspace to *wsid*."""
    ws = MagicMock()
    ws.ws_id = wsid
    ws.ws_name = f"narrative_{wsid}"   # real str so system-var injection is serializable
    ws.set_ws = MagicMock()
    return ws


# ── NMSSpecCache tests ─────────────────────────────────────────────────────────


class TestNMSSpecCache:
    def test_parse_fastqc_spec(self):
        """Cache correctly parses the FastQC fixture."""
        from kbutillib.kb_app_runner.nms import NMSSpecCache

        cache = NMSSpecCache()
        with patch("kbutillib.kb_app_runner.nms.requests.post", _make_nms_mock(FASTQC_SPEC_RAW)):
            spec = cache.get("kb_fastqc/runFastQC")

        assert spec.app_id == "kb_fastqc/runFastQC"
        assert spec.method == "kb_fastqc.runFastQC"
        assert spec.service_ver == "7e67f706dcbfa6008b7b07585fadff3e790d83f0"
        # input_mapping should include the workspace system variable and input_file_ref
        keys = {entry.get("target_property") for entry in spec.input_mapping}
        assert "input_ws" in keys
        assert "input_file_ref" in keys

    def test_cache_hit_issues_one_rpc(self):
        """AC#6: exactly one NMS RPC per app_id across repeated get() calls."""
        from kbutillib.kb_app_runner.nms import NMSSpecCache

        mock_post = _make_nms_mock(FASTQC_SPEC_RAW)
        cache = NMSSpecCache()
        with patch("kbutillib.kb_app_runner.nms.requests.post", mock_post):
            spec1 = cache.get("kb_fastqc/runFastQC")
            spec2 = cache.get("kb_fastqc/runFastQC")
            spec3 = cache.get("kb_fastqc/runFastQC")

        assert mock_post.call_count == 1
        assert spec1 is spec2 is spec3

    def test_spec_not_found_on_nms_error(self):
        """AC#6 (error path): SpecNotFound is raised when NMS returns an error."""
        from kbutillib.kb_app_runner.errors import SpecNotFound
        from kbutillib.kb_app_runner.nms import NMSSpecCache

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"error": {"message": "App not found", "code": -32601}}
        mock_post = MagicMock(return_value=mock_resp)

        cache = NMSSpecCache()
        with patch("kbutillib.kb_app_runner.nms.requests.post", mock_post):
            with pytest.raises(SpecNotFound) as exc_info:
                cache.get("nonexistent/app")

        assert "nonexistent/app" in str(exc_info.value)

    def test_spec_not_found_on_empty_result(self):
        """SpecNotFound when NMS returns an empty spec list."""
        from kbutillib.kb_app_runner.errors import SpecNotFound
        from kbutillib.kb_app_runner.nms import NMSSpecCache

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"result": [[]]}
        mock_post = MagicMock(return_value=mock_resp)

        cache = NMSSpecCache()
        with patch("kbutillib.kb_app_runner.nms.requests.post", mock_post):
            with pytest.raises(SpecNotFound):
                cache.get("empty/app")

    def test_narrative_names_fastqc(self):
        """narrative_names() returns only input_parameter keys (not system vars)."""
        from kbutillib.kb_app_runner.nms import NMSSpecCache

        cache = NMSSpecCache()
        with patch("kbutillib.kb_app_runner.nms.requests.post", _make_nms_mock(FASTQC_SPEC_RAW)):
            spec = cache.get("kb_fastqc/runFastQC")

        names = spec.narrative_names()
        assert "input_file_ref" in names
        # Workspace system variable should NOT appear in narrative_names
        assert "workspace" not in names

    def test_clear_evicts_cache(self):
        """clear() causes the next get() to re-issue the RPC."""
        from kbutillib.kb_app_runner.nms import NMSSpecCache

        mock_post = _make_nms_mock(FASTQC_SPEC_RAW)
        cache = NMSSpecCache()
        with patch("kbutillib.kb_app_runner.nms.requests.post", mock_post):
            cache.get("kb_fastqc/runFastQC")
            cache.clear()
            cache.get("kb_fastqc/runFastQC")

        assert mock_post.call_count == 2

    def test_tag_forwarded_in_payload(self):
        """A non-None tag is sent as `tag` inside the get_method_spec params."""
        from kbutillib.kb_app_runner.nms import NMSSpecCache

        mock_post = _make_nms_mock(FASTQC_SPEC_RAW)
        cache = NMSSpecCache()
        with patch("kbutillib.kb_app_runner.nms.requests.post", mock_post):
            cache.get("kb_fastqc/runFastQC", "beta")

        sent = mock_post.call_args.kwargs["json"]
        assert sent["params"][0] == {"ids": ["kb_fastqc/runFastQC"], "tag": "beta"}

    def test_no_tag_omits_tag_key(self):
        """tag=None omits the `tag` key entirely (released-spec default)."""
        from kbutillib.kb_app_runner.nms import NMSSpecCache

        mock_post = _make_nms_mock(FASTQC_SPEC_RAW)
        cache = NMSSpecCache()
        with patch("kbutillib.kb_app_runner.nms.requests.post", mock_post):
            cache.get("kb_fastqc/runFastQC")

        sent = mock_post.call_args.kwargs["json"]
        assert sent["params"][0] == {"ids": ["kb_fastqc/runFastQC"]}

    def test_input_mapping_injects_workspace_system_var(self):
        """narrative_system_variable 'workspace' → target gets the workspace NAME."""
        from kbutillib.kb_app_runner.runner import _apply_input_mapping

        mapping = (
            {"input_parameter": "object_ref", "target_property": "object_ref"},
            {"narrative_system_variable": "workspace", "target_property": "output_workspace"},
            {"narrative_system_variable": "workspace_id", "target_property": "workspace_id"},
        )
        out = _apply_input_mapping(
            {"object_ref": "263213/g1"}, mapping,
            workspace_name="narrative_263213", workspace_id=263213,
        )
        assert out == {
            "object_ref": "263213/g1",
            "output_workspace": "narrative_263213",
            "workspace_id": 263213,
        }

    def test_input_mapping_skips_unresolved_workspace(self):
        """Without a resolved name, the workspace system var is omitted (no crash)."""
        from kbutillib.kb_app_runner.runner import _apply_input_mapping

        mapping = (
            {"input_parameter": "object_ref", "target_property": "object_ref"},
            {"narrative_system_variable": "workspace", "target_property": "output_workspace"},
        )
        out = _apply_input_mapping({"object_ref": "263213/g1"}, mapping)
        assert out == {"object_ref": "263213/g1"}  # output_workspace omitted

    def test_cache_keyed_by_app_and_tag(self):
        """Same app, different tags → two cache entries → two RPCs."""
        from kbutillib.kb_app_runner.nms import NMSSpecCache

        mock_post = _make_nms_mock(FASTQC_SPEC_RAW)
        cache = NMSSpecCache()
        with patch("kbutillib.kb_app_runner.nms.requests.post", mock_post):
            cache.get("kb_fastqc/runFastQC", "beta")
            cache.get("kb_fastqc/runFastQC", "beta")   # cached → no new RPC
            cache.get("kb_fastqc/runFastQC")           # different (app, tag) key

        assert mock_post.call_count == 2

    def test_parse_sra_spec(self):
        """Cache correctly parses the SRA import fixture."""
        from kbutillib.kb_app_runner.nms import NMSSpecCache

        cache = NMSSpecCache()
        with patch("kbutillib.kb_app_runner.nms.requests.post",
                   _make_nms_mock(SRA_SPEC_RAW)):
            spec = cache.get("kb_uploadmethods/import_sra_as_reads_from_web")

        # The NMS spec uses the short method name from kb_service_method
        assert spec.method == "kb_uploadmethods.import_sra_from_web"
        assert spec.service_ver == "5b9346463df88a422ff5d4f4cba421679f63c73f"


# ── AppRunner tests ────────────────────────────────────────────────────────────


class TestAppRunner:
    def _make_runner(self, spec_raw: dict, ee2_mock=None, wsid: int = 261700):
        """Build an AppRunner with mocked NMS and EE2."""
        from kbutillib.kb_app_runner.nms import NMSSpecCache
        from kbutillib.kb_app_runner.runner import AppRunner

        if ee2_mock is None:
            ee2_mock = _make_ee2_mock()

        nms_mock = _make_nms_mock(spec_raw)
        cache = NMSSpecCache()

        ws = _make_ws_mock(wsid)
        jutils = _make_job_utils(ee2_mock)

        runner = AppRunner(ws=ws, nms_cache=cache, job_store=jutils)
        return runner, nms_mock, ee2_mock

    def test_run_app_ui_shape_fastqc(self):
        """AC#2: run_app submits correct EE2 call shape for UI-shape params."""
        from kbutillib.kb_app_runner.runner import AppRunner

        ee2 = _make_ee2_mock("job-fastqc-001")
        runner, nms_mock, _ = self._make_runner(FASTQC_SPEC_RAW, ee2)

        with patch("kbutillib.kb_app_runner.nms.requests.post", nms_mock):
            handle = runner.run_app(
                "kb_fastqc/runFastQC",
                {"input_file_ref": "261700/2/1"},
                261700,
            )

        assert handle.job_id == "job-fastqc-001"
        assert handle.app_id == "kb_fastqc/runFastQC"
        assert handle.wsid == 261700

        # EE2 run_job was called with the right method and service_ver
        ee2.run_job.assert_called_once()
        call_args = ee2.run_job.call_args[0][0]
        assert call_args["method"] == "kb_fastqc.runFastQC"
        assert call_args["service_ver"] == "7e67f706dcbfa6008b7b07585fadff3e790d83f0"
        assert call_args.get("app_id") == "kb_fastqc/runFastQC"
        assert call_args.get("wsid") == 261700
        # params should be remapped: input_file_ref -> input_file_ref (same name here)
        assert isinstance(call_args["params"], list)
        assert "input_file_ref" in call_args["params"][0]

    def test_run_app_service_shape_list(self):
        """AC#4: list[dict] params passed through as service-shape without remap."""
        ee2 = _make_ee2_mock("job-svc-001")
        runner, nms_mock, _ = self._make_runner(FASTQC_SPEC_RAW, ee2)

        service_params = [{"input_file_ref": "261700/4/1", "input_ws": "my_ws"}]
        with patch("kbutillib.kb_app_runner.nms.requests.post", nms_mock):
            handle = runner.run_app("kb_fastqc/runFastQC", service_params, 261700)

        assert handle.job_id == "job-svc-001"
        call_args = ee2.run_job.call_args[0][0]
        # Passed through unchanged
        assert call_args["params"] == service_params

    def test_run_app_ambiguous_params_raises(self):
        """AC#3: AmbiguousParams raised for mixed UI and service keys."""
        from kbutillib.kb_app_runner.errors import AmbiguousParams

        runner, nms_mock, _ = self._make_runner(FASTQC_SPEC_RAW)

        mixed_params = {
            "input_file_ref": "261700/2/1",   # UI key
            "input_ws": "my_workspace",         # service key (not in narrative_names)
        }
        with patch("kbutillib.kb_app_runner.nms.requests.post", nms_mock):
            with pytest.raises(AmbiguousParams) as exc_info:
                runner.run_app("kb_fastqc/runFastQC", mixed_params, 261700)

        err = exc_info.value
        assert err.app_id == "kb_fastqc/runFastQC"
        assert "input_file_ref" in err.ui_keys
        assert "input_ws" in err.service_keys

    def test_run_app_pin_version_overrides_nms(self):
        """AC#5: pin_version overrides the NMS-supplied service_ver."""
        ee2 = _make_ee2_mock("job-pinned-001")
        runner, nms_mock, _ = self._make_runner(FASTQC_SPEC_RAW, ee2)

        with patch("kbutillib.kb_app_runner.nms.requests.post", nms_mock):
            runner.run_app(
                "kb_fastqc/runFastQC",
                {"input_file_ref": "261700/2/1"},
                261700,
                pin_version="abc123custom",
            )

        call_args = ee2.run_job.call_args[0][0]
        assert call_args["service_ver"] == "abc123custom"

    def test_run_app_cache_one_nms_rpc(self):
        """AC#6: two run_app calls for same app_id issue only one NMS RPC."""
        ee2 = _make_ee2_mock()
        runner, nms_mock, _ = self._make_runner(FASTQC_SPEC_RAW, ee2)

        with patch("kbutillib.kb_app_runner.nms.requests.post", nms_mock):
            runner.run_app("kb_fastqc/runFastQC", {"input_file_ref": "261700/2/1"}, 261700)
            runner.run_app("kb_fastqc/runFastQC", {"input_file_ref": "261700/4/1"}, 261700)

        assert nms_mock.call_count == 1

    def test_run_app_service_shape_dict_no_ui_keys(self):
        """Branch 4: dict with no UI keys treated as service-shape (wrapped in list)."""
        ee2 = _make_ee2_mock("job-svc-dict-001")
        runner, nms_mock, _ = self._make_runner(FASTQC_SPEC_RAW, ee2)

        # "input_ws" is not in narrative_names for FastQC
        service_dict = {"input_ws": "my_workspace", "input_file_ref_resolved": "261700/2/1"}
        with patch("kbutillib.kb_app_runner.nms.requests.post", nms_mock):
            handle = runner.run_app("kb_fastqc/runFastQC", service_dict, 261700)

        call_args = ee2.run_job.call_args[0][0]
        # Should be wrapped in a list
        assert call_args["params"] == [service_dict]

    def test_run_app_resolves_workspace_name(self):
        """Workspace name string is resolved to numeric wsid via KBWSUtils.set_ws."""
        from kbutillib.kb_app_runner.runner import AppRunner

        ee2 = _make_ee2_mock("job-ws-name-001")
        ws = _make_ws_mock(wsid=99999)
        from kbutillib.kb_app_runner.nms import NMSSpecCache
        cache = NMSSpecCache()
        jutils = _make_job_utils(ee2)
        runner = AppRunner(ws=ws, nms_cache=cache, job_store=jutils)

        nms_mock = _make_nms_mock(FASTQC_SPEC_RAW)
        with patch("kbutillib.kb_app_runner.nms.requests.post", nms_mock):
            handle = runner.run_app(
                "kb_fastqc/runFastQC",
                {"input_file_ref": "261700/2/1"},
                "my_workspace_name",
            )

        # set_ws should have been called with the name
        ws.set_ws.assert_called_once_with("my_workspace_name")
        # wsid should be what the mock returned
        assert handle.wsid == 99999


# ── run_app_if_missing tests ──────────────────────────────────────────────────


class TestRunAppIfMissing:
    def _make_runner_with_ws_objects(
        self,
        spec_raw: dict,
        existing_objects: dict | None = None,
        wsid: int = 261700,
    ):
        """Build AppRunner where list_ws_objects returns *existing_objects*."""
        from kbutillib.kb_app_runner.nms import NMSSpecCache
        from kbutillib.kb_app_runner.runner import AppRunner

        ee2 = _make_ee2_mock("job-ifmissing-001")
        ws = _make_ws_mock(wsid)
        # Simulate list_ws_objects: keyed by object name, value is a tuple
        # (objid, name, type, date, ver, owner, wsid, ...)
        if existing_objects is None:
            ws.list_ws_objects.return_value = {}
        else:
            ws.list_ws_objects.return_value = existing_objects

        cache = NMSSpecCache()
        jutils = _make_job_utils(ee2)
        runner = AppRunner(ws=ws, nms_cache=cache, job_store=jutils)
        return runner, _make_nms_mock(spec_raw), ee2, ws

    def test_returns_existing_object_when_output_present(self):
        """AC#7: ExistingObject returned when named output already exists."""
        from kbutillib.kb_app_runner.runner import ExistingObject

        # Object info tuple: (objid, name, type, date, ver, owner, wsid, ...)
        obj_info = (11, "reads_obj.fastqc_report", "KBaseReport.Report", "2026-06-01",
                    1, "chenry", 261700, "ws_name", {}, {}, {})
        existing = {"reads_obj.fastqc_report": obj_info}

        runner, nms_mock, ee2, ws = self._make_runner_with_ws_objects(
            FASTQC_SPEC_RAW, existing
        )

        with patch("kbutillib.kb_app_runner.nms.requests.post", nms_mock):
            result = runner.run_app_if_missing(
                "kb_fastqc/runFastQC",
                {"input_file_ref": "261700/2/1"},
                261700,
                output_name="reads_obj.fastqc_report",
                output_type="KBaseReport.Report",
            )

        assert isinstance(result, ExistingObject)
        assert result.ref == "261700/11/1"
        # EE2 should NOT have been called
        ee2.run_job.assert_not_called()

    def test_submits_when_output_absent(self):
        """AC#8: JobHandle returned and EE2 called when output absent."""
        from kbutillib.kb_app_runner.monitor import JobHandle

        runner, nms_mock, ee2, ws = self._make_runner_with_ws_objects(
            FASTQC_SPEC_RAW, {}
        )

        with patch("kbutillib.kb_app_runner.nms.requests.post", nms_mock):
            result = runner.run_app_if_missing(
                "kb_fastqc/runFastQC",
                {"input_file_ref": "261700/2/1"},
                261700,
                output_name="reads_obj.fastqc_report",
                output_type="KBaseReport.Report",
            )

        assert isinstance(result, JobHandle)
        assert result.job_id == "job-ifmissing-001"
        ee2.run_job.assert_called_once()

    def test_list_ws_objects_called_with_correct_type(self):
        """list_ws_objects is called with the specified output_type."""
        runner, nms_mock, ee2, ws = self._make_runner_with_ws_objects(FASTQC_SPEC_RAW, {})

        with patch("kbutillib.kb_app_runner.nms.requests.post", nms_mock):
            runner.run_app_if_missing(
                "kb_fastqc/runFastQC",
                {"input_file_ref": "261700/2/1"},
                261700,
                output_name="reads_obj.fastqc_report",
                output_type="KBaseReport.Report",
            )

        ws.list_ws_objects.assert_called_once_with(261700, type="KBaseReport.Report")


# ── JobMonitor tests ───────────────────────────────────────────────────────────


class TestJobMonitor:
    def _make_monitor(self, ee2_mock=None):
        """Build a JobMonitor with a mocked KBJobUtils."""
        from kbutillib.kb_app_runner.monitor import JobMonitor

        if ee2_mock is None:
            ee2_mock = MagicMock()
        jutils = _make_job_utils(ee2_mock)
        monitor = JobMonitor(jutils, poll_interval=0.0)
        return monitor, jutils, ee2_mock

    def _make_handle(self, job_id: str = "job-001") -> "JobHandle":
        from kbutillib.kb_app_runner.monitor import JobHandle
        return JobHandle(job_id=job_id, app_id="kb_fastqc/runFastQC", wsid=261700)

    def _make_check_jobs_response(self, job_id: str, status: str, error_msg: str = "") -> dict:
        """Build a fake check_jobs return value keyed by job_id."""
        from kbutillib.kb_job_utils.state import JobRecord, JobState

        record = JobRecord(job_id=job_id)
        record.state = JobState(status)
        record.ee2_raw = {"status": status, "job_id": job_id}
        if error_msg:
            record.error_message = error_msg
        return {job_id: record}

    def test_wait_all_completed(self):
        """AC#9: completed state maps to JobReport.state == 'completed'."""
        monitor, jutils, ee2 = self._make_monitor()
        handle = self._make_handle("job-ok-001")

        # First check_jobs returns completed
        jutils._ee2.check_jobs.return_value = {
            "job_states": [{"job_id": "job-ok-001", "status": "completed"}]
        }

        with patch.object(
            jutils,
            "check_jobs",
            return_value=self._make_check_jobs_response("job-ok-001", "completed"),
        ):
            reports = monitor.wait_all([handle])

        assert len(reports) == 1
        assert reports[0].state == "completed"
        assert reports[0].handle.job_id == "job-ok-001"

    def test_wait_all_error_state(self):
        """AC#9: error state maps to JobReport.state == 'error'."""
        monitor, jutils, ee2 = self._make_monitor()
        handle = self._make_handle("job-err-001")

        jutils._ee2.get_job_logs.return_value = {"lines": []}

        with patch.object(
            jutils,
            "check_jobs",
            return_value=self._make_check_jobs_response("job-err-001", "error", "OOM"),
        ):
            reports = monitor.wait_all([handle])

        assert reports[0].state == "error"
        assert "OOM" in (reports[0].error or "")

    def test_wait_all_terminated_state(self):
        """AC#9: terminated state maps to JobReport.state == 'error'."""
        monitor, jutils, ee2 = self._make_monitor()
        handle = self._make_handle("job-term-001")

        jutils._ee2.get_job_logs.return_value = {"lines": []}

        with patch.object(
            jutils,
            "check_jobs",
            return_value=self._make_check_jobs_response("job-term-001", "terminated"),
        ):
            reports = monitor.wait_all([handle])

        assert reports[0].state == "error"

    def test_wait_all_polls_until_terminal(self):
        """AC#9: non-terminal states keep polling; terminal state ends the loop."""
        monitor, jutils, ee2 = self._make_monitor()
        handle = self._make_handle("job-poll-001")

        from kbutillib.kb_job_utils.state import JobRecord, JobState

        running_rec = JobRecord(job_id="job-poll-001")
        running_rec.state = JobState.RUNNING
        running_rec.ee2_raw = {}

        done_rec = JobRecord(job_id="job-poll-001")
        done_rec.state = JobState.COMPLETED
        done_rec.ee2_raw = {"status": "completed"}

        call_count = 0

        def fake_check_jobs(job_ids):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return {"job-poll-001": running_rec}
            return {"job-poll-001": done_rec}

        with patch.object(jutils, "check_jobs", side_effect=fake_check_jobs):
            reports = monitor.wait_all([handle])

        assert reports[0].state == "completed"
        assert call_count == 3  # polled twice as running, once as completed

    def test_wait_all_calls_get_job_logs_on_error(self):
        """AC#10: get_job_logs called exactly once per errored handle."""
        monitor, jutils, ee2 = self._make_monitor()
        handle = self._make_handle("job-logtail-001")

        log_lines = [{"line": f"line {i}"} for i in range(60)]
        jutils._ee2.get_job_logs.return_value = {"lines": log_lines}

        with patch.object(
            jutils,
            "check_jobs",
            return_value=self._make_check_jobs_response("job-logtail-001", "error", "FAIL"),
        ):
            reports = monitor.wait_all([handle])

        # get_job_logs should have been called exactly once
        jutils._ee2.get_job_logs.assert_called_once()
        call_kwargs = jutils._ee2.get_job_logs.call_args[0][0]
        assert call_kwargs["job_id"] == "job-logtail-001"
        assert call_kwargs.get("latest") == 1

        # tail should be the last 50 lines (monitor default)
        assert len(reports[0].tail) == 50
        assert reports[0].tail[-1] == "line 59"

    def test_wait_all_no_get_job_logs_on_success(self):
        """AC#10: get_job_logs NOT called for completed jobs."""
        monitor, jutils, ee2 = self._make_monitor()
        handle = self._make_handle("job-nolog-001")

        with patch.object(
            jutils,
            "check_jobs",
            return_value=self._make_check_jobs_response("job-nolog-001", "completed"),
        ):
            monitor.wait_all([handle])

        jutils._ee2.get_job_logs.assert_not_called()

    def test_wait_all_uses_batch_check_jobs(self):
        """AC#11: wait_all uses check_jobs (batch), not per-job check_job."""
        monitor, jutils, ee2 = self._make_monitor()
        handles = [self._make_handle(f"job-batch-{i:03d}") for i in range(3)]

        from kbutillib.kb_job_utils.state import JobRecord, JobState

        def fake_check_jobs(job_ids):
            result = {}
            for jid in job_ids:
                rec = JobRecord(job_id=jid)
                rec.state = JobState.COMPLETED
                rec.ee2_raw = {}
                result[jid] = rec
            return result

        with patch.object(jutils, "check_jobs", side_effect=fake_check_jobs) as mock_cj:
            with patch.object(jutils, "check_job") as mock_single:
                monitor.wait_all(handles)

        # check_jobs (batch) should have been called
        assert mock_cj.call_count >= 1
        # check_job (single) should NOT have been called
        mock_single.assert_not_called()

    def test_wait_all_multiple_handles_ordering(self):
        """wait_all returns reports in the same order as the input handles."""
        monitor, jutils, ee2 = self._make_monitor()
        handles = [
            self._make_handle("job-ord-a"),
            self._make_handle("job-ord-b"),
            self._make_handle("job-ord-c"),
        ]

        from kbutillib.kb_job_utils.state import JobRecord, JobState

        def fake_check_jobs(job_ids):
            result = {}
            for jid in job_ids:
                rec = JobRecord(job_id=jid)
                rec.state = JobState.COMPLETED
                rec.ee2_raw = {}
                result[jid] = rec
            return result

        with patch.object(jutils, "check_jobs", side_effect=fake_check_jobs):
            reports = monitor.wait_all(handles)

        assert [r.handle.job_id for r in reports] == ["job-ord-a", "job-ord-b", "job-ord-c"]

    def test_wait_all_empty_handles(self):
        """wait_all([]) returns an empty list immediately."""
        monitor, jutils, ee2 = self._make_monitor()
        reports = monitor.wait_all([])
        assert reports == []


# ── check_jobs regression test (AC#12) ────────────────────────────────────────


class TestCheckJobsListShape:
    """Regression tests for the check_jobs list-vs-dict EE2 response shape.

    Ensures the fix from Phase 1 (list normalisation) is exercised through
    both the legacy dict shape and the current list shape.
    """

    def _make_jutils_with_ee2(self, ee2_mock):
        """Build KBJobUtils wired to a custom EE2 mock."""
        import tempfile

        from kbutillib.kb_job_utils.store import JobStore
        from kbutillib.kb_job_utils.utils import KBJobUtils
        from kbutillib.shared_env_utils import SharedEnvUtils

        env = SharedEnvUtils(config_file=False, token_file=None, kbase_token_file=None,
                             token="fake-token")
        tmp = tempfile.mktemp(suffix=".db")
        jutils = KBJobUtils.__new__(KBJobUtils)
        jutils._env = env
        jutils._kb_version = "prod"
        jutils._token = "fake-token"
        jutils._ee2 = ee2_mock
        jutils._store = JobStore(db_path=Path(tmp))
        return jutils

    def test_list_shape_returns_correct_mapping(self):
        """check_jobs with list-shape EE2 response returns dict keyed by job_id."""
        ee2 = MagicMock()
        ee2.check_jobs.return_value = {
            "job_states": [
                {"job_id": "job-a", "status": "completed"},
                {"job_id": "job-b", "status": "running"},
            ]
        }
        jutils = self._make_jutils_with_ee2(ee2)

        result = jutils.check_jobs(["job-a", "job-b"])

        assert set(result.keys()) == {"job-a", "job-b"}
        assert result["job-a"].state.value == "completed"
        assert result["job-b"].state.value == "running"

    def test_dict_shape_returns_correct_mapping(self):
        """check_jobs with legacy dict-shape EE2 response is handled identically."""
        ee2 = MagicMock()
        ee2.check_jobs.return_value = {
            "job_states": {
                "job-c": {"job_id": "job-c", "status": "error"},
                "job-d": {"job_id": "job-d", "status": "queued"},
            }
        }
        jutils = self._make_jutils_with_ee2(ee2)

        result = jutils.check_jobs(["job-c", "job-d"])

        assert set(result.keys()) == {"job-c", "job-d"}
        assert result["job-c"].state.value == "error"
        assert result["job-d"].state.value == "queued"

    def test_list_and_dict_shapes_produce_identical_keys(self):
        """Both shapes produce the same per-job mapping structure."""
        job_states_data = [
            {"job_id": "job-x", "status": "completed"},
        ]

        # List shape
        ee2_list = MagicMock()
        ee2_list.check_jobs.return_value = {"job_states": job_states_data}
        jutils_list = self._make_jutils_with_ee2(ee2_list)

        # Dict shape
        ee2_dict = MagicMock()
        ee2_dict.check_jobs.return_value = {
            "job_states": {"job-x": {"job_id": "job-x", "status": "completed"}}
        }
        jutils_dict = self._make_jutils_with_ee2(ee2_dict)

        result_list = jutils_list.check_jobs(["job-x"])
        result_dict = jutils_dict.check_jobs(["job-x"])

        assert set(result_list.keys()) == set(result_dict.keys())
        assert result_list["job-x"].state == result_dict["job-x"].state


# ── Fixture file presence tests ────────────────────────────────────────────────


class TestFixtures:
    """Verify that the required NMS fixture files are present."""

    def test_fastqc_fixture_exists(self):
        p = FIXTURES_DIR / "nms_runFastQC.json"
        assert p.exists(), f"Missing fixture: {p}"
        data = json.loads(p.read_text())
        assert data.get("info", {}).get("id") == "kb_fastqc/runFastQC"

    def test_sra_fixture_exists(self):
        p = FIXTURES_DIR / "nms_import_sra_as_reads_from_web.json"
        assert p.exists(), f"Missing fixture: {p}"
        data = json.loads(p.read_text())
        assert "kb_uploadmethods" in data.get("info", {}).get("id", "")

    def test_metaspades_fixture_exists(self):
        p = FIXTURES_DIR / "nms_run_metaSPAdes.json"
        assert p.exists(), f"Missing fixture: {p}"
        data = json.loads(p.read_text())
        assert "kb_SPAdes" in data.get("info", {}).get("id", "")

    def test_quast_fixture_exists(self):
        p = FIXTURES_DIR / "nms_run_QUAST_app.json"
        assert p.exists(), f"Missing fixture: {p}"
        data = json.loads(p.read_text())
        assert "kb_quast" in data.get("info", {}).get("id", "")
