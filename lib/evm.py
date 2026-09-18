import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from eth_abi import decode, encode
from eth_account.signers.local import LocalAccount
from eth_account.types import TransactionDictType
from eth_utils import keccak, to_checksum_address

from .http import ApiError, AsyncHttp
from .logger import logger

MAX_TX_FEE_WEI = 10**16


@dataclass(frozen=True)
class EvmToken:
    symbol: str
    address: str
    decimals: int


@dataclass(frozen=True)
class EvmNetwork:
    name: str
    chain_id: int
    rpc_url: str
    tokens: Mapping[str, EvmToken]
    explorer_url: str | None = None

    def tx_url(self, tx_hash: str) -> str:
        if self.explorer_url is None:
            return tx_hash
        return f"{self.explorer_url.rstrip('/')}/tx/{tx_hash}"


ROBINHOOD = EvmNetwork(
    name="robinhood",
    chain_id=4663,
    rpc_url="https://rpc.mainnet.chain.robinhood.com",
    tokens={
        "USDG": EvmToken("USDG", "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168", 6),
    },
)
BASE = EvmNetwork(
    name="base",
    chain_id=8453,
    rpc_url="https://mainnet.base.org",
    tokens={
        "USDC": EvmToken("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
    },
)
ARBITRUM = EvmNetwork(
    name="arbitrum",
    chain_id=42161,
    rpc_url="https://arb1.arbitrum.io/rpc",
    explorer_url="https://arbiscan.io",
    tokens={
        "USDC": EvmToken("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
    },
)
AVALANCHE = EvmNetwork(
    name="avalanche",
    chain_id=43114,
    rpc_url="https://api.avax.network/ext/bc/C/rpc",
    tokens={},
)
HYPERLIQUID = EvmNetwork(
    name="hyperliquid",
    chain_id=999,
    rpc_url="https://rpc.hyperliquid.xyz/evm",
    tokens={
        "USDC": EvmToken("USDC", "0xb88339CB7199b77E23DB6E890353E22632Ba630f", 6),
    },
)


class RPCError(ApiError):
    def __init__(self, method: str, code: int | None, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{method}: RPC error {code}: {message}")


class RPC:
    def __init__(self, url: str, network: str, proxy: str | None = None):
        self.network = network
        self.http = AsyncHttp(
            baseurl=url,
            headers={"Content-Type": "application/json"},
            proxy=proxy,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.close()

    async def close(self) -> None:
        await self.http.close()

    async def call(self, method: str, *params) -> Any:
        response = await self.http.request(
            "POST",
            "/",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)},
        )
        if not response.ok:
            raise ApiError(f"{self.network} RPC error", response)

        body = response.json()
        if body.get("error"):
            error = body["error"]
            raise RPCError(method, error.get("code"), str(error.get("message", "")))
        return body["result"]


def create_rpc(network: EvmNetwork, proxy: str | None = None) -> RPC:
    return RPC(network.rpc_url, network.name, proxy)


async def check_chain(rpc: RPC, expected: int) -> None:
    chain_id = int(await rpc.call("eth_chainId"), 16)
    if chain_id != expected:
        raise ApiError(f"Wrong RPC chain: expected {expected}, got {chain_id}")


@dataclass(frozen=True)
class SignedTransaction:
    hash: str
    raw: str


def to_addr(address: str | int) -> str:
    return to_checksum_address(f"0x{address:040x}" if isinstance(address, int) else address)


def to_token_units(amount: Decimal, token: EvmToken) -> int:
    units = amount.scaleb(token.decimals)
    if amount <= 0 or not amount.is_finite() or units != units.to_integral_value():
        raise ValueError(f"Invalid {token.symbol} amount: {amount}")

    return int(units)


def make_call(address: str, name: str, types: Sequence[str] = (), args: Sequence = ()) -> dict:
    signature = f"{name}({','.join(types)})"
    data = keccak(text=signature)[:4] + encode(types, args)
    return {"to": to_addr(address), "data": "0x" + data.hex()}


async def read_contract(
    rpc: RPC,
    address: str,
    name: str,
    types: Sequence[str] = (),
    args: Sequence = (),
    outputs: Sequence[str] = (),
    block: int | str = "latest",
) -> tuple:
    result = await rpc.call(
        "eth_call",
        make_call(address, name, types, args),
        hex(block) if isinstance(block, int) else block,
    )
    return decode(outputs, bytes.fromhex(result[2:]))


def rpc_tx(tx: dict) -> dict:
    return {key: hex(value) if isinstance(value, int) else value for key, value in tx.items()}


async def get_token_bal(rpc: RPC, token: str, owner: str) -> int:
    return (await read_contract(rpc, token, "balanceOf", ["address"], [owner], ["uint256"]))[0]


async def get_token_allowance(rpc: RPC, token: str, owner: str, spender: str) -> int:
    args = [owner, spender]
    result = await read_contract(rpc, token, "allowance", ["address", "address"], args, ["uint256"])
    return result[0]


async def check_token_balance(
    rpc: RPC,
    token: EvmToken,
    owner: str,
    amount: int,
) -> None:
    balance = await get_token_bal(rpc, token.address, owner)
    if balance < amount:
        unit = Decimal(1).scaleb(-token.decimals)
        raise ApiError(
            f"Insufficient {token.symbol} on {rpc.network}: "
            f"{Decimal(balance) * unit} < {Decimal(amount) * unit}"
        )


async def get_native_bal(rpc: RPC, owner: str) -> int:
    return int(await rpc.call("eth_getBalance", owner, "latest"), 16)


async def get_wallet_balances(
    network: EvmNetwork,
    token: EvmToken,
    owner: str,
    proxy: str | None = None,
) -> tuple[Decimal, Decimal]:
    async with create_rpc(network, proxy) as rpc:
        await check_chain(rpc, network.chain_id)
        token_balance = await get_token_bal(rpc, token.address, owner)
        native_balance = await get_native_bal(rpc, owner)
    return Decimal(token_balance).scaleb(-token.decimals), Decimal(native_balance).scaleb(-18)


async def dynamic_fee_params(rpc: RPC) -> dict:
    block = await rpc.call("eth_getBlockByNumber", "latest", False)
    base_fee = block.get("baseFeePerGas")
    if base_fee is None:
        return {"gasPrice": int(await rpc.call("eth_gasPrice"), 16)}

    base_fee = int(base_fee, 16)
    try:
        priority = int(await rpc.call("eth_maxPriorityFeePerGas"), 16)
    except RPCError as error:
        if error.code not in (-32601, -32004):
            raise
        priority = max(int(await rpc.call("eth_gasPrice"), 16) - base_fee, 0)

    return {"type": 2, "maxPriorityFeePerGas": priority, "maxFeePerGas": 2 * base_fee + priority}


def fee_cap(tx: dict) -> int:
    return int(tx["maxFeePerGas"] if "maxFeePerGas" in tx else tx["gasPrice"])


async def prepare_tx(rpc: RPC, account: LocalAccount, call: dict) -> dict:
    nonce = int(await rpc.call("eth_getTransactionCount", account.address, "pending"), 16)
    latest_nonce = int(await rpc.call("eth_getTransactionCount", account.address, "latest"), 16)
    if nonce != latest_nonce:
        raise ValueError("Wallet has pending transactions; wait before continuing")

    tx: dict = {
        **call,
        "from": account.address,
        "nonce": nonce,
        "chainId": int(await rpc.call("eth_chainId"), 16),
    }
    estimate = 0
    for attempt in range(3):
        for key in ("gasPrice", "type", "maxFeePerGas", "maxPriorityFeePerGas"):
            tx.pop(key, None)
        tx.update(await dynamic_fee_params(rpc))
        try:
            estimate = int(await rpc.call("eth_estimateGas", rpc_tx(tx)), 16)
            break
        except RPCError as error:
            if (
                "max fee per gas less than block base fee" not in error.message.lower()
                or attempt == 2
            ):
                raise
            await asyncio.sleep(3)

    tx["gas"] = (estimate * 120 + 99) // 100
    maximum = int(tx["gas"]) * fee_cap(tx) + int(tx.get("value", 0))
    balance = await get_native_bal(rpc, account.address)
    if balance < maximum:
        raise ValueError("Not enough native currency for the transaction and gas")
    return tx


def sign_tx(account: LocalAccount, tx: dict) -> SignedTransaction:
    if to_addr(str(tx["from"])) != account.address:
        raise ValueError("Transaction sender does not match the selected account")

    signed = account.sign_transaction(
        cast(TransactionDictType, {key: value for key, value in tx.items() if key != "from"})
    )
    return SignedTransaction(
        hash="0x" + bytes(signed.hash).hex(),
        raw="0x" + bytes(signed.raw_transaction).hex(),
    )


async def send_contract(
    rpc: RPC,
    account: LocalAccount,
    call: dict,
    max_fee_wei: int = MAX_TX_FEE_WEI,
) -> tuple[str, Decimal]:
    tx = await prepare_tx(rpc, account, call)
    fee = int(tx["gas"]) * fee_cap(tx)
    fee_native = Decimal(fee).scaleb(-18)
    if fee > max_fee_wei:
        raise ApiError(f"Transaction network fee is too high on {rpc.network}: {fee_native:,.8f}")

    signed = sign_tx(account, tx)
    sent_hash = str(await rpc.call("eth_sendRawTransaction", signed.raw))
    if sent_hash.lower() != signed.hash.lower():
        raise ApiError(f"RPC returned a different transaction hash on {rpc.network}")

    return signed.hash, fee_native


async def ensure_erc20_allowance(
    rpc: RPC,
    account: LocalAccount,
    token: EvmToken,
    spender: str,
    amount: int,
    receipt_timeout: float = 180,
) -> tuple[str, Decimal] | None:
    if amount <= 0:
        raise ValueError("Approval amount must be positive")
    if await get_token_allowance(rpc, token.address, account.address, spender) >= amount:
        return None

    call = make_call(token.address, "approve", ["address", "uint256"], [spender, amount])
    tx_hash, fee = await send_contract(rpc, account, call)
    await wait_receipt(rpc, tx_hash, receipt_timeout)
    return tx_hash, fee


async def execute_contract_with_erc20_allowance(
    network: EvmNetwork,
    account: LocalAccount,
    token: EvmToken,
    spender: str,
    amount: int,
    call: dict,
    proxy: str | None = None,
    receipt_timeout: float = 180,
) -> tuple[str, Decimal]:
    async with create_rpc(network, proxy) as rpc:
        await check_chain(rpc, network.chain_id)
        await check_token_balance(rpc, token, account.address, amount)
        approval = await ensure_erc20_allowance(
            rpc,
            account,
            token,
            spender,
            amount,
            receipt_timeout,
        )
        if approval:
            tx_hash, fee = approval
            logger.info(f"Approval tx: {tx_hash}; fee: {fee:,.8f} ETH")

        tx_hash, fee = await send_contract(rpc, account, call)
        await wait_receipt(rpc, tx_hash, receipt_timeout)
        return tx_hash, fee


async def transfer_erc20(
    rpc: RPC,
    account: LocalAccount,
    token: str,
    recipient: str,
    amount: int,
) -> tuple[str, Decimal]:
    call = make_call(token, "transfer", ["address", "uint256"], [to_addr(recipient), amount])
    return await send_contract(rpc, account, call)


async def wait_receipt(rpc: RPC, tx_hash: str, timeout: float = 180) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        receipt = await rpc.call("eth_getTransactionReceipt", tx_hash)
        if receipt is not None:
            if int(receipt["status"], 16) != 1:
                raise ApiError(f"{rpc.network} transaction failed: {tx_hash}")
            return receipt
        await asyncio.sleep(2)

    raise TimeoutError(f"{rpc.network} transaction receipt timed out: {tx_hash}")
