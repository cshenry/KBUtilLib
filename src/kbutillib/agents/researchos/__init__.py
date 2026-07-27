"""kbutillib.agents.researchos — Research-OS project scaffolder (canonical location).

Re-exports all public symbols. Old import path ``kbutillib.researchos`` is a shim.
"""

from __future__ import annotations

from kbutillib.agents.researchos.config import (
    resolve_aiassistant_root,
    resolve_researchos_root,
    resolve_tooling_venv,
    set_root,
)
from kbutillib.agents.researchos.manager import ResearchOSProject, ResearchOSProjectInfo
from kbutillib.agents.researchos.registry import RegistryResult, register_project
from kbutillib.agents.researchos.tooling import ensure_research_os_binary

__all__ = [
    "ResearchOSProject",
    "ResearchOSProjectInfo",
    "RegistryResult",
    "ensure_research_os_binary",
    "register_project",
    "resolve_aiassistant_root",
    "resolve_researchos_root",
    "resolve_tooling_venv",
    "set_root",
]
