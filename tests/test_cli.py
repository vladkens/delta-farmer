import argparse
import signal
from unittest.mock import AsyncMock, Mock, call

import pytest

from lib.cli import CliParser, _handle_login, confirm, create_clients
from lib.errors import AppError
from lib.models import AccountConfig


class Client:
    def __init__(self, name: str):
        self.name = name

    async def login(self, *, force: bool = False) -> None:
        return None


def test_confirm_sigint(monkeypatch):
    previous = Mock()
    set_signal = Mock(return_value=previous)
    monkeypatch.setattr("lib.cli.signal.signal", set_signal)
    monkeypatch.setattr("lib.cli.sys.stdin", Mock(isatty=Mock(return_value=False)))
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


async def test_enabled_accounts():
    accounts = [
        AccountConfig(name="on", privkey="x", enabled=True),
        AccountConfig(name="off", privkey="x", enabled=False),
    ]

    all_clients, active = await create_clients(
        argparse.Namespace(command="info"), accounts, lambda account: Client(account.name)
    )

    assert [client.name for client in all_clients] == ["on", "off"]
    assert [client.name for client in active] == ["on"]
