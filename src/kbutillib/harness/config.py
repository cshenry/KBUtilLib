"""harness.toml load/save and upward-search helpers.

Fields
------
beril_root      absolute path to the BERIL deployment root
harness_root    absolute path to the harness root directory (parent of the project dir)
project_id      sanitized project identifier
created_at      ISO-8601 UTC timestamp with trailing Z
kbutillib_version  installed dist version, or 'source_commit:<sha>' when running from checkout
python          optional absolute path to the harness venv interpreter
"""

from __future__ import annotations

import importlib.metadata
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


try:
    import tomllib  # py 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

import tomli_w


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass
class HarnessConfig:
    """All fields recorded in harness.toml."""

    beril_root: str
    harness_root: str
    project_id: str
    created_at: str
    kbutillib_version: str
    python: Optional[str] = field(default=None)

    def to_dict(self) -> dict:
        d: dict = {
            "beril_root": self.beril_root,
            "harness_root": self.harness_root,
            "project_id": self.project_id,
            "created_at": self.created_at,
            "kbutillib_version": self.kbutillib_version,
        }
        if self.python is not None:
            d["python"] = self.python
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "HarnessConfig":
        return cls(
            beril_root=d["beril_root"],
            harness_root=d["harness_root"],
            project_id=d["project_id"],
            created_at=d["created_at"],
            kbutillib_version=d["kbutillib_version"],
            python=d.get("python"),
        )


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

_DIST_NAME = "KBUtilLib"


def _get_kbutillib_version() -> str:
    """Return distribution version if available, else source_commit:<sha>."""
    try:
        return importlib.metadata.version(_DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        pass
    # Running from a source checkout — try to get HEAD sha
    try:
        import kbutillib as _kbu
        pkg_file = Path(_kbu.__file__).resolve()
        # Walk up: pkg_file -> kbutillib/ -> src/ -> repo root
        repo_root = pkg_file.parent.parent.parent
        result = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%H"],
            capture_output=True,
            text=True,
            check=False,
        )
        sha = result.stdout.strip()
        if sha:
            return f"source_commit:{sha}"
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Sanitize project-id
# ---------------------------------------------------------------------------


def sanitize_project_id(project_id: str) -> str:
    """Lowercase, strip, replace chars outside [a-z0-9._-] with '-'."""
    pid = project_id.lower().strip()
    pid = re.sub(r"[^a-z0-9._-]", "-", pid)
    return pid


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------


def save_config(harness_dir: Path, config: HarnessConfig) -> None:
    """Write harness.toml to *harness_dir*."""
    toml_path = harness_dir / "harness.toml"
    toml_path.write_bytes(tomli_w.dumps(config.to_dict()).encode("utf-8"))


def load_config(harness_dir: Path) -> HarnessConfig:
    """Load harness.toml from *harness_dir*.

    Raises FileNotFoundError if the file is absent, ValueError if malformed.
    """
    toml_path = harness_dir / "harness.toml"
    if not toml_path.is_file():
        raise FileNotFoundError(f"harness.toml not found at {toml_path}")
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Malformed harness.toml at {toml_path}: {exc}") from exc
    try:
        return HarnessConfig.from_dict(data)
    except KeyError as exc:
        raise ValueError(f"Missing required field in harness.toml: {exc}") from exc


# ---------------------------------------------------------------------------
# Upward search
# ---------------------------------------------------------------------------


def find_harness_toml(start: Optional[Path] = None) -> Optional[Path]:
    """Search upward from *start* (default CWD) for the nearest harness.toml.

    Returns the directory containing harness.toml, or None.
    """
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / "harness.toml"
        if candidate.is_file():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
