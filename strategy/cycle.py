# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Code so clean it squeaks
import asyncio
import random
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lib import telegram as tg
from lib import utils
from lib.decorators import retry
from lib.errors import AppError
from lib.logger import logger
from lib.utils import round_to_tick_size

from .execution import close_all
from .models import MarketHoursMode, TradeConfig, TradingClient, trading_client_trace
from .symbols import filter_exchange_symbols
from .trade import DeltaTrade, DeltaTradeSummary, plan_delta_trades

USD_TICK = Decimal("0.01")
SAFE_PCT = Decimal("0.96")  # leave 4% margin to avoid liquidation on leverage rounding
PNL_MISMATCH_WARN_USD = 5.0


def _format_error(exc: BaseException) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    client_path = trading_client_trace(exc)
    return f"{msg} @ {client_path}" if client_path else msg


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
    def __call__(self) -> Iterator[None]:
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
    ):
        self.cfg = cfg
        self.accounts = list(accounts)
        self.stop_event = stop_event
        self.initial_bal: float = 0.0
        self.last_total_pnl: float = 0.0

    # MARK: Core trading flow

    async def _wait(self, wait_sec: float):
        logger.info(utils.wait_msg(wait_sec))
        await utils.interruptible_sleep(wait_sec, self.stop_event)

    async def run(self):
        bals = await self.get_balances(self.accounts)
        self.initial_bal = bals.total
        clean = False  # close leftovers of a previous run before the first cycle

        failures = 0
        while True:
            try:
                print("-" * 60) if not self.cfg.group_size else None
                # Never trade on top of positions a failed cleanup left open.
                if not clean and not (clean := await close_all(self.accounts)):
                    raise AppError("Cleanup incomplete; positions remain open")

                await self.trade_cycle()
                failures = 0
                await self._wait(self.cfg.trade_cooldown.sample())
            except asyncio.CancelledError:  # stop_event triggered, time to exit
                logger.info(
                    "Stop requested, closing all positions; press Ctrl-C again only to force exit"
                )
                if not await close_all(self.accounts):
                    logger.error("Shutdown cleanup incomplete; close positions manually")
                    return

                logger.info("Shutdown cleanup complete")
                return
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
                await self._wait(wait_sec)

    async def trade_cycle(self):
        """Run one full trade cycle across the selected symbols."""

        # 1. Get balances
        accounts = self.get_ordered_accounts()
        was_bals = await self.get_balances(accounts)
        was_bals.log()

        # 2. Pick symbols (markets) and build full trading plan
        duration = int(self.cfg.trade_duration.sample())
        tradeable = await self._tradeable_symbols(duration)
        if len(tradeable) < self.cfg.symbols_per_trade:
            logger.warning(
                f"Only {len(tradeable)}/{self.cfg.symbols_per_trade} symbols are tradeable "
                "for the planned window; skipping cycle"
            )
            return

        symbols = random.sample(tradeable, self.cfg.symbols_per_trade)
        exp_usd = self.get_trade_size(was_bals)
        trades = await plan_delta_trades(
            accounts, symbols, exp_usd, self.cfg.leverage, was_bals.items()
        )
        if trades is None:
            logger.error("No valid account combination found for trading.")
            return

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
                    return

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

    # MARK: Helpers

    async def _fit_parts(self, trades: list[DeltaTrade]) -> int:
        """Most parts, up to `trade_parts`, that keep every part at or above each exchange minimum."""
        for n in range(self.cfg.trade_parts, 1, -1):
            try:
                parts = [part for trade in trades for part in trade.split(n)]
            except ValueError:  # fewer lots than parts
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

    async def _tradeable_symbols(self, duration: int) -> list[str]:
        if self.cfg.market_hours == MarketHoursMode.OFF:
            return list(dict.fromkeys(self.cfg.symbols))

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

        return await filter_exchange_symbols(self.accounts, self.cfg.symbols, check)

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
