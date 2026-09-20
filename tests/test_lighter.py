from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from curl_cffi import CurlError

from apps import lighter as lighter_app
from apps.lighter import apply_referral_code, print_info
from clients.lighter import LighterClient
from lib.models import DurationSec, TimeRange
from strategy import ProfileInfo


@pytest.mark.parametrize(
    ("market", "price", "expected"),
    [
        ({"min_base_amount": "0.00020", "min_quote_amount": "10.000000"}, "80496.1", "16.099220"),
        ({"min_base_amount": "0.0100", "min_quote_amount": "10.000000"}, "762.38", "10.000000"),
    ],
)
async def test_min_trade_usd_includes_base_amount(market, price, expected):
    client = object.__new__(LighterClient)
    client.name = "test"
    client._market = AsyncMock(return_value=market)
    client.get_price = AsyncMock(return_value=Decimal(price))

    assert await client.get_min_trade_usd("BTC") == Decimal(expected)


async def test_volume_reconnects_after_connection_failure(monkeypatch):
    failed = AsyncMock()
    failed.__aenter__.side_effect = CurlError("connection reset", 56)
    connected = AsyncMock()
    ws = AsyncMock()
    ws.recv_json.return_value = {
        "type": "subscribed/account_all_trades",
        "total_volume": "123.45",
    }
    connected.__aenter__.return_value = ws

    client = object.__new__(LighterClient)
    client.name = "test"
    client.http = Mock()
    client.http.session.ws_connect = Mock(side_effect=[failed, connected])
    client._get_auth_headers = Mock(return_value={"Authorization": "token"})
    sleep = AsyncMock()
    monkeypatch.setattr("clients.lighter.asyncio.sleep", sleep)

    assert await client._get_volume(42) == Decimal("123.45")
    assert client.http.session.ws_connect.call_count == 2
    sleep.assert_awaited_once_with(1.0)
    ws.send_json.assert_awaited_once_with(
        {"type": "subscribe", "channel": "account_all_trades/42", "auth": "token"}
    )


async def test_apply_referral_code_waits_between_accounts(monkeypatch):
    accounts: Any = [Mock(name="one"), Mock(name="two")]
    for name, account in zip(("one", "two"), accounts):
        account.name = name
        account.use_referral_code = AsyncMock()

    sleep = AsyncMock()
    monkeypatch.setattr(lighter_app.asyncio, "sleep", sleep)
    delay = TimeRange(min=DurationSec(30), max=DurationSec(30))

    await apply_referral_code(accounts, " code ", delay)

    for account in accounts:
        account.use_referral_code.assert_awaited_once_with("code")
    sleep.assert_awaited_once_with(30)


async def test_info_shows_referral_commands_for_missing_code(capsys):
    accounts: Any = [Mock(), Mock()]
    profiles = [
        ProfileInfo(
            addr="0x1111..1111",
            balance=Decimal(1),
            volume=Decimal(2),
            pnl=Decimal(0),
            points=Decimal(3),
            ref_code="existing",
        ),
        ProfileInfo(
            addr="0x2222..2222",
            balance=Decimal(1),
            volume=Decimal(2),
            pnl=Decimal(0),
            points=Decimal(3),
        ),
    ]
    for name, account, profile in zip(("ready", "missing"), accounts, profiles):
        account.name = name
        account.address = profile.addr
        account.registered = AsyncMock(return_value=True)
        account.auth_ready = AsyncMock(return_value=True)
        account.profile = AsyncMock(return_value=profile)

    await print_info(accounts)

    output = capsys.readouterr().out
    assert "Referral code missing: missing" in output
    assert "All enabled: uv run apps/lighter.py useref CODE" in output
    assert "One account: uv run apps/lighter.py useref CODE -a missing" in output
