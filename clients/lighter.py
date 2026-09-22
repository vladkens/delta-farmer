# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Probably works in production
import asyncio
import base64
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Self

from curl_cffi import CurlMime
from curl_cffi.requests import Response
from eth_account.messages import encode_defunct
from pydantic import BaseModel, Field

from lib import utils
from lib.decorators import bind_log_context, locked, retry, ttl_cache
from lib.errors import AppError
from lib.evm import (
    ROBINHOOD,
    EvmNetwork,
    EvmToken,
    execute_contract_with_erc20_allowance,
    get_evm_balances,
    make_call,
    to_token_units,
)
from lib.http import ApiError, AsyncHttp, HttpMethod
from lib.lighter_crypto import ROBINHOOD_SIGNING_CHAIN_ID, AuthToken, LighterSigner, SignedTx
from lib.logger import logger
from lib.models import AccountConfig
from strategy import Order, OrderBook, OrderStatus, Position, ProfileInfo, Side
from strategy.deposit import (
    DepositAsset,
    DepositBalances,
    deposit_amount,
    wait_for_deposit_credit,
)
from strategy.withdrawal import WithdrawalInfo, withdrawal_amount, withdrawal_limit

API_URL = "https://api.rh.lighter.xyz"
APP_URL = "https://robinhoodchain.lighter.xyz"
WS_URL = "wss://api.rh.lighter.xyz/stream"

DEPOSIT_CONTRACT = "0x94bAB9693Ba2f6358507eFfcbd372b0660AFfF9d"
USDG_ASSET_INDEX = 3
DEPOSIT_RECEIPT_TIMEOUT_SEC = 5 * 60
PERPS_ROUTE_TYPE = 0
WITHDRAWAL_CREDIT_TIMEOUT_SEC = 10 * 60
WITHDRAWAL_POLL_DELAY = 10

API_KEY_INDEX = 3
POLL_ATTEMPTS = 30
POLL_DELAY = 2.0

TX_LIFETIME_MS = 599_000
ORDER_LIFETIME_MS = 28 * 24 * 60 * 60 * 1_000
AUTH_LIFETIME_SEC = 7 * 60 * 60
AUTH_REFRESH_SEC = 10 * 60
INTEGRATOR_APPROVAL_LIFETIME_MS = 365 * 24 * 60 * 60 * 1_000
MARKET_SLIPPAGE = Decimal("0.01")
INTEGRATOR_APPROVAL_FIELDS = {
    "account_index": "fee_collector_account_index",
    "max_perps_taker_fee": "max_integrator_perps_taker_fee",
    "max_perps_maker_fee": "max_integrator_perps_maker_fee",
    "max_spot_taker_fee": "max_integrator_spot_taker_fee",
    "max_spot_maker_fee": "max_integrator_spot_maker_fee",
}

# The Lighter x Robinhood campaign started on 2026-09-01; earlier volume does not count.
VOLUME_START_TIMESTAMP = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp())


class LighterApiError(ApiError):
    def __init__(self, rep: Response, data: dict):
        self.code = int(data["code"])
        message = data.get("message", "Lighter API error")
        super().__init__(f"{message}: code {self.code}", rep)

    def is_account_missing(self) -> bool:
        return self.code == 21100

    def is_auth_missing(self) -> bool:
        return self.code in {20013, 21109}


@dataclass(frozen=True)
class DepositNetwork:
    network: EvmNetwork
    token: EvmToken
    contract: str
    asset_index: int
    min_amount: Decimal


class FastWithdrawalInfo(BaseModel):
    to_account_index: int = Field(ge=0)
    withdraw_limit: Decimal = Field(ge=0, allow_inf_nan=False)
    max_withdrawal_amount: Decimal = Field(ge=0, allow_inf_nan=False)


class WithdrawalAccountInfo(BaseModel):
    account_index: int = Field(ge=0)
    collateral: Decimal = Field(ge=0, allow_inf_nan=False)
    available_balance: Decimal = Field(ge=0, allow_inf_nan=False)


class TransferFeeInfo(BaseModel):
    transfer_fee_usdc: int = Field(ge=0)


class FastWithdrawalResult(BaseModel):
    tx_hash: str = Field(pattern=r"^(0x)?[0-9a-fA-F]{80}$")


def _deposit_network(layer1: dict, assets: dict) -> DepositNetwork:
    provider = utils.first(
        [x for x in layer1["l1_providers"] if x["chainId"] == ROBINHOOD.chain_id]
    )
    contract = utils.first(
        [x for x in layer1["contract_addresses"] if x["name"] == "ZkLighterContract"]
    )
    asset = utils.first([x for x in assets["asset_details"] if x["symbol"] == "USDG"])
    token = ROBINHOOD.stables["USDG"]
    if (
        provider is None
        or contract is None
        or contract["address"].lower() != DEPOSIT_CONTRACT.lower()
        or asset is None
        or asset["asset_id"] != USDG_ASSET_INDEX
        or asset["l1_address"].lower() != token.address.lower()
        or asset["l1_decimals"] != token.decimals
    ):
        raise ApiError("Unexpected Robinhood Lighter deposit metadata")

    try:
        min_amount = Decimal(str(asset["min_transfer_amount"]))
    except (InvalidOperation, KeyError):
        raise ApiError("Invalid minimum USDG deposit") from None
    if not min_amount.is_finite() or min_amount <= 0:
        raise ApiError("Invalid minimum USDG deposit")

    return DepositNetwork(
        network=ROBINHOOD,
        token=token,
        contract=DEPOSIT_CONTRACT,
        asset_index=USDG_ASSET_INDEX,
        min_amount=min_amount,
    )


def to_domain_status(status: str) -> OrderStatus:
    if status == "filled":
        return OrderStatus.FILLED
    if status.startswith("canceled"):
        return OrderStatus.CANCELED
    return OrderStatus.OPEN


def _market_order_done(order: Order) -> bool:
    if order.status == OrderStatus.OPEN:
        return False
    if order.status == OrderStatus.CANCELED:
        raise ApiError(
            f"Lighter market order {order.id} was canceled: filled {order.filled:g}/{order.size:g}"
        )
    if order.filled < order.size:
        raise ApiError(
            f"Lighter market order {order.id} was not fully filled: {order.filled:g}/{order.size:g}"
        )

    return True


def _has_integrator_approval(account: dict, config: dict) -> bool:
    expected = {key: config[value] for key, value in INTEGRATOR_APPROVAL_FIELDS.items()}
    now = int(time.time() * 1_000)
    for approval in account.get("approved_integrators", []):
        expires_at = int(approval.get("approval_expiry", 0))
        values = utils.pick(approval, *expected)
        if expires_at > now and values == expected:
            return True

    return False


@bind_log_context
class LighterClient:
    exchange = "lighter"

    @classmethod
    def from_config(cls, cfg: AccountConfig) -> Self:
        return cls(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.name = name
        self.proxy = proxy
        self.account = utils.parse_eth_key(privkey, name)
        self.address = self.account.address
        self.signer = LighterSigner(privkey, ROBINHOOD_SIGNING_CHAIN_ID)
        self._account_index: int | None = None
        self._auth_token: AuthToken | None = None
        self._last_nonce = 0
        self.http = AsyncHttp(
            baseurl=API_URL,
            headers={"Origin": APP_URL, "Referer": f"{APP_URL}/"},
            proxy=proxy,
        )

    async def close(self) -> None:
        await self.http.close()

    async def _call(self, method: HttpMethod, path: str, **kwargs) -> dict:
        rep = await self.http.request(method, path, **kwargs)
        try:
            res = rep.json()
        except ValueError:
            raise ApiError(f"Lighter API {method} {path} error", rep)

        if "code" in res and res["code"] != 200:
            raise LighterApiError(rep, res)
        if not rep.ok:
            raise ApiError(f"Lighter API {method} {path} error", rep)

        return res

    async def account_info(self) -> dict:
        pld = {"by": "l1_address", "value": self.address}
        res = await self._call("GET", "/api/v1/account", params=pld)
        accounts = res["accounts"]
        if not accounts:
            raise ApiError(f"Lighter account not found for {self.address}")

        account = utils.first([x for x in accounts if x.get("account_type") == 0])
        account = account or accounts[0]

        self._account_index = account["account_index"]
        return account

    @ttl_cache(3600)
    async def _system_config(self) -> dict:
        return await self._call("GET", "/api/v1/systemConfig")

    @retry(max_attempts=5, delay=1.0)
    async def _get_volume(self, account_index: int) -> Decimal:
        auth = self._get_auth_headers(account_index)["Authorization"]
        channel = f"account_all_trades/{account_index}"
        async with self.http.session.ws_connect(WS_URL) as ws:
            await ws.send_json({"type": "subscribe", "channel": channel, "auth": auth})
            while True:
                data = await ws.recv_json(timeout=30)
                if data.get("type") == "subscribed/account_all_trades":
                    return Decimal(str(data["total_volume"]))

    async def _get_pnl(self, account_index: int) -> Decimal:
        hdr = self._get_auth_headers(account_index)
        pld = {
            "by": "index",
            "value": account_index,
            "resolution": "1d",
            "start_timestamp": VOLUME_START_TIMESTAMP,
            "end_timestamp": int(time.time()),
            "count_back": 0,
            "ignore_transfers": "false",
        }
        res = await self._call("GET", "/api/v1/pnl", params=pld, headers=hdr)
        history: list[dict] = res["pnl"]
        if not history:
            # TODO: Replace this once current PnL is available without chart fallbacks.
            # Robinhood Lighter does not publish the daily rollup on an account's first day.
            pld["resolution"] = "1h"
            pld["start_timestamp"] = max(
                VOLUME_START_TIMESTAMP, pld["end_timestamp"] - 7 * 24 * 60 * 60
            )
            res = await self._call("GET", "/api/v1/pnl", params=pld, headers=hdr)
            history = res["pnl"]

        if not history:
            return Decimal(0)

        first = Decimal(str(history[0]["trade_pnl"]))
        last = Decimal(str(history[-1]["trade_pnl"]))
        return last - first

    async def _get_used_referral_code(self, account_index: int) -> str | None:
        hdr = self._get_auth_headers(account_index)
        pld = {"l1_address": self.address, "is_eligible": "false"}
        res = await self._call("GET", "/api/v1/referral/userReferrals", params=pld, headers=hdr)
        return res.get("used_code") or None

    async def _get_points(self, account_index: int) -> Decimal:
        try:
            auth = self._get_auth_headers(account_index)["Authorization"]
            res = await self._call(
                "GET",
                "/api/v1/leaderboard",
                params={"type": "all", "l1_address": self.address, "auth": auth},
                headers={"PreferAuthServer": "true"},
            )
            for entry in res["entries"]:
                if entry["l1_address"].lower() == self.address.lower():
                    return Decimal(str(entry["points"]))
        except Exception as error:
            logger.warning(f"Leaderboard points unavailable: {error}")
            return Decimal(0)

        logger.warning("No leaderboard points for account")
        return Decimal(0)

    async def profile(self) -> ProfileInfo:
        info = await self.account_info()
        account_index = info["account_index"]
        volume = await self._get_volume(account_index)
        pnl = await self._get_pnl(account_index)
        ref_code = await self._get_used_referral_code(account_index)
        points = await self._get_points(account_index)

        return ProfileInfo(
            addr=utils.short_addr(self.address),
            balance=Decimal(info["collateral"]),
            volume=volume,
            pnl=pnl,
            points=points,
            ref_code=ref_code,
        )

    async def use_referral_code(self, referral_code: str) -> None:
        # The web client calls this a signature, but it is a Base64 protocol marker.
        signature = f"{self.address}{referral_code}wP81zDNpES"
        signature = base64.b64encode(signature.encode()).decode()
        hdr = {"PreferAuthServer": "true"}
        pld = {
            "l1_address": self.address,
            "referral_code": referral_code,
            "discord": "",
            "telegram": "",
            "x": "",
            "signature": signature,
            "source": "none",
        }
        await self._call("POST", "/api/v1/referral/use", data=pld, headers=hdr)

    async def _get_account_index(self) -> int:
        if self._account_index is None:
            await self.account_info()
        assert self._account_index is not None
        return self._account_index

    async def _get_api_key(
        self, account_index: int, *, headers: dict[str, str] | None = None
    ) -> bytes | None:
        pld = {"account_index": account_index, "api_key_index": API_KEY_INDEX}
        try:
            res = await self._call("GET", "/api/v1/apikeys", params=pld, headers=headers)
        except LighterApiError as error:
            if error.is_auth_missing():
                return None
            raise

        item = utils.first([x for x in res["api_keys"] if x["api_key_index"] == API_KEY_INDEX])
        if item is None:
            return None

        return bytes.fromhex(item["public_key"].removeprefix("0x"))

    async def _get_next_nonce(self, account_index: int) -> int:
        pld = {"account_index": account_index, "api_key_index": API_KEY_INDEX}
        data = await self._call("GET", "/api/v1/nextNonce", params=pld)
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

    async def _send_integrator_approval(self, account_index: int, config: dict) -> None:
        nonce = await self._get_next_nonce(account_index)
        now = int(time.time() * 1_000)
        approval_expiry = now + INTEGRATOR_APPROVAL_LIFETIME_MS
        values = {
            "integrator_account_index": int(config["fee_collector_account_index"]),
            "max_perps_taker_fee": int(config["max_integrator_perps_taker_fee"]),
            "max_perps_maker_fee": int(config["max_integrator_perps_maker_fee"]),
            "max_spot_taker_fee": int(config["max_integrator_spot_taker_fee"]),
            "max_spot_maker_fee": int(config["max_integrator_spot_maker_fee"]),
        }
        message = self.signer.approve_integrator_message(
            account_index,
            API_KEY_INDEX,
            nonce,
            approval_expiry=approval_expiry,
            **values,
        )
        signature = self.account.sign_message(encode_defunct(text=message)).signature.hex()
        tx = self.signer.sign_approve_integrator(
            account_index,
            API_KEY_INDEX,
            approval_expiry=approval_expiry,
            nonce=nonce,
            expired_at=now + TX_LIFETIME_MS,
            l1_signature=signature,
            **values,
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

    async def _ensure_integrator_approval(self, account: dict) -> str:
        config = await self._system_config()
        if int(config["fee_collector_account_index"]) == 0:
            return "integrator disabled"
        if _has_integrator_approval(account, config):
            return "integrator already approved"

        account_index = account["account_index"]
        await self._send_integrator_approval(account_index, config)
        for attempt in range(POLL_ATTEMPTS):
            if _has_integrator_approval(await self.account_info(), config):
                return "integrator approved"
            if attempt < POLL_ATTEMPTS - 1:
                await asyncio.sleep(POLL_DELAY)

        raise ApiError("Lighter integrator approval timed out")

    def _get_auth_headers(self, account_index: int, *, force: bool = False) -> dict[str, str]:
        now = int(time.time())
        if force or self._auth_token is None or self._auth_token.deadline - now < AUTH_REFRESH_SEC:
            self._auth_token = self.signer.create_auth_token(
                account_index, API_KEY_INDEX, now + AUTH_LIFETIME_SEC
            )

        return {"PreferAuthServer": "true", "Authorization": self._auth_token.token}

    @locked
    async def login(self, *, force: bool = False) -> str:
        try:
            account = await self.account_info()
        except LighterApiError as error:
            if error.is_account_missing():
                raise AppError("Account is not registered; deposit first") from None
            raise

        account_index = account["account_index"]
        public_key = await self._get_api_key(account_index)
        if force or public_key != self.signer.public_key:
            await self._send_change_pub_key(account_index)
            await self._wait_for_api_key(account_index)
            status = "key installed" if public_key is None else "key replaced"
        else:
            status = "key already active"

        hdr = self._get_auth_headers(account_index, force=force)
        if await self._get_api_key(account_index, headers=hdr) != self.signer.public_key:
            raise ApiError("Lighter authentication returned a different API key")

        integrator = await self._ensure_integrator_approval(account)
        return f"slot {API_KEY_INDEX}, {status}, {integrator}"

    async def auth_ready(self) -> bool:
        try:
            account = await self.account_info()
        except LighterApiError as error:
            if error.is_account_missing():
                return False
            raise

        account_index = account["account_index"]
        # Another client may replace this slot; our auth token only works with our public key.
        if await self._get_api_key(account_index) != self.signer.public_key:
            return False

        hdr = self._get_auth_headers(account_index)
        return await self._get_api_key(account_index, headers=hdr) == self.signer.public_key

    async def registered(self) -> bool:
        try:
            await self.account_info()
            return True
        except LighterApiError as error:
            if error.is_account_missing():
                return False
            raise

    # MARK: Markets

    @ttl_cache(3600)
    async def _markets(self) -> list[dict]:
        data = await self._call("GET", "/api/v1/orderBookDetails")
        return data["order_book_details"]

    async def _market(self, symbol: str) -> dict:
        market = utils.first([x for x in await self._markets() if x["symbol"] == symbol])
        if market is None:
            raise ApiError(f"Unknown Lighter symbol: {symbol}")
        return market

    async def get_symbols(self) -> list[str]:
        markets = [x for x in await self._markets() if x["status"] == "active"]
        markets.sort(key=lambda item: Decimal(str(item["daily_quote_token_volume"])), reverse=True)
        return [item["symbol"] for item in markets]

    async def is_symbol_tradeable(
        self, symbol: str, at: datetime, reduce_only: bool = False
    ) -> bool:
        market = await self._market(symbol)
        status = market["status"]
        if reduce_only:
            return status in ("active", "reduce_only")
        config = market["market_config"]
        return status == "active" and not config["force_reduce_only"]

    @ttl_cache(5)
    async def get_order_book(self, symbol: str) -> OrderBook:
        market = await self._market(symbol)
        pld = {"market_id": market["market_id"], "limit": 250}
        res = await self._call("GET", "/api/v1/orderBookOrders", params=pld)
        multiplier = Decimal(market.get("multiplier") or 1).normalize()

        def level(item: dict) -> tuple[Decimal, Decimal]:
            price = Decimal(item["price"]) / multiplier
            size = Decimal(item["remaining_base_amount"]) * multiplier
            return price, size

        return OrderBook.build(
            bids=[level(item) for item in res["bids"][:5]],
            asks=[level(item) for item in res["asks"][:5]],
        )

    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        book = await self.get_order_book(symbol)
        if not book.bids or not book.asks:
            raise ApiError(f"Lighter order book is empty for {symbol}")

        return book.bids[0].price, book.asks[0].price

    async def get_price(self, symbol: str) -> Decimal:
        bid, ask = await self.get_bbo(symbol)
        return (bid + ask) / 2

    async def get_lot_size(self, symbol: str) -> Decimal:
        market = await self._market(symbol)
        multiplier = Decimal(market.get("multiplier") or 1).normalize()
        size_step = Decimal(10) ** -market["size_decimals"]
        return size_step * multiplier

    async def get_tick_size(self, symbol: str) -> Decimal:
        market = await self._market(symbol)
        multiplier = Decimal(market.get("multiplier") or 1).normalize()
        price_step = Decimal(10) ** -market["price_decimals"]
        return price_step / multiplier

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        market = await self._market(symbol)
        min_quote = Decimal(str(market["min_quote_amount"]))
        min_base = Decimal(str(market["min_base_amount"]))
        return max(min_quote, min_base * await self.get_price(symbol))

    # MARK: Account

    async def balance(self) -> Decimal:
        return Decimal((await self.account_info())["collateral"])

    async def deposit_balance(self) -> Decimal:
        return await self.balance() if await self.registered() else Decimal(0)

    @ttl_cache(3600)
    async def deposit_network(self) -> DepositNetwork:
        layer1 = await self._call("GET", "/api/v1/layer1BasicInfo")
        assets = await self._call("GET", "/api/v1/assetDetails")
        return _deposit_network(layer1, assets)

    async def deposit_asset(self) -> DepositAsset:
        info = await self.deposit_network()
        return DepositAsset(self.exchange.title(), info.network, info.token, info.min_amount)

    async def deposit_balances(self) -> DepositBalances:
        info = await self.deposit_network()
        balance, wallet_balances = await asyncio.gather(
            self.deposit_balance(),
            get_evm_balances(
                info.network,
                self.address,
                (info.token, info.network.native_token),
                self.proxy,
            ),
        )
        return DepositBalances(
            balance,
            wallet_balances[info.token],
            wallet_balances[info.network.native_token],
        )

    async def deposit(self, amount: Decimal) -> str:
        info = await self.deposit_network()
        amount = deposit_amount(amount, info.token.decimals, info.min_amount)
        logger.info(f"Deposit {amount:,.2f} {info.token.symbol}")
        amount_units = to_token_units(amount, info.token)
        balance = await self.deposit_balance()
        call = make_call(
            info.contract,
            "deposit",
            ["address", "uint16", "uint8", "uint256"],
            [self.address, info.asset_index, PERPS_ROUTE_TYPE, amount_units],
        )
        tx_hash, fee = await execute_contract_with_erc20_allowance(
            info.network,
            self.account,
            info.token,
            info.contract,
            amount_units,
            call,
            self.proxy,
            DEPOSIT_RECEIPT_TIMEOUT_SEC,
        )
        logger.info(f"Deposit tx: {tx_hash}; fee: {fee:,.8f} ETH")
        logger.info("Deposit tx confirmed; waiting for credit")
        await wait_for_deposit_credit(self, balance + amount, tx_hash)
        logger.success(f"Deposit credited: {info.network.tx_url(tx_hash)}")

        return tx_hash

    async def _fast_withdrawal_info(self, account_index: int) -> FastWithdrawalInfo:
        hdr = self._get_auth_headers(account_index)
        res = await self._call(
            "GET",
            "/api/v1/fastwithdraw/info",
            params={"account_index": account_index},
            headers=hdr,
        )
        return FastWithdrawalInfo.model_validate(res)

    async def _transfer_fee(self, account_index: int, to_account_index: int) -> int:
        hdr = self._get_auth_headers(account_index)
        pld = {"account_index": account_index, "to_account_index": to_account_index}
        res = await self._call("GET", "/api/v1/transferFeeInfo", params=pld, headers=hdr)
        return TransferFeeInfo.model_validate(res).transfer_fee_usdc

    async def _withdrawal_blocker(self) -> str | None:
        orders, positions = await asyncio.gather(self.orders(), self.positions())
        if orders and positions:
            return "open positions and orders"
        if positions:
            return "open positions"
        if orders:
            return "open orders"
        return None

    async def withdrawal_info(self) -> WithdrawalInfo:
        account = WithdrawalAccountInfo.model_validate(await self.account_info())
        account_index = account.account_index
        fast, blocker = await asyncio.gather(
            self._fast_withdrawal_info(account_index),
            self._withdrawal_blocker(),
        )
        fee_units = await self._transfer_fee(account_index, fast.to_account_index)
        asset = await self.deposit_network()
        fee = Decimal(fee_units).scaleb(-asset.token.decimals)
        available = min(
            account.available_balance,
            fast.withdraw_limit,
            fast.max_withdrawal_amount,
        )
        return WithdrawalInfo(
            self.exchange.title(),
            asset.network,
            asset.token,
            account.collateral,
            available,
            unavailable=blocker,
            fee=fee,
        )

    async def _wallet_balance(self, asset: DepositNetwork) -> Decimal:
        balances = await get_evm_balances(
            asset.network,
            self.address,
            (asset.token,),
            self.proxy,
        )
        return balances[asset.token]

    async def _submit_fast_withdrawal(
        self,
        account_index: int,
        tx: SignedTx,
    ) -> str:
        hdr = self._get_auth_headers(account_index)
        multipart = CurlMime.from_list(
            [
                {
                    "name": "tx_info",
                    "data": json.dumps(tx.info, separators=(",", ":")).encode(),
                },
                {"name": "to_address", "data": self.address.encode()},
            ]
        )
        try:
            res = await self._call(
                "POST",
                "/api/v1/fastwithdraw",
                multipart=multipart,
                headers=hdr,
            )
        finally:
            multipart.close()

        result = FastWithdrawalResult.model_validate(res)
        tx_hash = result.tx_hash.removeprefix("0x").lower()
        if tx_hash != tx.tx_hash:
            raise ApiError(f"Lighter returned a different withdrawal hash: {tx_hash}")

        return tx_hash

    async def withdraw(self, amount: Decimal) -> str:
        asset = await self.deposit_network()
        amount = withdrawal_amount(amount, asset.token.decimals)
        logger.info(f"Withdraw {amount:,.2f} {asset.token.symbol}")

        details = await self.withdrawal_info()
        if details.unavailable:
            raise ApiError(f"Withdrawal blocked: {details.unavailable}")

        account_index = await self._get_account_index()
        fast = await self._fast_withdrawal_info(account_index)
        fee_units = await self._transfer_fee(account_index, fast.to_account_index)
        fee = Decimal(fee_units).scaleb(-asset.token.decimals)
        available = min(
            details.available,
            fast.withdraw_limit,
            fast.max_withdrawal_amount,
        )
        safe_amount = withdrawal_limit(details.balance, available, fee)
        if amount > safe_amount:
            raise ApiError(
                f"Withdrawal exceeds safe {asset.token.symbol} amount: {safe_amount} < {amount}"
            )

        wallet_balance = await self._wallet_balance(asset)
        amount_units = to_token_units(amount, asset.token)
        nonce = self._next_nonce()
        memo = bytes.fromhex(self.address[2:]) + bytes(12)
        message = self.signer.transfer_message(
            account_index,
            API_KEY_INDEX,
            fast.to_account_index,
            asset.asset_index,
            PERPS_ROUTE_TYPE,
            PERPS_ROUTE_TYPE,
            amount_units,
            fee_units,
            memo,
            nonce,
        )
        signature = self.account.sign_message(encode_defunct(text=message)).signature.hex()
        tx = self.signer.sign_transfer(
            account_index,
            API_KEY_INDEX,
            fast.to_account_index,
            asset.asset_index,
            PERPS_ROUTE_TYPE,
            PERPS_ROUTE_TYPE,
            amount_units,
            fee_units,
            memo,
            nonce,
            nonce + TX_LIFETIME_MS,
            signature,
        )
        tx_hash = await self._submit_fast_withdrawal(account_index, tx)
        logger.info(f"Withdrawal submitted: {tx_hash}; waiting for wallet credit")

        expected_balance = wallet_balance + amount
        deadline = time.monotonic() + WITHDRAWAL_CREDIT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            wallet_balance = await self._wallet_balance(asset)
            if wallet_balance >= expected_balance:
                logger.success(
                    f"Withdrawal credited: {asset.token.symbol} wallet balance "
                    f"{wallet_balance:,.2f}"
                )
                return tx_hash

            await asyncio.sleep(WITHDRAWAL_POLL_DELAY)

        raise ApiError(f"Lighter withdrawal credit timed out: {tx_hash}")

    async def get_min_deposit_usd(self) -> Decimal:
        return (await self.deposit_network()).min_amount

    async def positions(self) -> list[Position]:
        info = await self.account_info()
        markets = {x["market_id"]: x for x in await self._markets()}
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
        market = utils.first(
            [
                market
                for market in await self._markets()
                if market["market_id"] == item["market_index"]
            ]
        )
        if market is None:
            raise ApiError(f"Unknown Lighter market: {item['market_index']}")

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

    async def orders(self) -> list[Order]:
        account_index = await self._get_account_index()
        hdr = self._get_auth_headers(account_index)
        pld = {"account_index": account_index, "market_type": "all"}
        res = await self._call("GET", "/api/v1/accountActiveOrders", params=pld, headers=hdr)
        return list(await asyncio.gather(*(self._load_order(item) for item in res["orders"])))

    async def get_order(self, order_id: str) -> Order | None:
        account_index = await self._get_account_index()
        hdr = self._get_auth_headers(account_index)
        pld = {"account_index": account_index, "client_order_indexes": order_id}
        res = await self._call("GET", "/api/v1/accountOrders", params=pld, headers=hdr)
        return await self._load_order(res["orders"][0]) if res["orders"] else None

    async def _wait_for_order(self, order_id: str, *, market: bool) -> Order:
        last_order = None
        for attempt in range(POLL_ATTEMPTS):
            order = await self.get_order(order_id)
            if order is not None:
                last_order = order
                if not market or _market_order_done(order):
                    return order

            if attempt < POLL_ATTEMPTS - 1:
                await asyncio.sleep(POLL_DELAY)

        if last_order is None:
            raise ApiError(f"Lighter order {order_id} not found")

        raise ApiError(
            f"Lighter market order {order_id} did not finish: "
            f"filled {last_order.filled:g}/{last_order.size:g}"
        )

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
        return await self._wait_for_order(str(nonce), market=order_type == 1)

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
        hdr = self._get_auth_headers(account_index)
        pld = {"account_index": account_index, "market_type": "all"}
        res = await self._call("GET", "/api/v1/accountActiveOrders", params=pld, headers=hdr)
        count = len(res["orders"])
        if count == 0:
            return 0

        nonce = self._next_nonce()
        tx = self.signer.sign_cancel_all_orders(
            account_index, API_KEY_INDEX, nonce, nonce + TX_LIFETIME_MS
        )
        await self._send_tx(tx)

        for attempt in range(POLL_ATTEMPTS):
            res = await self._call("GET", "/api/v1/accountActiveOrders", params=pld, headers=hdr)
            if not res["orders"]:
                return count
            if attempt < POLL_ATTEMPTS - 1:
                await asyncio.sleep(POLL_DELAY)
        raise ApiError("Lighter cancel all orders timed out")

    # MARK: Leverage

    async def get_leverage(self, symbol: str) -> int | None:
        info = await self.account_info()
        position = utils.first([x for x in info["positions"] if x["symbol"] == symbol])
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
