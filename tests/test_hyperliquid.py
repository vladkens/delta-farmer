"""Tests for HyperLiquidClient position filtering by DEX scope."""

from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from clients.hyena import HyenaClient
from clients.hyperliquid import HyperLiquidClient
from clients.onyx import OnyxClient
from lib.http import ApiError
from strategy.models import Position, Side

_FAKE_KEY = "a" * 64


class FakeHyperLiquidClient(HyperLiquidClient):
    def __init__(self, responses: dict[str, object]):
        super().__init__(name="test", privkey=_FAKE_KEY)
        self.responses = responses
        self.calls: list[str] = []

    async def _info(self, **kwargs):
        request_type = kwargs["type"]
        self.calls.append(request_type)
        return self.responses[request_type]


def _pos(symbol: str, side: Side = "bid") -> Position:
    return Position(
        id=symbol,
        symbol=symbol,
        side=side,
        size=Decimal("0.01"),
        entry_price=Decimal("80000"),
        unrealized_pnl=Decimal("0"),
    )


# MARK: Balance


@pytest.mark.asyncio
async def test_hyperliquid_balance_uses_clearinghouse_state_for_standard_accounts():
    c = FakeHyperLiquidClient(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": {"marginSummary": {"accountValue": "123.45"}},
        }
    )

    assert await c.balance() == Decimal("123.45")
    assert c.calls == ["userAbstraction", "clearinghouseState"]


@pytest.mark.asyncio
async def test_hyperliquid_balance_uses_spot_state_for_unified_accounts():
    c = FakeHyperLiquidClient(
        {
            "userAbstraction": "unifiedAccount",
            "spotClearinghouseState": {
                "balances": [
                    {"coin": "HYPE", "total": "2"},
                    {"coin": "USDC", "total": "456.78", "hold": "12.34"},
                ]
            },
        }
    )

    assert await c.balance() == Decimal("456.78")
    assert c.calls == ["userAbstraction", "spotClearinghouseState"]


# MARK: HyenaClient


def test_hyena_keeps_only_hyna_positions():
    """Hyena must only manage hyna: positions, ignoring native HL and other DEX positions."""
    c = HyenaClient(name="test", privkey=_FAKE_KEY)
    result = c._filter_positions([_pos("hyna:BTC"), _pos("BTC", "ask"), _pos("xyz:TSLA")])
    assert [p.symbol for p in result] == ["hyna:BTC"]


def test_hyena_empty_when_no_hyna_positions():
    """Hyena returns empty list when no hyna: positions are open."""
    c = HyenaClient(name="test", privkey=_FAKE_KEY)
    assert c._filter_positions([_pos("BTC"), _pos("xyz:TSLA")]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_cls", "http_attrs"),
    [(HyenaClient, ("_app_http",)), (OnyxClient, ("_privy_http", "_arjuna_http"))],
)
async def test_force_login_clears_auth_state(client_cls, http_attrs):
    client = client_cls(name="test", privkey=_FAKE_KEY)
    client._jwt = "old"
    for attr in http_attrs:
        getattr(client, attr).clear_cookies = Mock()

    async def authenticate():
        assert client._jwt is None
        client._jwt = "new"

    client._login = AsyncMock(side_effect=authenticate)

    await client.login(force=True)

    for attr in http_attrs:
        getattr(client, attr).clear_cookies.assert_called_once_with()
    client._login.assert_awaited_once_with()
    assert client._jwt == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_cls", "http_attrs", "auth_http_attr"),
    [
        (HyenaClient, ("_app_http",), "_app_http"),
        (OnyxClient, ("_privy_http", "_arjuna_http"), "_arjuna_http"),
    ],
)
@pytest.mark.parametrize("status_code", [None, 200, 401, 500])
async def test_login_checks_existing_auth(client_cls, http_attrs, auth_http_attr, status_code):
    client = client_cls(name="test", privkey=_FAKE_KEY)
    client._jwt = None if status_code is None else "old"
    for attr in http_attrs:
        getattr(client, attr).clear_cookies = Mock()

    auth_http = getattr(client, auth_http_attr)
    auth_http.request = AsyncMock(return_value=Mock(ok=status_code == 200, status_code=status_code))

    async def authenticate():
        client._jwt = "new"

    client._login = AsyncMock(side_effect=authenticate)

    if status_code == 500:
        with pytest.raises(ApiError, match="Auth check failed"):
            await client.login()
    else:
        await client.login()

    if status_code in {None, 401}:
        for attr in http_attrs:
            getattr(client, attr).clear_cookies.assert_called_once_with()
        client._login.assert_awaited_once_with()
        assert client._jwt == "new"
    else:
        for attr in http_attrs:
            getattr(client, attr).clear_cookies.assert_not_called()
        client._login.assert_not_awaited()
        assert client._jwt == "old"

    if status_code is None:
        auth_http.request.assert_not_awaited()
    else:
        auth_http.request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_cls", "http_attr", "call_name"),
    [
        (HyenaClient, "_app_http", "_authed_get"),
        (HyenaClient, "_app_http", "_authed_post"),
        (OnyxClient, "_arjuna_http", "_authed_get"),
    ],
)
async def test_stale_401_does_not_clear_refreshed_auth(client_cls, http_attr, call_name):
    client = client_cls(name="test", privkey=_FAKE_KEY)
    client._jwt = "old"
    http = getattr(client, http_attr)
    calls = 0

    async def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            client._jwt = "new"
            return Mock(ok=False, status_code=401)
        return Mock(ok=True, status_code=200, json=Mock(return_value={"ok": True}))

    http.request = AsyncMock(side_effect=request)
    client.login = AsyncMock()

    assert await getattr(client, call_name)("/test") == {"ok": True}

    client.login.assert_awaited_once_with()
    assert http.request.await_args_list[1].kwargs["headers"] == {"Authorization": "Bearer new"}


# MARK: OnyxClient


def test_onyx_ignores_hyna_positions_by_default():
    """Onyx configured for native BTC must not see hyna:BTC, but still sees xyz: positions."""
    c = OnyxClient(name="test", privkey=_FAKE_KEY)
    c._symbols = ["BTC"]
    result = c._filter_positions([_pos("hyna:BTC"), _pos("BTC", "ask"), _pos("xyz:TSLA")])
    assert [p.symbol for p in result] == ["BTC", "xyz:TSLA"]


def test_onyx_includes_explicit_hyna_symbol():
    """Onyx configured for hyna:BTC explicitly must include that position."""
    c = OnyxClient(name="test", privkey=_FAKE_KEY)
    c._symbols = ["hyna:BTC"]
    result = c._filter_positions([_pos("hyna:BTC"), _pos("BTC", "ask"), _pos("xyz:TSLA")])
    assert [p.symbol for p in result] == ["hyna:BTC", "BTC", "xyz:TSLA"]


def test_onyx_explicit_hyna_does_not_unlock_other_hyna_coins():
    """Onyx with hyna:ETH in config must still block hyna:BTC."""
    c = OnyxClient(name="test", privkey=_FAKE_KEY)
    c._symbols = ["BTC", "xyz:TSLA", "hyna:ETH"]
    result = c._filter_positions(
        [_pos("hyna:BTC"), _pos("hyna:ETH"), _pos("BTC"), _pos("xyz:TSLA")]
    )
    assert [p.symbol for p in result] == ["hyna:ETH", "BTC", "xyz:TSLA"]


def test_onyx_no_symbols_blocks_all_hyna():
    """Onyx with empty _symbols still blocks all hyna: positions."""
    c = OnyxClient(name="test", privkey=_FAKE_KEY)
    c._symbols = []
    result = c._filter_positions([_pos("hyna:BTC"), _pos("BTC"), _pos("xyz:TSLA")])
    assert [p.symbol for p in result] == ["BTC", "xyz:TSLA"]
