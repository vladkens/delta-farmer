# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Refactoring is just future procrastination
import asyncio
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import TypeVar

from clients.nado import NadoClient, NadoPoint, NadoTrade
from lib.cli import create_cli, create_clients, run_app
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

T = TypeVar("T")
DD = defaultdict[str, defaultdict[str, T]]

# MARK: Storages


async def sync_trades(acc: NadoClient, ttl: int) -> list[NadoTrade]:
    store_path = f".cache/nado_{short_addr(acc.address)}_trades.pkl"
    store = DataStore(store_path, id_key="digest", model=NadoTrade)
    await store.sync(lambda since: acc.trades(since), ttl_sec=ttl)
    return store.get_all()


async def sync_points(acc: NadoClient, ttl: int) -> list[NadoPoint]:
    store_path = f".cache/nado_{short_addr(acc.address)}_points.pkl"
    store = DataStore(store_path, id_key="since", model=NadoPoint)
    await store.sync(lambda _: acc.points(), ttl_sec=ttl)
    return store.get_all()


# MARK: Reports


async def print_info(accs: list[NadoClient]):
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

    async def row(acc: NadoClient):
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0)
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance)

    for r in await asyncio.gather(*[row(acc) for acc in accs]):
        tbl.add_row(*r)

    tbl.print()


async def print_stats(accs: list[NadoClient], period="week", filter_period="all", force=False):
    gtrades: DD[list[NadoTrade]] = defaultdict(lambda: defaultdict(list))
    gpoints: DD[Decimal] = defaultdict(lambda: defaultdict(Decimal))
    ttl = 0 if force else 3600

    all_trades, all_points = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_trades(acc, ttl)),
        gather_accs(accs, lambda acc: sync_points(acc, ttl)),
    )

    def period_fn(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d") if period == "day" else NadoClient.to_week_label(dt)

    for acc, trades in zip(accs, all_trades):
        for t in trades:
            gtrades[period_fn(t.created_at)][acc.name].append(t)
    for acc, pts in zip(accs, all_points):
        for p in pts:
            gpoints[period_fn(p.since)][acc.name] = p.points

    all_periods = sorted(gtrades.keys() | gpoints.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [x.name for x in accs]

    periods_data: dict[str, list[PeriodRow]] = {}
    for pk in all_periods:
        acc_names = [n for n in all_names if n in (gtrades[pk].keys() | gpoints[pk].keys())]
        rows = []
        for acc_name in acc_names:
            trades = gtrades[pk].get(acc_name, [])
            points = gpoints[pk].get(acc_name, Decimal(0))
            vol = sum((t.amount * t.price for t in trades), Decimal(0))
            pnl = sum((t.realized_pnl - t.fee for t in trades), Decimal(0))
            fee = sum((t.fee for t in trades), Decimal(0))
            rows.append(PeriodRow(acc_name, len(trades), vol, -pnl, points, fee))
        periods_data[pk] = rows

    render_stats(periods_data, periods_to_show, pprice_fmt="{:,.2f}")


# MARK: Main


async def main():
    cli = await create_cli("nado", "configs/nado.toml", ["privkey"])
    cfg = StrategyConfig.load(cli.config)

    all_accs, act_accs = await create_clients(cli, cfg.accounts, NadoClient.from_config)

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
