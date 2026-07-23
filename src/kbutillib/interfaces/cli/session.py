"""``kbu session`` — session save/list/show for kbu subprojects.

Writes local YAML session records under ``subprojects/<name>/sessions/``.
When AIAssistant is detected AND a project binding exists in kbu-project.toml,
also emits a JSON drop-file to ``<aia_root>/state/session-inbox/`` for the
platform ingester to pick up.  No direct ``assistant.*`` import; the old
``_route_save_aia`` path (direct SQLite write + auto-register) has been removed.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import click
import yaml

from .manifest import append_session_ref, now_utc_iso
from .subproject import _find_project_root


# ── env-var detection ──────────────────────────────────────────────────────

_DEFAULT_AIA_PATHS = (
    "~/Dropbox/Projects/AIAssistant/state/sessions.db"
    ":~/Projects/AIAssistant/state/sessions.db"
)


def _detect_aiassistant() -> Optional[Path]:
    """Return the AIAssistant repo root for the first existing sessions.db.

    Reads the colon-separated env var ``KBU_AIA_PATHS`` (defaults to the
    two common Dropbox/projects locations).  Returns the parent of the
    ``state/`` directory (i.e. the repo root) for the first path whose
    ``sessions.db`` actually exists, or ``None`` if none are found.
    """
    import os

    raw = os.environ.get("KBU_AIA_PATHS", _DEFAULT_AIA_PATHS)
    for entry in raw.split(":"):
        entry = entry.strip()
        if not entry:
            continue
        p = Path(entry).expanduser()
        if p.exists():
            # parent of state/sessions.db is state/, parent of that is repo root
            return p.parent.parent
    return None


# ── session-id generation ──────────────────────────────────────────────────


def _new_session_id() -> str:
    """Return an 8-character hex session id derived from uuid4."""
    return uuid.uuid4().hex[:8]


# ── timestamp for local YAML filenames ────────────────────────────────────


def _utc_timestamp_for_filename() -> str:
    """Return a UTC timestamp string safe for use in filenames.

    Example: ``"20260604T153000Z"``
    """
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── local YAML route ───────────────────────────────────────────────────────


def _route_save_local(payload: dict[str, Any], subproject: str) -> str:
    """Write *payload* as a YAML file in ``subprojects/<subproject>/sessions/``.

    Creates the directory if it does not exist.  Returns the session_id.
    """
    project_root = _find_project_root(Path.cwd())
    sessions_dir = project_root / "subprojects" / subproject / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    skill = payload.get("command", "session")
    ts = _utc_timestamp_for_filename()
    sid = payload.get("session_id", _new_session_id())
    filename = f"{ts}-{skill}-{sid}.yaml"
    session_file = sessions_dir / filename

    with open(session_file, "w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)

    return payload["session_id"]


# ── Click group ────────────────────────────────────────────────────────────


@click.group(name="session")
def session_cmd() -> None:
    """Save, list, and show kbu sessions."""


# ── save ───────────────────────────────────────────────────────────────────


@session_cmd.command(name="save")
@click.option("--skill", required=True, help="Skill name (e.g. kbu-plan).")
@click.option("--subproject", required=True, help="Subproject name.")
@click.option("--summary", required=True, help="One-sentence session summary.")
@click.option("--topics", default="", help="Topics discussed (free text).")
@click.option("--decisions", default="", help="Decisions made (free text).")
@click.option("--next-steps", "next_steps", default="", help="Next steps (free text).")
@click.option("--work-completed", "work_completed", default="", help="Work completed (free text).")
@click.option(
    "--json",
    "input_json",
    default=None,
    metavar="-",
    help="Read payload from stdin as JSON (use ``-`` to indicate stdin).",
)
@click.pass_context
def save_cmd(  # noqa: PLR0913
    ctx: click.Context,
    skill: str,
    subproject: str,
    summary: str,
    topics: str,
    decisions: str,
    next_steps: str,
    work_completed: str,
    input_json: Optional[str],
) -> None:
    """Save a session record for SUBPROJECT."""
    now = now_utc_iso()

    if input_json == "-":
        try:
            raw = sys.stdin.read()
            extra = json.loads(raw)
        except json.JSONDecodeError as exc:
            click.echo(f"Error: invalid JSON from stdin: {exc}", err=True)
            ctx.exit(1)
            return
    else:
        extra = {}

    session_id = _new_session_id()

    # Build the canonical payload.
    payload: dict[str, Any] = {
        "session_id": session_id,
        "command": skill,
        "summary": summary,
        "started_at": extra.get("started_at", now),
        "ended_at": extra.get("ended_at", now),
        "topics_discussed": _split_or_empty(topics or extra.get("topics_discussed", "")),
        "decisions_made": _split_or_empty(decisions or extra.get("decisions_made", "")),
        "work_submitted": _split_or_empty(
            work_completed or extra.get("work_submitted", "")
        ),
        "next_steps": _split_or_empty(
            next_steps or extra.get("next_steps", "")
        ),
    }
    # Merge any remaining extra keys (non-fatal additions).
    for k, v in extra.items():
        if k not in payload:
            payload[k] = v

    # Always write the local YAML record.
    session_id = _route_save_local(payload, subproject)

    # Conditionally emit a drop-file when AIAssistant is present AND a binding
    # exists.  No auto-derivation, no auto-registration, no direct DB import.
    project_root = _find_project_root(Path.cwd())
    aia_root = _detect_aiassistant()

    if aia_root is not None:
        from .binding import resolve_binding  # noqa: PLC0415
        bound_id = resolve_binding(project_root)

        if bound_id is not None:
            # Augment payload with bound project info and emit the drop-file.
            from .binding import set_binding as _sb  # noqa: PLC0415, F401
            try:
                from .manifest import read_project_manifest  # noqa: PLC0415
                mdata = read_project_manifest(project_root)
                aia_data = mdata.get("aiassistant", {})
                bound_name = aia_data.get("project_name") or bound_id
            except Exception:
                bound_name = bound_id

            drop_payload = dict(payload)
            drop_payload["project_id"] = bound_id
            drop_payload["project_name"] = bound_name

            try:
                from .dropfile_emitter import emit_session_dropfile  # noqa: PLC0415
                emit_session_dropfile(aia_root, drop_payload)
            except Exception as exc:
                # Drop-file failure is non-fatal; local YAML record is already written.
                click.echo(
                    f"Warning: could not emit session drop-file: {exc}",
                    err=True,
                )

    # Always append a lightweight ref to the subproject manifest.
    try:
        append_session_ref(
            project_root,
            subproject,
            {
                "id": session_id,
                "skill": skill,
                "at": now,
                "summary": summary,
            },
        )
    except FileNotFoundError:
        # Subproject manifest doesn't exist yet — non-fatal, still saved.
        click.echo(
            f"Warning: could not update subproject manifest for '{subproject}' "
            "(manifest not found). Session was saved.",
            err=True,
        )

    click.echo(session_id)


def _split_or_empty(value: Any) -> list[str]:
    """Return *value* as a list of non-empty strings.

    If *value* is already a list, return it filtered.  If it is a string,
    split on ``\\n`` and filter empty lines.
    """
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    return []


# ── list ───────────────────────────────────────────────────────────────────


@session_cmd.command(name="list")
@click.option("--subproject", default=None, help="Filter by subproject name.")
@click.option("--limit", default=20, show_default=True, help="Maximum rows to show.")
@click.option("--json", "output_json", is_flag=True, default=False,
              help="Output full payloads as JSON array.")
@click.pass_context
def list_cmd(
    ctx: click.Context,
    subproject: Optional[str],
    limit: int,
    output_json: bool,
) -> None:
    """List recent sessions, newest first."""
    project_root = _find_project_root(Path.cwd())
    sessions = _collect_local_sessions(project_root, subproject, limit)

    if output_json:
        click.echo(json.dumps(sessions))
        return

    # TSV output
    click.echo("id\tat\tsubproject\tskill\tsummary")
    for s in sessions:
        sid = s.get("session_id", "")
        at = s.get("started_at", s.get("ended_at", ""))
        sp = s.get("_subproject", "")
        skill = s.get("command", "")
        raw_summary = s.get("summary", "")
        # Collapse tabs/newlines to single space
        summary = re.sub(r"[\t\n\r]+", " ", raw_summary).strip()
        # Truncate to 120 chars
        if len(summary) > 120:
            summary = summary[:119] + "…"
        click.echo(f"{sid}\t{at}\t{sp}\t{skill}\t{summary}")


def _collect_local_sessions(
    project_root: Path,
    subproject_filter: Optional[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Collect local YAML sessions from all (or one) subproject directories.

    Returns a list of payload dicts, newest-first by ``started_at``, capped
    at *limit*.  Each dict gains a ``_subproject`` key with the subproject
    name so callers can display it.
    """
    sp_root = project_root / "subprojects"
    if not sp_root.is_dir():
        return []

    if subproject_filter:
        candidates = [sp_root / subproject_filter]
    else:
        candidates = sorted(sp_root.iterdir())

    records: list[tuple[str, str, dict]] = []  # (at, sp_name, payload)

    for sp_dir in candidates:
        if not sp_dir.is_dir():
            continue
        sp_name = sp_dir.name
        sessions_dir = sp_dir / "sessions"
        if not sessions_dir.is_dir():
            continue
        for yf in sessions_dir.glob("*.yaml"):
            try:
                with open(yf, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            at = data.get("started_at", data.get("ended_at", ""))
            data["_subproject"] = sp_name
            records.append((at, sp_name, data))

    # Sort newest-first by the at-string (ISO-8601 lexicographic order works).
    records.sort(key=lambda r: r[0], reverse=True)
    return [r[2] for r in records[:limit]]


# ── show ───────────────────────────────────────────────────────────────────


@session_cmd.command(name="show")
@click.argument("session_id")
@click.pass_context
def show_cmd(ctx: click.Context, session_id: str) -> None:
    """Show the full record for SESSION_ID."""
    project_root = _find_project_root(Path.cwd())
    sp_root = project_root / "subprojects"

    if sp_root.is_dir():
        for sp_dir in sp_root.iterdir():
            if not sp_dir.is_dir():
                continue
            sessions_dir = sp_dir / "sessions"
            if not sessions_dir.is_dir():
                continue
            for yf in sessions_dir.glob("*.yaml"):
                try:
                    with open(yf, encoding="utf-8") as fh:
                        data = yaml.safe_load(fh) or {}
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("session_id") == session_id:
                    click.echo(yaml.dump(data, allow_unicode=True, sort_keys=False))
                    return

    click.echo(f"Session '{session_id}' not found.", err=True)
    ctx.exit(1)
