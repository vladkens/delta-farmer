from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from clients.lighter import LighterClient


@pytest.mark.parametrize(
    ("market", "price", "expected"),
    [
        ({"min_base_amount": "0.00020", "min_quote_amount": "10.000000"}, "80496.1", "16.099220"),
        ({"min_base_amount": "0.0100", "min_quote_amount": "10.000000"}, "762.38", "10.000000"),
    ],
)
async def test_min_trade_usd_includes_base_amount(market, price, expected):
    client = object.__new__(LighterClient)
    client.name = "test"
    client._market = AsyncMock(return_value=market)
    client.get_price = AsyncMock(return_value=Decimal(price))

    assert await client.get_min_trade_usd("BTC") == Decimal(expected)
