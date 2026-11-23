"""``KBDLServiceUtils``' seven dataset-mirror methods, fully offline.

Every test here drives the REAL client with only its ``requests.Session``
replaced (:class:`FakeSession`), so the paths, the request bodies and the
status-code mapping are the ones the deployed service will see. Nothing
reaches a network and no KBDL server is needed -- the dataset routes are
being built in parallel and are not deployed.

The two that carry the most weight:

* ``test_409_carrying_a_tombstone_is_an_error_and_keeps_the_tombstone`` -- a
  409 on a version POST normally means "already published, carry on", but one
  whose body holds a tombstone means the manifest object id belongs to a
  RETIRED version and is permanently spent. Read as success, a withdrawn
  snapshot would be reported live.
* ``test_a_real_client_over_a_fake_session_satisfies_KBDLPort`` -- builds a
  :class:`DatasetPublisher` with a real :class:`KBDLServiceUtils` as its
  ``KBDLPort`` and runs ``status()`` all the way through it. ``KBDLPort`` is
  a Protocol neither module imports from the other, so this is the only place
  the two halves are proven to fit.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("requests")

import requests  # noqa: E402

from kbutillib.domains.external.kbdl_service_utils import (  # noqa: E402
    KBDLBadRequestError,
    KBDLConflictError,
    KBDLGoneError,
    KBDLNotAuthorizedError,
    KBDLNotFoundError,
    KBDLServiceError,
    KBDLServiceUtils,
)

FAKE_TOKEN = "tok-dataset"
TEST_BASE_URL = "http://127.0.0.1:8791"
DATASET = "genome_clearhouse.genome_quality"


# ---------------------------------------------------------------------------
# Fake HTTP transport (the ONLY thing faked in this file)
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
    """Returns canned responses in order and records every call made."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError("FakeSession ran out of canned responses")
        return self._responses.pop(0)


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


def record(version=3, state="PUBLISHED", **overrides):
    """A version record of the documented shape."""
    out = {
        "name": DATASET,
        "version": version,
        "state": state,
        "manifest_object_id": "a" * 64,
        "source_snapshot_id": "9007199254740994",
        "row_count": 7,
        "total_bytes": 4096,
        "published_at": "2026-11-20T10:00:00Z",
        "published_by": "chenry",
        "hold_reason": None,
        "retired_at": None,
        "retire_reason": None,
    }
    out.update(overrides)
    return out


# ---------------------------------------------------------------------------
# GET /datasets
# ---------------------------------------------------------------------------


def test_list_datasets_gets_the_collection_route():
    row = {
        "name": DATASET,
        "source": "kbaseincubator.genome_clearhouse.genome_quality",
        "latest_version": 3,
        "latest_published_at": "2026-11-20T10:00:00Z",
        "version_count": 3,
    }
    session = FakeSession([FakeResponse(200, [row])])
    assert make_client(session).list_datasets() == [row]
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == f"{TEST_BASE_URL}/datasets"


def test_the_token_is_sent_on_every_dataset_call():
    session = FakeSession([FakeResponse(200, [])])
    make_client(session).list_datasets()
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"


# ---------------------------------------------------------------------------
# GET /datasets/{name}/latest and /versions/{version}
# ---------------------------------------------------------------------------


def test_get_dataset_version_defaults_to_the_latest_route():
    body = {"version": record(), "manifest": {"manifest_schema": 1}}
    session = FakeSession([FakeResponse(200, body)])
    assert make_client(session).get_dataset_version(DATASET) == body
    assert session.calls[0]["url"] == f"{TEST_BASE_URL}/datasets/{DATASET}/latest"


def test_get_dataset_version_numbered_uses_the_versions_route():
    body = {"version": record(version=2), "manifest": {}}
    session = FakeSession([FakeResponse(200, body)])
    make_client(session).get_dataset_version(DATASET, 2)
    assert (
        session.calls[0]["url"] == f"{TEST_BASE_URL}/datasets/{DATASET}/versions/2"
    )


def test_get_dataset_version_410_raises_KBDLGoneError_with_the_tombstone():
    """A RETIRED version is 410, and the tombstone must survive the mapping.

    The 410 body is the same shape as a 200's, so the whole body is kept --
    not ``_error_detail``'s ``detail`` projection -- and the tombstone within
    it is lifted onto the exception. A puller has to be able to print who
    withdrew the version and why.
    """
    tombstone = {
        "retired_at": "2026-11-22T09:00:00Z",
        "retire_reason": "superseded by version 4",
    }
    body = {
        "version": record(version=2, state="RETIRED", **tombstone),
        "manifest": {"manifest_schema": 1},
        "tombstone": tombstone,
    }
    session = FakeSession([FakeResponse(410, body)])
    with pytest.raises(KBDLGoneError) as caught:
        make_client(session).get_dataset_version(DATASET, 2)
    assert caught.value.body == body
    assert caught.value.tombstone == tombstone
    assert caught.value.status_code == 410


def test_KBDLGoneError_is_not_a_not_found_error():
    """410 and 404 are DIFFERENT answers and must not share a type.

    "withdrawn" and "never existed" lead to different operator actions, and
    only the first has a tombstone; a puller that caught KBDLNotFoundError
    for both would report a retired version as nonexistent.
    """
    assert issubclass(KBDLGoneError, KBDLServiceError)
    assert not issubclass(KBDLGoneError, KBDLNotFoundError)


def test_get_dataset_version_410_without_json_keeps_the_text():
    session = FakeSession([FakeResponse(410, None, text="gone")])
    with pytest.raises(KBDLGoneError) as caught:
        make_client(session).get_dataset_version(DATASET)
    assert caught.value.body == "gone"
    assert caught.value.tombstone is None


def test_get_dataset_version_404_is_not_found():
    session = FakeSession([FakeResponse(404, {"detail": "no such dataset"})])
    with pytest.raises(KBDLNotFoundError):
        make_client(session).get_dataset_version("no.such")


# ---------------------------------------------------------------------------
# GET /datasets/{name}/versions
# ---------------------------------------------------------------------------


def test_list_dataset_versions_returns_the_service_order_untouched():
    """Newest first, retired included -- and the client reorders NOTHING."""
    rows = [
        record(version=3),
        record(version=2, state="RETIRED"),
        record(version=1),
    ]
    session = FakeSession([FakeResponse(200, rows)])
    assert make_client(session).list_dataset_versions(DATASET) == rows
    assert (
        session.calls[0]["url"] == f"{TEST_BASE_URL}/datasets/{DATASET}/versions"
    )


# ---------------------------------------------------------------------------
# POST /datasets/{name}/versions  (registration)
# ---------------------------------------------------------------------------


def test_publish_dataset_version_posts_the_manifest_object_id():
    session = FakeSession([FakeResponse(201, record(version=4))])
    out = make_client(session).publish_dataset_version(DATASET, "b" * 64)
    assert out["version"] == 4
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{TEST_BASE_URL}/datasets/{DATASET}/versions"
    assert call["json"] == {"manifest_object_id": "b" * 64}


def test_publish_dataset_version_200_is_the_idempotent_repeat():
    """A 200 means this manifest is ALREADY a published version.

    That is success, not an error: it is what makes re-running an interrupted
    publish safe.
    """
    session = FakeSession([FakeResponse(200, record(version=3))])
    assert make_client(session).publish_dataset_version(DATASET, "a" * 64)[
        "version"
    ] == 3


def test_409_carrying_a_tombstone_is_an_error_and_keeps_the_tombstone():
    """A 409 whose body holds a tombstone NEVER surfaces as success.

    It means the posted manifest object id belonged to a version that has
    since been RETIRED: the id is permanently spent, so the retry that is
    normally right against a 409 can never succeed. Reported as an idempotent
    repeat, the operator would believe a withdrawn snapshot is live.
    """
    tombstone = {"retired_at": "2026-11-22T09:00:00Z", "retire_reason": "bad export"}
    session = FakeSession([FakeResponse(409, {"detail": {"tombstone": tombstone}})])
    with pytest.raises(KBDLConflictError) as caught:
        make_client(session).publish_dataset_version(DATASET, "a" * 64)
    assert caught.value.tombstone == tombstone
    assert caught.value.status_code == 409


def test_a_plain_409_has_no_tombstone():
    session = FakeSession([FakeResponse(409, {"detail": "version is not ready"})])
    with pytest.raises(KBDLConflictError) as caught:
        make_client(session).publish_dataset_version(DATASET, "a" * 64)
    assert caught.value.tombstone is None
    assert "not ready" in str(caught.value)


def test_403_on_a_post_is_a_permission_error_not_an_auth_error():
    """Not on KBDL_DATASET_PUBLISHERS is a MISSING PERMISSION, not a bad token.

    Conflating the two sends an operator chasing fresh tokens forever for
    what is a one-line change to the service's publisher list.
    """
    session = FakeSession([FakeResponse(403, {"detail": "not a dataset publisher"})])
    with pytest.raises(KBDLNotAuthorizedError):
        make_client(session).publish_dataset_version(DATASET, "a" * 64)


# ---------------------------------------------------------------------------
# POST .../retire
# ---------------------------------------------------------------------------


def test_retire_dataset_version_posts_the_reason():
    updated = record(
        version=1,
        state="RETIRED",
        retired_at="2026-11-23T00:00:00Z",
        retire_reason="superseded",
    )
    session = FakeSession([FakeResponse(200, updated)])
    out = make_client(session).retire_dataset_version(DATASET, 1, "superseded")
    assert out["state"] == "RETIRED"
    call = session.calls[0]
    assert call["url"] == f"{TEST_BASE_URL}/datasets/{DATASET}/versions/1/retire"
    assert call["json"] == {"reason": "superseded"}


def test_retire_409_for_the_latest_or_a_held_version():
    session = FakeSession([FakeResponse(409, {"detail": "version 3 is the latest"})])
    with pytest.raises(KBDLConflictError):
        make_client(session).retire_dataset_version(DATASET, 3, "oops")


# ---------------------------------------------------------------------------
# POST .../hold
# ---------------------------------------------------------------------------


def test_hold_dataset_version_sends_the_reason():
    held = record(version=2, hold_reason="partner audit in progress")
    session = FakeSession([FakeResponse(200, held)])
    out = make_client(session).hold_dataset_version(
        DATASET, 2, "partner audit in progress"
    )
    assert out["hold_reason"] == "partner audit in progress"
    call = session.calls[0]
    assert call["url"] == f"{TEST_BASE_URL}/datasets/{DATASET}/versions/2/hold"
    assert call["json"] == {"reason": "partner audit in progress"}


def test_hold_with_reason_None_sends_an_explicit_json_null_and_releases():
    """``reason=None`` is the RELEASE instruction, so the key must be sent.

    The route distinguishes hold from release by the body alone: omitting
    ``reason`` would not release anything, and the released record is the
    proof this is the release direction and not "no reason supplied".
    """
    session = FakeSession([FakeResponse(200, record(version=2, hold_reason=None))])
    out = make_client(session).hold_dataset_version(DATASET, 2, None)
    assert out["hold_reason"] is None
    body = session.calls[0]["json"]
    assert body == {"reason": None}
    assert "reason" in body
    assert json.dumps(body) == '{"reason": null}'


# ---------------------------------------------------------------------------
# POST .../reclaim
# ---------------------------------------------------------------------------


def test_reclaim_dry_run_is_the_default():
    report = {
        "deleted": ["c" * 64],
        "kept_foreign": [],
        "kept_busy": [],
        "bytes_freed": 1024,
    }
    session = FakeSession([FakeResponse(200, report)])
    assert make_client(session).reclaim_dataset(DATASET) == report
    call = session.calls[0]
    assert call["url"] == f"{TEST_BASE_URL}/datasets/{DATASET}/reclaim"
    assert call["json"] == {"dry_run": True}


def test_reclaim_assume_retired_is_sent_as_strings_with_dry_run():
    session = FakeSession([FakeResponse(200, {"deleted": [], "bytes_freed": 0})])
    make_client(session).reclaim_dataset(
        DATASET, dry_run=True, assume_retired=[1, "2"]
    )
    assert session.calls[0]["json"] == {
        "dry_run": True,
        "assume_retired": ["1", "2"],
    }


def test_reclaim_assume_retired_without_dry_run_is_refused_locally():
    """The service answers 400; the client refuses before spending the trip.

    ``assume_retired`` asks for an ESTIMATE that pretends those versions are
    retired, which is meaningless for a run that actually deletes -- so this
    is a caller error and no request is made at all.
    """
    session = FakeSession([])
    with pytest.raises(KBDLBadRequestError) as caught:
        make_client(session).reclaim_dataset(
            DATASET, dry_run=False, assume_retired=["1"]
        )
    assert "only valid with dry_run" in str(caught.value)
    assert session.calls == []


def test_reclaim_real_run_sends_dry_run_false_and_nothing_else():
    session = FakeSession([FakeResponse(200, {"deleted": [], "bytes_freed": 0})])
    make_client(session).reclaim_dataset(DATASET, dry_run=False)
    assert session.calls[0]["json"] == {"dry_run": False}


# ---------------------------------------------------------------------------
# The real client IS the publisher's KBDLPort
# ---------------------------------------------------------------------------


def test_the_client_has_every_method_KBDLPort_declares():
    """Structural check against the Protocol's own declared method names.

    ``KBDLPort`` is a ``typing.Protocol`` that ``dataset_publish`` defines
    precisely so neither module imports the other, which means nothing fails
    at import time if they drift. This reads the method names off the
    Protocol itself rather than hard-coding them, so adding one to the port
    without adding it here is caught.
    """
    pytest.importorskip("pyarrow")
    from kbutillib.domains.kbase.berdl.dataset_publish import KBDLPort

    declared = [
        name
        for name in KBDLPort.__dict__
        if not name.startswith("_") and callable(KBDLPort.__dict__[name])
    ]
    assert declared, "KBDLPort declares no methods; the check would be vacuous"
    missing = [name for name in declared if not hasattr(KBDLServiceUtils, name)]
    assert not missing, f"KBDLServiceUtils does not satisfy KBDLPort: {missing}"


def test_a_real_client_over_a_fake_session_satisfies_KBDLPort():
    """``DatasetPublisher.status()`` driven through a REAL ``KBDLServiceUtils``.

    The only fake in the KBDL direction is the HTTP session; the publisher
    calls ``get_dataset_version`` on the real client, the real client builds
    the real ``GET /datasets/{name}/latest`` request, and the real
    ``_raise_for_status`` maps the response. That is the runtime proof the two
    halves of this PRD fit -- a Protocol alone proves nothing at runtime.
    """
    pytest.importorskip("pyarrow")
    from kbutillib.domains.kbase.berdl import dataset_publish as dp

    published_snapshot = "9007199254740994"
    target_snapshot = "9007199254740996"

    class FakeLakehouse:
        def current_snapshot_id(self, fqn):
            return target_snapshot

        def snapshots(self, fqn):
            return [
                {
                    "snapshot_id": published_snapshot,
                    "parent_id": None,
                    "committed_at": "2026-11-20T10:00:00Z",
                    "operation": "append",
                    "summary": {"total-records": "7", "total-files-size": "4096"},
                },
                {
                    "snapshot_id": target_snapshot,
                    "parent_id": published_snapshot,
                    "committed_at": "2026-11-21T10:00:00Z",
                    "operation": "append",
                    "summary": {"total-records": "9", "total-files-size": "5120"},
                },
            ]

        def spark_count(self, fqn, snapshot_id):  # pragma: no cover -- unused
            raise AssertionError("status() must not count")

        def export(self, *a, **k):  # pragma: no cover -- unused
            raise AssertionError("status() must not export")

    class FakeStaging:
        def list(self, prefix):
            return []

        def download(self, prefix, local_dir):  # pragma: no cover -- unused
            raise AssertionError("status() must not download")

        def delete(self, prefix):  # pragma: no cover -- unused
            raise AssertionError("status() must not delete")

    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "version": record(
                        version=3, source_snapshot_id=published_snapshot
                    ),
                    "manifest": {"manifest_schema": 1},
                },
            )
        ]
    )
    client = make_client(session)
    publisher = dp.DatasetPublisher(
        FakeLakehouse(), FakeStaging(), client, staging_root=None
    )

    verdict = publisher.status(DATASET)

    assert verdict["status"] == "changed"
    assert verdict["target"] == target_snapshot
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == f"{TEST_BASE_URL}/datasets/{DATASET}/latest"
