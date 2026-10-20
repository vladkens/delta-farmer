"""Entering and exiting positions in equal parts, run against a simulated exchange."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock

import pytest

from lib.http import ApiError
from strategy.cycle import DeltaStrategy
from strategy.models import (
    Order,
    OrderBook,
    OrderStatus,
    Position,
    Side,
    StrategyConfig,
    TradingClient,
)
from strategy.trade import DeltaLeg, DeltaTrade, split_qty

PRICE = Decimal(100)


class SimClient:
    """One exchange account: tracks positions and rejects orders a real exchange would."""

    exchange = "sim"

    def __init__(self, name: str, net: dict[str, Decimal], *, lot: str = "0.01"):
        self.name = name
        self.net = net  # shared across accounts: symbol -> signed exposure of the whole basket
        self.lot = Decimal(lot)
        self.pos: dict[str, Decimal] = {}  # symbol -> signed qty
        self.orders: list[tuple[str, Side, Decimal, bool]] = []
        self.min_trade_usd = Decimal(1)
        self.market_open = True
        self.attempts = 0
        self.fail: Callable[[int], bool] = lambda n: False  # fail the n-th order attempt
        self.leverage_set: list[int] = []

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        self.attempts += 1
        n = self.attempts
        if not self.market_open:
            raise ApiError("Market is not open for trading")
        if self.fail(n):
            raise ApiError(f"order {n} rejected")
        if qty <= 0 or qty % self.lot:
            raise ApiError(f"qty {qty} is not a positive multiple of lot {self.lot}")

        held = self.pos.get(symbol, Decimal(0))
        signed = qty if side == "bid" else -qty
        if reduce_only and (held == 0 or (held > 0) == (signed > 0) or qty > abs(held)):
            raise ApiError(f"reduce-only {side} {qty} exceeds position {held}")

        self.orders.append((symbol, side, qty, reduce_only))
        self.pos[symbol] = held + signed
        self.net[symbol] = self.net.get(symbol, Decimal(0)) + signed
        return Order(
            id=str(n), symbol=symbol, side=side, size=qty, filled=qty, price=PRICE,
            status=OrderStatus.FILLED, reduce_only=reduce_only,
        )  # fmt: skip

    async def positions(self) -> list[Position]:
        return [
            Position(
                id=symbol,
                symbol=symbol,
                side="bid" if qty > 0 else "ask",
                size=abs(qty),
                entry_price=PRICE,
            )
            for symbol, qty in self.pos.items()
            if qty
        ]

    async def close_position(self, value: Position) -> bool:
        side: Side = "ask" if value.side == "bid" else "bid"
        await self.market_order(value.symbol, side, value.size, reduce_only=True)
        return True

    async def close_all_positions(self) -> int:
        positions = await self.positions()
        for value in positions:
            await self.close_position(value)
        return len(positions)

    async def cancel_all_orders(self) -> int:
        return 0

    async def balance(self) -> Decimal:
        return Decimal(1000)

    async def get_price(self, symbol: str) -> Decimal:
        return PRICE

    async def get_order_book(self, symbol: str) -> OrderBook:
        return OrderBook.build(bids=[(PRICE - 1, 1000)], asks=[(PRICE + 1, 1000)])

    async def get_lot_size(self, symbol: str) -> Decimal:
        return self.lot

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return self.min_trade_usd

    async def get_leverage(self, symbol: str) -> int | None:
        return None

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage_set.append(leverage)

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        return True


def make_cfg(**values) -> StrategyConfig:
    return StrategyConfig.model_validate(
        {
            "accounts": [{"name": "test", "privkey": "x" * 32}],
            "symbols": ["BTC"],
            "leverage": 10,
            "trade_size_usd": [100, 100],
            "trade_duration": [1, 1],
            "trade_cooldown": [1, 1],
            "trade_parts": 4,
            "max_entry_spread_pct": None,
            **values,
        }
    )


def basket(*hedges: str, lead: str = "1.00", lot: str = "0.01"):
    """A lead account against one or more hedges, all trading BTC on the simulator."""
    net: dict[str, Decimal] = {}
    accounts = [SimClient(f"acc{i}", net, lot=lot) for i in range(len(hedges) + 1)]
    legs = [DeltaLeg(cast(TradingClient, accounts[0]), "bid", Decimal(lead) * PRICE)]
    legs += [
        DeltaLeg(cast(TradingClient, acc), "ask", Decimal(qty) * PRICE)
        for acc, qty in zip(accounts[1:], hedges)
    ]
    trade = DeltaTrade("BTC", legs[0], legs[1:])
    return accounts, trade, net


def strategy_for(accounts: list[SimClient], trades: list[DeltaTrade], monkeypatch, **cfg):
    strategy = DeltaStrategy(make_cfg(**cfg), cast(list[TradingClient], accounts))
    strategy.initial_bal = 1000 * len(accounts)
    monkeypatch.setattr("strategy.cycle.plan_delta_trades", AsyncMock(return_value=trades))
    monkeypatch.setattr("strategy.cycle.random.sample", lambda values, count: list(values))
    monkeypatch.setattr("strategy.cycle.tg.on_trade_start", AsyncMock(return_value=1))
    monkeypatch.setattr("strategy.cycle.tg.on_trade_stop", AsyncMock())
    return strategy


def record_rounds(monkeypatch, accounts: list[SimClient], net: dict[str, Decimal]) -> list[dict]:
    """Snapshot exposure after every opened and every reduced part."""
    snapshots: list[dict] = []
    open_part, reduce_part = DeltaTrade.open, DeltaTrade.reduce

    def snapshot() -> None:
        snapshots.append({"net": dict(net), "pos": [dict(acc.pos) for acc in accounts]})

    async def opened(self, cfg):
        result = await open_part(self, cfg)
        snapshot()
        return result

    async def reduced(self, cfg, remaining, use_limit=False):
        await reduce_part(self, cfg, remaining, use_limit=use_limit)
        snapshot()

    monkeypatch.setattr(DeltaTrade, "open", opened)
    monkeypatch.setattr(DeltaTrade, "reduce", reduced)
    return snapshots


def test_split_qty_sums_exactly_and_stays_even():
    lot = Decimal("0.01")
    parts = split_qty(Decimal("1.03"), lot, 4)

    assert sum(parts) == Decimal("1.03")
    assert max(parts) - min(parts) <= lot
    assert all(part % lot == 0 for part in parts)


def test_split_qty_rejects_qty_off_lot():
    with pytest.raises(ValueError, match="not a multiple"):
        split_qty(Decimal("1.005"), Decimal("0.01"), 4)


async def test_pair_enters_and_exits_in_neutral_parts(monkeypatch):
    accounts, trade, net = basket("1.00")
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)
    rounds = record_rounds(monkeypatch, accounts, net)

    await strategy.trade_cycle()

    lead_orders = [qty for _, _, qty, _ in accounts[0].orders]
    assert lead_orders == [Decimal("0.25")] * 8  # 4 entries, 3 reduces, final close
    assert [reduce for *_, reduce in accounts[0].orders] == [False] * 4 + [True] * 4
    assert [r["net"]["BTC"] for r in rounds] == [0] * 7  # neutral after every part
    assert [r["pos"][0]["BTC"] for r in rounds] == [
        Decimal(x) for x in ("0.25", "0.50", "0.75", "1.00", "0.75", "0.50", "0.25")
    ]
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_trio_parts_keep_lead_equal_to_hedges(monkeypatch):
    # The lead (2L/1S layout) carries what both hedges carry; odd lots must not unbalance a part.
    accounts, trade, net = basket("0.51", "0.50", lead="1.01")
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)
    rounds = record_rounds(monkeypatch, accounts, net)

    await strategy.trade_cycle()

    entries = [[qty for *_, qty, reduce in acc.orders if not reduce] for acc in accounts]
    assert [sum(qtys) for qtys in entries] == [Decimal("1.01"), Decimal("0.51"), Decimal("0.50")]
    assert all(lead == a + b for lead, a, b in zip(*entries))
    assert all(r["net"]["BTC"] == 0 for r in rounds[:4])  # every entry part is exactly neutral
    assert all(abs(r["net"]["BTC"]) <= Decimal("0.02") for r in rounds[4:])  # exits: lot rounding
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_exit_uses_each_account_lot(monkeypatch):
    # Entry sizes come from the lead's lot; a hedge on a finer-lot exchange reduces on its own lot.
    accounts, trade, net = basket("1.00")
    accounts[1].lot = Decimal("0.001")
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)
    record_rounds(monkeypatch, accounts, net)

    await strategy.trade_cycle()

    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_size_with_fewer_lots_than_parts_uses_fewer_parts(monkeypatch):
    accounts, trade, net = basket("0.03", lead="0.03")  # 3 lots cannot make 4 parts
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy.trade_cycle()

    entries = [qty for *_, qty, reduce in accounts[0].orders if not reduce]
    assert entries == [Decimal("0.01")] * 3
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_parts_below_exchange_minimum_use_fewer_parts(monkeypatch):
    accounts, trade, net = basket("1.00")
    accounts[1].min_trade_usd = Decimal(30)  # full leg is $100: a quarter is $25, a third $33
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy.trade_cycle()

    entries = [qty for *_, qty, reduce in accounts[1].orders if not reduce]
    assert entries == [Decimal("0.33"), Decimal("0.33"), Decimal("0.34")]
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_trade_below_exchange_minimum_fails_before_any_change(monkeypatch):
    accounts, trade, net = basket("1.00")
    accounts[1].min_trade_usd = Decimal(150)  # even the whole $100 leg is too small
    strategy = strategy_for(accounts, [trade], monkeypatch)

    with pytest.raises(RuntimeError, match="below minimum"):
        await strategy.trade_cycle()

    assert all(not acc.orders and not acc.leverage_set for acc in accounts)


@pytest.mark.parametrize("failed_order", [1, 3, 4, 6, 8])
async def test_failure_on_any_part_is_cleaned_up(monkeypatch, failed_order):
    # Orders 1-4 are entries, 5-7 reduces, 8 the final close on the hedge account.
    accounts, trade, net = basket("1.00")
    accounts[1].fail = lambda n: n == failed_order
    strategy = strategy_for(accounts, [trade], monkeypatch, max_failures=1)
    strategy.monitor_trades = AsyncMock(return_value=True)
    record_rounds(monkeypatch, accounts, net)
    monkeypatch.setattr("strategy.cycle.tg.on_crash", AsyncMock())
    monkeypatch.setattr("strategy.execution.asyncio.sleep", AsyncMock())

    await strategy.run()

    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_gate_failure_mid_entry_keeps_opened_parts(monkeypatch):
    accounts, trade, net = basket("1.00")
    strategy = strategy_for(accounts, [trade], monkeypatch)
    rounds = record_rounds(monkeypatch, accounts, net)
    gates = iter([True, True, False])

    async def gate(self, cfg) -> bool:
        return next(gates)

    monkeypatch.setattr(DeltaTrade, "gate", gate)

    await strategy.trade_cycle()  # real 1s monitoring: half the size must not count as drift

    entries = [qty for *_, qty, reduce in accounts[0].orders if not reduce]
    assert entries == [Decimal("0.25")] * 2
    assert trade.lead.qty == Decimal("0.50")
    assert len(rounds) == 3  # 2 entry parts, then 2 exit parts: 1 reduce and the final close
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_risk_exit_closes_everything_at_once(monkeypatch):
    accounts, trade, net = basket("1.00")
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=False)
    rounds = record_rounds(monkeypatch, accounts, net)

    await strategy.trade_cycle()

    exits = [qty for *_, qty, reduce in accounts[0].orders if reduce]
    assert exits == [Decimal("1.00")]
    assert len(rounds) == 4  # 4 entry parts, no reduces
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_stop_during_entry_stops_adding_parts_and_closes_everything(monkeypatch):
    accounts, trade, net = basket("1.00")
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.stop_event = asyncio.Event()
    gates = 0

    async def gate(self, cfg) -> bool:
        nonlocal gates
        gates += 1
        if gates == 2:
            strategy.stop_event.set()
        return True

    monkeypatch.setattr(DeltaTrade, "gate", gate)

    await strategy.trade_cycle()

    lead = [(qty, reduce) for *_, qty, reduce in accounts[0].orders]
    assert lead == [(Decimal("0.25"), False)] * 2 + [(Decimal("0.50"), True)]
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_closed_market_during_exit_fails_the_cycle(monkeypatch):
    accounts, trade, net = basket("1.00")
    strategy = strategy_for(accounts, [trade], monkeypatch)

    async def hold_until_market_closes(trades, duration) -> bool:
        for acc in accounts:
            acc.market_open = False
        return True

    strategy.monitor_trades = hold_until_market_closes

    with pytest.raises(ApiError, match="not open"):
        await strategy.trade_cycle()

    assert all(acc.pos.get("BTC") for acc in accounts)  # positions kept for the cleanup retry
