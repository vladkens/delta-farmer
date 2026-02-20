# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | May contain traces of genius
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial
from typing import TypeVar

from core.cli import create_cli
from core.store import DataStore
from core.table import AutoTable, Column
from core.utils import parse_filter, short_addr, to_period_day, to_period_week

from .client import Client, Trade
from .config import Config
from .manager import Manager

# https://docs.pacifica.fi/points-program
GENESIS = datetime(2025, 9, 4, tzinfo=timezone.utc)

T = TypeVar("T")
DD = defaultdict[str, defaultdict[str, T]]


def load_accs(cfg: Config) -> list[Client]:
    return [Client.from_config(acc) for acc in cfg.accounts]


async def print_info(accs: list[Client]):
    tbl = AutoTable(
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.1f}", total=sum),
        Column("P/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
    )

    for acc in accs:
        (eqt, pnl), vol, pts = await asyncio.gather(
            acc.portfolio(), acc.total_volume(), acc.points()
        )
        tbl.add_row(
            acc.name, short_addr(str(acc.keypair.pubkey()), 4, 4), vol, -pnl, pts.points, eqt
        )

    tbl.print()


async def sync_trades(acc: Client, ttl_sec: int = 3600) -> list[Trade]:
    store_path = f".cache/pacifica_{short_addr(str(acc.keypair.pubkey()), 4, 4)}_trades.pkl"
    store = DataStore(store_path, id_key="history_id")

    async def fetch_trades(since: datetime | None) -> list[dict]:
        trades = await acc.trades(since)
        return [t.model_dump(by_alias=True) for t in trades]

    await store.sync(fetch_trades, ttl_sec=ttl_sec)
    return [Trade(**t) for t in store.get_all()]


async def print_stats(accs: list[Client], period="week", filter_period="all", force=False):
    gtrades: DD[list[Trade]] = defaultdict(lambda: defaultdict(list))
    gpoints: DD[Decimal] = defaultdict(lambda: defaultdict(Decimal))

    period_fn = to_period_day if period == "day" else partial(to_period_week, genesis=GENESIS)
    ttl = 0 if force else 3600

    for acc in accs:
        trades = await sync_trades(acc, ttl)
        for trade in trades:
            period_key = period_fn(trade.created_at)
            gtrades[period_key][acc.name].append(trade)

        points = await acc.points_history()
        for week, point in points.items():
            gpoints[week][acc.name] = point

    all_periods = sorted(gtrades.keys())
    periods_to_show = parse_filter(filter_period, all_periods)

    tbl = AutoTable(
        Column("Account", justify="left"),
        Column("Trades", "{:,}", total=sum),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.1f}", total=sum),
        Column("P/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("V/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Volume"] * Decimal(1e5)),
        Column("Fees", "{:,.2f}", total=sum),
        Column("Fee, %", "{:.3%}", compute=lambda r: r["Fees"] / r["Volume"]),
        Column("Total Vol", "{:,.0f}", total=sum, grand_total=False),
    )

    tvol = defaultdict(Decimal)
    for period_key in periods_to_show:
        tbl.subgroup(f"{period_key}")
        for acc_name in sorted(gtrades[period_key].keys()):
            trades = gtrades[period_key][acc_name]
            points = gpoints.get(period_key, {}).get(acc_name, Decimal(0))

            vol = sum(trade.amount * trade.price for trade in trades)
            pnl = sum(trade.pnl for trade in trades)
            fee = sum(trade.fee for trade in trades)
            tvol[acc_name] += vol
            tbl.add_row(acc_name, len(trades), vol, -pnl, points, fee, tvol[acc_name])

    tbl.print()


async def main():
    cli = create_cli("pacifica", "configs/pacifica.toml", ["privkey"])

    cfg = Config.load(cli.config)
    accs = load_accs(cfg)

    match cli.command:
        case "info":
            await print_info(accs)
        case "stats":
            await print_stats(accs, period=cli.group, filter_period=cli.filter, force=cli.sync)
        case "close":
            await Manager(cfg).close()
        case "trade":
            await Manager(cfg).run_trade()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
