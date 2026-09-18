import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from eth_abi import decode, encode
from eth_account.signers.local import LocalAccount
from eth_account.types import TransactionDictType
from eth_utils import keccak, to_checksum_address

from .http import ApiError, AsyncHttp


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


@dataclass(frozen=True)
class SignedTransaction:
    hash: str
    raw: str


def to_addr(address: str | int) -> str:
    return to_checksum_address(f"0x{address:040x}" if isinstance(address, int) else address)


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
    if int(await rpc.call("eth_getBalance", account.address, "latest"), 16) < maximum:
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


def raw_tx_hash(raw_tx: str) -> str:
    return "0x" + keccak(bytes.fromhex(raw_tx.removeprefix("0x"))).hex()


async def wait_receipt(rpc: RPC, tx_hash: str, timeout: float = 180) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        receipt = await rpc.call("eth_getTransactionReceipt", tx_hash)
        if receipt is not None:
            return receipt
        await asyncio.sleep(2)

    raise TimeoutError(f"Receipt not available: {tx_hash}")
