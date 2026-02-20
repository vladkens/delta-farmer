# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Works on my machine ¯\_(ツ)_/¯
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from eth_account import Account
from eth_account.messages import encode_defunct
from pydantic import AliasPath, BaseModel, Field

from core import logger, utils
from core.decorators import bind_log_context, retry
from core.http import AsyncHttp, HttpMethod

from .config import AccountConfig

BASE_URL = "https://omni.variational.io/api"

Number = Decimal | int | float
OrderSide = Literal["ask", "bid"]


class ApiError(Exception):
    pass


class IndicativeQuote(BaseModel):
    quote_id: str
    mark_price: Decimal
    index_price: Decimal
    bid: Decimal
    ask: Decimal
    qty: Decimal
    qty_tick: Decimal = Field(validation_alias=AliasPath("qty_limits", "bid", "min_qty_tick"))


class PointsInfo(BaseModel):
    total_points: Decimal
    rank: int | None = None


class PointsRecord(BaseModel):
    start_window: datetime
    total_points: Decimal


class Position(BaseModel):
    symbol: str = Field(validation_alias=AliasPath("position_info", "instrument", "underlying"))
    qty: Decimal = Field(validation_alias=AliasPath("position_info", "qty"))
    entry_price: Decimal = Field(validation_alias=AliasPath("position_info", "avg_entry_price"))


class Order(BaseModel):
    id: str = Field(validation_alias="rfq_id")  # api also have order_id, but this used to cancel
    created_at: datetime
    market: str = Field(validation_alias=AliasPath("instrument", "underlying"))
    qty: Decimal
    side: str  # buy/sell -> how to bind to bid/ask?
    status: str
    is_reduce_only: bool
    limit_price: Decimal | None


@bind_log_context
class Client:
    async def close(self):
        await self.http.close()

    @classmethod
    def from_config(cls, cfg: AccountConfig):
        return cls(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.account = Account.from_key(privkey)
        self.address = self.account.address
        self.name = name
        self.http = AsyncHttp(
            baseurl=BASE_URL,
            headers={
                "Origin": "https://omni.variational.io",
                "Referer": "https://omni.variational.io/",
            },
            proxy=proxy,
            cookies_file=f".cache/omni_{utils.short_addr(self.address)}.pkl",
        )

    @retry(max_attempts=9, delay=2.0)
    async def warmup(self) -> None:
        rep = await self.http.request("GET", "https://omni.variational.io/")
        assert rep.ok, f"Warmup failed: {rep.status_code} {rep.text[:200]}"

    @retry(max_attempts=3, delay=1.0)
    async def is_registered(self):
        rep = await self.http.request("GET", f"/auth/company/{self.address}")
        rep.raise_for_status()
        res = rep.json()
        return res["company"] is not None and res["settlement_pool"] is not None

    @retry(max_attempts=3, delay=1.0)
    async def _check_auth(self):
        if "vr-token" in self.http.session.cookies:
            # logger.info("Already authenticated (found vr-token cookie)")
            return True

        pld = {"address": self.address}
        rep = await self.http.request("POST", f"{BASE_URL}/auth/generate_signing_data", json=pld)
        if not rep.text.startswith("omni.variational.io wants you to"):
            raise ApiError(f"Unexpected signing data: {rep.text}")

        msg = encode_defunct(text=rep.text)
        msg = self.account.sign_message(msg)
        sig = msg.signature.hex().replace("0x", "")

        pld = {"address": self.address, "signed_message": sig}
        rep = await self.http.request("POST", f"{BASE_URL}/auth/login", json=pld)
        if not rep.ok or "vr-token" not in self.http.session.cookies:
            raise ApiError(f"Login failed: {rep.status_code} {rep.text}")

        return True

    async def call(self, method: HttpMethod, path: str, **kwargs):
        await self._check_auth()
        rep = await self.http.request(method, path, **kwargs)
        logger.trace(f">> {method} {path} response: {rep.status_code}")
        if not rep.ok:
            raise ApiError(f"API error: {rep.status_code} {rep.text}")

        return rep.json()

    async def balance(self):
        res = await self.call("GET", "/portfolio?compute_margin=true")
        return Decimal(res["balance"])

    async def points(self):
        res = await self.call("GET", "/points/summary")
        return PointsInfo(**res)

    async def points_history(self) -> list[PointsRecord]:
        records = await self.call("GET", "/points/history", params={"limit": 20})
        return [PointsRecord(**r) for r in records if Decimal(r["total_points"]) > 0]

    async def positions(self, market: str | None = None):
        items = await self.call("GET", "/positions")
        items = [Position(**item) for item in items]
        items = [x for x in items if market is None or x.symbol == market]
        return items

    async def fetch_history(self, endpoint: str, since: datetime | None = None):
        """Generic method to fetch paginated history from API endpoints (trades, transfers, etc)."""
        since = since or datetime(2026, 1, 1, tzinfo=UTC)
        until = datetime.now(tz=UTC).replace(hour=23, minute=59, second=59, microsecond=999000)

        since_str = since.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        until_str = until.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        pld = {
            "order_by": "created_at",
            "order": "desc",
            "limit": 20,
            "offset": 0,
            "created_at_gte": since_str,
            "created_at_lte": until_str,
        }

        items = []
        while True:
            res = await self.call("GET", endpoint, params=pld)
            items.extend(res.get("result", []))
            pld["offset"] += pld["limit"]

            next_page = res.get("pagination", {}).get("next_page", None)
            if not next_page:
                break

        return items

    async def total_volume(self):
        res = await self.call("GET", "/referrals/summary")
        if "trade_volume" in res:
            return Decimal(res["trade_volume"]["current"])
        elif "own_volume" in res:
            return Decimal(res["own_volume"]["total"])
        return Decimal(0)

    async def pnl(self):
        params = {"limit": 20, "offset": 0, "period": "total", "ranking": "pnl"}
        res = await self.call("GET", "/leaderboard", params=params)
        data = res.get("result", {}).get("self", {})
        return Decimal(data.get("pnl", 0))

    async def get_leverage(self, asset: str):
        pld = {"assets": [asset]}
        res = await self.call("POST", "/settlement_pools/leverage", json=pld)
        return int(res[asset]["current"])

    async def set_leverage(self, asset: str, leverage: int):
        current_leverage = await self.get_leverage(asset)
        if current_leverage == leverage:
            return

        assert 1 <= leverage <= 50, "Leverage must be between 1 and 50"
        pld = {"leverage": leverage, "asset": asset}
        res = await self.call("POST", "/settlement_pools/set_leverage", json=pld)
        assert int(res["current"]) == leverage, f"Failed to set leverage: {res}"

    async def get_indicative(self, asset: str, qty: Number):
        pld = {
            "instrument": {
                "underlying": asset,
                "funding_interval_s": 3600,
                "settlement_asset": "USDC",
                "instrument_type": "perpetual_future",
            },
            "qty": str(qty),
        }
        res = await self.call("POST", "/quotes/indicative", json=pld)
        return IndicativeQuote(**res)

    async def usd_to_qty(self, asset: str, usd: Number):
        ind = await self.get_indicative(asset, 1)
        qty = Decimal(usd) / ind.mark_price
        return utils.round_to_tick_size(qty, ind.qty_tick)

    async def market_order(self, asset: str, qty: Number, reduce_only=False):
        ind = await self.get_indicative(asset, abs(qty))
        side = "buy" if qty > 0 else "sell"
        logger.debug(f"Market {side} order: {qty} {asset} (reduce_only={reduce_only})")

        pld = {
            "quote_id": ind.quote_id,
            "side": side,
            "max_slippage": 0.001 if reduce_only else 0.005,
            "is_reduce_only": reduce_only,
        }
        url = "/quotes/accept" if reduce_only else "/orders/new/market"
        res = await self.call("POST", url, json=pld)
        return res

    async def cancel_order(self, order_id: str):
        res = await self.call("POST", "/orders/cancel", json={"rfq_id": order_id})
        logger.debug(f"Cancel order response: {res}")
        return res

    async def orders(self, status="pending", market: str | None = None):
        pld = {
            "status": status,  # canceled cleared rejected pending
            "order_by": "created_at",
            "order": "desc",
            "limit": 20,
            "offset": 0,
        }

        items = []
        while True:
            res = await self.call("GET", "/orders/v2", params=pld)
            items.extend(res.get("result", []))
            pld["offset"] += pld["limit"]

            next_page = res.get("pagination", {}).get("next_page", None)
            if not next_page:
                break

        items = [Order(**item) for item in items]
        items = [x for x in items if market is None or x.market == market]
        return items
