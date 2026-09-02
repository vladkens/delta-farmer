import asyncio

import pytest

from lib.errors import AppError
from lib.models import DurationSec, TimeRange
from lib.utils import gather_accs, parse_duration


def test_parse_duration_supports_days():
    assert parse_duration("3d") == 259200
    assert parse_duration("1d2h30m") == 95400


def test_duration_sec_accepts_day_strings():
    assert DurationSec("4d") == 345600


def test_time_range_accepts_day_strings():
    duration = TimeRange.model_validate({"min": "3d", "max": "4d"})

    assert duration.min == 259200
    assert duration.max == 345600


async def test_gather_accs_waits_for_all_accounts_before_reporting_failure():
    finished = asyncio.Event()

    class Account:
        def __init__(self, name: str):
            self.name = name

    async def work(acc: Account):
        if acc.name == "failed":
            raise RuntimeError("login failed")

        await asyncio.sleep(0)
        finished.set()
        return acc.name

    with pytest.raises(AppError, match="failed: login failed.*Try again later"):
        await gather_accs([Account("failed"), Account("useful")], work)

    assert finished.is_set()
