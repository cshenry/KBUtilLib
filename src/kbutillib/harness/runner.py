"""runner.py — notebook execution via nbconvert --execute --inplace.

RunResult fields
----------------
notebook        relative path string (relative to harness_dir)
executed        True iff nbconvert exited 0
exit_code       nbconvert process exit code
error           nbconvert stderr trimmed to 10k bytes (empty string on success)
outputs_present True iff any code cell has non-error, non-empty outputs
runtime_s       wall-clock seconds for this notebook

run_notebooks()
---------------
- Discover: sorted(Path('notebooks').glob('*.ipynb')) excluding dot-files and
  .ipynb_checkpoints.
- Execute via harness venv interpreter:
    python -m jupyter nbconvert --to notebook --execute --inplace
    --ExecutePreprocessor.kernel_name=python3 <nb>
- Stop at first failure (exit code != 0).
- --on h100: write a task.md to the h100 inbox and return immediately.

No shell=True anywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence

try:
    import nbformat
    _NBFORMAT_AVAILABLE = True
except ImportError:
    _NBFORMAT_AVAILABLE = False

from .config import find_harness_toml, load_config


# ---------------------------------------------------------------------------
# RunResult
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    notebook: str
    executed: bool
    exit_code: int
    error: str
    outputs_present: bool
    runtime_s: float

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Interpreter resolution
# ---------------------------------------------------------------------------


def _resolve_interpreter(harness_dir: Path, cfg) -> Optional[str]:
    """Resolve the harness venv interpreter.

    Priority:
    1. harness.toml.python (when present and the file exists)
    2. <harness_dir>/.venv/bin/python
    """
    if cfg.python and Path(cfg.python).is_file():
        return cfg.python
    fallback = harness_dir / ".venv" / "bin" / "python"
    if fallback.is_file():
        return str(fallback)
    return None


# ---------------------------------------------------------------------------
# Notebook discovery
# ---------------------------------------------------------------------------


def discover_notebooks(harness_dir: Path) -> list[Path]:
    """Discover notebooks/*.ipynb in sorted lexicographic order."""
    nb_dir = harness_dir / "notebooks"
    if not nb_dir.is_dir():
        return []
    notebooks = sorted(
        p
        for p in nb_dir.glob("*.ipynb")
        if not p.name.startswith(".")
        and ".ipynb_checkpoints" not in p.parts
    )
    return notebooks


# ---------------------------------------------------------------------------
# Output presence check
# ---------------------------------------------------------------------------


def _outputs_present(nb_path: Path) -> bool:
    """True iff any code cell has non-error, non-empty outputs (nbformat>=5)."""
    if not _NBFORMAT_AVAILABLE:
        return False
    try:
        nb = nbformat.read(str(nb_path), as_version=4)
    except Exception:  # noqa: BLE001
        return False
    for cell in nb.cells:
        if cell.cell_type != "code":
            continue
        for output in cell.get("outputs", []):
            otype = output.get("output_type", "")
            if otype == "stream":
                return True
            if otype == "execute_result" and output.get("data"):
                return True
            if otype == "display_data" and output.get("data"):
                return True
    return False


# ---------------------------------------------------------------------------
# Execute a single notebook
# ---------------------------------------------------------------------------


_MAX_ERROR_BYTES = 10_000


def _execute_notebook(
    nb_path: Path,
    interpreter: str,
    harness_dir: Path,
) -> RunResult:
    """Execute *nb_path* via nbconvert; return RunResult."""
    nb_rel = str(nb_path.relative_to(harness_dir))
    t0 = time.monotonic()
    cmd = [
        interpreter,
        "-m",
        "jupyter",
        "nbconvert",
        "--to",
        "notebook",
        "--execute",
        "--inplace",
        "--ExecutePreprocessor.kernel_name=python3",
        str(nb_path),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    runtime_s = time.monotonic() - t0
    executed = result.returncode == 0
    error_raw = (result.stderr or "").strip()
    error = error_raw[:_MAX_ERROR_BYTES]

    outputs = _outputs_present(nb_path) if executed else False

    return RunResult(
        notebook=nb_rel,
        executed=executed,
        exit_code=result.returncode,
        error=error,
        outputs_present=outputs,
        runtime_s=round(runtime_s, 3),
    )


# ---------------------------------------------------------------------------
# h100 dispatch
# ---------------------------------------------------------------------------


def _resolve_h100_inbox(
    h100_inbox_override: Optional[str] = None,
) -> Path:
    """Resolve h100 inbox path from override, env var, or default."""
    if h100_inbox_override:
        return Path(h100_inbox_override).expanduser().resolve()
    env_val = os.environ.get("KBU_H100_INBOX")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return (
        Path.home()
        / "Dropbox"
        / "Projects"
        / "AIAssistant"
        / "cowork-inbox"
        / "h100"
    )


def _write_h100_task(
    harness_dir: Path,
    project_id: str,
    notebooks: list[Path],
    h100_inbox: Path,
    echo: Callable = print,
) -> tuple[bool, str]:
    """Write a cowork task file to the h100 inbox.

    Returns (ok, task_file_path_or_error).
    """
    if not h100_inbox.is_dir():
        msg = f"✗ h100 inbox not found at {h100_inbox}"
        echo(msg)
        return False, msg

    def _escape(nb: Path) -> str:
        """Single-quote a notebook path, escaping embedded single quotes."""
        s = str(nb)
        return "'" + s.replace("'", "'\"'\"'") + "'"

    harness_abs = str(harness_dir.resolve())
    nb_args = " ".join(_escape(nb) for nb in notebooks)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    task_filename = f"kbu-{project_id}-{ts}.task.md"
    task_path = h100_inbox / task_filename

    shell_block = (
        f"cd '{harness_abs}'; kbu harness run --on local {nb_args}"
    )
    body = (
        f"# kbu harness run — {project_id}\n\n"
        f"Run the harness notebooks for project `{project_id}` on h100.\n\n"
        f"```sh\n{shell_block}\n```\n"
    )
    task_path.write_text(body, encoding="utf-8")
    echo(f"   ✓ task file written: {task_path}")
    return True, str(task_path)


# ---------------------------------------------------------------------------
# Public run_notebooks
# ---------------------------------------------------------------------------


def run_notebooks(
    harness_dir: Path,
    notebooks: Optional[Sequence[Path]] = None,
    on: str = "local",
    h100_inbox_override: Optional[str] = None,
    echo: Callable = print,
) -> tuple[list[RunResult], str]:
    """Execute notebooks and return (results, overall_status).

    overall_status: 'ok' | 'partial' | 'failed' | 'none-matched'
    For --on h100: results is empty, overall_status is 'dispatched'.
    """
    try:
        cfg = load_config(harness_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError(f"Cannot load harness.toml: {exc}") from exc

    # Discover notebooks if not specified
    if notebooks is None or len(notebooks) == 0:
        discovered = discover_notebooks(harness_dir)
    else:
        discovered = list(notebooks)

    if not discovered:
        echo("✗ no notebooks matched in notebooks/")
        return [], "none-matched"

    if on == "h100":
        h100_inbox = _resolve_h100_inbox(h100_inbox_override)
        ok, detail = _write_h100_task(
            harness_dir, cfg.project_id, discovered, h100_inbox, echo=echo
        )
        if not ok:
            return [], "failed"
        echo(f"   Dispatched to h100: {detail}")
        return [], "dispatched"

    # Local execution
    interpreter = _resolve_interpreter(harness_dir, cfg)
    if interpreter is None:
        raise RuntimeError(
            "Cannot locate harness venv interpreter. "
            "Run `kbu harness init` or check harness.toml.python."
        )

    results: list[RunResult] = []
    for nb in discovered:
        echo(f"── run: {nb.name}")
        result = _execute_notebook(nb, interpreter, harness_dir)
        results.append(result)
        if result.executed:
            echo(f"   ✓ {nb.name} ({result.runtime_s}s, outputs_present={result.outputs_present})")
        else:
            echo(f"   ✗ {nb.name} failed (exit_code={result.exit_code})")
            break  # Stop at first failure

    # Determine overall status
    if not results:
        overall = "none-matched"
    elif all(r.executed and r.outputs_present for r in results):
        overall = "ok"
    elif any(not r.executed for r in results):
        if len(results) < len(discovered):
            overall = "partial"
        else:
            overall = "failed"
    else:
        overall = "partial"

    return results, overall
