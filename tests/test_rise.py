import pickle
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from clients.rise import RiseClient, RiseLeaderboardEntry, RisePointsInfo, RiseVolumeStats
from scripts import weekly


@pytest.fixture
def client():
    value = object.__new__(RiseClient)
    value.name = "test"
    value.address = "0x731a64a386a01B898963Ced8141c699B93b0bDE5"
    value._call = AsyncMock()
    value.http = Mock()
    value.http.request = AsyncMock()
    value._points_token = None
    value._points_token_expires_at = 0
    return value


async def test_points_login_signs_nonce_and_returns_access_token(client, monkeypatch):
    monkeypatch.setattr("clients.rise.time.time", lambda: 1000)
    client._call.return_value = {"nonce": "0abc"}
    client._sign_hex = AsyncMock(return_value="0xsig")
    client.http.request.return_value = Mock(
        ok=True,
        json=Mock(return_value={"data": {"access_token": "jwt", "expires_in": 600}}),
    )

    result = await client._points_login()

    assert result == "jwt"
    assert client._points_token_expires_at == 1600
    client._call.assert_awaited_once_with(
        "GET", "/api/v1/auth/nonce", params={"account": client.address}
    )
    sign_call = client._sign_hex.await_args
    assert sign_call.args[0] == "Login"
    assert sign_call.args[2] == {
        "account": client.address,
        "nonce": int("0abc", 16),
        "deadline": 1300,
    }
    client.http.request.assert_awaited_once_with(
        "POST",
        "https://www.rise.trade/api/risex-auth/login",
        json={
            "account": client.address,
            "nonce": "0abc",
            "deadline": 1300,
            "signature": "0xsig",
            "stayConnected": False,
        },
    )


async def test_points_call_reauthenticates_after_401(client):
    client._points_login = AsyncMock(side_effect=["old", "new"])
    client.http.request.side_effect = [
        Mock(ok=False, status_code=401),
        Mock(
            ok=True,
            status_code=200,
            json=Mock(return_value={"data": {"total_points": "209.9"}}),
        ),
    ]

    result = await client._points_call(f"/api/v1/points/{client.address}")

    assert result == {"total_points": "209.9"}
    assert client._points_login.await_args_list[0].kwargs == {"force": False}
    assert client._points_login.await_args_list[1].kwargs == {"force": True}
    assert client.http.request.await_args_list[0].kwargs["headers"] == {
        "Authorization": "Bearer old"
    }
    assert client.http.request.await_args_list[1].kwargs["headers"] == {
        "Authorization": "Bearer new"
    }


async def test_points_total_uses_wallet_endpoint(client):
    client._points_call = AsyncMock(
        return_value={
            "wallet_address": client.address.lower(),
            "total_points": "209.9",
            "rank": "1908",
        }
    )

    result = await client.points_total()

    assert result == RisePointsInfo(total_points=Decimal("209.9"), rank=1908)
    client._points_call.assert_awaited_once_with(f"/api/v1/points/{client.address}")


async def test_points_history_maps_retro_and_weekly_epochs(client):
    client._points_call = AsyncMock(
        return_value={
            "entries": [
                {
                    "epoch": {"epoch_id": "Retro Drop", "starts_at": "0"},
                    "ledger": {"total_points": "204"},
                    "rank": "1176",
                },
                {
                    "epoch": {"epoch_id": "Week 1", "starts_at": "1784505600"},
                    "ledger": {"total_points": "5.9"},
                    "rank": "2326",
                },
            ]
        }
    )

    result = await client.points_history()

    assert [point.epoch_id for point in result] == ["Retro Drop", "Week 1"]
    assert [point.points for point in result] == [Decimal("204"), Decimal("5.9")]
    assert result[0].start_window == datetime(2026, 7, 13, tzinfo=UTC)
    assert result[1].start_window == datetime(2026, 7, 20, tzinfo=UTC)
    assert [point.rank for point in result] == [1176, 2326]


def test_points_week_labels_match_rise_epochs():
    assert RiseClient.to_week_label(datetime(2026, 6, 1, tzinfo=UTC)) == "OFF Season 0"
    assert RiseClient.to_week_label(datetime(2026, 7, 19, tzinfo=UTC)) == "OFF Season 0"
    assert RiseClient.to_week_label(datetime(2026, 7, 20, tzinfo=UTC)) == "W01 Jul20-Jul26"
    assert RiseClient.to_week_label(datetime(2026, 8, 31, tzinfo=UTC)) == "W07 Aug31-Sep06"


def test_weekly_report_reads_cached_rise_points(tmp_path, monkeypatch):
    cache = {
        "records": {
            "Retro Drop": {
                "epoch_id": "Retro Drop",
                "start_window": datetime(2026, 7, 13, tzinfo=UTC),
                "points": Decimal("204"),
                "rank": 1176,
            },
            "Week 1": {
                "epoch_id": "Week 1",
                "start_window": datetime(2026, 7, 20, tzinfo=UTC),
                "points": Decimal("5.9"),
                "rank": 2326,
            },
        }
    }
    with (tmp_path / "rise_wallet_points.pkl").open("wb") as file:
        pickle.dump(cache, file)
    monkeypatch.setattr(weekly, "CACHE", str(tmp_path))

    assert weekly.rise_pts() == {
        "OFF Season 0": Decimal("204"),
        "W01 Jul20-Jul26": Decimal("5.9"),
    }


async def test_profile_uses_points_total_and_points_rank(client):
    client.balance = AsyncMock(return_value=Decimal("73.14"))
    client.volume_stats = AsyncMock(return_value=RiseVolumeStats(total_volume="394686"))
    client._realized_pnl = AsyncMock(return_value=Decimal("-176.86"))
    client._leaderboard_entry = AsyncMock(return_value=RiseLeaderboardEntry(rank=5667))
    client.points_total = AsyncMock(return_value=RisePointsInfo(total_points="209.9", rank=1908))

    result = await client.profile()

    assert result.points == Decimal("209.9")
    assert result.rank == 1908
