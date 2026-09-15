# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | It's not a bug, it's undocumented behavior

from collections.abc import Iterable

GOLDILOCKS_ORDER = 0xFFFFFFFF00000001

type Fp5 = tuple[int, int, int, int, int]

FP5_ZERO: Fp5 = (0, 0, 0, 0, 0)
FP5_ONE: Fp5 = (1, 0, 0, 0, 0)


def make_fp5(values: Iterable[int]) -> Fp5:
    values = tuple(value % GOLDILOCKS_ORDER for value in values)
    if len(values) != 5:
        raise ValueError(f"Fp5 requires 5 limbs, got {len(values)}")

    a, b, c, d, e = values
    return a, b, c, d, e


def fp5_from_bytes(data: bytes) -> Fp5:
    if len(data) != 40:
        raise ValueError(f"Fp5 encoding must be 40 bytes, got {len(data)}")

    limbs = tuple(int.from_bytes(data[i : i + 8], "little") for i in range(0, 40, 8))
    if any(limb >= GOLDILOCKS_ORDER for limb in limbs):
        raise ValueError("Fp5 encoding contains a non-canonical limb")

    return make_fp5(limbs)


def fp5_to_bytes(value: Fp5) -> bytes:
    return b"".join(limb.to_bytes(8, "little") for limb in value)


def fp5_add(left: Fp5, right: Fp5) -> Fp5:
    return make_fp5(a + b for a, b in zip(left, right))


def fp5_sub(left: Fp5, right: Fp5) -> Fp5:
    return make_fp5(a - b for a, b in zip(left, right))


def fp5_neg(value: Fp5) -> Fp5:
    return make_fp5(-limb for limb in value)


def fp5_mul(left: Fp5, right: Fp5) -> Fp5:
    # Fp5 is Fp[X] / (X^5 - 3).
    result = [0] * 5
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            degree = i + j
            factor = 1
            if degree >= 5:
                degree -= 5
                factor = 3

            result[degree] += factor * a * b

    return make_fp5(result)


def fp5_square(value: Fp5) -> Fp5:
    return fp5_mul(value, value)


def fp5_pow(value: Fp5, exponent: int) -> Fp5:
    if exponent < 0:
        raise ValueError("Fp5 exponent must be non-negative")

    result = FP5_ONE
    while exponent:
        if exponent & 1:
            result = fp5_mul(result, value)

        value = fp5_square(value)
        exponent >>= 1

    return result


def fp5_inv_or_zero(value: Fp5) -> Fp5:
    if value == FP5_ZERO:
        return FP5_ZERO

    return fp5_pow(value, GOLDILOCKS_ORDER**5 - 2)
