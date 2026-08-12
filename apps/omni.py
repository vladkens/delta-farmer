# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | No AI was harmed making this
import argparse
import asyncio
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import Field, SecretStr

from clients.omni import OmniClient, OmniCompetitionStatus, OmniPoint
from lib.cli import create_cli, create_clients, run_app
from lib.http import ApiError
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr, to_period_day
from strategy import StrategyConfig, load_config
from strategy.runner import close_all, print_positions, run_groups


class OmniConfig(StrategyConfig):
    captcha_key: SecretStr = Field(default=SecretStr(""), repr=False)

    @classmethod
    def load(cls, filepath: str) -> Self:
        return load_config(cls, filepath)


# MARK: Storages


async def sync_raw(acc: OmniClient, endpoint: str, ttl: int) -> list[dict]:
    store_name = endpoint.strip("/").replace("/", "_")
    store_path = f".cache/omni_{short_addr(acc.address)}_{store_name}.pkl"
    store = DataStore(store_path, id_key="id")
    await store.sync(lambda since: acc.fetch_history(endpoint, since=since), ttl)
    return store.get_all()


async def sync_points(acc: OmniClient, ttl: int) -> list[OmniPoint]:
    store_path = f".cache/omni_{short_addr(acc.address)}_points.pkl"
    store = DataStore(store_path, id_key="start_window", model=OmniPoint)
    await store.sync(lambda _: acc.points(), ttl_sec=ttl)
    return store.get_all()


# MARK: Reports


async def print_info(accs: list[OmniClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.1f}", total=sum),
        Column("P/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
        Column("Rank", justify="right"),
        Column("Ref", justify="left"),
    )

    async def row(acc: OmniClient):
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0, "", "")
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance, p.rank, p.ref_code)

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()


async def print_stats(accs: list[OmniClient], period="week", filter_period="all", force=False):
    gcnt = defaultdict(lambda: defaultdict(int))
    gpnl = defaultdict(lambda: defaultdict(Decimal))
    gvol = defaultdict(lambda: defaultdict(Decimal))
    gpts = defaultdict(lambda: defaultdict(Decimal))

    period_fn = to_period_day if period == "day" else OmniClient.to_week_label
    ttl = 0 if force else 3600

    all_transfers, all_trades, all_points = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_raw(acc, "/transfers", ttl)),
        gather_accs(accs, lambda acc: sync_raw(acc, "/trades", ttl)),
        gather_accs(accs, lambda acc: sync_points(acc, ttl)),
    )
    for acc, transfers, trades, points in zip(accs, all_transfers, all_trades, all_points):
        transfers = [t for t in transfers if t["status"] == "confirmed"]
        transfers = [t for t in transfers if t["transfer_type"] in ("funding", "realized_pnl")]
        trades = [t for t in trades if t["status"] == "confirmed"]

        for p in points:
            week = period_fn(p.start_window)
            gpts[week][acc.name] = p.total_points

        for t in transfers:
            p = period_fn(datetime.fromisoformat(t["created_at"]))
            gpnl[p][acc.name] += Decimal(t["qty"])

        for t in trades:
            p = period_fn(datetime.fromisoformat(t["created_at"]))
            usd_value = Decimal(t["price"]) * Decimal(t["qty"])
            gvol[p][acc.name] += usd_value
            gcnt[p][acc.name] += 1

    all_periods = sorted(gpnl.keys() | gvol.keys() | gpts.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [x.name for x in accs]

    periods_data: dict[str, list[PeriodRow]] = {}
    for p in all_periods:
        acc_names = [
            n for n in all_names if n in (gpnl[p].keys() | gvol[p].keys() | gpts[p].keys())
        ]
        rows = []
        for acc_name in acc_names:
            cnt = gcnt[p][acc_name] or 0
            pnl = gpnl[p][acc_name] or Decimal(0)
            vol = gvol[p][acc_name] or Decimal(0)
            pts = gpts[p][acc_name] or Decimal(0)
            rows.append(PeriodRow(acc_name, cnt, vol, -pnl, pts, Decimal(0)))
        periods_data[p] = rows

    render_stats(periods_data, periods_to_show, fees=False, points_fmt="{:,.2f}")


def setup_competition_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--join", action="store_true", help="Join competition with all accounts")


CompetitionRow = tuple[OmniClient, OmniCompetitionStatus | None, str | None]


def _print_competition_summary(status: OmniCompetitionStatus | None) -> None:
    if status is None:
        print("No Omni competition status available.")
        return

    if not status.ongoing:
        print(
            f"No active Omni competition. Last/next window: "
            f"{status.start_time:%Y-%m-%d %H:%M UTC} → {status.end_time:%Y-%m-%d %H:%M UTC}."
        )
        return

    print(
        f"Active Omni competition: "
        f"{status.start_time:%Y-%m-%d %H:%M UTC} → {status.end_time:%Y-%m-%d %H:%M UTC}; "
        f"eligibility threshold ${status.volume_threshold:,.0f} volume."
    )


def _competition_status_table(rows: list[CompetitionRow]) -> None:
    first_status = next((status for _acc, status, _error in rows if status is not None), None)
    _print_competition_summary(first_status)

    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Joined", justify="left"),
        Column("Eligible", justify="left"),
        Column("Volume", "{:,.0f}"),
        Column("Volume Place", "{:,}"),
        Column("PnL", "{:,.2f}"),
        Column("PnL Place", "{:,}"),
        Column("ROI", "{:.2f}%"),
        Column("ROI Place", "{:,}"),
    )

    for acc, status, error in rows:
        user = status.user if status else None
        volume = user.volume_total if user and user.volume_total is not None else Decimal(0)
        eligible = bool(status is not None and volume >= status.volume_threshold)
        tbl.add_row(
            "✗" if error else "✓",
            acc.name,
            short_addr(acc.address),
            "yes" if user else "no",
            "yes" if eligible else "no" if status and status.ongoing else "n/a",
            volume if user else None,
            user.volume_rank if user else None,
            user.pnl_total if user else None,
            user.pnl_rank if user else None,
            user.roi_total if user else None,
            user.roi_rank if user else None,
        )

    tbl.print()

    needs_join = any(status and status.ongoing and status.user is None for _a, status, _e in rows)
    if needs_join:
        print("* Some accounts have not joined. Run `uv run apps/omni.py competition --join`.")


async def print_competition_status(accs: list[OmniClient]) -> None:
    async def row(acc: OmniClient):
        try:
            return acc, await acc.competition_status(), None
        except ApiError as e:
            return acc, None, str(e)

    _competition_status_table(await gather_accs(accs, row))


async def join_competition(accs: list[OmniClient]) -> None:
    async def row(acc: OmniClient):
        try:
            status = await acc.competition_status()
            if status.ongoing:
                await acc.competition_opt_in()
                status = await acc.competition_status()
            return acc, status, None
        except ApiError as e:
            return acc, None, str(e)

    _competition_status_table(await gather_accs(accs, row))


# MARK: Main


async def main():
    cli = await create_cli(
        "omni",
        "configs/omni.toml",
        ["privkey", "captcha_key"],
        custom_commands={"competition": setup_competition_cli},
    )
    cfg = OmniConfig.load(cli.config)
    if key := cfg.captcha_key.get_secret_value():
        os.environ["CAPTCHA_KEY"] = key

    all_accs, act_accs = await create_clients(cli, cfg.accounts, OmniClient.from_config)

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
        case "competition":
            if cli.join:
                await join_competition(all_accs)
            else:
                await print_competition_status(all_accs)


if __name__ == "__main__":
    run_app(main())
