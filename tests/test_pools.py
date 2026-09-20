import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from lib.errors import AppError
from strategy.models import AppConfig, TradingClient
from strategy.runner import (
    _run_groups,
    _track_trade_started,
    run_strategy,
    select_strategy,
)


def trade_settings(symbol: str) -> dict:
    return {
        "symbols": [symbol],
        "trade_size_usd": [100, 100],
        "trade_duration": [60, 60],
        "trade_cooldown": [10, 10],
    }


def pooled_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "accounts": [
                {"name": name, "privkey": "secret"} for name in ("one", "two", "three", "four")
            ],
            "pools": {
                "btc": {"accounts": ["one", "two"], **trade_settings("BTC")},
                "eth": {
                    "accounts": ["three", "four"],
                    "group_size": 2,
                    "regroup_interval": "1h",
                    **trade_settings("ETH"),
                },
            },
        }
    )


def pool_clients() -> list[Mock]:
    clients = [Mock(name=name) for name in ("one", "two", "three", "four")]
    for client, name in zip(clients, ("one", "two", "three", "four"), strict=True):
        client.name = name
    return clients


def test_legacy_config_becomes_single_strategy():
    cfg = AppConfig.model_validate(
        {
            "accounts": [
                {"name": "one", "privkey": "secret"},
                {"name": "two", "privkey": "secret"},
            ],
            **trade_settings("BTC"),
        }
    )

    assert cfg.pools == {}
    assert cfg.strategy is not None
    assert cfg.strategy.symbols == ["BTC"]
    assert [account.name for account in cfg.accounts] == ["one", "two"]


def test_pool_accounts_cannot_overlap():
    with pytest.raises(ValidationError, match="accounts assigned to multiple pools: two"):
        AppConfig.model_validate(
            {
                "accounts": [
                    {"name": name, "privkey": "secret"} for name in ("one", "two", "three")
                ],
                "pools": {
                    "btc": {"accounts": ["one", "two"], **trade_settings("BTC")},
                    "eth": {"accounts": ["two", "three"], **trade_settings("ETH")},
                },
            }
        )


def test_pool_accounts_must_exist_and_be_assigned():
    accounts = [{"name": name, "privkey": "secret"} for name in ("one", "two", "three")]

    with pytest.raises(ValidationError, match="unknown pool accounts: missing"):
        AppConfig.model_validate(
            {
                "accounts": accounts,
                "pools": {
                    "btc": {"accounts": ["one", "missing"], **trade_settings("BTC")},
                },
            }
        )

    with pytest.raises(ValidationError, match="enabled accounts missing from pools: three"):
        AppConfig.model_validate(
            {
                "accounts": accounts,
                "pools": {
                    "btc": {"accounts": ["one", "two"], **trade_settings("BTC")},
                },
            }
        )


def test_pool_config_rejects_top_level_trading_settings():
    values = {
        "accounts": [
            {"name": name, "privkey": "secret"} for name in ("one", "two", "three", "four")
        ],
        "pools": pooled_config().model_dump()["pools"],
        **trade_settings("SOL"),
    }

    with pytest.raises(ValidationError, match="top-level trading settings cannot be used"):
        AppConfig.model_validate(values)


def test_select_named_pool_with_isolated_accounts():
    cfg = pooled_config()
    clients = pool_clients()

    strategy, selected = select_strategy(cfg, clients, "eth")

    assert strategy.symbols == ["ETH"]
    assert strategy.group_size == 2
    assert strategy.regroup_interval == 3600
    assert [account.name for account in selected] == ["three", "four"]


def test_pooled_config_requires_known_pool():
    cfg = pooled_config()
    clients = pool_clients()

    with pytest.raises(AppError, match="Select a pool: btc, eth"):
        select_strategy(cfg, clients, None)

    with pytest.raises(AppError, match="Unknown pool 'stock'. Available: btc, eth"):
        select_strategy(cfg, clients, "stock")


def test_legacy_config_rejects_pool_name():
    cfg = AppConfig.model_validate(
        {
            "accounts": [
                {"name": "one", "privkey": "secret"},
                {"name": "two", "privkey": "secret"},
            ],
            **trade_settings("BTC"),
        }
    )

    strategy, selected = select_strategy(cfg, pool_clients()[:2], None)
    assert strategy.symbols == ["BTC"]
    assert [account.name for account in selected] == ["one", "two"]

    with pytest.raises(AppError, match="This config has no named pools"):
        select_strategy(cfg, pool_clients()[:2], "btc")


async def test_run_strategy_delegates_only_selected_pool(monkeypatch):
    run_groups = AsyncMock()
    monkeypatch.setattr("strategy.runner.run_groups", run_groups)

    await run_strategy(pooled_config(), cast(list[TradingClient], pool_clients()), "btc")

    strategy, accounts = run_groups.await_args.args
    assert strategy.symbols == ["BTC"]
    assert [account.name for account in accounts] == ["one", "two"]


async def test_group_tasks_are_awaited_when_pool_is_cancelled(monkeypatch):
    cfg = pooled_config().pools["eth"]
    clients = cast(list[TradingClient], pool_clients()[2:])
    child_started = asyncio.Event()
    child_stopped = asyncio.Event()
    keep_running = asyncio.Event()

    async def balance_sorted(accounts):
        return list(accounts)

    async def run_group(*args, **kwargs):
        child_started.set()
        try:
            await keep_running.wait()
        finally:
            child_stopped.set()

    monkeypatch.setattr("strategy.runner._balance_sorted", balance_sorted)
    monkeypatch.setattr("strategy.runner._run_group", run_group)

    task = asyncio.create_task(_run_groups(cfg, clients))
    await child_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert child_stopped.is_set()


def test_pool_name_is_not_sent_to_telemetry(monkeypatch):
    cfg = pooled_config().pools["btc"]
    track = Mock()
    monkeypatch.setattr("strategy.runner.telemetry.track", track)

    _track_trade_started(cfg, cast(list[TradingClient], pool_clients()[:2]))

    properties = track.call_args.args[1]
    assert "pool" not in properties
