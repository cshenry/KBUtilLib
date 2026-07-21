"""KBUtilLib client for the Remote LP-Solver service.

Mirrors :class:`~kbutillib.argo_utils.ArgoUtils`'s submit-then-poll shape and
:class:`~kbutillib.kb_berdl_utils.KBBERDLUtils`'s config-driven-endpoint /
structured-return-dict convention, per
``agent-io/prds/remote-lp-solver/fullprompt.md`` ("Repo & module layout",
"Wire format & polling", the Confront-hardened specifics block, and
Acceptance Criteria 1, 10, 17-19).

The service (``kbutillib.services.lp_solver.app``) exposes:

- ``POST {base_url}/solve``: request body is the **gzip-compressed LP text**
  (``Content-Type: text/plain; charset=utf-8``, ``Content-Encoding: gzip``);
  ``solver``/``time_limit`` are sent as **query parameters** on that POST
  (never in the body or headers). Response is ``{"job_id": "<uuid4>"}``.
- ``GET {base_url}/result/{job_id}``: 200 + JSON whose ``status`` field
  carries ``queued``/``running`` for non-terminal jobs and the terminal
  status (``optimal``/``infeasible``/``unbounded``/``timeout``/``error``)
  otherwise; 404 for an unknown ``job_id``. The terminal payload has exactly
  the six keys ``status``, ``objective_value``, ``variables``, ``solver``,
  ``solve_time_s``, ``error``.
- Optional ``x-api-key`` header, sent only when ``remote_solver.api_key`` is
  configured (non-empty).

For full documentation, call: utils.print_docs() or see
docs/modules/ms_remote_solver_utils.md
"""

from __future__ import annotations

import gzip
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

import httpx

from .shared_env_utils import SharedEnvUtils

logger = logging.getLogger(__name__)

__all__ = ["MSRemoteSolverUtils", "MSRemoteSolverUtilsImpl"]

# Non-terminal statuses reported by GET /result/{job_id} while a job is
# still being worked (S8 / Acceptance Criterion 11).
_NON_TERMINAL_STATUSES = {"queued", "running"}

# Poll cadence defaults (S10 / S16).
DEFAULT_BASE_URL = "http://127.0.0.1:8091"
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT = 30.0
DEFAULT_JITTER = 0.10  # +/-10% (S10)

# Client-side overall-wait grace beyond min(requested, max) time_limit (S17
# in fullprompt.md's "Wire format & polling" section / Acceptance
# Criterion 17). Keeps the client always outlasting the server watchdog
# (time_limit + 60s grace), so the normal terminal path is a real
# error/timeout result, not an early client give-up.
_CLIENT_WAIT_GRACE_S = 90.0

# Service defaults mirrored client-side purely to compute the client-side
# overall-wait deadline (Acceptance Criterion 17); the service applies its
# own defaults/clamping independently and is the source of truth for what
# actually gets passed to the solver.
_DEFAULT_TIME_LIMIT = 3600.0
_DEFAULT_MAX_TIME_LIMIT = 7200.0


class MSRemoteSolverUtils(SharedEnvUtils):
    """Client for the Remote LP-Solver service (H100 Gurobi/CPLEX over SSH tunnel).

    Example:
        >>> from kbutillib import MSRemoteSolverUtils
        >>> utils = MSRemoteSolverUtils()
        >>> result = utils.solve_lp(lp_text, solver="gurobi", time_limit=60)
        >>> print(result["status"], result["objective_value"])
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Remote LP-Solver client.

        Args:
            base_url: Override for ``remote_solver.base_url``
                (default ``http://127.0.0.1:8091``).
            timeout: Override for ``remote_solver.timeout`` (per-HTTP-request
                timeout, seconds).
            poll_interval: Override for ``remote_solver.poll_interval``
                (default 2.0s, +/-10% jitter applied per poll).
            api_key: Override for ``remote_solver.api_key``; sent as the
                ``x-api-key`` header only when non-empty.
            **kwargs: Additional keyword arguments passed to SharedEnvUtils.
        """
        super().__init__(**kwargs)

        self.base_url = (
            base_url
            if base_url is not None
            else self.get_config_value("remote_solver.base_url", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = float(
            timeout
            if timeout is not None
            else self.get_config_value("remote_solver.timeout", DEFAULT_TIMEOUT)
        )
        self.poll_interval = float(
            poll_interval
            if poll_interval is not None
            else self.get_config_value(
                "remote_solver.poll_interval", DEFAULT_POLL_INTERVAL
            )
        )
        self.api_key = (
            api_key
            if api_key is not None
            else self.get_config_value("remote_solver.api_key", None)
        )
        self.default_time_limit = float(
            self.get_config_value(
                "remote_solver.default_time_limit", _DEFAULT_TIME_LIMIT
            )
        )
        self.max_time_limit = float(
            self.get_config_value(
                "remote_solver.max_time_limit", _DEFAULT_MAX_TIME_LIMIT
            )
        )

        self.headers = {"Content-Type": "text/plain; charset=utf-8"}
        if self.api_key:
            self.headers["x-api-key"] = self.api_key

        self.cli = httpx.Client(timeout=self.timeout)

        self.log_info(
            f"MSRemoteSolverUtils initialized (base_url={self.base_url}, "
            f"poll_interval={self.poll_interval:.1f}s)"
        )

    def print_docs(self) -> None:
        """Print the Remote LP-Solver client documentation to the console."""
        module_dir = Path(__file__).parent
        docs_path = (
            module_dir.parent.parent / "docs" / "modules" / "ms_remote_solver_utils.md"
        )
        if docs_path.exists():
            print(docs_path.read_text())
        else:
            print(__doc__)

    # ------------------------------------------------------------------
    def solve_lp(
        self,
        lp_text_or_path: Union[str, Path],
        solver: Optional[str] = None,
        time_limit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Submit an LP to the Remote LP-Solver service and block until solved.

        Gzip-compresses the LP text and ``POST``s it to ``{base_url}/solve``
        with ``solver``/``time_limit`` as query parameters (never in the
        body or headers, per the service's wire contract). Then polls
        ``GET {base_url}/result/{job_id}`` every ``poll_interval`` seconds
        (+/-10% jitter) until the job reaches a terminal status.

        Args:
            lp_text_or_path: Raw LP-format text, or a path to a file
                containing it. A string is treated as a path only if it
                names an existing file; otherwise it is treated as LP text
                directly.
            solver: ``"gurobi"`` or ``"cplex"``; ``None`` lets the service
                apply its own default (Gurobi).
            time_limit: Solver time limit in seconds; ``None`` lets the
                service apply its own default.

        Returns:
            The service's result dict, verbatim, with exactly the keys
            ``status``, ``objective_value``, ``variables``, ``solver``,
            ``solve_time_s``, ``error``.

        Raises:
            TimeoutError: If no terminal result is observed within
                ``min(requested_time_limit, max_time_limit) + 90s`` of
                submission.
            httpx.HTTPStatusError: On a non-2xx response from the service
                (e.g. 503 when the job queue is full).
        """
        lp_text = self._read_lp_text(lp_text_or_path)
        job_id = self._submit(lp_text, solver=solver, time_limit=time_limit)
        deadline = time.monotonic() + self._client_wait_seconds(time_limit)
        return self._poll_until_terminal(job_id, deadline)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_lp_text(lp_text_or_path: Union[str, Path]) -> str:
        """Return LP text, reading it from disk if a path was given."""
        if isinstance(lp_text_or_path, Path):
            return lp_text_or_path.read_text()
        if isinstance(lp_text_or_path, str):
            # Only treat as a path if it actually names an existing file --
            # LP text itself is never mistaken for a path (multi-line text
            # can't be a valid filename on any platform we care about).
            try:
                if os.path.isfile(lp_text_or_path):
                    return Path(lp_text_or_path).read_text()
            except (OSError, ValueError):
                pass
            return lp_text_or_path
        raise TypeError(
            f"lp_text_or_path must be str or Path, got {type(lp_text_or_path)!r}"
        )

    def _client_wait_seconds(self, requested_time_limit: Optional[float]) -> float:
        """Client-side overall wait budget (Acceptance Criterion 17).

        ``min(requested_time_limit, max_time_limit) + 90s`` -- always at
        least the server watchdog's own grace window, so the client never
        gives up before the service could possibly have produced a
        definitive terminal result.
        """
        effective_limit = (
            requested_time_limit
            if requested_time_limit is not None
            else self.default_time_limit
        )
        return min(effective_limit, self.max_time_limit) + _CLIENT_WAIT_GRACE_S

    def _submit(
        self,
        lp_text: str,
        solver: Optional[str],
        time_limit: Optional[float],
    ) -> str:
        """POST the gzipped LP to {base_url}/solve; return the job_id."""
        compressed = gzip.compress(lp_text.encode("utf-8"))
        params: Dict[str, Any] = {}
        if solver is not None:
            params["solver"] = solver
        if time_limit is not None:
            params["time_limit"] = time_limit

        r = self.cli.post(
            f"{self.base_url}/solve",
            params=params,
            content=compressed,
            headers={**self.headers, "Content-Encoding": "gzip"},
        )
        r.raise_for_status()
        return r.json()["job_id"]

    def _poll_until_terminal(self, job_id: str, deadline: float) -> Dict[str, Any]:
        """Poll GET /result/{job_id} until a terminal status or the deadline."""
        api_key_header = (
            {"x-api-key": self.api_key} if self.api_key else {}
        )
        while True:
            r = self.cli.get(
                f"{self.base_url}/result/{job_id}", headers=api_key_header
            )
            r.raise_for_status()
            payload = r.json()
            if payload.get("status") not in _NON_TERMINAL_STATUSES:
                return payload

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Remote LP-Solver job {job_id!r} did not reach a terminal "
                    f"status within the client wait budget"
                )

            jitter = 1.0 + random.uniform(-DEFAULT_JITTER, DEFAULT_JITTER)
            sleep_for = max(0.0, self.poll_interval * jitter)
            remaining = deadline - time.monotonic()
            time.sleep(min(sleep_for, max(0.0, remaining)))


# ── Composition-based implementation ─────────────────────────────────────


class MSRemoteSolverUtilsImpl:
    """Composition-based version of MSRemoteSolverUtils.

    Holds ``env: SharedEnvUtils`` instead of inheriting. Delegates all
    method calls to an internal legacy instance.

    The legacy ``MSRemoteSolverUtils`` eagerly constructs an ``httpx.Client``
    during ``__init__``. To keep ``KBUtilLib().remote_solver`` cheap, the
    delegate is created lazily on first real attribute access, mirroring
    ``ArgoUtilsImpl``.
    """

    def __init__(self, env, **kwargs):
        self._env = env
        self._init_kwargs = kwargs
        self._delegate = None

    def _ensure_delegate(self):
        if self._delegate is None:
            _kwargs = {
                "config_file": False,
                "token_file": None,
                "kbase_token_file": None,
            }
            # Copy token if env has one (mirrors ArgoUtilsImpl; unused today
            # since the service's auth is the optional x-api-key config
            # value, not a kbase token, but kept for parity/consistency).
            try:
                _kwargs["token"] = self._env.get_token("kbase")
            except Exception:
                pass
            _kwargs.update(self._init_kwargs)
            self._delegate = MSRemoteSolverUtils(**_kwargs)

    @property
    def env(self):
        return self._env

    def __getattr__(self, name):
        # Delegate all attribute access to the lazily-created legacy instance
        self._ensure_delegate()
        return getattr(self._delegate, name)
