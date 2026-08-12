# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Self

from eth_account.messages import encode_typed_data
from pydantic import BaseModel, ConfigDict, Field

from lib import utils
from lib.decorators import bind_log_context, retry, ttl_cache
from lib.http import ApiError, AsyncHttp, HttpMethod
from lib.logger import logger
from lib.models import AccountConfig
from strategy import Order, OrderBook, OrderStatus, Position, ProfileInfo, Side, TradingClient

API_URL = "https://api.ethereal.trade/v1"
APP_URL = "https://app.ethereal.trade"
ARCHIVE_URL = "https://archive.ethereal.trade/v1"
_POINTS_GENESIS = datetime(2025, 12, 18, tzinfo=UTC)

NativeSide = Literal["buy", "sell"]


def _encode_subaccount_name(name: str) -> str:
    if name.startswith("0x") and len(name) == 66:
        return name.lower()
    raw = name.encode("ascii")
    if len(raw) > 32:
        raise ValueError("subaccount_name must be <= 32 ascii bytes")
    return "0x" + raw.hex().ljust(64, "0")


# https://docs.ethereal.trade/developer-guides/trading-api/order-placement#lifecycle
def to_domain_status(s: str) -> OrderStatus:
    s = s.lower()
    if s in ("new", "pending", "filled_partial"):
        return OrderStatus.OPEN
    if s == "filled":
        return OrderStatus.FILLED
    if s in ("canceled", "expired"):
        return OrderStatus.CANCELED
    logger.warning(f"Unknown order status: {s!r}, treating as open")
    return OrderStatus.OPEN


class Subaccount(BaseModel):
    id: str  # uuidv4 internal ethereal id
    name: str  # name from _encode_subaccount_name
    account: str  # eth address


# https://api.ethereal.trade/v1/product
class SymbolInfo(BaseModel):
    id: str
    symbol: str = Field(alias="baseTokenName")
    lot_size: Decimal = Field(alias="lotSize")
    tick_size: Decimal = Field(alias="tickSize")
    onchain_id: int = Field(alias="onchainId")


class EtherealPosition(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    id: str
    product_id: str = Field(alias="productId")
    size: Decimal
    side: NativeSide
    cost: Decimal
    realized_pnl: Decimal = Field(alias="realizedPnl")
    fees_usd: Decimal = Field(default=Decimal(0), alias="feesAccruedUsd")
    funding_usd: Decimal = Field(default=Decimal(0), alias="fundingAccruedUsd")
    total_inc: Decimal = Field(default=Decimal(0), alias="totalIncreaseNotional")
    total_dec: Decimal = Field(default=Decimal(0), alias="totalDecreaseNotional")
    created_at: datetime = Field(..., alias="createdAt")
    symbol: str = ""


class EtherealPoint(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    id: str
    season: int
    epoch: int
    points: Decimal
    referral_points: Decimal = Field(alias="referralPoints")
    started_at: datetime = Field(alias="startedAt")
    ended_at: int = Field(alias="endedAt")

    @property
    def total_points(self) -> Decimal:
        return self.points + self.referral_points


# MARK: Client


@bind_log_context
class EtherealClient:
    exchange = "ethereal"

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return EtherealClient

    @classmethod
    def to_week_label(cls, dt: datetime) -> str:
        return utils.to_period_week(dt, genesis=_POINTS_GENESIS)

    @classmethod
    def from_config(cls, cfg: AccountConfig) -> Self:
        return cls(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.name = name
        self.account = utils.parse_eth_key(privkey, name)
        self.address = self.account.address
        self.http = AsyncHttp(
            baseurl=API_URL,
            headers={"Origin": APP_URL, "Referer": f"{APP_URL}/"},
            proxy=proxy,
        )

    async def _call(self, method: HttpMethod, path: str, **kwargs):
        rep = await self.http.request(method, path, **kwargs)
        if not rep.ok:
            raise ApiError("API error", rep)
        return rep.json()

    @ttl_cache(3600)
    async def _rpc_config(self):
        res = await self._call("GET", "/rpc/config")
        domain = res["domain"]
        types = res["signatureTypes"]
        types["EIP712Domain"] = (
            "string name,string version,uint256 chainId,address verifyingContract"
        )

        return domain, {k: utils.parse_signature_type(v) for k, v in types.items()}

    async def _sign(self, primary_type: str, message: dict[str, Any]) -> str:
        domain, types = await self._rpc_config()  # ensure config is loaded and cached
        assert primary_type in types, f"Unknown primary type: {primary_type}"

        msg = {
            "types": {"EIP712Domain": types["EIP712Domain"], primary_type: types[primary_type]},
            "domain": domain,
            "primaryType": primary_type,
            "message": message,
        }

        msg = encode_typed_data(full_message=msg)
        return "0x" + self.account.sign_message(msg).signature.hex()

    @ttl_cache(3600)
    @retry(max_attempts=9, delay=2.0)
    async def subaccount(self):
        res = await self._call("GET", "/subaccount", params={"sender": self.address})
        items = [Subaccount(**x) for x in res.get("data", [])]
        if not items:
            raise ApiError(f"No subaccounts found for {self.address}")

        target_name = _encode_subaccount_name("primary")  # always use main account
        sub = utils.first([x for x in items if x.name.lower() == target_name]) or items[0]
        return sub

    # MARK: Lifecycle

    async def login(self, *, force: bool = False) -> None:
        return self.http.clear_cookies() if force else None

    async def registered(self) -> bool:
        res = await self._call("GET", "/subaccount", params={"sender": self.address})
        return len(res.get("data", [])) > 0

    async def balance(self) -> Decimal:
        sub = await self.subaccount()
        res = await self._call("GET", "/subaccount/balance", params={"subaccountId": sub.id})
        for b in res["data"]:
            if b["tokenName"].upper() == "USD":
                return Decimal(b["available"])  # or amount?

        logger.warning(f"USD balance not found for subaccount {sub.id}")
        return Decimal(0)

    @ttl_cache(3600)
    async def symbols(self):
        res = await self._call("GET", "/product")
        return [SymbolInfo.model_validate(x) for x in res.get("data", [])]

    async def symbol_info(
        self, *, symbol: str | None = None, product_id: str | None = None
    ) -> SymbolInfo:
        assert product_id or symbol, "Must provide product_id or symbol"
        for sym in await self.symbols():
            if (symbol and sym.symbol == symbol) or (product_id and sym.id == product_id):
                return sym

        raise ApiError(f"Symbol not found: symbol={symbol} product_id={product_id}")

    async def get_lot_size(self, symbol: str) -> Decimal:
        sym = await self.symbol_info(symbol=symbol)
        return sym.lot_size

    async def get_tick_size(self, symbol: str) -> Decimal:
        sym = await self.symbol_info(symbol=symbol)
        return sym.tick_size

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return Decimal(10)  # TODO: derive from API

    @ttl_cache(3600)
    async def get_symbols(self) -> list[str]:
        return [s.symbol for s in await self.symbols()]

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        return True

    # MARK: Prices

    @ttl_cache(5)
    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        sym = await self.symbol_info(symbol=symbol)
        res = await self._call("GET", "/product/market-price", params={"productIds": sym.id})
        res = res.get("data", [])
        if not res:
            raise ApiError(f"No market price for {symbol}")

        return Decimal(res[0]["bestBidPrice"]), Decimal(res[0]["bestAskPrice"])

    @ttl_cache(5)
    async def get_order_book(self, symbol: str) -> OrderBook:
        sym = await self.symbol_info(symbol=symbol)
        res = await self._call("GET", "/product/market-liquidity", params={"productId": sym.id})
        return OrderBook.build(bids=res["bids"][:5], asks=res["asks"][:5])

    async def get_price(self, symbol: str) -> Decimal:
        bid, ask = await self.get_bbo(symbol)
        return (bid + ask) / 2

    # MARK: Leverage

    async def get_leverage(self, symbol: str) -> int | None:
        return None

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        pass  # no leverage on ethereal (they compute it automatically)

    # MARK: Orders

    async def _load_order(self, res: dict[str, Any]) -> Order:
        sym = await self.symbol_info(product_id=res["productId"])
        return Order(
            id=res["id"],
            symbol=sym.symbol,
            side="bid" if res["side"] == 0 else "ask",
            size=Decimal(res["quantity"]),
            price=Decimal(res["price"]),
            status=to_domain_status(res["status"]),
            filled=Decimal(res["filled"]),
            reduce_only=res.get("reduceOnly", False),
        )

    async def get_order(self, order_id: str) -> Order | None:
        try:
            res = await self._call("GET", f"/order/{order_id}")
            return await self._load_order(res)
        except ApiError as e:
            if "order not found" in str(e).lower():
                return None
            raise e

    async def _place_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal | None, reduce_only: bool
    ) -> Order:
        sub, sym = await asyncio.gather(self.subaccount(), self.symbol_info(symbol=symbol))

        is_market = price is None
        order_price = price or Decimal(0)
        side_int = 0 if side == "bid" else 1
        nonce = str(time.time_ns())
        now_sec = int(time.time())

        data: dict[str, Any] = {
            "sender": self.address,
            "subaccount": sub.name,
            "quantity": str(qty),
            "reduceOnly": reduce_only,
            "side": side_int,
            "engineType": 0,
            "onchainId": sym.onchain_id,
            "nonce": nonce,
            "signedAt": now_sec,
            "type": "MARKET" if is_market else "LIMIT",
        }
        if not is_market:
            data.update({"price": str(order_price), "postOnly": False, "timeInForce": "GTD"})

        sign_data = {
            "sender": self.address,
            "subaccount": sub.name,
            "quantity": int(qty * Decimal("1e9")),
            "price": int(order_price * Decimal("1e9")),
            "reduceOnly": reduce_only,
            "side": side_int,
            "engineType": 0,
            "productId": sym.onchain_id,
            "nonce": int(nonce),
            "signedAt": now_sec,
        }
        sig = await self._sign("TradeOrder", sign_data)
        res = await self._call("POST", "/order", json={"data": data, "signature": sig})
        # {'result': 'Ok', 'filled': '0', 'id': '40fa3578-a219-4daa-afdc-e784ab768ae9'}
        if res.get("result").lower() != "ok":
            raise ApiError(f"Order placement failed: {res}")

        order = await self.get_order(res.get("id", ""))
        if order is None:
            raise ApiError(f"Order {res.get('id')} not found after placement")

        return order

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        return await self._place_order(symbol, side, qty, None, reduce_only)

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        return await self._place_order(symbol, side, qty, price, reduce_only)

    async def cancel_order(self, order: Order) -> bool:
        sub = await self.subaccount()
        msg = {"sender": self.address, "subaccount": sub.name, "nonce": str(time.time_ns())}
        order_id_bytes = "0x" + order.id.replace("-", "").ljust(64, "0")
        sig = await self._sign("CancelOrder", {**msg, "orderIds": [order_id_bytes]})
        pld = {"data": {**msg, "orderIds": [order.id]}, "signature": sig}
        res = await self._call("POST", "/order/cancel", json=pld)
        res = res.get("data", [])
        return len(res) > 0 and res[0].get("result").lower() == "ok"

    async def cancel_all_orders(self) -> int:
        sub = await self.subaccount()

        pld = {"subaccountId": sub.id, "limit": 200, "isWorking": "true"}
        res = await self._call("GET", "/order", params=pld)

        canceled = 0
        for o in res.get("data", []):
            order = await self._load_order(o)
            await self.cancel_order(order)

        return canceled

    # MARK: Positions

    async def raw_positions(self, open_only=True, limit=200) -> list[EtherealPosition]:
        limit = max(10, min(limit, 200))  # API max limit is 200
        sub = await self.subaccount()

        items: dict[str, EtherealPosition] = {}
        cursor, has_next = None, True

        while has_next:
            pld = {"subaccountId": sub.id, "open": str(open_only).lower(), "limit": limit}
            pld.update({"cursor": cursor} if cursor else {})
            res = await self._call("GET", "/position", params=pld)

            for r in res.get("data", []):
                r["side"] = "buy" if r.get("side") == 0 else "sell"
                if open_only and Decimal(r.get("size", 0)) == 0:
                    continue

                s = await self.symbol_info(product_id=r["productId"])
                r["symbol"] = s.symbol
                p = EtherealPosition(**r)
                items[p.id] = p

            cursor = res.get("nextCursor")
            has_next = res.get("hasNext", False) and cursor is not None

        return list(items.values())

    async def positions(self) -> list[Position]:
        return [
            Position(
                id=p.id,
                symbol=p.symbol,
                side="bid" if p.side == "buy" else "ask",
                size=abs(p.size),
                entry_price=abs(p.cost / p.size) if p.size != 0 else Decimal(0),
            )
            for p in await self.raw_positions(open_only=True, limit=200)
        ]

    async def close_position(self, position: Position) -> bool:
        if position.size > 0:
            close_side = "ask" if position.side == "bid" else "bid"
            await self.market_order(position.symbol, close_side, position.size, reduce_only=True)
        return True

    async def close_all_positions(self) -> int:
        positions = await self.positions()
        for p in positions:
            await self.close_position(p)
        return len(positions)

    # MARK: Stats

    async def _eip712_auth_headers(self, intent: int) -> dict[str, str]:
        now_sec = int(time.time())
        msg = {"sender": self.address, "intent": intent, "signedAt": now_sec}
        sig = await self._sign("EIP712Auth", msg)
        return {
            "x-ethereal-auth": "EIP712Auth",
            "x-ethereal-sender": self.address,
            "x-ethereal-signature": sig,
            "x-ethereal-intent": str(intent),
            "x-ethereal-signedat": str(now_sec),
        }

    async def points(self, season: int = 1, epoch: int = 2) -> list[EtherealPoint]:
        hdr = await self._eip712_auth_headers(3)
        pld = {"address": self.address, "season": season, "epoch": epoch}
        res = await self._call("GET", "/points", params=pld, headers=hdr)
        return [EtherealPoint.model_validate(x) for x in res.get("data", [])]

    async def _total_volume(self) -> Decimal:
        sub = await self.subaccount()
        pld = {"subaccountId": sub.id}
        res = await self._call("GET", f"{ARCHIVE_URL}/subaccount/total-volume", params=pld)
        return Decimal(res.get("volumeUsd", 0))

    async def _account_snapshot(self) -> dict:
        gms = int(_POINTS_GENESIS.timestamp() * 1000)  # genesis in ms
        sub = await self.subaccount()
        pld = {"resolution": "week1", "startTime": gms, "orderBy": "time", "order": "asc"}
        pld = {"subaccountId": sub.id, **pld}
        res = await self._call("GET", f"{ARCHIVE_URL}/subaccount/balance", params=pld)
        data = res.get("data", [])
        return data[-1] if data else {}

    async def profile(self) -> ProfileInfo:
        bal, pts, vol, snap = await asyncio.gather(
            self.balance(), self.points(), self._total_volume(), self._account_snapshot()
        )
        total_pts = sum((ep.total_points for ep in pts), Decimal(0))
        pnl = (
            Decimal(snap.get("realizedPnl", 0))  # realizedPnl is gross trading PnL
            + Decimal(snap.get("tradingFee", 0))  # tradingFee arrives with negative sign
            + Decimal(snap.get("realizedFunding", 0))
        )
        return ProfileInfo(
            addr=utils.short_addr(self.address),
            balance=bal,
            volume=vol,
            pnl=pnl,
            points=total_pts,
        )
