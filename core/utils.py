# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Bugs are features in disguise
import json
import os
import pickle
import random
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TypeVar

from filelock import FileLock

from .logger import logger

T = TypeVar("T")


def first(items: list[T]) -> T | None:
    return items[0] if items else None


def pick(d: dict, *keys: str) -> dict:
    return {k: d[k] for k in keys if k in d}


def shuffle(items: list[T]) -> list[T]:
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


# MARK: Period functions


def to_period_week(ts: int, genesis: datetime) -> str:
    """Convert timestamp (ms) to week period string like W01, W02, etc."""
    dt = datetime.fromtimestamp(ts // 1000, tz=genesis.tzinfo)
    delta = dt - genesis
    index = delta.days // 7 + 1
    return f"W{index:02d}" if index > 0 else "OFF"


def to_period_day(ts: int) -> str:
    """Convert timestamp (ms) to day period string like 2025-02-19."""
    from datetime import UTC

    dt = datetime.fromtimestamp(ts // 1000, tz=UTC)
    return dt.strftime("%Y-%m-%d")


def parse_filter(filter_str: str, all_periods: list[str]) -> list[str]:
    """Parse filter string and return list of periods to show.

    Supported formats:
    - "all": all periods
    - "this": current (last) period
    - "last": previous period
    - "-1", "-2", "-3": index from end
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
        return [all_periods[idx]] if abs(idx) <= len(all_periods) else []
    elif filter_str in all_periods:
        return [filter_str]
    else:
        return []


# MARK: FS functions


def pickle_load(filepath: str, *, lock: bool = False, delete_on_error: bool = False):
    try:
        if lock:
            with FileLock(f"{filepath}.lock", timeout=5):
                with open(filepath, "rb") as fp:
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


def pickle_dump(filepath: str, data: object, *, lock: bool = False):
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        if lock:
            with FileLock(f"{filepath}.lock", timeout=5):
                with open(filepath, "wb") as fp:
                    pickle.dump(data, fp)
        else:
            with open(filepath, "wb") as fp:
                pickle.dump(data, fp)
    except Exception as e:
        logger.warning(f"Failed to save {filepath}: {e}")


def json_load(filepath: str):
    try:
        with open(filepath, "r") as fp:
            return json.load(fp)
    except FileNotFoundError:
        return None


def json_dump(filepath: str, data: object):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as fp:
        json.dump(data, fp, indent=2, default=str)


# MARK: Duration parsing


# inspired by https://pkg.go.dev/time#ParseDuration
DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
UNITS_VALUE = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
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
    for main_name, bal in bals:
        main_size = size_usd / 2
        if bal * leverage * safety < main_size:
            continue  # insufficient balance for given main

        rest = [x for x in bals if x[0] != main_name]
        rest_size = random_partition(main_size, len(rest), precision=0.01)
        for i, (_, bal) in enumerate(rest):
            if bal * leverage * safety < rest_size[i]:
                break  # insufficient balance for given rest
        else:
            names = [main_name] + [x[0] for x in rest]
            sizes = [main_size] + rest_size
            return [(na, round_to_tick_size(sz, tick_size)) for na, sz in zip(names, sizes)]

    # fallback: highest balance as main and rest with proportional sizes
    logger.warning("Low balance on some accounts, trying fallback method...")
    main_name, main_bal = max(bals, key=lambda x: x[1])
    main_size = main_bal * leverage * safety

    rest = [x for x in bals if x[0] != main_name]
    rest_size = random_partition(main_size, len(rest), precision=0.01)
    for i, (_, bal) in enumerate(rest):
        if bal * leverage * safety < rest_size[i]:
            logger.error(f"No valid accounts found trade {size_usd:.2f} x{leverage}")
            return None

    names = [main_name] + [x[0] for x in rest]
    sizes = [main_size] + rest_size
    return [(na, round_to_tick_size(sz, tick_size)) for na, sz in zip(names, sizes)]
