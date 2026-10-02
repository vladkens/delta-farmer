# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Crafted with love and ctrl+c
import asyncio
from collections import defaultdict
from decimal import Decimal
from functools import partial

from clients.ethereal import (
    REWARD_BRIDGE_DUST,
    EtherealClient,
    EtherealPoint,
    EtherealPosition,
    EtherealReward,
    EtherealRewardBalances,
)
from lib.cli import create_cli, noop_command, print_deprecation_notice, run_app, setup_app
from lib.errors import AppError
from lib.evm import ETHEREAL, RelayQuote, resolve_evm_asset
from lib.logger import logger
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import confirm, gather_accs, parse_filter, short_addr, to_period_day
from strategy import AppConfig
from strategy.runner import close_all, print_positions, run_strategy

# MARK: Storages


async def sync_trades(acc: EtherealClient, ttl: int) -> list[EtherealPosition]:
    store_path = f".cache/ethereal_{short_addr(acc.address)}_trades.pkl"
    store = DataStore(store_path, id_key="id", model=EtherealPosition)
    await store.sync(lambda _: acc.raw_positions(open_only=False), ttl_sec=ttl)
    return store.get_all()


async def sync_points(acc: EtherealClient, ttl: int) -> list[EtherealPoint]:
    store_path = f".cache/ethereal_{short_addr(acc.address)}_points.pkl"
    store = DataStore(store_path, id_key="id", model=EtherealPoint)
    await store.sync(lambda _: acc.points(), ttl_sec=ttl)
    return store.get_all()


# MARK: Reports


async def print_info(accs: list[EtherealClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.0f}", total=sum),
        Column("P/Price", "{:,.4f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
    )

    async def row(acc: EtherealClient):
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0)
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance)

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()


async def print_stats(accs: list[EtherealClient], period="week", filter_period="all", force=False):
    gpos: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    gpts: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    ttl = 0 if force else 3600

    period_fn = to_period_day if period == "day" else EtherealClient.to_week_label

    all_trades, all_points = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_trades(acc, ttl)),
        gather_accs(accs, lambda acc: sync_points(acc, ttl)),
    )
    for acc, trades in zip(accs, all_trades):
        for t in trades:
            gpos[period_fn(t.created_at)][acc.name].append(t)
    for acc, pts in zip(accs, all_points):
        for p in pts:
            gpts[period_fn(p.started_at)][acc.name] += p.total_points

    all_periods = sorted(gpos.keys() | gpts.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [x.name for x in accs]

    periods_data: dict[str, list[PeriodRow]] = {}
    for p in all_periods:
        acc_names = [n for n in all_names if n in (gpos[p].keys() | gpts[p].keys())]
        rows = []
        for acc_name in acc_names:
            positions = gpos[p][acc_name]
            pts = gpts.get(p, {}).get(acc_name, Decimal(0))
            if not positions and pts < 1:
                continue
            vol = sum((pos.total_inc + pos.total_dec for pos in positions), Decimal(0))
            fee = sum((pos.fees_usd for pos in positions), Decimal(0))
            fnd = sum((pos.funding_usd for pos in positions), Decimal(0))
            pnl = sum((pos.realized_pnl for pos in positions), Decimal(0)) - fee - fnd
            rows.append(PeriodRow(acc_name, len(positions), vol, -pnl, pts, fee))
        periods_data[p] = rows

    render_stats(periods_data, periods_to_show, points_fmt="{:,.0f}", pprice_fmt="{:,.4f}")


async def load_rewards(
    accs: list[EtherealClient], claimable_only: bool = False
) -> list[tuple[EtherealClient, list[EtherealReward]]]:
    rewards = await gather_accs(accs, lambda acc: acc.rewards(claimable_only))
    rows = []
    for acc, items in zip(accs, rewards, strict=True):
        if claimable_only:
            items = [reward for reward in items if reward.claimable > 0]
        if items:
            rows.append((acc, items))
    return rows


def reward_totals(
    rows: list[tuple[EtherealClient, list[EtherealReward]]], field: str
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for _acc, rewards in rows:
        for reward in rewards:
            totals[reward.token.symbol] += getattr(reward, field)

    return totals


def format_rewards(totals: dict[str, Decimal]) -> str:
    return ", ".join(
        f"{amount:,.6f}".rstrip("0").rstrip(".") + f" {symbol}"
        for symbol, amount in sorted(totals.items())
    )


async def print_reward_notice(accs: list[EtherealClient]) -> None:
    try:
        rows = await load_rewards(accs, claimable_only=True)
    except AppError as error:
        logger.warning(f"Ethereal rewards unavailable: {error}")
        rows = []

    totals = reward_totals(rows, "claimable")
    if totals:
        accounts = ", ".join(acc.name for acc, _rewards in rows)
        print(
            f"* Claimable Ethereal rewards: {format_rewards(totals)} ({accounts}). "
            "Run `uv run apps/ethereal.py claim`."
        )

    try:
        balances = await load_reward_balances(accs)
    except AppError as error:
        logger.warning(f"Ethereal reward balances unavailable: {error}")
        return

    for field, symbol, action in (("wrapped", "WUSDe", "unwrap"), ("native", "USDe", "bridge")):
        items = [(acc, getattr(balance, field)) for acc, balance in balances]
        items = [(acc, amount) for acc, amount in items if amount > REWARD_BRIDGE_DUST]
        if items:
            amount = sum((amount for _acc, amount in items), Decimal(0))
            accounts = ", ".join(acc.name for acc, _amount in items)
            print(
                f"* Ethereal {action}: {format_rewards({symbol: amount})} ({accounts}). "
                "Run `uv run apps/ethereal.py claim` to continue recovery."
            )


async def load_reward_balances(
    accs: list[EtherealClient],
) -> list[tuple[EtherealClient, EtherealRewardBalances]]:
    balances = await gather_accs(accs, lambda acc: acc.reward_balances())
    return list(zip(accs, balances, strict=True))


def review_bridge_quote(name: str, target: str, quote: RelayQuote) -> bool:
    network, token = resolve_evm_asset(target)
    amount = Decimal(quote.input_amount).scaleb(-ETHEREAL.native_token.decimals)
    received = Decimal(quote.output_amount).scaleb(-token.decimals)
    minimum = Decimal(quote.minimum_output_amount).scaleb(-token.decimals)
    gas = Decimal(quote.required_gas).scaleb(-ETHEREAL.native_token.decimals)
    print(f"{name}: bridge {amount:,.6f} USDe → {network.name.title()} {token.symbol}")
    print(f"Receive: {received:,.6f} {token.symbol}; minimum: {minimum:,.6f}")
    print(f"Estimated loss at minimum output: ${quote.loss_usd:,.4f}; gas budget: {gas:.8f} USDe")
    if quote.loss_usd > quote.loss_limit_usd:
        return confirm("Loss exceeds the automatic limit. Execute this bridge anyway?")
    return confirm("Execute this bridge?")


async def claim_rewards(accs: list[EtherealClient]) -> None:
    try:
        rows = await load_rewards(accs, claimable_only=True)
    except AppError as error:
        logger.warning(f"Ethereal rewards unavailable: {error}")
        rows = []

    balances = await load_reward_balances(accs)

    for acc, rewards in rows:
        acc.reward_claim_call(rewards)

    if rows:
        totals = reward_totals(rows, "claimable")
        accounts = ", ".join(acc.name for acc, _rewards in rows)
        print(f"Claimable: {format_rewards(totals)} ({accounts}).")
        if not confirm("Claim these Ethereal rewards?"):
            return

        for acc, rewards in rows:
            await acc.claim_rewards(rewards)

        print(f"Claimed Ethereal rewards: {format_rewards(totals)}.")
        balances = await load_reward_balances(accs)

    wrapped = [(acc, balance.wrapped) for acc, balance in balances if balance.wrapped > 0]
    if wrapped:
        amount = sum((amount for _acc, amount in wrapped), Decimal(0))
        accounts = ", ".join(acc.name for acc, _amount in wrapped)
        print(f"Unwrap: {format_rewards({'WUSDe': amount})} ({accounts}).")
        if not confirm("Unwrap these rewards to USDe?"):
            return

        for acc, amount in wrapped:
            await acc.unwrap_rewards(amount)

        balances = await load_reward_balances(accs)

    bridge = [acc for acc, balance in balances if balance.native > REWARD_BRIDGE_DUST]
    targets = await gather_accs(bridge, lambda acc: acc.reward_destination())
    for acc, target in zip(bridge, targets, strict=True):
        result = await acc.bridge_rewards(target, partial(review_bridge_quote, acc.name, target))
        if result is None:
            print(f"{acc.name}: bridge cancelled.")

    if not rows and not wrapped and not bridge:
        print("No Ethereal rewards or wallet balances to recover.")


# MARK: Main


async def main():
    cli = await create_cli(
        "ethereal",
        "configs/ethereal.toml",
        ["privkey"],
        custom_commands={"claim": lambda _: None},
    )
    cfg = AppConfig.load(cli.config)

    all_accs, act_accs = await setup_app(cli, cfg, EtherealClient.from_config)
    print_deprecation_notice("Ethereal closed; removed next release. https://app.ethereal.trade/")

    match cli.command:
        case "info":
            await print_info(all_accs)
            await print_reward_notice(all_accs)
        case "stats":
            await print_stats(all_accs, period=cli.group, filter_period=cli.filter, force=cli.force)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_strategy(cfg, act_accs, cli.pool)
        case "positions":
            await print_positions(act_accs)
        case "claim":
            await claim_rewards(all_accs)
        case "deposit" | "withdraw":
            noop_command("Ethereal", cli.command)


if __name__ == "__main__":
    run_app(main())
