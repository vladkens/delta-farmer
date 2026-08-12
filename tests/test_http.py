# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | If it compiles, ship it
from unittest.mock import Mock

import pytest

from lib import unwaf_cf
from lib.http import AsyncHttp
from lib.utils import pickle_load


async def test_clear_cookies_removes_persisted_cookie_jar(tmp_path):
    cookies_file = str(tmp_path / "cookies.pkl")
    saved_http = AsyncHttp(baseurl="https://example.com", headers={}, cookies_file=cookies_file)
    saved_http.session.cookies.set("stale", "cookie", domain="example.com")
    await saved_http.close()

    http = AsyncHttp(baseurl="https://example.com", headers={}, cookies_file=cookies_file)
    try:
        http.clear_cookies()

        assert list(http.session.cookies.jar) == []
        assert pickle_load(cookies_file) == {}
    finally:
        await http.close()


async def test_managed_cf_capacity_error_is_not_retried(monkeypatch):
    requests = 0

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, _json=None, **_kwargs):
            nonlocal requests
            requests += 1
            return Mock(
                json=Mock(
                    return_value={
                        "errorId": 1,
                        "errorCode": "ACTIVE_TASKS_LIMIT_EXCEEDED",
                    }
                )
            )

    monkeypatch.setattr(unwaf_cf, "AsyncSession", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(unwaf_cf, "parse_proxy", lambda _proxy: "http://proxy")

    with pytest.raises(RuntimeError, match="ACTIVE_TASKS_LIMIT_EXCEEDED"):
        await unwaf_cf.solve_managed_cf_clearance(
            Mock(), "https://omni.variational.io", proxy="proxy"
        )

    assert requests == 1
