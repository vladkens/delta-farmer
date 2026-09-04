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
from fastapi.responses import HTMLResponse, Response

ASTRUM_API = "https://solver.astrum.foundation/api"
OMNI_HOST = "omni.variational.io"
TASK_LIMIT = 3
TASK_TTL_SECONDS = 300
STATS_DAYS = 7
STAT_NAMES = ("active", "created", "blocked", "completed", "failed")
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


def parse_response(response: Response) -> dict[str, Any]:
    try:
        result = json.loads(response.body)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


async def add_stat(name: str, ip: str) -> None:
    key = f"astrum:stats:{datetime.now(UTC):%Y-%m-%d}"
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.hincrby(key, f"{name}:{ip}", 1)
        pipe.expire(key, (STATS_DAYS + 1) * 86400)
        await pipe.execute()


async def add_client_stat(ip: str, task: dict[str, Any]) -> None:
    identity = json.dumps(
        [task.get("proxyURL"), task.get("userAgent")], separators=(",", ":")
    ).encode()
    client_id = hmac.digest(os.environ["ASTRUM_CAPTCHA_KEY"].encode(), identity, "sha256").hex()
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
    await add_client_stat(ip, task)
    key = f"astrum:active:{ip}"
    reservation = uuid.uuid4().hex
    if not await redis_client.eval(ACQUIRE_TASK, 1, key, TASK_LIMIT, TASK_TTL_SECONDS, reservation):
        await add_stat("blocked", ip)
        raise HTTPException(status_code=429, detail="Too many active CAPTCHA tasks")

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
    if get_client_ip(request) not in STATUS_IPS:
        raise HTTPException(status_code=403)

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

    totals = {name: sum(row[name] for row in rows.values()) for name in STAT_NAMES}
    cards = "".join(
        f"<div><b>{value:,}</b><span>{name}</span></div>" for name, value in totals.items()
    )
    body = "".join(
        f"<tr><td>{escape(ip)}</td>"
        + "".join(f"<td>{row[name]:,}</td>" for name in (*STAT_NAMES, "clients"))
        + "</tr>"
        for ip, row in sorted(rows.items(), key=lambda item: IPv4Address(item[0]))
    )
    headers = "".join(f"<th>{name}</th>" for name in (*STAT_NAMES, "clients"))
    body = body or '<tr><td colspan="7">No activity</td></tr>'
    updated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<meta name="viewport" content="width=device-width"><title>Gateway status</title>
<style>
body {{ font: 14px system-ui; margin: 40px auto; max-width: 900px; color: #222 }}
h1 {{ font-size: 22px }}
.cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 24px 0 }}
.cards div {{ background: #f3f4f6; border-radius: 8px; padding: 14px 20px; min-width: 110px }}
.cards b, .cards span {{ display: block }} .cards b {{ font-size: 24px }}
.cards span, p {{ color: #666 }} table {{ border-collapse: collapse; width: 100% }}
th, td {{ padding: 9px 12px; border-bottom: 1px solid #ddd; text-align: right }}
th:first-child, td:first-child {{ text-align: left }}
</style></head><body><h1>Astrum gateway</h1>
<div class="cards">{cards}</div>
<table><thead><tr><th>IP</th>{headers}</tr></thead><tbody>{body}</tbody></table>
<p>Last {STATS_DAYS} days · refreshes every 10 seconds · {updated}</p>
</body></html>"""
