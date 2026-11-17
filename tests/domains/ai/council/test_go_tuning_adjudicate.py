"""``go_tuning.adjudicate`` -- blinded items, metered tranches, read-back.

Everything here runs against a throwaway ``SqliteStore`` under ``tmp_path``
with every seat served by a ``FakeBackend``, so a full build -> submit ->
work -> read round trip happens without a network call, a token, a CLI login
or a GPU.  The query type is the SHIPPED ``go_mf_adjudicate@1`` builtin, not
an in-test one: this module's whole subject is that query type's own
vocabulary, and a convenient test spec would pass while the product failed.

THE SEATS' ENDPOINT KINDS ARE THE POINT OF HALF THESE TESTS.  The roster
seats proposers and the critic/meta on an ``argo`` endpoint and one proposer
on a ``claude_cli`` one, with a real (non-null) usage figure on BOTH, because
C10 meters ARGO-billed seats only and the executor's note is explicit that
"ARGO-billed" is decided by endpoint kind and never by whether a usage figure
is null -- a successful CLI call reports real tokens, and only a failed one
reports ``null``.  A rule that read null as "CLI" would have metered every
successful CLI call against the ARGO ceiling.

``Council`` below is a ``CouncilUtils``-shaped facade over the real
``CouncilWorker``: the same ``submit``/``work`` library calls the facade
makes, without the endpoint configuration ``CouncilConfig`` cannot express
(its ``kind`` admits no ``fake``, which is why ``test_council_utils`` seats an
unreachable ``openai`` port and patches ``build_backends``).  Here the seats
need REAL kind strings -- ``argo`` and ``claude_cli`` -- so the backends are
injected into the worker, where an injected api type wins over the
endpoint's configured kind.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

pytest.importorskip("sqlalchemy")

from kbutillib.domains.ai.council.engine import CouncilWorker  # noqa: E402
from kbutillib.domains.ai.council.go_tuning import adjudicate as A  # noqa: E402
from kbutillib.domains.ai.council.go_tuning.conflicts import (  # noqa: E402
    MatchedItem,
    Refused,
)
from kbutillib.domains.ai.council.go_tuning.ledger import Ledger  # noqa: E402
from kbutillib.domains.ai.council.go_tuning.sample import (  # noqa: E402
    Plan,
    build_manifest,
)
from kbutillib.domains.ai.council.queries import QueryRegistry  # noqa: E402
from kbutillib.domains.ai.council.store.sqlite import SqliteStore  # noqa: E402
from kbutillib.domains.ai.council.types import Roster, SeatSpec  # noqa: E402

from .engine_support import InstrumentedBackend, run  # noqa: E402
from .go_tuning_support import hash_of  # noqa: E402

WORKER_DEFAULTS: Dict[str, Any] = {
    "heartbeat_interval_s": 0.0,
    "poll_interval_s": 0.01,
    "retry_backoff_s": (0.0, 0.0),
    "retry_jitter": 0.0,
    "idle_grace_s": 0.2,
    "max_idle_s": 20.0,
}

#: Three terms at three levels of specificity, so the rival answers differ in
#: a way policy rules P1-P3 can actually decide.
MENU = [
    {
        "id": "GO:0015749",
        "label": "glucose transmembrane transport",
        "description": "The directed movement of glucose across a membrane.",
        "rank": 1,
        "score": 0.88,
        "metadata": {},
    },
    {
        "id": "GO:0005524",
        "label": "ATP binding",
        "description": "Binding to ATP.",
        "rank": 2,
        "score": 0.71,
        "metadata": {},
    },
    {
        "id": "GO:0016887",
        "label": "ATP hydrolysis activity",
        "description": "Catalysis of ATP + H2O = ADP + phosphate.",
        "rank": 3,
        "score": 0.52,
        "metadata": {},
    },
]

#: Tokens each endpoint's fake reports per successful call.  Both are REAL
#: numbers: the CLI figure is deliberately the larger of the two, so a test
#: that metered it would overshoot a ceiling the ARGO figure alone clears.
ARGO_USAGE = {"prompt_tokens": 800, "completion_tokens": 200}
CLI_USAGE = {"prompt_tokens": 17_000, "completion_tokens": 3_000}


# ---------------------------------------------------------------------------
# the corpus: matched conflicts and a manifest over them
# ---------------------------------------------------------------------------


def matched_item(label: str, candidate: Sequence[str], baseline: Sequence[str]) -> MatchedItem:
    return MatchedItem(
        entity_hash=hash_of("entity", label),
        data_hash=hash_of("data", label),
        item_id=f"c-{label}",
        candidate_job_id="j-cand",
        baseline_item_id=f"b-{label}",
        baseline_job_id="j-base",
        candidate_selected=frozenset(candidate),
        baseline_selected=frozenset(baseline),
        candidate_answers=1,
        baseline_answers=1,
        source="prokka",
        evidence_text=f"Glucose ABC transporter, ATP-binding protein {label}",
        menu=tuple(MENU),
        kind="disjoint" if not set(candidate) & set(baseline) else "partial",
        item_sub_kind=None,
    )


def corpus(n: int = 6) -> List[MatchedItem]:
    """*n* conflicts, alternating disjoint and partial."""
    items: List[MatchedItem] = []
    for i in range(n):
        if i % 2:
            items.append(
                matched_item(f"pa{i:02d}", ["GO:0005524", "GO:0016887"], ["GO:0005524"])
            )
        else:
            items.append(matched_item(f"dj{i:02d}", ["GO:0005524"], ["GO:0015749"]))
    return items


#: A plan small enough to run in a test, with the floors lowered.  Guard
#: slices are off: they draw from classes this corpus has none of, and an
#: empty pool is not what any test here is about.
SMALL_PLAN = Plan(
    pilot_size=6,
    swap_slice=2,
    regression_size=0,
    decline_guard_size=0,
    min_conflicts=2,
)


@pytest.fixture()
def material() -> Any:
    items = corpus()
    manifest = build_manifest(items, "seed-1", SMALL_PLAN)
    return manifest, items


# ---------------------------------------------------------------------------
# the council: a CouncilUtils-shaped facade over the real worker
# ---------------------------------------------------------------------------


def seat(name: str, model: str, endpoint: str, kind: str) -> SeatSpec:
    return SeatSpec(
        seat_name=name,
        endpoint=endpoint,
        endpoint_kind=kind,
        model=model,
        canonical_model=f"test/{model}",
        max_output_tokens=512,
        temperature=0,
        timeout_s=5,
    )


def mixed_roster() -> Roster:
    """Two ARGO proposers, one CLI proposer, ARGO critic and meta."""
    return Roster(
        proposers=[
            seat("argo_a", "p1", "argo1", "argo"),
            seat("argo_b", "p2", "argo1", "argo"),
            seat("cli_c", "p3", "cli1", "claude_cli"),
        ],
        critic=seat("critic", "cr", "argo1", "argo"),
        meta=seat("meta", "mt", "argo1", "argo"),
    )


class Council:
    """Enough of ``CouncilUtils`` for :func:`adjudicate.run_tranches`."""

    def __init__(
        self,
        path: Any,
        roster: Optional[Roster] = None,
        *,
        argo: Optional[InstrumentedBackend] = None,
        cli: Optional[InstrumentedBackend] = None,
        script: Any = None,
    ) -> None:
        self.url = f"sqlite:///{path}"
        self._store = SqliteStore(self.url)
        self._store.init_schema()
        QueryRegistry(self._store).seed_builtins()
        self._query = QueryRegistry(self._store).get(A.QUERY_NAME, A.QUERY_VERSION)
        self._roster = roster or mixed_roster()
        self.argo = argo or InstrumentedBackend(
            script or unanimous_script, kind="argo", usage=dict(ARGO_USAGE)
        )
        self.cli = cli or InstrumentedBackend(
            script or unanimous_script, kind="claude_cli", usage=dict(CLI_USAGE)
        )
        self.backends = {"argo1": self.argo, "cli1": self.cli}
        self.submits: List[Dict[str, Any]] = []

    # -- the facade's surface ---------------------------------------------

    def require_schema(self) -> Any:
        return self._store

    def store_url(self) -> str:
        return self.url

    def query(self, ref: str) -> Any:
        return self._query

    def roster(self, profile: Optional[str] = None, **_overrides: Any) -> Roster:
        return self._roster

    def submit(
        self,
        query_ref: Any,
        items: Sequence[Any],
        label: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> str:
        self.submits.append({"label": label, "items": [t.item_id for t in items]})
        return self._worker().submit(
            self._query, list(items), self._roster, label=label, profile_name=profile
        )

    def run(self, job_id: str) -> Dict[str, Any]:
        worker = self._worker()
        run(worker.work(job_id=job_id, until_idle=True))
        return dict(self._store.job_status(job_id))

    def job_status(self, job_id: str) -> Dict[str, Any]:
        return dict(self._store.job_status(job_id))

    def close(self) -> None:
        self._store.close()

    # -- internals --------------------------------------------------------

    def _worker(self) -> CouncilWorker:
        return CouncilWorker(
            self._store,
            {
                "endpoints": {
                    "argo1": {"kind": "argo", "max_concurrency": 4},
                    "cli1": {"kind": "claude_cli", "max_concurrency": 1},
                }
            },
            self.backends,
            "alice",
            "w-1",
            **WORKER_DEFAULTS,
        )


# ---------------------------------------------------------------------------
# scripted answers, in go_mf_adjudicate@1's own vocabulary
# ---------------------------------------------------------------------------


def proposer_reply(
    *go_ids: str,
    prefers: str = "B",
    failure_type: str = "too_generic",
    rules: Sequence[str] = ("P1", "P3"),
    confidence: str = "high",
    off_menu: bool = False,
) -> Dict[str, Any]:
    return {
        "correct_go_ids": list(go_ids),
        "prefers": prefers,
        "correct_term_off_menu": off_menu,
        "off_menu_description": "none",
        "failure_type": failure_type,
        "rules_cited": list(rules),
        "confidence": confidence,
        "why": "P1 maps the named system, not its component's chemistry.",
        "notes": "none",
    }


def critic_reply(verdict: str = "consistent") -> Dict[str, Any]:
    return {
        "verdict": verdict,
        "reason": "The panel cited the rule it decided on.",
        "overpicked": [],
        "missed": [],
        "contentless_input": False,
        "misapplied_rules": [],
        "off_menu_missed": False,
        "notes": "none",
    }


def meta_reply(
    *go_ids: str,
    prefers: str = "B",
    failure_type: str = "wrong_substrate",
    rules: Sequence[str] = ("P2",),
    confidence: str = "medium",
) -> Dict[str, Any]:
    return {
        "final_go_ids": list(go_ids),
        "prefers": prefers,
        "correct_term_off_menu": False,
        "off_menu_description": "none",
        "failure_type": failure_type,
        "rules_cited": list(rules),
        "final_confidence": confidence,
        "no_functional_content": False,
        "reasoning": "The panel split on the extra term; P6 settles it.",
        "notes": "none",
    }


def unanimous_script(req: Any) -> Dict[str, Any]:
    """Every proposer picks the menu's first term; the critic approves.

    Unanimity means the engine SKIPS meta (measured on h100's live run: 5
    items gave 21 calls -- 15 proposer, 5 critic, 1 meta), so the scripted
    meta answer below is reached only by the tests that force a split.
    """
    if req.role == "proposer":
        return proposer_reply("GO:0015749")
    if req.role == "critic":
        return critic_reply()
    return meta_reply("GO:0015749")


def split_script(req: Any) -> Dict[str, Any]:
    """The proposers disagree, so meta runs and decides."""
    if req.role == "proposer":
        if req.seat.seat_name == "argo_a":
            return proposer_reply("GO:0015749", prefers="B")
        if req.seat.seat_name == "argo_b":
            return proposer_reply("GO:0005524", prefers="A")
        return proposer_reply("GO:0016887", prefers="neither")
    if req.role == "critic":
        return critic_reply(verdict="ambiguous")
    return meta_reply("GO:0015749", prefers="B")


# ---------------------------------------------------------------------------
# helpers shared by the run tests
# ---------------------------------------------------------------------------


def all_marked(manifest: Any, band: str = "trusted") -> Dict[str, str]:
    """A mark for every entry, i.e. an operator who marked the whole pool."""
    return {str(entry["id"]): "agree" for entry in manifest.entries}


def seed_calibration(
    ledger: Ledger, council: Council, manifest: Any, band: str = "trusted"
) -> Dict[str, Any]:
    """The calibration record the spot-check gate looks for."""
    return ledger.append(
        "calibration",
        manifest_id=manifest.manifest_id,
        adjudicate_spec_hash=A.adjudicate_spec_hash(council.query(A.QUERY_REF)),
        roster_fingerprint=council.roster().fingerprint(),
        band=band,
        marks=all_marked(manifest, band),
    )


# ---------------------------------------------------------------------------
# blinding
# ---------------------------------------------------------------------------


class TestBlinding:
    """Which side is ALCF must not be derivable from a built item."""

    def test_no_built_item_names_a_side_anywhere(self, material: Any) -> None:
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        assert tasks, "the pilot built no items"

        for task in tasks:
            blob = json.dumps(
                {"item_id": task.item_id, "evidence": dict(task.evidence)}
            ).lower()
            assert "alcf" not in blob, task.item_id
            assert "go_mf" not in blob, task.item_id
            assert "gomf" not in blob, task.item_id
            # Nor by shape: both sides are rendered identically, so neither
            # can be recognised by its formatting.
            assert set(task.evidence) == {"text", "source", "answer_a", "answer_b"}
        assert A.blinding_problems(tasks) == []

    def test_a_leak_is_reported_rather_than_submitted(self, material: Any) -> None:
        """The guard is what makes the blinding a property and not a habit."""
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        leaky = tasks[0].model_copy(
            update={
                "evidence": {**tasks[0].evidence, "source": "alcf_bench_a"},
            }
        )
        problems = A.blinding_problems([leaky, *tasks[1:]])
        assert [p["field"] for p in problems] == ["source"]
        assert problems[0]["found"] == "alcf"

    def test_run_tranches_refuses_a_leaking_item_before_submitting(
        self, tmp_path: Path, material: Any
    ) -> None:
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        tasks[0] = tasks[0].model_copy(
            update={"evidence": {**tasks[0].evidence, "text": "go_mf said so"}}
        )
        council = Council(tmp_path / "council.sqlite")
        ledger = Ledger(tmp_path / "run")
        try:
            with pytest.raises(Refused, match="name a side"):
                A.run_tranches(
                    council.url,
                    "test",
                    tasks,
                    ledger=ledger,
                    manifest_id=manifest.manifest_id,
                    council=council,
                )
            assert council.submits == []
            assert ledger.records() == []
        finally:
            council.close()

    def test_an_empty_answer_renders_as_no_terms_not_as_nothing(self) -> None:
        """A declining side must be visibly declining, not a blank block."""
        assert A.render_answer(MENU, []) == A.NO_TERMS
        assert A.parse_answer(A.NO_TERMS) == ()

    def test_answers_render_in_menu_order_not_set_order(self) -> None:
        """Set order is not reproducible between processes; the menu's is."""
        text = A.render_answer(MENU, frozenset({"GO:0016887", "GO:0015749"}))
        assert text == (
            "GO:0015749 | glucose transmembrane transport\n"
            "GO:0016887 | ATP hydrolysis activity"
        )
        assert A.parse_answer(text) == ("GO:0015749", "GO:0016887")


# ---------------------------------------------------------------------------
# the swap twin
# ---------------------------------------------------------------------------


class TestSwapItems:
    def test_a_swapped_item_is_the_same_item_with_the_answers_reversed(
        self, material: Any
    ) -> None:
        manifest, items = material
        tasks = {t.item_id: t for t in A.build_items(manifest, items, "pilot")}
        swapped = sorted(i for i in tasks if i.endswith(":swap"))
        assert swapped, "the manifest sampled no swap slice"

        for swap_id in swapped:
            base_id = swap_id[: -len(":swap")]
            assert base_id in tasks, "a swapped twin with no original"
            base, twin = tasks[base_id], tasks[swap_id]
            assert twin.evidence["answer_a"] == base.evidence["answer_b"]
            assert twin.evidence["answer_b"] == base.evidence["answer_a"]
            # Everything else is the SAME item: same annotation, same menu.
            assert twin.evidence["text"] == base.evidence["text"]
            assert twin.evidence["source"] == base.evidence["source"]
            assert [e.id for e in twin.menu] == [e.id for e in base.menu]

    def test_the_manifest_orientation_is_what_decides_a_b(self, material: Any) -> None:
        """A/B comes from the manifest entry, never from the row's own order."""
        manifest, items = material
        by_item_id = {i.item_id: i for i in items}
        tasks = {t.item_id: t for t in A.build_items(manifest, items, "pilot")}
        for entry in manifest.entries:
            row = by_item_id[entry["item_id"]]
            task = tasks[entry["id"]]
            alcf = A.render_answer(row.menu, row.candidate_selected)
            gomf = A.render_answer(row.menu, row.baseline_selected)
            if entry["alcf_is_A"]:
                assert task.evidence["answer_a"] == alcf
                assert task.evidence["answer_b"] == gomf
            else:
                assert task.evidence["answer_a"] == gomf
                assert task.evidence["answer_b"] == alcf

    def test_items_are_built_in_the_manifest_s_frozen_execution_order(
        self, material: Any
    ) -> None:
        """Class order would correlate queue position with class."""
        manifest, items = material
        built = [t.item_id for t in A.build_items(manifest, items, "pilot")]
        assert built == list(manifest.execution_order["pilot"])

    def test_a_pool_the_manifest_does_not_hold_is_refused(self, material: Any) -> None:
        manifest, items = material
        with pytest.raises(Refused, match="no entries for pool"):
            A.build_items(manifest, items, "heldout_typo")

    def test_an_item_the_extract_does_not_hold_is_refused(self, material: Any) -> None:
        """sample and adjudicate must read the SAME extract."""
        manifest, items = material
        with pytest.raises(Refused, match="SAME extract"):
            A.build_items(manifest, items[:2], "pilot")


# ---------------------------------------------------------------------------
# the token ceiling
# ---------------------------------------------------------------------------


def run_once(
    council: Council,
    ledger: Ledger,
    manifest: Any,
    tasks: Sequence[Any],
    **kwargs: Any,
) -> Dict[str, Any]:
    return A.run_tranches(
        council.url,
        "test",
        list(tasks),
        ledger=ledger,
        manifest_id=manifest.manifest_id,
        council=council,
        **kwargs,
    )


class TestTokenCeiling:
    """A SOFT admission threshold over ARGO-billed seats, per invocation."""

    def test_a_tranche_is_refused_once_the_ceiling_is_reached(
        self, tmp_path: Path, material: Any
    ) -> None:
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite")
        ledger = Ledger(tmp_path / "run")
        seed_calibration(ledger, council, manifest)
        try:
            # Tranche 0 is admitted while the total is still zero and spends
            # 12,000 ARGO tokens; tranche 1 is then refused.  That is the
            # "at most one tranche may overshoot" rule in one run.
            report = run_once(
                council, ledger, manifest, tasks, tranche_size=4, token_ceiling=10_000
            )
        finally:
            council.close()

        assert [t["outcome"] for t in report["tranches"]] == [
            "submitted",
            "refused_token_ceiling",
        ]
        assert len(report["job_ids"]) == 1
        assert len(council.submits) == 1, "a refused tranche must not submit"
        assert report["stopped"]["reason"] == "refused_token_ceiling"
        assert report["usage"]["argo_tokens"] == 12_000
        assert report["overshoot"] is True

        stops = ledger.records("stop")
        assert [s["reason"] for s in stops] == [
            "refused_token_ceiling",
            "token_ceiling_overshoot",
        ]
        assert stops[0]["tranche_index"] == 1
        assert stops[0]["submission_token"] == report["tranches"][1]["token"]

    def test_a_cli_seat_s_real_usage_is_reported_and_never_metered(
        self, tmp_path: Path, material: Any
    ) -> None:
        """The executor's correction to C10, as an executable claim.

        The CLI proposer reports 20,000 real tokens per call -- far more than
        the ARGO seats -- so a ceiling of 50,000 is cleared by the ARGO total
        (12,000 after tranche 0) and blown twice over by the combined one
        (92,000).  Both tranches running is what proves the CLI tokens were
        not counted; the ledger still carries them.
        """
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite")
        ledger = Ledger(tmp_path / "run")
        seed_calibration(ledger, council, manifest)
        try:
            report = run_once(
                council, ledger, manifest, tasks, tranche_size=4, token_ceiling=50_000
            )
        finally:
            council.close()

        assert [t["outcome"] for t in report["tranches"]] == ["submitted", "submitted"]
        assert report["stopped"] is None
        usage = report["usage"]
        assert usage["argo_tokens"] == 24_000
        assert usage["unmetered_tokens"] == 160_000
        assert usage["by_endpoint_kind"] == {"argo": 24_000, "claude_cli": 160_000}
        assert report["overshoot"] is False, (
            "the ARGO total is under the ceiling; only the CLI total is over, and "
            "no number of subscription tokens moves the ARGO bill"
        )
        # And the subscription spend is on the record, not silently dropped.
        complete = [r for r in ledger.records("tranche") if r.get("phase") == "complete"]
        assert [r["usage"]["unmetered_tokens"] for r in complete] == [80_000, 80_000]

    def test_missing_usage_on_an_argo_ok_call_refuses_further_submission(
        self, tmp_path: Path, material: Any
    ) -> None:
        """An unmeasurable spend makes the ceiling unenforceable."""
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(
            tmp_path / "council.sqlite",
            argo=InstrumentedBackend(
                unanimous_script, kind="argo", usage=dict(ARGO_USAGE), drop_usage=True
            ),
        )
        ledger = Ledger(tmp_path / "run")
        seed_calibration(ledger, council, manifest)
        try:
            with pytest.raises(Refused, match="carry no token usage"):
                run_once(
                    council,
                    ledger,
                    manifest,
                    tasks,
                    tranche_size=4,
                    token_ceiling=10_000_000,
                )
            assert len(council.submits) == 1, "the second tranche must not be submitted"
            submitted = [
                r["job_id"]
                for r in ledger.records("tranche")
                if r.get("phase") == "submitted"
            ]
            usage = A.usage_report(council.require_schema(), submitted)
            assert usage["missing_usage"], "the ARGO calls reported no usage"
            assert usage["argo_tokens"] == 0
        finally:
            council.close()

    def test_a_null_usage_on_a_cli_seat_is_not_missing(
        self, tmp_path: Path, material: Any
    ) -> None:
        """Only ARGO-billed calls are checked, so a CLI null cannot halt a run.

        The complement of the test above, and the reason the two are
        separate: a failed CLI call records ``usage: null``, and a rule that
        treated any null as missing would stop a run over a seat whose
        tokens it never counted in the first place.
        """
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(
            tmp_path / "council.sqlite",
            cli=InstrumentedBackend(
                unanimous_script,
                kind="claude_cli",
                usage=dict(CLI_USAGE),
                drop_usage=True,
            ),
        )
        ledger = Ledger(tmp_path / "run")
        seed_calibration(ledger, council, manifest)
        try:
            report = run_once(
                council, ledger, manifest, tasks, tranche_size=4, token_ceiling=10_000_000
            )
            assert [t["outcome"] for t in report["tranches"]] == [
                "submitted",
                "submitted",
            ]
            assert report["usage"]["missing_usage"] == []
            assert report["usage"]["unmetered_tokens"] == 0, (
                "the CLI seat reported nothing, which is a fact about the CLI and "
                "not a gap in the ARGO accounting"
            )
            assert report["usage"]["argo_tokens"] == 24_000
        finally:
            council.close()


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


class TestResume:
    """Re-running must adopt what was paid for, never pay again."""

    def test_a_second_invocation_submits_nothing_and_adopts_every_job(
        self, tmp_path: Path, material: Any
    ) -> None:
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite")
        first_ledger = Ledger(tmp_path / "run", invocation_id="inv-1")
        seed_calibration(first_ledger, council, manifest)
        try:
            first = run_once(council, first_ledger, manifest, tasks, tranche_size=4)
            assert [t["outcome"] for t in first["tranches"]] == [
                "submitted",
                "submitted",
            ]
            submitted_once = list(council.submits)

            second_ledger = Ledger(tmp_path / "run", invocation_id="inv-2")
            second = run_once(council, second_ledger, manifest, tasks, tranche_size=4)
        finally:
            council.close()

        assert [t["outcome"] for t in second["tranches"]] == ["adopted", "adopted"]
        assert council.submits == submitted_once, "the resume submitted something"
        assert second["job_ids"] == first["job_ids"]
        # The ceiling meters THIS invocation: an adopted job was paid for by
        # the previous one and is reported rather than re-charged.
        assert second["usage"]["argo_tokens"] == 0
        assert [t["usage"]["argo_tokens"] for t in second["tranches"]] == [
            12_000,
            12_000,
        ]

    def test_a_crash_between_submit_and_the_ledger_record_does_not_double_submit(
        self, tmp_path: Path, material: Any
    ) -> None:
        """The exact window the submission token exists to close.

        The ``planned`` record is on disk (it is written and fsync-ed BEFORE
        the submit); everything after it is lost, so the ledger has no idea
        the job exists.  The resume recomputes the token, finds the job in
        the store by its label, and adopts it.
        """
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite")
        ledger = Ledger(tmp_path / "run", invocation_id="inv-1")
        seed_calibration(ledger, council, manifest)
        try:
            first = run_once(council, ledger, manifest, tasks, tranche_size=8)
            job_id = first["job_ids"][0]

            # Simulate the crash: drop every record from the `planned` one on.
            lines = ledger.path.read_text(encoding="utf-8").splitlines()
            keep: List[str] = []
            for line in lines:
                keep.append(line)
                record = json.loads(line)
                if record.get("kind") == "tranche" and record.get("phase") == "planned":
                    break
            ledger.path.write_text("\n".join(keep) + "\n", encoding="utf-8")
            assert not [
                r for r in ledger.records("tranche") if r.get("phase") == "submitted"
            ], "the crash left the job id on disk after all"

            resumed = Ledger(tmp_path / "run", invocation_id="inv-2")
            second = run_once(council, resumed, manifest, tasks, tranche_size=8)
        finally:
            council.close()

        assert len(council.submits) == 1, "the job was submitted a second time"
        assert second["tranches"][0]["outcome"] == "adopted"
        assert second["tranches"][0]["job_id"] == job_id
        # And the reconciliation is itself recorded, so the gap is visible.
        reconciled = [
            r for r in resumed.records("tranche") if r.get("phase") == "reconciled"
        ]
        assert [r["job_id"] for r in reconciled] == [job_id]

    def test_an_item_that_already_has_an_ok_result_is_never_re_submitted(
        self, tmp_path: Path, material: Any
    ) -> None:
        """A re-plan that changes the tranching must still not re-pay.

        The first invocation runs 8 items as two tranches of four.  The
        second runs the SAME items as one tranche of eight -- a different
        plan, so a different token and no job to adopt -- and must still send
        nothing, because every item already has an ok result in the store.
        """
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite")
        ledger = Ledger(tmp_path / "run", invocation_id="inv-1")
        seed_calibration(ledger, council, manifest)
        try:
            first = run_once(council, ledger, manifest, tasks, tranche_size=4)
            answered = {
                row["item_id"]
                for job_id in first["job_ids"]
                for row in council.require_schema().get_items(job_id, status="ok")
            }
            assert answered == {t.item_id for t in tasks}

            second = run_once(
                council,
                Ledger(tmp_path / "run", invocation_id="inv-2"),
                manifest,
                tasks,
                tranche_size=8,
            )
        finally:
            council.close()

        assert [t["outcome"] for t in second["tranches"]] == ["skipped_already_ok"]
        assert len(council.submits) == 2, "the re-planned tranche re-submitted"
        assert second["job_ids"] == []

    def test_the_same_id_in_the_other_orientation_is_not_an_answer(
        self, tmp_path: Path, material: Any
    ) -> None:
        """C9: the reuse key is the content and the roster, not the id.

        The same row asked the other way round is a different question about
        the same protein, and an ``item_id`` match is not evidence that it was
        answered.  Built here by flipping the item's two answers, which is
        exactly what a re-sample under a different seed would produce.
        """
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite")
        ledger = Ledger(tmp_path / "run")
        seed_calibration(ledger, council, manifest)
        spec = council.query(A.QUERY_REF)
        try:
            run_once(council, ledger, manifest, tasks, tranche_size=8)
            store = council.require_schema()
            answered = A._already_ok(
                store, A.QUERY_NAME, A.QUERY_VERSION, council.roster().fingerprint()
            )
            assert A.item_key(spec, tasks[0]) in answered

            flipped = tasks[0].model_copy(
                update={
                    "evidence": {
                        **tasks[0].evidence,
                        "answer_a": tasks[0].evidence["answer_b"],
                        "answer_b": tasks[0].evidence["answer_a"],
                    }
                }
            )
            assert A.item_key(spec, flipped) not in answered, (
                "the same id in the other orientation read as already answered"
            )
            # Nor does another bench's answer count as this bench's.
            assert (
                A._already_ok(store, A.QUERY_NAME, A.QUERY_VERSION, "0" * 64) == set()
            )
        finally:
            council.close()

    def test_the_token_is_the_same_in_a_later_process_and_covers_orientation(
        self, material: Any
    ) -> None:
        """Determinism is the whole mechanism; orientation is part of the key."""
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        kwargs = dict(
            run="run",
            manifest_id=manifest.manifest_id,
            tranche_index=0,
            query_ref=A.QUERY_REF,
            roster_fingerprint="f" * 64,
        )
        assert A.submission_token(items=tasks, **kwargs) == A.submission_token(
            items=tasks, **kwargs
        )
        # A swapped twin differs from its original only in orientation, and
        # the token has to see that.
        swap_id = next(t.item_id for t in tasks if t.item_id.endswith(":swap"))
        base_id = swap_id[: -len(":swap")]
        by_id = {t.item_id: t for t in tasks}
        assert A.submission_token(
            items=[by_id[base_id]], **kwargs
        ) != A.submission_token(items=[by_id[swap_id]], **kwargs)
        # And so does a different tranche index, roster or manifest.
        for change in (
            {"tranche_index": 1},
            {"roster_fingerprint": "e" * 64},
            {"manifest_id": "other"},
            {"run": "other-run"},
        ):
            assert A.submission_token(items=tasks, **{**kwargs, **change}) != (
                A.submission_token(items=tasks, **kwargs)
            )


# ---------------------------------------------------------------------------
# halting, and the spot-check gate
# ---------------------------------------------------------------------------


def broken_proposer_script(req: Any) -> Any:
    """Every proposer call fails, so the item settles ``error``."""
    if req.role == "proposer":
        return RuntimeError("the gateway refused")
    if req.role == "critic":
        return critic_reply()
    return meta_reply("GO:0015749")


class TestHaltAndGates:
    def test_error_items_halt_the_run_and_the_tranche_is_not_complete(
        self, tmp_path: Path, material: Any
    ) -> None:
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite", script=broken_proposer_script)
        ledger = Ledger(tmp_path / "run")
        seed_calibration(ledger, council, manifest)
        try:
            with pytest.raises(Refused, match="error/cancelled item"):
                run_once(council, ledger, manifest, tasks, tranche_size=4)
        finally:
            council.close()

        assert len(council.submits) == 1
        stops = ledger.records("stop")
        assert [s["reason"] for s in stops] == ["halted_failed_items"]
        assert len(stops[0]["failed_item_ids"]) == 4
        # A halted tranche is NEVER recorded complete: the next invocation
        # must not read it as a finished one.
        assert not [
            r for r in ledger.records("tranche") if r.get("phase") == "complete"
        ]

    def test_retry_failed_resubmits_the_failed_items_as_their_own_job(
        self, tmp_path: Path, material: Any
    ) -> None:
        """The operator's explicit act, and it is reconcilable in its turn."""
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite", script=broken_proposer_script)
        ledger = Ledger(tmp_path / "run")
        seed_calibration(ledger, council, manifest)
        try:
            report = run_once(
                council, ledger, manifest, tasks, tranche_size=8, retry_failed=True
            )
        finally:
            council.close()

        entry = report["tranches"][0]
        assert entry["outcome"] == "halted_failed_items"
        assert entry["retry"]["job_id"]
        assert sorted(entry["retry"]["items"]) == sorted(t.item_id for t in tasks)
        # It failed again -- the fake is still broken -- and says so rather
        # than reporting a retry as a repair.
        assert len(entry["retry"]["failed_again"]) == len(tasks)
        assert [r["phase"] for r in ledger.records("tranche")] == [
            "planned",
            "submitted",
            "retry_planned",
            "retry_submitted",
        ]

    def test_a_tranche_after_the_first_needs_a_matching_calibration_record(
        self, tmp_path: Path, material: Any
    ) -> None:
        """No calibration record at all: the second tranche does not start."""
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite")
        ledger = Ledger(tmp_path / "run")
        try:
            report = run_once(council, ledger, manifest, tasks, tranche_size=4)
        finally:
            council.close()

        assert [t["outcome"] for t in report["tranches"]] == [
            "submitted",
            "refused_spot_check",
        ]
        assert "no calibration record matches" in report["stopped"]["detail"]
        assert len(council.submits) == 1
        # The first tranche recorded the ids the operator has to mark, at the
        # cautious size, because no band is known yet.
        planned = next(
            r for r in ledger.records("tranche") if r.get("phase") == "planned"
        )
        assert len(planned["spot_check_ids"]) == 4, "min(20, the tranche's 4 items)"
        assert planned["spot_check_band"] is None

    def test_an_unmarked_spot_check_item_blocks_the_next_tranche(
        self, tmp_path: Path, material: Any
    ) -> None:
        """A calibration record that does not cover the ids is not enough."""
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite")
        ledger = Ledger(tmp_path / "run")
        ledger.append(
            "calibration",
            manifest_id=manifest.manifest_id,
            adjudicate_spec_hash=A.adjudicate_spec_hash(council.query(A.QUERY_REF)),
            roster_fingerprint=council.roster().fingerprint(),
            band="trusted",
            marks={tasks[0].item_id: "agree"},
        )
        try:
            report = run_once(council, ledger, manifest, tasks, tranche_size=4)
        finally:
            council.close()

        assert report["tranches"][1]["outcome"] == "refused_spot_check"
        assert "no imported mark" in report["stopped"]["detail"]

    def test_a_not_trusted_band_blocks_the_next_tranche(
        self, tmp_path: Path, material: Any
    ) -> None:
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite")
        ledger = Ledger(tmp_path / "run")
        seed_calibration(ledger, council, manifest, band="not_trusted")
        try:
            report = run_once(council, ledger, manifest, tasks, tranche_size=4)
        finally:
            council.close()

        assert report["tranches"][1]["outcome"] == "refused_spot_check"
        assert "not_trusted" in report["stopped"]["detail"]

    def test_a_calibration_record_for_another_manifest_or_roster_does_not_count(
        self, tmp_path: Path, material: Any
    ) -> None:
        """Marks collected against another sample say nothing about this one."""
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite")
        ledger = Ledger(tmp_path / "run")
        spec_hash = A.adjudicate_spec_hash(council.query(A.QUERY_REF))
        roster_fp = council.roster().fingerprint()
        for wrong in (
            {"manifest_id": "another-manifest"},
            {"adjudicate_spec_hash": "0" * 64},
            {"roster_fingerprint": "0" * 64},
        ):
            ledger.append(
                "calibration",
                **{
                    "manifest_id": manifest.manifest_id,
                    "adjudicate_spec_hash": spec_hash,
                    "roster_fingerprint": roster_fp,
                    "band": "trusted",
                    "marks": all_marked(manifest),
                    **wrong,
                },
            )
        try:
            gate = A.calibration_gate(
                ledger,
                manifest_id=manifest.manifest_id,
                spec_hash=spec_hash,
                roster_fingerprint=roster_fp,
                required_ids=[t.item_id for t in tasks],
            )
        finally:
            council.close()
        assert gate["ok"] is False
        assert gate["records"] == 0
        assert "no calibration record matches" in gate["reason"]

    def test_a_trusted_band_shrinks_the_spot_check_and_a_conditional_one_does_not(
        self, tmp_path: Path, material: Any
    ) -> None:
        """5 if trusted, 20 if conditional -- capped by the tranche's size."""
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        assert len(tasks) == 8
        for band, expected in (("trusted", 5), ("conditional", 8)):
            council = Council(tmp_path / f"council-{band}.sqlite")
            ledger = Ledger(tmp_path / f"run-{band}")
            seed_calibration(ledger, council, manifest, band=band)
            try:
                run_once(council, ledger, manifest, tasks, tranche_size=8)
            finally:
                council.close()
            planned = next(
                r for r in ledger.records("tranche") if r.get("phase") == "planned"
            )
            # Tranche 0 is not itself gated, but the band the gate would read
            # is what sizes its spot check for the tranche that follows.
            assert len(planned["spot_check_ids"]) == min(expected, 8), band

    def test_the_spot_check_sample_is_seeded_by_the_tranche_s_own_token(self) -> None:
        ids = [f"item-{i:02d}" for i in range(20)]
        first = A.spot_check_ids("tok-a", ids, 5)
        assert first == A.spot_check_ids("tok-a", ids, 5)
        assert first != A.spot_check_ids("tok-b", ids, 5)
        assert len(first) == 5
        assert set(first) <= set(ids)
        assert A.spot_check_ids("tok-a", ids, 99) == tuple(sorted(ids))
        assert A.spot_check_ids("tok-a", [], 5) == ()


# ---------------------------------------------------------------------------
# derived verdict
# ---------------------------------------------------------------------------


class TestDerivedVerdict:
    """All four values, and the one the final set cannot produce by itself."""

    def test_the_final_set_equalling_a_side_names_that_side(self) -> None:
        assert A.derived_verdict(["GO:1"], ["GO:1"], ["GO:2"]) == "A"
        assert A.derived_verdict(["GO:2"], ["GO:1"], ["GO:2"]) == "B"
        # Set equality, not list equality: the order a council reported its
        # ids in is not part of its answer.
        assert A.derived_verdict(["GO:2", "GO:1"], ["GO:1", "GO:2"], ["GO:3"]) == "A"

    def test_a_set_that_is_neither_side_s_is_neither(self) -> None:
        assert A.derived_verdict(["GO:3"], ["GO:1"], ["GO:2"]) == "neither"
        assert A.derived_verdict([], ["GO:1"], ["GO:2"]) == "neither"
        # A superset of A is not A: P6 makes an unsupported extra a failure.
        assert A.derived_verdict(["GO:1", "GO:3"], ["GO:1"], ["GO:2"]) == "neither"

    def test_both_acceptable_comes_only_from_the_deciding_voice(self) -> None:
        """It is the one value equality cannot produce, so it must override."""
        assert A.derived_verdict(["GO:1"], ["GO:1"], ["GO:2"], "both_acceptable") == (
            "both_acceptable"
        )
        assert A.derived_verdict(["GO:9"], ["GO:1"], ["GO:2"], "both_acceptable") == (
            "both_acceptable"
        )
        # Any other `prefers` leaves the derivation to the sets: a voice that
        # merely said "A" has said nothing the set does not already say.
        assert A.derived_verdict(["GO:2"], ["GO:1"], ["GO:2"], "A") == "B"
        assert A.derived_verdict(["GO:1"], ["GO:1"], ["GO:2"], None) == "A"

    def test_the_four_values_map_through_the_manifest_to_a_pipeline(self) -> None:
        assert A.verdict_side("A", True) == "alcf"
        assert A.verdict_side("A", False) == "gomf"
        assert A.verdict_side("B", True) == "gomf"
        assert A.verdict_side("B", False) == "alcf"
        # Neither of these names a side, in either orientation.
        for orientation in (True, False):
            assert A.verdict_side("both_acceptable", orientation) == "both_acceptable"
            assert A.verdict_side("neither", orientation) == "neither"


# ---------------------------------------------------------------------------
# read_results
# ---------------------------------------------------------------------------


def adjudicated(
    tmp_path: Path, material: Any, script: Any = None, **kwargs: Any
) -> Any:
    """Run the whole pilot and read it back.  ``(results, report, council)``."""
    manifest, items = material
    tasks = A.build_items(manifest, items, "pilot")
    council = Council(tmp_path / "council.sqlite", script=script)
    ledger = Ledger(tmp_path / "run")
    seed_calibration(ledger, council, manifest)
    report = run_once(council, ledger, manifest, tasks, tranche_size=8, **kwargs)
    results = A.read_results(council.url, report["job_ids"], manifest)
    return results, report, council


class TestReadResults:
    def test_a_unanimous_item_reports_the_bench_s_own_metadata(
        self, tmp_path: Path, material: Any
    ) -> None:
        manifest, _items = material
        results, _report, council = adjudicated(tmp_path, material)
        try:
            assert len(results) == len(manifest.entries)
            by_id = {r.item_id: r for r in results}
            result = by_id["c-dj00"]

            assert result.status == "ok"
            assert result.final_ids == ("GO:0015749",)
            assert result.decided_by == "unanimous"
            assert result.meta_skipped is True
            assert result.meta is None, "meta did not run on a unanimous item"
            # Meta did not decide, so the ITEM's metadata is the bench's.
            assert result.failure_type == "too_generic"
            assert result.failure_type_source == "proposer_majority"
            assert result.correct_term_off_menu is False
            assert result.rules_cited == ("P1", "P3"), "the sorted union"
            assert result.confidence == "high"
            assert {p["seat"] for p in result.proposers} == {
                "argo_a",
                "argo_b",
                "cli_c",
            }
            assert sorted(result.why) == ["argo_a", "argo_b", "cli_c"]
            assert all(v.startswith("P1 maps") for v in result.why.values())
            assert sorted(result.off_menu_description) == ["argo_a", "argo_b", "cli_c"]
            # The critic audits the panel and is not a vote, so it is
            # reported separately and is in neither of the two above.
            assert result.critic["seat"] == "critic"
            assert result.critic["verdict"] == "consistent"
            assert "critic" not in result.why
        finally:
            council.close()

    def test_the_two_sets_are_read_back_off_the_item_s_own_evidence(
        self, tmp_path: Path, material: Any
    ) -> None:
        """What is compared is what the panel was shown, not a re-derivation."""
        manifest, items = material
        results, _report, council = adjudicated(tmp_path, material)
        try:
            by_item_id = {i.item_id: i for i in items}
            for result in results:
                row = by_item_id[result.item_id.split(":swap")[0]]
                alcf = tuple(A.parse_answer(A.render_answer(row.menu, row.candidate_selected)))
                gomf = tuple(A.parse_answer(A.render_answer(row.menu, row.baseline_selected)))
                if result.alcf_is_a:
                    assert result.set_a == alcf and result.set_b == gomf
                else:
                    assert result.set_a == gomf and result.set_b == alcf
        finally:
            council.close()

    def test_a_derived_verdict_is_mapped_to_a_pipeline_through_the_manifest(
        self, tmp_path: Path, material: Any
    ) -> None:
        """The panel's A/B becomes a statement about ALCF or go_mf@4.

        Every item here settles on ``GO:0015749``, which is the go_mf@4 side
        of a ``disjoint`` conflict and only part of the ALCF side of a
        ``partial`` one -- so the disjoint items read ``gomf`` and the
        partial ones ``neither``, whichever way round the manifest put them.
        """
        manifest, _items = material
        results, _report, council = adjudicated(tmp_path, material)
        try:
            by_id = {r.item_id: r for r in results}
            for item_id, result in sorted(by_id.items()):
                expected = A.verdict_side(result.derived_verdict, result.alcf_is_a)
                assert result.side == expected
                if item_id.startswith("c-dj"):
                    assert result.derived_verdict in ("A", "B"), item_id
                    assert result.side == "gomf", item_id
                else:
                    assert result.side == "neither", item_id
            # A swapped twin lands on the same SIDE as its original even
            # though its A/B letter is the opposite -- which is the whole
            # point of mapping through the manifest.
            for item_id in [i for i in by_id if i.endswith(":swap")]:
                base = by_id[item_id[: -len(":swap")]]
                twin = by_id[item_id]
                assert twin.side == base.side
                if base.derived_verdict in ("A", "B"):
                    assert twin.derived_verdict != base.derived_verdict
        finally:
            council.close()

    def test_when_meta_decides_the_item_s_metadata_is_meta_s(
        self, tmp_path: Path, material: Any
    ) -> None:
        """C7's first clause, with the bench's own view kept beside it."""
        results, _report, council = adjudicated(
            tmp_path, material, script=split_script
        )
        try:
            result = next(r for r in results if r.item_id == "c-dj00")
            assert result.decided_by == "meta"
            assert result.meta is not None
            assert result.failure_type == "wrong_substrate", "meta's, not the bench's"
            assert result.failure_type_source == "meta"
            assert result.rules_cited == ("P2",), "meta's citation"
            assert result.confidence == "medium", "meta's final_confidence"
            assert result.deciding_prefers == "B"
            # The bench's view is still reported -- a reader must be able to
            # see what meta overruled.
            assert result.proposer_consensus["failure_type"] == "too_generic"
            assert result.proposer_consensus["rules_cited"] == ["P1", "P3"]
            assert result.proposer_consensus["prefers"] is None, "they disagreed"
            # meta's reasoning is seat-labelled alongside the proposers'.
            assert "meta" in result.why
        finally:
            council.close()

    def test_a_three_way_split_on_failure_type_reads_mixed(
        self, tmp_path: Path, material: Any
    ) -> None:
        """No value is what the bench said, so neither is reported as if it was."""

        def three_ways(req: Any) -> Dict[str, Any]:
            if req.role == "proposer":
                kinds = {
                    "argo_a": "too_generic",
                    "argo_b": "wrong_substrate",
                    "cli_c": "missed_term",
                }
                return proposer_reply(
                    "GO:0015749",
                    failure_type=kinds[req.seat.seat_name],
                    rules=("P1",) if req.seat.seat_name == "argo_a" else ("P4",),
                    confidence="low" if req.seat.seat_name == "cli_c" else "high",
                )
            if req.role == "critic":
                return critic_reply()
            return meta_reply("GO:0015749")

        results, _report, council = adjudicated(tmp_path, material, script=three_ways)
        try:
            result = results[0]
            assert result.decided_by == "unanimous", "they agreed on the TERMS"
            assert result.failure_type == "mixed"
            assert result.failure_type_source == "proposer_mixed"
            assert result.rules_cited == ("P1", "P4"), "the sorted union, still"
            assert result.confidence == "low", "the lowest any voice expressed"
        finally:
            council.close()

    def test_an_item_outside_the_manifest_cannot_be_attributed_and_is_refused(
        self, tmp_path: Path, material: Any
    ) -> None:
        manifest, items = material
        tasks = A.build_items(manifest, items, "pilot")
        council = Council(tmp_path / "council.sqlite")
        try:
            stray = tasks[0].model_copy(update={"item_id": "not-in-the-manifest"})
            job_id = council.submit(A.QUERY_REF, [stray], label="stray")
            council.run(job_id)
            with pytest.raises(Refused, match="not in the manifest"):
                A.read_results(council.url, [job_id], manifest)
        finally:
            council.close()

    def test_a_job_of_another_query_type_is_refused(
        self, tmp_path: Path, material: Any
    ) -> None:
        """read_results reads one query type's vocabulary and says so."""
        manifest, _items = material
        council = Council(tmp_path / "council.sqlite")
        try:
            store = council.require_schema()
            from .store_contract import call_row, item_row, job_row

            job_id = store.create_job(
                job_row(query_name="go_mf", query_version="4"),
                [item_row("c-dj00")],
                [call_row("c-dj00")],
            )
            with pytest.raises(Refused, match="not go_mf_adjudicate@1"):
                A.read_results(council.url, [job_id], manifest)
        finally:
            council.close()

    def test_a_settled_ok_call_with_no_validated_metadata_fails_the_read(
        self, tmp_path: Path, material: Any
    ) -> None:
        """C8: metadata comes from a schema-valid reply or from nowhere.

        The reply is blanked in the store AFTER the job settled, which is the
        shape a build that forgot to persist ``reply_parsed`` would leave --
        and reading a label out of the raw prose instead is exactly what must
        not happen silently.
        """
        import sqlalchemy as sa

        from kbutillib.domains.ai.council.store import schema as sch

        manifest, _items = material
        results, report, council = adjudicated(tmp_path, material)
        try:
            assert results, "nothing was read back to begin with"
            store = council.require_schema()
            with store.engine.begin() as conn:
                conn.execute(
                    sa.update(sch.calls)
                    .where(sch.calls.c.role == "proposer")
                    .values(reply_parsed=None)
                )
            with pytest.raises(Refused, match="carries no parsed reply"):
                A.read_results(council.url, report["job_ids"], manifest)
        finally:
            council.close()

    def test_a_reply_that_does_not_satisfy_its_role_schema_fails_the_read(
        self, tmp_path: Path, material: Any
    ) -> None:
        import sqlalchemy as sa

        from kbutillib.domains.ai.council.store import schema as sch

        manifest, _items = material
        _results, report, council = adjudicated(tmp_path, material)
        try:
            store = council.require_schema()
            with store.engine.begin() as conn:
                conn.execute(
                    sa.update(sch.calls)
                    .where(sch.calls.c.role == "proposer")
                    .values(reply_parsed={"prefers": "sideways"})
                )
            with pytest.raises(Refused, match="does not satisfy"):
                A.read_results(council.url, report["job_ids"], manifest)
        finally:
            council.close()

    def test_a_unanimous_both_acceptable_bench_overrides_equality_end_to_end(
        self, tmp_path: Path, material: Any
    ) -> None:
        """The fourth value, through the real engine rather than the helper.

        Every proposer settles on ``GO:0015749`` -- which IS one side's set on
        the disjoint items -- and every one of them says ``both_acceptable``.
        A derivation that read the sets alone would report that side and lose
        the judgement the panel actually made.
        """

        def both_ok(req: Any) -> Dict[str, Any]:
            if req.role == "proposer":
                return proposer_reply(
                    "GO:0015749", prefers="both_acceptable", failure_type="none"
                )
            if req.role == "critic":
                return critic_reply()
            return meta_reply("GO:0015749")

        results, _report, council = adjudicated(tmp_path, material, script=both_ok)
        try:
            disjoint = [r for r in results if r.item_id.startswith("c-dj")]
            assert disjoint
            for result in disjoint:
                assert result.decided_by == "unanimous"
                assert result.deciding_prefers == "both_acceptable"
                assert result.derived_verdict == "both_acceptable"
                assert result.side == "both_acceptable"
                # ...and the set really did equal one of the two, so the
                # override is doing the work and not the equality.
                assert frozenset(result.final_ids) in (
                    frozenset(result.set_a),
                    frozenset(result.set_b),
                )
        finally:
            council.close()

    def test_a_split_bench_does_not_reach_both_acceptable(
        self, tmp_path: Path, material: Any
    ) -> None:
        """Only a UNANIMOUS bench overrides equality (C7)."""

        def one_dissenter(req: Any) -> Dict[str, Any]:
            if req.role == "proposer":
                prefers = "B" if req.seat.seat_name == "cli_c" else "both_acceptable"
                return proposer_reply("GO:0015749", prefers=prefers)
            if req.role == "critic":
                return critic_reply()
            return meta_reply("GO:0015749")

        results, _report, council = adjudicated(
            tmp_path, material, script=one_dissenter
        )
        try:
            result = next(r for r in results if r.item_id == "c-dj00")
            assert result.decided_by == "unanimous", "they agreed on the terms"
            assert result.deciding_prefers is None, "but not on `prefers`"
            assert result.derived_verdict in ("A", "B")
        finally:
            council.close()


# ---------------------------------------------------------------------------
# an unsettled item is reported as what it is
# ---------------------------------------------------------------------------


def test_an_item_that_never_settled_ok_has_no_metadata_to_be_missing(
    tmp_path: Path, material: Any
) -> None:
    """A reader counting adjudications must be able to see the denominator."""
    manifest, items = material
    tasks = A.build_items(manifest, items, "pilot")
    council = Council(tmp_path / "council.sqlite", script=broken_proposer_script)
    try:
        job_id = council.submit(A.QUERY_REF, tasks[:2], label="broken")
        council.run(job_id)
        results = A.read_results(council.url, [job_id], manifest)
    finally:
        council.close()

    assert [r.status for r in results] == ["error", "error"]
    for result in results:
        assert result.failure_type is None
        assert result.failure_type_source == "unsettled"
        assert result.proposers == ()
        assert result.derived_verdict == "neither"
