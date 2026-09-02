"""Tests for KBDLServiceUtils — the KBDL job-service HTTP client.

Fully offline: every test injects a fake ``requests.Session``-shaped
transport (:class:`FakeSession`) and asserts on the requests it recorded
and the responses it was told to return. No test makes a real network
call or requires a running ``kbdl_service`` instance.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
import time

import pytest
import requests

from kbutillib.domains.external import kbdl_service_utils as kbdl_client_module
from kbutillib.domains.external.kbdl_service_utils import (
    KBDLAuthenticationError,
    KBDLConflictError,
    KBDLInvalidInputError,
    KBDLJobFailedError,
    KBDLNotFoundError,
    KBDLPayloadTooLargeError,
    KBDLServiceUtils,
    KBDLUnsupportedSchemaVersionError,
    KBDLUpstreamAuthUnavailableError,
)

FAKE_TOKEN = "tok-123"


# ---------------------------------------------------------------------------
# Fake HTTP transport
# ---------------------------------------------------------------------------


class FakeResponse:
    """A minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code: int, json_data=None, text: str | None = None):
        self.status_code = status_code
        self._json_data = json_data
        if text is not None:
            self.text = text
        elif json_data is not None:
            self.text = json.dumps(json_data)
        else:
            self.text = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self):
        if self._json_data is None:
            raise ValueError("no JSON body")
        return self._json_data

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class FakeSession:
    """A stand-in ``requests.Session`` that returns canned responses in order
    and records every call made through it."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError("FakeSession ran out of canned responses")
        return self._responses.pop(0)


def make_client(session, **kwargs):
    return KBDLServiceUtils(
        session=session,
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        token=FAKE_TOKEN,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Wire-contract-only isolation: must never import kbdl_service
# ---------------------------------------------------------------------------


def test_module_source_never_imports_kbdl_service():
    """Static check: no ``import kbdl_service`` / ``from kbdl_service import``
    anywhere in the module's source, at any scope (module or function body)."""
    source = inspect.getsource(kbdl_client_module)
    tree = ast.parse(source)
    offending = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "kbdl_service":
                    offending.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "kbdl_service":
                offending.append(node.module)
    assert not offending, f"kbdl_service_utils imports kbdl_service: {offending}"


def test_importing_module_does_not_import_kbdl_service():
    """Dynamic check: having imported ``kbdl_service_utils`` (at module
    collection time, above) never caused ``kbdl_service`` to appear in
    ``sys.modules``.

    Deliberately does NOT ``importlib.reload()`` the module here: reloading
    would redefine its classes in place, leaving this test file's
    already-imported error-class names (``KBDLConflictError`` etc., bound
    above) pointing at now-stale class objects that no longer match what
    the reloaded module's methods raise -- silently breaking every
    ``pytest.raises(...)`` check below for the rest of the session.
    """
    assert (
        kbdl_client_module.__name__ == "kbutillib.domains.external.kbdl_service_utils"
    )
    assert "kbdl_service" not in sys.modules
    assert not any(m.startswith("kbdl_service.") for m in sys.modules)


# ---------------------------------------------------------------------------
# Endpoint & auth configuration
# ---------------------------------------------------------------------------


def test_default_base_url_is_the_tunnelled_loopback_endpoint():
    client = KBDLServiceUtils(
        session=FakeSession([]),
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )
    assert client.base_url == "http://127.0.0.1:8791"


def test_base_url_overridable_by_environment_variable(monkeypatch):
    monkeypatch.setenv("KBDL_SERVICE_URL", "http://127.0.0.1:9999")
    client = KBDLServiceUtils(
        session=FakeSession([]),
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )
    assert client.base_url == "http://127.0.0.1:9999"


def test_explicit_base_url_kwarg_wins_over_environment_variable(monkeypatch):
    monkeypatch.setenv("KBDL_SERVICE_URL", "http://ignored:1")
    client = KBDLServiceUtils(
        base_url="http://explicit:2",
        session=FakeSession([]),
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )
    assert client.base_url == "http://explicit:2"


def test_authorization_header_sent_as_bearer_token():
    session = FakeSession([FakeResponse(200, [])])
    client = make_client(session)
    client.list_jobs()
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"


def test_constructor_accepts_no_username_parameter():
    """The service derives identity from the token alone (D5) -- a
    client-supplied username must not exist as a constructor knob."""
    sig = inspect.signature(KBDLServiceUtils.__init__)
    assert "username" not in sig.parameters


# ---------------------------------------------------------------------------
# Job submission -- one test per job type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method_name,job_type,params",
    [
        (
            "submit_genome_annotation",
            "KBDLGenomeAnnotation",
            {"fasta": ">contig\nACGT", "tools": ["rast"]},
        ),
        (
            "submit_model_reconstruction",
            "KBDLModelReconstruction",
            {
                "mapping": {"object_id": "map-1"},
                "annotations": {},
                "tools": ["ms"],
            },
        ),
        (
            "submit_fitness_model_analysis",
            "KBDLFitnessModelAnalysis",
            {"model": {}, "fitness": {}, "gene_reactions": {}},
        ),
        (
            "submit_skani",
            "KBDLSKANI",
            {"skani_db": {"object_id": "db-1"}, "fasta": "seq"},
        ),
    ],
)
def test_submit_each_job_type_issues_expected_envelope_and_returns_job_id(
    method_name, job_type, params
):
    session = FakeSession([FakeResponse(202, {"job_id": "job-xyz"})])
    client = make_client(session)

    job_id = getattr(client, method_name)(**params)

    assert job_id == "job-xyz"
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:8791/jobs"
    assert call["json"] == {
        "schema_version": "1",
        "job_type": job_type,
        "params": params,
    }


# ---------------------------------------------------------------------------
# Job lifecycle: list / check / result / clear
# ---------------------------------------------------------------------------


def test_list_jobs():
    session = FakeSession(
        [
            FakeResponse(
                200,
                [
                    {
                        "job_id": "j1",
                        "job_type": "KBDLSKANI",
                        "state": "queued",
                        "submitted_at": "2026-08-10T00:00:00Z",
                    }
                ],
            )
        ]
    )
    client = make_client(session)

    jobs = client.list_jobs()

    assert jobs == [
        {
            "job_id": "j1",
            "job_type": "KBDLSKANI",
            "state": "queued",
            "submitted_at": "2026-08-10T00:00:00Z",
        }
    ]
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == "http://127.0.0.1:8791/jobs"


def test_check_job():
    session = FakeSession([FakeResponse(200, {"job_id": "j1", "state": "running"})])
    client = make_client(session)

    status = client.check_job("j1")

    assert status == {"job_id": "j1", "state": "running"}
    assert session.calls[0]["url"] == "http://127.0.0.1:8791/jobs/j1"


def test_get_job_result():
    session = FakeSession([FakeResponse(200, {"gaa_data": {}})])
    client = make_client(session)

    result = client.get_job_result("j1")

    assert result == {"gaa_data": {}}
    assert session.calls[0]["url"] == "http://127.0.0.1:8791/jobs/j1/result"


def test_clear_job():
    session = FakeSession([FakeResponse(204, None)])
    client = make_client(session)

    client.clear_job("j1")

    assert session.calls[0]["method"] == "DELETE"
    assert session.calls[0]["url"] == "http://127.0.0.1:8791/jobs/j1"


# ---------------------------------------------------------------------------
# Object store: upload / list / metadata / archive files / delete
# ---------------------------------------------------------------------------


def test_upload_object_new_content_returns_job_id():
    session = FakeSession([FakeResponse(202, {"job_id": "up-1"})])
    client = make_client(session)

    result = client.upload_object(
        b"file bytes",
        object_type="GenomeArchive",
        name="archive1",
        visibility="private",
    )

    assert result == {"job_id": "up-1"}
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:8791/objects"
    assert "files" in call
    assert call["data"] == {
        "object_type": "GenomeArchive",
        "name": "archive1",
        "visibility": "private",
    }


def test_upload_object_already_registered_content_returns_object_id():
    """Content that already hashes to a registered object short-circuits
    to HTTP 201 + {"object_id": ...} instead of queuing a new job."""
    session = FakeSession([FakeResponse(201, {"object_id": "sha256-abc"})])
    client = make_client(session)

    result = client.upload_object(
        b"file bytes",
        object_type="GenomeArchive",
        name="archive1",
        visibility="private",
    )

    assert result == {"object_id": "sha256-abc"}


def test_list_objects():
    session = FakeSession([FakeResponse(200, [{"object_id": "o1"}])])
    client = make_client(session)

    objects = client.list_objects()

    assert objects == [{"object_id": "o1"}]
    assert session.calls[0]["url"] == "http://127.0.0.1:8791/objects"


def test_get_object_metadata():
    session = FakeSession(
        [FakeResponse(200, {"object_id": "o1", "object_type": "Media"})]
    )
    client = make_client(session)

    meta = client.get_object_metadata("o1")

    assert meta["object_id"] == "o1"
    assert session.calls[0]["url"] == "http://127.0.0.1:8791/objects/o1"


def test_list_archive_files():
    session = FakeSession(
        [FakeResponse(200, [{"name": "genome1.fna", "size_bytes": 123}])]
    )
    client = make_client(session)

    entries = client.list_archive_files("archive-1")

    assert entries == [{"name": "genome1.fna", "size_bytes": 123}]
    assert session.calls[0]["url"] == "http://127.0.0.1:8791/objects/archive-1/files"


def test_delete_object():
    session = FakeSession([FakeResponse(204, None)])
    client = make_client(session)

    client.delete_object("o1")

    assert session.calls[0]["method"] == "DELETE"
    assert session.calls[0]["url"] == "http://127.0.0.1:8791/objects/o1"


# ---------------------------------------------------------------------------
# poll_until_terminal — deterministic, no real sleeping
# ---------------------------------------------------------------------------


def test_poll_until_terminal_stops_at_completed_without_real_sleep():
    session = FakeSession(
        [
            FakeResponse(200, {"job_id": "j1", "state": "queued"}),
            FakeResponse(200, {"job_id": "j1", "state": "running"}),
            FakeResponse(200, {"job_id": "j1", "state": "completed"}),
        ]
    )
    client = make_client(session)
    sleep_calls = []

    status = client.poll_until_terminal(
        "j1",
        interval=5.0,
        sleep_fn=sleep_calls.append,
        time_fn=lambda: 0.0,
    )

    assert status["state"] == "completed"
    # Slept between polls, but never really -- sleep_fn just recorded calls.
    assert sleep_calls == [5.0, 5.0]
    assert len(session.calls) == 3


def test_poll_until_terminal_stops_at_failed():
    session = FakeSession(
        [
            FakeResponse(200, {"job_id": "j1", "state": "running"}),
            FakeResponse(
                200,
                {
                    "job_id": "j1",
                    "state": "failed",
                    "failure": {"code": "tool_failure", "message": "boom"},
                },
            ),
        ]
    )
    client = make_client(session)

    status = client.poll_until_terminal(
        "j1", sleep_fn=lambda _seconds: None, time_fn=lambda: 0.0
    )

    assert status["state"] == "failed"
    assert status["failure"] == {"code": "tool_failure", "message": "boom"}


def test_poll_until_terminal_never_calls_real_time_sleep(monkeypatch):
    """Guard against a regression where the injected sleep_fn is ignored and
    the real ``time.sleep`` is used instead."""

    def _fail_if_called(_seconds):
        raise AssertionError("poll_until_terminal must not really sleep in tests")

    monkeypatch.setattr(time, "sleep", _fail_if_called)
    session = FakeSession(
        [
            FakeResponse(200, {"state": "queued"}),
            FakeResponse(200, {"state": "completed"}),
        ]
    )
    client = make_client(session)

    status = client.poll_until_terminal(
        "j1", interval=100.0, sleep_fn=lambda _seconds: None, time_fn=lambda: 0.0
    )

    assert status["state"] == "completed"


# ---------------------------------------------------------------------------
# submit_and_wait convenience wrapper
# ---------------------------------------------------------------------------


def test_submit_and_wait_returns_result_on_completion():
    session = FakeSession(
        [
            FakeResponse(202, {"job_id": "j1"}),
            FakeResponse(200, {"state": "completed"}),
            FakeResponse(200, {"results": {"q1": []}}),
        ]
    )
    client = make_client(session)

    result = client.submit_and_wait(
        "KBDLSKANI",
        {"skani_db": {"object_id": "db-1"}, "fasta": "seq"},
        sleep_fn=lambda _seconds: None,
        time_fn=lambda: 0.0,
    )

    assert result == {"results": {"q1": []}}


def test_submit_and_wait_raises_typed_error_on_failure():
    session = FakeSession(
        [
            FakeResponse(202, {"job_id": "j1"}),
            FakeResponse(
                200,
                {
                    "state": "failed",
                    "failure": {"code": "tool_failure", "message": "boom"},
                },
            ),
        ]
    )
    client = make_client(session)

    with pytest.raises(KBDLJobFailedError) as excinfo:
        client.submit_and_wait(
            "KBDLSKANI",
            {"skani_db": {"object_id": "db-1"}, "fasta": "seq"},
            sleep_fn=lambda _seconds: None,
            time_fn=lambda: 0.0,
        )

    assert excinfo.value.job_id == "j1"
    assert excinfo.value.failure == {"code": "tool_failure", "message": "boom"}


# ---------------------------------------------------------------------------
# Typed, distinguishable HTTP errors
# ---------------------------------------------------------------------------


def test_400_with_supported_versions_body_surfaces_typed_error():
    session = FakeSession(
        [FakeResponse(400, {"detail": {"supported_versions": ["1"]}})]
    )
    client = make_client(session)

    with pytest.raises(KBDLUnsupportedSchemaVersionError) as excinfo:
        client.submit_skani(skani_db={"object_id": "db-1"}, fasta="seq")

    assert excinfo.value.supported_versions == ["1"]


def test_400_with_code_message_body_surfaces_typed_error():
    session = FakeSession(
        [
            FakeResponse(
                400, {"detail": {"code": "invalid_input", "message": "bad params"}}
            )
        ]
    )
    client = make_client(session)

    with pytest.raises(KBDLInvalidInputError) as excinfo:
        client.submit_skani(skani_db={"object_id": "db-1"}, fasta="seq")

    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.message == "bad params"


def test_401_and_503_are_distinguishable_typed_errors():
    """401 (token rejected) and 503 (auth service unreachable) must never
    be conflated -- see kbdl_service.identity."""
    session_401 = FakeSession(
        [
            FakeResponse(
                401, {"detail": {"error": "invalid_token", "message": "bad token"}}
            )
        ]
    )
    with pytest.raises(KBDLAuthenticationError):
        make_client(session_401).list_jobs()

    session_503 = FakeSession(
        [
            FakeResponse(
                503,
                {"detail": {"error": "upstream_unavailable", "message": "auth down"}},
            )
        ]
    )
    with pytest.raises(KBDLUpstreamAuthUnavailableError):
        make_client(session_503).list_jobs()

    assert not issubclass(KBDLAuthenticationError, KBDLUpstreamAuthUnavailableError)
    assert not issubclass(KBDLUpstreamAuthUnavailableError, KBDLAuthenticationError)


def test_404_surfaces_typed_error():
    session = FakeSession([FakeResponse(404, {"detail": "job not found"})])
    client = make_client(session)

    with pytest.raises(KBDLNotFoundError):
        client.check_job("nope")


def test_409_surfaces_typed_error_when_result_requested_before_completion():
    session = FakeSession(
        [FakeResponse(409, {"detail": "job j1 is not completed (state='running')"})]
    )
    client = make_client(session)

    with pytest.raises(KBDLConflictError):
        client.get_job_result("j1")


def test_413_surfaces_typed_error_on_oversized_upload():
    session = FakeSession(
        [
            FakeResponse(
                413, {"detail": "upload exceeds the maximum object size of 100 bytes"}
            )
        ]
    )
    client = make_client(session)

    with pytest.raises(KBDLPayloadTooLargeError):
        client.upload_object(
            b"x" * 200, object_type="GenomeArchive", name="big", visibility="private"
        )


# ---------------------------------------------------------------------------
# toolkit.py registration
# ---------------------------------------------------------------------------


def test_toolkit_registers_kbdl_service_alongside_its_siblings():
    from kbutillib import KBUtilLib
    from kbutillib.domains.external.kbdl_service_utils import KBDLServiceUtilsImpl

    kbu = KBUtilLib()

    assert isinstance(kbu.kbdl_service, KBDLServiceUtilsImpl)
    # Lazy singleton, same as every other domain property on the facade.
    assert kbu.kbdl_service is kbu.kbdl_service
