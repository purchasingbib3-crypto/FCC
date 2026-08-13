from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from .config import get_settings

settings = get_settings()
_client: httpx.AsyncClient | None = None
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _client
    _client = httpx.AsyncClient(timeout=httpx.Timeout(float(settings.proxy_timeout_seconds)), follow_redirects=False)
    yield
    await _client.aclose()
    _client = None


app = FastAPI(title="FCC Local Reverse Proxy", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request) -> Response:
    assert _client is not None
    upstream = settings.upstream_url.rstrip("/") + "/" + path
    if request.url.query:
        upstream += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    headers["x-forwarded-proto"] = request.url.scheme
    headers["x-forwarded-host"] = request.headers.get("host", "")
    body = await request.body()
    result = await _client.request(request.method, upstream, headers=headers, content=body)
    response_headers = {
        k: v for k, v in result.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in {"content-encoding"}
    }
    return Response(content=result.content, status_code=result.status_code, headers=response_headers)
