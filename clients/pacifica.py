# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | It's not a bug, it's undocumented behavior
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Self, cast

import base58
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from lib import utils
from lib.decorators import bind_log_context, retry, ttl_cache
from lib.http import ApiError, AsyncHttp, HttpMethod
from lib.models import AccountConfig
from strategy import Order, OrderBook, OrderStatus, Position, ProfileInfo, Side, TradingClient

API_URL = "https://api.pacifica.fi/api/v1"
APP_URL = "https://app.pacifica.fi"
_POINTS_GENESIS = datetime(2025, 9, 4, tzinfo=UTC)

DEFAULT_SLIPPAGE = Decimal("0.5")


# https://pacifica.gitbook.io/closed-alpha/api-documentation/api/rest-api/orders/get-order-history
def to_domain_status(s: str) -> OrderStatus:
    s = s.lower()
    if s == "filled":
        return OrderStatus.FILLED
    if s in ("cancelled", "canceled", "rejected"):
        return OrderStatus.CANCELED
    return OrderStatus.OPEN


class PacificaAccount(BaseModel):
    balance: Decimal
    maker_fee: Decimal
    taker_fee: Decimal
    positions_count: int
    orders_count: int
    stop_orders_count: int
    total_margin_used: Decimal


class OrderBookItem(BaseModel):
    price: Decimal = Field(..., alias="p")
    amount: Decimal = Field(..., alias="a")
    orders: int = Field(..., alias="n")


class PointsInfo(BaseModel):
    points: Decimal
    referral_points: Decimal
    volume_7d: Decimal
    last_distribution_points: Decimal
    points_boost: Decimal
    rank: int


class PacificaPosition(BaseModel):
    symbol: str
    side: Side
    amount: Decimal
    entry_price: Decimal


class PacificaOrder(BaseModel):
    order_id: int
    symbol: str
    side: Side
    price: Decimal
    initial_amount: Decimal
    filled_amount: Decimal
    cancelled_amount: Decimal
    stop_price: Decimal | None
    order_type: Literal["limit", "market"]
    stop_parent_order_id: int | None
    trigger_price_type: str | None
    reduce_only: bool
    created_at: int
    updated_at: int = Field(validation_alias=AliasChoices("updated_at", "created_at"))
    status: str = Field("open", alias="order_status")


class PacificaTrade(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)
    trade_id: int = Field(..., alias="history_id")
    order_id: int
    symbol: str
    side: Literal["open_long", "open_short", "close_long", "close_short"]
    price: Decimal
    amount: Decimal
    fee: Decimal
    pnl: Decimal
    event_type: str
    created_at: datetime


class PacificaPoint(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)
    start_window: datetime = Field(..., alias="timestamp")
    total_points: Decimal = Field(..., alias="total_points")


# MARK: Client


@bind_log_context
class PacificaClient:
    exchange = "pacifica"

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return PacificaClient

    @classmethod
    def to_week_label(cls, dt: datetime) -> str:
        return utils.to_period_week(dt, genesis=_POINTS_GENESIS)

    @classmethod
    def from_config(cls, cfg: AccountConfig) -> Self:
        return cls(name=cfg.name, seckey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, seckey: str | list[int], proxy: str | None = None):
        self.keypair = utils.parse_sol_key(seckey, name)
        self.name = name
        self.http = AsyncHttp(
            baseurl=API_URL,
            headers={"Origin": APP_URL, "Referer": f"{APP_URL}/"},
            proxy=proxy,
        )

    async def _call(self, method: HttpMethod, path: str, **kwargs):
        rep = await self.http.request(method, path, **kwargs)
        if not rep.ok and '"success":' not in rep.text:
            raise ApiError("API error", rep)
        res = rep.json()
        if not res["success"]:
            raise ApiError(res["error"])
        return res

    def _sign(self, op_type: str, op_data: dict):
        dat = {
            "type": op_type,
            "data": op_data,
            "timestamp": int(time.time() * 1_000),
            "expiry_window": 5_000,
        }
        msg = json.dumps(dat, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = base58.b58encode(bytes(self.keypair.sign_message(msg))).decode("ascii")
        return {
            "account": str(self.keypair.pubkey()),
            "signature": sig,
            "timestamp": dat["timestamp"],
            "expiry_window": dat["expiry_window"],
            **op_data,
        }

    # MARK: Lifecycle

    async def login(self, *, force: bool = False) -> None:
        return self.http.clear_cookies() if force else None

    async def registered(self) -> bool:
        try:
            await self._call("GET", f"/account?account={self.keypair.pubkey()!s}")
            return True
        except ApiError as e:
            if "account not found" in str(e).lower():
                return False
            raise

    @ttl_cache(60)
    async def _info(self):
        res = await self._call("GET", "/info")
        return res["data"]

    @ttl_cache(3600)
    async def get_symbols(self) -> list[str]:
        items = await self._info()
        items = sorted(items, key=lambda x: int(x.get("max_leverage", 0)), reverse=True)
        return [x["symbol"] for x in items]

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        return True

    @ttl_cache(5)
    async def _order_book(self, symbol: str, agg_level=1):
        res = await self._call("GET", f"/book?symbol={symbol}&agg_level={agg_level}")
        bids = [OrderBookItem(**x) for x in res["data"]["l"][0]]
        asks = [OrderBookItem(**x) for x in res["data"]["l"][1]]
        return bids, asks

    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        bids, asks = await self._order_book(symbol)
        return bids[0].price, asks[0].price

    async def get_order_book(self, symbol: str) -> OrderBook:
        bids, asks = await self._order_book(symbol)
        return OrderBook.build(
            bids=[(x.price, x.amount) for x in bids[:5]],
            asks=[(x.price, x.amount) for x in asks[:5]],
        )

    async def get_price(self, symbol: str) -> Decimal:
        bid, ask = await self.get_bbo(symbol)
        return (bid + ask) / 2

    async def get_lot_size(self, symbol: str) -> Decimal:
        items = await self._info()
        item = utils.first([x for x in items if x["symbol"] == symbol])
        assert item is not None, f"Unknown symbol: {symbol}"
        return Decimal(item["lot_size"])

    async def get_tick_size(self, symbol: str) -> Decimal:
        items = await self._info()
        item = utils.first([x for x in items if x["symbol"] == symbol])
        assert item is not None, f"Unknown symbol: {symbol}"
        return Decimal(item["tick_size"])

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return Decimal(10)  # TODO: derive from API

    @ttl_cache(5)
    async def balance(self) -> Decimal:
        res = await self._call("GET", f"/account?account={self.keypair.pubkey()}")
        res = PacificaAccount(**res["data"])
        return res.balance

    # MARK: Leverage

    async def get_leverage(self, symbol: str) -> int | None:
        return None  # todo:

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        dat = {"symbol": symbol, "leverage": leverage}
        msg = self._sign("update_leverage", dat)
        await self._call("POST", "/account/leverage", json=msg)

    # MARK: Positions

    async def positions(self) -> list[Position]:
        res = await self._call("GET", f"/positions?account={self.keypair.pubkey()}")
        raw = [PacificaPosition(**x) for x in res["data"]]
        return [
            Position(
                id=f"{p.symbol}_{p.side}",
                symbol=p.symbol,
                side=p.side,
                size=abs(p.amount),
                entry_price=p.entry_price,
            )
            for p in raw
            if p.amount != 0
        ]

    async def close_position(self, position: Position) -> bool:
        if position.size == 0:
            return True
        close_side: Side = "ask" if position.side == "bid" else "bid"
        await self.market_order(position.symbol, close_side, position.size, reduce_only=True)
        return True

    async def close_all_positions(self) -> int:
        res = await self._call("GET", f"/positions?account={self.keypair.pubkey()}")
        raw = [PacificaPosition(**x) for x in res["data"]]
        closed = 0
        for p in raw:
            if p.amount != 0:
                close_side: Side = "ask" if p.side == "bid" else "bid"
                await self.market_order(p.symbol, close_side, abs(p.amount), reduce_only=True)
                closed += 1
        return closed

    # MARK: Orders

    async def get_order(self, order_id: str) -> Order | None:
        try:
            res = await self._call("GET", f"/orders/history_by_id?order_id={order_id}")
            raw = PacificaOrder(**res["data"][0])
            return Order(
                id=str(raw.order_id),
                symbol=raw.symbol,
                side=raw.side,
                size=raw.initial_amount,
                filled=raw.filled_amount,
                price=raw.price,
                status=to_domain_status(raw.status),
                reduce_only=raw.reduce_only,
            )
        except Exception:
            return None

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        lot_size = await self.get_lot_size(symbol)
        amount = utils.round_to_tick_size(qty, lot_size)

        dat = {
            "symbol": symbol,
            "amount": str(amount),
            "side": side,
            "slippage_percent": str(DEFAULT_SLIPPAGE),
            "reduce_only": reduce_only,
        }
        msg = self._sign("create_market_order", dat)
        res = await self._call("POST", "/orders/create_market", json=msg)
        order_id = cast(int, res["data"]["order_id"])

        order = await self.get_order(str(order_id))
        if order is None:
            raise ApiError(f"Order {order_id} not found")
        return order

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        tick_size = await self.get_tick_size(symbol)
        lot_size = await self.get_lot_size(symbol)
        price = utils.round_to_tick_size(price, tick_size)
        amount = utils.round_to_tick_size(qty, lot_size)

        dat = {
            "symbol": symbol,
            "amount": str(amount),
            "price": str(price),
            "side": side,
            "tif": "GTC",
            "reduce_only": reduce_only,
        }
        msg = self._sign("create_order", dat)
        res = await self._call("POST", "/orders/create", json=msg)
        order_id = cast(int, res["data"]["order_id"])

        order = await self.get_order(str(order_id))
        if order is None:
            raise ApiError(f"Order {order_id} not found")
        return order

    async def cancel_order(self, order: Order) -> bool:
        try:
            dat = {"order_id": int(order.id), "symbol": order.symbol}
            msg = self._sign("cancel_order", dat)
            await self._call("POST", "/orders/cancel", json=msg)
            return True
        except Exception:
            return False

    async def cancel_all_orders(self) -> int:
        res = await self._call("GET", f"/orders?account={self.keypair.pubkey()}")
        if not res["data"]:
            return 0

        dat = {"all_symbols": True, "exclude_reduce_only": False}
        msg = self._sign("cancel_all_orders", dat)
        res = await self._call("POST", "/orders/cancel_all", json=msg)
        return cast(int, res["data"]["cancelled_count"])

    # MARK: Stats

    async def trades(self, since: datetime | None = None) -> list[PacificaTrade]:
        has_more, cursor = True, None
        items: dict[int, PacificaTrade] = {}

        while has_more:
            url = f"/positions/history?account={self.keypair.pubkey()}&limit=1000"
            url = url + f"&cursor={cursor}" if cursor else url
            res = await self._call("GET", url)
            has_more = res["has_more"]
            cursor = res.get("next_cursor")

            for t in res["data"]:
                t = PacificaTrade(**t)
                if since and t.created_at < since:
                    has_more = False
                    break
                items[t.trade_id] = t

        return sorted(items.values(), key=lambda x: x.created_at)

    @retry()
    async def points(self):
        msg = self._sign("get_points", {})
        res = await self._call("POST", "/account/points/history", json=msg)
        items = [PacificaPoint(**x) for x in res["data"]]
        for x in items:
            x.start_window -= timedelta(seconds=1)
        return items

    @retry()
    async def points_total(self):
        msg = self._sign("get_points", {})
        res = await self._call("POST", "/account/points", json=msg)
        return PointsInfo(**res["data"])

    async def total_volume(self):
        res = await self._call("GET", f"/portfolio/volume?account={self.keypair.pubkey()}")
        return Decimal(res["data"]["volume_all_time"])

    async def portfolio(self):
        res = await self._call("GET", f"/portfolio?account={self.keypair.pubkey()}&time_range=all")
        res = res["data"][-1]
        return Decimal(res["pnl"])

    async def profile(self) -> ProfileInfo:
        pts, vol = await asyncio.gather(self.points_total(), self.total_volume())
        bal, pnl = await asyncio.gather(self.balance(), self.portfolio())
        return ProfileInfo(
            addr=utils.short_addr(str(self.keypair.pubkey()), 4, 4),
            balance=bal,
            volume=vol,
            pnl=pnl,
            points=pts.points,
        )
