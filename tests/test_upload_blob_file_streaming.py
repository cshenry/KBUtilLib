"""Tests for streaming upload in KBWSUtils.upload_blob_file.

Verifies that:
1. ``requests.post`` is called with ``data=<MultipartEncoder>`` and NOT with ``files=``.
2. Peak resident-set-size (RSS) delta during the upload stays under 10 MB when
   streaming a 1 MB in-memory buffer — confirming O(chunk-size) memory behaviour.
"""

from __future__ import annotations

import io
import os
import resource
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from requests_toolbelt.multipart.encoder import MultipartEncoder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_kbws_utils(shock_url: str = "https://kbase.us/services/shock-api") -> object:
    """Return a minimal KBWSUtils-like object with the dependencies mocked out.

    We construct the object without going through __init__ so that we don't need
    a real config file, token file, or installed KBase clients.
    """
    from kbutillib.kb_ws_utils import KBWSUtils

    obj = object.__new__(KBWSUtils)
    # Minimal attributes required by upload_blob_file
    obj.shock_url = shock_url
    obj._token = "fake-token"

    # Stub get_token so it returns a predictable value
    obj.get_token = MagicMock(return_value="fake-token")

    # Stub log_info (used in the first definition)
    obj.log_info = MagicMock()

    # Stub hs_client (handle service)
    mock_hs = MagicMock()
    mock_hs.persist_handle.return_value = "handle-abc"
    obj.hs_client = mock_hs

    return obj


def _build_mock_response(shock_id: str = "node-xyz") -> MagicMock:
    """Return a mock requests.Response with a valid Shock/Blobstore JSON body."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"data": {"id": shock_id}}
    return mock_resp


# ---------------------------------------------------------------------------
# Test 1 — call signature: data=MultipartEncoder, no files=
# ---------------------------------------------------------------------------


def test_upload_blob_file_uses_multipart_encoder(tmp_path):
    """upload_blob_file must call requests.post with data=MultipartEncoder, not files=."""
    # Create a 1 MB temporary file
    test_file = tmp_path / "test_payload.bin"
    test_file.write_bytes(os.urandom(1024 * 1024))

    obj = _make_kbws_utils()
    mock_resp = _build_mock_response()

    captured_kwargs: dict = {}

    def capture_post(url, **kwargs):
        captured_kwargs.update(kwargs)
        return mock_resp

    with patch("requests.post", side_effect=capture_post):
        shock_id, handle_id = obj.upload_blob_file(str(test_file))

    # --- assertions on the return value ---
    assert shock_id == "node-xyz"
    assert handle_id == "handle-abc"

    # --- assertions on the call signature ---
    assert "files" not in captured_kwargs, (
        "requests.post must NOT be called with files=; found files= in kwargs"
    )
    assert "data" in captured_kwargs, (
        "requests.post must be called with data=<MultipartEncoder>"
    )
    assert isinstance(captured_kwargs["data"], MultipartEncoder), (
        f"data= must be a MultipartEncoder, got {type(captured_kwargs['data'])}"
    )
    assert "Content-Type" in captured_kwargs.get("headers", {}), (
        "headers must include Content-Type (set by MultipartEncoder)"
    )
    assert "multipart/form-data" in captured_kwargs["headers"]["Content-Type"], (
        "Content-Type header must be multipart/form-data"
    )


# ---------------------------------------------------------------------------
# Test 2 — memory: peak RSS delta < 10 MB for a 1 MB upload
# ---------------------------------------------------------------------------


def test_upload_blob_file_peak_rss_under_10mb(tmp_path):
    """Peak RSS increase during upload of a 1 MB file must stay below 10 MB."""
    ONE_MB = 1024 * 1024
    TEN_MB = 10 * ONE_MB

    test_file = tmp_path / "test_1mb.bin"
    test_file.write_bytes(b"\xab" * ONE_MB)

    obj = _make_kbws_utils()
    mock_resp = _build_mock_response()

    # On macOS resource.RUSAGE_SELF returns ru_maxrss in bytes; on Linux it's
    # in kilobytes.  Normalise to bytes.
    def _rss_bytes() -> int:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if os.uname().sysname == "Linux":
            return rss * 1024
        return rss  # macOS already in bytes

    # Force a GC pass before measuring baseline so heap is settled
    import gc
    gc.collect()
    rss_before = _rss_bytes()

    with patch("requests.post", return_value=mock_resp):
        obj.upload_blob_file(str(test_file))

    rss_after = _rss_bytes()

    delta = rss_after - rss_before
    assert delta < TEN_MB, (
        f"Peak RSS delta {delta / ONE_MB:.2f} MB exceeds 10 MB limit "
        f"(before={rss_before / ONE_MB:.1f} MB, after={rss_after / ONE_MB:.1f} MB). "
        "The implementation may be buffering the entire file in memory."
    )
