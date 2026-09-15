# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Probably works in production
import asyncio
import json
import time
from datetime import datetime
from decimal import Decimal
from typing import Self

from curl_cffi import CurlMime
from eth_account.messages import encode_defunct

from lib import utils
from lib.decorators import bind_log_context, locked, ttl_cache
from lib.http import ApiError, AsyncHttp, HttpMethod
from lib.lighter_crypto import LighterSigner
from lib.lighter_crypto.signer import AuthToken, SignedTx
from lib.models import AccountConfig
from strategy import Order, OrderBook, OrderStatus, Position, ProfileInfo, Side

API_URL = "https://mainnet.zklighter.elliot.ai"
APP_URL = "https://app.lighter.xyz"

API_KEY_INDEX = 0
POLL_ATTEMPTS = 30
POLL_DELAY = 2.0

TX_LIFETIME_MS = 599_000
ORDER_LIFETIME_MS = 28 * 24 * 60 * 60 * 1_000
AUTH_LIFETIME_SEC = 7 * 60 * 60
AUTH_REFRESH_SEC = 10 * 60
MARKET_SLIPPAGE = Decimal("0.01")


def to_domain_status(status: str) -> OrderStatus:
    if status == "filled":
        return OrderStatus.FILLED
    if status.startswith("canceled"):
        return OrderStatus.CANCELED
    return OrderStatus.OPEN


@bind_log_context
class LighterClient:
    exchange = "lighter"

    @classmethod
    def from_config(cls, cfg: AccountConfig) -> Self:
        return cls(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.name = name
        self.account = utils.parse_eth_key(privkey, name)
        self.address = self.account.address
        self.signer = LighterSigner(privkey)
        self._account_index: int | None = None
        self._auth_token: AuthToken | None = None
        self._last_nonce = 0
        self.http = AsyncHttp(
            baseurl=API_URL,
            headers={"Origin": APP_URL, "Referer": f"{APP_URL}/"},
            proxy=proxy,
        )

    async def _call(self, method: HttpMethod, path: str, **kwargs) -> dict:
        rep = await self.http.request(method, path, **kwargs)
        if not rep.ok:
            raise ApiError("Lighter API error", rep)

        data = rep.json()
        if data["code"] != 200:
            message = data.get("message", "Lighter API error")
            raise ApiError(f"{message}: code {data['code']}")

        return data

    async def account_info(self) -> dict:
        data = await self._call(
            "GET", "/api/v1/account", params={"by": "l1_address", "value": self.address}
        )
        accounts = data["accounts"]
        if not accounts:
            raise ApiError(f"Lighter account not found for {self.address}")

        account = next(
            (item for item in accounts if item.get("account_type") == 0),
            accounts[0],
        )
        self._account_index = account["account_index"]
        return account

    async def profile(self) -> ProfileInfo:
        info = await self.account_info()
        return ProfileInfo(
            addr=utils.short_addr(self.address),
            balance=Decimal(info["collateral"]),
            volume=Decimal(0),
            pnl=Decimal(0),
            points=Decimal(0),
        )

    async def _get_account_index(self) -> int:
        if self._account_index is None:
            await self.account_info()
        assert self._account_index is not None
        return self._account_index

    async def _get_api_key(
        self, account_index: int, *, headers: dict[str, str] | None = None
    ) -> bytes | None:
        data = await self._call(
            "GET",
            "/api/v1/apikeys",
            params={"account_index": account_index, "api_key_index": API_KEY_INDEX},
            headers=headers,
        )
        item = next(
            (item for item in data["api_keys"] if item["api_key_index"] == API_KEY_INDEX),
            None,
        )
        if item is None:
            return None

        return bytes.fromhex(item["public_key"].removeprefix("0x"))

    async def _get_next_nonce(self, account_index: int) -> int:
        data = await self._call(
            "GET",
            "/api/v1/nextNonce",
            params={"account_index": account_index, "api_key_index": API_KEY_INDEX},
        )
        return data["nonce"]

    async def _send_change_pub_key(self, account_index: int) -> None:
        nonce = await self._get_next_nonce(account_index)
        expired_at = int(time.time() * 1_000) + TX_LIFETIME_MS
        message = self.signer.change_pub_key_message(account_index, API_KEY_INDEX, nonce)
        signature = self.account.sign_message(encode_defunct(text=message)).signature.hex()
        tx = self.signer.sign_change_pub_key(
            account_index,
            API_KEY_INDEX,
            nonce,
            expired_at,
            signature,
        )
        await self._send_tx(tx)

    async def _send_tx(self, tx: SignedTx) -> dict:
        multipart = CurlMime.from_list(
            [
                {"name": "tx_type", "data": str(tx.tx_type).encode()},
                {
                    "name": "tx_info",
                    "data": json.dumps(tx.info, separators=(",", ":")).encode(),
                },
                {"name": "price_protection", "data": b"false"},
            ]
        )

        try:
            return await self._call("POST", "/api/v1/sendTx", multipart=multipart)
        finally:
            multipart.close()

    async def _wait_for_api_key(self, account_index: int) -> None:
        for attempt in range(POLL_ATTEMPTS):
            if await self._get_api_key(account_index) == self.signer.public_key:
                return
            if attempt < POLL_ATTEMPTS - 1:
                await asyncio.sleep(POLL_DELAY)

        raise ApiError("Lighter API-key update timed out")

    def _get_auth_headers(self, account_index: int, *, force: bool = False) -> dict[str, str]:
        now = int(time.time())
        if force or self._auth_token is None or self._auth_token.deadline - now < AUTH_REFRESH_SEC:
            self._auth_token = self.signer.create_auth_token(
                account_index, API_KEY_INDEX, now + AUTH_LIFETIME_SEC
            )

        return {"PreferAuthServer": "true", "Authorization": self._auth_token.token}

    @locked
    async def login(self, *, force: bool = False) -> None:
        account_index = await self._get_account_index()
        public_key = await self._get_api_key(account_index)
        if force or public_key != self.signer.public_key:
            await self._send_change_pub_key(account_index)
            await self._wait_for_api_key(account_index)

        headers = self._get_auth_headers(account_index, force=force)
        if await self._get_api_key(account_index, headers=headers) != self.signer.public_key:
            raise ApiError("Lighter authentication returned a different API key")

    async def registered(self) -> bool:
        data = await self._call(
            "GET", "/api/v1/account", params={"by": "l1_address", "value": self.address}
        )
        return bool(data["accounts"])

    # MARK: Markets

    @ttl_cache(3600)
    async def _markets(self) -> list[dict]:
        data = await self._call("GET", "/api/v1/orderBookDetails")
        return data["order_book_details"]

    async def _market(self, symbol: str) -> dict:
        market = next((item for item in await self._markets() if item["symbol"] == symbol), None)
        if market is None:
            raise ApiError(f"Unknown Lighter symbol: {symbol}")
        return market

    async def get_symbols(self) -> list[str]:
        markets = [item for item in await self._markets() if item["status"] == "active"]
        markets.sort(key=lambda item: Decimal(str(item["daily_quote_token_volume"])), reverse=True)
        return [item["symbol"] for item in markets]

    async def is_symbol_tradeable(
        self, symbol: str, at: datetime, reduce_only: bool = False
    ) -> bool:
        market = await self._market(symbol)
        status = market["status"]
        if reduce_only:
            return status in ("active", "reduce_only")
        return status == "active" and not market["market_config"]["force_reduce_only"]

    @ttl_cache(5)
    async def get_order_book(self, symbol: str) -> OrderBook:
        market = await self._market(symbol)
        data = await self._call(
            "GET", "/api/v1/orderBookOrders", params={"market_id": market["market_id"], "limit": 250}
        )
        multiplier = Decimal(market.get("multiplier") or 1).normalize()

        def level(item: dict) -> tuple[Decimal, Decimal]:
            return Decimal(item["price"]) / multiplier, Decimal(item["remaining_base_amount"]) * multiplier

        return OrderBook.build(
            bids=[level(item) for item in data["bids"][:5]],
            asks=[level(item) for item in data["asks"][:5]],
        )

    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        book = await self.get_order_book(symbol)
        return book.bids[0].price, book.asks[0].price

    async def get_price(self, symbol: str) -> Decimal:
        bid, ask = await self.get_bbo(symbol)
        return (bid + ask) / 2

    async def get_lot_size(self, symbol: str) -> Decimal:
        market = await self._market(symbol)
        return Decimal(1).scaleb(-market["size_decimals"]) * Decimal(
            market.get("multiplier") or 1
        ).normalize()

    async def get_tick_size(self, symbol: str) -> Decimal:
        market = await self._market(symbol)
        return Decimal(1).scaleb(-market["price_decimals"]) / Decimal(
            market.get("multiplier") or 1
        ).normalize()

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return Decimal((await self._market(symbol))["min_quote_amount"])

    # MARK: Account

    async def balance(self) -> Decimal:
        return Decimal((await self.account_info())["collateral"])

    async def positions(self) -> list[Position]:
        info = await self.account_info()
        markets = {item["market_id"]: item for item in await self._markets()}
        result = []
        for item in info["positions"]:
            size = Decimal(item["position"])
            if size == 0:
                continue
            multiplier = Decimal(markets[item["market_id"]].get("multiplier") or 1).normalize()
            result.append(
                Position(
                    id=str(item["market_id"]),
                    symbol=item["symbol"],
                    side="bid" if item["sign"] == 1 else "ask",
                    size=abs(size) * multiplier,
                    entry_price=Decimal(item["avg_entry_price"]) / multiplier,
                    unrealized_pnl=Decimal(item["unrealized_pnl"]),
                )
            )
        return result

    async def close_position(self, position: Position) -> bool:
        side: Side = "ask" if position.side == "bid" else "bid"
        await self.market_order(position.symbol, side, position.size, reduce_only=True)
        return True

    async def close_all_positions(self) -> int:
        positions = await self.positions()
        for position in positions:
            await self.close_position(position)
        return len(positions)

    # MARK: Orders

    def _next_nonce(self) -> int:
        self._last_nonce = max(int(time.time() * 1_000), self._last_nonce + 1)
        return self._last_nonce

    async def _load_order(self, item: dict) -> Order:
        market = next(
            item_ for item_ in await self._markets() if item_["market_id"] == item["market_index"]
        )
        multiplier = Decimal(market.get("multiplier") or 1).normalize()
        is_market = item["type"] == "market"
        return Order(
            id=str(item["client_order_index"]),
            symbol=market["symbol"],
            side="ask" if item["is_ask"] else "bid",
            size=Decimal(item["initial_base_amount"]) * multiplier,
            filled=Decimal(item["filled_base_amount"]) * multiplier,
            price=None if is_market else Decimal(item["price"]) / multiplier,
            status=to_domain_status(item["status"]),
            reduce_only=item["reduce_only"],
        )

    async def get_order(self, order_id: str) -> Order | None:
        account_index = await self._get_account_index()
        data = await self._call(
            "GET",
            "/api/v1/accountOrders",
            params={"account_index": account_index, "client_order_indexes": order_id},
            headers=self._get_auth_headers(account_index),
        )
        return await self._load_order(data["orders"][0]) if data["orders"] else None

    async def _place_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        order_type: int,
        reduce_only: bool,
    ) -> Order:
        market = await self._market(symbol)
        multiplier = Decimal(market.get("multiplier") or 1).normalize()
        qty = utils.round_to_tick_size(qty, await self.get_lot_size(symbol))
        price = utils.round_to_tick_size(price, await self.get_tick_size(symbol))
        base_amount = int(qty / multiplier * Decimal(10) ** market["size_decimals"])
        base_price = int(price * multiplier * Decimal(10) ** market["price_decimals"])
        nonce = self._next_nonce()
        order_expiry = 0 if order_type == 1 else nonce + ORDER_LIFETIME_MS
        tx = self.signer.sign_create_order(
            await self._get_account_index(),
            API_KEY_INDEX,
            market["market_id"],
            nonce,
            base_amount,
            base_price,
            side == "ask",
            order_type,
            0 if order_type == 1 else 1,
            reduce_only,
            0,
            order_expiry,
            nonce,
            nonce + TX_LIFETIME_MS,
        )
        await self._send_tx(tx)

        for attempt in range(POLL_ATTEMPTS):
            order = await self.get_order(str(nonce))
            if order is not None:
                return order
            if attempt < POLL_ATTEMPTS - 1:
                await asyncio.sleep(POLL_DELAY)
        raise ApiError(f"Lighter order {nonce} not found")

    async def market_order(
        self, symbol: str, side: Side, qty: Decimal, reduce_only: bool = False
    ) -> Order:
        bid, ask = await self.get_bbo(symbol)
        price = ask * (1 + MARKET_SLIPPAGE) if side == "bid" else bid * (1 - MARKET_SLIPPAGE)
        return await self._place_order(symbol, side, qty, price, 1, reduce_only)

    async def limit_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        reduce_only: bool = False,
    ) -> Order:
        return await self._place_order(symbol, side, qty, price, 0, reduce_only)

    async def cancel_order(self, order: Order) -> bool:
        market = await self._market(order.symbol)
        nonce = self._next_nonce()
        tx = self.signer.sign_cancel_order(
            await self._get_account_index(),
            API_KEY_INDEX,
            market["market_id"],
            int(order.id),
            nonce,
            nonce + TX_LIFETIME_MS,
        )
        await self._send_tx(tx)

        for attempt in range(POLL_ATTEMPTS):
            current = await self.get_order(order.id)
            if current is None or current.status == OrderStatus.CANCELED:
                return True
            if attempt < POLL_ATTEMPTS - 1:
                await asyncio.sleep(POLL_DELAY)
        return False

    async def cancel_all_orders(self) -> int:
        account_index = await self._get_account_index()
        headers = self._get_auth_headers(account_index)
        data = await self._call(
            "GET",
            "/api/v1/accountActiveOrders",
            params={"account_index": account_index, "market_type": "all"},
            headers=headers,
        )
        count = len(data["orders"])
        if count == 0:
            return 0

        nonce = self._next_nonce()
        tx = self.signer.sign_cancel_all_orders(
            account_index, API_KEY_INDEX, nonce, nonce + TX_LIFETIME_MS
        )
        await self._send_tx(tx)

        for attempt in range(POLL_ATTEMPTS):
            data = await self._call(
                "GET",
                "/api/v1/accountActiveOrders",
                params={"account_index": account_index, "market_type": "all"},
                headers=headers,
            )
            if not data["orders"]:
                return count
            if attempt < POLL_ATTEMPTS - 1:
                await asyncio.sleep(POLL_DELAY)
        raise ApiError("Lighter cancel all orders timed out")

    # MARK: Leverage

    async def get_leverage(self, symbol: str) -> int | None:
        info = await self.account_info()
        position = next((item for item in info["positions"] if item["symbol"] == symbol), None)
        if position is not None:
            return int(Decimal(100) / Decimal(position["initial_margin_fraction"]))
        market = await self._market(symbol)
        return 10_000 // market["default_initial_margin_fraction"]

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        market = await self._market(symbol)
        nonce = self._next_nonce()
        tx = self.signer.sign_update_leverage(
            await self._get_account_index(),
            API_KEY_INDEX,
            market["market_id"],
            10_000 // leverage,
            nonce,
            nonce + TX_LIFETIME_MS,
        )
        await self._send_tx(tx)

        for attempt in range(POLL_ATTEMPTS):
            if await self.get_leverage(symbol) == leverage:
                return
            if attempt < POLL_ATTEMPTS - 1:
                await asyncio.sleep(POLL_DELAY)
        raise ApiError(f"Lighter leverage update timed out for {symbol}")
