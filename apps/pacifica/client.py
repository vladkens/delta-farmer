# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Code so clean it squeaks
import asyncio
import json
import time
from decimal import Decimal
from typing import Literal

import base58
from pydantic import BaseModel, Field
from solders.keypair import Keypair

from core import logger, utils
from core.decorators import bind_log_context, ttl_cache
from core.http import AsyncHttp, HttpMethod

from .config import AccountConfig

BASE_URL = "https://api.pacifica.fi/api/v1"
DEFAULT_SLIPPAGE = Decimal("0.5")

OrderSide = Literal["bid", "ask"]
Number = Decimal | int | float


class ApiError(Exception):
    pass


class AccountInfo(BaseModel):
    balance: Decimal
    maker_fee: Decimal
    taker_fee: Decimal
    positions_count: int
    orders_count: int
    stop_orders_count: int
    total_margin_used: Decimal


class PointsInfo(BaseModel):
    # user: str
    points: Decimal
    referral_points: Decimal
    volume_7d: Decimal
    last_distribution_points: Decimal
    points_boost: Decimal
    # league: str | None
    rank: int


class Position(BaseModel):
    symbol: str
    side: OrderSide
    amount: Decimal
    entry_price: Decimal
    # margin: float
    # funding: float
    # isolated: bool
    # liquidation_price: float
    # created_at: int
    # updated_at: int


# https://docs.pacifica.fi/api-documentation/api/rest-api/orders/get-open-orders#response
class Order(BaseModel):
    order_id: int
    # client_order_id: str | None
    symbol: str
    side: OrderSide
    price: Decimal
    initial_amount: Decimal
    filled_amount: Decimal
    cancelled_amount: Decimal
    stop_price: Decimal | None
    order_type: Literal["limit", "market"]  # note: spec have more types
    stop_parent_order_id: int | None
    trigger_price_type: str | None
    reduce_only: bool
    created_at: int
    updated_at: int


class Trade(BaseModel):
    trade_id: int = Field(..., alias="history_id")
    order_id: int
    symbol: str
    side: Literal["open_long", "open_short", "close_long", "close_short"]
    price: Decimal
    amount: Decimal
    fee: Decimal
    pnl: Decimal
    event_type: str
    created_at: int


class OrderBookItem(BaseModel):
    price: Decimal = Field(..., alias="p")
    amount: Decimal = Field(..., alias="a")
    orders: int = Field(..., alias="n")


def prepare_msg(keypair: Keypair, op_type: str, op_data: dict):
    # https://docs.pacifica.fi/api-documentation/api/signing/implementation
    dat = {
        "type": op_type,
        "data": op_data,
        "timestamp": int(time.time() * 1_000),
        "expiry_window": 5_000,
    }

    msg = json.dumps(dat, sort_keys=True, separators=(",", ":"))
    msg = msg.encode("utf-8")
    sig = keypair.sign_message(msg)
    sig = base58.b58encode(bytes(sig)).decode("ascii")

    return {
        "account": str(keypair.pubkey()),
        "signature": sig,
        "timestamp": dat["timestamp"],
        "expiry_window": dat["expiry_window"],
        **op_data,
    }


@bind_log_context
class Client:
    @classmethod
    def from_config(cls, cfg: AccountConfig):
        return cls(name=cfg.name, seckey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, seckey: str, proxy: str | None = None):
        self.keypair = Keypair.from_bytes(base58.b58decode(seckey))
        self.name = name
        self.http = AsyncHttp(
            baseurl=BASE_URL,
            headers={"Origin": "https://app.pacifica.fi", "Referer": "https://app.pacifica.fi/"},
            proxy=proxy,
        )

    def __repr__(self) -> str:
        shortk = str(self.keypair.pubkey())[:4] + "..." + str(self.keypair.pubkey())[-4:]
        return f"Pacifica(name={self.name}, pubkey={shortk})"

    async def call(self, method: HttpMethod, path: str, **kwargs):
        rep = await self.http.request(method, path, **kwargs)

        if not rep.ok and '"success":' not in rep.text:
            raise ApiError(f"Unknown API error: {rep.status_code} {rep.text}")

        res = rep.json()
        if not res["success"]:
            raise ApiError(res["error"])

        return res

    async def ip(self):
        rep = await self.http.request("GET", "https://ipapi.co/json/")
        return utils.pick(rep.json(), "ip", "country_name", "country_code")

    async def total_volume(self):
        res = await self.call("GET", f"/portfolio/volume?account={self.keypair.pubkey()}")
        return Decimal(res["data"]["volume_all_time"])

    async def portfolio(self):
        res = await self.call("GET", f"/portfolio?account={self.keypair.pubkey()}&time_range=all")
        res = res["data"][-1]
        return Decimal(res["account_equity"]), Decimal(res["pnl"])

    async def account_info(self):
        res = await self.call("GET", f"/account?account={self.keypair.pubkey()}")
        return AccountInfo(**res["data"])

    async def balance(self):
        info = await self.account_info()
        return info.balance

    async def points(self):
        msg = prepare_msg(self.keypair, "get_points", {})
        res = await self.call("POST", "/account/points", json=msg)
        return PointsInfo(**res["data"])

    async def set_leverage(self, symbol: str, leverage: int):
        dat = {"symbol": symbol, "leverage": leverage}
        msg = prepare_msg(self.keypair, "update_leverage", dat)
        await self.call("POST", "/account/leverage", json=msg)

    async def positions(self):
        res = await self.call("GET", f"/positions?account={self.keypair.pubkey()}")
        return [Position(**x) for x in res["data"]]

    async def orders(self):
        res = await self.call("GET", f"/orders?account={self.keypair.pubkey()}")
        return [Order(**x) for x in res["data"]]

    async def points_history(self):
        msg = prepare_msg(self.keypair, "get_points", {})
        res = await self.call("POST", "/account/points/history", json=msg)
        return {f"W{x['week_number']}": Decimal(x["total_points"]) for x in res["data"]}

    async def trades(self):
        # https://docs.pacifica.fi/api-documentation/api/rest-api/account/get-trade-history
        has_more = True
        cursor = None
        items: dict[int, Trade] = {}

        while has_more:
            url = f"/positions/history?account={self.keypair.pubkey()}&limit=1000"
            url = url + f"&cursor={cursor}" if cursor else url
            res = await self.call("GET", url)

            has_more = res["has_more"]
            cursor = res.get("next_cursor", None)

            for t in res["data"]:
                t = Trade(**t)
                items[t.trade_id] = t

        return sorted(list(items.values()), key=lambda x: x.created_at)

    async def cancel_all_orders(self) -> int:
        orders = await self.orders()
        if not orders:
            return 0

        dat = {"all_symbols": True, "exclude_reduce_only": False}
        msg = prepare_msg(self.keypair, "cancel_all_orders", dat)
        res = await self.call("POST", "/orders/cancel_all", json=msg)
        return res["data"]["cancelled_count"]

    async def cancel_all_positions(self) -> int:
        positions = await self.positions()
        if not positions:
            return 0

        for x in positions:
            await self.market_order(
                symbol=x.symbol,
                asize=x.amount,
                side="ask" if x.side == "bid" else "bid",
                reduce_only=True,
            )
        return len(positions)

    @ttl_cache(60)
    async def info(self):
        res = await self.call("GET", "/info")
        return res["data"]

    async def get_lot_size(self, symbol: str) -> Decimal:
        items = await self.info()
        item = utils.first([x for x in items if x["symbol"] == symbol])
        assert item is not None, f"Unknown symbol: {symbol}"
        return Decimal(item["lot_size"])

    @ttl_cache(5)
    async def order_book(self, symbol: str, agg_level=1):
        res = await self.call("GET", f"/book?symbol={symbol}&agg_level={agg_level}")
        bids = [OrderBookItem(**x) for x in res["data"]["l"][0]]
        asks = [OrderBookItem(**x) for x in res["data"]["l"][1]]
        return bids, asks

    async def vwap_price(self, symbol: str, side: OrderSide, slippage=0.001) -> Decimal:
        bids, asks = await self.order_book(symbol)

        bid, ask = Decimal(bids[0].price), Decimal(asks[0].price)
        avg_price = (ask + bid) / 2
        # logger.debug(f"VWAP price for {symbol} ({side}): bid={bid}, ask={ask}, avg={avg_price}")

        slippage = Decimal(1 + slippage) if side == "bid" else Decimal(1 - slippage)
        return utils.round_to_tick_size(avg_price * slippage, Decimal("0.01"))

    # https://docs.pacifica.fi/api-documentation/api/rest-api/orders/create-market-order
    async def market_order(
        self,
        symbol: str,
        side: OrderSide,
        *,
        asize: Number | None = None,
        qsize: Number | None = None,
        reduce_only=False,
        slippage=DEFAULT_SLIPPAGE,
    ):
        assert (asize is not None) ^ (qsize is not None), "One of asize or qsize must be provided."
        amount = Decimal(asize) if asize is not None else None

        if qsize is not None:
            price = await self.vwap_price(symbol, side)
            amount = Decimal(qsize) / price

        assert amount is not None and amount > 0, "Amount must be positive."
        lot_size = await self.get_lot_size(symbol)
        amount = utils.round_to_tick_size(amount, lot_size)
        logger.debug(f"Market {side} order: {amount} {symbol} (slip={slippage:.3})")

        dat = {
            "symbol": symbol,
            "amount": str(amount),
            "side": side,
            "slippage_percent": str(slippage),
            "reduce_only": reduce_only,
        }
        msg = prepare_msg(self.keypair, "create_market_order", dat)
        res = await self.call("POST", "/orders/create_market", json=msg)
        return res["data"]["order_id"]

    async def limit_order(
        self,
        symbol: str,
        side: OrderSide,
        *,
        asize: Number | None = None,
        qsize: Number | None = None,
        price: Number | None = None,
        reduce_only=False,
        tif="GTC",
    ):
        assert (asize is not None) ^ (qsize is not None), "One of asize or qsize must be provided."

        if not price:
            bids, asks = await self.order_book(symbol)
            price = bids[0].price if side == "bid" else asks[0].price

        price = utils.round_to_tick_size(price, Decimal("0.01")) if price is not None else None
        assert price is not None and price > 0, "Price must be positive."

        if asize is None and qsize is not None:
            asize = Decimal(qsize) / price

        asize = Decimal(asize) if asize is not None else None
        assert asize is not None and asize > 0, "Amount must be positive."
        asize = utils.round_to_tick_size(asize, await self.get_lot_size(symbol))

        logger.debug(f"Limit {side} order: {asize} {symbol} @ {price}")
        dat = {
            "symbol": symbol,
            "amount": str(asize),
            "price": str(price),
            "side": side,
            "tif": tif,
            "reduce_only": reduce_only,
        }
        msg = prepare_msg(self.keypair, "create_order", dat)
        res = await self.call("POST", "/orders/create", json=msg)
        return res["data"]["order_id"]

    # https://docs.pacifica.fi/api-documentation/api/rest-api/orders/get-order-history-by-id#response
    async def wait_order_filled(self, order_id: int, timeout=30):
        since_ts = time.time()
        while True:
            await asyncio.sleep(2)
            res = await self.call("GET", f"/orders/history_by_id?order_id={order_id}")
            res = res["data"][0]  # hist list of order status changes, pick most recent

            status = res["order_status"]
            inited, filled = Decimal(res["initial_amount"]), Decimal(res["filled_amount"])
            lbl = f"Limit order ({filled} / {inited})"

            if status == "filled":
                logger.info(f"{lbl} filled in {time.time() - since_ts:.1f}s")
                return True

            if status in ("cancelled", "rejected"):
                logger.warning(f"{lbl} got {status}, stopping...")
                return False

            if time.time() - since_ts > timeout:
                logger.warning(f"{lbl} not filled after {timeout}s, cancelling...")
                return False
