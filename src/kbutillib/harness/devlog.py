"""devlog.py — append-only DEVLOG.md writer.

Each entry has the form:

  ## <ISO-8601 UTC Z> — <pull|run|push>

  ```yaml
  notebooks: [<list>]
  scope: sample|full
  where: local|h100
  outcome: ok|failed
  runtime_s: <float>
  traceback: |
    <text, <=10k bytes>   (only on failure)
  ```

Existing entries are NEVER rewritten.
No file locking (single-writer assumption — local, one user at a time).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence


_MAX_TRACEBACK_BYTES = 10_000


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_entry(
    devlog_path: Path,
    action: str,
    notebooks: Sequence[str],
    scope: str,
    where: str,
    outcome: str,
    runtime_s: float,
    traceback: Optional[str] = None,
) -> None:
    """Append one entry to DEVLOG.md at *devlog_path*.

    Parameters
    ----------
    devlog_path : Path to DEVLOG.md (created if absent).
    action      : 'pull' | 'run' | 'push'
    notebooks   : list of notebook names/paths (may be empty for pull/push)
    scope       : 'sample' | 'full'
    where       : 'local' | 'h100'
    outcome     : 'ok' | 'failed'
    runtime_s   : wall-clock seconds
    traceback   : optional stderr text (trimmed to 10k bytes)
    """
    ts = _utc_now_z()
    nb_list = list(notebooks)

    # Build yaml block
    lines = [
        f"## {ts} — {action}",
        "",
        "```yaml",
        f"notebooks: {_yaml_str_list(nb_list)}",
        f"scope: {scope}",
        f"where: {where}",
        f"outcome: {outcome}",
        f"runtime_s: {round(runtime_s, 3)}",
    ]
    if traceback:
        tb = traceback[:_MAX_TRACEBACK_BYTES]
        indented = "\n".join("  " + line for line in tb.splitlines())
        lines.append(f"traceback: |")
        lines.append(indented)
    lines.append("```")
    lines.append("")

    entry_text = "\n".join(lines) + "\n"

    # Append to the file (never rewrite)
    with devlog_path.open("a", encoding="utf-8") as fh:
        fh.write(entry_text)


def _yaml_str_list(items: list[str]) -> str:
    """Format a list of strings as inline YAML list."""
    if not items:
        return "[]"
    inner = ", ".join(f"{_yaml_str(s)}" for s in items)
    return f"[{inner}]"


def _yaml_str(s: str) -> str:
    """Minimally quote a string for inline YAML."""
    # Use double quotes if the string contains special chars
    if any(c in s for c in ('"', "'", ":", "{", "}", "[", "]", ",", "#", "&", "*", "?")):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s
