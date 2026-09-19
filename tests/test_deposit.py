from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, call

import pytest

from lib.evm import EvmNetwork, EvmToken
from lib.models import DurationSec, TimeRange
from strategy import deposit as deposit_strategy
from strategy.deposit import (
    DepositAsset,
    DepositBalances,
    DepositConfig,
    balance_target,
    deposit_amount,
    run_deposits,
)

NETWORK = EvmNetwork("testnet", 1, "https://rpc.test", {})
TOKEN = EvmToken("USDC", "0x0000000000000000000000000000000000000001", 6)
ASSET = DepositAsset("Test", NETWORK, TOKEN)


def test_balance_transfer_delay_defaults_to_two_to_four_minutes():
    cfg = DepositConfig.model_construct()

    assert cfg.balance_transfer_delay.min == DurationSec("2m")
    assert cfg.balance_transfer_delay.max == DurationSec("4m")


@pytest.mark.parametrize(
    ("pick", "expected"),
    [(min, "326.35"), (max, "339.65")],
    ids=["minimum", "maximum"],
)
def test_balance_target_uses_five_cent_steps(monkeypatch, pick, expected):
    cfg = DepositConfig.model_construct(
        balance_target=Decimal(333),
        balance_target_jitter_pct=Decimal(2),
    )
    monkeypatch.setattr(deposit_strategy.random, "randint", pick)

    assert balance_target(cfg) == Decimal(expected)


@dataclass
class Account:
    name: str
    balances: DepositBalances
    execute: Callable[["Account", Decimal], Awaitable[None]] | None = None
    exchange: str = "test"

    async def deposit_asset(self) -> DepositAsset:
        return ASSET

    async def deposit_balances(self) -> DepositBalances:
        return self.balances

    async def deposit(self, amount: Decimal) -> str:
        if self.execute:
            await self.execute(self, amount)

        return "deposit-id"

    async def deposit_balance(self) -> Decimal:
        return self.balances.exchange


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("1.0000001", "1.000001"), ("1.1", "1.100000")],
    ids=["ceil", "scale"],
)
def test_deposit_rounding(amount, expected):
    assert deposit_amount(Decimal(amount), 6, Decimal(1)) == Decimal(expected)


def test_deposit_rejects_invalid():
    with pytest.raises(ValueError, match="positive"):
        deposit_amount(Decimal(0), 6)
    with pytest.raises(ValueError, match="Minimum deposit"):
        deposit_amount(Decimal("0.5"), 6, Decimal(1))


async def test_deposits_are_sequential(monkeypatch):
    events: list[str] = []

    async def execute(account: Account, amount: Decimal) -> None:
        events.append(f"{account.name}:{amount:g}")

    accounts = [
        Account("first", DepositBalances(exchange=Decimal(2), wallet=Decimal(20)), execute),
        Account("second", DepositBalances(exchange=Decimal(0), wallet=Decimal(20)), execute),
    ]
    cfg = DepositConfig.model_construct(
        balance_target=Decimal(10),
        balance_target_jitter_pct=Decimal(0),
        deposit_min_amount=Decimal(1),
        balance_transfer_delay=TimeRange(min=DurationSec(30), max=DurationSec(30)),
    )
    sleep = AsyncMock(side_effect=lambda delay: events.append(f"sleep:{delay:g}"))
    monkeypatch.setattr(deposit_strategy, "confirm", Mock(return_value=True))
    monkeypatch.setattr(deposit_strategy.asyncio, "sleep", sleep)

    await run_deposits(accounts, cfg)

    assert events == ["sleep:1.5", "first:8.000000", "sleep:30", "second:10.000000"]
    assert sleep.await_args_list == [call(1.5), call(30)]
