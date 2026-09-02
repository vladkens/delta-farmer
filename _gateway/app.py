import json
import os
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

ASTRUM_API = "https://solver.astrum.foundation/api"
OMNI_HOST = "omni.variational.io"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


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
async def create_task(payload: dict[str, Any]) -> Response:
    task = get_task(payload)
    validate_create_task(task)
    return await proxy("createTask", task)


@app.post("/api/getTaskResult")
async def get_task_result(payload: dict[str, Any]) -> Response:
    return await proxy("getTaskResult", get_task(payload))
