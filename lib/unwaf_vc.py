# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | If it compiles, ship it
import base64
import hashlib
import secrets
from urllib.parse import urlparse

from lib.http import AsyncHttp

# Algorithm adapted from: https://github.com/notemrovsky/vercel-waf-solver


def _find_nonce(seed2: str, expected_prefix: str) -> tuple[str, str]:
    while True:
        nonce = secrets.token_hex(8)
        hash_result = hashlib.sha256((seed2 + nonce).encode()).hexdigest()
        if hash_result[:4] == expected_prefix:
            return nonce, hash_result


def _parse_token(token: str) -> dict:
    _, request_id, difficulty, challenge_b64, _ = token.split(".")
    challenge_parts = base64.b64decode(challenge_b64).split(b";")
    return {
        "request_id": int(request_id),
        "difficulty": int(difficulty),
        "seed2": challenge_parts[1].decode("ascii"),
        "seed3": challenge_parts[2].decode("ascii"),
        "count": int(challenge_parts[3].decode("ascii")),
    }


def _solve_challenge(token: str) -> str:
    K = [498787, 533737, 619763, 708403, 828071]
    p = _parse_token(token)
    M, seed2, seed3 = p["request_id"], p["seed2"], p["seed3"]
    initial_offset = (M * K[M % 5]) % 36
    nonces: list[str] = []
    prev_hash = ""
    for i in range(p["count"]):
        if i == 0:
            prefix = seed3[initial_offset : initial_offset + 4]
        else:
            offset = (M * K[(i - 1) % 5]) % p["difficulty"]
            prefix = prev_hash[offset : offset + 4]
        nonce, prev_hash = _find_nonce(seed2, prefix)
        nonces.append(nonce)
    return ";".join(nonces)


async def ensure_unwaf(http: AsyncHttp, url: str) -> None:
    """Solve Vercel WAF PoW challenge if present. Cookie is saved via http's cookies_file."""
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    rep = await http.request("POST", url, headers={"Referer": f"{origin}/", "Origin": origin})
    tkn = rep.headers.get("x-vercel-challenge-token")
    if tkn is None:
        return
    sol = _solve_challenge(tkn)
    await http.request(
        "POST",
        f"{origin}/.well-known/vercel/security/request-challenge",
        headers={
            "x-vercel-challenge-solution": sol,
            "x-vercel-challenge-token": tkn,
            "x-vercel-challenge-version": "2",
            "Referer": f"{origin}/",
            "Origin": origin,
        },
    )
