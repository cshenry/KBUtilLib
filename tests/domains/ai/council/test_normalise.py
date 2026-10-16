"""Answer normalisation: the two measured reply defects, and only those two.

Every test here drives replies through the REAL ``CouncilWorker`` into a REAL
``SqliteStore`` opened with ``open_store`` on a fresh ``tmp_path`` file. The
BACKEND is the only substitute (the shipped ``FakeBackend``, scripted).
``normalise()``, ``_validate_answer``, ``decide()`` and the store are not
monkeypatched, stubbed or subclassed anywhere in this module, and
:class:`TestTheRepairIsInTheStore` reads the repair record back out of the
database row rather than off an in-memory object.

No network call, no token, no CLI login, no GPU.

WHY THESE TWO REPAIRS EXIST
===========================
They are a measurement, not a policy. Replaying Jose Faria's recorded V3 GO
council through this worker reproduced his decisions on 91 of 99 scored rows;
all eight shortfalls were replies the answer schema refused over spelling
rather than substance -- seven meta replies that omitted
``no_functional_content`` while naming a non-empty final id list, and one
proposer reply with ``"notes": null``. See
``agent-io/prds/kbu-ai-council-v1/research/go-replay-parity.md``.

:class:`TestEveryOtherSchemaFailureStillFails` is the other half of the
contract and the reason this module is worth as much as the repairs are: a
reply missing a DIFFERENT required field is still ``invalid_output``.
"""

from __future__ import annotations

import importlib.util
import json
from typing import Any, Dict, List, Mapping, Optional

import pytest

from kbutillib.domains.ai.council.engine import CouncilWorker
from kbutillib.domains.ai.council.engine.normalise import (
    DERIVED_CONTENT_FLAG,
    NULL_STRING,
    normalise,
)
from kbutillib.domains.ai.council.types import CouncilVerdict

from .engine_support import (
    InstrumentedBackend,
    critic_answer,
    endpoint_config,
    make_query,
    make_roster,
    make_task,
    proposer_answer,
    run,
    scripted,
)

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("sqlalchemy") is None,
    reason="the council store needs the 'council' extra (pip install -e '.[council]')",
)

#: Same test-fast timings the worker tests use.
WORKER_DEFAULTS: Dict[str, Any] = {
    "heartbeat_interval_s": 0.0,
    "poll_interval_s": 0.01,
    "retry_backoff_s": (0.02, 0.05),
    "retry_jitter": 0.0,
    "idle_grace_s": 0.3,
    "max_idle_s": 15.0,
}


# ---------------------------------------------------------------------------
# A real store, a real worker, a scripted backend
# ---------------------------------------------------------------------------


def open_real_store(tmp_path: Any, name: str = "council") -> Any:
    """A real ``SqliteStore`` on a new file, through the shipped ``open_store``."""
    from kbutillib.domains.ai.council.store import open_store

    store = open_store(f"sqlite:///{tmp_path}/{name}/council.sqlite")
    store.init_schema()
    return store


def worker_for(store: Any, backends: Mapping[str, Any], **kwargs: Any) -> CouncilWorker:
    settings = dict(WORKER_DEFAULTS)
    settings.update(kwargs)
    return CouncilWorker(
        store,
        endpoint_config(**{name: {} for name in backends}),
        backends,
        "alice",
        "w-1",
        **settings,
    )


def fake(script: Any, **kwargs: Any) -> InstrumentedBackend:
    return InstrumentedBackend(scripted(script), **kwargs)


def settle(
    store: Any,
    script: Mapping[Any, Any],
    *,
    n_proposers: int = 3,
    task: Optional[Any] = None,
    worker: Optional[CouncilWorker] = None,
    label: str = "normalise",
) -> str:
    """Submit one item against *script* and work it to a verdict; return job_id.

    *worker* is passed in when a test needs TWO jobs to share one worker --
    which is how store reuse is reached, since the second job must find the
    first job's answer rather than its own backend.
    """
    worker = worker or worker_for(store, {"fake1": fake(script)})
    job_id = worker.submit(
        make_query(), [task or make_task()], make_roster(n_proposers), label=label
    )
    run(worker.work(job_id=job_id, until_idle=True))
    return job_id


def calls_of(store: Any, job_id: str, role: Optional[str] = None) -> List[Dict[str, Any]]:
    return [
        row
        for row in store.get_calls(job_id)
        if role is None or row["role"] == role
    ]


def verdict_of(store: Any, job_id: str, item_id: str = "item-1") -> CouncilVerdict:
    row = next(r for r in store.get_items(job_id) if r["item_id"] == item_id)
    assert row["verdict"] is not None, f"item {item_id} is {row['status']}, not decided"
    return CouncilVerdict.model_validate(row["verdict"])


def null_notes_proposer(*ids: str) -> Dict[str, Any]:
    """A proposer reply whose ``notes`` is null -- gemini35flash, row 11938."""
    reply = proposer_answer(*ids)
    reply["notes"] = None
    return reply


def meta_without_the_flag(*ids: str) -> Dict[str, Any]:
    """A meta reply that omits ``no_functional_content`` -- claudeopus45."""
    return {
        "final_ids": list(ids),
        "final_confidence": "medium",
        "reasoning": "adjudicated",
    }


def split_script(**overrides: Any) -> Dict[Any, Any]:
    """A vote table of ``{A:3, B:2}``: not unanimous, so meta runs."""
    script: Dict[Any, Any] = {
        ("proposer", "proposer_1"): proposer_answer("A:1", "B:2"),
        ("proposer", "proposer_2"): proposer_answer("A:1", "B:2"),
        ("proposer", "proposer_3"): proposer_answer("A:1"),
        "critic": critic_answer(),
        "meta": meta_without_the_flag("A:1"),
    }
    script.update(overrides)
    return script


# ---------------------------------------------------------------------------
# Rule 1: a schema-declared string that arrived as null
# ---------------------------------------------------------------------------


class TestNullString:
    """``null`` in a string field is repaired to ``""`` and the call settles."""

    def test_a_null_notes_settles_the_call_ok(self, tmp_path: Any) -> None:
        store = open_real_store(tmp_path)
        job_id = settle(
            store,
            {
                "proposer": null_notes_proposer("A:1"),
                "critic": critic_answer(verdict="strongly_consistent"),
            },
        )

        rows = calls_of(store, job_id, "proposer")
        assert [row["status"] for row in rows] == ["ok", "ok", "ok"]
        assert all(row["error"] is None for row in rows)
        verdict = verdict_of(store, job_id)
        assert verdict.status == "ok"
        assert verdict.selected_ids == ["A:1"]

    def test_the_repair_is_recorded_on_the_call(self, tmp_path: Any) -> None:
        store = open_real_store(tmp_path)
        job_id = settle(
            store,
            {
                "proposer": null_notes_proposer("A:1"),
                "critic": critic_answer(verdict="strongly_consistent"),
            },
        )

        for row in calls_of(store, job_id, "proposer"):
            assert row["normalised"] == [
                {
                    "field": "notes",
                    "rule": NULL_STRING,
                    "original": None,
                    "value": "",
                }
            ]

    def test_the_stored_answer_is_repaired_and_the_raw_text_is_not(
        self, tmp_path: Any
    ) -> None:
        """The two columns hold both: what the model said, and what was read."""
        store = open_real_store(tmp_path)
        job_id = settle(
            store,
            {
                "proposer": null_notes_proposer("A:1"),
                "critic": critic_answer(verdict="strongly_consistent"),
            },
        )

        row = calls_of(store, job_id, "proposer")[0]
        assert row["reply_parsed"]["notes"] == ""
        # The raw text is the model's own bytes, null and all.
        assert json.loads(row["reply_text"])["notes"] is None
        assert '"notes": null' in row["reply_text"]

    def test_a_null_why_inside_picked_is_repaired_too(self, tmp_path: Any) -> None:
        """``picked[].why`` is typed by the array's ``items`` schema."""
        store = open_real_store(tmp_path)
        reply = proposer_answer("A:1")
        reply["picked"][0]["why"] = None
        job_id = settle(
            store,
            {"proposer": reply, "critic": critic_answer(verdict="consistent")},
        )

        row = calls_of(store, job_id, "proposer")[0]
        assert row["reply_parsed"]["picked"][0]["why"] == ""
        assert row["normalised"] == [
            {
                "field": "picked[0].why",
                "rule": NULL_STRING,
                "original": None,
                "value": "",
            }
        ]
        assert verdict_of(store, job_id).status == "ok"

    def test_an_absent_optional_string_is_left_absent(self, tmp_path: Any) -> None:
        """"Said nothing" is not "said the empty string"."""
        store = open_real_store(tmp_path)
        reply = proposer_answer("A:1")
        reply.pop("notes")
        job_id = settle(
            store,
            {"proposer": reply, "critic": critic_answer(verdict="consistent")},
        )

        row = calls_of(store, job_id, "proposer")[0]
        assert "notes" not in row["reply_parsed"]
        assert row["normalised"] is None
        assert verdict_of(store, job_id).status == "ok"


class TestTheRepairIsInTheStore:
    """Read the record back out of the DATABASE, not off an object."""

    def test_the_repair_is_readable_from_a_second_store_on_the_same_file(
        self, tmp_path: Any
    ) -> None:
        from kbutillib.domains.ai.council.store import open_store

        store = open_real_store(tmp_path)
        job_id = settle(
            store,
            {
                "proposer": null_notes_proposer("A:1"),
                "critic": critic_answer(verdict="strongly_consistent"),
            },
        )
        store.close()

        reader = open_store(f"sqlite:///{tmp_path}/council/council.sqlite")
        try:
            rows = [r for r in reader.get_calls(job_id) if r["role"] == "proposer"]
            assert rows
            for row in rows:
                assert [entry["rule"] for entry in row["normalised"]] == [NULL_STRING]
                assert row["reply_parsed"]["notes"] == ""
                assert json.loads(row["reply_text"])["notes"] is None
            item = next(r for r in reader.get_items(job_id) if r["item_id"] == "item-1")
            assert item["verdict"]["normalised"] is True
        finally:
            reader.close()


# ---------------------------------------------------------------------------
# Rule 2: a meta reply that omitted the content flag
# ---------------------------------------------------------------------------


class TestDerivedContentFlag:
    """The flag is derived from the reply, or from the skip path's rule."""

    def test_non_empty_final_ids_derive_false(self, tmp_path: Any) -> None:
        """An item with ids selected is not contentless, whatever else holds."""
        store = open_real_store(tmp_path)
        job_id = settle(store, split_script())

        verdict = verdict_of(store, job_id)
        assert verdict.status == "ok"
        assert verdict.decided_by == "meta"
        assert verdict.selected_ids == ["A:1"]
        assert verdict.meta["no_functional_content"] is False
        assert verdict.decline_reason is None

        meta_row = calls_of(store, job_id, "meta")[0]
        assert meta_row["status"] == "ok"
        assert meta_row["reply_parsed"]["no_functional_content"] is False
        assert meta_row["normalised"] == [
            {
                "field": "no_functional_content",
                "rule": DERIVED_CONTENT_FLAG,
                "original": None,
                "value": False,
            }
        ]
        # The model never said it: the raw text has no such key.
        assert "no_functional_content" not in json.loads(meta_row["reply_text"])

    def test_empty_final_ids_take_the_proposer_majority_true(
        self, tmp_path: Any
    ) -> None:
        """Two of three proposers said the string states no function."""
        store = open_real_store(tmp_path)
        job_id = settle(
            store,
            {
                ("proposer", "proposer_1"): proposer_answer("A:1", no_content=True),
                ("proposer", "proposer_2"): proposer_answer(no_content=True),
                ("proposer", "proposer_3"): proposer_answer(),
                "critic": critic_answer(verdict="inconsistent", contentless=False),
                "meta": meta_without_the_flag(),
            },
        )

        verdict = verdict_of(store, job_id)
        assert verdict.decided_by == "meta"
        assert verdict.selected_ids == []
        assert verdict.meta["no_functional_content"] is True
        # And the derived flag is what chose the decline reason.
        assert verdict.decline_reason == "contentless"
        assert calls_of(store, job_id, "meta")[0]["normalised"] == [
            {
                "field": "no_functional_content",
                "rule": DERIVED_CONTENT_FLAG,
                "original": None,
                "value": True,
            }
        ]

    def test_empty_final_ids_take_the_proposer_majority_false(
        self, tmp_path: Any
    ) -> None:
        """One of three is not a majority, so the flag is False.

        The critic says ``contentless_input: True`` and is IGNORED: it breaks
        a tie, it does not outvote the proposers.
        """
        store = open_real_store(tmp_path)
        job_id = settle(
            store,
            {
                ("proposer", "proposer_1"): proposer_answer("A:1", no_content=True),
                ("proposer", "proposer_2"): proposer_answer(),
                ("proposer", "proposer_3"): proposer_answer(),
                "critic": critic_answer(verdict="inconsistent", contentless=True),
                "meta": meta_without_the_flag(),
            },
        )

        verdict = verdict_of(store, job_id)
        assert verdict.selected_ids == []
        assert verdict.meta["no_functional_content"] is False
        assert verdict.decline_reason == "no_candidate_matched"

    @pytest.mark.parametrize("critic_says", [True, False])
    def test_an_exact_tie_takes_the_critics_answer(
        self, tmp_path: Any, critic_says: bool
    ) -> None:
        """One of two proposers flagged it: the critic breaks the tie."""
        store = open_real_store(tmp_path, name=f"tie-{critic_says}")
        job_id = settle(
            store,
            {
                ("proposer", "proposer_1"): proposer_answer("A:1", no_content=True),
                ("proposer", "proposer_2"): proposer_answer(),
                "critic": critic_answer(
                    verdict="inconsistent", contentless=critic_says
                ),
                "meta": meta_without_the_flag(),
            },
            n_proposers=2,
        )

        verdict = verdict_of(store, job_id)
        assert verdict.selected_ids == []
        assert verdict.meta["no_functional_content"] is critic_says
        assert verdict.decline_reason == (
            "contentless" if critic_says else "no_candidate_matched"
        )

    def test_a_meta_reply_that_states_the_flag_is_not_touched(
        self, tmp_path: Any
    ) -> None:
        """Nothing is derived over a model that answered the question."""
        store = open_real_store(tmp_path)
        stated = meta_without_the_flag("A:1")
        stated["no_functional_content"] = False
        job_id = settle(store, split_script(meta=stated))

        meta_row = calls_of(store, job_id, "meta")[0]
        assert meta_row["normalised"] is None
        verdict = verdict_of(store, job_id)
        assert verdict.normalised is False
        assert verdict.repairs == {}


# ---------------------------------------------------------------------------
# The verdict's record
# ---------------------------------------------------------------------------


class TestTheVerdictCarriesTheRecord:
    """``normalised: true`` plus the per-seat repair list."""

    def test_the_verdict_names_every_repaired_seat(self, tmp_path: Any) -> None:
        store = open_real_store(tmp_path)
        job_id = settle(
            store,
            {
                ("proposer", "proposer_1"): null_notes_proposer("A:1", "B:2"),
                ("proposer", "proposer_2"): proposer_answer("A:1", "B:2"),
                ("proposer", "proposer_3"): proposer_answer("A:1"),
                "critic": critic_answer(),
                "meta": meta_without_the_flag("A:1"),
            },
        )

        verdict = verdict_of(store, job_id)
        assert verdict.status == "ok"
        assert verdict.normalised is True
        assert sorted(verdict.repairs) == ["meta", "proposer_1"]
        assert verdict.repairs["proposer_1"] == [
            {"field": "notes", "rule": NULL_STRING, "original": None, "value": ""}
        ]
        assert verdict.repairs["meta"] == [
            {
                "field": "no_functional_content",
                "rule": DERIVED_CONTENT_FLAG,
                "original": None,
                "value": False,
            }
        ]
        # The seats that needed nothing are absent, not present-and-empty.
        assert "proposer_2" not in verdict.repairs

    def test_an_unrepaired_council_says_so(self, tmp_path: Any) -> None:
        store = open_real_store(tmp_path)
        job_id = settle(
            store,
            {
                "proposer": proposer_answer("A:1"),
                "critic": critic_answer(verdict="strongly_consistent"),
            },
        )

        verdict = verdict_of(store, job_id)
        assert verdict.normalised is False
        assert verdict.repairs == {}
        assert all(row["normalised"] is None for row in calls_of(store, job_id))


# ---------------------------------------------------------------------------
# Nothing else is repaired
# ---------------------------------------------------------------------------


class TestEveryOtherSchemaFailureStillFails:
    """Exactly two defects are repaired. Everything else is invalid_output."""

    def test_a_proposer_missing_a_different_required_field_fails(
        self, tmp_path: Any
    ) -> None:
        store = open_real_store(tmp_path)
        reply = proposer_answer("A:1")
        reply.pop("overall_confidence")
        job_id = settle(store, {"proposer": reply, "critic": critic_answer()})

        rows = calls_of(store, job_id, "proposer")
        assert [row["status"] for row in rows] == ["error", "error", "error"]
        assert all(row["error"]["kind"] == "invalid_output" for row in rows)
        assert all("overall_confidence" in row["error"]["message"] for row in rows)
        assert all(row["normalised"] is None for row in rows)

        verdict = verdict_of(store, job_id)
        assert verdict.status == "error"
        assert verdict.normalised is False
        # No critic was asked: a council that lost a seat does not go on.
        assert calls_of(store, job_id, "critic") == []

    def test_a_meta_missing_a_different_required_field_fails(
        self, tmp_path: Any
    ) -> None:
        store = open_real_store(tmp_path)
        broken = meta_without_the_flag("A:1")
        broken.pop("reasoning")
        job_id = settle(store, split_script(meta=broken))

        meta_row = calls_of(store, job_id, "meta")[0]
        assert meta_row["status"] == "error"
        assert meta_row["error"]["kind"] == "invalid_output"
        assert "reasoning" in meta_row["error"]["message"]
        verdict = verdict_of(store, job_id)
        assert verdict.status == "error"

    def test_a_null_value_in_a_non_string_field_still_fails(
        self, tmp_path: Any
    ) -> None:
        """``no_functional_content: null`` is a boolean field, not a string."""
        store = open_real_store(tmp_path)
        reply = proposer_answer("A:1")
        reply["no_functional_content"] = None
        job_id = settle(store, {"proposer": reply, "critic": critic_answer()})

        rows = calls_of(store, job_id, "proposer")
        assert [row["status"] for row in rows] == ["error", "error", "error"]
        assert all(row["error"]["kind"] == "invalid_output" for row in rows)

    def test_a_reply_that_is_not_an_object_still_fails(self, tmp_path: Any) -> None:
        """``normalise`` hands a non-mapping straight on to the gate."""
        parsed, repairs = normalise("proposer", ["not", "an", "object"], {})
        assert parsed == ["not", "an", "object"]
        assert repairs == []


# ---------------------------------------------------------------------------
# Reuse
# ---------------------------------------------------------------------------


class TestReuseReappliesTheRepair:
    """A reused answer is repaired identically to a fresh one."""

    def test_a_second_job_reusing_the_answer_carries_the_same_repair(
        self, tmp_path: Any
    ) -> None:
        store = open_real_store(tmp_path)
        script = {
            "proposer": null_notes_proposer("A:1"),
            "critic": critic_answer(verdict="strongly_consistent"),
        }
        backend = fake(script)
        worker = worker_for(store, {"fake1": backend})

        first = settle(store, script, worker=worker, label="first")
        spent = len(backend.calls)
        assert spent == 4

        second = settle(store, script, worker=worker, label="second")
        assert len(backend.calls) == spent, "the second job asked a model something"

        rows = [r for r in calls_of(store, second, "proposer")]
        assert all(row["reused_from"] for row in rows)
        for row in rows:
            assert row["status"] == "ok"
            assert row["reply_parsed"]["notes"] == ""
            assert row["normalised"] == [
                {
                    "field": "notes",
                    "rule": NULL_STRING,
                    "original": None,
                    "value": "",
                }
            ]
        assert verdict_of(store, second).normalised is True
        assert verdict_of(store, second).selected_ids == ["A:1"]
        assert verdict_of(store, first).repairs == verdict_of(store, second).repairs

    def test_a_reuse_source_that_predates_normalisation_is_repaired_on_reuse(
        self, tmp_path: Any
    ) -> None:
        """The reuse path RE-APPLIES the rules; it does not copy a record.

        A row written by an earlier build holds the raw reply and no repair
        record. Inheriting it would give the reusing call an answer its own
        schema would have refused, so the source row here is rewritten to
        exactly that older shape -- through the store's own engine, with no
        part of the council substituted -- and the reusing call must repair
        it for itself.
        """
        import sqlalchemy as sa

        from kbutillib.domains.ai.council.store import schema as sch

        store = open_real_store(tmp_path)
        script = {
            "proposer": null_notes_proposer("A:1"),
            "critic": critic_answer(verdict="strongly_consistent"),
        }
        backend = fake(script)
        worker = worker_for(store, {"fake1": backend})
        first = settle(store, script, worker=worker, label="first")

        raw = null_notes_proposer("A:1")
        with store.transaction() as conn:
            conn.execute(
                sch.calls.update()
                .where(
                    sch.calls.c.job_id == first,
                    sch.calls.c.role == "proposer",
                )
                .values(reply_parsed=raw, normalised=sa.null())
            )
        aged = calls_of(store, first, "proposer")
        assert all(row["reply_parsed"]["notes"] is None for row in aged)
        assert all(row["normalised"] is None for row in aged)

        second = settle(store, script, worker=worker, label="second")
        rows = calls_of(store, second, "proposer")
        assert all(row["reused_from"] for row in rows)
        for row in rows:
            assert row["reply_parsed"]["notes"] == "", "the reuse path skipped normalise"
            assert [entry["rule"] for entry in row["normalised"]] == [NULL_STRING]
        assert verdict_of(store, second).normalised is True

    def test_a_reused_meta_reply_gets_the_derived_flag(self, tmp_path: Any) -> None:
        store = open_real_store(tmp_path)
        script = split_script()
        backend = fake(script)
        worker = worker_for(store, {"fake1": backend})

        first = settle(store, script, worker=worker, label="first")
        spent = len(backend.calls)
        second = settle(store, script, worker=worker, label="second")
        assert len(backend.calls) == spent

        meta_row = calls_of(store, second, "meta")[0]
        assert meta_row["reused_from"]
        assert meta_row["reply_parsed"]["no_functional_content"] is False
        assert [entry["rule"] for entry in meta_row["normalised"]] == [
            DERIVED_CONTENT_FLAG
        ]
        assert verdict_of(store, second).decided_by == "meta"
        assert verdict_of(store, second).selected_ids == ["A:1"]


# ---------------------------------------------------------------------------
# The function on its own
# ---------------------------------------------------------------------------


class TestNormaliseDirectly:
    """Two properties the worker tests above rely on but cannot show."""

    def test_the_callers_reply_is_not_mutated(self) -> None:
        schema = make_query().answer_schema("proposer")
        reply = null_notes_proposer("A:1")
        reply["picked"][0]["why"] = None
        parsed, repairs = normalise("proposer", reply, schema)

        assert reply["notes"] is None, "normalise mutated the raw reply"
        assert reply["picked"][0]["why"] is None
        assert parsed["notes"] == ""
        assert parsed["picked"][0]["why"] == ""
        assert sorted(entry["field"] for entry in repairs) == [
            "notes",
            "picked[0].why",
        ]

    def test_it_is_idempotent(self) -> None:
        """Re-applying it repairs nothing -- which is what the reuse path needs."""
        schema = make_query().answer_schema("meta")
        once, first = normalise(
            "meta",
            meta_without_the_flag(),
            schema,
            {"ballots": [{"no_functional_content": True}], "critic": {}},
        )
        twice, second = normalise("meta", once, schema, {})

        assert first == [
            {
                "field": "no_functional_content",
                "rule": DERIVED_CONTENT_FLAG,
                "original": None,
                "value": True,
            }
        ]
        assert twice == once
        assert second == []


# ---------------------------------------------------------------------------
# The column the record lives in
# ---------------------------------------------------------------------------


class TestTheAdditiveColumn:
    """``calls.normalised`` on a store file that predates the column."""

    def test_migrate_adds_the_column_to_an_existing_store(
        self, tmp_path: Any
    ) -> None:
        """A store created by the earlier build of schema v1 has every table
        and the right stamp, and is missing exactly this column. ``migrate()``
        adds it; nothing else in the file changes.
        """
        import sqlalchemy as sa

        from kbutillib.domains.ai.council.store import schema as sch

        store = open_real_store(tmp_path)
        with store.transaction() as conn:
            conn.execute(sa.text("ALTER TABLE calls DROP COLUMN normalised"))
        assert "normalised" not in {
            col["name"] for col in sa.inspect(store.engine).get_columns("calls")
        }
        assert store.schema_version() == sch.SCHEMA_VERSION

        assert store.migrate() == sch.SCHEMA_VERSION
        assert "normalised" in {
            col["name"] for col in sa.inspect(store.engine).get_columns("calls")
        }
        # Idempotent: a second run adds nothing and does not raise.
        assert store.add_missing_columns() == []

        # And the migrated file still works end to end.
        job_id = settle(
            store,
            {
                "proposer": null_notes_proposer("A:1"),
                "critic": critic_answer(verdict="strongly_consistent"),
            },
        )
        row = calls_of(store, job_id, "proposer")[0]
        assert [entry["rule"] for entry in row["normalised"]] == [NULL_STRING]

    def test_every_additive_column_is_really_in_the_schema(self) -> None:
        """A name in ``ADDITIVE_COLUMNS`` that no table defines would make
        ``add_missing_columns`` raise on the store it was meant to repair."""
        from kbutillib.domains.ai.council.store import schema as sch

        for table_name, column_name in sch.ADDITIVE_COLUMNS:
            assert table_name in sch.metadata.tables
            assert column_name in sch.metadata.tables[table_name].c
