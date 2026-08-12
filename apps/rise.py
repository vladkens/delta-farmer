# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
from collections import defaultdict
from decimal import Decimal

from clients.rise import RiseClient, RiseTrade
from lib.cli import create_cli, create_clients, run_app
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr, to_period_day
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

# MARK: Storages


async def sync_trades(acc: RiseClient, ttl: int) -> list[RiseTrade]:
    store_path = f".cache/rise_{short_addr(acc.address)}_trades.pkl"
    store = DataStore(store_path, id_key="id", model=RiseTrade)
    await store.sync(lambda since: acc.trades(since=since), ttl_sec=ttl)
    return store.get_all()


# MARK: Reports


async def print_info(accs: list[RiseClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.0f}", total=sum),
        Column("P/Price", "{:,.4f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
        Column("Rank", justify="right"),
    )

    async def row(acc: RiseClient):
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0, None)
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance, p.rank)

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()


async def print_stats(accs: list[RiseClient], period="week", filter_period="all", force=False):
    gcnt = defaultdict(lambda: defaultdict(int))
    gvol = defaultdict(lambda: defaultdict(Decimal))
    gpnl = defaultdict(lambda: defaultdict(Decimal))
    gfee = defaultdict(lambda: defaultdict(Decimal))

    period_fn = to_period_day if period == "day" else RiseClient.to_week_label
    ttl = 0 if force else 3600

    all_trades = await gather_accs(accs, lambda acc: sync_trades(acc, ttl))

    for acc, trades in zip(accs, all_trades):
        for trade in trades:
            pk = period_fn(trade.created_at)
            gvol[pk][acc.name] += trade.volume
            gpnl[pk][acc.name] += trade.realized_pnl
            gfee[pk][acc.name] += trade.fee
            gcnt[pk][acc.name] += 1

    all_periods = sorted(gvol.keys() | gpnl.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [x.name for x in accs]

    periods_data: dict[str, list[PeriodRow]] = {}
    for pk in all_periods:
        rows = []
        for name in all_names:
            cnt = gcnt[pk][name]
            vol = gvol[pk][name]
            pnl = gpnl[pk][name]
            fee = gfee[pk][name]
            if not vol and not pnl:
                continue
            rows.append(PeriodRow(name, cnt, vol, -pnl, Decimal(0), fee))
        periods_data[pk] = rows

    render_stats(periods_data, periods_to_show, points_fmt="{:,.0f}", pprice_fmt="{:,.4f}")


async def main():
    cli = await create_cli("rise", "configs/rise.toml", ["privkey"])
    cfg = StrategyConfig.load(cli.config)

    all_accs, act_accs = await create_clients(cli, cfg.accounts, RiseClient.from_config)

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "positions":
            await print_positions(act_accs)
        case "stats":
            await print_stats(all_accs, period=cli.group, filter_period=cli.filter, force=cli.force)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_groups(cfg, act_accs)


if __name__ == "__main__":
    run_app(main())
