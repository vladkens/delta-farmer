# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | May contain traces of genius
import decimal
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from rich import print as console_print
from rich.box import SIMPLE, Box
from rich.console import JustifyMethod
from rich.table import Table

from . import telemetry
from .logger import logger

_TBD = object()  # sentinel: not enough data to compute


@dataclass
class Column:
    name: str
    fmt: str = "{}"
    justify: JustifyMethod = "right"

    total: Callable | None = None
    compute: Callable | None = None
    guard: Callable | None = None  # if returns False, shows "tbd" instead of computing
    grand_total: bool = True


class RowProxy:
    def __init__(self, row, name_to_index):
        self._row = row
        self._map = name_to_index

    def __getitem__(self, key):
        return self._row[self._map[key]]


def _compute(col: Column, proxy: RowProxy):
    assert col.compute is not None, "No compute function defined for column"

    if col.guard and not col.guard(proxy):
        return _TBD
    try:
        return col.compute(proxy)
    except (ZeroDivisionError, decimal.InvalidOperation):
        return None
    except Exception as e:
        logger.error(f"Error computing column '{col.name}' for row {proxy._row}: {e}")
        return None


class AutoTable:
    def __init__(self, *columns, box: Box = SIMPLE, title: str | None = None, gtitle="Group"):
        columns = cast(list[Column], list(columns))
        self.columns = columns
        self.rows: list[list] = []
        self.name_to_index = {col.name: i for i, col in enumerate(columns)}

        self._gtitle = gtitle
        self._sub_title = None
        self._groups = []
        self._footer = None

        self.box = box
        self.title = title

    def add_row(self, *values):
        row, value_iter = [], iter(values)
        for col in self.columns:
            row.append(None if col.compute else next(value_iter))

        self.rows.append(row)

    def set_footer(self, *values):
        self._footer = list(values)

    def _flush_group(self):
        if self._sub_title is not None:
            self._groups.append((self._sub_title, self._sub_index, len(self.rows)))
            self._sub_title = None
            self._sub_index = None

    def subgroup(self, title: str):
        self._flush_group()
        self._sub_title = title
        self._sub_index = len(self.rows)

    def _compute_totals_for_rows(self, rows):
        totals: list = [None] * len(self.columns)
        proxy = RowProxy(totals, self.name_to_index)

        # normal totals
        for i, col in enumerate(self.columns):
            if col.total:
                totals[i] = col.total(row[i] for row in rows)

        # computed columns
        for i, col in enumerate(self.columns):
            if col.compute:
                totals[i] = _compute(col, proxy)

        return totals

    def _render_rows(self, tbl: Table, rows: list, gtitle: str | None = None):
        for idx, row in enumerate(rows):
            proxy = RowProxy(row, self.name_to_index)
            rendered = []

            for i, col in enumerate(self.columns):
                val = _compute(col, proxy) if col.compute else row[i]
                if val is _TBD:
                    rendered.append("tbd")
                elif val is not None:
                    rendered.append(col.fmt.format(val))
                else:
                    rendered.append("n/a")

            if gtitle:
                tbl.add_row(gtitle if idx == 0 else "", *rendered)
            else:
                tbl.add_row(*rendered)

    def render(self):
        self._flush_group()

        def fmt_cell(col: Column, val) -> str:
            try:
                return "tbd" if val is _TBD else col.fmt.format(val) if val is not None else ""
            except Exception as e:
                logger.error(f"Error formatting column '{col.name}' value {val!r}: {e}")
                return "err"

        totals = self._footer or self._compute_totals_for_rows(self.rows)
        footers = [
            fmt_cell(col, totals[i]) if col.grand_total else ""
            for i, col in enumerate(self.columns)
        ]
        show_footer = any(footers)
        tbl = Table(title=self.title, box=self.box, show_footer=show_footer)

        if self._groups:
            tbl.add_column(self._gtitle, justify="left")

        for i, col in enumerate(self.columns):
            footer = footers[i]
            if i == 0 and self._groups and show_footer:
                footer = "TOTAL"

            tbl.add_column(col.name, justify=col.justify, footer=footer)

        if not self._groups:
            self._render_rows(tbl, self.rows)
            return tbl

        for title, since, until in self._groups:
            subrows = self.rows[since:until]
            if not subrows:
                continue  # skip empty groups

            subtotals = self._compute_totals_for_rows(subrows)

            self._render_rows(tbl, subrows, title)
            footer = [fmt_cell(col, subtotals[i]) for i, col in enumerate(self.columns)]

            footer.insert(0, "")  # for group title column
            footer[1] = "Total"  # for first data column
            tbl.add_row(*footer, end_section=True, style="bold italic")
            # tbl.add_row(*footer, end_section=True, style="bold reverse")

        return tbl

    def print(self):
        console_print(self.render())


# MARK: Stats rendering


@dataclass
class PeriodRow:
    account: str
    trades: int
    volume: Decimal
    burn: Decimal
    points: Decimal
    fees: Decimal


def render_stats(
    periods: dict[str, list[PeriodRow]],
    periods_to_show: list[str],
    *,
    fees: bool = True,
    points_fmt: str = "{:,.2f}",
    pprice_fmt: str = "{:,.2f}",
    min_vol: int = 1_000,
) -> None:
    telemetry.track(
        "stats_rendered",
        {
            "stats_selected_period_count": len(periods_to_show),
            "stats_available_period_count": len(periods),
        },
    )

    cols: list[Column] = [
        Column("Account", justify="left"),
        Column("Trades", "{:,}", total=sum),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", points_fmt, total=sum),
        Column("P/Price", pprice_fmt, compute=lambda r: r["Burn"] / r["Points"]),
        Column(
            "$/100k",
            "${:,.2f}",
            compute=lambda r: r["Burn"] / r["Volume"] * Decimal("1e5"),
            guard=lambda r: r["Volume"] >= min_vol,
        ),
    ]
    if fees:
        cols += [
            Column("Fees", "{:,.2f}", total=sum),
            Column(
                "Fee, %",
                "{:.3%}",
                compute=lambda r: r["Fees"] / r["Volume"],
                guard=lambda r: r["Volume"] >= min_vol,
            ),
        ]
    cols.append(Column("Total Vol", "{:,.0f}", total=sum, grand_total=False))

    tbl = AutoTable(*cols)
    tvol: defaultdict[str, Decimal] = defaultdict(Decimal)

    for pk in periods_to_show:
        tbl.subgroup(pk)
        for row in periods.get(pk, []):
            tvol[row.account] += row.volume
            if fees:
                tbl.add_row(
                    row.account,
                    row.trades,
                    row.volume,
                    row.burn,
                    row.points,
                    row.fees,
                    tvol[row.account],
                )
            else:
                tbl.add_row(
                    row.account, row.trades, row.volume, row.burn, row.points, tvol[row.account]
                )

    tbl.print()


if __name__ == "__main__":
    tbl = AutoTable(
        Column("Name", justify="left"),
        Column("Price", "{:.2f}", total=sum),
        Column("Quantity", "{:.2f}", total=sum),
        Column("Percent", fmt="{:.1%}", compute=lambda r: r["Price"] / r["Quantity"]),
    )

    dateset = {
        1: [("Apple", 0.5, 10), ("Banana", 0.3, 20)],
        2: [("Apple", 0.5, 5), ("Banana", 0.3, 15)],
    }

    for week, items in dateset.items():
        # tbl.subgroup(f"Week {week}")
        for name, price, quantity in items:
            tbl.add_row(name, price, quantity)

    tbl.print()
