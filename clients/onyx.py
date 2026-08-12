# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import ClassVar, Self

import lz4.frame
from eth_account.messages import encode_defunct
from pydantic import BaseModel

from lib import utils
from lib.decorators import bind_log_context, locked, ttl_cache
from lib.http import ApiError, AsyncHttp
from lib.logger import logger
from strategy import Position, ProfileInfo, TradingClient

from .hyperliquid import HyperLiquidClient

PRIVY_API = "https://auth.privy.io"
ARJUNA_API = "https://arjuna-production.up.railway.app"
ONYX_APP = "https://app.onyx.live"
PRIVY_APP_ID = "cmcmc1m1t012bl80npg0gl99u"
_POINTS_GENESIS = datetime(2026, 3, 1, tzinfo=UTC)

_PRIVY_HEADERS = {
    "privy-app-id": PRIVY_APP_ID,
    "privy-ca-id": "b98336a6-ca35-4d8a-a3a9-93e6cb54ab6f",
    "privy-client": "react-auth:3.17.0",
    "origin": ONYX_APP,
}

_ONYX_BUILDER = {"b": "0xb290f2f3fad4e540d0550985951cdad2711ac34a", "f": 10}
_BUILDER_ADDR = _ONYX_BUILDER["b"]
_ARCHIVE_START = date(2026, 1, 1)
_ARCHIVE_CACHE = ".cache/onyx_builder.pkl"

_ARCHIVE_HTTP = AsyncHttp(baseurl="https://stats-data.hyperliquid.xyz", headers={})


# MARK: BuilderArchive


class BuilderArchive:
    """Disk-cached builder fills archive for identifying Onyx-attributed trades.

    Cache file .cache/onyx_builder.pkl is intentionally global (not per-account)
    and named to match the `clean` glob `.cache/onyx_*.pkl`.
    """

    _keys: ClassVar[set[tuple]] = set()
    _last_date: ClassVar[date | None] = None
    _loaded: ClassVar[bool] = False

    @classmethod
    def _load(cls) -> None:
        """Load cached archive data from disk. No-ops if already loaded."""
        if cls._loaded:
            return
        data = utils.pickle_load(_ARCHIVE_CACHE, lock=True)
        if data:
            cls._keys = data.get("keys", set())
            cls._last_date = data.get("last_date")
        cls._loaded = True

    @classmethod
    async def sync(cls) -> None:
        """Download and cache any missing archive days up to today - 3 days.

        Archive start is Jan 1 2026 (not GENESIS Mar 1) — GENESIS is for stats display only.
        403/404 days mean no archive data for that day and are silently skipped.
        Other errors log a warning and skip.
        """
        cls._load()
        start = (cls._last_date + timedelta(days=1)) if cls._last_date else _ARCHIVE_START
        end = datetime.now(UTC).date() - timedelta(days=3)
        if start > end:
            return

        changed = False
        day = start
        while day <= end:
            url = f"/Mainnet/builder_fills/{_BUILDER_ADDR}/{day.strftime('%Y%m%d')}.csv.lz4"
            try:
                rep = await _ARCHIVE_HTTP.request("GET", url)
                if not rep.ok and rep.status_code not in (403, 404):
                    logger.warning(f"BuilderArchive: fetch {day} failed: {rep.status_code}")
                    day += timedelta(days=1)
                    continue
                elif rep.ok:
                    content = lz4.frame.decompress(rep.content)
                    reader = csv.DictReader(io.StringIO(content.decode()))
                    for row in reader:
                        cls._keys.add(
                            (
                                int(datetime.fromisoformat(row["time"]).timestamp()),
                                row["coin"],
                                Decimal(str(row["px"])),
                                Decimal(str(row["sz"])),
                            )
                        )
            except Exception as e:
                logger.warning(f"BuilderArchive: error processing {day}: {e}")
                day += timedelta(days=1)
                continue

            cls._last_date = day
            changed = True
            day += timedelta(days=1)

        if changed:
            utils.pickle_dump(
                _ARCHIVE_CACHE, {"last_date": cls._last_date, "keys": cls._keys}, lock=True
            )

    @classmethod
    def contains(cls, time_sec: int, coin: str, px: object, sz: object) -> bool:
        """Check if a fill is in the builder archive.

        Uses Decimal(str(x)) on both ingest and lookup to avoid float precision drift.
        """
        return (time_sec, coin, Decimal(str(px)), Decimal(str(sz))) in cls._keys


# MARK: Models


class OnyxAccountSummary(BaseModel):
    totalVolume: Decimal = Decimal(0)
    totalFees: Decimal = Decimal(0)
    totalPnl: Decimal = Decimal(0)
    onyxVolume: Decimal = Decimal(0)
    onyxBoostedVolume: Decimal = Decimal(0)
    onyxNonBoostedVolume: Decimal = Decimal(0)
    onyxTradeCount: int = 0


class OnyxUserInfo(BaseModel):
    boostedWalletAddress: str | None = None
    eoaAddress: str | None = None
    accountSummary: OnyxAccountSummary = OnyxAccountSummary()


class OnyxPoint(BaseModel):
    start_window: datetime
    points: Decimal

    @classmethod
    def day0(cls, points: Decimal) -> Self:
        return cls(start_window=_POINTS_GENESIS - timedelta(weeks=1), points=points)

    @classmethod
    def from_period(cls, data: dict) -> Self | None:
        points = Decimal(str(data.get("pointsEarned", data.get("points", 0))))
        if not points:
            return None
        return cls(
            start_window=datetime.fromisoformat(data["epochStart"].replace("Z", "+00:00")),
            points=points,
        )

    @classmethod
    def from_overview(cls, data: dict) -> list[Self]:
        result: list[Self] = []

        day0 = data.get("day0Points") or {}
        day0_points = Decimal(str(day0.get("total", 0)))
        if day0_points:
            result.append(cls.day0(day0_points))

        current = data.get("currentPeriod") or {}
        if current_point := cls.from_period(current):
            result.append(current_point)

        for item in data.get("periodBreakdown") or []:
            if point := cls.from_period(item):
                result.append(point)

        return result


# MARK: Client


@bind_log_context
class OnyxClient(HyperLiquidClient):
    """Onyx is not a DEX — it has no own clearinghouse or markets.
    It injects a builder fee into every order to attribute volume on Onyx's side.
    Positions live on the underlying market (native HL, xyz, etc.) as usual.
    Specify symbols with market prefix in config: "xyz:TSLA", "BTC".
    """

    exchange = "onyx"
    dex_prefix = ""
    _builder = _ONYX_BUILDER
    _symbols: list[str] = []  # noqa: RUF012

    def _filter_positions(self, positions: list[Position]) -> list[Position]:
        explicit = {s for s in self._symbols if s.startswith("hyna:")}
        return [p for p in positions if not p.symbol.startswith("hyna:") or p.symbol in explicit]

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        return True

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return OnyxClient

    @classmethod
    def to_week_label(cls, dt: datetime) -> str:
        if _POINTS_GENESIS - timedelta(weeks=1) <= dt < _POINTS_GENESIS:
            since = _POINTS_GENESIS - timedelta(weeks=1)
            until = _POINTS_GENESIS - timedelta(days=1)
            return f"OFF {since.strftime('%b%d')}-{until.strftime('%b%d')}"
        return utils.to_period_week(dt, genesis=_POINTS_GENESIS)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        super().__init__(name, privkey, proxy)
        self._privy_http = AsyncHttp(baseurl=PRIVY_API, headers=_PRIVY_HEADERS, proxy=proxy)
        self._arjuna_http = AsyncHttp(baseurl=ARJUNA_API, headers={"origin": ONYX_APP}, proxy=proxy)
        self._jwt: str | None = None

    # MARK: Auth

    async def _login(self) -> str:
        rep = await self._privy_http.request(
            "POST", "/api/v1/siwe/init", json={"address": self.address}
        )
        if not rep.ok:
            raise ApiError("Privy nonce failed", rep)
        nonce = rep.json()["nonce"]

        issued_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        message = (
            f"app.onyx.live wants you to sign in with your Ethereum account:\n"
            f"{self.address}\n\n"
            f"By signing, you are proving you own this wallet and logging in. "
            f"This does not initiate a transaction or cost any fees.\n\n"
            f"URI: https://app.onyx.live\n"
            f"Version: 1\n"
            f"Chain ID: 42161\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued_at}\n"
            f"Resources:\n"
            f"- https://privy.io"
        )
        signed = self.account.sign_message(encode_defunct(text=message))
        signature = "0x" + signed.signature.hex()

        rep = await self._privy_http.request(
            "POST",
            "/api/v1/siwe/authenticate",
            json={
                "message": message,
                "signature": signature,
                "chainId": "eip155:42161",
                "walletClientType": "python_bot",
                "connectorType": "injected",
                "mode": "login-or-sign-up",
            },
        )
        if not rep.ok:
            raise ApiError("Privy SIWE auth failed", rep)

        jwt: str = rep.json()["token"]
        self._jwt = jwt
        return jwt

    def _clear_auth(self) -> None:
        self._privy_http.clear_cookies()
        self._arjuna_http.clear_cookies()
        self._jwt = None

    async def _check_auth(self) -> bool:
        if not self._jwt:
            return False
        rep = await self._arjuna_http.request(
            "GET", "/me/user", headers={"Authorization": f"Bearer {self._jwt}"}
        )
        if rep.status_code == 401:
            return False
        if not rep.ok:
            raise ApiError("Auth check failed", rep)
        return True

    @locked
    async def login(self, *, force: bool = False) -> None:
        if force or not await self._check_auth():
            self._clear_auth()
            await self._login()

    async def _authed_get(self, path: str, **kwargs) -> dict:
        if not self._jwt:
            await self.login()
        jwt = self._jwt
        rep = await self._arjuna_http.request(
            "GET", path, headers={"Authorization": f"Bearer {jwt}"}, **kwargs
        )
        if rep.status_code == 401:
            if self._jwt == jwt:
                self._jwt = None
            await self.login()
            jwt = self._jwt
            rep = await self._arjuna_http.request(
                "GET", path, headers={"Authorization": f"Bearer {jwt}"}, **kwargs
            )
        if not rep.ok:
            raise ApiError(f"Arjuna GET {path} failed", rep)
        return rep.json()

    # MARK: Arjuna API

    async def user_info(self) -> OnyxUserInfo:
        data = await self._authed_get("/me/user")
        return OnyxUserInfo.model_validate(data)

    async def points_total(self) -> Decimal:
        data = await self.points_overview()
        return Decimal(str(data.get("totalPoints", 0)))

    async def points_overview(self) -> dict:
        return await self._authed_get("/me/points/overview")

    async def points(self) -> list[OnyxPoint]:
        return OnyxPoint.from_overview(await self.points_overview())

    @ttl_cache(300)
    async def _fetch_order_oids(self) -> frozenset[int]:
        """Fetch oids of filled orders from Arjuna public order-history (last 2000 orders)."""
        try:
            rep = await self._arjuna_http.request(
                "GET", f"/public/users/{self.address}/order-history", params={"limit": 2000}
            )
            if not rep.ok:
                return frozenset()
            data = rep.json()
            return frozenset(int(row["oid"]) for row in data if row.get("status") == "filled")
        except Exception as e:
            logger.warning(f"fetch_order_oids: {e}")
            return frozenset()

    async def fetch_fills(self, since: datetime | None = None) -> list[dict]:
        """Return only Onyx-attributed fills (confirmed via archive or order-history OIDs)."""
        raw_fills, (_, oid_set) = await asyncio.gather(
            self._fetch_fills(since, aggregate=False),
            asyncio.gather(BuilderArchive.sync(), self._fetch_order_oids()),
        )

        cutoff_ms = int((datetime.now(UTC) - timedelta(days=3)).timestamp() * 1000)
        missed = 0
        archive_range = 0
        result = []

        for fill in raw_fills:
            if fill["oid"] in oid_set or BuilderArchive.contains(
                fill["time"] // 1000, fill["coin"], fill["px"], fill["sz"]
            ):
                result.append(fill)
            elif fill["time"] < cutoff_ms:
                archive_range += 1
                missed += 1
                continue

            if fill["time"] < cutoff_ms:
                archive_range += 1

        if missed:
            logger.debug(
                f"fetch_fills: {missed}/{archive_range} fills in archive range "
                "not confirmed as Onyx"
            )

        return result

    # MARK: Profile

    async def profile(self) -> ProfileInfo:
        bal, info, pts, mode = await asyncio.gather(
            self.balance(), self.user_info(), self.points_total(), self.account_mode()
        )
        s = info.accountSummary

        pnl = s.totalPnl

        addr = utils.short_addr(self.address)
        return ProfileInfo(
            addr=addr, balance=bal, volume=s.onyxVolume, pnl=pnl, points=pts, mode=mode
        )
