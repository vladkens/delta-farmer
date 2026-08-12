# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Optimized for confusion
import asyncio
import random
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Self

from eth_account.messages import encode_typed_data
from pydantic import BaseModel

from lib import logger, utils
from lib.decorators import bind_log_context, retry, ttl_cache
from lib.http import ApiError, AsyncHttp, NotFoundError
from lib.models import AccountConfig
from strategy import Order, OrderBook, OrderStatus, Position, ProfileInfo, Side, TradingClient

APP_URL = "https://app.nado.xyz"
_ISOLATED_MARGIN_BUFFER = Decimal("1.02")
_NAMED_EPOCHS: list[tuple[str, datetime]] = [
    ("ALP", datetime(2025, 11, 20, tzinfo=UTC)),
    ("OFF", datetime(2026, 1, 16, tzinfo=UTC)),
]
_POINTS_GENESIS = datetime(2026, 1, 30, tzinfo=UTC)

USDT_PID = 0  # Nado uses product_id=0 for USDT spot balance and fees
USDC_PID = 5  # USDC spot balance (if any) and fee rebates for isolated margin

# Error codes that map to NotFoundError instead of ApiError
# https://docs.nado.xyz/developer-resources/api/errors
_NOT_FOUND_CODES = frozenset(
    {
        2015,  # MarketNotFound
        2020,  # OrderNotFound
        2058,  # TriggerOrderNotFound
    }
)

_EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "sender", "type": "bytes32"},
        {"name": "priceX18", "type": "int128"},
        {"name": "amount", "type": "int128"},
        {"name": "expiration", "type": "uint64"},
        {"name": "nonce", "type": "uint64"},
        {"name": "appendix", "type": "uint128"},
    ],
    "Cancellation": [
        {"name": "sender", "type": "bytes32"},
        {"name": "productIds", "type": "uint32[]"},
        {"name": "digests", "type": "bytes32[]"},
        {"name": "nonce", "type": "uint64"},
    ],
    "CancellationProducts": [
        {"name": "sender", "type": "bytes32"},
        {"name": "productIds", "type": "uint32[]"},
        {"name": "nonce", "type": "uint64"},
    ],
}


def _to_x6(v: Decimal | int | float) -> int:
    return int(Decimal(str(v)) * Decimal("1e6"))


def _to_x18(v: Decimal | int | float) -> int:
    return int(Decimal(str(v)) * Decimal("1e18"))


def _from_x18(v: str | int) -> Decimal:
    res = (Decimal(str(v)) / Decimal("1e18")).normalize()
    _, _, exp = res.as_tuple()
    return res.quantize(Decimal(1)) if isinstance(exp, int) and exp > 0 else res


def _build_sender(address: str, subaccount="default") -> str:
    # bytes32 = wallet address (20 bytes) + subaccount name (12 bytes, null-padded)
    addr = bytes.fromhex(address.removeprefix("0x"))
    name = subaccount.encode("ascii").ljust(12, b"\x00")
    return "0x" + (addr + name).hex()


# https://docs.nado.xyz/developer-resources/api/order-appendix
def _build_appendix(order_type=0, reduce_only=False, isolated=False, isolated_margin_x6=0) -> int:
    # bits 0-7: version=1, bit 8: isolated, bits 9-10: order_type, bit 11: reduce_only
    # bits 64-127: isolated margin amount (x6 precision)
    v = 1 | ((order_type & 0b11) << 9)
    if reduce_only:
        v |= 1 << 11
    if isolated:
        v |= 1 << 8
        if isolated_margin_x6 > 0:
            v |= (isolated_margin_x6 & ((1 << 64) - 1)) << 64
    return v


class MarketHours(BaseModel):
    is_open: bool
    next_close: datetime | None = None
    next_open: datetime | None = None


class SymbolInfo(BaseModel):
    product_id: int
    symbol: str
    size_increment: Decimal
    price_increment: Decimal
    min_size: Decimal
    isolated_only: bool = False
    trading_status: str = "live"
    market_hours: MarketHours | None = None


class NadoTrade(BaseModel):
    digest: str
    product_id: int
    symbol: str
    submission_idx: int
    side: str
    amount: Decimal
    price: Decimal
    volume: Decimal  # abs(quote_filled)
    fee: Decimal
    realized_pnl: Decimal
    created_at: datetime


class NadoPoint(BaseModel):
    epoch: int
    description: str
    since: datetime
    until: datetime
    points: Decimal
    rank: int
    tier: int


@bind_log_context
class NadoClient:
    exchange = "nado"

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return NadoClient

    @classmethod
    def to_week_label(cls, dt: datetime) -> str:
        for i, (prefix, since) in enumerate(_NAMED_EPOCHS):
            until = _NAMED_EPOCHS[i + 1][1] if i + 1 < len(_NAMED_EPOCHS) else _POINTS_GENESIS
            if since <= dt < until:
                s, e = since.strftime("%b%d"), (until - timedelta(seconds=1)).strftime("%b%d")
                return f"{prefix} {s}-{e}"
        if dt >= _POINTS_GENESIS:
            n = (dt - _POINTS_GENESIS).days // 7 + 1
            since = _POINTS_GENESIS + timedelta(weeks=n - 1)
            until = since + timedelta(weeks=1)
            s, e = since.strftime("%b%d"), (until - timedelta(seconds=1)).strftime("%b%d")
            return f"W{n:02d} {s}-{e}"
        return dt.strftime("%Y-%m-%d")

    @classmethod
    def from_config(cls, cfg: AccountConfig) -> Self:
        return cls(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.name = name
        self.http = AsyncHttp(
            baseurl="https://gateway.prod.nado.xyz",
            headers={"Origin": APP_URL, "Referer": f"{APP_URL}/"},
            proxy=proxy,
        )

        self.account = utils.parse_eth_key(privkey, name)
        self.address = str(self.account.address)
        self.sender = _build_sender(self.address, "default")
        self._order_products: dict[str, int] = {}  # digest → product_id
        self._leverage: dict[str, int] = {}  # symbol → configured leverage

    async def _query(self, pld: dict) -> dict:
        rep = await self.http.request("GET", "/v1/query", params=pld)
        res = rep.json()
        if not rep.ok or res.get("status") != "success":
            if res.get("error_code") in _NOT_FOUND_CODES:
                raise NotFoundError(res.get("error", "Not found"))
            raise ApiError("Query error", rep)
        return res.get("data", {})

    async def _execute(self, pld: dict) -> dict:
        rep = await self.http.request("POST", "/v1/execute", json=pld)
        if not rep.ok or rep.json().get("status") != "success":
            raise ApiError(f"[{self.name}] Execute error", rep)

        return rep.json().get("data", {})

    async def _archive(self, pld: dict) -> dict:
        rep = await self.http.request("POST", "https://archive.prod.nado.xyz/v1", json=pld)
        if not rep.ok:
            raise ApiError("Archive error", rep)
        return rep.json()

    @ttl_cache(3600)
    @retry(max_attempts=9, delay=2.0)
    async def endpoint_addr(self) -> str:
        res = await self._query({"type": "contracts"})
        return res["endpoint_addr"]

    @ttl_cache(300)
    async def _all_products_raw(self) -> dict[int, Any]:
        res = await self._query({"type": "all_products"})
        return {p["product_id"]: p for p in res.get("perp_products", [])}

    def _sign(self, primary_type: str, verifying_contract: str, message: dict[str, Any]) -> str:
        msg = {
            "types": {
                "EIP712Domain": _EIP712_TYPES["EIP712Domain"],
                primary_type: _EIP712_TYPES[primary_type],
            },
            "domain": {
                "name": "Nado",
                "version": "0.0.1",
                "chainId": 57073,  # Nado mainnet,
                "verifyingContract": verifying_contract,
            },
            "primaryType": primary_type,
            "message": message,
        }

        msg = encode_typed_data(full_message=msg)
        return "0x" + self.account.sign_message(msg).signature.hex()

    def _make_nonce(self) -> int:
        # Server allows recv_time up to 100s ahead; use 90s to keep a 10s safety margin.
        # https://github.com/nadohq/nado-python-sdk/blob/v0.3.5/nado_protocol/utils/nonce.py
        ts_ms = int(time.time() * 1000)
        return ((ts_ms + 90_000) << 20) + random.randint(0, (1 << 20) - 1)

    # MARK: Lifecycle

    async def login(self, *, force: bool = False) -> None:
        return self.http.clear_cookies() if force else None

    async def registered(self) -> bool:
        res = await self._query({"type": "subaccount_info", "subaccount": self.sender})
        return res.get("exists", False)

    @ttl_cache(5)
    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        pld = {"ticker_id": f"{symbol}-PERP_USDT0", "depth": 1}
        rep = await self.http.request("GET", "/v2/orderbook", params=pld)
        if not rep.ok:
            raise ApiError(f"Orderbook error for {symbol}", rep)

        data = rep.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            raise ApiError(f"No orderbook data for {symbol}")

        return Decimal(str(bids[0][0])), Decimal(str(asks[0][0]))

    @ttl_cache(5)
    async def get_order_book(self, symbol: str) -> OrderBook:
        pld = {"ticker_id": f"{symbol}-PERP_USDT0", "depth": 5}
        rep = await self.http.request("GET", "/v2/orderbook", params=pld)
        if not rep.ok:
            raise ApiError(f"Orderbook error for {symbol}", rep)

        data = rep.json()
        return OrderBook.build(bids=data.get("bids", []), asks=data.get("asks", []))

    async def get_price(self, symbol: str) -> Decimal:
        bid, ask = await self.get_bbo(symbol)
        return (bid + ask) / 2

    @ttl_cache(600)
    async def symbols(self) -> list[SymbolInfo]:
        rep = await self.http.request("GET", "https://archive.prod.nado.xyz/v2/symbols")
        if not rep.ok:
            raise ApiError("Symbols error", rep)

        items = rep.json()

        return [
            SymbolInfo(
                product_id=x["product_id"],
                symbol=symbol.removesuffix("-PERP"),
                size_increment=_from_x18(x["size_increment"]),
                price_increment=_from_x18(x["price_increment_x18"]),
                min_size=_from_x18(x["min_size"]),
                isolated_only=bool(x.get("isolated_only", False)),
                trading_status=x.get("trading_status", "live"),
                market_hours=x.get("market_hours"),
            )
            for symbol, x in items.items()
        ]

    @ttl_cache(3600)
    async def get_symbols(self) -> list[str]:
        return [s.symbol for s in await self.symbols()]

    async def symbol_info(
        self, *, symbol: str | None = None, product_id: int | None = None
    ) -> SymbolInfo:
        assert symbol or product_id, "Must provide symbol or product_id"

        for sym in await self.symbols():
            if (symbol and sym.symbol == symbol) or (product_id and sym.product_id == product_id):
                return sym

        raise ApiError(f"Symbol not found: symbol={symbol} product_id={product_id}")

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        sym = await self.symbol_info(symbol=symbol)
        if sym.trading_status == "not_tradable":
            return False
        if reduce_only and sym.trading_status == "post_only":
            return False
        if reduce_only:
            return sym.trading_status in ("live", "reduce_only", "soft_reduce_only")
        if sym.trading_status in ("reduce_only", "soft_reduce_only"):
            return False
        if sym.market_hours is None:
            return True

        nc = sym.market_hours.next_close
        no = sym.market_hours.next_open
        if sym.market_hours.is_open:
            ok = nc is None or at < nc
        else:
            ok = (no is None or at >= no) and (nc is None or at < nc)
        return ok

    async def get_lot_size(self, symbol: str) -> Decimal:
        sym = await self.symbol_info(symbol=symbol)
        return sym.size_increment

    async def get_tick_size(self, symbol: str) -> Decimal:
        sym = await self.symbol_info(symbol=symbol)
        return sym.price_increment

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return Decimal(100)  # Nado enforces $100 minimum notional; TODO: derive from API

    @ttl_cache(5)
    async def balance(self) -> Decimal:
        # Use healths[0].health (assets × oracle price − open positions)
        res = await self._query({"type": "subaccount_info", "subaccount": self.sender})
        return _from_x18(res["healths"][0]["health"])

    @ttl_cache(3600)
    async def _taker_fee_rate(self, product_id: int) -> Decimal:
        """Return taker fee rate for product_id. Falls back to 0.05% if not found."""
        res = await self._query({"type": "fee_rates", "sender": self.sender})
        rates = res.get("taker_fee_rates_x18", [])
        if product_id < len(rates):
            return Decimal(rates[product_id]) / Decimal("1e18")
        logger.warning(f"fee rate for product_id={product_id} not found, using 0.05% fallback")
        return Decimal("0.0005")

    # MARK: Leverage

    async def get_leverage(self, symbol: str) -> int | None:
        return self._leverage.get(symbol)

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self._leverage[symbol] = leverage

    # MARK: Orders

    async def _place_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        order_type: int,  # 0=DEFAULT, 1=IOC
        reduce_only=False,
    ) -> Order:
        exp = int(time.time()) + (30 if order_type == 1 else 3600)
        sym = await self.symbol_info(symbol=symbol)

        notional = qty * price
        if notional < sym.min_size:
            msg = f"Order notional {notional:.2f} < min {sym.min_size:.2f} USD for {symbol}"
            raise ApiError(msg)

        nonce = self._make_nonce()
        amount = _to_x18(qty) if side == "bid" else -_to_x18(qty)

        isolated = sym.isolated_only
        isolated_margin_x6 = 0
        if isolated and not reduce_only:
            fee_rate = await self._taker_fee_rate(sym.product_id)
            lev = Decimal(self._leverage.get(symbol, 1))

            # Bid (long): sweeps book down → average fill ≤ order price → order price is the ceiling
            # Ask (short): sweeps book up → average fill ≥ order price → add 1% slippage buffer
            # Oracle price is used as a floor — exchange health uses oracle for margin calculations.
            # https://docs.nado.xyz/subaccounts-and-health
            # IMPORTANT: Docs have errors here in practice; do not rely on them for rewrites.
            bid, ask = await self.get_bbo(symbol)
            mid = bid if side == "bid" else ask

            p = (await self._all_products_raw()).get(sym.product_id, {})
            oracle = _from_x18(p.get("oracle_price_x18", "0"))
            risk = p.get("risk", {})

            slp = Decimal("1.01") if side == "ask" else Decimal("1.00")
            prc = max(price, mid, oracle)
            margin_notional = qty * prc
            margin_fee = margin_notional * fee_rate
            margin_min = margin_notional / lev + margin_fee
            margin_buf = margin_min * _ISOLATED_MARGIN_BUFFER * slp
            eff_lev = (margin_notional / margin_buf) if margin_buf else 0
            lw = _from_x18(risk.get("long_weight_initial_x18", "0"))
            sw = _from_x18(risk.get("short_weight_initial_x18", "0"))

            logger.debug(
                f"{side} {qty:.3f} ord={price:.2f} book={mid:.2f} "
                f"m={margin_buf:.2f} lev={eff_lev:.2f}x/{int(lev)}x "
                f"rsk={prc:.2f} fee={margin_fee:.2f} "
                f"[oracle={oracle:.2f} lw={lw:.2f} sw={sw:.2f}]"
            )

            isolated_margin_x6 = _to_x6(margin_buf)

        appendix = _build_appendix(
            order_type=order_type,
            reduce_only=reduce_only,
            isolated=isolated,
            isolated_margin_x6=isolated_margin_x6,
        )

        # uses native int types (as required by Solidity struct)
        msg = {
            "sender": self.sender,
            "priceX18": _to_x18(price),
            "amount": amount,
            "expiration": exp,
            "nonce": nonce,
            "appendix": appendix,
        }

        # For place_order, verifyingContract = address(productId)
        vct = "0x" + sym.product_id.to_bytes(20, byteorder="big").hex()
        sig = self._sign("Order", vct, msg)

        pld = {k: str(v) for k, v in msg.items()}
        pld = {"place_order": {"product_id": sym.product_id, "order": pld, "signature": sig}}

        res = await self._execute(pld)
        assert "digest" in res, f"Unexpected place_order response: {res}"
        self._order_products[res["digest"]] = sym.product_id

        return Order(
            id=res["digest"],
            symbol=symbol,
            side=side,
            size=qty,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
            reduce_only=reduce_only,
        )

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        bid, ask = await self.get_bbo(symbol)
        price = ask * Decimal("1.005") if side == "bid" else bid * Decimal("0.995")
        tick = await self.get_tick_size(symbol)
        price = utils.round_to_tick_size(price, tick)
        return await self._place_order(
            symbol, side, qty, price, order_type=1, reduce_only=reduce_only
        )

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        return await self._place_order(
            symbol, side, qty, price, order_type=0, reduce_only=reduce_only
        )

    async def _get_order_live(self, order_id: str) -> Order | None:
        """Query live gateway for an open order. Returns Order(OPEN) if found, None otherwise."""
        product_id = self._order_products.get(order_id)
        if product_id is None:
            logger.warning(f"Product ID not found for order {order_id}, skipping live query")
            return None

        try:
            res = await self._query({"type": "order", "product_id": product_id, "digest": order_id})
        except NotFoundError:
            return None

        sym = await self.symbol_info(product_id=product_id)
        amount = _from_x18(res["amount"])
        size = abs(amount)
        unfilled = abs(_from_x18(res["unfilled_amount"]))
        return Order(
            id=order_id,
            symbol=sym.symbol,
            side="bid" if amount > 0 else "ask",
            size=size,
            filled=size - unfilled,
            price=_from_x18(res["price_x18"]),
            status=OrderStatus.OPEN,
            reduce_only=False,
        )

    async def _get_order_arch(self, order_id: str) -> Order | None:
        """Query archive for a terminal order. Returns Order(FILLED/CANCELED) or None."""
        res = await self._archive({"orders": {"digests": [order_id]}})
        res = utils.first([x for x in res.get("orders", []) if x["digest"] == order_id])
        if res is None:
            return None

        sym = await self.symbol_info(product_id=res["product_id"])
        amount = _from_x18(res["amount"])
        size = abs(amount)
        filled = abs(_from_x18(res["base_filled"]))

        return Order(
            id=order_id,
            symbol=sym.symbol,
            side="bid" if amount > 0 else "ask",
            size=size,
            filled=filled,
            price=_from_x18(res["price_x18"]),
            status=OrderStatus.FILLED if filled >= size else OrderStatus.CANCELED,
            reduce_only=False,
        )

    async def get_order(self, order_id: str) -> Order | None:
        live, arch = await asyncio.gather(
            self._get_order_live(order_id),
            self._get_order_arch(order_id),
        )
        # live is authoritative while order exists; archive only for terminal state.
        # archive can hold a stale partial-fill snapshot → filled < size → false CANCELED.
        return live or arch

    async def cancel_order(self, order: Order) -> bool:
        sym = await self.symbol_info(symbol=order.symbol)
        nonce = self._make_nonce()
        msg = {
            "sender": self.sender,
            "productIds": [sym.product_id],
            "digests": [order.id],
            "nonce": nonce,
        }

        sig = self._sign("Cancellation", await self.endpoint_addr(), msg)
        pld = {"cancel_orders": {"tx": {**msg, "nonce": str(nonce)}, "signature": sig}}
        await self._execute(pld)
        return True

    async def cancel_all_orders(self) -> int:
        nonce = self._make_nonce()
        msg = {"sender": self.sender, "productIds": [], "nonce": nonce}

        sig = self._sign("CancellationProducts", await self.endpoint_addr(), msg)
        pld = {"cancel_product_orders": {"tx": {**msg, "nonce": str(nonce)}, "signature": sig}}
        res = await self._execute(pld)
        return len(res.get("cancelled_orders", []))

    # MARK: Positions

    async def _positions_cross(self) -> list[Position]:
        payload = await self._query({"type": "subaccount_info", "subaccount": self.sender})
        balances = payload.get("perp_balances", [])
        products = payload.get("perp_products", [])

        rs: list[Position] = []
        for b in balances:
            amount = _from_x18(b["balance"]["amount"])
            if amount == 0:
                continue

            sym = await self.symbol_info(product_id=b["product_id"])
            amount = _from_x18(b["balance"]["amount"])
            vquote = _from_x18(b["balance"].get("v_quote_balance", "0"))
            entry_price = abs(vquote / amount) if amount != 0 else Decimal(0)

            oracle_price = Decimal(0)
            for p in products:
                if p.get("product_id") == b["product_id"]:
                    oracle_price = _from_x18(p.get("oracle_price_x18", "0"))
                    break

            pos = Position(
                id=str(b["product_id"]),
                symbol=sym.symbol,
                side="bid" if amount > 0 else "ask",
                size=abs(amount),
                entry_price=entry_price,
                unrealized_pnl=amount * oracle_price + vquote,
            )
            rs.append(pos)

        return rs

    async def _positions_isolated(self) -> list[Position]:
        items = await self._query({"type": "isolated_positions", "subaccount": self.sender})
        items = items.get("isolated_positions", [])

        rs: list[Position] = []
        for p in items:
            base = p["base_balance"]["balance"]
            amount = _from_x18(base["amount"])
            if amount == 0:
                continue
            vquote = _from_x18(base.get("v_quote_balance", "0"))
            entry_price = abs(vquote / amount)
            oracle_price = _from_x18(p["base_product"]["oracle_price_x18"])
            product_id = p["base_product"]["product_id"]
            sym = await self.symbol_info(product_id=product_id)

            pos = Position(
                id=p["subaccount"],  # isolated sender — used in close_position
                symbol=sym.symbol,
                side="bid" if amount > 0 else "ask",
                size=abs(amount),
                entry_price=entry_price,
                unrealized_pnl=amount * oracle_price + vquote,
            )

            rs.append(pos)

        return rs

    async def positions(self) -> list[Position]:
        crs_pos, iso_pos = await asyncio.gather(self._positions_cross(), self._positions_isolated())
        return crs_pos + iso_pos

    async def close_position(self, position: Position) -> bool:
        close_side: Side = "ask" if position.side == "bid" else "bid"
        await self.market_order(position.symbol, close_side, position.size, reduce_only=True)
        return True

    async def close_all_positions(self) -> int:
        positions = await self.positions()
        for p in positions:
            await self.close_position(p)
        return len(positions)

    # MARK: Stats

    async def trades(self, since: datetime | None = None, limit=50) -> list[NadoTrade]:
        cursor: int | None = None
        has_more = True

        items: dict[str, NadoTrade] = {}
        while has_more:
            pld = {"orders": {"subaccounts": [self.sender], "limit": limit}}
            if cursor is not None:
                pld["orders"]["idx"] = cursor
                await asyncio.sleep(0.3)  # be nice to the archive API when paginating

            res = await self._archive(pld)
            res = res.get("orders", [])
            cursor = res[-1]["submission_idx"] if res else None
            has_more = len(res) >= limit

            for o in res:
                created_ms = (int(o["nonce"]) >> 20) - 100_000
                s = await self.symbol_info(product_id=o["product_id"])
                amount = _from_x18(o["amount"])
                t = NadoTrade(
                    digest=o["digest"],
                    product_id=o["product_id"],
                    symbol=s.symbol,
                    submission_idx=o["submission_idx"],
                    side="bid" if amount > 0 else "ask",
                    amount=abs(amount),
                    price=_from_x18(o["price_x18"]),
                    volume=abs(_from_x18(o["quote_filled"])),
                    fee=_from_x18(o["fee"]),
                    realized_pnl=_from_x18(o["realized_pnl"]),
                    created_at=datetime.fromtimestamp(created_ms / 1000, tz=UTC),
                )
                items[t.digest] = t
                if since and t.created_at <= since:
                    has_more = False
                    break

        return sorted(items.values(), key=lambda t: t.created_at)

    async def points(self) -> list[NadoPoint]:
        res = await self._archive({"nado_points": {"address": self.address}})

        return [
            NadoPoint(
                epoch=int(x["epoch"]),
                description=x["description"],
                since=datetime.fromtimestamp(int(x["start_time"]), tz=UTC),
                until=datetime.fromtimestamp(int(x["end_time"]), tz=UTC),
                points=_from_x18(x["points"]),
                rank=int(x["rank"]),
                tier=int(x["tier"]),
            )
            for x in res.get("points_per_epoch") or []
        ]

    async def points_total(self) -> Decimal:
        pts = await self.points()
        return Decimal(sum(p.points for p in pts))

    async def profile(self) -> ProfileInfo:
        # TODO: find a faster aggregate endpoint for volume/pnl on nado API
        bal, pts, trades = await asyncio.gather(self.balance(), self.points_total(), self.trades())
        vol = sum((t.volume for t in trades), Decimal(0))
        pnl = sum((t.realized_pnl - t.fee for t in trades), Decimal(0))
        return ProfileInfo(
            addr=utils.short_addr(self.address), balance=bal, volume=vol, pnl=pnl, points=pts
        )
