"""Regression tests for KBJobUtils.check_jobs list-vs-dict normalisation.

EE2's ``check_jobs`` response has historically returned ``job_states`` as a
``dict`` keyed by job_id (legacy shape) and currently returns it as a
``list[dict]`` where each item has a ``job_id`` key (current shape).

These tests verify that both shapes produce an identical ``Dict[str, JobRecord]``
result, and that the list shape does NOT raise ``'list' object has no attribute
'items'`` (the defect described in PRD wetlands-pipeline-metaspades, Defect 2).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.kb_job_utils.state import JobRecord, JobState
from kbutillib.kb_job_utils.utils import KBJobUtils


# ── fixture helpers ──────────────────────────────────────────────────────────

JOB_STATES_DICT = {
    "job-aaa": {"job_id": "job-aaa", "status": "completed"},
    "job-bbb": {"job_id": "job-bbb", "status": "running"},
    "job-ccc": {"job_id": "job-ccc", "status": "error", "errormsg": "OOM"},
}

JOB_STATES_LIST = list(JOB_STATES_LIST_ITEMS := [
    {"job_id": "job-aaa", "status": "completed"},
    {"job_id": "job-bbb", "status": "running"},
    {"job_id": "job-ccc", "status": "error", "errormsg": "OOM"},
])


def _make_utils(tmp_path: Path, ee2_response: dict) -> KBJobUtils:
    """Build a KBJobUtils instance whose EE2 client returns *ee2_response*."""
    mock_env = MagicMock()
    mock_env.get_token.return_value = "fake-token"

    with patch("kbutillib.kb_job_utils.utils.execution_engine2") as mock_cls:
        mock_ee2 = MagicMock()
        mock_cls.return_value = mock_ee2
        mock_ee2.check_jobs.return_value = ee2_response

        utils = KBJobUtils(env=mock_env, kb_version="ci", db_path=tmp_path / "jobs.db")
        utils._mock_ee2 = mock_ee2
        return utils


# ── tests ────────────────────────────────────────────────────────────────────


class TestCheckJobsListVsDict:
    """check_jobs must handle both EE2 response shapes identically."""

    def test_dict_shape_returns_keyed_mapping(self, tmp_path):
        """Legacy dict shape: job_states is already keyed by job_id."""
        utils = _make_utils(
            tmp_path,
            {"job_states": JOB_STATES_DICT},
        )
        results = utils.check_jobs(["job-aaa", "job-bbb", "job-ccc"])

        assert set(results.keys()) == {"job-aaa", "job-bbb", "job-ccc"}
        assert results["job-aaa"].state == JobState.COMPLETED
        assert results["job-bbb"].state == JobState.RUNNING
        assert results["job-ccc"].state == JobState.ERROR
        assert results["job-ccc"].error_message == "OOM"

        utils.close()

    def test_list_shape_returns_keyed_mapping(self, tmp_path):
        """Current EE2 list shape: job_states is a list[dict] with job_id keys."""
        utils = _make_utils(
            tmp_path,
            {"job_states": JOB_STATES_LIST},
        )
        results = utils.check_jobs(["job-aaa", "job-bbb", "job-ccc"])

        assert set(results.keys()) == {"job-aaa", "job-bbb", "job-ccc"}
        assert results["job-aaa"].state == JobState.COMPLETED
        assert results["job-bbb"].state == JobState.RUNNING
        assert results["job-ccc"].state == JobState.ERROR
        assert results["job-ccc"].error_message == "OOM"

        utils.close()

    def test_list_shape_does_not_raise_attribute_error(self, tmp_path):
        """The list-shape must NOT raise 'list object has no attribute items'."""
        utils = _make_utils(
            tmp_path,
            {"job_states": JOB_STATES_LIST},
        )
        try:
            utils.check_jobs(["job-aaa", "job-bbb", "job-ccc"])
        except AttributeError as exc:
            pytest.fail(
                f"check_jobs raised AttributeError on list-shape response: {exc}"
            )
        finally:
            utils.close()

    def test_both_shapes_produce_identical_results(self, tmp_path):
        """Dict and list shapes must yield identical job-id → state mappings."""
        tmp_dict = tmp_path / "dict"
        tmp_dict.mkdir()
        tmp_list = tmp_path / "list"
        tmp_list.mkdir()

        utils_dict = _make_utils(tmp_dict, {"job_states": JOB_STATES_DICT})
        utils_list = _make_utils(tmp_list, {"job_states": JOB_STATES_LIST})

        job_ids = ["job-aaa", "job-bbb", "job-ccc"]
        results_dict = utils_dict.check_jobs(job_ids)
        results_list = utils_list.check_jobs(job_ids)

        assert set(results_dict.keys()) == set(results_list.keys()), (
            "Key sets differ between dict and list shapes"
        )
        for jid in job_ids:
            assert results_dict[jid].state == results_list[jid].state, (
                f"State mismatch for {jid}: "
                f"{results_dict[jid].state} vs {results_list[jid].state}"
            )
            assert results_dict[jid].error_message == results_list[jid].error_message, (
                f"error_message mismatch for {jid}"
            )

        utils_dict.close()
        utils_list.close()

    def test_all_jobs_persisted_to_store_list_shape(self, tmp_path):
        """All jobs from list-shape response must be persisted to the local store."""
        utils = _make_utils(
            tmp_path,
            {"job_states": JOB_STATES_LIST},
        )
        utils.check_jobs(["job-aaa", "job-bbb", "job-ccc"])

        for jid in ("job-aaa", "job-bbb", "job-ccc"):
            stored = utils.store.get(jid)
            assert stored is not None, f"{jid} was not persisted to store"

        utils.close()

    def test_empty_list_shape_returns_empty_dict(self, tmp_path):
        """An empty list in job_states should return an empty result dict."""
        utils = _make_utils(tmp_path, {"job_states": []})
        results = utils.check_jobs([])
        assert results == {}
        utils.close()

    def test_empty_dict_shape_returns_empty_dict(self, tmp_path):
        """An empty dict in job_states should return an empty result dict."""
        utils = _make_utils(tmp_path, {"job_states": {}})
        results = utils.check_jobs([])
        assert results == {}
        utils.close()
