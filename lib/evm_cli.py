# Copyright (c) vladkens | MIT License | https://github.com/vladkens/delta-farmer
import argparse
import asyncio
from dataclasses import dataclass
from decimal import Decimal

from eth_account.signers.local import LocalAccount
from rich import print as console_print

from .errors import AppError
from .evm import (
    EVM_NETWORKS,
    ZERO_ADDRESS,
    EvmNetwork,
    get_evm_balances,
    relay_move,
    resolve_evm_asset,
    to_token_units,
    transfer_evm_asset,
)
from .logger import logger
from .models import AccountConfig
from .table import AutoTable, Column
from .utils import confirm, parse_eth_key

EVM_START_DELAY_SEC = 1.5
EVM_MIN_MOVE_AMOUNT = Decimal(10)


@dataclass
class WalletState:
    config: AccountConfig
    account: LocalAccount
    balances: dict[str, Decimal]
    errors: dict[str, str]


type EvmAction = tuple[str, WalletState, str, str, str]


def setup_evm_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-a", "--account", default=None, help="Use one enabled account")
    sub = parser.add_subparsers(dest="evm_command")
    move = sub.add_parser("move", help="Move one EVM asset within each wallet")
    move.add_argument("source", help="Source such as arb:usdc")
    move.add_argument("target", help="Destination such as base:usdc or cex")
    move.add_argument("-a", "--account", default=argparse.SUPPRESS)
    move.add_argument(
        "-q",
        dest="amount",
        metavar="QUANTITY",
        type=Decimal,
        default=None,
        help="Quantity per account; max by default",
    )


def _networks() -> list[EvmNetwork]:
    return list(EVM_NETWORKS.values())


def _format_amount(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def _stable_balances(state: WalletState) -> list[tuple[str, str, Decimal]]:
    rows = []
    for asset, balance in state.balances.items():
        network, token = resolve_evm_asset(asset)
        if token.symbol not in network.stables or balance < Decimal("0.01"):
            continue

        rows.append((network.code, token.symbol.lower(), balance))

    rows.sort(key=lambda row: row[2], reverse=True)
    return rows


def _select_accounts(accounts: list[AccountConfig], name: str | None) -> list[AccountConfig]:
    if name is None:
        return [account for account in accounts if account.enabled]

    account = next((account for account in accounts if account.name == name), None)
    if account is None:
        raise AppError(f"Unknown account: {name}")
    if not account.enabled:
        raise AppError(f"Account is disabled: {name}")

    return [account]


def _validate_cex_addrs(accounts: list[AccountConfig]) -> None:
    missing = [account.name for account in accounts if account.cex_addr is None]
    if missing:
        raise AppError(f"Missing CEX address: {', '.join(missing)}")

    for account in accounts:
        assert account.cex_addr is not None
        if account.cex_addr.lower() == ZERO_ADDRESS:
            raise ValueError(f"CEX address is the zero address: {account.name}")


async def _scan_wallet(config: AccountConfig) -> WalletState:
    account = parse_eth_key(config.privkey.get_secret_value(), config.name)
    networks = _networks()
    results = await asyncio.gather(
        *(
            get_evm_balances(
                network,
                account.address,
                (*network.stables.values(), network.native_token),
                config.proxy,
            )
            for network in networks
        ),
        return_exceptions=True,
    )

    balances: dict[str, Decimal] = {}
    errors: dict[str, str] = {}
    for network, result in zip(networks, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            errors[network.code] = str(result)
        else:
            for token, balance in result.items():
                balances[network.asset_code(token)] = balance

    return WalletState(config, account, balances, errors)


async def _scan(accounts: list[AccountConfig]) -> list[WalletState]:
    return list(await asyncio.gather(*(_scan_wallet(account) for account in accounts)))


def _print_status(states: list[WalletState]) -> None:
    table = AutoTable(
        Column("Account", justify="left"),
        Column("Network", justify="left"),
        Column("Asset", justify="left"),
        Column("Balance"),
    )
    empty_accounts = []

    for state in states:
        rows = _stable_balances(state)
        for network, token, balance in rows:
            table.add_row(
                state.config.name,
                network,
                token,
                _format_amount(balance),
            )

        for network in state.errors:
            table.add_row(
                state.config.name,
                network,
                "",
                "[yellow]unavailable[/yellow]",
            )

        if not rows and not state.errors:
            empty_accounts.append(state.config.name)

    if empty_accounts:
        table.set_footer("No stables:", ", ".join(empty_accounts), "", "")

    table.print()
    for state in states:
        for network, error in state.errors.items():
            logger.warning(f"{state.config.name}: {network} balance unavailable: {error}")

    print("Move assets between networks within each wallet:")
    print("  Max, all accounts:    evm move arb:usdc base:usdc")
    print("  Max, one account:     evm move arb:usdc base:usdc -a mx01")
    print("  Quantity per account: evm move arb:usdc base:usdc -q 50")


def _plan_table(plan: list[EvmAction]) -> AutoTable:
    table = AutoTable(
        Column("Account", justify="left"),
        Column("Operation", justify="left"),
        Column("From", justify="left"),
        Column("To", justify="left"),
        Column("Quantity"),
    )
    for operation, state, source, target, quantity in plan:
        table.add_row(
            state.config.name,
            operation,
            source,
            target,
            quantity if quantity == "max" else _format_amount(Decimal(quantity)),
        )

    return table


async def _execute_plan(plan: list[EvmAction]) -> None:
    for operation, state, source, target, quantity in plan:
        with logger.contextualize(account=state.config.name):
            if operation == "move":
                await relay_move(
                    state.account,
                    source,
                    target,
                    quantity,
                    state.config.proxy,
                    EVM_MIN_MOVE_AMOUNT,
                )
                continue

            if operation == "transfer":
                await transfer_evm_asset(
                    state.account,
                    source,
                    target,
                    quantity,
                    state.config.proxy,
                )
                continue

            raise ValueError(f"Unknown EVM operation: {operation}")


def _make_plan(
    states: list[WalletState],
    source: str,
    target: str,
    amount: Decimal | None,
) -> tuple[list[EvmAction], list[tuple[str, str]]]:
    if amount is not None and (not amount.is_finite() or amount <= 0):
        raise AppError("EVM move amount must be positive")

    source_network, source_token = resolve_evm_asset(source)
    if amount is not None:
        to_token_units(amount, source_token)

    cex = target == "cex"
    if not cex:
        target_network, target_token = resolve_evm_asset(target)

        if (source_network, source_token) == (target_network, target_token):
            raise AppError("EVM source and destination are the same")

    plan: list[EvmAction] = []
    skipped: list[tuple[str, str]] = []
    for state in states:
        if source_network.code in state.errors:
            error = state.errors[source_network.code]
            raise AppError(
                f"{state.config.name}: {source_network.code} balance unavailable: {error}"
            )

        balance = state.balances.get(source, Decimal(0))
        requested = balance if amount is None else amount
        if requested > balance:
            skipped.append((state.config.name, f"insufficient {source} balance"))
            continue

        if requested <= 0:
            skipped.append((state.config.name, f"no {source} balance"))
            continue

        if source_token.address.lower() != ZERO_ADDRESS and requested < EVM_MIN_MOVE_AMOUNT:
            skipped.append((state.config.name, f"{source} quantity is uneconomical"))
            continue

        if (
            source_token.address.lower() == ZERO_ADDRESS
            and amount is not None
            and requested == balance
        ):
            skipped.append((state.config.name, f"insufficient {source} balance for gas"))
            continue

        action_quantity = (
            "max"
            if amount is None and source_token.address.lower() == ZERO_ADDRESS
            else str(requested)
        )
        if not cex:
            plan.append(("move", state, source, target, action_quantity))
            continue

        assert state.config.cex_addr is not None
        plan.append(("transfer", state, source, state.config.cex_addr, action_quantity))

    return plan, skipped


async def _move(
    states: list[WalletState],
    source_value: str,
    target_value: str,
    amount: Decimal | None,
) -> None:
    source = source_value.lower()
    target = target_value.lower()
    cex = target == "cex"
    plan, skipped = _make_plan(states, source, target, amount)

    quantity = _format_amount(amount) if amount is not None else "max"
    print(f"{source} -> {target}, quantity: {quantity}")
    print()
    for account, reason in skipped:
        logger.warning(f"{account}: {reason}")

    if not plan:
        logger.info("No EVM moves planned")
        return

    _plan_table(plan).print()
    if cex:
        console_print(
            f"[orange1]Warning: Make sure your exchange supports {source} deposits. Using an "
            "unsupported network may result in permanent loss of funds.[/orange1]"
        )
    if not confirm("Execute EVM plan?"):
        logger.info("EVM plan cancelled")
        return

    await asyncio.sleep(EVM_START_DELAY_SEC)
    print("-" * 60)
    await _execute_plan(plan)


async def run_evm(
    args: argparse.Namespace,
    accounts: list[AccountConfig],
) -> None:
    accounts = _select_accounts(accounts, getattr(args, "account", None))
    if not accounts:
        raise AppError("No enabled accounts configured")

    if args.evm_command == "move" and args.target.lower() == "cex":
        _validate_cex_addrs(accounts)

    states = await _scan(accounts)
    if args.evm_command is None:
        _print_status(states)
        return
    if args.evm_command == "move":
        await _move(states, args.source, args.target, args.amount)
        return

    raise AppError(f"Unknown EVM command: {args.evm_command}")
