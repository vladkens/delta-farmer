# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License
"""Check and claim the Big Short base airdrop on BSC, with optional SHORT swaps.

Quickstart:
1. Install uv and run from the Delta Farmer repository root.
2. Check a public wallet (no private key required):
    uv run scripts/bigshort-claim.py check 0xYOUR_ADDRESS
3. Claim tokens; enter the wallet's private key locally when prompted (hidden input):
    uv run scripts/bigshort-claim.py claim 0xYOUR_ADDRESS
4. Choose Keep SHORT, USDT, or BNB, then review and confirm any swap.

Using existing config accounts:
    uv run scripts/bigshort-claim.py check ACCOUNT_NAME
    uv run scripts/bigshort-claim.py claim ACCOUNT_NAME
    uv run scripts/bigshort-claim.py claim -c configs/lighter.toml ACCOUNT_NAME

Swap options:
    --swap USDT    Select USDT on BSC (also selected by bare --swap or -s)
    --swap BNB     Select native BNB on BSC
    --swap none    Keep SHORT and skip the swap menu

How it works:
- With no command, shows this help. 'check' never signs or sends transactions.
- Account names are searched across configs/*.toml unless -c selects one file.
- Different stored key values require -c; they are not decrypted for comparison.
- With -c, a wallet address is also looked up in that config.
- Completed claims are not submitted again; saved claim transactions are resumed.
- After claiming (including an earlier claim), offers to swap the full SHORT balance via Relay.
- In a terminal, shows the quote, minimum received, loss, and gas before confirmation.
- Without a terminal, swaps require an explicit --swap and must pass the automatic limits.
- Swaps require at least $1. Loss above max($0.25, 2% of input value) needs explicit terminal confirmation.

Risks:
- Claims and swaps spend BNB for gas and cannot be undone after confirmation.
- Swaps sell all SHORT in the wallet, including tokens held before this claim.
- API or RPC failures can leave submitted transactions unconfirmed; check their hashes before retrying.
- Contract or API changes may break the script.
"""

import argparse
import asyncio
import getpass
import os
import re
import sys
import time
import tomllib
from decimal import Decimal
from functools import partial
from pathlib import Path
from uuid import uuid4

from curl_cffi.requests import errors
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from filelock import FileLock, Timeout
from rich.prompt import Prompt

from lib.decorators import retry_on
from lib.errors import AppError
from lib.evm import (
    BSC,
    MAX_TX_FEE_WEI,
    RPC,
    EvmToken,
    RelayQuote,
    check_chain,
    create_rpc,
    fee_cap,
    get_token_bal,
    make_call,
    prepare_tx,
    read_contract,
    relay_swap,
    sign_tx,
    to_addr,
    wait_receipt,
)
from lib.http import ApiError, AsyncHttp, HttpMethod
from lib.models import AccountConfig
from lib.utils import confirm, parse_eth_key

ORIGIN = "https://airdrop.bigshort.xyz"
REFERRAL_CODE = "EVM-S6VK"
REFERRAL_URL = f"{ORIGIN}/?ref={REFERRAL_CODE}"
CAMPAIGN = "airdrop-20260920-v1"
API = f"/api/v1/airdrops/{CAMPAIGN}"
CONFIGS = Path(__file__).resolve().parents[1] / "configs"
CACHE = Path(__file__).resolve().parents[1] / ".cache" / "bigshort"
SWAP_TARGETS = {"USDT": BSC.stables["USDT"], "BNB": BSC.native_token}
SWAP_MIN_USD = Decimal(1)


class Airdrop(AsyncHttp):
    def __init__(self, proxy: str | None = None):
        super().__init__(
            baseurl=ORIGIN,
            headers={"Origin": ORIGIN, "Referer": f"{ORIGIN}/", "Accept": "application/json"},
            proxy=proxy,
        )

    async def api(self, method: HttpMethod, path: str, **kwargs) -> dict:
        if method == "POST":
            session = await self.api("GET", "/api/v1/auth/session")
            kwargs["headers"] = {
                "X-CSRF-Token": session["csrfToken"],
                "Idempotency-Key": str(uuid4()),
            }
        response = await self.request(method, path, retry=False, **kwargs)
        if not response.ok:
            raise ApiError(path, response)
        return response.json()["data"]

    async def login(self, account: LocalAccount, identity: dict) -> None:
        challenge = await self.api(
            "POST", "/api/v1/auth/challenge", json={**identity, "purpose": "login", "chainId": 56}
        )
        message = challenge["message"]
        lines = message.splitlines()
        if not (
            message.startswith(
                f"airdrop.bigshort.xyz wants you to sign in with your Ethereum account:\n{account.address}\n"
            )
            and f"URI: {ORIGIN}" in lines
            and "Chain ID: 56" in lines
            and "- urn:bigshort:airdrop:login" in lines
        ):
            raise ValueError("Unexpected login message; nothing signed")
        await self.api(
            "POST",
            "/api/v1/auth/verify",
            json={
                "challengeId": challenge["challengeId"],
                "signature": personal_sign(account, message),
            },
        )

    @retry_on(ApiError, retries=2, delay=1)
    async def ensure_referral(self, identity: dict) -> None:
        state = await self.api("GET", f"{API}/referrals/me", params=identity)
        if state["invitedBy"] is not None or not state["bindingEnabled"]:
            return
        await self.api(
            "POST", f"{API}/referrals/bind", json={"identity": identity, "code": REFERRAL_CODE}
        )

    async def prepare_claim(self, identity: dict) -> dict:
        try:
            await self.ensure_referral(identity)
        except (ApiError, ValueError, KeyError):
            pass
        return await self.api("GET", f"{API}/account", params=identity)


def personal_sign(account: LocalAccount, message: str) -> str:
    return "0x" + account.sign_message(encode_defunct(text=message)).signature.hex()


def validate_receipt(receipt: dict, address: str) -> None:
    if receipt["kind"] != "base" or to_addr(receipt["recipient"]) != address:
        raise ValueError("Claim receipt does not match this wallet's base airdrop")
    if int(receipt["amountRaw"]) <= 0:
        raise ValueError("Claim amount must be positive")


def claim_call(config: dict, receipt: dict, authorization: dict, address: str) -> dict:
    validate_receipt(receipt, address)
    if not (
        int(authorization["chainId"]) == BSC.chain_id
        and authorization["receiptId"] == receipt["id"]
        and to_addr(authorization["claimant"]) == address
        and to_addr(authorization["proxy"]) == to_addr(config["contractAddress"])
        and to_addr(authorization["token"]) == to_addr(config["token"]["address"])
        and int(authorization["amount"]) == int(receipt["amountRaw"])
    ):
        raise ValueError("Authorization does not match the claim receipt and campaign")
    if int(authorization["deadline"]) <= time.time() + 30:
        raise ValueError("Claim authorization expires too soon; rerun to refresh it")
    return make_call(
        config["contractAddress"],
        "claim",
        ["uint256", "uint256", "bytes"],
        [
            int(authorization["amount"]),
            int(authorization["deadline"]),
            bytes.fromhex(authorization["signature"].removeprefix("0x")),
        ],
    )


async def submit_claim(rpc: RPC, account: LocalAccount, call: dict, pending: Path) -> str:
    tx = await prepare_tx(rpc, account, call)
    if tx["chainId"] != BSC.chain_id:
        raise ValueError("Wrong network for the claim")
    fee = tx["gas"] * fee_cap(tx)
    if fee > MAX_TX_FEE_WEI:
        raise ValueError(f"Gas fee too high: {Decimal(fee).scaleb(-18)} BNB")
    signed = sign_tx(account, tx)
    # Persist before broadcasting: an RPC timeout must not cause a second claim transaction.
    with pending.open("x") as fp:
        fp.write(signed.hash)
        fp.flush()
        os.fsync(fp.fileno())
    print(f"Transaction (saved before submission): {BSC.tx_url(signed.hash)}", flush=True)
    print(f"Maximum gas fee: {Decimal(fee).scaleb(-18):.8f} BNB", flush=True)
    sent = await rpc.call("eth_sendRawTransaction", signed.raw)
    if sent.lower() != signed.hash.lower():
        raise ValueError(f"RPC returned a different hash; check {signed.hash}")
    return signed.hash


async def settle_claim(api: Airdrop, rpc: RPC, identity: dict, tx_hash: str, pending: Path) -> None:
    print(f"Waiting for confirmation: {BSC.tx_url(tx_hash)}", flush=True)
    # Keep the hash on failure or timeout. Never replace a transaction of unknown status.
    try:
        receipt = await wait_receipt(rpc, tx_hash, timeout=120)
    except ApiError:
        failed = await rpc.call("eth_getTransactionReceipt", tx_hash)
        if failed is not None and int(failed["status"], 16) == 0:
            pending.unlink(missing_ok=True)
            raise ValueError(
                f"Transaction reverted: {BSC.tx_url(tx_hash)}. Rerun to try again."
            ) from None
        raise
    target_block = int(receipt["blockNumber"], 16) + 1
    async with asyncio.timeout(60):
        while int(await rpc.call("eth_blockNumber"), 16) < target_block:
            await asyncio.sleep(2)
    await api.api(
        "POST",
        f"{API}/claim/receipt",
        json={"identity": identity, "claimant": identity["address"], "hash": tx_hash},
    )
    pending.unlink(missing_ok=True)
    print(f"Claim confirmed: {BSC.tx_url(tx_hash)}")


def review_swap_quote(origin: EvmToken, target: EvmToken, quote: RelayQuote) -> bool:
    sold = Decimal(quote.input_amount).scaleb(-origin.decimals)
    received = Decimal(quote.output_amount).scaleb(-target.decimals)
    minimum = Decimal(quote.minimum_output_amount).scaleb(-target.decimals)
    assert quote.input_usd is not None
    assert quote.output_usd is not None
    loss_pct = quote.loss_usd / quote.input_usd * 100
    gas = Decimal(quote.required_gas).scaleb(-BSC.native_token.decimals)
    print(f"Swap on BSC: {sold.normalize():,f} {origin.symbol} (${quote.input_usd:,.2f})")
    print(f"Receive: {received.normalize():,f} {target.symbol} (${quote.output_usd:,.2f})")
    print(
        f"Minimum received: {minimum.normalize():,f} {target.symbol} "
        f"(${quote.minimum_output_usd:,.2f})"
    )
    print(
        f"Estimated loss at minimum output: ${quote.loss_usd:,.2f} ({loss_pct:.2f}%); "
        f"automatic limit: ${quote.loss_limit_usd:,.2f}"
    )
    print(f"Gas budget (estimate, paid separately): {gas:.8f} BNB")
    if quote.loss_usd > quote.loss_limit_usd:
        return confirm("Loss exceeds the automatic limit. Execute this swap anyway?")
    return confirm("Execute this swap?")


async def swap_claimed_token(
    account: LocalAccount, proxy: str | None, token: dict, swap: str | None
) -> None:
    interactive = sys.stdin.isatty()
    if swap == "NONE" or (swap is None and not interactive):
        return
    origin = EvmToken(token["symbol"], to_addr(token["address"]), token["decimals"])
    async with create_rpc(BSC, proxy) as rpc:
        balance = await get_token_bal(rpc, origin.address, account.address)
    if balance == 0:
        print(f"No {origin.symbol} balance to swap.")
        return
    if swap is None:
        amount = Decimal(balance).scaleb(-origin.decimals)
        print(f"Remaining balance: {amount.normalize():,f} {origin.symbol}")
        print(f"0. Keep {origin.symbol}\n1. Swap to USDT on BSC\n2. Swap to BNB on BSC")
        try:
            choice = Prompt.ask("Choose an option", choices=["0", "1", "2"], default="0")
        except EOFError:
            return
        if choice == "0":
            return
        swap = {"1": "USDT", "2": "BNB"}[choice]
    target = SWAP_TARGETS[swap]
    review = partial(review_swap_quote, origin, target) if interactive else None
    try:
        result = await relay_swap(
            account, BSC, origin, target, "max", proxy, SWAP_MIN_USD, review_quote=review
        )
    except (ValueError, ApiError, TimeoutError, errors.RequestsError) as error:
        raise AppError(f"Swap to {target.symbol} did not complete: {error}") from error
    if result is None:
        print("Swap cancelled.")


async def claim(account: LocalAccount, proxy: str | None, swap: str | None = None) -> None:
    identity = {"namespace": "evm", "address": account.address.lower()}
    print(f"Wallet: {account.address}")
    async with Airdrop(proxy) as api, create_rpc(BSC, proxy) as rpc:
        config = await api.api("GET", f"{API}/config")
        if config["chainId"] != BSC.chain_id or config["settlementMode"] != "mainnet":
            raise ValueError("Expected the BSC mainnet campaign")
        await check_chain(rpc, BSC.chain_id)
        token = await read_contract(
            rpc, config["contractAddress"], "claimToken", outputs=["address"]
        )
        if to_addr(token[0]) != to_addr(config["token"]["address"]):
            raise ValueError("The contract's token does not match the campaign")
        await api.login(account, identity)
        state = await api.prepare_claim(identity)
        pending = CACHE / f"{CAMPAIGN}-{account.address.lower()}.tx"
        if pending.exists():
            await settle_claim(api, rpc, identity, pending.read_text().strip(), pending)
            await swap_claimed_token(account, proxy, config["token"], swap)
            return
        receipt = state["claimed"]
        if receipt is None:
            intent = await api.api(
                "POST",
                f"{API}/claim/intents",
                json={"identity": identity, "kind": "base", "recipient": identity["address"]},
            )
            validate_receipt(intent, account.address)
            lines = intent["message"].splitlines()
            expected = (
                f"Origin: {ORIGIN}",
                f"Campaign: {CAMPAIGN}",
                "Action: base",
                f"Source: evm:{identity['address']}",
                f"BSC Recipient: {identity['address']}",
                f"Preview Amount (18 decimals): {intent['amountRaw']}",
                f"Request ID: {intent['id']}",
            )
            if not all(line in lines for line in expected):
                raise ValueError("Unexpected claim message; nothing signed")
            receipt = await api.api(
                "POST",
                f"{API}/claim/confirm",
                json={
                    "identity": identity,
                    "intentId": intent["id"],
                    "signature": personal_sign(account, intent["message"]),
                    "outcome": "success",
                },
            )
        validate_receipt(receipt, account.address)
        if receipt["settlementMode"] == "chain":
            print(f"Already claimed: {receipt.get('txHash')}")
            await swap_claimed_token(account, proxy, config["token"], swap)
            return
        if receipt["settlementMode"] != "pending":
            raise ValueError(f"Unexpected claim state: {receipt['settlementMode']}")

        amount = Decimal(receipt["amountRaw"]).scaleb(-config["token"]["decimals"])
        print(f"Claim: {amount:,.0f} {config['token']['symbol']} on BSC")
        tx_hash = receipt.get("txHash")
        if not tx_hash:
            auth = await api.api(
                "POST",
                f"{API}/claim/authorize",
                json={
                    "identity": identity,
                    "claimant": identity["address"],
                    "receiptId": receipt["id"],
                },
            )
            call = claim_call(config, receipt, auth, account.address)
            await rpc.call("eth_call", {**call, "from": account.address}, "latest")
            tx_hash = await submit_claim(rpc, account, call, pending)
        await settle_claim(api, rpc, identity, tx_hash, pending)
        await swap_claimed_token(account, proxy, config["token"], swap)


def eligibility_summary(data: dict, token: dict) -> str:
    receipt = data.get("claimed")
    status = data["eligibility"]["status"]
    if receipt:
        mode = receipt["settlementMode"]
        label = {"chain": "Already claimed", "pending": "Pending claim"}.get(
            mode, f"Claim status: {mode}"
        )
        raw = receipt["amountRaw"]
    elif status == "eligible":
        allocation = data.get("allocation") or {}
        raw = allocation.get("currentRaw")
        label = "Eligible"
        if raw is None:
            raw = allocation.get("estimatedRaw")
            label = "Eligible (estimated)"
        if raw is None:
            return "Eligible; allocation is not available yet"
    elif status == "not_in_snapshot":
        return "No airdrop: address is not in the snapshot"
    elif status == "data_not_ready":
        return "Eligibility data is not ready yet"
    else:
        return f"Eligibility status: {status}"

    amount = Decimal(raw).scaleb(-token["decimals"]).normalize()
    return f"{label}: {amount:,f} {token['symbol']}"


async def check(address: str, proxy: str | None) -> None:
    address = to_addr(address.strip())
    async with Airdrop(proxy) as api:
        config, data = await asyncio.gather(
            api.api("GET", f"{API}/config"),
            api.api(
                "GET", f"{API}/eligibility", params={"namespace": "evm", "address": address.lower()}
            ),
        )
    print(f"Wallet: {address}")
    print(f"Campaign: {'paused' if config['paused'] else config['status']}")
    print(eligibility_summary(data, config["token"]))
    receipt = data.get("claimed")
    if receipt and receipt.get("txHash"):
        print(f"Transaction: {BSC.tx_url(receipt['txHash'])}")


def target_address(target: str) -> str | None:
    if not target.lower().startswith("0x"):
        return None
    if not re.fullmatch(r"0x[0-9a-f]{40}", target, re.IGNORECASE):
        raise ValueError(
            "Invalid wallet address: expected 0x followed by 40 hexadecimal characters"
        )
    return to_addr(target)


def load_account(config_path: str | None, target: str) -> tuple[LocalAccount, str | None]:
    address = target_address(target)
    paths = [Path(config_path)] if config_path else sorted(CONFIGS.glob("*.toml"))
    rows = []
    for path in paths:
        with path.open("rb") as fp:
            try:
                accounts = tomllib.load(fp).get("accounts", [])
            except tomllib.TOMLDecodeError as error:
                raise ValueError(f"Invalid TOML in {os.path.relpath(path)}: {error}") from None
        rows.extend(
            (path, row) for row in accounts if address is not None or row.get("name") == target
        )

    if not config_path and address is None:
        seen_keys = set()
        unique_rows = []
        for path, row in rows:
            stored_key = row.get("privkey")
            if stored_key in seen_keys:
                continue
            seen_keys.add(stored_key)
            unique_rows.append((path, row))
        if len(unique_rows) > 1:
            files = "\n".join(
                f"- {os.path.relpath(path)}"
                for path in dict.fromkeys(path for path, _ in unique_rows)
            )
            raise ValueError(
                f"Account '{target}' has different stored key values in:\n{files}\n"
                "Use -c FILE to select a config."
            )
        rows = unique_rows

    matches = []
    for _, row in rows:
        config = AccountConfig.model_validate(row)
        account = parse_eth_key(config.privkey.get_secret_value(), config.name)
        if address is None or account.address == address:
            matches.append((account, config.proxy))
    if len(matches) != 1:
        source = os.path.relpath(config_path or CONFIGS / "*.toml")
        raise ValueError(f"Expected exactly one account matching {target} in {source}")
    return matches[0]


def main() -> None:
    epilog = f"Airdrop website: {REFERRAL_URL}"
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command")
    claim_parser = commands.add_parser(
        "claim",
        help="Claim tokens on BSC",
        description="Claim by account name or wallet address. Without -c, an address prompts for its private key.",
        epilog=epilog,
    )
    claim_parser.add_argument(
        "-s",
        "--swap",
        nargs="?",
        const="USDT",
        type=str.upper,
        choices=["USDT", "BNB", "NONE"],
        metavar="USDT|BNB|none",
        help="Swap all SHORT on BSC (bare flag: USDT; default: ask in a terminal; none: skip)",
    )
    check_parser = commands.add_parser(
        "check",
        help="Check airdrop eligibility and claim status",
        description="Check a public address or config account without signing or sending transactions.",
        epilog=epilog,
    )
    for command_parser in (claim_parser, check_parser):
        command_parser.add_argument(
            "-c",
            "--config",
            help="Use one TOML config (default: search names across configs/*.toml)",
        )
        command_parser.add_argument(
            "target", metavar="ACCOUNT_OR_ADDRESS", help="Account name or 0x-prefixed EVM address"
        )
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg in ("-s", "--swap") and (
            i + 1 == len(argv) or argv[i + 1].upper() not in (*SWAP_TARGETS, "NONE")
        ):
            argv[i] = "--swap=USDT"
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    try:
        address = target_address(args.target)
        account = None
        proxy = None
        if args.config or address is None:
            account, proxy = load_account(args.config, args.target)
            address = account.address
        if args.command == "check":
            asyncio.run(check(address, proxy))
            return

        if account is None:
            account = parse_eth_key(getpass.getpass("Wallet private key (hidden, local input): "))
            if account.address != address:
                raise ValueError("The private key does not match the requested wallet address")
        CACHE.mkdir(parents=True, exist_ok=True)
        with FileLock(CACHE / f"{account.address.lower()}.lock", timeout=0):
            asyncio.run(claim(account, proxy, args.swap))
    except Timeout:
        parser.exit(1, "Another claim process is already running for this wallet.\n")
    except (AppError, ApiError, ValueError, OSError, TimeoutError, errors.RequestsError) as error:
        parser.exit(1, f"{error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted. Any saved transaction will be checked on the next run.\n")


if __name__ == "__main__":
    main()
