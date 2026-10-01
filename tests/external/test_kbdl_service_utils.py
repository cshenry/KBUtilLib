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
    KBDLNotAuthorizedError,
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


#: Endpoint the offline tests point the client at. KBDL no longer has a
#: built-in default (the loopback/tunnel posture was retired), so every
#: constructed client must be given one explicitly.
TEST_BASE_URL = "http://127.0.0.1:8791"


def make_client(session, **kwargs):
    kwargs.setdefault("base_url", TEST_BASE_URL)
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


def test_zero_config_resolves_to_the_deployed_poplar_endpoint(monkeypatch):
    """With nothing configured the client must point at the DEPLOYED service.

    Changed 2026-10-01. This previously asserted a ``ValueError``, on the
    reasoning that no default beat a dead loopback default. That was right
    about loopback and wrong about having no default: the zero-config path is
    the one agents and notebooks actually take, and telling them to "set
    ``KBDL_SERVICE_URL``" is what sent them hunting for the retired tunnel
    address. The deployed endpoint is directly reachable from any
    ANL-networked host, so zero-config now succeeds.
    """
    monkeypatch.delenv("KBDL_SERVICE_URL", raising=False)
    client = KBDLServiceUtils(
        session=FakeSession([]),
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )
    assert client.base_url == "http://poplar.cels.anl.gov:8791"


def test_default_base_url_is_the_deployed_endpoint_not_loopback():
    """The default must be the real service, and must never be loopback."""
    assert kbdl_client_module.DEFAULT_BASE_URL == "http://poplar.cels.anl.gov:8791"
    assert "127.0.0.1" not in kbdl_client_module.DEFAULT_BASE_URL
    assert "localhost" not in kbdl_client_module.DEFAULT_BASE_URL


def test_base_url_from_environment_variable(monkeypatch):
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
            {"genome_ref": {"object_id": "genome-1"}, "tools": ["rast"]},
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
        (
            "submit_horizyn",
            "KBDLHorizyn",
            {"genome": {"object_id": "genome-1"}},
        ),
        (
            "submit_checkm2",
            "KBDLCheckM2",
            {"checkm2_db": {"object_id": "db-1"}, "fasta": "seq"},
        ),
        (
            "submit_build_genome",
            "KBDLBuildGenome",
            {"skani_db": {"object_id": "db-1"}, "fasta": "seq"},
        ),
        (
            "submit_build_genome",
            "KBDLBuildGenome",
            {"skani_db": {"object_id": "db-1"}, "genbank": "gb-text", "fasta": "seq"},
        ),
        (
            "submit_build_skani_db",
            "KBDLBuildSKANIDB",
            {
                "sources": [{"object_id": "genome-1"}, {"object_id": "genome-2"}],
                "name": "my-skani-db",
                "visibility": "private",
            },
        ),
        (
            "submit_store_load",
            "KBDLStoreLoad",
            {"source_job_ids": ["job-1", "job-2"]},
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


def test_job_type_horizyn_constant():
    assert kbdl_client_module.JOB_TYPE_HORIZYN == "KBDLHorizyn"


def test_submit_horizyn_issues_expected_envelope_and_returns_job_id():
    session = FakeSession([FakeResponse(202, {"job_id": "job-xyz"})])
    client = make_client(session)

    params = {"genome": {"object_id": "genome-1"}}
    job_id = client.submit_horizyn(**params)

    assert job_id == "job-xyz"
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:8791/jobs"
    assert call["json"] == {
        "schema_version": "1",
        "job_type": "KBDLHorizyn",
        "params": params,
    }


# ---------------------------------------------------------------------------
# submit_build_genome / submit_genome_annotation -- client-side validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        # no genome source at all
        {"skani_db": {"object_id": "db-1"}},
        # two mutually exclusive sources
        {"skani_db": {"object_id": "db-1"}, "fasta": "seq", "archive": "arch"},
        {"skani_db": {"object_id": "db-1"}, "genbank": "gb", "archive": "arch"},
        {
            "skani_db": {"object_id": "db-1"},
            "fasta": "seq",
            "genbank": "gb",
            "archive": "arch",
        },
        # gff without fasta
        {"skani_db": {"object_id": "db-1"}, "genbank": "gb", "gff": "annot.gff"},
    ],
)
def test_submit_build_genome_rejects_invalid_source_combination_without_a_request(
    kwargs,
):
    session = FakeSession([])
    client = make_client(session)

    with pytest.raises(ValueError):
        client.submit_build_genome(**kwargs)

    assert session.calls == []


def test_submit_build_genome_rejects_missing_skani_db_without_a_request():
    session = FakeSession([])
    client = make_client(session)

    with pytest.raises(ValueError):
        client.submit_build_genome(skani_db=None, fasta="seq")

    assert session.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tools": ["rast"]},
        {
            "tools": ["rast"],
            "genome": {"features": []},
            "genome_ref": {"object_id": "genome-1"},
        },
    ],
)
def test_submit_genome_annotation_rejects_invalid_genome_source_without_a_request(
    kwargs,
):
    session = FakeSession([])
    client = make_client(session)

    with pytest.raises(ValueError):
        client.submit_genome_annotation(**kwargs)

    assert session.calls == []


def test_submit_genome_annotation_no_longer_accepts_legacy_parameters():
    """The narrowed contract removed fasta/gff/genbank/features/
    kbase_genome_id -- passing any of them must be a TypeError (unknown
    keyword), not a silently-forwarded param."""
    sig = inspect.signature(KBDLServiceUtils.submit_genome_annotation)
    for legacy_name in ("fasta", "gff", "genbank", "features", "kbase_genome_id"):
        assert legacy_name not in sig.parameters

    session = FakeSession([])
    client = make_client(session)
    for legacy_name in ("fasta", "gff", "genbank", "features", "kbase_genome_id"):
        with pytest.raises(TypeError):
            client.submit_genome_annotation(
                tools=["rast"], genome_ref={"object_id": "g1"}, **{legacy_name: "x"}
            )
    assert session.calls == []


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


def test_upload_object_default_transform_is_byte_identical_to_pre_transform_behavior():
    """Regression guard: the default transform="none" must leave the
    outgoing request completely unchanged from before ``transform``
    existed, so every existing caller is unaffected. No "transform" key
    should appear in the form data at all -- not even set to "none"."""
    session = FakeSession([FakeResponse(202, {"job_id": "up-1"})])
    client = make_client(session)

    client.upload_object(
        b"file bytes",
        object_type="GenomeArchive",
        name="archive1",
        visibility="private",
    )

    call = session.calls[0]
    assert call["data"] == {
        "object_type": "GenomeArchive",
        "name": "archive1",
        "visibility": "private",
    }
    assert "transform" not in call["data"]


def test_upload_object_includes_transform_in_form_data_when_passed():
    session = FakeSession([FakeResponse(202, {"job_id": "up-1"})])
    client = make_client(session)

    client.upload_object(
        b"file bytes",
        object_type="GenomeArchive",
        name="archive1",
        visibility="private",
        transform="some-transform",
    )

    call = session.calls[0]
    assert call["data"] == {
        "object_type": "GenomeArchive",
        "name": "archive1",
        "visibility": "private",
        "transform": "some-transform",
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


def test_submit_and_wait_works_with_store_load_job_type():
    """submit_store_load's job type flows through the same generic
    submit_and_wait/poll_until_terminal helpers as every other job type --
    no special-casing needed."""
    session = FakeSession(
        [
            FakeResponse(202, {"job_id": "j1"}),
            FakeResponse(200, {"state": "completed"}),
            FakeResponse(200, {"loaded": ["job-1", "job-2"]}),
        ]
    )
    client = make_client(session)

    result = client.submit_and_wait(
        "KBDLStoreLoad",
        {"source_job_ids": ["job-1", "job-2"]},
        sleep_fn=lambda _seconds: None,
        time_fn=lambda: 0.0,
    )

    assert result == {"loaded": ["job-1", "job-2"]}
    assert session.calls[0]["json"] == {
        "schema_version": "1",
        "job_type": "KBDLStoreLoad",
        "params": {"source_job_ids": ["job-1", "job-2"]},
    }


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


def test_401_and_403_are_distinguishable_typed_errors():
    """401 (invalid token) and 403 (valid token, missing permission) must
    never be conflated -- otherwise an operator would chase fresh tokens
    forever for what is actually a missing permission."""
    session_401 = FakeSession(
        [
            FakeResponse(
                401, {"detail": {"error": "invalid_token", "message": "bad token"}}
            )
        ]
    )
    with pytest.raises(KBDLAuthenticationError):
        make_client(session_401).list_jobs()

    session_403 = FakeSession(
        [FakeResponse(403, {"detail": "not permitted to submit KBDLStoreLoad jobs"})]
    )
    with pytest.raises(KBDLNotAuthorizedError):
        make_client(session_403).list_jobs()

    assert KBDLNotAuthorizedError is not KBDLAuthenticationError
    assert not issubclass(KBDLAuthenticationError, KBDLNotAuthorizedError)
    assert not issubclass(KBDLNotAuthorizedError, KBDLAuthenticationError)
    assert issubclass(KBDLNotAuthorizedError, kbdl_client_module.KBDLServiceError)


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


# ---------------------------------------------------------------------------
# Transient connection failures while polling — a lost connection is not a
# lost job
# ---------------------------------------------------------------------------


class RaisingSession(FakeSession):
    """A :class:`FakeSession` whose canned entries may be EXCEPTIONS.

    An entry that is an exception instance is raised instead of returned, which
    is how a transport-level failure (a reset keep-alive connection) is
    reproduced without a network.
    """

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError("RaisingSession ran out of canned entries")
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def test_poll_survives_a_transient_connection_reset_and_still_returns_the_result():
    """The regression this guards is a real incident, 2026-10-01.

    Ten KBDLHorizyn jobs were running normally and the API was serving 200 OK
    when ``poll_until_terminal`` raised ``ConnectionResetError(104)`` and the
    caller abandoned the wait. The job state lives on the service; an
    interrupted GET carries no information about it, so the only correct
    response is to poll again.
    """
    session = RaisingSession(
        [
            FakeResponse(200, {"job_id": "j1", "state": "running"}),
            requests.exceptions.ConnectionError(
                "('Connection aborted.', ConnectionResetError(104, "
                "'Connection reset by peer'))"
            ),
            FakeResponse(200, {"job_id": "j1", "state": "completed"}),
        ]
    )
    client = make_client(session)
    sleep_calls = []

    status = client.poll_until_terminal(
        "j1", interval=1.0, sleep_fn=sleep_calls.append, time_fn=lambda: 0.0
    )

    assert status["state"] == "completed"
    # Three transport calls: the reset one was retried, not fatal.
    assert len(session.calls) == 3
    assert sleep_calls == [1.0, 1.0]


def test_poll_survives_a_transient_timeout_too():
    session = RaisingSession(
        [
            requests.exceptions.Timeout("read timed out"),
            FakeResponse(200, {"job_id": "j1", "state": "completed"}),
        ]
    )
    client = make_client(session)

    status = client.poll_until_terminal(
        "j1", interval=0.0, sleep_fn=lambda _: None, time_fn=lambda: 0.0
    )

    assert status["state"] == "completed"


def test_poll_gives_up_after_too_many_CONSECUTIVE_connection_errors():
    """Tolerance is bounded, so a genuinely unreachable service still surfaces."""
    n = kbdl_client_module._POLL_MAX_CONNECTION_ERRORS + 1
    session = RaisingSession(
        [requests.exceptions.ConnectionError("down") for _ in range(n)]
    )
    client = make_client(session)

    with pytest.raises(requests.exceptions.ConnectionError):
        client.poll_until_terminal(
            "j1", interval=0.0, sleep_fn=lambda _: None, time_fn=lambda: 0.0
        )


def test_a_successful_poll_resets_the_connection_error_streak():
    """The bound is on CONSECUTIVE failures, not on failures in total.

    A job that runs for hours may accumulate more than the bound in total
    while never failing twice in a row; that must not end the wait.
    """
    bound = kbdl_client_module._POLL_MAX_CONNECTION_ERRORS
    entries = []
    for _ in range(bound + 2):
        entries.append(requests.exceptions.ConnectionError("blip"))
        entries.append(FakeResponse(200, {"job_id": "j1", "state": "running"}))
    entries.append(FakeResponse(200, {"job_id": "j1", "state": "completed"}))
    client = make_client(RaisingSession(entries))

    status = client.poll_until_terminal(
        "j1", interval=0.0, sleep_fn=lambda _: None, time_fn=lambda: 0.0
    )

    assert status["state"] == "completed"


def test_poll_still_honours_its_timeout_budget_while_tolerating_errors():
    """Tolerating resets must not let a wait outlive its timeout."""
    session = RaisingSession(
        [requests.exceptions.ConnectionError("blip") for _ in range(3)]
    )
    client = make_client(session)
    clock = iter([0.0, 100.0, 200.0, 300.0])

    with pytest.raises(TimeoutError):
        client.poll_until_terminal(
            "j1",
            interval=0.0,
            timeout=50.0,
            sleep_fn=lambda _: None,
            time_fn=lambda: next(clock),
        )


# ---------------------------------------------------------------------------
# The default session retries connections — but never a POST
# ---------------------------------------------------------------------------


def test_default_session_mounts_a_connection_retry_on_both_schemes():
    session = KBDLServiceUtils._build_retrying_session()
    for scheme in ("http://", "https://"):
        adapter = session.get_adapter(scheme)
        retry = adapter.max_retries
        assert retry.connect >= 1, f"{scheme} adapter does not retry connections"
        assert retry.read >= 1


def test_default_session_does_NOT_retry_post():
    """A retried submission would create a SECOND job.

    ``_submit`` is a POST, so POST must stay out of the retried set. This is
    asserted rather than left to urllib3's default because the default is the
    only thing standing between a flaky network and duplicate jobs.
    """
    session = KBDLServiceUtils._build_retrying_session()
    retry = session.get_adapter("http://").max_retries
    allowed = {m.upper() for m in retry.allowed_methods}
    assert "POST" not in allowed
    assert "GET" in allowed


def test_default_session_does_not_retry_http_status_codes():
    """A 4xx/5xx is mapped to a typed exception and is meaningful.

    Retrying statuses would swallow and repeat it, so ``status`` is 0 and
    ``raise_on_status`` is off -- ``_raise_for_status`` owns that mapping.
    """
    retry = KBDLServiceUtils._build_retrying_session().get_adapter(
        "http://"
    ).max_retries
    assert not retry.status
    assert retry.raise_on_status is False


def test_an_injected_session_is_used_verbatim_and_not_wrapped():
    """Tests and callers that inject a transport must keep getting theirs."""
    session = FakeSession([])
    client = make_client(session)
    assert client.session is session
