"""Stage 1 split in two: the IN-POD un-run read, and the off-pod planner.

WHY THIS EXISTS. The off-pod clearinghouse read worked from h100 on
2026-10-22 12:03 CDT and FAILED on 2026-10-23 00:35 CDT with an auth error:
``~/.kbase/token`` on poplar had expired at 27 days old. Stage 1 is
SCHEDULABLE and UNATTENDED, so an expiry means the plan silently is not
produced and nobody learns until somebody looks. After the split the read
happens in the pod, where the credential lives, and an immutable bundle
crosses to h100 -- which then holds no BERDL credential at all.

THE COLLABORATORS ARE IMPORTED FROM ``test_ontomap_council_plan``, not
re-declared. That module already owns "what the clearinghouse hands back" and
"what the roster looks like", and a second copy here would be free to drift
from the one the plan tests assert against -- the same arrangement
``test_ontomap_council_publish`` has with ``test_ontomap_council_export``.

WHAT IS REAL. Everything except the retrieval backend and the two
clearinghouse reads: ``council_unrun_bundle`` writes a real bundle with a
real sha256 inventory, ``read_unrun_bundle`` verifies it, and
``council_plan(bundle=...)`` is the real planner producing a real
``PLAN.json``. The no-BERDL-client test patches the clearinghouse
CONSTRUCTOR to raise and then runs the planner with its DEFAULT
collaborators -- so reaching ``PLAN.json`` proves no client was built,
rather than proving a fake was accepted.

No test here makes a network call, spends a token, or needs a pod.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytest.importorskip("numpy", reason="the plan stage imports the retrieval module")

from kbutillib.domains.genome.ontology_mapping import (  # noqa: E402
    ontology_mapping_utils as omu,
)
from kbutillib.domains.genome.ontology_mapping.ontology_mapping_utils import (  # noqa: E402
    UNRUN_BUNDLE_FILENAME,
    UNRUN_BUNDLE_ROWS_FILENAME,
    UNRUN_BUNDLE_SCHEMA_VERSION,
    read_unrun_bundle,
)
from tests.domains.test_ontomap_council_plan import (  # noqa: E402
    ABSTAIN_TEXTS,
    ENDPOINTS,
    MAPPING_HASH,
    QUERY,
    SOURCE,
    UNIVERSE,
    _FakeClearinghouse,
    _FakeConfig,
    _FakeCouncil,
    _Stage1,
    _hash,
    _roster,
)

#: The parameter set the in-pod reader is handed. It is NEVER derived in the
#: pod: deriving it needs the council roster and the retrieval mapping
#: version, which are exactly the requirements the split keeps out of there.
PARAMETER_SET: Dict[str, Any] = {
    "query": QUERY,
    "bench_fingerprint": "b" * 64,
    "mapping_hash": MAPPING_HASH,
}


def _stage(
    *,
    universe: Optional[List[Dict[str, Any]]] = None,
    done_by_hash: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> _Stage1:
    clearinghouse = _FakeClearinghouse(
        UNIVERSE if universe is None else universe, done_by_hash
    )
    council = _FakeCouncil(_roster(), _FakeConfig(ENDPOINTS))
    return _Stage1(
        clearinghouse, council, mapping_hash=MAPPING_HASH, abstain_texts=ABSTAIN_TEXTS
    )


def _bundle(tmp_path: Path, stage: _Stage1, **kwargs: Any) -> Dict[str, Any]:
    kwargs.setdefault("query", QUERY)
    kwargs.setdefault("parameter_set", PARAMETER_SET)
    return stage.council_unrun_bundle(tmp_path / "bundle", **kwargs)


def _plan_from_bundle(
    tmp_path: Path, stage: _Stage1, bundle_path: Any, **kwargs: Any
) -> Dict[str, Any]:
    kwargs.setdefault("query", QUERY)
    kwargs.setdefault("parameter_set", PARAMETER_SET)
    kwargs.setdefault("scratch_dir", tmp_path / "scratch")
    return stage.council_plan(
        tmp_path / "run" / "from-bundle", bundle=bundle_path, **kwargs
    )


def _manifest(bundle: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(Path(bundle["bundle_file"]).read_text())


def _rewrite_manifest(bundle: Dict[str, Any], **changes: Any) -> None:
    path = Path(bundle["bundle_file"])
    manifest = json.loads(path.read_text())
    manifest.update(changes)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# What the in-pod reader produces
# --------------------------------------------------------------------------


class TestTheBundleTheInPodReaderWrites:
    def test_it_writes_a_manifest_and_a_rows_file(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage())
        assert Path(bundle["bundle_file"]).name == UNRUN_BUNDLE_FILENAME
        assert Path(bundle["rows_path"]).name == UNRUN_BUNDLE_ROWS_FILENAME
        assert Path(bundle["bundle_file"]).is_file()
        assert Path(bundle["rows_path"]).is_file()

    def test_the_bundle_is_versioned(self, tmp_path: Path) -> None:
        # VERSIONED because the bundle crosses machines and the two ends are
        # deployed independently; a consumer that had to guess at the shape
        # would be guessing about which work to plan.
        assert (
            _manifest(_bundle(tmp_path, _stage()))["schema_version"]
            == UNRUN_BUNDLE_SCHEMA_VERSION
        )

    def test_it_carries_the_query_id_and_the_exact_definition_text(
        self, tmp_path: Path
    ) -> None:
        bundle = _bundle(tmp_path, _stage())
        rows = [
            json.loads(line)
            for line in Path(bundle["rows_path"]).read_text().splitlines()
        ]
        assert {row["query_id"] for row in rows} == {_hash(n) for n in (1, 2, 3, 4)}
        by_id = {row["query_id"]: row["text"] for row in rows}
        assert by_id[_hash(1)] == "glucokinase"
        assert by_id[_hash(4)] == "hypothetical protein"

    def test_it_carries_the_canonical_parameter_set_and_its_hash(
        self, tmp_path: Path
    ) -> None:
        manifest = _manifest(_bundle(tmp_path, _stage()))
        assert manifest["parameter_set"] == PARAMETER_SET
        assert len(manifest["parameter_set_hash"]) == 64
        # The canonical PRE-IMAGE travels too, so the hash rule stays
        # recomputable from the bundle alone.
        assert json.loads(manifest["parameter_set_canonical_json"]) == PARAMETER_SET

    def test_the_hash_is_the_clearinghouses_own_and_not_a_local_rule(
        self, tmp_path: Path
    ) -> None:
        from kbutillib.domains.kbase.berdl.clearinghouse_parameters import (
            result_parameter_set_identity,
        )

        _canonical, expected = result_parameter_set_identity(
            PARAMETER_SET, source=SOURCE, where="test"
        )
        assert _manifest(_bundle(tmp_path, _stage()))["parameter_set_hash"] == expected

    def test_it_carries_the_source_the_extraction_time_and_the_counts(
        self, tmp_path: Path
    ) -> None:
        manifest = _manifest(_bundle(tmp_path, _stage()))
        assert manifest["source"] == SOURCE
        assert manifest["query"] == QUERY
        assert manifest["generated_at"].endswith("Z")
        assert manifest["counts"] == {"universe": 4, "done": 0, "unrun": 4}

    def test_the_inventory_matches_its_content(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage())
        manifest = _manifest(bundle)
        entry = manifest["inventory"][UNRUN_BUNDLE_ROWS_FILENAME]
        rows_path = Path(bundle["rows_path"])
        assert entry["sha256"] == hashlib.sha256(rows_path.read_bytes()).hexdigest()
        assert entry["bytes"] == rows_path.stat().st_size
        assert entry["rows"] == 4

    def test_the_manifest_is_not_in_its_own_inventory(self, tmp_path: Path) -> None:
        # A file cannot carry its own digest, and the manifest is the thing
        # the digests are read FROM.
        inventory = _manifest(_bundle(tmp_path, _stage()))["inventory"]
        assert UNRUN_BUNDLE_FILENAME not in inventory

    def test_it_applies_the_done_filter_at_this_parameter_set(
        self, tmp_path: Path
    ) -> None:
        stage = _stage()
        bundle = _bundle(tmp_path, stage)
        psh = _manifest(bundle)["parameter_set_hash"]
        (call,) = stage._fake_clearinghouse.current_state_calls
        assert call["entity_type"] == "function"
        assert call["sources"] == [SOURCE]
        assert call["parameter_set_hashes"] == [psh]

    def test_a_function_already_done_at_this_parameter_set_is_excluded(
        self, tmp_path: Path
    ) -> None:
        stage = _stage()
        probe = _bundle(tmp_path / "probe", stage)
        psh = _manifest(probe)["parameter_set_hash"]

        done_stage = _stage(done_by_hash={psh: [{"entity_hash": _hash(2)}]})
        bundle = _bundle(tmp_path, done_stage)
        rows = [
            json.loads(line)
            for line in Path(bundle["rows_path"]).read_text().splitlines()
        ]
        assert {row["query_id"] for row in rows} == {_hash(n) for n in (1, 3, 4)}
        assert _manifest(bundle)["counts"] == {
            "universe": 4,
            "done": 1,
            "unrun": 3,
        }

    def test_it_does_not_rank_partition_chunk_or_quote(self, tmp_path: Path) -> None:
        stage = _stage()
        bundle = _bundle(tmp_path, stage)
        # The retrieval backend was never called...
        assert stage.ranked_calls == []
        # ...and nothing the planner produces is in the bundle or beside it.
        manifest = _manifest(bundle)
        for absent in (
            "chunks",
            "items_file",
            "abstain_file",
            "fanout_file",
            "ranked_file",
            "plan_hash",
            "transport_allocation",
            "concurrency",
        ):
            assert absent not in manifest, absent
        assert sorted(p.name for p in Path(bundle["bundle_dir"]).iterdir()) == [
            UNRUN_BUNDLE_FILENAME,
            UNRUN_BUNDLE_ROWS_FILENAME,
        ]

    def test_a_query_without_a_version_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="version"):
            _bundle(tmp_path, _stage(), query="go_mf")

    def test_an_empty_un_run_set_is_produced_but_warned_about_loudly(
        self, tmp_path: Path
    ) -> None:
        # The PRODUCER emits it and says so; the CONSUMER refuses it. Both
        # halves matter: a producer that refused outright could not be used
        # to show the consumer's refusal, and a producer that said nothing
        # would hand over a bundle that reads as a finished corpus.
        stage = _stage(universe=[])
        bundle = _bundle(tmp_path, stage)
        assert _manifest(bundle)["counts"]["unrun"] == 0
        assert bundle["warnings"]
        assert "EMPTY" in bundle["warnings"][0]


# --------------------------------------------------------------------------
# Verifying a bundle
# --------------------------------------------------------------------------


class TestReadingABundle:
    def _psh(self, tmp_path: Path, bundle: Dict[str, Any]) -> str:
        return _manifest(bundle)["parameter_set_hash"]

    def test_a_good_bundle_reads_back(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage())
        read = read_unrun_bundle(
            bundle["bundle_dir"],
            source=SOURCE,
            parameter_set_hash=self._psh(tmp_path, bundle),
        )
        assert len(read["rows"]) == 4
        assert read["counts"] == {"universe": 4, "done": 0, "unrun": 4}

    def test_the_manifest_file_may_be_named_directly(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage())
        read = read_unrun_bundle(
            bundle["bundle_file"],
            source=SOURCE,
            parameter_set_hash=self._psh(tmp_path, bundle),
        )
        assert len(read["rows"]) == 4

    def test_a_missing_bundle_is_a_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_unrun_bundle(
                tmp_path / "nope", source=SOURCE, parameter_set_hash="x" * 64
            )

    def test_a_corrupted_digest_is_refused_with_expected_and_observed(
        self, tmp_path: Path
    ) -> None:
        bundle = _bundle(tmp_path, _stage())
        psh = self._psh(tmp_path, bundle)
        expected = _manifest(bundle)["inventory"][UNRUN_BUNDLE_ROWS_FILENAME][
            "sha256"
        ]
        rows_path = Path(bundle["rows_path"])
        # Truncated in transit -- which reads as a SMALLER un-run set, plans
        # less work, and looks like progress.
        kept = rows_path.read_text().splitlines()[:2]
        rows_path.write_text("\n".join(kept) + "\n")
        observed = hashlib.sha256(rows_path.read_bytes()).hexdigest()

        with pytest.raises(ValueError) as excinfo:
            read_unrun_bundle(
                bundle["bundle_dir"], source=SOURCE, parameter_set_hash=psh
            )
        message = str(excinfo.value)
        assert expected in message
        assert observed in message
        assert "sha256" in message

    def test_a_missing_inventory_member_is_a_file_not_found(
        self, tmp_path: Path
    ) -> None:
        bundle = _bundle(tmp_path, _stage())
        psh = self._psh(tmp_path, bundle)
        Path(bundle["rows_path"]).unlink()
        with pytest.raises(FileNotFoundError):
            read_unrun_bundle(
                bundle["bundle_dir"], source=SOURCE, parameter_set_hash=psh
            )

    def test_an_absent_inventory_is_refused(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage())
        psh = self._psh(tmp_path, bundle)
        _rewrite_manifest(bundle, inventory={})
        with pytest.raises(ValueError, match="inventory"):
            read_unrun_bundle(
                bundle["bundle_dir"], source=SOURCE, parameter_set_hash=psh
            )

    def test_an_unknown_schema_version_is_refused(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage())
        psh = self._psh(tmp_path, bundle)
        _rewrite_manifest(bundle, schema_version=UNRUN_BUNDLE_SCHEMA_VERSION + 1)
        with pytest.raises(ValueError, match="schema_version"):
            read_unrun_bundle(
                bundle["bundle_dir"], source=SOURCE, parameter_set_hash=psh
            )

    def test_a_parameter_set_hash_mismatch_is_refused_naming_both(
        self, tmp_path: Path
    ) -> None:
        bundle = _bundle(tmp_path, _stage())
        theirs = self._psh(tmp_path, bundle)
        mine = "9" * 64
        with pytest.raises(ValueError) as excinfo:
            read_unrun_bundle(
                bundle["bundle_dir"], source=SOURCE, parameter_set_hash=mine
            )
        message = str(excinfo.value)
        assert theirs in message
        assert mine in message

    def test_a_source_mismatch_is_refused_naming_both(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage())
        psh = self._psh(tmp_path, bundle)
        with pytest.raises(ValueError) as excinfo:
            read_unrun_bundle(
                bundle["bundle_dir"],
                source="council/go_mf@9",
                parameter_set_hash=psh,
            )
        message = str(excinfo.value)
        assert SOURCE in message
        assert "council/go_mf@9" in message

    def test_an_empty_un_run_set_is_refused_as_a_producer_failure(
        self, tmp_path: Path
    ) -> None:
        bundle = _bundle(tmp_path, _stage(universe=[]))
        psh = self._psh(tmp_path, bundle)
        with pytest.raises(ValueError) as excinfo:
            read_unrun_bundle(
                bundle["bundle_dir"], source=SOURCE, parameter_set_hash=psh
            )
        message = str(excinfo.value)
        assert "EMPTY" in message
        assert "PRODUCER FAILURE" in message

    def test_a_row_count_the_producer_wrote_wrong_is_refused(
        self, tmp_path: Path
    ) -> None:
        bundle = _bundle(tmp_path, _stage())
        psh = self._psh(tmp_path, bundle)
        manifest = _manifest(bundle)
        inventory = dict(manifest["inventory"])
        entry = dict(inventory[UNRUN_BUNDLE_ROWS_FILENAME])
        entry["rows"] = 99  # digest still correct; the count is not
        inventory[UNRUN_BUNDLE_ROWS_FILENAME] = entry
        _rewrite_manifest(bundle, inventory=inventory)
        with pytest.raises(ValueError, match="declares 99 rows"):
            read_unrun_bundle(
                bundle["bundle_dir"], source=SOURCE, parameter_set_hash=psh
            )


# --------------------------------------------------------------------------
# Planning from a bundle
# --------------------------------------------------------------------------


class TestPlanningFromABundle:
    def test_the_plan_command_accepts_a_valid_bundle(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage())
        plan = _plan_from_bundle(tmp_path, _stage(), bundle["bundle_dir"])
        assert Path(plan["plan_file"]).is_file()
        assert plan["counts"]["unrun"] == 4
        assert plan["unrun_bundle_file"] == bundle["bundle_file"]

    def test_the_plan_is_equivalent_to_the_single_command_path(
        self, tmp_path: Path
    ) -> None:
        """Same inputs, two routes in, one plan.

        Compared key by key rather than by digest, because two plans written
        a second apart differ in ``generated_at``, in their ``run_dir`` and
        in which bundle (if any) they came from -- and nowhere else.
        """
        direct = _stage().council_plan(
            tmp_path / "run" / "direct",
            query=QUERY,
            parameter_set=PARAMETER_SET,
            scratch_dir=tmp_path / "scratch-direct",
        )
        bundle = _bundle(tmp_path, _stage())
        from_bundle = _stage().council_plan(
            tmp_path / "run" / "bundled",
            query=QUERY,
            parameter_set=PARAMETER_SET,
            bundle=bundle["bundle_dir"],
            scratch_dir=tmp_path / "scratch-bundled",
        )

        expected_to_differ = {
            "generated_at",
            "run_dir",
            "run_id",
            "unrun_bundle_file",
            "items_file",
            "abstain_file",
            "fanout_file",
            "ranked_file",
            "plan_file",
        }
        assert set(direct) == set(from_bundle)
        for key in sorted(set(direct) - expected_to_differ):
            assert direct[key] == from_bundle[key], key

        # The load-bearing ones, named explicitly rather than left to the
        # loop: the partition, the chunking and the plan identity.
        assert from_bundle["counts"] == direct["counts"]
        assert from_bundle["chunks"] == direct["chunks"]
        assert from_bundle["plan_hash"] == direct["plan_hash"]
        assert from_bundle["parameter_set_hash"] == direct["parameter_set_hash"]

    def test_the_universe_and_done_counts_come_from_the_bundle(
        self, tmp_path: Path
    ) -> None:
        # h100 never saw either set, so the two numbers are the pod's -- and
        # they are the same two numbers the direct path reports.
        probe = _bundle(tmp_path / "probe", _stage())
        psh = _manifest(probe)["parameter_set_hash"]
        bundle = _bundle(
            tmp_path, _stage(done_by_hash={psh: [{"entity_hash": _hash(2)}]})
        )
        plan = _plan_from_bundle(tmp_path, _stage(), bundle["bundle_dir"])
        assert plan["counts"]["universe"] == 4
        assert plan["counts"]["done"] == 1
        assert plan["counts"]["unrun"] == 3

    def test_everything_downstream_is_unchanged(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage())
        plan = _plan_from_bundle(tmp_path, _stage(), bundle["bundle_dir"])
        # Ranking, the three-way partition, the chunking and PLAN.json's
        # existing contents.
        assert plan["counts"]["submit"] == 3
        assert plan["counts"]["abstain"] == 1
        assert plan["counts"]["fanout"] == 3
        assert plan["chunks"]
        assert plan["plan_hash"]
        assert Path(plan["items_file"]).is_file()
        assert Path(plan["abstain_file"]).is_file()
        assert Path(plan["fanout_file"]).is_file()
        assert Path(plan["ranked_file"]).is_file()

    def test_a_corrupted_bundle_is_refused_before_any_retrieval_runs(
        self, tmp_path: Path
    ) -> None:
        bundle = _bundle(tmp_path, _stage())
        Path(bundle["rows_path"]).write_text("{}\n")
        stage = _stage()
        with pytest.raises(ValueError) as excinfo:
            _plan_from_bundle(tmp_path, stage, bundle["bundle_dir"])
        assert "sha256" in str(excinfo.value)
        # Refused BEFORE retrieval, not after: the backend was never called
        # and no plan was written.
        assert stage.ranked_calls == []
        assert not (tmp_path / "run" / "from-bundle").exists()

    def test_a_parameter_set_mismatch_is_refused(self, tmp_path: Path) -> None:
        bundle = _bundle(
            tmp_path,
            _stage(),
            parameter_set={**PARAMETER_SET, "bench_fingerprint": "c" * 64},
        )
        stage = _stage()
        with pytest.raises(ValueError, match="MANDATORY"):
            _plan_from_bundle(tmp_path, stage, bundle["bundle_dir"])
        assert stage.ranked_calls == []

    def test_a_bundle_for_another_query_version_is_refused(
        self, tmp_path: Path
    ) -> None:
        # Consuming it would incorrectly SKIP work that needs doing, because
        # "un-run" is defined at a parameter set.
        other = {**PARAMETER_SET, "query": "go_mf@2"}
        bundle = _bundle(tmp_path, _stage(), query="go_mf@2", parameter_set=other)
        with pytest.raises(ValueError):
            _plan_from_bundle(tmp_path, _stage(), bundle["bundle_dir"])

    def test_an_empty_bundle_is_refused(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage(universe=[]))
        stage = _stage()
        with pytest.raises(ValueError, match="PRODUCER FAILURE"):
            _plan_from_bundle(tmp_path, stage, bundle["bundle_dir"])
        assert stage.ranked_calls == []

    def test_an_unknown_schema_version_is_refused(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage())
        _rewrite_manifest(bundle, schema_version=99)
        with pytest.raises(ValueError, match="schema_version"):
            _plan_from_bundle(tmp_path, _stage(), bundle["bundle_dir"])

    def test_there_is_no_implicit_latest_bundle_selection(
        self, tmp_path: Path
    ) -> None:
        # Two bundles for two DIFFERENT parameter sets, the second newer.
        # Nothing picks one: the planner plans the bundle it was handed, and
        # the newer one is refused when it is not the one asked for.
        mine = _bundle(tmp_path / "mine", _stage())
        theirs = _bundle(
            tmp_path / "theirs",
            _stage(),
            parameter_set={**PARAMETER_SET, "bench_fingerprint": "d" * 64},
        )
        assert Path(theirs["bundle_file"]).stat().st_mtime >= Path(
            mine["bundle_file"]
        ).stat().st_mtime
        plan = _plan_from_bundle(tmp_path, _stage(), mine["bundle_dir"])
        assert plan["unrun_bundle_file"] == mine["bundle_file"]
        with pytest.raises(ValueError):
            _plan_from_bundle(tmp_path / "other", _stage(), theirs["bundle_dir"])

    def test_the_bundle_argument_has_no_default(self, tmp_path: Path) -> None:
        import inspect

        signature = inspect.signature(omu.OntologyMappingUtils.council_plan)
        assert signature.parameters["bundle"].default is None


# --------------------------------------------------------------------------
# No BERDL client on the h100 path
# --------------------------------------------------------------------------


class TestNoBerdlClientOnTheH100Path:
    """The REAL planner, with its DEFAULT collaborators, over a fixture bundle.

    The clearinghouse CONSTRUCTORS are patched to raise, so if the planner
    builds one at all the test fails with that exception rather than reaching
    ``PLAN.json``. Injecting a fake clearinghouse would prove the opposite of
    what is wanted here -- that a fake is accepted -- so nothing is injected:
    ``_clearinghouse`` is the shipped method on the real class.
    """

    class _Stage(omu.OntologyMappingUtils):
        """Only the retrieval BACKEND is stubbed; ``_clearinghouse`` is real."""

        def __init__(self) -> None:
            super().__init__(
                config_file=False, token_file=None, kbase_token_file=None
            )
            self.ranked_calls: List[int] = []

        def mapping_version(self) -> Dict[str, Any]:
            return {
                "mapping_version": "ontomap-go-text-union@1.0.0 (test)",
                "mapping_hash": MAPPING_HASH,
                "provenance": {"backend": "ontomap-go-text-union"},
            }

        def function_to_go(
            self, functions: Any, max_candidates: int = 40
        ) -> Dict[str, Any]:
            rows = list(functions)
            self.ranked_calls.append(len(rows))
            return {
                **self.mapping_version(),
                "results": [
                    {
                        "query_id": row["query_id"],
                        "text": row["text"],
                        "source": row.get("source") or "",
                        "abstained": False,
                        "abstain_reason": None,
                        "candidates": [
                            {
                                "go_id": "GO:0003824",
                                "name": "catalytic activity",
                                "def": "Catalysis of a biochemical reaction.",
                                "score": 0.5,
                                "height": 3,
                                "rank": 1,
                            }
                        ],
                    }
                    for row in rows
                ],
            }

        def _council(self) -> Any:
            return _FakeCouncil(_roster(), _FakeConfig(ENDPOINTS))

    @staticmethod
    def _poison(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make every BERDL/clearinghouse construction raise."""
        from kbutillib.domains.kbase.berdl import (
            capability as berdl_capability,
            clearinghouse_capability as ch_capability,
        )

        def _refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError(
                "a BERDL client was constructed on the h100 plan path; the "
                "whole point of the bundle is that h100 holds no BERDL "
                "credential"
            )

        monkeypatch.setattr(ch_capability, "ClearinghouseCapability", _refuse)
        monkeypatch.setattr(berdl_capability, "BerdlCapability", _refuse)

    def test_the_real_plan_command_reaches_plan_json_with_no_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = _bundle(tmp_path, _stage())

        self._poison(monkeypatch)
        stage = self._Stage()
        plan = stage.council_plan(
            tmp_path / "run" / "no-client",
            query=QUERY,
            parameter_set=PARAMETER_SET,
            bundle=bundle["bundle_dir"],
            scratch_dir=tmp_path / "scratch-no-client",
        )
        assert Path(plan["plan_file"]).is_file()
        assert plan["counts"]["unrun"] == 4
        assert stage.ranked_calls == [4]

    def test_the_poison_really_bites_on_the_direct_read_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The control: without a bundle the SAME stage builds a client and
        # the patched constructor fires. Without this, the test above would
        # pass even if the patch were ineffective.
        self._poison(monkeypatch)
        stage = self._Stage()
        with pytest.raises(AssertionError, match="BERDL client was constructed"):
            stage.council_plan(
                tmp_path / "run" / "direct",
                query=QUERY,
                parameter_set=PARAMETER_SET,
                scratch_dir=tmp_path / "scratch-direct",
            )


# --------------------------------------------------------------------------
# The decisions of the finished PRD, preserved
# --------------------------------------------------------------------------


class TestPreservedPrdDecisions:
    def test_abstentions_are_carried_forward_not_dropped(
        self, tmp_path: Path
    ) -> None:
        # What stops the un-run detector re-selecting them forever.
        bundle = _bundle(tmp_path, _stage())
        plan = _plan_from_bundle(tmp_path, _stage(), bundle["bundle_dir"])
        abstain = [
            json.loads(line)
            for line in Path(plan["abstain_file"]).read_text().splitlines()
        ]
        assert len(abstain) == 1
        (row,) = abstain
        assert row["query_id"] == _hash(4)
        assert row["decline_reason"].startswith("abstained:")

    def test_an_abstention_is_in_neither_the_submit_nor_the_fanout_set(
        self, tmp_path: Path
    ) -> None:
        bundle = _bundle(tmp_path, _stage())
        plan = _plan_from_bundle(tmp_path, _stage(), bundle["bundle_dir"])
        items = [
            json.loads(line)
            for line in Path(plan["items_file"]).read_text().splitlines()
        ]
        fanout = [
            json.loads(line)
            for line in Path(plan["fanout_file"]).read_text().splitlines()
        ]
        assert _hash(4) not in {row["query_id"] for row in items}
        assert _hash(4) not in {row["query_id"] for row in fanout}

    def test_a_council_error_item_is_in_neither_the_done_set_nor_a_result_row(
        self, tmp_path: Path
    ) -> None:
        """All calls failed, so no verdict -- genuinely un-run, and re-selected.

        The council-error case is modelled where it is observable from this
        stage: an item with no verdict produces NO clearinghouse result row,
        so it is absent from ``done`` and the next bundle selects it again.
        The export half of the same rule (no row is written for it) is
        asserted in ``tests/domains/test_ontomap_council_export.py``.
        """
        from kbutillib.domains.genome.ontology_mapping import verdicts as vx

        built = vx.export_rows(
            abstain=[],
            fanout=[
                {"query_id": _hash(1), "item_id": "item-ok", "text": "glucokinase"},
                {"query_id": _hash(2), "item_id": "item-err", "text": "glucose oxidase"},
            ],
            verdicts={
                "item-ok": {
                    "status": "ok",
                    "verdict": {"selected_ids": ["GO:0004340"], "n_valid": 3},
                    "decided_at": "2026-10-22T10:00:00+00:00",
                    "job_id": "job-a",
                },
                # council_error: every call failed, so the item never settled.
                "item-err": {
                    "status": "error",
                    "verdict": {},
                    "decided_at": None,
                    "job_id": "job-a",
                },
            },
            identity={
                "query_name": "go_mf",
                "query_version": "1",
                "roster_fingerprint": "r" * 64,
                "mapping_hash": MAPPING_HASH,
            },
            job_ids=["job-a"],
            store_id="poplar/council-1",
        )
        published = {row["query_id"] for row in built["rows"]}
        assert _hash(1) in published
        assert _hash(2) not in published
        assert built["counts"]["withheld"] == 1

        # And so the next bundle re-selects it: it is absent from `done`.
        probe = _bundle(tmp_path / "probe", _stage())
        psh = _manifest(probe)["parameter_set_hash"]
        bundle = _bundle(
            tmp_path, _stage(done_by_hash={psh: [{"entity_hash": _hash(1)}]})
        )
        rows = [
            json.loads(line)
            for line in Path(bundle["rows_path"]).read_text().splitlines()
        ]
        assert _hash(2) in {row["query_id"] for row in rows}
        assert _hash(1) not in {row["query_id"] for row in rows}

    def test_the_parameter_set_stays_query_bench_and_mapping_hash(
        self, tmp_path: Path
    ) -> None:
        # bench_fingerprint is model-only and deliberately NOT
        # roster_fingerprint: the bench partitions the corpus, the transport
        # does not.
        manifest = _manifest(_bundle(tmp_path, _stage()))
        assert set(manifest["parameter_set"]) == {
            "query",
            "bench_fingerprint",
            "mapping_hash",
        }
        assert "roster_fingerprint" not in manifest["parameter_set"]

    def test_the_chunking_is_unchanged(self, tmp_path: Path) -> None:
        bundle = _bundle(tmp_path, _stage())
        plan = _plan_from_bundle(tmp_path, _stage(), bundle["bundle_dir"])
        assert plan["concurrency"]
        # The default is PLAN_CHUNK_SIZE (~2,000) with one chunk in flight;
        # with three submit items that is one chunk.
        assert len(plan["chunks"]) == 1
        assert plan["chunks"][0]["items"] == 3

    def test_the_bundle_knows_nothing_about_the_council_store(
        self, tmp_path: Path
    ) -> None:
        # The first in-pod component must not depend on the council store or
        # the council namespace -- the archiver and the scanner come later.
        manifest = _manifest(_bundle(tmp_path, _stage()))
        text = json.dumps(manifest)
        for forbidden in ("store_id", "job_id", "item_id", "council_entity_hash"):
            assert forbidden not in text, forbidden
