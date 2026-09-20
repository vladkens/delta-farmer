# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from decimal import Decimal

from clients.lighter import LighterClient
from lib.cli import create_cli, run_app, setup_app
from lib.errors import AppError
from lib.table import AutoTable, Column
from lib.utils import gather_accs, short_addr
from strategy import load_config
from strategy.deposit import DepositConfig, run_deposits
from strategy.runner import close_all, print_positions, run_groups
from strategy.withdrawal import run_withdrawals


class LighterConfig(DepositConfig):
    pass


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

    for item in await gather_accs(accs, row):
        tbl.add_row(*item)

    tbl.print()


async def require_login(accs: list[LighterClient]) -> None:
    ready = await asyncio.gather(*(acc.auth_ready() for acc in accs))
    missing = [acc.name for acc, ok in zip(accs, ready) if not ok]
    if missing:
        names = ", ".join(missing)
        raise AppError(f"Login required: {names}. Run: uv run apps/lighter.py login")


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
                await require_login(act_accs)
                await run_groups(cfg, act_accs)
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
    finally:
        await asyncio.gather(*(acc.close() for acc in all_accs))


if __name__ == "__main__":
    run_app(main())
