import asyncio
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

from lib.errors import AppError
from lib.http import ApiError
from strategy.execution import (
    EntryQuality,
    combine_entry_quality,
    evaluate_entry_quality,
    fill_limit_order,
    wait_for_entry_quality,
    wait_for_legs_entry_quality,
)
from strategy.models import Order, OrderBook, OrderStatus, Side, StrategyConfig, TradingClient
from strategy.trade import DeltaLeg, DeltaTrade


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
    def __init__(self, books: list[OrderBook], name="book", exchange="book"):
        self.name = name
        self.exchange = exchange
        self.books = books
        self.calls = 0

    async def get_order_book(self, symbol: str) -> OrderBook:
        book = self.books[min(self.calls, len(self.books) - 1)]
        self.calls += 1
        return book


class LogSpy:
    """Captures what the gate logs, through the account-bound logger it uses."""

    def __init__(self):
        self.messages: list[str] = []
        self.levels: list[str] = []  # level of each line in `messages`, in the same order
        self.warnings: list[str] = []
        self.accounts: list[str | None] = []

    def bind(self, account: str | None = None, **_) -> "LogSpy":
        self.accounts.append(account)
        return self

    def debug(self, msg: str) -> None:
        self.levels.append("debug")
        self.messages.append(msg)

    def info(self, msg: str) -> None:
        self.levels.append("info")
        self.messages.append(msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)


def leg(client, side: Side, qty: str) -> tuple[TradingClient, Side, Decimal]:
    return cast(TradingClient, client), side, Decimal(qty)


async def no_sleep(_):
    raise AssertionError("gate waited instead of combining the venue books")


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
        exchange = "estimator"

        def __init__(self):
            self.calls = 0

        async def estimate_entry_quality(self, symbol, legs):
            self.calls += 1
            return EntryQuality(
                Decimal(101), Decimal(99), Decimal(2), bid_qty=Decimal(1), ask_qty=Decimal(1)
            )

    client = Estimator()
    quality = await wait_for_entry_quality(
        client,
        "ETH",
        [("bid", Decimal(1)), ("ask", Decimal(1))],
        entry_cfg("3"),
    )

    assert quality is not None and quality.entry_spread_pct == Decimal(2)
    assert client.calls == 1


async def test_entry_gate_on_one_client_matches_its_own_book():
    # Every leg on one client: the gate must report exactly what that single book simulates.
    book = OrderBook.build(
        bids=[("49998.5", "0.4"), ("49990", "3")],
        asks=[("50001.5", "0.4"), ("50010", "3")],
    )
    legs: list[tuple[Side, Decimal]] = [
        ("bid", Decimal("0.7")),
        ("ask", Decimal("0.3")),
        ("ask", Decimal("0.4")),
    ]
    client = BookClient([book])

    quality = await wait_for_legs_entry_quality(
        [leg(client, side, str(qty)) for side, qty in legs], "BTC", entry_cfg("0.1")
    )

    assert quality == evaluate_entry_quality(book, legs)
    assert client.calls == 1


async def test_entry_gate_combines_both_venue_books(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", no_sleep)
    # Each venue holds one leg; the other side of its own book is far away and must not be priced.
    rh = BookClient([OrderBook.build(bids=[("90", "5")], asks=[("100.5", "5")])], "rh", "lighter")
    core = BookClient(
        [OrderBook.build(bids=[("99.5", "5")], asks=[("110", "5")])], "core", "lighter_core"
    )

    quality = await wait_for_legs_entry_quality(
        [leg(rh, "bid", "2"), leg(core, "ask", "2")], "BTC", entry_cfg("1.5")
    )

    assert quality is not None
    assert (quality.avg_bid_price, quality.avg_ask_price) == (Decimal("100.5"), Decimal("99.5"))
    assert quality.entry_spread_pct == Decimal(1) / Decimal("99.5") * 100  # the venues' basis
    assert (rh.calls, core.calls) == (1, 1)


async def test_entry_gate_measures_one_leg_per_client(monkeypatch):
    # A per-client gate sees one side per book, so its spread is always None: it would wait out
    # entry_gate_wait and skip every cycle. Two accounts on one venue are the ordinary pool.
    # They share one book, so it is read once, by the first of them.
    monkeypatch.setattr("strategy.execution.asyncio.sleep", no_sleep)
    book = OrderBook.build(bids=[("99.95", "5")], asks=[("100.05", "5")])
    acc1 = BookClient([book], "acc1", "mock")
    acc2 = BookClient([book], "acc2", "mock")

    quality = await wait_for_legs_entry_quality(
        [leg(acc1, "bid", "2"), leg(acc2, "ask", "2")], "BTC", entry_cfg("0.2", wait=300)
    )
    one_book = evaluate_entry_quality(book, [("bid", Decimal(2)), ("ask", Decimal(2))])

    assert quality is not None
    assert quality.entry_spread_pct == one_book.entry_spread_pct  # same books, same cost
    assert (quality.avg_bid_price, quality.avg_ask_price) == (
        one_book.avg_bid_price,
        one_book.avg_ask_price,
    )
    assert (acc1.calls, acc2.calls) == (1, 0)


def test_entry_quality_combines_qty_weighted():
    parts = [
        EntryQuality(Decimal(100), None, None, bid_qty=Decimal(1)),
        EntryQuality(Decimal(104), None, None, bid_qty=Decimal(3)),
        EntryQuality(None, Decimal(99), None, ask_qty=Decimal(4)),
    ]

    quality = combine_entry_quality(parts)

    assert quality.avg_bid_price == Decimal(103)  # qty-weighted, not the plain mean 102
    assert quality.avg_ask_price == Decimal(99)
    assert quality.entry_spread_pct == Decimal(4) / Decimal(99) * 100
    assert (quality.bid_qty, quality.ask_qty) == (Decimal(4), Decimal(4))


async def test_entry_gate_skips_when_one_venue_is_thin(monkeypatch):
    spy = LogSpy()
    monkeypatch.setattr("strategy.execution.logger", spy)
    rh = BookClient([OrderBook.build(bids=[("99", "5")], asks=[("101", "5")])], "rh", "lighter")
    core = BookClient(
        [OrderBook.build(bids=[("99.5", "0.5")], asks=[("100.5", "5")])], "core", "lighter_core"
    )

    quality = await wait_for_legs_entry_quality(
        [leg(rh, "bid", "2"), leg(core, "ask", "2")], "BTC", entry_cfg("1", wait=0)
    )

    assert quality is None
    reason = "reason=lighter_core: insufficient bids depth 0.5/2"
    assert spy.messages == [
        f"BTC gate waiting spread=n/a {reason}",
        f"BTC gate skip spread=n/a {reason} timeout=0s",
    ]


async def test_entry_gate_refuses_a_side_one_venue_cannot_fill():
    # Two venues share the ask side: the deep one must not stand in for the thin one's legs.
    rh = BookClient([OrderBook.build(bids=[("99", "9")], asks=[("101", "9")])], "rh", "lighter")
    core = BookClient(
        [OrderBook.build(bids=[("99.5", "9")], asks=[("100.5", "9")])], "core", "lighter_core"
    )
    n1 = BookClient([OrderBook.build(bids=[("99.4", "0.5")], asks=[("100.6", "9")])], "n1", "n1")

    quality = await wait_for_legs_entry_quality(
        [leg(rh, "bid", "4"), leg(core, "ask", "2"), leg(n1, "ask", "2")],
        "BTC",
        entry_cfg("2", wait=0),  # the deep venue alone would be inside the limit
    )

    assert quality is None


async def test_entry_gate_asks_each_venue_only_for_its_own_legs(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", no_sleep)

    class QuoteClient:
        name = "omni"
        exchange = "omni"

        def __init__(self):
            self.legs: list[tuple[Side, Decimal]] = []

        async def estimate_entry_quality(self, symbol, legs) -> EntryQuality:
            self.legs = list(legs)
            return EntryQuality(None, Decimal("99.8"), None, ask_qty=Decimal(2))

        async def get_order_book(self, symbol) -> OrderBook:
            raise AssertionError("an estimator must not be priced from a book")

    quoted = QuoteClient()
    booked = BookClient([OrderBook.build(bids=[("99", "5")], asks=[("100.2", "5")])], "n1", "n1")

    quality = await wait_for_legs_entry_quality(
        [leg(booked, "bid", "2"), leg(quoted, "ask", "2")], "ETH", entry_cfg("0.5")
    )

    assert quality is not None
    assert (quality.avg_bid_price, quality.avg_ask_price) == (Decimal("100.2"), Decimal("99.8"))
    assert quoted.legs == [("ask", Decimal(2))]  # not the whole basket
    assert booked.calls == 1


async def test_trade_gate_prices_every_leg_on_its_own_client(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", no_sleep)
    rh = BookClient([OrderBook.build(bids=[("90", "5")], asks=[("100.5", "5")])], "rh", "lighter")
    core = BookClient(
        [OrderBook.build(bids=[("99.5", "5")], asks=[("110", "5")])], "core", "lighter_core"
    )
    trade = DeltaTrade(
        symbol="BTC",
        lead=DeltaLeg(cast(TradingClient, rh), "bid", Decimal(201), Decimal(2)),
        rest=[DeltaLeg(cast(TradingClient, core), "ask", Decimal(199), Decimal(2))],
    )

    assert await trade.gate(entry_cfg("1.5")) is True
    assert (rh.calls, core.calls) == (1, 1)


async def test_entry_gate_prices_one_exchange_from_its_single_book(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", no_sleep)
    # Three accounts on one venue share one book and `open()` fires their legs into it at once:
    # 0.1 of bids eats 0.05 at 100000 and 0.05 at 100600, not 0.05 at 100000 twice.
    book = OrderBook.build(bids=[("100000", "5")], asks=[("100000", "0.05"), ("100600", "5")])
    accs = [BookClient([book], f"rh{i}", "lighter") for i in range(3)]
    legs: list[tuple[Side, Decimal]] = [
        ("ask", Decimal("0.1")),
        ("bid", Decimal("0.05")),
        ("bid", Decimal("0.05")),
    ]

    quality = await wait_for_legs_entry_quality(
        [leg(acc, side, str(qty)) for acc, (side, qty) in zip(accs, legs)],
        "BTC",
        entry_cfg("1"),
    )

    assert quality == evaluate_entry_quality(book, legs)
    assert quality is not None and quality.avg_bid_price == Decimal(100300)
    assert [acc.calls for acc in accs] == [1, 0, 0]  # one book, read once by its first account


async def test_entry_gate_blocks_the_impact_two_accounts_would_hide():
    # Same basket at the default max_entry_spread_pct: its real cost is 0.30%, and pricing each
    # account at the top of the book reports 0.00% and opens a position that pays the 0.30%.
    book = OrderBook.build(bids=[("100000", "5")], asks=[("100000", "0.05"), ("100600", "5")])
    accs = [BookClient([book], f"rh{i}", "lighter") for i in range(3)]

    quality = await wait_for_legs_entry_quality(
        [leg(accs[0], "ask", "0.1"), leg(accs[1], "bid", "0.05"), leg(accs[2], "bid", "0.05")],
        "BTC",
        entry_cfg("0.25", wait=0),
    )

    assert quality is None


async def test_entry_gate_quotes_one_exchange_once_for_every_leg(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", no_sleep)

    class QuoteClient:
        """Like `clients/omni.py`: one quote sized from the legs it is handed, so small quotes
        price better than the aggregate the orders will actually hit."""

        exchange = "omni"

        def __init__(self, name: str):
            self.name = name
            self.asked: list[list[tuple[Side, Decimal]]] = []

        async def estimate_entry_quality(self, symbol, legs) -> EntryQuality:
            self.asked.append(list(legs))
            bid_qty = sum((qty for side, qty in legs if side == "bid"), Decimal(0))
            ask_qty = sum((qty for side, qty in legs if side == "ask"), Decimal(0))
            qty = max(bid_qty, ask_qty)
            bid_price, ask_price = Decimal(100) + qty, Decimal(100) - qty
            return EntryQuality(
                bid_price,
                ask_price,
                (bid_price - ask_price) / ask_price * 100,
                bid_qty=bid_qty,
                ask_qty=ask_qty,
            )

    accs = [QuoteClient("om1"), QuoteClient("om2"), QuoteClient("om3")]

    quality = await wait_for_legs_entry_quality(
        [leg(accs[0], "bid", "0.5"), leg(accs[1], "bid", "0.5"), leg(accs[2], "ask", "1")],
        "ETH",
        entry_cfg("5"),
    )

    assert quality is not None
    assert quality.avg_bid_price == Decimal(101)  # quoted for 1, not 0.5 twice
    assert [acc.asked for acc in accs] == [
        [[("bid", Decimal("0.5")), ("bid", Decimal("0.5")), ("ask", Decimal(1))]],  # one quote
        [],
        [],
    ]


async def test_entry_gate_passes_a_favourable_cross_venue_basis(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", no_sleep)
    # Buying on rh costs less than selling on core pays: entering earns the basis instead of
    # paying it, so there is nothing to wait for.
    rh = BookClient([OrderBook.build(bids=[("98", "5")], asks=[("99.5", "5")])], "rh", "lighter")
    core = BookClient(
        [OrderBook.build(bids=[("100.5", "5")], asks=[("102", "5")])], "core", "lighter_core"
    )

    quality = await wait_for_legs_entry_quality(
        [leg(rh, "bid", "2"), leg(core, "ask", "2")], "BTC", entry_cfg("0.25")
    )

    assert quality is not None
    assert (quality.avg_bid_price, quality.avg_ask_price) == (Decimal("99.5"), Decimal("100.5"))
    assert quality.entry_spread_pct == Decimal(-1) / Decimal("100.5") * 100


def test_entry_cost_is_signed_so_a_gain_is_not_a_cost():
    cheap_buy = EntryQuality(Decimal("99.5"), None, None, bid_qty=Decimal(2))
    rich_sell = EntryQuality(None, Decimal("100.5"), None, ask_qty=Decimal(2))

    quality = combine_entry_quality([cheap_buy, rich_sell])

    assert quality.entry_spread_pct == Decimal(-1) / Decimal("100.5") * 100
    assert quality.entry_spread_pct is not None and quality.entry_spread_pct < 0


async def test_gate_log_reads_a_negative_cost_as_a_gain(monkeypatch):
    spy = LogSpy()
    monkeypatch.setattr("strategy.execution.logger", spy)
    rh = BookClient([OrderBook.build(bids=[("98", "5")], asks=[("99.5", "5")])], "rh", "lighter")
    core = BookClient(
        [OrderBook.build(bids=[("100.5", "5")], asks=[("102", "5")])], "core", "lighter_core"
    )

    # A negative limit demands a basis gain. `max_entry_spread_pct` is `gt=0` in the config, so
    # only a test asks for one; it is the one way to see the gate render a negative cost.
    quality = await wait_for_legs_entry_quality(
        [leg(rh, "bid", "2"), leg(core, "ask", "2")], "BTC", entry_cfg("-2", wait=0)
    )

    assert quality is None
    assert spy.messages == [
        "BTC gate waiting spread=-1.00% (gain)",
        "BTC gate skip spread=-1.00% (gain) timeout=0s",
    ]


async def test_entry_gate_waits_out_a_venue_that_cannot_answer(monkeypatch):
    spy = LogSpy()
    monkeypatch.setattr("strategy.execution.logger", spy)

    class BrokenClient:
        name = "core"
        exchange = "lighter_core"

        async def get_order_book(self, symbol: str) -> OrderBook:
            raise ApiError("GET /orderBook: 503 overloaded")

    rh = BookClient([OrderBook.build(bids=[("99", "5")], asks=[("101", "5")])], "rh", "lighter")

    # No position exists yet, so one transient read must not raise into the cycle's close_all and
    # failure backoff. The gate waits, and skips this cycle if the venue stays unavailable.
    quality = await wait_for_legs_entry_quality(
        [leg(rh, "bid", "2"), leg(BrokenClient(), "ask", "2")], "BTC", entry_cfg("1", wait=0)
    )

    assert quality is None
    assert rh.calls == 1  # the healthy book is still read
    reason = "reason=lighter_core: GET /orderBook: 503 overloaded"
    assert spy.messages == [
        f"BTC gate waiting spread=n/a {reason}",
        f"BTC gate skip spread=n/a {reason} timeout=0s",
    ]
    assert spy.warnings == ["BTC gate: book unavailable, GET /orderBook: 503 overloaded"]
    assert spy.accounts == ["rh", "core"]  # the warning carries the venue that failed


async def test_entry_gate_passes_a_shutdown_through(monkeypatch):
    monkeypatch.setattr("strategy.execution.logger", LogSpy())

    class StoppedClient:
        name = "core"
        exchange = "lighter_core"

        async def get_order_book(self, symbol: str) -> OrderBook:
            raise asyncio.CancelledError  # the runner cancelling the cycle, not a venue problem

    rh = BookClient([OrderBook.build(bids=[("99", "5")], asks=[("101", "5")])], "rh", "lighter")

    with pytest.raises(asyncio.CancelledError):
        await wait_for_legs_entry_quality(
            [leg(rh, "bid", "2"), leg(StoppedClient(), "ask", "2")], "BTC", entry_cfg("1", wait=0)
        )


async def test_entry_gate_prices_a_venue_that_answers_on_a_later_poll(monkeypatch):
    monkeypatch.setattr("strategy.execution.asyncio.sleep", instant_sleep)

    class FlakyClient:
        name = "core"
        exchange = "lighter_core"

        def __init__(self):
            self.calls = 0

        async def get_order_book(self, symbol: str) -> OrderBook:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError

            return OrderBook.build(bids=[("99.5", "5")], asks=[("110", "5")])

    rh = BookClient(
        [OrderBook.build(bids=[("90", "5")], asks=[("100.5", "5")])] * 2, "rh", "lighter"
    )
    core = FlakyClient()

    quality = await wait_for_legs_entry_quality(
        [leg(rh, "bid", "2"), leg(core, "ask", "2")], "BTC", entry_cfg("1.5", wait=30)
    )

    assert quality is not None
    assert quality.entry_spread_pct == Decimal(1) / Decimal("99.5") * 100
    assert (rh.calls, core.calls) == (2, 2)


async def test_entry_gate_never_fills_a_failing_venue_from_a_healthy_one(monkeypatch):
    monkeypatch.setattr("strategy.execution.logger", LogSpy())

    class BrokenClient:
        name = "n1"
        exchange = "n1"

        async def get_order_book(self, symbol: str) -> OrderBook:
            raise ApiError("GET /book: 429")

    # Both venues hold ask legs and the deep one alone would be inside the limit.
    rh = BookClient([OrderBook.build(bids=[("99", "9")], asks=[("101", "9")])], "rh", "lighter")
    core = BookClient(
        [OrderBook.build(bids=[("100.9", "9")], asks=[("101", "9")])], "core", "lighter_core"
    )

    quality = await wait_for_legs_entry_quality(
        [leg(rh, "bid", "4"), leg(core, "ask", "2"), leg(BrokenClient(), "ask", "2")],
        "BTC",
        entry_cfg("1", wait=0),
    )

    assert quality is None


async def test_single_venue_gate_reason_keeps_its_wording(monkeypatch):
    spy = LogSpy()
    monkeypatch.setattr("strategy.execution.logger", spy)
    book = OrderBook.build(bids=[("99", "5")], asks=[])
    expected = [
        "BTC gate waiting spread=n/a reason=insufficient asks depth 0/2",
        "BTC gate skip spread=n/a reason=insufficient asks depth 0/2 timeout=0s",
    ]

    one = BookClient([book], "rh1", "lighter")
    assert (
        await wait_for_legs_entry_quality(
            [leg(one, "bid", "2"), leg(one, "ask", "2")], "BTC", entry_cfg("0.25", wait=0)
        )
        is None
    )
    assert spy.messages == expected  # one venue names no venue: today's exact line

    spy.messages.clear()
    accs = [BookClient([book], "rh1", "lighter"), BookClient([book], "rh2", "lighter")]
    assert (
        await wait_for_legs_entry_quality(
            [leg(accs[0], "bid", "2"), leg(accs[1], "ask", "2")], "BTC", entry_cfg("0.25", wait=0)
        )
        is None
    )
    assert spy.messages == expected  # two accounts, still one book, still the same line


async def test_a_skipped_gate_is_logged_above_debug(monkeypatch):
    # A skipped gate means this part, and with it the cycle, traded nothing. Both outcome lines
    # used to be debug, so at LOGURU_LEVEL=INFO a pool whose entry cost never clears the cap said
    # nothing at all.
    # The waiting line stays at debug: it is a normal thin market, and the gate is still waiting.
    spy = LogSpy()
    monkeypatch.setattr("strategy.execution.logger", spy)
    wide = OrderBook.build(bids=[("90", "9")], asks=[("110", "9")])
    one = BookClient([wide], "rh1", "lighter")

    quality = await wait_for_legs_entry_quality(
        [leg(one, "bid", "2"), leg(one, "ask", "2")], "BTC", entry_cfg("0.25", wait=0)
    )

    assert quality is None
    assert spy.levels == ["debug", "info"]
    assert spy.messages[1] == "BTC gate skip spread=22.22% timeout=0s"


async def test_a_passing_gate_stays_quiet(monkeypatch):
    # The quiet market is the normal one: a gate that passes must not add a line per cycle.
    spy = LogSpy()
    monkeypatch.setattr("strategy.execution.logger", spy)
    tight = OrderBook.build(bids=[("100", "9")], asks=[("100", "9")])
    one = BookClient([tight], "rh1", "lighter")

    quality = await wait_for_legs_entry_quality(
        [leg(one, "bid", "2"), leg(one, "ask", "2")], "BTC", entry_cfg("0.25", wait=0)
    )

    assert quality is not None
    assert spy.messages == [] and spy.warnings == []


def test_combine_refuses_a_price_without_a_qty():
    # An estimator that prices a side without reporting its qty must not vanish from the fold:
    # dropping it would price that side off the other books and move the gate by any amount.
    parts = [
        EntryQuality(Decimal(100), None, None, bid_qty=Decimal(1)),
        EntryQuality(Decimal(120), None, None),
    ]

    with pytest.raises(ValueError, match="bid price 120"):
        combine_entry_quality(parts)


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
