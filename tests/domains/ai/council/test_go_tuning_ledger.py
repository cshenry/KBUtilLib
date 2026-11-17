"""The tuning run's append-only ledger.

Nothing here makes a network call, spends a token or needs a GPU: the ledger
is a JSONL file and a lock file under ``tmp_path``.

The two properties worth testing are the two the ledger exists for -- a
record is durable before ``append`` returns, and a read survives the one kind
of damage a killed process can do.  The ``fsync`` itself cannot be observed
from inside the process that called it, so what is pinned here is the
observable half: the record is on disk and readable by a SECOND ``Ledger``
object opened on the same directory, which is what a restart is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kbutillib.domains.ai.council.go_tuning.ledger import (
    LEDGER_KINDS,
    Ledger,
    LedgerError,
)


def test_every_record_carries_kind_ts_code_and_invocation(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "run", invocation_id="inv-1")
    record = ledger.append("tranche", manifest_id="m1", submission_token="t0")

    assert record["kind"] == "tranche"
    assert record["ts"].endswith("Z")
    assert "package_version" in record["code"]
    assert record["invocation_id"] == "inv-1"
    assert record["seq"] == 0
    assert record["manifest_id"] == "m1"
    assert record["submission_token"] == "t0"


def test_a_record_is_readable_by_a_second_ledger_on_the_same_dir(tmp_path: Path) -> None:
    """What durability looks like from outside: a restart can read it."""
    Ledger(tmp_path / "run", invocation_id="inv-1").append("manifest", manifest_id="m1")
    restart = Ledger(tmp_path / "run", invocation_id="inv-2")

    records = restart.records()
    assert [r["kind"] for r in records] == ["manifest"]
    assert records[0]["invocation_id"] == "inv-1", "the WRITER's invocation, not the reader's"


def test_records_filters_by_kind_and_keeps_file_order(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "run")
    ledger.append("extract", rows=10)
    ledger.append("tranche", tranche_index=0)
    ledger.append("calibration", band="trusted")
    ledger.append("tranche", tranche_index=1)

    assert [r["kind"] for r in ledger.records()] == [
        "extract",
        "tranche",
        "calibration",
        "tranche",
    ]
    assert [r["tranche_index"] for r in ledger.records("tranche")] == [0, 1]
    assert [r["seq"] for r in ledger.records()] == [0, 1, 2, 3]
    assert ledger.last("calibration")["band"] == "trusted"


def test_an_unknown_kind_is_refused_rather_than_written(tmp_path: Path) -> None:
    """A typo'd kind would be fsync-ed and then never found by the reader."""
    ledger = Ledger(tmp_path / "run")
    with pytest.raises(ValueError, match="not a ledger record kind"):
        ledger.append("tranch", tranche_index=0)
    assert ledger.records() == []
    with pytest.raises(ValueError, match="not a ledger record kind"):
        ledger.records("tranch")


#: ``kind`` is deliberately absent: it is a POSITIONAL parameter of
#: ``append``, so ``append("tranche", kind="mine")`` is a ``TypeError`` from
#: Python itself and never reaches the guard.
@pytest.mark.parametrize("field", ["ts", "code", "invocation_id", "seq"])
def test_a_caller_may_not_shadow_the_fields_the_ledger_owns(
    tmp_path: Path, field: str
) -> None:
    ledger = Ledger(tmp_path / "run")
    with pytest.raises(ValueError, match="the ledger owns those fields"):
        ledger.append("tranche", **{field: "mine"})


def test_a_truncated_last_line_is_reported_and_ignored(tmp_path: Path) -> None:
    """The only damage a kill can do, and the one a restart must read past."""
    ledger = Ledger(tmp_path / "run")
    ledger.append("manifest", manifest_id="m1")
    ledger.append("tranche", tranche_index=0)
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "tranche", "tranche_ind')

    records = ledger.records()
    assert [r["kind"] for r in records] == ["manifest", "tranche"]
    assert ledger.truncated is not None
    assert ledger.truncated["line_no"] == 3

    # And the next append lands AFTER it rather than overwriting it, so the
    # damaged line stays visible in the file.
    written = ledger.append("tranche", tranche_index=1)
    assert written["seq"] == 3
    assert [r["kind"] for r in ledger.records()] == ["manifest", "tranche", "tranche"]


def test_an_older_truncation_stays_readable_after_later_appends(
    tmp_path: Path,
) -> None:
    """The damage must not brick the ledger from the second append onwards."""
    ledger = Ledger(tmp_path / "run")
    ledger.append("manifest", manifest_id="m1")
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "tranche", "tranche_ind')
    ledger.append("tranche", tranche_index=0)
    ledger.append("tranche", tranche_index=1)

    records = ledger.records()
    assert [r["kind"] for r in records] == ["manifest", "tranche", "tranche"]
    assert [r["tranche_index"] for r in ledger.records("tranche")] == [0, 1]
    # No longer the LAST line, so not `truncated` -- but still reported.
    assert ledger.truncated is None
    assert [d["seq"] for d in ledger.damaged] == [1]


def test_a_missing_record_raises_rather_than_being_read_past(tmp_path: Path) -> None:
    """``seq`` is what tells a hole from a scratch: a hole is not readable."""
    ledger = Ledger(tmp_path / "run")
    ledger.append("manifest", manifest_id="m1")
    ledger.append("tranche", tranche_index=0)
    ledger.append("tranche", tranche_index=1)
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    # Somebody removed the middle record and rewrote the file.
    ledger.path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    with pytest.raises(LedgerError, match="record\\(s\\) before it are missing"):
        ledger.records()


def test_a_record_is_one_line_of_sorted_key_json(tmp_path: Path) -> None:
    """One record per line is what makes a partial write identifiable."""
    ledger = Ledger(tmp_path / "run")
    ledger.append("score", metrics={"accuracy": 0.9, "n": 150})
    lines = ledger.path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 1
    assert json.loads(lines[0])["metrics"] == {"accuracy": 0.9, "n": 150}
    assert lines[0] == json.dumps(
        json.loads(lines[0]), sort_keys=True, separators=(",", ":")
    )


def test_reading_a_ledger_that_does_not_exist_yet_is_empty_not_an_error(
    tmp_path: Path,
) -> None:
    assert Ledger(tmp_path / "unwritten").records() == []
    assert Ledger(tmp_path / "unwritten").last() is None


def test_the_lock_file_is_beside_the_ledger_and_not_the_ledger(tmp_path: Path) -> None:
    """``flock`` on the appended file would be dropped by any other handle."""
    ledger = Ledger(tmp_path / "run")
    ledger.append("stop", reason="done")
    assert ledger.lock_path != ledger.path
    assert ledger.lock_path.parent == ledger.path.parent


def test_two_ledger_objects_appending_interleaved_keep_every_record(
    tmp_path: Path,
) -> None:
    """Two writers on one run directory -- the case the lock file is for."""
    first = Ledger(tmp_path / "run", invocation_id="a")
    second = Ledger(tmp_path / "run", invocation_id="b")
    for i in range(5):
        first.append("tranche", tranche_index=i, who="a")
        second.append("tranche", tranche_index=i, who="b")

    records = first.records("tranche")
    assert len(records) == 10
    assert [r["seq"] for r in records] == list(range(10))
    assert {r["invocation_id"] for r in records} == {"a", "b"}


def test_the_kind_vocabulary_is_the_run_s_steps() -> None:
    """Pinned: step N+1 looks for step N by kind, so a rename is a break."""
    assert LEDGER_KINDS == (
        "extract",
        "manifest",
        "tranche",
        "calibration",
        "candidate",
        "score",
        "stop",
    )


def test_a_write_that_cannot_land_raises(tmp_path: Path, monkeypatch: Any) -> None:
    """A failed write must never look like a recorded one."""
    ledger = Ledger(tmp_path / "run")
    ledger.path.parent.chmod(0o500)
    try:
        with pytest.raises(LedgerError, match="cannot append"):
            ledger.append("tranche", tranche_index=0)
    finally:
        ledger.path.parent.chmod(0o700)
