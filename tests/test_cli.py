# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Small plans, fewer surprises
import argparse
from unittest.mock import Mock

import pytest

from lib.cli import _handle_login, create_clients
from lib.models import AccountConfig


class FakeClient:
    def __init__(self, name: str, failures: int = 0):
        self.name = name
        self.failures = failures
        self.login_calls: list[bool] = []

    async def login(self, *, force: bool = False) -> None:
        self.login_calls.append(force)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary login failure")


async def test_create_clients_splits_enabled_accounts():
    args = argparse.Namespace(command="info")
    accounts = [
        AccountConfig(name="enabled", privkey="x", enabled=True),
        AccountConfig(name="disabled", privkey="x", enabled=False),
    ]

    all_clients, active_clients = await create_clients(
        args, accounts, lambda account: FakeClient(account.name)
    )

    assert [client.name for client in all_clients] == ["enabled", "disabled"]
    assert [client.name for client in active_clients] == ["enabled"]


@pytest.mark.parametrize("force", [False, True])
async def test_create_clients_handles_login_before_app_dispatch(monkeypatch, force):
    args = argparse.Namespace(command="login", force=force)
    accounts = [
        AccountConfig(name="flaky", privkey="x", enabled=False),
        AccountConfig(name="stable", privkey="x", enabled=True),
    ]
    clients: dict[str, FakeClient] = {}
    order: list[str] = []

    def factory(account: AccountConfig) -> FakeClient:
        client = FakeClient(account.name, failures=1 if account.name == "flaky" else 0)
        login = client.login

        async def tracked_login(*, force: bool = False) -> None:
            order.append(account.name)
            await login(force=force)

        client.login = tracked_login
        clients[account.name] = client
        return client

    now = 0.0
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    monkeypatch.setattr("lib.cli.asyncio.sleep", sleep)
    monkeypatch.setattr("lib.cli.time.monotonic", Mock(side_effect=lambda: now))

    with pytest.raises(SystemExit) as exc:
        await create_clients(args, accounts, factory)

    assert exc.value.code == 0
    assert clients["stable"].login_calls == [force]
    assert clients["flaky"].login_calls == [force, force]
    assert order == ["flaky", "stable", "flaky"]
    assert sleeps == [30]


async def test_login_recommends_new_proxy_after_three_failures(monkeypatch):
    client = FakeClient("blocked", failures=3)
    now = 0.0

    async def sleep(delay: float) -> None:
        nonlocal now
        now += delay

    warning = Mock()
    monkeypatch.setattr("lib.cli.asyncio.sleep", sleep)
    monkeypatch.setattr("lib.cli.time.monotonic", Mock(side_effect=lambda: now))
    monkeypatch.setattr("lib.cli.logger.warning", warning)

    await _handle_login([client], force=False)

    assert client.login_calls == [False] * 4
    warning.assert_any_call("Login keeps failing: blocked. Try a different proxy.")
