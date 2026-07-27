"""Integration test for the Remote LP-Solver client (MSRemoteSolverUtils).

Runs a stdlib ``http.server`` stub in a background thread (no FastAPI/uvicorn
dependency, no real LP-solver service) and drives ``solve_lp`` against it,
per ``agent-io/prds/remote-lp-solver/fullprompt.md`` ("Testing Decisions" /
Acceptance Criteria 1, 10, 17-19). Verifies:

- the LP body is gzip-compressed with the correct ``Content-Type`` /
  ``Content-Encoding`` headers;
- ``solver``/``time_limit`` are sent as query parameters on ``POST /solve``
  (never in the body or headers);
- the client polls ``GET /result/{job_id}`` until a terminal status and
  returns the service's result dict verbatim;
- a client-side ``TimeoutError`` is raised when the stub never reaches a
  terminal status.

No solver dependency (gurobipy/cplex) is required; this test runs in any
environment.
"""

from __future__ import annotations

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

import pytest

from kbutillib.domains.modeling import ms_remote_solver_utils
from kbutillib.domains.modeling.ms_remote_solver_utils import MSRemoteSolverUtils

SAMPLE_LP = "Maximize\n obj: x\nSubject To\n c1: x <= 1\nEnd\n"

_TERMINAL_RESULT: Dict[str, Any] = {
    "status": "optimal",
    "objective_value": 7.0,
    "variables": {"x": 1.0},
    "solver": "gurobi",
    "solve_time_s": 0.01,
    "error": None,
}


class _StubHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for kbutillib.services.lp_solver.app's /solve + /result."""

    def _send_json(self, code: int, obj: Dict[str, Any]) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urlparse(self.path)
        if parsed.path != "/solve":
            self._send_json(404, {"detail": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        captured = self.server.captured  # type: ignore[attr-defined]
        captured["content_type"] = self.headers.get("Content-Type")
        captured["content_encoding"] = self.headers.get("Content-Encoding")
        captured["query"] = parse_qs(parsed.query)
        captured["lp_text"] = gzip.decompress(raw_body).decode("utf-8")

        self._send_json(200, {"job_id": self.server.job_id})  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urlparse(self.path)
        prefix = "/result/"
        if not parsed.path.startswith(prefix):
            self._send_json(404, {"detail": "not found"})
            return

        job_id = parsed.path[len(prefix):]
        self.server.poll_count += 1  # type: ignore[attr-defined]
        payload = self.server.result_provider(job_id, self.server.poll_count)  # type: ignore[attr-defined]
        self._send_json(200, payload)

    def log_message(self, format: str, *args: Any) -> None:  # silence stderr noise
        pass


class _StubServer:
    """Runs ``_StubHandler`` on an ephemeral localhost port in a thread."""

    def __init__(self, result_provider) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.server.captured = {}
        self.server.job_id = "test-job-1"
        self.server.poll_count = 0
        self.server.result_provider = result_provider
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def captured(self) -> Dict[str, Any]:
        return self.server.captured

    def __enter__(self) -> "_StubServer":
        self.thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def test_solve_lp_gzip_submit_query_params_and_poll_to_terminal():
    """Happy path: gzip body/headers, solver/time_limit as query params, poll-to-terminal."""

    # Non-terminal for the first two polls, terminal on the third -- exercises
    # the poll-until-terminal loop rather than a trivial single-poll return.
    def result_provider(job_id: str, poll_count: int) -> Dict[str, Any]:
        if poll_count < 3:
            return {"job_id": job_id, "status": "running"}
        return dict(_TERMINAL_RESULT)

    with _StubServer(result_provider) as stub:
        client = MSRemoteSolverUtils(
            base_url=stub.base_url,
            poll_interval=0.02,
            timeout=5,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )

        result = client.solve_lp(SAMPLE_LP, solver="gurobi", time_limit=42)

        assert result == _TERMINAL_RESULT

        # Wire format: gzip body + exact headers (S9 / interop contract).
        assert stub.captured["content_type"] == "text/plain; charset=utf-8"
        assert stub.captured["content_encoding"] == "gzip"
        assert stub.captured["lp_text"] == SAMPLE_LP

        # solver/time_limit must be QUERY PARAMETERS on POST /solve, not body/headers.
        query = stub.captured["query"]
        assert query["solver"] == ["gurobi"]
        assert query["time_limit"] == ["42.0"] or query["time_limit"] == ["42"]

        # Poll loop actually polled more than once before returning.
        assert stub.server.poll_count >= 3


def test_solve_lp_raises_timeout_error_when_never_terminal(monkeypatch):
    """Client raises TimeoutError only after min(requested, max_time_limit) + 90s grace."""

    # Speed the test up: shrink the fixed 90s post-time_limit grace window
    # without changing the documented production default (90s), which the
    # module exposes as a module-level constant read fresh on every call.
    monkeypatch.setattr(ms_remote_solver_utils, "_CLIENT_WAIT_GRACE_S", 0.05)

    def result_provider(job_id: str, poll_count: int) -> Dict[str, Any]:
        return {"job_id": job_id, "status": "running"}

    with _StubServer(result_provider) as stub:
        client = MSRemoteSolverUtils(
            base_url=stub.base_url,
            poll_interval=0.02,
            timeout=5,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )

        with pytest.raises(TimeoutError):
            client.solve_lp(SAMPLE_LP, time_limit=0.05)

        # Confirms this was a real poll loop (not an immediate raise).
        assert stub.server.poll_count >= 1


def test_read_lp_text_accepts_text_or_path(tmp_path):
    """solve_lp's lp_text_or_path accepts raw LP text or a path to a file."""
    assert MSRemoteSolverUtils._read_lp_text(SAMPLE_LP) == SAMPLE_LP

    lp_file = tmp_path / "model.lp"
    lp_file.write_text(SAMPLE_LP)
    assert MSRemoteSolverUtils._read_lp_text(str(lp_file)) == SAMPLE_LP
    assert MSRemoteSolverUtils._read_lp_text(lp_file) == SAMPLE_LP
