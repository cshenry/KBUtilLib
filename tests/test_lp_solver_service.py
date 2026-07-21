"""End-to-end test for the Remote LP-Solver FastAPI service.

Boots ``kbutillib.services.lp_solver.app`` in-process on an ephemeral
uvicorn port with a stub solve backend (no gurobipy/cplex required),
submits a small gzipped LP over real HTTP, and polls ``GET /result`` to a
terminal ``optimal`` result. This is the one test exercising ``app.py`` +
``worker.py`` glue end-to-end (per fullprompt.md "Testing Decisions" /
S14 -- solver-independent, runs in any environment). All service state is
rooted under a pytest ``tmp_path``; the real ``~/.lp-solver`` is never
touched.
"""

from __future__ import annotations

import gzip
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

fastapi = pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")
requests = pytest.importorskip("requests")

from kbutillib.services.lp_solver import app as lp_solver_app  # noqa: E402
from kbutillib.shared_env_utils import SharedEnvUtils  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

SAMPLE_LP = "Maximize\n obj: x\nSubject To\n c1: x <= 1\nEnd\n"

_STUB_RESULT: Dict[str, Any] = {
    "status": "optimal",
    "objective_value": 7.0,
    "variables": {"x": 1.0},
    "solver": "gurobi",
    "solve_time_s": 0.01,
    "error": None,
}


def stub_solve(
    lp_path: str,
    solver: Optional[str] = None,
    time_limit: Optional[float] = None,
    threads: Optional[int] = None,
) -> Dict[str, Any]:
    """Stub replacing solver_backends.solve for this test's subprocess.

    Must be importable as ``tests.test_lp_solver_service:stub_solve`` from
    a *fresh* interpreter (worker.py launches every solve in its own
    subprocess, per S6) -- confirms the LP temp file really made it to the
    subprocess, then returns a canned, schema-shaped result.
    """
    text = Path(lp_path).read_text()
    assert "obj: x" in text
    result = dict(_STUB_RESULT)
    result["solver"] = solver or "gurobi"
    return result


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _ServerThread:
    """Runs a uvicorn.Server for *app* on an ephemeral port in a thread."""

    def __init__(self, app) -> None:
        self.port = _free_port()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "_ServerThread":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("uvicorn server did not start within 10s")
        return self

    def __exit__(self, *exc_info) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def lp_solver_test_app(tmp_path):
    """A create_app() instance fully isolated under tmp_path with a stub backend."""
    env = SharedEnvUtils(config_file=False, token_file=None, kbase_token_file=None)
    application = lp_solver_app.create_app(
        base_dir=tmp_path / "lp-solver-state",
        env=env,
        solve_entrypoint="tests.test_lp_solver_service:stub_solve",
        subprocess_cwd=str(REPO_ROOT),
    )
    return application


def _poll_until_terminal(base_url: str, job_id: str, timeout: float = 15.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last_payload: Dict[str, Any] = {}
    while time.time() < deadline:
        r = requests.get(f"{base_url}/result/{job_id}", timeout=5)
        assert r.status_code == 200
        last_payload = r.json()
        if last_payload.get("status") not in ("queued", "running"):
            return last_payload
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached a terminal state: {last_payload}")


def test_e2e_submit_and_poll_to_optimal_result(lp_solver_test_app):
    with _ServerThread(lp_solver_test_app) as srv:
        base_url = srv.base_url

        r = requests.get(f"{base_url}/healthz", timeout=5)
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        body = gzip.compress(SAMPLE_LP.encode("utf-8"))
        r = requests.post(
            f"{base_url}/solve",
            data=body,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Encoding": "gzip",
            },
            timeout=5,
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        assert isinstance(job_id, str) and job_id

        result = _poll_until_terminal(base_url, job_id)

        assert result["status"] == "optimal"
        assert result["objective_value"] == 7.0
        assert result["variables"] == {"x": 1.0}
        assert result["solver"] == "gurobi"
        assert result["error"] is None

        # Unknown job_id -> 404 (Acceptance Criterion 11 / S8).
        r = requests.get(f"{base_url}/result/does-not-exist", timeout=5)
        assert r.status_code == 404


def test_queue_depth_503(tmp_path):
    """POST /solve returns 503 once queued jobs exceed max_queue_depth (AC12).

    Uses ``fastapi.testclient.TestClient`` *without* entering it as a context
    manager, which means the app's lifespan (and therefore
    ``LPWorkerPool.start()``) never runs -- submitted jobs stay ``queued``
    forever, giving a deterministic queue to assert 503 against without
    reaching into any asyncio/worker internals.
    """
    from fastapi.testclient import TestClient

    env = SharedEnvUtils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        config={"remote_solver": {"max_queue_depth": 1}},
    )
    application = lp_solver_app.create_app(
        base_dir=tmp_path / "lp-solver-state",
        env=env,
        solve_entrypoint="tests.test_lp_solver_service:stub_solve",
        subprocess_cwd=str(REPO_ROOT),
    )
    client = TestClient(application)  # not entered as a context manager -> no lifespan

    body = gzip.compress(SAMPLE_LP.encode("utf-8"))
    headers = {"Content-Type": "text/plain; charset=utf-8", "Content-Encoding": "gzip"}

    r1 = client.post("/solve", content=body, headers=headers)
    assert r1.status_code == 200

    r2 = client.post("/solve", content=body, headers=headers)
    assert r2.status_code == 503
