from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache

from ..config import get_settings
from .xlsx_import import parse_ss6_export

settings = get_settings()


@dataclass
class TempPayload:
    created_at: float
    rows: list[dict[str, Any]]
    meta: dict[str, Any]


class SS6TemporaryStore:
    def __init__(self) -> None:
        self._cache: TTLCache[str, TempPayload] = TTLCache(maxsize=64, ttl=settings.ss6_temp_ttl_seconds)
        self._lock = asyncio.Lock()

    async def put(self, rows: list[dict[str, Any]], meta: dict[str, Any]) -> str:
        async with self._lock:
            token = secrets.token_urlsafe(24)
            self._cache[token] = TempPayload(time.time(), rows, meta)
            return token

    async def get(self, token: str) -> TempPayload | None:
        async with self._lock:
            return self._cache.get(token)

    async def delete(self, token: str) -> None:
        async with self._lock:
            self._cache.pop(token, None)


store = SS6TemporaryStore()


async def fetch_export(date_from: str, date_to: str, shift: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # If ss6_username is set, use it as the actual username (e.g. "24006305").
    # Otherwise fallback to ss6_default_pwd companion credential "24006305".
    # The fields ss6_username_field / ss6_password_field are HTML form names (e.g. "p_nrp", "p_password").
    username = settings.ss6_username or "24006305"
    if username == settings.ss6_username_field:
        # Field name accidentally set as username; use default instead
        username = "24006305"
    password = settings.ss6_password or settings.ss6_default_pwd or "24006305"
    if password == settings.ss6_password_field:
        # Same — use default
        password = settings.ss6_default_pwd or "24006305"
    if not username or not password:
        raise RuntimeError("Credential SS6 belum dikonfigurasi di environment VPS")

    timeout = httpx.Timeout(settings.ss6_timeout_seconds)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        verify=settings.ss6_verify_tls,
        headers={"User-Agent": "FCC-PPA-BIB/2026.08"},
    ) as client:
        login_page = await client.get(settings.ss6_login_url)
        login_page.raise_for_status()
        soup = BeautifulSoup(login_page.text, "html.parser")
        form = soup.find("form")
        data: dict[str, str] = {}
        action = settings.ss6_login_url
        if form:
            action = urljoin(str(login_page.url), form.get("action") or settings.ss6_login_url)
            for inp in form.find_all("input"):
                name = inp.get("name")
                if name and inp.get("type") in {"hidden", None}:
                    data[name] = inp.get("value") or ""
        data[settings.ss6_username_field] = username
        data[settings.ss6_password_field] = password
        response = await client.post(action, data=data)
        response.raise_for_status()
        if "/auth" in str(response.url).rstrip("/"):
            # Some deployments return HTTP 200 with login error.
            text = response.text.lower()
            if "password" in text and ("salah" in text or "invalid" in text or "login" in text):
                raise RuntimeError("Login SS6 gagal. Periksa NRP/password dan field login environment.")

        export_url = settings.ss6_export_url_template.format(
            date_from=date_from,
            date_to=date_to,
            shift=shift,
        )
        exported = await client.get(export_url)
        exported.raise_for_status()
        content_type = exported.headers.get("content-type", "")
        if "text/html" in content_type and "/auth" in str(exported.url):
            raise RuntimeError("Session SS6 tidak terbentuk; export kembali ke halaman login")
        filename = "ss6_export.xls"
        disposition = exported.headers.get("content-disposition", "")
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip().strip('"')
        rows = parse_ss6_export(exported.content, filename)
        meta = {
            "date_from": date_from,
            "date_to": date_to,
            "shift": shift,
            "source_url": export_url,
            "filename": filename,
            "content_type": content_type,
            "row_count": len(rows),
            "total_l": round(sum(float(r["volume_l"]) for r in rows), 3),
            "unique_units": len({r["unit_normalized"] for r in rows}),
            "gas_stations": sorted({r["gas_station"] for r in rows if r["gas_station"]}),
            "fuelmen": sorted({r["fuelman"] for r in rows if r["fuelman"]}),
        }
        return rows, meta
