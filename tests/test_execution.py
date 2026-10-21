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
        self.cancel_takes = True  # False: the exchange keeps the order open after a cancel
        self.fill_on_cancel = Decimal(0)  # a fill that lands while the cancel is in flight

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
        if self.cancel_takes:
            current = self.orders[order.id]
            filled = max(current.filled, self.fill_on_cancel)
            update = {"status": OrderStatus.CANCELED, "filled": filled}
            self.orders[order.id] = current.model_copy(update=update)
        return self.cancel_takes

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


class Clock:
    """Fake time for the limit loop: sleeping advances it instantly."""

    def __init__(self):
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    async def sleep(self, sec: float) -> None:
        self.now += sec


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr("strategy.execution.time", SimpleNamespace(time=fake.time))
    monkeypatch.setattr("strategy.execution.asyncio.sleep", fake.sleep)
    return fake


async def test_limit_follows_market_that_left(clock):
    qty = Decimal("0.002")
    client = ScriptedClient(
        [("49999", "50001"), ("50010", "50012")],  # best bid moves 11 up, spread is 2
        {"order-2": [Poll(OrderStatus.FILLED, qty)]},
    )

    result = await fill_limit_order(client, "BTC", "bid", qty, timeout=40)

    assert result is not None and result.id == "order-2"
    assert [request[2] for request in client.limit_requests] == [Decimal(49999), Decimal(50010)]
    assert client.canceled == ["order-1"]
    assert client.market_requests == []


async def test_limit_stays_while_market_moves_within_spread(clock):
    qty = Decimal("0.002")
    client = ScriptedClient(
        [("49999", "50001"), ("50000", "50002")],  # one tick away, inside the spread
        {"order-1": [Poll()] * 20 + [Poll(OrderStatus.FILLED, qty)]},  # past the first window
    )

    result = await fill_limit_order(client, "BTC", "bid", qty, timeout=40)

    assert result is not None and result.id == "order-1"
    assert len(client.limit_requests) == 1
    assert client.canceled == []


async def test_limit_moves_at_most_once_per_window(clock):
    qty = Decimal("0.002")
    runaway = [(str(50000 + 20 * i), str(50002 + 20 * i)) for i in range(100)]
    client = ScriptedClient([("49999", "50001"), *runaway], {})

    result = await fill_limit_order(client, "BTC", "bid", qty, timeout=40, max_wait_retries=3)

    assert result is not None and result.id == "market"
    assert 2 <= len(client.limit_requests) <= 4  # 4 windows of 40s: at most one move each
    assert client.market_requests == [("bid", qty, False)]


async def test_limit_counts_fill_that_landed_during_cancel(clock):
    qty = Decimal("0.0020")
    partial = Decimal("0.0007")
    client = ScriptedClient(
        [("49999", "50001"), ("50010", "50012")],
        {"order-2": [Poll(OrderStatus.FILLED, qty - partial)]},
    )
    client.fill_on_cancel = partial  # never seen by a poll, only after the cancel

    result = await fill_limit_order(client, "BTC", "bid", qty, timeout=40)

    assert result is not None and result.id == "order-2"
    assert [request[1] for request in client.limit_requests] == [qty, qty - partial]


async def test_limit_timeout_goes_to_market(clock):
    qty = Decimal("0.002")
    client = ScriptedClient([("49999", "50001")], {})

    result = await fill_limit_order(client, "BTC", "bid", qty, timeout=4, max_wait_retries=0)

    assert result is not None and result.id == "market"
    assert client.market_requests == [("bid", qty, False)]
    assert client.canceled == ["order-1"]
    assert len(client.limit_requests) == 1


async def test_limit_waits_every_window_before_market(clock):
    # limit_wait 15s with 99 retries: about 25 minutes at the best price before paying the spread.
    qty = Decimal("0.002")
    client = ScriptedClient([("49999", "50001")], {})

    await fill_limit_order(client, "BTC", "bid", qty, timeout=15, max_wait_retries=99)

    assert clock.now - 1000 >= 15 * 100
    assert client.market_requests == [("bid", qty, False)]


async def test_limit_without_fallback_waits_for_liquidity(clock):
    # No volume for far longer than limit_wait: never pay the spread, keep waiting at the best price.
    qty = Decimal("0.002")
    client = ScriptedClient(
        [("49999", "50001")], {"order-1": [Poll()] * 200 + [Poll(OrderStatus.FILLED, qty)]}
    )

    result = await fill_limit_order(client, "BTC", "bid", qty, timeout=4, use_market_fallback=False)

    assert result is not None and result.id == "order-1"
    assert clock.now - 1000 > 100 * 4
    assert client.market_requests == []
    assert client.canceled == []


async def test_limit_timeout_sends_only_the_remainder(clock):
    qty = Decimal("0.0020")
    partial = Decimal("0.0005")
    client = ScriptedClient([("49999", "50001")], {})
    client.fill_on_cancel = partial  # reported only by the re-read after the timeout cancel

    await fill_limit_order(client, "BTC", "bid", qty, timeout=4, max_wait_retries=0)

    assert client.market_requests == [("bid", qty - partial, False)]


async def test_limit_keeps_reduce_only(clock):
    qty = Decimal("0.002")
    client = ScriptedClient(
        [("49999", "50001"), ("49988", "49990")],  # best ask moves 11 down, away from a sell
        {"order-2": [Poll(OrderStatus.FILLED, qty)]},
    )

    await fill_limit_order(client, "BTC", "ask", qty, reduce_only=True, timeout=40)

    assert [(request[2], request[3]) for request in client.limit_requests] == [
        (Decimal(50001), True),
        (Decimal(49990), True),
    ]


async def test_limit_unknown(clock):
    client = ScriptedClient([("49999", "50001")], {"order-1": [None] * 10})

    with pytest.raises(AppError, match="unknown state"):
        await fill_limit_order(client, "BTC", "bid", Decimal("0.002"), timeout=4)

    assert client.canceled == []
    assert client.market_requests == []


async def test_limit_canceled(clock):
    client = ScriptedClient(
        [("49999", "50001")],
        {"order-1": [Poll(OrderStatus.CANCELED)]},
    )

    with pytest.raises(RuntimeError, match="canceled by exchange"):
        await fill_limit_order(client, "BTC", "bid", Decimal("0.002"))

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


async def test_limit_never_replaces_an_order_still_open_after_cancel(clock):
    # The cancel did not take: placing the new price now could fill twice.
    client = ScriptedClient([("49999", "50001"), ("50010", "50012")], {"order-1": [Poll()]})
    client.cancel_takes = False

    with pytest.raises(AppError, match="still open after cancel"):
        await fill_limit_order(client, "BTC", "bid", Decimal("0.002"), timeout=40)

    assert len(client.limit_requests) == 1
    assert client.market_requests == []


async def test_limit_visibility_is_timed_from_the_latest_placement(clock):
    # A long wait without fallback, then a move: a brief index lag on the new order is fine.
    qty = Decimal("0.002")
    client = ScriptedClient(
        [("49999", "50001"), *[("49999", "50001")] * 60, ("50010", "50012")],
        {"order-2": [None, Poll(OrderStatus.FILLED, qty)]},
    )

    result = await fill_limit_order(client, "BTC", "bid", qty, timeout=4, use_market_fallback=False)

    assert result is not None and result.id == "order-2"
