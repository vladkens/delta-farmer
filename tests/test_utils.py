import asyncio

import pytest

from lib.errors import AppError
from lib.utils import gather_accs


async def test_gather_waits_before_error():
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
