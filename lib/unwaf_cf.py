# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | If it compiles, ship it
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.impersonate import DEFAULT_CHROME

from lib.http import AsyncHttp, parse_proxy
from lib.logger import logger

# Algorithm adapted from: https://github.com/B00H0O/cloudflare-jsd-solver
#
# This only targets Cloudflare's silent JSD sensor flow:
#   1. fetch a page containing __CF$cv$params
#   2. fetch /cdn-cgi/challenge-platform/scripts/jsd/main.js
#   3. extract site key, oneshot path, and the encoder alphabet
#   4. post the compressed browser fingerprint payload to /jsd/oneshot
#
# It is not a Managed Challenge or Turnstile solver.


def _default_chrome_version() -> str:
    major = DEFAULT_CHROME.removeprefix("chrome").split("_", 1)[0]
    if not major.isdigit():
        raise RuntimeError(f"Unsupported curl_cffi Chrome target: {DEFAULT_CHROME}")
    return f"{major}.0.0.0"


_APP_VERSION = (
    f"5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_default_chrome_version()} Safari/537.36"
)
USER_AGENT = f"Mozilla/{_APP_VERSION}"
ASTRUM_API = "https://solver.astrum.foundation/api"
GATEWAY_API = "https://delta-gateway.fly.dev/api"


@dataclass(frozen=True)
class _ChallengeParams:
    r: str
    t: str


@dataclass(frozen=True)
class _ScriptData:
    sitekey: str
    path: str
    alphabet: str


def _origin_from_url(raw: str) -> str:
    u = urlparse(raw)
    return f"{u.scheme}://{u.netloc}"


def _find_cookie(http: AsyncHttp, name: str) -> str | None:
    for cookie in http.session.cookies.jar:
        if cookie.name == name:
            return cookie.value
    return None


def _parse_challenge_params(html: str) -> _ChallengeParams:
    match = re.search(r"__CF\$cv\$params\s*=\s*\{([^}]*)\}", html)
    if not match:
        raise ValueError("Cloudflare JSD params not found")

    block = match.group(1)
    r_match = re.search(r"\br\s*:\s*['\"]([a-fA-F0-9]+)['\"]", block)
    t_match = re.search(r"\bt\s*:\s*['\"]([^'\"]+)['\"]", block)
    if not r_match or not t_match:
        raise ValueError(f"Cloudflare JSD params incomplete: {block}")

    return _ChallengeParams(r=r_match.group(1), t=t_match.group(1))


def _parse_script_data(script: str) -> _ScriptData:
    sitekey_match = re.search(
        r"(?:window\.)?\s*_cf_chl_opt\s*=\s*\{\s*\w+\s*:\s*['\"]([^'\"]+)['\"]",
        script,
    )
    if not sitekey_match:
        raise ValueError("Cloudflare JSD sitekey not found")

    path_match = re.search(r"/jsd/oneshot/([^`'\",\)]+)", script)
    if not path_match:
        raise ValueError("Cloudflare JSD oneshot path not found")

    alphabet: str | None = None
    for match in re.finditer(r"([A-Za-z_$][\w$]*)\s*=\s*`([^`]+)`", script):
        value = match.group(2)
        if (
            64 <= len(value) <= 70
            and len(set(value)) == len(value)
            and "+" in value
            and "$" in value
            and ";" not in value
        ):
            alphabet = value
            break

    if not alphabet:
        raise ValueError("Cloudflare JSD encoder alphabet not found")

    return _ScriptData(
        sitekey=sitekey_match.group(1),
        path=path_match.group(1),
        alphabet=alphabet,
    )


def _emit_lz_bits(
    out: list[str],
    key: str,
    state: list[int],
    value: int,
    bit_count: int,
) -> None:
    data_val, data_pos = state
    for _ in range(bit_count):
        data_val = (data_val << 1) | (value & 1)
        if data_pos == 5:
            out.append(key[data_val])
            data_val = 0
            data_pos = 0
        else:
            data_pos += 1
        value >>= 1
    state[0], state[1] = data_val, data_pos


def _lz_compress_to_base64(text: str, alphabet: str) -> str:
    if not text:
        return ""

    dictionary: dict[str, int] = {}
    to_create: dict[str, bool] = {}
    dict_size = 3
    enlarge_in = 2
    num_bits = 2
    data: list[str] = []
    state = [0, 0]  # data value, data bit position
    w = ""

    for c in text:
        if c not in dictionary:
            dictionary[c] = dict_size
            dict_size += 1
            to_create[c] = True

        wc = w + c
        if wc in dictionary:
            w = wc
            continue

        if w in to_create:
            codepoint = ord(w[0])
            if codepoint < 256:
                _emit_lz_bits(data, alphabet, state, 0, num_bits)
                _emit_lz_bits(data, alphabet, state, codepoint, 8)
            else:
                _emit_lz_bits(data, alphabet, state, 1, num_bits)
                _emit_lz_bits(data, alphabet, state, codepoint, 16)

            enlarge_in -= 1
            if enlarge_in == 0:
                enlarge_in = 1 << num_bits
                num_bits += 1
            del to_create[w]
        else:
            _emit_lz_bits(data, alphabet, state, dictionary[w], num_bits)

        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

        dictionary[wc] = dict_size
        dict_size += 1
        w = c

    if w:
        if w in to_create:
            codepoint = ord(w[0])
            if codepoint < 256:
                _emit_lz_bits(data, alphabet, state, 0, num_bits)
                _emit_lz_bits(data, alphabet, state, codepoint, 8)
            else:
                _emit_lz_bits(data, alphabet, state, 1, num_bits)
                _emit_lz_bits(data, alphabet, state, codepoint, 16)

            enlarge_in -= 1
            if enlarge_in == 0:
                enlarge_in = 1 << num_bits
                num_bits += 1
            del to_create[w]
        else:
            _emit_lz_bits(data, alphabet, state, dictionary[w], num_bits)

        enlarge_in -= 1
        if enlarge_in == 0:
            num_bits += 1

    _emit_lz_bits(data, alphabet, state, 2, num_bits)

    data_val, data_pos = state
    while True:
        data_val <<= 1
        if data_pos == 5:
            data.append(alphabet[data_val])
            break
        data_pos += 1

    out = "".join(data)
    match len(out) % 4:
        case 0:
            return out
        case 1:
            return out + "==="
        case 2:
            return out + "=="
        case 3:
            return out + "="
        case _:
            return out


def _fingerprint(target_url: str) -> dict[str, list[str]]:
    origin = _origin_from_url(target_url)
    host = urlparse(target_url).netloc
    return {
        "0": ["length", "innerWidth", "innerHeight", "scrollX", "pageXOffset", "scrollY"],
        "1.25": ["devicePixelRatio"],
        "Google Inc.": ["n.vendor"],
        "Mozilla": ["n.appCodeName"],
        "Netscape": ["n.appName"],
        _APP_VERSION: ["n.appVersion"],
        "MacIntel": ["n.platform"],
        "Gecko": ["n.product"],
        USER_AGENT: ["n.userAgent"],
        "en-US": ["n.language", "n.languages"],
        origin: ["origin"],
        host: ["d.domain"],
        "about:blank": ["d.URL", "d.documentURI", "d.referrer"],
        "BackCompat": ["d.compatMode"],
        "UTF-8": ["d.characterSet", "d.charset", "d.inputEncoding"],
        "text/html": ["d.contentType"],
        "s": ["d.cookie"],
        time.strftime("%m/%d/%Y %H:%M:%S"): ["d.lastModified"],
        "complete": ["d.readyState"],
        "visible": ["d.visibilityState", "d.webkitVisibilityState"],
        "#document": ["d.nodeName"],
        target_url: ["d.baseURI"],
        "T": ["isSecureContext", "originAgentCluster", "n.cookieEnabled", "n.onLine"],
        "F": ["closed", "crossOriginIsolated", "credentialless", "d.hidden"],
        "u": ["event", "undefined"],
    }


def _build_payload(target_url: str) -> str:
    payload = {
        "t": int(time.time()),
        "lhr": "about:blank",
        "api": False,
        "c": False,
        "payload": _fingerprint(target_url),
    }
    return json.dumps(payload, separators=(",", ":"))


async def solve_cf_clearance(http: AsyncHttp, target_url: str) -> bool:
    """Run Cloudflare JSD oneshot flow and leave received cookies in http.session."""
    origin = _origin_from_url(target_url)
    page = await http.request("GET", target_url)
    params = _parse_challenge_params(page.text)

    script_url = f"{origin}/cdn-cgi/challenge-platform/scripts/jsd/main.js"
    script = await http.request("GET", script_url)
    script_data = _parse_script_data(script.text)

    body = _lz_compress_to_base64(_build_payload(target_url), script_data.alphabet)
    endpoint = (
        f"{origin}/cdn-cgi/challenge-platform/h/"
        f"{script_data.sitekey}/jsd/oneshot/{script_data.path}{params.r}"
    )

    await http.request(
        "POST",
        endpoint,
        data=body,
        headers={
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "text/plain;charset=UTF-8",
            "origin": origin,
            "referer": f"{origin}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": USER_AGENT,
            "priority": "u=1, i",
        },
        default_headers=False,
    )

    return bool(_find_cookie(http, "cf_clearance"))


async def ensure_cf_clearance(http: AsyncHttp, target_url: str, *, force: bool = False) -> bool:
    """Solve Cloudflare JSD when no clearance exists or Cloudflare rejected one."""
    if not force and _find_cookie(http, "cf_clearance"):
        return True
    try:
        return await solve_cf_clearance(http, target_url)
    except ValueError as e:
        if "Cloudflare JSD params not found" in str(e):
            return False
        raise


async def solve_managed_cf_clearance(
    http: AsyncHttp, target_url: str, *, proxy: str | None
) -> None:
    """Request a Cloudflare Managed Challenge clearance from Astrum."""
    api_key = os.getenv("CAPTCHA_KEY", "")
    solver_api = ASTRUM_API if api_key else GATEWAY_API

    proxy_url = parse_proxy(proxy)
    if not proxy_url:
        raise RuntimeError("Proxy is required for Cloudflare Managed Challenge")

    async with AsyncSession(timeout=(10, 60)) as session:
        task = {
            "type": "cf_clearance",
            "websiteURL": target_url,
            "proxyURL": proxy_url,
            "userAgent": USER_AGENT,
        }

        rep = await session.post(
            f"{solver_api}/createTask", json={"clientKey": api_key, "task": task}
        )
        res = rep.json()
        if res.get("errorId", 0) != 0 or not res.get("taskId"):
            raise RuntimeError(f"Cloudflare task creation failed: {res}")

        task_id = res["taskId"]
        logger.debug(f"Cloudflare task {task_id} created")
        for _ in range(90):
            await asyncio.sleep(3)
            rep = await session.post(
                f"{solver_api}/getTaskResult",
                json={"clientKey": api_key, "task": {"taskId": task_id, "type": "cf_clearance"}},
            )
            res = rep.json()
            token = res.get("solution", {}).get("token")
            status = res.get("status")

            if status in {"opened", "in_progress"}:
                continue

            if status == "closed":
                if not token:
                    raise RuntimeError("Cloudflare task closed without cf_clearance")
                host = urlparse(target_url).hostname
                if not host:
                    raise RuntimeError(f"Invalid Cloudflare target URL: {target_url}")
                http.session.cookies.set("cf_clearance", token, domain=host, path="/")
                logger.debug(f"Cloudflare task {task_id} solved")
                return

            raise RuntimeError(f"Cloudflare task failed: {res}")

    raise RuntimeError("Cloudflare task timed out")
