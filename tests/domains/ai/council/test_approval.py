"""Tests for stage 3: the approval gate and the six checks stage 4 makes.

THE POINT OF THIS MODULE is the SIX SEPARATE REFUSAL TESTS in
:class:`TestValidateApprovalRefusals` -- one per check, each asserting the
check it names. A single "it refuses a bad approval" test would pass with five
of the six checks missing, which is the failure mode this file exists to
prevent.

The quote these tests approve is built from the REAL
:class:`~kbutillib.domains.ai.council.quote.Quote` dataclass and written with
its own ``to_dict``, not from a hand-written dict shaped like one. The ceiling
is derived from ``gateways[].total_tokens_upper`` and
``headline.upper.total_tokens``, so a fixture that invented those key names
would prove the ceiling arithmetic and nothing about whether it can read a
real quote.

Nothing here spends a token, reaches a network or needs the council store: an
approval is a hash of two files on disk plus a clock.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from kbutillib.domains.ai.council.approval import (
    APPROVAL_CHECKS,
    APPROVAL_FILENAME,
    CONSUMED_KEY,
    DEFAULT_EXPIRES_IN_DAYS,
    DEFAULT_HEADROOM,
    RUN_FILENAME,
    Approval,
    ApprovalRefused,
    approve,
    ceiling_from_quote,
    live_bench,
    mark_consumed,
    quote_hash,
    validate_approval,
)
from kbutillib.domains.ai.council.quote import (
    PLAN_FILENAME,
    QUOTE_FILENAME,
    Quote,
    QuoteLine,
    load_plan,
)
from kbutillib.domains.ai.council.types import (
    Roster,
    SeatSpec,
    bench_fingerprint,
    canonical_sha256,
)

#: The two gateways the fixture quote prices, and their UPPER-band totals.
#: Deliberately unequal, so a ceiling that split the unattributed repair
#: tokens evenly instead of pro rata would land on different numbers.
ARGO_UPPER = 120_000_000
MAG_UPPER = 80_000_000

#: The repair line's upper band. Attributed to NO gateway in a real quote
#: (which seat truncates is not knowable in advance), so it is the part of the
#: band a ceiling built only from the gateway block would silently drop.
REPAIR_UPPER = 1_000_000

#: The bench the fixture plan was built against.
PLANNED_BENCH = "b" * 64
PLANNED_ROSTER = "r" * 64

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Scaffolding: a real plan, a real quote, on disk
# ---------------------------------------------------------------------------


def _transport() -> List[Dict[str, Any]]:
    """The plan's ``transport_allocation``, shaped as stage 1 writes it."""
    return [
        {
            "role": "proposer",
            "seat_name": "proposer_1",
            "endpoint": "argo",
            "endpoint_kind": "argo",
            "gateway": "argo",
            "model": "gemini35flash",
            "canonical_model": "gemini-3.5-flash",
            "route_fingerprint": "1" * 64,
        },
        {
            "role": "proposer",
            "seat_name": "proposer_2",
            "endpoint": "mag",
            "endpoint_kind": "openai",
            "gateway": "mag",
            "model": "gpt-5.6-sol",
            "canonical_model": "gpt-5.6",
            "route_fingerprint": "2" * 64,
        },
    ]


def _plan_dict(items: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """A ``PLAN.json`` body, items inline so no scratch file is needed."""
    return {
        "schema_version": 1,
        "generated_at": "2026-10-22T09:00:00Z",
        "query": "go_mf@1",
        "source": "council/go_mf@1",
        "profile": "go_council_v3",
        "mapping_hash": "e" * 64,
        "bench_fingerprint": PLANNED_BENCH,
        "roster_fingerprint": PLANNED_ROSTER,
        "parameter_set": {
            "query": "go_mf@1",
            "bench_fingerprint": PLANNED_BENCH,
            "mapping_hash": "e" * 64,
        },
        "transport_allocation": _transport(),
        "items": list(
            items
            if items is not None
            else [
                {
                    "item_id": "t0",
                    "query_id": "a" * 64,
                    "evidence": {"text": "hydrolase", "source": "ADP1"},
                    "menu": [{"id": "GO:0016787", "label": "hydrolase activity"}],
                }
            ]
        ),
    }


def _write_plan(run_dir: Path, body: Optional[Dict[str, Any]] = None) -> str:
    """Write ``PLAN.json`` with its own declared ``plan_hash``, and return it.

    The hash is :attr:`Plan.plan_hash` via :func:`load_plan` -- stage 1's own
    rule, not a second one this test could get wrong in the same way the
    product does.
    """
    body = dict(body or _plan_dict())
    path = run_dir / PLAN_FILENAME
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    plan_hash = load_plan(run_dir).plan_hash
    body["plan_hash"] = plan_hash
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    return plan_hash


def _quote_object(plan_hash: str) -> Quote:
    """A real :class:`Quote` over two gateways plus an unattributed repair line."""
    lines = (
        QuoteLine(
            line="proposer_1",
            role="proposer",
            basis="exact",
            basis_reason="rendered here",
            calls_total=19276,
            calls_reused=0,
            calls_paid=19276,
            input_tokens_point=ARGO_UPPER * 0.8,
            output_tokens_point=ARGO_UPPER * 0.1,
            output_tokens_upper=ARGO_UPPER * 0.2,
            seat_name="proposer_1",
            gateway="argo",
        ),
        QuoteLine(
            line="proposer_2",
            role="proposer",
            basis="exact",
            basis_reason="rendered here",
            calls_total=19276,
            calls_reused=0,
            calls_paid=19276,
            input_tokens_point=MAG_UPPER * 0.8,
            output_tokens_point=MAG_UPPER * 0.1,
            output_tokens_upper=MAG_UPPER * 0.2,
            seat_name="proposer_2",
            gateway="mag",
        ),
        QuoteLine(
            line="paid_repair",
            role="repair",
            basis="projected",
            basis_reason="the retry pass is first-class, so it is a first-class line",
            calls_total=114,
            calls_reused=0,
            calls_paid=114,
            input_tokens_point=0,
            output_tokens_point=REPAIR_UPPER * 0.5,
            output_tokens_upper=REPAIR_UPPER,
            gateway=None,
        ),
    )
    gateways = (
        {"gateway": "argo", "total_tokens_upper": ARGO_UPPER, "priced": False},
        {"gateway": "mag", "total_tokens_upper": MAG_UPPER, "priced": False},
    )
    return Quote(
        plan_hash=plan_hash,
        plan={"query": "go_mf@1", "n_items": 1, "run_dir": None},
        parameter_set={
            "query": "go_mf@1",
            "bench_fingerprint": PLANNED_BENCH,
            "mapping_hash": "e" * 64,
        },
        provenance={"roster_fingerprint": PLANNED_ROSTER},
        lines=lines,
        reuse={"by_seat": {}, "misses": {}},
        wall_clock={"hours_point": 9.46, "hours_upper": 14.2},
        gateways=gateways,
        balance_delta={"gateways": []},
        token_model={"seats": []},
        meta_fire_rate=0.241,
        meta_fire_rate_n=19268,
        generated_at="2026-10-22T10:00:00Z",
    )


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A run directory holding a real ``PLAN.json`` and ``QUOTE.json``."""
    directory = tmp_path / "2026-10-23-adp1"
    directory.mkdir()
    plan_hash = _write_plan(directory)
    _quote_object(plan_hash).write(directory)
    return directory


@pytest.fixture
def live() -> Dict[str, Any]:
    """The live bench, matching the plan exactly: nothing has moved."""
    return {
        "bench_fingerprint": PLANNED_BENCH,
        "roster_fingerprint": PLANNED_ROSTER,
        "transport_allocation": _transport(),
    }


def _quote_body(run_dir: Path) -> Dict[str, Any]:
    return json.loads((run_dir / QUOTE_FILENAME).read_text(encoding="utf-8"))


def _write_quote_body(run_dir: Path, body: Mapping[str, Any]) -> None:
    (run_dir / QUOTE_FILENAME).write_text(
        json.dumps(dict(body), indent=2, sort_keys=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------


class TestCeiling:
    """The ceiling is the UPPER band times headroom, never the point estimate."""

    def test_the_ceiling_is_the_upper_band_times_the_headroom(
        self, run_dir: Path
    ) -> None:
        quote = _quote_body(run_dir)
        ceiling, basis, warnings = ceiling_from_quote(quote)
        assert basis["headroom"] == DEFAULT_HEADROOM == 1.25
        assert warnings == []
        # argo's own upper band plus its pro-rata share of the repair line.
        share = ARGO_UPPER / (ARGO_UPPER + MAG_UPPER)
        assert ceiling["argo"] == pytest.approx(
            (ARGO_UPPER + REPAIR_UPPER * share) * 1.25, rel=1e-6
        )

    def test_the_ceiling_is_not_the_point_estimate(self, run_dir: Path) -> None:
        quote = _quote_body(run_dir)
        ceiling, _basis, _warnings = ceiling_from_quote(quote)
        points = {
            entry["gateway"]: entry["total_tokens_point"]
            for entry in quote["gateways"]
            if "total_tokens_point" in entry
        }
        uppers = {
            entry["gateway"]: entry["total_tokens_upper"] for entry in quote["gateways"]
        }
        for gateway, value in ceiling.items():
            assert value > uppers[gateway], (
                f"{gateway}'s ceiling is at or below its own upper band; a run "
                "at the band would halt"
            )
            if gateway in points:
                assert value > points[gateway]

    def test_the_unattributed_repair_tokens_are_not_dropped(
        self, run_dir: Path
    ) -> None:
        """The repair line has no gateway, so it must be spread, not lost."""
        quote = _quote_body(run_dir)
        ceiling, basis, _warnings = ceiling_from_quote(quote, headroom=1.0)
        assert basis["unattributed_upper_tokens"] == REPAIR_UPPER
        assert sum(ceiling.values()) >= quote["headline"]["upper"]["total_tokens"]

    def test_a_headroom_below_one_is_refused(self, run_dir: Path) -> None:
        with pytest.raises(ValueError, match="below 1"):
            ceiling_from_quote(_quote_body(run_dir), headroom=0.9)

    def test_an_override_replaces_one_gateway_and_leaves_the_other(
        self, run_dir: Path
    ) -> None:
        quote = _quote_body(run_dir)
        derived, _basis, _warnings = ceiling_from_quote(quote)
        ceiling, basis, _warn = ceiling_from_quote(
            quote, overrides={"mag": 999_999_999}
        )
        assert ceiling["mag"] == 999_999_999
        assert ceiling["argo"] == derived["argo"]
        assert basis["overridden_gateways"] == ["mag"]

    def test_an_override_below_the_derived_ceiling_warns(self, run_dir: Path) -> None:
        _ceiling, _basis, warnings = ceiling_from_quote(
            _quote_body(run_dir), overrides={"argo": 1}
        )
        assert any("BELOW" in warning for warning in warnings)
        assert any("halt partway" in warning for warning in warnings)

    def test_an_override_for_an_unpriced_gateway_is_accepted_with_a_warning(
        self, run_dir: Path
    ) -> None:
        """A transport change after approval is permitted, so this is legal."""
        ceiling, _basis, warnings = ceiling_from_quote(
            _quote_body(run_dir), overrides={"codex": 5_000_000}
        )
        assert ceiling["codex"] == 5_000_000
        assert any("does not price" in warning for warning in warnings)

    def test_a_non_positive_override_is_refused(self, run_dir: Path) -> None:
        with pytest.raises(ValueError, match="halt the run on its first call"):
            ceiling_from_quote(_quote_body(run_dir), overrides={"argo": 0})

    def test_a_quote_pricing_no_gateway_cannot_be_approved_blind(
        self, run_dir: Path
    ) -> None:
        quote = _quote_body(run_dir)
        quote["gateways"] = []
        with pytest.raises(ValueError, match="no ceiling to approve"):
            ceiling_from_quote(quote)


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


class TestApprove:
    def test_it_writes_approved_json_with_every_promised_field(
        self, run_dir: Path
    ) -> None:
        approval = approve(run_dir, approver="chenry", note="ADP1+PMI", now=NOW)
        path = run_dir / APPROVAL_FILENAME
        assert path.exists()
        written = json.loads(path.read_text(encoding="utf-8"))
        for key in (
            "approved_at",
            "expires_at",
            "approver",
            "plan_hash",
            "quote_hash",
            "ceiling_tokens",
            "note",
        ):
            assert key in written, f"APPROVED.json carries no {key}"
        assert written["approver"] == "chenry"
        assert written["note"] == "ADP1+PMI"
        assert written["approved_at"] == "2026-10-23T12:00:00Z"
        assert set(written["ceiling_tokens"]) == {"argo", "mag"}
        assert approval.source == str(path)

    def test_expires_at_is_the_default_window_after_approved_at(
        self, run_dir: Path
    ) -> None:
        approval = approve(run_dir, now=NOW)
        expected = NOW + timedelta(days=DEFAULT_EXPIRES_IN_DAYS)
        assert approval.expires_at == expected.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_expires_in_is_honoured(self, run_dir: Path) -> None:
        approval = approve(run_dir, expires_in_days=1, now=NOW)
        assert approval.expires_at == "2026-10-24T12:00:00Z"

    def test_an_approval_that_expires_immediately_is_refused(
        self, run_dir: Path
    ) -> None:
        with pytest.raises(ValueError, match="authorises nothing"):
            approve(run_dir, expires_in_days=0)

    def test_quote_hash_is_the_stores_canonical_hash_of_the_quote(
        self, run_dir: Path
    ) -> None:
        approval = approve(run_dir, now=NOW)
        body = _quote_body(run_dir)
        assert approval.quote_hash == canonical_sha256(body)

    def test_reformatting_the_quote_does_not_change_its_hash(
        self, run_dir: Path
    ) -> None:
        """Canonicalisation is the store's, so layout is not part of the quote."""
        before = quote_hash(run_dir)
        body = _quote_body(run_dir)
        (run_dir / QUOTE_FILENAME).write_text(
            json.dumps(body, indent=7, sort_keys=False) + "\n\n", encoding="utf-8"
        )
        assert quote_hash(run_dir) == before

    def test_plan_hash_is_read_from_the_quote_not_recomputed_here(
        self, run_dir: Path
    ) -> None:
        approval = approve(run_dir, now=NOW)
        assert approval.plan_hash == _quote_body(run_dir)["plan_hash"]
        assert approval.plan_hash == load_plan(run_dir).plan_hash

    def test_the_artifact_says_it_is_not_an_authentication_boundary(
        self, run_dir: Path
    ) -> None:
        approve(run_dir, now=NOW)
        written = json.loads(
            (run_dir / APPROVAL_FILENAME).read_text(encoding="utf-8")
        )
        statement = written["what_this_is"]
        assert "NOT AN AUTHENTICATION BOUNDARY" in statement
        assert "AUDIT TRAIL" in statement
        assert "BINDING CEILING" in statement

    def test_the_docstrings_say_so_too(self) -> None:
        """The honesty is in the API's own documentation, not only the file."""
        import kbutillib.domains.ai.council.approval as approval_module

        for text in (
            approval_module.__doc__,
            Approval.__doc__,
            validate_approval.__doc__,
        ):
            assert "NOT AN AUTHENTICATION BOUNDARY" in (text or "").upper()

    def test_no_write_computes_the_ceiling_without_approving_anything(
        self, run_dir: Path
    ) -> None:
        approval = approve(run_dir, now=NOW, write=False)
        assert approval.ceiling_tokens
        assert approval.source == ""
        assert not (run_dir / APPROVAL_FILENAME).exists()

    def test_the_approver_defaults_to_the_environment_label(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KBU_APPROVER", "miles")
        assert approve(run_dir, now=NOW).approver == "miles"

    def test_a_run_directory_with_no_quote_cannot_be_approved(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(FileNotFoundError, match="council-quote"):
            approve(tmp_path)

    def test_a_quote_with_no_plan_hash_cannot_be_approved(self, run_dir: Path) -> None:
        body = _quote_body(run_dir)
        body.pop("plan_hash")
        _write_quote_body(run_dir, body)
        with pytest.raises(ValueError, match="no plan_hash"):
            approve(run_dir)


# ---------------------------------------------------------------------------
# validate_approval -- the happy path
# ---------------------------------------------------------------------------


class TestValidateApprovalAccepts:
    def test_a_fresh_approval_over_an_unchanged_job_validates(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        approve(run_dir, approver="chenry", now=NOW)
        approval = validate_approval(run_dir, live=live, now=NOW)
        assert approval.approver == "chenry"
        assert approval.warnings == ()
        assert approval.ceiling_tokens["argo"] > ARGO_UPPER

    def test_every_check_name_is_declared(self) -> None:
        assert APPROVAL_CHECKS == (
            "approval_present",
            "quote_hash",
            "plan_hash",
            "bench_fingerprint",
            "expires_at",
            "consumed",
        )

    def test_a_transport_only_change_is_accepted_and_names_the_rerouted_seats(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        """Re-routing the same models is how token cost is allocated."""
        approve(run_dir, now=NOW)
        rerouted = [dict(row) for row in live["transport_allocation"]]
        rerouted[1].update(
            {
                "endpoint": "codex",
                "endpoint_kind": "codex_cli",
                "gateway": "codex",
                "route_fingerprint": "9" * 64,
            }
        )
        moved = {**live, "transport_allocation": rerouted}
        approval = validate_approval(run_dir, live=moved, now=NOW)
        assert approval.warnings, "a re-routed seat must be reported"
        warning = " ".join(approval.warnings)
        assert "proposer_2" in warning
        assert "proposer_1" not in warning
        assert "BENCH is unchanged" in warning
        assert "PRICE" in warning

    def test_a_declared_resume_may_reuse_the_approval_it_consumed(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        approval = approve(run_dir, now=NOW)
        mark_consumed(run_dir, approval, run_id="2026-10-23-adp1")
        resumed = validate_approval(run_dir, live=live, now=NOW, resume=True)
        assert any("resuming run" in warning for warning in resumed.warnings)

    def test_mark_consumed_writes_the_field_the_gate_reads(
        self, run_dir: Path
    ) -> None:
        approval = approve(run_dir, now=NOW)
        path = mark_consumed(run_dir, approval)
        record = json.loads(path.read_text(encoding="utf-8"))
        assert path.name == RUN_FILENAME
        assert record[CONSUMED_KEY] == approval.quote_hash

    def test_mark_consumed_merges_into_what_stage_four_already_wrote(
        self, run_dir: Path
    ) -> None:
        (run_dir / RUN_FILENAME).write_text(
            json.dumps({"job_ids": ["477d6c53"], "run_id": "r1"}), encoding="utf-8"
        )
        approval = approve(run_dir, now=NOW)
        record = json.loads(
            mark_consumed(run_dir, approval).read_text(encoding="utf-8")
        )
        assert record["job_ids"] == ["477d6c53"]
        assert record["run_id"] == "r1"
        assert record[CONSUMED_KEY] == approval.quote_hash


# ---------------------------------------------------------------------------
# validate_approval -- the six refusals, one test each
# ---------------------------------------------------------------------------


class TestValidateApprovalRefusals:
    """One test per check. Six checks, six tests, each naming its own.

    A single "it refuses a bad approval" test would pass with five of these
    six checks unimplemented, which is why they are not parametrised into one.
    """

    def test_check_1_a_missing_approval_is_refused(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=live, now=NOW)
        assert excinfo.value.check == "approval_present"
        assert "council-approve" in str(excinfo.value)

    def test_check_2_an_edited_quote_is_refused_on_quote_hash(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        approve(run_dir, now=NOW)
        body = _quote_body(run_dir)
        # The same plan, re-quoted cheaper: a different price than the one
        # approved, which is exactly what this check is for.
        body["gateways"][0]["total_tokens_upper"] = 1
        _write_quote_body(run_dir, body)
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=live, now=NOW)
        assert excinfo.value.check == "quote_hash"
        assert "changed since it was approved" in str(excinfo.value)

    def test_check_3_a_replanned_job_is_refused_on_plan_hash(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        approve(run_dir, now=NOW)
        # Stage 1 re-run: more items, and PLAN.json declares the new hash
        # honestly. The quote (and so the approval) still names the old one.
        body = _plan_dict(
            items=[
                {
                    "item_id": "t0",
                    "query_id": "a" * 64,
                    "evidence": {"text": "hydrolase", "source": "ADP1"},
                    "menu": [{"id": "GO:0016787", "label": "hydrolase activity"}],
                },
                {
                    "item_id": "t1",
                    "query_id": "c" * 64,
                    "evidence": {"text": "kinase", "source": "PMI"},
                    "menu": [{"id": "GO:0016301", "label": "kinase activity"}],
                },
            ]
        )
        replanned = _write_plan(run_dir, body)
        assert replanned != _quote_body(run_dir)["plan_hash"]
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=live, now=NOW)
        assert excinfo.value.check == "plan_hash"
        assert "re-derived since it was quoted" in str(excinfo.value)

    def test_check_4_a_model_swap_is_refused_on_bench_fingerprint(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        """A bench change is approving one experiment and running another."""
        approve(run_dir, now=NOW)
        swapped = [dict(row) for row in live["transport_allocation"]]
        swapped[0]["canonical_model"] = "claude-sonnet-5"
        moved = {
            **live,
            "bench_fingerprint": "f" * 64,
            "transport_allocation": swapped,
        }
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=moved, now=NOW)
        assert excinfo.value.check == "bench_fingerprint"
        message = str(excinfo.value)
        assert "proposer_1: gemini-3.5-flash -> claude-sonnet-5" in message
        assert "MODEL change" in message

    def test_check_4b_a_query_version_change_is_refused_on_the_same_check(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        """The bench check also covers the query the live config resolves."""
        approve(run_dir, now=NOW)
        moved = {**live, "query": "go_mf@2"}
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=moved, now=NOW)
        assert excinfo.value.check == "bench_fingerprint"
        assert "go_mf@2" in str(excinfo.value)

    def test_check_5_an_expired_approval_is_refused(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        approve(run_dir, expires_in_days=1, now=NOW)
        later = NOW + timedelta(days=2)
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=live, now=later)
        assert excinfo.value.check == "expires_at"
        assert "expired at" in str(excinfo.value)

    def test_check_6_an_approval_already_consumed_by_another_run_is_refused(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        """An approval is a ceiling for ONE run, and this is not a resume."""
        approval = approve(run_dir, now=NOW)
        mark_consumed(run_dir, approval, run_id="2026-10-23-adp1")
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=live, now=NOW, resume=False)
        assert excinfo.value.check == "consumed"
        message = str(excinfo.value)
        assert "already been consumed" in message
        assert "double the approved cost" in message


class TestValidateApprovalEdges:
    def test_an_unparseable_approval_is_refused_as_absent_not_as_a_traceback(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        (run_dir / APPROVAL_FILENAME).write_text("{not json", encoding="utf-8")
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=live, now=NOW)
        assert excinfo.value.check == "approval_present"

    def test_an_approval_with_no_ceiling_is_refused(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        approval = approve(run_dir, now=NOW)
        body = json.loads((run_dir / APPROVAL_FILENAME).read_text(encoding="utf-8"))
        body["ceiling_tokens"] = {}
        (run_dir / APPROVAL_FILENAME).write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=live, now=NOW)
        assert excinfo.value.check == "approval_present"
        assert "no ceiling" in str(excinfo.value)
        assert approval.ceiling_tokens  # the written one did have one

    def test_an_approval_whose_plan_hash_disagrees_with_the_quote_is_refused(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        approve(run_dir, now=NOW)
        body = json.loads((run_dir / APPROVAL_FILENAME).read_text(encoding="utf-8"))
        body["plan_hash"] = "0" * 64
        (run_dir / APPROVAL_FILENAME).write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=live, now=NOW)
        assert excinfo.value.check == "plan_hash"
        assert "do not agree about which plan" in str(excinfo.value)

    def test_a_run_record_naming_a_different_approval_is_refused(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        approve(run_dir, now=NOW)
        mark_consumed(run_dir, "d" * 64, run_id="some-other-run")
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=live, now=NOW, resume=True)
        assert excinfo.value.check == "consumed"
        assert "already committed to a different approved quote" in str(excinfo.value)

    def test_a_plan_with_no_bench_fingerprint_is_refused(
        self, run_dir: Path, live: Dict[str, Any]
    ) -> None:
        approve(run_dir, now=NOW)
        body = _plan_dict()
        body.pop("bench_fingerprint")
        _write_plan(run_dir, body)
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, live=live, now=NOW)
        assert excinfo.value.check == "bench_fingerprint"
        assert "Re-run stage 1" in str(excinfo.value)

    def test_an_unresolvable_live_bench_is_a_refusal_not_a_pass(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unanswerable staleness question must not read as "unchanged"."""
        import kbutillib.domains.ai.council.approval as approval_module

        approve(run_dir, now=NOW)

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("no profile configured")

        monkeypatch.setattr(approval_module, "live_bench", _boom)
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, now=NOW)
        assert excinfo.value.check == "bench_fingerprint"
        assert "no profile configured" in str(excinfo.value)


# ---------------------------------------------------------------------------
# live_bench: the comparison is against the BENCH the config resolves
# ---------------------------------------------------------------------------


def _seat(name: str, canonical: str, *, kind: str = "argo") -> SeatSpec:
    return SeatSpec(
        seat_name=name,
        endpoint="argo",
        endpoint_kind=kind,
        model=canonical,
        canonical_model=canonical,
    )


def _roster() -> Roster:
    return Roster(
        proposers=[
            _seat("proposer_1", "gemini-3.5-flash"),
            _seat("proposer_2", "gpt-5.6", kind="openai"),
        ],
        critic=_seat("critic", "claude-sonnet-5"),
        meta=_seat("meta", "claude-opus-5"),
    )


class TestLiveBench:
    def test_it_reports_the_benchs_own_fingerprint(self) -> None:
        roster = _roster()
        assert live_bench(roster=roster)["bench_fingerprint"] == bench_fingerprint(
            roster
        )

    def test_it_reports_the_roster_fingerprint_as_provenance_too(self) -> None:
        roster = _roster()
        block = live_bench(roster=roster)
        assert block["roster_fingerprint"] == roster.fingerprint()
        assert block["roster_fingerprint"] != block["bench_fingerprint"]

    def test_the_rows_carry_what_the_plans_transport_allocation_carries(self) -> None:
        rows = live_bench(roster=_roster())["transport_allocation"]
        assert {row["seat_name"] for row in rows} == {
            "proposer_1",
            "proposer_2",
            "critic",
            "meta",
        }
        for row in rows:
            assert set(row) >= {
                "role",
                "seat_name",
                "endpoint",
                "endpoint_kind",
                "model",
                "canonical_model",
            }

    def test_a_transport_change_does_not_move_the_bench_fingerprint(self) -> None:
        """The property the whole gate rests on, asserted on real rosters."""
        before = _roster()
        after = Roster(
            proposers=[
                _seat("proposer_1", "gemini-3.5-flash", kind="codex_cli"),
                _seat("proposer_2", "gpt-5.6", kind="openai"),
            ],
            critic=_seat("critic", "claude-sonnet-5"),
            meta=_seat("meta", "claude-opus-5"),
        )
        assert (
            live_bench(roster=after)["bench_fingerprint"]
            == live_bench(roster=before)["bench_fingerprint"]
        )
        assert (
            live_bench(roster=after)["roster_fingerprint"]
            != live_bench(roster=before)["roster_fingerprint"]
        )


class TestPublicSurface:
    """The names stage 4 imports are reachable from the package, lazily."""

    def test_the_gate_is_exported_from_the_council_package(self) -> None:
        import kbutillib.domains.ai.council as council

        assert council.validate_approval is validate_approval
        assert council.approve is approve
        assert council.ApprovalRefused is ApprovalRefused
        assert {"approve", "validate_approval", "Approval", "ApprovalRefused"} <= set(
            council.__all__
        )

    def test_the_default_window_is_the_prds_seven_days(self) -> None:
        assert DEFAULT_EXPIRES_IN_DAYS == 7
