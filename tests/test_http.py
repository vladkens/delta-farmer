from unittest.mock import AsyncMock, Mock

import pytest
from curl_cffi.requests import errors

from lib.http import AsyncHttp
from lib.utils import pickle_load


async def test_cookies_are_cleared(tmp_path):
    cookies_file = str(tmp_path / "cookies.pkl")
    saved = AsyncHttp(baseurl="https://example.com", headers={}, cookies_file=cookies_file)
    saved.session.cookies.set("stale", "cookie", domain="example.com")
    await saved.close()

    http = AsyncHttp(baseurl="https://example.com", headers={}, cookies_file=cookies_file)
    try:
        http.clear_cookies()

        assert list(http.session.cookies.jar) == []
        assert pickle_load(cookies_file) == {}
    finally:
        await http.close()


async def test_http_retry_can_be_disabled():
    http = AsyncHttp(baseurl="https://example.com", headers={})
    http.session.request = AsyncMock(side_effect=errors.RequestsError("connection lost"))
    try:
        with pytest.raises(errors.RequestsError, match="connection lost"):
            await http.request("POST", "/transfer", retry=False)

        http.session.request.assert_awaited_once()
    finally:
        await http.close()


@pytest.mark.parametrize(
    ("failures", "expected_logs"),
    [(1, 0), (3, 1)],
    ids=["recovered", "persistent"],
)
async def test_http_error_logging(monkeypatch, failures, expected_logs):
    http = AsyncHttp(baseurl="https://example.com", headers={})
    response = Mock(status_code=200, text="")
    http.session.request = AsyncMock(
        side_effect=[*[errors.RequestsError("timeout") for _ in range(failures)], response]
    )
    debug = Mock()
    monkeypatch.setattr("lib.http.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("lib.http.logger.debug", debug)
    try:
        assert await http.request("GET", "/account") is response
        assert debug.call_count == expected_logs
    finally:
        await http.close()
