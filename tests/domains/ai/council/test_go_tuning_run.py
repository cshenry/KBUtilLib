"""``go_tuning.run`` -- one isolated store per run, and PROOF it was fresh (C14).

Freshness is a property of the ENGINE, not of arithmetic: the reuse path is
``CouncilWorker``'s, it is silent (the job settles ``ok`` either way), and the
only durable evidence it happened is ``calls.reused_from``.  So these tests run
a REAL council -- the shipped ``go_mf@4`` query type, the real worker, the real
store -- with every seat served by a ``FakeBackend``, and the central test
produces a GENUINE cache reuse rather than stubbing one: two items carrying the
same annotation and the same menu render the same prompt, the second call finds
the first's answer, and :func:`fresh_run` must reject the run.

A stubbed ``reused_from`` would have passed against an engine that had stopped
setting it, which is exactly the failure the check exists to catch.

Nothing here makes a network call, spends a token, needs a CLI login or needs
a GPU.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import pytest

pytest.importorskip("sqlalchemy")

from kbutillib.domains.ai.council.engine import CouncilWorker  # noqa: E402
from kbutillib.domains.ai.council.go_tuning import run as R  # noqa: E402
from kbutillib.domains.ai.council.go_tuning.conflicts import Refused  # noqa: E402
from kbutillib.domains.ai.council.go_tuning.ledger import Ledger  # noqa: E402
from kbutillib.domains.ai.council.queries import QueryRegistry  # noqa: E402
from kbutillib.domains.ai.council.store.sqlite import SqliteStore  # noqa: E402
from kbutillib.domains.ai.council.types import (  # noqa: E402
    CouncilTask,
    MenuEntry,
    Roster,
)

from .engine_support import InstrumentedBackend, endpoint_config, fake_seat, run  # noqa: E402
from .go_tuning_score_support import MENU  # noqa: E402

WORKER_DEFAULTS: Dict[str, Any] = {
    "heartbeat_interval_s": 0.0,
    "poll_interval_s": 0.01,
    "retry_backoff_s": (0.0, 0.0),
    "retry_jitter": 0.0,
    "idle_grace_s": 0.2,
    "max_idle_s": 20.0,
}

QUERY = ("go_mf", "4")


# ---------------------------------------------------------------------------
# a CouncilUtils-shaped facade over a store fresh_run has already created
# ---------------------------------------------------------------------------


def roster() -> Roster:
    """Two proposers plus critic and meta, all on one ``fake`` endpoint.

    Two proposers that answer identically are UNANIMOUS, so meta is skipped
    and the run costs four calls per item instead of five -- which keeps the
    reuse arithmetic in these tests small enough to state exactly.
    """
    return Roster(
        proposers=[fake_seat("proposer_1", "p1"), fake_seat("proposer_2", "p2")],
        critic=fake_seat("critic", "critic-model"),
        meta=fake_seat("meta", "meta-model"),
    )


def go_mf_script(picked: Sequence[str] = ("GO:0005524",)) -> Any:
    """Replies in ``go_mf@4``'s own vocabulary, for every role."""

    def _script(req: Any) -> Dict[str, Any]:
        if req.role == "proposer":
            return {
                "picked": [
                    {"go_id": go_id, "confidence": "high", "why": f"because {go_id}"}
                    for go_id in picked
                ],
                "overall_confidence": "high",
                "no_functional_content": False,
                "notes": "none",
            }
        if req.role == "critic":
            return {
                "verdict": "consistent",
                "reason": "the panel cited the menu",
                "overpicked": [],
                "missed": [],
                "contentless_input": False,
            }
        return {
            "final_go_ids": list(picked),
            "final_confidence": "medium",
            "no_functional_content": False,
            "reasoning": "adjudicated",
        }

    return _script


class Council:
    """Enough of ``CouncilUtils`` for :func:`run.fresh_run`, over an OPEN store.

    Built by a factory and not handed in ready-made, because the store does
    not exist until ``fresh_run`` creates it -- opening it early would create
    the file ``isolated_store`` has to find absent.
    """

    def __init__(self, store_url: str, *, picked: Sequence[str] = ("GO:0005524",)) -> None:
        self.url = store_url
        self._store = SqliteStore(store_url)
        self.backend = InstrumentedBackend(go_mf_script(picked), kind="fake")
        self.submits: List[Dict[str, Any]] = []

    def store_url(self) -> str:
        return self.url

    def submit(
        self,
        query_ref: Any,
        items: Sequence[Any],
        label: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> str:
        self.submits.append({"query": str(query_ref), "label": label, "profile": profile})
        spec = QueryRegistry(self._store).get(*str(query_ref).split("@"))
        return self._worker().submit(
            spec, list(items), roster(), label=label, profile_name=profile
        )

    def run(self, job_id: str) -> Dict[str, Any]:
        run(self._worker().work(job_id=job_id, until_idle=True))
        return dict(self._store.job_status(job_id))

    def close(self) -> None:
        self._store.close()

    def _worker(self) -> CouncilWorker:
        return CouncilWorker(
            self._store,
            endpoint_config(fake1={"kind": "fake", "max_concurrency": 1}),
            {"fake1": self.backend},
            "alice",
            "w-1",
            **WORKER_DEFAULTS,
        )


@pytest.fixture()
def factory() -> Any:
    """A ``council_factory`` and the facades it built, for inspection."""
    built: List[Council] = []

    def _factory(store_url: str) -> Council:
        council = Council(store_url)
        built.append(council)
        return council

    _factory.built = built  # type: ignore[attr-defined]
    return _factory


@pytest.fixture()
def loop_store(tmp_path: Any) -> str:
    """The LOOP store: holds the specs, never a measured run.

    ``fresh_run`` copies the spec out of here into the run's own store, which
    is how a candidate's prompt (registered by whoever proposed it) reaches a
    measurement without a packaged YAML standing in for it.
    """
    url = f"sqlite:///{tmp_path / 'loop.sqlite'}"
    store = SqliteStore(url)
    try:
        store.init_schema()
        QueryRegistry(store).seed_builtins()
    finally:
        store.close()
    return url


def task(item_id: str, *, text: Optional[str] = None) -> CouncilTask:
    """One ``go_mf`` item.  ``text=None`` gives it its own annotation string."""
    return CouncilTask(
        item_id=item_id,
        evidence={
            "text": text if text is not None else f"ABC transporter subunit {item_id}",
            "source": "prokka",
        },
        menu=[MenuEntry.model_validate(dict(e)) for e in MENU],
    )


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


class TestFreshRun:
    def test_a_run_lands_in_its_own_store_and_is_proven_fresh(
        self, tmp_path: Any, loop_store: str, factory: Any
    ) -> None:
        run_dir = tmp_path / "run"
        ledger = Ledger(run_dir)
        report = R.fresh_run(
            loop_store,
            "go_mf",
            "4",
            [task("a"), task("b"), task("c")],
            "argo",
            run_dir,
            ledger=ledger,
            label="iter0-baseline",
            council_factory=factory,
        )
        assert report["fresh"] is True
        assert report["reused"] == 0
        assert report["reused_call_ids"] == []
        assert report["query"] == "go_mf@4"
        assert report["n_items"] == 3
        expected = R.store_path_for(run_dir, "iter0-baseline")
        assert report["store"] == f"sqlite:///{expected}"
        assert expected.exists()
        assert expected.parent.name == R.STORES_DIR

        # The run's store holds that job and nothing else.
        store = SqliteStore(report["store"])
        try:
            assert [j["job_id"] for j in store.list_jobs()] == [report["job_id"]]
            assert R.reused_calls(store, report["job_id"]) == []
        finally:
            store.close()

    def test_the_identities_are_recorded_before_the_spend(
        self, tmp_path: Any, loop_store: str, factory: Any
    ) -> None:
        run_dir = tmp_path / "run"
        ledger = Ledger(run_dir)
        report = R.fresh_run(
            loop_store,
            "go_mf",
            "4",
            [task("a")],
            None,
            run_dir,
            ledger=ledger,
            label="iter0-a",
            council_factory=factory,
        )
        records = ledger.records("run")
        assert [r["phase"] for r in records] == ["submitted", "complete"]
        for record in records:
            assert record["store"] == report["store"]
            assert record["job_id"] == report["job_id"]
            assert record["run_label"] == "iter0-a"
        assert records[1]["reused"] == 0

    def test_the_spec_is_copied_into_the_run_s_store(
        self, tmp_path: Any, loop_store: str, factory: Any
    ) -> None:
        run_dir = tmp_path / "run"
        report = R.fresh_run(
            loop_store,
            "go_mf",
            "4",
            [task("a")],
            None,
            run_dir,
            ledger=Ledger(run_dir),
            label="iter0-a",
            council_factory=factory,
        )
        store = SqliteStore(report["store"])
        try:
            saved = [(r["name"], str(r["version"])) for r in QueryRegistry(store).list()]
            assert saved == [QUERY]
        finally:
            store.close()

    def test_no_work_submits_without_claiming_freshness(
        self, tmp_path: Any, loop_store: str, factory: Any
    ) -> None:
        """Nothing has been measured, so the report must not say ``fresh``."""
        run_dir = tmp_path / "run"
        ledger = Ledger(run_dir)
        report = R.fresh_run(
            loop_store,
            "go_mf",
            "4",
            [task("a")],
            None,
            run_dir,
            ledger=ledger,
            label="iter0-a",
            council_factory=factory,
            work=False,
        )
        assert report["fresh"] is None
        assert report["reused"] is None
        assert [r["phase"] for r in ledger.records("run")] == ["submitted"]

    def test_a_label_is_generated_when_none_is_given(
        self, tmp_path: Any, loop_store: str, factory: Any
    ) -> None:
        run_dir = tmp_path / "run"
        report = R.fresh_run(
            loop_store,
            "go_mf",
            "4",
            [task("a")],
            None,
            run_dir,
            ledger=Ledger(run_dir),
            council_factory=factory,
        )
        assert report["label"].startswith("go_mf-4-")


# ---------------------------------------------------------------------------
# the reuse rejection: the point of the whole module
# ---------------------------------------------------------------------------


class TestAReusedCallIsNotAFreshRun:
    def test_a_real_cache_reuse_is_rejected(
        self, tmp_path: Any, loop_store: str, factory: Any
    ) -> None:
        """TWO ITEMS, ONE QUESTION -- so the engine genuinely reuses.

        ``prompt_hash`` is a content address of the rendered invocation, so
        two items whose annotation string and menu are identical produce the
        same hash; the second item's calls find the first's answers and the
        engine settles them with ``reused_from`` set.  Nothing is stubbed:
        this is the shipped reuse path.
        """
        run_dir = tmp_path / "run"
        ledger = Ledger(run_dir)
        with pytest.raises(Refused) as exc:
            R.fresh_run(
                loop_store,
                "go_mf",
                "4",
                [
                    task("twin-a", text="identical annotation string"),
                    task("twin-b", text="identical annotation string"),
                ],
                None,
                run_dir,
                ledger=ledger,
                label="iter0-twins",
                council_factory=factory,
            )
        assert "answered out of the" in str(exc.value)
        assert "not an independent run" in str(exc.value)
        assert exc.value.ids, "the offending call ids must be named"

        # The rejection is recorded, with the evidence it was made from.
        records = ledger.records("run")
        assert [r["phase"] for r in records] == ["submitted", "not_fresh"]
        assert records[1]["reused"] == len(exc.value.ids)
        assert records[1]["reused_call_ids"] == sorted(exc.value.ids)

        # And the evidence really is ``calls.reused_from`` in the store.
        store = SqliteStore(records[1]["store"])
        try:
            offenders = R.reused_calls(store, records[1]["job_id"])
            assert offenders == sorted(exc.value.ids)
            rows = {c["call_id"]: c for c in store.get_calls(records[1]["job_id"])}
            for call_id in offenders:
                source = rows[call_id]["reused_from"]
                assert source and source != call_id
                assert rows[source]["status"] == "ok"
        finally:
            store.close()

    def test_distinct_items_do_not_reuse_each_other(
        self, tmp_path: Any, loop_store: str, factory: Any
    ) -> None:
        """The control for the test above: different questions, no reuse.

        Without this, "rejected" could as easily mean the check fires on every
        run, which would be a different bug with the same test outcome.
        """
        run_dir = tmp_path / "run"
        report = R.fresh_run(
            loop_store,
            "go_mf",
            "4",
            [task("a"), task("b")],
            None,
            run_dir,
            ledger=Ledger(run_dir),
            label="iter0-distinct",
            council_factory=factory,
        )
        assert report["fresh"] is True

    def test_a_second_job_in_the_store_is_rejected(
        self, tmp_path: Any, loop_store: str, factory: Any
    ) -> None:
        """A store holding another job is a call cache, reused or not yet.

        Exercised by letting a facade submit a second job behind
        ``fresh_run``'s back, which is what an operator reusing a store file
        would produce.
        """
        run_dir = tmp_path / "run"
        ledger = Ledger(run_dir)

        def _factory(store_url: str) -> Council:
            council = Council(store_url)
            original = council.submit

            def _submit(*args: Any, **kwargs: Any) -> str:
                first = original(*args, **kwargs)
                original(args[0], [task("intruder")], label="intruder")
                return first

            council.submit = _submit  # type: ignore[method-assign]
            return council

        with pytest.raises(Refused) as exc:
            R.fresh_run(
                loop_store,
                "go_mf",
                "4",
                [task("a")],
                None,
                run_dir,
                ledger=ledger,
                label="iter0-shared",
                council_factory=_factory,
            )
        assert "besides" in str(exc.value)
        assert ledger.records("run")[-1]["phase"] == "not_fresh"


# ---------------------------------------------------------------------------
# where the store may go
# ---------------------------------------------------------------------------


class TestStoreLocation:
    def test_an_existing_store_file_is_refused(self, tmp_path: Any, loop_store: str) -> None:
        path = R.store_path_for(tmp_path / "run", "iter0-a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not really a database", encoding="utf-8")
        with pytest.raises(Refused) as exc:
            R.isolated_store(path)
        assert "existing store" in str(exc.value)
        assert "different label" in str(exc.value)

    def test_a_second_run_under_the_same_label_is_refused(
        self, tmp_path: Any, loop_store: str, factory: Any
    ) -> None:
        run_dir = tmp_path / "run"
        R.fresh_run(
            loop_store,
            "go_mf",
            "4",
            [task("a")],
            None,
            run_dir,
            ledger=Ledger(run_dir),
            label="iter0-a",
            council_factory=factory,
        )
        with pytest.raises(Refused) as exc:
            R.fresh_run(
                loop_store,
                "go_mf",
                "4",
                [task("a")],
                None,
                run_dir,
                ledger=Ledger(run_dir),
                label="iter0-a",
                council_factory=factory,
            )
        assert "existing store" in str(exc.value)

    def test_a_refused_path_fails_loudly_and_is_never_relocated(
        self, tmp_path: Any, loop_store: str, factory: Any
    ) -> None:
        """The store refuses a synced directory; ``fresh_run`` says so.

        It must NOT quietly put the store on local disk instead: an operator
        who cannot see where their measurement went has no way to find the
        one thing they have to fix.
        """
        run_dir = tmp_path / "Dropbox" / "run"
        with pytest.raises(Refused) as exc:
            R.fresh_run(
                loop_store,
                "go_mf",
                "4",
                [task("a")],
                None,
                run_dir,
                ledger=Ledger(run_dir),
                label="iter0-a",
                council_factory=factory,
            )
        message = str(exc.value)
        assert "will not open under" in message
        assert str(run_dir / R.STORES_DIR) in message
        assert "/scratch" in message
        assert not R.store_path_for(run_dir, "iter0-a").exists()

    def test_a_label_that_is_not_a_filename_component_is_refused(self, tmp_path: Any) -> None:
        with pytest.raises(Refused):
            R.store_path_for(tmp_path, "sub/dir")
        with pytest.raises(Refused):
            R.store_path_for(tmp_path, "   ")


class TestTheSpecMustExist:
    def test_a_spec_missing_from_the_loop_store_is_refused(
        self, tmp_path: Any, loop_store: str, factory: Any
    ) -> None:
        run_dir = tmp_path / "run"
        with pytest.raises(Refused) as exc:
            R.fresh_run(
                loop_store,
                "go_mf",
                "99",
                [task("a")],
                None,
                run_dir,
                ledger=Ledger(run_dir),
                label="iter0-a",
                council_factory=factory,
            )
        assert "go_mf@99" in str(exc.value)
        # Nothing was created: the spec is checked BEFORE the store.
        assert not R.store_path_for(run_dir, "iter0-a").exists()


# ---------------------------------------------------------------------------
# building the items
# ---------------------------------------------------------------------------


class TestGoMfTasks:
    def _material(self) -> Any:
        from . import go_tuning_score_support as S

        entries = [
            S.entry("x1", "heldout"),
            S.entry("x2", "pilot"),
            S.entry("x2", "pilot", swap=True),
            S.entry("x3", "regression"),
        ]
        matched = [
            {
                "item_id": item_id,
                "evidence_text": f"annotation {item_id}",
                "source": "prokka",
                "menu": [dict(e) for e in MENU],
            }
            for item_id in ("x1", "x2", "x3")
        ]
        return S.manifest_of(entries), matched

    def test_every_pool_is_built_and_swapped_twins_are_one_item(self) -> None:
        manifest, matched = self._material()
        tasks = R.go_mf_tasks(manifest, matched)
        assert [t.item_id for t in tasks] == ["x1", "x2", "x3"]
        assert tasks[0].evidence == {"text": "annotation x1", "source": "prokka"}
        assert [e.id for e in tasks[0].menu] == [e["id"] for e in MENU]

    def test_pools_can_be_narrowed(self) -> None:
        manifest, matched = self._material()
        tasks = R.go_mf_tasks(manifest, matched, ["heldout", "regression"])
        assert [t.item_id for t in tasks] == ["x1", "x3"]

    def test_an_item_the_extract_does_not_hold_is_refused(self) -> None:
        manifest, matched = self._material()
        with pytest.raises(Refused) as exc:
            R.go_mf_tasks(manifest, [m for m in matched if m["item_id"] != "x2"])
        assert exc.value.ids == ["x2"]


def test_an_unserialisable_job_status_does_not_lose_the_run_record(
    tmp_path: Any, loop_store: str
) -> None:
    """The spend is already made when the second record is written.

    ``CouncilUtils.run`` returns the job status with the WORKER'S OWN SUMMARY
    attached, which this module does not control.  A ``TypeError`` from one
    unserialisable field there would lose the record that says whether the
    run was fresh -- the one thing a later reader cannot reconstruct.
    """
    from datetime import datetime, timezone

    class AwkwardCouncil(Council):
        def run(self, job_id: str) -> Dict[str, Any]:
            status = super().run(job_id)
            status["worker"] = {"started_at": datetime.now(tz=timezone.utc)}
            return status

    run_dir = tmp_path / "run"
    ledger = Ledger(run_dir)
    report = R.fresh_run(
        loop_store,
        "go_mf",
        "4",
        [task("a")],
        None,
        run_dir,
        ledger=ledger,
        label="iter0-a",
        council_factory=AwkwardCouncil,
    )
    assert report["fresh"] is True
    records = ledger.records("run")
    assert [r["phase"] for r in records] == ["submitted", "complete"]
    assert isinstance(records[1]["job_status"]["worker"]["started_at"], str)
