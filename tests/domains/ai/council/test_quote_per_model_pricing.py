"""Per-canonical-model pricing inside one gateway, asserted on the PUBLISHED
quote (``Quote.to_dict``) and the quote CLI's output -- not only on
``_price_gateways`` -- because the tri-state defect this design avoids lived
in the gap between them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

# quote.py imports SQLAlchemy at module scope: the `council` extra, which CI's
# `.[dev]` install does not include.
pytest.importorskip("sqlalchemy", reason="quote.py needs the 'council' extra")

from kbutillib.domains.ai.council.config import ANTHROPIC_RATE_LIMIT_BUCKETS, load_council_config
from kbutillib.domains.ai.council.quote import (
    Quote,
    QuoteLine,
    _balance_delta_block,
    _price_gateways,
    _wallets_of,
)
from kbutillib.interfaces.cli.council import _render

OPUS4 = ANTHROPIC_RATE_LIMIT_BUCKETS["anthropic/claude-opus-4.8"]
HAIKU = ANTHROPIC_RATE_LIMIT_BUCKETS["anthropic/claude-haiku-4.5"]
SONNET5 = ANTHROPIC_RATE_LIMIT_BUCKETS["anthropic/claude-sonnet-5"]
M48, M47, M46 = "anthropic/claude-opus-4.8", "anthropic/claude-opus-4.7", "anthropic/claude-opus-4.6"


def rate(inp: float, out: float, *, prov: str = "vendor list page 2026-11-01",
         floor: bool = False, currency: str = "USD") -> Dict[str, Any]:
    return {"input_per_mtok": inp, "output_per_mtok": out, "provenance": prov,
            "is_floor": floor, "currency": currency}


def config(rates: Optional[Mapping[str, Any]] = None) -> Any:
    anth = {"kind": "anthropic", "billing_wallet": "anthropic-credit"}
    raw: Dict[str, Any] = {
        "endpoints": {
            "a-opus48": {**anth, "account": OPUS4},
            "a-haiku": {**anth, "account": HAIKU},
            "a-sonnet5": {**anth, "account": SONNET5},
            "mag": {"kind": "openai", "base_url": "https://mag.example/v1", "account": "mag-account"},
        },
        "models": {
            "opus48": {"endpoint": "a-opus48", "served_name": "claude-opus-4-8"},
            "haiku45": {"endpoint": "a-haiku", "served_name": "claude-haiku-4-5"},
            "sonnet5": {"endpoint": "a-sonnet5", "served_name": "claude-sonnet-5"},
        },
        "profiles": {},
    }
    if rates is not None:
        raw["rates"] = dict(rates)
    return load_council_config(raw)


def line(name: str, gateway: Optional[str], canonical: Optional[str], inp: float, out: float,
         out_upper: Optional[float] = None, endpoint: Optional[str] = None) -> QuoteLine:
    return QuoteLine(
        line=name, role="proposer", basis="exact", basis_reason="test",
        calls_total=1, calls_reused=0, calls_paid=1,
        input_tokens_point=inp, output_tokens_point=out,
        output_tokens_upper=out if out_upper is None else out_upper,
        gateway=gateway, endpoint=endpoint, canonical_model=canonical,
    )


def opus_lines() -> List[QuoteLine]:
    # Token counts chosen so per-model dollars have long fractional parts.
    return [
        line("p1", OPUS4, M48, 1_234_567, 76_543, 99_999, endpoint="a-opus48"),
        line("p2", OPUS4, M47, 2_345_671, 65_437, 88_888, endpoint="a-opus48"),
        line("p3", OPUS4, M46, 3_456_713, 54_371, 77_777, endpoint="a-opus48"),
    ]


def quote_from(gateways: Sequence[Mapping[str, Any]], lines: Sequence[QuoteLine], cfg: Any) -> Quote:
    return Quote(
        plan_hash="h", plan={}, parameter_set={}, provenance={}, lines=tuple(lines), reuse={},
        wall_clock={}, gateways=tuple(gateways),
        balance_delta=_balance_delta_block(gateways, _wallets_of(cfg, lines)),
        token_model={}, meta_fire_rate=0.1, meta_fire_rate_n=1, generated_at="now",
    )


def cli_text(quote: Quote, capsys: Any) -> str:
    _render({"verb": "quote", "data": quote.to_dict()})
    return capsys.readouterr().out


def gw(gateways: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any]:
    return next(g for g in gateways if g["gateway"] == name)


class TestPerModel:
    THREE = {f"{OPUS4}/{M48}": rate(15, 75), f"{OPUS4}/{M47}": rate(5, 25), f"{OPUS4}/{M46}": rate(3, 15)}

    def test_three_models_three_rates_sum_exactly(self) -> None:
        gateways, _ = _price_gateways(config(self.THREE), opus_lines())
        entry = gw(gateways, OPUS4)
        assert entry["priced"] is True
        by = {m["canonical_model"]: m for m in entry["by_model"]}
        assert by[M48]["rate"] == {"input_per_mtok": 15.0, "output_per_mtok": 75.0}
        assert by[M47]["rate_key"] == f"{OPUS4}/{M47}"
        expected48 = 1_234_567 * 15 / 1e6 + 76_543 * 75 / 1e6
        assert by[M48]["dollars_point"] == pytest.approx(expected48)
        # summed UNROUNDED, rounded once: the published figures agree exactly
        assert entry["dollars_point"] == round(sum(m["dollars_point"] for m in entry["by_model"]), 2)
        assert entry["dollars_upper"] == round(sum(m["dollars_upper"] for m in entry["by_model"]), 2)

    def test_multi_rate_gateway_publishes_null_rate_and_provenance(self) -> None:
        gateways, _ = _price_gateways(config(self.THREE), opus_lines())
        entry = gw(gateways, OPUS4)
        assert entry["rate"] is None
        assert entry["rate_provenance"] is None
        assert all(m["rate_provenance"] for m in entry["by_model"])

    def test_composite_preferred_over_gateway_fallback(self) -> None:
        rates = {OPUS4: rate(1, 1), f"{OPUS4}/{M48}": rate(15, 75)}
        gateways, _ = _price_gateways(config(rates), opus_lines())
        by = {m["canonical_model"]: m for m in gw(gateways, OPUS4)["by_model"]}
        assert by[M48]["rate_key"] == f"{OPUS4}/{M48}"
        assert by[M47]["rate_key"] == OPUS4
        assert by[M46]["rate_key"] == OPUS4

    def test_gateway_rate_alone_prices_every_model(self) -> None:
        gateways, _ = _price_gateways(config({OPUS4: rate(15, 75)}), opus_lines())
        entry = gw(gateways, OPUS4)
        assert entry["priced"] is True
        assert entry["rate"] == {"input_per_mtok": 15.0, "output_per_mtok": 75.0}
        assert entry["rate_provenance"] == "vendor list page 2026-11-01"
        assert all(m["priced"] for m in entry["by_model"])
        total_in = sum(l.input_tokens_point for l in opus_lines())
        total_out = sum(l.output_tokens_point for l in opus_lines())
        assert entry["dollars_point"] == round(total_in * 15 / 1e6 + total_out * 75 / 1e6, 2)

    def test_line_without_gateway_is_skipped(self) -> None:
        lines = opus_lines() + [line("x", None, M48, 10**9, 10**9)]
        gateways, _ = _price_gateways(config({OPUS4: rate(15, 75)}), lines)
        assert [g["gateway"] for g in gateways] == [OPUS4]
        assert gw(gateways, OPUS4)["input_tokens"] == round(sum(l.input_tokens_point for l in opus_lines()))


class TestPartial:
    RATES = {f"{OPUS4}/{M48}": rate(15, 75), f"{OPUS4}/{M47}": rate(5, 25)}

    def test_partial_gateway_shape(self) -> None:
        gateways, warnings = _price_gateways(config(self.RATES), opus_lines())
        entry = gw(gateways, OPUS4)
        assert entry["priced"] is False  # STRICT boolean
        assert entry["partially_priced"] is True
        assert entry["unpriced_models"] == [M46]
        priced = [m for m in entry["by_model"] if m["priced"]]
        assert entry["dollars_known_subtotal"] == round(sum(m["dollars_point"] for m in priced), 2)
        assert entry["dollars_point"] is None
        assert "only part" in entry["unpriced_reason"]
        assert any("partially priced" in w for w in warnings)

    def test_published_quote_lists_partial_as_unpriced(self) -> None:
        cfg = config(self.RATES)
        lines = opus_lines()
        gateways, _ = _price_gateways(cfg, lines)
        headline = quote_from(gateways, lines, cfg).to_dict()["headline"]
        assert OPUS4 in headline["unpriced_gateways"]
        assert headline["dollars_complete"] is False
        assert headline["dollars_point"] is None  # partial never reaches the total
        (partial,) = headline["partially_priced_gateways"]
        assert partial["gateway"] == OPUS4
        assert partial["unpriced_models"] == [M46]
        assert partial["dollars_known_subtotal"] == gw(gateways, OPUS4)["dollars_known_subtotal"]

    def test_cli_names_the_unpriced_canonical_model(self, capsys: Any) -> None:
        cfg = config(self.RATES)
        lines = opus_lines()
        gateways, _ = _price_gateways(cfg, lines)
        out = cli_text(quote_from(gateways, lines, cfg), capsys)
        assert "PARTIALLY priced" in out
        assert M46 in out

    def test_no_rate_at_all_is_unchanged_from_base(self, capsys: Any) -> None:
        cfg = config()
        lines = opus_lines()
        gateways, warnings = _price_gateways(cfg, lines)
        entry = dict(gw(gateways, OPUS4))
        entry.pop("by_model")  # the one additive key
        total_in = round(sum(l.input_tokens_point for l in lines))
        out_p = round(sum(l.output_tokens_point for l in lines))
        out_u = round(sum(l.output_tokens_upper for l in lines))
        assert entry == {
            "gateway": OPUS4,
            "input_tokens": total_in,
            "output_tokens_point": out_p,
            "output_tokens_upper": out_u,
            "total_tokens_point": total_in + out_p,
            "total_tokens_upper": total_in + out_u,
            "priced": False,
            "dollars_point": None,
            "dollars_upper": None,
            "currency": None,
            "rate": None,
            "is_floor": None,
            "rate_provenance": None,
            "unpriced_reason": (
                f"no rate configured for gateway {OPUS4!r} under "
                "`ai_council.rates`. A vendor list rate is NOT "
                "substituted: it would be a floor, not a cost."
            ),
        }
        assert warnings == [
            f"gateway {OPUS4!r} is unpriced: {entry['total_tokens_point']:,} tokens carry no dollar figure"
        ]
        assert f"{OPUS4}: unpriced" in cli_text(quote_from(gateways, lines, cfg), capsys)


class TestRefusalsAndFloors:
    def test_mixed_currency_refused(self) -> None:
        rates = {f"{OPUS4}/{M48}": rate(15, 75), f"{OPUS4}/{M47}": rate(5, 25, currency="EUR"),
                 OPUS4: rate(3, 15)}
        with pytest.raises(ValueError, match="currency"):
            _price_gateways(config(rates), opus_lines())

    def test_floor_from_any_single_contributor(self) -> None:
        rates = {f"{OPUS4}/{M48}": rate(15, 75), f"{OPUS4}/{M47}": rate(5, 25, prov="list page", floor=True),
                 OPUS4: rate(3, 15)}
        gateways, warnings = _price_gateways(config(rates), opus_lines())
        assert gw(gateways, OPUS4)["is_floor"] is True
        # the pre-existing warning text still fires ...
        assert any("marks as a FLOOR (list page)" in w for w in warnings)
        # ... and a new one names which model it prices
        assert any(f"{OPUS4}/{M47}" in w and M47 in w and "FLOOR" in w for w in warnings)


class TestBalanceDeltaByWallet:
    def test_three_buckets_one_wallet_one_row(self) -> None:
        cfg = config()
        lines = [
            line("p1", OPUS4, M48, 100, 10, endpoint="a-opus48"),
            line("p2", HAIKU, "anthropic/claude-haiku-4.5", 200, 20, endpoint="a-haiku"),
            line("p3", SONNET5, "anthropic/claude-sonnet-5", 300, 30, endpoint="a-sonnet5"),
            line("p4", "mag-account", "openai/gpt", 50, 5, endpoint="mag"),
        ]
        gateways, _ = _price_gateways(cfg, lines)
        block = quote_from(gateways, lines, cfg).to_dict()["balance_delta"]
        rows = {row["billing_wallet"]: row for row in block["gateways"]}
        assert set(rows) == {"anthropic-credit", "mag-account"}
        wallet = rows["anthropic-credit"]
        assert wallet["gateways"] == sorted([OPUS4, HAIKU, SONNET5])
        assert wallet["quoted_total_tokens_point"] == 660
        assert wallet["implied_input_per_mtok"] is None
        assert "do NOT derive" in wallet["note"]
        # a non-anthropic gateway's row keeps its old key
        assert rows["mag-account"]["gateway"] == "mag-account"
        assert rows["mag-account"]["gateways"] == ["mag-account"]
