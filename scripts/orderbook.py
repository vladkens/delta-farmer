# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Read-only order book snapshot helper
# Usage:
#   uv run scripts/orderbook.py
#   uv run scripts/orderbook.py -e nado -e pacifica
#   uv run scripts/orderbook.py --levels 3
import argparse
import asyncio
import glob
from decimal import Decimal

from apps.hyperliquid import HyperLiquidNativeClient
from clients.ethereal import EtherealClient
from clients.hyena import HyenaClient
from clients.n1 import N1Client
from clients.nado import NadoClient
from clients.omni import OmniClient
from clients.pacifica import PacificaClient
from clients.rise import RiseClient
from strategy import OrderBook, OrderBookLevel, StrategyConfig

EXCHANGES = [
    "ethereal",
    "hyena",
    "hyperliquid",
    "nado",
    "omni",
    "pacifica",
    "rise",
    "n1",
]


def _format_num(value: Decimal, step: Decimal) -> str:
    if step <= 0:
        return f"{value.normalize():f}"
    return f"{value.quantize(step):f}"


def _format_level(level: OrderBookLevel, price_step: Decimal, size_step: Decimal) -> str:
    return f"{_format_num(level.price, price_step)} x {_format_num(level.size, size_step)}"


def _format_levels(
    levels: list[OrderBookLevel], limit: int, price_step: Decimal, size_step: Decimal
) -> str:
    if not levels:
        return "-"
    return " | ".join(_format_level(level, price_step, size_step) for level in levels[:limit])


def _format_spread(bid: Decimal, ask: Decimal) -> str:
    if bid <= 0 or ask <= 0 or ask <= bid:
        return "-"
    spread = (Decimal(1) - (bid / ask)) * Decimal(100)
    return f"{spread:.3f}%"


async def snapshot(exchange: str, config_path: str, levels: int) -> tuple[str, str]:

    client_map = {
        "ethereal": EtherealClient,
        "hyena": HyenaClient,
        "hyperliquid": HyperLiquidNativeClient,
        "nado": NadoClient,
        "omni": OmniClient,
        "pacifica": PacificaClient,
        "rise": RiseClient,
        "n1": N1Client,
    }

    cfg = StrategyConfig.load(config_path)
    acc_cfg = next((acc for acc in cfg.accounts if acc.enabled), cfg.accounts[0])
    client = client_map[exchange].from_config(acc_cfg)  # type: ignore[call-arg]
    symbol = getattr(client, "_coin", lambda s: s)(cfg.symbols[0])

    try:
        (bid, ask), tick, lot = await asyncio.gather(
            client.get_bbo(symbol),
            client.get_tick_size(symbol),
            client.get_lot_size(symbol),
        )
        spread = _format_spread(bid, ask)
        bbo = f"bid={_format_num(bid, tick)} ask={_format_num(ask, tick)} spread={spread}"

        get_order_book = getattr(client, "get_order_book", None)
        if get_order_book is None:
            note = "BBO only (no depth API)"
            body = [
                f"config : {config_path}",
                f"account: {acc_cfg.name}",
                f"symbol : {symbol}",
                f"bbo    : {bbo}",
                f"note   : {note}",
            ]
            return exchange, "\n".join(body)

        book: OrderBook = await get_order_book(symbol)
        body = [
            f"config : {config_path}",
            f"account: {acc_cfg.name}",
            f"symbol : {symbol}",
            f"bbo    : {bbo}",
            f"bids   : {_format_levels(book.bids, levels, tick, lot)}",
            f"asks   : {_format_levels(book.asks, levels, tick, lot)}",
        ]
        return exchange, "\n".join(body)
    except Exception as e:
        return exchange, "\n".join(
            [
                f"config : {config_path}",
                f"account: {acc_cfg.name}",
                f"symbol : {symbol}",
                f"error  : {type(e).__name__}: {e}",
            ]
        )


def _pick_config(exchange: str) -> str | None:
    matches = sorted(glob.glob(f"configs/{exchange}*.toml"))
    return matches[0] if matches else None


async def main() -> None:
    parser = argparse.ArgumentParser(description="Print order book snapshots for all exchanges")
    parser.add_argument(
        "-e",
        "--exchange",
        action="append",
        choices=EXCHANGES,
        dest="exchanges",
        help="Limit to selected exchange(s); default is all",
    )
    parser.add_argument("--levels", type=int, default=5, help="How many book levels to print")
    args = parser.parse_args()

    exchanges = args.exchanges or EXCHANGES
    jobs: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    for exchange in exchanges:
        config_path = _pick_config(exchange)
        if config_path is None:
            skipped.append((exchange, "no config found in configs/"))
            continue
        jobs.append((exchange, config_path))

    results = await asyncio.gather(
        *(snapshot(exchange, config_path, args.levels) for exchange, config_path in jobs)
    )

    for exchange, text in results:
        print(f"[{exchange}]")
        print(text)
        print()

    for exchange, reason in skipped:
        print(f"[{exchange}]")
        print(f"skip   : {reason}")
        print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
