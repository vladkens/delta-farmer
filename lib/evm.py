# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License
import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from eth_abi import decode, encode
from eth_account.signers.local import LocalAccount
from eth_account.types import TransactionDictType
from eth_utils import keccak, to_checksum_address
from pydantic import AliasPath, BaseModel, Field

from .decorators import retry_on
from .http import ApiError, AsyncHttp
from .logger import logger

# Adapted from https://github.com/vladkens/web3-cybil-tools

MAX_TX_FEE_WEI = 10**16
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


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
    stables: Mapping[str, EvmToken]
    explorer_url: str | None = None
    native_token: EvmToken = EvmToken("ETH", ZERO_ADDRESS, 18)
    short_name: str = ""

    @property
    def code(self) -> str:
        return self.short_name.lower()

    def asset_code(self, token: EvmToken) -> str:
        return f"{self.code}:{token.symbol.lower()}"

    def tx_url(self, tx_hash: str) -> str:
        if self.explorer_url is None:
            return tx_hash
        return f"{self.explorer_url.rstrip('/')}/tx/{tx_hash}"


ROBINHOOD = EvmNetwork(
    name="robinhood",
    chain_id=4663,
    rpc_url="https://rpc.mainnet.chain.robinhood.com",
    explorer_url="https://robin.etherscan.io",
    short_name="RH",
    stables={
        "USDG": EvmToken("USDG", "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168", 6),
    },
)
ETHEREUM = EvmNetwork(
    name="ethereum",
    chain_id=1,
    rpc_url="https://eth.drpc.org",
    explorer_url="https://etherscan.io",
    short_name="ETH",
    stables={
        "USDC": EvmToken("USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
        "USDT": EvmToken("USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7", 6),
    },
)
OPTIMISM = EvmNetwork(
    name="optimism",
    chain_id=10,
    rpc_url="https://mainnet.optimism.io",
    explorer_url="https://optimistic.etherscan.io",
    short_name="OP",
    stables={
        "USDC": EvmToken("USDC", "0x0b2c639c533813f4aa9d7837caf62653d097ff85", 6),
        "USDT": EvmToken("USDT", "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58", 6),
    },
)
BSC = EvmNetwork(
    name="bsc",
    chain_id=56,
    rpc_url="https://bsc-dataseed.bnbchain.org",
    explorer_url="https://bscscan.com",
    native_token=EvmToken("BNB", ZERO_ADDRESS, 18),
    short_name="BSC",
    stables={
        "USDC": EvmToken("USDC", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", 18),
        "USDT": EvmToken("USDT", "0x55d398326f99059ff775485246999027b3197955", 18),
    },
)
BASE = EvmNetwork(
    name="base",
    chain_id=8453,
    rpc_url="https://mainnet.base.org",
    explorer_url="https://basescan.org",
    short_name="BASE",
    stables={
        "USDC": EvmToken("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
        "USDT": EvmToken("USDT", "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2", 6),
    },
)
ARBITRUM = EvmNetwork(
    name="arbitrum",
    chain_id=42161,
    rpc_url="https://arb1.arbitrum.io/rpc",
    explorer_url="https://arbiscan.io",
    short_name="ARB",
    stables={
        "USDC": EvmToken("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
        "USDT": EvmToken("USDT", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", 6),
    },
)
HYPEREVM = EvmNetwork(
    name="hyperevm",
    chain_id=999,
    rpc_url="https://rpc.hyperliquid.xyz/evm",
    explorer_url="https://hyperevmscan.io",
    native_token=EvmToken("HYPE", ZERO_ADDRESS, 18),
    short_name="HEVM",
    stables={
        "USDC": EvmToken("USDC", "0xb88339CB7199b77E23DB6E890353E22632Ba630f", 6),
        "USDT": EvmToken("USDT", "0xb8ce59fc3717ada4c02eadf9682a9e934f625ebb", 6),
    },
)

EVM_NETWORKS = {
    network.code: network
    for network in (
        ETHEREUM,
        OPTIMISM,
        BSC,
        BASE,
        ARBITRUM,
        ROBINHOOD,
        HYPEREVM,
    )
}


def resolve_evm_asset(value: str) -> tuple[EvmNetwork, EvmToken]:
    parts = value.lower().split(":", 1)
    if len(parts) != 2:
        raise ValueError("EVM asset must look like arb:usdc or hevm:hype")

    name, symbol = parts
    network = EVM_NETWORKS.get(name)
    if network is None:
        raise ValueError(f"Unsupported EVM network: {name}")

    symbol = symbol.upper()
    token = (
        network.native_token
        if symbol == network.native_token.symbol
        else network.stables.get(symbol)
    )
    if token is None:
        raise ValueError(f"Unsupported token on {network.name}: {symbol}")

    return network, token


class RPCError(ApiError):
    def __init__(self, method: str, code: int | None, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{method}: RPC error {code}: {message}")


class RPC(AsyncHttp):
    def __init__(self, url: str, network: str, proxy: str | None = None):
        self.url = url
        self.network = network
        super().__init__(
            baseurl=url,
            headers={"Content-Type": "application/json"},
            proxy=proxy,
        )

    async def call(self, method: str, *params) -> Any:
        response = await self.request(
            "POST",
            self.url,
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


@retry_on(RPCError, retries=2, delay=1)
async def get_evm_balances(
    network: EvmNetwork,
    owner: str,
    tokens: Sequence[EvmToken],
    proxy: str | None = None,
) -> dict[EvmToken, Decimal]:
    async with create_rpc(network, proxy) as rpc:
        await check_chain(rpc, network.chain_id)
        values = await asyncio.gather(
            *(
                get_native_bal(rpc, owner)
                if token.address == ZERO_ADDRESS
                else get_token_bal(rpc, token.address, owner)
                for token in tokens
            )
        )

    balances = {}
    for token, value in zip(tokens, values, strict=True):
        balances[token] = Decimal(value).scaleb(-token.decimals)

    return balances


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


async def estimate_call_fee(rpc: RPC, sender: str, call: dict) -> int:
    tx = {**call, "from": sender, **await dynamic_fee_params(rpc)}
    gas = int(await rpc.call("eth_estimateGas", rpc_tx(tx)), 16)
    return (gas * 120 + 99) // 100 * fee_cap(tx)


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

    tx.update(await dynamic_fee_params(rpc))
    estimate = int(await rpc.call("eth_estimateGas", rpc_tx(tx)), 16)

    tx["gas"] = (estimate * 120 + 99) // 100
    maximum = int(tx["gas"]) * fee_cap(tx) + int(tx.get("value", 0))
    balance = await get_native_bal(rpc, account.address)
    if balance < maximum:
        raise ValueError("Not enough native currency for the transaction and gas")

    return tx


async def _validate_prepared_tx(rpc: RPC, account: LocalAccount, tx: dict) -> None:
    chain_id = int(await rpc.call("eth_chainId"), 16)
    if chain_id != tx["chainId"]:
        raise ValueError("RPC chain changed before signing")

    nonce = int(await rpc.call("eth_getTransactionCount", account.address, "pending"), 16)
    if nonce != tx["nonce"]:
        raise ValueError("Wallet nonce changed before signing; refresh the transaction")

    block = await rpc.call("eth_getBlockByNumber", "latest", False)
    base_fee = block.get("baseFeePerGas")
    if base_fee is not None and int(base_fee, 16) > fee_cap(tx):
        raise ValueError("Base fee exceeds the approved fee cap; rerun to refresh the quote")


def sign_tx(account: LocalAccount, tx: dict) -> SignedTransaction:
    if to_addr(str(tx["from"])) != account.address:
        raise ValueError("Transaction sender does not match the selected account")

    payload = {key: value for key, value in tx.items() if key != "from"}
    if payload.get("to"):
        payload["to"] = to_addr(str(payload["to"]))

    signed = account.sign_transaction(cast(TransactionDictType, payload))
    return SignedTransaction(
        hash="0x" + bytes(signed.hash).hex(),
        raw="0x" + bytes(signed.raw_transaction).hex(),
    )


async def send_contract(
    rpc: RPC, account: LocalAccount, call: dict, max_fee_wei: int = MAX_TX_FEE_WEI
) -> tuple[str, Decimal]:
    tx = await prepare_tx(rpc, account, call)
    fee = int(tx["gas"]) * fee_cap(tx)
    fee_native = Decimal(fee).scaleb(-18)
    if fee > max_fee_wei:
        raise ApiError(f"Transaction network fee is too high on {rpc.network}: {fee_native:,.8f}")

    await _validate_prepared_tx(rpc, account, tx)
    signed = sign_tx(account, tx)
    try:
        sent_hash = str(await rpc.call("eth_sendRawTransaction", signed.raw))
    except Exception:
        logger.error(f"Submission status is uncertain; check {signed.hash} before retrying")
        raise

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
            rpc, account, token, spender, amount, receipt_timeout
        )
        if approval:
            tx_hash, fee = approval
            logger.info(f"Approval tx: {tx_hash}; fee: {fee:,.8f} ETH")

        tx_hash, fee = await send_contract(rpc, account, call)
        await wait_receipt(rpc, tx_hash, receipt_timeout)
        return tx_hash, fee


async def transfer_erc20(
    rpc: RPC, account: LocalAccount, token: str, recipient: str, amount: int
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


# Relay API: https://docs.relay.link/references/api/get-quote
RELAY_API_URL = "https://api.relay.link"
RELAY_RECEIPT_TIMEOUT_SEC = 3 * 60
RELAY_STATUS_TIMEOUT_SEC = 15 * 60
RELAY_STATUS_POLL_DELAY_SEC = 3
RELAY_NONCE_SYNC_TIMEOUT_SEC = 30
RELAY_GAS_BUFFER_PCT = 30
RELAY_SLIPPAGE_BPS = 50
RELAY_SDK_VERSION = "8.0.1"
RELAY_MAX_TIME_SEC = 10 * 60
RELAY_MAX_LOSS_PCT = Decimal(2)
RELAY_MAX_LOSS_USD = Decimal("0.25")
EVM_TRANSFER_RECEIPT_TIMEOUT_SEC = 3 * 60


def _quantity(value: str | int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return int(value, 0 if value.startswith("0x") else 10)


class RelayTransaction(BaseModel):
    to: str
    data: str
    value: str | int | None = None
    gas: str | int | None = None
    max_fee: str | int | None = Field(None, alias="maxFeePerGas")


class RelayCheck(BaseModel):
    endpoint: str


class RelayItem(BaseModel):
    data: RelayTransaction
    check: RelayCheck | None = None


class RelayStep(BaseModel):
    id: str
    items: list[RelayItem]


_RELAY_IN = ("details", "currencyIn")
_RELAY_OUT = ("details", "currencyOut")
_RELAY_GAS = ("fees", "gas", "minimumAmount")


class RelayQuote(BaseModel):
    input_amount: int = Field(validation_alias=AliasPath(*_RELAY_IN, "amount"))
    output_amount: int = Field(validation_alias=AliasPath(*_RELAY_OUT, "amount"))
    minimum_output_amount: int = Field(validation_alias=AliasPath(*_RELAY_OUT, "minimumAmount"))
    input_usd: Decimal | None = Field(None, validation_alias=AliasPath(*_RELAY_IN, "amountUsd"))
    output_usd: Decimal | None = Field(None, validation_alias=AliasPath(*_RELAY_OUT, "amountUsd"))
    gas_topup_usd: Decimal = Field(
        Decimal(0), validation_alias=AliasPath("details", "currencyGasTopup", "amountUsd")
    )
    quoted_gas: str | int | None = Field(None, validation_alias=AliasPath(*_RELAY_GAS))
    time_estimate: int = Field(0, validation_alias=AliasPath("details", "timeEstimate"), ge=0)
    steps: list[RelayStep]

    @property
    def required_gas(self) -> int:
        quoted = _quantity(self.quoted_gas)
        transactions = (item.data for step in self.steps for item in step.items)
        calculated = sum(_quantity(tx.gas) * _quantity(tx.max_fee) for tx in transactions)
        estimate = max(quoted, calculated)
        if estimate <= 0:
            raise ApiError("Relay quote has no gas estimate")
        return (estimate * (100 + RELAY_GAS_BUFFER_PCT) + 99) // 100


async def _get_relay_quote(
    http: AsyncHttp,
    owner: str,
    source: str,
    target: str,
    amount: int,
    topup_gas: bool = False,
) -> RelayQuote:
    origin_network, origin_token = resolve_evm_asset(source)
    destination_network, destination_token = resolve_evm_asset(target)
    pld = {
        "user": owner,
        "recipient": owner,
        "originChainId": origin_network.chain_id,
        "destinationChainId": destination_network.chain_id,
        "originCurrency": origin_token.address,
        "destinationCurrency": destination_token.address,
        "amount": str(amount),
        "tradeType": "EXACT_INPUT",
        "slippageTolerance": str(RELAY_SLIPPAGE_BPS),
        "useExternalLiquidity": False,
        "useFallbacks": False,
        "usePermit": False,
    }
    if topup_gas:
        pld["topupGas"] = True

    res = await http.request("POST", "/quote/v2", json=pld)
    if not res.ok:
        raise ApiError("Relay quote failed", res)
    return RelayQuote.model_validate(res.json())


async def _wait_relay_status(http: AsyncHttp, endpoint: str) -> None:
    deadline = time.monotonic() + RELAY_STATUS_TIMEOUT_SEC
    while time.monotonic() < deadline:
        res = await http.request("GET", endpoint)
        if not res.ok and res.status_code < 500:
            raise ApiError("Relay status failed", res)

        if res.ok:
            status = res.json().get("status")
            if status == "success":
                return
            if status in ("failure", "fallback", "refund"):
                raise ApiError(f"Relay transaction ended with status: {status}")

        await asyncio.sleep(RELAY_STATUS_POLL_DELAY_SEC)

    raise TimeoutError("Relay transaction confirmation timed out")


async def _wait_nonce_sync(rpc: RPC, owner: str) -> None:
    deadline = time.monotonic() + RELAY_NONCE_SYNC_TIMEOUT_SEC
    while time.monotonic() < deadline:
        pending = await rpc.call("eth_getTransactionCount", owner, "pending")
        latest = await rpc.call("eth_getTransactionCount", owner, "latest")
        if pending == latest:
            return

        await asyncio.sleep(1)

    raise TimeoutError(f"{rpc.network} wallet nonce did not synchronize")


async def _execute_relay_step(
    http: AsyncHttp,
    rpc: RPC,
    network: EvmNetwork,
    account: LocalAccount,
    step: RelayStep,
) -> None:
    for item in step.items:
        await _execute_relay_item(http, rpc, network, account, step.id, item)


async def _execute_relay_item(
    http: AsyncHttp,
    rpc: RPC,
    network: EvmNetwork,
    account: LocalAccount,
    step_id: str,
    item: RelayItem,
) -> None:
    tx = item.data
    call = {"to": tx.to, "data": tx.data, "value": _quantity(tx.value)}
    tx_hash, fee = await send_contract(rpc, account, call)
    url = network.tx_url(tx_hash)
    symbol = network.native_token.symbol
    logger.info(f"{step_id.title()} submitted: fee {fee:,.8f} {symbol}; {url}")
    await wait_receipt(rpc, tx_hash, RELAY_RECEIPT_TIMEOUT_SEC)
    await _wait_nonce_sync(rpc, account.address)
    if item.check:
        await _wait_relay_status(http, item.check.endpoint)


async def relay_move(
    account: LocalAccount,
    source: str,
    target: str,
    quantity: str,
    proxy: str | None = None,
    minimum_usd: Decimal = Decimal(0),
    topup_gas: bool = False,
) -> RelayQuote:
    origin_network, origin_token = resolve_evm_asset(source)
    destination_network, destination_token = resolve_evm_asset(target)
    if (destination_network, destination_token) == (origin_network, origin_token):
        raise ValueError("EVM source and destination are the same")

    async with (
        AsyncHttp(
            baseurl=RELAY_API_URL,
            headers={"Content-Type": "application/json", "relay-sdk-version": RELAY_SDK_VERSION},
            proxy=proxy,
        ) as http,
        create_rpc(origin_network, proxy) as rpc,
    ):
        await check_chain(rpc, origin_network.chain_id)
        native_balance = await get_native_bal(rpc, account.address)
        if origin_token.address.lower() == ZERO_ADDRESS:
            balance = native_balance
        else:
            balance = await get_token_bal(rpc, origin_token.address, account.address)

        amount = balance if quantity == "max" else to_token_units(Decimal(quantity), origin_token)
        if amount <= 0 or amount > balance:
            raise ValueError(f"Insufficient {source} balance")

        quote = await _get_relay_quote(http, account.address, source, target, amount, topup_gas)
        required_gas = quote.required_gas
        if quantity == "max" and origin_token.address.lower() == ZERO_ADDRESS:
            amount = native_balance - required_gas
            if amount <= 0:
                raise ValueError(f"Insufficient {source} balance for gas")

            quote = await _get_relay_quote(http, account.address, source, target, amount, topup_gas)
            required_gas = quote.required_gas

        spent = quote.input_amount if origin_token.address.lower() == ZERO_ADDRESS else 0
        if spent + required_gas > native_balance:
            raise ValueError(f"Insufficient {origin_network.code} gas")

        if topup_gas and quote.gas_topup_usd <= 0:
            raise ApiError("Relay route does not provide the requested gas top-up")

        validate_relay_quote(quote, minimum_usd)
        if origin_network != destination_network and not any(
            item.check for step in quote.steps for item in step.items
        ):
            raise ApiError("Relay quote has no destination confirmation")

        for step in quote.steps:
            await _execute_relay_step(http, rpc, origin_network, account, step)

    try:
        balances = await get_evm_balances(
            destination_network,
            account.address,
            (destination_token,),
            proxy,
        )
    except Exception as error:
        logger.success(f"Move confirmed: {target}")
        logger.warning(f"Destination balance unavailable: {error}")
    else:
        balance = balances[destination_token]
        decimals = 6 if destination_token.address == ZERO_ADDRESS else 2
        value = f"{balance:,.{decimals}f}".rstrip("0").rstrip(".")
        logger.success(f"Move confirmed: {target} balance {value}")

    return quote


def validate_relay_quote(quote: RelayQuote, minimum_usd: Decimal = Decimal(0)) -> None:
    if quote.output_amount <= 0 or not 0 < quote.minimum_output_amount <= quote.output_amount:
        raise ApiError("Relay quote has no valid output amount")
    if quote.input_usd is None or quote.output_usd is None:
        raise ApiError("Relay quote has no USD value")
    if quote.input_usd < minimum_usd:
        raise ApiError(f"Relay route is uneconomical: ${quote.input_usd:,.2f}")
    if quote.time_estimate > RELAY_MAX_TIME_SEC:
        raise ApiError(f"Relay route is too slow: {quote.time_estimate}s")

    minimum_output_usd = (
        quote.output_usd * Decimal(quote.minimum_output_amount) / Decimal(quote.output_amount)
    )
    loss = quote.input_usd - minimum_output_usd - quote.gas_topup_usd
    allowed = max(RELAY_MAX_LOSS_USD, quote.input_usd * RELAY_MAX_LOSS_PCT / 100)
    if loss > allowed:
        raise ApiError(f"Relay route loss is too high: ${loss:,.2f}")


async def transfer_evm_asset(
    account: LocalAccount,
    source: str,
    recipient: str,
    quantity: str,
    proxy: str | None = None,
) -> None:
    network, token = resolve_evm_asset(source)
    recipient = to_addr(recipient)
    async with create_rpc(network, proxy) as rpc:
        await check_chain(rpc, network.chain_id)
        if token.address.lower() == ZERO_ADDRESS:
            balance = await get_native_bal(rpc, account.address)
            if quantity == "max":
                call = {"to": recipient, "data": "0x", "value": 0}
                amount = balance - await estimate_call_fee(rpc, account.address, call)
            else:
                amount = to_token_units(Decimal(quantity), token)

            if amount <= 0 or amount > balance:
                raise ValueError(f"Insufficient {source} balance")

            call = {"to": recipient, "data": "0x", "value": amount}
            tx_hash, fee = await send_contract(rpc, account, call)
        else:
            balance = await get_token_bal(rpc, token.address, account.address)
            amount = balance if quantity == "max" else to_token_units(Decimal(quantity), token)
            if amount <= 0 or amount > balance:
                raise ValueError(f"Insufficient {source} balance")

            tx_hash, fee = await transfer_erc20(rpc, account, token.address, recipient, amount)

        url = network.tx_url(tx_hash)
        symbol = network.native_token.symbol
        logger.info(f"Transfer submitted: fee {fee:,.8f} {symbol}; {url}")
        await wait_receipt(rpc, tx_hash, EVM_TRANSFER_RECEIPT_TIMEOUT_SEC)

    logger.success(f"Transfer confirmed: {url}")
