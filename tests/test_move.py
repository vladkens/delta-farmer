import argparse
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, call

from eth_account import Account

from lib import move_cmd
from lib.evm import BSC, ROBINHOOD
from lib.models import AccountConfig, DurationSec, TimeRange


async def test_move_waits_between_accounts(monkeypatch):
    states = []
    for index, name in enumerate(("first", "second"), start=1):
        key = f"{index:064x}"
        config = AccountConfig(name=name, privkey=key)
        balances = {
            BSC.asset_code(BSC.stables["USDC"]): Decimal(10),
            BSC.asset_code(BSC.stables["USDT"]): Decimal(20),
            ROBINHOOD.asset_code(ROBINHOOD.native_token): Decimal("0.001"),
        }
        states.append(move_cmd.WalletState(config, Account.from_key(key), balances, {}))

    relay_move = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr(move_cmd, "_scan", AsyncMock(return_value=states))
    monkeypatch.setattr(move_cmd, "confirm", Mock(return_value=True))
    monkeypatch.setattr(move_cmd, "relay_move", relay_move)
    monkeypatch.setattr(move_cmd.asyncio, "sleep", sleep)

    args = argparse.Namespace(account=None, source="bsc", target="rh:usdg", amount=None)
    delay = TimeRange(min=DurationSec("2m"), max=DurationSec("2m"))
    cfg = Mock(accounts=[state.config for state in states], balance_transfer_delay=delay)
    await move_cmd.run_move(args, cfg)

    assert [item.kwargs["topup_gas"] for item in relay_move.await_args_list] == [
        True,
        False,
        True,
        False,
    ]
    assert sleep.await_args_list == [call(move_cmd.EVM_START_DELAY_SEC), call(120)]
