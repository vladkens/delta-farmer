import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from rich.console import Console
from rich.table import Table

from lib.errors import AppError
from lib.http import ApiError
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
    usd_to_qty,
)
from strategy.runner import _check_cfg, _check_symbols, _prepare_groups, print_positions
from strategy.trade import DeltaLeg, DeltaTrade, DeltaTradeSummary, basket_lot_size


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

    def __init__(self, name: str, *, price: str = "50000", lot: str = "0.001"):
        self.name = name
        self.price = Decimal(price)
        self.lot = Decimal(lot)
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
        return self.lot

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


def basket(clients: list[Client], sizes: list[str]) -> DeltaTrade:
    """A lead bid against one or more ask hedges, as plan_delta_trades lays them out."""
    sides: list[Side] = ["bid"] + ["ask"] * (len(clients) - 1)
    legs = [
        DeltaLeg(cast(TradingClient, client), side, Decimal(size))
        for client, side, size in zip(clients, sides, sizes, strict=True)
    ]
    return DeltaTrade("BTC", legs[0], legs[1:])


def lead_sized_qtys(sides: list[Side], sizes: list[str], price: str, lot: str) -> list[Decimal]:
    """Sizing as it was before Task 4: the lead's price and lot applied to every leg."""
    qtys = [usd_to_qty(Decimal(size), Decimal(price), Decimal(lot)) for size in sizes]
    bid = sum(qty for qty, side in zip(qtys, sides) if side == "bid")
    ask = sum(qty for qty, side in zip(qtys, sides) if side == "ask")
    delta = bid - ask
    if delta:
        larger: Side = "bid" if delta > 0 else "ask"
        last = max(i for i, side in enumerate(sides) if side == larger)
        if abs(delta) < qtys[last]:
            qtys[last] -= abs(delta)

    return qtys


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


async def test_hedges_follow_the_lead_after_hedge_delay(monkeypatch):
    lead, rest = Client("lead"), Client("rest")
    events: list[str] = []
    lead.calls, rest.calls = events, events

    async def sleep(sec: float) -> None:
        events.append(f"sleep:{sec}")

    monkeypatch.setattr("strategy.trade.asyncio.sleep", sleep)

    await trade(lead, rest).open(make_cfg(use_limit=False, hedge_delay=[7, 7]))

    assert events == ["market:BTC:bid:0.002", "sleep:7", "market:BTC:ask:0.002"]


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
    [
        ([], "missing positions (1/2)"),
        ([position("BTC", "bid", "0.001")], "position size drift"),
        ([position("BTC", "ask", "0.002")], "position size drift"),
    ],
    ids=["missing", "drift", "flipped"],
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
        symbol = "BTC"
        legs: list[DeltaLeg] = []

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


def xrp_pair(rh: Client, core: Client) -> DeltaTrade:
    return DeltaTrade(
        "XRP",
        DeltaLeg(cast(TradingClient, rh), "bid", Decimal("1001")),
        [DeltaLeg(cast(TradingClient, core), "ask", Decimal("1001"))],
    )


async def test_venues_with_different_lots_size_on_the_coarsest():
    # XRP: Lighter RH trades 0.01, Core whole units. 500.50 on RH's lot would be cut to 500 on
    # Core and leave half an XRP unhedged; both legs must trade the same whole-unit qty.
    rh, core = Client("rh", price="2", lot="0.01"), Client("core", price="2", lot="1")
    core.exchange = "lighter_core"
    plan = xrp_pair(rh, core)

    await plan.load_qtys()

    assert plan.lot_size == Decimal(1)
    assert [leg.qty for leg in plan.legs] == [Decimal(500), Decimal(500)]


async def test_qtys_sized_per_leg_on_the_coarsest_lot():
    # The price gap exaggerates the ~0.03% Lighter RH/Core basis, so lot rounding stays visible.
    lead = Client("lead", price="100", lot="0.001")
    rest = Client("rest", price="125", lot="0.1")
    plan = basket([lead, rest], ["100", "100"])

    await plan.load_qtys()

    assert plan.lot_size == Decimal("0.1")  # coarsest of the two venues, valid on both
    assert plan.rest[0].qty == Decimal("0.8")  # 100 USD at its own price, not the lead's
    assert plan.lead.qty == Decimal("0.8")  # basket nets to zero in base qty
    assert all(leg.qty % plan.lot_size == 0 for leg in plan.legs)


def test_basket_lot_meets_grids_that_reject_each_other():
    # 0.2 and 0.5 reject each other's lot, but 1.0 is tradeable on both grids.
    assert basket_lot_size([Decimal("0.5"), Decimal("0.2")]) == Decimal(1)


async def test_qtys_reject_a_basket_that_rounds_to_zero():
    lead = Client("lead", lot="0.5")
    rest = Client("rest", lot="0.2")  # their common step is 1.0 BTC, far above a 100 USD leg

    with pytest.raises(RuntimeError, match="size is too small to trade on lead, rest"):
        await basket([lead, rest], ["100", "100"]).load_qtys()

    assert lead.calls == [] and rest.calls == []  # refused while planning, before any order


@pytest.mark.parametrize(
    "sizes", [["1000", "1000"], ["1000", "500.37", "499.63"], ["250.05", "125.02", "125.03"]]
)
async def test_qtys_on_one_venue_match_lead_sizing(sizes: list[str]):
    clients = [Client(f"acc{i}") for i in range(len(sizes))]  # one venue: one price, one lot
    plan = basket(clients, sizes)

    await plan.load_qtys()

    sides: list[Side] = ["bid"] + ["ask"] * (len(sizes) - 1)
    assert [leg.qty for leg in plan.legs] == lead_sized_qtys(sides, sizes, "50000", "0.001")
    assert plan.lot_size == Decimal("0.001")


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


@pytest.mark.parametrize(("configured", "canonical"), [("nvda", "NVDA"), ("XYZ:NVDA", "xyz:NVDA")])
async def test_symbol_case_preserves_position_monitoring_and_closure(configured, canonical):
    lead, rest = Client("lead"), Client("rest")
    lead.get_symbols = AsyncMock(return_value=[canonical])
    lead.positions_value = [position(canonical, "bid", "0.002")]
    rest.positions_value = [position(canonical, "ask", "0.002")]
    cfg = make_cfg(symbols=[configured])

    await _check_symbols(cfg, [lead, rest])
    active = trade(lead, rest, cfg.symbols[0])
    await active.open(cfg)
    summary = await active.state(cfg)
    await active.close(cfg)

    assert summary.healthy is True
    assert summary.open_leg_count == 2
    assert lead.calls == [f"market:{canonical}:bid:0.002", f"close:{canonical}"]
    assert rest.calls == [f"market:{canonical}:ask:0.002", f"close:{canonical}"]


async def test_symbols_once_per_exchange():
    class CountingClient(Client):
        def __init__(self, name: str, exchange: str):
            super().__init__(name)
            self.exchange = exchange
            self.symbols: list[str] = []

        async def get_symbols(self) -> list[str]:
            return []  # unlisted, so every check reaches the lot-size lookup

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


class VenueClient(Client):
    """One venue of a mixed pool: it lists, quotes and sizes only its own markets."""

    def __init__(
        self, name: str, exchange: str, prices: dict[str, str], unlisted: tuple[str, ...] = ()
    ):
        super().__init__(name)
        self.exchange = exchange
        self.prices = {symbol: Decimal(price) for symbol, price in prices.items()}
        self.unlisted = unlisted  # markets this venue sizes but does not list, e.g. paused ones
        self.price_calls: list[str] = []
        self.listing_calls = 0

    async def get_price(self, symbol: str) -> Decimal:
        self.price_calls.append(symbol)
        if symbol not in self.prices:
            raise RuntimeError(f"No quote for {symbol} on {self.exchange}")

        return self.prices[symbol]

    async def get_symbols(self) -> list[str]:
        self.listing_calls += 1
        return list(self.prices)

    async def get_lot_size(self, symbol: str) -> Decimal:
        if symbol not in self.prices and symbol not in self.unlisted:
            raise RuntimeError(f"Unknown symbol {symbol} on {self.exchange}")

        return self.lot


POSITION_COLUMNS = [
    "Account",
    "Symbol",
    "Qty",
    "Entry",
    "Mark",
    "Value",
    "PnL",
    "ROI",
    "Balance",
    "MM%",
]


async def row_cells(accs: list[Client], monkeypatch) -> dict[tuple[str, str], dict[str, str]]:
    """Render the positions table and map each row's (account, symbol) to its cells by column."""
    tables: list[Table] = []
    monkeypatch.setattr("strategy.runner.rprint", tables.append)
    await print_positions(cast(list[TradingClient], accs))

    console = Console(width=200, no_color=True)
    with console.capture() as capture:
        console.print(tables[0])

    lines = [line.split() for line in capture.get().splitlines() if line.split()]
    assert lines[0] == POSITION_COLUMNS  # a cell with an inner space would shift every column

    names = {acc.name for acc in accs}
    rows = [line for line in lines if line[0] in names]
    assert all(len(row) == len(POSITION_COLUMNS) for row in rows)
    return {(row[0], row[1]): dict(zip(POSITION_COLUMNS, row, strict=True)) for row in rows}


async def marks_by_row(accs: list[Client], monkeypatch) -> dict[tuple[str, str], str]:
    return {key: row["Mark"] for key, row in (await row_cells(accs, monkeypatch)).items()}


async def test_positions_priced_by_the_account_holding_them(monkeypatch):
    # Lighter RH lists 243 markets against Core's 71, so the lead cannot price every leg.
    rh = VenueClient("rh1", "lighter", {"BTC": "100000"})
    core = VenueClient("core1", "lighter_core", {"DOGE": "0.25"})
    rh.positions_value = [position("BTC", "bid", "0.01", "99000")]
    core.positions_value = [position("DOGE", "ask", "4000", "0.24")]

    marks = await marks_by_row([rh, core], monkeypatch)

    assert marks == {("rh1", "BTC"): "100,000.00", ("core1", "DOGE"): "0.25"}


async def test_marks_are_quoted_once_per_market_per_venue(monkeypatch):
    # A mark is market data, so accounts on one venue share a quote: 6 accounts holding the same
    # two markets are 4 quotes, not 12. On Lighter each one is a 250-level order-book read.
    quotes = {"BTC": "100000", "ETH": "3000"}
    accs = [VenueClient(f"rh{i}", "lighter", quotes) for i in range(3)]
    accs += [VenueClient(f"core{i}", "lighter_core", quotes) for i in range(3)]
    for acc in accs:
        acc.positions_value = [position("BTC", "bid", "0.01"), position("ETH", "ask", "0.1")]

    rows = await row_cells(cast(list[Client], accs), monkeypatch)

    assert len(rows) == 12  # every position still printed, each with a live mark
    assert all(row["Mark"] != "n/a" for row in rows.values())
    asked = sorted((acc.exchange, symbol) for acc in accs for symbol in acc.price_calls)
    assert asked == [
        ("lighter", "BTC"),
        ("lighter", "ETH"),
        ("lighter_core", "BTC"),
        ("lighter_core", "ETH"),
    ]


async def test_unpriceable_position_is_flagged_and_logged(monkeypatch):
    rh = VenueClient("rh1", "lighter", {"BTC": "100000"})
    core = VenueClient("core1", "lighter_core", {})  # this venue quotes nothing right now
    rh.positions_value = [position("BTC", "bid", "0.01", "99000")]
    core.positions_value = [position("BTC", "ask", "0.01", "99500")]
    warning = Mock()
    monkeypatch.setattr("strategy.runner.logger.warning", warning)

    rows = await row_cells([rh, core], monkeypatch)

    assert rows[("rh1", "BTC")]["Mark"] == "100,000.00"
    stale = rows[("core1", "BTC")]
    assert [stale["Mark"], stale["PnL"], stale["ROI"]] == ["n/a", "n/a", "n/a"]
    assert stale["Value"] == "995.00"  # valued at its entry: 0.01 x 99,500
    warning.assert_called_once_with(
        "lighter_core: no mark price for BTC: No quote for BTC on lighter_core"
    )


async def test_a_hedged_pair_on_one_venue_goes_stale_together(monkeypatch):
    # One quote per venue market: a pair can never show a live mark on one leg and an entry
    # price on the other, which would read as net long or short while it is flat.
    prime = VenueClient("prime", "lighter", {})
    hedge = VenueClient("hedge", "lighter", {})
    prime.positions_value = [position("BTC", "bid", "0.01", "99000")]
    hedge.positions_value = [position("BTC", "ask", "0.01", "99500")]
    warning = Mock()
    monkeypatch.setattr("strategy.runner.logger.warning", warning)

    rows = await row_cells([prime, hedge], monkeypatch)

    assert [row["Mark"] for row in rows.values()] == ["n/a", "n/a"]
    assert prime.price_calls == ["BTC"] and hedge.price_calls == []
    warning.assert_called_once_with("lighter: no mark price for BTC: No quote for BTC on lighter")


class AnyCaseVenueClient(VenueClient):
    """A venue that accepts any casing, as N1 and Rise do by upper-casing the symbol first."""

    async def get_lot_size(self, symbol: str) -> Decimal:
        known = {s.casefold() for s in (*self.prices, *self.unlisted)}
        if symbol.casefold() not in known:
            raise RuntimeError(f"Unknown symbol {symbol} on {self.exchange}")

        return self.lot


async def test_symbols_listed_on_one_venue_only_fail_startup():
    rh = VenueClient("rh1", "lighter", {"BTC": "100000"})
    core = VenueClient("core1", "lighter_core", {"BTC": "100000", "DOGE": "0.25"})

    with pytest.raises(
        AppError, match=r"^Invalid configured symbols: DOGE is not available on lighter$"
    ):
        await _check_symbols(make_cfg(symbols=["btc", "doge"]), [rh, core])


async def test_symbols_canonicalized_from_every_venue(monkeypatch):
    # A market paused on one venue drops out of its get_symbols(), but its lot size still
    # resolves, so the pool starts. One warning names the pairs that only the lot size resolved.
    rh = VenueClient("rh1", "lighter", {"BTC": "100000"}, unlisted=("DOGE",))
    core = VenueClient("core1", "lighter_core", {"BTC": "100000", "DOGE": "0.25"})
    cfg = make_cfg(symbols=["btc", "doge"])
    warning = Mock()
    monkeypatch.setattr("strategy.runner.logger.warning", warning)

    await _check_symbols(cfg, [rh, core])

    assert cfg.symbols == ["BTC", "DOGE"]
    warning.assert_called_once_with("Not listed as active, may be untradeable: lighter DOGE")


async def test_configured_casing_is_rewritten_for_a_case_sensitive_venue():
    # Both Lighter clients match case-sensitively; N1 and Rise accept any casing. A spelling that
    # matches the forgiving venue verbatim must still be rewritten, or the strict one fails.
    lighter = VenueClient("rh1", "lighter", {"BTC": "100000"})
    n1 = AnyCaseVenueClient("n1a", "n1", {"btc": "100000"})
    cfg = make_cfg(symbols=["btc"])

    await _check_symbols(cfg, [lighter, n1])

    assert cfg.symbols == ["BTC"]


async def test_casefold_collision_keeps_the_first_listing():
    # Lighter sorts get_symbols by 24h volume, so of two spellings the first is the busier market.
    venue = VenueClient("rh1", "lighter", {"NVDA": "200", "nvda": "1"})
    cfg = make_cfg(symbols=["Nvda"])

    await _check_symbols(cfg, [venue])

    assert cfg.symbols == ["NVDA"]


async def test_a_name_every_venue_lists_is_kept_as_configured():
    # Two markets that differ only by case: the configured one trades, not the busier namesake.
    venue = VenueClient("rh1", "lighter", {"NVDA": "200", "nvda": "1"})
    cfg = make_cfg(symbols=["nvda"])

    await _check_symbols(cfg, [venue])

    assert cfg.symbols == ["nvda"]


async def test_one_casing_cannot_serve_two_case_sensitive_venues():
    # Known limitation: cfg.symbols holds one string per market for the whole pool, so a market
    # whose venues spell it differently cannot be expressed. Startup fails naming the venue that
    # rejects the resolved name; no venue is picked silently.
    rh = VenueClient("rh1", "lighter", {"NVDA": "200"})
    core = VenueClient("core1", "lighter_core", {"nvda": "200"})

    with pytest.raises(AppError, match="NVDA is not available on lighter_core"):
        await _check_symbols(make_cfg(symbols=["nvda"]), [rh, core])


async def test_listing_failure_fails_as_an_app_error():
    class BrokenVenueClient(VenueClient):
        async def get_symbols(self) -> list[str]:
            raise ApiError("Cannot get markets: 429 rate limit exceeded")

    rh = VenueClient("rh1", "lighter", {"BTC": "100000"})
    core = BrokenVenueClient("core1", "lighter_core", {"BTC": "100000"})

    with pytest.raises(
        AppError, match=r"^Cannot list markets: lighter_core: Cannot get markets: 429 rate limit"
    ):
        await _check_symbols(make_cfg(symbols=["BTC"]), [rh, core])

    assert rh.listing_calls == 1  # the sibling listing is awaited, not left orphaned


class ClosedMarketClient(Client):
    """Omni over a weekend: the swaps stay listed, but nothing quotes until the market reopens."""

    def __init__(self, name: str):
        super().__init__(name)
        self.tradeable = False

    async def registered(self) -> bool:
        return True

    async def get_symbols(self) -> list[str]:
        return ["US500S", "XAUS"]

    async def get_price(self, symbol: str) -> Decimal:
        raise RuntimeError("Market is not open for trading")

    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        raise RuntimeError("Market is not open for trading")

    async def get_order_book(self, symbol: str) -> OrderBook:
        raise RuntimeError("Market is not open for trading")

    async def get_lot_size(self, symbol: str) -> Decimal:
        raise RuntimeError("Market is not open for trading")


async def test_closed_market_starts_and_waits(monkeypatch):
    accounts = [ClosedMarketClient("prime"), ClosedMarketClient("hedge")]
    clients = cast(list[TradingClient], accounts)
    cfg = make_cfg(symbols=["us500s", "XAUS"], market_hours="strict")
    warning = Mock()
    monkeypatch.setattr("strategy.cycle.logger.warning", warning)

    await _prepare_groups(cfg, clients)
    strategy = DeltaStrategy(cfg, clients)
    strategy._wait = AsyncMock(side_effect=asyncio.CancelledError)  # stop at the first cooldown
    await strategy.run()

    assert cfg.symbols == ["US500S", "XAUS"]
    warning.assert_called_once_with(
        "Only 0/1 symbols are tradeable for the planned window; skipping cycle"
    )
    assert not [call for acc in accounts for call in acc.calls if call.startswith("market:")]


async def test_closed_market_rejects_unknown_symbols():
    clients = cast(list[TradingClient], [ClosedMarketClient("prime"), ClosedMarketClient("hedge")])

    with pytest.raises(AppError, match="symbols: XAUSS is not available on mock$"):
        await _check_symbols(make_cfg(symbols=["XAUS", "XAUSS"]), clients)


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

    assert await strategy._tradeable_symbols(30, Decimal(1000)) == []
    assert checked == [now + timedelta(seconds=5), now + timedelta(seconds=35)]


@dataclass
class PlannedTrade:
    symbol: str
    gate_result: bool
    events: list[str]
    legs: list[DeltaLeg]

    async def load_qtys(self) -> None:
        self.events.append(f"{self.symbol}:prepare")

    def split(self, parts: int) -> list["PlannedTrade"]:
        return [self]

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


async def test_failed_cleanup_retries_before_trading(monkeypatch):
    # A closed market refuses closes; positions are known, so cleanup retries
    # with backoff instead of stopping, and no cycle starts until it succeeds.
    events: list[str] = []
    results = iter([True, False, False, False, True, True])

    async def close_all(accounts):
        ok = next(results)
        events.append(f"cleanup:{ok}")
        return ok

    async def trade_cycle():
        events.append("trade")
        if events.count("trade") == 1:
            raise RuntimeError("Market is not open for trading")
        raise asyncio.CancelledError

    clients = cast(list[TradingClient], [Client("prime"), Client("hedge")])
    strategy = DeltaStrategy(make_cfg(), clients)
    strategy.trade_cycle = trade_cycle
    strategy._wait = AsyncMock()
    on_error = AsyncMock()
    monkeypatch.setattr("strategy.cycle.close_all", close_all)
    monkeypatch.setattr("strategy.cycle.tg.on_error", on_error)

    await strategy.run()

    assert events == [
        "cleanup:True",
        "trade",
        "cleanup:False",
        "cleanup:False",
        "cleanup:False",
        "cleanup:True",
        "trade",
        "cleanup:True",
    ]
    assert [call.args[1] for call in on_error.await_args_list] == [1, 2]


async def test_shutdown_with_failed_cleanup_reports_open_positions(monkeypatch):
    clients = cast(list[TradingClient], [Client("prime"), Client("hedge")])
    strategy = DeltaStrategy(make_cfg(), clients)
    strategy.trade_cycle = AsyncMock(side_effect=asyncio.CancelledError)
    error = Mock()
    monkeypatch.setattr("strategy.cycle.close_all", AsyncMock(side_effect=[True, False]))
    monkeypatch.setattr("strategy.cycle.logger.error", error)

    await strategy.run()

    error.assert_called_once_with("Shutdown cleanup incomplete; close positions manually")


def test_cfg_allows_50x_leverage():
    # Omni lists DXI swaps at up to 50x.
    assert make_cfg(leverage=50).leverage == 50
