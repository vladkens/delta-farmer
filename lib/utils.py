# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Optimized for confusion
import asyncio
import hashlib
import json
import os
import pickle
import random
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast, overload

from filelock import FileLock

from .errors import AppError
from .logger import logger

GATHER_ACCS_LIMIT = 1


async def gather_accs[T, R](
    accs: list[T],
    fn: Callable[[T], Awaitable[R]],
    *,
    limit=GATHER_ACCS_LIMIT,
) -> list[R]:
    if limit < 1:
        raise ValueError("Account concurrency limit must be at least 1")

    semaphore = asyncio.Semaphore(limit)

    async def run(acc: T) -> R:
        async with semaphore:
            return await fn(acc)

    results = await asyncio.gather(*(run(acc) for acc in accs), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result

    failures = [
        (getattr(acc, "name", str(acc)), result)
        for acc, result in zip(accs, results)
        if isinstance(result, Exception)
    ]
    if failures:
        details = "; ".join(f"{name}: {error}" for name, error in failures)
        raise AppError(f"Account tasks failed: {details}. Try again later.")

    return cast(list[R], results)


def first[T](items: list[T]) -> T | None:
    return items[0] if items else None


def pick(d: dict, *keys: str) -> dict:
    return {k: d[k] for k in keys if k in d}


@overload
def get_or(obj: dict, key: str) -> Any | None: ...


@overload
def get_or[T](obj: dict, key: str, default_value: T) -> Any | T: ...


def get_or(obj: dict, key: str, default_value: Any = None) -> Any:
    for part in key.split("."):
        if part not in obj:
            return default_value
        obj = obj[part]

    return obj


def shuffle[T](items: list[T]) -> list[T]:
    items = items.copy()
    random.shuffle(items)
    return items


def short_addr(addr: str, left: int = 6, right: int = 4) -> str:
    return f"{addr[:left]}..{addr[-right:]}"


def format_duration(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    elif sec < 3600:
        m, s = divmod(sec, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    else:
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m}m" if m else f"{h}h"


def wait_msg(sec: float) -> str:
    until_dt = datetime.now() + timedelta(seconds=sec)
    until_dt = until_dt.isoformat().split(".")[0].split("T")[1]
    return f"Sleeping for {format_duration(sec)}, next run at {until_dt}"


def sha256(data: bytes | str) -> str:
    data = data.encode() if isinstance(data, str) else data
    return hashlib.sha256(data).hexdigest()


# MARK: Period functions


def _week_date_range(week_start: datetime) -> str:
    week_end = week_start + timedelta(days=6)
    return f"{week_start.strftime('%b%d')}-{week_end.strftime('%b%d')}"


def to_period_week(dt: datetime, genesis: datetime, prefix: str = "W") -> str:
    """Convert datetime to week period string like 'W01 Dec18-Dec24'."""
    assert dt.tzinfo == UTC, "to_period_week: dt must be in UTC timezone"
    delta = dt - genesis
    index = delta.days // 7 + 1
    if index <= 0:
        return f"OFF {_week_date_range(genesis + timedelta(weeks=index - 1))}"
    week_start = genesis + timedelta(weeks=index - 1)
    return f"{prefix}{index:02d} {_week_date_range(week_start)}"


def to_period_day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def parse_filter(filter_str: str, all_periods: list[str]) -> list[str]:
    """Parse filter string and return list of periods to show.

    Supported formats:
    - "all": all periods
    - "this": current (last) period
    - "last": previous period
    - "0", "-1", "-2": 0 = current week, -1 = previous, -2 = two ago
    - "W05", "2025-02-19": specific period
    """
    if not all_periods:
        return []

    if filter_str == "all":
        return all_periods
    elif filter_str == "this":
        return [all_periods[-1]]
    elif filter_str == "last" or filter_str == "prev":
        return [all_periods[-2]] if len(all_periods) >= 2 else []
    elif filter_str.lstrip("-").isdigit():
        idx = int(filter_str)
        if idx > 0:
            return []
        # 0 = current (last), -1 = previous, -2 = two ago, etc.
        real_idx = idx - 1
        return [all_periods[real_idx]] if abs(real_idx) <= len(all_periods) else []
    elif matches := [p for p in all_periods if p.startswith(filter_str)]:
        return matches
    else:
        return []


# MARK: FS functions


def pickle_load(filepath: str, *, lock=False, delete_on_error=False):
    try:
        if lock:
            with FileLock(f"{filepath}.lock", timeout=5), open(filepath, "rb") as fp:
                return pickle.load(fp)
        else:
            with open(filepath, "rb") as fp:
                return pickle.load(fp)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug(f"Failed to load {filepath}: {e}")
        if delete_on_error and os.path.exists(filepath):
            os.remove(filepath)
        return None


def pickle_dump(filepath: str, data: object, *, lock=False):
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        if lock:
            with FileLock(f"{filepath}.lock", timeout=5), open(filepath, "wb") as fp:
                pickle.dump(data, fp)
        else:
            with open(filepath, "wb") as fp:
                pickle.dump(data, fp)
    except Exception as e:
        logger.warning(f"Failed to save {filepath}: {e}")


def json_load(filepath: str):
    try:
        with open(filepath) as fp:
            return json.load(fp)
    except FileNotFoundError:
        return None


def json_dump(filepath: str, data: object):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as fp:
        json.dump(data, fp, indent=2, default=str)


# MARK: Duration parsing


# inspired by https://pkg.go.dev/time#ParseDuration
DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
UNITS_VALUE = {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}
UNITS_ORDER = list(UNITS_VALUE.keys())


def parse_duration(s: str) -> float:
    matches = DURATION_RE.findall(s)

    reconstructed = {u: n for n, u in matches}.items()  # drop duplicates
    reconstructed = [(u, n) for n, u in reconstructed]
    reconstructed = sorted(reconstructed, key=lambda x: UNITS_ORDER.index(x[1]))
    reconstructed = "".join("".join(m) for m in reconstructed)

    if not s or reconstructed != s:
        raise ValueError(f"Invalid duration string: '{s}'")

    total_seconds = 0.0
    for value_str, unit in matches:
        value = float(value_str)
        total_seconds += value * UNITS_VALUE[unit]

    return total_seconds


# MARK: Business logic utils


def random_partition(M, N, randomness=0.1, precision=0.1):
    # Splits M into N parts with ±(randomness*100)% jitter per part.
    # Default randomness=0.1 → ±10% on each part.
    # Noise is forced sum-neutral: total always equals M exactly.
    # Used in find_safe_pair to add slight variation to hedge account sizes
    # so positions are not perfectly symmetric (anti-pattern detection).
    assert 0.0 <= randomness <= 1.0, "randomness must be in [0.0, 1.0]"
    assert precision > 0.0, "precision must be positive"
    assert M >= 0.0, "M must be non-negative"
    assert N > 0, "N must be positive"

    scale = round(1 / precision)
    M_units = round(M * scale)

    if M_units % N != 0 and randomness == 0:
        raise ValueError("Exact equal split impossible with this precision")

    avg_units = M_units // N

    # Generate noise in integer units
    max_noise = int(randomness * avg_units)
    noise = [random.randint(-max_noise, max_noise) for _ in range(N)]

    # Force noise sum to zero
    noise_mean = sum(noise) // N
    noise = [x - noise_mean for x in noise]

    # Build values
    values_units = [avg_units + x for x in noise]

    # Fix rounding drift
    correction = M_units - sum(values_units)
    values_units[0] += correction

    # Convert back to floats
    return [x / scale for x in values_units]


def round_to_tick_size(value: Decimal | float | int, tick_size: Decimal) -> Decimal:
    assert tick_size > 0, "tick_size must be positive"
    value = Decimal(value)
    return (value / tick_size).quantize(Decimal(1)) * tick_size


def find_safe_pair(bals: list[tuple[str, float]], size_usd: float, leverage: int, safety=0.9):
    tick_size = Decimal("0.01")  # default tick for USD pairs

    # search accounts combinations with enought balance to satisfy sz_usd
    for prime_name, bal in bals:
        prime_size = size_usd / 2
        if bal * leverage * safety < prime_size:
            continue  # insufficient balance for given prime

        rest = [x for x in bals if x[0] != prime_name]
        rest_size = random_partition(prime_size, len(rest), precision=0.01)
        for i, (_, bal) in enumerate(rest):
            if bal * leverage * safety < rest_size[i]:
                break  # insufficient balance for given rest
        else:
            names = [prime_name] + [x[0] for x in rest]
            sizes = [prime_size] + rest_size
            return [(na, round_to_tick_size(sz, tick_size)) for na, sz in zip(names, sizes)]

    # fallback: highest balance as prime and rest with proportional sizes
    logger.warning("Low balance on some accounts, trying fallback method...")
    prime_name, prime_bal = max(bals, key=lambda x: x[1])
    prime_size = prime_bal * leverage * safety

    rest = [x for x in bals if x[0] != prime_name]
    rest_size = random_partition(prime_size, len(rest), precision=0.01)
    for i, (_, bal) in enumerate(rest):
        if bal * leverage * safety < rest_size[i]:
            logger.error(f"No valid accounts found trade {size_usd:.2f} x{leverage}")
            return None

    names = [prime_name] + [x[0] for x in rest]
    sizes = [prime_size] + rest_size
    return [(na, round_to_tick_size(sz, tick_size)) for na, sz in zip(names, sizes)]


# MARK: Ethereal-specific utils


def parse_signature_type(value: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        ftype, fname = part.rsplit(" ", 1)
        out.append({"name": fname, "type": ftype})
    return out


# MARK: Key parsing


def parse_eth_key(raw: str, name: str = ""):
    # Lazy import to avoid pulling eth_account into every module that imports utils.
    from eth_account import Account

    who = f" for account '{name}'" if name else ""
    try:
        return Account.from_key(raw)
    except Exception as e:
        raise AppError(f"Invalid Ethereum private key{who}: {e}") from None


def parse_sol_key(raw: str | list[int], name: str = ""):
    # Lazy imports to avoid pulling Solana deps into every module that imports utils.
    import base58
    from solders.keypair import Keypair

    who = f" for account '{name}'" if name else ""
    try:
        if isinstance(raw, list):
            return Keypair.from_bytes(bytes(raw))
        if raw.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(raw)))
        return Keypair.from_bytes(base58.b58decode(raw))
    except Exception as e:
        raise AppError(
            f"Invalid Solana private key{who}: expected base58 string or JSON byte array — {e}"
        ) from None


# MARK: Async utils


async def gather_cancel(tasks: list[asyncio.Task], timeout: float) -> None:
    """Gather tasks, canceling any that exceed the timeout."""
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
    except TimeoutError:
        logger.warning(f"Timeout ({timeout:.0f}s), canceling stuck tasks")
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def interruptible_sleep(sec: float, stop_event: asyncio.Event | None = None) -> None:
    """Sleep for sec seconds, raising CancelledError early if stop_event fires."""
    if stop_event is None:
        await asyncio.sleep(sec)
        return

    if stop_event.is_set():
        raise asyncio.CancelledError

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=sec)
        raise asyncio.CancelledError
    except TimeoutError:
        pass  # completed normally
