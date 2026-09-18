import sqlite3
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lib import support

KEY = "DF-ABCDE-FGHIJ-KLMNO-PQRST-UVWXYZ"
SIGNING_KEY = Ed25519PrivateKey.generate()


@pytest.fixture(autouse=True)
def use_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(support, "DB_PATH", tmp_path / "support.db")
    monkeypatch.setattr(support, "_key", SIGNING_KEY.public_key())


def make_license(*, paid_until: int, account_limit: int = 5) -> support.License:
    return {"plan": "small", "account_limit": account_limit, "paid_until": paid_until}


def make_receipt(license: support.License) -> tuple[str, support.License]:
    payload = {
        "sub": "license-1",
        "plan": license["plan"],
        "account_limit": license["account_limit"],
        "key_hash": sha256(support.normalize_key(KEY).encode()).hexdigest(),
        "iat": 100,
        "exp": license["paid_until"],
    }
    return jwt.encode(payload, SIGNING_KEY, algorithm="EdDSA"), license


def test_normalize_key():
    assert support.normalize_key("abcde fghij klmno pqrst uvwxyz") == KEY
    assert support.normalize_key(KEY.lower()) == KEY

    with pytest.raises(ValueError):
        support.normalize_key("short")


def test_address_hash_normalization():
    lower = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    assert support.address_hash(lower) == support.address_hash(lower.upper().replace("0X", "0x"))
    assert support.address_hash("Wallet") != support.address_hash("wallet")


def test_default_db_path_uses_platform_data_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(support.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert support.default_db_path() == tmp_path / "delta-farmer" / "support.db"

    monkeypatch.setattr(support.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert support.default_db_path() == tmp_path / "xdg" / "delta-farmer" / "support.db"


def test_store_counts_wallets_across_runs_without_raw_addresses(tmp_path):
    path = support.DB_PATH
    evm = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"

    assert support.record_accounts([evm, evm.upper().replace("0X", "0x")], now=100) == 1
    assert support.record_accounts(["SolanaWallet", "solanawallet"], now=200) == 3

    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT identity_hash FROM account_usage").fetchall()
    assert len(rows) == 3
    assert evm not in repr(rows)
    assert "SolanaWallet" not in repr(rows)

    assert support.record_accounts(["new-wallet"], now=200 + 30 * 86400 + 1) == 1


def test_store_handles_concurrent_process_shape(tmp_path):
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(support.record_accounts, ["wallet-one"], 100)
        second = pool.submit(support.record_accounts, ["wallet-two"], 100)
        first.result()
        second.result()

    assert support.count_accounts(now=100) == 2


async def test_activate_and_refresh_same_key(monkeypatch):
    initial = make_license(paid_until=1_000)
    renewed = make_license(paid_until=2_000)
    responses = iter([make_receipt(initial), make_receipt(renewed)])

    async def fetch_license(_key: str):
        return next(responses)

    monkeypatch.setattr(support, "fetch_license", fetch_license)
    monkeypatch.setattr(support.time, "time", lambda: 100)

    assert await support.activate(KEY) == initial
    status = await support.get_status(3, now=200, force=True)
    saved = support.load_subscription()

    assert status == {
        "state": "active",
        "account_count": 3,
        "license": renewed,
        "offline": False,
    }
    assert saved and saved[0] == KEY
    assert saved[1] and support.decode_token(saved[1], KEY) == renewed


async def test_status_uses_offline_grace_and_never_blocks(monkeypatch):
    license = make_license(paid_until=1_000)
    token, _license = make_receipt(license)
    support.save_subscription(KEY, token, checked_at=0)

    async def unavailable(_key: str):
        raise OSError("offline")

    monkeypatch.setattr(support, "fetch_license", unavailable)
    status = await support.get_status(2, now=1_001, force=True)
    assert status == {
        "state": "active",
        "account_count": 2,
        "license": license,
        "offline": True,
    }

    monkeypatch.setattr(support, "record_accounts", lambda *_args: 1 / 0)
    failed = await support.check(["wallet"])
    assert failed == {
        "state": "unavailable",
        "account_count": 0,
        "license": None,
        "offline": True,
    }


def test_modified_token_is_rejected():
    token, _license = make_receipt(make_license(paid_until=1_000))
    with pytest.raises(jwt.PyJWTError):
        support.decode_token(token + "x", KEY)


def test_notice_is_advisory():
    license = make_license(paid_until=1_000)
    active = {"state": "active", "account_count": 5, "license": license, "offline": False}
    over_limit = {"state": "active", "account_count": 6, "license": license, "offline": False}

    assert support.notice(active) is None
    assert "6 accounts last month" in support.notice(over_limit)
    assert "supporter: free" in support.notice(
        {"state": "none", "account_count": 3, "license": None, "offline": False}
    )
