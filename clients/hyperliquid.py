# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
import struct
import time
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from math import floor, log10
from typing import Any, NoReturn, Self

import msgpack
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak
from rich import print as rprint

from lib import utils
from lib.decorators import bind_log_context, ttl_cache
from lib.http import ApiError, AsyncHttp
from lib.logger import logger
from lib.models import AccountConfig
from strategy import Order, OrderBook, OrderStatus, Position, ProfileInfo, Side, TradingClient

HL_API = "https://api.hyperliquid.xyz"

_AGENT_DOMAIN = {
    "name": "Exchange",
    "version": "1",
    "chainId": 1337,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}
_AGENT_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Agent": [
        {"name": "source", "type": "string"},
        {"name": "connectionId", "type": "bytes32"},
    ],
}
_USER_SIGN_DOMAIN = {
    "name": "HyperliquidSignTransaction",
    "version": "1",
    "chainId": 42161,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}
_USER_SET_ABSTRACTION_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "HyperliquidTransaction:UserSetAbstraction": [
        {"name": "hyperliquidChain", "type": "string"},
        {"name": "user", "type": "address"},
        {"name": "abstraction", "type": "string"},
        {"name": "nonce", "type": "uint64"},
    ],
}


def _fmt(x: Decimal) -> str:
    """Format decimal for wire: normalize then no scientific notation."""
    return f"{Decimal(f'{x:.8f}').normalize():f}"


# MARK: Client


@bind_log_context
class HyperLiquidClient:
    exchange: str  # set by subclass
    # HIP-3 market namespace. Two roles:
    # 1. API routing — passed as `dex=` param in HL info/exchange calls
    # 2. Input normalization — _coin() adds prefix to plain symbols (e.g. "ETH" → "hyna:ETH")
    #    All output (positions, orders) always returns full coin names (e.g. "hyna:ETH").
    # "" = native HyperLiquid markets (no prefix, no dex param)
    dex_prefix: str = ""

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return HyperLiquidClient

    @classmethod
    def from_config(cls, cfg: AccountConfig) -> Self:
        return cls(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.name = name
        self.account: LocalAccount = utils.parse_eth_key(privkey, name)
        self.address: str = self.account.address
        self.http = AsyncHttp(
            baseurl=HL_API,
            headers={"Content-Type": "application/json"},
            proxy=proxy,
        )

    # MARK: Infra

    def _coin(self, symbol: str) -> str:
        return f"{self.dex_prefix}:{symbol}" if self.dex_prefix else symbol

    @staticmethod
    def _sym_dex(symbol: str) -> str:
        """Extract DEX prefix from symbol, e.g. 'xyz:TSLA' → 'xyz'."""
        return symbol.split(":")[0] if ":" in symbol else ""

    def _resolve(self, symbol: str) -> tuple[str, str]:
        """Returns (dex, coin_on_wire). Handles 'xyz:TSLA' and plain symbol with prefix."""
        dex = self._sym_dex(symbol)
        return (dex, symbol) if dex else (self.dex_prefix, self._coin(symbol))

    async def _info(self, **kwargs: Any) -> Any:
        rep = await self.http.request("POST", "/info", json=kwargs)
        if not rep.ok:
            raise ApiError("Info error", rep)
        return rep.json()

    async def _fetch_fills(self, since: datetime | None = None, *, aggregate=True) -> list[dict]:
        """Fetch all fills via userFillsByTime (paginated), no coin filter."""
        start_ms = int(since.timestamp() * 1000) if since else 0
        all_fills: list[dict] = []
        while True:
            page: list[dict] = await self._info(
                type="userFillsByTime",
                user=self.address,
                startTime=start_ms,
                aggregateByTime=aggregate,
            )
            all_fills.extend(page)
            if len(page) < 2000:
                break
            start_ms = page[-1]["time"] + 1
        return all_fills

    async def fetch_fills(self, since: datetime | None = None) -> list[dict]:
        """Fetch fills filtered by dex_prefix:
        non-empty prefix → only fills with matching 'dex:' coin prefix;
        empty prefix → only native HL fills (no ':' in coin)."""
        all_fills = await self._fetch_fills(since)
        if self.dex_prefix:
            return [f for f in all_fills if f["coin"].startswith(f"{self.dex_prefix}:")]
        return [f for f in all_fills if ":" not in f["coin"]]

    def _sign_l1_action(self, action: dict, nonce: int) -> dict:
        data: bytes = msgpack.packb(action, use_bin_type=True)  # type: ignore[assignment]
        data += struct.pack(">Q", nonce)
        data += b"\x00"
        connection_id = keccak(data)
        payload = {
            "types": _AGENT_TYPES,
            "domain": _AGENT_DOMAIN,
            "primaryType": "Agent",
            "message": {"source": "a", "connectionId": connection_id},
        }
        signed = self.account.sign_message(encode_typed_data(full_message=payload))
        r, s, v = signed.r, signed.s, signed.v
        return {"r": f"0x{r:064x}", "s": f"0x{s:064x}", "v": v}

    def _sign_user_set_abstraction(self, msg: dict) -> dict:
        payload = {
            "types": _USER_SET_ABSTRACTION_TYPES,
            "domain": _USER_SIGN_DOMAIN,
            "primaryType": "HyperliquidTransaction:UserSetAbstraction",
            "message": msg,
        }
        signed = self.account.sign_message(encode_typed_data(full_message=payload))
        return {"r": f"0x{signed.r:064x}", "s": f"0x{signed.s:064x}", "v": signed.v}

    _builder: dict | None = None  # set by subclass to include builder fee in orders

    async def _exchange(self, action: dict) -> dict:
        nonce = int(time.time() * 1000)
        if self._builder and action.get("type") == "order":
            action = {**action, "builder": self._builder}
        sig = self._sign_l1_action(action, nonce)
        payload: dict = {"action": action, "nonce": nonce, "signature": sig}
        rep = await self.http.request("POST", "/exchange", json=payload)
        if not rep.ok:
            raise ApiError("Exchange error", rep)
        res = rep.json()
        if res is None:
            raise ApiError("Exchange returned null", rep)
        if res.get("status") != "ok":
            raise ApiError(f"Exchange rejected: {res}")
        return res

    # MARK: Metadata

    @ttl_cache(3600)
    async def _meta_for(self, dex: str) -> dict:
        return await self._info(type="meta", **({} if not dex else {"dex": dex}))

    @ttl_cache(5)
    async def _ctxs_for(self, dex: str) -> list[dict]:
        return (await self._info(type="metaAndAssetCtxs", **({} if not dex else {"dex": dex})))[1]

    @ttl_cache(3600)
    async def _dex_idx_for(self, dex: str) -> int:
        for i, d in enumerate(await self._info(type="perpDexs")):
            if d and d.get("name") == dex:
                return i
        raise ApiError(f"DEX not found: {dex}")

    async def _symbol_not_found(self, symbol: str) -> NoReturn:
        msg = f"Symbol not found: {symbol!r}"
        if ":" not in symbol:
            for d in await self._dex_names():
                for asset in (await self._meta_for(d))["universe"]:
                    if asset["name"].endswith(f":{symbol}"):
                        raise ApiError(f"{msg} (did you mean {asset['name']!r}?)")

        raise ApiError(msg)

    async def _asset_id(self, symbol: str) -> int:
        dex, coin = self._resolve(symbol)
        meta = await self._meta_for(dex)
        for local_idx, asset in enumerate(meta["universe"]):
            if asset["name"] == coin:
                if not dex:
                    return local_idx
                dex_idx = await self._dex_idx_for(dex)
                return dex_idx * 10000 + 100000 + local_idx
        await self._symbol_not_found(symbol)

    async def _asset_ctx(self, symbol: str) -> dict:
        dex, coin = self._resolve(symbol)
        meta = await self._meta_for(dex)
        ctxs = await self._ctxs_for(dex)
        for i, asset in enumerate(meta["universe"]):
            if asset["name"] == coin:
                return ctxs[i]
        await self._symbol_not_found(symbol)

    # MARK: Lifecycle

    async def login(self, *, force: bool = False) -> None:
        return self.http.clear_cookies() if force else None

    async def registered(self) -> bool:
        rep = await self._info(type="legalCheck", user=self.address)
        return rep.get("userAllowed", False)

    # MARK: Market info

    @ttl_cache(3600)
    async def get_symbols(self) -> list[str]:
        meta = await self._meta_for(self.dex_prefix)
        return [a["name"] for a in meta["universe"] if not a.get("isDelisted")]

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        return True

    async def get_lot_size(self, symbol: str) -> Decimal:
        dex, coin = self._resolve(symbol)
        for asset in (await self._meta_for(dex))["universe"]:
            if asset["name"] == coin:
                return Decimal(10) ** -asset["szDecimals"]
        await self._symbol_not_found(symbol)

    async def get_tick_size(self, symbol: str) -> Decimal:
        mid = float(await self.get_price(symbol))
        return Decimal(10) ** (floor(log10(mid)) - 4)

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return Decimal(10)

    # MARK: Prices

    @ttl_cache(5)
    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        ctx = await self._asset_ctx(symbol)
        impact = ctx["impactPxs"]
        return Decimal(str(impact[0])), Decimal(str(impact[1]))

    @ttl_cache(5)
    async def get_order_book(self, symbol: str) -> OrderBook:
        dex, coin = self._resolve(symbol)
        rep = await self._info(type="l2Book", coin=coin, **({} if not dex else {"dex": dex}))
        levels = rep
        if isinstance(rep, dict):
            levels = rep.get("levels") or rep.get("data", {}).get("levels")
        if not isinstance(levels, list) or len(levels) < 2:
            raise ApiError(f"Unexpected l2Book response for {symbol}: {rep}")

        return OrderBook.build(
            bids=[(x["px"], x["sz"]) for x in levels[0][:5]],
            asks=[(x["px"], x["sz"]) for x in levels[1][:5]],
        )

    async def get_price(self, symbol: str) -> Decimal:
        ctx = await self._asset_ctx(symbol)
        return Decimal(str(ctx["midPx"]))

    # MARK: Account

    async def _spot_balance(self, coin: str = "USDC") -> Decimal:
        rep = await self._info(type="spotClearinghouseState", user=self.address)
        for bal in rep.get("balances", []):
            if bal.get("coin") == coin:
                return Decimal(str(bal.get("total", 0)))
        return Decimal(0)

    async def balance(self) -> Decimal:
        mode = await self.account_mode()
        if mode in ("unifiedAccount", "portfolioMargin"):
            return await self._spot_balance()

        rep = await self._info(type="clearinghouseState", user=self.address)
        return Decimal(str(rep["marginSummary"]["accountValue"]))

    async def account_mode(self) -> str:
        try:
            data = await self._info(type="userAbstraction", user=self.address)
        except ApiError:
            return "unknown"

        return data if isinstance(data, str) else "unknown"

    async def set_account_mode(self, mode: str = "unifiedAccount") -> None:
        if mode not in ("disabled", "unifiedAccount", "portfolioMargin"):
            raise ValueError(f"unsupported account mode: {mode}")

        nonce = int(time.time() * 1000)
        msg = {
            "hyperliquidChain": "Mainnet",
            "user": self.address.lower(),
            "abstraction": mode,
            "nonce": nonce,
        }
        action = {
            "type": "userSetAbstraction",
            "hyperliquidChain": msg["hyperliquidChain"],
            "signatureChainId": "0xa4b1",
            "user": msg["user"],
            "abstraction": msg["abstraction"],
            "nonce": nonce,
        }
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": self._sign_user_set_abstraction(msg),
        }
        rep = await self.http.request("POST", "/exchange", json=payload)
        if not rep.ok:
            raise ApiError("Exchange error", rep)
        res = rep.json()
        if res.get("status") != "ok":
            raise ApiError(f"Exchange rejected: {res}")

    async def get_leverage(self, symbol: str) -> int | None:
        dex, coin = self._resolve(symbol)
        rep = await self._info(
            type="clearinghouseState", user=self.address, **({} if not dex else {"dex": dex})
        )
        for pos in rep.get("assetPositions", []):
            p = pos["position"]
            if p["coin"] == coin:
                lev = p.get("leverage", {})
                val = int(lev.get("value", 0))
                return val if val else None
        return None

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        asset_id = await self._asset_id(symbol)
        action = {
            "type": "updateLeverage",
            "asset": asset_id,
            "isCross": False,
            "leverage": leverage,
        }
        await self._exchange(action)

    # MARK: Positions

    async def _dex_names(self) -> list[str]:
        # HL API: clearinghouseState `dex` param defaults to "" (native HL only), no "all" option.
        # Must query each DEX separately. Single-DEX clients use dex_prefix; aggregators query all.
        if self.dex_prefix:
            return [self.dex_prefix]
        return [""] + [d["name"] for d in await self._info(type="perpDexs") if d]

    async def positions(self) -> list[Position]:
        dex_list = await self._dex_names()
        reps = await asyncio.gather(
            *[
                self._info(
                    type="clearinghouseState", user=self.address, **({} if not d else {"dex": d})
                )
                for d in dex_list
            ]
        )
        result = []
        for rep in reps:
            for pos in rep.get("assetPositions", []):
                p = pos["position"]
                szi = Decimal(str(p["szi"]))
                if szi == 0:
                    continue
                symbol = p["coin"]
                side: Side = "bid" if szi > 0 else "ask"
                entry_px = p.get("entryPx")
                result.append(
                    Position(
                        id=symbol,
                        symbol=symbol,
                        side=side,
                        size=abs(szi),
                        entry_price=Decimal(str(entry_px)) if entry_px else Decimal(0),
                        unrealized_pnl=Decimal(str(p.get("unrealizedPnl", 0))),
                    )
                )
        return self._filter_positions(result)

    def _filter_positions(self, positions: list[Position]) -> list[Position]:
        """Return positions owned by this client. Override in subclasses to scope by DEX."""
        return positions

    async def close_position(self, position: Position) -> bool:
        close_side: Side = "ask" if position.side == "bid" else "bid"
        await self.market_order(position.symbol, close_side, position.size, reduce_only=True)
        return True

    async def close_all_positions(self) -> int:
        positions = await self.positions()
        for p in positions:
            await self.close_position(p)
        return len(positions)

    # MARK: Orders

    async def _place_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        tif: str,
        reduce_only=False,
    ) -> Order:
        asset_id = await self._asset_id(symbol)
        order_wire = {
            "a": asset_id,
            "b": side == "bid",
            "p": _fmt(price),
            "s": _fmt(qty),
            "r": reduce_only,
            "t": {"limit": {"tif": tif}},
        }
        res = await self._exchange({"type": "order", "orders": [order_wire], "grouping": "na"})
        statuses = res["response"]["data"]["statuses"]
        st = statuses[0]
        if "error" in st:
            raise ApiError(f"Order rejected: {st['error']}")

        if "filled" in st:
            filled_data = st["filled"]
            return Order(
                id=str(filled_data["oid"]),
                symbol=symbol,
                side=side,
                size=qty,
                filled=Decimal(str(filled_data.get("totalSz", qty))),
                price=Decimal(str(filled_data.get("avgPx", price))),
                status=OrderStatus.FILLED,
                reduce_only=reduce_only,
            )

        oid = str(st["resting"]["oid"])
        return Order(
            id=oid,
            symbol=symbol,
            side=side,
            size=qty,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
            reduce_only=reduce_only,
        )

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        mid, tick = await asyncio.gather(self.get_price(symbol), self.get_tick_size(symbol))
        slippage = Decimal("1.05") if side == "bid" else Decimal("0.95")
        price = utils.round_to_tick_size(mid * slippage, tick)
        return await self._place_order(symbol, side, qty, price, "FrontendMarket", reduce_only)

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        return await self._place_order(symbol, side, qty, price, "Gtc", reduce_only)

    async def get_order(self, order_id: str) -> Order | None:
        rep = await self._info(
            type="orderStatus",
            user=self.address,
            oid=int(order_id),
        )
        if rep.get("status") != "order":
            return None
        o = rep["order"]["order"]
        status_str = rep["order"]["status"]
        if status_str == "filled":
            status = OrderStatus.FILLED
        elif status_str == "open":
            status = OrderStatus.OPEN
        else:
            status = OrderStatus.CANCELED
        sz = Decimal(str(o["sz"]))
        orig_sz = Decimal(str(o["origSz"]))
        return Order(
            id=order_id,
            symbol=o["coin"],
            side="bid" if o["side"] == "B" else "ask",
            size=orig_sz,
            filled=orig_sz - sz,
            price=Decimal(str(o["limitPx"])),
            status=status,
            reduce_only=o.get("reduceOnly", False),
        )

    async def cancel_order(self, order: Order) -> bool:
        asset_id = await self._asset_id(order.symbol)
        action = {"type": "cancel", "cancels": [{"a": asset_id, "o": int(order.id)}]}
        try:
            await self._exchange(action)
            return True
        except ApiError as e:
            logger.warning(f"cancel_order failed: {e}")
            return False

    async def cancel_all_orders(self) -> int:
        dex_list = await self._dex_names()
        total = 0
        for dex in dex_list:
            rep = await self._info(
                type="openOrders", user=self.address, **({} if not dex else {"dex": dex})
            )
            orders = rep if isinstance(rep, list) else []
            if not orders:
                continue
            cancels = [{"a": await self._asset_id(o["coin"]), "o": o["oid"]} for o in orders]
            await self._exchange({"type": "cancel", "cancels": cancels})
            total += len(cancels)
        return total

    # MARK: Profile

    async def profile(self) -> ProfileInfo:
        bal, portfolio = await asyncio.gather(
            self.balance(),
            self._info(type="portfolio", user=self.address),
        )

        volume = Decimal(0)
        pnl = Decimal(0)
        for period_name, period_data in portfolio:
            if period_name == "perpAllTime":
                volume = Decimal(str(period_data.get("vlm", 0)))
                history = period_data.get("pnlHistory", [])
                pnl = Decimal(str(history[-1][1])) if history else Decimal(0)
                break

        addr = utils.short_addr(self.address)
        return ProfileInfo(addr=addr, balance=bal, volume=volume, pnl=pnl, points=Decimal(0))


def warn_legacy_hyperliquid_accounts(accounts: Sequence[str], app_name: str) -> None:
    if not accounts:
        return

    names = ", ".join(accounts)
    command = f"uv run apps/{app_name.lower()}.py migrate"
    rprint(
        "[cyan]*[/cyan] Hyperliquid Unified Account is recommended; "
        f"other account modes are best-effort and not guaranteed: {names}. "
        f"Run [cyan]{command}[/cyan] or migrate in {app_name}/Hyperliquid UI."
    )


async def migrate_hyperliquid_accounts(accs: Sequence[HyperLiquidClient], app_name: str) -> None:
    modes = await asyncio.gather(*[acc.account_mode() for acc in accs])
    todo = [(acc, mode) for acc, mode in zip(accs, modes) if mode != "unifiedAccount"]
    if not todo:
        logger.info("All accounts are already using Hyperliquid Unified Account")
        return

    names = ", ".join(acc.name for acc, _ in todo)
    logger.warning(f"Migrating {app_name} accounts to Hyperliquid Unified Account: {names}")
    results = await asyncio.gather(
        *[acc.set_account_mode("unifiedAccount") for acc, _ in todo], return_exceptions=True
    )
    for (acc, old_mode), res in zip(todo, results):
        if isinstance(res, Exception):
            logger.error(f"{acc.name}: migration failed from {old_mode!r}: {res}")
        else:
            logger.success(f"{acc.name}: migrated from {old_mode!r} to 'unifiedAccount'")
