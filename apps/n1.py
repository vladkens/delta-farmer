# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

from clients.n1 import N1Client, N1Point
from lib.cli import create_cli, create_clients, run_app
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr, to_period_day
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

T = TypeVar("T")
DD = defaultdict[str, defaultdict[str, T]]

# MARK: Storages


async def sync_points(acc: N1Client, ttl: int) -> list[N1Point]:
    store_path = f".cache/n1_{short_addr(acc.address)}_points.pkl"
    store = DataStore(store_path, id_key="stage", model=N1Point)
    await store.sync(lambda _: acc.points_history(), ttl_sec=ttl)
    return store.get_all()


async def sync_trades(acc: N1Client, role: str, acc_id: int, ttl: int) -> list[dict]:
    fee_key = f"{role}Fee"
    store_path = f".cache/n1_{short_addr(acc.address)}_trades_{role}.pkl"
    store = DataStore(store_path, id_key="uid")
    needs_backfill = any(t.get(fee_key) is None for t in store.get_all())
    path = f"/trades?{role}Id={acc_id}"
    await store.sync(
        lambda since: acc.paged(path, since=None if needs_backfill else since),
        ttl_sec=0 if needs_backfill else ttl,
    )
    return store.get_all()


async def sync_pnl(acc: N1Client, acc_id: int, ttl: int) -> list[dict]:
    store_path = f".cache/n1_{short_addr(acc.address)}_pnl.pkl"
    store = DataStore(store_path, id_key="day")
    await store.sync(lambda _: acc.daily_pnl(acc_id), ttl_sec=ttl)
    return store.get_all()


async def _fetch_stats(acc: N1Client, ttl: int) -> dict:
    _, acc_id = await acc._ensure_session()
    maker, taker, pnl = await asyncio.gather(
        sync_trades(acc, "maker", acc_id, ttl),
        sync_trades(acc, "taker", acc_id, ttl),
        sync_pnl(acc, acc_id, ttl),
    )

    for t in maker:
        t["fee"] = Decimal(str(t["makerFee"]))
    for t in taker:
        t["fee"] = Decimal(str(t["takerFee"]))

    trades = maker + taker
    return {"trades": trades, "pnl": pnl}


# MARK: Reports


async def print_info(accs: list[N1Client]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.2f}", total=sum),
        Column("P/Price", "{:,.3f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
    )

    async def row(acc: N1Client):
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, None, None, None, None)
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance)

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()


async def print_stats(accs: list[N1Client], period="week", filter_period="all", force=False):
    ttl = 0 if force else 3600

    def period_fn(dt: datetime) -> str:
        return to_period_day(dt) if period == "day" else N1Client.to_week_label(dt)

    stats_list = await gather_accs(accs, lambda acc: _fetch_stats(acc, ttl))
    all_points = await gather_accs(accs, lambda acc: sync_points(acc, ttl))

    gcnt: DD[int] = defaultdict(lambda: defaultdict(int))
    gpnl: DD[Decimal] = defaultdict(lambda: defaultdict(Decimal))
    gfee: DD[Decimal] = defaultdict(lambda: defaultdict(Decimal))
    gpts: DD[Decimal] = defaultdict(lambda: defaultdict(Decimal))
    gvol: DD[Decimal] = defaultdict(lambda: defaultdict(Decimal))

    for acc, stats in zip(accs, stats_list):
        for t in stats["pnl"]:
            dt = datetime.fromisoformat(t["day"]).replace(tzinfo=UTC)
            gpnl[period_fn(dt)][acc.name] += Decimal(str(t["pnl"]))

        for t in stats["trades"]:
            dt = datetime.fromisoformat(t["time"].rstrip("Z")).replace(tzinfo=UTC)
            gfee[period_fn(dt)][acc.name] += Decimal(str(t["fee"]))
            gvol[period_fn(dt)][acc.name] += Decimal(str(t["price"])) * Decimal(str(t["baseSize"]))
            gcnt[period_fn(dt)][acc.name] += 1

    for acc, pts in zip(accs, all_points):
        for p in pts:
            gpts[period_fn(p.start_window)][acc.name] += p.points

    all_periods = sorted(gpnl.keys() | gfee.keys() | gpts.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [acc.name for acc in accs]

    periods_data: dict[str, list[PeriodRow]] = {}
    for pk in all_periods:
        rows = []
        for name in all_names:
            cnt = gcnt[pk].get(name, 0)
            pts = gpts[pk].get(name, Decimal(0))
            pnl = gpnl[pk].get(name, Decimal(0))
            fee = gfee[pk].get(name, Decimal(0))
            vol = gvol[pk].get(name, Decimal(0))

            if not vol and not pts:
                continue

            rows.append(PeriodRow(name, cnt, vol, -pnl, pts, fee))
        periods_data[pk] = rows

    render_stats(periods_data, periods_to_show, pprice_fmt="{:,.3f}")


# MARK: Main


async def main():
    cli = await create_cli("n1", "configs/n1.toml", ["privkey"])
    cfg = StrategyConfig.load(cli.config)

    all_accs, act_accs = await create_clients(cli, cfg.accounts, N1Client.from_config)

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
