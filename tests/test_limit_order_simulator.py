"""Scenario tests for limit-order repricing against a scripted exchange."""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

import pytest

from strategy.execution import fill_limit_order
from strategy.models import Order, OrderBook, OrderStatus, Position, Side, TradingClient


async def _instant_sleep(_):
    """Replace asyncio.sleep with a no-op for fast tests."""


@dataclass(frozen=True)
class PollStep:
    status: OrderStatus = OrderStatus.OPEN
    filled: Decimal = Decimal(0)


@dataclass(frozen=True)
class LimitRequest:
    order_id: str
    side: Side
    qty: Decimal
    price: Decimal
    reduce_only: bool


@dataclass(frozen=True)
class MarketRequest:
    side: Side
    qty: Decimal
    reduce_only: bool


class ScriptedBookClient(TradingClient):
    exchange = "scripted"

    def __init__(
        self,
        *,
        bbo: Iterable[tuple[str, str]],
        order_polls: dict[str, list[PollStep]],
    ) -> None:
        self._name = "scripted"
        self._bbo = [(Decimal(bid), Decimal(ask)) for bid, ask in bbo]
        self._last_bbo = self._bbo[-1]
        self._order_polls = {order_id: list(steps) for order_id, steps in order_polls.items()}
        self._orders: dict[str, Order] = {}
        self._next_order = 1
        self.limit_requests: list[LimitRequest] = []
        self.cancel_requests: list[str] = []
        self.market_requests: list[MarketRequest] = []

    @property
    def name(self) -> str:
        return self._name

    async def login(self, *, force: bool = False) -> None:
        return None

    async def balance(self) -> Decimal:
        return Decimal("1000")

    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        if self._bbo:
            self._last_bbo = self._bbo.pop(0)
        return self._last_bbo

    async def get_order_book(self, symbol: str) -> OrderBook:
        bid, ask = await self.get_bbo(symbol)
        return OrderBook.build(bids=[(bid, "10")], asks=[(ask, "10")])

    async def get_price(self, symbol: str) -> Decimal:
        bid, ask = await self.get_bbo(symbol)
        return (bid + ask) / 2

    async def get_lot_size(self, symbol: str) -> Decimal:
        return Decimal("0.0001")

    async def get_tick_size(self, symbol: str) -> Decimal:
        return Decimal("1")

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return Decimal("1")

    async def positions(self) -> list[Position]:
        return []

    async def close_position(self, position: Position) -> bool:
        return True

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        self.market_requests.append(MarketRequest(side, qty, bool(reduce_only)))
        return Order(
            id="market-1",
            symbol=symbol,
            side=side,
            size=qty,
            filled=qty,
            price=None,
            status=OrderStatus.FILLED,
            reduce_only=reduce_only,
        )

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        order_id = f"ord-{self._next_order}"
        self._next_order += 1
        self.limit_requests.append(LimitRequest(order_id, side, qty, price, bool(reduce_only)))
        order = Order(
            id=order_id,
            symbol=symbol,
            side=side,
            size=qty,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
            reduce_only=reduce_only,
        )
        self._orders[order_id] = order
        return order

    async def cancel_order(self, order: Order) -> bool:
        self.cancel_requests.append(order.id)
        self._orders[order.id] = order.model_copy(update={"status": OrderStatus.CANCELED})
        return True

    async def get_order(self, order_id: str) -> Order | None:
        order = self._orders[order_id]
        steps = self._order_polls.get(order_id)
        if steps:
            step = steps.pop(0)
            order = order.model_copy(update={"status": step.status, "filled": step.filled})
            self._orders[order_id] = order
        return order

    async def cancel_all_orders(self) -> int:
        return 0

    async def close_all_positions(self) -> int:
        return 0

    async def registered(self) -> bool:
        return True

    async def get_symbols(self) -> list[str]:
        return ["BTC"]

    async def is_symbol_tradeable(self, symbol, at, reduce_only=False) -> bool:
        return True

    async def get_leverage(self, symbol: str) -> int | None:
        return 10

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        return None


async def test_scripted_book_reprices_drifted_limit_before_fallback(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.002")
    client = ScriptedBookClient(
        bbo=[("49999", "50001"), ("49860", "50140")],
        order_polls={
            "ord-1": [PollStep()],
            "ord-2": [PollStep(OrderStatus.FILLED, qty)],
        },
    )

    result = await fill_limit_order(
        client, "BTC", "bid", qty, timeout=0, use_market_fallback=True, max_wait_retries=1
    )

    assert result is not None
    assert result.id == "ord-2"
    assert result.status == OrderStatus.FILLED
    assert [req.price for req in client.limit_requests] == [Decimal("49999"), Decimal("49860")]
    assert client.cancel_requests == ["ord-1"]
    assert client.market_requests == []


async def test_scripted_book_reprices_only_remaining_partial_qty(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.0020")
    partial = Decimal("0.0007")
    remaining = qty - partial
    client = ScriptedBookClient(
        bbo=[("49999", "50001"), ("49860", "50140")],
        order_polls={
            "ord-1": [PollStep(OrderStatus.OPEN, partial)],
            "ord-2": [PollStep(OrderStatus.FILLED, remaining)],
        },
    )

    result = await fill_limit_order(
        client, "BTC", "bid", qty, timeout=-1, use_market_fallback=True, max_wait_retries=1
    )

    assert result is not None
    assert result.id == "ord-2"
    assert [req.qty for req in client.limit_requests] == [qty, remaining]
    assert client.cancel_requests == ["ord-1"]
    assert client.market_requests == []


async def test_scripted_book_uses_market_fallback_after_reprice_retries_exhausted(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.002")
    client = ScriptedBookClient(
        bbo=[("49999", "50001"), ("49860", "50140"), ("49700", "50300")],
        order_polls={
            "ord-1": [PollStep()],
            "ord-2": [PollStep()],
        },
    )

    result = await fill_limit_order(
        client, "BTC", "bid", qty, timeout=0, use_market_fallback=True, max_wait_retries=1
    )

    assert result is not None
    assert result.id == "market-1"
    assert [req.price for req in client.limit_requests] == [Decimal("49999"), Decimal("49860")]
    assert client.cancel_requests == ["ord-1", "ord-2"]
    assert client.market_requests == [MarketRequest("bid", qty, False)]


async def test_scripted_book_no_market_fallback_after_reprice_retries_exhausted(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.002")
    client = ScriptedBookClient(
        bbo=[("49999", "50001"), ("49860", "50140"), ("49700", "50300")],
        order_polls={
            "ord-1": [PollStep()],
            "ord-2": [PollStep()],
        },
    )

    with pytest.raises(RuntimeError, match="no fallback"):
        await fill_limit_order(
            client, "BTC", "bid", qty, timeout=0, use_market_fallback=False, max_wait_retries=1
        )

    assert [req.price for req in client.limit_requests] == [Decimal("49999"), Decimal("49860")]
    assert client.cancel_requests == ["ord-1", "ord-2"]
    assert client.market_requests == []


async def test_scripted_book_reprice_preserves_reduce_only(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.002")
    client = ScriptedBookClient(
        bbo=[("49999", "50001"), ("49860", "50140")],
        order_polls={
            "ord-1": [PollStep()],
            "ord-2": [PollStep(OrderStatus.FILLED, qty)],
        },
    )

    result = await fill_limit_order(
        client,
        "BTC",
        "ask",
        qty,
        reduce_only=True,
        timeout=0,
        use_market_fallback=True,
        max_wait_retries=1,
    )

    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert [req.reduce_only for req in client.limit_requests] == [True, True]
    assert client.market_requests == []
