# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
import base64
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Self
from urllib.parse import urlencode

from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from eth_account.messages import encode_typed_data
from eth_utils.crypto import keccak
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from lib import utils
from lib.decorators import bind_log_context, locked, retry, ttl_cache
from lib.http import ApiError, AsyncHttp, HttpMethod
from lib.models import AccountConfig, OptionalDec
from strategy import (
    Order,
    OrderBook,
    OrderStatus,
    Position,
    ProfileInfo,
    Side,
    TradingClient,
    opposite_side,
)

API_URL = "https://api.rise.trade"
APP_URL = "https://www.rise.trade"
RPC_URL = "https://rpc.risechain.com"
LOGIN_MESSAGE = "Please sign in with your wallet to access RISEx."
DEFAULT_MIN_TRADE_USD = Decimal(10)
_POINTS_GENESIS = datetime(2026, 7, 20, tzinfo=UTC)
_OFF_LABEL = "OFF Season 0"

_REGISTER_SIGNER = [
    {"name": "account", "type": "address"},
    {"name": "signer", "type": "address"},
    {"name": "message", "type": "string"},
    {"name": "expiration", "type": "uint32"},
    {"name": "nonceAnchor", "type": "uint48"},
    {"name": "nonceBitmap", "type": "uint8"},
]
_VERIFY_SIGNER = [
    {"name": "account", "type": "address"},
    {"name": "nonceAnchor", "type": "uint48"},
    {"name": "nonceBitmap", "type": "uint8"},
]
_VERIFY_WITNESS = [
    {"name": "account", "type": "address"},
    {"name": "target", "type": "address"},
    {"name": "hash", "type": "bytes32"},
    {"name": "nonceAnchor", "type": "uint48"},
    {"name": "nonceBitmap", "type": "uint8"},
    {"name": "deadline", "type": "uint32"},
]
_LOGIN = [
    {"name": "account", "type": "address"},
    {"name": "nonce", "type": "uint256"},
    {"name": "deadline", "type": "uint32"},
]


def _from_x18(value: Decimal | int | str) -> Decimal:
    res = (Decimal(str(value)) / Decimal("1e18")).normalize()
    _, _, exp = res.as_tuple()
    return res.quantize(Decimal(1)) if isinstance(exp, int) and exp > 0 else res


def _keccak_hex(data: bytes) -> str:
    return "0x" + keccak(data).hex()


def _to_domain_status(status: str) -> OrderStatus:
    status = status.upper()
    if status in (
        "OPEN",
        "PENDING",
        "ACCEPTED",
        "ORDER_STATUS_OPEN",
        "ORDER_STATUS_PENDING",
        "ORDER_STATUS_ACCEPTED",
    ):
        return OrderStatus.OPEN
    if status in ("FILLED", "ORDER_STATUS_FILLED"):
        return OrderStatus.FILLED
    if status in ("CANCELLED", "CANCELED", "ORDER_STATUS_CANCELLED", "ORDER_STATUS_CANCELED"):
        return OrderStatus.CANCELED
    return OrderStatus.OPEN


def _to_domain_side(side: int | str) -> Side:
    value = str(side).upper()
    if value in ("0", "BUY", "BID", "LONG"):
        return "bid"
    if value in ("1", "SELL", "ASK", "SHORT"):
        return "ask"
    raise ApiError(f"Unknown RISEx side: {side}")


def _encode_order_data(p: dict[str, Any]) -> int:
    # Compact bit layout copied from the official TS SDK order encoder.
    order_flags = 0
    if p["side"] & 1:
        order_flags |= 1
    if p["post_only"]:
        order_flags |= 2
    if p["reduce_only"]:
        order_flags |= 4
    order_flags |= (p["stp_mode"] & 3) << 3
    order_flags |= (p["order_type"] & 1) << 5
    order_flags |= (p["time_in_force"] & 3) << 6

    data = 0
    data |= (p["market_id"] & 0xFFFF) << 70
    data |= (p["size_steps"] & 0xFFFFFFFF) << 38
    data |= (p["price_ticks"] & 0xFFFFFF) << 14
    data |= (order_flags & 0xFF) << 6
    data |= (1 & 0x1F) << 1
    return data


def _encode_order_hash(p: dict[str, Any]) -> str:
    builder_id = int(p.get("builder_id", 0))
    client_order_id = int(p.get("client_order_id", 0))
    ttl_units = int(p["ttl_units"])

    # RISEx signs a compact order envelope. Header flags are bit flags from the
    # official TS SDK encoder: permit=1, builder=2, client_order_id=4, ttl=16.
    header_flags = 1
    if builder_id:
        header_flags |= 2
    if client_order_id:
        header_flags |= 4
    if ttl_units:
        header_flags |= 16

    encoded = abi_encode(
        ["bytes32", "uint8", "uint256", "uint16", "uint64", "uint16"],
        [
            keccak(b"RISE_PERPS_PLACE_ORDER_V1"),
            header_flags,
            _encode_order_data(p),
            builder_id,
            client_order_id,
            ttl_units,
        ],
    )
    return _keccak_hex(encoded)


def _encode_cancel_hash(market_id: int, resting_order_id: int | str) -> str:
    encoded = abi_encode(
        ["bytes32", "uint256", "uint256"],
        [keccak(b"RISE_PERPS_CANCEL_ORDER_V1"), market_id, int(resting_order_id)],
    )
    return _keccak_hex(encoded)


def _encode_cancel_all_hash(market_id: int) -> str:
    encoded = abi_encode(
        ["bytes32", "uint256"],
        [keccak(b"RISE_PERPS_CANCEL_ALL_ORDERS_V1"), market_id],
    )
    return _keccak_hex(encoded)


def _encode_leverage_hash(market_id: int, leverage: int) -> str:
    encoded = abi_encode(
        ["bytes32", "uint256", "uint128"],
        [keccak(b"RISE_PERPS_UPDATE_LEVERAGE_V1"), market_id, leverage],
    )
    return _keccak_hex(encoded)


def _short_symbol(name: str) -> str:
    return name.split("/")[0].split()[0].strip()


class RiseMarketConfig(BaseModel):
    name: str
    step_size: Decimal
    step_price: Decimal
    max_leverage: int
    min_order_size: Decimal
    unlocked: bool = True


class RiseMarket(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    market_id: int
    config: RiseMarketConfig
    available: bool = True
    symbol: str = Field(default="", validation_alias="display_name")
    last_price: Decimal = Decimal(0)
    mark_price: Decimal = Decimal(0)
    index_price: Decimal = Decimal(0)
    post_only: bool = False
    visible: bool = True

    @property
    def short_symbol(self) -> str:
        return _short_symbol(self.config.name)

    @property
    def price(self) -> Decimal:
        return self.mark_price or self.last_price or self.index_price


class RiseOrderBookLevel(BaseModel):
    price: Decimal
    quantity: Decimal


class RisePosition(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    market_id: int
    market_name: str = ""
    size: Decimal
    side: int | str = 0
    avg_entry_price: OptionalDec = None
    entry_price: OptionalDec = None
    unrealized_pnl: OptionalDec = None


class RiseHistoryOrder(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    order_id: str = Field(validation_alias=AliasChoices("order_id", "id"))
    market_id: int
    side: int | str
    size: Decimal = Decimal(0)
    price: Decimal | None = None
    filled_size: Decimal = Decimal(0)
    order_type: int = 1
    status: str = "open"
    reduce_only: bool = False


class RiseTrade(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    id: str = Field(default="", validation_alias=AliasChoices("id", "fill_id"))
    market_id: int | None = None
    order_id: str | None = None
    side: int | str | None = None
    price: Decimal = Decimal(0)
    size: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    time: str = Field(validation_alias=AliasChoices("time", "timestamp"))

    @model_validator(mode="before")
    @classmethod
    def set_id(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("id") or data.get("fill_id"):
            return data
        fallback = f"{data.get('order_id', '')}-{data.get('time') or data.get('timestamp')}"
        return {**data, "id": fallback}

    @property
    def created_at(self) -> datetime:
        return datetime.fromtimestamp(int(self.time) / 1_000_000_000, tz=UTC)

    @property
    def volume(self) -> Decimal:
        return self.price * self.size


class RiseVolumeStats(BaseModel):
    total_volume: Decimal = Decimal(0)
    total_fee: Decimal = Decimal(0)


class RisePointsInfo(BaseModel):
    total_points: Decimal = Decimal(0)
    rank: int | None = None


class RisePoint(BaseModel):
    epoch_id: str
    start_window: datetime
    points: Decimal
    rank: int | None = None


class RiseLeaderboardEntry(BaseModel):
    account_value: Decimal = Decimal(0)
    notional_pnl: Decimal = Decimal(0)
    rank: int | None = None


@bind_log_context
class RiseClient:
    exchange = "rise"

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return RiseClient

    @classmethod
    def from_config(cls, cfg: AccountConfig) -> Self:
        return cls(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    @classmethod
    def to_week_label(cls, dt: datetime) -> str:
        if dt < _POINTS_GENESIS:
            return _OFF_LABEL
        return utils.to_period_week(dt, genesis=_POINTS_GENESIS)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.name = name
        self.account = utils.parse_eth_key(privkey, name)
        self.address = self.account.address
        self.signer = self.account
        self.signer_address = self.signer.address
        self.http = AsyncHttp(
            baseurl=API_URL,
            headers={"Origin": APP_URL, "Referer": f"{APP_URL}/"},
            proxy=proxy,
        )
        self.rpc = AsyncHttp(
            baseurl=RPC_URL,
            headers={"Origin": APP_URL, "Referer": f"{APP_URL}/"},
            proxy=proxy,
        )
        self._rpc_id = 0
        self._order_markets: dict[str, int] = {}
        self._order_resting_ids: dict[str, str] = {}
        self._points_token: str | None = None
        self._points_token_expires_at = 0.0

    async def _call(self, method: HttpMethod, path: str, **kwargs):
        rep = await self.http.request(method, path, **kwargs)
        if not rep.ok:
            raise ApiError("API error", rep)
        res = rep.json()
        if isinstance(res, dict) and "data" in res:
            return res["data"]
        return res

    async def _rpc_call(self, to: str, data: str) -> str:
        self._rpc_id += 1
        rep = await self.rpc.request(
            "POST",
            "/",
            json={
                "jsonrpc": "2.0",
                "id": self._rpc_id,
                "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"],
            },
        )
        if not rep.ok:
            raise ApiError("RPC error", rep)
        res = rep.json()
        if "error" in res:
            raise ApiError(f"RPC rejected: {res['error']}")
        return res.get("result", "0x")

    @ttl_cache(3600)
    async def _system_config(self) -> dict[str, Any]:
        return await self._call("GET", "/api/v1/system/config")

    @ttl_cache(3600)
    async def _domain(self) -> dict[str, Any]:
        data = await self._call("GET", "/api/v1/auth/eip712-domain")
        return {
            "name": data["name"],
            "version": data["version"],
            "chainId": int(data["chain_id"]),
            "verifyingContract": data["verifying_contract"],
        }

    async def _sign_typed(
        self, primary_type: str, fields: list[dict], message: dict, *, signer=False
    ) -> bytes:
        payload = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                primary_type: fields,
            },
            "domain": await self._domain(),
            "primaryType": primary_type,
            "message": message,
        }
        account = self.signer if signer else self.account
        return account.sign_message(encode_typed_data(full_message=payload)).signature

    async def _sign_hex(
        self, primary_type: str, fields: list[dict], message: dict, *, signer=False
    ) -> str:
        return "0x" + (await self._sign_typed(primary_type, fields, message, signer=signer)).hex()

    async def _sign_b64(
        self, primary_type: str, fields: list[dict], message: dict, *, signer=False
    ) -> str:
        sig = await self._sign_typed(primary_type, fields, message, signer=signer)
        return base64.b64encode(sig).decode()

    async def _nonce_slot(self) -> tuple[int, int]:
        data = await self._call("GET", f"/api/v1/nonce-state/{self.address}")
        anchor = int(data.get("nonce_anchor", 0))
        idx = int(data.get("current_bitmap_index", 0))
        return (anchor + 1, 0) if idx > 207 else (anchor, idx)

    async def _target(self) -> str:
        cfg = await self._system_config()
        return cfg["addresses"]["router"]

    async def _permit(self, action_hash: str, ttl_sec: int = 7 * 24 * 3600) -> dict[str, Any]:
        await self.login()
        nonce_anchor, nonce_bitmap = await self._nonce_slot()
        target = await self._target()
        deadline = int(time.time()) + ttl_sec
        msg = {
            "account": self.address,
            "target": target,
            "hash": action_hash,
            "nonceAnchor": nonce_anchor,
            "nonceBitmap": nonce_bitmap,
            "deadline": deadline,
        }
        return {
            "account": self.address,
            "signer": self.signer_address,
            "deadline": deadline,
            "nonce_anchor": nonce_anchor,
            "nonce_bitmap_index": nonce_bitmap,
            "signature": await self._sign_b64("VerifyWitness", _VERIFY_WITNESS, msg, signer=True),
        }

    # MARK: Lifecycle

    async def registered(self) -> bool:
        data = await self._call("GET", f"/api/v1/invite/account/{self.address}")
        return bool(data.get("has_access")) and data.get("status") == "ACTIVE"

    @locked
    @retry(max_attempts=3, delay=2.0)
    async def login(self, *, force: bool = False) -> None:
        if force:
            self.http.clear_cookies()
            self.rpc.clear_cookies()

        signers = await self._call("GET", "/api/v1/auth/signers", params={"account": self.address})
        for s in signers.get("signers", []):
            if s.get("signer", "").lower() == self.signer_address.lower():
                return

        nonce_anchor, nonce_bitmap = await self._nonce_slot()
        expiration = int(time.time()) + 365 * 24 * 3600
        msg = {
            "account": self.address,
            "signer": self.signer_address,
            "message": LOGIN_MESSAGE,
            "expiration": expiration,
            "nonceAnchor": nonce_anchor,
            "nonceBitmap": nonce_bitmap,
        }
        account_sig = await self._sign_hex("RegisterSigner", _REGISTER_SIGNER, msg)
        signer_sig = await self._sign_hex(
            "VerifySigner",
            _VERIFY_SIGNER,
            {
                "account": self.address,
                "nonceAnchor": nonce_anchor,
                "nonceBitmap": nonce_bitmap,
            },
            signer=True,
        )
        await self._call(
            "POST",
            "/api/v1/auth/register-signer",
            json={
                "account": self.address,
                "signer": self.signer_address,
                "message": LOGIN_MESSAGE,
                "nonce_anchor": str(nonce_anchor),
                "nonce_bitmap_index": str(nonce_bitmap),
                "expiration": str(expiration),
                "account_signature": account_sig,
                "signer_signature": signer_sig,
            },
        )

    # MARK: Markets

    @ttl_cache(60)
    async def markets(self) -> list[RiseMarket]:
        data = await self._call("GET", "/api/v1/markets")
        return [RiseMarket.model_validate(x) for x in data.get("markets", [])]

    async def market_info(self, symbol: str) -> RiseMarket:
        normalized = _short_symbol(symbol).upper()
        market_id = int(symbol) if symbol.isdigit() else None
        for item in await self.markets():
            if (
                item.short_symbol.upper() == normalized
                or item.config.name.upper() == symbol.upper()
                or item.market_id == market_id
            ):
                return item
        raise ApiError(f"Symbol not found: {symbol}")

    @ttl_cache(3600)
    async def get_symbols(self) -> list[str]:
        return [
            m.short_symbol
            for m in await self.markets()
            if m.available and m.visible and not m.post_only and m.config.unlocked
        ]

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        m = await self.market_info(symbol)
        return reduce_only or (m.available and m.visible and not m.post_only and m.config.unlocked)

    async def get_lot_size(self, symbol: str) -> Decimal:
        return (await self.market_info(symbol)).config.step_size

    async def get_tick_size(self, symbol: str) -> Decimal:
        return (await self.market_info(symbol)).config.step_price

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        m = await self.market_info(symbol)
        return max(DEFAULT_MIN_TRADE_USD, m.config.min_order_size * m.price)

    # MARK: Account

    @ttl_cache(5)
    async def balance(self) -> Decimal:
        data = await self._call(
            "GET", "/api/v1/account/cross-margin-balance", params={"account": self.address}
        )
        return Decimal(str(data.get("balance", 0)))

    async def get_leverage(self, symbol: str) -> int | None:
        cfg = await self._system_config()
        m = await self.market_info(symbol)
        perps_manager = cfg["addresses"]["perps_manager"]
        data = "0x044c9eb8" + abi_encode(["uint256", "address"], [m.market_id, self.address]).hex()
        result = await self._rpc_call(perps_manager, data)
        if not result or result == "0x":
            return None
        value = _from_x18(abi_decode(["uint256"], bytes.fromhex(result.removeprefix("0x")))[0])
        return int(value) if value else None

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        m = await self.market_info(symbol)
        if leverage > m.config.max_leverage:
            raise ApiError(f"Leverage {leverage} exceeds max {m.config.max_leverage} for {symbol}")
        action_hash = _encode_leverage_hash(m.market_id, leverage)
        await self._call(
            "POST",
            "/api/v1/account/leverage",
            json={
                "market_id": str(m.market_id),
                "leverage": str(leverage),
                "permit_params": await self._permit(action_hash),
            },
        )

    # MARK: Prices

    async def get_price(self, symbol: str) -> Decimal:
        return (await self.market_info(symbol)).price

    @ttl_cache(5)
    async def get_order_book(self, symbol: str) -> OrderBook:
        m = await self.market_info(symbol)
        data = await self._call(
            "GET",
            "/api/v1/orderbook",
            params={"market_id": m.market_id, "limit": 5},
        )
        bids = [RiseOrderBookLevel.model_validate(x) for x in data.get("bids", [])]
        asks = [RiseOrderBookLevel.model_validate(x) for x in data.get("asks", [])]
        if not bids or not asks:
            raise ApiError(f"No orderbook data for {symbol}")

        return OrderBook.build(
            bids=[(x.price, x.quantity) for x in bids],
            asks=[(x.price, x.quantity) for x in asks],
        )

    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        book = await self.get_order_book(symbol)
        return book.bids[0].price, book.asks[0].price

    # MARK: Positions / Orders

    async def positions(self) -> list[Position]:
        data = await self._call("GET", "/api/v1/positions", params={"account": self.address})
        result: list[Position] = []
        for raw in data.get("positions", []):
            pos = RisePosition.model_validate(raw)
            if pos.size == 0:
                continue
            market = await self.market_info(str(pos.market_id))
            entry = _from_x18(pos.avg_entry_price or pos.entry_price or Decimal(0))
            size = abs(_from_x18(pos.size))
            result.append(
                Position(
                    id=str(pos.market_id),
                    symbol=market.short_symbol,
                    side=_to_domain_side(pos.side),
                    size=size,
                    entry_price=entry,
                    unrealized_pnl=_from_x18(pos.unrealized_pnl or Decimal(0)),
                )
            )
        return result

    async def close_position(self, position: Position) -> bool:
        if position.size == 0:
            return True
        await self.market_order(
            position.symbol,
            opposite_side(position.side),
            position.size,
            reduce_only=True,
        )
        return True

    async def close_all_positions(self) -> int:
        positions = await self.positions()
        for p in positions:
            await self.close_position(p)
        return len(positions)

    async def _order_from_history(self, raw: dict[str, Any]) -> Order:
        order = RiseHistoryOrder.model_validate(raw)
        market = await self.market_info(str(order.market_id))
        self._order_markets[order.order_id] = order.market_id
        if resting_id := raw.get("resting_order_id"):
            self._order_resting_ids[order.order_id] = str(resting_id)
        return Order(
            id=order.order_id,
            symbol=market.short_symbol,
            side=_to_domain_side(order.side),
            size=order.size,
            filled=order.filled_size,
            price=order.price,
            status=_to_domain_status(order.status),
            reduce_only=order.reduce_only,
        )

    async def _orders_page(
        self,
        statuses: list[str],
        *,
        market_id: int | None = None,
        limit: int = 100,
        page: int = 1,
    ) -> dict[str, Any]:
        query: list[tuple[str, str | int]] = [
            ("sorted_by", "-created_at"),
            ("account", self.address),
            ("limit", limit),
            ("page", page),
        ]
        for status in statuses:
            query.append(("statuses", status))
        if market_id is not None:
            query.append(("market_id", market_id))
        return await self._call("GET", f"/api/v1/orders?{urlencode(query)}")

    async def _open_orders(self, market_id: int | None = None) -> list[Order]:
        data = await self._orders_page(["ORDER_STATUS_OPEN"], market_id=market_id)
        return [await self._order_from_history(x) for x in data.get("orders", [])]

    async def order_history(
        self,
        *,
        market_id: int | None = None,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[Order]:
        statuses = statuses or ["ORDER_STATUS_FILLED", "ORDER_STATUS_CANCELLED"]
        data = await self._orders_page(statuses, market_id=market_id, limit=limit)
        return [await self._order_from_history(x) for x in data.get("orders", [])]

    async def tpsl_orders(self) -> list[dict[str, Any]]:
        data = await self._call(
            "GET",
            "/api/v1/orders/tpsl",
            params={"account": self.address, "statuses": "TPSL_ORDER_STATUS_ACCEPTED"},
        )
        return list(data.get("orders", []))

    async def trade_history(
        self, limit: int = 100, since: datetime | None = None
    ) -> list[RiseTrade]:
        page = 1
        trades: list[RiseTrade] = []
        while True:
            data = await self._call(
                "GET",
                "/api/v1/trade-history",
                params={
                    "account": self.address,
                    "limit": limit,
                    "page": page,
                    "sorted_by": "-time",
                },
            )
            items = [RiseTrade.model_validate(x) for x in data.get("trades", [])]
            trades.extend(items)
            if since is not None and any(x.created_at < since for x in items):
                return [x for x in trades if x.created_at >= since]
            if not data.get("has_next_page") or not items:
                return trades
            page += 1

    async def trades(self, since: datetime | None = None) -> list[RiseTrade]:
        return await self.trade_history(since=since)

    async def transfer_history(self, limit: int = 50) -> list[dict[str, Any]]:
        data = await self._call(
            "GET",
            "/api/v1/account/transfer-history",
            params={"account": self.address, "limit": limit, "page": 1},
        )
        return list(data.get("items", []))

    async def _place_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal | None,
        *,
        order_type: int,
        time_in_force: int,
        reduce_only: bool,
    ) -> Order:
        market = await self.market_info(symbol)
        qty = utils.round_to_tick_size(qty, market.config.step_size)
        price = (
            utils.round_to_tick_size(price, market.config.step_price) if price is not None else None
        )
        size_steps = int(qty / market.config.step_size)
        price_ticks = int(price / market.config.step_price) if price is not None else 0
        if size_steps <= 0:
            raise ApiError(f"Order size too small for {symbol}: qty={qty}")
        params = {
            "market_id": market.market_id,
            "size_steps": size_steps,
            "price_ticks": price_ticks,
            "side": 0 if side == "bid" else 1,
            "stp_mode": 0,
            "order_type": order_type,
            "post_only": False,
            "reduce_only": reduce_only,
            "time_in_force": time_in_force,
            "ttl_units": 0,
            "client_order_id": "0",
            "builder_id": 0,
        }
        res = await self._call(
            "POST",
            "/api/v1/orders/place",
            json={**params, "permit": await self._permit(_encode_order_hash(params))},
        )
        order_id = str(res["order_id"])
        self._order_markets[order_id] = market.market_id
        if resting_order_id := res.get("resting_order_id"):
            self._order_resting_ids[order_id] = str(resting_order_id)
        filled = Decimal(0)
        if res.get("filled_quantity"):
            filled = _from_x18(res["filled_quantity"])
        status = OrderStatus.FILLED if filled >= qty or res.get("message") else OrderStatus.OPEN
        return Order(
            id=order_id,
            symbol=market.short_symbol,
            side=side,
            size=qty,
            filled=filled,
            price=price,
            status=status,
            reduce_only=reduce_only,
        )

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        return await self._place_order(
            symbol,
            side,
            qty,
            None,
            order_type=0,
            time_in_force=3,
            reduce_only=reduce_only,
        )

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        return await self._place_order(
            symbol,
            side,
            qty,
            price,
            order_type=1,
            time_in_force=0,
            reduce_only=reduce_only,
        )

    async def get_order(self, order_id: str) -> Order | None:
        market_id = self._order_markets.get(order_id)
        for order in await self._open_orders(market_id):
            if order.id == order_id:
                return order

        orders = await self.order_history(market_id=market_id)
        for order in orders:
            if order.id == order_id:
                return order
        return None

    async def cancel_order(self, order: Order) -> bool:
        market_id = self._order_markets.get(order.id)
        if market_id is None:
            market_id = (await self.market_info(order.symbol)).market_id
        resting_order_id = self._order_resting_ids.get(order.id)
        if resting_order_id is None:
            for open_order in await self._open_orders(market_id):
                if open_order.id == order.id:
                    resting_order_id = self._order_resting_ids.get(order.id)
                    break
        if resting_order_id is None:
            raise ApiError(f"Cannot cancel {order.id}: resting_order_id not found")

        action_hash = _encode_cancel_hash(market_id, resting_order_id)
        res = await self._call(
            "POST",
            "/api/v1/orders/cancel",
            json={
                "market_id": market_id,
                "order_id": order.id,
                "permit": await self._permit(action_hash),
            },
        )
        return bool(res.get("success", True))

    async def cancel_all_orders(self) -> int:
        orders = await self._open_orders()
        market_ids = {self._order_markets[o.id] for o in orders if o.id in self._order_markets}
        for market_id in market_ids:
            await self._call(
                "POST",
                "/api/v1/orders/cancel-all",
                json={
                    "market_id": market_id,
                    "permit": await self._permit(_encode_cancel_all_hash(market_id)),
                },
            )
        return len(orders)

    # MARK: Profile

    async def volume_stats(self) -> RiseVolumeStats:
        try:
            data = await self._call(
                "GET",
                "/api/v1/stats/user-volume",
                params={"address": self.address, "window": "all"},
            )
            return RiseVolumeStats.model_validate(data)
        except ApiError:
            return RiseVolumeStats()

    @locked
    async def _points_login(self, *, force: bool = False) -> str:
        now = time.time()
        if not force and self._points_token and now < self._points_token_expires_at - 10:
            return self._points_token

        data = await self._call("GET", "/api/v1/auth/nonce", params={"account": self.address})
        nonce = str(data["nonce"]).removeprefix("0x")
        deadline = int(now) + 300
        signature = await self._sign_hex(
            "Login",
            _LOGIN,
            {
                "account": self.address,
                "nonce": int(nonce, 16),
                "deadline": deadline,
            },
        )
        rep = await self.http.request(
            "POST",
            f"{APP_URL}/api/risex-auth/login",
            json={
                "account": self.address,
                "nonce": nonce,
                "deadline": deadline,
                "signature": signature,
                "stayConnected": False,
            },
        )
        if not rep.ok:
            raise ApiError("Points login failed", rep)
        payload = rep.json()
        auth = payload.get("data", payload)
        token = auth.get("access_token")
        if not token:
            raise ApiError("Points login returned no access token")
        self._points_token = str(token)
        self._points_token_expires_at = now + int(auth.get("expires_in", 300))
        return self._points_token

    async def _points_call(self, path: str) -> dict:
        async def request(*, force_login: bool):
            token = await self._points_login(force=force_login)
            return await self.http.request(
                "GET", path, headers={"Authorization": f"Bearer {token}"}
            )

        rep = await request(force_login=False)
        if rep.status_code == 401:
            rep = await request(force_login=True)
        if not rep.ok:
            raise ApiError("Points API error", rep)
        payload = rep.json()
        return payload.get("data", payload)

    async def points_total(self) -> RisePointsInfo:
        data = await self._points_call(f"/api/v1/points/{self.address}")
        return RisePointsInfo.model_validate(data)

    async def points_history(self) -> list[RisePoint]:
        data = await self._points_call(f"/api/v1/points/{self.address}/history")
        points = []
        for entry in data.get("entries", []):
            epoch = entry.get("epoch", {})
            ledger = entry.get("ledger", {})
            epoch_id = str(epoch.get("epoch_id", ""))
            starts_at = int(epoch.get("starts_at", 0))
            start_window = (
                datetime.fromtimestamp(starts_at, tz=UTC)
                if starts_at
                else _POINTS_GENESIS - timedelta(weeks=1)
            )
            points.append(
                RisePoint(
                    epoch_id=epoch_id,
                    start_window=start_window,
                    points=Decimal(str(ledger.get("total_points", 0))),
                    rank=entry.get("rank"),
                )
            )
        return points

    async def _leaderboard_entry(self) -> RiseLeaderboardEntry:
        try:
            data = await self._call(
                "GET",
                "/api/v1/leaderboard/combined/entry",
                params={
                    "timeframe": "LEADERBOARD_TIME_FRAME_24H",
                    "address": self.address,
                    "sort_by": "COMBINED_LEADERBOARD_SORT_BY_PNL",
                },
            )
        except Exception:
            return RiseLeaderboardEntry()
        raw = data.get("entry", {})
        entry = RiseLeaderboardEntry.model_validate(raw)
        entry.account_value = _from_x18(entry.account_value)
        entry.notional_pnl = _from_x18(entry.notional_pnl)
        return entry

    async def _realized_pnl(self) -> Decimal:
        try:
            trades = await self.trades()
        except ApiError:
            trades = []
        return sum((x.realized_pnl for x in trades), Decimal(0))

    async def profile(self) -> ProfileInfo:
        balance, volume, pnl, leaderboard, points = await asyncio.gather(
            self.balance(),
            self.volume_stats(),
            self._realized_pnl(),
            self._leaderboard_entry(),
            self.points_total(),
        )
        return ProfileInfo(
            addr=utils.short_addr(self.address),
            balance=balance or leaderboard.account_value,
            volume=volume.total_volume,
            pnl=pnl or leaderboard.notional_pnl,
            points=points.total_points,
            rank=points.rank if points.rank is not None else leaderboard.rank,
        )
