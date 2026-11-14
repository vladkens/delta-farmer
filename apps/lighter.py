# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import argparse
import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from clients.lighter import (
    VOLUME_START_TIMESTAMP,
    LighterCoreClient,
    LighterFunding,
    LighterRobinhoodClient,
    LighterTrade,
)
from lib.cli import create_cli, run_app, setup_app
from lib.errors import AppError
from lib.http import ApiError
from lib.logger import logger
from lib.models import AccountConfig, TimeRange
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import format_duration, gather_accs, parse_filter, short_addr, to_period_day
from strategy import AppConfig, load_config
from strategy.deposit import DepositClient, DepositConfig, run_deposits
from strategy.runner import close_all, print_positions, run_groups, select_strategy
from strategy.withdrawal import run_withdrawals

# A wallet's Core twin: the same key and proxy on Lighter Core, named `<account>@core`.
CORE_SUFFIX = "@core"
# Core reports from the Robinhood campaign start too: a wallet's earlier manual trading there
# is not farming.
CORE_START = datetime.fromtimestamp(VOLUME_START_TIMESTAMP, tz=UTC)


class LighterConfig(DepositConfig):
    pass


def make_core_twin(cfg: AccountConfig) -> LighterCoreClient:
    return LighterCoreClient.from_config(cfg.model_copy(update={"name": cfg.name + CORE_SUFFIX}))


def _account_name(acc: LighterCoreClient) -> str:
    # A table titled with its deployment names the account, not its Core twin.
    return acc.name.removesuffix(CORE_SUFFIX)


async def _caught[T](coro: Awaitable[T]) -> T | Exception:
    # A failed Core lookup becomes a note: Core trouble must not stop the RH report or `close`.
    try:
        return await coro
    except Exception as error:
        return error


async def _core_state(acc: LighterCoreClient) -> bool | None:
    """Logged in on Core, awaiting login, or None without a Core account (one lookup)."""
    if not await acc.registered():
        return None

    return await acc.auth_ready()


async def core_logins(
    cores: Sequence[LighterCoreClient],
) -> tuple[list[LighterCoreClient], list[LighterCoreClient], list[str]]:
    """Twins on Core split into logged in and awaiting login, plus failed lookups as notes."""
    states = await asyncio.gather(*(_caught(_core_state(acc)) for acc in cores))
    return (
        [acc for acc, state in zip(cores, states) if state is True],
        [acc for acc, state in zip(cores, states) if state is False],
        [
            f"{acc.name}: {state}"
            for acc, state in zip(cores, states)
            if isinstance(state, Exception)
        ],
    )


async def logged_in_cores(cores: Sequence[LighterCoreClient]) -> list[LighterCoreClient]:
    """Twins outside a hedge that can act on Core; the others are named in a warning."""
    ready, pending, failed = await core_logins(cores)
    if pending:
        logger.warning(f"Not logged in on Core, skipped: {_names(pending)}")
    if failed:
        logger.warning(f"Core lookup failed, skipped: {'; '.join(failed)}")

    return ready


def hedge_accounts(cfg: AppConfig) -> set[str]:
    """Accounts of a strategy with long_exchange, whose wallets trade on Core too."""
    if cfg.strategy is not None:
        return {acc.name for acc in cfg.accounts} if cfg.strategy.long_exchange else set()

    return {name for pool in cfg.pools.values() if pool.long_exchange for name in pool.accounts}


def core_twins(
    accounts: Sequence[LighterCoreClient], cores: dict[str, LighterCoreClient], hedged: set[str]
) -> tuple[list[LighterCoreClient], list[LighterCoreClient]]:
    """Core twins of the enabled accounts for `close` and `positions`: hedge legs, required to
    close with their pair, then the others, which join once logged in in case they still hold a
    position. A disabled account's twin is left alone, like its RH account."""
    return (
        [cores[acc.name] for acc in accounts if acc.name in hedged],
        [cores[acc.name] for acc in accounts if acc.name not in hedged],
    )


def hedge_pairs(
    accounts: Sequence[LighterCoreClient], cores: dict[str, LighterCoreClient]
) -> list[LighterCoreClient]:
    """Each account hedges against its own wallet on Core: its RH leg, then its Core twin."""
    names: dict[str, list[str]] = defaultdict(list)
    for acc in accounts:
        names[acc.address].append(acc.name)

    # Two accounts on one wallet would run two pairs on the same positions.
    shared = ["/".join(group) for group in names.values() if len(group) > 1]
    if shared:
        raise AppError(f"Accounts share one wallet: {', '.join(shared)}")

    return [leg for acc in accounts for leg in (acc, cores[acc.name])]


def setup_useref_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("code", metavar="CODE", help="Referral code to apply")
    parser.add_argument("-a", "--account", metavar="NAME", help="Use one enabled account")


async def sync_trades(acc: LighterCoreClient, ttl: int) -> list[LighterTrade]:
    store_path = f".cache/{acc.exchange}_{short_addr(acc.address)}_trades.pkl"
    store = DataStore(store_path, id_key="trade_id", model=LighterTrade)
    await store.sync(acc.trades, ttl_sec=ttl)
    return store.get_all()


async def sync_funding(acc: LighterCoreClient, ttl: int) -> list[LighterFunding]:
    store_path = f".cache/{acc.exchange}_{short_addr(acc.address)}_funding.pkl"
    store = DataStore(store_path, id_key="funding_id", model=LighterFunding)
    await store.sync(acc.funding_history, ttl_sec=ttl, lookback_sec=3600)
    return store.get_all()


async def sync_points_total(acc: LighterCoreClient, ttl: int) -> None:
    store_path = f".cache/{acc.exchange}_{short_addr(acc.address)}_points_total.pkl"
    store = DataStore(store_path)
    if not store.needs_sync(ttl):
        return
    points = await acc.points_total()
    if points is None:
        return  # Keep the last successful snapshot and its sync time on API failure.
    store.upsert([{"id": "total", "points": points}])
    store.update_sync_time()
    store.save()


def _since[T: (LighterTrade, LighterFunding)](items: list[T], start: datetime) -> list[T]:
    return [item for item in items if item.created_at >= start]


def to_iso_week_label(dt: datetime) -> str:
    """Week label for a deployment with no campaign calendar, keyed on the ISO year and week."""
    # Core history spans years: without the year, a week of last year shares or outsorts this one.
    iso = dt.isocalendar()
    monday = dt - timedelta(days=dt.weekday())
    return f"{iso.year}-W{iso.week:02d} {monday:%b%d}-{monday + timedelta(days=6):%b%d}"


async def _print_stats_block(
    title: str,
    accs: Sequence[LighterCoreClient],
    week_label: Callable[[datetime], str],
    period: str,
    filter_period: str,
    ttl: int,
    start: datetime | None = None,
) -> None:
    period_fn = to_period_day if period == "day" else week_label
    accs = list(accs)
    all_trades, all_funding, _ = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_trades(acc, ttl)),
        gather_accs(accs, lambda acc: sync_funding(acc, ttl)),
        gather_accs(accs, lambda acc: sync_points_total(acc, ttl)),
    )

    grouped: dict[str, dict[str, PeriodRow]] = defaultdict(dict)

    def row_for(pk: str, name: str) -> PeriodRow:
        if name not in grouped[pk]:
            grouped[pk][name] = PeriodRow(name, 0, Decimal(0), Decimal(0), Decimal(0), Decimal(0))

        return grouped[pk][name]

    for acc, trades, funding in zip(accs, all_trades, all_funding):
        name = _account_name(acc)
        if start is not None:
            trades = _since(trades, start)
            funding = _since(funding, start)

        for trade in trades:
            row = row_for(period_fn(trade.created_at), name)
            row.trades += 1
            row.volume += trade.volume
            row.fees += trade.fee
            row.burn += trade.fee - trade.realized_pnl
        for payment in funding:
            row = row_for(period_fn(payment.created_at), name)
            row.burn -= payment.change

    all_periods = sorted(grouped)
    periods_data = {pk: list(grouped[pk].values()) for pk in all_periods}
    # The leaderboard exposes total points, without a history to assign to periods.
    shown = parse_filter(filter_period, all_periods)
    render_stats(periods_data, shown, points=False, title=title)


async def print_stats(
    accs: list[LighterRobinhoodClient],
    cores: list[LighterCoreClient],
    period="week",
    filter_period="all",
    force=False,
):
    ttl = 0 if force else 3600
    rh_accs = await ready_accounts(accs, "stats")
    # Core history needs a login; a Core account outside a hedge may never have had one.
    core_accs, pending, failed = await core_logins(cores)
    if pending:
        logger.info(f"Not logged in, no Core stats: {_names(pending)}")
    if failed:
        logger.warning(f"Core lookup failed, no Core stats: {'; '.join(failed)}")

    # Each deployment keeps its own periods and totals: RH counts campaign weeks, Core ISO weeks.
    rh_week = LighterRobinhoodClient.to_week_label
    await _print_stats_block("Lighter RH", rh_accs, rh_week, period, filter_period, ttl)
    if not core_accs:
        return

    print()
    try:
        await _print_stats_block(
            "Lighter Core", core_accs, to_iso_week_label, period, filter_period, ttl, CORE_START
        )
    except AppError as error:
        logger.warning(f"Lighter Core stats unavailable: {error}")


def _info_table(title: str, *, campaign: bool) -> AutoTable:
    """Accounts of one deployment; points and referral codes exist only in the RH campaign."""

    def total(values):
        return sum(value for value in values if value is not None)

    columns = [
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=total),
        Column("Burn", "{:,.2f}", total=total),
    ]
    if campaign:
        columns += [
            Column("Points", "{:,.6f}", total=total),
            Column(
                "P/Price",
                "{:,.4f}",
                compute=lambda r: (
                    r["Burn"] / r["Points"] if r["Burn"] is not None and r["Points"] else None
                ),
            ),
        ]
    columns += [
        Column(
            "$/100k",
            "${:,.2f}",
            compute=lambda r: (
                r["Burn"] / r["Volume"] * Decimal("1e5")
                if r["Burn"] is not None and r["Volume"]
                else None
            ),
        ),
        Column("Balance", "{:,.2f}", total=total),
    ]
    if campaign:
        columns.append(Column("Ref", justify="left"))

    return AutoTable(*columns, title=title)


async def _info_cells(acc: LighterCoreClient) -> tuple | None:
    """Status, volume, burn, points, balance and referral code; None without an account there."""
    if not await acc.registered():
        return None
    if not await acc.auth_ready():
        return ("○", None, None, None, await acc.balance(), None)

    p = await acc.profile()
    return ("✓", p.volume, -p.pnl, p.points, p.balance, p.ref_code)


async def _core_info_cells(acc: LighterCoreClient) -> tuple | str | None:
    """Info cells of a Core twin, or its lookup error as text: Core trouble stays in its table."""
    try:
        return await _info_cells(acc)
    except Exception as error:
        return str(error)


async def print_info(accs: list[LighterRobinhoodClient], cores: list[LighterCoreClient]):
    rh_cells, core_cells = await asyncio.gather(
        gather_accs(accs, _info_cells), gather_accs(cores, _core_info_cells)
    )

    rh = _info_table("Lighter RH", campaign=True)
    for acc, cells in zip(accs, rh_cells):
        status, volume, burn, points, balance, ref = cells or ("✗", None, None, None, None, None)
        rh.add_row(status, acc.name, short_addr(acc.address), volume, burn, points, balance, ref)
    rh.print()

    print()
    core = _info_table("Lighter Core", campaign=False)
    missing: list[str] = []
    pending: list[str] = []
    failed: list[str] = []
    for acc, cells in zip(cores, core_cells):
        if isinstance(cells, str):
            failed.append(f"{_account_name(acc)}: {cells}")
            continue
        if cells is None:
            missing.append(_account_name(acc))
            continue
        if cells[0] != "✓":
            pending.append(_account_name(acc))
            continue

        status, volume, burn, _, balance, _ = cells
        core.add_row(status, _account_name(acc), short_addr(acc.address), volume, burn, balance)
    if core.rows:
        core.print()
    if missing:
        print(f"Not on Core: {', '.join(missing)}")
    if pending:
        print(f"Not logged in on Core: {', '.join(pending)}")
    if failed:
        print(f"Core lookup failed: {'; '.join(failed)}")

    missing_refs = [
        acc.name
        for acc, cells in zip(accs, rh_cells)
        if cells and cells[0] == "✓" and not cells[-1]
    ]
    if missing_refs:
        cmd = "uv run apps/lighter.py"
        print()
        print(f"Referral code missing: {', '.join(missing_refs)}")
        print(f"  All enabled: {cmd} useref CODE")
        print(f"  One account: {cmd} useref CODE -a {missing_refs[0]}")


def _names(accounts: Sequence[LighterCoreClient]) -> str:
    return ", ".join(acc.name for acc in accounts)


def _funding_hint(account: LighterCoreClient) -> str:
    # The first deposit registers the account; a deployment this tool cannot deposit to is
    # funded on its own web app.
    if isinstance(account, DepositClient):
        return "uv run apps/lighter.py deposit"

    return f"fund it on {account.label} by hand"


def _unfunded_error(accounts: Sequence[LighterCoreClient]) -> AppError:
    hints: dict[str, list[str]] = defaultdict(list)
    for account in accounts:
        hints[_funding_hint(account)].append(account.name)

    detail = "; ".join(f"{', '.join(names)}: {hint}" for hint, names in sorted(hints.items()))
    return AppError(f"Not registered — {detail}")


def _pending_error(accounts: Sequence[LighterCoreClient]) -> AppError:
    return AppError(f"Login required: {_names(accounts)}. Run: uv run apps/lighter.py login")


async def check_login[T: LighterCoreClient](
    accs: Sequence[T],
) -> tuple[list[T], list[T], list[T]]:
    """Split a selection into logged in, awaiting login, and without an exchange account yet."""
    ready = await asyncio.gather(*(acc.auth_ready() for acc in accs))
    pending = [acc for acc, ok in zip(accs, ready) if not ok]
    known = await asyncio.gather(*(acc.registered() for acc in pending))
    return (
        [acc for acc, ok in zip(accs, ready) if ok],
        [acc for acc, ok in zip(pending, known) if ok],
        [acc for acc, ok in zip(pending, known) if not ok],
    )


async def require_login(accs: Sequence[LighterCoreClient]) -> None:
    """Every selected account must be able to act; used where a missing one changes the plan."""
    _, pending, unfunded = await check_login(accs)
    if unfunded:
        raise _unfunded_error(unfunded)

    if pending:
        raise _pending_error(pending)


async def ready_accounts[T: LighterCoreClient](accs: Sequence[T], action: str) -> list[T]:
    """Accounts to act on. One with no exchange account holds no position and reports nothing,
    so it is skipped instead of stopping the whole selection."""
    logged_in, pending, unfunded = await check_login(accs)
    if pending:
        raise _pending_error(pending)

    if unfunded and not logged_in:
        raise _unfunded_error(unfunded)

    if unfunded:
        logger.info(f"Not registered, no {action}: {_names(unfunded)}")

    return logged_in


async def apply_referral_code(
    accounts: list[LighterRobinhoodClient],
    code: str,
    delay: TimeRange,
) -> None:
    code = code.strip()
    if not code:
        raise AppError("Referral code cannot be empty")

    failed: list[str] = []
    for index, account in enumerate(accounts):
        progress = f"{index + 1}/{len(accounts)}"
        with logger.contextualize(account=account.name, progress=progress):
            try:
                await account.use_referral_code(code)
                logger.success("Referral code applied")
            except (AppError, ApiError) as error:
                # One account's refusal must not drop the accounts queued behind it.
                failed.append(account.name)
                logger.error(f"Referral code failed: {error}")

        if index < len(accounts) - 1:
            wait = delay.sample()
            logger.info(f"Waiting {format_duration(wait)} before next account")
            await asyncio.sleep(wait)

    if failed:
        raise AppError(f"Referral code not applied: {', '.join(failed)}")


def _select_account(
    all_accs: list[LighterRobinhoodClient],
    act_accs: list[LighterRobinhoodClient],
    name: str | None,
) -> list[LighterRobinhoodClient]:
    if name is None:
        return act_accs

    account = next((acc for acc in all_accs if acc.name == name), None)
    if account is None:
        raise AppError(f"Account not found: {name}")
    if account not in act_accs:
        raise AppError(f"Account is disabled: {name}")

    return [account]


async def main():
    cli = await create_cli(
        "lighter",
        "configs/lighter.toml",
        ["privkey"],
        custom_commands={"useref": setup_useref_cli},
    )
    cfg = load_config(LighterConfig, cli.config)
    cores = {acc.name: make_core_twin(acc) for acc in cfg.accounts}
    # Only a hedge trades on Core, so `login` signs in the twins of its accounts alone.
    hedged = hedge_accounts(cfg)
    all_accs, act_accs = await setup_app(
        cli,
        cfg,
        LighterRobinhoodClient.from_config,
        extra_logins=[cores[acc.name] for acc in cfg.accounts if acc.name in hedged],
    )
    hedge_cores, other_cores = core_twins(act_accs, cores, hedged)

    try:
        match cli.command:
            case "info":
                await print_info(all_accs, list(cores.values()))
            case "stats":
                await print_stats(
                    all_accs,
                    list(cores.values()),
                    period=cli.group,
                    filter_period=cli.filter,
                    force=cli.force,
                )
            case "close":
                others = await logged_in_cores(other_cores)
                await close_all(
                    [*await ready_accounts([*act_accs, *hedge_cores], "close"), *others]
                )
            case "trade":
                strategy, accounts = select_strategy(cfg, act_accs, cli.pool)
                if strategy.long_exchange is not None:
                    accounts = hedge_pairs(accounts, cores)
                await require_login(accounts)
                await run_groups(strategy, accounts, cycles=cli.cycles)
            case "positions":
                others = await logged_in_cores(other_cores)
                await print_positions([*act_accs, *hedge_cores, *others])
            case "deposit":
                accounts = _select_account(all_accs, act_accs, cli.account)
                # The first deposit registers the Lighter account; login is only possible afterward.
                await run_deposits(accounts, cfg)
            case "withdraw":
                accounts = _select_account(all_accs, act_accs, cli.account)
                ready = await ready_accounts(accounts, "withdraw")
                await run_withdrawals(ready, cfg, withdraw_full=cli.full)
            case "useref":
                accounts = _select_account(all_accs, act_accs, cli.account)
                await apply_referral_code(accounts, cli.code, cfg.balance_transfer_delay)
    finally:
        await asyncio.gather(*(acc.close() for acc in [*all_accs, *cores.values()]))


if __name__ == "__main__":
    run_app(main())
