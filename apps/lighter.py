# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
import random
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from pydantic import Field

from clients.lighter import DepositNetwork, LighterClient
from lib.cli import confirm, create_cli, create_clients, run_app
from lib.errors import AppError
from lib.evm import (
    ERC20_TRANSFER_GAS_LIMIT,
    check_chain,
    create_rpc,
    estimate_network_fee,
    get_wallet_balances,
    transfer_erc20,
    wait_receipt,
)
from lib.http import ApiError
from lib.logger import logger
from lib.models import DurationSec, TimeRange
from lib.table import AutoTable, Column
from lib.utils import format_duration, gather_accs, short_addr
from strategy import StrategyConfig, load_config
from strategy.runner import close_all, print_positions, run_groups

DEPOSIT_RECEIPT_TIMEOUT_SEC = 5 * 60
DEPOSIT_CREDIT_TIMEOUT_SEC = 30 * 60
DEPOSIT_POLL_DELAY = 10


class LighterConfig(StrategyConfig):
    deposit_network: str = "base"
    deposit_target: Decimal | None = Field(None, gt=0)
    deposit_target_random_pct: Decimal = Field(Decimal(0), ge=0, lt=100)
    deposit_min_amount: Decimal = Field(Decimal(10), ge=0)
    deposit_delay: TimeRange = TimeRange(
        min=DurationSec("30s"),
        max=DurationSec("90s"),
    )


@dataclass(frozen=True)
class DepositPlanItem:
    account: LighterClient
    target: Decimal
    lighter_balance: Decimal
    wallet_balance: Decimal
    amount: Decimal
    status: str


async def print_info(accs: list[LighterClient]):
    def total(values):
        return sum(value for value in values if value is not None)

    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=total),
        Column("Burn", "{:,.2f}", total=total),
        Column("Points", "{:,.2f}", total=total),
        Column(
            "P/Price",
            "{:,.4f}",
            compute=lambda r: (
                r["Burn"] / r["Points"] if r["Burn"] is not None and r["Points"] else None
            ),
        ),
        Column(
            "$/100k",
            "${:,.2f}",
            compute=lambda r: (
                r["Burn"] / r["Volume"] * Decimal("1e5")
                if r["Burn"] is not None and r["Volume"]
                else None
            ),
        ),
        Column("Balance", "{:,.2f}", total=total),
        Column("Ref", justify="left"),
    )

    async def row(acc: LighterClient):
        a = short_addr(acc.address)
        if not await acc.registered():
            return ("✗", acc.name, a, None, None, None, None, None)
        if not await acc.auth_ready():
            return ("✗", acc.name, a, None, None, None, await acc.balance(), None)

        p = await acc.profile()
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance, p.ref_code)

    for item in await gather_accs(accs, row):
        tbl.add_row(*item)

    tbl.print()


async def require_login(accs: list[LighterClient]) -> None:
    ready = await asyncio.gather(*(acc.auth_ready() for acc in accs))
    missing = [acc.name for acc, ok in zip(accs, ready) if not ok]
    if missing:
        names = ", ".join(missing)
        raise AppError(f"Login required: {names}. Run: uv run apps/lighter.py login")


async def _wait_for_lighter_credit(acc: LighterClient, intent_address: str, tx_hash: str) -> None:
    deadline = time.monotonic() + DEPOSIT_CREDIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        deposit = await acc.latest_deposit()
        if deposit is None or deposit["intent_address"].lower() != intent_address.lower():
            await asyncio.sleep(DEPOSIT_POLL_DELAY)
            continue

        status = deposit["status"]
        if status == "completed":
            return
        if status not in ("pending", "bridging"):
            description = deposit.get("description", status)
            raise ApiError(f"Lighter deposit failed: {description}")

        await asyncio.sleep(DEPOSIT_POLL_DELAY)

    raise ApiError(f"Lighter deposit credit timed out: {tx_hash}")


def _deposit_amount(
    amount: Decimal,
    info: DepositNetwork,
) -> Decimal:
    amount = amount.quantize(Decimal(1).scaleb(-info.decimals), rounding=ROUND_CEILING)
    if amount <= 0:
        raise ValueError("Deposit amount must be positive")
    if amount < info.min_amount:
        raise ValueError(f"Minimum deposit is ${info.min_amount:g}")

    return amount


async def execute_deposit(acc: LighterClient, amount: Decimal, network: str) -> str:
    info = await acc.deposit_network(network)
    amount = _deposit_amount(amount, info)
    amount_units = int(amount.scaleb(info.decimals))
    recipient = await acc.create_deposit_intent(info.chain_id, amount_units)
    async with create_rpc(info.chain_id, network, acc.proxy) as rpc:
        await check_chain(rpc, info.chain_id)
        tx_hash, fee = await transfer_erc20(
            rpc,
            acc.account,
            info.usdc,
            recipient,
            amount_units,
            info.decimals,
            "USDC",
        )
        logger.info(f"Deposit network fee on {network}: {fee:,.8f} native")
        logger.info(f"Deposit transaction on {network}: {tx_hash}")
        await wait_receipt(rpc, tx_hash, DEPOSIT_RECEIPT_TIMEOUT_SEC)
        logger.info(f"Deposit transaction confirmed on {network}: {tx_hash}")

        logger.info(f"Waiting for Lighter deposit credit: {acc.name}")
        await _wait_for_lighter_credit(acc, recipient, tx_hash)

    return tx_hash


def _random_deposit_target(cfg: LighterConfig) -> Decimal:
    assert cfg.deposit_target is not None
    spread = cfg.deposit_target * cfg.deposit_target_random_pct / 100
    if not spread:
        return cfg.deposit_target

    offset = Decimal(str(random.random())) * spread * 2
    return (cfg.deposit_target - spread + offset).quantize(Decimal("0.01"))


def _deposit_plan_title(cfg: LighterConfig, info: DepositNetwork, minimum: Decimal) -> str:
    assert cfg.deposit_target is not None
    spread = cfg.deposit_target * cfg.deposit_target_random_pct / 100
    target = f"${cfg.deposit_target:,.2f}"
    if spread:
        target = f"${cfg.deposit_target - spread:,.2f}–${cfg.deposit_target + spread:,.2f}"

    delay = format_duration(cfg.deposit_delay.min)
    if cfg.deposit_delay.min != cfg.deposit_delay.max:
        delay += f"–{format_duration(cfg.deposit_delay.max)}"

    return (
        f"Deposit plan: {cfg.deposit_network} (chain {info.chain_id}) · "
        f"target {target} · min deposit ${minimum:,.2f} · delay {delay}"
    )


async def _plan_deposit(
    acc: LighterClient,
    cfg: LighterConfig,
    info: DepositNetwork,
    minimum: Decimal,
) -> DepositPlanItem:
    balance = await acc.balance() if await acc.registered() else Decimal(0)
    wallet_balance, native_balance = await get_wallet_balances(
        info.chain_id,
        cfg.deposit_network,
        info.usdc,
        info.decimals,
        acc.address,
        acc.proxy,
    )
    random_target = _random_deposit_target(cfg)
    target = min(random_target, balance + wallet_balance)

    if balance >= random_target:
        return DepositPlanItem(
            acc, target, balance, wallet_balance, Decimal(0), "skip: target reached"
        )

    amount = min(target - balance, wallet_balance)
    if amount < minimum:
        status = f"skip: deposit <${minimum:,.2f}"
        return DepositPlanItem(acc, target, balance, wallet_balance, Decimal(0), status)

    fee = await estimate_network_fee(
        info.chain_id,
        cfg.deposit_network,
        ERC20_TRANSFER_GAS_LIMIT,
        acc.proxy,
    )
    if native_balance < fee:
        return DepositPlanItem(
            acc, target, balance, wallet_balance, Decimal(0), "skip: insufficient gas"
        )

    return DepositPlanItem(acc, target, balance, wallet_balance, amount, "deposit")


def _deposit_plan_table(title: str) -> AutoTable:
    tbl = AutoTable(
        Column("Account", justify="left"),
        Column("Target", "${:,.2f}"),
        Column("Lighter", "${:,.2f}"),
        Column("Wallet", "${:,.2f}"),
        Column("Deposit", "${:,.2f}", total=sum),
        Column("Status", justify="left"),
    )
    tbl.title = title
    return tbl


async def deposit_funds(accs: list[LighterClient], cfg: LighterConfig) -> None:
    if cfg.deposit_target is None:
        raise AppError("Set deposit_target in config")
    if not accs:
        logger.info("No accounts configured for deposit")
        return

    info = await accs[0].deposit_network(cfg.deposit_network)
    minimum = max(cfg.deposit_min_amount, info.min_amount)
    rows = [await _plan_deposit(acc, cfg, info, minimum) for acc in accs]
    tbl = _deposit_plan_table(_deposit_plan_title(cfg, info, minimum))
    for row in rows:
        tbl.add_row(
            row.account.name,
            row.target,
            row.lighter_balance,
            row.wallet_balance,
            row.amount,
            row.status,
        )

    tbl.print()

    plan = [row for row in rows if row.amount > 0]
    if not plan:
        logger.info("No deposits planned")
        return

    if not confirm("Execute deposit plan?"):
        logger.info("Deposit cancelled")
        return

    for index, row in enumerate(plan):
        logger.info(
            f"Depositing {row.amount:,.2f} USDC via {cfg.deposit_network}: {row.account.name}"
        )
        tx_hash = await execute_deposit(row.account, row.amount, cfg.deposit_network)
        logger.info(f"Deposit completed: {row.account.name} {tx_hash}")
        if index < len(plan) - 1:
            wait = cfg.deposit_delay.sample()
            logger.info(f"Waiting {format_duration(wait)} before next deposit")
            await asyncio.sleep(wait)


async def main():
    cli = await create_cli(
        "lighter",
        "configs/lighter.toml",
        ["privkey"],
        custom_commands={"deposit": lambda _: None},
    )
    cfg = load_config(LighterConfig, cli.config)
    all_accs, act_accs = await create_clients(cli, cfg.accounts, LighterClient.from_config)

    try:
        match cli.command:
            case "info" | "stats":
                await print_info(all_accs)
            case "close":
                await require_login(act_accs)
                await close_all(act_accs)
            case "trade":
                await require_login(act_accs)
                await run_groups(cfg, act_accs)
            case "positions":
                await print_positions(act_accs)
            case "deposit":
                await deposit_funds(act_accs, cfg)
    finally:
        await asyncio.gather(*(acc.close() for acc in all_accs))


if __name__ == "__main__":
    run_app(main())
