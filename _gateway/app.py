import hmac
import json
import os
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from html import escape
from ipaddress import AddressValueError, IPv4Address
from typing import Any
from urllib.parse import urlparse

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

ASTRUM_API = "https://solver.astrum.foundation/api"
OMNI_HOST = "omni.variational.io"
TASK_LIMIT = 3
TASK_TTL_SECONDS = 300
PENALTY_BASE_SECONDS = 15 * 60
PENALTY_FACTOR = 2
PENALTY_MAX_LEVEL = 4
PENALTY_MAX_SECONDS = 4 * 60 * 60
PENALTY_RESET_SECONDS = 12 * 60 * 60
PENALTY_RETRY_SECONDS = 24 * 60 * 60
STATS_DAYS = 7
STAT_NAMES = ("active", "created", "blocked", "penalized", "completed", "failed")
STATUS_NAMES = ("active", "created", "penalized", "clients")
STATUS_IPS = {
    str(IPv4Address(ip.strip())) for ip in os.environ["STATUS_IPS"].split(",") if ip.strip()
}

ACQUIRE_TASK = """
local now = tonumber(redis.call("TIME")[1])
redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", now)
if redis.call("ZCARD", KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call("ZADD", KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
redis.call("EXPIRE", KEYS[1], ARGV[2])
return 1
"""

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
redis_client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)


def add_legacy_token(content: bytes) -> bytes:
    try:
        result = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return content

    if not isinstance(result, dict):
        return content

    solution = result.get("solution")
    if not isinstance(solution, dict) or solution.get("token"):
        return content

    cookies = solution.get("cookies")
    if not isinstance(cookies, dict) or not (token := cookies.get("cf_clearance")):
        return content

    solution["token"] = token
    return json.dumps(result, separators=(",", ":")).encode()


def get_task(payload: dict[str, Any]) -> dict[str, Any]:
    task = payload.get("task")
    if not isinstance(task, dict):
        raise HTTPException(status_code=422, detail="task must be an object")
    return task


def validate_create_task(task: dict[str, Any]) -> None:
    if task.get("type") != "cf_clearance":
        raise HTTPException(status_code=422, detail="Only cf_clearance tasks are allowed")

    website_url = task.get("websiteURL")
    if not isinstance(website_url, str):
        raise HTTPException(status_code=422, detail="websiteURL must be a string")

    url = urlparse(website_url)
    if url.scheme != "https" or url.hostname != OMNI_HOST:
        raise HTTPException(status_code=422, detail="websiteURL must target omni.variational.io")


def get_client_ip(request: Request) -> str:
    try:
        return str(IPv4Address(request.headers.get("fly-client-ip", "")))
    except AddressValueError as e:
        raise HTTPException(status_code=400, detail="Invalid Fly-Client-IP") from e


def validate_status_ip(request: Request) -> None:
    if get_client_ip(request) not in STATUS_IPS:
        raise HTTPException(status_code=403)


def parse_response(response: Response) -> dict[str, Any]:
    try:
        result = json.loads(response.body)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def get_client_id(task: dict[str, Any]) -> str:
    identity = json.dumps(
        [task.get("proxyURL"), task.get("userAgent")], separators=(",", ":")
    ).encode()
    return hmac.digest(os.environ["ASTRUM_CAPTCHA_KEY"].encode(), identity, "sha256").hex()


async def add_stat(name: str, ip: str) -> None:
    key = f"astrum:stats:{datetime.now(UTC):%Y-%m-%d}"
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.hincrby(key, f"{name}:{ip}", 1)
        pipe.expire(key, (STATS_DAYS + 1) * 86400)
        await pipe.execute()


async def add_client_stat(ip: str, client_id: str) -> None:
    key = f"astrum:clients:{datetime.now(UTC):%Y-%m-%d}:{ip}"
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.zincrby(key, 1, client_id)
        pipe.expire(key, (STATS_DAYS + 1) * 86400)
        await pipe.execute()


async def proxy(path: str, task: dict[str, Any]) -> Response:
    payload = {"clientKey": os.environ["ASTRUM_CAPTCHA_KEY"], "task": task}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        upstream = await client.post(f"{ASTRUM_API}/{path}", json=payload)

    content = add_legacy_token(upstream.content) if path == "getTaskResult" else upstream.content
    return Response(
        content=content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


@app.post("/api/createTask")
async def create_task(payload: dict[str, Any], request: Request) -> Response:
    task = get_task(payload)
    validate_create_task(task)
    ip = get_client_ip(request)
    client_id = get_client_id(task)
    await add_client_stat(ip, client_id)
    key = f"astrum:active:{ip}"
    reservation = uuid.uuid4().hex
    penalty_key = f"astrum:penalty:{ip}:{client_id}"
    lock = redis_client.lock(f"{penalty_key}:lock", timeout=TASK_TTL_SECONDS)
    if not await lock.acquire(blocking=False):
        await add_stat("penalized", ip)
        raise HTTPException(status_code=429, detail="CAPTCHA client is already creating a task")

    try:
        now = int(time.time())
        state = await redis_client.hgetall(penalty_key)
        next_allowed = int(state.get("next_allowed", 0))
        if now < next_allowed:
            next_allowed = now + PENALTY_RETRY_SECONDS
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.hset(penalty_key, "next_allowed", next_allowed)
                pipe.expire(penalty_key, PENALTY_RETRY_SECONDS)
                await pipe.execute()
            await add_stat("penalized", ip)
            raise HTTPException(
                status_code=429,
                detail="CAPTCHA client is temporarily limited",
                headers={"Retry-After": str(PENALTY_RETRY_SECONDS)},
            )

        if not await redis_client.eval(
            ACQUIRE_TASK, 1, key, TASK_LIMIT, TASK_TTL_SECONDS, reservation
        ):
            await add_stat("blocked", ip)
            raise HTTPException(status_code=429, detail="Too many active CAPTCHA tasks")

        last_created = int(state.get("last_created", 0))
        level = int(state.get("level", -1)) + 1 if now - last_created < PENALTY_RESET_SECONDS else 0
        level = min(level, PENALTY_MAX_LEVEL)
        delay = min(PENALTY_BASE_SECONDS * PENALTY_FACTOR**level, PENALTY_MAX_SECONDS)
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.hset(
                penalty_key,
                mapping={"level": level, "last_created": now, "next_allowed": now + delay},
            )
            pipe.expire(penalty_key, PENALTY_RESET_SECONDS)
            await pipe.execute()

        response = await proxy("createTask", task)
        result = parse_response(response)
        task_id = result.get("taskId")
        if task_id:
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.zrem(key, reservation)
                pipe.zadd(key, {str(task_id): time.time() + TASK_TTL_SECONDS})
                pipe.expire(key, TASK_TTL_SECONDS)
                await pipe.execute()
            await add_stat("created", ip)
        elif result.get("errorId") or 400 <= response.status_code < 500:
            await redis_client.zrem(key, reservation)
            await add_stat("failed", ip)
        elif response.status_code >= 500:
            await add_stat("failed", ip)

        return response
    finally:
        await lock.release()


@app.post("/api/getTaskResult")
async def get_task_result(payload: dict[str, Any], request: Request) -> Response:
    task = get_task(payload)
    response = await proxy("getTaskResult", task)
    result = parse_response(response)
    task_id = task.get("taskId")
    is_done = result.get("status") == "closed" or result.get("errorId")
    if task_id and is_done:
        ip = get_client_ip(request)
        removed = await redis_client.zrem(f"astrum:active:{ip}", str(task_id))
        if removed:
            name = "completed" if result.get("status") == "closed" else "failed"
            await add_stat(name, ip)

    return response


@app.get("/status", response_class=HTMLResponse)
async def status(request: Request) -> str:
    validate_status_ip(request)

    rows: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    async for key in redis_client.scan_iter("astrum:active:*"):
        ip = key.removeprefix("astrum:active:")
        active = await redis_client.zcount(key, time.time(), "+inf")
        if active:
            rows[ip]["active"] = active

    today = datetime.now(UTC).date()
    for offset in range(STATS_DAYS):
        day = today - timedelta(days=offset)
        for field, count in (await redis_client.hgetall(f"astrum:stats:{day}")).items():
            name, ip = field.split(":", 1)
            rows[ip][name] += int(count)
        async for key in redis_client.scan_iter(f"astrum:clients:{day}:*"):
            ip = key.rsplit(":", 1)[1]
            for client_id, count in await redis_client.zrange(key, 0, -1, withscores=True):
                rows[ip][f"client:{client_id}"] += int(count)

    for row in rows.values():
        row["clients"] = sum(name.startswith("client:") for name in row)

    totals = {name: sum(row[name] for row in rows.values()) for name in STATUS_NAMES}
    summary = " · ".join(f"<b>{value:,}</b> {name}" for name, value in totals.items())
    body = "".join(
        f"<tr><td>{escape(ip)}</td>"
        + "".join(f"<td>{row[name]:,}</td>" for name in STATUS_NAMES)
        + f'<td><form method="post" action="/status/reset/{escape(ip)}" '
        + f"onsubmit=\"return confirm('Clear data for {escape(ip)}?')\">"
        + '<button type="submit">Clear</button></form></td>'
        + "</tr>"
        for ip, row in sorted(rows.items(), key=lambda item: IPv4Address(item[0]))
    )
    headers = "".join(f"<th>{name}</th>" for name in STATUS_NAMES) + "<th></th>"
    body = body or '<tr><td colspan="6">No activity</td></tr>'
    updated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<meta name="viewport" content="width=device-width"><title>Gateway status</title>
<style>
body {{ font: 14px system-ui; margin: 40px auto; max-width: 900px; color: #222 }}
h1 {{ font-size: 22px }} p {{ color: #666 }} table {{ border-collapse: collapse; width: 100% }}
th, td {{ padding: 9px 12px; border-bottom: 1px solid #ddd; text-align: right }}
th:first-child, td:first-child {{ text-align: left }}
form {{ margin: 0 }}
button {{ background: #b42318; border: 0; border-radius: 6px; color: white; padding: 8px 12px }}
</style></head><body><h1>Astrum gateway</h1>
<p>{summary}</p>
<table><thead><tr><th>IP</th>{headers}</tr></thead><tbody>{body}</tbody></table>
<p>Last {STATS_DAYS} days · refreshes every 10 seconds · {updated}</p>
</body></html>"""


@app.post("/status/reset/{ip}")
async def reset_status(ip: str, request: Request) -> RedirectResponse:
    validate_status_ip(request)
    try:
        ip = str(IPv4Address(ip))
    except AddressValueError as e:
        raise HTTPException(status_code=400, detail="Invalid IP address") from e

    keys = [f"astrum:active:{ip}"]
    keys.extend(
        [
            key
            async for key in redis_client.scan_iter(f"astrum:penalty:{ip}:*")
            if not key.endswith(":lock")
        ]
    )
    keys.extend([key async for key in redis_client.scan_iter(f"astrum:clients:*:{ip}")])
    if keys:
        await redis_client.delete(*keys)

    fields = [f"{name}:{ip}" for name in STAT_NAMES]
    async for key in redis_client.scan_iter("astrum:stats:*"):
        await redis_client.hdel(key, *fields)

    return RedirectResponse("/status", status_code=303)
