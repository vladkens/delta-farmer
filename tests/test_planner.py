from decimal import Decimal
from typing import cast

import pytest

from strategy import Side, TradingClient
from strategy.cycle import SAFE_PCT, calc_total_from_pct
from strategy.trade import calc_symbol_sizes, plan_delta_trades


class Client:
    def __init__(self, name: str):
        self.name = name


@pytest.mark.parametrize(
    ("symbols", "expected"),
    [
        (["BTC"], {"BTC": ("100", "bid")}),
        (["BTC", "ETH"], {"BTC": ("50", "bid"), "ETH": ("50", "ask")}),
        (
            ["BTC", "ETH", "SOL"],
            {"BTC": ("50", "bid"), "ETH": ("25", "ask"), "SOL": ("25", "ask")},
        ),
        (
            ["BTC", "ETH", "SOL", "XRP"],
            {
                "BTC": ("25", "bid"),
                "ETH": ("25", "bid"),
                "SOL": ("25", "ask"),
                "XRP": ("25", "ask"),
            },
        ),
    ],
    ids=["1", "2", "3", "4"],
)
def test_symbol_sizes(symbols, expected):
    expected = {symbol: (Decimal(size), side) for symbol, (size, side) in expected.items()}
    assert calc_symbol_sizes(Decimal(100), symbols, "bid") == expected


def test_symbol_count():
    with pytest.raises(ValueError, match="at least one"):
        calc_symbol_sizes(Decimal(100), [], "bid")
    with pytest.raises(ValueError, match="up to 4"):
        calc_symbol_sizes(Decimal(100), ["A", "B", "C", "D", "E"], "bid")


async def test_plan_is_neutral(monkeypatch):
    accounts = [Client("prime"), Client("a"), Client("b")]
    monkeypatch.setattr(
        "strategy.trade.find_safe_pair",
        lambda *_: [("prime", Decimal(40)), ("a", Decimal(10)), ("b", Decimal(30))],
    )
    monkeypatch.setattr("strategy.trade.random.choice", lambda _: "bid")

    trades = await plan_delta_trades(
        cast(list[TradingClient], accounts),
        ["BTC", "ETH"],
        Decimal(100),
        10,
        [(account.name, 1000.0) for account in accounts],
    )

    assert trades is not None
    for trade in trades:
        totals: dict[Side, Decimal] = {"bid": Decimal(0), "ask": Decimal(0)}
        for leg in trade.legs:
            totals[leg.side] += leg.size_usd
        assert totals["bid"] == totals["ask"] == Decimal(20)

    for account in accounts:
        bids = sum(
            (
                leg.size_usd
                for trade in trades
                for leg in trade.legs
                if leg.client is account and leg.side == "bid"
            ),
            Decimal(0),
        )
        asks = sum(
            (
                leg.size_usd
                for trade in trades
                for leg in trade.legs
                if leg.client is account and leg.side == "ask"
            ),
            Decimal(0),
        )
        assert bids == asks


async def test_plan_without_pair(monkeypatch):
    monkeypatch.setattr("strategy.trade.find_safe_pair", lambda *_: None)
    accounts = cast(list[TradingClient], [Client("a"), Client("b")])

    assert await plan_delta_trades(accounts, ["BTC"], Decimal(100), 10, []) is None


async def test_plan_zero_pair(monkeypatch):
    monkeypatch.setattr(
        "strategy.trade.find_safe_pair",
        lambda *_: [("a", Decimal(0)), ("b", Decimal(0))],
    )
    accounts = cast(list[TradingClient], [Client("a"), Client("b")])

    assert await plan_delta_trades(accounts, ["BTC"], Decimal(100), 10, []) is None


@pytest.mark.parametrize(
    ("balances", "expected"),
    [
        ([1000.0, 500.0], 500 * 10 * SAFE_PCT / Decimal("0.5")),
        ([100.0, 900.0], 100 * 10 * SAFE_PCT / Decimal("0.5")),
        ([900.0, 100.0, 900.0], 100 * 10 * SAFE_PCT / Decimal("0.25")),
        ([300.0, 200.0, 200.0], 300 * 10 * SAFE_PCT / Decimal("0.5")),
    ],
    ids=["hedge", "prime", "split", "prime-cap"],
)
def test_trade_size_limit(balances, expected):
    named = [(str(index), balance) for index, balance in enumerate(balances)]
    assert calc_total_from_pct(named, leverage=10, pct=1.0) == expected


def test_trade_size_pct():
    balances = [("prime", 1000.0), ("hedge", 1000.0)]
    full = calc_total_from_pct(balances, leverage=10, pct=1.0)
    partial = calc_total_from_pct(balances, leverage=10, pct=0.1)

    assert partial == full / 10
