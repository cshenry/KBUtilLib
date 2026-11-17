"""The tuning run's APPEND-ONLY LEDGER: what was done, in what order, by whom.

A tuning pass is a sequence of expensive, partly-manual steps spread over
days and several processes -- extract, sample, submit a tranche, import
calibration marks, score a candidate, stop.  The store records the COUNCIL's
work (jobs, calls, verdicts) and the manifest records the SAMPLE, but neither
records the RUN: which tranche was started when, under which prompt version,
against which store, what it cost, and why the run stopped.  That is this
file, and it is the only artefact from which a restart can tell what the
previous invocation had already set in motion.

WHY APPEND-ONLY, AND WHY ``fsync``
==================================
The one question the ledger has to answer after a crash is "did I already
submit that tranche?", and a ledger that lost its last line cannot answer it:
the job exists in the store, nothing in the ledger mentions it, and a resume
submits it a second time and pays twice.  So every record is flushed and
``fsync``-ed before :meth:`Ledger.append` returns, and a record is NEVER
rewritten -- a later record supersedes an earlier one by being later.  The
reconciliation that closes the remaining window (the crash BETWEEN the
``fsync`` and the submit, or between the submit and its follow-up record) is
:mod:`.adjudicate`'s, and it works by matching the submission token this file
persisted BEFORE the submit against the jobs in the store.

WHAT EACH KIND CARRIES
======================
Every record carries ``kind``, ``ts`` (UTC ISO-8601), ``code`` (the
``code_version()`` of the writing build, so the commit is on every line),
``invocation_id`` and ``seq``.  Beyond those the fields are the kind's, and
they are NOT validated here -- the ledger is a journal, and a step that
learns to record one more number must not need a schema change to say so.
What the steps write today, for a reader looking for a field:

==============  ==========================================================
``extract``     the two stores, the job ids read, the matched/excluded counts
``manifest``    ``manifest_id``, the store, the query ``name@version``, the
                adjudicate spec hash, the profile and roster fingerprint,
                the item count, the tranche size and the token ceiling
``tranche``     ``phase`` (``planned`` -> ``submitted`` -> ``complete``, plus
                ``reconciled``/``retry_*``), ``submission_token``,
                ``tranche_index``, the planned and submitted item ids, the
                ``spot_check_ids`` and their band, ``job_id`` and ``usage``
``calibration`` ``manifest_id``, ``adjudicate_spec_hash``,
                ``roster_fingerprint``, ``band`` and the imported ``marks``
``candidate``   a candidate prompt's own ``name@version`` and the PARENT
                version it was derived from
``score``       the ``metrics`` a candidate scored, and over which pool
``stop``        ``reason``, ``detail``, the usage at the time and, when the
                stop is about one tranche, its ``submission_token`` and
                ``tranche_index``
==============  ==========================================================

WHY A LOCK FILE
===============
Two processes appending to one JSONL can interleave a partial line, and the
reader cannot tell an interleaving from a truncation.  Writers therefore
serialise on ``ledger.lock`` with a blocking ``flock`` -- blocking rather than
non-blocking because an append is a few hundred bytes and the honest answer to
"somebody else is appending" is to wait, not to fail a submission.

A DAMAGED LINE IS REPORTED AND SKIPPED; A MISSING RECORD IS AN ERROR
===================================================================
A process killed mid-write damages the line it was writing, and refusing to
read the whole ledger because of it would strand exactly the restart the
ledger exists to serve.  So :meth:`Ledger.records` skips an unparsable line
and reports it on :attr:`Ledger.damaged` (and, when it is the last line, on
:attr:`Ledger.truncated`), and :meth:`Ledger.append` terminates an
unterminated final line before adding to the file -- appending straight onto
it would splice the new record into the damaged one and lose BOTH, which is
the one way a reported truncation could turn into a silent gap.

Note the consequence: once a later record has been appended, yesterday's
truncated line is no longer the LAST line, and a rule that only tolerated
damage in final position would make the ledger permanently unreadable from
the second append onwards.  That is strictly worse than the damage.

What must still be an error is a MISSING record, which is a different thing
from a damaged one: ``seq`` is each record's position among the file's
non-empty lines, assigned under the write lock, so a record whose ``seq``
does not match where it sits proves the file was rewritten and that records
before it are gone.  That raises.  Reading past it would hide exactly the
tranche a resume needs to know about.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..code_version import code_version

__all__ = [
    "LEDGER_FILE",
    "LEDGER_KINDS",
    "LOCK_FILE",
    "Ledger",
    "LedgerError",
]

#: The record kinds a ledger accepts, in the order a run produces them.
#:
#: A CLOSED set on purpose: the ledger is read by step N+1 to decide whether
#: step N happened, and a typo'd ``"tranch"`` would be written, fsync-ed and
#: then never found by the reader looking for ``"tranche"`` -- a silent
#: double-submit.  :meth:`Ledger.append` refuses anything not listed here.
LEDGER_KINDS = (
    "extract",
    "manifest",
    "tranche",
    "calibration",
    "candidate",
    "score",
    "stop",
)

#: The ledger file's name inside the run directory.
LEDGER_FILE = "ledger.jsonl"

#: The lock writers serialise on.  Beside the ledger, never the ledger
#: itself: ``flock`` on the file being appended to would be released by any
#: other handle on it closing.
LOCK_FILE = "ledger.lock"

#: Record fields this class owns.  A caller's ``**fields`` may not shadow
#: them -- a record whose ``ts`` was supplied by the caller would make the
#: ledger's one ordering guarantee unverifiable.
_RESERVED = ("kind", "ts", "code", "invocation_id", "seq")


class LedgerError(RuntimeError):
    """A ledger record could not be written, or the ledger cannot be read."""


def _utc_iso() -> str:
    """Now, as an ISO-8601 UTC string with a ``Z`` suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _canonical(payload: Mapping[str, Any]) -> str:
    """One record as ONE line: sorted keys, no embedded newline."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class Ledger:
    """The append-only record of one tuning run, at ``run_dir/ledger.jsonl``.

    Args:
        run_dir: the run directory.  Created if it does not exist; the
            ledger and its lock file live directly inside it.
        invocation_id: an id for THIS process's pass over the run.  Stamped
            on every record this object writes, which is what makes the
            token ceiling ("this invocation's completed usage") and the
            crash reconciliation ("a token from a PREVIOUS invocation")
            answerable from the file alone.  Generated when not given.
        code: the ``code_version()`` payload to stamp, for a caller that
            already has one.  Read once per object, not per record: a run
            cannot change build mid-append, and shelling out to git on every
            ledger line would make an append cost a subprocess.
    """

    def __init__(
        self,
        run_dir: Any,
        invocation_id: Optional[str] = None,
        code: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LedgerError(f"cannot create run directory {self.run_dir}: {exc}") from exc
        self.path = self.run_dir / LEDGER_FILE
        self.lock_path = self.run_dir / LOCK_FILE
        self.invocation_id = str(invocation_id or uuid.uuid4().hex)
        self.code: Dict[str, Any] = dict(code) if code is not None else dict(code_version())
        #: Set by :meth:`records` when the file's LAST non-empty line was
        #: unparsable: ``{"line_no", "bytes", "detail"}``, else ``None``.
        #: That is the shape a kill leaves, and the one C22 names.
        self.truncated: Optional[Dict[str, Any]] = None
        #: Every unparsable line :meth:`records` skipped, newest last.  A
        #: superset of :attr:`truncated`: an older truncation stays here
        #: after later appends have pushed it out of final position.
        self.damaged: List[Dict[str, Any]] = []

    # -- writing ----------------------------------------------------------

    def append(self, kind: str, **fields: Any) -> Dict[str, Any]:
        """Append one record and return exactly what was written.

        The record is ``{kind, ts, code, invocation_id, seq}`` plus *fields*,
        serialised as one line with sorted keys, written under the lock, and
        flushed and ``fsync``-ed before this returns.  ``seq`` is the
        record's 0-based position in the file, assigned under the same lock
        that appends it, so two processes cannot be handed the same one.

        Raises:
            ValueError: *kind* is not in :data:`LEDGER_KINDS`, or *fields*
                shadows a field this class owns.
            LedgerError: the write did not land.  Never swallowed: a caller
                that was told its tranche was recorded and then submits is
                exactly the case the fsync exists to prevent.
        """
        if kind not in LEDGER_KINDS:
            raise ValueError(
                f"{kind!r} is not a ledger record kind; expected one of "
                f"{', '.join(LEDGER_KINDS)}"
            )
        clashing = sorted(set(fields) & set(_RESERVED))
        if clashing:
            raise ValueError(
                f"a {kind!r} record may not set {', '.join(clashing)}: the ledger "
                "owns those fields"
            )
        record: Dict[str, Any] = {
            "kind": kind,
            "ts": _utc_iso(),
            "code": dict(self.code),
            "invocation_id": self.invocation_id,
        }
        record.update(fields)
        try:
            with self._locked():
                record["seq"] = self._count()
                # A newline FIRST when the file does not end with one.  The
                # file can only lack a trailing newline because a previous
                # writer was killed mid-line, and appending straight onto
                # that line would splice this record into the damaged one --
                # turning a reported, ignorable truncation into a silently
                # LOST record.  Terminating it instead leaves the damage on
                # its own line, where ``records`` can report and skip it.
                prefix = "\n" if self._unterminated() else ""
                line = prefix + _canonical(record) + "\n"
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise LedgerError(f"cannot append to {self.path}: {exc}") from exc
        return record

    # -- reading ----------------------------------------------------------

    def records(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every record, in file order, optionally of one *kind* only.

        An unparsable line is skipped and reported on :attr:`damaged` (and
        on :attr:`truncated` when it is the last one).  A record whose
        ``seq`` does not match its position raises: that is a MISSING
        record, not a damaged one.  See the module docstring.

        Raises:
            ValueError: *kind* is not in :data:`LEDGER_KINDS`.
            LedgerError: the file cannot be read, or a record is missing.
        """
        if kind is not None and kind not in LEDGER_KINDS:
            raise ValueError(
                f"{kind!r} is not a ledger record kind; expected one of "
                f"{', '.join(LEDGER_KINDS)}"
            )
        self.truncated = None
        self.damaged = []
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerError(f"cannot read {self.path}: {exc}") from exc
        out: List[Dict[str, Any]] = []
        position = -1
        last_damaged_position = -2
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            position += 1
            try:
                payload = json.loads(line)
            except ValueError as exc:
                self.damaged.append(
                    {
                        "line_no": number,
                        "seq": position,
                        "bytes": len(line),
                        "detail": str(exc),
                    }
                )
                last_damaged_position = position
                continue
            if not isinstance(payload, dict):
                self.damaged.append(
                    {
                        "line_no": number,
                        "seq": position,
                        "bytes": len(line),
                        "detail": f"a {type(payload).__name__}, not a record",
                    }
                )
                last_damaged_position = position
                continue
            recorded = payload.get("seq")
            if recorded is not None and int(recorded) != position:
                raise LedgerError(
                    f"{self.path}:{number} records seq {recorded} but sits at "
                    f"position {position}: the file has been rewritten and "
                    f"{position - int(recorded)} record(s) before it are missing. "
                    "A resume cannot be trusted to this ledger"
                )
            out.append(payload)
        if last_damaged_position == position:
            self.truncated = dict(self.damaged[-1])
        if kind is not None:
            out = [r for r in out if r.get("kind") == kind]
        return out

    def last(self, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The newest record, or the newest of *kind*.  ``None`` if there is none."""
        records = self.records(kind)
        return records[-1] if records else None

    # -- internals --------------------------------------------------------

    def _locked(self) -> Any:
        """A context manager holding the writers' lock, blocking."""
        return _FileLock(self.lock_path)

    def _unterminated(self) -> bool:
        """Does the file end mid-line?  Called under the lock."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return False
        with self.path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"

    def _count(self) -> int:
        """How many records the file already holds.  Called under the lock.

        Counts NON-EMPTY lines rather than parsing them: ``seq`` is a
        position, and a truncated last line still occupies one.
        """
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())


class _FileLock:
    """A blocking exclusive ``flock`` on one path, as a context manager."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "_FileLock":
        self._handle = self.path.open("a+", encoding="utf-8")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: Any) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
