from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import close_pool, open_pool
from .routers import (
    auth, closing, dashboard, discrepancy, evidence, fm_awal_public, fuel_bridge,
    health, imports, master, master_public, master_write_public, sounding_public,
    ss6, voucher, reporting,
)

try:
    from .schema_contract import validate_schema_contract
except ImportError:  # pragma: no cover - compatibility only
    validate_schema_contract = None

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("fcc.main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.evidence_dir_resolved.mkdir(parents=True, exist_ok=True)
    open_pool()
    if validate_schema_contract is not None:
        validate_schema_contract()
        log.info("schema_contract: OK")
    yield
    close_pool()


app = FastAPI(title="Fuel Control Center PPA-BIB", version="2026.08.14-reporting-v12.3.1-fast-frontend", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Requested-With"],
)

# API routers must be registered before StaticFiles catch-all.
for router in (
    health.router, auth.router, dashboard.router, discrepancy.router, closing.router,
    imports.router, ss6.router, voucher.router, reporting.router, master_public.router,
    master_write_public.router, master.router, evidence.router, fuel_bridge.router,
    sounding_public.router, fm_awal_public.router,
):
    app.include_router(router)


bundle_root = Path(__file__).resolve().parent.parent
candidates = [
    bundle_root / "03_frontend",              # canonical bundle frontend
    bundle_root / "frontend",                 # compatibility bundle layout
]
static_root = next((p for p in candidates if p.is_dir() and (p / "index.html").is_file()), None)
_FAST_IMPORT_PATCH_TAG = '<script src="/v12_3_1_fast_import_patch.js?v=20260814"></script>'


def _patched_frontend_html() -> str:
    if static_root is None:
        return "Frontend index.html tidak ditemukan."
    html = (static_root / "index.html").read_text(encoding="utf-8")
    if _FAST_IMPORT_PATCH_TAG in html:
        return html
    marker = "</body>"
    if marker in html:
        return html.replace(marker, f"{_FAST_IMPORT_PATCH_TAG}\n{marker}", 1)
    return html + _FAST_IMPORT_PATCH_TAG


if static_root:
    log.info("Serving canonical FCC frontend from: %s", static_root)

    # Explicit index routes inject the V12.3.1 runtime patch without duplicating
    # the giant canonical index.html. Static assets remain served from one source.
    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    @app.get("/field", include_in_schema=False)
    @app.get("/field/", include_in_schema=False)
    @app.get("/field/index.html", include_in_schema=False)
    def frontend_index() -> HTMLResponse:
        return HTMLResponse(
            _patched_frontend_html(),
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    # Bookmarks can keep using /field; root serves the same single source of truth.
    app.mount("/field", StaticFiles(directory=static_root, html=True), name="field")
    app.mount("/", StaticFiles(directory=static_root, html=True), name="frontend")
else:
    # Do not make the API unavailable merely because an external web server serves the UI.
    log.warning("Frontend index.html not found in candidates: %s", [str(p) for p in candidates])
