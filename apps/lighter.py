# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
from clients.lighter import LighterClient
from lib.cli import create_cli, create_clients, run_app
from lib.table import AutoTable, Column
from lib.utils import gather_accs, short_addr
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups


async def print_info(accs: list[LighterClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.2f}", total=sum),
        Column("P/Price", "{:,.4f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
    )

    async def row(acc: LighterClient):
        p = await acc.profile()
        a = short_addr(acc.address)
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance)

    for item in await gather_accs(accs, row):
        tbl.add_row(*item)

    tbl.print()


async def main():
    cli = await create_cli("lighter", "configs/lighter.toml", ["privkey"])
    cfg = StrategyConfig.load(cli.config)
    all_accs, act_accs = await create_clients(cli, cfg.accounts, LighterClient.from_config)

    match cli.command:
        case "info" | "stats":
            await print_info(all_accs)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_groups(cfg, act_accs)
        case "positions":
            await print_positions(act_accs)


if __name__ == "__main__":
    run_app(main())
