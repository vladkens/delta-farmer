# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

from rich.console import Console

from clients.hyena import HyenaClient
from clients.hyperliquid import migrate_hyperliquid_accounts, warn_legacy_hyperliquid_accounts
from lib.cli import create_cli, run_app
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

T = TypeVar("T")
DD = defaultdict[str, defaultdict[str, T]]
console = Console(stderr=True)


def _normalize_symbols(symbols: list[str]) -> list[str]:
    return [s if ":" in s else f"hyna:{s}" for s in symbols]


def _is_hyena_fill(fill: dict) -> bool:
    return str(fill.get("coin", "")).startswith("hyna:")


# MARK: Reports


async def print_info(accs: list[HyenaClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.0f}", total=sum),
        Column("P/Price", "{:,.4f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
        Column("Rewards", "{:,.2f}", total=sum),
    )
    legacy_accounts: list[str] = []
    claimable_rewards: list[tuple[str, Decimal]] = []

    async def row(acc: HyenaClient):
        await acc.warmup()
        if await acc.registered():
            p, total, rewards = await asyncio.gather(
                acc.profile(), acc.reward_total(), acc.system_rewards()
            )
        else:
            p, total, rewards = None, None, None
        a = short_addr(acc.address)
        if p and p.mode != "unifiedAccount":
            legacy_accounts.append(acc.name)
        if rewards and rewards.summary.claimableAmount > 0:
            claimable_rewards.append((acc.name, rewards.summary.claimableAmount))
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0, 0)

        assert total is not None
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance, total.totalClaimed)

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()
    warn_legacy_hyperliquid_accounts(legacy_accounts, "Hyena")
    if claimable_rewards:
        amount = sum((x for _, x in claimable_rewards), Decimal(0))
        names = ", ".join(name for name, _ in claimable_rewards)
        print(
            f"* Claimable Hyena rewards: {amount:,.4f} USDE ({names}). "
            "Run `uv run apps/hyena.py reward claim`."
        )


async def sync_fills(acc: HyenaClient, ttl: int) -> list[dict]:
    store_path = f".cache/hyena_{short_addr(acc.address)}_fills.pkl"
    store = DataStore(store_path, id_key="hash")
    await store.sync(acc.fetch_fills, ttl_sec=ttl)
    return [fill for fill in store.get_all() if _is_hyena_fill(fill)]


async def sync_rewards(acc: HyenaClient, ttl: int) -> list[dict]:
    store_path = f".cache/hyena_{short_addr(acc.address)}_rewards.pkl"
    store = DataStore(store_path, id_key="id")

    async def fetch(_since):
        rewards = await acc.rewards()
        return [
            {
                "id": h.id,
                "enaxPoints": h.enaxPoints,
                "period": HyenaClient.to_week_label(h.start_window),
                "start_window": h.start_window,
            }
            for h in rewards.history
        ]

    await store.sync(fetch, ttl_sec=ttl)
    return store.get_all()


async def print_stats(
    accs: list[HyenaClient], period: str = "week", filter_period: str = "all", force: bool = False
):
    ttl = 0 if force else 3600
    fills_list, rewards_list = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_fills(acc, ttl)),
        gather_accs(accs, lambda acc: sync_rewards(acc, ttl)),
    )

    gtrades: DD[list[dict]] = defaultdict(lambda: defaultdict(list))
    gpoints: DD[Decimal] = defaultdict(lambda: defaultdict(Decimal))

    for acc, fills in zip(accs, fills_list):
        for fill in fills:
            if not _is_hyena_fill(fill):
                continue
            dt = datetime.fromtimestamp(fill["time"] / 1000, tz=UTC)
            gtrades[HyenaClient.to_week_label(dt)][acc.name].append(fill)

    for acc, rewards in zip(accs, rewards_list):
        for h in rewards:
            gpoints[h["period"]][acc.name] += Decimal(str(h["enaxPoints"]))

    all_periods = sorted(gtrades.keys() | gpoints.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [acc.name for acc in accs]

    periods_data: dict[str, list[PeriodRow]] = {}
    for pk in all_periods:
        rows = []
        for name in all_names:
            fills = gtrades[pk].get(name, [])
            points = gpoints[pk].get(name, Decimal(0))
            if not fills and not points:
                continue
            vol = sum((Decimal(str(f["px"])) * Decimal(str(f["sz"])) for f in fills), Decimal(0))
            fee = sum((Decimal(str(f["fee"])) for f in fills), Decimal(0))
            pnl = sum((Decimal(str(f.get("closedPnl", 0))) for f in fills), Decimal(0))
            rows.append(PeriodRow(name, len(fills), vol, -pnl, points, fee))
        periods_data[pk] = rows

    render_stats(periods_data, periods_to_show, points_fmt="{:,.0f}", pprice_fmt="{:,.4f}")


def setup_reward_cli(parser):
    sub = parser.add_subparsers(dest="reward_action")
    sub.required = True
    sub.add_parser("claim", help="Claim rewards")


async def claim_rewards(accs: list[HyenaClient]):
    async def row(acc: HyenaClient):
        rewards = await acc.system_rewards()
        if not rewards.claimableReports:
            return (acc.name, Decimal(0), 0)

        claims = []
        for report in rewards.claimableReports:
            claim = await acc.claim_system_reward(report.id)
            claims.append(claim)

        amount = sum((c.amount for c in claims), Decimal(0))
        return (acc.name, amount, len(claims))

    with console.status("Claiming Hyena rewards..."):
        rows = await gather_accs(accs, row)

    amount = sum((r[1] for r in rows), Decimal(0))
    count = sum(r[2] for r in rows)
    accounts = ", ".join(r[0] for r in rows if r[2])
    if count:
        print(f"Claimed {amount:,.4f} USDE from {count} Hyena reward report(s): {accounts}")
    else:
        print("No claimable Hyena rewards.")


# MARK: Main


async def main():
    cli = await create_cli(
        "hyena",
        "configs/hyena.toml",
        ["privkey"],
        custom_commands={
            "migrate": lambda _: None,
            "reward": setup_reward_cli,
        },
    )
    cfg = StrategyConfig.load(cli.config)
    cfg.symbols = _normalize_symbols(cfg.symbols)

    accs = [(HyenaClient.from_config(x), x.enabled) for x in cfg.accounts]
    all_accs, act_accs = [c for c, _ in accs], [c for c, e in accs if e]

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "positions":
            await print_positions(act_accs)
        case "stats":
            await print_stats(all_accs, period=cli.group, filter_period=cli.filter, force=cli.force)
        case "reward":
            await claim_rewards(all_accs)
        case "close":
            await close_all(act_accs)
        case "migrate":
            await migrate_hyperliquid_accounts(all_accs, "Hyena")
        case "trade":
            await run_groups(cfg, act_accs)


if __name__ == "__main__":
    run_app(main())
