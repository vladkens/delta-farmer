# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import TypeVar

from core.table import AutoTable, Column

from .client import Client, Trade
from .config import Config

T = TypeVar("T")
DD = defaultdict[str, defaultdict[str, T]]


def to_points_week(ts: int) -> str:
    # https://docs.pacifica.fi/points-program
    GENESIS = datetime(2025, 9, 4, tzinfo=timezone.utc)
    dt = datetime.fromtimestamp(ts // 1000, tz=timezone.utc)
    delta = dt - GENESIS
    return f"W{delta.days // 7 + 1}"


class Report:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.accs = [Client.from_config(acc) for acc in cfg.accounts]

    async def estimate_cfg(self):
        min_dur = self.cfg.trade_duration.min + self.cfg.trade_cooldown.min
        max_dur = self.cfg.trade_duration.max + self.cfg.trade_cooldown.max
        min_trades, max_trades = (3600 / max_dur) * 24, (3600 / min_dur) * 24

        min_vol = self.cfg.trade_size_usd.min * min_trades
        max_vol = self.cfg.trade_size_usd.max * max_trades
        print(f"Est. daily volume: {min_vol:,.0f} - {max_vol:,.0f}")
        print(f"Est. daily trades: {min_trades:,.0f} - {max_trades:,.0f}")

    async def info(self):
        await self.estimate_cfg()

        tbl = AutoTable(
            Column("Account", justify="left"),
            Column("Volume", "{:,.0f}", total=sum),
            Column("Burn", "{:,.2f}", total=sum),
            Column("Points", "{:,.1f}", total=sum),
            Column("P/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Points"]),
            Column("Balance", "{:,.2f}", total=sum),
        )

        for acc in self.accs:
            (eqt, pnl), vol, pts = await asyncio.gather(
                acc.portfolio(), acc.total_volume(), acc.points()
            )

            tbl.add_row(acc.name, vol, -pnl, pts.points, eqt)

        tbl.print()

    async def weekly(self):
        gtrades: DD[list[Trade]] = defaultdict(lambda: defaultdict(list))
        gpoints: DD[Decimal] = defaultdict(lambda: defaultdict(Decimal))

        for acc in self.accs:
            trades = await acc.trades()
            for trade in trades:
                week = to_points_week(trade.created_at)
                gtrades[week][acc.name].append(trade)

            points = await acc.points_history()
            for week, point in points.items():
                gpoints[week][acc.name] = point

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
        for week in sorted(gtrades.keys()):
            tbl.subgroup(f"{week}")
            for acc_name in sorted(gtrades[week].keys()):
                trades = gtrades[week][acc_name]
                points = gpoints[week][acc_name]

                vol = sum(trade.amount * trade.price for trade in trades)
                pnl = sum(trade.pnl for trade in trades)
                fee = sum(trade.fee for trade in trades)
                tvol[acc_name] += vol
                tbl.add_row(acc_name, len(trades), vol, -pnl, points, fee, tvol[acc_name])

        tbl.print()
