# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Small plans, fewer surprises
import asyncio
import os
import random
from collections.abc import Sequence
from decimal import Decimal
from itertools import batched

from rich import print as rprint
from rich.box import SIMPLE
from rich.table import Table

from lib import telegram as tg
from lib import telemetry, utils
from lib.errors import AppError
from lib.logger import logger

from .cycle import DeltaStrategy
from .models import Position, StrategyConfig, TradingClient
from .symbols import ensure_exchange_symbols


def _env_enabled(name: str) -> bool:
    return (os.environ.get(name) or "").lower() in ("1", "true", "yes", "on")


async def print_positions(accs: Sequence[TradingClient]) -> None:
    all_pos, all_bal = await asyncio.gather(
        asyncio.gather(*[a.positions() for a in accs], return_exceptions=True),
        asyncio.gather(*[a.balance() for a in accs], return_exceptions=True),
    )

    entries: list[tuple[str, Position]] = []
    balances: dict[str, Decimal] = {}
    for acc, pos_res, bal_res in zip(accs, all_pos, all_bal):
        if not isinstance(pos_res, BaseException):
            for pos in pos_res:
                entries.append((acc.name, pos))
        if not isinstance(bal_res, BaseException):
            balances[acc.name] = bal_res

    if not entries:
        logger.info("No active positions")
        return

    symbols = {pos.symbol for _, pos in entries}
    acc_map = {a.name: a for a in accs}

    prices_raw = await asyncio.gather(
        *[accs[0].get_price(s) for s in symbols], return_exceptions=True
    )
    prices: dict[str, Decimal] = {}
    for s, p in zip(symbols, prices_raw):
        if not isinstance(p, BaseException):
            prices[s] = p

    acc_sym_pairs = list({(acc_name, pos.symbol) for acc_name, pos in entries})
    lev_raw = await asyncio.gather(
        *[acc_map[a].get_leverage(s) for a, s in acc_sym_pairs], return_exceptions=True
    )
    leverages: dict[tuple[str, str], int | None] = {}
    for (a, s), lev in zip(acc_sym_pairs, lev_raw):
        if not isinstance(lev, BaseException):
            leverages[(a, s)] = lev  # None for clients that don't support it

    acc_mm: dict[str, Decimal] = {}
    acc_pnl: dict[str, Decimal] = {}

    rows = []
    total_pnl = Decimal(0)
    total_value = Decimal(0)
    total_entry_cost = Decimal(0)
    for acc_name, pos in entries:
        mark = prices.get(pos.symbol, pos.entry_price)
        sign = Decimal(1) if pos.side == "bid" else Decimal(-1)
        signed_qty = pos.size * sign
        entry_cost = pos.size * pos.entry_price
        pnl = (pos.size * mark - entry_cost) * sign
        roi = pnl / entry_cost if entry_cost else Decimal(0)
        value = pos.size * mark
        lev = leverages.get((acc_name, pos.symbol))
        mm = value / (lev * 2) if lev else None

        if mm is not None:
            acc_mm[acc_name] = acc_mm.get(acc_name, Decimal(0)) + mm
        acc_pnl[acc_name] = acc_pnl.get(acc_name, Decimal(0)) + pnl
        total_pnl += pnl
        total_value += value
        total_entry_cost += entry_cost
        rows.append((acc_name, pos.symbol, signed_qty, pos.entry_price, mark, value, pnl, roi))

    tc = "green" if total_pnl >= 0 else "red"
    total_roi = total_pnl / total_entry_cost if total_entry_cost else Decimal(0)
    trc = "green" if total_roi >= 0 else "red"

    tbl = Table(box=SIMPLE, show_footer=True)
    tbl.add_column("Account", justify="left")
    tbl.add_column("Symbol", justify="left")
    tbl.add_column("Qty", justify="right")
    tbl.add_column("Entry", justify="right")
    tbl.add_column("Mark", justify="right")
    total_bal = sum(balances.get(a.name, Decimal(0)) for a in {a.name: a for a in accs}.values())
    tbl.add_column("Value", justify="right", footer=f"{total_value:,.2f}")
    tbl.add_column("PnL", justify="right", footer=f"[{tc}]{total_pnl:+,.2f}[/{tc}]")
    tbl.add_column("ROI", justify="right", footer=f"[{trc}]{total_roi:+.2%}[/{trc}]")
    tbl.add_column("Balance", justify="right", footer=f"{total_bal:,.2f}")
    tbl.add_column("MM%", justify="right")

    seen_accs: set[str] = set()
    for acc_name, symbol, signed_qty, entry, mark, value, pnl, roi in rows:
        first = acc_name not in seen_accs
        seen_accs.add(acc_name)

        equity = balances.get(acc_name, Decimal(0)) + acc_pnl.get(acc_name, Decimal(0))
        raw_mm = acc_mm.get(acc_name)
        mm_usage = raw_mm / equity if (raw_mm is not None and equity) else None
        mmc = (
            "red"
            if mm_usage and mm_usage > Decimal("0.8")
            else "yellow"
            if mm_usage and mm_usage > Decimal("0.5")
            else "green"
        )

        c = "green" if pnl >= 0 else "red"
        qc = "green" if signed_qty >= 0 else "red"
        tbl.add_row(
            acc_name,
            symbol,
            f"[{qc}]{signed_qty:+.4f}[/{qc}]",
            f"{entry:,.2f}",
            f"{mark:,.2f}",
            f"{value:,.2f}",
            f"[{c}]{pnl:+,.2f}[/{c}]",
            f"[{c}]{roi:+.2%}[/{c}]",
            f"{balances.get(acc_name, Decimal(0)):,.2f}" if first else "-",
            (f"[{mmc}]{mm_usage:.1%}[/{mmc}]" if mm_usage is not None else "n/a") if first else "-",
        )

    rprint(tbl)


async def close_all(clients: Sequence[TradingClient]) -> None:
    """Cancel all orders and close all positions. Used by the CLI close command."""
    for client in clients:
        count1 = await client.cancel_all_orders()
        count2 = await client.close_all_positions()
        logger.info(f"{client.name}: Canceled {count1} orders, closed {count2} positions")


# MARK: Groups


async def _check_accounts(accs: Sequence[TradingClient]) -> None:
    rs = await asyncio.gather(*[a.registered() for a in accs], return_exceptions=True)
    failed = [a.name for a, r in zip(accs, rs) if isinstance(r, Exception) or r is False]
    if failed:
        raise AppError(f"Not registered: {', '.join(failed)}")


async def _check_symbols(cfg: StrategyConfig, accs: Sequence[TradingClient]) -> None:
    async def symbol_exists(acc: TradingClient, symbol: str) -> bool:
        await acc.get_lot_size(symbol)
        return True

    await ensure_exchange_symbols(accs, cfg.symbols, symbol_exists)


async def _run_group(
    cfg: StrategyConfig,
    name: str,
    accs: Sequence[TradingClient],
    stop_event: asyncio.Event,
    stagger: float = 0,
) -> None:
    strategy = DeltaStrategy(cfg, accs, stop_event=stop_event)
    with logger.contextualize(group=name):
        await asyncio.sleep(stagger) if stagger else None
        logger.info(f"Starting group with accounts: {', '.join(a.name for a in accs)}")
        await strategy.run()  # all exceptions should be handled inside run()


def _check_cfg(cfg: StrategyConfig, accs: Sequence[TradingClient]):
    n = len(accs)

    if cfg.trade_size_usd is None and cfg.trade_size_pct is None:
        raise AppError("either trade_size_usd or trade_size_pct must be specified in config")
    if cfg.trade_size_usd is not None and cfg.trade_size_pct is not None:
        raise AppError("trade_size_usd and trade_size_pct are mutually exclusive")

    if n < 2:
        raise AppError(f"At least 2 accounts are required for trading, got {n}")

    if cfg.symbols_per_trade > 1 and len(cfg.symbols) != cfg.symbols_per_trade:
        raise AppError(
            f"symbols_per_trade={cfg.symbols_per_trade} requires exactly "
            f"{cfg.symbols_per_trade} symbols, got {len(cfg.symbols)}"
        )

    if cfg.group_size is None and n > 5:
        raise AppError("Single-group mode supports up to 5 enabled accounts")

    if cfg.group_size is not None and n % cfg.group_size != 0:
        raise AppError(f"{n} enabled accounts is not divisible by group_size={cfg.group_size}")

    if cfg.group_size is not None and cfg.first_as_prime:
        cfg.first_as_prime = False
        logger.warning("group_size is set, ignoring first_as_prime=true")

    return cfg, accs


async def _balance_sorted(accs: Sequence[TradingClient]) -> list[TradingClient]:
    rs = await asyncio.gather(*[a.balance() for a in accs])
    pairs = [(acc, bal) for acc, bal in zip(accs, rs)]
    pairs.sort(key=lambda x: x[1])
    return [acc for acc, _ in pairs]


async def run_groups(cfg: StrategyConfig, accs: Sequence[TradingClient]) -> None:
    cfg, accs = _check_cfg(cfg, accs)
    await _check_accounts(accs)
    await _check_symbols(cfg, accs)

    tg.start()
    telemetry.track(
        "trade_started",
        {
            "account_count": len(accs),
            "symbols_count": len(cfg.symbols),
            "symbols_per_trade": cfg.symbols_per_trade,
            "trade_size_mode": "pct" if cfg.trade_size_pct is not None else "usd",
            "market_hours": cfg.market_hours.value,
            "use_limit": cfg.use_limit,
            "limit_wait_retries": cfg.limit_wait_retries,
            "limit_market_fallback": cfg.limit_market_fallback,
            "entry_gate_enabled": cfg.max_entry_spread_pct is not None,
            "entry_gate_wait": int(cfg.entry_gate_wait),
            "entry_gate_poll": int(cfg.entry_gate_poll),
            "group_mode": cfg.group_size is not None,
            "group_enabled": cfg.group_size is not None,
            "group_size": cfg.group_size,
            "regroup_interval": cfg.regroup_interval is not None,
            "regroup_enabled": cfg.regroup_interval is not None,
            "first_as_prime": cfg.first_as_prime,
            "max_failures_enabled": cfg.max_failures > 0,
            "telegram_enabled": tg.enabled(),
            "log_file_enabled": _env_enabled("DF_LOG_FILE"),
        },
    )

    if not cfg.group_size:  # Single group mode, no regrouping
        return await DeltaStrategy(cfg, accs).run()

    while True:
        print("-" * 60)
        # sort by balance only if regrouping requested - otherwise keep config order
        accs = await _balance_sorted(accs) if cfg.regroup_interval else accs
        grps = [list(g) for g in batched(accs, cfg.group_size)]
        logger.info(f"Running trading with {len(grps)} groups ({len(accs)} accounts)")

        tasks: list[asyncio.Task] = []
        stop_event = asyncio.Event()

        for i, grp_accounts in enumerate(grps):
            stagger = i * random.uniform(10, 30)
            name = f"{i + 1:02d}"
            coro = _run_group(cfg, name, grp_accounts, stop_event, stagger)
            tasks.append(asyncio.create_task(coro, name=f"delta-{name}"))

        if cfg.regroup_interval is None:  # if not regrouping, just run until manually stopped
            await asyncio.gather(*tasks, return_exceptions=True)
            return

        await asyncio.sleep(cfg.regroup_interval)
        stop_event.set()

        limit_wait = cfg.limit_wait_budget if cfg.use_limit else 0
        max_wait = limit_wait * cfg.symbols_per_trade * 2 + int(cfg.trade_duration.max) + 60
        await utils.gather_cancel(tasks, max_wait)
