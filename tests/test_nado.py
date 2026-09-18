from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MethodType
from unittest.mock import AsyncMock, Mock

import pytest

from clients.nado import MarketHours, NadoClient, SymbolInfo

AT = datetime(2026, 5, 29, 12, tzinfo=UTC)


def symbol(status="live", hours=None) -> SymbolInfo:
    return SymbolInfo(
        product_id=1,
        symbol="TEST",
        size_increment=Decimal("0.1"),
        price_increment=Decimal("0.01"),
        min_size=Decimal(1),
        trading_status=status,
        market_hours=hours,
    )


def client(info: SymbolInfo) -> NadoClient:
    value = object.__new__(NadoClient)
    value.name = "test"

    async def get_info(self, *, symbol=None, product_id=None):
        return info

    value.symbol_info = MethodType(get_info, value)
    return value


@pytest.mark.parametrize(
    ("status", "reduce_only", "expected"),
    [
        ("not_tradable", False, False),
        ("not_tradable", True, False),
        ("reduce_only", False, False),
        ("reduce_only", True, True),
        ("soft_reduce_only", False, False),
        ("soft_reduce_only", True, True),
        ("post_only", False, True),
        ("post_only", True, False),
    ],
    ids=[
        "halt",
        "halt-close",
        "reduce",
        "reduce-close",
        "soft",
        "soft-close",
        "post",
        "post-close",
    ],
)
async def test_tradeable_status(status, reduce_only, expected):
    assert await client(symbol(status)).is_symbol_tradeable("TEST", AT, reduce_only) is expected


@pytest.mark.parametrize(
    ("hours", "reduce_only", "expected"),
    [
        (None, False, True),
        (MarketHours(is_open=True, next_close=AT + timedelta(seconds=1)), False, True),
        (MarketHours(is_open=True, next_close=AT), False, False),
        (MarketHours(is_open=False, next_open=AT + timedelta(seconds=1)), False, False),
        (
            MarketHours(
                is_open=False,
                next_open=AT - timedelta(seconds=1),
                next_close=AT + timedelta(seconds=1),
            ),
            False,
            True,
        ),
        (
            MarketHours(is_open=False, next_open=AT - timedelta(seconds=1), next_close=AT),
            False,
            False,
        ),
        (MarketHours(is_open=False, next_open=AT + timedelta(seconds=1)), True, True),
    ],
    ids=["always", "open", "closing", "closed", "reopened", "reclosed", "close-only"],
)
async def test_market_window(hours, reduce_only, expected):
    assert (
        await client(symbol(hours=hours)).is_symbol_tradeable("TEST", AT, reduce_only) is expected
    )


async def test_symbol_market_metadata():
    response = Mock(ok=True)
    response.json.return_value = {
        "XAG-PERP": {
            "product_id": 1,
            "size_increment": "100000000000000000",
            "price_increment_x18": "10000000000000000",
            "min_size": "1000000000000000000",
            "trading_status": "soft_reduce_only",
            "market_hours": {
                "is_open": True,
                "next_close": "2026-05-29T20:00:00Z",
                "next_open": None,
            },
        }
    }
    value = object.__new__(NadoClient)
    value.name = "test"
    value.http = Mock(request=AsyncMock(return_value=response))

    [info] = await value.symbols()

    assert info.symbol == "XAG"
    assert info.trading_status == "soft_reduce_only"
    assert info.market_hours == MarketHours(
        is_open=True,
        next_close=datetime(2026, 5, 29, 20, tzinfo=UTC),
        next_open=None,
    )
