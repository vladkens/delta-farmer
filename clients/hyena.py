# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from eth_account.messages import encode_defunct
from pydantic import BaseModel

from lib import utils
from lib.decorators import bind_log_context
from lib.http import ApiError, AsyncHttp
from strategy import Position, ProfileInfo, TradingClient

from .hyperliquid import HyperLiquidClient

HYENA_API = "https://app.hyena.trade"
_POINTS_GENESIS = datetime(2025, 12, 4, tzinfo=UTC)


# MARK: Models


class HyenaRewardBalance(BaseModel):
    enaxPoints: Decimal
    sats: Decimal
    gold: Decimal


class HyenaRank(BaseModel):
    tier: str
    percentile: float


class HyenaHistoryItem(BaseModel):
    id: str  # "reward-week-6" etc.
    enaxPoints: Decimal

    @property
    def start_window(self) -> datetime:
        n = int(self.id.removeprefix("reward-week-"))
        return _POINTS_GENESIS + timedelta(weeks=n - 1)


class HyenaFill(BaseModel):
    coin: str
    px: Decimal
    sz: Decimal
    closedPnl: Decimal = Decimal(0)


class HyenaRewards(BaseModel):
    balance: HyenaRewardBalance
    rank: HyenaRank
    availableToClaim: Decimal
    history: list[HyenaHistoryItem] = []


class HyenaRewardTotal(BaseModel):
    totalClaimed: Decimal
    todayAmount: Decimal


class HyenaSystemRewardSummary(BaseModel):
    totalReports: int
    claimableReports: int
    claimableAmount: Decimal
    totalProcessed: Decimal
    processingCount: int
    failedCount: int


class HyenaSystemRewardReport(BaseModel):
    id: str
    amount: Decimal
    tokenSymbol: str
    status: str
    startDate: datetime
    endDate: datetime


class HyenaSystemRewards(BaseModel):
    summary: HyenaSystemRewardSummary
    reports: list[HyenaSystemRewardReport]
    claimableReports: list[HyenaSystemRewardReport]


class HyenaSystemRewardClaim(BaseModel):
    reportId: str
    status: str
    amount: Decimal
    tokenSymbol: str


# MARK: Client


@bind_log_context
class HyenaClient(HyperLiquidClient):
    exchange = "hyena"
    dex_prefix = "hyna"

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return HyenaClient

    @classmethod
    def to_week_label(cls, dt: datetime) -> str:
        return utils.to_period_week(dt, genesis=_POINTS_GENESIS)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        super().__init__(name, privkey, proxy)
        self._app_http = AsyncHttp(
            baseurl=HYENA_API,
            headers={"Origin": HYENA_API, "Referer": f"{HYENA_API}/"},
            proxy=proxy,
        )
        self._jwt: str | None = None
        self._login_lock = asyncio.Lock()

    # MARK: Auth

    async def _login(self) -> str:  # serialized via _login_lock
        rep = await self._app_http.request(
            "GET", "/api/auth/nonce", params={"address": self.address}
        )
        if not rep.ok:
            raise ApiError("Nonce failed", rep)
        nonce = rep.json()["nonce"]

        issued_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        message = (
            f"app.hyena.trade wants you to sign in with your Ethereum account:\n"
            f"{self.address}\n"
            f"\n"
            f"Sign in with Ethereum to the app.\n"
            f"\n"
            f"URI: https://app.hyena.trade\n"
            f"Version: 1\n"
            f"Chain ID: 42161\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued_at}"
        )
        signed = self.account.sign_message(encode_defunct(text=message))
        signature = "0x" + signed.signature.hex()

        rep = await self._app_http.request(
            "POST", "/api/auth", json={"message": message, "signature": signature}
        )
        if not rep.ok:
            raise ApiError("SIWE auth failed", rep)

        jwt: str = rep.json()["token"]
        self._jwt = jwt
        return jwt

    async def _authed_get(self, path: str, **kwargs) -> dict:
        if not self._jwt:
            async with self._login_lock:
                if not self._jwt:  # re-check after acquiring lock
                    await self._login()
        jwt = self._jwt
        rep = await self._app_http.request(
            "GET", path, headers={"Authorization": f"Bearer {jwt}"}, **kwargs
        )
        if rep.status_code == 401:
            self._jwt = None
            async with self._login_lock:
                if not self._jwt:
                    await self._login()
            jwt = self._jwt
            rep = await self._app_http.request(
                "GET", path, headers={"Authorization": f"Bearer {jwt}"}, **kwargs
            )
        if not rep.ok:
            raise ApiError(f"Hyena GET {path} failed", rep)
        return rep.json()

    async def _authed_post(self, path: str, **kwargs) -> dict:
        if not self._jwt:
            async with self._login_lock:
                if not self._jwt:  # re-check after acquiring lock
                    await self._login()
        jwt = self._jwt
        rep = await self._app_http.request(
            "POST", path, headers={"Authorization": f"Bearer {jwt}"}, **kwargs
        )
        if rep.status_code == 401:
            self._jwt = None
            async with self._login_lock:
                if not self._jwt:
                    await self._login()
            jwt = self._jwt
            rep = await self._app_http.request(
                "POST", path, headers={"Authorization": f"Bearer {jwt}"}, **kwargs
            )
        if not rep.ok:
            raise ApiError(f"Hyena POST {path} failed", rep)
        return rep.json()

    def _filter_positions(self, positions: list[Position]) -> list[Position]:
        return [p for p in positions if p.symbol.startswith("hyna:")]

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        return True

    async def _place_order(self, symbol: str, *args, **kwargs):
        if not symbol.startswith("hyna:"):
            raise ValueError(f"HyenaClient only trades hyna: symbols, got {symbol!r}")
        return await super()._place_order(symbol, *args, **kwargs)

    # MARK: Account

    async def balance(self) -> Decimal:
        spot, perp = await asyncio.gather(
            self._info(type="spotClearinghouseState", user=self.address),
            self._info(type="clearinghouseState", user=self.address, dex=self.dex_prefix),
        )
        usde = next(
            (Decimal(str(b["total"])) for b in spot["balances"] if b["coin"] == "USDE"), Decimal(0)
        )
        dex_equity = Decimal(str(perp["marginSummary"]["accountValue"]))
        return usde + dex_equity

    # MARK: Points API

    async def rewards(self) -> HyenaRewards:
        data = await self._authed_get(
            f"/api/hyena/rewards/{self.address}", params={"page": 1, "limit": 50}
        )
        return HyenaRewards.model_validate(data["data"])

    async def reward_total(self) -> HyenaRewardTotal:
        data = await self._authed_get("/api/hyena/payouts/total")
        return HyenaRewardTotal.model_validate(data)

    async def system_rewards(self) -> HyenaSystemRewards:
        data = await self._authed_get(
            "/api/hyena/system-payouts/claim", params={"payoutType": "HYENA_WEEKLY_BOOSTED_YIELD"}
        )
        return HyenaSystemRewards.model_validate(data["data"])

    async def claim_system_reward(self, report_id: str) -> HyenaSystemRewardClaim:
        data = await self._authed_post(
            "/api/hyena/system-payouts/claim", json={"systemPayoutReportId": report_id}
        )
        return HyenaSystemRewardClaim.model_validate(data["data"])

    async def fills(self) -> list[HyenaFill]:
        data = await self._info(type="userFills", user=self.address, aggregateByTime=True)
        return [HyenaFill.model_validate(x) for x in data]

    # MARK: Profile

    async def profile(self) -> ProfileInfo:
        bal, rewards, fills, mode = await asyncio.gather(
            self.balance(),
            self.rewards(),
            self.fetch_fills(),
            self.account_mode(),
        )
        # HyperLiquid /info portfolio is overall HIP-3 PnL, not Hyena-only.
        # Restrict to Hyena assets so `info` matches Hyena `stats` lifetime volume/burn reporting.
        hyena_fills = [f for f in fills if str(f.get("coin", "")).startswith("hyna:")]
        volume = sum(
            (Decimal(str(f["px"])) * Decimal(str(f["sz"])) for f in hyena_fills), Decimal(0)
        )
        pnl = sum((Decimal(str(f.get("closedPnl", 0))) for f in hyena_fills), Decimal(0))

        addr = utils.short_addr(self.address)
        epts = rewards.balance.enaxPoints
        return ProfileInfo(addr=addr, balance=bal, volume=volume, pnl=pnl, points=epts, mode=mode)
