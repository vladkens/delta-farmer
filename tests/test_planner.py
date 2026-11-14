import random
from decimal import Decimal
from typing import cast

import pytest

from strategy import Side, TradingClient
from strategy.cycle import SAFE_PCT, calc_total_from_pct
from strategy.models import opposite_side
from strategy.trade import calc_symbol_sizes, plan_delta_trades


class Client:
    def __init__(self, name: str, exchange: str = "lighter"):
        self.name = name
        self.exchange = exchange


class LogSpy:
    """Collects what the planner logs."""

    def __init__(self):
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)


async def plan(accounts: list[Client], symbols: list[str], long_exchange: str | None = None):
    return await plan_delta_trades(
        cast(list[TradingClient], accounts),
        symbols,
        Decimal(100),
        10,
        [(account.name, 1000.0) for account in accounts],
        long_exchange,
    )


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


# MARK: Hedge direction


@pytest.mark.parametrize("draw", ["bid", "ask"], ids=["draw-bid", "draw-ask"])
@pytest.mark.parametrize("prime", ["rh", "core"], ids=["rh-prime", "core-prime"])
@pytest.mark.parametrize("long_exchange", ["lighter", "lighter_core"], ids=["rh-long", "core-long"])
async def test_long_exchange_sides_follow_the_venue(monkeypatch, long_exchange, prime, draw):
    accounts = [Client("rh", "lighter"), Client("core", "lighter_core")]
    order = [prime] + [acc.name for acc in accounts if acc.name != prime]
    monkeypatch.setattr(
        "strategy.trade.find_safe_pair", lambda *_: [(name, Decimal(50)) for name in order]
    )
    monkeypatch.setattr("strategy.trade.random.choice", lambda _: draw)

    trades = await plan(accounts, ["BTC"], long_exchange)

    assert trades is not None and len(trades) == 1
    short_exchange = "lighter_core" if long_exchange == "lighter" else "lighter"
    sides = {leg.client.exchange: leg.side for leg in trades[0].legs}
    assert sides == {long_exchange: "bid", short_exchange: "ask"}

    # neither the draw nor which account is prime may move the direction, and the basket stays
    # neutral in USD: the prime holds half and the one hedge the other half.
    bid = sum(leg.size_usd for leg in trades[0].legs if leg.side == "bid")
    ask = sum(leg.size_usd for leg in trades[0].legs if leg.side == "ask")
    assert bid == ask == Decimal(50)


async def test_long_exchange_logs_the_direction_once(monkeypatch):
    accounts = [Client("rh", "lighter"), Client("core", "lighter_core")]
    monkeypatch.setattr(
        "strategy.trade.find_safe_pair",
        lambda *_: [("rh", Decimal(50)), ("core", Decimal(50))],
    )
    spy = LogSpy()
    monkeypatch.setattr("strategy.trade.logger", spy)

    assert await plan(accounts, ["BTC"], "lighter") is not None
    assert spy.infos == ["Hedge direction: lighter long, lighter_core short"]

    spy.infos.clear()
    assert await plan(accounts, ["BTC"]) is not None
    assert spy.infos == []  # a pool without a required venue says nothing


@pytest.mark.parametrize("draw", ["bid", "ask"], ids=["draw-bid", "draw-ask"])
async def test_without_long_exchange_sides_follow_the_draw(monkeypatch, draw):
    # Three legs over two venues: keying the side on the exchange would group them differently.
    accounts = [Client("core", "lighter_core"), Client("rh", "lighter"), Client("rh2", "lighter")]
    monkeypatch.setattr(
        "strategy.trade.find_safe_pair",
        lambda *_: [("core", Decimal(50)), ("rh", Decimal(25)), ("rh2", Decimal(25))],
    )
    monkeypatch.setattr("strategy.trade.random.choice", lambda _: draw)

    trades = await plan(accounts, ["BTC"])

    assert trades is not None
    assert [leg.side for leg in trades[0].legs] == [draw, opposite_side(draw), opposite_side(draw)]


async def test_plan_without_long_exchange_matches_the_old_planner(monkeypatch):
    """Random baskets planned the old way: lead side from the draw, every hedge the opposite."""
    rng = random.Random(7)
    all_symbols = ["BTC", "ETH", "SOL", "XRP"]

    for _ in range(100):
        n = rng.randint(2, 4)
        accounts = [Client(f"a{i}", rng.choice(["lighter", "lighter_core"])) for i in range(n)]
        pairs = [
            (acc.name, Decimal(rng.randrange(10, 100)))
            for acc in rng.sample(accounts, len(accounts))
        ]
        draw: Side = rng.choice(["bid", "ask"])
        symbols = all_symbols[: rng.randint(1, 4)]

        monkeypatch.setattr("strategy.trade.find_safe_pair", lambda *_, p=pairs: p)
        monkeypatch.setattr("strategy.trade.random.choice", lambda _, d=draw: d)

        trades = await plan(accounts, symbols)

        assert trades is not None
        total = Decimal(sum(size for _, size in pairs))
        expected = calc_symbol_sizes(total, symbols, draw)
        assert [trade.symbol for trade in trades] == symbols
        for trade in trades:
            trade_size, trade_side = expected[trade.symbol]
            ratio = trade_size / total
            assert [(leg.client.name, leg.side, leg.size_usd) for leg in trade.legs] == [
                (name, trade_side if j == 0 else opposite_side(trade_side), size * ratio)
                for j, (name, size) in enumerate(pairs)
            ]


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
