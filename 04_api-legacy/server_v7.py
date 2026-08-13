#!/usr/bin/env python3
"""
Fuel Control Center API v7 — Comprehensive improvements:
- POST /auth/login (no password in URL)
- Full OpenAPI descriptions, examples, tags
- Security scheme declared
- Response examples
- Better error format
- Cache headers
- Field selection
- Rate limiting baseline
"""
import os
import hashlib
from contextlib import contextmanager
from typing import Optional, List
from datetime import datetime, timedelta
import psycopg2
import psycopg2.pool
from fastapi import FastAPI, Depends, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
import secrets

# ── Config ──
API_KEY = os.environ.get("FUEL_API_KEY", "fcc-ppa-bib-2026-juni-secret-key-7f3a9b")
_pool = None

# ── Database pool ──
def get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn=2, maxconn=10,
            host="/var/run/postgresql", port=5432,
            dbname="fuel_control_center", user="postgres"
        )
    return _pool

@contextmanager
def get_db():
    pool = get_pool()
    conn = pool.getconn()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)

# ── Simple rate limiter (in-memory) ──
_rate_cache = {}
RATE_LIMIT = 100  # requests per minute per IP

def check_rate_limit(ip: str):
    now = datetime.utcnow()
    window = now - timedelta(minutes=1)
    calls = _rate_cache.get(ip, [])
    calls = [t for t in calls if t > window]
    if len(calls) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail={
            "code": "RATE_LIMIT_EXCEEDED",
            "message": f"Terlalu banyak request. Maks {RATE_LIMIT}/menit per IP.",
            "retry_after_seconds": 60,
        })
    calls.append(now)
    _rate_cache[ip] = calls

# ── Auth: POST login + API key ──
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(request: Request, key: Optional[str] = Depends(api_key_header)):
    check_rate_limit(request.client.host if request.client else "unknown")
    if key == API_KEY:
        return True
    raise HTTPException(status_code=401, detail={
        "code": "UNAUTHORIZED",
        "message": "API key tidak valid. Sertakan X-API-Key header.",
    })

# ── Pydantic schemas ──
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, example="ADMIN001")
    password: str = Field(..., min_length=1, max_length=128, example="admin123")

class LoginResponse(BaseModel):
    id: int = Field(..., example=5)
    username: str = Field(..., example="ADMIN001")
    nama: str = Field(..., example="Admin Fuel")
    role: str = Field(..., example="SUPER_ADMIN")
    vendor: Optional[str] = Field(None, example="PPA")
    active: bool = Field(..., example=True)
    session_token: Optional[str] = Field(None, description="Optional session token for subsequent calls")

class ErrorResponse(BaseModel):
    code: str = Field(..., example="VALIDATION_ERROR")
    message: str = Field(..., example="Request tidak valid")
    request_id: Optional[str] = None
    timestamp: str = Field(..., example="2026-08-03T04:00:00Z")

class PaginationMeta(BaseModel):
    page: int
    per_page: int
    total: int
    total_pages: int
    has_next: bool
    has_prev: bool

class PaginatedResponse(BaseModel):
    data: List[dict]
    pagination: PaginationMeta

# ── Cache helper ──
def set_cache_headers(response: Response, max_age: int = 30, private: bool = True):
    """Add cache-control headers for safe read endpoints."""
    visibility = "private" if private else "public"
    response.headers["Cache-Control"] = f"{visibility}, max-age={max_age}"
    response.headers["Pragma"] = "cache"

def set_etag(response: Response, etag: str):
    response.headers["ETag"] = f'"{etag}"'
    response.headers["Last-Modified"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")

def paginate(page: int, per_page: int):
    return (page - 1) * per_page, per_page

# ── App ──
app = FastAPI(
    title="FCC PPA-BIB API",
    description=(
        "REST API untuk **Fuel Control Center PPA-BIB** — Sistem kontrol fuel SPBU dengan PostgreSQL FCC schema.\n\n"
        "## Fitur\n"
        "- **Master Data**: vendor, unit, alias, jalur, main tank, fuel truck, FT Mandar Ocean, sounding table\n"
        "- **Autentikasi**: POST /auth/login (basic auth, password SHA-256)\n"
        "- **Pagination**: semua list endpoint dukung `page` + `per_page`\n"
        "- **Field Selection**: parameter `?fields=` untuk pilih kolom\n"
        "- **Rate Limiting**: 100 request/menit per IP\n\n"
        "## Login\n"
        "Kirim POST /api/v1/auth/login dengan body JSON `{\"username\": \"ADMIN001\", \"password\": \"admin123\"}`. "
        "Default users: ADMIN001/admin123, RECEIVE01/demo123, 81230150/demo123, vendor.mnk/mnk123.\n\n"
        "## API Key\n"
        "Sertakan header `X-API-Key: fcc-pp...a9b` di semua endpoint kecuali /auth/login dan /health."
    ),
    version="7.0",
    contact={"name": "FCC PPA-BIB Team"},
    license_info={"name": "Internal"},
    openapi_tags=[
        {"name": "auth", "description": "Login dan session"},
        {"name": "master", "description": "Master data — vendor, unit, alias, jalur"},
        {"name": "assets", "description": "Main tank, fuel truck, FT Mandar Ocean"},
        {"name": "sounding", "description": "Tabel konversi sounding dip → volume"},
        {"name": "config", "description": "Konfigurasi aplikasi dan dashboard"},
        {"name": "dashboard", "description": "Ringkasan untuk dashboard"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["ETag", "Last-Modified", "X-Total-Count", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
)

# ── OpenAPI security scheme ──
api_key_scheme = {
    "type": "apiKey",
    "in": "header",
    "name": "X-API-Key",
    "description": "API key required for all endpoints except /auth/login and /health"
}
app.openapi_schema = None  # Force regeneration

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title=app.title, version=app.version,
        description=app.description, routes=app.routes, tags=app.openapi_tags
    )
    schema["components"]["securitySchemes"] = {"ApiKeyAuth": api_key_scheme}
    schema["security"] = [{"ApiKeyAuth": []}]
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi

# ── Health (no auth, no rate limit) ──
@app.get("/api/v1/health", tags=["config"],
         summary="Health check",
         description="Cek koneksi database dan stats tabel. Tidak butuh auth.")
async def health():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            counts = {}
            for t in ['master_unit','master_vendor','unit_alias','master_jalur',
                     'master_main_tank','master_fuel_truck','ft_mandar_ocean',
                     'sounding_table','app_user','app_config']:
                cur.execute(f"SELECT count(*) FROM fcc.{t}")
                counts[t] = cur.fetchone()[0]
            return {
                "status": "ok",
                "version": "7.0",
                "database": "postgresql_v6",
                "schema": "fcc",
                "tables": counts,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
    except Exception as e:
        return {"status": "error", "detail": str(e), "timestamp": datetime.utcnow().isoformat() + "Z"}

# ── Auth: POST login ──
@app.post("/api/v1/auth/login", tags=["auth"],
          summary="Login dengan username + password",
          description="Autentikasi user. Body: JSON atau form-urlencoded. Response: user data + optional session token.",
          responses={
              200: {"description": "Login berhasil"},
              401: {"description": "Username/password salah"},
              422: {"description": "Username/password wajib diisi"},
          })
async def login(request: Request, response: Response):
    """Login with JSON or form-urlencoded. Accepts both for compatibility."""
    check_rate_limit(request.client.host if request.client else "unknown")
    # Try JSON first, fallback to form-urlencoded
    username, password = None, None
    content_type = (request.headers.get("Content-Type") or "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json()
            username = body.get("username", "").strip()
            password = body.get("password", "")
        except Exception:
            raise HTTPException(status_code=422, detail={"code": "INVALID_JSON", "message": "Request body harus JSON valid"})
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
    else:
        # Try JSON anyway
        try:
            body = await request.json()
            username = body.get("username", "").strip()
            password = body.get("password", "")
        except Exception:
            raise HTTPException(status_code=422, detail={"code": "UNSUPPORTED_CONTENT_TYPE", "message": "Gunakan JSON atau form-urlencoded"})
    if not username or not password:
        raise HTTPException(status_code=422, detail={"code": "VALIDATION_ERROR", "message": "Username dan password wajib diisi"})
    """Authenticate user with username + password (SHA-256 hashed server-side)."""
    check_rate_limit(request.client.host if request.client else "unknown")
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT id, username, nama, role, vendor_kode, status
            FROM fcc.app_user WHERE username = %s AND password_hash = %s""",
            (username, pwd_hash))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail={
                "code": "INVALID_CREDENTIALS",
                "message": "Username atau password salah",
            })
        cur.execute("UPDATE fcc.app_user SET last_login = now() WHERE id = %s", (row[0],))
        conn.commit()
        # Set rate limit headers
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT)
        # Simple session token (in production use JWT)
        session_token = secrets.token_urlsafe(32)
        return {
            "id": row[0], "username": row[1], "nama": row[2],
            "role": row[3], "vendor": row[4], "active": row[5] == "ACTIVE",
            "session_token": session_token,
        }

# ── Helper: parse ?fields= ──
def select_fields(data: list, fields: Optional[str]):
    if not fields: return data
    keys = [f.strip() for f in fields.split(",") if f.strip()]
    if not keys: return data
    out = []
    for r in data:
        out.append({k: r[k] for k in keys if k in r})
    return out

# ── Master data endpoints ──
@app.get("/api/v1/users", tags=["master"],
         summary="Daftar user aplikasi",
         description="Returns paginated list of app users. Filter by role, search by name/nrp.",
         dependencies=[Depends(verify_api_key)],
         response_model=PaginatedResponse,
         responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}})
async def users(
    response: Response,
    page: int = Query(1, ge=1, description="Nomor halaman"),
    per_page: int = Query(50, ge=1, le=200, description="Records per halaman (max 200)"),
    search: Optional[str] = Query(None, description="Cari di username/nama"),
    role: Optional[str] = Query(None, description="Filter role (SUPER_ADMIN, ADMIN, dll)"),
    fields: Optional[str] = Query(None, description="Field selection (comma-separated)"),
):
    """List users dengan pagination, search, dan field selection."""
    with get_db() as conn:
        cur = conn.cursor()
        where = ["1=1"]; params = []
        if search:
            where.append("(username ILIKE %s OR nama ILIKE %s)")
            params += [f"%{search}%", f"%{search}%"]
        if role:
            where.append("role = %s"); params.append(role.upper())
        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM fcc.app_user WHERE {w}", params)
        total = cur.fetchone()[0]
        offset, limit = paginate(page, per_page)
        cur.execute(f"""SELECT id, username, nama, role, vendor_kode, status, last_login
            FROM fcc.app_user WHERE {w} ORDER BY nama LIMIT %s OFFSET %s""", params + [limit, offset])
        rows = cur.fetchall()
        data = select_fields([{
            "id":r[0],"username":r[1],"nama":r[2],"role":r[3],
            "vendor":r[4],"active":r[5]=="ACTIVE",
            "last_login":r[6].isoformat() if r[6] else None
        } for r in rows], fields)
        set_cache_headers(response, max_age=15, private=True)
        response.headers["X-Total-Count"] = str(total)
        return {
            "data": data,
            "pagination": {
                "page": page, "per_page": per_page, "total": total,
                "total_pages": (total + per_page - 1) // per_page,
                "has_next": offset + per_page < total, "has_prev": page > 1
            }
        }

@app.get("/api/v1/vendors", tags=["master"],
         summary="Daftar vendor",
         description="Master vendor PPA-BIB. Returns all active vendors. Supports search and field selection.",
         dependencies=[Depends(verify_api_key)])
async def vendors(
    response: Response,
    search: Optional[str] = Query(None),
    kategori: Optional[str] = Query(None, description="INTERNAL/RENTAL/SUPPLIER BBM"),
    fields: Optional[str] = Query(None),
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["status = 'ACTIVE'"]; params = []
        if search:
            where.append("(kode ILIKE %s OR nama ILIKE %s)")
            params += [f"%{search}%", f"%{search}%"]
        if kategori:
            where.append("kategori = %s"); params.append(kategori)
        w = " AND ".join(where)
        cur.execute(f"SELECT kode, nama, kategori, status FROM fcc.master_vendor WHERE {w} ORDER BY kode", params)
        rows = cur.fetchall()
        data = select_fields([{"kode":r[0],"nama":r[1],"kategori":r[2],"active":r[3]=="ACTIVE"} for r in rows], fields)
        set_cache_headers(response, max_age=60, private=True)
        return {"data": data, "total": len(data)}

@app.get("/api/v1/units", tags=["master"],
         summary="Master unit",
         description="Unit operasional PPA-BIB dengan alias SS6 dan SAP. Paginated.",
         dependencies=[Depends(verify_api_key)],
         response_model=PaginatedResponse)
async def units(
    response: Response,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, description="Cari di kode/nama unit"),
    vendor: Optional[str] = Query(None, description="Filter vendor (PPA, MNK, dll)"),
    kategori: Optional[str] = Query(None, description="A2B/A2S/SARANA/NON A2B"),
    fields: Optional[str] = Query(None),
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["status = 'ACTIVE'"]; params = []
        if search:
            where.append("(kode ILIKE %s OR nama ILIKE %s)")
            params += [f"%{search}%", f"%{search}%"]
        if vendor:
            where.append("vendor_kode = %s"); params.append(vendor)
        if kategori:
            where.append("kategori = %s"); params.append(kategori)
        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM fcc.master_unit WHERE {w}", params)
        total = cur.fetchone()[0]
        offset, limit = paginate(page, per_page)
        cur.execute(f"""SELECT kode, nama, vendor_kode, kategori, status
            FROM fcc.master_unit WHERE {w} ORDER BY kode LIMIT %s OFFSET %s""",
            params + [limit, offset])
        rows = cur.fetchall()
        data = select_fields([{"kode":r[0],"nama":r[1],"vendor":r[2],"kategori":r[3],"active":r[4]=="ACTIVE"} for r in rows], fields)
        set_cache_headers(response, max_age=30, private=True)
        return {
            "data": data,
            "pagination": {"page":page,"per_page":per_page,"total":total,
                          "total_pages":(total+per_page-1)//per_page,
                          "has_next":offset+per_page<total,"has_prev":page>1}
        }

@app.get("/api/v1/aliases", tags=["master"],
         summary="Unit aliases (SS6/SAP mapping)",
         description="Mapping SS6 dan SAP ke unit standar. 1.891 aliases.",
         dependencies=[Depends(verify_api_key)],
         response_model=PaginatedResponse)
async def aliases(
    response: Response,
    page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    vendor: Optional[str] = Query(None),
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["status = 'ACTIVE'"]; params = []
        if search:
            where.append("(alias_ss6 ILIKE %s OR alias_sap ILIKE %s OR unit_standar ILIKE %s)")
            params += [f"%{search}%", f"%{search}%", f"%{search}%"]
        if vendor:
            where.append("vendor_kode = %s"); params.append(vendor)
        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM fcc.unit_alias WHERE {w}", params)
        total = cur.fetchone()[0]
        offset, limit = paginate(page, per_page)
        cur.execute(f"""SELECT id, unit_standar, alias_ss6, alias_sap, vendor_kode, kategori, status
            FROM fcc.unit_alias WHERE {w} ORDER BY unit_standar LIMIT %s OFFSET %s""",
            params + [limit, offset])
        rows = cur.fetchall()
        data = [{"id":r[0],"unit_standar":r[1],"alias_ss6":r[2],"alias_sap":r[3],
                "vendor":r[4],"kategori":r[5],"active":r[6]=="ACTIVE"} for r in rows]
        set_cache_headers(response, max_age=60, private=True)
        return {
            "data": data,
            "pagination": {"page":page,"per_page":per_page,"total":total,
                          "total_pages":(total+per_page-1)//per_page,
                          "has_next":offset+per_page<total,"has_prev":page>1}
        }

@app.get("/api/v1/jalur", tags=["master"], summary="Master jalur operasional",
         description="Jalur 1-3 (transfer) dan 5-7 (penerimaan) plus JLR- aliases.",
         dependencies=[Depends(verify_api_key)])
async def jalur(response: Response, search: Optional[str] = Query(None)):
    with get_db() as conn:
        cur = conn.cursor()
        if search:
            cur.execute("SELECT kode, nama, tujuan, peruntukan, site, status FROM fcc.master_jalur WHERE kode ILIKE %s OR nama ILIKE %s ORDER BY kode",
                        [f"%{search}%", f"%{search}%"])
        else:
            cur.execute("SELECT kode, nama, tujuan, peruntukan, site, status FROM fcc.master_jalur ORDER BY kode")
        rows = cur.fetchall()
        data = [{"kode":r[0],"nama":r[1],"tujuan":r[2],"peruntukan":r[3],"site":r[4],"active":r[5]=="ACTIVE"} for r in rows]
        set_cache_headers(response, max_age=300, private=True)
        return {"data": data, "total": len(data)}

@app.get("/api/v1/main-tanks", tags=["assets"], summary="Master Main Tank (FS10-FS15)",
         dependencies=[Depends(verify_api_key)])
async def main_tanks(response: Response):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT kode, nama, kapasitas_l, status FROM fcc.master_main_tank ORDER BY kode")
        rows = cur.fetchall()
        data = [{"kode":r[0],"nama":r[1],"kapasitas":float(r[2]),"active":r[3]=="ACTIVE"} for r in rows]
        set_cache_headers(response, max_age=300, private=True)
        return {"data": data}

@app.get("/api/v1/fuel-trucks", tags=["assets"], summary="Master Fuel Truck",
         dependencies=[Depends(verify_api_key)])
async def fuel_trucks(response: Response):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT kode, nama, tipe, kapasitas_l, status FROM fcc.master_fuel_truck ORDER BY kode")
        rows = cur.fetchall()
        data = [{"kode":r[0],"nama":r[1],"tipe":r[2],"kapasitas":float(r[3]) if r[3] else None,"active":r[4]=="ACTIVE"} for r in rows]
        set_cache_headers(response, max_age=300, private=True)
        return {"data": data}

@app.get("/api/v1/ft-mandar-ocean", tags=["assets"], summary="Master FT Mandar Ocean",
         description="Fuel Truck vendor Mandar Ocean. 89 entries dengan masa berlaku komisioning.",
         dependencies=[Depends(verify_api_key)])
async def ft_mandar_ocean(response: Response):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id_ft, no_lambung, no_polisi, kapasitas_l, masa_berlaku, status FROM fcc.ft_mandar_ocean ORDER BY id_ft")
        rows = cur.fetchall()
        data = [{"id_ft":r[0],"no_lambung":r[1],"no_polisi":r[2],
                 "kapasitas":float(r[3]) if r[3] else None,
                 "masa_berlaku":r[4].isoformat() if r[4] else None,
                 "status":r[5]} for r in rows]
        set_cache_headers(response, max_age=300, private=True)
        return {"data": data}

@app.get("/api/v1/sounding", tags=["sounding"],
         summary="Tabel konversi sounding → volume",
         description="84.168 titik kalibrasi untuk 31 aset. Query per aset atau seluruh data.",
         dependencies=[Depends(verify_api_key)],
         response_model=PaginatedResponse)
async def sounding(
    response: Response,
    asset: Optional[str] = Query(None, description="Filter per aset (FS10, FT-2609, dll)"),
    page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=500),
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["1=1"]; params = []
        if asset:
            where.append("aset = %s"); params.append(asset)
        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM fcc.sounding_table WHERE {w}", params)
        total = cur.fetchone()[0]
        offset, limit = paginate(page, per_page)
        cur.execute(f"""SELECT aset, dip_cm, volume_l, status
            FROM fcc.sounding_table WHERE {w} ORDER BY aset, dip_cm LIMIT %s OFFSET %s""",
            params + [limit, offset])
        rows = cur.fetchall()
        data = [{"aset":r[0],"dip_cm":float(r[1]),"volume_l":float(r[2]),"active":r[3]=="ACTIVE"} for r in rows]
        set_cache_headers(response, max_age=300, private=True)
        return {
            "data": data,
            "pagination": {"page":page,"per_page":per_page,"total":total,
                          "total_pages":(total+per_page-1)//per_page,
                          "has_next":offset+per_page<total,"has_prev":page>1}
        }

@app.get("/api/v1/sounding/volume", tags=["sounding"],
         summary="Lookup volume_l untuk (aset, dip_cm)",
         description="""Lookup cepat dari tabel sounding untuk form input
         transfer_fuel, pengurasan, sounding_main_tank. Mendukung input dip
         sembarang (tidak harus kelipatan 0.1 cm) via interpolasi linier.
         """,
         dependencies=[Depends(verify_api_key)])
async def sounding_volume(
    aset: str = Query(..., description="Kode aset (FS10-FS15 atau FT-xxxx)"),
    dip: float = Query(..., description="Tinggi sounding (cm), boleh desimal"),
    exact: bool = Query(False, description="True=eksak match, False=interpolasi linier"),
):
    import traceback
    try:
        with get_db() as conn:
            cur = conn.cursor()
            if exact:
                cur.execute("SELECT volume_l FROM fcc.sounding_table WHERE aset=%s AND dip_cm=%s AND status='ACTIVE' LIMIT 1", (aset, dip))
                row = cur.fetchone()
                vol = row[0] if row else None
                method = "exact"
            else:
                # Interpolasi langsung di SQL (lebih cepat & akurat)
                cur.execute("""
                    WITH lo AS (
                        SELECT dip_cm, volume_l FROM fcc.sounding_table
                         WHERE aset=%s AND dip_cm<=%s AND status='ACTIVE'
                         ORDER BY dip_cm DESC LIMIT 1
                    ),
                    hi AS (
                        SELECT dip_cm, volume_l FROM fcc.sounding_table
                         WHERE aset=%s AND dip_cm>=%s AND status='ACTIVE'
                         ORDER BY dip_cm ASC LIMIT 1
                    )
                    SELECT
                        lo.dip_cm, lo.volume_l,
                        hi.dip_cm, hi.volume_l,
                        CASE WHEN lo.dip_cm IS NULL OR hi.dip_cm IS NULL THEN NULL
                             WHEN lo.dip_cm = hi.dip_cm THEN lo.volume_l
                             ELSE ROUND(lo.volume_l + (%s - lo.dip_cm)/(hi.dip_cm - lo.dip_cm) * (hi.volume_l - lo.volume_l), 3)
                        END
                    FROM lo FULL OUTER JOIN hi ON true
                """, (aset, dip, aset, dip, dip))
                row = cur.fetchone()
                if row and row[4] is not None:
                    vol = row[4]
                else:
                    vol = None
                method = "interpolation"
            # Metadata
            cur.execute("""SELECT MIN(dip_cm), MAX(dip_cm), MAX(volume_l), COUNT(*)
                           FROM fcc.sounding_table WHERE aset=%s AND status='ACTIVE'""", (aset,))
            meta = cur.fetchone()
        return {
            "aset": aset,
            "dip_cm": dip,
            "volume_l": float(vol) if vol is not None else None,
            "found": vol is not None,
            "method": method,
            "meta": {
                "dip_min":      float(meta[0]) if meta[0] is not None else None,
                "dip_max":      float(meta[1]) if meta[1] is not None else None,
                "volume_max_l": float(meta[2]) if meta[2] is not None else None,
                "point_count":  int(meta[3])  if meta[3] is not None else 0,
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail={
            "error": str(e),
            "type": type(e).__name__,
            "trace": traceback.format_exc()[:500],
        })


@app.get("/api/v1/shift-route-config", tags=["config"],
         summary="Konfigurasi jalur per shift",
         dependencies=[Depends(verify_api_key)])
async def shift_route_config():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT id, tanggal, shift, jalur, main_tank, fm_akhir_shift_sebelumnya,
            fm_aktual_awal, deviasi, status FROM fcc.shift_route_config ORDER BY tanggal DESC""")
        rows = cur.fetchall()
        return {"data":[{"id":r[0],"tanggal":r[1].isoformat(),"shift":r[2],"jalur":r[3],
                         "main_tank":r[4],"fm_akhir":float(r[5]),"fm_aktual":float(r[6]),
                         "deviasi":float(r[7]) if r[7] else 0,"status":r[8]} for r in rows]}

@app.get("/api/v1/config", tags=["config"], summary="Konfigurasi aplikasi",
         dependencies=[Depends(verify_api_key)])
async def config():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT parameter, nilai, tipe, keterangan, rahasia FROM fcc.app_config ORDER BY parameter")
        rows = cur.fetchall()
        return {"data":[{"key":r[0],"value":r[1],"type":r[2],"description":r[3],"secret":r[4]} for r in rows]}

@app.get("/api/v1/summary", tags=["dashboard"], summary="Ringkasan cepat",
         description="Count rows untuk semua tabel master. Quick health check untuk dashboard.",
         dependencies=[Depends(verify_api_key)])
async def summary():
    with get_db() as conn:
        cur = conn.cursor()
        s = {}
        for t in ['master_unit','master_vendor','unit_alias','master_jalur',
                 'master_main_tank','master_fuel_truck','ft_mandar_ocean',
                 'sounding_table','shift_route_config','app_user']:
            cur.execute(f"SELECT count(*) FROM fcc.{t}")
            s[t] = cur.fetchone()[0]
        s["generated_at"] = datetime.utcnow().isoformat() + "Z"
        return s

@app.get("/api/v1/dashboard", tags=["dashboard"], summary="KPI dashboard",
         description="Aggregated metrics untuk dashboard utama: counts, capacity, breakdown per type.",
         dependencies=[Depends(verify_api_key)])
async def dashboard():
    with get_db() as conn:
        cur = conn.cursor()
        s = {}
        for t in ['master_unit','master_vendor','unit_alias','master_main_tank',
                 'master_fuel_truck','ft_mandar_ocean','sounding_table','shift_route_config']:
            cur.execute(f"SELECT count(*) FROM fcc.{t}")
            s[t] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FILTER (WHERE status='ACTIVE'), COUNT(*) FROM fcc.master_unit")
        s["unit_active"], s["unit_total"] = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM fcc.master_jalur WHERE peruntukan='TRANSFER'")
        s["jalur_transfer"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM fcc.master_jalur WHERE peruntukan='RECEIVING'")
        s["jalur_receiving"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM fcc.ft_mandar_ocean WHERE status='ACTIVE'")
        s["ft_mo_active"] = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(kapasitas_l),0) FROM fcc.master_main_tank WHERE status='ACTIVE'")
        s["total_capacity_main_tank"] = float(cur.fetchone()[0])
        cur.execute("SELECT COALESCE(SUM(kapasitas_l),0) FROM fcc.master_fuel_truck WHERE status='ACTIVE'")
        s["total_capacity_fuel_truck"] = float(cur.fetchone()[0])
        s["generated_at"] = datetime.utcnow().isoformat() + "Z"
        return s

# ── Error handler ──
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content={
            "error": detail,
            "request_id": id(request),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })
    return JSONResponse(status_code=exc.status_code, content={
        "error": {"code": "HTTP_ERROR", "message": str(detail)},
        "request_id": id(request),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Request tidak valid",
            "details": exc.errors(),
        },
        "request_id": id(request),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })

# ── Run ──

# ── Backward-compatible aliases (old DB table names from frontend) ──
@app.get("/api/v1/master_unit", dependencies=[Depends(verify_api_key)], include_in_schema=False)
async def master_unit_alias(response: Response, page: int = Query(1,ge=1), per_page: int = Query(50,ge=1,le=200),
                              search: Optional[str] = None, vendor: Optional[str] = None,
                              kategori: Optional[str] = None, fields: Optional[str] = None):
    return await units(response=response, page=page, per_page=per_page, search=search, vendor=vendor,
                       kategori=kategori, fields=fields)

@app.get("/api/v1/master_vendor", dependencies=[Depends(verify_api_key)], include_in_schema=False)
async def master_vendor_alias(response: Response, search: Optional[str] = None, kategori: Optional[str] = None, fields: Optional[str] = None):
    return await vendors(response=response, search=search, kategori=kategori, fields=fields)

@app.get("/api/v1/master_jalur", dependencies=[Depends(verify_api_key)], include_in_schema=False)
async def master_jalur_alias(response: Response, search: Optional[str] = None):
    return await jalur(response=response, search=search)

@app.get("/api/v1/master_main_tank", dependencies=[Depends(verify_api_key)], include_in_schema=False)
async def master_main_tank_alias(response: Response):
    return await main_tanks(response=response)

@app.get("/api/v1/master_fuel_truck", dependencies=[Depends(verify_api_key)], include_in_schema=False)
async def master_fuel_truck_alias(response: Response):
    return await fuel_trucks(response=response)

@app.get("/api/v1/ft_mandar_ocean", dependencies=[Depends(verify_api_key)], include_in_schema=False)
async def ft_mandar_ocean_alias(response: Response):
    return await ft_mandar_ocean(response=response)

@app.get("/api/v1/app_user", dependencies=[Depends(verify_api_key)], include_in_schema=False)
async def app_user_alias(response: Response, page: int = Query(1,ge=1), per_page: int = Query(50,ge=1,le=200),
                          search: Optional[str] = None, role: Optional[str] = None, fields: Optional[str] = None):
    return await users(response=response, page=page, per_page=per_page, search=search, role=role, fields=fields)

@app.get("/api/v1/app_config", dependencies=[Depends(verify_api_key)], include_in_schema=False)
async def app_config_alias(response: Response):
    return await config(response=response)

@app.get("/api/v1/ref_lookup", dependencies=[Depends(verify_api_key)], include_in_schema=False)
async def ref_lookup_alias(**kwargs):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT jenis, kode, label, urutan, aktif FROM fcc.ref_lookup ORDER BY jenis, urutan, kode")
        rows = cur.fetchall()
        return {"data":[{"jenis":r[0],"kode":r[1],"label":r[2],"urutan":r[3],"aktif":r[4]} for r in rows]}


if __name__ == "__main__":
    import uvicorn
    print(f"FCC PPA-BIB API v7")
    print(f"  Database: fuel_control_center @ /var/run/postgresql")
    print(f"  API Key: {API_KEY}")
    print(f"  Login: POST /api/v1/auth/login")
    print(f"  Docs: http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000)