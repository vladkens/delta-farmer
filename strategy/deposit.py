# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Probably works in production
import asyncio
import random
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import NamedTuple, Protocol

from pydantic import Field

from lib.cli import confirm
from lib.errors import AppError
from lib.evm import EvmNetwork, EvmToken
from lib.http import ApiError
from lib.logger import logger
from lib.models import DurationSec, TimeRange
from lib.table import AutoTable, Column
from lib.utils import format_duration

from .models import StrategyConfig

DEPOSIT_CREDIT_TIMEOUT_SEC = 30 * 60
DEPOSIT_POLL_DELAY = 10
DEPOSIT_START_DELAY_SEC = 1.5


class DepositClient(Protocol):
    exchange: str
    name: str

    async def deposit_asset(self) -> "DepositAsset": ...

    async def deposit_balances(self) -> "DepositBalances": ...

    async def deposit(self, amount: Decimal) -> str: ...

    async def deposit_balance(self) -> Decimal: ...


class DepositConfig(StrategyConfig):
    deposit_target: Decimal | None = Field(None, gt=0)
    deposit_target_random_pct: Decimal = Field(Decimal(0), ge=0, lt=100)
    deposit_min_amount: Decimal = Field(Decimal(10), ge=0)
    deposit_delay: TimeRange = TimeRange(
        min=DurationSec("30s"),
        max=DurationSec("90s"),
    )


@dataclass(frozen=True)
class DepositAsset:
    exchange: str
    network: EvmNetwork
    token: EvmToken
    min_amount: Decimal = Decimal(0)


@dataclass(frozen=True)
class DepositBalances:
    exchange: Decimal
    wallet: Decimal
    native: Decimal | None = None
    unavailable: str | None = None


class DepositStatus(NamedTuple):
    text: str
    problem: bool


@dataclass(frozen=True)
class DepositPlanItem[T: DepositClient]:
    account: T
    target: Decimal
    exchange_balance: Decimal
    wallet_balance: Decimal
    amount: Decimal
    status: DepositStatus


def deposit_amount(
    amount: Decimal,
    decimals: int,
    minimum: Decimal = Decimal(0),
) -> Decimal:
    amount = amount.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_CEILING)
    if amount <= 0:
        raise ValueError("Deposit amount must be positive")
    if amount < minimum:
        raise ValueError(f"Minimum deposit is ${minimum:g}")

    return amount


async def wait_for_deposit_credit(
    account: DepositClient,
    expected_balance: Decimal,
    reference: str,
) -> None:
    deadline = time.monotonic() + DEPOSIT_CREDIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if await account.deposit_balance() >= expected_balance:
            return

        await asyncio.sleep(DEPOSIT_POLL_DELAY)

    raise ApiError(f"{account.exchange.title()} deposit credit timed out: {reference}")


def balance_target(cfg: DepositConfig) -> Decimal:
    assert cfg.deposit_target is not None
    spread = cfg.deposit_target * cfg.deposit_target_random_pct / 100
    if not spread:
        return cfg.deposit_target

    offset = Decimal(str(random.random())) * spread * 2
    return (cfg.deposit_target - spread + offset).quantize(Decimal("0.01"))


def _make_plan[T: DepositClient](
    account: T,
    cfg: DepositConfig,
    asset: DepositAsset,
    balances: DepositBalances,
    minimum: Decimal,
) -> DepositPlanItem[T]:
    random_target = balance_target(cfg)
    assert cfg.deposit_target is not None
    spread = cfg.deposit_target * cfg.deposit_target_random_pct / 100
    maximum_target = cfg.deposit_target + spread
    if balances.unavailable:
        return DepositPlanItem(
            account,
            random_target,
            balances.exchange,
            balances.wallet,
            Decimal(0),
            DepositStatus(balances.unavailable, True),
        )

    if balances.exchange < maximum_target and maximum_target - balances.exchange < minimum:
        return DepositPlanItem(
            account,
            random_target,
            balances.exchange,
            balances.wallet,
            Decimal(0),
            DepositStatus("gap below minimum", False),
        )

    if balances.exchange >= random_target:
        return DepositPlanItem(
            account,
            random_target,
            balances.exchange,
            balances.wallet,
            Decimal(0),
            DepositStatus("target reached", False),
        )

    amount = min(random_target - balances.exchange, balances.wallet)
    if amount > 0:
        amount = deposit_amount(amount, asset.token.decimals)
    if amount < minimum:
        return DepositPlanItem(
            account,
            random_target,
            balances.exchange,
            balances.wallet,
            Decimal(0),
            DepositStatus(f"available deposit <${minimum:,.2f}", True),
        )

    return DepositPlanItem(
        account,
        random_target,
        balances.exchange,
        balances.wallet,
        amount,
        DepositStatus("deposit", False),
    )


def _plan_header(cfg: DepositConfig, asset: DepositAsset, minimum: Decimal) -> str:
    assert cfg.deposit_target is not None
    spread = cfg.deposit_target * cfg.deposit_target_random_pct / 100
    target = f"${cfg.deposit_target:,.2f}"
    if spread:
        target = f"${cfg.deposit_target - spread:,.2f}–${cfg.deposit_target + spread:,.2f}"

    delay = format_duration(cfg.deposit_delay.min)
    if cfg.deposit_delay.min != cfg.deposit_delay.max:
        delay += f"–{format_duration(cfg.deposit_delay.max)}"

    network = asset.network.name.title()
    return (
        f"Network: {network} ({asset.network.chain_id}), token: {asset.token.symbol}\n"
        f"Target: {target}, minimum: ${minimum:,.2f}, delay: {delay}"
    )


def _plan_table(asset: DepositAsset) -> AutoTable:
    return AutoTable(
        Column("Account", justify="left"),
        Column("Target", "${:,.2f}"),
        Column(asset.exchange, "${:,.2f}"),
        Column("Wallet", "${:,.2f}"),
        Column("Deposit", "${:,.2f}", total=sum),
        Column("Status", justify="left"),
    )


async def run_deposits[T: DepositClient](
    accounts: list[T],
    cfg: DepositConfig,
) -> None:
    if cfg.deposit_target is None:
        raise AppError("Set deposit_target in config")
    if not accounts:
        logger.info("No accounts configured for deposit")
        return

    asset = await accounts[0].deposit_asset()
    minimum = max(cfg.deposit_min_amount, asset.min_amount)
    rows = []
    for account in accounts:
        balances = await account.deposit_balances()
        row = _make_plan(account, cfg, asset, balances, minimum)
        rows.append(row)

    print(_plan_header(cfg, asset, minimum))
    print()
    tbl = _plan_table(asset)
    for row in rows:
        color = "yellow" if row.status.problem else "green"
        status = f"[{color}]{row.status.text}[/{color}]"
        tbl.add_row(
            row.account.name,
            row.target,
            row.exchange_balance,
            row.wallet_balance,
            row.amount,
            status,
        )

    tbl.print()
    plan = [row for row in rows if row.amount > 0]
    if not plan:
        return

    if not confirm("Execute deposit plan?"):
        logger.info("Deposit cancelled")
        return

    await asyncio.sleep(DEPOSIT_START_DELAY_SEC)
    print("-" * 60)
    for index, row in enumerate(plan):
        await row.account.deposit(row.amount)
        if index < len(plan) - 1:
            wait = cfg.deposit_delay.sample()
            logger.info(f"Waiting {format_duration(wait)} before next deposit")
            await asyncio.sleep(wait)
