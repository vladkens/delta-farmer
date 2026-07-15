# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | It's not a bug, it's undocumented behavior
import argparse
import asyncio
import glob
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Coroutine
from typing import Any

from pydantic import BaseModel, Field

from . import telegram as tg
from . import telemetry
from .crypto import config_cli_parser
from .errors import AppError
from .logger import enable_file_logging, logger
from .models import AccountConfig
from .proxy import print_proxies
from .telegram import TgConfig
from .update import latest_release_notice


def eprint(*args, **kwargs):
    print(*args, **kwargs, file=sys.stderr)


def _env_enabled(name: str) -> bool:
    return (os.environ.get(name) or "").lower() in ("1", "true", "yes", "on")


def _telemetry_props(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "stats":
        return {
            "stats_group": args.group,
            "stats_force": args.force,
        }
    if args.command == "competition":
        return {
            "competition_join": bool(getattr(args, "join", False)),
        }
    return {}


class HelpFormatter(argparse.HelpFormatter):
    def _iter_indented_subactions(self, action):
        for subaction in super()._iter_indented_subactions(action):
            if getattr(subaction, "help", None) == argparse.SUPPRESS:
                continue
            yield subaction


def cli_anyarg(
    parser: argparse.ArgumentParser,
    *flags: str,
    default: Any = argparse.SUPPRESS,
    action: str | None = None,
    help: str | None = None,
) -> None:
    option_strings = set(flags)
    parser_default = default

    def _apply(target: argparse.ArgumentParser, *, is_root: bool) -> None:
        if not any(
            option_strings.intersection(existing.option_strings) for existing in target._actions
        ):
            default_value = parser_default if is_root else argparse.SUPPRESS
            if action is None:
                target.add_argument(*flags, default=default_value, help=help)
            else:
                target.add_argument(*flags, default=default_value, action=action, help=help)

        for existing in target._actions:
            if not isinstance(existing, argparse._SubParsersAction):
                continue
            for subparser in existing.choices.values():
                if isinstance(subparser, argparse.ArgumentParser):
                    _apply(subparser, is_root=False)

    _apply(parser, is_root=True)


def _git_hash(repo: str) -> str | None:
    try:
        cmd = ["git", "rev-parse", "--short", "HEAD"]
        return subprocess.check_output(cmd, cwd=repo, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


def _git_tag(repo: str) -> bool:
    # returns non-zero (CalledProcessError) if HEAD is not exactly on a tag
    try:
        subprocess.check_output(
            ["git", "describe", "--exact-match", "--tags", "HEAD"],
            cwd=repo,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _get_version() -> tuple[str, bool]:
    try:
        pyproject = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        with open(pyproject) as f:
            match = re.search(r'version\s*=\s*"([^"]+)"', f.read())
        version = match.group(1) if match else None
        if not version:
            return "", True

        repo = os.path.join(os.path.dirname(__file__), "..")
        if _git_tag(repo):
            return f"v{version} ", True
        short = _git_hash(repo)
        return (f"v{version}-{short} ", False) if short else (f"v{version} ", True)
    except Exception:
        return "", True


VERSION, IS_RELEASE = _get_version()


class _TgOnlyConfig(BaseModel):
    telegram: TgConfig = Field(default_factory=TgConfig)


def _load_tg_config(filepath: str) -> TgConfig:
    try:
        with open(filepath, "rb") as fp:
            obj = tomllib.load(fp)
        return _TgOnlyConfig.model_validate(obj).telegram
    except Exception:
        return TgConfig()


async def _handle_tgtest(name: str) -> None:
    if not tg.enabled():
        eprint("Telegram not configured (set token and chat_id in [telegram] section)")
        sys.exit(1)

    await tg.send(f"✅ *{name}* — Telegram is working")
    eprint("Message sent.")


def _load_accounts_config(filepath: str) -> list[AccountConfig]:
    try:
        with open(filepath, "rb") as fp:
            obj = tomllib.load(fp)
    except FileNotFoundError:
        eprint(f"Config file not found: {filepath}")
        sys.exit(1)
    except tomllib.TOMLDecodeError as e:
        eprint(f"Invalid TOML syntax in {filepath}: {e}")
        sys.exit(1)

    try:
        accounts = obj.get("accounts", [])
        return [AccountConfig.model_validate(acc) for acc in accounts]
    except Exception as e:
        eprint(f"Failed to load accounts from {filepath}: {e}")
        sys.exit(1)


async def create_cli(
    name: str,
    config_path: str,
    sec_fields: list[str],
    custom_commands: dict[str, Callable[[argparse.ArgumentParser], None]] | None = None,
) -> argparse.Namespace:
    cli = argparse.ArgumentParser(prog=name, formatter_class=HelpFormatter)

    sub = cli.add_subparsers(dest="command")
    sub.add_parser("trade", help="Run trading manager")
    sub.add_parser("close", help="Close all positions")
    sub.add_parser("positions", help="Show active positions")
    sub.add_parser("info", help="Show accounts info")
    sub.add_parser("proxy", help="Check configured proxies")
    sub.add_parser("clean", help="Delete cached data")
    sub.add_parser("tgtest", help=argparse.SUPPRESS)
    for command, setup in (custom_commands or {}).items():
        parser = sub.add_parser(command, help=f"Run {command} tools")
        setup(parser)

    stats_parser = sub.add_parser("stats", help="Show trading stats")
    stats_parser.add_argument(
        "filter", nargs="?", default="all", help="Period filter (all/this/last/W05)"
    )
    stats_parser.add_argument("-g", "--group", choices=["week", "day"], default="week")
    stats_parser.add_argument("--force", dest="force", action="store_true", help="Force stats sync")
    stats_parser.add_argument("--sync", dest="force", action="store_true", help=argparse.SUPPRESS)

    all_fields = list(sec_fields) + ([] if "token" in sec_fields else ["token"])
    handle_config = config_cli_parser(sub, fields=all_fields)

    cli_anyarg(cli, "-c", "--config", default=config_path, help="Path to config file")
    cli_anyarg(cli, "--no-banner", default=False, action="store_true", help=argparse.SUPPRESS)

    acts = [a for a in sub._get_subactions() if getattr(a, "help", None) != argparse.SUPPRESS]
    sub.metavar = "{" + ",".join(a.dest for a in acts) + "}"
    args = cli.parse_args()

    if not args.no_banner:
        eprint(f":: delta-farmer {VERSION}| https://x.com/uid127 | https://t.me/eazyrekt")
        if notice := await latest_release_notice(VERSION):
            eprint(notice)

    telemetry.init(
        exchange=name,
        command=args.command or "",
        version=VERSION,
        release=IS_RELEASE,
        props=_telemetry_props(args),
    )

    if args.command is None:
        cli.print_help()
        exit(1)

    if args.command == "trade" and _env_enabled("DF_LOG_FILE"):
        enable_file_logging(name)

    if args.command == "config":
        handle_config(args)
        exit(0)

    if args.command == "clean":
        files = glob.glob(f".cache/{name}_*.pkl")
        for f in files:
            os.remove(f)
            eprint(f"Deleted {f}")
        if not files:
            eprint("No cache files found")
        exit(0)

    if args.command == "proxy":
        await print_proxies(_load_accounts_config(args.config))
        sys.exit(0)

    if args.command in ("trade", "tgtest"):
        tg.init(name, _load_tg_config(args.config))

    if args.command == "tgtest":
        await _handle_tgtest(name)
        sys.exit(0)

    return args


def run_app(coro: Coroutine) -> None:
    try:
        asyncio.run(coro)
    except AppError as e:
        logger.error(str(e))
    except KeyboardInterrupt:
        pass
