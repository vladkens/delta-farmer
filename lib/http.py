# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | If it compiles, ship it
import asyncio
import hashlib

from curl_cffi.requests import AsyncSession, Response, errors
from curl_cffi.requests.session import HttpMethod

from .logger import logger
from .utils import pickle_dump, pickle_load

__all__ = ["ApiError", "AsyncHttp", "HttpMethod", "NotFoundError", "parse_proxy"]


class ApiError(Exception):
    """Transient API or network error — safe to retry."""

    def __init__(self, msg: str, rep: Response | None = None):
        if rep is not None:
            body = rep.text.strip() if rep.text else ""
            body = "[html]" if body.startswith("<") else body  # Cloudflare WAF or nginx error page
            super().__init__(
                f"{msg}: {rep.status_code} {body}" if body else f"{msg}: {rep.status_code}"
            )
        else:
            super().__init__(msg)


class NotFoundError(ApiError):
    """Resource not found — expected empty result, not a failure."""


def _cookies_hash(jar: dict) -> str:
    """Create deterministic hash from cookie jar for comparison."""
    items = []
    for domain in sorted(jar.keys()):
        for path in sorted(jar[domain].keys()):
            for name in sorted(jar[domain][path].keys()):
                cookie = jar[domain][path][name]
                items.append(f"{domain}|{path}|{name}|{cookie.value}")
    return hashlib.md5("".join(items).encode()).hexdigest()


def parse_proxy(proxy: str | None) -> str | None:
    if not proxy:
        return None

    if not proxy.startswith("http") and proxy.count(":") == 3:
        parts = proxy.split(":")
        proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        return proxy

    return proxy


class AsyncHttp:
    def __init__(
        self,
        *,
        baseurl: str,
        headers: dict[str, str],
        proxy: str | None = None,
        cookies_file: str | None = None,
    ):
        assert baseurl.strip(), "baseurl is required"
        self.baseurl = baseurl
        self.cookies_file = cookies_file
        self._cookies_loaded = False
        self._cookies_hash = None
        self.session = AsyncSession(
            impersonate="chrome",
            headers=headers,
            proxy=parse_proxy(proxy),
            timeout=(10, 60),  # (connect, read)
        )

    def _load_cookies(self) -> None:
        # https://curl-cffi.readthedocs.io/en/latest/cookies.html
        if not self.cookies_file:
            return

        cookies = pickle_load(self.cookies_file, lock=True, delete_on_error=True)
        if cookies:
            self.session.cookies.jar._cookies.update(cookies)  # type: ignore
            # logger.debug(f"Loaded cookies from {self.cookies_file}")

        jar = self.session.cookies.jar._cookies  # type: ignore
        self._cookies_hash = _cookies_hash(jar)

    def _save_cookies(self) -> None:
        # https://curl-cffi.readthedocs.io/en/latest/cookies.html
        if not self.cookies_file:
            return

        new_jar = self.session.cookies.jar._cookies  # type: ignore
        new_hash = _cookies_hash(new_jar)

        if new_hash == self._cookies_hash:
            return

        pickle_dump(self.cookies_file, new_jar, lock=True)
        self._cookies_hash = new_hash

    def load_cookies(self) -> None:
        if self._cookies_loaded:
            return

        self._load_cookies()
        self._cookies_loaded = True

    async def close(self) -> None:
        self._save_cookies()
        await self.session.close()

    def clear_cookies(self) -> None:
        self.session.cookies.clear()
        self._cookies_loaded = True
        self._cookies_hash = None
        self._save_cookies()

    def _build_url(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url

        return f"{self.baseurl.rstrip('/')}/{url.lstrip('/')}"

    async def request(self, method: HttpMethod, url: str, **kwargs) -> Response:
        self.load_cookies()

        fullurl = self._build_url(url)
        logname = f"Http {method} {url.split('?')[0]}"
        max_retries, retries = 9, 0
        first_error_logged = False
        while True:
            try:
                rep = await self.session.request(method, fullurl, **kwargs)
                self._save_cookies()
                logger.trace(f">> {logname} response: {rep.status_code} {rep.text}")
                return rep
            except (errors.CurlError, errors.RequestsError) as e:
                retries += 1
                if retries >= max_retries:
                    logger.error(f"{logname} failed after {max_retries} retries.")
                    raise e

                if not first_error_logged:
                    logger.debug(f"{logname} network error ({type(e)}), retrying...")
                    first_error_logged = True

                wait_sec = 0.75 * retries
                await asyncio.sleep(wait_sec)
                continue
