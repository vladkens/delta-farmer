# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | May contain traces of genius
from dataclasses import dataclass
from typing import Callable, cast

from rich import print as console_print
from rich.box import SIMPLE, Box
from rich.console import JustifyMethod
from rich.table import Table


@dataclass
class Column:
    name: str
    fmt: str = "{}"
    justify: JustifyMethod = "right"

    total: Callable | None = None
    compute: Callable | None = None
    grand_total: bool = True


class RowProxy:
    def __init__(self, row, name_to_index):
        self._row = row
        self._map = name_to_index

    def __getitem__(self, key):
        return self._row[self._map[key]]


class AutoTable:
    def __init__(self, *columns, box: Box = SIMPLE, title: str | None = None, gtitle="Group"):
        columns = cast(list[Column], list(columns))
        self.columns = columns
        self.rows: list[list] = []
        self.name_to_index = {col.name: i for i, col in enumerate(columns)}

        self._gtitle = gtitle
        self._sub_title = None
        self._groups = []

        self.box = box
        self.title = title

    def add_row(self, *values):
        row, value_iter = [], iter(values)
        for col in self.columns:
            row.append(None if col.compute else next(value_iter))

        self.rows.append(row)

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
        totals = [None] * len(self.columns)
        proxy = RowProxy(totals, self.name_to_index)

        # normal totals
        for i, col in enumerate(self.columns):
            if col.total:
                totals[i] = col.total(row[i] for row in rows)

        # computed columns
        for i, col in enumerate(self.columns):
            if col.compute:
                try:
                    totals[i] = col.compute(proxy)
                except ZeroDivisionError:
                    totals[i] = None

        return totals

    def _render_rows(self, tbl: Table, rows: list, gtitle: str | None = None):
        for idx, row in enumerate(rows):
            proxy = RowProxy(row, self.name_to_index)
            rendered = []

            for i, col in enumerate(self.columns):
                try:
                    value = col.compute(proxy) if col.compute else row[i]
                    rendered.append(col.fmt.format(value))
                except ZeroDivisionError:
                    rendered.append("n/a")

            if gtitle:
                tbl.add_row(gtitle if idx == 0 else "", *rendered)
            else:
                tbl.add_row(*rendered)

    def render(self):
        self._flush_group()

        tbl = Table(title=self.title, box=self.box, show_footer=True)
        totals = self._compute_totals_for_rows(self.rows)

        if self._groups:
            tbl.add_column(self._gtitle, justify="left")

        for i, col in enumerate(self.columns):
            footer = col.fmt.format(totals[i]) if totals[i] is not None else ""
            footer = footer if col.grand_total else ""
            if i == 0 and self._groups:
                footer = "TOTAL"

            tbl.add_column(col.name, justify=col.justify, footer=footer)

        if not self._groups:
            self._render_rows(tbl, self.rows)
            return tbl

        for title, since, until in self._groups:
            subrows = self.rows[since:until]
            subtotals = self._compute_totals_for_rows(subrows)

            self._render_rows(tbl, subrows, title)
            footer = [
                col.fmt.format(subtotals[i]) if subtotals[i] is not None else ""
                for i, col in enumerate(self.columns)
            ]

            footer.insert(0, "")  # for group title column
            footer[1] = "Total"  # for first data column
            tbl.add_row(*footer, end_section=True, style="bold italic")
            # tbl.add_row(*footer, end_section=True, style="bold reverse")

        return tbl

    def print(self):
        console_print(self.render())


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
