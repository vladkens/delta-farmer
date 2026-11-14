# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Code so clean it squeaks
import asyncio
import random
import time
from collections.abc import Awaitable, Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from lib import telegram as tg
from lib import utils
from lib.decorators import retry
from lib.errors import AppError
from lib.logger import logger
from lib.utils import round_to_tick_size

from .execution import close_all, error_detail
from .models import MarketHoursMode, TradeConfig, TradingClient, trading_client_trace
from .symbols import filter_exchange_symbols, unique_exchange_clients
from .trade import (
    SIZE_OVERSHOOT_LIMIT,
    DeltaTrade,
    DeltaTradeSummary,
    PartsTooSmall,
    PlanError,
    basket_lot_size,
    plan_delta_trades,
    smallest_leg_usd,
)

USD_TICK = Decimal("0.01")
SAFE_PCT = Decimal("0.96")  # leave 4% margin to avoid liquidation on leverage rounding
PNL_MISMATCH_WARN_USD = 5.0


def _format_error(exc: BaseException) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    client_path = trading_client_trace(exc)
    return f"{msg} @ {client_path}" if client_path else msg


async def _read_markets[T](
    calls: Sequence[tuple[str, TradingClient, Awaitable[T]]],
) -> tuple[list[tuple[str, T]], dict[str, str]]:
    """Read one market value per (symbol, venue); name the symbols whose venue could not answer.

    Every sibling is awaited and cancellation propagates, as `strategy/symbols.py` does. Anything
    else is one venue's answer about one market, so it costs that symbol and not the cycle: Omni
    serves both the lot size and the price from one quote endpoint that refuses while that market
    is shut, and no exception type tells a closure from a transient error.
    """
    values = await asyncio.gather(*[call for _, _, call in calls], return_exceptions=True)

    read: list[tuple[str, T]] = []
    failed: dict[str, str] = {}
    for (symbol, client, _), value in zip(calls, values, strict=True):
        if isinstance(value, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise value

        if isinstance(value, BaseException):
            failed.setdefault(symbol, f"{client.exchange}: {error_detail(value)}")
            continue

        read.append((symbol, cast(T, value)))

    return read, failed


def calc_total_from_pct(balances: list[tuple[str, float]], leverage: int, pct: float) -> Decimal:
    """Compute max safe total trade size from account balances in execution order.

    ordered_balances[0] is prime (gets 50% of total).
    ordered_balances[1:] are hedge accounts (split the remaining 50% equally).
    The binding constraint is the account whose balance is smallest relative to its share.
    """
    n = len(balances)
    n_hedge = n - 1
    if n_hedge > 0:
        shares = [Decimal("0.5")] + [Decimal("0.5") / n_hedge] * n_hedge
    else:
        shares = [Decimal("1")]

    max_totals = [
        Decimal(str(bal)) * leverage * SAFE_PCT * Decimal(str(pct)) / share
        for (_, bal), share in zip(balances, shares)
    ]
    return round_to_tick_size(min(max_totals), USD_TICK)


class Balances:
    def __init__(self, data: dict[str, float]):
        self._data = data

    @property
    def total(self) -> float:
        return sum(self._data.values())

    def items(self) -> list[tuple[str, float]]:
        return list(self._data.items())

    def log(self) -> None:
        parts = " | ".join(f"{name} {bal:.2f}" for name, bal in self._data.items())
        logger.info(f"Balances: {self.total:.2f} = {parts}")

    def log_pnl(
        self, prev: "Balances", initial_total: float | Decimal, last_total_pnl: float
    ) -> tuple[float, float]:
        diff_sum = self.total - prev.total
        diffs = " | ".join(f"{x} {self._data[x] - prev._data[x]:+.2f}" for x in self._data)
        total_pnl = self.total - float(initial_total)
        logger.info(f"Δ {diff_sum:+.2f} ~ {diffs}; Total P/L: {total_pnl:+.2f}")

        expected_total_pnl = last_total_pnl + diff_sum
        mismatch = total_pnl - expected_total_pnl
        if abs(mismatch) > PNL_MISMATCH_WARN_USD:
            logger.warning(
                "Unexpected P/L change: "
                f"expected {expected_total_pnl:+.2f}, actual {total_pnl:+.2f}, "
                f"Δ {mismatch:+.2f}. Possible balance movement."
            )

        return diff_sum, total_pnl


class RepeatErrorGuard:
    def __init__(self, message_prefix: str):
        self.prefix = message_prefix
        self.last_error_key: str | None = None
        self.last_error_count = 0

    @contextmanager
    def __call__(self) -> Generator[None]:
        try:
            yield
            self.last_error_key = None
            self.last_error_count = 0
        except Exception as e:
            error_key = _format_error(e)
            if error_key == self.last_error_key:
                self.last_error_count += 1
            else:
                self.last_error_key = error_key
                self.last_error_count = 1

            if self.last_error_count == 2:
                logger.warning(f"{self.prefix} {error_key[:200]}, continuing wait...")


class CycleBudget:
    """The traded cycles of this run counted against the limit it stops at.

    `limit` is `None` for the unbounded run, which is the default.

    One counter belongs to the whole process, so `--cycles N` is N cycles of this run and not N
    per group: every group shares this object. Only a cycle that traded counts, and a group reads
    the count before it starts the next cycle, so nothing is reserved and no group ever waits for
    another group's cycle. The run therefore stops after at least `limit` traded cycles: a single
    group trades exactly `limit`, and every extra group can add the one cycle it already had in
    flight when the limit was reached.
    """

    def __init__(self, limit: int | None = None):
        self.limit = limit
        self.done = 0  # cycles that opened and closed a trade, across every group
        # Set once and never cleared, so no group can miss it: a group waiting out its cooldown
        # when another group trades the last cycle leaves at once. `None` while unbounded, which
        # keeps the default run's cooldown on the exact path it has today.
        self.spent_event = asyncio.Event() if limit is not None else None

    def is_spent(self) -> bool:
        """True once `limit` cycles have traded; always False for the unbounded run."""
        return self.limit is not None and self.done >= self.limit

    def record(self, *, traded: bool) -> None:
        """Count a finished cycle: only one that opened and closed a trade spends the budget."""
        if self.limit is None:
            return

        self.done += 1 if traded else 0
        progress = f"Cycle budget: {self.done}/{self.limit} traded"
        logger.info(progress if traded else f"{progress}, this one did not count")

        if self.is_spent() and self.spent_event is not None:
            self.spent_event.set()


class DeltaStrategy:
    """
    Delta-neutral strategy that works with any TradingClient.
    Opens opposite positions on multiple accounts.
    """

    def __init__(
        self,
        cfg: TradeConfig,
        accounts: Sequence[TradingClient],
        stop_event: asyncio.Event | None = None,
        budget: CycleBudget | None = None,
    ):
        self.cfg = cfg
        self.accounts = list(accounts)
        self.stop_event = stop_event
        self.budget = budget if budget is not None else CycleBudget()
        self.initial_bal: float = 0.0
        self.last_total_pnl: float = 0.0

    # MARK: Core trading flow

    async def _wait(self, wait_sec: float, *stops: asyncio.Event | None):
        logger.info(utils.wait_msg(wait_sec))
        await utils.interruptible_sleep(wait_sec, self.stop_event, *stops)

    async def _shutdown(self) -> None:
        """Leave nothing open anywhere: the one exit path a stop and a spent budget share."""
        if not await close_all(self.accounts):
            logger.error("Shutdown cleanup incomplete; close positions manually")
            return

        logger.info("Shutdown cleanup complete")

    async def run(self):
        bals = await self.get_balances(self.accounts)
        self.initial_bal = bals.total
        clean = False  # close leftovers of a previous run before the first cycle

        failures = 0
        while not self.budget.is_spent():  # unbounded without --cycles: nothing is ever spent
            try:
                print("-" * 60) if not self.cfg.group_size else None
                # Never trade on top of positions a failed cleanup left open.
                if not clean and not (clean := await close_all(self.accounts)):
                    raise AppError("Cleanup incomplete; positions remain open")

                traded = await self.trade_cycle()
                failures = 0
                self.budget.record(traded=traded)
                if self.budget.is_spent():
                    break  # the cooldown would only delay the exit

                # Another group can trade the last cycle while this group waits here, so the
                # cooldown ends on a spent budget exactly as it ends on a stop.
                await self._wait(self.cfg.trade_cooldown.sample(), self.budget.spent_event)
            except asyncio.CancelledError:  # a stop, or a budget spent during the cooldown
                if self.budget.is_spent():
                    break  # another group traded the last cycle; leave by the same exit it took

                logger.info(
                    "Stop requested, closing all positions; press Ctrl-C again only to force exit"
                )
                return await self._shutdown()
            except Exception as e:
                clean = await close_all(self.accounts)

                failures += 1
                if self.cfg.max_failures > 0 and failures >= self.cfg.max_failures:
                    logger.opt(exception=True).error("Too many consecutive failures, stopping")
                    await tg.on_crash(_format_error(e))
                    # TODO: return exits only this group (others keep running); raise propagates to
                    # CLI and prints ugly traceback; SystemExit(1) kills the whole process cleanly.
                    # Decide which behaviour is correct for multi-group mode.
                    return

                wait_sec = min(3 * (2 ** (failures - 1)), 60) * 60  # 3m→6m→12m→24m→48m→60m
                wait_str = utils.format_duration(wait_sec)
                error = _format_error(e)
                msg = f"Cycle failed ({failures}) {error}, retrying in {wait_str}"
                logger.warning(msg)
                await tg.on_error(error, failures, wait_sec)
                try:
                    # The backoff keeps its length and its progression; a spent budget only ends
                    # the waiting, as it ends the cooldown.
                    await self._wait(wait_sec, self.budget.spent_event)
                except asyncio.CancelledError:
                    if not self.budget.is_spent():
                        raise  # a stop mid-backoff leaves exactly as it does today

                    break  # another group traded the last cycle; leave by the budget's own exit

        # Only a spent budget leaves the loop, either by the check above or by one of its breaks;
        # every other group of this run is leaving too, each after the cycle it had in flight.
        logger.info(
            f"No cycle budget left ({self.budget.done}/{self.budget.limit} traded), stopping"
        )
        await self._shutdown()

    async def trade_cycle(self) -> bool:
        """Run one full trade cycle across the selected symbols; True if it opened and closed one."""

        # 1. Get balances
        accounts = self.get_ordered_accounts()
        was_bals = await self.get_balances(accounts)
        was_bals.log()

        # 2. Pick symbols (markets) and build full trading plan
        duration = int(self.cfg.trade_duration.sample())
        exp_usd = self.get_trade_size(was_bals)  # drawn before the filters: they size against it
        tradeable = await self._tradeable_symbols(duration, exp_usd)
        if len(tradeable) < self.cfg.symbols_per_trade:
            logger.warning(
                f"Only {len(tradeable)}/{self.cfg.symbols_per_trade} symbols are tradeable "
                "for the planned window; skipping cycle"
            )
            return False

        symbols = random.sample(tradeable, self.cfg.symbols_per_trade)
        trades = await plan_delta_trades(
            accounts, symbols, exp_usd, self.cfg.leverage, was_bals.items(), self.cfg.long_exchange
        )
        if trades is None:
            logger.error("No valid account combination found for trading.")
            return False

        # 3. Validate the whole plan, parts included, before changing anything on accounts
        for trade in trades:
            await trade.load_qtys()
            await trade.check_min_sizes()

        n_parts = await self._fit_parts(trades)
        parts = [trade.split(n_parts) for trade in trades]

        for trade in trades:
            await trade.check_leverage(self.cfg.leverage)
            await trade.log_plan()

        # 4. Open in rounds: one part of every trade per round, each round gated on entry quality
        opened = 0
        msgid, stime, act_usd = None, time.time(), Decimal(0)
        for i, round_parts in enumerate(zip(*parts)):
            if i > 0 and self.stop_event and self.stop_event.is_set():
                break  # keep the opened parts; monitoring sees the stop and closes them

            gate_ok = await asyncio.gather(*[part.gate(self.cfg) for part in round_parts])
            if not all(gate_ok):
                if i == 0:
                    return False  # nothing opened, nothing to close

                logger.warning(f"Entry gate failed at part {i + 1}/{n_parts}; keeping {i} parts")
                break

            if i == 0:  # notify Telegram (if enabled)
                act_usd = sum(sum(leg.size_usd for leg in trade.legs) for trade in trades)
                s_names = [trade.symbol for trade in trades]
                a_names = [leg.client.name for trade in trades for leg in trade.legs]
                a_names = list(dict.fromkeys(a_names))  # unique while preserving order
                msgid = await tg.on_trade_start(s_names, float(act_usd), a_names)
                stime = time.time()

            for part in round_parts:
                await part.open(self.cfg)

            opened = i + 1
            if n_parts > 1:
                logger.info(f"Opened part {opened}/{n_parts}")

        if opened < n_parts:
            for trade, trade_parts in zip(trades, parts):
                trade.keep(trade_parts[:opened])

        # 5. Wait with safety checks
        success = await self.monitor_trades(trades, duration)

        # 6. Close in parts after a normal hold; a risk exit closes everything at once
        use_limit = self.cfg.use_limit and success
        if success and opened > 1:
            for remaining in range(opened, 1, -1):
                for trade in trades:
                    await trade.reduce(self.cfg, remaining, use_limit=use_limit)

                logger.info(f"Closed part {opened - remaining + 1}/{opened}")

        for trade in trades:
            await trade.close(self.cfg, use_limit=use_limit)

        # 7. Report P/L
        now_bals = await self.get_balances(accounts)
        pnl, self.last_total_pnl = now_bals.log_pnl(was_bals, self.initial_bal, self.last_total_pnl)
        dur = time.time() - stime
        await tg.on_trade_stop(pnl, dur, float(act_usd), now_bals.items(), msgid)

        return True

    # MARK: Helpers

    async def _fit_parts(self, trades: list[DeltaTrade]) -> int:
        """Most parts, up to `trade_parts`, that keep every part at or above each exchange minimum."""
        for n in range(self.cfg.trade_parts, 1, -1):
            try:
                parts = [part for trade in trades for part in trade.split(n)]
            except PartsTooSmall:  # fewer lots than parts; an invalid plan must not step down
                continue

            if not any([await part.below_min() for part in parts]):
                if n < self.cfg.trade_parts:
                    logger.warning(f"Part size below exchange minimum; using {n} parts")
                return n

        if self.cfg.trade_parts > 1:
            logger.warning("Part size below exchange minimum; trading in one part")
        return 1

    async def monitor_trades(self, trades: list[DeltaTrade], duration: int) -> bool:
        logger.info(utils.wait_msg(duration))

        until = time.time() + duration
        error_guard = RepeatErrorGuard("Position safety check failed")
        combined_roi_limit = Decimal(str(self.cfg.combined_roi_limit))

        market_closed = False
        while True:
            if self.stop_event and self.stop_event.is_set():
                logger.info("Stop event received, exiting early")
                return False

            left = until - time.time()
            if left <= 0 and not market_closed:
                return True

            # Holding through a market closure is normal; the close waits until it reopens.
            await asyncio.sleep(
                min(self.cfg.trade_heartbeat, left) if left > 0 else self.cfg.trade_heartbeat
            )

            with error_guard():
                if market_closed != (market_closed := not await self._markets_open(trades)):
                    logger.info(
                        "Market closed, holding until it reopens"
                        if market_closed
                        else "Market reopened"
                    )
                if market_closed:
                    continue  # no prices while closed, and nothing could be closed anyway

                summaries = await asyncio.gather(*[trade.state(self.cfg) for trade in trades])
                if any(not s.healthy for s in summaries):
                    for s in (s for s in summaries if not s.healthy):
                        logger.warning(f"Trade {s.symbol}: {s.close_reason}, closing...")
                    return False

                combined_roi = self.basket_combined_roi(summaries)
                if combined_roi is not None and abs(combined_roi) >= combined_roi_limit:
                    logger.info(f"Combined ROI hit {combined_roi:.2%}, closing...")
                    return False

    async def _markets_open(self, trades: list[DeltaTrade]) -> bool:
        if self.cfg.market_hours == MarketHoursMode.OFF:
            return True

        now = datetime.now(UTC)
        checks = [
            leg.client.is_symbol_tradeable(trade.symbol, now, reduce_only=True)
            for trade in trades
            for leg in trade.legs
        ]
        return all(await asyncio.gather(*checks))

    async def _tradeable_symbols(self, duration: int, total_usd: Decimal) -> list[str]:
        symbols = list(dict.fromkeys(self.cfg.symbols))
        if self.cfg.market_hours != MarketHoursMode.OFF:
            symbols = await self._open_symbols(symbols, duration)

        # After the market filter: a closed market has no lot to read, and no symbol to size.
        return await self._lot_compatible_symbols(symbols, total_usd)

    async def _open_symbols(self, symbols: list[str], duration: int) -> list[str]:
        now = datetime.now(UTC)
        limit_wait = self.cfg.limit_wait_budget if self.cfg.use_limit else 0
        drift = limit_wait * 2
        seq_limit = limit_wait * self.cfg.symbols_per_trade
        open_at = now + timedelta(seconds=int(self.cfg.entry_gate_wait) + seq_limit + drift)
        exit_at = int(duration) + seq_limit * self.cfg.trade_parts
        close_at = open_at + timedelta(seconds=exit_at)

        async def check(client: TradingClient, symbol: str) -> bool:
            ok_open = await client.is_symbol_tradeable(symbol, open_at)
            if self.cfg.market_hours == MarketHoursMode.AUTO:
                return ok_open

            ok_close = await client.is_symbol_tradeable(symbol, close_at)
            return ok_open and ok_close

        return await filter_exchange_symbols(self.accounts, symbols, check)

    async def _lot_compatible_symbols(self, symbols: list[str], total_usd: Decimal) -> list[str]:
        """Keep symbols whose venues' lot grids carry a basket step this trade size can use.

        A pool on one exchange has nothing to reconcile and reads nothing here: every client in
        this repo reads the lot from a market record keyed by the symbol alone, so its accounts
        share one grid per market by construction, and the basket step is that grid's own lot.
        Only a pool mixing venues can hold grids that reject each other, and there the lot is read
        once per exchange, as every other fan-out in `strategy/` does.

        Grids that cannot carry the configured size are a problem for that symbol alone, so it is
        dropped here instead of failing planning every cycle and backing the whole bot off. A
        venue that cannot answer for one market drops the same way, with the API's own words in
        the log: a market that is merely closed must not cost a cycle that has symbols to trade.
        """
        venues = unique_exchange_clients(self.accounts)
        if len(venues) < 2:
            return symbols  # one exchange, one grid per market: nothing to reconcile

        calls = [(symbol, acc, acc.get_lot_size(symbol)) for symbol in symbols for acc in venues]
        lots, dropped = await _read_markets(calls)

        grids: dict[str, list[Decimal]] = {}
        for symbol, lot in lots:
            grids.setdefault(symbol, []).append(lot)

        steps: dict[str, Decimal] = {}
        for symbol, symbol_lots in grids.items():
            if symbol in dropped:
                continue  # a venue did not answer, so these are not the whole pool's grids

            try:
                steps[symbol] = basket_lot_size(symbol_lots)
            except PlanError as e:
                dropped[symbol] = str(e)

        priced, unpriced = await self._coarse_steps(steps, grids)
        dropped.update(unpriced)
        if priced:
            leg_usd = smallest_leg_usd(total_usd, self.cfg.symbols_per_trade, len(self.accounts))
            limit_usd = leg_usd * SIZE_OVERSHOOT_LIMIT * 2  # half a step is the worst leg error
            for symbol, step, step_usd in priced:
                # Past that the lightest leg rounds off its planned size, to one lot or to zero,
                # and `load_qtys` refuses the plan — every cycle, for a configured symbol.
                if step_usd > limit_usd:
                    dropped[symbol] = f"step {step} = {step_usd:.2f} USD vs {leg_usd:.2f} USD leg"

        if dropped:
            details = "; ".join(f"{symbol}: {why}" for symbol, why in dropped.items())
            logger.warning(f"Symbols not tradeable across venues — {details}")

        return [symbol for symbol in symbols if symbol not in dropped]

    async def _coarse_steps(
        self, steps: dict[str, Decimal], grids: dict[str, list[Decimal]]
    ) -> tuple[list[tuple[str, Decimal, Decimal]], dict[str, str]]:  # [(symbol, step, step usd)]
        """Price the basket steps that are coarser than some venue's own grid.

        A step equal to every venue's lot rounds each leg as its own venue does and needs no
        price. A coarser step lifts the legs on the finer venues by up to half a step, which
        `load_qtys` bounds by each leg's own lot, so that step is measured against the trade size.
        A price this filter cannot read drops its symbol, exactly as an unreadable lot does.
        """
        coarse = [symbol for symbol, step in steps.items() if step > min(grids[symbol])]
        if not coarse:
            return [], {}

        # One price per symbol from one account: a cross-venue basis is ~0.03%, immaterial here.
        acc = self.accounts[0]
        prices, unpriced = await _read_markets([(x, acc, acc.get_price(x)) for x in coarse])

        return [(x, steps[x], steps[x] * price) for x, price in prices], unpriced

    def basket_combined_roi(self, summaries: list[DeltaTradeSummary]) -> Decimal | None:
        total_pnl = sum((s.total_pnl for s in summaries), Decimal(0))
        total_entry_cost = sum((s.total_entry_cost for s in summaries), Decimal(0))
        if total_entry_cost == 0:
            return None
        return total_pnl / total_entry_cost

    @retry(max_attempts=3, delay=2.0)
    async def get_balances(self, accs: list[TradingClient]) -> Balances:
        vals = await asyncio.gather(*[acc.balance() for acc in accs])
        return Balances({acc.name: float(v) for acc, v in zip(accs, vals)})

    def get_trade_size(self, bals: Balances) -> Decimal:
        usd, pct = self.cfg.trade_size_usd, self.cfg.trade_size_pct
        if (usd is None) == (pct is None):
            raise RuntimeError("configure exactly one of trade_size_usd or trade_size_pct")
        if pct is not None:
            return calc_total_from_pct(bals.items(), self.cfg.leverage, pct)
        assert usd is not None
        return Decimal(str(usd.sample()))

    def get_ordered_accounts(self) -> list[TradingClient]:
        return (
            self.accounts[:1] + utils.shuffle(self.accounts[1:])
            if self.cfg.first_as_prime
            else utils.shuffle(self.accounts)
        )
