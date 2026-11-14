"""Entering and exiting positions in equal parts, run against a simulated exchange."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from clients.omni import OmniClient, OmniSupportedAsset, OmniTradingSession
from lib.http import ApiError
from strategy.cycle import CycleBudget, DeltaStrategy
from strategy.models import (
    Order,
    OrderBook,
    OrderStatus,
    Position,
    Side,
    StrategyConfig,
    TradingClient,
)
from strategy.runner import _run_groups
from strategy.trade import DeltaLeg, DeltaTrade, basket_lot_size, split_qty

PRICE = Decimal(100)


class SimClient:
    """One exchange account: tracks positions and rejects orders a real exchange would."""

    exchange = "sim"

    def __init__(
        self, name: str, net: dict[str, Decimal], *, lot: str = "0.01", exchange: str = "sim"
    ):
        self.name = name
        self.exchange = exchange
        self.net = net  # shared across accounts: symbol -> signed exposure of the whole basket
        self.lot = Decimal(lot)
        self.pos: dict[str, Decimal] = {}  # symbol -> signed qty
        self.orders: list[tuple[str, Side, Decimal, bool]] = []
        self.min_trade_usd = Decimal(1)
        self.market_open = True
        self.closed: set[str] = set()  # markets shut right now while the rest of them trade
        self.lot_reads: list[str] = []
        self.price_reads: list[str] = []
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
        if not reduce_only and qty * PRICE < self.min_trade_usd:
            raise ApiError(f"notional {qty * PRICE} under min {self.min_trade_usd}")

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

    def _tradeable_now(self, symbol: str) -> bool:
        return self.market_open and symbol not in self.closed

    async def get_price(self, symbol: str) -> Decimal:
        self.price_reads.append(symbol)
        if not self._tradeable_now(symbol):  # like Omni: no quotes while the market is closed
            raise ApiError("Market is not open for trading")
        return PRICE

    async def get_order_book(self, symbol: str) -> OrderBook:
        return OrderBook.build(bids=[(PRICE - 1, 1000)], asks=[(PRICE + 1, 1000)])

    async def get_lot_size(self, symbol: str) -> Decimal:
        self.lot_reads.append(symbol)
        if not self._tradeable_now(symbol):
            # Omni reads the lot from the same indicative quote as the price, so a shut market
            # has no lot either; every other client reads it from a cached market listing.
            raise ApiError("Market is not open for trading")
        return self.lot

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return self.min_trade_usd

    async def get_leverage(self, symbol: str) -> int | None:
        return None

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage_set.append(leverage)

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        return self._tradeable_now(symbol)


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


def strategy_for(accounts: list[SimClient], trades: list[DeltaTrade] | None, monkeypatch, **cfg):
    """A strategy over the simulator. `trades=None` runs the real planner instead of a fixed plan."""
    strategy = DeltaStrategy(make_cfg(**cfg), cast(list[TradingClient], accounts))
    strategy.initial_bal = 1000 * len(accounts)
    if trades is not None:
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


async def test_mixed_lot_parts_fit_every_venue_lot_and_minimum(monkeypatch):
    # A hedge on a coarser grid: the whole basket sizes on 0.1, so a $105 leg trades 1.0 = $100,
    # and 4 parts of that are $20/$30/$20/$30 — under that hedge's $30 minimum, so the entry
    # trades in 3 parts of $30, $30, $40.
    accounts, trade, net = basket("1.05", lead="1.05")  # $105 a leg
    accounts[1].lot = Decimal("0.1")  # coarser than the lead's 0.01
    accounts[1].min_trade_usd = Decimal(30)
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy.trade_cycle()

    entries = [[qty for *_, qty, reduce in acc.orders if not reduce] for acc in accounts]
    assert trade.lot_size == Decimal("0.1")
    assert entries == [[Decimal("0.3"), Decimal("0.3"), Decimal("0.4")]] * 2
    assert all(qty % acc.lot == 0 for acc, qtys in zip(accounts, entries) for qty in qtys)
    assert all(qty * PRICE >= accounts[1].min_trade_usd for qty in entries[1])
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_trade_below_exchange_minimum_fails_before_any_change(monkeypatch):
    accounts, trade, net = basket("1.00")
    accounts[1].min_trade_usd = Decimal(150)  # even the whole $100 leg is too small
    strategy = strategy_for(accounts, [trade], monkeypatch)

    with pytest.raises(RuntimeError, match="below minimum"):
        await strategy.trade_cycle()

    assert all(not acc.orders and not acc.leverage_set for acc in accounts)


# MARK: Hedge direction


@pytest.mark.parametrize("draw", ["bid", "ask"], ids=["draw-bid", "draw-ask"])
@pytest.mark.parametrize("long_exchange", ["sim_rh", "sim_core"], ids=["rh-long", "core-long"])
async def test_configured_long_exchange_reaches_the_orders(monkeypatch, long_exchange, draw):
    # The real planner, driven through a whole cycle: a configured direction has to survive the
    # cycle's call into it, whatever side the random draw would have picked, or roughly half the
    # cycles of a hedge pool hold the forbidden direction and earn no boost.
    net: dict[str, Decimal] = {}
    accounts = [
        SimClient("rh", net, exchange="sim_rh"),
        SimClient("core", net, exchange="sim_core"),
    ]
    strategy = strategy_for(
        accounts, None, monkeypatch, long_exchange=long_exchange, first_as_prime=True
    )
    strategy.monitor_trades = AsyncMock(return_value=True)
    monkeypatch.setattr("strategy.trade.random.choice", lambda _: draw)

    await strategy.trade_cycle()

    short_exchange = "sim_core" if long_exchange == "sim_rh" else "sim_rh"
    entries = {
        acc.exchange: {side for _, side, _, reduce in acc.orders if not reduce} for acc in accounts
    }
    assert entries == {long_exchange: {"bid"}, short_exchange: {"ask"}}
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_legs_on_two_lots_reduce_on_the_common_lot():
    # RH lists BTC on 0.001 and Core on 0.0001: half of 0.003 rounds to 0.002 on the coarse lot
    # but 0.0015 on the fine one, which would leave 0.0005 directional after the reduce.
    net: dict[str, Decimal] = {}
    rh = SimClient("main", net, lot="0.001", exchange="sim_rh")
    core = SimClient("main@core", net, lot="0.0001", exchange="sim_core")
    rh.min_trade_usd = core.min_trade_usd = Decimal(0)  # BTC's lots at the sim's $100 price
    size = Decimal("0.003") * PRICE
    trade = DeltaTrade(
        "BTC",
        DeltaLeg(cast(TradingClient, rh), "bid", size),
        [DeltaLeg(cast(TradingClient, core), "ask", size)],
    )
    cfg = make_cfg()

    await trade.load_qtys()
    await trade.open(cfg)
    await trade.reduce(cfg, 2)

    assert [qty for _, _, qty, reduce in [*rh.orders, *core.orders] if reduce] == [
        Decimal("0.002"),
        Decimal("0.002"),
    ]
    assert net["BTC"] == 0


# MARK: Plan validation


async def test_unbalanced_basket_is_rejected_before_any_order(monkeypatch):
    # A $1000 bid leg on a 0.01 grid against two $500 ask legs whose venue trades 7-unit lots:
    # each leg rounds to one lot, so the basket would hold 7 long against 14 short — $700 of
    # naked short for the whole trade, which no downstream check measures.
    accounts, trade, net = basket("5.00", "5.00", lead="10.00")
    accounts[1].lot = accounts[2].lot = Decimal(7)
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)

    with pytest.raises(RuntimeError, match="imbalance"):
        await strategy.trade_cycle()

    assert all(not acc.orders and not acc.leverage_set for acc in accounts)
    assert not net.get("BTC")


async def test_leg_that_rounds_to_zero_is_rejected_before_any_order(monkeypatch):
    # $1000 / $970 / $30 on a 1-unit lot at $100 give 10, 10 and 0: the basket nets to zero and
    # every leg clears the venue minimum in USD, so only the zero order itself would have failed,
    # after the other two legs filled.
    accounts, trade, net = basket("9.70", "0.30", lead="10.00", lot="1")
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)

    with pytest.raises(RuntimeError, match="too small to trade"):
        await strategy.trade_cycle()

    assert all(not acc.orders and not acc.leverage_set for acc in accounts)
    assert not net.get("BTC")


async def test_min_size_is_checked_against_the_qty_that_will_be_sent(monkeypatch):
    # A $100 venue minimum (Nado) against a basket lot worth $24: a $105 plan sends 4 lots = $96,
    # which the venue rejects mid-open, after the other leg has already filled.
    accounts, trade, net = basket("1.05", lead="1.05")
    accounts[1].lot = Decimal("0.24")
    accounts[1].min_trade_usd = Decimal(100)
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)

    with pytest.raises(RuntimeError, match="below minimum"):
        await strategy.trade_cycle()

    assert all(not acc.orders and not acc.leverage_set for acc in accounts)


async def test_reported_size_is_what_was_actually_sent(monkeypatch):
    # One venue, an off-lot plan: each leg trades whole lots, so the plan log and the reported
    # volume must show the lot-aligned notional, not the requested one.
    accounts, trade, net = basket("5.0025", "5.0025", lead="10.005")
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy.trade_cycle()

    entries = [[qty for *_, qty, reduce in acc.orders if not reduce] for acc in accounts]
    assert [sum(qtys) for qtys in entries] == [Decimal(10), Decimal(5), Decimal(5)]
    assert [leg.size_usd for leg in trade.legs] == [Decimal(1000), Decimal(500), Decimal(500)]
    assert all(not acc.pos.get("BTC") for acc in accounts)


@pytest.mark.parametrize(
    ("lead_lot", "hedge_lot"),
    [("0.3", "0.13"), ("0.01", "0"), ("0.01", "NaN"), ("0.01", "Infinity")],
    ids=["step-too-coarse", "zero-lot", "nan-lot", "inf-lot"],
)
async def test_untradeable_lot_grids_skip_the_symbol_instead_of_failing_the_cycle(
    monkeypatch, lead_lot, hedge_lot
):
    # 0.3 against 0.13 only meet at 3.9, worth $390 against a $50 leg of this $100 basket, so
    # every leg would round to one lot or to zero; a zero, NaN or Infinity lot is a broken venue
    # response. Either way the symbol is not tradeable this cycle, which must not count as a
    # cycle failure and back the bot off while every other symbol is fine.
    accounts, trade, net = basket("10.00", lead="10.00", lot=lead_lot)
    accounts[1].lot = Decimal(hedge_lot)
    accounts[1].exchange = "sim_core"  # grids differ across venues, never within one
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy.trade_cycle()

    assert all(not acc.orders and not acc.leverage_set for acc in accounts)


async def test_a_coarse_step_is_tradeable_once_the_trade_size_carries_it(monkeypatch):
    # The same 0.3/0.13 grids as above, meeting at 3.9 = $390: with an $8000 basket the lightest
    # leg is $4000 and that step is under a tenth of it, so the symbol trades instead of being
    # skipped for a ratio to the coarsest lot it never had to satisfy.
    accounts, trade, net = basket("39.00", lead="39.00", lot="0.3")
    accounts[1].lot = Decimal("0.13")
    strategy = strategy_for(accounts, [trade], monkeypatch, trade_size_usd=[8000, 8000])
    strategy.monitor_trades = AsyncMock(return_value=True)
    rounds = record_rounds(monkeypatch, accounts, net)

    await strategy.trade_cycle()

    entries = [[qty for *_, qty, reduce in acc.orders if not reduce] for acc in accounts]
    assert trade.lot_size == Decimal("3.9")
    assert entries == [[Decimal("7.8"), Decimal("11.7"), Decimal("7.8"), Decimal("11.7")]] * 2
    assert all(qty % acc.lot == 0 for acc, qtys in zip(accounts, entries) for qty in qtys)
    assert [r["net"]["BTC"] for r in rounds[:4]] == [0] * 4  # every entry part is neutral
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_lot_grids_that_meet_further_out_are_tradeable(monkeypatch):
    # 0.2 and 0.5 reject each other's lot, but both accept 1.0, so the basket trades on 1.0.
    # The $4000 trade size matches the planned basket: that step is $100, a tenth of a leg.
    accounts, trade, net = basket("10.00", lead="10.00", lot="0.5")
    accounts[1].lot = Decimal("0.2")
    strategy = strategy_for(accounts, [trade], monkeypatch, trade_size_usd=[4000, 4000])
    strategy.monitor_trades = AsyncMock(return_value=True)
    rounds = record_rounds(monkeypatch, accounts, net)

    await strategy.trade_cycle()

    entries = [[qty for *_, qty, reduce in acc.orders if not reduce] for acc in accounts]
    assert trade.lot_size == Decimal(1)
    assert entries == [[Decimal(2), Decimal(3), Decimal(2), Decimal(3)]] * 2
    assert all(qty % acc.lot == 0 for acc, qtys in zip(accounts, entries) for qty in qtys)
    assert [r["net"]["BTC"] for r in rounds[:4]] == [0] * 4  # every entry part is neutral
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_a_step_that_overshoots_the_planned_size_is_refused_before_any_order(monkeypatch):
    # 0.2 and 0.5 meet at 1.0, and a $150 leg at price 100 is 1.5 steps, which rounds half-even
    # up: $200 a leg, a third over plan, out of margin find_safe_pair sized at 90%. The venue
    # rejects it for margin mid-open, after the lead has filled on a limit or a hedge delay.
    accounts, trade, net = basket("1.50", lead="1.50", lot="0.5")
    accounts[1].lot = Decimal("0.2")
    strategy = strategy_for(accounts, [trade], monkeypatch, trade_size_usd=[4000, 4000])
    strategy.monitor_trades = AsyncMock(return_value=True)

    with pytest.raises(RuntimeError, match="over the planned size"):
        await strategy.trade_cycle()

    assert all(not acc.orders and not acc.leverage_set for acc in accounts)
    assert not net.get("BTC")


async def test_a_fine_venue_leg_cannot_borrow_the_coarse_venue_lot():
    # 0.001 against 1 nest, so the basket trades whole units. A $60 leg on the coarse venue rounds
    # to $100 on its own grid, as it always has; the same $100 on the fine venue is the basket
    # step's doing, two thirds over its plan and paid out of that account's margin.
    accounts, trade, net = basket("0.60", lead="0.60", lot="0.001")
    accounts[1].lot = Decimal(1)
    accounts[1].exchange = "sim_core"

    with pytest.raises(RuntimeError, match=r"over the planned size \(acc0 100\.00/60\.00 USD\)"):
        await trade.load_qtys()


async def test_a_nested_step_too_coarse_for_the_fine_venue_skips_the_symbol(monkeypatch):
    # The same grids through a cycle: the filter prices the whole-unit step against the planned
    # legs and skips the symbol instead of letting the plan fail every cycle.
    accounts, trade, net = basket("0.60", lead="0.60", lot="0.001")
    accounts[1].lot = Decimal(1)
    accounts[1].exchange = "sim_core"
    strategy = strategy_for(accounts, [trade], monkeypatch, market_hours="off")
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy.trade_cycle()

    assert all(not acc.orders and not acc.leverage_set for acc in accounts)


async def test_a_single_venue_grid_may_round_a_leg_up_as_it_always_has(monkeypatch):
    # One venue on a 1.0 lot: a $150 leg is 1.5 lots and sends $200, the same third over plan.
    # That is the venue's own grid rather than a step the basket invented, and it trades today,
    # so the ceiling above must not reach it — half a leg lot is always allowed.
    accounts, trade, net = basket("1.50", lead="1.50", lot="1")
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy.trade_cycle()

    entries = [[qty for *_, qty, reduce in acc.orders if not reduce] for acc in accounts]
    assert entries == [[Decimal(1), Decimal(1)]] * 2  # 2 lots a leg, so 2 parts, not 4
    assert [leg.size_usd for leg in trade.legs] == [Decimal(200)] * 2  # what was really sent
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_an_unreadable_lot_size_fails_the_cycle_instead_of_dropping_the_symbol(monkeypatch):
    # A symbol the cycle actually plans must never be skipped quietly: an expired session, a 429
    # or a dead proxy has to reach `run()`'s backoff and Telegram error, not log "0/1 symbols are
    # tradeable", reset the failure count and leave the bot looping forever doing nothing.
    accounts, trade, net = basket("1.00")

    async def no_lot(symbol: str) -> Decimal:
        raise ApiError("401 session expired")

    monkeypatch.setattr(accounts[1], "get_lot_size", no_lot)
    strategy = strategy_for(accounts, [trade], monkeypatch)
    strategy.monitor_trades = AsyncMock(return_value=True)

    with pytest.raises(ApiError, match="session expired"):
        await strategy.trade_cycle()

    assert all(not acc.orders and not acc.leverage_set for acc in accounts)


async def test_a_closed_market_does_not_fail_a_cycle_that_has_symbols_to_trade(monkeypatch):
    # The live shape: one exchange, several configured markets, one of them inside its daily
    # closure. Omni answers both the lot size and the price from one indicative quote and refuses
    # while a market is shut, so reading a lot per configured symbol failed every cycle — closing
    # the pool, counting a failure, backing off 3m→60m and sending a Telegram error — for a market
    # the cycle was not going to trade. One exchange shares one grid per market, so nothing is
    # read here at all.
    accounts, trade, net = basket("1.00")
    for acc in accounts:
        acc.closed = {"US100S"}

    strategy = strategy_for(
        accounts, [trade], monkeypatch, symbols=["BTC", "US100S"], market_hours="off"
    )
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy.trade_cycle()

    assert [qty for *_, qty, reduce in accounts[0].orders if not reduce] == [Decimal("0.25")] * 4
    assert all("US100S" not in acc.lot_reads + acc.price_reads for acc in accounts)
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_a_market_that_reopens_before_the_entry_does_not_fail_the_cycle(monkeypatch):
    # The same failure under the default `market_hours = "auto"`: the hours filter asks about
    # `open_at`, which limit waits push hours ahead, so a market closed right now passes it and
    # only then has its lot read. An index swap inside its daily closure failed every cycle.
    accounts, trade, net = basket("1.00")

    async def open_at_entry(symbol: str, at: datetime, reduce_only=False) -> bool:
        return True  # shut right now, open by the time the cycle would enter

    for acc in accounts:
        acc.closed = {"US100S"}
        acc.is_symbol_tradeable = open_at_entry

    strategy = strategy_for(accounts, [trade], monkeypatch, symbols=["BTC", "US100S"])
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy.trade_cycle()

    assert [qty for *_, qty, reduce in accounts[0].orders if not reduce] == [Decimal("0.25")] * 4
    assert all("US100S" not in acc.lot_reads for acc in accounts)


async def test_lot_grids_are_read_once_per_venue(monkeypatch):
    # Lots come from a market record keyed by the symbol alone in every client here, so a second
    # account on a venue has nothing new to say and its reads are pure cost — on Omni an uncached
    # authenticated quote per account per symbol per cycle.
    accounts, trade, net = basket("0.50", "0.50")  # acc0 and acc1 share the simulator venue
    accounts[2].exchange = "sim_core"
    strategy = strategy_for(accounts, [trade], monkeypatch, symbols=["BTC", "ETH"])
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy._tradeable_symbols(30, Decimal(100))

    assert accounts[0].lot_reads == ["BTC", "ETH"]  # first account of its venue reads for it
    assert accounts[1].lot_reads == []  # same venue as the lead: the same grid
    assert accounts[2].lot_reads == ["BTC", "ETH"]


async def test_a_mixed_venue_pool_drops_a_symbol_its_venue_cannot_quote(monkeypatch):
    # Across venues the grids do have to be reconciled, and Omni's refusal to quote a shut market
    # is indistinguishable from a transient error. Dropping the symbol with the API's own words
    # beats failing a cycle that has other symbols to trade; the drop is a warning, and the empty
    # cycle is the "Only n/m symbols are tradeable" warning, so neither is silent.
    accounts, trade, net = basket("1.00")
    accounts[1].exchange = "sim_core"
    accounts[1].closed = {"BTC"}
    warning = Mock()
    monkeypatch.setattr("strategy.cycle.logger.warning", warning)
    strategy = strategy_for(accounts, [trade], monkeypatch, market_hours="off")
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy.trade_cycle()

    assert all(not acc.orders and not acc.leverage_set for acc in accounts)
    assert warning.call_args_list[0][0][0] == (
        "Symbols not tradeable across venues — BTC: sim_core: Market is not open for trading"
    )
    assert "Only 0/1 symbols are tradeable" in warning.call_args_list[1][0][0]


async def test_a_coarse_step_whose_price_is_unavailable_drops_the_symbol(monkeypatch):
    # Valuing an invented step needs one price, and the lot being readable says nothing about the
    # price: Lighter reads lots from a cached listing and prices from a live book, Omni serves both
    # from the same quote. A step that cannot be valued drops its symbol instead of failing the
    # cycle, exactly as an unreadable lot does.
    accounts, trade, net = basket("10.00", lead="10.00", lot="0.3")
    accounts[1].lot = Decimal("0.13")  # 0.3 and 0.13 meet at 3.9, a step neither venue trades
    accounts[1].exchange = "sim_core"

    async def no_price(symbol: str) -> Decimal:
        raise ApiError("429 too many requests")

    monkeypatch.setattr(accounts[0], "get_price", no_price)
    warning = Mock()
    monkeypatch.setattr("strategy.cycle.logger.warning", warning)
    strategy = strategy_for(accounts, [trade], monkeypatch, market_hours="off")
    strategy.monitor_trades = AsyncMock(return_value=True)

    await strategy.trade_cycle()

    assert all(not acc.orders and not acc.leverage_set for acc in accounts)
    assert warning.call_args_list[0][0][0] == (
        "Symbols not tradeable across venues — BTC: sim: 429 too many requests"
    )


def test_split_refuses_a_lead_that_shares_its_side():
    # Every part's lead is the sum of that part's hedges, so a second leg on the lead's side would
    # plan a lead larger than its own side. The bid total still equals the ask total here, which
    # is exactly why check_plan cannot see it.
    accounts, trade, net = basket("1.00", "1.00")
    trade.lot_size = Decimal("0.01")
    trade.rest[0].side = "bid"
    trade.lead.qty, trade.rest[0].qty, trade.rest[1].qty = Decimal(1), Decimal(1), Decimal(2)

    trade.check_plan()  # balanced in totals: 2 bid against 2 ask

    with pytest.raises(RuntimeError, match="cannot share its bid side"):
        trade.split(2)


def test_split_refuses_an_unbalanced_trade_even_in_one_part():
    accounts, trade, net = basket("1.00")
    trade.lot_size = Decimal("0.01")
    trade.lead.qty, trade.rest[0].qty = Decimal("0.07"), Decimal("0.14")

    with pytest.raises(RuntimeError, match="not delta-neutral"):
        trade.split(1)


async def test_an_off_grid_plan_does_not_fall_back_to_one_part(monkeypatch):
    # Only "too few lots for this many parts" may step down to fewer parts. Qtys that do not sit
    # on the basket lot are a broken plan: trading it in one part sends off-grid orders.
    accounts, trade, net = basket("1.00")
    trade.lot_size = Decimal("0.1")
    trade.lead.qty, trade.rest[0].qty = Decimal("0.25"), Decimal("0.25")
    strategy = strategy_for(accounts, [trade], monkeypatch)

    with pytest.raises(ValueError, match="not a multiple"):
        await strategy._fit_parts([trade])

    assert all(not acc.orders for acc in accounts)


async def test_zero_lot_size_fails_with_a_clear_error():
    accounts, trade, net = basket("1.00")
    accounts[1].lot = Decimal(0)  # Omni reads qty_tick straight from a quote payload

    with pytest.raises(RuntimeError, match="lot size"):
        await trade.load_qtys()


@pytest.mark.parametrize(
    ("lots", "step"),
    [
        (["0.01"], "0.01"),
        (["0.001", "0.001"], "0.001"),
        (["0.01", "0.1"], "0.1"),  # nested grids: the coarsest lot, as a single venue uses
        (["0.2", "0.5"], "1"),
        (["0.0025", "0.001"], "0.005"),
        (["0.1", "0.01", "7"], "7"),
    ],
)
def test_basket_lot_is_the_finest_step_every_grid_accepts(lots, step):
    assert basket_lot_size([Decimal(lot) for lot in lots]) == Decimal(step)


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


async def test_hold_through_market_closure_then_close_in_parts(monkeypatch):
    # The market closes during the hold and is still closed when the hold ends:
    # keep the position quietly and close it in parts once the market reopens.
    accounts, trade, net = basket("1.00")
    strategy = strategy_for(accounts, [trade], monkeypatch, trade_heartbeat=15)
    clock = SimpleNamespace(now=1000.0)
    reduced_at: list[float] = []

    async def sleep(sec: float) -> None:
        clock.now += sec
        for acc in accounts:
            acc.market_open = not 1000.5 <= clock.now < 1030

    reduce_part = DeltaTrade.reduce

    async def reduce(self, cfg, remaining, use_limit=False):
        reduced_at.append(clock.now)
        await reduce_part(self, cfg, remaining, use_limit=use_limit)

    warning = Mock()
    monkeypatch.setattr("strategy.cycle.time", SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr("strategy.cycle.asyncio.sleep", sleep)
    monkeypatch.setattr("strategy.cycle.logger.warning", warning)
    monkeypatch.setattr(DeltaTrade, "reduce", reduce)

    await strategy.trade_cycle()

    assert reduced_at and min(reduced_at) >= 1030  # nothing was closed while the market was shut
    assert warning.call_count == 0  # no safety-check noise while prices are unavailable
    assert all(not acc.pos.get("BTC") for acc in accounts)


# MARK: Cycle budget


def count_cycles(monkeypatch) -> list[bool | str]:
    """Outcome of every cycle the loop ran: traded, skipped, or the error that failed it."""
    outcomes: list[bool | str] = []
    real_cycle = DeltaStrategy.trade_cycle

    async def counted(self) -> bool:
        try:
            traded = await real_cycle(self)
        except Exception as e:
            outcomes.append(str(e))
            raise

        outcomes.append(traded)
        return traded

    monkeypatch.setattr(DeltaStrategy, "trade_cycle", counted)
    return outcomes


def budget_lines(info: Mock) -> list[str]:
    """Every line the loop logged about the cycle budget, in order."""
    return [call.args[0] for call in info.call_args_list if "cycle budget" in call.args[0].lower()]


def budget_runner(accounts: list[SimClient], monkeypatch, cycles: int, **cfg):
    """A whole run against the simulator, stopped by the cycle budget instead of by a signal."""
    strategy = strategy_for(accounts, None, monkeypatch, **cfg)
    strategy.budget = CycleBudget(cycles)
    return strategy


async def test_the_cycle_budget_ends_the_run_after_its_trades(monkeypatch):
    # The flag exists to leave a live test unattended: the run has to end by itself after N
    # cycles that traded, through the same cleanup a Ctrl-C goes through.
    accounts, _, net = basket("1.00")
    outcomes = count_cycles(monkeypatch)
    info, waits = Mock(), []
    monkeypatch.setattr("strategy.cycle.logger.info", info)
    strategy = budget_runner(accounts, monkeypatch, 2)
    wait = strategy._wait

    async def recorded(sec: float, *stops) -> None:
        waits.append(sec)
        await wait(sec, *stops)

    strategy._wait = recorded

    await strategy.run()

    lines = [call.args[0] for call in info.call_args_list]
    assert outcomes == [True, True]  # no third cycle was ever started
    assert waits == [1.0]  # cooldown between the two cycles, none after the last
    assert budget_lines(info) == [
        "Cycle budget: 1/2 traded",
        "Cycle budget: 2/2 traded",
        "No cycle budget left (2/2 traded), stopping",
    ]
    assert lines[-1] == "Shutdown cleanup complete"  # the normal shutdown path, not a crash
    assert all(not acc.pos.get("BTC") for acc in accounts) and not net["BTC"]


async def test_the_budget_exit_closes_what_a_cycle_left_behind(monkeypatch):
    # The exit is the shutdown cleanup, not just the last cycle's own close: a leg a cycle lost
    # track of, or an order resting on a venue, must not outlive the run.
    accounts, _, net = basket("1.00")
    strategy = budget_runner(accounts, monkeypatch, 1)
    stop = AsyncMock()

    async def leftover(*args, **kwargs) -> None:
        accounts[0].pos["ETH"] = Decimal("0.5")  # a position the cycle did not close
        await stop(*args, **kwargs)

    monkeypatch.setattr("strategy.cycle.tg.on_trade_stop", leftover)

    await strategy.run()

    assert stop.await_count == 1
    assert accounts[0].orders[-1] == ("ETH", "ask", Decimal("0.5"), True)
    assert all(not any(acc.pos.values()) for acc in accounts)


async def test_a_skipped_cycle_does_not_spend_the_budget(monkeypatch):
    # "Let it do N real trades" is the whole point: a cycle that found nothing to trade (a closed
    # market here) must leave the budget where it was, or the run ends having traded less than N.
    accounts, _, net = basket("1.00")
    for acc in accounts:
        acc.market_open = False

    async def reopens(symbol: str, at: datetime, reduce_only=False) -> bool:
        open_now = accounts[0].market_open
        for acc in accounts:
            acc.market_open = True  # shut for the first cycle's filter, open from then on
        return open_now

    for acc in accounts:
        acc.is_symbol_tradeable = reopens

    outcomes = count_cycles(monkeypatch)
    info = Mock()
    monkeypatch.setattr("strategy.cycle.logger.info", info)
    monkeypatch.setattr("strategy.cycle.logger.warning", Mock())
    strategy = budget_runner(accounts, monkeypatch, 1)

    await strategy.run()

    assert outcomes == [False, True]  # the skipped cycle did not count, so one trade still ran
    assert budget_lines(info) == [
        "Cycle budget: 0/1 traded, this one did not count",
        "Cycle budget: 1/1 traded",
        "No cycle budget left (1/1 traded), stopping",
    ]
    assert len([qty for *_, qty, reduce in accounts[0].orders if not reduce]) == 4
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_a_failed_cycle_does_not_spend_the_budget(monkeypatch):
    # A cycle that failed is cleaned up and retried, so it traded nothing either. Counting it
    # would end a `--cycles 1` run on an exchange error, with no trade to show for it.
    accounts, _, net = basket("1.00")
    accounts[1].fail = lambda n: n == 1
    outcomes = count_cycles(monkeypatch)
    info, crash = Mock(), AsyncMock()
    monkeypatch.setattr("strategy.cycle.logger.info", info)
    monkeypatch.setattr("strategy.cycle.logger.warning", Mock())
    monkeypatch.setattr("strategy.cycle.tg.on_error", AsyncMock())
    monkeypatch.setattr("strategy.cycle.tg.on_crash", crash)
    monkeypatch.setattr("strategy.execution.asyncio.sleep", AsyncMock())
    strategy = budget_runner(accounts, monkeypatch, 1)
    strategy._wait = AsyncMock()  # the 3m failure backoff; its length is not what this pins

    await strategy.run()

    assert outcomes == ["order 1 rejected", True]
    assert budget_lines(info) == [  # the failed cycle is counted nowhere, so it says nothing here
        "Cycle budget: 1/1 traded",
        "No cycle budget left (1/1 traded), stopping",
    ]
    assert crash.await_count == 0  # a budget exit is not a crash, and neither is a retried cycle
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_without_the_flag_the_run_never_ends_on_its_own(monkeypatch):
    # The default has to stay today's unbounded run: no budget object, no counting, no extra line,
    # and the only way out is still the stop that closes everything.
    accounts, _, net = basket("1.00")
    outcomes = count_cycles(monkeypatch)
    info = Mock()
    monkeypatch.setattr("strategy.cycle.logger.info", info)
    strategy = strategy_for(accounts, None, monkeypatch)
    strategy.stop_event = asyncio.Event()

    task = asyncio.create_task(strategy.run())
    while len(outcomes) < 2:
        await asyncio.sleep(0.05)

    assert not task.done()  # two traded cycles and still running, as it would forever
    strategy.stop_event.set()
    await task

    lines = [call.args[0] for call in info.call_args_list]
    assert outcomes == [True, True]
    assert not budget_lines(info)
    assert lines[-1] == "Shutdown cleanup complete"
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_a_group_starting_on_a_spent_budget_trades_nothing(monkeypatch):
    # Groups start staggered, and a regroup builds new ones, so a group can reach its first cycle
    # after another group traded the last one. It has to stop before trading, not after.
    accounts, _, net = basket("1.00")
    outcomes = count_cycles(monkeypatch)
    info = Mock()
    monkeypatch.setattr("strategy.cycle.logger.info", info)
    strategy = budget_runner(accounts, monkeypatch, 1)
    strategy.budget.record(traded=True)  # another group traded the only cycle of this run

    await strategy.run()

    lines = [call.args[0] for call in info.call_args_list]
    assert not outcomes  # the group never entered a cycle
    assert all(not acc.orders for acc in accounts)
    assert budget_lines(info)[-1] == "No cycle budget left (1/1 traded), stopping"
    assert lines[-1] == "Shutdown cleanup complete"


async def test_a_stop_ends_a_limited_run_waiting_out_its_cooldown(monkeypatch):
    # The cooldown of a `--cycles` run now watches the budget as well, and the stop is still the
    # other latch: a Ctrl-C mid-cooldown closes everything instead of waiting for the next cycle.
    accounts, _, net = basket("1.00")
    outcomes = count_cycles(monkeypatch)
    info = Mock()
    monkeypatch.setattr("strategy.cycle.logger.info", info)
    strategy = budget_runner(accounts, monkeypatch, 3, trade_cooldown=[60, 60])
    strategy.stop_event = asyncio.Event()

    task = asyncio.create_task(strategy.run())
    while not outcomes:  # the first cycle, with two still left in the budget
        await asyncio.sleep(0.05)

    strategy.stop_event.set()
    # Not wait_for: that cancels the run itself, which is exactly the signal under test here.
    done, pending = await asyncio.wait([task], timeout=5)
    for leftover in pending:  # a cooldown deaf to the stop must not keep running
        leftover.cancel()

    assert done, "the stop did not end the cooldown"
    lines = [call.args[0] for call in info.call_args_list]
    assert outcomes == [True]  # no second cycle, and no minute spent waiting for one
    assert lines[-2].startswith("Stop requested")
    assert lines[-1] == "Shutdown cleanup complete"
    assert all(not any(acc.pos.values()) for acc in accounts)


async def test_a_stop_in_the_failure_backoff_still_leaves_as_before(monkeypatch):
    # The budget latch shares that backoff with the stop, and each keeps its own exit: a spent
    # budget breaks to the budget's cleanup, while a stop propagates as it always has.
    accounts, _, net = basket("1.00")
    accounts[1].fail = lambda n: n == 1  # the cycle fails, its cleanup still works
    outcomes = count_cycles(monkeypatch)
    info = Mock()
    monkeypatch.setattr("strategy.cycle.logger.info", info)
    monkeypatch.setattr("strategy.cycle.logger.warning", Mock())
    monkeypatch.setattr("strategy.cycle.tg.on_error", AsyncMock())
    strategy = budget_runner(accounts, monkeypatch, 2)
    strategy.stop_event = asyncio.Event()

    task = asyncio.create_task(strategy.run())
    while not outcomes:  # the failed cycle, now waiting out its 3m backoff
        await asyncio.sleep(0.05)

    strategy.stop_event.set()
    done, pending = await asyncio.wait([task], timeout=5)  # never cancels the task itself
    for leftover in pending:  # a backoff deaf to the stop must not keep running
        leftover.cancel()

    assert done and task.cancelled()  # the stop still ends the run by propagating
    assert not budget_lines(info)  # and not as a budget that ran out, because it has not
    assert all(not any(acc.pos.values()) for acc in accounts)


def grouped_run(monkeypatch) -> list[SimClient]:
    """Four simulator accounts for two groups of two, with the group stagger out of the way."""
    net: dict[str, Decimal] = {}
    accounts = [SimClient(f"acc{i}", net) for i in range(4)]
    # Only the group stagger: patching random.uniform itself would zero the trade size too.
    monkeypatch.setattr("strategy.runner.random", SimpleNamespace(uniform=lambda *_: 0))
    monkeypatch.setattr("strategy.cycle.random.sample", lambda values, count: list(values))
    monkeypatch.setattr("strategy.cycle.tg.on_trade_start", AsyncMock(return_value=1))
    monkeypatch.setattr("strategy.cycle.tg.on_trade_stop", AsyncMock())
    return accounts


async def test_several_groups_stop_promptly_when_the_limit_is_reached(monkeypatch):
    # Two groups share one counter, so the first one to finish is waiting out a cooldown it no
    # longer needs when the other trades the last cycle. It has to leave right then: the cooldown
    # here is far longer than the whole run, so sitting it out would hang this test.
    accounts = grouped_run(monkeypatch)
    order = accounts[2].market_order

    async def slow_order(*args, **kwargs):  # group 2 lands its cycle after group 1 is waiting
        await asyncio.sleep(0.3)
        return await order(*args, **kwargs)

    accounts[2].market_order = slow_order
    outcomes = count_cycles(monkeypatch)
    info = Mock()
    monkeypatch.setattr("strategy.cycle.logger.info", info)
    budget = CycleBudget(2)
    cfg = make_cfg(group_size=2, trade_cooldown=[60, 60])

    await asyncio.wait_for(_run_groups(cfg, cast(list[TradingClient], accounts), budget), 20)

    lines = [call.args[0] for call in info.call_args_list]
    assert outcomes == [True, True]  # N cycles of this run between them, not N per group
    assert budget.done == 2
    # Both groups left over the budget, the group that was waiting included: nobody was stopped.
    assert budget_lines(info).count("No cycle budget left (2/2 traded), stopping") == 2
    assert not any(line.startswith("Stop requested") for line in lines)
    assert all(not any(acc.pos.values()) for acc in accounts)


async def test_a_group_in_its_failure_backoff_leaves_when_the_limit_is_reached(monkeypatch):
    # Group 2's cycle fails and it goes into the 3m retry backoff; group 1 then trades the last
    # cycle. The backoff keeps its length, so the only way this run ends in seconds is that a
    # spent budget ends the waiting the way it ends a cooldown.
    accounts = grouped_run(monkeypatch)
    accounts[2].fail = lambda n: n == 1  # the cycle fails, its cleanup still works
    outcomes = count_cycles(monkeypatch)
    info, warning = Mock(), Mock()
    monkeypatch.setattr("strategy.cycle.logger.info", info)
    monkeypatch.setattr("strategy.cycle.logger.warning", warning)
    monkeypatch.setattr("strategy.cycle.tg.on_error", AsyncMock())
    crash = AsyncMock()
    monkeypatch.setattr("strategy.cycle.tg.on_crash", crash)
    budget = CycleBudget(2)

    await asyncio.wait_for(
        _run_groups(make_cfg(group_size=2), cast(list[TradingClient], accounts), budget), 20
    )

    warnings = [call.args[0] for call in warning.call_args_list]
    assert outcomes.count(True) == 2 and len(outcomes) == 3  # no cycle traded past the limit
    assert "order 1 rejected" in outcomes  # group 2 failed its cycle and waited to retry
    assert any("retrying in 3m" in line for line in warnings)  # the backoff was not shortened
    assert budget_lines(info).count("No cycle budget left (2/2 traded), stopping") == 2
    assert crash.await_count == 0  # leaving a backoff on a spent budget is not a crash
    assert all(not any(acc.pos.values()) for acc in accounts)


async def test_a_single_group_trades_exactly_the_limit(monkeypatch):
    # One group is the normal case, and there the number the operator typed is exact: no cycle is
    # lost and none is started past the limit, whatever the multi-group slack allows.
    accounts = grouped_run(monkeypatch)[:2]
    outcomes = count_cycles(monkeypatch)
    budget = CycleBudget(3)

    await asyncio.wait_for(_run_groups(make_cfg(), cast(list[TradingClient], accounts), budget), 30)

    assert outcomes == [True, True, True]
    assert budget.done == 3
    assert all(not any(acc.pos.values()) for acc in accounts)


async def test_a_spent_budget_ends_the_run_instead_of_regrouping(monkeypatch):
    # Regrouping re-creates the groups forever by design, so the budget has to end that loop too,
    # and without waiting out a regroup interval the operator never asked to sit through.
    accounts = grouped_run(monkeypatch)
    outcomes = count_cycles(monkeypatch)
    # A cooldown outlasting the run keeps this about the regroup loop, not about which group
    # finishes its cycle first.
    cfg = make_cfg(group_size=2, regroup_interval=30, trade_cooldown=[60, 60])

    await asyncio.wait_for(
        _run_groups(cfg, cast(list[TradingClient], accounts), CycleBudget(2)), 20
    )

    assert outcomes == [True, True]  # one round of groups, not a second set after the regroup
    assert all(not acc.pos.get("BTC") for acc in accounts)


async def test_groups_that_exit_early_wait_out_the_regroup_interval(monkeypatch):
    # Every group can leave before the budget is spent (max_failures, a failed balance read).
    # The run must still wait the regroup interval, not restart the groups at once in a loop.
    accounts = grouped_run(monkeypatch)
    budget = CycleBudget(2)
    started: list[str] = []
    waits: list[float] = []

    async def run_group(cfg, name, accs, stop_event, stagger, budget) -> None:
        started.append(name)
        if len(started) >= 6:  # a regroup loop that never waits ends here instead of hanging
            budget.record(traded=True)
            budget.record(traded=True)

    async def regroup_wait(sec: float, *events) -> None:
        waits.append(sec)
        budget.record(traded=True)
        budget.record(traded=True)  # spent while waiting: the wait ends early, as it does live
        raise asyncio.CancelledError

    monkeypatch.setattr("strategy.runner._run_group", run_group)
    monkeypatch.setattr("strategy.runner.utils.interruptible_sleep", regroup_wait)
    cfg = make_cfg(group_size=2, regroup_interval=30)

    await asyncio.wait_for(_run_groups(cfg, cast(list[TradingClient], accounts), budget), 20)

    assert started == ["01", "02"]  # one round of groups, no restart before the interval
    assert waits == [30]


def omni_with(asset: OmniSupportedAsset) -> OmniClient:
    client = OmniClient("test", "0x" + "11" * 32)
    client.supported_assets = AsyncMock(return_value={asset.asset: asset})  # type: ignore[method-assign]
    return client


@pytest.mark.parametrize(
    ("hour", "tradeable"), [(20, True), (21, False), (21.5, False), (22, True), (23, True)]
)
async def test_omni_follows_trading_sessions(hour, tradeable):
    day = datetime(2026, 10, 20, tzinfo=UTC).timestamp()
    at = datetime.fromtimestamp(day + hour * 3600, UTC)
    sessions = [
        OmniTradingSession(open=datetime(2026, 10, 19, 22, tzinfo=UTC), close=datetime(2026, 10, 20, 21, tzinfo=UTC)),
        OmniTradingSession(open=datetime(2026, 10, 20, 22, tzinfo=UTC), close=datetime(2026, 10, 21, 21, tzinfo=UTC)),
    ]  # fmt: skip
    asset = OmniSupportedAsset(asset="US100S", has_perp=True, trading_sessions=sessions)

    assert await omni_with(asset).is_symbol_tradeable("US100S", at) is tradeable


async def test_omni_without_sessions_trades_around_the_clock():
    asset = OmniSupportedAsset(asset="BTC", has_perp=True)
    at = datetime(2026, 10, 20, 21, 30, tzinfo=UTC)

    assert await omni_with(asset).is_symbol_tradeable("BTC", at) is True
