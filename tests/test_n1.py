# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from apps import n1 as n1_app
from clients.n1 import N1Client, N1Turnkey
from lib.http import ApiError


@pytest.fixture
def client():
    value = object.__new__(N1Client)
    value.name = "test"
    value.http = Mock()
    value.http.request = AsyncMock()
    value._n1 = Mock()
    value._n1.request = AsyncMock()
    return value


async def test_fetch_points_uses_n1_api(client):
    data = {"data": [{"stage": "week_1", "points": 12.5}]}
    client._n1.request.return_value = Mock(ok=True, json=Mock(return_value=data))

    result = await client._fetch_points("solana-address")

    assert result == data
    client._n1.request.assert_awaited_once_with(
        "GET",
        "/v1/app/points",
        params={"address": "solana-address"},
    )


async def test_points_sums_legacy_stages(client):
    client._fetch_points = AsyncMock(
        return_value={
            "data": [
                {
                    "stage": "referral_rewards",
                    "points": 238.37277,
                    "rank": 274,
                    "isCurrent": False,
                },
                {
                    "stage": "week_7",
                    "points": 349.413501,
                    "rank": 1181,
                    "isCurrent": False,
                },
            ]
        }
    )

    assert await client._points("solana-address") == (Decimal("587.786271"), None)


async def test_points_history_uses_stage_weeks_and_skips_testing_phase(client):
    client._turnkey = Mock()
    client._turnkey.login = AsyncMock(return_value="solana-address")
    client._fetch_points = AsyncMock(
        return_value={
            "data": [
                {"stage": "referral_rewards", "points": 10, "startDate": None},
                {"stage": "testing_phase", "points": 0, "startDate": None},
                {
                    "stage": "week_1",
                    "points": 20,
                    "startDate": "2026-02-04T07:38:55.180Z",
                },
            ]
        }
    )

    result = await client.points_history()

    assert [point.points for point in result] == [Decimal(10), Decimal(20)]
    assert [point.stage for point in result] == ["referral_rewards", "week_1"]
    assert result[1].start_window == datetime(2026, 2, 3, tzinfo=UTC)


async def test_fetch_points_rejects_invalid_json(client):
    client._n1.request.return_value = Mock(
        ok=True,
        status_code=200,
        text="<html>Moved</html>",
        json=Mock(side_effect=ValueError("invalid JSON")),
    )

    with pytest.raises(ApiError, match=r"N1 app API returned invalid JSON: 200 \[html\]"):
        await client._fetch_points("solana-address")


async def test_fetch_points_rejects_error_response(client):
    response = Mock(ok=False, status_code=503, text="<html>Unavailable</html>")
    response.json = Mock(side_effect=AssertionError("response must not be parsed"))
    client._n1.request.return_value = response

    with pytest.raises(ApiError, match=r"N1 app API error: 503 \[html\]"):
        await client._fetch_points("solana-address")
    response.json.assert_not_called()


async def test_profile_stats_use_n1_analytics(client):
    client._n1_call = AsyncMock(
        side_effect=[
            {"data": {"lifetimeVolumeUsd": "98871.872609"}},
            {"summary": {"snapshot": {"totalPnl": -20.132039}}},
            {"items": [{"day": "2026-06-11", "pnl": 0.839439}]},
        ]
    )

    assert await client._total_volume(8989) == Decimal("98871.872609")
    assert await client._total_pnl(8989) == Decimal("-20.132039")
    assert await client.daily_pnl(8989) == [{"day": "2026-06-11", "pnl": 0.839439}]

    volume_call, pnl_call, daily_call = client._n1_call.await_args_list
    assert volume_call.args == ("/v1/app/analytics/accounts/lifetime-volume",)
    assert volume_call.kwargs["params"] == {"accountIds": "8989"}
    assert pnl_call.args == ("/v1/app/analytics/account-overview",)
    assert pnl_call.kwargs["params"]["include"] == "summary"
    assert daily_call.args == ("/v1/app/analytics/daily-pnl",)
    assert daily_call.kwargs["params"] == {"accountId": "8989", "limit": "730"}


async def test_paged_follows_cursor_even_when_page_is_short(client):
    client.http.request.side_effect = [
        Mock(
            ok=True,
            json=Mock(
                return_value={
                    "items": [{"actionId": 2, "time": "2026-01-02T00:00:00Z"}],
                    "nextStartInclusive": 123,
                }
            ),
        ),
        Mock(
            ok=True,
            json=Mock(
                return_value={
                    "items": [{"actionId": 1, "time": "2026-01-01T00:00:00Z"}],
                    "nextStartInclusive": None,
                }
            ),
        ),
    ]

    result = await client.paged("/trades?makerId=8989")

    assert [item["actionId"] for item in result] == [1, 2]
    first_call, second_call = client.http.request.await_args_list
    assert first_call.kwargs["params"] == {"pageSize": "255", "paginationMode": "actionId"}
    assert second_call.kwargs["params"] == {
        "pageSize": "255",
        "paginationMode": "actionId",
        "startInclusive": 123,
    }


async def test_fee_rates_use_current_tier_endpoints(client):
    client._ensure_session = AsyncMock(return_value=(1, 8989))
    client._call = AsyncMock(
        side_effect=[
            5,
            [[5, {"maker_fee_ppm": 95, "taker_fee_ppm": 332}]],
        ]
    )

    assert await client.get_fee_rates() == (Decimal("0.000332"), Decimal("0.000095"))
    assert [call.args for call in client._call.await_args_list] == [
        ("GET", "/account/8989/fee/tier"),
        ("GET", "/fee/brackets/info"),
    ]


async def test_stats_use_net_daily_pnl_without_subtracting_fees(monkeypatch):
    acc = Mock(name="test")
    acc.name = "test"
    monkeypatch.setattr(
        n1_app,
        "_fetch_stats",
        AsyncMock(
            return_value={
                "pnl": [{"day": "2026-06-11", "pnl": -5}],
                "trades": [
                    {
                        "time": "2026-06-11T12:00:00Z",
                        "price": 100,
                        "baseSize": 2,
                        "fee": Decimal(1),
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(n1_app, "sync_points", AsyncMock(return_value=[]))
    render = Mock()
    monkeypatch.setattr(n1_app, "render_stats", render)

    await n1_app.print_stats([acc], period="day")

    periods = render.call_args.args[0]
    row = periods["2026-06-11"][0]
    assert row.burn == Decimal(5)
    assert row.fees == Decimal(1)


async def test_turnkey_login_uses_n1_auth_api():
    turnkey = object.__new__(N1Turnkey)
    turnkey._evm_account = Mock(address="0xAbC")
    turnkey._ephem_pubkey_hex = "session-public-key"
    turnkey._sub_org_id = None
    turnkey._stamp_eip191 = Mock(return_value="stamp")
    turnkey._n1_api = Mock()
    turnkey._n1_api.request = AsyncMock(
        side_effect=[
            Mock(
                ok=True,
                json=Mock(
                    return_value={
                        "data": {
                            "challengeId": "challenge-id",
                            "nonce": "challenge-nonce",
                        }
                    }
                ),
            ),
            Mock(
                ok=True,
                json=Mock(
                    return_value={
                        "data": {
                            "session": {"sessionToken": "token"},
                            "identifyData": {"user": {"turnkeySuborgId": "sub-org"}},
                        }
                    }
                ),
            ),
        ]
    )
    turnkey._call = AsyncMock(
        return_value={"accounts": [{"curve": "CURVE_ED25519", "address": "solana-address"}]}
    )

    assert await turnkey.login() == "solana-address"

    challenge_call, login_call = turnkey._n1_api.request.await_args_list
    assert challenge_call.args[:2] == ("POST", "/v1/auth/wallet/challenge")
    assert login_call.args[:2] == ("POST", "/v1/auth/wallet/login")
    login_payload = login_call.kwargs["json"]
    assert login_payload["expectedAddress"] == "0xabc"
    assert (
        json.loads(login_payload["signedRequest"]["body"])["parameters"]["expirationSeconds"]
        == "604800"
    )
