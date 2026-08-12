# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Crafted with love and ctrl+c
import asyncio
from collections import defaultdict
from decimal import Decimal

from clients.ethereal import EtherealClient, EtherealPoint, EtherealPosition
from lib.cli import create_cli, create_clients, run_app
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr, to_period_day
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

# MARK: Storages


async def sync_trades(acc: EtherealClient, ttl: int) -> list[EtherealPosition]:
    store_path = f".cache/ethereal_{short_addr(acc.address)}_trades.pkl"
    store = DataStore(store_path, id_key="id", model=EtherealPosition)
    await store.sync(lambda _: acc.raw_positions(open_only=False), ttl_sec=ttl)
    return store.get_all()


async def sync_points(acc: EtherealClient, ttl: int) -> list[EtherealPoint]:
    store_path = f".cache/ethereal_{short_addr(acc.address)}_points.pkl"
    store = DataStore(store_path, id_key="id", model=EtherealPoint)
    await store.sync(lambda _: acc.points(), ttl_sec=ttl)
    return store.get_all()


# MARK: Reports


async def print_info(accs: list[EtherealClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.0f}", total=sum),
        Column("P/Price", "{:,.4f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
    )

    async def row(acc: EtherealClient):
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0)
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance)

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()


async def print_stats(accs: list[EtherealClient], period="week", filter_period="all", force=False):
    gpos: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    gpts: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    ttl = 0 if force else 3600

    period_fn = to_period_day if period == "day" else EtherealClient.to_week_label

    all_trades, all_points = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_trades(acc, ttl)),
        gather_accs(accs, lambda acc: sync_points(acc, ttl)),
    )
    for acc, trades in zip(accs, all_trades):
        for t in trades:
            gpos[period_fn(t.created_at)][acc.name].append(t)
    for acc, pts in zip(accs, all_points):
        for p in pts:
            gpts[period_fn(p.started_at)][acc.name] += p.total_points

    all_periods = sorted(gpos.keys() | gpts.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [x.name for x in accs]

    periods_data: dict[str, list[PeriodRow]] = {}
    for p in all_periods:
        acc_names = [n for n in all_names if n in (gpos[p].keys() | gpts[p].keys())]
        rows = []
        for acc_name in acc_names:
            positions = gpos[p][acc_name]
            pts = gpts.get(p, {}).get(acc_name, Decimal(0))
            if not positions and pts < 1:
                continue
            vol = sum((pos.total_inc + pos.total_dec for pos in positions), Decimal(0))
            fee = sum((pos.fees_usd for pos in positions), Decimal(0))
            fnd = sum((pos.funding_usd for pos in positions), Decimal(0))
            pnl = sum((pos.realized_pnl for pos in positions), Decimal(0)) - fee - fnd
            rows.append(PeriodRow(acc_name, len(positions), vol, -pnl, pts, fee))
        periods_data[p] = rows

    render_stats(periods_data, periods_to_show, points_fmt="{:,.0f}", pprice_fmt="{:,.4f}")


# MARK: Main


async def main():
    cli = await create_cli("ethereal", "configs/ethereal.toml", ["privkey"])
    cfg = StrategyConfig.load(cli.config)

    all_accs, act_accs = await create_clients(cli, cfg.accounts, EtherealClient.from_config)

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "stats":
            await print_stats(all_accs, period=cli.group, filter_period=cli.filter, force=cli.force)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_groups(cfg, act_accs)
        case "positions":
            await print_positions(act_accs)


if __name__ == "__main__":
    run_app(main())
