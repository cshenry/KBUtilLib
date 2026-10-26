"""``council_status`` -- coverage by LIFECYCLE and by ``parameter_set_hash``.

The arithmetic is PURE (:func:`~council_status.lifecycle_coverage`,
:func:`~council_status.parameter_set_coverage`,
:func:`~council_status.concurrent_runs`) and the classification is six
``stat`` calls, so everything here runs without numpy, without pyarrow and
without a clearinghouse.

WHAT IS PINNED HERE, as against in the rehearsal:

* that the seven lifecycle buckets are a PARTITION of the corpus -- they add
  up, with no function in two of them and none in none of them;
* that ``paid_for_not_landed`` is reported, because work that has been paid
  for and has not landed is invisible in both a "what is un-run" count and a
  "what is loaded" count;
* that partial-version coverage is REPORTED rather than summed away;
* that a second run in stage 4 is surfaced;
* that an unknown answer reads ``None`` and never 0.

The end-to-end behaviour against real run directories and real stage
artifacts is in ``test_ontomap_pipeline_rehearsal.py``, which drives the real
stages; what would be wrong to check there is the arithmetic's edge cases,
because reaching them would mean building eight contrived runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from kbutillib.domains.genome.ontology_mapping import council_status as cstat

#: The corpus this module reasons about. Deliberately not 47,341: a round
#: number makes a wrong bucket obvious in the assertion rather than in the
#: arithmetic.
CORPUS = 100

CURRENT = "c" * 64
OLDER = "0" * 64


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_run(
    root: Path,
    name: str,
    *,
    artifacts: Any = (),
    unrun: int = 10,
    parameter_set_hash: str = CURRENT,
    planned_at: str = "2026-10-20T00:00:00Z",
    run_payload: Optional[Dict[str, Any]] = None,
) -> Path:
    """One run directory with *artifacts* present beside its ``PLAN.json``.

    ``artifacts`` names the stage keys, so a test reads as the lifecycle it is
    building (``("quote", "approval")``) rather than as a list of filenames.
    """
    names = cstat.stage_artifacts()
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / names["plan"]).write_text(
        json.dumps(
            {
                "run_id": name,
                "query": "go_mf@1",
                "source": "council/go_mf@1",
                "parameter_set_hash": parameter_set_hash,
                "plan_hash": "p" * 64,
                "generated_at": planned_at,
                "counts": {
                    "universe": CORPUS,
                    "done": CORPUS - unrun,
                    "unrun": unrun,
                    "submit": unrun,
                    "abstain": 0,
                    "fanout": unrun,
                },
            }
        )
    )
    for key in artifacts:
        payload: Dict[str, Any] = {}
        if key == "run":
            payload = run_payload if run_payload is not None else {"complete": True}
        if key == "receipt":
            payload = {"ok": True}
        (directory / names[key]).write_text(json.dumps(payload))
    return directory


def _done(hashes_by_parameter_set: Dict[str, List[int]]) -> List[Dict[str, Any]]:
    """Current-state rows: ``{parameter_set_hash: [entity indices]}``."""
    return [
        {"entity_hash": f"{index:064x}", "parameter_set_hash": digest}
        for digest, indices in hashes_by_parameter_set.items()
        for index in indices
    ]


class _FakeClearinghouse:
    def __init__(
        self,
        done: Optional[List[Dict[str, Any]]] = None,
        *,
        entity_rows: int = CORPUS,
        content_rows: Optional[int] = None,
        stats_raises: bool = False,
        current_state_raises: bool = False,
    ) -> None:
        self._done = list(done or [])
        self._entity_rows = entity_rows
        self._content_rows = entity_rows if content_rows is None else content_rows
        self._stats_raises = stats_raises
        self._current_state_raises = current_state_raises
        self.sources_asked: List[Any] = []

    def stats(self, **_kw: Any) -> Dict[str, Any]:
        if self._stats_raises:
            raise RuntimeError("the delta API is unreachable")
        return {
            "tables": [
                {"name": "function_entity", "row_count": self._entity_rows},
                {"name": "function_content", "row_count": self._content_rows},
            ],
            "warnings": [],
        }

    def current_state(self, entity_type: str, **kwargs: Any) -> List[Dict[str, Any]]:
        self.sources_asked.append(kwargs.get("sources"))
        if self._current_state_raises:
            raise RuntimeError("Spark is not available off-pod")
        return [dict(row) for row in self._done]


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    return tmp_path / "runs"


def _states(status: Dict[str, Any]) -> Dict[str, Any]:
    return {row["state"]: row["functions"] for row in status["lifecycle"]["states"]}


# ---------------------------------------------------------------------------
# The artifact names
# ---------------------------------------------------------------------------


class TestTheArtifactNamesComeFromTheStagesThatWriteThem:
    """Imported, never restated.

    A status verb with its own copy of ``PUBLISH.json`` would report "not
    published" about a published run the day either side renamed the file.
    """

    def test_each_name_is_the_writing_stages_own_constant(self) -> None:
        from kbutillib.domains.ai.council.approval import (
            APPROVAL_FILENAME,
            RUN_FILENAME,
        )
        from kbutillib.domains.ai.council.quote import PLAN_FILENAME, QUOTE_FILENAME
        from kbutillib.domains.genome.ontology_mapping.council_publish import (
            PUBLISH_FILENAME,
        )
        from kbutillib.domains.kbase.berdl.clearinghouse_ingest import (
            RECEIPT_FILENAME,
        )

        assert cstat.stage_artifacts() == {
            "plan": PLAN_FILENAME,
            "quote": QUOTE_FILENAME,
            "approval": APPROVAL_FILENAME,
            "run": RUN_FILENAME,
            "publish": PUBLISH_FILENAME,
            "receipt": RECEIPT_FILENAME,
        }

    def test_resolving_them_is_lazy_so_importing_this_module_is_cheap(self) -> None:
        """No module-level constant, because resolving one pulls sqlalchemy.

        ``council.quote`` imports sqlalchemy and ``council_publish`` reaches
        the berdl stack; a module-level resolution would pull both into every
        importer, including ``kbu --help``.
        """
        assert not hasattr(cstat, "STAGE_ARTIFACTS")
        assert cstat.stage_artifacts() is cstat.stage_artifacts()


# ---------------------------------------------------------------------------
# One run directory
# ---------------------------------------------------------------------------


class TestClassifyingOneRunDirectory:
    @pytest.mark.parametrize(
        ("artifacts", "expected"),
        [
            ((), "planned-not-quoted"),
            (("quote",), "quoted-not-approved"),
            (("quote", "approval"), "approved-not-run"),
            (("quote", "approval", "run"), "running"),
            (("quote", "approval", "run", "publish"), "exported-not-loaded"),
            (("quote", "approval", "run", "publish", "receipt"), "loaded"),
        ],
    )
    def test_the_state_is_a_function_of_which_artifacts_exist(
        self, runs_root: Path, artifacts: Any, expected: str
    ) -> None:
        directory = _make_run(runs_root, "r", artifacts=artifacts)
        assert cstat.run_lifecycle(directory)["state"] == expected

    def test_a_settled_but_unpublished_run_is_running_and_says_it_is_complete(
        self, runs_root: Path
    ) -> None:
        """The fold that makes the seven states a PARTITION.

        Taken literally the PRD's seven leave "ran, not published" in none of
        them, and a lifecycle report whose buckets do not add up to the corpus
        is not a report. The per-run row keeps the distinction.
        """
        directory = _make_run(
            runs_root, "r", artifacts=("quote", "approval", "run")
        )
        row = cstat.run_lifecycle(directory)
        assert row["state"] == "running"
        assert row["complete"] is True
        assert row["published"] is False

    def test_a_directory_with_no_plan_is_not_a_run(self, runs_root: Path) -> None:
        """A runs root on a synced surface accumulates things that are not runs."""
        stray = runs_root / "scratch"
        stray.mkdir(parents=True)
        (stray / "notes.txt").write_text("hello")
        assert cstat.run_lifecycle(stray)["state"] is None

    def test_the_function_count_is_the_plans_unrun_and_not_its_submit(
        self, runs_root: Path
    ) -> None:
        """One paid answer can serve several functions.

        ``submit`` is the paid answers; ``unrun`` (= fanout + abstain) is the
        functions. Using ``submit`` would under-report coverage of the corpus
        by exactly the fan-out.
        """
        directory = _make_run(runs_root, "r", unrun=7)
        plan_path = directory / cstat.stage_artifacts()["plan"]
        plan = json.loads(plan_path.read_text())
        plan["counts"].update(submit=4, fanout=7, abstain=0, unrun=7)
        plan_path.write_text(json.dumps(plan))
        row = cstat.run_lifecycle(directory)
        assert row["functions"] == 7
        assert row["items"] == 4

    def test_an_unparseable_artifact_counts_for_the_state_and_not_for_the_fields(
        self, runs_root: Path
    ) -> None:
        """A stage that wrote a file DID run, even if the file will not parse.

        The run directory is Dropbox-synced and a partial sync is not the
        writer's doing; treating the file as absent would report a stage that
        ran as one that never did.
        """
        directory = _make_run(runs_root, "r", artifacts=("quote", "approval", "run"))
        (directory / cstat.stage_artifacts()["run"]).write_text("{truncated")
        row = cstat.run_lifecycle(directory)
        assert row["state"] == "running"
        assert row["unreadable"] == ["run"]
        assert row["complete"] is None

    def test_a_legitimately_empty_artifact_is_NOT_reported_as_unreadable(
        self, runs_root: Path
    ) -> None:
        """"Parsed to an empty dict" and "would not parse" are different things."""
        directory = _make_run(runs_root, "r", artifacts=("quote",))
        assert (directory / cstat.stage_artifacts()["quote"]).read_text() == "{}"
        assert cstat.run_lifecycle(directory)["unreadable"] == []


# ---------------------------------------------------------------------------
# Lifecycle coverage
# ---------------------------------------------------------------------------


class TestLifecycleCoverage:
    def test_the_seven_buckets_are_a_partition_of_the_corpus(
        self, runs_root: Path
    ) -> None:
        runs = [
            cstat.run_lifecycle(
                _make_run(runs_root, "a", artifacts=(), unrun=5)
            ),
            cstat.run_lifecycle(
                _make_run(runs_root, "b", artifacts=("quote",), unrun=6)
            ),
            cstat.run_lifecycle(
                _make_run(runs_root, "c", artifacts=("quote", "approval"), unrun=7)
            ),
            cstat.run_lifecycle(
                _make_run(
                    runs_root,
                    "d",
                    artifacts=("quote", "approval", "run"),
                    unrun=8,
                    run_payload={"complete": False},
                )
            ),
            cstat.run_lifecycle(
                _make_run(
                    runs_root,
                    "e",
                    artifacts=("quote", "approval", "run", "publish"),
                    unrun=9,
                )
            ),
        ]
        coverage = cstat.lifecycle_coverage(corpus=CORPUS, loaded=20, runs=runs)
        counts = {row["state"]: row["functions"] for row in coverage["states"]}
        assert counts == {
            "un-run": CORPUS - 20 - (5 + 6 + 7 + 8 + 9),
            "planned-not-quoted": 5,
            "quoted-not-approved": 6,
            "approved-not-run": 7,
            "running": 8,
            "exported-not-loaded": 9,
            "loaded": 20,
        }
        assert sum(counts.values()) == CORPUS

    def test_paid_for_not_landed_is_running_plus_exported_not_loaded(
        self, runs_root: Path
    ) -> None:
        """THE NUMBER THIS VERB EXISTS FOR.

        Tokens spent and nothing in the clearinghouse. Invisible in both an
        un-run count and a loaded count, and exactly what a crashed run
        leaves behind.
        """
        runs = [
            cstat.run_lifecycle(
                _make_run(
                    runs_root, "running", artifacts=("quote", "approval", "run"),
                    unrun=11,
                )
            ),
            cstat.run_lifecycle(
                _make_run(
                    runs_root,
                    "published",
                    artifacts=("quote", "approval", "run", "publish"),
                    unrun=13,
                )
            ),
            cstat.run_lifecycle(
                _make_run(runs_root, "planned", artifacts=(), unrun=17)
            ),
        ]
        coverage = cstat.lifecycle_coverage(corpus=CORPUS, loaded=0, runs=runs)
        assert coverage["paid_for_not_landed"] == 11 + 13

    def test_un_run_and_loaded_carry_no_run_count(self, runs_root: Path) -> None:
        """They are properties of the CORPUS, not of any run directory.

        Reporting 0 there would read as "no runs have loaded anything".
        """
        coverage = cstat.lifecycle_coverage(corpus=CORPUS, loaded=4, runs=[])
        by_state = {row["state"]: row for row in coverage["states"]}
        assert by_state["un-run"]["runs"] is None
        assert by_state["loaded"]["runs"] is None
        assert by_state["running"]["runs"] == 0

    def test_an_overclaim_clamps_un_run_at_zero_and_declares_it_a_floor(
        self, runs_root: Path
    ) -> None:
        """The in-flight claims are AS-OF PLAN TIME and can overlap ``loaded``.

        A published run whose rows landed and whose receipt write was lost is
        counted in ``exported-not-loaded`` by its files AND in ``loaded`` by
        the table. That is a real condition worth seeing, so it is reported
        rather than silently reconciled.
        """
        runs = [
            cstat.run_lifecycle(
                _make_run(
                    runs_root,
                    "published",
                    artifacts=("quote", "approval", "run", "publish"),
                    unrun=CORPUS,
                )
            )
        ]
        coverage = cstat.lifecycle_coverage(corpus=CORPUS, loaded=CORPUS, runs=runs)
        counts = {row["state"]: row["functions"] for row in coverage["states"]}
        assert counts["un-run"] == 0
        assert coverage["overclaimed"] == CORPUS
        assert coverage["un_run_is_a_floor"] is True

    @pytest.mark.parametrize(
        ("corpus", "loaded"), [(None, 10), (CORPUS, None), (None, None)]
    )
    def test_an_unknown_input_makes_un_run_None_and_never_zero(
        self, corpus: Optional[int], loaded: Optional[int]
    ) -> None:
        coverage = cstat.lifecycle_coverage(corpus=corpus, loaded=loaded, runs=[])
        counts = {row["state"]: row["functions"] for row in coverage["states"]}
        assert counts["un-run"] is None
        assert coverage["overclaimed"] is None

    def test_the_states_are_reported_in_pipeline_order(self) -> None:
        coverage = cstat.lifecycle_coverage(corpus=CORPUS, loaded=0, runs=[])
        assert [row["state"] for row in coverage["states"]] == list(
            cstat.LIFECYCLE_STATES
        )


# ---------------------------------------------------------------------------
# Parameter-set coverage
# ---------------------------------------------------------------------------


class TestParameterSetCoverage:
    def test_partial_version_coverage_is_reported_and_not_summed_away(self) -> None:
        """Re-running the whole corpus on every bump is unaffordable.

        So a corpus legitimately carries several parameter sets at once -- and
        "is this dataset consistent?" only has an answer if the split is
        published. 10 current rows and 30 older rows must not read as 40
        covered functions.
        """
        coverage = cstat.parameter_set_coverage(
            _done({CURRENT: list(range(10)), OLDER: list(range(30))}),
            corpus=CORPUS,
            current=CURRENT,
        )
        rows = {row["parameter_set_hash"]: row for row in coverage["parameter_sets"]}
        assert rows[CURRENT]["functions"] == 10
        assert rows[OLDER]["functions"] == 30
        # The first ten entities appear at BOTH, so the distinct union is 30.
        assert coverage["covered_at_all"] == 30
        assert coverage["covered_only_by_an_older_parameter_set"] == 20
        assert coverage["uncovered"] == CORPUS - 30

    def test_the_current_parameter_set_is_reported_first(self) -> None:
        """It is the one an operator is asking about, even when it is smallest."""
        coverage = cstat.parameter_set_coverage(
            _done({OLDER: list(range(30)), CURRENT: [0]}),
            corpus=CORPUS,
            current=CURRENT,
        )
        assert coverage["parameter_sets"][0]["parameter_set_hash"] == CURRENT
        assert coverage["parameter_sets"][0]["current"] is True

    def test_a_current_parameter_set_with_no_rows_is_still_reported_as_zero(
        self,
    ) -> None:
        """Absent from the answer and present at zero are different statements."""
        coverage = cstat.parameter_set_coverage(
            _done({OLDER: list(range(5))}), corpus=CORPUS, current=CURRENT
        )
        assert coverage["parameter_sets"][0] == {
            "parameter_set_hash": CURRENT,
            "current": True,
            "functions": 0,
            "share": 0.0,
        }
        assert coverage["covered_only_by_an_older_parameter_set"] == 5

    def test_with_no_current_set_the_stale_count_is_None_rather_than_everything(
        self,
    ) -> None:
        coverage = cstat.parameter_set_coverage(
            _done({OLDER: list(range(5))}), corpus=CORPUS, current=None
        )
        assert coverage["covered_only_by_an_older_parameter_set"] is None
        assert coverage["covered_at_all"] == 5

    def test_a_row_missing_either_key_is_refused_rather_than_dropped(self) -> None:
        """Dropping it would under-report coverage with nothing to explain the gap."""
        with pytest.raises(ValueError, match="parameter_set_hash"):
            cstat.parameter_set_coverage(
                [{"entity_hash": "a" * 64}], corpus=CORPUS, current=CURRENT
            )
        with pytest.raises(ValueError, match="entity_hash"):
            cstat.parameter_set_coverage(
                [{"parameter_set_hash": CURRENT}], corpus=CORPUS, current=CURRENT
            )

    def test_shares_are_None_when_the_corpus_size_is_unknown(self) -> None:
        coverage = cstat.parameter_set_coverage(
            _done({CURRENT: [0, 1]}), corpus=None, current=CURRENT
        )
        assert coverage["parameter_sets"][0]["share"] is None
        assert coverage["uncovered"] is None


# ---------------------------------------------------------------------------
# A second concurrent run
# ---------------------------------------------------------------------------


class TestASecondConcurrentRun:
    """Two runs in stage 4 defeat the chunking entirely.

    One chunk in flight is load-bearing (KBUtilLib issue 84) and the daemon
    drains every claimable job, so two runs interleave their chunks through it
    and rebuild the pending set the chunking exists to avoid. Neither run can
    see the other.
    """

    def _running(self, runs_root: Path, name: str, *, complete: bool) -> Dict[str, Any]:
        return cstat.run_lifecycle(
            _make_run(
                runs_root,
                name,
                artifacts=("quote", "approval", "run"),
                run_payload={"complete": complete},
            )
        )

    def test_two_incomplete_runs_are_surfaced_with_their_directories(
        self, runs_root: Path
    ) -> None:
        rows = [
            self._running(runs_root, "one", complete=False),
            self._running(runs_root, "two", complete=False),
        ]
        concurrent = cstat.concurrent_runs(rows)
        assert {row["run_id"] for row in concurrent} == {"one", "two"}
        # A path, not a boolean: the remedy is a STOP file in one of them.
        assert all(row["run_dir"] for row in concurrent)

    def test_one_incomplete_run_is_not_concurrent_with_itself(
        self, runs_root: Path
    ) -> None:
        assert cstat.concurrent_runs([self._running(runs_root, "one", complete=False)]) == []

    def test_a_settled_unpublished_run_does_not_cry_wolf(
        self, runs_root: Path
    ) -> None:
        """It is claiming no daemon capacity.

        Reporting it would raise the alarm on every operator who had not got
        round to ``council-publish`` yet.
        """
        rows = [
            self._running(runs_root, "draining", complete=False),
            self._running(runs_root, "settled", complete=True),
        ]
        assert cstat.concurrent_runs(rows) == []

    def test_the_verb_warns_about_it_naming_issue_84_and_the_remedy(
        self, runs_root: Path
    ) -> None:
        for name in ("one", "two"):
            _make_run(
                runs_root,
                name,
                artifacts=("quote", "approval", "run"),
                run_payload={"complete": False},
            )
        status = cstat.council_status(
            runs_root, clearinghouse=_FakeClearinghouse()
        )
        assert len(status["concurrent_runs"]) == 2
        warning = next(
            line for line in status["warnings"] if "STAGE 4 AT THE SAME TIME" in line
        )
        assert "KBUtilLib issue 84" in warning
        assert "STOP file" in warning


# ---------------------------------------------------------------------------
# The verb
# ---------------------------------------------------------------------------


class TestTheVerb:
    def test_it_reads_the_current_state_for_this_querys_source_only(
        self, runs_root: Path
    ) -> None:
        _make_run(runs_root, "r")
        cap = _FakeClearinghouse()
        status = cstat.council_status(runs_root, clearinghouse=cap)
        assert status["source"] == "council/go_mf@1"
        assert cap.sources_asked == [["council/go_mf@1"]]

    def test_the_current_parameter_set_comes_from_the_NEWEST_plan(
        self, runs_root: Path
    ) -> None:
        """A runs root holds every historical run.

        The oldest plan's parameter set is precisely the stale one the
        partition half reports, so it must not be the one "current" means.
        """
        _make_run(
            runs_root, "old", parameter_set_hash=OLDER, planned_at="2026-09-01T00:00:00Z"
        )
        _make_run(
            runs_root,
            "new",
            parameter_set_hash=CURRENT,
            planned_at="2026-10-22T00:00:00Z",
        )
        status = cstat.council_status(runs_root, clearinghouse=_FakeClearinghouse())
        assert status["parameter_set_hash"] == CURRENT

    def test_an_in_flight_run_at_another_parameter_set_is_warned_about(
        self, runs_root: Path
    ) -> None:
        """Its work will land at its OWN hash and not count towards current."""
        _make_run(
            runs_root,
            "stale",
            artifacts=("quote", "approval", "run"),
            parameter_set_hash=OLDER,
            planned_at="2026-09-01T00:00:00Z",
        )
        _make_run(
            runs_root,
            "fresh",
            parameter_set_hash=CURRENT,
            planned_at="2026-10-22T00:00:00Z",
        )
        status = cstat.council_status(runs_root, clearinghouse=_FakeClearinghouse())
        assert any(
            "DIFFERENT parameter set" in line for line in status["warnings"]
        )

    def test_with_no_resolvable_parameter_set_un_run_is_None_and_the_split_still_answers(
        self, tmp_path: Path
    ) -> None:
        """Deriving it live would need the retrieval backend and the live bench.

        A status verb must not fail on a host with no numpy, so the honest
        answer is a null with a warning rather than a refusal or a zero.
        """
        empty = tmp_path / "nothing"
        empty.mkdir()
        cap = _FakeClearinghouse(_done({OLDER: list(range(7))}))
        status = cstat.council_status(empty, clearinghouse=cap)
        assert status["parameter_set_hash"] is None
        assert _states(status)["un-run"] is None
        assert _states(status)["loaded"] is None
        assert status["by_parameter_set"]["covered_at_all"] == 7
        assert any("no current parameter_set_hash" in w for w in status["warnings"])

    def test_an_explicit_parameter_set_hash_overrides_the_plans(
        self, runs_root: Path
    ) -> None:
        _make_run(runs_root, "r", parameter_set_hash=OLDER)
        status = cstat.council_status(
            runs_root,
            parameter_set_hash=CURRENT,
            clearinghouse=_FakeClearinghouse(_done({CURRENT: [0, 1, 2]})),
        )
        assert status["parameter_set_hash"] == CURRENT
        assert _states(status)["loaded"] == 3

    def test_explicit_run_dirs_work_without_a_root(self, runs_root: Path) -> None:
        one = _make_run(runs_root, "one")
        status = cstat.council_status(
            run_dirs=[one], clearinghouse=_FakeClearinghouse()
        )
        assert [row["run_id"] for row in status["runs"]] == ["one"]

    def test_a_run_named_twice_is_counted_once(self, runs_root: Path) -> None:
        """Otherwise ``--runs-root`` plus ``--run-dir`` would double its claim."""
        one = _make_run(runs_root, "one", unrun=10)
        status = cstat.council_status(
            runs_root, run_dirs=[one], clearinghouse=_FakeClearinghouse()
        )
        assert len(status["runs"]) == 1
        assert _states(status)["planned-not-quoted"] == 10

    def test_no_target_at_all_is_refused_naming_the_check(self) -> None:
        with pytest.raises(cstat.StatusRefused) as excinfo:
            cstat.council_status()
        assert excinfo.value.check == "target"

    def test_a_runs_root_that_is_not_a_directory_is_refused(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(cstat.StatusRefused) as excinfo:
            cstat.council_status(tmp_path / "absent", clearinghouse=_FakeClearinghouse())
        assert excinfo.value.check == "runs_root"

    def test_a_halted_run_is_warned_about_with_its_reason(
        self, runs_root: Path
    ) -> None:
        _make_run(
            runs_root,
            "halted",
            artifacts=("quote", "approval", "run"),
            run_payload={
                "complete": False,
                "halt": {"reason": "ceiling", "detail": "spent the approved ceiling."},
            },
        )
        status = cstat.council_status(runs_root, clearinghouse=_FakeClearinghouse())
        warning = next(line for line in status["warnings"] if "HALTED" in line)
        assert "ceiling" in warning
        assert "the tokens it spent are spent" in warning

    def test_a_published_run_whose_rows_landed_is_told_to_RE_EXPORT_the_receipt(
        self, runs_root: Path
    ) -> None:
        """The crash-after-commit shape, which is why ``loaded`` is read from the lake.

        A status verb that trusted the receipts would report this run as
        unloaded and send an operator to reload rows that are already there.
        """
        _make_run(
            runs_root, "published", artifacts=("quote", "approval", "run", "publish")
        )
        cap = _FakeClearinghouse(_done({CURRENT: list(range(6))}))
        status = cstat.council_status(runs_root, clearinghouse=cap)
        row = status["runs"][0]
        assert row["state"] == "exported-not-loaded"
        assert row["rows_at_parameter_set"] == 6
        assert "RE-EXPORT" in row["note"]
        assert "rather than reloading" in row["note"]

    def test_a_disagreement_between_entity_and_content_uses_the_smaller_and_warns(
        self, runs_root: Path
    ) -> None:
        """The universe is their INNER JOIN, so it is at most the smaller."""
        _make_run(runs_root, "r")
        cap = _FakeClearinghouse(entity_rows=100, content_rows=90)
        status = cstat.council_status(runs_root, clearinghouse=cap)
        assert status["corpus"]["functions"] == 90
        assert status["corpus"]["entity_rows"] == 100
        assert any("INNER JOIN" in line for line in status["warnings"])

    def test_a_failed_stats_read_leaves_the_corpus_unknown_not_zero(
        self, runs_root: Path
    ) -> None:
        _make_run(runs_root, "r")
        cap = _FakeClearinghouse(stats_raises=True)
        status = cstat.council_status(runs_root, clearinghouse=cap)
        assert status["corpus"]["functions"] is None
        assert _states(status)["un-run"] is None
        assert any("corpus size is UNKNOWN" in line for line in status["warnings"])
        # The run directories' own states are unaffected.
        assert status["runs"][0]["state"] == "planned-not-quoted"

    def test_a_failed_current_state_read_leaves_loaded_unknown_not_zero(
        self, runs_root: Path
    ) -> None:
        _make_run(runs_root, "r")
        cap = _FakeClearinghouse(current_state_raises=True)
        status = cstat.council_status(runs_root, clearinghouse=cap)
        assert _states(status)["loaded"] is None
        assert status["by_parameter_set"]["covered_at_all"] is None
        assert any("UNKNOWN rather than zero" in line for line in status["warnings"])

    def test_the_record_carries_the_envelope_fields_a_consumer_pins(
        self, runs_root: Path
    ) -> None:
        _make_run(runs_root, "r")
        status = cstat.council_status(runs_root, clearinghouse=_FakeClearinghouse())
        assert status["schema_version"] == 1
        assert status["generated_at"].endswith("Z")
        assert set(status) >= {
            "query",
            "source",
            "parameter_set_hash",
            "corpus",
            "lifecycle",
            "by_parameter_set",
            "runs",
            "concurrent_runs",
            "warnings",
        }


class TestItIsReachableBesideTheOtherStages:
    def test_council_status_is_re_exported_from_ontology_mapping_utils(self) -> None:
        """Beside ``council_run`` and ``council_publish``, and for the same reason.

        The verb belongs with the other stages; the MODULE stays separate so
        it does not pull ``go_text`` (numpy / scikit-learn / joblib) and
        become unrunnable on a host with no retrieval stack.
        """
        from kbutillib.domains.genome.ontology_mapping import (
            ontology_mapping_utils as omu,
        )

        assert omu.council_status is cstat.council_status

    def test_the_module_does_not_import_the_retrieval_stack(self) -> None:
        """Status ranks nothing, so it must not reach ``go_text``."""
        import ast

        source = Path(cstat.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_level = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        names = {
            alias.name
            for node in module_level
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            str(node.module)
            for node in module_level
            if isinstance(node, ast.ImportFrom) and node.module
        }
        for forbidden in ("numpy", "sklearn", "joblib", "pyarrow", "sqlalchemy"):
            assert not any(name.startswith(forbidden) for name in names), (
                f"council_status imports {forbidden} at module level"
            )
