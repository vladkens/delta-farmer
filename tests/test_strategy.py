from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import ANY, AsyncMock, Mock

import pytest

from lib.errors import AppError
from strategy.cycle import Balances, DeltaStrategy
from strategy.execution import close_all
from strategy.models import (
    Order,
    OrderBook,
    OrderStatus,
    Position,
    Side,
    StrategyConfig,
    TradingClient,
)
from strategy.runner import _check_cfg, _check_symbols
from strategy.trade import DeltaLeg, DeltaTrade, DeltaTradeSummary


def make_cfg(**values) -> StrategyConfig:
    return StrategyConfig.model_validate(
        {
            "accounts": [{"name": "test", "privkey": "x" * 32}],
            "symbols": ["BTC"],
            "leverage": 10,
            "trade_size_usd": [100, 100],
            "trade_duration": [1, 1],
            "trade_cooldown": [1, 1],
            "trade_heartbeat": 1,
            "position_roi_limit": 0.8,
            "combined_roi_limit": 0.1,
            **values,
        }
    )


def position(symbol: str, side: Side, size: str, entry: str = "50000") -> Position:
    return Position(
        id="position",
        symbol=symbol,
        side=side,
        size=Decimal(size),
        entry_price=Decimal(entry),
    )


class Client:
    exchange = "mock"

    def __init__(self, name: str, *, price: str = "50000"):
        self.name = name
        self.price = Decimal(price)
        self.balance_value = Decimal(1000)
        self.positions_value: list[Position] = []
        self.tradeable = True
        self.calls: list[str] = []
        self.leverage: int | None = None
        self.min_trade_usd = Decimal(1)

    async def balance(self) -> Decimal:
        return self.balance_value

    async def get_price(self, symbol: str) -> Decimal:
        return self.price

    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        return self.price - 1, self.price + 1

    async def get_order_book(self, symbol: str) -> OrderBook:
        return OrderBook.build(bids=[(self.price - 1, 10)], asks=[(self.price + 1, 10)])

    async def get_lot_size(self, symbol: str) -> Decimal:
        return Decimal("0.001")

    async def get_tick_size(self, symbol: str) -> Decimal:
        return Decimal(1)

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return self.min_trade_usd

    async def get_leverage(self, symbol: str) -> int | None:
        return self.leverage

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage = leverage
        self.calls.append(f"leverage:{symbol}:{leverage}")

    async def positions(self) -> list[Position]:
        return self.positions_value

    async def close_position(self, value: Position) -> bool:
        self.calls.append(f"close:{value.symbol}")
        return True

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        self.calls.append(f"market:{symbol}:{side}:{qty}")
        return Order(
            id="market",
            symbol=symbol,
            side=side,
            size=qty,
            filled=qty,
            price=None,
            status=OrderStatus.FILLED,
        )

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        raise AssertionError("unexpected limit order")

    async def cancel_order(self, order: Order) -> bool:
        return True

    async def get_order(self, order_id: str) -> Order | None:
        return None

    async def cancel_all_orders(self) -> int:
        self.calls.append("cancel_all_orders")
        return 0

    async def close_all_positions(self) -> int:
        self.calls.append("close_all_positions")
        return 0

    async def get_symbols(self) -> list[str]:
        return ["BTC"]

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        self.calls.append(f"tradeable:{symbol}:{at.isoformat()}")
        return self.tradeable


def trade(lead: Client, rest: Client, symbol: str = "BTC") -> DeltaTrade:
    return DeltaTrade(
        symbol,
        DeltaLeg(cast(TradingClient, lead), "bid", Decimal(100), qty=Decimal("0.002")),
        [DeltaLeg(cast(TradingClient, rest), "ask", Decimal(100), qty=Decimal("0.002"))],
    )


def test_pnl_mismatch_warns(monkeypatch):
    warning = Mock()
    monkeypatch.setattr("strategy.cycle.logger.info", Mock())
    monkeypatch.setattr("strategy.cycle.logger.warning", warning)

    current = Balances({"main": 79.54, "ok01": 38.68})
    previous = Balances({"main": 79.64, "ok01": 38.61})
    pnl, total_pnl = current.log_pnl(previous, Decimal("138.44"), -0.19)

    assert pnl == pytest.approx(-0.03)
    assert total_pnl == pytest.approx(-20.22)
    warning.assert_called_once_with(
        "Unexpected P/L change: expected -0.22, actual -20.22, Δ -20.00. Possible balance movement."
    )


def test_expected_pnl_is_quiet(monkeypatch):
    warning = Mock()
    monkeypatch.setattr("strategy.cycle.logger.info", Mock())
    monkeypatch.setattr("strategy.cycle.logger.warning", warning)

    current = Balances({"main": 99.90, "ok01": 39.95})
    previous = Balances({"main": 100.0, "ok01": 39.88})
    current.log_pnl(previous, Decimal(140), -0.12)

    warning.assert_not_called()


async def test_market_opens_all_legs():
    lead, rest = Client("lead"), Client("rest")

    await trade(lead, rest).open(make_cfg(use_limit=False))

    assert lead.calls == ["market:BTC:bid:0.002"]
    assert rest.calls == ["market:BTC:ask:0.002"]


async def test_trade_pnl():
    lead, rest = Client("lead", price="55000"), Client("rest", price="45000")
    lead.positions_value = [position("BTC", "bid", "0.002")]
    rest.positions_value = [position("BTC", "ask", "0.002")]

    summary = await trade(lead, rest).state(make_cfg())

    assert summary.total_pnl == Decimal(20)
    assert summary.total_entry_cost == Decimal(200)
    assert summary.combined_roi == Decimal("0.1")
    assert summary.healthy is True


async def test_trade_roi_limit():
    lead, rest = Client("lead", price="95000"), Client("rest", price="5000")
    lead.positions_value = [position("BTC", "bid", "0.002")]
    rest.positions_value = [position("BTC", "ask", "0.002")]

    summary = await trade(lead, rest).state(make_cfg(position_roi_limit=0.8))

    assert summary.healthy is False
    assert summary.roi_breach is True
    assert summary.close_reason == "leg ROI hit 90.00%"


@pytest.mark.parametrize(
    ("lead_positions", "reason"),
    [([], "missing positions (1/2)"), ([position("BTC", "bid", "0.001")], "position size drift")],
    ids=["missing", "drift"],
)
async def test_unsafe_positions(lead_positions, reason):
    lead, rest = Client("lead"), Client("rest")
    lead.positions_value = lead_positions
    rest.positions_value = [position("BTC", "ask", "0.002")]

    summary = await trade(lead, rest).state(make_cfg())

    assert summary.healthy is False
    assert summary.close_reason == reason


@pytest.mark.parametrize(("healthy", "pnl"), [(False, "0"), (True, "20")], ids=["leg", "basket"])
async def test_monitor_risk(monkeypatch, healthy, pnl):
    summary = DeltaTradeSummary(
        symbol="BTC",
        total_pnl=Decimal(pnl),
        total_entry_cost=Decimal(100),
        combined_roi=Decimal(pnl) / Decimal(100),
        max_abs_leg_roi=Decimal("0.9") if not healthy else Decimal("0.2"),
        leg_count=2,
        open_leg_count=2,
        has_size_drift=False,
        roi_breach=not healthy,
        healthy=healthy,
    )

    class Trade:
        async def state(self, cfg):
            return summary

    strategy = DeltaStrategy(make_cfg(), [cast(TradingClient, Client("account"))])
    monkeypatch.setattr("strategy.cycle.asyncio.sleep", AsyncMock())

    assert await strategy.monitor_trades([Trade()], 1) is False


async def test_trade_leverage():
    lead, rest = Client("lead"), Client("rest")
    rest.leverage = 10

    await trade(lead, rest).check_leverage(10)

    assert lead.leverage == 10
    assert lead.calls == ["leverage:BTC:10"]
    assert rest.calls == []


async def test_trade_min_size():
    lead, rest = Client("lead"), Client("rest")
    lead.min_trade_usd = Decimal(101)

    with pytest.raises(RuntimeError, match="lead"):
        await trade(lead, rest).check_min_sizes()


async def test_limit_open(monkeypatch):
    lead, rest = Client("lead"), Client("rest")
    cfg = make_cfg(use_limit=True)
    filled = Order(
        id="limit",
        symbol="BTC",
        side="bid",
        size=Decimal("0.002"),
        filled=Decimal("0.002"),
        price=Decimal(50000),
        status=OrderStatus.FILLED,
    )
    fill = AsyncMock(return_value=filled)
    monkeypatch.setattr("strategy.trade._fill_limit_order", fill)

    await trade(lead, rest).open(cfg)

    fill.assert_awaited_once_with(lead, "BTC", "bid", Decimal("0.002"), cfg)
    assert lead.calls == []
    assert rest.calls == ["market:BTC:ask:0.002"]


async def test_limit_close(monkeypatch):
    lead, rest = Client("lead"), Client("rest")
    lead.positions_value = [position("BTC", "bid", "0.002"), position("ETH", "bid", "0.1")]
    rest.positions_value = [position("BTC", "ask", "0.002"), position("ETH", "ask", "0.1")]
    fill = AsyncMock()
    monkeypatch.setattr("strategy.trade._fill_limit_order", fill)

    await trade(lead, rest).close(make_cfg(), use_limit=True)

    fill.assert_awaited_once_with(
        lead,
        "BTC",
        "ask",
        Decimal("0.002"),
        ANY,
        reduce_only=True,
    )
    assert lead.calls == []
    assert rest.calls == ["close:BTC"]


async def test_symbols_once_per_exchange():
    class CountingClient(Client):
        def __init__(self, name: str, exchange: str):
            super().__init__(name)
            self.exchange = exchange
            self.symbols: list[str] = []

        async def get_lot_size(self, symbol: str) -> Decimal:
            self.symbols.append(symbol)
            return Decimal("0.001")

    first = CountingClient("first", "one")
    duplicate = CountingClient("duplicate", "one")
    second = CountingClient("second", "two")

    await _check_symbols(make_cfg(symbols=["BTC", "ETH", "BTC"]), [first, duplicate, second])

    assert first.symbols == ["BTC", "ETH"]
    assert duplicate.symbols == []
    assert second.symbols == ["BTC", "ETH"]


async def test_symbols_reject_exchange():
    class FailingClient(Client):
        exchange = "second"

        async def get_lot_size(self, symbol: str) -> Decimal:
            if symbol == "ETH":
                raise RuntimeError("missing")

            return Decimal("0.001")

    clients = [Client("first"), FailingClient("second")]

    with pytest.raises(AppError, match="ETH is not available on second"):
        await _check_symbols(make_cfg(symbols=["BTC", "ETH"]), clients)


def test_cfg_symbol_count():
    cfg = make_cfg(symbols=["BTC"], symbols_per_trade=2)
    clients = [Client("first"), Client("second")]

    with pytest.raises(AppError, match="requires exactly 2 symbols"):
        _check_cfg(cfg, clients)


async def test_strict_market_window(monkeypatch):
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    client = Client("account")
    checked: list[datetime] = []

    async def tradeable(symbol: str, at: datetime, reduce_only=False) -> bool:
        checked.append(at)
        return len(checked) == 1

    client.is_symbol_tradeable = tradeable
    strategy = DeltaStrategy(
        make_cfg(symbols=["BTC"], market_hours="strict", entry_gate_wait=5),
        [cast(TradingClient, client)],
    )
    monkeypatch.setattr("strategy.cycle.datetime", FrozenDateTime)

    assert await strategy._tradeable_symbols(30) == []
    assert checked == [now + timedelta(seconds=5), now + timedelta(seconds=35)]


@dataclass
class PlannedTrade:
    symbol: str
    gate_result: bool
    events: list[str]
    legs: list[DeltaLeg]

    async def load_qtys(self) -> None:
        self.events.append(f"{self.symbol}:prepare")

    async def check_min_sizes(self) -> None:
        return None

    async def check_leverage(self, leverage: int) -> None:
        return None

    async def log_plan(self) -> None:
        return None

    async def gate(self, cfg: StrategyConfig) -> bool:
        self.events.append(f"{self.symbol}:gate")
        return self.gate_result

    async def open(self, cfg: StrategyConfig) -> bool:
        self.events.append(f"{self.symbol}:open")
        return True

    async def state(self, cfg: StrategyConfig) -> DeltaTradeSummary:
        raise AssertionError("trade must not be monitored")

    async def close(self, cfg: StrategyConfig, use_limit=False) -> None:
        self.events.append(f"{self.symbol}:close:{use_limit}")


async def test_gates_before_opens(monkeypatch):
    clients = [Client("prime"), Client("hedge")]
    legs = [
        DeltaLeg(cast(TradingClient, clients[0]), "bid", Decimal(50)),
        DeltaLeg(cast(TradingClient, clients[1]), "ask", Decimal(50)),
    ]
    events: list[str] = []
    trades = [
        PlannedTrade("BTC", True, events, legs),
        PlannedTrade("ETH", False, events, legs),
    ]
    strategy = DeltaStrategy(
        make_cfg(symbols=["BTC", "ETH"], symbols_per_trade=2),
        cast(list[TradingClient], clients),
    )
    strategy.initial_bal = Decimal(2000)
    monkeypatch.setattr("strategy.cycle.random.sample", lambda values, count: list(values))
    monkeypatch.setattr("strategy.cycle.plan_delta_trades", AsyncMock(return_value=trades))

    await strategy.trade_cycle()

    assert events == ["BTC:prepare", "ETH:prepare", "BTC:gate", "ETH:gate"]


async def test_cycle_closes_trade(monkeypatch):
    clients = [Client("prime"), Client("hedge")]
    legs = [
        DeltaLeg(cast(TradingClient, clients[0]), "bid", Decimal(50)),
        DeltaLeg(cast(TradingClient, clients[1]), "ask", Decimal(50)),
    ]
    events: list[str] = []
    planned = PlannedTrade("BTC", True, events, legs)
    strategy = DeltaStrategy(make_cfg(use_limit=True), cast(list[TradingClient], clients))
    strategy.initial_bal = Decimal(2000)
    strategy.monitor_trades = AsyncMock(return_value=True)
    monkeypatch.setattr("strategy.cycle.random.sample", lambda values, count: list(values))
    monkeypatch.setattr("strategy.cycle.plan_delta_trades", AsyncMock(return_value=[planned]))
    monkeypatch.setattr("strategy.cycle.tg.on_trade_start", AsyncMock(return_value=1))
    monkeypatch.setattr("strategy.cycle.tg.on_trade_stop", AsyncMock())

    await strategy.trade_cycle()

    assert events == ["BTC:prepare", "BTC:gate", "BTC:open", "BTC:close:True"]


async def test_close_all_retries(monkeypatch):
    client = Client("account")
    client.cancel_all_orders = AsyncMock(side_effect=[RuntimeError("network"), 0])
    client.close_all_positions = AsyncMock(return_value=0)
    sleep = AsyncMock()
    monkeypatch.setattr("strategy.execution.asyncio.sleep", sleep)

    await close_all([cast(TradingClient, client)], _attempts=2)

    assert client.cancel_all_orders.await_count == 2
    assert client.close_all_positions.await_count == 2
    sleep.assert_awaited_once_with(2.0)


async def test_failure_closes_positions(monkeypatch):
    clients = cast(list[TradingClient], [Client("prime"), Client("hedge")])
    strategy = DeltaStrategy(make_cfg(max_failures=1), clients)
    strategy.trade_cycle = AsyncMock(side_effect=RuntimeError("exchange down"))
    close_all = AsyncMock()
    monkeypatch.setattr("strategy.cycle.close_all", close_all)
    monkeypatch.setattr("strategy.cycle.tg.on_crash", AsyncMock())

    await strategy.run()

    assert close_all.await_count == 2
