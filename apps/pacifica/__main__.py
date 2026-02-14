# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | May contain traces of genius
import argparse
import asyncio

from core.crypto import config_cli_parser

from .config import Config
from .manager import Manager
from .report import Report


async def main():
    # https://docs.pacifica.fi/trading-on-pacifica/trading-fees
    cli = argparse.ArgumentParser(prog="pacifica" if __name__ == "__main__" else None)
    cli.add_argument("-c", "--config", default="configs/pacifica.toml", help="Path to config file")

    sub = cli.add_subparsers(dest="command")
    sub.add_parser("trade", help="Run trading manager")
    sub.add_parser("close", help="Close all positions")
    sub.add_parser("info", help="Show accounts info")
    sub.add_parser("stats", help="Show trading stats")
    handle_config = config_cli_parser(sub, fields=["privkey"])

    args = cli.parse_args()
    if args.command is None:
        cli.print_help()
        return

    if args.command == "config":
        return handle_config(args)

    cfg = Config.load(args.config)

    match args.command:
        case "info":
            await Report(cfg).info()
        case "stats":
            await Report(cfg).weekly()
        case "close":
            await Manager(cfg).close()
        case "trade":
            await Manager(cfg).run_trade()


if __name__ == "__main__":
    asyncio.run(main())
