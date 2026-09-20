# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Probably works in production
import asyncio
import random
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import NamedTuple, Protocol

from pydantic import Field

from lib.errors import AppError
from lib.evm import EvmNetwork, EvmToken
from lib.http import ApiError
from lib.logger import logger
from lib.table import AutoTable, Column
from lib.utils import confirm, format_duration

from .models import StrategyConfig

DEPOSIT_CREDIT_TIMEOUT_SEC = 30 * 60
DEPOSIT_POLL_DELAY = 10
DEPOSIT_START_DELAY_SEC = 1.5
BALANCE_TARGET_STEP = Decimal("0.05")


class DepositClient(Protocol):
    exchange: str
    name: str

    async def deposit_asset(self) -> "DepositAsset": ...

    async def deposit_balances(self) -> "DepositBalances": ...

    async def deposit(self, amount: Decimal) -> str: ...

    async def deposit_balance(self) -> Decimal: ...


class DepositConfig(StrategyConfig):
    balance_target: Decimal | None = Field(None, gt=0)
    balance_target_jitter_pct: Decimal = Field(Decimal(2), ge=0, lt=100)
    deposit_min_amount: Decimal = Field(Decimal(10), ge=0)


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
    assert cfg.balance_target is not None
    spread = cfg.balance_target * cfg.balance_target_jitter_pct / 100
    if not spread:
        return cfg.balance_target

    min_units = int(
        ((cfg.balance_target - spread) / BALANCE_TARGET_STEP).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    max_units = int(
        ((cfg.balance_target + spread) / BALANCE_TARGET_STEP).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    if min_units > max_units:
        return cfg.balance_target

    return Decimal(random.randint(min_units, max_units)) * BALANCE_TARGET_STEP


def _make_plan[T: DepositClient](
    account: T,
    cfg: DepositConfig,
    asset: DepositAsset,
    balances: DepositBalances,
    minimum: Decimal,
) -> DepositPlanItem[T]:
    random_target = balance_target(cfg)
    assert cfg.balance_target is not None
    spread = cfg.balance_target * cfg.balance_target_jitter_pct / 100
    maximum_target = cfg.balance_target + spread
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
            DepositStatus("insufficient funds", True),
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
    assert cfg.balance_target is not None
    spread = cfg.balance_target * cfg.balance_target_jitter_pct / 100
    target = f"${cfg.balance_target:,.2f}"
    if spread:
        target = f"${cfg.balance_target - spread:,.2f}–${cfg.balance_target + spread:,.2f}"

    delay = format_duration(cfg.balance_transfer_delay.min)
    if cfg.balance_transfer_delay.min != cfg.balance_transfer_delay.max:
        delay += f"–{format_duration(cfg.balance_transfer_delay.max)}"

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
    if cfg.balance_target is None:
        raise AppError("Set balance_target in config")
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
        progress = f"{index + 1}/{len(plan)}"
        with logger.contextualize(progress=progress):
            await row.account.deposit(row.amount)

        if index < len(plan) - 1:
            wait = cfg.balance_transfer_delay.sample()
            logger.info(f"Waiting {format_duration(wait)} before next deposit")
            await asyncio.sleep(wait)
