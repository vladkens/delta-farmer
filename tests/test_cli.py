import argparse
import signal
from unittest.mock import AsyncMock, Mock, call

import pytest

from lib.cli import CliParser, _handle_login, create_cli, setup_app
from lib.errors import AppError
from lib.models import AccountConfig
from lib.utils import confirm


class Client:
    def __init__(self, name: str):
        self.name = name
        self.address = f"wallet-{name}"

    async def login(self, *, force: bool = False) -> None:
        return None


def test_confirm_sigint(monkeypatch):
    previous = Mock()
    set_signal = Mock(return_value=previous)
    monkeypatch.setattr("lib.utils.signal.signal", set_signal)
    monkeypatch.setattr("lib.utils.sys.stdin", Mock(isatty=Mock(return_value=False)))
    monkeypatch.setattr("builtins.input", Mock(side_effect=KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        confirm("Continue?")

    assert set_signal.call_args_list == [
        call(signal.SIGINT, signal.default_int_handler),
        call(signal.SIGINT, previous),
    ]


def test_unknown_command_help(capsys):
    parser = CliParser(prog="exchange")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("trade", help="Run trading manager")
    commands.add_parser("info", help="Show accounts info")

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["withdraw"])

    output = capsys.readouterr().err
    assert exc.value.code == 2
    assert "Run trading manager" in output
    assert "Show accounts info" in output
    assert "exchange: error: unknown command: 'withdraw'" in output


async def test_login_error_stops_retry(monkeypatch):
    client = Mock(name="missing")
    client.name = "missing"
    client.login = AsyncMock(side_effect=AppError("Account is not registered; deposit first"))
    sleep = AsyncMock()
    monkeypatch.setattr("lib.cli.asyncio.sleep", sleep)

    await _handle_login([client], force=False)

    client.login.assert_awaited_once_with(force=False)
    sleep.assert_not_awaited()


async def test_enabled_accounts(monkeypatch):
    accounts = [
        AccountConfig(name="on", privkey="x", enabled=True),
        AccountConfig(name="off", privkey="x", enabled=False),
    ]
    check = AsyncMock(
        return_value={"state": "none", "account_count": 1, "license": None, "offline": False}
    )
    eprint = Mock()
    monkeypatch.setattr("lib.cli.support.check", check)
    monkeypatch.setattr("lib.cli.eprint", eprint)
    monkeypatch.setattr("lib.cli.latest_release_notice", AsyncMock(return_value=None))
    monkeypatch.setenv("DF_NO_BANNER", "1")

    cfg = Mock(accounts=accounts)
    all_clients, active = await setup_app(
        argparse.Namespace(command="info"), cfg, lambda account: Client(account.name)
    )

    assert [client.name for client in all_clients] == ["on", "off"]
    assert [client.name for client in active] == ["on"]
    check.assert_awaited_once_with(["wallet-on"])
    assert any("delta-farmer" in call.args[0] for call in eprint.call_args_list)
    assert not any("supporter" in call.args[0] for call in eprint.call_args_list)


async def test_supporter_can_hide_banner_with_env(monkeypatch):
    license = {
        "plan": "small",
        "plan_name": "Solo",
        "suggested_accounts": 5,
        "paid_until": 2_000_000_000,
    }
    check = AsyncMock(
        return_value={
            "state": "active",
            "account_count": 1,
            "license": license,
            "offline": False,
        }
    )
    eprint = Mock()
    monkeypatch.setattr("lib.cli.support.check", check)
    monkeypatch.setattr("lib.cli.eprint", eprint)
    monkeypatch.setattr("lib.cli.latest_release_notice", AsyncMock(return_value=None))
    monkeypatch.setenv("DF_NO_BANNER", "1")

    await setup_app(
        argparse.Namespace(command="info"),
        Mock(accounts=[AccountConfig(name="on", privkey="x")]),
        lambda account: Client(account.name),
    )

    check.assert_awaited_once_with(["wallet-on"])
    eprint.assert_not_called()


async def test_setup_app_handles_move(monkeypatch):
    args = argparse.Namespace(command="move")
    cfg = Mock(accounts=[AccountConfig(name="on", privkey="x")])
    run_move = AsyncMock()
    monkeypatch.setattr("lib.cli.support.check", AsyncMock(return_value={}))
    monkeypatch.setattr("lib.cli._show_banner", AsyncMock())
    monkeypatch.setattr("lib.cli.run_move", run_move)

    with pytest.raises(SystemExit) as exc:
        await setup_app(args, cfg, lambda account: Client(account.name))

    assert exc.value.code == 0
    run_move.assert_awaited_once_with(args, cfg)


async def test_non_evm_cli_omits_move(monkeypatch, capsys):
    monkeypatch.setattr("lib.cli.sys.argv", ["pacifica", "move"])

    with pytest.raises(SystemExit) as exc:
        await create_cli("pacifica", "config.toml", ["privkey"], evm=False)

    assert exc.value.code == 2
    assert "unknown command: 'move'" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "pool"),
    [
        (["exchange", "trade"], None),
        (["exchange", "trade", "stock"], "stock"),
    ],
)
async def test_trade_accepts_optional_pool(monkeypatch, argv, pool):
    monkeypatch.setattr("lib.cli.sys.argv", argv)
    monkeypatch.setattr("lib.cli._load_tg_config", Mock(return_value=Mock()))
    monkeypatch.setattr("lib.cli.tg.init", Mock())
    monkeypatch.setattr("lib.cli.telemetry.init", Mock())
    monkeypatch.delenv("DF_LOG_FILE", raising=False)

    args = await create_cli("exchange", "config.toml", ["privkey"])

    assert args.pool == pool


async def test_license_prompts_when_missing_then_shows_status(monkeypatch):
    license = {
        "plan": "small",
        "plan_name": "Solo",
        "suggested_accounts": 5,
        "paid_until": 2_000_000_000,
    }
    missing = {"state": "none", "account_count": 6, "license": None, "offline": False}
    active = {"state": "active", "account_count": 6, "license": license, "offline": False}
    activate = AsyncMock(return_value=license)
    get_status = AsyncMock(side_effect=[missing, active])
    prompt = Mock(return_value="secret-key")
    console_print = Mock()
    monkeypatch.setattr("lib.cli.sys.argv", ["exchange", "license"])
    monkeypatch.setattr("lib.cli.getpass.getpass", prompt)
    monkeypatch.setattr("lib.cli.support.activate", activate)
    monkeypatch.setattr("lib.cli.support.count_accounts", Mock(return_value=6))
    monkeypatch.setattr("lib.cli.support.get_status", get_status)
    monkeypatch.setattr("lib.cli.support.check", AsyncMock())
    monkeypatch.setattr("lib.cli._show_banner", AsyncMock())
    monkeypatch.setattr("lib.cli.console.print", console_print)
    monkeypatch.setattr("lib.cli.telemetry.init", Mock())
    monkeypatch.setattr("lib.cli.telemetry.flush", AsyncMock())

    with pytest.raises(SystemExit) as exc:
        await create_cli("exchange", "config.toml", ["privkey"])

    assert exc.value.code == 0
    prompt.assert_called_once_with("Supporter key (leave blank to cancel): ")
    assert activate.await_args.args[0] == "secret-key"
    assert get_status.await_args_list == [call(6, force=True), call(6)]
    assert [entry.args[0].plain for entry in console_print.call_args_list] == [
        ":: free  |   6 accounts / 30d · support the work → t.me/deltafarm_bot",
        "◆  solo  |   6 accounts / 30d · thank you for supporting the work",
    ]


async def test_license_with_saved_key_only_shows_status(monkeypatch):
    license = {
        "plan": "small",
        "plan_name": "Solo",
        "suggested_accounts": 5,
        "paid_until": 2_000_000_000,
    }
    status = {"state": "active", "account_count": 3, "license": license, "offline": False}
    prompt = Mock()
    console_print = Mock()
    monkeypatch.setattr("lib.cli.sys.argv", ["exchange", "license"])
    monkeypatch.setattr("lib.cli.getpass.getpass", prompt)
    monkeypatch.setattr("lib.cli.support.count_accounts", Mock(return_value=3))
    monkeypatch.setattr("lib.cli.support.get_status", AsyncMock(return_value=status))
    monkeypatch.setattr("lib.cli.support.check", AsyncMock())
    monkeypatch.setattr("lib.cli._show_banner", AsyncMock())
    monkeypatch.setattr("lib.cli.console.print", console_print)
    monkeypatch.setattr("lib.cli.telemetry.init", Mock())
    monkeypatch.setattr("lib.cli.telemetry.flush", AsyncMock())

    with pytest.raises(SystemExit) as exc:
        await create_cli("exchange", "config.toml", ["privkey"])

    assert exc.value.code == 0
    prompt.assert_not_called()
    assert console_print.call_args.args[0].plain == (
        "◆  solo  |   3 accounts / 30d · thank you for supporting the work"
    )
