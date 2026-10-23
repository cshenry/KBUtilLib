"""Tests for the fitted token model a council quote is priced from.

Nothing here makes a network call, spends a token, needs a CLI login or needs
a GPU -- and one test (:class:`TestNoSocket`) exists to keep it that way for
the quote stage that builds on this module.
"""

from __future__ import annotations

import ast
import importlib.util
import statistics
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pytest
import sqlalchemy as sa

from kbutillib.domains.ai.council.quote import (
    DEFAULT_UPPER_PERCENTILE,
    FALLBACK_PROMPT_CHARS,
    PROJECTED_UPPER_FACTOR,
    ROLE_FALLBACKS,
    SEAT_FALLBACKS,
    fit_token_model,
    item_prompt_chars,
)
from kbutillib.domains.ai.council.store import schema as sch
from kbutillib.domains.ai.council.types import canonical_json

from .store_contract import call_row, item_row, job_row, utcnow

#: The ratio each seeded seat's input tokens are generated at, in tokens per
#: prompt character.  Distinct per seat so a fit that mixed two seats up
#: would land between them and fail.
SEEDED_RATIO = {"proposer_1": 0.32, "proposer_2": 0.25, "critic": 0.20}

#: The completion tokens seeded for ``proposer_1``, in the order they are
#: written.  Twenty values, right-skewed: the mean is 104.0 and the p95 by
#: nearest rank (``ceil(0.95*20) - 1 = 18``) is 900 -- which is the point of
#: quoting an order statistic rather than a normal band around the mean.
SEEDED_OUTPUTS = [10] * 18 + [900, 1000]


def _menu(n_entries: int) -> List[Dict[str, Any]]:
    """A candidate menu of *n_entries*, so prompt characters VARY per item.

    A fit over items that all had the same prompt size would be satisfied by
    any ratio that reproduced one total, which is not a fit at all.
    """
    return [
        {
            "id": f"GO:{i:07d}",
            "label": f"candidate {i}",
            "description": "a definition " * (i + 1),
            "rank": i + 1,
            "score": 1.0 / (i + 1),
        }
        for i in range(n_entries)
    ]


def _seed(
    store: Any,
    *,
    n_items: int = 20,
    seats: Iterable[Tuple[str, str]] = (
        ("proposer_1", "proposer"),
        ("proposer_2", "proposer"),
        ("critic", "critic"),
    ),
) -> Dict[str, int]:
    """Seed one job whose attempts carry EXACTLY :data:`SEEDED_RATIO`.

    Returns ``{item_id: prompt_chars}`` so a test can compute the totals the
    fit should reproduce without re-deriving them from the store.
    """
    items = [
        item_row(
            f"item-{i}",
            evidence={"text": f"protein {i}", "source": "test"},
            menu=_menu(i % 5 + 1),
        )
        for i in range(n_items)
    ]
    chars = {
        str(item["item_id"]): item_prompt_chars(item["evidence"], item["menu"])
        for item in items
    }
    calls = [
        call_row(str(item["item_id"]), seat_name=seat, role=role)
        for item in items
        for seat, role in seats
    ]
    job_id = store.create_job(job_row(), items, calls)

    now = utcnow()
    for call in store.get_calls(job_id):
        seat = str(call["seat_name"])
        prompt_tokens = round(SEEDED_RATIO[seat] * chars[str(call["item_id"])])
        index = int(str(call["item_id"]).rsplit("-", 1)[1])
        completion = (
            SEEDED_OUTPUTS[index % len(SEEDED_OUTPUTS)] if seat == "proposer_1" else 7
        )
        store.record_attempt(
            call["call_id"],
            1,
            now,
            now,
            "ok",
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion},
        )
    return chars


def _update_call(store: Any, call_id: str, **values: Any) -> None:
    """Set columns on one call row directly.

    The store's own ``complete_call`` needs a claim token, and these tests are
    about what the FIT reads out of a settled row rather than about how the
    row got that way.
    """
    with store.transaction() as conn:
        conn.execute(
            sa.update(sch.calls).where(sch.calls.c.call_id == call_id).values(**values)
        )


class TestItemPromptChars:
    """The regressor: the same measure at fit time and at quote time."""

    def test_it_is_the_canonical_json_length_of_evidence_plus_menu(self) -> None:
        evidence = {"text": "alpha synthase", "source": "test"}
        menu = _menu(3)
        assert item_prompt_chars(evidence, menu) == len(canonical_json(evidence)) + len(
            canonical_json(menu)
        )

    def test_it_grows_with_the_menu_and_needs_no_store(self) -> None:
        evidence = {"text": "alpha synthase", "source": "test"}
        assert item_prompt_chars(evidence, _menu(1)) < item_prompt_chars(
            evidence, _menu(8)
        )

    def test_a_missing_evidence_or_menu_is_the_empty_one(self) -> None:
        # "{}" plus "[]": a quote for an item with no menu must still be a
        # number, not a crash on None.
        assert item_prompt_chars(None, None) == 4


class TestMeasuredFit:
    """A fit over a seeded store reproduces the seeded ratio."""

    def test_the_fit_reproduces_the_seeded_ratio_and_sample_size(
        self, store: Any
    ) -> None:
        chars = _seed(store)
        model = fit_token_model(
            store,
            seats=[
                ("proposer", "proposer_1"),
                ("proposer", "proposer_2"),
                ("critic", "critic"),
            ],
        )

        for seat, role in (
            ("proposer_1", "proposer"),
            ("proposer_2", "proposer"),
            ("critic", "critic"),
        ):
            fit = model.fit_for(seat, role)
            assert fit.basis == "measured"
            assert fit.fallback is None
            assert fit.n_attempts == len(chars)
            # Rounding to whole tokens is the only error in the seed.
            assert fit.input_tokens_per_char == pytest.approx(
                SEEDED_RATIO[seat], rel=1e-3
            )
            assert fit.prompt_chars_mean == pytest.approx(
                sum(chars.values()) / len(chars)
            )

    def test_the_output_band_is_a_high_percentile_not_a_normal_band(
        self, store: Any
    ) -> None:
        _seed(store)
        fit = fit_token_model(
            store, seats=[("proposer", "proposer_1")]
        ).fit_for("proposer_1", "proposer")

        assert fit.output_tokens_mean == pytest.approx(
            sum(SEEDED_OUTPUTS) / len(SEEDED_OUTPUTS)
        )
        assert fit.output_tokens_upper == 900.0
        # The tail the band exists for.  A mean + 2sd band on this sample is
        # 669, BELOW the p95 of 900: on a right-skewed sample the normal band
        # understates exactly the tail it is supposed to cover, which is why
        # the band here is an order statistic.
        normal_band = fit.output_tokens_mean + 2 * statistics.pstdev(SEEDED_OUTPUTS)
        assert normal_band == pytest.approx(668.8, abs=0.1)
        assert normal_band < fit.output_tokens_upper

    def test_estimate_scales_the_fit_by_the_item_size(self, store: Any) -> None:
        _seed(store)
        fit = fit_token_model(store, seats=[("critic", "critic")]).fit_for(
            "critic", "critic"
        )
        estimate = fit.estimate(10_000)
        assert estimate["input_tokens"] == pytest.approx(
            10_000 * SEEDED_RATIO["critic"], rel=1e-3
        )
        assert estimate["output_tokens"] == fit.output_tokens_mean

    def test_a_requested_percentile_moves_only_the_band(self, store: Any) -> None:
        _seed(store)
        fit = fit_token_model(
            store, seats=[("proposer", "proposer_1")], upper_percentile=50.0
        ).fit_for("proposer_1", "proposer")
        assert fit.output_tokens_upper == 10.0
        assert fit.input_tokens_per_char == pytest.approx(
            SEEDED_RATIO["proposer_1"], rel=1e-3
        )

    def test_the_published_shape_always_carries_the_sample_size(
        self, store: Any
    ) -> None:
        _seed(store)
        data = fit_token_model(
            store, seats=[("proposer", "proposer_1"), ("meta", "meta")]
        ).to_dict()
        assert data["upper_percentile"] == DEFAULT_UPPER_PERCENTILE
        assert data["n_measured"] == 1 and data["n_projected"] == 1
        assert [seat["n_attempts"] for seat in data["seats"]] == [20, 0]
        assert {"basis", "n_attempts", "input_tokens_per_char"} <= set(
            data["seats"][0]
        )


class TestWhatIsNotFittable:
    """Rows a ratio must not be fitted on, and why each would poison it."""

    def test_only_successful_charged_unbatched_attempts_count(
        self, store: Any
    ) -> None:
        items = [item_row("item-1", menu=_menu(3))]
        chars = item_prompt_chars(items[0]["evidence"], items[0]["menu"])
        calls = [call_row("item-1", seat_name="proposer_1", role="proposer")]
        job_id = store.create_job(job_row(), items, calls)
        call_id = store.get_calls(job_id)[0]["call_id"]
        now = utcnow()

        # The one fittable row.
        store.record_attempt(
            call_id,
            1,
            now,
            now,
            "ok",
            usage={"prompt_tokens": round(0.32 * chars), "completion_tokens": 50},
        )
        # A transport failure: the gateway may have charged for it, but it
        # answered nothing, so it is not evidence about a successful call.
        store.record_attempt(
            call_id, 2, now, now, "transport", usage={"prompt_tokens": 99_999}
        )
        # An UNCHARGED attempt, filed below zero by a stopped daemon.
        store.record_attempt(
            call_id, -1, now, now, "ok", usage={"prompt_tokens": 99_999}
        )
        # One member of a BATCHED invocation: the anchor's counts cover every
        # member's prompt, so charging them to one item's characters would
        # overstate the ratio by the batch size.
        store.record_attempt(
            call_id,
            3,
            now,
            now,
            "ok",
            usage={
                "prompt_tokens": 8 * round(0.32 * chars),
                "completion_tokens": 400,
                "batch_size": 8,
                "invocation_id": "i1",
            },
        )

        fit = fit_token_model(store, seats=[("proposer", "proposer_1")]).fit_for(
            "proposer_1", "proposer"
        )
        assert fit.n_attempts == 1
        # Exactly the one fittable row: whole tokens over that item's chars.
        assert fit.input_tokens_per_char == round(0.32 * chars) / chars

    def test_fresh_usage_covers_an_attempt_row_with_no_counts(
        self, store: Any
    ) -> None:
        items = [item_row("item-1", menu=_menu(3))]
        chars = item_prompt_chars(items[0]["evidence"], items[0]["menu"])
        calls = [call_row("item-1", seat_name="critic", role="critic")]
        job_id = store.create_job(job_row(), items, calls)
        call_id = store.get_calls(job_id)[0]["call_id"]
        now = utcnow()
        store.record_attempt(call_id, 1, now, now, "ok", usage=None)
        _update_call(
            store,
            call_id,
            status="ok",
            fresh_usage={
                "prompt_tokens": round(0.2 * chars),
                "completion_tokens": 60,
            },
        )

        fit = fit_token_model(store, seats=[("critic", "critic")]).fit_for(
            "critic", "critic"
        )
        assert fit.n_attempts == 1
        assert fit.input_tokens_per_char == round(0.2 * chars) / chars
        assert fit.output_tokens_mean == 60.0

    def test_a_reused_call_spent_nothing_and_is_not_a_zero_sample(
        self, store: Any
    ) -> None:
        items = [item_row("item-1", menu=_menu(3))]
        calls = [call_row("item-1", seat_name="meta", role="meta")]
        job_id = store.create_job(job_row(), items, calls)
        call_id = store.get_calls(job_id)[0]["call_id"]
        now = utcnow()
        store.record_attempt(
            call_id,
            1,
            now,
            now,
            "ok",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )
        _update_call(
            store,
            call_id,
            status="ok",
            reused_from="f" * 32,
            fresh_usage={"prompt_tokens": 0, "completion_tokens": 0},
        )

        fit = fit_token_model(store, seats=[("meta", "meta")]).fit_for("meta", "meta")
        assert fit.basis == "projected"
        assert fit.input_tokens_per_char > 0


class TestProjectedSeats:
    """A seat with no history is reported as such, against a NAMED fallback."""

    def test_a_seat_with_zero_attempts_is_projected_from_its_own_figures(
        self, store: Any
    ) -> None:
        fit = fit_token_model(store, seats=[("meta", "meta")]).fit_for("meta", "meta")
        assert fit.basis == "projected"
        assert fit.n_attempts == 0
        assert fit.input_tokens_per_char == pytest.approx(
            SEAT_FALLBACKS["meta"].input_tokens / FALLBACK_PROMPT_CHARS
        )
        assert fit.output_tokens_mean == float(SEAT_FALLBACKS["meta"].output_tokens)
        assert fit.output_tokens_upper == pytest.approx(
            SEAT_FALLBACKS["meta"].output_tokens * PROJECTED_UPPER_FACTOR
        )
        assert "meta (ARGO claudeopus5)" in str(fit.fallback)

    def test_it_is_never_zero_and_never_another_seats_figures(
        self, store: Any
    ) -> None:
        model = fit_token_model(
            store,
            seats=[
                ("proposer", "proposer_1"),
                ("proposer", "proposer_2"),
                ("critic", "critic"),
                ("meta", "meta"),
            ],
        )
        for fit in model:
            assert fit.basis == "projected"
            assert fit.input_tokens_per_char > 0
            assert fit.output_tokens_mean > 0
            assert fit.seat_name in str(fit.fallback)
        # Four seats, four DIFFERENT output figures: a fallback that had
        # silently borrowed one seat's numbers for another would collapse
        # these onto each other.
        assert len({fit.output_tokens_mean for fit in model.fits}) == 4

    def test_a_seat_name_with_no_entry_falls_back_by_role_and_says_so(
        self, store: Any
    ) -> None:
        fit = fit_token_model(store, seats=[("proposer", "gemini_on_vertex")]).fit_for(
            "gemini_on_vertex", "proposer"
        )
        assert fit.basis == "projected"
        assert fit.seat_name == "gemini_on_vertex"
        assert "role default" in str(fit.fallback)
        assert fit.output_tokens_mean == float(ROLE_FALLBACKS["proposer"].output_tokens)

    def test_a_measured_seat_and_a_projected_seat_are_quoted_in_one_unit(
        self, store: Any
    ) -> None:
        # The projected ratio is per CHARACTER like the measured one, so a
        # quote can multiply both by the same corpus size.  Sanity: the
        # fallback is the same order of magnitude as the fit it stands in for.
        _seed(store)
        model = fit_token_model(
            store, seats=[("proposer", "proposer_1"), ("meta", "meta")]
        )
        measured = model.fit_for("proposer_1", "proposer")
        projected = model.fit_for("meta", "meta")
        assert 0.1 < projected.input_tokens_per_char < 1.0
        assert 0.1 < measured.input_tokens_per_char < 1.0


class TestSeatArgument:
    """What ``seats=`` accepts, and what it refuses rather than guesses."""

    def test_a_roster_is_fitted_in_protocol_order(self, store: Any) -> None:
        from kbutillib.domains.ai.council.types import Roster, SeatSpec

        def seat(name: str, canonical: str) -> SeatSpec:
            return SeatSpec(
                seat_name=name,
                endpoint="argo",
                endpoint_kind="argo",
                model=canonical.split("/")[-1],
                canonical_model=canonical,
            )

        roster = Roster(
            proposers=[seat("proposer_1", "g/flash"), seat("proposer_2", "o/gpt")],
            critic=seat("critic", "a/sonnet"),
            meta=seat("meta", "a/opus"),
        )
        model = fit_token_model(store, seats=roster)
        assert [(fit.seat_name, fit.role) for fit in model] == [
            ("proposer_1", "proposer"),
            ("proposer_2", "proposer"),
            ("critic", "critic"),
            ("meta", "meta"),
        ]

    def test_a_bare_seat_without_its_role_is_refused(self, store: Any) -> None:
        with pytest.raises(TypeError, match="iterable of \\(role, seat\\) pairs"):
            fit_token_model(store, seats=["proposer_1"])

    def test_an_unknown_role_is_refused_rather_than_fitted(self, store: Any) -> None:
        with pytest.raises(ValueError, match="unknown council role"):
            fit_token_model(store, seats=[("referee", "proposer_1")])

    def test_a_store_with_no_engine_says_so(self) -> None:
        with pytest.raises(TypeError, match=r"\.engine"):
            fit_token_model(object(), seats=[("meta", "meta")])

    def test_asking_for_a_pair_that_was_not_fitted_raises(self, store: Any) -> None:
        model = fit_token_model(store, seats=[("meta", "meta")])
        with pytest.raises(KeyError):
            model.fit_for("critic", "critic")


# ---------------------------------------------------------------------------
# The property the quote stage depends on
# ---------------------------------------------------------------------------

#: Third-party clients that would mean this module could reach a network.
NETWORK_CLIENTS = {
    "httpx",
    "requests",
    "aiohttp",
    "urllib3",
    "urllib.request",
    "http.client",
    "openai",
    "anthropic",
    "websockets",
    "boto3",
}

#: KBUtilLib modules that construct or drive a model call.  Reaching any of
#: them would put a backend in the quote's import graph even if the client
#: library itself were lazily imported inside a function.
NETWORKED_KBUTILLIB = (
    "domains.ai.llm.apis",
    "domains.ai.council.backends",
    "domains.ai.council.engine",
    "domains.ai.council.service_client",
    "domains.ai.council.daemon",
    "domains.ai.argo_utils",
)


def _module_path(name: str) -> Optional[Path]:
    """The source file of *name*, or ``None`` if it is not a Python source."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):  # pragma: no cover - not importable
        return None
    origin = getattr(spec, "origin", None) if spec is not None else None
    return Path(origin) if origin and origin.endswith(".py") else None


def _module_level_imports(path: Path, package: str) -> Set[str]:
    """Absolute module names *path* imports WHEN IT IS IMPORTED.

    Module and class level only, and ``if TYPE_CHECKING:`` blocks are pruned:
    neither a function-body import nor a typing-only one runs at import time,
    and the claim under test is about what importing this module pulls in.
    """
    found: Set[str] = set()

    def walk(body: Iterable[ast.stmt], pkg: str) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = pkg.split(".")
                    base = ".".join(parts[: len(parts) - node.level + 1])
                    module = f"{base}.{node.module}" if node.module else base
                else:
                    module = node.module or ""
                found.add(module)
                found.update(f"{module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.ClassDef):
                walk(node.body, pkg)
            elif isinstance(node, ast.If):
                test = node.test
                name = getattr(test, "id", None) or getattr(test, "attr", None)
                if name == "TYPE_CHECKING":
                    continue
                walk(node.body, pkg)
                walk(node.orelse, pkg)
            elif isinstance(node, ast.Try):
                walk(node.body, pkg)
                walk(node.orelse, pkg)
                walk(node.finalbody, pkg)
                for handler in node.handlers:
                    walk(handler.body, pkg)

    walk(ast.parse(path.read_text(encoding="utf-8")).body, package)
    return found


def _import_closure(root: str) -> Set[str]:
    """Every module *root*'s module-level imports reach, transitively.

    Only KBUtilLib modules are followed -- a third-party package's own import
    graph is not this repository's to police -- but every name reached is
    returned, so a third-party client appearing anywhere in the closure is
    visible to the caller.
    """
    seen: Set[str] = set()
    queue = [root]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _module_path(name)
        if path is None or not name.startswith("kbutillib"):
            continue
        package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
        for imported in _module_level_imports(path, package):
            if imported not in seen:
                queue.append(imported)
    return seen


class TestNoSocket:
    """``quote.py`` must not be able to open a socket.

    "The quote spends nothing" is asserted by the quote stage that builds on
    this module, and this is the structural property that holds it up.  It is
    checked statically rather than by inspecting ``sys.modules``, because
    importing ``kbutillib`` at all pulls in ``httpx`` today -- so a
    ``sys.modules`` assertion would be green no matter what this module did.
    """

    def test_the_import_graph_reaches_no_network_client(self) -> None:
        closure = _import_closure("kbutillib.domains.ai.council.quote")
        assert "kbutillib.domains.ai.council.store.schema" in closure
        offenders = sorted(
            name
            for name in closure
            if name in NETWORK_CLIENTS or name.split(".")[0] in NETWORK_CLIENTS
        )
        assert offenders == []

    def test_the_import_graph_reaches_no_backend_or_engine(self) -> None:
        closure = _import_closure("kbutillib.domains.ai.council.quote")
        offenders = sorted(
            name
            for name in closure
            if any(fragment in name for fragment in NETWORKED_KBUTILLIB)
        )
        assert offenders == []
