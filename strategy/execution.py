# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Code so clean it squeaks
import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from lib.errors import AppError
from lib.logger import logger
from lib.utils import format_duration, round_to_tick_size

from .models import (
    Order,
    OrderBook,
    OrderBookLevel,
    OrderStatus,
    Side,
    TradeConfig,
    TradingClient,
)


@dataclass(frozen=True)
class EntryQuality:
    avg_bid_price: Decimal | None
    avg_ask_price: Decimal | None
    entry_spread_pct: Decimal | None
    bid_qty: Decimal = Decimal(0)
    ask_qty: Decimal = Decimal(0)
    bid_depth: Decimal = Decimal(0)
    ask_depth: Decimal = Decimal(0)

    @property
    def unavailable_reason(self) -> str | None:
        missing = []
        if self.bid_qty > 0 and self.avg_bid_price is None:
            missing.append(f"asks depth {self.ask_depth}/{self.bid_qty}")
        if self.ask_qty > 0 and self.avg_ask_price is None:
            missing.append(f"bids depth {self.bid_depth}/{self.ask_qty}")
        if missing:
            return "insufficient " + ", ".join(missing)
        if self.entry_spread_pct is None:
            return "spread unavailable"
        return None


@runtime_checkable
class EntryQualityEstimator(Protocol):
    async def estimate_entry_quality(
        self, symbol: str, legs: Sequence[tuple[Side, Decimal]]
    ) -> EntryQuality: ...


def _simulate_book_fill(levels: list[OrderBookLevel], qty: Decimal) -> Decimal | None:
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")

    remaining = qty
    notional = Decimal(0)
    filled = Decimal(0)

    for level in levels:
        if remaining <= 0:
            break
        take = min(level.size, remaining)
        if take <= 0:
            continue
        notional += take * level.price
        filled += take
        remaining -= take

    return notional / filled if filled > 0 and filled == qty else None


def evaluate_entry_quality(
    order_book: OrderBook,
    legs: Sequence[tuple[Side, Decimal]],
) -> EntryQuality:
    bid_qty = sum((qty for side, qty in legs if side == "bid"), Decimal(0))
    ask_qty = sum((qty for side, qty in legs if side == "ask"), Decimal(0))
    bid_depth = sum((level.size for level in order_book.bids), Decimal(0))
    ask_depth = sum((level.size for level in order_book.asks), Decimal(0))

    avg_bid_price = _simulate_book_fill(order_book.asks, bid_qty) if bid_qty else None
    avg_ask_price = _simulate_book_fill(order_book.bids, ask_qty) if ask_qty else None
    entry_spread_pct = (
        abs(avg_ask_price - avg_bid_price) / avg_ask_price * 100
        if avg_bid_price is not None and avg_ask_price is not None and avg_ask_price > 0
        else None
    )

    return EntryQuality(
        avg_bid_price=avg_bid_price,
        avg_ask_price=avg_ask_price,
        entry_spread_pct=entry_spread_pct,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
    )


async def wait_for_entry_quality(
    client: TradingClient, symbol: str, legs: Sequence[tuple[Side, Decimal]], cfg: TradeConfig
) -> EntryQuality | None:
    if cfg.max_entry_spread_pct is None:
        return EntryQuality(None, None, None)

    log = logger.bind(account=client.name)
    timeout = cfg.entry_gate_wait
    poll_interval = cfg.entry_gate_poll
    deadline = time.time() + timeout
    warned = False
    last_quality: EntryQuality | None = None

    def fmt(x: Decimal | None) -> str:
        return f"{x:.2f}%" if x is not None else "n/a"

    def gate_msg(prefix: str, quality: EntryQuality) -> str:
        reason = quality.unavailable_reason
        detail = f" reason={reason}" if reason else ""
        return f"{symbol} gate {prefix} spread={fmt(quality.entry_spread_pct)}{detail}"

    while True:
        if isinstance(client, EntryQualityEstimator):
            quality = await client.estimate_entry_quality(symbol, legs)
        else:
            quality = evaluate_entry_quality(await client.get_order_book(symbol), legs)
        last_quality = quality
        if (
            quality.entry_spread_pct is not None
            and quality.entry_spread_pct <= cfg.max_entry_spread_pct
        ):
            return quality

        if not warned:
            log.debug(gate_msg("waiting", quality))
            warned = True

        remaining = deadline - time.time()
        if remaining <= 0:
            msg = gate_msg("skip", last_quality) if last_quality else f"{symbol} gate skip"
            log.debug(f"{msg} timeout={timeout:.0f}s")
            return None

        await asyncio.sleep(min(poll_interval, remaining))


# MARK: Limit order

_LIMIT_LOG_SEC = 30


async def _fetch_bbo(
    client: TradingClient, symbol: str, tick_size: Decimal
) -> tuple[Decimal, Decimal]:
    bid, ask = await client.get_bbo(symbol)
    return round_to_tick_size(bid, tick_size), round_to_tick_size(ask, tick_size)


def _is_behind(side: Side, price: Decimal, bid: Decimal, ask: Decimal, tick: Decimal) -> bool:
    """True when the best price on the order's side has left it by more than the spread."""
    spread = max(ask - bid, tick)
    return bid - price > spread if side == "bid" else price - ask > spread


async def _cancel(client: TradingClient, order: Order) -> Order:
    """Cancel, then re-read the order so a fill that landed during the cancel is counted."""
    await client.cancel_order(order)
    current = await client.get_order(order.id)
    if current is not None and current.status == OrderStatus.OPEN:
        # Never place a replacement while this order can still fill; cleanup cancels everything.
        raise AppError(f"Limit order {order.id} still open after cancel, aborting")

    return current if current is not None and current.filled >= order.filled else order


async def _cancel_and_market(
    client: TradingClient, order: Order, side: Side, symbol: str, reduce_only: bool
) -> Order:
    log = logger.bind(account=client.name)
    order = await _cancel(client, order)
    remaining = order.size - order.filled
    if remaining <= 0:
        return order.model_copy(update={"status": OrderStatus.FILLED})

    log.debug(f"Limit timeout → market fallback {side} {remaining} {symbol}")
    return await client.market_order(symbol, side, remaining, reduce_only)


async def _reprice_limit_order(
    client: TradingClient,
    order: Order,
    side: Side,
    symbol: str,
    price: Decimal,
    reduce_only: bool,
) -> Order:
    log = logger.bind(account=client.name)
    order = await _cancel(client, order)
    remaining = order.size - order.filled
    if remaining <= 0:
        return order.model_copy(update={"status": OrderStatus.FILLED})

    log.debug(f"Limit reprice {side} {remaining} {symbol} @ {price}")
    return await client.limit_order(symbol, side, remaining, price, reduce_only)


async def fill_limit_order(
    client: TradingClient,
    symbol: str,
    side: Side,
    qty: Decimal,
    reduce_only=False,
    timeout=60,
    use_market_fallback=True,
    max_wait_retries: int = 9,
) -> Order | None:
    """Place a limit order at the best price on its side and wait for the fill.

    `timeout` is one wait window: after each window the order moves to the best price if the
    market has left it by more than the spread. With `use_market_fallback`, whatever is still
    unfilled after `1 + max_wait_retries` windows goes to market; without it the order keeps
    waiting at the best price until it fills.
    """
    tick_size = await client.get_tick_size(symbol)
    bid, ask = await _fetch_bbo(client, symbol, tick_size)
    price = bid if side == "bid" else ask

    log = logger.bind(account=client.name)
    log.debug(f"Limit {side} {qty} {symbol} @ {price}")
    order = await client.limit_order(symbol, side, qty, price, reduce_only)
    if order.status == OrderStatus.FILLED:
        return order  # already filled (e.g. exchange falls back to market internally)

    order_id = order.id
    started = placed_at = logged_at = time.time()
    deadline = started + timeout * (1 + max_wait_retries)
    poll_delay = 0.25  # starts at 250ms, grows to ~3s

    while True:
        await asyncio.sleep(poll_delay)
        poll_delay = min(poll_delay * 2.5, 3.0)

        current = await client.get_order(order_id)
        if current is None:
            if time.time() - placed_at > timeout:
                raise AppError(f"Limit order {order_id} never appeared — unknown state, aborting")
            continue  # archive lag — keep polling

        order = current
        if order.status == OrderStatus.FILLED:
            log.debug(
                f"Limit {side} {qty} {symbol} filled in {format_duration(time.time() - started)}"
            )
            return order

        if order.status == OrderStatus.CANCELED:
            elapsed = time.time() - started
            raise RuntimeError(
                f"Limit {symbol} canceled by exchange after {elapsed:.0f}s"
                f" (filled {order.filled}/{order.size})"
            )

        now = time.time()
        if use_market_fallback and now >= deadline:
            log.debug(
                f"Limit {side} {qty} {symbol} timed out after {format_duration(now - started)}"
            )
            return await _cancel_and_market(client, order, side, symbol, reduce_only)

        if now - logged_at >= _LIMIT_LOG_SEC:
            logged_at = now
            elapsed = format_duration(now - started)
            log.debug(f"Limit {side} {qty} {symbol}: waiting @ {price} elapsed={elapsed}")

        if now - placed_at < timeout:
            continue  # at most one move per wait window

        bid, ask = await _fetch_bbo(client, symbol, tick_size)
        if not _is_behind(side, price, bid, ask, tick_size):
            continue  # within the spread the market usually comes back to the order

        price = bid if side == "bid" else ask
        order = await _reprice_limit_order(client, order, side, symbol, price, reduce_only)
        if order.status == OrderStatus.FILLED:
            return order

        order_id = order.id
        placed_at = time.time()
        poll_delay = 0.25


# MARK: Execution primitives


async def close_all(accs: Sequence[TradingClient], _attempts: int = 3) -> bool:
    """Best-effort cleanup — retries, never raises; False if something stayed open."""
    n_failed = 0
    for attempt in range(_attempts):
        rs1 = await asyncio.gather(*[a.cancel_all_orders() for a in accs], return_exceptions=True)
        rs2 = await asyncio.gather(*[a.close_all_positions() for a in accs], return_exceptions=True)

        n_orders = sum(r for r in rs1 if isinstance(r, int))
        n_positions = sum(r for r in rs2 if isinstance(r, int))
        n_failed = sum(1 for r in [*rs1, *rs2] if isinstance(r, Exception))
        if n_orders + n_positions > 0:
            logger.info(f"Closed {n_orders} orders and {n_positions} positions")
        if not n_failed:
            return True

        if attempt < _attempts - 1:
            logger.warning(f"close_all: {n_failed} failed, retrying ({attempt + 1}/{_attempts})...")
            await asyncio.sleep(2.0 * 2**attempt)

    logger.warning(f"close_all: still {n_failed} account(s) failed after {_attempts} attempts")
    return False
