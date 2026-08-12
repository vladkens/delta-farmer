# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

from clients.hyperliquid import migrate_hyperliquid_accounts, warn_legacy_hyperliquid_accounts
from clients.onyx import OnyxClient, OnyxPoint
from lib.cli import create_cli, create_clients, run_app
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr, to_period_day
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

T = TypeVar("T")
DD = defaultdict[str, defaultdict[str, T]]

# MARK: Storages


async def sync_fills(acc: OnyxClient, ttl: int) -> list[dict]:
    store_path = f".cache/onyx_{short_addr(acc.address)}_fills.pkl"
    store = DataStore(store_path, id_key="hash")
    await store.sync(acc.fetch_fills, ttl_sec=ttl)
    return store.get_all()


async def sync_points(acc: OnyxClient, ttl: int) -> list[OnyxPoint]:
    store_path = f".cache/onyx_{short_addr(acc.address)}_points.pkl"
    store = DataStore(store_path, id_key="start_window", model=OnyxPoint)
    await store.sync(lambda _: acc.points(), ttl_sec=ttl)
    return store.get_all()


# MARK: Reports


def _calc_burn(fills: list[dict]) -> Decimal:
    pnl = sum((Decimal(str(f.get("closedPnl", 0))) for f in fills), Decimal(0))
    fee = sum((Decimal(str(f.get("fee", 0))) for f in fills), Decimal(0))
    return -(pnl - fee)


def _calc_vol(fills: list[dict]) -> Decimal:
    return sum((Decimal(str(f["px"])) * Decimal(str(f["sz"])) for f in fills), Decimal(0))


async def print_info(accs: list[OnyxClient], force: bool = False):
    ttl = 0 if force else 3600
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.1f}", total=sum),
        Column("P/Price", "{:,.4f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
    )

    vol_rows: list[tuple[Decimal, Decimal]] = []
    legacy_accounts: list[str] = []

    async def row(acc: OnyxClient):
        if not await acc.registered():
            a = short_addr(acc.address)
            vol_rows.append((Decimal(0), Decimal(0)))
            return ("✗", acc.name, a, 0, 0, 0, 0)

        p, fills = await asyncio.gather(acc.profile(), sync_fills(acc, ttl))
        a = short_addr(acc.address)
        burn = _calc_burn(fills)
        fills_vol = _calc_vol(fills)
        vol_rows.append((p.volume, fills_vol))
        if p.mode != "unifiedAccount":
            legacy_accounts.append(acc.name)
        return ("✓", acc.name, a, p.volume, burn, p.points, p.balance)

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()
    if vol_rows:
        arjuna_vol = sum(v[0] for v in vol_rows)
        fills_vol = sum(v[1] for v in vol_rows)
        diff = fills_vol - arjuna_vol
        pct = (diff / arjuna_vol * 100) if arjuna_vol else Decimal(0)
        print(
            "* Fills computed indirectly (no native Onyx API). "
            f"Volume diff vs Arjuna: {diff:+,.0f} ({pct:+.2f}%)"
        )
    warn_legacy_hyperliquid_accounts(legacy_accounts, "Onyx")


async def print_stats(
    accs: list[OnyxClient], period: str = "week", filter_period: str = "all", force: bool = False
):
    ttl = 0 if force else 3600
    fills_list, points_list = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_fills(acc, ttl)),
        gather_accs(accs, lambda acc: sync_points(acc, ttl)),
    )

    period_fn = to_period_day if period == "day" else OnyxClient.to_week_label
    gtrades: DD[list[dict]] = defaultdict(lambda: defaultdict(list))
    gpoints: DD[Decimal] = defaultdict(lambda: defaultdict(Decimal))

    for acc, fills in zip(accs, fills_list):
        for fill in fills:
            dt = datetime.fromtimestamp(fill["time"] / 1000, tz=UTC)
            gtrades[period_fn(dt)][acc.name].append(fill)

    for acc, points in zip(accs, points_list):
        for point in points:
            gpoints[period_fn(point.start_window)][acc.name] += point.points

    all_periods = sorted(gtrades.keys() | gpoints.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [acc.name for acc in accs]

    periods_data: dict[str, list[PeriodRow]] = {}
    for pk in all_periods:
        rows = []
        for name in all_names:
            fills = gtrades[pk].get(name, [])
            points = gpoints[pk].get(name, Decimal(0))
            if not fills and not points:
                continue
            vol = sum((Decimal(str(f["px"])) * Decimal(str(f["sz"])) for f in fills), Decimal(0))
            fee = sum((Decimal(str(f["fee"])) for f in fills), Decimal(0))
            pnl = sum((Decimal(str(f.get("closedPnl", 0))) for f in fills), Decimal(0))
            rows.append(PeriodRow(name, len(fills), vol, -pnl, points, fee))
        periods_data[pk] = rows

    render_stats(periods_data, periods_to_show, pprice_fmt="{:,.2f}")


# MARK: Main


async def main():
    cli = await create_cli(
        "onyx", "configs/onyx.toml", ["privkey"], custom_commands={"migrate": lambda _: None}
    )
    cfg = StrategyConfig.load(cli.config)

    all_accs, act_accs = await create_clients(cli, cfg.accounts, OnyxClient.from_config)
    for c in act_accs:
        c._symbols = cfg.symbols

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "positions":
            await print_positions(act_accs)
        case "close":
            await close_all(act_accs)
        case "migrate":
            await migrate_hyperliquid_accounts(all_accs, "Onyx")
        case "stats":
            await print_stats(all_accs, period=cli.group, filter_period=cli.filter, force=cli.force)
        case "trade":
            await run_groups(cfg, act_accs)


if __name__ == "__main__":
    run_app(main())
