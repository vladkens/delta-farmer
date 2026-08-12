"""Tests for current delta trade lifecycle and orchestration behavior."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from lib.logger import logger
from strategy.cycle import DeltaStrategy
from strategy.execution import (
    EntryQuality,
    _simulate_book_fill,
    evaluate_entry_quality,
    fill_limit_order,
    wait_for_entry_quality,
)
from strategy.models import (
    Order,
    OrderBook,
    OrderStatus,
    Position,
    Side,
    StrategyConfig,
    TradingClient,
)
from strategy.trade import DeltaLeg, DeltaTrade, DeltaTradeSummary


async def _instant_sleep(_):
    """Replace asyncio.sleep with a no-op for fast tests."""


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


def make_order(id: str, symbol: str, side: Side, qty: Decimal) -> Order:
    return Order(
        id=id,
        symbol=symbol,
        side=side,
        size=qty,
        filled=qty,
        price=Decimal("50000"),
        status=OrderStatus.FILLED,
    )


def make_position(symbol: str, side: Side, size: str, entry: str = "50000") -> Position:
    return Position(
        id="p1", symbol=symbol, side=side, size=Decimal(size), entry_price=Decimal(entry)
    )


def make_book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> OrderBook:
    return OrderBook.build(bids=bids, asks=asks)


def make_cfg(**kw) -> StrategyConfig:
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
            **kw,
        }
    )


class MockClient(TradingClient):
    exchange = "mock"

    def __init__(self, name: str, balance: float = 1000, side: Side = "bid", price: float = 50000):
        self._name = name
        self._balance = Decimal(str(balance))
        self._side: Side = side
        self._price = Decimal(str(price))
        self._positions: list[Position] | None = None
        self.calls: list[str] = []
        self.tradeable_symbols: dict[str, bool] = {}
        self.tradeable_by_check: dict[tuple[str, bool], bool] = {}
        self.tradeable_by_time: dict[tuple[str, datetime | None, bool], bool] = {}
        self.tradeable_errors: dict[str, Exception] = {}
        self.tradeable_checks: list[tuple[str, datetime | None, bool]] = []
        self._leverage: int | None = None
        self._min_trade_usd: Decimal = Decimal(10)

    @property
    def name(self) -> str:
        return self._name

    def _rec(self, name: str) -> None:
        self.calls.append(name)

    async def login(self, *, force: bool = False):
        self._rec("force_login" if force else "login")

    async def registered(self):
        return True

    async def balance(self):
        self._rec("balance")
        return self._balance

    async def get_price(self, symbol: str):
        self._rec("get_price")
        return self._price

    async def get_bbo(self, symbol: str):
        return Decimal("49999"), Decimal("50001")

    async def get_order_book(self, symbol: str):
        return make_book([("49999", "10")], [("50001", "10")])

    async def get_lot_size(self, symbol: str):
        return Decimal("0.001")

    async def get_tick_size(self, symbol: str):
        return Decimal("1")

    async def get_min_trade_usd(self, symbol: str):
        return self._min_trade_usd

    async def get_leverage(self, symbol: str):
        self._rec("get_leverage")
        return self._leverage

    async def set_leverage(self, symbol: str, leverage: int):
        self._rec("set_leverage")
        self._leverage = leverage

    async def positions(self):
        self._rec("positions")
        if self._positions is not None:
            return self._positions
        return [make_position("BTC", self._side, "0.002")]

    async def close_position(self, position: Position):
        self._rec("close_position")
        return True

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False):
        self._rec("market_order")
        return make_order("ord-m", symbol, side, qty)

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ):
        self._rec("limit_order")
        return make_order("ord-l", symbol, side, qty)

    async def cancel_order(self, order: Order):
        self._rec("cancel_order")
        return True

    async def get_order(self, order_id: str):
        self._rec("get_order")
        return None

    async def cancel_all_orders(self):
        self._rec("cancel_all_orders")
        return 0

    async def close_all_positions(self):
        self._rec("close_all_positions")
        return 1

    async def get_symbols(self):
        return ["BTC"]

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        self.tradeable_checks.append((symbol, at, reduce_only))
        if symbol in self.tradeable_errors:
            raise self.tradeable_errors[symbol]
        if (symbol, at, reduce_only) in self.tradeable_by_time:
            return self.tradeable_by_time[(symbol, at, reduce_only)]
        if (symbol, reduce_only) in self.tradeable_by_check:
            return self.tradeable_by_check[(symbol, reduce_only)]
        return self.tradeable_symbols.get(symbol, True)


async def test_account_check_does_not_eagerly_login():
    from strategy.runner import _check_accounts

    clients = [MockClient("a"), MockClient("b")]

    await _check_accounts(clients)

    assert [client.calls for client in clients] == [[], []]


def test_check_cfg_rejects_symbol_pool_smaller_than_symbols_per_trade():
    from lib.errors import AppError
    from strategy.runner import _check_cfg

    cfg = make_cfg(symbols=["BTC"], symbols_per_trade=2)

    with pytest.raises(AppError, match="requires exactly 2 symbols"):
        _check_cfg(cfg, [MockClient("a"), MockClient("b")])


async def test_check_symbols_rejects_invalid_startup_symbol():
    from lib.errors import AppError
    from strategy.runner import _check_symbols

    class SymbolFailClient(MockClient):
        async def get_lot_size(self, symbol: str):
            if symbol == "SPI":
                raise RuntimeError("Symbol not found: symbol=SPI product_id=None")
            return await super().get_lot_size(symbol)

    cfg = make_cfg(symbols=["BTC", "SPI"])

    with pytest.raises(AppError) as exc:
        await _check_symbols(cfg, [SymbolFailClient("a"), MockClient("b")])

    msg = str(exc.value)
    assert msg == "Invalid configured symbols: SPI is not available on mock"
    assert "SymbolFailClient" not in msg
    assert "RuntimeError" not in msg
    assert "/a" not in msg


async def test_check_symbols_checks_unique_symbols_per_unique_exchange():
    from strategy.runner import _check_symbols

    class CountingClient(MockClient):
        def __init__(self, name: str, exchange: str):
            super().__init__(name)
            self.exchange = exchange
            self.seen_symbols: list[str] = []

        async def get_lot_size(self, symbol: str):
            self.seen_symbols.append(symbol)
            return await super().get_lot_size(symbol)

    ex1a = CountingClient("ex1a", "ex1")
    ex1b = CountingClient("ex1b", "ex1")
    ex2 = CountingClient("ex2", "ex2")
    cfg = make_cfg(symbols=["BTC", "ETH", "BTC"])

    await _check_symbols(cfg, [ex1a, ex1b, ex2])

    assert ex1a.seen_symbols == ["BTC", "ETH"]
    assert ex1b.seen_symbols == []
    assert ex2.seen_symbols == ["BTC", "ETH"]


async def test_filter_exchange_symbols_keeps_symbols_true_on_every_exchange():
    from strategy.symbols import filter_exchange_symbols

    class CountingClient(MockClient):
        def __init__(self, name: str, exchange: str):
            super().__init__(name)
            self.exchange = exchange
            self.seen_symbols: list[str] = []

    ex1a = CountingClient("ex1a", "ex1")
    ex1b = CountingClient("ex1b", "ex1")
    ex2 = CountingClient("ex2", "ex2")

    async def check(acc: TradingClient, symbol: str) -> bool:
        client = cast(CountingClient, acc)
        client.seen_symbols.append(symbol)
        return symbol != "ETH" or client.exchange != "ex2"

    result = await filter_exchange_symbols([ex1a, ex1b, ex2], ["BTC", "ETH", "BTC"], check)

    assert result == ["BTC"]
    assert ex1a.seen_symbols == ["BTC", "ETH"]
    assert ex1b.seen_symbols == []
    assert ex2.seen_symbols == ["BTC", "ETH"]


async def test_ensure_exchange_symbols_rejects_false_predicate():
    from lib.errors import AppError
    from strategy.symbols import ensure_exchange_symbols

    acc = MockClient("acc")

    async def check(_acc: TradingClient, symbol: str) -> bool:
        return symbol != "SPI"

    with pytest.raises(AppError, match="SPI is not available on mock"):
        await ensure_exchange_symbols([acc], ["BTC", "SPI"], check)


def make_trade(
    symbol: str = "BTC",
    lead_client: MockClient | None = None,
    rest_clients: list[MockClient] | None = None,
) -> DeltaTrade:
    lead_client = lead_client or MockClient("lead")
    rest_clients = rest_clients or [MockClient("rest", side="ask")]
    lead = DeltaLeg(cast(TradingClient, lead_client), "bid", Decimal("100"), qty=Decimal("0.002"))
    rest = [
        DeltaLeg(cast(TradingClient, client), "ask", Decimal("100"), qty=Decimal("0.002"))
        for client in rest_clients
    ]
    return DeltaTrade(symbol=symbol, lead=lead, rest=rest)


@dataclass
class FakeTrade:
    symbol: str
    summary: DeltaTradeSummary
    calls: list[str]
    close_calls: list[bool]
    legs: list[DeltaLeg]

    async def load_qtys(self) -> None:
        self.calls.append(f"{self.symbol}:load_qtys")

    async def check_min_sizes(self) -> None:
        self.calls.append(f"{self.symbol}:check_min_sizes")

    async def check_leverage(self, leverage: int) -> None:
        self.calls.append(f"{self.symbol}:check_leverage:{leverage}")

    async def log_plan(self) -> None:
        self.calls.append(f"{self.symbol}:log_plan")

    async def gate(self, cfg: StrategyConfig) -> bool:
        self.calls.append(f"{self.symbol}:gate")
        return True

    async def open(self, cfg: StrategyConfig) -> bool:
        self.calls.append(f"{self.symbol}:open")
        return True

    async def state(self, cfg: StrategyConfig) -> DeltaTradeSummary:
        self.calls.append(f"{self.symbol}:state")
        return self.summary

    async def close(self, cfg: StrategyConfig, use_limit=False) -> None:
        self.calls.append(f"{self.symbol}:close")
        self.close_calls.append(use_limit)


async def test_trade_check_leverage_sets_when_missing_or_wrong():
    a, b = MockClient("a"), MockClient("b")
    trade = make_trade(lead_client=a, rest_clients=[b])

    await trade.check_leverage(10)
    assert "set_leverage" in a.calls
    assert "set_leverage" in b.calls

    a.calls.clear()
    b.calls.clear()
    a._leverage = 10
    b._leverage = 10

    await trade.check_leverage(10)
    assert "set_leverage" not in a.calls
    assert "set_leverage" not in b.calls


async def test_trade_check_min_sizes_raises_for_failing_accounts():
    a, b = MockClient("a"), MockClient("b")
    trade = make_trade(lead_client=a, rest_clients=[b])
    a._min_trade_usd = Decimal(200)

    with pytest.raises(RuntimeError, match="a"):
        await trade.check_min_sizes()

    b._min_trade_usd = Decimal(200)
    with pytest.raises(RuntimeError) as exc:
        await trade.check_min_sizes()
    assert "a" in str(exc.value) and "b" in str(exc.value)


async def test_trade_open_market_mode():
    a, b = MockClient("a"), MockClient("b", side="ask")
    trade = make_trade(lead_client=a, rest_clients=[b])
    assert await trade.open(make_cfg(use_limit=False)) is True
    assert a.calls.count("market_order") == 1
    assert b.calls.count("market_order") == 1
    assert "limit_order" not in a.calls


async def test_trade_gate_market_mode_uses_wait_for_entry_quality(monkeypatch):
    a, b = MockClient("a"), MockClient("b", side="ask")
    trade = make_trade(lead_client=a, rest_clients=[b])
    seen: list[tuple[str, list[tuple[Side, Decimal]]]] = []

    async def gate_ok(client, symbol, legs, cfg):
        seen.append((symbol, legs))
        return object()

    monkeypatch.setattr("strategy.trade.wait_for_entry_quality", gate_ok)

    assert await trade.gate(make_cfg(use_limit=False)) is True
    assert seen == [("BTC", [("bid", Decimal("0.002")), ("ask", Decimal("0.002"))])]


async def test_trade_open_limit_mode_fills(monkeypatch):
    a, b = MockClient("a"), MockClient("b", side="ask")
    trade = make_trade(lead_client=a, rest_clients=[b])
    filled = make_order("ord-l", "BTC", "bid", Decimal("0.002"))

    async def fake_limit(*args, **kwargs):
        return filled

    monkeypatch.setattr("strategy.trade._fill_limit_order", fake_limit)

    assert await trade.open(make_cfg(use_limit=True)) is True
    assert "market_order" not in a.calls
    assert b.calls.count("market_order") == 1


async def test_trade_gate_limit_mode_skips_when_gate_times_out(monkeypatch):
    a, b = MockClient("a"), MockClient("b", side="ask")
    trade = make_trade(lead_client=a, rest_clients=[b])

    async def gate_none(*args, **kwargs):
        return None

    monkeypatch.setattr("strategy.trade.wait_for_entry_quality", gate_none)

    assert await trade.gate(make_cfg(use_limit=True)) is False


async def test_trade_open_limit_mode_fails(monkeypatch):
    trade = make_trade()

    async def fake_limit_fail(*args, **kwargs):
        return None

    monkeypatch.setattr("strategy.trade._fill_limit_order", fake_limit_fail)

    with pytest.raises(RuntimeError, match="Limit order failed"):
        await trade.open(make_cfg(use_limit=True))


async def test_trade_close_use_limit_closes_single_symbol(monkeypatch):
    lead, rest = MockClient("lead"), MockClient("rest", side="ask")
    lead._positions = [make_position("ETH", "ask", "0.002"), make_position("BTC", "ask", "0.002")]
    rest._positions = [make_position("ETH", "bid", "0.002"), make_position("BTC", "bid", "0.002")]
    trade = make_trade(symbol="ETH", lead_client=lead, rest_clients=[rest])
    seen: list[tuple[str, str, bool]] = []

    async def fake_fill(client, symbol, side, qty, cfg, reduce_only=False):
        seen.append((client.name, symbol, reduce_only))
        return make_order("ord-l", symbol, side, qty)

    monkeypatch.setattr("strategy.trade._fill_limit_order", fake_fill)

    await trade.close(make_cfg(), use_limit=True)
    assert seen == [("lead", "ETH", True)]
    assert rest.calls.count("close_position") == 1


async def test_trade_state_missing_position_is_unhealthy():
    a, b = MockClient("a"), MockClient("b")
    trade = make_trade(lead_client=a, rest_clients=[b])
    a._positions = []
    b._positions = [make_position("BTC", "ask", "0.002")]

    summary = await trade.state(make_cfg())
    assert summary.healthy is False
    assert summary.open_leg_count == 1
    assert summary.close_reason == "missing positions (1/2)"


async def test_trade_state_detects_size_drift():
    a, b = MockClient("a"), MockClient("b")
    trade = make_trade(lead_client=a, rest_clients=[b])
    a._positions = [make_position("BTC", "bid", "0.001")]
    b._positions = [make_position("BTC", "ask", "0.002")]

    summary = await trade.state(make_cfg())
    assert summary.healthy is False
    assert summary.has_size_drift is True
    assert summary.close_reason == "position size drift"


async def test_trade_state_detects_roi_breach():
    a, b = MockClient("a", price=95000), MockClient("b", side="ask", price=5000)
    trade = make_trade(lead_client=a, rest_clients=[b])
    a._positions = [make_position("BTC", "bid", "0.002", "50000")]
    b._positions = [make_position("BTC", "ask", "0.002", "50000")]

    summary = await trade.state(make_cfg(position_roi_limit=0.8))
    assert summary.healthy is False
    assert summary.roi_breach is True
    assert summary.close_reason == "leg ROI hit 90.00%"


async def test_trade_state_aggregates_totals():
    a, b = MockClient("a", price=55000), MockClient("b", side="ask", price=45000)
    trade = make_trade(lead_client=a, rest_clients=[b])
    a._positions = [make_position("BTC", "bid", "0.002", "50000")]
    b._positions = [make_position("BTC", "ask", "0.002", "50000")]

    summary = await trade.state(make_cfg())
    assert summary.total_pnl == Decimal("20")
    assert summary.total_entry_cost == Decimal("200")
    assert summary.combined_roi == Decimal("0.1")
    assert summary.healthy is True


async def test_monitor_trades_stop_event_exits_early():
    stop = asyncio.Event()
    stop.set()
    strategy = DeltaStrategy(make_cfg(trade_duration=[5, 5]), [MockClient("a")], stop_event=stop)

    result = await strategy.monitor_trades([], 5)
    assert result is False


async def test_monitor_trades_exits_on_unhealthy_summary(monkeypatch):
    strategy = DeltaStrategy(make_cfg(trade_duration=[5, 5]), [MockClient("a")])
    monkeypatch.setattr("strategy.cycle.asyncio.sleep", _instant_sleep)
    summary = DeltaTradeSummary(
        symbol="BTC",
        total_pnl=Decimal(0),
        total_entry_cost=Decimal(100),
        combined_roi=Decimal(0),
        max_abs_leg_roi=Decimal("0.9"),
        leg_count=2,
        open_leg_count=2,
        has_size_drift=False,
        roi_breach=True,
        healthy=False,
    )
    trade = FakeTrade("BTC", summary, [], [], [])

    result = await strategy.monitor_trades([trade], 5)
    assert result is False


async def test_monitor_trades_exits_on_combined_roi(monkeypatch):
    strategy = DeltaStrategy(
        make_cfg(trade_duration=[5, 5], combined_roi_limit=0.1),
        [MockClient("a")],
    )
    monkeypatch.setattr("strategy.cycle.asyncio.sleep", _instant_sleep)
    summary = DeltaTradeSummary(
        symbol="BTC",
        total_pnl=Decimal("20"),
        total_entry_cost=Decimal("100"),
        combined_roi=Decimal("0.2"),
        max_abs_leg_roi=Decimal("0.2"),
        leg_count=2,
        open_leg_count=2,
        has_size_drift=False,
        roi_breach=False,
        healthy=True,
    )
    trade = FakeTrade("BTC", summary, [], [], [])

    result = await strategy.monitor_trades([trade], 5)
    assert result is False


async def test_tradeable_symbols_auto_keeps_all_symbols_and_checks_open_window(monkeypatch):
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return base if tz is UTC else base.astimezone(tz)

    monkeypatch.setattr("strategy.cycle.datetime", FrozenDateTime)
    acc = MockClient("a")
    strategy = DeltaStrategy(
        make_cfg(symbols=["BTC", "ETH"], symbols_per_trade=1, limit_wait=2, entry_gate_wait=5),
        [acc],
    )

    result = await strategy._tradeable_symbols(30)

    assert result == ["BTC", "ETH"]
    assert acc.tradeable_checks == [
        ("BTC", base + timedelta(seconds=5), False),
        ("ETH", base + timedelta(seconds=5), False),
    ]


async def test_tradeable_symbols_auto_budgets_sequential_limit_baskets(monkeypatch):
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return base if tz is UTC else base.astimezone(tz)

    monkeypatch.setattr("strategy.cycle.datetime", FrozenDateTime)
    acc = MockClient("a")
    strategy = DeltaStrategy(
        make_cfg(
            symbols=["BTC", "ETH", "SOL", "DOGE"],
            symbols_per_trade=4,
            limit_wait=2,
            entry_gate_wait=5,
            use_limit=True,
        ),
        [acc],
    )

    result = await strategy._tradeable_symbols(30)

    assert result == ["BTC", "ETH", "SOL", "DOGE"]
    checks_by_symbol: dict[str, list[tuple[datetime | None, bool]]] = {}
    for symbol, at, reduce_only in acc.tradeable_checks:
        checks_by_symbol.setdefault(symbol, []).append((at, reduce_only))
    assert set(checks_by_symbol) == {"BTC", "ETH", "SOL", "DOGE"}
    assert all(
        checks
        == [
            (base + timedelta(seconds=1205), False),
        ]
        for checks in checks_by_symbol.values()
    )


async def test_tradeable_symbols_filters_unavailable():
    a = MockClient("a")
    a.tradeable_symbols["ETH"] = False
    strategy = DeltaStrategy(
        make_cfg(symbols=["BTC", "ETH", "SOL"], symbols_per_trade=1),
        [a],
    )

    result = await strategy._tradeable_symbols(30)

    assert result == ["BTC", "SOL"]


async def test_tradeable_symbols_auto_ignores_unavailable_close_window():
    acc = MockClient("a")
    acc.tradeable_by_check[("ETH", True)] = False
    strategy = DeltaStrategy(
        make_cfg(symbols=["BTC", "ETH"], symbols_per_trade=1),
        [acc],
    )

    result = await strategy._tradeable_symbols(30)

    assert result == ["BTC", "ETH"]
    assert not any(reduce_only for _symbol, _at, reduce_only in acc.tradeable_checks)


async def test_tradeable_symbols_strict_filters_unavailable_close_window(monkeypatch):
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return base if tz is UTC else base.astimezone(tz)

    monkeypatch.setattr("strategy.cycle.datetime", FrozenDateTime)
    acc = MockClient("a")
    close_at = base + timedelta(seconds=330)
    acc.tradeable_by_time[("ETH", close_at, False)] = False
    strategy = DeltaStrategy(
        make_cfg(symbols=["BTC", "ETH"], symbols_per_trade=1, market_hours="strict"),
        [acc],
    )

    result = await strategy._tradeable_symbols(30)

    assert result == ["BTC"]
    assert any(
        symbol == "ETH" and at == close_at for symbol, at, _reduce_only in acc.tradeable_checks
    )


async def test_tradeable_symbols_filters_closed_before_entry(monkeypatch):
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return base if tz is UTC else base.astimezone(tz)

    monkeypatch.setattr("strategy.cycle.datetime", FrozenDateTime)
    acc = MockClient("a")
    open_at = base + timedelta(seconds=300)
    acc.tradeable_by_time[("ETH", open_at, False)] = False
    strategy = DeltaStrategy(
        make_cfg(symbols=["BTC", "ETH"], symbols_per_trade=1),
        [acc],
    )

    result = await strategy._tradeable_symbols(30)

    assert result == ["BTC"]


async def test_tradeable_symbols_can_skip_market_hours_check():
    acc = MockClient("a")
    acc.tradeable_symbols["ETH"] = False
    strategy = DeltaStrategy(
        make_cfg(
            symbols=["BTC", "ETH", "BTC"],
            symbols_per_trade=1,
            market_hours="off",
        ),
        [acc],
    )

    result = await strategy._tradeable_symbols(30)

    assert result == ["BTC", "ETH"]
    assert acc.tradeable_checks == []


async def test_cycle_samples_from_all_tradeable_symbols(monkeypatch):
    acc = MockClient("a")
    strategy = DeltaStrategy(
        make_cfg(symbols=["BTC", "ETH"], symbols_per_trade=1),
        [acc],
    )
    strategy.initial_bal = Decimal("1000")
    sampled_from: list[str] = []
    planned_symbols: list[str] = []

    def fake_sample(seq, n):
        sampled_from.extend(seq)
        return ["ETH"]

    async def fake_plan(accounts, symbols, exp_usd, leverage, balances):
        planned_symbols.extend(symbols)
        return None

    monkeypatch.setattr("strategy.cycle.random.sample", fake_sample)
    monkeypatch.setattr("strategy.cycle.plan_delta_trades", fake_plan)

    await strategy.trade_cycle()

    assert sampled_from == ["BTC", "ETH"]
    assert planned_symbols == ["ETH"]


async def test_cycle_returns_early_when_too_few_symbols_tradeable(monkeypatch):
    acc = MockClient("a")
    acc.tradeable_symbols["ETH"] = False
    strategy = DeltaStrategy(
        make_cfg(symbols=["BTC", "ETH"], symbols_per_trade=2),
        [acc],
    )
    strategy.initial_bal = Decimal("1000")
    plan_called = False

    async def fake_plan(*args, **kwargs):
        nonlocal plan_called
        plan_called = True
        return None

    def fail_sample(seq, n):
        raise AssertionError("random.sample should not run when too few symbols are tradeable")

    monkeypatch.setattr("strategy.cycle.random.sample", fail_sample)
    monkeypatch.setattr("strategy.cycle.plan_delta_trades", fake_plan)

    await strategy.trade_cycle()

    assert plan_called is False


async def test_cycle_samples_duration_once_and_passes_to_monitor(monkeypatch):
    class CountingDuration:
        def __init__(self):
            self.calls = 0

        def sample(self):
            self.calls += 1
            return 42

    accs = [MockClient("prime"), MockClient("acc2")]
    cfg = make_cfg(use_limit=True)
    duration = CountingDuration()
    object.__setattr__(cfg, "trade_duration", duration)
    strategy = DeltaStrategy(cfg, accs)
    strategy.initial_bal = Decimal("2000")
    calls: list[str] = []
    close_calls: list[bool] = []
    legs = [
        DeltaLeg(cast(TradingClient, accs[0]), "bid", Decimal("50")),
        DeltaLeg(cast(TradingClient, accs[1]), "ask", Decimal("50")),
    ]
    summary = DeltaTradeSummary(
        symbol="BTC",
        total_pnl=Decimal("1"),
        total_entry_cost=Decimal("100"),
        combined_roi=Decimal("0.01"),
        max_abs_leg_roi=Decimal("0.01"),
        leg_count=2,
        open_leg_count=2,
        has_size_drift=False,
        roi_breach=False,
        healthy=True,
    )
    trade = FakeTrade("BTC", summary, calls, close_calls, legs)

    async def fake_plan(*args, **kwargs):
        return [trade]

    async def fake_monitor(self, trades, monitor_duration):
        calls.append(f"monitor:{monitor_duration}")
        return True

    monkeypatch.setattr("strategy.cycle.plan_delta_trades", fake_plan)
    monkeypatch.setattr("strategy.cycle.random.sample", lambda seq, n: list(seq)[:n])
    monkeypatch.setattr(DeltaStrategy, "monitor_trades", fake_monitor)

    await strategy.trade_cycle()

    assert duration.calls == 1
    assert "monitor:42" in calls


async def test_cycle_skips_when_no_valid_pair():
    a, b = MockClient("a", balance=0.01), MockClient("b", balance=1000)
    strategy = DeltaStrategy(make_cfg(), [a, b])
    strategy.initial_bal = Decimal("1000.01")

    await strategy.trade_cycle()
    assert "market_order" not in a.calls
    assert "market_order" not in b.calls


async def test_cycle_uses_trade_objects_and_limit_close(monkeypatch):
    accs = [MockClient("prime"), MockClient("acc2")]
    strategy = DeltaStrategy(make_cfg(use_limit=True), accs)
    strategy.initial_bal = Decimal("2000")
    calls: list[str] = []
    close_calls: list[bool] = []
    legs = [
        DeltaLeg(cast(TradingClient, accs[0]), "bid", Decimal("50")),
        DeltaLeg(cast(TradingClient, accs[1]), "ask", Decimal("50")),
    ]
    summary = DeltaTradeSummary(
        symbol="BTC",
        total_pnl=Decimal("1"),
        total_entry_cost=Decimal("100"),
        combined_roi=Decimal("0.01"),
        max_abs_leg_roi=Decimal("0.01"),
        leg_count=2,
        open_leg_count=2,
        has_size_drift=False,
        roi_breach=False,
        healthy=True,
    )
    trade = FakeTrade("BTC", summary, calls, close_calls, legs)

    async def fake_plan(*args, **kwargs):
        return [trade]

    async def fake_monitor(self, trades, duration):
        calls.append("monitor")
        return True

    monkeypatch.setattr("strategy.cycle.plan_delta_trades", fake_plan)
    monkeypatch.setattr("strategy.cycle.random.sample", lambda seq, n: list(seq)[:n])
    monkeypatch.setattr(DeltaStrategy, "monitor_trades", fake_monitor)

    await strategy.trade_cycle()
    assert calls == [
        "BTC:load_qtys",
        "BTC:check_min_sizes",
        "BTC:check_leverage:10",
        "BTC:log_plan",
        "BTC:gate",
        "BTC:open",
        "monitor",
        "BTC:close",
    ]
    assert close_calls == [True]


async def test_cycle_aborts_before_open_when_trade_check_fails(monkeypatch):
    accs = [MockClient("prime"), MockClient("acc2")]
    strategy = DeltaStrategy(make_cfg(), accs)
    strategy.initial_bal = Decimal("2000")
    trade = FakeTrade(
        "BTC",
        DeltaTradeSummary(
            symbol="BTC",
            total_pnl=Decimal(0),
            total_entry_cost=Decimal(0),
            combined_roi=Decimal(0),
            max_abs_leg_roi=Decimal(0),
            leg_count=2,
            open_leg_count=2,
            has_size_drift=False,
            roi_breach=False,
            healthy=True,
        ),
        [],
        [],
        [
            DeltaLeg(cast(TradingClient, accs[0]), "bid", Decimal("50")),
            DeltaLeg(cast(TradingClient, accs[1]), "ask", Decimal("50")),
        ],
    )

    async def fake_plan(*args, **kwargs):
        return [trade]

    async def boom():
        raise RuntimeError("min size fail")

    trade.check_min_sizes = boom  # type: ignore[method-assign]
    monkeypatch.setattr("strategy.cycle.plan_delta_trades", fake_plan)
    monkeypatch.setattr("strategy.cycle.random.sample", lambda seq, n: list(seq)[:n])

    with pytest.raises(RuntimeError, match="min size fail"):
        await strategy.trade_cycle()
    assert "BTC:open" not in trade.calls


async def test_cycle_skips_cleanly_when_first_trade_gate_blocks(monkeypatch):
    accs = [MockClient("prime"), MockClient("acc2")]
    strategy = DeltaStrategy(make_cfg(use_limit=True), accs)
    strategy.initial_bal = Decimal("2000")
    calls: list[str] = []
    close_calls: list[bool] = []
    trade = FakeTrade(
        "BTC",
        DeltaTradeSummary(
            symbol="BTC",
            total_pnl=Decimal(0),
            total_entry_cost=Decimal("100"),
            combined_roi=Decimal(0),
            max_abs_leg_roi=Decimal(0),
            leg_count=2,
            open_leg_count=2,
            has_size_drift=False,
            roi_breach=False,
            healthy=True,
        ),
        calls,
        close_calls,
        [
            DeltaLeg(cast(TradingClient, accs[0]), "bid", Decimal("50")),
            DeltaLeg(cast(TradingClient, accs[1]), "ask", Decimal("50")),
        ],
    )

    async def fake_plan(*args, **kwargs):
        return [trade]

    async def gate_false(cfg):
        calls.append("BTC:gate")
        return False

    trade.gate = gate_false  # type: ignore[method-assign]
    monkeypatch.setattr("strategy.cycle.plan_delta_trades", fake_plan)
    monkeypatch.setattr("strategy.cycle.random.sample", lambda seq, n: list(seq)[:n])

    await strategy.trade_cycle()
    assert calls == [
        "BTC:load_qtys",
        "BTC:check_min_sizes",
        "BTC:check_leverage:10",
        "BTC:log_plan",
        "BTC:gate",
    ]
    assert close_calls == []


async def test_cycle_skips_all_opens_when_any_gate_blocks(monkeypatch):
    accs = [MockClient("prime"), MockClient("acc2")]
    strategy = DeltaStrategy(
        make_cfg(use_limit=True, symbols=["BTC", "ETH"], symbols_per_trade=2), accs
    )
    strategy.initial_bal = Decimal("2000")
    calls: list[str] = []
    close_calls: list[bool] = []
    summary = DeltaTradeSummary(
        symbol="BTC",
        total_pnl=Decimal(0),
        total_entry_cost=Decimal("100"),
        combined_roi=Decimal(0),
        max_abs_leg_roi=Decimal(0),
        leg_count=2,
        open_leg_count=2,
        has_size_drift=False,
        roi_breach=False,
        healthy=True,
    )
    trade1 = FakeTrade(
        "BTC",
        summary,
        calls,
        close_calls,
        [
            DeltaLeg(cast(TradingClient, accs[0]), "bid", Decimal("50")),
            DeltaLeg(cast(TradingClient, accs[1]), "ask", Decimal("50")),
        ],
    )
    trade2 = FakeTrade(
        "ETH",
        DeltaTradeSummary(
            symbol="ETH",
            total_pnl=Decimal(0),
            total_entry_cost=Decimal("100"),
            combined_roi=Decimal(0),
            max_abs_leg_roi=Decimal(0),
            leg_count=2,
            open_leg_count=2,
            has_size_drift=False,
            roi_breach=False,
            healthy=True,
        ),
        calls,
        close_calls,
        [
            DeltaLeg(cast(TradingClient, accs[0]), "bid", Decimal("50")),
            DeltaLeg(cast(TradingClient, accs[1]), "ask", Decimal("50")),
        ],
    )

    async def fake_plan(*args, **kwargs):
        return [trade1, trade2]

    async def gate_true(cfg):
        calls.append("BTC:gate")
        return True

    async def gate_false(cfg):
        calls.append("ETH:gate")
        return False

    trade1.gate = gate_true  # type: ignore[method-assign]
    trade2.gate = gate_false  # type: ignore[method-assign]
    monkeypatch.setattr("strategy.cycle.plan_delta_trades", fake_plan)
    monkeypatch.setattr("strategy.cycle.random.sample", lambda seq, n: list(seq)[:n])

    await strategy.trade_cycle()
    assert calls == [
        "BTC:load_qtys",
        "BTC:check_min_sizes",
        "BTC:check_leverage:10",
        "BTC:log_plan",
        "ETH:load_qtys",
        "ETH:check_min_sizes",
        "ETH:check_leverage:10",
        "ETH:log_plan",
        "BTC:gate",
        "ETH:gate",
    ]
    assert close_calls == []


async def test_loop_closes_all_on_startup():
    a, b = MockClient("a"), MockClient("b")
    stop = asyncio.Event()
    strategy = DeltaStrategy(make_cfg(), [a, b], stop_event=stop)

    async def stop_after_cleanup():
        for _ in range(50):
            if "cancel_all_orders" in a.calls:
                stop.set()
                return
            await asyncio.sleep(0.05)

    task = asyncio.create_task(strategy.run())
    await stop_after_cleanup()
    try:
        await asyncio.wait_for(task, timeout=3)
    except (TimeoutError, asyncio.CancelledError):
        pass

    assert "cancel_all_orders" in a.calls


async def test_loop_closes_all_on_exception():
    a, b = MockClient("a"), MockClient("b")
    strategy = DeltaStrategy(make_cfg(max_failures=3), [a, b])
    strategy._wait = lambda _sec: asyncio.sleep(0)  # type: ignore[method-assign]

    async def boom():
        raise RuntimeError("exchange down")

    strategy.trade_cycle = boom  # type: ignore[method-assign]
    await strategy.run()
    assert "cancel_all_orders" in a.calls


async def test_limit_filled_immediately_no_polling():
    a = MockClient("a")
    result = await fill_limit_order(a, "BTC", "bid", Decimal("0.002"))
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert "get_order" not in a.calls


async def test_limit_open_get_order_none_raises_fatal(monkeypatch):
    from lib.errors import AppError

    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)

    async def open_limit(s, side, qty, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=qty,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    a.limit_order = open_limit  # type: ignore[method-assign]

    with pytest.raises(AppError, match="never appeared"):
        await fill_limit_order(a, "BTC", "bid", Decimal("0.002"), timeout=0)


async def test_limit_open_polls_until_filled(monkeypatch):
    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    call_count = 0

    async def open_limit(s, side, qty, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=qty,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    async def eventually_filled(oid):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return Order(
                id=oid,
                symbol="BTC",
                side="bid",
                size=Decimal("0.002"),
                filled=Decimal(0),
                price=Decimal("50000"),
                status=OrderStatus.OPEN,
            )
        return make_order(oid, "BTC", "bid", Decimal("0.002"))

    a.limit_order = open_limit  # type: ignore[method-assign]
    a.get_order = eventually_filled  # type: ignore[method-assign]

    result = await fill_limit_order(a, "BTC", "bid", Decimal("0.002"), timeout=60)
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert call_count >= 3


async def test_limit_canceled_by_exchange_raises(monkeypatch):
    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.002")

    async def open_limit(s, side, q, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=q,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    async def get_canceled(oid):
        return Order(
            id=oid,
            symbol="BTC",
            side="bid",
            size=qty,
            filled=Decimal(0),
            price=Decimal("50000"),
            status=OrderStatus.CANCELED,
        )

    a.limit_order = open_limit  # type: ignore[method-assign]
    a.get_order = get_canceled  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="canceled by exchange"):
        await fill_limit_order(a, "BTC", "bid", qty, use_market_fallback=True)
    assert "market_order" not in a.calls


async def test_limit_timeout_uses_market_fallback(monkeypatch):
    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.002")

    async def open_limit(s, side, q, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=q,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    async def still_open(oid):
        return Order(
            id=oid,
            symbol="BTC",
            side="bid",
            size=qty,
            filled=Decimal(0),
            price=Decimal("49999"),
            status=OrderStatus.OPEN,
        )

    bbo_calls = 0

    async def drifted_bbo(symbol):
        nonlocal bbo_calls
        bbo_calls += 1
        if bbo_calls == 1:
            return Decimal("49999"), Decimal("50001")
        return Decimal("49860"), Decimal("50140")

    a.limit_order = open_limit  # type: ignore[method-assign]
    a.get_order = still_open  # type: ignore[method-assign]
    a.get_bbo = drifted_bbo  # type: ignore[method-assign]

    result = await fill_limit_order(a, "BTC", "bid", qty, timeout=0, use_market_fallback=True)
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert "cancel_order" in a.calls
    assert "market_order" in a.calls


async def test_limit_timeout_no_fallback_raises(monkeypatch):
    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.002")

    async def open_limit(s, side, q, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=q,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    async def still_open(oid):
        return Order(
            id=oid,
            symbol="BTC",
            side="bid",
            size=qty,
            filled=Decimal(0),
            price=Decimal("49999"),
            status=OrderStatus.OPEN,
        )

    bbo_calls = 0

    async def drifted_bbo(symbol):
        nonlocal bbo_calls
        bbo_calls += 1
        if bbo_calls == 1:
            return Decimal("49999"), Decimal("50001")
        return Decimal("49860"), Decimal("50140")

    a.limit_order = open_limit  # type: ignore[method-assign]
    a.get_order = still_open  # type: ignore[method-assign]
    a.get_bbo = drifted_bbo  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="timed out"):
        await fill_limit_order(a, "BTC", "bid", qty, timeout=0, use_market_fallback=False)
    assert "cancel_order" in a.calls
    assert "market_order" not in a.calls


async def test_limit_stable_bbo_resets_timer(monkeypatch):
    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.002")
    get_order_calls = 0

    async def open_limit(s, side, q, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=q,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    async def fills_on_second(oid):
        nonlocal get_order_calls
        get_order_calls += 1
        if get_order_calls == 1:
            return Order(
                id=oid,
                symbol="BTC",
                side="bid",
                size=qty,
                filled=Decimal(0),
                price=Decimal("49999"),
                status=OrderStatus.OPEN,
            )
        return make_order(oid, "BTC", "bid", qty)

    a.limit_order = open_limit  # type: ignore[method-assign]
    a.get_order = fills_on_second  # type: ignore[method-assign]

    result = await fill_limit_order(a, "BTC", "bid", qty, timeout=0, use_market_fallback=True)
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert "cancel_order" not in a.calls
    assert "market_order" not in a.calls


async def test_limit_stable_bbo_wait_logs_use_total_elapsed(monkeypatch):
    a = MockClient("a")
    clock = FakeClock()
    monkeypatch.setattr("strategy.execution.time.time", clock.time)
    monkeypatch.setattr("strategy.execution.asyncio.sleep", clock.sleep)
    qty = Decimal("0.002")
    get_order_calls = 0
    logs: list[str] = []
    sink_id = logger.add(lambda msg: logs.append(str(msg)), format="{message}", level="DEBUG")

    async def open_limit(s, side, q, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=q,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    async def fills_after_stable_extension(oid):
        nonlocal get_order_calls
        get_order_calls += 1
        if get_order_calls < 4:
            return Order(
                id=oid,
                symbol="BTC",
                side="bid",
                size=qty,
                filled=Decimal(0),
                price=Decimal("49999"),
                status=OrderStatus.OPEN,
            )
        return make_order(oid, "BTC", "bid", qty)

    a.limit_order = open_limit  # type: ignore[method-assign]
    a.get_order = fills_after_stable_extension  # type: ignore[method-assign]

    try:
        result = await fill_limit_order(a, "BTC", "bid", qty, timeout=1, use_market_fallback=True)
    finally:
        logger.remove(sink_id)

    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert any(
        "Limit bid 0.002 BTC: waiting elapsed=2s (BBO stable drift=0.000%, retry 1/9)" in message
        for message in logs
    )
    assert any("Limit bid 0.002 BTC filled in 5s" in message for message in logs)


async def test_limit_wait_retries_zero_skips_stable_bbo_retry(monkeypatch):
    a = MockClient("a")
    clock = FakeClock()
    monkeypatch.setattr("strategy.execution.time.time", clock.time)
    monkeypatch.setattr("strategy.execution.asyncio.sleep", clock.sleep)
    qty = Decimal("0.002")
    logs: list[str] = []
    sink_id = logger.add(lambda msg: logs.append(str(msg)), format="{message}", level="DEBUG")

    async def open_limit(s, side, q, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=q,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    async def still_open(oid):
        return Order(
            id=oid,
            symbol="BTC",
            side="bid",
            size=qty,
            filled=Decimal(0),
            price=Decimal("49999"),
            status=OrderStatus.OPEN,
        )

    a.limit_order = open_limit  # type: ignore[method-assign]
    a.get_order = still_open  # type: ignore[method-assign]

    try:
        result = await fill_limit_order(
            a,
            "BTC",
            "bid",
            qty,
            timeout=1,
            use_market_fallback=True,
            max_wait_retries=0,
        )
    finally:
        logger.remove(sink_id)

    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert "cancel_order" in a.calls
    assert "market_order" in a.calls
    assert any("retries exhausted 0/0" in message for message in logs)
    assert not any("retry 1/" in message for message in logs)


def test_simulate_book_fill_single_level():
    levels = OrderBook.build(bids=[("100", "2")], asks=[("101", "3")]).asks

    result = _simulate_book_fill(levels, Decimal("2"))

    assert result == Decimal("101")


def test_simulate_book_fill_multiple_levels():
    levels = OrderBook.build(
        bids=[("100", "5")],
        asks=[("101", "1"), ("102", "2"), ("103", "5")],
    ).asks

    result = _simulate_book_fill(levels, Decimal("2.5"))

    assert result == Decimal("254") / Decimal("2.5")


def test_simulate_book_fill_not_enough_depth():
    levels = OrderBook.build(bids=[("100", "1")], asks=[("101", "1"), ("102", "1")]).asks

    result = _simulate_book_fill(levels, Decimal("3"))

    assert result is None


def test_simulate_book_fill_empty_side():
    result = _simulate_book_fill([], Decimal("1"))

    assert result is None


@pytest.mark.parametrize("qty", [Decimal(0), Decimal("-1")])
def test_simulate_book_fill_rejects_non_positive_qty(qty):
    with pytest.raises(ValueError, match="qty must be positive"):
        _simulate_book_fill([], qty)


def test_evaluate_entry_quality_good_symmetric_book():
    book = OrderBook.build(
        bids=[("99", "3"), ("98", "3")],
        asks=[("101", "3"), ("102", "3")],
    )

    result = evaluate_entry_quality(book, [("bid", Decimal("2")), ("ask", Decimal("2"))])

    assert result.avg_bid_price == Decimal("101")
    assert result.avg_ask_price == Decimal("99")
    assert result.entry_spread_pct == Decimal("2.020202020202020202020202020")


def test_evaluate_entry_quality_detects_asymmetric_cost():
    book = OrderBook.build(
        bids=[("99", "1"), ("95", "10")],
        asks=[("101", "1"), ("103", "10")],
    )

    result = evaluate_entry_quality(book, [("bid", Decimal("2")), ("ask", Decimal("2"))])

    assert result.avg_bid_price == Decimal("102")
    assert result.avg_ask_price == Decimal("97")
    assert result.entry_spread_pct == Decimal("5") / Decimal("97") * 100


def test_evaluate_entry_quality_marks_insufficient_depth():
    book = OrderBook.build(
        bids=[("99", "1")],
        asks=[("101", "1")],
    )

    result = evaluate_entry_quality(book, [("bid", Decimal("2")), ("ask", Decimal("1"))])

    assert result.avg_bid_price is None
    assert result.avg_ask_price == Decimal("99")
    assert result.entry_spread_pct is None


def test_evaluate_entry_quality_sums_multiple_legs_same_side():
    book = OrderBook.build(
        bids=[("99", "5"), ("98", "5")],
        asks=[("101", "5"), ("102", "5")],
    )

    result = evaluate_entry_quality(
        book,
        [("bid", Decimal("1")), ("bid", Decimal("2")), ("ask", Decimal("3"))],
    )

    assert result.avg_bid_price == Decimal("101")
    assert result.avg_ask_price == Decimal("99")
    assert result.entry_spread_pct == Decimal("2.020202020202020202020202020")


async def test_wait_for_entry_quality_disabled_returns_immediately():
    a = MockClient("a")

    result = await wait_for_entry_quality(
        a,
        "BTC",
        [("bid", Decimal("1")), ("ask", Decimal("1"))],
        make_cfg(max_entry_spread_pct=None),
    )

    assert result is not None
    assert result.avg_bid_price is None
    assert result.avg_ask_price is None
    assert result.entry_spread_pct is None


async def test_wait_for_entry_quality_returns_immediately_when_quality_ok():
    a = MockClient("a")

    result = await wait_for_entry_quality(
        a,
        "BTC",
        [("bid", Decimal("1")), ("ask", Decimal("1"))],
        make_cfg(max_entry_spread_pct=2.10),
    )

    assert result is not None
    assert result.entry_spread_pct == Decimal("0.004000080001600032000640012800")


async def test_wait_for_entry_quality_uses_client_estimator():
    class EstimatingClient(MockClient):
        async def estimate_entry_quality(self, symbol: str, legs: list[tuple[Side, Decimal]]):
            self._rec("estimate_entry_quality")
            return EntryQuality(
                avg_bid_price=Decimal("101"),
                avg_ask_price=Decimal("99"),
                entry_spread_pct=Decimal("2"),
            )

    a = EstimatingClient("a")

    result = await wait_for_entry_quality(
        a,
        "ETH",
        [("bid", Decimal("1")), ("ask", Decimal("1"))],
        make_cfg(max_entry_spread_pct=3),
    )

    assert result is not None
    assert result.entry_spread_pct == Decimal("2")
    assert "estimate_entry_quality" in a.calls
    assert "get_order_book" not in a.calls


async def test_wait_for_entry_quality_polls_until_quality_ok(monkeypatch):
    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    calls = 0

    async def changing_book(symbol: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            return make_book([("95", "1")], [("105", "1")])
        return make_book([("99.99", "10")], [("100.01", "10")])

    a.get_order_book = changing_book  # type: ignore[method-assign]
    result = await wait_for_entry_quality(
        a,
        "BTC",
        [("bid", Decimal("1")), ("ask", Decimal("1"))],
        make_cfg(max_entry_spread_pct=0.05, entry_gate_wait=1),
    )

    assert result is not None
    assert calls >= 2
    assert result.entry_spread_pct == Decimal("0.02000200020002000200020002000")


async def test_wait_for_entry_quality_times_out(monkeypatch):
    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)

    async def bad_book(symbol: str):
        return make_book([("90", "0.1")], [("110", "0.1")])

    a.get_order_book = bad_book  # type: ignore[method-assign]
    result = await wait_for_entry_quality(
        a,
        "BTC",
        [("bid", Decimal("1")), ("ask", Decimal("1"))],
        make_cfg(max_entry_spread_pct=0.10, entry_gate_wait=0),
    )

    assert result is None


async def test_wait_for_entry_quality_logs_insufficient_depth(monkeypatch):
    a = MockClient("a")
    logs: list[str] = []
    sink_id = logger.add(lambda msg: logs.append(str(msg)), format="{message}", level="DEBUG")

    async def shallow_book(symbol: str):
        return make_book([("99", "0.5")], [("101", "0.25")])

    a.get_order_book = shallow_book  # type: ignore[method-assign]

    try:
        result = await wait_for_entry_quality(
            a,
            "ETH",
            [("bid", Decimal("1")), ("ask", Decimal("2"))],
            make_cfg(max_entry_spread_pct=0.10, entry_gate_wait=0),
        )
    finally:
        logger.remove(sink_id)

    assert result is None
    assert any(
        "ETH gate waiting spread=n/a reason=insufficient asks depth 0.25/1, "
        "bids depth 0.5/2" in message
        for message in logs
    )
    assert any(
        "ETH gate skip spread=n/a reason=insufficient asks depth 0.25/1, "
        "bids depth 0.5/2 timeout=0s" in message
        for message in logs
    )


async def test_omni_estimates_entry_quality_from_trade_size_quote():
    from clients.omni import OmniClient

    class Quote:
        bid = Decimal("99")
        ask = Decimal("101")

        def __init__(self, qty: Decimal):
            self.qty = qty

    client = object.__new__(OmniClient)
    quote_calls: list[Decimal] = []

    async def quote(symbol: str, qty: Decimal):
        quote_qty = Decimal(str(qty))
        quote_calls.append(quote_qty)
        return Quote(quote_qty)

    client._quote = quote  # type: ignore[method-assign]

    result = await client.estimate_entry_quality(
        "ETH",
        [("bid", Decimal("0.56773")), ("ask", Decimal("0.56773"))],
    )

    assert quote_calls == [Decimal("0.56773")]
    assert result.avg_bid_price == Decimal("101")
    assert result.avg_ask_price == Decimal("99")
    assert result.entry_spread_pct == Decimal("2") / Decimal("99") * 100
    assert result.bid_depth == Decimal("0.56773")
    assert result.ask_depth == Decimal("0.56773")
