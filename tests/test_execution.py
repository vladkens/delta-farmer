from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

from lib.errors import AppError
from strategy.execution import (
    EntryQuality,
    evaluate_entry_quality,
    fill_limit_order,
    wait_for_entry_quality,
)
from strategy.models import Order, OrderBook, OrderStatus, Side, StrategyConfig


async def instant_sleep(_):
    return None


@dataclass(frozen=True)
class Poll:
    status: OrderStatus = OrderStatus.OPEN
    filled: Decimal = Decimal(0)


class ScriptedClient:
    name = "scripted"

    def __init__(
        self,
        bbo: list[tuple[str, str]],
        polls: dict[str, list[Poll | None]],
    ):
        self.bbo = [(Decimal(bid), Decimal(ask)) for bid, ask in bbo]
        self.last_bbo = self.bbo[-1]
        self.polls = {order_id: list(steps) for order_id, steps in polls.items()}
        self.orders: dict[str, Order] = {}
        self.limit_requests: list[tuple[Side, Decimal, Decimal, bool]] = []
        self.market_requests: list[tuple[Side, Decimal, bool]] = []
        self.canceled: list[str] = []

    async def get_tick_size(self, symbol: str) -> Decimal:
        return Decimal(1)

    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        if self.bbo:
            self.last_bbo = self.bbo.pop(0)
        return self.last_bbo

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        order_id = f"order-{len(self.limit_requests) + 1}"
        self.limit_requests.append((side, qty, price, bool(reduce_only)))
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
        self.orders[order_id] = order
        return order

    async def get_order(self, order_id: str) -> Order | None:
        order = self.orders[order_id]
        steps = self.polls.get(order_id)
        if steps:
            step = steps.pop(0)
            if step is None:
                return None

            order = order.model_copy(update={"status": step.status, "filled": step.filled})
            self.orders[order_id] = order
        return order

    async def cancel_order(self, order: Order) -> bool:
        self.canceled.append(order.id)
        return True

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        self.market_requests.append((side, qty, bool(reduce_only)))
        return Order(
            id="market",
            symbol=symbol,
            side=side,
            size=qty,
            filled=qty,
            price=None,
            status=OrderStatus.FILLED,
            reduce_only=reduce_only,
        )


class BookClient:
    name = "book"

    def __init__(self, books: list[OrderBook]):
        self.books = books
        self.calls = 0

    async def get_order_book(self, symbol: str) -> OrderBook:
        book = self.books[min(self.calls, len(self.books) - 1)]
        self.calls += 1
        return book


def entry_cfg(spread: str | None, wait=1) -> StrategyConfig:
    return cast(
        StrategyConfig,
        SimpleNamespace(
            max_entry_spread_pct=Decimal(spread) if spread is not None else None,
            entry_gate_wait=wait,
            entry_gate_poll=1,
        ),
    )


async def test_limit_reprices(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", instant_sleep)
    qty = Decimal("0.002")
    client = ScriptedClient(
        [("49999", "50001"), ("49860", "50140")],
        {
            "order-1": [Poll()],
            "order-2": [Poll(OrderStatus.FILLED, qty)],
        },
    )

    result = await fill_limit_order(client, "BTC", "bid", qty, timeout=0, max_wait_retries=1)

    assert result is not None and result.id == "order-2"
    assert [request[2] for request in client.limit_requests] == [
        Decimal(49999),
        Decimal(49860),
    ]
    assert client.canceled == ["order-1"]
    assert client.market_requests == []


async def test_limit_reprices_remainder(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", instant_sleep)
    qty = Decimal("0.0020")
    partial = Decimal("0.0007")
    client = ScriptedClient(
        [("49999", "50001"), ("49860", "50140")],
        {
            "order-1": [Poll(filled=partial)],
            "order-2": [Poll(OrderStatus.FILLED, qty - partial)],
        },
    )

    result = await fill_limit_order(client, "BTC", "bid", qty, timeout=-1, max_wait_retries=1)

    assert result is not None and result.id == "order-2"
    assert [request[1] for request in client.limit_requests] == [qty, qty - partial]


@pytest.mark.parametrize("fallback", [False, True], ids=["off", "on"])
async def test_limit_fallback(monkeypatch, fallback):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", instant_sleep)
    qty = Decimal("0.002")
    client = ScriptedClient(
        [("49999", "50001"), ("49860", "50140"), ("49700", "50300")],
        {"order-1": [Poll()], "order-2": [Poll()]},
    )

    if fallback:
        result = await fill_limit_order(
            client,
            "BTC",
            "bid",
            qty,
            timeout=0,
            use_market_fallback=True,
            max_wait_retries=1,
        )
        assert result is not None and result.id == "market"
        assert client.market_requests == [("bid", qty, False)]
    else:
        with pytest.raises(RuntimeError, match="no fallback"):
            await fill_limit_order(
                client,
                "BTC",
                "bid",
                qty,
                timeout=0,
                use_market_fallback=False,
                max_wait_retries=1,
            )
        assert client.market_requests == []

    assert client.canceled == ["order-1", "order-2"]


async def test_limit_keeps_reduce_only(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", instant_sleep)
    qty = Decimal("0.002")
    client = ScriptedClient(
        [("49999", "50001"), ("49860", "50140")],
        {
            "order-1": [Poll()],
            "order-2": [Poll(OrderStatus.FILLED, qty)],
        },
    )

    await fill_limit_order(
        client,
        "BTC",
        "ask",
        qty,
        reduce_only=True,
        timeout=0,
        max_wait_retries=1,
    )

    assert [request[3] for request in client.limit_requests] == [True, True]


async def test_limit_unknown(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", instant_sleep)
    client = ScriptedClient([("49999", "50001")], {"order-1": [None]})

    with pytest.raises(AppError, match="unknown state"):
        await fill_limit_order(client, "BTC", "bid", Decimal("0.002"), timeout=-1)

    assert client.canceled == []
    assert client.market_requests == []


async def test_limit_canceled(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", instant_sleep)
    client = ScriptedClient(
        [("49999", "50001")],
        {"order-1": [Poll(OrderStatus.CANCELED)]},
    )

    with pytest.raises(RuntimeError, match="canceled by exchange"):
        await fill_limit_order(client, "BTC", "bid", Decimal("0.002"))

    assert client.market_requests == []


async def test_limit_waits_on_stable_bbo(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", instant_sleep)
    qty = Decimal("0.002")
    client = ScriptedClient(
        [("49999", "50001"), ("49999", "50001")],
        {"order-1": [Poll(), Poll(OrderStatus.FILLED, qty)]},
    )

    result = await fill_limit_order(client, "BTC", "bid", qty, timeout=0, max_wait_retries=1)

    assert result is not None and result.status == OrderStatus.FILLED
    assert client.canceled == []
    assert client.market_requests == []


def test_entry_quality_weighted():
    book = OrderBook.build(
        bids=[("99", "1"), ("95", "10")],
        asks=[("101", "1"), ("103", "10")],
    )

    quality = evaluate_entry_quality(book, [("bid", Decimal(2)), ("ask", Decimal(2))])

    assert quality.avg_bid_price == Decimal(102)
    assert quality.avg_ask_price == Decimal(97)
    assert quality.entry_spread_pct == Decimal(5) / Decimal(97) * 100


def test_entry_quality_no_depth():
    book = OrderBook.build(bids=[("99", "1")], asks=[("101", "1")])

    quality = evaluate_entry_quality(book, [("bid", Decimal(2)), ("ask", Decimal(1))])

    assert quality.avg_bid_price is None
    assert quality.avg_ask_price == Decimal(99)
    assert quality.entry_spread_pct is None


async def test_entry_gate_disabled():
    client = BookClient([])

    quality = await wait_for_entry_quality(client, "BTC", [], entry_cfg(None))

    assert quality is not None
    assert client.calls == 0


async def test_entry_gate_polls(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", instant_sleep)
    client = BookClient(
        [
            OrderBook.build(bids=[("90", "2")], asks=[("110", "2")]),
            OrderBook.build(bids=[("99.99", "2")], asks=[("100.01", "2")]),
        ]
    )

    quality = await wait_for_entry_quality(
        client,
        "BTC",
        [("bid", Decimal(1)), ("ask", Decimal(1))],
        entry_cfg("0.05"),
    )

    assert quality is not None
    assert client.calls == 2


async def test_entry_gate_timeout():
    client = BookClient([OrderBook.build(bids=[("99", "0.5")], asks=[("101", "0.5")])])

    quality = await wait_for_entry_quality(
        client,
        "BTC",
        [("bid", Decimal(1)), ("ask", Decimal(1))],
        entry_cfg("0.1", wait=0),
    )

    assert quality is None
    assert client.calls == 1


async def test_entry_gate_estimator():
    class Estimator:
        name = "estimator"

        def __init__(self):
            self.calls = 0

        async def estimate_entry_quality(self, symbol, legs):
            self.calls += 1
            return EntryQuality(Decimal(101), Decimal(99), Decimal(2))

    client = Estimator()
    quality = await wait_for_entry_quality(
        client,
        "ETH",
        [("bid", Decimal(1)), ("ask", Decimal(1))],
        entry_cfg("3"),
    )

    assert quality is not None and quality.entry_spread_pct == Decimal(2)
    assert client.calls == 1
