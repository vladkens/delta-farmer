# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Small plans, fewer surprises
import asyncio
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import NamedTuple, Protocol

from lib.cli import confirm
from lib.errors import AppError
from lib.evm import EvmNetwork, EvmToken
from lib.logger import logger
from lib.table import AutoTable, Column
from lib.utils import format_duration

from .deposit import DepositConfig, balance_target

WITHDRAWAL_START_DELAY_SEC = 1.5


class WithdrawalClient(Protocol):
    name: str

    async def withdrawal_info(self) -> "WithdrawalInfo": ...

    async def withdraw(self, amount: Decimal) -> str: ...


@dataclass(frozen=True)
class WithdrawalInfo:
    exchange: str
    network: EvmNetwork
    token: EvmToken
    balance: Decimal
    available: Decimal
    unavailable: str | None = None
    fee: Decimal = Decimal(0)


class WithdrawalStatus(NamedTuple):
    text: str
    problem: bool


@dataclass(frozen=True)
class WithdrawalPlanItem[T: WithdrawalClient]:
    account: T
    target: Decimal | None
    balance: Decimal
    available: Decimal
    fee: Decimal
    amount: Decimal
    status: WithdrawalStatus


def withdrawal_amount(amount: Decimal, decimals: int) -> Decimal:
    amount = amount.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_FLOOR)
    if amount <= 0:
        raise ValueError("Withdrawal amount must be positive")
    return amount


def withdrawal_limit(
    balance: Decimal,
    available: Decimal,
    fee: Decimal = Decimal(0),
) -> Decimal:
    return min(max(available, Decimal(0)), max(balance - fee, Decimal(0)))


def _make_plan[T: WithdrawalClient](
    account: T,
    info: WithdrawalInfo,
    target: Decimal | None,
    minimum: Decimal,
) -> WithdrawalPlanItem[T]:
    available = withdrawal_limit(info.balance, info.available, info.fee)
    if info.unavailable:
        return WithdrawalPlanItem(
            account,
            target,
            info.balance,
            available,
            info.fee,
            Decimal(0),
            WithdrawalStatus(info.unavailable, True),
        )

    if target is not None and info.balance <= target:
        return WithdrawalPlanItem(
            account,
            target,
            info.balance,
            available,
            info.fee,
            Decimal(0),
            WithdrawalStatus("target reached", False),
        )

    requested = available if target is None else min(info.balance - target - info.fee, available)
    if target is not None and info.balance - target < minimum:
        return WithdrawalPlanItem(
            account,
            target,
            info.balance,
            available,
            info.fee,
            Decimal(0),
            WithdrawalStatus("gap below minimum", False),
        )

    if available <= 0:
        return WithdrawalPlanItem(
            account,
            target,
            info.balance,
            available,
            info.fee,
            Decimal(0),
            WithdrawalStatus("nothing to withdraw", False),
        )

    if requested < minimum:
        return WithdrawalPlanItem(
            account,
            target,
            info.balance,
            available,
            info.fee,
            Decimal(0),
            WithdrawalStatus(f"safe amount <${minimum:,.2f}", False),
        )

    amount = withdrawal_amount(requested, info.token.decimals)
    return WithdrawalPlanItem(
        account,
        target,
        info.balance,
        available,
        info.fee,
        amount,
        WithdrawalStatus("withdraw", False),
    )


def _target_label(cfg: DepositConfig) -> str:
    assert cfg.deposit_target is not None
    spread = cfg.deposit_target * cfg.deposit_target_random_pct / 100
    if not spread:
        return f"${cfg.deposit_target:,.2f}"
    return f"${cfg.deposit_target - spread:,.2f}–${cfg.deposit_target + spread:,.2f}"


async def run_withdrawals[T: WithdrawalClient](
    accounts: list[T],
    cfg: DepositConfig,
    *,
    withdraw_full: bool = False,
) -> None:
    if not withdraw_full and cfg.deposit_target is None:
        raise AppError("Set deposit_target in config")
    if not accounts:
        logger.info("No accounts configured for withdrawal")
        return

    rows = []
    asset = None
    for account in accounts:
        info = await account.withdrawal_info()
        asset = asset or info
        target = None if withdraw_full else balance_target(cfg)
        rows.append(_make_plan(account, info, target, cfg.deposit_min_amount))

    assert asset is not None
    network = asset.network.name.title()
    delay = format_duration(cfg.deposit_delay.min)
    if cfg.deposit_delay.min != cfg.deposit_delay.max:
        delay += f"–{format_duration(cfg.deposit_delay.max)}"

    print(f"Network: {network} ({asset.network.chain_id}), token: {asset.token.symbol}")
    if withdraw_full:
        print(
            f"Withdrawal: full balance, minimum: ${cfg.deposit_min_amount:,.2f}, "
            f"fee: ${asset.fee:,.2f}, delay: {delay}"
        )
    else:
        print(
            f"Balance target: {_target_label(cfg)}, minimum: ${cfg.deposit_min_amount:,.2f}, "
            f"fee: ${asset.fee:,.2f}, delay: {delay}"
        )
    print()

    columns = [Column("Account", justify="left")]
    if not withdraw_full:
        columns.append(Column("Target", "${:,.2f}"))
    columns.extend(
        [
            Column(asset.exchange, "${:,.2f}"),
            Column("Withdraw", "${:,.2f}", total=sum),
            Column("After", "${:,.2f}"),
            Column("Status", justify="left"),
        ]
    )
    tbl = AutoTable(*columns)
    for row in rows:
        color = "yellow" if row.status.problem else "green"
        status = f"[{color}]{row.status.text}[/{color}]"
        values = [row.account.name]
        if not withdraw_full:
            values.append(row.target)
        after = row.balance - row.amount - (row.fee if row.amount > 0 else Decimal(0))
        values.extend([row.balance, row.amount, after])
        values.append(status)
        tbl.add_row(*values)

    tbl.print()
    plan = [row for row in rows if row.amount > 0]
    blocked = [row for row in rows if row.status.text.startswith("open ")]
    if blocked:
        objects = []
        if any("positions" in row.status.text for row in blocked):
            objects.append("positions")
        if any("orders" in row.status.text for row in blocked):
            objects.append("orders")
        accounts = ", ".join(row.account.name for row in blocked)
        logger.warning(f"Close open {' and '.join(objects)} before withdrawing: {accounts}")
        if withdraw_full:
            return

    if not plan:
        if not blocked:
            logger.info("No withdrawals planned")
        return

    if not confirm("Execute withdrawal plan?"):
        logger.info("Withdrawal cancelled")
        return

    await asyncio.sleep(WITHDRAWAL_START_DELAY_SEC)
    print("-" * 60)
    for index, row in enumerate(plan):
        await row.account.withdraw(row.amount)
        if index < len(plan) - 1:
            wait = cfg.deposit_delay.sample()
            logger.info(f"Waiting {format_duration(wait)} before next withdrawal")
            await asyncio.sleep(wait)
