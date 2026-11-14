from decimal import Decimal

import pytest

from apps.lighter import (
    _select_account,
    core_twins,
    hedge_accounts,
    hedge_pairs,
    make_core_twin,
)
from clients.lighter import LighterCoreClient, LighterRobinhoodClient
from lib.errors import AppError
from lib.models import AccountConfig
from strategy import AppConfig, TradeConfig
from strategy.runner import _check_cfg, _run_groups
from strategy.trade import plan_delta_trades

# Synthetic keys, never used for an on-chain account or network authentication.
MAIN_KEY = "01" * 32
ALT_KEY = "02" * 32


def account(name: str, privkey: str = MAIN_KEY, **fields) -> AccountConfig:
    return AccountConfig.model_validate({"name": name, "privkey": privkey, **fields})


def hedge_pool(*names: str, long_exchange: str = "lighter") -> TradeConfig:
    cfg = AppConfig.model_validate(
        {
            "accounts": [{"name": name, "privkey": "secret"} for name in names],
            "pools": {
                "hedge": {
                    "accounts": list(names),
                    "symbols": ["BTC"],
                    "long_exchange": long_exchange,
                    "trade_size_usd": {"min": 100, "max": 100},
                    "trade_duration": [60, 60],
                    "trade_cooldown": [10, 10],
                }
            },
        }
    )
    return cfg.pools["hedge"]


@pytest.fixture
async def wallets():
    """Two configured accounts, `main` and `alt`, as the app builds them: RH clients and twins."""
    configs = [account("main"), account("alt", ALT_KEY, proxy="http://user:pass@host:1")]
    accounts = [LighterRobinhoodClient.from_config(cfg) for cfg in configs]
    cores = {cfg.name: make_core_twin(cfg) for cfg in configs}
    try:
        yield accounts, cores
    finally:
        for client in [*accounts, *cores.values()]:
            await client.close()


async def test_core_twin_is_the_same_wallet_on_core(wallets):
    accounts, cores = wallets
    twin = cores["alt"]

    assert type(twin) is LighterCoreClient
    assert (twin.name, twin.exchange, twin.address, twin.proxy) == (
        "alt@core",
        "lighter_core",
        accounts[1].address,
        "http://user:pass@host:1",
    )


@pytest.mark.parametrize("long_exchange", ["lighter", "lighter_core"])
async def test_one_account_trades_its_rh_leg_against_its_core_twin(wallets, long_exchange):
    accounts, cores = wallets
    legs = hedge_pairs(accounts[:1], cores)
    cfg = hedge_pool("main", long_exchange=long_exchange)
    _check_cfg(cfg, legs)

    assert [leg.name for leg in legs] == ["main", "main@core"]
    assert cfg.group_size is None  # one pair runs as the single group

    balances = [(leg.name, 1000.0) for leg in legs]
    trades = await plan_delta_trades(legs, ["BTC"], Decimal(100), 10, balances, long_exchange)

    assert trades is not None
    sides = {leg.client.name: leg.side for leg in trades[0].legs}
    if long_exchange == "lighter":
        assert sides == {"main": "bid", "main@core": "ask"}
    else:
        assert sides == {"main": "ask", "main@core": "bid"}


async def test_two_accounts_run_as_two_pairs_never_mixed(wallets, monkeypatch):
    accounts, cores = wallets
    legs = hedge_pairs(accounts, cores)
    cfg = hedge_pool("main", "alt")
    _check_cfg(cfg, legs)

    assert cfg.group_size == 2

    groups: list[list[LighterCoreClient]] = []

    async def run_group(_cfg, _name, group, *_args) -> None:
        groups.append(list(group))

    monkeypatch.setattr("strategy.runner._run_group", run_group)
    await _run_groups(cfg, legs)

    assert [[leg.name for leg in group] for group in groups] == [
        ["main", "main@core"],
        ["alt", "alt@core"],
    ]
    assert all(len({leg.address for leg in group}) == 1 for group in groups)


async def test_two_accounts_on_one_wallet_are_refused():
    # The old config shape: `core1` repeated `main`'s key to stand for its Core leg.
    configs = [account("main"), account("core1")]
    accounts = [LighterRobinhoodClient.from_config(cfg) for cfg in configs]
    cores = {cfg.name: make_core_twin(cfg) for cfg in configs}
    try:
        with pytest.raises(AppError, match="Accounts share one wallet: main/core1"):
            hedge_pairs(accounts, cores)
    finally:
        for client in [*accounts, *cores.values()]:
            await client.close()


async def test_money_and_referral_commands_never_reach_a_core_twin(wallets):
    # deposit, withdraw and useref act on configured accounts only, which are RH clients.
    accounts, _ = wallets

    with pytest.raises(AppError, match="Account not found: main@core"):
        _select_account(accounts, accounts, "main@core")


@pytest.mark.parametrize("hedged", [set(), {"main", "alt"}])
async def test_close_leaves_a_disabled_accounts_core_twin_alone(wallets, hedged):
    # `alt` is disabled: `close` and `positions` must not touch its Core twin either.
    accounts, cores = wallets
    hedge, others = core_twins(accounts[:1], cores, hedged)

    assert [twin.name for twin in hedge] == (["main@core"] if hedged else [])
    assert [twin.name for twin in others] == ([] if hedged else ["main@core"])


def test_only_hedge_accounts_reach_core():
    # login, close and positions touch the Core twins of these accounts only.
    settings = {"symbols": ["BTC"], "trade_duration": [60, 60], "trade_cooldown": [10, 10]}
    accounts = [{"name": name, "privkey": "secret"} for name in ("main", "ok01", "mx01")]
    pools = AppConfig.model_validate(
        {
            "accounts": accounts,
            "pools": {
                "hedge": {"accounts": ["main"], "long_exchange": "lighter", **settings},
                "plain": {"accounts": ["ok01", "mx01"], **settings},
            },
        }
    )
    legacy = AppConfig.model_validate(
        {"accounts": accounts, "long_exchange": "lighter", **settings}
    )
    plain = AppConfig.model_validate({"accounts": accounts, **settings})

    assert hedge_accounts(pools) == {"main"}
    assert hedge_accounts(legacy) == {"main", "ok01", "mx01"}
    assert hedge_accounts(plain) == set()
