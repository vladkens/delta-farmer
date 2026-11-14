import argparse
import signal
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call

import pytest

from lib.cli import CliParser, _get_version, _handle_login, create_cli, setup_app
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
    eprint = Mock()
    monkeypatch.setattr("lib.cli.eprint", eprint)
    monkeypatch.setattr("lib.cli.latest_release_notice", AsyncMock(return_value=None))
    monkeypatch.delenv("DF_NO_BANNER", raising=False)

    cfg = Mock(accounts=accounts)
    all_clients, active = await setup_app(
        argparse.Namespace(command="info"), cfg, lambda account: Client(account.name)
    )

    assert [client.name for client in all_clients] == ["on", "off"]
    assert [client.name for client in active] == ["on"]
    assert any("delta-farmer" in call.args[0] for call in eprint.call_args_list)


async def test_banner_can_be_hidden_with_env(monkeypatch):
    eprint = Mock()
    monkeypatch.setattr("lib.cli.eprint", eprint)
    monkeypatch.setattr("lib.cli.latest_release_notice", AsyncMock(return_value=None))
    monkeypatch.setenv("DF_NO_BANNER", "1")

    await setup_app(
        argparse.Namespace(command="info"),
        Mock(accounts=[AccountConfig(name="on", privkey="x")]),
        lambda account: Client(account.name),
    )

    eprint.assert_not_called()


async def test_setup_app_handles_move(monkeypatch):
    args = argparse.Namespace(command="move")
    cfg = Mock(accounts=[AccountConfig(name="on", privkey="x")])
    run_move = AsyncMock()
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


@pytest.mark.parametrize(
    ("argv", "cycles"),
    [
        (["exchange", "trade"], None),  # absent means the unbounded run, as before
        (["exchange", "trade", "--cycles", "3"], 3),
        (["exchange", "trade", "stock", "--cycles", "1"], 1),
    ],
)
async def test_trade_accepts_a_cycle_limit(monkeypatch, argv, cycles):
    monkeypatch.setattr("lib.cli.sys.argv", argv)
    monkeypatch.setattr("lib.cli._load_tg_config", Mock(return_value=Mock()))
    monkeypatch.setattr("lib.cli.tg.init", Mock())
    monkeypatch.setattr("lib.cli.telemetry.init", Mock())
    monkeypatch.delenv("DF_LOG_FILE", raising=False)

    args = await create_cli("exchange", "config.toml", ["privkey"])

    assert args.cycles == cycles


@pytest.mark.parametrize(("value", "message"), [("0", "must be 1 or more"), ("x", "not a number")])
async def test_trade_refuses_a_cycle_limit_that_cannot_run(monkeypatch, capsys, value, message):
    monkeypatch.setattr("lib.cli.sys.argv", ["exchange", "trade", "--cycles", value])
    monkeypatch.setattr("lib.cli.telemetry.init", Mock())

    with pytest.raises(SystemExit) as exc:
        await create_cli("exchange", "config.toml", ["privkey"])

    assert exc.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("described", "sha", "expected"),
    [
        ("v{v}", "abc1234", ("v{v} ", True)),
        ("v0.9.0-12-g3672a1b", "3672a1b", ("v0.9.0-12-g3672a1b ", False)),
        (None, "3672a1b", ("v{v}-g3672a1b ", False)),
        (None, None, ("v{v}-dev ", False)),
    ],
    ids=["release", "after-release", "no-tags", "no-git"],
)
def test_version_reports_commits_since_release(monkeypatch, described, sha, expected):
    with open(Path(__file__).parent.parent / "pyproject.toml", "rb") as fp:
        version = tomllib.load(fp)["project"]["version"]

    def git(repo, *args):
        value = described if args[0] == "describe" else sha
        return value.format(v=version) if value else None

    monkeypatch.setattr("lib.cli._git", git)

    label, release = expected
    assert _get_version() == (label.format(v=version), release)
