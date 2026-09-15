# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Probably works in production

import base64
import hashlib
import secrets
from dataclasses import dataclass, field

from .field import (
    FP5_ONE,
    FP5_ZERO,
    Fp5,
    fp5_add,
    fp5_from_bytes,
    fp5_inv_or_zero,
    fp5_mul,
    fp5_square,
    fp5_sub,
    fp5_to_bytes,
    make_fp5,
)
from .poseidon import bytes_to_fields, poseidon_hash_fp5

SCALAR_ORDER = 1067993516717146951041484916571792702745057740581727230159139685185762082554198619328292418486241
SCALAR_BITS = SCALAR_ORDER.bit_length()
LIGHTER_CHAIN_ID = 304
CHANGE_PUB_KEY_TX_TYPE = 8
CREATE_ORDER_TX_TYPE = 14
CANCEL_ORDER_TX_TYPE = 15
CANCEL_ALL_ORDERS_TX_TYPE = 16
UPDATE_LEVERAGE_TX_TYPE = 20
SKIP_NONCE_ATTRIBUTE = 4

MAX_ACCOUNT_INDEX = (1 << 48) - 2
MAX_API_KEY_INDEX = (1 << 8) - 2
MAX_MARKET_INDEX = (1 << 15) - 1
MAX_ORDER_VALUE = (1 << 48) - 1
MAX_ORDER_PRICE = (1 << 32) - 1
MAX_TIMESTAMP = (1 << 48) - 1

CURVE_B = make_fp5((0, 263, 0, 0, 0))
CURVE_B2 = make_fp5((0, 526, 0, 0, 0))
CURVE_B4 = make_fp5((0, 1052, 0, 0, 0))


@dataclass(frozen=True, slots=True)
class Point:
    x: Fp5
    z: Fp5
    u: Fp5
    t: Fp5

    def add(self, other: "Point") -> "Point":
        t1 = fp5_mul(self.x, other.x)
        t2 = fp5_mul(self.z, other.z)
        t3 = fp5_mul(self.u, other.u)
        t4 = fp5_mul(self.t, other.t)
        t5 = fp5_sub(fp5_mul(fp5_add(self.x, self.z), fp5_add(other.x, other.z)), fp5_add(t1, t2))
        t6 = fp5_sub(fp5_mul(fp5_add(self.u, self.t), fp5_add(other.u, other.t)), fp5_add(t3, t4))
        t7 = fp5_add(t1, fp5_mul(t2, CURVE_B))
        t8 = fp5_mul(t4, t7)
        t9 = fp5_mul(t3, fp5_add(fp5_mul(t5, CURVE_B2), fp5_add(t7, t7)))
        t10 = fp5_mul(fp5_add(t4, fp5_add(t3, t3)), fp5_add(t5, t7))
        return Point(
            x=fp5_mul(fp5_sub(t10, t8), CURVE_B),
            z=fp5_sub(t8, t9),
            u=fp5_mul(t6, fp5_sub(fp5_mul(t2, CURVE_B), t1)),
            t=fp5_add(t8, t9),
        )

    def double(self) -> "Point":
        t1 = fp5_mul(self.z, self.t)
        t2 = fp5_mul(t1, self.t)
        x1 = fp5_square(t2)
        z1 = fp5_mul(t1, self.u)
        t3 = fp5_square(self.u)
        w1 = fp5_sub(t2, fp5_mul(t3, fp5_add(fp5_add(self.x, self.z), fp5_add(self.x, self.z))))
        t4 = fp5_square(z1)
        x = fp5_mul(t4, CURVE_B4)
        z = fp5_square(w1)
        u = fp5_sub(fp5_square(fp5_add(w1, z1)), fp5_add(t4, z))
        t = fp5_sub(fp5_add(x1, x1), fp5_add(fp5_mul(t4, make_fp5((4, 0, 0, 0, 0))), z))
        return Point(x=x, z=z, u=u, t=t)

    def mul(self, scalar: int) -> "Point":
        if scalar < 0:
            raise ValueError("curve scalar must be non-negative")

        scalar %= SCALAR_ORDER
        result = NEUTRAL_POINT
        for shift in range(SCALAR_BITS - 1, -1, -1):
            doubled = result.double()
            added = doubled.add(self)
            result = (doubled, added)[(scalar >> shift) & 1]

        return result

    def encode(self) -> Fp5:
        return fp5_mul(self.t, fp5_inv_or_zero(self.u))


NEUTRAL_POINT = Point(x=FP5_ZERO, z=FP5_ONE, u=FP5_ZERO, t=FP5_ONE)
GENERATOR = Point(
    x=make_fp5(
        (
            12883135586176881569,
            4356519642755055268,
            5248930565894896907,
            2165973894480315022,
            2448410071095648785,
        )
    ),
    z=FP5_ONE,
    u=FP5_ONE,
    t=make_fp5((4, 0, 0, 0, 0)),
)


@dataclass(frozen=True, slots=True)
class SignedTx:
    tx_type: int
    tx_hash: str
    info: dict[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthToken:
    deadline: int
    token: str = field(repr=False)


def _check_range(name: str, value: int, minimum: int, maximum: int) -> None:
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")


def _scalar_from_fp5(value: Fp5) -> int:
    return sum(limb << (64 * i) for i, limb in enumerate(value)) % SCALAR_ORDER


def _attributes_hash(skip_nonce: bool) -> Fp5 | None:
    if not skip_nonce:
        return None

    return poseidon_hash_fp5((SKIP_NONCE_ATTRIBUTE, 1, 0, 0, 0, 0, 0, 0))


class LighterSigner:
    __slots__ = ("_private_key", "_public_key")

    def __init__(self, seed: str):
        seed = seed.removeprefix("0x")
        try:
            seed_bytes = bytes.fromhex(seed)
        except ValueError:
            raise ValueError("Lighter seed must be hexadecimal") from None

        if len(seed_bytes) < 32:
            raise ValueError(f"Lighter seed must be at least 32 bytes, got {len(seed_bytes)}")

        material = hashlib.sha256(b"\x01" + seed_bytes).digest()
        material += hashlib.sha256(b"\x02" + seed_bytes).digest()[:8]
        self._private_key = int.from_bytes(material, "big") % SCALAR_ORDER
        if self._private_key == 0:
            raise ValueError("Lighter seed produced an invalid zero scalar")

        self._public_key = fp5_to_bytes(GENERATOR.mul(self._private_key).encode())

    @property
    def public_key(self) -> bytes:
        return self._public_key

    @property
    def public_key_hex(self) -> str:
        return self._public_key.hex()

    def change_pub_key_message(self, account_index: int, api_key_index: int, nonce: int) -> str:
        self._validate_indices(account_index, api_key_index, nonce)
        return (
            "Register Lighter Account\n\n"
            f"pubkey: 0x{self.public_key_hex}\n"
            f"nonce: 0x{nonce:016x}\n"
            f"account index: 0x{account_index:016x}\n"
            f"api key index: 0x{api_key_index:016x}\n"
            "Only sign this message for a trusted client!"
        )

    def change_pub_key_hash(
        self,
        account_index: int,
        api_key_index: int,
        nonce: int,
        expired_at: int,
        *,
        skip_nonce: bool = True,
        chain_id: int = LIGHTER_CHAIN_ID,
    ) -> bytes:
        self._validate_indices(account_index, api_key_index, nonce)
        _check_range("expired_at", expired_at, 0, MAX_TIMESTAMP)
        _check_range("chain_id", chain_id, 0, (1 << 32) - 1)

        public_key = fp5_from_bytes(self._public_key)
        tx_hash = poseidon_hash_fp5(
            (
                chain_id,
                CHANGE_PUB_KEY_TX_TYPE,
                nonce,
                expired_at,
                account_index,
                api_key_index,
                *public_key,
            )
        )
        attributes_hash = _attributes_hash(skip_nonce)
        if attributes_hash is not None:
            tx_hash = poseidon_hash_fp5((*tx_hash, *attributes_hash))

        return fp5_to_bytes(tx_hash)

    def sign_change_pub_key(
        self,
        account_index: int,
        api_key_index: int,
        nonce: int,
        expired_at: int,
        l1_signature: str,
        *,
        skip_nonce: bool = True,
        chain_id: int = LIGHTER_CHAIN_ID,
    ) -> SignedTx:
        l1_signature = l1_signature.removeprefix("0x")
        try:
            l1_signature_bytes = bytes.fromhex(l1_signature)
        except ValueError:
            raise ValueError("L1 signature must be hexadecimal") from None

        if len(l1_signature_bytes) != 65:
            raise ValueError(f"L1 signature must be 65 bytes, got {len(l1_signature_bytes)}")

        tx_hash = self.change_pub_key_hash(
            account_index,
            api_key_index,
            nonce,
            expired_at,
            skip_nonce=skip_nonce,
            chain_id=chain_id,
        )
        signature = self._sign_hash(tx_hash)
        info: dict[str, object] = {
            "AccountIndex": account_index,
            "ApiKeyIndex": api_key_index,
            "PubKey": base64.b64encode(self._public_key).decode(),
            "L1Sig": f"0x{l1_signature}",
            "ExpiredAt": expired_at,
            "Nonce": nonce,
            "Sig": base64.b64encode(signature).decode(),
            "L2TxAttributes": {str(SKIP_NONCE_ATTRIBUTE): 1} if skip_nonce else None,
        }
        return SignedTx(tx_type=CHANGE_PUB_KEY_TX_TYPE, tx_hash=tx_hash.hex(), info=info)

    def sign_create_order(
        self,
        account_index: int,
        api_key_index: int,
        market_index: int,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        order_type: int,
        time_in_force: int,
        reduce_only: bool,
        trigger_price: int,
        order_expiry: int,
        nonce: int,
        expired_at: int,
    ) -> SignedTx:
        self._validate_indices(account_index, api_key_index, nonce)
        _check_range("market_index", market_index, 0, MAX_MARKET_INDEX)
        _check_range("client_order_index", client_order_index, 0, MAX_ORDER_VALUE)
        _check_range("base_amount", base_amount, 1, MAX_ORDER_VALUE)
        _check_range("price", price, 1, MAX_ORDER_PRICE)
        _check_range("order_type", order_type, 0, 6)
        _check_range("time_in_force", time_in_force, 0, 2)
        _check_range("trigger_price", trigger_price, 0, MAX_ORDER_PRICE)
        _check_range("order_expiry", order_expiry, 0, MAX_TIMESTAMP)
        _check_range("expired_at", expired_at, 0, MAX_TIMESTAMP)

        info = {
            "MarketIndex": market_index,
            "ClientOrderIndex": client_order_index,
            "BaseAmount": base_amount,
            "Price": price,
            "IsAsk": int(is_ask),
            "Type": order_type,
            "TimeInForce": time_in_force,
            "ReduceOnly": int(reduce_only),
            "TriggerPrice": trigger_price,
            "OrderExpiry": order_expiry,
        }
        fields = (
            market_index,
            client_order_index,
            base_amount,
            price,
            int(is_ask),
            order_type,
            time_in_force,
            int(reduce_only),
            trigger_price,
            order_expiry,
        )
        return self._sign_transaction(
            CREATE_ORDER_TX_TYPE,
            account_index,
            api_key_index,
            nonce,
            expired_at,
            fields,
            info,
        )

    def sign_cancel_order(
        self,
        account_index: int,
        api_key_index: int,
        market_index: int,
        order_index: int,
        nonce: int,
        expired_at: int,
    ) -> SignedTx:
        self._validate_indices(account_index, api_key_index, nonce)
        _check_range("market_index", market_index, 0, MAX_MARKET_INDEX)
        _check_range("order_index", order_index, 0, (1 << 63) - 1)
        _check_range("expired_at", expired_at, 0, MAX_TIMESTAMP)
        info = {"MarketIndex": market_index, "Index": order_index}
        return self._sign_transaction(
            CANCEL_ORDER_TX_TYPE,
            account_index,
            api_key_index,
            nonce,
            expired_at,
            tuple(info.values()),
            info,
        )

    def sign_cancel_all_orders(
        self,
        account_index: int,
        api_key_index: int,
        nonce: int,
        expired_at: int,
    ) -> SignedTx:
        self._validate_indices(account_index, api_key_index, nonce)
        _check_range("expired_at", expired_at, 0, MAX_TIMESTAMP)
        info = {"TimeInForce": 0, "Time": 0}
        return self._sign_transaction(
            CANCEL_ALL_ORDERS_TX_TYPE,
            account_index,
            api_key_index,
            nonce,
            expired_at,
            tuple(info.values()),
            info,
        )

    def sign_update_leverage(
        self,
        account_index: int,
        api_key_index: int,
        market_index: int,
        initial_margin_fraction: int,
        nonce: int,
        expired_at: int,
    ) -> SignedTx:
        self._validate_indices(account_index, api_key_index, nonce)
        _check_range("market_index", market_index, 0, MAX_MARKET_INDEX)
        _check_range("initial_margin_fraction", initial_margin_fraction, 1, 10_000)
        _check_range("expired_at", expired_at, 0, MAX_TIMESTAMP)
        info = {
            "MarketIndex": market_index,
            "InitialMarginFraction": initial_margin_fraction,
            "MarginMode": 0,
        }
        return self._sign_transaction(
            UPDATE_LEVERAGE_TX_TYPE,
            account_index,
            api_key_index,
            nonce,
            expired_at,
            tuple(info.values()),
            info,
        )

    def create_auth_token(self, account_index: int, api_key_index: int, deadline: int) -> AuthToken:
        _check_range("account_index", account_index, 0, MAX_ACCOUNT_INDEX)
        _check_range("api_key_index", api_key_index, 0, MAX_API_KEY_INDEX)
        _check_range("deadline", deadline, 0, (1 << 63) - 1)

        message = f"{deadline}:{account_index}:{api_key_index}"
        message_hash = fp5_to_bytes(poseidon_hash_fp5(bytes_to_fields(message.encode())))
        signature = self._sign_hash(message_hash).hex()
        return AuthToken(deadline=deadline, token=f"{message}:{signature}")

    def _sign_hash(self, message_hash: bytes) -> bytes:
        message = fp5_from_bytes(message_hash)
        nonce = 0
        while nonce == 0:
            nonce = secrets.randbelow(SCALAR_ORDER)

        commitment = GENERATOR.mul(nonce).encode()
        challenge = _scalar_from_fp5(poseidon_hash_fp5((*commitment, *message)))
        response = (nonce - challenge * self._private_key) % SCALAR_ORDER
        return response.to_bytes(40, "little") + challenge.to_bytes(40, "little")

    def _sign_transaction(
        self,
        tx_type: int,
        account_index: int,
        api_key_index: int,
        nonce: int,
        expired_at: int,
        fields: tuple[int, ...],
        info: dict[str, object],
    ) -> SignedTx:
        tx_hash = poseidon_hash_fp5(
            (LIGHTER_CHAIN_ID, tx_type, nonce, expired_at, account_index, api_key_index, *fields)
        )
        attributes_hash = _attributes_hash(True)
        assert attributes_hash is not None
        tx_hash = poseidon_hash_fp5((*tx_hash, *attributes_hash))
        tx_hash_bytes = fp5_to_bytes(tx_hash)
        signature = self._sign_hash(tx_hash_bytes)
        tx_info = {
            "AccountIndex": account_index,
            "ApiKeyIndex": api_key_index,
            **info,
            "ExpiredAt": expired_at,
            "Nonce": nonce,
            "Sig": base64.b64encode(signature).decode(),
            "L2TxAttributes": {str(SKIP_NONCE_ATTRIBUTE): 1},
        }
        return SignedTx(tx_type=tx_type, tx_hash=tx_hash_bytes.hex(), info=tx_info)

    @staticmethod
    def _validate_indices(account_index: int, api_key_index: int, nonce: int) -> None:
        _check_range("account_index", account_index, -1, MAX_ACCOUNT_INDEX)
        _check_range("api_key_index", api_key_index, 0, MAX_API_KEY_INDEX)
        _check_range("nonce", nonce, 0, (1 << 63) - 1)
