# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Bugs are features in disguise
import random
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TypeVar

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


def wait_msg(sec: float) -> str:
    until_dt = datetime.now() + timedelta(seconds=sec)
    until_dt = until_dt.isoformat().split(".")[0].split("T")[1]
    return f"Sleeping for {sec:.1f}s, next run at {until_dt}"


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
