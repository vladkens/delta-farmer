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
from lib.cli import create_cli, run_app, setup_app
from lib.errors import AppError
from lib.http import ApiError
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import confirm, gather_accs, parse_filter, short_addr, to_period_day
from strategy import load_config
from strategy.deposit import DepositConfig, run_deposits
from strategy.runner import close_all, print_positions, run_strategy
from strategy.withdrawal import run_withdrawals


class OmniConfig(DepositConfig):
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
        Column("Boost", "{:.0%}"),
        Column("Ref", justify="left"),
    )

    async def row(acc: OmniClient):
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0, "", 0, "")
        return (
            "✓",
            acc.name,
            a,
            p.volume,
            -p.pnl,
            p.points,
            p.balance,
            p.rank,
            p.referral_boost,
            p.ref_code,
        )

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
    parser.add_argument("--join", action="store_true", help=argparse.SUPPRESS)


def _select_account(
    all_accs: list[OmniClient],
    act_accs: list[OmniClient],
    name: str | None,
) -> list[OmniClient]:
    if name is None:
        return act_accs

    account = next((acc for acc in all_accs if acc.name == name), None)
    if account is None:
        raise AppError(f"Account not found: {name}")
    if account not in act_accs:
        raise AppError(f"Account is disabled: {name}")

    return [account]


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
        f"Omni competition: {status.start_time:%Y-%m-%d} → {status.end_time:%Y-%m-%d} UTC · "
        f"min volume ${status.volume_threshold:,.0f}"
    )


def _competition_status_table(rows: list[CompetitionRow]) -> None:
    def metric(value: Decimal | None, rank: int | None, spec: str, suffix: str = "") -> str | None:
        if value is None:
            return None

        result = f"{format(value, spec)}{suffix}"
        return f"{result} (#{rank})" if rank is not None else result

    first_status = next((status for _acc, status, _error in rows if status is not None), None)
    _print_competition_summary(first_status)

    show_pnl = any(s and s.user and s.user.pnl_total is not None for _, s, _ in rows)
    show_roi = any(s and s.user and s.user.roi_total is not None for _, s, _ in rows)
    columns = [
        Column("Account", justify="left"),
        Column("Status", justify="left"),
        Column("Volume"),
    ]
    if show_pnl:
        columns.append(Column("PnL"))
    if show_roi:
        columns.append(Column("ROI"))

    tbl = AutoTable(*columns)

    for acc, status, error in rows:
        user = status.user if status else None
        volume = user.volume_total if user and user.volume_total is not None else Decimal(0)
        eligible = bool(status is not None and volume >= status.volume_threshold)
        if error:
            state = "error"
        elif eligible:
            state = "eligible"
        elif user:
            state = "joined"
        else:
            state = "not joined"

        values = [
            acc.name,
            state,
            metric(volume, user.volume_rank, ",.0f") if user else None,
        ]
        if show_pnl:
            values.append(metric(user.pnl_total, user.pnl_rank, ",.2f") if user else None)
        if show_roi:
            values.append(metric(user.roi_total, user.roi_rank, ".2f", "%") if user else None)

        tbl.add_row(*values)

    tbl.print()


async def print_competition_hint(accs: list[OmniClient]) -> None:
    if not accs:
        return

    try:
        status = await accs[0].competition_status()
    except ApiError:
        return

    if status.ongoing:
        print("Omni competition is active. Run `uv run apps/omni.py competition` to check or join.")


async def _load_competition_rows(accs: list[OmniClient]) -> list[CompetitionRow]:
    async def row(acc: OmniClient):
        try:
            return acc, await acc.competition_status(), None
        except ApiError as e:
            return acc, None, str(e)

    return await gather_accs(accs, row)


async def run_competition(accs: list[OmniClient], *, auto_join: bool = False) -> None:
    rows = await _load_competition_rows(accs)
    _competition_status_table(rows)

    pending = [row for row in rows if row[1] is not None and row[1].ongoing and row[1].user is None]
    if not pending:
        return

    count = len(pending)
    account_label = "account" if count == 1 else "accounts"
    if not auto_join and not confirm(f"Join competition with {count} {account_label}?"):
        return

    async def join(row: CompetitionRow) -> CompetitionRow:
        acc, _status, _error = row
        try:
            await acc.competition_opt_in()
            status = await acc.competition_status()
            return acc, status, None
        except ApiError as e:
            return acc, None, str(e)

    updates = {row[0].name: row for row in await gather_accs(pending, join)}
    rows = [updates.get(row[0].name, row) for row in rows]
    print()
    _competition_status_table(rows)


# MARK: Main


async def main():
    cli = await create_cli(
        "omni",
        "configs/omni.toml",
        ["privkey", "captcha_key"],
        custom_commands={
            "competition": setup_competition_cli,
        },
    )
    cfg = OmniConfig.load(cli.config)
    if key := cfg.captcha_key.get_secret_value():
        os.environ["CAPTCHA_KEY"] = key

    all_accs, act_accs = await setup_app(cli, cfg, OmniClient.from_config)

    match cli.command:
        case "info":
            await print_info(all_accs)
            await print_competition_hint(all_accs)
        case "stats":
            await print_stats(all_accs, period=cli.group, filter_period=cli.filter, force=cli.force)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_strategy(cfg, act_accs, cli.pool)
        case "positions":
            await print_positions(act_accs)
        case "competition":
            await run_competition(all_accs, auto_join=cli.join)
        case "deposit":
            accounts = _select_account(all_accs, act_accs, cli.account)
            await run_deposits(accounts, cfg)
        case "withdraw":
            accounts = _select_account(all_accs, act_accs, cli.account)
            await run_withdrawals(
                accounts,
                cfg,
                withdraw_full=cli.full,
            )


if __name__ == "__main__":
    run_app(main())
