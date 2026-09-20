# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import argparse
import asyncio
from decimal import Decimal

from clients.lighter import LighterClient
from lib.cli import create_cli, run_app, setup_app
from lib.errors import AppError
from lib.logger import logger
from lib.models import TimeRange
from lib.table import AutoTable, Column
from lib.utils import format_duration, gather_accs, short_addr
from strategy import load_config
from strategy.deposit import DepositConfig, run_deposits
from strategy.runner import close_all, print_positions, run_groups, select_strategy
from strategy.withdrawal import run_withdrawals


class LighterConfig(DepositConfig):
    pass


def setup_useref_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("code", metavar="CODE", help="Referral code to apply")
    parser.add_argument("-a", "--account", metavar="NAME", help="Use one enabled account")


async def print_info(accs: list[LighterClient]):
    def total(values):
        return sum(value for value in values if value is not None)

    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=total),
        Column("Burn", "{:,.2f}", total=total),
        Column("Points", "{:,.6f}", total=total),
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
            return ("○", acc.name, a, None, None, None, await acc.balance(), None)

        p = await acc.profile()
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance, p.ref_code)

    items = await gather_accs(accs, row)
    for item in items:
        tbl.add_row(*item)

    tbl.print()
    missing_refs = [item[1] for item in items if item[0] == "✓" and not item[-1]]
    if missing_refs:
        cmd = "uv run apps/lighter.py"
        print()
        print(f"Referral code missing: {', '.join(missing_refs)}")
        print(f"  All enabled: {cmd} useref CODE")
        print(f"  One account: {cmd} useref CODE -a {missing_refs[0]}")


async def require_login(accs: list[LighterClient]) -> None:
    ready = await asyncio.gather(*(acc.auth_ready() for acc in accs))
    missing = [acc.name for acc, ok in zip(accs, ready) if not ok]
    if missing:
        names = ", ".join(missing)
        raise AppError(f"Login required: {names}. Run: uv run apps/lighter.py login")


async def apply_referral_code(
    accounts: list[LighterClient],
    code: str,
    delay: TimeRange,
) -> None:
    code = code.strip()
    if not code:
        raise AppError("Referral code cannot be empty")

    for index, account in enumerate(accounts):
        progress = f"{index + 1}/{len(accounts)}"
        with logger.contextualize(account=account.name, progress=progress):
            await account.use_referral_code(code)
            logger.success("Referral code applied")

        if index < len(accounts) - 1:
            wait = delay.sample()
            logger.info(f"Waiting {format_duration(wait)} before next account")
            await asyncio.sleep(wait)


def _select_account(
    all_accs: list[LighterClient],
    act_accs: list[LighterClient],
    name: str | None,
) -> list[LighterClient]:
    if name is None:
        return act_accs

    account = next((acc for acc in all_accs if acc.name == name), None)
    if account is None:
        raise AppError(f"Account not found: {name}")
    if account not in act_accs:
        raise AppError(f"Account is disabled: {name}")

    return [account]


async def main():
    cli = await create_cli(
        "lighter",
        "configs/lighter.toml",
        ["privkey"],
        custom_commands={"useref": setup_useref_cli},
    )
    cfg = load_config(LighterConfig, cli.config)
    all_accs, act_accs = await setup_app(cli, cfg, LighterClient.from_config)

    try:
        match cli.command:
            case "info" | "stats":
                await print_info(all_accs)
            case "close":
                await require_login(act_accs)
                await close_all(act_accs)
            case "trade":
                strategy, accounts = select_strategy(cfg, act_accs, cli.pool)
                await require_login(accounts)
                await run_groups(strategy, accounts)
            case "positions":
                await print_positions(act_accs)
            case "deposit":
                accounts = _select_account(all_accs, act_accs, cli.account)
                # The first deposit registers the Lighter account; login is only possible afterward.
                await run_deposits(accounts, cfg)
            case "withdraw":
                accounts = _select_account(all_accs, act_accs, cli.account)
                await require_login(accounts)
                await run_withdrawals(accounts, cfg, withdraw_full=cli.full)
            case "useref":
                accounts = _select_account(all_accs, act_accs, cli.account)
                await apply_referral_code(accounts, cli.code, cfg.balance_transfer_delay)
    finally:
        await asyncio.gather(*(acc.close() for acc in all_accs))


if __name__ == "__main__":
    run_app(main())
