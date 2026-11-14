# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License
import asyncio
from collections.abc import Awaitable, Callable, Sequence

from lib.errors import AppError

from .models import TradingClient


def unique_exchange_clients(accs: Sequence[TradingClient]) -> list[TradingClient]:
    """One account per exchange, first seen: market data is per exchange, not per account.

    Every client reads lots, ticks, listings and market hours from a shared market record keyed by
    the symbol alone, so one account per exchange answers for all of them.
    """
    by_exchange: dict[str, TradingClient] = {}
    for acc in accs:
        by_exchange.setdefault(acc.exchange, acc)
    return list(by_exchange.values())


async def _run_exchange_symbols[T](
    accs: Sequence[TradingClient],
    symbols: Sequence[str],
    fn: Callable[[TradingClient, str], Awaitable[T]],
) -> list[tuple[TradingClient, str, T | BaseException]]:
    unique_symbols = list(dict.fromkeys(symbols))
    pairs = [(acc, symbol) for acc in unique_exchange_clients(accs) for symbol in unique_symbols]
    values = await asyncio.gather(
        *[fn(acc, symbol) for acc, symbol in pairs],
        return_exceptions=True,
    )

    results: list[tuple[TradingClient, str, T | BaseException]] = []
    for (acc, symbol), value in zip(pairs, values, strict=True):
        if isinstance(value, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise value
        results.append((acc, symbol, value))
    return results


async def ensure_exchange_symbols(
    accs: Sequence[TradingClient],
    symbols: Sequence[str],
    predicate: Callable[[TradingClient, str], Awaitable[bool]],
) -> None:
    results = await _run_exchange_symbols(accs, symbols, predicate)
    failed = {
        (acc.exchange, symbol)
        for acc, symbol, value in results
        if isinstance(value, Exception) or value is not True
    }
    if failed:
        details = "; ".join(
            f"{symbol} is not available on {exchange}" for exchange, symbol in sorted(failed)
        )
        raise AppError(f"Invalid configured symbols: {details}")


async def filter_exchange_symbols(
    accs: Sequence[TradingClient],
    symbols: Sequence[str],
    predicate: Callable[[TradingClient, str], Awaitable[bool]],
) -> list[str]:
    unique_symbols = list(dict.fromkeys(symbols))
    results = await _run_exchange_symbols(accs, unique_symbols, predicate)
    for _acc, _symbol, value in results:
        if isinstance(value, Exception):
            raise value

    failed = {symbol for _acc, symbol, value in results if value is not True}
    return [symbol for symbol in unique_symbols if symbol not in failed]
