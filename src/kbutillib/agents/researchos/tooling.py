"""researchos.tooling — Ensure the research-os binary is available.

The ``research-os`` package is installed into a shared tooling venv
(default ``~/.venvs/research-os``).  This module provides a single function
:func:`ensure_research_os_binary` that either returns the binary path (if
already present) or creates the venv and pip-installs ``research-os`` into it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def ensure_research_os_binary(
    tooling_venv: Path,
    *,
    create: bool = True,
) -> Path:
    """Return the absolute path to ``<tooling_venv>/bin/research-os``.

    If the venv or binary is missing:
    - When *create* is True: create the venv (``python3 -m venv``) and
      ``pip install research-os`` into it, then return the binary path.
    - When *create* is False: raise ``RuntimeError`` with guidance.

    Idempotent: if the binary already exists, returns it without reinstalling.

    Args:
        tooling_venv: Path to the shared Research-OS tooling venv directory.
        create: When True, create the venv and install research-os if missing.

    Returns:
        Absolute path to ``<tooling_venv>/bin/research-os``.

    Raises:
        RuntimeError: When the binary is missing and *create* is False, or
            when venv creation or pip install fails.
    """
    bin_path = tooling_venv / "bin" / "research-os"

    if bin_path.is_file():
        return bin_path

    if not create:
        raise RuntimeError(
            f"research-os binary not found at {bin_path}.\n"
            "Create the tooling venv and install research-os with:\n"
            f"  python3 -m venv {tooling_venv}\n"
            f"  {tooling_venv}/bin/pip install research-os\n"
            "Or run: kbu researchos new <parent> <name>  (creates it automatically)"
        )

    # Create the venv
    venv_result = subprocess.run(
        [sys.executable, "-m", "venv", str(tooling_venv)],
        capture_output=True,
        text=True,
    )
    if venv_result.returncode != 0:
        raise RuntimeError(
            f"Failed to create tooling venv at {tooling_venv} "
            f"(rc={venv_result.returncode}):\n"
            + (venv_result.stderr or venv_result.stdout or "").strip()
        )

    # pip install research-os into the new venv
    pip_bin = tooling_venv / "bin" / "pip"
    pip_result = subprocess.run(
        [str(pip_bin), "install", "research-os"],
        capture_output=True,
        text=True,
    )
    if pip_result.returncode != 0:
        raise RuntimeError(
            f"pip install research-os failed (rc={pip_result.returncode}):\n"
            + (pip_result.stderr or pip_result.stdout or "").strip()
        )

    if not bin_path.is_file():
        raise RuntimeError(
            f"pip install succeeded but research-os binary not found at {bin_path}.\n"
            "The package may not install a 'research-os' entry point."
        )

    return bin_path
