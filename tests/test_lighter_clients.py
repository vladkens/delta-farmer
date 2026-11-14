from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from curl_cffi.requests import Response

from clients.lighter import LighterApiError, LighterCoreClient, LighterRobinhoodClient
from lib.errors import AppError
from lib.http import ApiError
from lib.lighter_crypto import LIGHTER_CHAIN_ID, ROBINHOOD_SIGNING_CHAIN_ID
from lib.models import AccountConfig
from strategy import TradingClient

# Synthetic key, never used for an on-chain account or network authentication.
PRIVATE_KEY = "01" * 32


@pytest.fixture
async def clients():
    config = AccountConfig.model_validate({"name": "test", "privkey": PRIVATE_KEY})
    core = LighterCoreClient.from_config(config)
    robinhood = LighterRobinhoodClient.from_config(config)
    for client in (core, robinhood):
        client.http.request = AsyncMock(side_effect=AssertionError("Unexpected network request"))
    try:
        yield core, robinhood
    finally:
        await core.close()
        await robinhood.close()


async def test_deployments_keep_distinct_routes_and_signing_domains(clients):
    core, robinhood = clients
    assert core.address == robinhood.address
    assert core.exchange == "lighter_core"
    assert robinhood.exchange == "lighter"
    assert isinstance(core, TradingClient)
    assert isinstance(robinhood, TradingClient)

    assert core.http.baseurl == "https://mainnet.zklighter.elliot.ai"
    assert core.http.session.headers["Origin"] == "https://app.lighter.xyz"
    assert core.http.session.headers["Referer"] == "https://app.lighter.xyz/"
    assert core.signer.chain_id == LIGHTER_CHAIN_ID == 304

    assert robinhood.http.baseurl == "https://api.rh.lighter.xyz"
    assert robinhood.ws_url == "wss://api.rh.lighter.xyz/stream"
    assert robinhood.http.session.headers["Origin"] == "https://robinhoodchain.lighter.xyz"
    assert robinhood.http.session.headers["Referer"] == "https://robinhoodchain.lighter.xyz/"
    assert robinhood.signer.chain_id == ROBINHOOD_SIGNING_CHAIN_ID == 466324


async def test_transactions_are_bound_to_the_client_deployment(clients, monkeypatch):
    monkeypatch.setattr("lib.lighter_crypto.signer.secrets.randbelow", lambda _: 1)
    core, robinhood = clients
    core_tx = core.signer.sign_cancel_all_orders(42, 3, 100, 1700000600000)
    robinhood_tx = robinhood.signer.sign_cancel_all_orders(42, 3, 100, 1700000600000)

    assert core_tx.tx_hash != robinhood_tx.tx_hash
    assert core_tx.info["Sig"] != robinhood_tx.info["Sig"]
    assert {k: v for k, v in core_tx.info.items() if k != "Sig"} == {
        k: v for k, v in robinhood_tx.info.items() if k != "Sig"
    }


async def test_core_does_not_expose_robinhood_money_movement(clients):
    core, robinhood = clients
    for method in ("deposit", "deposit_asset", "withdraw", "withdrawal_info"):
        assert not hasattr(core, method)
        assert callable(getattr(robinhood, method))


async def test_core_does_not_query_robinhood_points(clients):
    core, _ = clients
    assert await core.points_total() is None
    assert await core._get_points(42) is None
    core.http.request.assert_not_called()


# Daily P/L buckets: trade_pnl is cumulative since the account's first trade.
PNL_BUCKETS = [{"trade_pnl": -900, "volume": 0}, {"trade_pnl": -950, "volume": "1234.5"}]


async def test_both_deployments_count_pnl_from_the_campaign_start(clients):
    # A Core account may carry manual trading from before the farm; it must not reach the burn.
    start = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp())
    for client in clients:
        client._get_auth_headers = lambda _index: {"Authorization": "token"}
        client._call = AsyncMock(return_value={"pnl": PNL_BUCKETS})

        assert client._get_pnl(await client._get_pnl_history(42)) == -50
        assert client._call.await_args.kwargs["params"]["start_timestamp"] == start

    assert clients[1].to_week_label(datetime(2026, 9, 1, tzinfo=UTC)) == "W01 Sep01-Sep07"


async def test_core_volume_is_summed_from_the_same_pnl_buckets(clients, monkeypatch):
    # The websocket volume feed totals the account's whole life, so Core never opens it.
    core, _ = clients
    core.account_info = AsyncMock(return_value={"account_index": 42, "collateral": "20"})
    core._get_auth_headers = lambda _index: {"Authorization": "token"}
    core._call = AsyncMock(return_value={"pnl": PNL_BUCKETS})
    core._get_used_referral_code = AsyncMock(return_value=None)
    ws_connect = Mock()
    monkeypatch.setattr(core.http.session, "ws_connect", ws_connect)

    profile = await core.profile()

    assert (profile.volume, profile.pnl) == (Decimal("1234.5"), -50)
    core._call.assert_awaited_once()
    ws_connect.assert_not_called()


async def test_core_login_never_approves_an_integrator(clients):
    core, _ = clients
    assert core.integrator_approval is False
    # Driven through login() so a second systemConfig or sendTx caller anywhere in the flow fails.
    core.account_info = AsyncMock(return_value={"account_index": 42})
    core._get_api_key = AsyncMock(return_value=core.signer.public_key)
    core._system_config = AsyncMock(side_effect=AssertionError("Core read systemConfig"))
    core._send_tx = AsyncMock(side_effect=AssertionError("Core signed a tx on login"))

    assert await core.login() == "slot 4, key already active, integrator skipped"
    core._system_config.assert_not_awaited()
    core._send_tx.assert_not_awaited()
    core.http.request.assert_not_called()


async def test_robinhood_still_checks_the_integrator_approval(clients):
    _, robinhood = clients
    assert robinhood.integrator_approval is True
    robinhood._system_config = AsyncMock(return_value={"fee_collector_account_index": "0"})
    robinhood._send_integrator_approval = AsyncMock(
        side_effect=AssertionError("Approval signed for a disabled collector")
    )

    assert await robinhood._ensure_integrator_approval({}) == "integrator disabled"
    robinhood._system_config.assert_awaited_once()


async def test_core_refuses_an_unverified_referral_call(clients):
    core, _ = clients
    with pytest.raises(AppError, match="not verified on Lighter Core"):
        await core.use_referral_code("TESTCODE")

    core.http.request.assert_not_called()


async def test_a_failing_referral_lookup_degrades_only_on_core(clients):
    core, robinhood = clients
    missing = ApiError("Lighter API GET /api/v1/referral/userReferrals error")
    core._fetch_used_referral_code = AsyncMock(side_effect=missing)
    robinhood._fetch_used_referral_code = AsyncMock(side_effect=missing)

    assert await core._get_used_referral_code(42) is None
    with pytest.raises(ApiError):
        await robinhood._get_used_referral_code(42)

    # A working endpoint is still reported on Core; only the error is swallowed.
    core._fetch_used_referral_code = AsyncMock(return_value="TESTCODE")
    assert await core._get_used_referral_code(42) == "TESTCODE"


async def test_deployments_render_their_own_label(clients):
    core, robinhood = clients
    # exchange ids key caches and configs; labels are what an operator reads.
    assert core.label == "Lighter Core"
    assert robinhood.label == "Lighter"


@pytest.mark.parametrize(
    ("index", "hint"), [(0, "fund it on Lighter Core by hand"), (1, "deposit first")]
)
async def test_login_names_the_funding_route_of_its_deployment(clients, index, hint):
    # `deposit` reaches Robinhood Chain only, so a Core account cannot be told to run it.
    client = clients[index]
    missing = LighterApiError(cast(Response, None), {"code": 21100, "message": "account not found"})
    client.account_info = AsyncMock(side_effect=missing)

    with pytest.raises(AppError, match=f"^Account is not registered; {hint}$"):
        await client.login()
