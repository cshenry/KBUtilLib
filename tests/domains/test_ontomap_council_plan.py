"""``council_plan`` -- stage 1 of the standing ``function_to_go`` pipeline.

Spends nothing and reaches nothing. The two clearinghouse reads and the
council's configuration are injected (``_clearinghouse``/``_council``), and
the retrieval stage is stubbed, so what is exercised here is the stage's own
behaviour: the parameter set it resolves, the partition it records, where it
writes each file, and the ``PLAN.json`` the quote stage must be able to load.

THE ASSERTION THAT MATTERS MOST is the re-selection matrix
(:class:`TestReSelection`): a TRANSPORT-only change must re-select NOTHING,
while a bench, query-version or ``mapping_hash`` change re-selects the whole
corpus. ``Roster.fingerprint()`` covers the endpoint kind and the route
spelling, so using it as the partition key would re-partition all 47,341
clearinghouse functions and re-charge ~199M tokens for a pure cost
optimisation -- which is the defect ``bench_fingerprint`` exists to fix.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytest.importorskip("numpy")

from kbutillib.domains.ai.council.quote import (  # noqa: E402
    PLAN_FILENAME,
    load_plan,
)
from kbutillib.domains.ai.council.types import (  # noqa: E402
    Roster,
    SeatSpec,
    bench_fingerprint,
    canonical_sha256,
)
from kbutillib.domains.genome.ontology_mapping import (  # noqa: E402
    ontology_mapping_utils as omu,
)

QUERY = "go_mf@1"
SOURCE = "council/go_mf@1"
MAPPING_HASH = "e" * 64


def _hash(n: int) -> str:
    """A 64-char lowercase-hex entity hash, distinct per ``n``."""
    return f"{n:064x}"


# ---------------------------------------------------------------------------
# The injected collaborators
# ---------------------------------------------------------------------------


class _FakeClearinghouse:
    """The two sanctioned reads stage 1 makes, and nothing else.

    ``current_state`` records the filters it was given, because "filtered to
    THIS run's source and parameter_set_hash" is the whole definition of
    un-run and a read that dropped either filter would quietly plan the wrong
    set.
    """

    def __init__(
        self,
        universe_rows: List[Dict[str, Any]],
        done_by_hash: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> None:
        self.universe_rows = universe_rows
        self.done_by_hash = done_by_hash or {}
        self.current_state_calls: List[Dict[str, Any]] = []
        self.universe_calls: List[str] = []

    def universe(self, entity_type: str, **_kw: Any) -> List[Dict[str, Any]]:
        self.universe_calls.append(entity_type)
        return list(self.universe_rows)

    def current_state(
        self,
        entity_type: str,
        *,
        sources: Optional[List[str]] = None,
        parameter_set_hashes: Optional[List[str]] = None,
        **_kw: Any,
    ) -> List[Dict[str, Any]]:
        self.current_state_calls.append(
            {
                "entity_type": entity_type,
                "sources": sources,
                "parameter_set_hashes": parameter_set_hashes,
            }
        )
        rows: List[Dict[str, Any]] = []
        for psh in parameter_set_hashes or []:
            rows.extend(self.done_by_hash.get(psh, []))
        return rows


class _FakeQueue:
    def __init__(self, seats: List[str], concurrency: int, workers: int = 1) -> None:
        self.seats = seats
        self.concurrency = concurrency
        self.workers = workers


class _FakeConfig:
    """Just enough ``CouncilConfig`` for the identity and the queue table."""

    def __init__(self, endpoints: Dict[str, Dict[str, Any]]) -> None:
        self._endpoints = endpoints
        self.default_profile = "go_council_v3"
        self.queues = {
            "mag_sol": _FakeQueue(["proposer_2"], 3),
            "mag_luna": _FakeQueue(["proposer_3"], 3),
        }

    def endpoint(self, name: str) -> Dict[str, Any]:
        return dict(self._endpoints[name])


class _FakeCouncil:
    def __init__(self, roster: Roster, config: _FakeConfig) -> None:
        self._roster = roster
        self._config = config

    def config(self) -> _FakeConfig:
        return self._config

    def roster(self, profile: Optional[str] = None, **_overrides: Any) -> Roster:
        return self._roster


def _seat(
    seat_name: str,
    canonical_model: str,
    *,
    endpoint: str = "argo",
    endpoint_kind: str = "argo",
    model: Optional[str] = None,
) -> SeatSpec:
    return SeatSpec(
        seat_name=seat_name,
        endpoint=endpoint,
        endpoint_kind=endpoint_kind,
        model=model or canonical_model,
        canonical_model=canonical_model,
    )


#: The 2026-10-20 bench, as the roster the plan is derived against.
def _roster(**moves: str) -> Roster:
    """The measured bench, with optional ``seat_name=canonical_model`` moves."""
    models = {
        "proposer_1": "gemini-3.5-flash",
        "proposer_2": "gpt-5.6-sol",
        "proposer_3": "gpt-5.6-luna",
        "critic": "claude-sonnet-5",
        "meta": "claude-opus-5",
    }
    models.update(moves)
    return Roster(
        proposers=[
            _seat("proposer_1", models["proposer_1"]),
            _seat("proposer_2", models["proposer_2"], endpoint="mag", endpoint_kind="openai"),
            _seat("proposer_3", models["proposer_3"], endpoint="mag", endpoint_kind="openai"),
        ],
        critic=_seat("critic", models["critic"]),
        meta=_seat("meta", models["meta"]),
    )


def _rerouted_roster() -> Roster:
    """The SAME bench, every seat reached a different way.

    Same five canonical models in the same five roles; different endpoint
    kinds, different route spellings. ``Roster.fingerprint()`` moves,
    ``bench_fingerprint`` must not.
    """
    return Roster(
        proposers=[
            _seat("proposer_1", "gemini-3.5-flash", endpoint="mag", endpoint_kind="openai",
                  model="google/gemini-3.5-flash"),
            _seat("proposer_2", "gpt-5.6-sol", endpoint="codex", endpoint_kind="codex_cli",
                  model="gpt-5.6-sol-latest"),
            _seat("proposer_3", "gpt-5.6-luna", endpoint="argo", endpoint_kind="argo",
                  model="gpt56luna"),
        ],
        critic=_seat("critic", "claude-sonnet-5", endpoint="claude", endpoint_kind="claude_cli",
                     model="sonnet"),
        meta=_seat("meta", "claude-opus-5", endpoint="mag", endpoint_kind="openai",
                   model="anthropic/claude-opus-5"),
    )


ENDPOINTS: Dict[str, Dict[str, Any]] = {
    "argo": {"kind": "argo", "base_url": "https://argo.example/api", "account": "argo-anl",
             "max_concurrency": 24},
    "mag": {"kind": "openai", "base_url": "https://mag.example/v1", "account": "mag-shared",
            "max_concurrency": 20, "max_requests_per_minute": 120},
    "codex": {"kind": "codex_cli", "executable": "codex", "account": "codex-max"},
    "claude": {"kind": "claude_cli", "executable": "claude", "account": "claude-max"},
}


class _Stage1(omu.OntologyMappingUtils):
    """``OntologyMappingUtils`` with the backend and collaborators injected.

    ``mapping_version`` and ``function_to_go`` are the retrieval backend, which
    needs scikit-learn and the vendored GO artifacts; the stage's own logic
    does not, so they are stubbed and the stage is testable anywhere numpy
    imports. Everything else -- ``write_ranked``, the partition, the plan -- is
    the real code path.
    """

    def __init__(
        self,
        clearinghouse: _FakeClearinghouse,
        council: _FakeCouncil,
        *,
        mapping_hash: str = MAPPING_HASH,
        abstain_texts: Optional[Any] = None,
    ) -> None:
        super().__init__(config_file=False, token_file=None, kbase_token_file=None)
        self._fake_clearinghouse = clearinghouse
        self._fake_council = council
        self._mapping_hash = mapping_hash
        self._abstain_texts = set(abstain_texts or ())
        self.ranked_calls: List[int] = []

    def mapping_version(self) -> Dict[str, Any]:
        return {
            "mapping_version": "ontomap-go-text-union@1.0.0 (test)",
            "mapping_hash": self._mapping_hash,
            "provenance": {"backend": "ontomap-go-text-union", "placeholder_rules": "1.0.0"},
        }

    def function_to_go(self, functions: Any, max_candidates: int = 40) -> Dict[str, Any]:
        rows = list(functions)
        self.ranked_calls.append(len(rows))
        results = []
        for row in rows:
            abstained = row["text"] in self._abstain_texts
            results.append(
                {
                    "query_id": row["query_id"],
                    "text": row["text"],
                    "source": row.get("source") or "",
                    "abstained": abstained,
                    "abstain_reason": "placeholder function" if abstained else None,
                    "candidates": (
                        []
                        if abstained
                        else [
                            {
                                "go_id": "GO:0003824",
                                "name": "catalytic activity",
                                "def": "Catalysis of a biochemical reaction.",
                                "score": 0.5,
                                "height": 3,
                                "rank": 1,
                            }
                        ]
                    ),
                }
            )
        return {**self.mapping_version(), "results": results}

    def _clearinghouse(self) -> Any:
        return self._fake_clearinghouse

    def _council(self) -> Any:
        return self._fake_council


def _universe(*rows: Any) -> List[Dict[str, Any]]:
    return [
        {"entity_hash": entity_hash, "definition": definition}
        for entity_hash, definition in rows
    ]


#: Four functions: three ordinary, one placeholder that retrieval declines.
UNIVERSE = _universe(
    (_hash(1), "glucokinase"),
    (_hash(2), "glucose oxidase"),
    (_hash(3), "ATP synthase subunit alpha"),
    (_hash(4), "hypothetical protein"),
)
ABSTAIN_TEXTS = {"hypothetical protein"}


def _stage(
    tmp_path: Path,
    *,
    universe: Optional[List[Dict[str, Any]]] = None,
    done_by_hash: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    roster: Optional[Roster] = None,
    mapping_hash: str = MAPPING_HASH,
) -> _Stage1:
    clearinghouse = _FakeClearinghouse(
        UNIVERSE if universe is None else universe, done_by_hash
    )
    council = _FakeCouncil(roster or _roster(), _FakeConfig(ENDPOINTS))
    return _Stage1(
        clearinghouse, council, mapping_hash=mapping_hash, abstain_texts=ABSTAIN_TEXTS
    )


def _plan(tmp_path: Path, stage: _Stage1, **kwargs: Any) -> Dict[str, Any]:
    kwargs.setdefault("query", QUERY)
    kwargs.setdefault("scratch_dir", tmp_path / "scratch")
    return stage.council_plan(tmp_path / "run" / "2026-10-22-adp1", **kwargs)


# ---------------------------------------------------------------------------
# The plan file
# ---------------------------------------------------------------------------


class TestPlanFile:
    def test_it_writes_plan_json_into_the_run_directory(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path, _stage(tmp_path))
        plan_file = Path(plan["plan_file"])
        assert plan_file.name == PLAN_FILENAME
        assert plan_file.parent == tmp_path / "run" / "2026-10-22-adp1"
        assert json.loads(plan_file.read_text())["plan_hash"] == plan["plan_hash"]

    def test_plan_json_is_the_only_file_in_the_run_directory(
        self, tmp_path: Path
    ) -> None:
        """The run directory is a Dropbox-synced surface.

        The ranked file and the items file are 100+ MB of machine-local
        intermediates that nothing off-machine reads, and every byte on the
        sync surface needs an integrity gate.
        """
        plan = _plan(tmp_path, _stage(tmp_path))
        run_dir = Path(plan["run_dir"])
        assert sorted(path.name for path in run_dir.iterdir()) == [PLAN_FILENAME]

    def test_it_carries_the_counts_the_chunks_and_the_identity(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path, _stage(tmp_path))
        assert plan["counts"] == {
            "universe": 4,
            "done": 0,
            "unrun": 4,
            "submit": 3,
            "abstain": 1,
            "fanout": 3,
        }
        assert plan["query"] == QUERY
        assert plan["source"] == SOURCE
        assert plan["mapping_hash"] == MAPPING_HASH
        assert len(plan["chunks"]) == 1
        assert plan["chunks"][0]["items"] == 3

    def test_the_plan_hash_is_canonical_sha256_over_the_plan_contents(
        self, tmp_path: Path
    ) -> None:
        """Reusing the store's own canonicalisation, via ``Plan.plan_hash``.

        Not a second hashing rule: ``load_plan`` RE-DERIVES this hash from the
        file and refuses a plan whose declared hash disagrees, so a plan
        written with any other rule would be unquotable.
        """
        plan = _plan(tmp_path, _stage(tmp_path))
        assert len(plan["plan_hash"]) == 64
        reloaded = load_plan(plan["plan_file"])
        assert reloaded.plan_hash == plan["plan_hash"]
        assert reloaded.plan_hash == canonical_sha256(
            {
                "plan_version": plan["schema_version"],
                "query": QUERY,
                "mapping_hash": MAPPING_HASH,
                "profile": plan["profile"],
                "items": [item.identity() for item in reloaded.items],
            }
        )

    def test_stage_two_can_load_the_plan_it_writes(self, tmp_path: Path) -> None:
        """The plan's contract is ``load_plan``'s, not this module's own."""
        plan = _plan(tmp_path, _stage(tmp_path))
        loaded = load_plan(plan["plan_file"])
        assert loaded.query_name == "go_mf"
        assert loaded.query_version == "1"
        assert loaded.mapping_hash == MAPPING_HASH
        assert len(loaded.items) == 3
        # query_id IS the join key into the clearinghouse, and it must survive
        # the round trip through the items file.
        assert all(item.query_id for item in loaded.items)
        assert {item.query_id for item in loaded.items} == {
            _hash(1),
            _hash(2),
            _hash(3),
        }

    def test_the_menu_is_the_shape_the_council_itself_builds(
        self, tmp_path: Path
    ) -> None:
        """So a planned item renders to the same prompt as a direct submit.

        If it did not, every ``prompt_hash`` would miss and the quote would
        price a full re-run of answers the store already holds.
        """
        plan = _plan(tmp_path, _stage(tmp_path))
        (entry,) = load_plan(plan["plan_file"]).items[0].menu
        assert set(entry) == {"id", "label", "description", "rank", "score"}
        assert entry["id"] == "GO:0003824"
        assert entry["label"] == "catalytic activity"

    def test_it_is_written_atomically_leaving_no_partial_behind(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path, _stage(tmp_path))
        run_dir = Path(plan["run_dir"])
        assert not list(run_dir.glob("*.partial"))

    def test_re_planning_unchanged_inputs_reproduces_the_plan_hash(
        self, tmp_path: Path
    ) -> None:
        """An approval binds to ``plan_hash``, so a re-plan must not move it."""
        first = _plan(tmp_path, _stage(tmp_path))
        second = _plan(tmp_path, _stage(tmp_path))
        assert first["plan_hash"] == second["plan_hash"]


# ---------------------------------------------------------------------------
# Where the bulk files go
# ---------------------------------------------------------------------------


class TestScratchFiles:
    def test_the_ranked_file_is_under_scratch_and_not_in_the_run_directory(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path, _stage(tmp_path))
        ranked = Path(plan["ranked_file"])
        assert ranked.exists()
        assert ranked.name == "ranked.jsonl.gz"
        assert tmp_path / "scratch" in ranked.parents
        assert Path(plan["run_dir"]) not in ranked.parents

    def test_the_items_abstain_and_fanout_records_are_all_under_scratch(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path, _stage(tmp_path))
        for key in ("items_file", "abstain_file", "fanout_file"):
            path = Path(plan[key])
            assert path.exists(), key
            assert tmp_path / "scratch" in path.parents, key

    def test_the_abstained_set_is_recorded_rather_than_dropped(
        self, tmp_path: Path
    ) -> None:
        """Dropping them is what made 760 definitions look like a coverage gap.

        They are carried forward by stage 5 as result rows with
        ``decline_reason = "abstained:<reason>"``, so the reason has to be in
        the record, not just the count.
        """
        plan = _plan(tmp_path, _stage(tmp_path))
        rows = [
            json.loads(line)
            for line in Path(plan["abstain_file"]).read_text().splitlines()
        ]
        assert [row["query_id"] for row in rows] == [_hash(4)]
        assert rows[0]["decline_reason"] == "abstained:placeholder function"
        assert plan["counts"]["abstain"] == 1

    def test_the_fanout_record_maps_every_query_id_to_its_paid_item(
        self, tmp_path: Path
    ) -> None:
        shared = "glucokinase"
        universe = _universe(
            (_hash(1), shared), (_hash(2), shared), (_hash(3), shared)
        )
        plan = _plan(tmp_path, _stage(tmp_path, universe=universe))
        rows = [
            json.loads(line)
            for line in Path(plan["fanout_file"]).read_text().splitlines()
        ]
        assert plan["counts"]["submit"] == 1
        assert len(rows) == 3
        assert len({row["item_id"] for row in rows}) == 1

    def test_the_scratch_root_is_namespaced_by_run_id(self, tmp_path: Path) -> None:
        """Two runs under one scratch root must not share a path."""
        plan = _plan(tmp_path, _stage(tmp_path))
        ranked = Path(plan["ranked_file"])
        assert ranked.parent == tmp_path / "scratch" / "function-to-go" / plan["run_id"]

    def test_the_scratch_root_defaults_to_the_configured_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KBU_SCRATCH_DIR", str(tmp_path / "configured"))
        plan = _stage(tmp_path).council_plan(
            tmp_path / "run" / "2026-10-22-adp1", query=QUERY
        )
        assert Path(plan["ranked_file"]).parent == (
            tmp_path / "configured" / "function-to-go" / "2026-10-22-adp1"
        )

    def test_a_scratch_path_inside_the_run_directory_is_refused(
        self, tmp_path: Path
    ) -> None:
        run_dir = tmp_path / "run" / "2026-10-22-adp1"
        with pytest.raises(ValueError, match="inside the run directory"):
            _stage(tmp_path).council_plan(
                run_dir, query=QUERY, scratch_dir=run_dir / "scratch"
            )


# ---------------------------------------------------------------------------
# The parameter set -- the key the un-run set is derived against
# ---------------------------------------------------------------------------


class TestParameterSet:
    def test_it_is_query_bench_and_mapping_hash_and_nothing_else(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path, _stage(tmp_path))
        assert set(plan["parameter_set"]) == {
            "query",
            "bench_fingerprint",
            "mapping_hash",
        }
        assert plan["parameter_set"]["query"] == QUERY
        assert plan["parameter_set"]["mapping_hash"] == MAPPING_HASH

    def test_it_carries_bench_fingerprint_and_not_roster_fingerprint(
        self, tmp_path: Path
    ) -> None:
        """The bench partitions the corpus; the transport does not.

        ``Roster.fingerprint()`` covers the endpoint kind, the route spelling,
        the token ceiling, the temperature and the extras, so moving a seat
        from the codex CLI to MAG moves it -- which as a partition key would
        re-partition all 47,341 clearinghouse functions.
        """
        roster = _roster()
        plan = _plan(tmp_path, _stage(tmp_path, roster=roster))
        assert plan["parameter_set"]["bench_fingerprint"] == bench_fingerprint(roster)
        assert "roster_fingerprint" not in plan["parameter_set"]
        assert plan["parameter_set"]["bench_fingerprint"] != roster.fingerprint()

    def test_the_hash_is_the_clearinghouses_own(self, tmp_path: Path) -> None:
        from kbutillib.domains.identity import parameter_set_hash

        plan = _plan(tmp_path, _stage(tmp_path))
        assert plan["parameter_set_hash"] == parameter_set_hash(plan["parameter_set"])
        assert json.loads(plan["parameter_set_canonical_json"]) == plan["parameter_set"]

    def test_both_clearinghouse_filters_are_applied_to_the_done_read(
        self, tmp_path: Path
    ) -> None:
        stage = _stage(tmp_path)
        plan = _plan(tmp_path, stage)
        (call,) = stage._fake_clearinghouse.current_state_calls
        assert call["entity_type"] == "function"
        assert call["sources"] == [SOURCE]
        assert call["parameter_set_hashes"] == [plan["parameter_set_hash"]]

    def test_the_universe_read_is_not_gated_on_the_locus(
        self, tmp_path: Path
    ) -> None:
        """It works from h100 OFF-POD over the BERDL delta API (2026-10-22).

        Only ``load`` and ``verify`` are in-pod verbs, so stage 1 asks for the
        universe without consulting a locus at all -- a fake that offers no
        ``locus()`` is enough to plan.
        """
        stage = _stage(tmp_path)
        assert not hasattr(stage._fake_clearinghouse, "locus")
        _plan(tmp_path, stage)
        assert stage._fake_clearinghouse.universe_calls == ["function"]

    def test_an_explicit_parameter_set_is_used_as_given(self, tmp_path: Path) -> None:
        given = {"query": QUERY, "bench_fingerprint": "c" * 64, "mapping_hash": "d" * 64}
        plan = _plan(tmp_path, _stage(tmp_path), parameter_set=given)
        assert plan["parameter_set"] == given
        # The plan's headline bench_fingerprint is the one that PARTITIONS,
        # so it cannot disagree with the parameter set it was hashed from.
        assert plan["bench_fingerprint"] == given["bench_fingerprint"]
        # The seated bench is still recorded, as provenance.
        assert plan["roster_fingerprint"] == _roster().fingerprint()

    def test_a_query_without_a_version_is_refused_before_any_read(
        self, tmp_path: Path
    ) -> None:
        stage = _stage(tmp_path)
        with pytest.raises(ValueError, match="names no VERSION"):
            _plan(tmp_path, stage, query="go_mf")
        assert stage._fake_clearinghouse.universe_calls == []


# ---------------------------------------------------------------------------
# Re-selection: THE criterion the amendment exists for
# ---------------------------------------------------------------------------


class TestReSelection:
    """What re-selects the corpus and what does not.

    ``done`` is keyed on the resolved ``parameter_set_hash``, so these four
    cases are one question asked four ways: does this change move the hash?
    """

    @staticmethod
    def _done_at(parameter_set_hash: str) -> Dict[str, List[Dict[str, Any]]]:
        """A current-state result for every universe row, at one hash."""
        return {
            parameter_set_hash: [
                {
                    "entity_hash": row["entity_hash"],
                    "source": SOURCE,
                    "parameter_set_hash": parameter_set_hash,
                }
                for row in UNIVERSE
            ]
        }

    def _baseline_hash(self, tmp_path: Path) -> str:
        return _plan(tmp_path / "base", _stage(tmp_path))["parameter_set_hash"]

    def test_a_fully_answered_corpus_re_selects_nothing(self, tmp_path: Path) -> None:
        baseline = self._baseline_hash(tmp_path)
        plan = _plan(
            tmp_path / "again",
            _stage(tmp_path, done_by_hash=self._done_at(baseline)),
        )
        assert plan["counts"]["done"] == 4
        assert plan["counts"]["unrun"] == 0
        assert plan["counts"]["submit"] == 0

    def test_a_transport_only_change_re_selects_nothing(self, tmp_path: Path) -> None:
        """Same five models in the same five roles, every one reached a
        different way: a pure cost optimisation must cost nothing."""
        baseline = self._baseline_hash(tmp_path)
        rerouted = _rerouted_roster()
        assert bench_fingerprint(rerouted) == bench_fingerprint(_roster())
        assert rerouted.fingerprint() != _roster().fingerprint()
        plan = _plan(
            tmp_path / "rerouted",
            _stage(
                tmp_path, done_by_hash=self._done_at(baseline), roster=rerouted
            ),
        )
        assert plan["parameter_set_hash"] == baseline
        assert plan["counts"]["unrun"] == 0, (
            "a transport change re-selected the corpus; the partition key has "
            "picked up the route again"
        )

    def test_a_bench_change_re_selects_the_whole_corpus(self, tmp_path: Path) -> None:
        baseline = self._baseline_hash(tmp_path)
        rebenched = _roster(critic="claude-sonnet-6")
        plan = _plan(
            tmp_path / "rebenched",
            _stage(
                tmp_path, done_by_hash=self._done_at(baseline), roster=rebenched
            ),
        )
        assert plan["parameter_set_hash"] != baseline
        assert plan["counts"]["done"] == 0
        assert plan["counts"]["unrun"] == len(UNIVERSE)

    def test_a_query_version_change_re_selects_the_whole_corpus(
        self, tmp_path: Path
    ) -> None:
        baseline = self._baseline_hash(tmp_path)
        plan = _plan(
            tmp_path / "requeried",
            _stage(tmp_path, done_by_hash=self._done_at(baseline)),
            query="go_mf@2",
        )
        assert plan["parameter_set_hash"] != baseline
        assert plan["counts"]["unrun"] == len(UNIVERSE)

    def test_a_mapping_hash_change_re_selects_the_whole_corpus(
        self, tmp_path: Path
    ) -> None:
        baseline = self._baseline_hash(tmp_path)
        plan = _plan(
            tmp_path / "remapped",
            _stage(
                tmp_path,
                done_by_hash=self._done_at(baseline),
                mapping_hash="9" * 64,
            ),
        )
        assert plan["parameter_set_hash"] != baseline
        assert plan["counts"]["unrun"] == len(UNIVERSE)

    def test_only_the_un_run_set_is_ranked(self, tmp_path: Path) -> None:
        """Ranking the whole corpus to throw most of it away is the cost this
        derivation exists to avoid."""
        baseline = self._baseline_hash(tmp_path)
        done = self._done_at(baseline)
        done[baseline] = done[baseline][:2]
        stage = _stage(tmp_path, done_by_hash=done)
        _plan(tmp_path / "partial", stage)
        assert stage.ranked_calls == [2]


# ---------------------------------------------------------------------------
# Provenance that is deliberately NOT a key
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_the_plan_records_roster_fingerprint_as_provenance(
        self, tmp_path: Path
    ) -> None:
        roster = _roster()
        plan = _plan(tmp_path, _stage(tmp_path, roster=roster))
        assert plan["roster_fingerprint"] == roster.fingerprint()

    def test_the_plan_records_the_transport_allocation_per_seat(
        self, tmp_path: Path
    ) -> None:
        """Expected to DIFFER between runs: which gateway each seat is routed
        over is the thing being iterated on to allocate token cost."""
        plan = _plan(tmp_path, _stage(tmp_path))
        allocation = plan["transport_allocation"]
        assert [row["seat_name"] for row in allocation] == [
            "proposer_1",
            "proposer_2",
            "proposer_3",
            "critic",
            "meta",
        ]
        assert [row["role"] for row in allocation] == [
            "proposer",
            "proposer",
            "proposer",
            "critic",
            "meta",
        ]
        by_seat = {row["seat_name"]: row for row in allocation}
        assert by_seat["proposer_1"]["gateway"] == "argo-anl"
        assert by_seat["proposer_2"]["gateway"] == "mag-shared"
        assert by_seat["proposer_2"]["endpoint_kind"] == "openai"

    def test_every_seat_carries_its_own_route_fingerprint(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path, _stage(tmp_path))
        prints = {row["route_fingerprint"] for row in plan["transport_allocation"]}
        assert len(prints) == 5
        assert all(len(value) == 64 for value in prints)

    def test_a_transport_change_moves_the_provenance_but_not_the_key(
        self, tmp_path: Path
    ) -> None:
        """The whole point of recording both: one answers "where did this come
        from", the other "is this the same experiment"."""
        base = _plan(tmp_path / "a", _stage(tmp_path))
        moved = _plan(tmp_path / "b", _stage(tmp_path, roster=_rerouted_roster()))
        assert base["parameter_set_hash"] == moved["parameter_set_hash"]
        assert base["bench_fingerprint"] == moved["bench_fingerprint"]
        assert base["roster_fingerprint"] != moved["roster_fingerprint"]
        assert base["transport_allocation"] != moved["transport_allocation"]


# ---------------------------------------------------------------------------
# Chunking -- a workaround, recorded as one
# ---------------------------------------------------------------------------


class TestChunking:
    def test_the_chunks_are_ordered_and_cover_the_submit_set_exactly_once(
        self, tmp_path: Path
    ) -> None:
        universe = _universe(
            *((_hash(i), f"activity {i}") for i in range(10))
        )
        plan = _plan(
            tmp_path, _stage(tmp_path, universe=universe), chunk_size=3
        )
        chunks = plan["chunks"]
        assert [chunk["index"] for chunk in chunks] == [0, 1, 2, 3]
        assert [chunk["items"] for chunk in chunks] == [3, 3, 3, 1]
        assert [chunk["offset"] for chunk in chunks] == [0, 3, 6, 9]
        assert sum(chunk["items"] for chunk in chunks) == plan["counts"]["submit"]
        items = load_plan(plan["plan_file"]).items
        for chunk in chunks:
            window = items[chunk["offset"] : chunk["offset"] + chunk["items"]]
            assert window[0].item_id == chunk["first_item_id"]
            assert window[-1].item_id == chunk["last_item_id"]

    def test_the_default_chunk_size_is_two_thousand(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path, _stage(tmp_path))
        assert plan["concurrency"]["chunk_size"] == 2000
        assert omu.PLAN_CHUNK_SIZE == 2000

    def test_the_concurrency_block_is_in_the_plan_and_cites_issue_84(
        self, tmp_path: Path
    ) -> None:
        """A workaround baked in as a code constant becomes permanent
        silently, and this one must change the day issue 84 is fixed."""
        block = _plan(tmp_path, _stage(tmp_path))["concurrency"]
        assert block["chunks_in_flight"] == 1
        assert block["issue"] == "KBUtilLib issue 84"
        assert "claim_calls" in block["rationale"]
        assert "217 ms" in block["rationale"]

    def test_the_concurrency_block_carries_the_measured_per_gateway_limits(
        self, tmp_path: Path
    ) -> None:
        gateways = _plan(tmp_path, _stage(tmp_path))["concurrency"]["gateways"]
        assert gateways["mag"]["queues_sum_to"] == 6
        assert gateways["mag"]["max_parallel_requests"] == 20
        assert gateways["mag"]["requests_per_minute"] == 120
        assert gateways["mag"]["scope"] == "account-wide"
        assert gateways["argo"] == {
            "chair": 24,
            "critic": 8,
            "meta": 8,
            "note": gateways["argo"]["note"],
        }

    def test_the_live_configs_declared_queues_are_recorded_too(
        self, tmp_path: Path
    ) -> None:
        """So an operator can see whether the config the run will execute
        under matches the shape the plan assumes."""
        declared = _plan(tmp_path, _stage(tmp_path))["concurrency"]["declared_queues"]
        assert [row["queue"] for row in declared] == ["mag_luna", "mag_sol"]
        assert sum(row["concurrency"] * row["workers"] for row in declared) == 6

    def test_a_chunk_size_below_one_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="at least one item"):
            _plan(tmp_path, _stage(tmp_path), chunk_size=0)

    def test_an_empty_submit_set_yields_no_chunks_rather_than_an_empty_one(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(
            tmp_path,
            _stage(tmp_path, universe=_universe((_hash(4), "hypothetical protein"))),
        )
        assert plan["counts"]["submit"] == 0
        assert plan["chunks"] == []
        assert plan["counts"]["abstain"] == 1
