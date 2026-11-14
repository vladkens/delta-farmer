import os
import pickle
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from curl_cffi import CurlError

from apps import lighter as lighter_app
from apps.lighter import apply_referral_code, print_info
from clients.lighter import LighterCoreClient, LighterFunding, LighterRobinhoodClient, LighterTrade
from lib.errors import AppError
from lib.http import ApiError
from lib.models import DurationSec, TimeRange
from lib.table import AutoTable
from scripts import weekly
from strategy import ProfileInfo

# 2026-11-10 is Robinhood campaign week 11 and ISO week 46.
TRADED_AT = datetime(2026, 11, 10, tzinfo=UTC)
# Manual Core trading before the campaign start (2026-09-01), which no report counts.
BEFORE_CAMPAIGN = datetime(2025, 11, 12, tzinfo=UTC)


def client(client_type, name: str, *, registered: bool = True, logged_in: bool = True):
    acc = object.__new__(client_type)
    acc.name = name
    acc.registered = AsyncMock(return_value=registered)
    acc.auth_ready = AsyncMock(return_value=logged_in)
    return acc


@pytest.mark.parametrize(
    ("market", "price", "expected"),
    [
        ({"min_base_amount": "0.00020", "min_quote_amount": "10.000000"}, "80496.1", "16.099220"),
        ({"min_base_amount": "0.0100", "min_quote_amount": "10.000000"}, "762.38", "10.000000"),
    ],
)
async def test_min_trade_usd_includes_base_amount(market, price, expected):
    client = object.__new__(LighterRobinhoodClient)
    client.name = "test"
    client._market = AsyncMock(return_value=market)
    client.get_price = AsyncMock(return_value=Decimal(price))

    assert await client.get_min_trade_usd("BTC") == Decimal(expected)


async def test_volume_reconnects_after_connection_failure(monkeypatch):
    failed = AsyncMock()
    failed.__aenter__.side_effect = CurlError("connection reset", 56)
    connected = AsyncMock()
    ws = AsyncMock()
    ws.recv_json.return_value = {
        "type": "subscribed/account_all_trades",
        "total_volume": "123.45",
    }
    connected.__aenter__.return_value = ws

    client = object.__new__(LighterRobinhoodClient)
    client.name = "test"
    client.http = Mock()
    client.http.session.ws_connect = Mock(side_effect=[failed, connected])
    client._get_auth_headers = Mock(return_value={"Authorization": "token"})
    sleep = AsyncMock()
    monkeypatch.setattr("clients.lighter.asyncio.sleep", sleep)

    assert await client._get_volume(42, []) == Decimal("123.45")
    assert client.http.session.ws_connect.call_count == 2
    assert all(
        call.args == (client.ws_url,) for call in client.http.session.ws_connect.call_args_list
    )
    sleep.assert_awaited_once_with(1.0)
    ws.send_json.assert_awaited_once_with(
        {"type": "subscribe", "channel": "account_all_trades/42", "auth": "token"}
    )


async def test_points_api_failure_returns_none():
    client = object.__new__(LighterRobinhoodClient)
    client.name = "test"
    client.address = "0xabc"
    client._get_auth_headers = Mock(return_value={"Authorization": "token"})
    client._call = AsyncMock(side_effect=ApiError("Leaderboard unavailable"))

    assert await client._get_points(42) is None


async def test_apply_referral_code_waits_between_accounts(monkeypatch):
    accounts: Any = [Mock(name="one"), Mock(name="two")]
    for name, account in zip(("one", "two"), accounts):
        account.name = name
        account.use_referral_code = AsyncMock()

    sleep = AsyncMock()
    monkeypatch.setattr(lighter_app.asyncio, "sleep", sleep)
    delay = TimeRange(min=DurationSec(30), max=DurationSec(30))

    await apply_referral_code(accounts, " code ", delay)

    for account in accounts:
        account.use_referral_code.assert_awaited_once_with("code")
    sleep.assert_awaited_once_with(30)


async def test_referral_code_continues_after_one_account_refuses(monkeypatch):
    accounts: Any = [
        client(LighterRobinhoodClient, "one"),
        client(LighterRobinhoodClient, "two"),
    ]
    accounts[0].use_referral_code = AsyncMock(side_effect=ApiError("Code already used"))
    accounts[1].use_referral_code = AsyncMock()
    monkeypatch.setattr(lighter_app.asyncio, "sleep", AsyncMock())
    delay = TimeRange(min=DurationSec(1), max=DurationSec(1))

    with pytest.raises(AppError, match="Referral code not applied: one"):
        await apply_referral_code(accounts, "code", delay)

    accounts[1].use_referral_code.assert_awaited_once_with("code")


def profile(volume: str, pnl: str, balance: str, ref_code: str | None = None) -> ProfileInfo:
    return ProfileInfo(
        addr="0x1111..1111",
        balance=Decimal(balance),
        volume=Decimal(volume),
        pnl=Decimal(pnl),
        points=Decimal(3),
        ref_code=ref_code,
    )


def trade(trade_id: int, created_at: datetime, volume: int = 100) -> LighterTrade:
    return LighterTrade(
        trade_id=trade_id,
        created_at=created_at,
        volume=Decimal(volume),
        realized_pnl=Decimal(0),
        fee=Decimal(1),
    )


def with_profile(acc, info: ProfileInfo):
    acc.address = "0x" + "11" * 20
    acc.profile = AsyncMock(return_value=info)
    acc.balance = AsyncMock(return_value=info.balance)
    return acc


@pytest.fixture
def printed_tables(monkeypatch) -> list[tuple[str | None, list[str], list[list], list]]:
    """Every table a command prints: its title, columns, rows and grand totals."""
    tables: list[tuple[str | None, list[str], list[list], list]] = []

    def capture(tbl: AutoTable) -> None:
        names = [col.name for col in tbl.columns]
        tables.append((tbl.title, names, tbl.rows, tbl._compute_totals_for_rows(tbl.rows)))

    monkeypatch.setattr(AutoTable, "print", capture)
    monkeypatch.setattr("lib.table.telemetry.track", Mock())
    return tables


async def test_info_shows_referral_commands_for_missing_code(capsys, printed_tables):
    accounts: Any = [
        with_profile(client(LighterRobinhoodClient, "ready"), profile("2", "0", "1", "existing")),
        with_profile(client(LighterRobinhoodClient, "missing"), profile("2", "0", "1")),
    ]

    await print_info(accounts, [])

    output = capsys.readouterr().out
    assert "Referral code missing: missing" in output
    assert "All enabled: uv run apps/lighter.py useref CODE" in output
    assert "One account: uv run apps/lighter.py useref CODE -a missing" in output


async def test_info_prints_one_table_per_deployment(monkeypatch, capsys, printed_tables):
    # `info` stays live: every figure comes from the profile, never from the stats cache.
    sync_trades = AsyncMock(side_effect=AssertionError("info read the trade cache"))
    monkeypatch.setattr(lighter_app, "sync_trades", sync_trades)
    accounts: Any = [
        with_profile(client(LighterRobinhoodClient, "main"), profile("1000", "-2", "50", "ref")),
        with_profile(client(LighterRobinhoodClient, "ok01"), profile("3000", "-4", "70", "ref")),
        with_profile(client(LighterRobinhoodClient, "mx01"), profile("2000", "-3", "60", "ref")),
    ]
    cores: Any = [
        with_profile(client(LighterCoreClient, "main@core"), profile("500", "-1", "20")),
        client(LighterCoreClient, "ok01@core", registered=False, logged_in=False),
        with_profile(
            client(LighterCoreClient, "mx01@core", logged_in=False), profile("0", "0", "0")
        ),
    ]
    cores[1].address = "0x" + "22" * 20

    await print_info(accounts, cores)

    (rh_title, rh_cols, rh_rows, rh_totals), (core_title, core_cols, core_rows, core_totals) = (
        printed_tables
    )
    assert rh_title == "Lighter RH"
    assert [row[1] for row in rh_rows] == ["main", "ok01", "mx01"]
    assert rh_cols[5:7] + rh_cols[-1:] == ["Points", "P/Price", "Ref"]
    totals = dict(zip(rh_cols, rh_totals))
    assert (totals["Volume"], totals["Burn"], totals["Balance"]) == (6000, 9, 180)

    # Core has no campaign: no points or referral code, and its own totals under plain names.
    assert core_title == "Lighter Core"
    assert core_cols == ["", "Account", "Address", "Volume", "Burn", "$/100k", "Balance"]
    assert [row[:2] for row in core_rows] == [["✓", "main"]]
    totals = dict(zip(core_cols, core_totals))
    assert (totals["Volume"], totals["Burn"], totals["Balance"]) == (500, 1, 20)

    sync_trades.assert_not_awaited()

    output = capsys.readouterr().out
    assert "Not on Core: ok01" in output
    assert "Not logged in on Core: mx01" in output
    assert "Referral code missing" not in output


async def test_info_without_core_accounts_prints_only_the_rh_table(capsys, printed_tables):
    accounts: Any = [
        with_profile(client(LighterRobinhoodClient, "main"), profile("1000", "-2", "50", "ref"))
    ]
    cores: Any = [client(LighterCoreClient, "main@core", registered=False, logged_in=False)]
    cores[0].address = accounts[0].address

    await print_info(accounts, cores)

    assert [title for title, *_ in printed_tables] == ["Lighter RH"]
    assert "Not on Core: main" in capsys.readouterr().out


async def test_a_failing_core_lookup_is_a_note_under_the_core_table(capsys, printed_tables):
    # A Core timeout or bad proxy must not take the RH table, or the other twins, down with it.
    accounts: Any = [
        with_profile(client(LighterRobinhoodClient, "main"), profile("1000", "-2", "50", "ref")),
        with_profile(client(LighterRobinhoodClient, "ok01"), profile("3000", "-4", "70", "ref")),
    ]
    cores: Any = [
        with_profile(client(LighterCoreClient, "main@core"), profile("500", "-1", "20")),
        client(LighterCoreClient, "ok01@core"),
    ]
    cores[1].address = "0x" + "22" * 20
    cores[1].registered = AsyncMock(side_effect=ApiError("Lighter API timeout"))

    await print_info(accounts, cores)

    assert [(title, [row[1] for row in rows]) for title, _, rows, _ in printed_tables] == [
        ("Lighter RH", ["main", "ok01"]),
        ("Lighter Core", ["main"]),
    ]
    assert "Core lookup failed: ok01: Lighter API timeout" in capsys.readouterr().out


async def test_core_twins_outside_a_hedge_join_only_when_logged_in(monkeypatch):
    # `close` and `positions` reach any twin that can act; one without a login is named.
    warning = Mock()
    monkeypatch.setattr("apps.lighter.logger.warning", warning)
    cores: Any = [
        client(LighterCoreClient, "main@core"),
        client(LighterCoreClient, "ok01@core", logged_in=False),
        client(LighterCoreClient, "mx01@core", registered=False, logged_in=False),
        client(LighterCoreClient, "mx02@core"),
    ]
    cores[3].registered = AsyncMock(side_effect=ApiError("Lighter API timeout"))

    assert await lighter_app.logged_in_cores(cores) == [cores[0]]
    assert [call.args[0] for call in warning.call_args_list] == [
        "Not logged in on Core, skipped: ok01@core",
        "Core lookup failed, skipped: mx02@core: Lighter API timeout",
    ]


async def test_cache_paths_separate_the_two_venues(monkeypatch):
    paths: list[str] = []

    class FakeStore:
        def __init__(self, filepath, **_kwargs):
            paths.append(filepath)

        async def sync(self, *_args, **_kwargs) -> None:
            pass

        def get_all(self) -> list:
            return []

        def needs_sync(self, _ttl: int) -> bool:
            return False

    monkeypatch.setattr(lighter_app, "DataStore", FakeStore)
    accounts: Any = [Mock(), Mock()]
    for account, exchange in zip(accounts, ("lighter", "lighter_core")):
        account.exchange = exchange
        account.address = "0x" + "11" * 20
        account.trades = AsyncMock()
        account.funding_history = AsyncMock()

    for account in accounts:
        await lighter_app.sync_trades(account, 0)
        await lighter_app.sync_funding(account, 0)
        await lighter_app.sync_points_total(account, 0)

    assert paths == [
        ".cache/lighter_0x1111..1111_trades.pkl",
        ".cache/lighter_0x1111..1111_funding.pkl",
        ".cache/lighter_0x1111..1111_points_total.pkl",
        ".cache/lighter_core_0x1111..1111_trades.pkl",
        ".cache/lighter_core_0x1111..1111_funding.pkl",
        ".cache/lighter_core_0x1111..1111_points_total.pkl",
    ]


# MARK: Login state


async def test_stats_skip_an_account_the_exchange_does_not_know(monkeypatch):
    # A Core account is funded by hand, so it can stay unregistered for days.
    info = Mock()
    monkeypatch.setattr("apps.lighter.logger.info", info)
    rh = client(LighterRobinhoodClient, "rh1")
    core = client(LighterCoreClient, "main@core", registered=False, logged_in=False)

    assert await lighter_app.ready_accounts([rh, core], "stats") == [rh]
    info.assert_called_once_with("Not registered, no stats: main@core")


async def test_a_registered_account_still_requires_a_login():
    rh = client(LighterRobinhoodClient, "rh1")
    core = client(LighterCoreClient, "main@core", logged_in=False)

    with pytest.raises(AppError, match="Login required: main@core"):
        await lighter_app.ready_accounts([rh, core], "close")


async def test_nothing_left_to_act_on_reports_the_funding_step():
    core = client(LighterCoreClient, "main@core", registered=False, logged_in=False)

    with pytest.raises(
        AppError, match="Not registered — main@core: fund it on Lighter Core by hand"
    ):
        await lighter_app.ready_accounts([core], "close")


@pytest.mark.parametrize(
    ("client_type", "expected"),
    [
        (LighterRobinhoodClient, "Not registered — rh1: uv run apps/lighter.py deposit"),
        (LighterCoreClient, "Not registered — rh1: fund it on Lighter Core by hand"),
    ],
)
async def test_trade_names_the_funding_route_per_venue(client_type, expected):
    acc = client(client_type, "rh1", registered=False, logged_in=False)

    with pytest.raises(AppError, match=expected):
        await lighter_app.require_login([acc])


async def test_trade_refuses_an_unregistered_account():
    # A pool is delta-neutral only with every leg, so trade never skips one.
    rh = client(LighterRobinhoodClient, "rh1")
    core = client(LighterCoreClient, "main@core", registered=False, logged_in=False)

    with pytest.raises(AppError, match="Not registered"):
        await lighter_app.require_login([rh, core])


async def test_the_robinhood_login_message_is_unchanged():
    rh = client(LighterRobinhoodClient, "rh1", logged_in=False)

    message = r"^Login required: rh1\. Run: uv run apps/lighter\.py login$"
    with pytest.raises(AppError, match=message):
        await lighter_app.require_login([rh])


async def test_a_logged_in_selection_passes_both_checks():
    accounts = [client(LighterRobinhoodClient, "rh1"), client(LighterCoreClient, "main@core")]

    assert await lighter_app.require_login(accounts) is None
    assert await lighter_app.ready_accounts(accounts, "close") == accounts


# MARK: Week labels


def test_core_weeks_of_different_years_stay_apart_and_in_order():
    dates = [datetime(2024, 12, 30, tzinfo=UTC), datetime(2025, 11, 12, tzinfo=UTC), TRADED_AT]
    labels = [lighter_app.to_iso_week_label(dt) for dt in dates]

    assert labels == ["2025-W01 Dec30-Jan05", "2025-W46 Nov10-Nov16", "2026-W46 Nov09-Nov15"]
    assert sorted(labels) == labels


async def _stats_tables(monkeypatch, printed_tables, period: str, filter_period: str = "this"):
    traded = {
        "main": [TRADED_AT],
        "ok01": [TRADED_AT, TRADED_AT],
        "main@core": [BEFORE_CAMPAIGN, datetime(2026, 9, 14, tzinfo=UTC), TRADED_AT],
    }
    paid = {"main@core": [(BEFORE_CAMPAIGN, -50)]}

    async def sync_trades(acc, _ttl):
        return [trade(i, dt) for i, dt in enumerate(traded[acc.name])]

    async def sync_funding(acc, _ttl):
        return [
            LighterFunding(funding_id=i, created_at=dt, change=Decimal(change))
            for i, (dt, change) in enumerate(paid.get(acc.name, []))
        ]

    monkeypatch.setattr(lighter_app, "sync_trades", sync_trades)
    monkeypatch.setattr(lighter_app, "sync_funding", sync_funding)
    monkeypatch.setattr(lighter_app, "sync_points_total", AsyncMock())
    accounts: Any = [client(LighterRobinhoodClient, "main"), client(LighterRobinhoodClient, "ok01")]
    cores: Any = [
        client(LighterCoreClient, "main@core"),
        client(LighterCoreClient, "ok01@core", registered=False, logged_in=False),
        client(LighterCoreClient, "mx01@core", logged_in=False),
    ]

    await lighter_app.print_stats(accounts, cores, period=period, filter_period=filter_period)
    # Title, then (period, account, trades, volume) per row, then the grand totals.
    return [
        (title, [row[:4] for row in rows], dict(zip(cols, totals)))
        for title, cols, rows, totals in printed_tables
    ]


async def test_weekly_stats_print_one_block_per_deployment(monkeypatch, printed_tables):
    (rh_title, rh_rows, rh_totals), (core_title, core_rows, core_totals) = await _stats_tables(
        monkeypatch, printed_tables, "week"
    )

    assert rh_title == "Lighter RH"
    assert rh_rows == [["main", 1, Decimal(100), Decimal(1)], ["ok01", 2, Decimal(200), Decimal(2)]]
    assert (rh_totals["Trades"], rh_totals["Volume"]) == (3, Decimal(300))

    assert core_title == "Lighter Core"
    assert core_rows == [["main", 1, Decimal(100), Decimal(1)]]  # this ISO week only
    assert (core_totals["Trades"], core_totals["Volume"]) == (1, Decimal(100))


async def test_stats_periods_follow_each_deployment_calendar(monkeypatch, printed_tables):
    shown: list[list[str]] = []
    monkeypatch.setattr(
        lighter_app, "render_stats", lambda periods, picked, **_: shown.append(list(periods))
    )

    # Sorted together, one calendar's labels would always outrank the other's for `this`.
    await _stats_tables(monkeypatch, printed_tables, "week", "all")
    assert shown == [["W11 Nov10-Nov16"], ["2026-W38 Sep14-Sep20", "2026-W46 Nov09-Nov15"]]

    shown.clear()
    await _stats_tables(monkeypatch, printed_tables, "day", "all")
    assert shown == [["2026-11-10"], ["2026-09-14", "2026-11-10"]]


@pytest.mark.parametrize("period", ["week", "day"])
async def test_core_stats_start_at_the_campaign(monkeypatch, printed_tables, period):
    # Manual Core trading and funding before the campaign start is not farming.
    _, (_, core_rows, core_totals) = await _stats_tables(monkeypatch, printed_tables, period, "all")

    assert core_rows == [
        ["main", 1, Decimal(100), Decimal(1)],
        ["main", 1, Decimal(100), Decimal(1)],
    ]
    assert (core_totals["Trades"], core_totals["Volume"], core_totals["Burn"]) == (2, 200, 2)


async def test_a_core_account_without_login_does_not_stop_stats(monkeypatch, printed_tables):
    # A wallet can hold a Core account outside any hedge, which `login` never signs in.
    info = Mock()
    monkeypatch.setattr("apps.lighter.logger.info", info)

    tables = await _stats_tables(monkeypatch, printed_tables, "week")

    assert [title for title, *_ in tables] == ["Lighter RH", "Lighter Core"]
    info.assert_called_once_with("Not logged in, no Core stats: mx01@core")


@pytest.mark.parametrize("failing", ["registered", "sync"])
async def test_core_trouble_never_hides_the_rh_stats(monkeypatch, printed_tables, failing):
    warning = Mock()
    monkeypatch.setattr("apps.lighter.logger.warning", warning)
    trades = AsyncMock(return_value=[])
    if failing == "sync":
        trades = AsyncMock(side_effect=lambda acc, _ttl: _core_sync_fails(acc))
    monkeypatch.setattr(lighter_app, "sync_trades", trades)
    monkeypatch.setattr(lighter_app, "sync_funding", AsyncMock(return_value=[]))
    monkeypatch.setattr(lighter_app, "sync_points_total", AsyncMock())
    accounts: Any = [client(LighterRobinhoodClient, "main")]
    cores: Any = [client(LighterCoreClient, "main@core")]
    if failing == "registered":
        cores[0].registered = AsyncMock(side_effect=ApiError("Lighter API timeout"))

    await lighter_app.print_stats(accounts, cores, period="week", filter_period="this")

    assert [title for title, *_ in printed_tables] == ["Lighter RH"]
    assert "Lighter API timeout" in warning.call_args.args[0]


def _core_sync_fails(acc) -> list:
    if acc.exchange == "lighter_core":
        raise ApiError("Lighter API timeout")
    return []


async def test_daily_stats_keep_the_deployments_apart(monkeypatch, printed_tables):
    tables = await _stats_tables(monkeypatch, printed_tables, "day")

    assert [(title, totals["Volume"]) for title, _, totals in tables] == [
        ("Lighter RH", Decimal(300)),
        ("Lighter Core", Decimal(100)),
    ]


@pytest.mark.parametrize(
    "dt",
    [
        TRADED_AT,
        datetime(2024, 12, 30, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 8, 30, tzinfo=UTC),
    ],
)
def test_core_week_labels_match_the_weekly_report(dt):
    # `stats` and the weekly report must bucket the same Core trade into the same period.
    assert lighter_app.to_iso_week_label(dt) == weekly.lighter_core_week(dt)


# MARK: Weekly report


def _write_cache(cache: Path, name: str, records: list[dict]) -> None:
    payload = {"last_sync": TRADED_AT, "records": {str(i): r for i, r in enumerate(records)}}
    (cache / name).write_bytes(pickle.dumps(payload))


def _trade(volume: str, created_at: datetime = TRADED_AT) -> dict:
    return {"created_at": created_at, "volume": volume, "fee": "1", "realized_pnl": "0"}


@pytest.fixture
def both_venues(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(weekly, "CACHE", str(tmp_path))
    _write_cache(tmp_path, "lighter_0xaaaa..aaaa_trades.pkl", [_trade("1000")])
    _write_cache(
        tmp_path, "lighter_0xaaaa..aaaa_funding.pkl", [{"created_at": TRADED_AT, "change": "-2"}]
    )
    _write_cache(
        tmp_path, "lighter_0xaaaa..aaaa_points_total.pkl", [{"id": "total", "points": "42"}]
    )
    # Manual Core trading and funding before the campaign start stays out of the report.
    _write_cache(
        tmp_path,
        "lighter_core_0xbbbb..bbbb_trades.pkl",
        [_trade("9000", BEFORE_CAMPAIGN), _trade("500")],
    )
    _write_cache(
        tmp_path,
        "lighter_core_0xbbbb..bbbb_funding.pkl",
        [{"created_at": BEFORE_CAMPAIGN, "change": "-50"}],
    )
    return tmp_path


def _report_row(out: str, name: str) -> list[str]:
    for line in out.splitlines():
        cells = [cell.strip() for cell in line.split("  ") if cell.strip()]
        if cells and cells[0] == name:
            return cells
    raise AssertionError(f"No {name!r} row in:\n{out}")


def test_core_caches_are_not_read_as_robinhood_caches(both_venues):
    assert weekly.glob_cache("lighter_", "_trades.pkl") == [
        os.path.join(str(both_venues), "lighter_0xaaaa..aaaa_trades.pkl")
    ]
    assert weekly.lighter_stats() == {"W11 Nov10-Nov16": (Decimal(1000), Decimal(3))}
    assert weekly.lighter_core_stats() == {"2026-W46 Nov09-Nov15": (Decimal(500), Decimal(1))}
    assert weekly.lighter_core_burn_weeks() == {"2026-W46": (Decimal(500), Decimal(1), Decimal(0))}


def test_robinhood_points_ignore_core_accounts(both_venues):
    # The Core account has no points cache; read as RH, it would void the RH total.
    assert weekly.lighter_total_pts() == Decimal(42)


def test_weekly_report_gives_each_venue_its_own_row(both_venues, capsys):
    assert weekly.report_view() == 0

    out = capsys.readouterr().out
    assert _report_row(out, "Lighter")[1:4] == ["1,000", "3.00", "42.0"]
    assert _report_row(out, "Lighter Core")[1:4] == ["500", "1.00", "0.0"]
    assert "· Lighter points: 42.0" in out
    assert "· Lighter Core points" not in out


def test_a_robinhood_only_cache_reports_exactly_as_before(both_venues, capsys):
    for name in ("lighter_core_0xbbbb..bbbb_trades.pkl", "lighter_core_0xbbbb..bbbb_funding.pkl"):
        (both_venues / name).unlink()

    assert weekly.report_view() == 0

    out = capsys.readouterr().out
    assert _report_row(out, "Lighter")[1:4] == ["1,000", "3.00", "42.0"]
    assert "Lighter Core" not in out


def test_core_is_reachable_without_quoting_its_name():
    assert weekly._exchange_by_name("lightercore")[0] == "Lighter Core"
    assert weekly._exchange_by_name("Lighter Core")[0] == "Lighter Core"
    assert weekly._exchange_by_name("lighter")[0] == "Lighter"


def test_core_reports_volume_and_burn_but_no_points():
    assert "Lighter Core" not in weekly.CURRENT_POINTS
    assert [name for name, *_ in weekly.EXCHANGES if name.startswith("Lighter")] == [
        "Lighter",
        "Lighter Core",
    ]
    assert [name for name, _ in weekly.BURN_EXCHANGES if name.startswith("Lighter")] == [
        "Lighter",
        "Lighter Core",
    ]
