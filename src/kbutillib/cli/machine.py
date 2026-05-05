"""Machine alias resolution and per-machine config loading."""

from __future__ import annotations

import copy
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

import click
import yaml

logger = logging.getLogger(__name__)

_CONFLICT_PATTERN = re.compile(r"\(Conflict")


def find_machine_configs_dir() -> Path:
    """Locate the ``machine_configs/`` directory at the KBUtilLib repo root.

    Walks up from this file's location (``src/kbutillib/cli/machine.py``)
    until it finds a directory containing ``machine_configs/``.
    """
    current = Path(__file__).resolve().parent
    for _ in range(10):  # safety bound
        candidate = current / "machine_configs"
        if candidate.is_dir():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    raise FileNotFoundError(
        "Could not locate machine_configs/ directory. "
        "Ensure this code is running from within the KBUtilLib repository."
    )


def _list_known_aliases(configs_dir: Path) -> list[str]:
    """Return alias names from YAML files in *configs_dir*, excluding conflicts."""
    aliases: list[str] = []
    for p in sorted(configs_dir.glob("*.yaml")):
        if _CONFLICT_PATTERN.search(p.name):
            continue
        name = p.stem
        if name.startswith("_"):
            continue
        aliases.append(name)
    return aliases


def get_hardware_uuid() -> Optional[str]:
    """Return a hardware UUID for the current machine, or None on failure.

    macOS: ``ioreg`` IOPlatformUUID.
    Linux: ``/etc/machine-id``.
    """
    # macOS
    try:
        result = subprocess.run(
            ["ioreg", "-d2", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout)
            if match:
                return match.group(1)
    except Exception:
        pass

    # Linux
    try:
        machine_id_path = Path("/etc/machine-id")
        if machine_id_path.exists():
            return machine_id_path.read_text().strip()
    except Exception:
        pass

    return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge *override* into a copy of *base*. Override wins on conflicts."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_machine_config(alias: str) -> dict[str, Any]:
    """Load merged config: ``_default.yaml`` deep-merged with ``<alias>.yaml``."""
    configs_dir = find_machine_configs_dir()
    default_path = configs_dir / "_default.yaml"
    alias_path = configs_dir / f"{alias}.yaml"

    base: dict[str, Any] = {}
    if default_path.exists():
        with open(default_path) as f:
            base = yaml.safe_load(f) or {}

    override: dict[str, Any] = {}
    if alias_path.exists():
        with open(alias_path) as f:
            override = yaml.safe_load(f) or {}

    return _deep_merge(base, override)


def resolve_alias(prompt_fallback: bool = True) -> str:
    """Resolve the current machine's alias.

    Resolution order:
    1. AgentForge ``config.load_config().worker.machine_alias`` (import).
    2. Direct YAML parse of ``~/.agentforge/config.yaml`` -> ``worker.machine_alias``.
    3. Hardware UUID matched against ``machine_configs/*.yaml`` ``hardware_uuids`` lists.
    4. Interactive prompt from known aliases (if *prompt_fallback* is True).
    """
    # 1. Try AgentForge Python import
    try:
        from agentforge.config import load_config  # type: ignore[import-untyped]

        cfg = load_config()
        alias = cfg.worker.machine_alias
        if alias:
            logger.debug("Resolved alias via AgentForge config import: %s", alias)
            return str(alias)
    except ImportError:
        logger.debug("AgentForge not installed; skipping import-based resolution.")
    except Exception:
        logger.debug("AgentForge config load failed; falling through to YAML parse.")

    # 2. Direct YAML parse
    agentforge_config = Path("~/.agentforge/config.yaml").expanduser()
    if agentforge_config.exists():
        try:
            with open(agentforge_config) as f:
                raw = yaml.safe_load(f) or {}
            worker_block = raw.get("worker", {})
            alias = worker_block.get("machine_alias")
            if alias:
                logger.debug("Resolved alias via YAML parse: %s", alias)
                return str(alias)
        except Exception:
            logger.debug("YAML parse of agentforge config failed; falling through.")

    # 3. Hardware UUID match
    hw_uuid = get_hardware_uuid()
    if hw_uuid:
        try:
            configs_dir = find_machine_configs_dir()
            for path in sorted(configs_dir.glob("*.yaml")):
                if _CONFLICT_PATTERN.search(path.name):
                    continue
                if path.stem.startswith("_"):
                    continue
                with open(path) as f:
                    data = yaml.safe_load(f) or {}
                uuids = data.get("hardware_uuids", [])
                if hw_uuid in uuids:
                    alias = path.stem
                    logger.debug("Resolved alias via hardware UUID: %s", alias)
                    return alias
        except FileNotFoundError:
            pass
        logger.debug("Hardware UUID %s did not match any machine config.", hw_uuid)
    else:
        logger.debug("Could not determine hardware UUID.")

    # 4. Interactive prompt
    if prompt_fallback:
        try:
            configs_dir = find_machine_configs_dir()
            known = _list_known_aliases(configs_dir)
        except FileNotFoundError:
            known = []

        if known:
            click.echo("Could not auto-detect machine alias.")
            click.echo(f"Known aliases: {', '.join(known)}")
            return click.prompt(
                "Enter your machine alias",
                type=click.Choice(known),
            )

    raise click.ClickException(
        "Could not resolve machine alias. Set worker.machine_alias in "
        "~/.agentforge/config.yaml, or add your hardware UUID to a "
        "machine_configs/<alias>.yaml file."
    )
