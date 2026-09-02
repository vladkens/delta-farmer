# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | If it compiles, ship it
import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Self

from eth_account.messages import encode_defunct
from pydantic import AliasPath, BaseModel, ConfigDict, Field

from lib import utils
from lib.decorators import bind_log_context, locked, retry, retry_on, ttl_cache
from lib.http import ApiError, AsyncHttp, HttpMethod
from lib.logger import logger
from lib.models import AccountConfig
from lib.unwaf_cf import USER_AGENT, ensure_cf_clearance, solve_managed_cf_clearance
from strategy import (
    Order,
    OrderBook,
    OrderStatus,
    Position,
    ProfileInfo,
    Side,
    TradingClient,
)
from strategy.execution import EntryQuality

API_URL = "https://omni.variational.io/api"
APP_URL = "https://omni.variational.io"

_PAGINATION_DELAY = 1.0
# Season was announced on Dec 17, but Omni UI labels week 1 as starting 6 days earlier.
_POINTS_GENESIS = datetime(2025, 12, 17 - 6, tzinfo=UTC)


class CloudflareClearanceUpdated(Exception):
    """Cloudflare clearance was refreshed instead of replaying a request."""


def _volume_field_total(value: Any) -> Decimal:
    if isinstance(value, dict):
        value = value.get("total") or value.get("current")
    return Decimal(str(value or "0"))


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


class OmniProfileInfo(ProfileInfo):
    referral_boost: Decimal = Decimal(0)


class OmniSupportedAsset(BaseModel):
    asset: str
    has_perp: bool = False
    instrument_type: str | None = None
    asset_class: str | None = None
    is_close_only_mode: bool = False


class OmniOrder(BaseModel):
    id: str = Field(validation_alias="rfq_id")
    created_at: datetime
    market: str = Field(validation_alias=AliasPath("instrument", "underlying"))
    qty: Decimal
    side: str
    status: str
    is_reduce_only: bool
    limit_price: Decimal | None
    price: Decimal | None = None  # execution price (present when cleared)


class OmniPosition(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)
    symbol: str = Field(validation_alias=AliasPath("position_info", "instrument", "underlying"))
    qty: Decimal = Field(validation_alias=AliasPath("position_info", "qty"))
    entry_price: Decimal = Field(validation_alias=AliasPath("position_info", "avg_entry_price"))


class OmniPoint(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)
    start_window: datetime
    total_points: Decimal


class OmniLeaderboardSelf(BaseModel):
    pnl: Decimal = Decimal(0)
    place: int | None = None


class OmniCompetitionUser(BaseModel):
    leaderboard_name: str | None = None
    volume_total: Decimal | None = None
    volume_rank: int | None = None
    pnl_total: Decimal | None = None
    pnl_rank: int | None = None
    roi_total: Decimal | None = None
    roi_rank: int | None = None


class OmniCompetitionStatus(BaseModel):
    start_time: datetime
    end_time: datetime
    volume_threshold: Decimal
    ongoing: bool
    user: OmniCompetitionUser | None = None


# MARK: Client


@bind_log_context
class OmniClient:
    exchange = "omni"

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return OmniClient

    @classmethod
    def to_week_label(cls, dt: datetime) -> str:
        return utils.to_period_week(dt, genesis=_POINTS_GENESIS)

    @classmethod
    def from_config(cls, cfg: AccountConfig) -> Self:
        return cls(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.account = utils.parse_eth_key(privkey, name)
        self.address = self.account.address
        self.name = name
        self.proxy = proxy
        self.http = AsyncHttp(
            baseurl=API_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Origin": APP_URL,
                "Referer": f"{APP_URL}/",
                "vr-connected-address": self.address,
            },
            proxy=proxy,
            cookies_file=f".cache/omni_{utils.short_addr(self.address)}_http.pkl",
        )

    async def _request(
        self, method: HttpMethod, url: str, *, replay_after_cf: bool = True, **kwargs
    ):
        rep = await self.http.request(method, url, **kwargs)
        is_cf_challenge = rep.status_code in {403, 429, 503} and (
            ">Just a moment...<" in rep.text or "/cdn-cgi/challenge-platform/" in rep.text
        )
        if not is_cf_challenge:
            return rep

        target_url = (
            url if url.startswith(("http://", "https://")) else f"{API_URL}/{url.lstrip('/')}"
        )
        if "cType: 'managed'" in rep.text:
            logger.debug(f"Cloudflare Managed Challenge {method} {url}; requesting cf_clearance")
            await solve_managed_cf_clearance(self.http, target_url, proxy=self.proxy)
        else:
            logger.debug(f"Cloudflare JSD challenged {method} {url}; refreshing cf_clearance")
            if not await ensure_cf_clearance(self.http, target_url, force=True):
                return rep

        if not replay_after_cf:
            raise CloudflareClearanceUpdated(
                "Cloudflare clearance ready; restarting login with fresh signing data"
            )

        return await self.http.request(method, url, **kwargs)

    async def _prepare_login(self) -> None:
        rep = await self._request("GET", f"{APP_URL}/portfolio?tab=positions")  # app access
        assert rep.ok, f"Login preparation failed: {rep.status_code} {rep.text[:200]}"

        rep = await self._request("GET", "/banner")  # api access
        assert rep.ok, f"Login preparation failed: {rep.status_code} {rep.text[:200]}"

    @retry(max_attempts=3, delay=2.0)  # bypass cloudflare
    async def registered(self) -> bool:
        rep = await self._request("GET", f"/auth/company/{self.address}")
        rep.raise_for_status()
        res = rep.json()
        return res["settlement_pool"] is not None

    @retry_on(CloudflareClearanceUpdated, retries=1)
    async def _login(self) -> None:
        await self._prepare_login()
        pld = {"address": self.address}
        rep = await self._request("POST", "/auth/generate_signing_data", json=pld)
        if not rep.text.startswith("omni.variational.io wants you to"):
            raise ApiError("Unexpected signing data", rep)

        msg = encode_defunct(text=rep.text)
        sig = self.account.sign_message(msg).signature.hex().replace("0x", "")

        pld = {"address": self.address, "signed_message": sig}
        rep = await self._request("POST", "/auth/login", json=pld, replay_after_cf=False)
        if not rep.ok or "vr-token" not in self.http.session.cookies:
            raise ApiError("Login failed", rep)

    def _clear_auth(self) -> None:
        self.http.session.cookies.pop("vr-token", None)

    async def _check_auth(self) -> bool:
        self.http.load_cookies()
        if "vr-token" not in self.http.session.cookies:
            return False
        rep = await self._request("GET", "/portfolio?compute_margin=true")
        if rep.status_code == 401:
            return False
        if not rep.ok:
            raise ApiError("Auth check failed", rep)
        return True

    @locked
    async def login(self, *, force: bool = False) -> None:
        self.http.clear_cookies() if force else None
        if force or not await self._check_auth():
            self._clear_auth()
            await self._login()

    async def _call(self, method: HttpMethod, path: str, **kwargs):
        self.http.load_cookies()
        if "vr-token" not in self.http.session.cookies:
            await self.login()
        rejected_token = self.http.session.cookies.get("vr-token")
        rep = await self._request(method, path, **kwargs)
        if rep.status_code == 401:
            if self.http.session.cookies.get("vr-token") == rejected_token:
                self._clear_auth()
            await self.login()
            rep = await self._request(method, path, **kwargs)
        if not rep.ok:
            raise ApiError("API error", rep)
        return rep.json()

    # MARK: Account

    @ttl_cache(5)
    async def balance(self) -> Decimal:
        res = await self._call("GET", "/portfolio?compute_margin=true")
        return Decimal(res["balance"])

    @ttl_cache(600)
    async def supported_assets(self) -> dict[str, OmniSupportedAsset]:
        res = await self._call("GET", "/metadata/supported_assets")
        items: dict[str, OmniSupportedAsset] = {}
        for asset, variants in res.items():
            if not variants:
                continue
            item = OmniSupportedAsset(**variants[0])
            items[asset] = item
        return items

    async def _instrument(self, symbol: str) -> dict[str, Any]:
        assets = await self.supported_assets()
        asset = assets.get(symbol)
        if asset and asset.instrument_type == "perpetual_rwa_future":
            return {
                "underlying": symbol,
                "settlement_asset": "USDC",
                "instrument_type": "perpetual_rwa_future",
                "kind": asset.asset_class,
            }

        return {
            "underlying": symbol,
            "funding_interval_s": 3600,
            "settlement_asset": "USDC",
            "instrument_type": "perpetual_future",
        }

    async def get_symbols(self) -> list[str]:
        assets = await self.supported_assets()
        return [asset.asset for asset in assets.values() if asset.has_perp]

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        assets = await self.supported_assets()
        asset = assets.get(symbol)
        if asset is None or not asset.has_perp:
            return False
        return reduce_only or not asset.is_close_only_mode

    async def _quote(self, asset: str, qty: Decimal | int | float) -> IndicativeQuote:
        pld = {
            "instrument": await self._instrument(asset),
            "qty": str(qty),
        }
        res = await self._call("POST", "/quotes/indicative", json=pld)
        return IndicativeQuote(**res)

    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        q = await self._quote(symbol, 1)
        return q.bid, q.ask

    @ttl_cache(5)
    async def get_order_book(self, symbol: str) -> OrderBook:
        lot = await self.get_lot_size(symbol)
        steps = [lot, lot * 2, lot * 5, lot * 10, lot * 20]
        quotes = await asyncio.gather(*[self._quote(symbol, qty) for qty in steps])

        # Omni does not expose native L2 depth in this client. Build a synthetic book
        # from indicative quotes so downstream code can reason about price deterioration.
        bids = [(q.bid, q.qty) for q in quotes]
        asks = [(q.ask, q.qty) for q in quotes]
        return OrderBook.build(bids=bids, asks=asks)

    async def estimate_entry_quality(
        self, symbol: str, legs: Sequence[tuple[Side, Decimal]]
    ) -> EntryQuality:
        bid_qty = sum((qty for side, qty in legs if side == "bid"), Decimal(0))
        ask_qty = sum((qty for side, qty in legs if side == "ask"), Decimal(0))
        quote_qty = max(bid_qty, ask_qty)
        if quote_qty <= 0:
            return EntryQuality(None, None, None)

        quote = await self._quote(symbol, quote_qty)
        avg_bid_price = quote.ask if bid_qty else None
        avg_ask_price = quote.bid if ask_qty else None
        entry_spread_pct = (
            abs(avg_ask_price - avg_bid_price) / avg_ask_price * 100
            if avg_bid_price is not None and avg_ask_price is not None and avg_ask_price > 0
            else None
        )
        return EntryQuality(
            avg_bid_price=avg_bid_price,
            avg_ask_price=avg_ask_price,
            entry_spread_pct=entry_spread_pct,
            bid_qty=bid_qty,
            ask_qty=ask_qty,
            bid_depth=quote.qty,
            ask_depth=quote.qty,
        )

    async def get_price(self, symbol: str) -> Decimal:
        return (await self._quote(symbol, 1)).mark_price

    async def get_lot_size(self, symbol: str) -> Decimal:
        return (await self._quote(symbol, 1)).qty_tick

    async def get_tick_size(self, symbol: str) -> Decimal:
        return Decimal("0.01")

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return Decimal(10)  # TODO: derive from API

    # MARK: Leverage

    async def get_leverage(self, symbol: str) -> int:
        res = await self._call("POST", "/settlement_pools/leverage", json={"assets": [symbol]})
        return int(res[symbol]["current"])

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        assert 1 <= leverage <= 50, "Leverage must be between 1 and 50"
        dat = {"leverage": leverage, "asset": symbol}
        res = await self._call("POST", "/settlement_pools/set_leverage", json=dat)
        assert int(res["current"]) == leverage

    # MARK: Orders

    async def get_order(self, order_id: str) -> Order | None:
        pld = {"order_by": "created_at", "order": "desc", "limit": 20, "offset": 0}
        res = await self._call("GET", "/orders/v2", params=pld)
        item = next((x for x in res.get("result", []) if x.get("rfq_id") == order_id), None)
        if item is None:
            return None
        o = OmniOrder(**item)
        status_map = {
            "filled": OrderStatus.FILLED,
            "cleared": OrderStatus.FILLED,
            "pending": OrderStatus.OPEN,
        }
        status = status_map.get(o.status, OrderStatus.CANCELED)
        return Order(
            id=o.id,
            symbol=o.market,
            side="bid" if o.side == "buy" else "ask",
            size=o.qty,
            filled=o.qty if status == OrderStatus.FILLED else Decimal(0),
            price=o.price or o.limit_price,
            status=status,
            reduce_only=o.is_reduce_only,
        )

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        signed_qty = qty if side == "bid" else -qty
        quote = await self._quote(symbol, abs(signed_qty))
        pld = {
            "quote_id": quote.quote_id,
            "side": "buy" if side == "bid" else "sell",
            "max_slippage": 0.001 if reduce_only else 0.005,
            "is_reduce_only": reduce_only,
        }
        url = "/quotes/accept" if reduce_only else "/orders/new/market"
        res = await self._call("POST", url, json=pld)
        order_id = res.get("rfq_id", res.get("order_id", ""))
        return Order(
            id=str(order_id),
            symbol=symbol,
            side=side,
            size=qty,
            filled=qty,
            price=None,
            status=OrderStatus.FILLED,
            reduce_only=reduce_only,
        )

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        pld = {
            "order_type": "limit",
            "limit_price": str(price),
            "side": "buy" if side == "bid" else "sell",
            "instrument": await self._instrument(symbol),
            "qty": str(qty),
            "is_auto_resize": False,
            "use_mark_price": False,
            "is_reduce_only": reduce_only,
        }
        res = await self._call("POST", "/orders/new/limit", json=pld)
        return Order(
            id=str(res["rfq_id"]),
            symbol=symbol,
            side=side,
            size=qty,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
            reduce_only=reduce_only,
        )

    async def cancel_order(self, order: Order) -> bool:
        try:
            res = await self._call("POST", "/orders/cancel", json={"rfq_id": order.id})
            logger.debug(f"Cancel order response: {res}")
            return True
        except Exception:
            return False

    async def cancel_all_orders(self) -> int:
        pld = {"status": "pending", "order_by": "created_at", "order": "desc"}
        pld = {**pld, "limit": 20, "offset": 0}

        items = []
        while True:
            res = await self._call("GET", "/orders/v2", params=pld)
            items.extend(res.get("result", []))
            pld["offset"] += pld["limit"]
            if not res.get("pagination", {}).get("next_page"):
                break
            await asyncio.sleep(_PAGINATION_DELAY)

        for x in items:
            await self._call("POST", "/orders/cancel", json={"rfq_id": x["rfq_id"]})

        return len(items)

    # MARK: Positions

    async def positions(self) -> list[Position]:
        items = await self._call("GET", "/positions")
        raw = [OmniPosition(**x) for x in items]
        return [
            Position(
                id=p.symbol,
                symbol=p.symbol,
                side="bid" if p.qty > 0 else "ask",
                size=abs(p.qty),
                entry_price=p.entry_price,
            )
            for p in raw
            if p.qty != 0
        ]

    async def close_position(self, position: Position) -> bool:
        if position.size == 0:
            return True
        signed_qty = -position.size if position.side == "bid" else position.size
        quote = await self._quote(position.symbol, abs(signed_qty))
        pld = {
            "quote_id": quote.quote_id,
            "side": "sell" if signed_qty < 0 else "buy",
            "max_slippage": 0.001,
            "is_reduce_only": True,
        }
        await self._call("POST", "/quotes/accept", json=pld)
        return True

    async def close_all_positions(self) -> int:
        items = await self._call("GET", "/positions")
        raw = [OmniPosition(**x) for x in items]
        count = 0
        for p in raw:
            if p.qty != 0:
                quote = await self._quote(p.symbol, abs(p.qty))
                pld = {
                    "quote_id": quote.quote_id,
                    "side": "sell" if p.qty > 0 else "buy",
                    "max_slippage": 0.001,
                    "is_reduce_only": True,
                }
                await self._call("POST", "/quotes/accept", json=pld)
                count += 1
        return count

    # MARK: Stats

    async def fetch_history(self, endpoint: str, since: datetime | None = None) -> list[Any]:
        since = since or datetime(2026, 1, 1, tzinfo=UTC)
        until = datetime.now(tz=UTC).replace(hour=23, minute=59, second=59, microsecond=999000)
        pld = {
            "order_by": "created_at",
            "order": "desc",
            "limit": 20,
            "offset": 0,
            "created_at_gte": since.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "created_at_lte": until.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        items = []
        while True:
            res = await self._call("GET", endpoint, params=pld)
            pagination = res.get("pagination", {})
            if pld["offset"] == 0:
                count = int(pagination.get("object_count", 0))
                pages = (count + pld["limit"] - 1) // pld["limit"]
                if pages > 10:
                    delay = utils.format_duration((pages - 1) * _PAGINATION_DELAY)
                    name = endpoint.strip("/")
                    logger.warning(
                        f"Fetching {count:,} {name} across {pages} pages; "
                        f"pagination delay: ~{delay}"
                    )
            items.extend(res.get("result", []))
            pld["offset"] += pld["limit"]
            if not pagination.get("next_page"):
                break
            await asyncio.sleep(_PAGINATION_DELAY)
        return items

    async def points(self) -> list[OmniPoint]:
        records = await self._call("GET", "/points/history", params={"limit": 20})
        return [OmniPoint(**r) for r in records if Decimal(r["total_points"]) > 0]

    async def points_total(self) -> PointsInfo:
        res = await self._call("GET", "/points/summary")
        return PointsInfo(**res) if res else PointsInfo(total_points=Decimal(0))

    async def _referral_summary(self) -> tuple[Decimal, str | None, Decimal]:
        raw = await self._call("GET", "/referrals/summary")
        res = raw if isinstance(raw, dict) else {}
        vol = _volume_field_total(res.get("own_volume") or res.get("trade_volume"))
        referred_by = res.get("referred_by")
        referred_by = referred_by if isinstance(referred_by, dict) else {}
        ref = referred_by.get("code")
        ref = ref if isinstance(ref, str) and ref else None
        try:
            boost = max(Decimal(str(referred_by.get("points_boost", 1))) - 1, Decimal(0))
        except (InvalidOperation, TypeError, ValueError):
            boost = Decimal(0)
        return vol, ref, boost

    async def total_volume(self) -> tuple[Decimal, str | None]:
        vol, ref, _boost = await self._referral_summary()
        return vol, ref

    async def leaderboard_self(self) -> OmniLeaderboardSelf:
        params = {"limit": 20, "offset": 0, "period": "total", "ranking": "pnl"}
        res = await self._call("GET", "/leaderboard/v2", params=params)
        data = res.get("result", {}).get("self", {})
        return OmniLeaderboardSelf(**data)

    async def competition_status(self) -> OmniCompetitionStatus:
        res = await self._call("GET", "/competition/status")
        return OmniCompetitionStatus(**res)

    async def competition_opt_in(self) -> None:
        await self._call("POST", "/competition/opt_in")

    async def profile(self) -> OmniProfileInfo:
        # Omni have Cloudflare protection, so do it one by one to avoid triggering anti-bot
        bal = await self.balance()
        pts = await self.points_total()
        lb = await self.leaderboard_self()
        vol, ref, boost = await self._referral_summary()

        return OmniProfileInfo(
            addr=utils.short_addr(self.address),
            balance=bal,
            volume=vol,
            pnl=lb.pnl,
            points=pts.total_points,
            ref_code=ref,
            referral_boost=boost,
            rank=lb.place,
        )
