# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Probably works in production
import asyncio

from curl_cffi.requests import AsyncSession, Response, errors
from curl_cffi.requests.session import HttpMethod  # noqa: F401

from .logger import logger

__all__ = ["AsyncHttp", "HttpMethod", "parse_proxy"]


def parse_proxy(proxy: str | None) -> str | None:
    if not proxy:
        return None

    if not proxy.startswith("http") and proxy.count(":") == 3:
        parts = proxy.split(":")
        proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        return proxy

    return proxy


class AsyncHttp:
    def __init__(self, *, baseurl: str, headers: dict[str, str], proxy: str | None = None):
        assert baseurl.strip(), "baseurl is required"
        self.baseurl = baseurl
        self.session = AsyncSession(
            impersonate="chrome",
            headers=headers,
            proxy=parse_proxy(proxy),
            timeout=(10, 60),  # (connect, read)
            # HttpVersionLiteral = Literal["v1", "v2", "v2tls", "v2_prior_knowledge", "v3", "v3only"]
            # http_version="v3",
            # curl_options={"VERBOSE": True},
        )

    def _build_url(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url

        return f"{self.baseurl.rstrip('/')}/{url.lstrip('/')}"

    async def close(self) -> None:
        await self.session.close()

    async def request(self, method: HttpMethod, url: str, **kwargs) -> Response:
        fullurl = self._build_url(url)
        logname = f"Http {method} {url.split('?')[0]}"

        max_retries, retries = 9, 0
        first_error_logged = False
        while True:
            try:
                rep = await self.session.request(method, fullurl, **kwargs)

                # cf_headers = ["ratelimit-policy", "ratelimit"]
                # if any(h in rep.headers for h in cf_headers):
                #     logger.debug(
                #         f"{logname} response headers: { {h: rep.headers[h] for h in cf_headers if h in rep.headers} }"
                #     )

                return rep
            except (errors.CurlError, errors.RequestsError) as e:
                retries += 1
                if retries >= max_retries:
                    logger.error(f"{logname} failed after {max_retries} retries.")
                    raise e

                if not first_error_logged:
                    logger.debug(f"{logname} network error, retrying...")
                    first_error_logged = True

                wait_sec = 0.75 * retries
                await asyncio.sleep(wait_sec)
                continue
