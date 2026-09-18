from decimal import Decimal
from unittest.mock import AsyncMock, Mock

from apps import n1 as n1_app
from clients.n1 import N1Client


def client() -> N1Client:
    value = object.__new__(N1Client)
    value.name = "test"
    value.http = Mock(request=AsyncMock())
    return value


async def test_paging_follows_cursor():
    value = client()
    value.http.request.side_effect = [
        Mock(
            ok=True,
            json=Mock(
                return_value={
                    "items": [{"actionId": 2, "time": "2026-01-02T00:00:00Z"}],
                    "nextStartInclusive": 123,
                }
            ),
        ),
        Mock(
            ok=True,
            json=Mock(
                return_value={
                    "items": [{"actionId": 1, "time": "2026-01-01T00:00:00Z"}],
                    "nextStartInclusive": None,
                }
            ),
        ),
    ]

    result = await value.paged("/trades?makerId=8989")

    assert [item["actionId"] for item in result] == [1, 2]
    assert value.http.request.await_args_list[1].kwargs["params"]["startInclusive"] == 123


async def test_stats_keep_net_pnl(monkeypatch):
    account = Mock(name="test")
    account.name = "test"
    monkeypatch.setattr(
        n1_app,
        "_fetch_stats",
        AsyncMock(
            return_value={
                "pnl": [{"day": "2026-06-11", "pnl": -5}],
                "trades": [
                    {
                        "time": "2026-06-11T12:00:00Z",
                        "price": 100,
                        "baseSize": 2,
                        "fee": Decimal(1),
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(n1_app, "sync_points", AsyncMock(return_value=[]))
    render = Mock()
    monkeypatch.setattr(n1_app, "render_stats", render)

    await n1_app.print_stats([account], period="day")

    row = render.call_args.args[0]["2026-06-11"][0]
    assert row.burn == Decimal(5)
    assert row.fees == Decimal(1)
