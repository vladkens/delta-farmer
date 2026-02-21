# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Don't blame me, blame the API docs
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial

from core.cli import create_cli
from core.store import DataStore
from core.table import AutoTable, Column
from core.utils import parse_filter, short_addr, to_period_day, to_period_week

from .client import Client, PointsRecord
from .config import Config
from .manager import Manager

# https://docs.variational.io/omni/rewards/points
# https://omni.variational.io/points (UI counts from -1 week)
GENESIS = datetime(2025, 12, 17 - 7, tzinfo=timezone.utc)


def load_accs(cfg: Config) -> list[Client]:
    return [Client.from_config(x) for x in cfg.accounts]


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
        bal, vol, pnl, pts = await asyncio.gather(
            acc.balance(), acc.total_volume(), acc.pnl(), acc.points()
        )
        tbl.add_row(acc.name, short_addr(acc.address), vol, -pnl, pts.total_points, bal)

    tbl.print()


async def sync_history(acc: Client, endpoint: str, ttl: int) -> DataStore:
    store_name = endpoint.strip("/").replace("/", "_")
    store_path = f".cache/omni_{short_addr(acc.address)}_{store_name}.pkl"
    store = DataStore(store_path, id_key="id")
    return await store.sync(lambda since: acc.fetch_history(endpoint, since=since), ttl)


async def sync_points(acc: Client, ttl: int) -> list[PointsRecord]:
    store_path = f".cache/omni_{short_addr(acc.address)}_points.pkl"
    store = DataStore(store_path, id_key="start_window")

    async def fetch(_: datetime | None) -> list[dict]:
        records = await acc.points_history()
        return [r.model_dump(mode="json") for r in records]

    await store.sync(fetch, ttl)
    return [PointsRecord(**r) for r in store.get_all()]


async def print_stats(accs: list[Client], period="week", filter_period="all", force=False):
    gcnt = defaultdict(lambda: defaultdict(int))
    gpnl = defaultdict(lambda: defaultdict(Decimal))
    gvol = defaultdict(lambda: defaultdict(Decimal))
    gpts = defaultdict(lambda: defaultdict(Decimal))

    period_fn = to_period_day if period == "day" else partial(to_period_week, genesis=GENESIS)
    ttl = 0 if force else 3600

    for acc in accs:
        transfers_store = await sync_history(acc, "/transfers", ttl)
        trades_store = await sync_history(acc, "/trades", ttl)
        points = await sync_points(acc, ttl)

        transfers = transfers_store.get_all()
        trades = trades_store.get_all()

        transfers = [t for t in transfers if t["status"] == "confirmed"]
        transfers = [t for t in transfers if t["transfer_type"] in ("funding", "realized_pnl")]
        trades = [t for t in trades if t["status"] == "confirmed"]

        for p in points:
            week = period_fn(int(p.start_window.timestamp() * 1000))
            gpts[week][acc.name] = p.total_points

        for t in transfers:
            p = datetime.fromisoformat(t["created_at"])
            p = period_fn(int(p.timestamp() * 1000))
            gpnl[p][acc.name] += Decimal(t["qty"])

        for t in trades:
            p = datetime.fromisoformat(t["created_at"])
            p = period_fn(int(p.timestamp() * 1000))
            usd_value = Decimal(t["price"]) * Decimal(t["qty"])
            gvol[p][acc.name] += usd_value
            gcnt[p][acc.name] += 1

    tbl = AutoTable(
        Column("Account", justify="left"),
        Column("Trades", "{:,}", total=sum),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.1f}", total=sum),
        Column("P/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("V/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Volume"] * Decimal(1e5)),
        Column("Total Vol", "{:,.0f}", total=sum, grand_total=False),
    )

    all_periods = sorted(gpnl.keys() | gvol.keys() | gpts.keys())
    periods_to_show = parse_filter(filter_period, all_periods)

    tvol = defaultdict(Decimal)
    for p in periods_to_show:
        tbl.subgroup(f"{p}")
        acc_names = sorted(gpnl[p].keys() | gvol[p].keys() | gpts[p].keys())
        for acc_name in acc_names:
            cnt = gcnt[p][acc_name] or 0
            pnl = gpnl[p][acc_name] or 0
            vol = gvol[p][acc_name] or 0
            pts = gpts[p][acc_name] or 0
            tvol[acc_name] += vol
            tbl.add_row(acc_name, cnt, vol, -pnl, pts, tvol[acc_name])

    tbl.print()


async def main():
    cli = create_cli("omni", "configs/omni.toml", ["privkey"])

    cfg = Config.load(cli.config)
    accs = load_accs(cfg)

    match cli.command:
        case "info":
            await print_info(accs)
        case "stats":
            await print_stats(accs, period=cli.group, filter_period=cli.filter, force=cli.sync)
        case "trade":
            await Manager(cfg).run_trade()
        case "close":
            await Manager(cfg).close(accs)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
