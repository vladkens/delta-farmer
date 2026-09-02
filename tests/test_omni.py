# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | No AI was harmed making this
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from clients.omni import CloudflareClearanceUpdated, OmniClient
from lib.http import ApiError, AsyncHttp


@pytest.fixture
def client():
    value = object.__new__(OmniClient)
    value.name = "test"
    value.address = "0x123"
    value.account = Mock()
    value.account.sign_message.return_value.signature = b"sig"
    value.http = Mock()
    value.http.session.cookies = {}
    value._prepare_login = AsyncMock()
    value._request = AsyncMock()
    return value


async def test_login_reuses_existing_session(client):
    client.http.session.cookies["vr-token"] = "saved"
    client._request.return_value = Mock(ok=True, status_code=200)

    await client.login()

    client._prepare_login.assert_not_awaited()
    client._request.assert_awaited_once_with("GET", "/portfolio?compute_margin=true")
    client.http.clear_cookies.assert_not_called()


async def test_login_reuses_persisted_session(client, tmp_path):
    cookies_file = str(tmp_path / "cookies.pkl")
    saved_http = AsyncHttp(baseurl="https://example.com", headers={}, cookies_file=cookies_file)
    saved_http.session.cookies.set("vr-token", "saved", domain="example.com")
    await saved_http.close()

    client.http = AsyncHttp(baseurl="https://example.com", headers={}, cookies_file=cookies_file)
    client.http.clear_cookies = Mock(wraps=client.http.clear_cookies)
    client._request.return_value = Mock(ok=True, status_code=200)

    try:
        assert not client.http._cookies_loaded
        await client.login()

        assert client.http.session.cookies.get("vr-token") == "saved"
        client.http.clear_cookies.assert_not_called()
        client._prepare_login.assert_not_awaited()
        client._request.assert_awaited_once_with("GET", "/portfolio?compute_margin=true")
    finally:
        await client.http.close()


@pytest.mark.parametrize("force", [False, True])
async def test_login_authenticates_missing_or_forced_session(client, force):
    client.http.session.cookies["vr-token"] = "old"
    client.http.session.cookies["cf_clearance"] = "clearance"
    client.http.clear_cookies.side_effect = client.http.session.cookies.clear

    async def request(_method, path, **_kwargs):
        if path == "/auth/generate_signing_data":
            return Mock(text="omni.variational.io wants you to sign in")
        client.http.session.cookies["vr-token"] = "new"
        return Mock(ok=True)

    client._request.side_effect = request
    if not force:
        client.http.session.cookies.pop("vr-token")

    await client.login(force=force)

    assert client.http.session.cookies["vr-token"] == "new"
    assert client.http.clear_cookies.call_count == int(force)
    assert ("cf_clearance" in client.http.session.cookies) is not force


async def test_login_reauthenticates_rejected_session(client):
    client.http.session.cookies["vr-token"] = "old"
    client.http.session.cookies["cf_clearance"] = "clearance"
    client.http.clear_cookies.side_effect = client.http.session.cookies.clear

    async def request(_method, path, **_kwargs):
        if path == "/portfolio?compute_margin=true":
            return Mock(ok=False, status_code=401)
        if path == "/auth/generate_signing_data":
            return Mock(text="omni.variational.io wants you to sign in")
        client.http.session.cookies["vr-token"] = "new"
        return Mock(ok=True)

    client._request.side_effect = request

    await client.login()

    assert client.http.session.cookies["vr-token"] == "new"
    assert client.http.session.cookies["cf_clearance"] == "clearance"
    client.http.clear_cookies.assert_not_called()
    client._prepare_login.assert_awaited_once_with()


async def test_login_does_not_clear_session_on_server_error(client):
    client.http.session.cookies["vr-token"] = "saved"
    client._request.return_value = Mock(ok=False, status_code=500, text="Unavailable")

    with pytest.raises(ApiError, match="Auth check failed"):
        await client.login()

    assert client.http.session.cookies["vr-token"] == "saved"
    client.http.clear_cookies.assert_not_called()
    client._prepare_login.assert_not_awaited()


async def test_login_preparation_does_not_retry_solver_error(client):
    client._request.side_effect = RuntimeError("ACTIVE_TASKS_LIMIT_EXCEEDED")

    with pytest.raises(RuntimeError, match="ACTIVE_TASKS_LIMIT_EXCEEDED"):
        await OmniClient._prepare_login(client)

    client._request.assert_awaited_once()


async def test_login_restarts_with_fresh_signing_data_after_clearance(client):
    paths = []
    signing_data = iter(
        [
            "omni.variational.io wants you to sign in: first",
            "omni.variational.io wants you to sign in: second",
        ]
    )

    async def request(_method, path, **kwargs):
        paths.append(path)
        if path == "/auth/generate_signing_data":
            return Mock(text=next(signing_data))
        if paths.count("/auth/login") == 1:
            raise CloudflareClearanceUpdated
        assert kwargs["replay_after_cf"] is False
        client.http.session.cookies["vr-token"] = "new"
        return Mock(ok=True)

    client._request.side_effect = request

    await client.login()

    assert paths == [
        "/auth/generate_signing_data",
        "/auth/login",
        "/auth/generate_signing_data",
        "/auth/login",
    ]
    assert client.account.sign_message.call_count == 2


async def test_call_relogs_only_once(client):
    client.http.session.cookies["vr-token"] = "old"
    unauthorized = Mock(ok=False, status_code=401, text="Unauthorized")
    client._request.side_effect = [unauthorized, unauthorized]

    async def relogin():
        assert "vr-token" not in client.http.session.cookies
        client.http.session.cookies["vr-token"] = "new"

    client.login = AsyncMock(side_effect=relogin)

    with pytest.raises(ApiError, match="API error: 401 Unauthorized"):
        await client._call("GET", "/portfolio")

    client.login.assert_awaited_once_with()
    assert client._request.await_count == 2


async def test_concurrent_unauthorized_calls_share_one_authentication(client):
    client.http.session.cookies["vr-token"] = "old"
    unauthorized = Mock(ok=False, status_code=401, text="Unauthorized")
    success = Mock(ok=True, status_code=200)
    success.json.return_value = {"balance": "1"}
    both_requests_started = asyncio.Event()
    portfolio_requests = 0
    login_requests = 0

    async def request(_method, path, **_kwargs):
        nonlocal portfolio_requests, login_requests
        if path == "/portfolio":
            portfolio_requests += 1
            if portfolio_requests <= 2:
                if portfolio_requests == 2:
                    both_requests_started.set()
                await both_requests_started.wait()
                return unauthorized
            return success
        if path == "/auth/generate_signing_data":
            login_requests += 1
            return Mock(text="omni.variational.io wants you to sign in")
        if path == "/auth/login":
            login_requests += 1
            client.http.session.cookies["vr-token"] = "new"
            return Mock(ok=True)
        raise AssertionError(path)

    client._request.side_effect = request
    client._check_auth = AsyncMock(side_effect=lambda: "vr-token" in client.http.session.cookies)
    client.http.clear_cookies.side_effect = client.http.session.cookies.clear

    results = await asyncio.gather(
        client._call("GET", "/portfolio"),
        client._call("GET", "/portfolio"),
    )

    assert results == [{"balance": "1"}, {"balance": "1"}]
    assert login_requests == 2
    assert portfolio_requests == 4
    assert client._check_auth.await_count == 2


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"own_volume": None, "trade_volume": None, "referred_by": None},
    ],
)
async def test_total_volume_handles_missing_referral_summary(client, response):
    client._call = AsyncMock(return_value=response)

    assert await client.total_volume() == (Decimal(0), None)
    client._call.assert_awaited_once_with("GET", "/referrals/summary")


async def test_total_volume_parses_referral_summary(client):
    client._call = AsyncMock(
        return_value={"own_volume": {"total": "123.45"}, "referred_by": {"code": "REF"}}
    )

    assert await client.total_volume() == (Decimal("123.45"), "REF")
