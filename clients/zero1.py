# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
import base64
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Self

import base58
from cryptography.hazmat.primitives.asymmetric.ec import (
    ECDSA,
    SECP256K1,
    SECP256R1,
    derive_private_key,
    generate_private_key,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from pydantic import BaseModel

from lib import utils
from lib.decorators import bind_log_context, ttl_cache
from lib.http import ApiError, AsyncHttp, HttpMethod, NotFoundError
from lib.logger import logger
from lib.models import AccountConfig
from lib.unwaf_vc import ensure_unwaf
from strategy import Order, OrderBook, OrderStatus, Position, ProfileInfo, Side, TradingClient

# Nord (01.xyz) protobuf schema — action types, field numbers, error codes, FillMode enum:
# https://zo-devnet.n1.xyz/schema.proto
# All error codes referenced in this file (e.g. 133, 140) are defined in the Error enum there.
# Other docs: https://docs.01.xyz/reference/rest-api

NORD_API = "https://zo-mainnet.n1.xyz"
TURNKEY_API = "https://api.turnkey.com"
TURNKEY_ORG_ID = "497f60f3-57cd-4aec-af39-7415c2fafaab"
ZERO1_JANUS_LOGIN_PATH = "/api/janus/v1/auth/wallet/login"
ZERO1_JANUS_CHALLENGE_PATH = "/api/janus/v1/auth/wallet/challenge"

ZERO1_APP = "https://01.xyz"
N1_SESSION_STAMP_PREFIX = "N1-SESSION-STAMP-V1"
N1_AUTH_IDENTIFY_PATH = "/v1/auth/identify"
N1_AUTH_IDENTIFY_BODY = "{}"
_POINTS_GENESIS = datetime(2026, 2, 3, tzinfo=UTC)  # week 1 start (Tuesday)

# last know values. can be changed on next deployment, but code have auto-discovery fallback
AUTH_ACT = "404455b12249fd9ec1aea6c44bf40eb0338e7cd9a2"
NEXT_DPL = "dpl_BnNc2gSJ2i8QQPVgf45a7rCAsAmA"

_POINTS_META_LOCK = asyncio.Lock()
_POINTS_META_CACHE = ".cache/zero1_points_meta.json"


async def _get_points_meta(
    http: AsyncHttp, fresh: bool = False, stale_act: str | None = None
) -> tuple[str, str]:
    """Returns (action_hash, deployment_id). Pure — no side effects."""
    if not fresh:
        d = utils.json_load(_POINTS_META_CACHE)
        if d:
            return d["act"], d["dpl"]
        return AUTH_ACT, NEXT_DPL

    async with _POINTS_META_LOCK:
        # double-check: another client may have already refreshed while we waited on the lock
        d = utils.json_load(_POINTS_META_CACHE)
        if d and d.get("act") != stale_act:
            return d["act"], d["dpl"]

        # discover: RSC fetch → chunk scan → find createToken action hash
        rsc_hdr = {
            "RSC": "1",
            "Next-Router-State-Tree": "%5B%22%22%2C%7B%7D%2Cnull%2Cnull%2Ctrue%5D",
        }
        rsc = await http.request("GET", f"{ZERO1_APP}/points", headers=rsc_hdr)
        html = await http.request("GET", f"{ZERO1_APP}/points")
        dpl_re = r"dpl=(dpl_[A-Za-z0-9]+)"
        dpl_mm = re.search(dpl_re, rsc.text) or re.search(dpl_re, html.text)
        dpl = dpl_mm.group(1) if dpl_mm else NEXT_DPL
        combined = rsc.text + html.text
        chunks = list(dict.fromkeys(re.findall(r'"(/_next/static/chunks/[^"?]+\.js)', combined)))
        for c in chunks:
            r = await http.request("GET", f"{ZERO1_APP}{c}?dpl={dpl}")
            m = re.search(
                r'createServerReference\)\("([0-9a-f]{40,42})"[^"]*"createToken"\)', r.text
            )
            if m:
                act = m.group(1)
                utils.json_dump(_POINTS_META_CACHE, {"act": act, "dpl": dpl})
                return act, dpl

        logger.warning("Points meta discovery failed, using fallback")
        return AUTH_ACT, NEXT_DPL


# MARK: Protobuf codec (minimal, hand-rolled for Nord actions)


def _vi(n: int) -> bytes:
    """Encode non-negative integer as protobuf varint."""
    if n == 0:
        return b"\x00"
    out = []
    while n:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
    return bytes(out)


def _fv(field: int, val: int) -> bytes:
    return _vi((field << 3) | 0) + _vi(val)


def _fb(field: int, val: bytes) -> bytes:
    return _vi((field << 3) | 2) + _vi(len(val)) + val


def _read_vi(data: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return result, pos


def _parse_fields(data: bytes) -> dict[int, list]:
    """Parse protobuf message into {field_num: [values]}."""
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        tag, pos = _read_vi(data, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            val, pos = _read_vi(data, pos)
        elif wire == 2:
            length, pos = _read_vi(data, pos)
            val = data[pos : pos + length]
            pos += length
        else:
            break  # unsupported wire type, stop
        fields.setdefault(field, []).append(val)
    return fields


def _read_receipt(data: bytes) -> dict:
    """Decode Receipt from Nord /action response."""
    payload_len, pos = _read_vi(data, 0)
    payload = data[pos : pos + payload_len]

    f = _parse_fields(payload)
    result: dict = {}

    if 1 in f:
        result["action_id"] = f[1][0]
    if 32 in f:
        result["error"] = f[32][0]
        return result
    if 33 in f:  # CreateSessionResult
        sf = _parse_fields(f[33][0])
        result["session_id"] = sf.get(1, [0])[0]
    if 34 in f:  # PlaceOrderResult
        result["place_result"] = _parse_place_result(f[34][0])
    if 35 in f:  # CancelOrderResult
        result["cancelled"] = True

    return result


def _parse_place_result(data: bytes) -> dict:
    f = _parse_fields(data)
    result: dict = {"order_id": None, "fills": [], "account_id": None}

    if 1 in f:  # Posted
        pf = _parse_fields(f[1][0])
        result["order_id"] = pf.get(5, [None])[0]
        result["account_id"] = pf.get(6, [None])[0]
        result["posted_side"] = pf.get(1, [0])[0]
        result["posted_price"] = pf.get(3, [0])[0]
        result["posted_size"] = pf.get(4, [0])[0]

    for trade_bytes in f.get(2, []):  # Trade (fill)
        tf = _parse_fields(trade_bytes)
        result["fills"].append(
            {"order_id": tf.get(2, [0])[0], "price": tf.get(4, [0])[0], "size": tf.get(5, [0])[0]}
        )

    return result


# MARK: Turnkey


class ZeroOneTurnkey:
    def __init__(self, privkey: str, proxy: str | None = None):
        raw = bytes.fromhex(privkey.removeprefix("0x").zfill(64))
        self._evm_account: LocalAccount = Account.from_key(raw)
        secp_key = derive_private_key(int.from_bytes(raw, "big"), SECP256K1())
        self._evm_pubkey_hex = (
            secp_key.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint).hex()
        )

        self._ephem_key = generate_private_key(SECP256R1())
        self._ephem_pubkey_hex = (
            self._ephem_key.public_key()
            .public_bytes(Encoding.X962, PublicFormat.CompressedPoint)
            .hex()
        )
        self._sub_org_id: str | None = None
        self._session_token: str | None = None

        headers = {"Referer": f"{ZERO1_APP}/", "Origin": ZERO1_APP}
        self._janus = AsyncHttp(baseurl=ZERO1_APP, headers=headers, proxy=proxy)
        self._api = AsyncHttp(baseurl=TURNKEY_API, headers=headers, proxy=proxy)

    def _stamp_eip191(self, body: str) -> str:
        signed = self._evm_account.sign_message(encode_defunct(text=body))
        stamp = {
            "publicKey": self._evm_pubkey_hex,
            "scheme": "SIGNATURE_SCHEME_TK_API_SECP256K1_EIP191",
            "signature": encode_dss_signature(signed.r, signed.s).hex(),
        }
        return base64.b64encode(json.dumps(stamp, separators=(",", ":")).encode()).decode()

    def _stamp_p256(self, body: bytes) -> str:
        stamp = {
            "publicKey": self._ephem_pubkey_hex,
            "scheme": "SIGNATURE_SCHEME_TK_API_P256",
            "signature": self._ephem_key.sign(body, ECDSA(SHA256())).hex(),
        }
        return base64.b64encode(json.dumps(stamp, separators=(",", ":")).encode()).decode()

    async def _call(self, path: str, body: dict, *, eip191: bool = False) -> dict:
        data = json.dumps(
            {**body, "timestampMs": str(int(time.time() * 1000))}, separators=(",", ":")
        )
        if eip191:
            rep = await self._api.request(
                "POST",
                path,
                data=data,
                headers={"x-stamp": self._stamp_eip191(data), "Content-Type": "application/json"},
            )
        else:
            enc = data.encode()
            rep = await self._api.request(
                "POST",
                path,
                data=enc,
                headers={
                    "x-stamp": self._stamp_p256(enc),
                    "Content-Type": "text/plain;charset=UTF-8",
                    "x-client-version": "@turnkey/core@1.7.0",
                },
            )
        if not rep.ok:
            raise ApiError(f"Turnkey {path} failed", rep)
        return rep.json()

    def _jwt_org_id(self, token: str) -> str:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["organization_id"]

    @ttl_cache(86400)
    async def login(self) -> str:
        """Authenticate via EVM key → Turnkey (via Janus proxy) → returns Solana address."""
        challenge_payload = {
            "sessionPublicKey": self._ephem_pubkey_hex,
            "walletAddress": self._evm_account.address.lower(),
            "network": "ethereum",
        }
        challenge_rep = await self._janus.request(
            "POST", ZERO1_JANUS_CHALLENGE_PATH, json=challenge_payload
        )
        if not challenge_rep.ok:
            raise ApiError("Turnkey wallet challenge via Janus failed", challenge_rep)

        challenge = challenge_rep.json().get("data")
        if not isinstance(challenge, dict) or not challenge.get("challengeId"):
            raise ApiError("Turnkey wallet challenge via Janus returned invalid data")

        body = json.dumps(
            {
                "parameters": {
                    "publicKey": self._ephem_pubkey_hex,
                    "expirationSeconds": "1209600",
                    "invalidateExisting": True,
                },
                "organizationId": TURNKEY_ORG_ID,
                "timestampMs": str(int(time.time() * 1000)),
                "type": "ACTIVITY_TYPE_STAMP_LOGIN",
            },
            separators=(",", ":"),
        )
        stamp = self._stamp_eip191(body)
        payload = {
            "signedRequest": {
                "body": body,
                "stamp": {"stampHeaderName": "X-Stamp", "stampHeaderValue": stamp},
                "url": f"{TURNKEY_API}/public/v1/submit/stamp_login",
            },
            "expectedAddress": self._evm_account.address.lower(),
            "challenge": {
                "challengeId": challenge["challengeId"],
                "nonce": challenge["nonce"],
            },
        }
        rep = await self._janus.request("POST", ZERO1_JANUS_LOGIN_PATH, json=payload)
        if not rep.ok:
            raise ApiError("Turnkey stamp_login via Janus failed", rep)

        rep_data = rep.json()["data"]
        session_token = rep_data["session"]["sessionToken"]
        self._session_token = session_token
        user = rep_data.get("identifyData", {}).get("user", {})
        self._sub_org_id = user.get("turnkeySuborgId") or self._jwt_org_id(session_token)

        res = await self._call(
            "/public/v1/query/list_wallet_accounts",
            {
                "organizationId": self._sub_org_id,
                "includeWalletDetails": True,
                "paginationOptions": {"limit": "100"},
            },
        )
        for acc in res.get("accounts", []):
            if acc.get("curve") == "CURVE_ED25519":
                return acc["address"]
        raise ApiError("Turnkey Ed25519 wallet not found")

    async def n1_auth_headers(self) -> dict[str, str]:
        """Build N1 session-proof headers for authenticated 01.xyz API routes."""
        await self.login()
        assert self._session_token is not None

        timestamp_ms = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4())
        body_hash = hashlib.sha256(N1_AUTH_IDENTIFY_BODY.encode()).hexdigest()
        proof = "\n".join(
            [
                N1_SESSION_STAMP_PREFIX,
                "POST",
                N1_AUTH_IDENTIFY_PATH,
                body_hash,
                timestamp_ms,
                nonce,
            ]
        )

        return {
            "Authorization": f"Bearer {self._session_token}",
            "Content-Type": "application/json",
            "X-N1-Session-Stamp": self._stamp_p256(proof.encode()),
            "X-N1-Session-Timestamp": timestamp_ms,
            "X-N1-Session-Nonce": nonce,
        }

    async def sign_payload(self, payload_hex: str) -> bytes:
        """Sign Nord action payload (hex string) via Turnkey managed Ed25519 key."""
        solana_addr = await self.login()
        assert self._sub_org_id is not None
        res = await self._call(
            "/public/v1/submit/sign_raw_payload",
            {
                "parameters": {
                    "signWith": solana_addr,
                    "payload": payload_hex,
                    "encoding": "PAYLOAD_ENCODING_TEXT_UTF8",
                    "hashFunction": "HASH_FUNCTION_NOT_APPLICABLE",
                },
                "organizationId": self._sub_org_id,
                "type": "ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2",
            },
        )
        r = res["activity"]["result"]["signRawPayloadResult"]
        return bytes.fromhex(r["r"]) + bytes.fromhex(r["s"])


# MARK: Client


class ZeroOnePoint(BaseModel):
    start_window: datetime
    points: Decimal


@bind_log_context
class ZeroOneClient:
    exchange = "zero1"

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return ZeroOneClient

    @classmethod
    def to_week_label(cls, dt: datetime) -> str:
        return utils.to_period_week(dt, genesis=_POINTS_GENESIS)

    @classmethod
    def from_config(cls, cfg: AccountConfig) -> Self:
        return cls(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.name = name
        self.address = utils.parse_eth_key(privkey, name).address
        self._turnkey = ZeroOneTurnkey(privkey, proxy)
        self._session_key = Ed25519PrivateKey.generate()
        self._session_pubkey = self._session_key.public_key().public_bytes_raw()
        self._session_id: int | None = None
        self._account_id: int | None = None
        self._login_lock = asyncio.Lock()
        self.http = AsyncHttp(
            baseurl=NORD_API,
            headers={"Referer": f"{ZERO1_APP}/", "Origin": ZERO1_APP},
            proxy=proxy,
            cookies_file=f".cache/zero1_{utils.short_addr(self.address)}_http.pkl",
        )

    async def _call(self, method: HttpMethod, path: str, **kwargs):
        rep = await self.http.request(method, path, **kwargs)
        if rep.status_code == 404:
            raise NotFoundError("Not found", rep)
        if not rep.ok:
            raise ApiError("API error", rep)
        return rep.json()

    # MARK: Auth

    async def _get_server_ts(self) -> int:
        return int(await self._call("GET", "/timestamp"))

    async def _login(self) -> None:
        solana_addr = await self._turnkey.login()
        # logger.info(f"Turnkey Solana address: {solana_addr}")

        data = await self._call("GET", f"/user/{solana_addr}")
        if not data.get("accountIds"):
            addr = utils.short_addr(solana_addr)
            raise ApiError(f"Nord account not found for {addr} — connect wallet on 01.xyz first")
        account_ids = data["accountIds"]
        self._account_id = account_ids[0]

        ts = await self._get_server_ts()
        user_pubkey = base58.b58decode(solana_addr)
        expiry = ts + 3600 * 24

        # CreateSession action (field 4)
        inner = _fb(1, user_pubkey) + _fb(2, self._session_pubkey) + _fv(3, expiry)
        action = _fv(1, ts) + _fb(4, inner)

        # user_sign: Turnkey signs hex(raw) with managed Ed25519 key
        raw = _vi(len(action)) + action
        sig = await self._turnkey.sign_payload(raw.hex())

        receipt = await self._submit_raw(action, sig)
        if "error" in receipt:
            raise ApiError(f"Nord CreateSession error code: {receipt['error']}")
        self._session_id = receipt.get("session_id")
        # logger.info(f"Nord session {self._session_id}, account {self._account_id}")

    async def _ensure_session(self) -> tuple[int, int]:
        if self._session_id is not None and self._account_id is not None:
            return self._session_id, self._account_id
        async with self._login_lock:
            if self._session_id is None:
                await self._login()
        assert self._session_id is not None and self._account_id is not None
        return self._session_id, self._account_id

    async def _submit_raw(self, action: bytes, sig: bytes) -> dict:
        raw = _vi(len(action)) + action
        rep = await self.http.request(
            "POST",
            "/action",
            data=raw + sig,
            headers={"Content-Type": "application/octet-stream"},
        )
        if not rep.ok:
            raise ApiError("Nord /action failed", rep)
        return _read_receipt(rep.content)

    async def _action(self, kind_field: int, kind_data: bytes, nonce: int = 0) -> dict:
        ts = await self._get_server_ts()
        action = _fv(1, ts)
        if nonce:
            action += _fv(2, nonce)
        action += _fb(kind_field, kind_data)
        raw = _vi(len(action)) + action
        sig = self._session_key.sign(raw)
        receipt = await self._submit_raw(action, sig)
        if "error" in receipt:
            raise ApiError(f"Nord action error code: {receipt['error']}")
        return receipt

    # MARK: Lifecycle

    async def warmup(self) -> None:
        await self._ensure_session()

    async def registered(self) -> bool:
        try:
            await self._ensure_session()
            return True
        except Exception:
            return False

    # MARK: Market info

    @ttl_cache(3600)
    async def _meta(self) -> dict:
        return await self._call("GET", "/info")

    async def _market(self, symbol: str) -> dict:
        info = await self._meta()
        sym = symbol.upper() + "USD"
        for m in info.get("markets", []):
            if m["symbol"] == sym:
                return m
        raise ApiError(f"Market not found: {symbol}")

    @ttl_cache(3600)
    async def get_symbols(self) -> list[str]:
        info = await self._meta()
        return [m["symbol"].removesuffix("USD") for m in info.get("markets", [])]

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        return True

    async def get_lot_size(self, symbol: str) -> Decimal:
        m = await self._market(symbol)
        return Decimal(10) ** -m["sizeDecimals"]

    async def get_tick_size(self, symbol: str) -> Decimal:
        m = await self._market(symbol)
        return Decimal(10) ** -m["priceDecimals"]

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return Decimal(10)

    # MARK: Prices

    @ttl_cache(5)
    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        m = await self._market(symbol)
        ob = await self._call("GET", f"/market/{m['marketId']}/orderbook")
        bid = Decimal(str(ob["bids"][0][0])) if ob.get("bids") else Decimal(0)
        ask = Decimal(str(ob["asks"][0][0])) if ob.get("asks") else Decimal(0)
        return bid, ask

    @ttl_cache(5)
    async def get_order_book(self, symbol: str) -> OrderBook:
        m = await self._market(symbol)
        ob = await self._call("GET", f"/market/{m['marketId']}/orderbook")
        return OrderBook.build(bids=ob.get("bids", [])[:5], asks=ob.get("asks", [])[:5])

    async def get_price(self, symbol: str) -> Decimal:
        bid, ask = await self.get_bbo(symbol)
        return (bid + ask) / 2

    # MARK: History

    async def paged(self, path: str, since: datetime | None = None) -> list[dict]:
        page_size = 255
        records: list[dict] = []
        cursor: str | None = None

        while True:
            params = {"pageSize": str(page_size)}
            if since and not cursor:
                params["since"] = since.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            if cursor:
                params["startInclusive"] = cursor

            rep = await self.http.request("GET", path, params=params)
            if not rep.ok:
                logger.warning(f"Paged req failed: {path} {params} {rep.status_code} {rep.text}")
                break

            data = rep.json()
            batch = data.get("items", [])
            records.extend(batch)
            cursor = data.get("nextStartInclusive")
            if not cursor or len(batch) < page_size:
                break

        fields = ["actionId", "marketId", "tradeId", "orderId", "time"]
        for r in records:
            values = [str(r[x]) for x in fields if x in r]
            assert len(values) > 0, f"No ID fields found in record: {r}"
            r["uid"] = utils.sha256("-".join(values))

        rs = {r["uid"]: r for r in records}.values()  # dedupe by uid
        rs = sorted(rs, key=lambda r: r.get("time", 0))  # sort by time
        return rs

    # MARK: Account

    async def balance(self) -> Decimal:
        _, acc_id = await self._ensure_session()
        data = await self._call("GET", f"/account/{acc_id}")
        for bal in data.get("balances", []):
            if bal["token"] == "USDC":
                return Decimal(str(bal["amount"]))
        return Decimal(0)

    @ttl_cache(3600)
    async def get_fee_rates(self) -> tuple[Decimal, Decimal]:
        _, acc_id = await self._ensure_session()
        try:
            tr, mr = await asyncio.gather(
                self._call("GET", f"/market/0/fees/taker/{acc_id}"),
                self._call("GET", f"/market/0/fees/maker/{acc_id}"),
            )
            return Decimal(str(tr)), Decimal(str(mr))
        except Exception:
            return Decimal("0.00035"), Decimal("0.0001")

    async def get_leverage(self, symbol: str) -> int | None:
        return None  # Nord uses risk-based margin; leverage on 01.xyz is frontend-only

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        pass

    # MARK: Positions

    async def positions(self) -> list[Position]:
        _, acc_id = await self._ensure_session()
        data = await self._call("GET", f"/account/{acc_id}")

        info = await self._meta()
        market_map = {m["marketId"]: m for m in info.get("markets", [])}

        result = []
        for pos in data.get("positions", []):
            perp = pos.get("perp")
            if not perp:
                continue
            base_size = Decimal(str(perp["baseSize"]))
            if base_size == 0:
                continue
            m = market_map.get(pos["marketId"], {})
            symbol = m.get("symbol", str(pos["marketId"])).removesuffix("USD")
            side: Side = "bid" if perp.get("isLong", base_size > 0) else "ask"
            result.append(
                Position(
                    id=str(pos["marketId"]),
                    symbol=symbol,
                    side=side,
                    size=abs(base_size),
                    entry_price=Decimal(str(perp.get("price", 0))),
                    unrealized_pnl=Decimal(str(perp.get("sizePricePnl", 0))),
                )
            )
        return result

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
        fill_mode: int,
        reduce_only=False,
    ) -> Order:
        sess_id, acc_id = await self._ensure_session()
        m = await self._market(symbol)
        market_id = m["marketId"]
        price_dec, size_dec = m["priceDecimals"], m["sizeDecimals"]

        side_val = 1 if side == "bid" else 0
        price_int = int(price * Decimal(10) ** price_dec)
        size_int = int(qty * Decimal(10) ** size_dec)

        # PlaceOrder fields: session_id=1, market_id=2, side=3, fill_mode=4,
        # reduce_only=5, price=6, size=7, sender_account_id=34
        inner = (
            _fv(1, sess_id)
            + _fv(2, market_id)
            + _fv(3, side_val)
            + _fv(4, fill_mode)
            + _fv(5, int(reduce_only))
            + _fv(6, price_int)
            + _fv(7, size_int)
            + _fv(34, acc_id)
        )

        receipt = await self._action(7, inner)  # field 7 = place_order
        pr = receipt.get("place_result", {})
        fills = pr.get("fills", [])
        order_id = pr.get("order_id")

        if fills:
            total_qty = sum(
                (Decimal(str(f["size"])) * Decimal(10) ** -size_dec for f in fills), Decimal(0)
            )
            avg_px = sum(
                (Decimal(str(f["price"])) * Decimal(10) ** -price_dec for f in fills), Decimal(0)
            ) / len(fills)
            return Order(
                id=str(order_id or fills[0].get("order_id", 0)),
                symbol=symbol,
                side=side,
                size=qty,
                filled=total_qty,
                price=avg_px,
                status=OrderStatus.FILLED,
                reduce_only=reduce_only,
            )

        if order_id:
            return Order(
                id=str(order_id),
                symbol=symbol,
                side=side,
                size=qty,
                filled=Decimal(0),
                price=price,
                status=OrderStatus.OPEN,
                reduce_only=reduce_only,
            )

        raise ApiError(f"PlaceOrder: unexpected receipt: {receipt}")

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        bid, ask = await self.get_bbo(symbol)
        mid = (bid + ask) / 2
        m = await self._market(symbol)
        tick = Decimal(10) ** -m["priceDecimals"]
        slippage = Decimal("1.05") if side == "bid" else Decimal("0.95")
        price = utils.round_to_tick_size(mid * slippage, tick)
        return await self._place_order(symbol, side, qty, price, 2, reduce_only)  # IOC

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        if reduce_only:
            # Nord rejects LIMIT and POST_ONLY with reduce_only=True (error 140 always).
            # Only IOC works for closing. If price moved and IOC finds no counterpart
            # (error 133), fall back to market which uses BBO ± slippage and always fills.
            try:
                return await self._place_order(symbol, side, qty, price, 2, reduce_only)  # IOC
            except ApiError as e:
                if "133" not in str(e):  # IMMEDIATE_ORDER_GOT_NO_FILLS
                    raise
                logger.warning(f"limit_order {symbol}: IOC got no fills, falling back to market")
                return await self.market_order(symbol, side, qty, reduce_only)

        return await self._place_order(symbol, side, qty, price, 0, reduce_only)  # LIMIT

    async def get_order(self, order_id: str) -> Order | None:
        try:
            o = await self._call("GET", f"/order/{order_id}")
        except NotFoundError:
            return None
        symbol = o.get("marketSymbol", "?").removesuffix("USD")
        raw_size = Decimal(str(o.get("placedSize") or 0))
        filled = Decimal(str(o.get("filledSize") or 0))
        price = Decimal(str(o.get("placedPrice") or 0))
        side: Side = "bid" if o.get("side") == "bid" else "ask"

        reason = o.get("finalizationReason")
        if reason == "Canceled":
            status = OrderStatus.CANCELED
        elif filled > 0 and filled >= raw_size:
            status = OrderStatus.FILLED
        else:
            status = OrderStatus.OPEN

        return Order(
            id=order_id,
            symbol=symbol,
            side=side,
            size=raw_size,
            filled=filled,
            price=price,
            status=status,
        )

    async def cancel_order(self, order: Order) -> bool:
        sess_id, acc_id = await self._ensure_session()
        # CancelOrderById: session_id=1, order_id=2, sender_account_id=33
        inner = _fv(1, sess_id) + _fv(2, int(order.id)) + _fv(33, acc_id)
        try:
            await self._action(8, inner)  # field 8 = cancel_order_by_id
            return True
        except ApiError as e:
            logger.warning(f"cancel_order failed: {e}")
            return False

    async def cancel_all_orders(self) -> int:
        _, acc_id = await self._ensure_session()
        data = await self._call("GET", f"/account/{acc_id}/orders", params={"pageSize": 100})
        items = data.get("items", data) if isinstance(data, dict) else data
        count = 0
        for o in items:
            oid = o.get("orderId")
            if not oid or o.get("finalizationReason"):
                continue
            dummy = Order(
                id=str(oid),
                symbol="?",
                side="bid",
                size=Decimal(0),
                filled=Decimal(0),
                price=None,
                status=OrderStatus.OPEN,
            )
            if await self.cancel_order(dummy):
                count += 1
        return count

    # MARK: Profile

    @ttl_cache(86400)
    async def _ensure_points_auth(self, solana_addr: str, acc_id: int) -> None:
        await ensure_unwaf(self.http, f"{ZERO1_APP}/")

        # NOTE: When auto-discovery breaks, do this manually:
        # 1. Open DevTools > Application > "Clear site data" > Reload page
        # 2. Login, filter network: "has-response-header:set-cookie domain:01.xyz"
        # 3. Copy header values into AUTH_ACT / NEXT_DPL above, delete _POINTS_META_CACHE

        act, dpl = await _get_points_meta(self.http)
        pld = {"address": solana_addr, "accountId": acc_id, "network": "mainnet", "sessionId": None}
        hdr = {"next-action": act, "x-deployment-id": dpl}
        rep = await self.http.request("POST", f"{ZERO1_APP}/points", json=[pld], headers=hdr)

        if rep.status_code == 404 and rep.headers.get("x-nextjs-action-not-found"):
            act, dpl = await _get_points_meta(self.http, fresh=True, stale_act=act)
            hdr = {"next-action": act, "x-deployment-id": dpl}
            rep = await self.http.request("POST", f"{ZERO1_APP}/points", json=[pld], headers=hdr)

        cname = f"01session_{solana_addr}_mainnet"
        if cname not in self.http.session.cookies:
            logger.warning(f"Points auth cookie not found ({rep.status_code})")

    async def _fetch_points(self, solana_addr: str, acc_id: int) -> dict:
        await ensure_unwaf(self.http, f"{ZERO1_APP}/")
        headers = await self._turnkey.n1_auth_headers()
        headers["Referer"] = f"{ZERO1_APP}/points"

        url = f"{ZERO1_APP}/api/points?walletAddress={solana_addr}"
        rep = await self.http.request("GET", url, headers=headers)
        if rep.status_code != 200:
            logger.warning(f"Failed to fetch points: {rep.status_code} {rep.text}")

        return rep.json()

    async def _points(self, solana_addr: str, acc_id: int) -> tuple[Decimal, int | None]:
        data = await self._fetch_points(solana_addr, acc_id)
        lb = data.get("leaderboardData", {}) or {}
        points = sum(
            (Decimal(str(d.get("points", 0))) for d in data.get("data", [])),
            Decimal(0),
        )
        if not points:
            points = Decimal(str(lb.get("points", 0)))
        return points, lb.get("rank")

    async def points_history(self) -> list[ZeroOnePoint]:
        _, acc_id = await self._ensure_session()
        solana_addr = await self._turnkey.login()
        data = await self._fetch_points(solana_addr, acc_id)
        result = []
        for d in data.get("data", []):
            stage = d["stage"]
            n = 0 if stage == "referral_rewards" else int(stage.split("_")[1])
            start = _POINTS_GENESIS + timedelta(weeks=n - 1)
            result.append(ZeroOnePoint(start_window=start, points=Decimal(str(d["points"]))))
        return result

    async def _total_volume(self, acc_id: int) -> Decimal:
        pld = {"accountId": acc_id, "since": "2020-01-01T00:00:00Z"}
        res = await self._call("GET", "/account/volume", params=pld)
        return sum((Decimal(str(e.get("volumeQuote", 0))) for e in res), Decimal(0))

    async def _total_pnl(self, acc_id: int) -> Decimal:
        pnl_data, vol_data = await asyncio.gather(
            self._call("GET", f"{ZERO1_APP}/api/calendar/{acc_id}"),
            self._call("GET", f"{ZERO1_APP}/api/volume-calendar/{acc_id}"),
        )
        pnl_days = pnl_data.get("days", {})
        fee_days = vol_data.get("days", {})
        all_days = set(pnl_days) | set(fee_days)
        total = Decimal(0)
        for day in all_days:
            d = pnl_days.get(day, {})
            total += Decimal(str(d.get("tradingPnl", 0)))
            total += Decimal(str(d.get("fundingPnl", 0)))
            total -= Decimal(str(fee_days.get(day, {}).get("totalFees", 0)))
        return total

    async def profile(self) -> ProfileInfo:
        _, acc_id = await self._ensure_session()
        solana_addr = await self._turnkey.login()
        account, vol, pnl, (pts, rank) = await asyncio.gather(
            self._call("GET", f"/account/{acc_id}"),
            self._total_volume(acc_id),
            self._total_pnl(acc_id),
            self._points(solana_addr, acc_id),
        )
        bal = Decimal(0)
        for b in account.get("balances", []):
            if b["token"] == "USDC":
                bal = Decimal(str(b["amount"]))

        addr = utils.short_addr(self.address)
        return ProfileInfo(addr=addr, balance=bal, volume=vol, pnl=pnl, points=pts, rank=rank)
