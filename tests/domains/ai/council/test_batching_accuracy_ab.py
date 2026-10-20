"""End-to-end test of ``scripts/council/batching_accuracy_ab.py`` (t10).

This drives the SHIPPED script -- imported by path, since ``scripts/`` is
not a package, exactly as ``test_go_replay.py`` imports
``scripts/council/replay_go_v3.py`` -- against a real ``SqliteStore`` and a
scripted ``FakeBackend``. No network call, no token, no CLI login, no GPU,
and no real backend of any kind: every answer below is this test's own
script, so the comparisons below are fully known in advance and asserted
exactly.

THE FIXTURE, BY DESIGN
=======================
Ten items, two groups:

* ``agree-0`` .. ``agree-6`` (7 items): both proposers pick the SAME menu id
  in both arms, so the vote is unanimous and the critic approves in both
  arms -- EXCEPT ``agree-1``, where the critic's BATCHED answer alone
  disapproves (``verdict="ambiguous"``), forcing the batched arm (only) to
  fall through to meta. Meta's own answer (for every item in this group,
  whichever arm asks) always names the same id the vote already agreed on,
  so the item's FINAL VERDICT is identical in both arms even though the
  CRITIC seat's own answer differed. This is point 4 of the task prompt,
  made concrete: "an intermediate chair disagreement that the council
  resolves the same way is not a regression" -- and it is why the critic's
  own per-seat disagreement count (1) and the headline verdict-difference
  count (0, from this group) are asserted separately below and do not match.
  ``agree-0`` additionally has ``proposer_1`` answer differently in the
  BATCHED arm only (picking an off-menu-relative-to-consensus id); since the
  headline freezes on the UNBATCHED proposer answers for both arms, this
  never reaches the critic, meta or the headline -- it exists purely to give
  the proposer seat report a non-trivial, independently known disagreement
  count (1) that provably does not leak into anything downstream.

* ``dis-0`` .. ``dis-2`` (3 items): the two proposers pick DIFFERENT menu
  ids, so the vote is never unanimous and meta always runs in both arms.
  Meta's own answer differs between arms for exactly these three items
  (unbatched picks one id, batched picks the other), which is rigged to
  change the FINAL VERDICT for exactly these three items -- the headline
  this module pins is therefore exactly 3/10.

Expected, mechanically derived from the above (and asserted, not merely
described):

* proposer_1: 9 identical / 1 differ (``agree-0``)
* proposer_2: 10 identical / 0 differ
* critic:     9 identical / 1 differ (``agree-1``)
* meta:       7 identical / 3 differ (``dis-0``, ``dis-1``, ``dis-2``)
* headline:   3/10 verdicts differ (``dis-0``, ``dis-1``, ``dis-2`` only --
  NOT ``agree-1``, which is the whole point of distinguishing the two
  figures)
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from .engine_support import (
    critic_answer,
    endpoint_config,
    make_query,
    make_roster,
    make_task,
    meta_answer,
    proposer_answer,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT_PATH = _REPO_ROOT / "scripts/council/batching_accuracy_ab.py"

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("sqlalchemy") is None,
    reason="the council store needs the 'council' extra (pip install -e '.[council]')",
)


def _load_script() -> Any:
    """Import the shipped script by path, the same way test_go_replay.py does.

    Importing the real file -- rather than reimplementing its logic here --
    is what makes this test and a real operator run share ONE
    implementation.

    Registered in ``sys.modules`` BEFORE ``exec_module`` runs: the script's
    ``@dataclass`` definitions resolve their own module via
    ``sys.modules[cls.__module__]`` while the class body executes, which
    raises ``AttributeError`` on a module that was never registered --
    ``test_go_replay.py``'s loader omits this only because the module it
    loads defines no dataclass.
    """
    import sys

    spec = importlib.util.spec_from_file_location("batching_accuracy_ab", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ab = _load_script()

#: Bounded timeout for every test in this module (no test may hang): every
#: call below is routed through a worker thread with a hard deadline, since
#: nothing here is actually slow (a scripted fake, zero sleeps) and a hang
#: would mean a real bug, not a slow backend.
_TIMEOUT_S = 30.0


def _bounded(fn: Any, *args: Any, **kwargs: Any) -> Any:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, *args, **kwargs)
        return future.result(timeout=_TIMEOUT_S)


AGREE_ITEMS: List[str] = [f"agree-{i}" for i in range(7)]
DISAGREE_ITEMS: List[str] = [f"dis-{i}" for i in range(3)]

#: DISAGREE_ITEMS submitted FIRST: with batch_size=3 over 10 items, the
#: groups are [dis-0,dis-1,dis-2], [agree-0,1,2], [agree-3,4,5], [agree-6]
#: (sizes 3,3,3,1). The trailing size-1 group falls through to a plain,
#: UNWRAPPED single call -- indistinguishable, on the wire, from a genuinely
#: unbatched call (production's own ``_run_group`` does the same thing: a
#: one-row group is not a batch). Putting all three rigged DISAGREE_ITEMS in
#: one real multi-item batch, rather than letting one land alone, is what
#: keeps this fixture's "batched" vs "unbatched" answers actually distinct
#: for every item this test asserts a disagreement on.
ALL_ITEMS: List[str] = DISAGREE_ITEMS + AGREE_ITEMS

#: proposer_1 answers this item differently in the batched arm ONLY. Must
#: never affect the critic, meta or the headline (see module docstring).
PROPOSER_PERTURBED_ITEM = "agree-0"

#: The critic answers this item differently in the batched arm ONLY, but
#: meta (invoked only in the batched arm, since the critic's disapproval is
#: what sends it there) still names the id the vote already agreed on, so
#: the FINAL VERDICT is unaffected.
CRITIC_PERTURBED_ITEM = "agree-1"


def _proposer_pick(seat_name: str, item_id: str) -> str:
    """Both proposers agree on ``agree-*``; they disagree on ``dis-*``."""
    if item_id in DISAGREE_ITEMS:
        return "A:1" if seat_name == "proposer_1" else "B:2"
    return "A:1"


def _answer_for(role: str, seat_name: str, item_id: str, batched: bool) -> Dict[str, Any]:
    """The one answer table this whole test is driven by.

    *batched* is whether THIS seat's call for THIS item rode in a batched
    invocation; everything else (role, seat_name, item_id) is known from the
    request itself.
    """
    if role == "proposer":
        pick = _proposer_pick(seat_name, item_id)
        if seat_name == "proposer_1" and item_id == PROPOSER_PERTURBED_ITEM and batched:
            pick = "C:3"
        return proposer_answer(pick)
    if role == "critic":
        if item_id == CRITIC_PERTURBED_ITEM and batched:
            return critic_answer(verdict="ambiguous")
        return critic_answer()
    if role == "meta":
        if item_id in DISAGREE_ITEMS:
            return meta_answer("B:2") if batched else meta_answer("A:1")
        return meta_answer("A:1")
    raise AssertionError(f"unexpected role {role!r}")


#: Matches the batched user prompt's "(item_key: <key>)" marker -- the same
#: wrapping ``render_batch_user`` in the shipped engine produces, and the
#: same thing ``test_batched_invocation.py`` matches against.
_ITEM_KEY_RE = re.compile(r"\(item_key:\s*([^)]+)\)")


def _script(req: Any) -> Any:
    """A ``FakeBackend`` script answering both request shapes.

    ``req.trace_id`` starts with ``"batch|"`` for a batched invocation (set
    by ``invoke_batched`` in the script under test, mirroring the engine's
    own convention) and is ``"<item_id>|<role>|<seat_name>"`` for a single
    call.
    """
    batched = (req.trace_id or "").startswith("batch|")
    if not batched:
        item_id = (req.trace_id or "").split("|", 1)[0]
        return _answer_for(req.role, req.seat.seat_name, item_id, False)
    item_ids = _ITEM_KEY_RE.findall(req.user or "")
    assert item_ids, "a batched invocation this test sent carried no item_key markers"
    answers = []
    for item_id in item_ids:
        entry = dict(_answer_for(req.role, req.seat.seat_name, item_id, True))
        entry["item_key"] = item_id
        answers.append(entry)
    return {"answers": answers}


def _backend_for(_seat: Any) -> Any:
    """``backend_for(seat)`` for the script under test: one fake, every seat.

    A fresh ``FakeBackend`` per call is fine -- the script is a pure
    function of the request, so there is no shared state to lose by not
    caching one instance per endpoint.
    """
    from kbutillib.domains.ai.council.backends.testing import FakeBackend

    return FakeBackend(_script)


def _make_job(store: Any) -> str:
    """Submit the ten-item job this whole module measures, via the real,
    public ``CouncilWorker.submit`` -- never ``store.create_job`` directly,
    so the job this test measures is shaped exactly like a real one.
    """
    from kbutillib.domains.ai.council.backends.testing import FakeBackend
    from kbutillib.domains.ai.council.engine.worker import CouncilWorker
    from kbutillib.domains.ai.council.queries import QueryRegistry

    backend = FakeBackend(_script)
    worker = CouncilWorker(
        store, endpoint_config(fake1={}), {"fake1": backend}, "tester", "w-1",
    )
    roster = make_roster(2)
    query = make_query()
    QueryRegistry(store).register(query, created_by="test")
    tasks = [make_task(item_id, text=item_id) for item_id in ALL_ITEMS]
    return worker.submit(query, tasks, roster, label="batching ab test fixture")


def _seat_report(report: Any, role: str, seat_name: str) -> Any:
    for seat_report in report.seat_reports:
        if seat_report.role == role and seat_report.seat_name == seat_name:
            return seat_report
    raise AssertionError(f"no seat report for ({role}, {seat_name})")


# ---------------------------------------------------------------------------
# The core measurement, called directly (no argparse, no confirmation prompt)
# ---------------------------------------------------------------------------


class TestRunAB:
    def test_per_seat_and_headline_counts(self, store: Any) -> None:
        job_id = _make_job(store)

        report = _bounded(
            ab.run_ab,
            store,
            job_id,
            sample_size=len(ALL_ITEMS),
            seed=1,
            batch_size=3,  # 10 items -> groups of 3,3,3,1: exercises both the
            # multi-item wrapped-batch path and the "group of one falls
            # through to a plain single call" path, for every seat.
            threshold=1.0,  # irrelevant here; only the counts are asserted
            backend_for=_backend_for,
        )

        assert sorted(report.item_ids) == sorted(ALL_ITEMS)

        proposer_1 = _seat_report(report, "proposer", "proposer_1")
        assert (proposer_1.n_items, proposer_1.n_identical, proposer_1.n_differ, proposer_1.n_invalid) == (10, 9, 1, 0)
        assert [d["item_id"] for d in proposer_1.disagreements] == [PROPOSER_PERTURBED_ITEM]

        proposer_2 = _seat_report(report, "proposer", "proposer_2")
        assert (proposer_2.n_items, proposer_2.n_identical, proposer_2.n_differ, proposer_2.n_invalid) == (10, 10, 0, 0)

        critic = _seat_report(report, "critic", "critic")
        assert (critic.n_items, critic.n_identical, critic.n_differ, critic.n_invalid) == (10, 9, 1, 0)
        assert [d["item_id"] for d in critic.disagreements] == [CRITIC_PERTURBED_ITEM]

        meta = _seat_report(report, "meta", "meta")
        assert (meta.n_items, meta.n_identical, meta.n_differ, meta.n_invalid) == (10, 7, 3, 0)
        assert sorted(d["item_id"] for d in meta.disagreements) == sorted(DISAGREE_ITEMS)

        # The headline: exactly the three non-unanimous items, and NOT the
        # critic-perturbed item, which resolves to the same selection either
        # way (point 4 of the task prompt).
        assert report.n_verdict_differs == 3
        assert sorted(d["item_id"] for d in report.verdict_differing_items) == sorted(DISAGREE_ITEMS)
        assert report.verdict_difference_fraction == pytest.approx(0.3)
        assert CRITIC_PERTURBED_ITEM not in {d["item_id"] for d in report.verdict_differing_items}

        # Token cost is reported per arm, per seat, and never fabricated:
        # FakeBackend reports zero usage, which must show up as zero, not as
        # a missing key.
        for seat_report in report.seat_reports:
            jsonable = seat_report.to_jsonable()
            assert jsonable["usage"]["unbatched"].get("prompt_tokens", 0) == 0
            assert jsonable["n_invocations"]["unbatched"] == 10
            # batch_size=3 over 10 items -> 4 invocations (3,3,3,1).
            assert jsonable["n_invocations"]["batched"] == 4

    def test_sampling_is_reproducible_for_the_same_seed(self, store: Any) -> None:
        job_id = _make_job(store)
        first = _bounded(
            ab.run_ab, store, job_id, sample_size=6, seed=42, batch_size=3,
            threshold=1.0, backend_for=_backend_for,
        )
        second = _bounded(
            ab.run_ab, store, job_id, sample_size=6, seed=42, batch_size=3,
            threshold=1.0, backend_for=_backend_for,
        )
        assert first.item_ids == second.item_ids

    def test_proposer_batching_never_reaches_the_headline(self, store: Any) -> None:
        """``agree-0``'s proposer_1 perturbation must never change its own
        item's verdict, since the headline freezes on the UNBATCHED
        proposer answers for both arms."""
        job_id = _make_job(store)
        report = _bounded(
            ab.run_ab, store, job_id, sample_size=len(ALL_ITEMS), seed=1, batch_size=3,
            threshold=1.0, backend_for=_backend_for,
        )
        differing_ids = {d["item_id"] for d in report.verdict_differing_items}
        assert PROPOSER_PERTURBED_ITEM not in differing_ids


# ---------------------------------------------------------------------------
# The gate: exit code on each side of --threshold, through the real CLI path
# ---------------------------------------------------------------------------


class TestGateExitCode:
    def _argv(self, store_url: str, job_id: str, threshold: float) -> List[str]:
        return [
            "--store", store_url,
            "--job", job_id,
            "--sample-size", str(len(ALL_ITEMS)),
            "--seed", "1",
            "--batch-size", "3",
            "--threshold", str(threshold),
            "--yes",
        ]

    def test_exit_zero_when_within_threshold(self, store_url: Any, make_store: Any) -> None:
        url = store_url("gate_pass")
        store = make_store("gate_pass")
        job_id = _make_job(store)

        # 3/10 = 0.3 differ; 0.35 is comfortably above that.
        code = _bounded(ab.main, self._argv(url, job_id, 0.35), backend_for_override=_backend_for)
        assert code == 0

    def test_exit_one_when_exceeding_threshold(self, store_url: Any, make_store: Any) -> None:
        url = store_url("gate_fail")
        store = make_store("gate_fail")
        job_id = _make_job(store)

        # 3/10 = 0.3 differ; 0.25 is below that -- the gate must fail.
        code = _bounded(ab.main, self._argv(url, job_id, 0.25), backend_for_override=_backend_for)
        assert code == 1

    def test_report_written_to_out_matches_the_in_process_run(
        self, store_url: Any, make_store: Any, tmp_path: Any
    ) -> None:
        import json

        url = store_url("gate_out")
        store = make_store("gate_out")
        job_id = _make_job(store)
        out_path = tmp_path / "report.json"

        argv = self._argv(url, job_id, 0.35) + ["--out", str(out_path)]
        code = _bounded(ab.main, argv, backend_for_override=_backend_for)
        assert code == 0
        assert out_path.exists()

        payload = json.loads(out_path.read_text())
        assert payload["headline"]["n_verdict_differs"] == 3
        assert payload["headline"]["fraction"] == pytest.approx(0.3)
        assert payload["headline"]["gate_passed"] is True
        assert "NOT accuracy against truth" in payload["framing"]["measures"]
        assert payload["authorisation"].startswith("this report does not enable batching")

    def test_main_refuses_to_write_report_over_the_store_file(
        self, store_url: Any, make_store: Any
    ) -> None:
        url = store_url("gate_selfwrite")
        store = make_store("gate_selfwrite")
        job_id = _make_job(store)

        argv = self._argv(url, job_id, 0.35) + ["--out", str(store.path)]
        code = _bounded(ab.main, argv, backend_for_override=_backend_for)
        assert code == 2


# ---------------------------------------------------------------------------
# No real token is ever reachable from this module
#
# Every test above calls ``ab.run_ab`` or ``ab.main`` with an EXPLICIT
# ``backend_for`` / ``backend_for_override`` pointing at ``_backend_for``,
# which only ever constructs a ``FakeBackend``. None of them passes
# ``--config`` or omits the override, so ``ab.live_backend_for`` -- the only
# function in the script under test that can reach a real network call -- is
# never invoked anywhere in this module.
# ---------------------------------------------------------------------------
