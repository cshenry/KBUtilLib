"""Offline parity guard: Jose Faria's recorded V3 GO council, replayed.

Ten rows of the recorded CONCORDIA V3 GO council travel with the repository
as ``fixtures/go_v3_replay_10.json``, so the parity the full
``scripts/council/replay_go_v3.py`` run measured on poplar against
``/scratch`` is re-checked on every machine, on every run of the suite.

WHAT IS REAL HERE, AND WHAT IS SUBSTITUTED
==========================================
Real: the store (a ``SqliteStore`` on a ``tmp_path`` file), the query type
(the SHIPPED ``go_mf@1``, seeded with ``QueryRegistry.seed_builtins()`` and
read back out of the database), the ``CouncilWorker``, and the decision rule
``engine.decide()``. None of them is monkeypatched, stubbed or subclassed.

Substituted: the BACKEND, and only the backend. ``ReplayBackend`` serves the
recorded model replies keyed by the worker's own ``trace_id``. That is the
design -- a replay exists precisely to swap the model out and leave
everything else standing.

No network call, no token, no CLI login, no GPU.

ALL TEN ROWS REPRODUCE V3 -- THREE OF THEM ONLY BECAUSE OF NORMALISATION
========================================================================
Seven rows reproduce V3 with nothing repaired. Three (1250, 1711, 2029) do so
only because ``engine/normalise.py`` supplies the ``no_functional_content``
that ``claudeopus45`` omitted from those meta replies, derived from each
reply's own non-empty final id list. Before that repair existed the worker
refused all three on ``go_mf@1``'s answer schema -- which Jose's V3 runner did
not have, and this worker enforces -- and the offline figure was 7/10.

Both facts are pinned below, separately: the ten agreements, and that exactly
three of them carry a ``derived_content_flag`` repair while the RECORDED reply
is still schema-invalid on its own. Pinning only the agreements would let a
regression that quietly widened normalisation pass unnoticed.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT_PATH = _REPO_ROOT / "scripts/council/replay_go_v3.py"
_FIXTURE_PATH = Path(__file__).parent / "fixtures/go_v3_replay_10.json"

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("sqlalchemy") is None,
    reason="the council store needs the 'council' extra (pip install -e '.[council]')",
)


def _load_replay_script() -> Any:
    """Import the replay script by path (``scripts/`` is not a package).

    Importing it is what makes this test and the full ``/scratch`` run share
    ONE implementation: the task construction, the seat mapping, the replay
    table and the comparison all come from the script. A test that
    reimplemented them could agree with itself while disagreeing with the
    thing that produced the report.

    The module reads nothing at import time -- every ``/scratch`` path is
    reached only from ``main()`` via the manifest -- so this is safe on a
    machine that has never seen the oracle.
    """
    spec = importlib.util.spec_from_file_location("replay_go_v3", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


replay_script = _load_replay_script()


#: The ten fixture rows, by the fixed selection rule recorded in the manifest.
EXPECTED_IDS = ["148", "293", "631", "789", "1250", "1402", "1465", "1497", "1711", "2029"]

#: Rows that reproduce V3 end to end, through the worker: all of them.
EXPECTED_AGREE = EXPECTED_IDS

#: Rows whose RECORDED reply fails ``go_mf@1``'s answer schema and which
#: reproduce V3 only because ``normalise()`` repairs it first. The defect is
#: in the transcript; the repair is what lets the decision through.
EXPECTED_NORMALISED = ["1250", "1711", "2029"]


@pytest.fixture(scope="module")
def fixture_rows() -> List[Dict[str, Any]]:
    """The checked-in transcript rows."""
    return json.loads(_FIXTURE_PATH.read_text())["rows"]


@pytest.fixture(scope="module")
def fixture_doc() -> Dict[str, Any]:
    return json.loads(_FIXTURE_PATH.read_text())


@pytest.fixture(scope="module")
def replayed(fixture_rows: List[Dict[str, Any]], tmp_path_factory: Any) -> Any:
    """Run the replay ONCE for the module, through the real worker."""
    store_dir = tmp_path_factory.mktemp("go-replay")
    return replay_script.replay_rows(
        fixture_rows, f"sqlite:///{store_dir}/replay.sqlite"
    )


class TestTheFixture:
    """The fixture is the ten rows the fixed rule selects, unaltered."""

    def test_the_fixture_holds_the_selected_rows(self, fixture_doc: Dict[str, Any]) -> None:
        assert [str(i) for i in fixture_doc["input_ids"]] == EXPECTED_IDS
        assert len(fixture_doc["rows"]) == 10

    def test_the_selection_rule_reproduces_the_fixture_ids(
        self, fixture_rows: List[Dict[str, Any]]
    ) -> None:
        """Re-apply the rule to the fixture; it must pick every row in it.

        The rule is "the first ten ``ok`` rows by input id, extended until a
        meta-skipped and a meta-run row are both present". All ten fixture
        rows are ``ok``, so applying it here must return all ten -- which is
        what proves the fixture was not hand-picked.
        """
        results = {str(r["result"]["input_id"]): r["result"] for r in fixture_rows}
        assert replay_script.select_fixture_ids(results) == EXPECTED_IDS

    def test_the_fixture_covers_both_meta_paths(
        self, fixture_rows: List[Dict[str, Any]]
    ) -> None:
        skipped = [
            str(r["result"]["input_id"])
            for r in fixture_rows
            if r["result"]["meta"].get("skipped")
        ]
        ran = [
            str(r["result"]["input_id"])
            for r in fixture_rows
            if not r["result"]["meta"].get("skipped")
        ]
        assert skipped == ["148", "631", "789", "1402", "1497"]
        assert ran == ["293", "1250", "1465", "1711", "2029"]

    def test_the_fixture_covers_both_decline_reasons(
        self, fixture_rows: List[Dict[str, Any]]
    ) -> None:
        """A parity fixture that never declines proves only the easy half."""
        reasons = {r["result"]["decline_reason"] for r in fixture_rows}
        assert reasons == {None, "contentless", "no_candidate_matched"}

    def test_no_fixture_row_satisfies_an_expected_difference_predicate(
        self, fixture_rows: List[Dict[str, Any]]
    ) -> None:
        """Every fixture row is held to strict parity; none is excused."""
        for row in fixture_rows:
            assert (
                replay_script.expected_difference_classes(row["input"], row["result"])
                == []
            ), f"row {row['result']['input_id']} would be excused"


class TestTheReplayReproducesV3:
    """The headline: the worker, fed V3's replies, reaches V3's decisions."""

    def test_every_row_is_decided(self, replayed: Any) -> None:
        _comparison, verdicts = replayed
        assert sorted(verdicts, key=int) == EXPECTED_IDS

    def test_the_agreeing_rows_agree_on_every_semantic_field(
        self, replayed: Any
    ) -> None:
        comparison, _verdicts = replayed
        agreed = [row["item_id"] for row in comparison if row["agrees"]]
        assert agreed == EXPECTED_AGREE

        # And not vacuously: each one really matched all four fields.
        for row in comparison:
            if row["item_id"] not in EXPECTED_AGREE:
                continue
            assert row["recorded"] == row["observed"], row["item_id"]
            assert row["differing_fields"] == []

    def test_the_selected_ids_match_as_sets(self, replayed: Any) -> None:
        """Ids are compared as SETS: the two implementations order differently.

        V3 reports the meta-skip path in first-vote order and the meta path
        in the order meta named them; ``decide()`` reports both in MENU
        order. Comparing lists would fail on agreement.
        """
        comparison, _verdicts = replayed
        for row in comparison:
            if row["item_id"] not in EXPECTED_AGREE:
                continue
            assert set(row["recorded"]["selected_ids"]) == set(
                row["observed"]["selected_ids"]
            )

    def test_a_real_term_is_actually_selected_somewhere(
        self, replayed: Any
    ) -> None:
        """Guard against a 'parity' of universal silence.

        Seven rows agreeing that nothing was picked would satisfy every other
        assertion here while proving nothing.
        """
        comparison, _verdicts = replayed
        picked = {
            row["item_id"]: row["observed"]["selected_ids"]
            for row in comparison
            if row["item_id"] in EXPECTED_AGREE and row["observed"]["selected_ids"]
        }
        assert picked == {
            "631": ["GO:0004332"],
            "789": ["GO:0046583"],
            "1250": ["GO:0008745"],
            "1402": ["GO:0004671"],
            "1497": ["GO:0015426"],
            "1711": ["GO:0090585"],
            "2029": ["GO:0000400"],
        }

    def test_both_decline_reasons_are_reproduced(self, replayed: Any) -> None:
        comparison, _verdicts = replayed
        by_id = {row["item_id"]: row for row in comparison}
        assert by_id["148"]["observed"]["decline_reason"] == "contentless"
        assert by_id["293"]["observed"]["decline_reason"] == "no_candidate_matched"
        assert by_id["1465"]["observed"]["decline_reason"] == "no_candidate_matched"

    def test_the_meta_skip_decision_is_reproduced(self, replayed: Any) -> None:
        """Whether meta ran at all is part of parity, not an implementation
        detail: it is the protocol's one spend decision."""
        comparison, _verdicts = replayed
        for row in comparison:
            if row["item_id"] not in EXPECTED_AGREE:
                continue
            assert row["recorded"]["meta_skipped"] == row["observed"]["meta_skipped"]


class TestTheNormalisedRows:
    """The three rows that reproduce V3 only after a repair, and exactly why."""

    def test_nothing_fails_and_nothing_is_excused(self, replayed: Any) -> None:
        comparison, _verdicts = replayed
        failing = [
            row["item_id"]
            for row in comparison
            if not row["agrees"] and not row["expected_difference_classes"]
        ]
        assert failing == []

    def test_exactly_these_rows_carry_a_repair(self, replayed: Any) -> None:
        """Pinned so a widened rule shows up as a changed set, not a pass."""
        _comparison, verdicts = replayed
        repaired = {
            item_id: verdict.repairs
            for item_id, verdict in verdicts.items()
            if verdict.normalised
        }
        assert sorted(repaired, key=int) == EXPECTED_NORMALISED
        for item_id in EXPECTED_NORMALISED:
            assert list(repaired[item_id]) == ["meta"], item_id
            assert repaired[item_id]["meta"] == [
                {
                    "field": "no_functional_content",
                    "rule": "derived_content_flag",
                    "original": None,
                    "value": False,
                }
            ], item_id

    def test_each_repaired_row_is_decided_by_meta_and_errors_on_nothing(
        self, replayed: Any
    ) -> None:
        """The cause is pinned, so a DIFFERENT outcome cannot hide here."""
        _comparison, verdicts = replayed
        for item_id in EXPECTED_NORMALISED:
            verdict = verdicts[item_id]
            assert verdict.status == "ok"
            assert verdict.decided_by == "meta"
            assert replay_script.failure_notes(verdict) == []
            # The flag the model omitted is False, and the ids it named stand.
            assert verdict.meta["no_functional_content"] is False
            assert verdict.selected_ids

    def test_the_recorded_reply_really_is_schema_invalid(
        self, fixture_rows: List[Dict[str, Any]], tmp_path: Path
    ) -> None:
        """Independently of the replay: the transcript, versus the shipped
        schema.

        ``schema_complaint`` validates the RAW recorded reply, with no
        normalisation, which is what keeps this a statement about the
        transcript: the reply really is invalid, and the repair is why the
        row goes through anyway.
        """
        from kbutillib.domains.ai.council.queries import QueryRegistry
        from kbutillib.domains.ai.council.store import open_store

        store = open_store(f"sqlite:///{tmp_path}/q.sqlite")
        store.init_schema()
        registry = QueryRegistry(store)
        registry.seed_builtins()
        query = registry.get("go_mf")

        by_id = {str(r["result"]["input_id"]): r for r in fixture_rows}
        for item_id in EXPECTED_NORMALISED:
            row = by_id[item_id]
            complaints = replay_script.schema_complaint(
                row["input"], row["result"], query
            )
            assert complaints, item_id
            assert all("no_functional_content" in c for c in complaints), complaints
            # The meta reply is missing the key the schema requires.
            assert "no_functional_content" not in row["result"]["meta"]

        # ...and a row that agrees carries a reply the schema accepts.
        assert (
            replay_script.schema_complaint(
                by_id["631"]["input"], by_id["631"]["result"], query
            )
            == []
        )

    def test_the_decision_rule_agrees_on_all_ten_rows(
        self, fixture_rows: List[Dict[str, Any]], tmp_path: Path
    ) -> None:
        """With the schema gate as the only variable, parity is 10/10.

        Kept after normalisation closed the end-to-end gap: it measures a
        different thing, and it is the control. The rule agreed on all ten
        rows BEFORE the repair existed, so a repair that changed a decision
        rather than merely admitting one would show up as this test and the
        end-to-end one disagreeing.
        """
        from kbutillib.domains.ai.council.queries import QueryRegistry
        from kbutillib.domains.ai.council.store import open_store

        store = open_store(f"sqlite:///{tmp_path}/rule.sqlite")
        store.init_schema()
        registry = QueryRegistry(store)
        registry.seed_builtins()
        query = registry.get("go_mf")

        disagreed = []
        for row in fixture_rows:
            item_id = str(row["result"]["input_id"])
            rule = replay_script.rule_only_decision(row["input"], row["result"], query)
            if rule != replay_script.recorded_decision(row["result"]):
                disagreed.append((item_id, rule))
        assert disagreed == []


class TestNothingWasFaked:
    """The guard on the guard: the real parts really are the real parts."""

    def test_the_query_type_is_the_shipped_builtin_from_the_database(
        self, tmp_path: Path
    ) -> None:
        """``go_mf@1`` is seeded and READ BACK, not constructed in-test."""
        from kbutillib.domains.ai.council.queries import QueryRegistry
        from kbutillib.domains.ai.council.store import open_store

        store = open_store(f"sqlite:///{tmp_path}/seeded.sqlite")
        store.init_schema()
        QueryRegistry(store).seed_builtins()

        reader = open_store(f"sqlite:///{tmp_path}/seeded.sqlite")
        query = QueryRegistry(reader).get("go_mf")
        assert query.name == "go_mf"
        assert query.version == "1"
        assert query.renderer == "go_mf_v3@1"
        assert query.adapter == "go_mf_v3@1"
        assert query.id_field == "go_id"
        assert query.roles["proposer"].system.startswith(
            "You assign Gene Ontology molecular-function terms"
        )

    def test_the_roster_is_the_v3_roster_on_replay_endpoints(self) -> None:
        roster = replay_script.build_roster()
        assert [seat.model for seat in roster.proposers] == [
            "gemini35flash",
            "gpt54",
            "gpt55",
        ]
        assert roster.critic.model == "claudesonnet5"
        assert roster.meta.model == "claudeopus45"
        assert {seat.endpoint_kind for _role, seat in roster.seats()} == {"replay"}

    def test_the_backend_is_keyed_by_the_workers_own_trace_id(
        self, fixture_rows: List[Dict[str, Any]]
    ) -> None:
        """``replay_entries`` keys must be what ``worker.py:941`` builds.

        If the worker's trace format changed, the table would miss and every
        row would error -- so this pins the contract between them rather
        than letting a silent total failure look like a backend problem.
        """
        row = next(r for r in fixture_rows if str(r["result"]["input_id"]) == "631")
        table = replay_script.replay_entries(row["result"])
        assert set(table) == {
            ("631", "proposer", "proposer_1"),
            ("631", "proposer", "proposer_2"),
            ("631", "proposer", "proposer_3"),
            ("631", "critic", "critic"),
        }
        # 631 is meta-skipped, so no meta entry exists to be served.
        assert not any(key[1] == "meta" for key in table)

    def test_a_meta_run_row_serves_a_meta_reply_without_the_runners_key(
        self, fixture_rows: List[Dict[str, Any]]
    ) -> None:
        """``skipped`` is the V3 RUNNER's annotation, not the model's answer.

        ``go_mf``'s meta schema is ``additionalProperties: false``, so
        serving it back would fail a call that never failed live.
        """
        row = next(r for r in fixture_rows if str(r["result"]["input_id"]) == "293")
        table = replay_script.replay_entries(row["result"])
        served = table[("293", "meta", "meta")]
        assert "skipped" in row["result"]["meta"]
        assert "skipped" not in served
        assert "final_go_ids" in served

    def test_an_errored_proposer_is_withheld_rather_than_invented(self) -> None:
        """No fixture row has one, so this is checked on a synthetic row."""
        result = {
            "input_id": 999,
            "proposers": {
                "gemini35flash": {"error": "ValueError: no JSON object"},
                "gpt54": {"picked": [], "overall_confidence": "low",
                          "no_functional_content": False, "notes": ""},
                "gpt55": {"picked": [], "overall_confidence": "low",
                          "no_functional_content": False, "notes": ""},
            },
            "critic": {"verdict": "consistent", "reason": "", "overpicked": [],
                       "missed": [], "contentless_input": False},
            "meta": {"skipped": True},
        }
        table = replay_script.replay_entries(result)
        assert ("999", "proposer", "proposer_1") not in table
        assert ("999", "proposer", "proposer_2") in table

    def test_the_replay_makes_no_http_call(
        self, fixture_rows: List[Dict[str, Any]], tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Break every HTTP transport, then replay: it must still pass.

        ``monkeypatch`` is used here to make the network UNUSABLE, not to
        substitute any part of the council. If anything reached for a socket
        this would raise.
        """
        import socket

        def _no_sockets(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("the replay attempted a network connection")

        monkeypatch.setattr(socket.socket, "connect", _no_sockets)
        monkeypatch.setattr(socket, "create_connection", _no_sockets)

        comparison, _verdicts = replay_script.replay_rows(
            fixture_rows, f"sqlite:///{tmp_path}/nonet.sqlite"
        )
        assert [row["item_id"] for row in comparison if row["agrees"]] == EXPECTED_AGREE
