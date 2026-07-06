"""researchos.registry — Register Research-OS studies in the AIAssistant project registry.

The AIAssistant registry (``state/project_registry.yaml``) is an event-sourced
store that must only be mutated via ``assistant.state.registry``.  Since
``kbu`` runs in its own venv, the mutation is performed **out of process**:
we invoke ``python3 -c "<script>"`` with ``<aiassistant_root>/src`` prepended
to ``sys.path`` inside the subprocess.

This module is best-effort: :func:`register_project` returns a
:class:`RegistryResult` and never raises through to abort callers.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class RegistryResult:
    """Result of a :func:`register_project` call."""

    status: Literal["ok", "skipped", "failed"]
    message: str


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------


def _slug(name: str) -> str:
    """Convert *name* to a lowercase, hyphen-separated slug.

    Rules:
    - Lowercase the entire string.
    - Replace runs of non-alphanumeric characters with a single hyphen.
    - Strip leading and trailing hyphens.

    Examples:
        'AIALE' -> 'aiale'
        'RoboticLabManuscript' -> 'roboticlabmanuscript'
        'My Study 2024!' -> 'my-study-2024'
    """
    lower = name.lower()
    slugged = re.sub(r"[^a-z0-9]+", "-", lower)
    return slugged.strip("-")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_project(
    parent: str,
    name: str,
    project_path: Path,
    *,
    aiassistant_root: Path,
) -> RegistryResult:
    """Ensure parent + study exist in the AIAssistant project registry.

    Performs the mutation out-of-process via ``python3 -c`` with
    ``<aiassistant_root>/src`` on ``sys.path``.

    The script idempotently ensures (checking get_project(id) first):
    1. Top-level 'researchos' group.
    2. Parent group (id = _slug(parent)) under 'researchos'.
    3. Study project (id = f'{parent_id}-{_slug(name)}') under parent group,
       with repo_path = str(project_path).

    Args:
        parent: The parent group name (e.g. 'AIALE').
        name: The study name (e.g. 'RoboticLabManuscript').
        project_path: Absolute path to the project directory.
        aiassistant_root: Path to the AIAssistant repository root.

    Returns:
        A :class:`RegistryResult` indicating ok/skipped/failed.
        Never raises.
    """
    src_path = aiassistant_root / "src"
    if not src_path.is_dir():
        return RegistryResult(
            status="failed",
            message=(
                f"AIAssistant src not found at {src_path}. "
                "Check --aiassistant-root or AIASSISTANT_ROOT env var."
            ),
        )

    parent_id = _slug(parent)
    study_id = f"{parent_id}-{_slug(name)}"

    # Build the Python script that runs inside the subprocess.
    script = textwrap.dedent(f"""\
        import sys
        sys.path.insert(0, {str(src_path)!r})
        from assistant.state.registry import add_project, get_project

        # 1. Top-level researchos group
        if not get_project("researchos"):
            add_project(
                "researchos",
                "ResearchOS",
                node_type="group",
                description="Research-OS harness studies (kbu researchos)",
            )

        # 2. Parent group
        parent_id = {parent_id!r}
        parent_name = {parent!r}
        if not get_project(parent_id):
            add_project(
                parent_id,
                parent_name,
                node_type="group",
                parent="researchos",
            )

        # 3. Study project
        study_id = {study_id!r}
        study_name = {name!r}
        project_path_str = {str(project_path)!r}
        if not get_project(study_id):
            add_project(
                study_id,
                study_name,
                node_type="project",
                parent=parent_id,
                repo_path=project_path_str,
            )
        else:
            print(f"skip: {{study_id}} already registered", flush=True)
    """)

    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
        )
    except Exception as exc:  # noqa: BLE001
        return RegistryResult(
            status="failed",
            message=f"subprocess raised: {exc}",
        )

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        return RegistryResult(
            status="failed",
            message=f"registry subprocess failed (rc={result.returncode}): {stderr}",
        )

    stdout = result.stdout.strip()
    if "skip:" in stdout:
        return RegistryResult(
            status="skipped",
            message=f"already registered: {study_id}",
        )

    return RegistryResult(
        status="ok",
        message=f"registered {parent}/{name} as {study_id}",
    )
