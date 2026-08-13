"""
Fuel Control Center — API server v8 (PostgreSQL backend)
- asyncpg connection pool
- Session cookie httpOnly, login/logout/me
- /api/ref/bootstrap returning all masters (ACTIVE only, dropdown columns)
- Generic CRUD: /api/:table with role-based read/write matrix
- PUT /api/closing/:tanggal/:shift transactional (header + lines)
- GET /api/report/summary?dari&sampai
- GET /api/sounding/volume?aset=&dip= → fcc.volume_from_dip
- Error code translasi (23505/23503/23514/23502 → Indonesian)
- Per-transaction SET LOCAL app.actor, app.role, app.vendor
- Generated columns stripped on the API side before INSERT/UPDATE
"""
import os, json, hmac, hashlib, secrets, base64, datetime as dt, re, asyncio, urllib.request, urllib.parse, urllib.error
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from contextlib import asynccontextmanager

import asyncpg, uvicorn
from fastapi import FastAPI, Request, Response, HTTPException, Cookie, Depends, Query
from fastapi.responses import JSONResponse
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError, VerificationError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_URL       = os.environ.get("FCC_DB_URL", "postgres://fcc_app@/fcc?host=/var/run/postgresql")
SESSION_KEY  = os.environ.get("FCC_SESSION_KEY", "dev-only-change-me-" + secrets.token_urlsafe(16))
LISTEN_HOST  = os.environ.get("FCC_HOST", "0.0.0.0")
LISTEN_PORT  = int(os.environ.get("FCC_PORT", "8001"))

# Role × table read/write matrix.
# True = allowed, False = denied, "O" = own only (VENDOR on refuelling).
ROLES = {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER","VENDOR"}
ROLE_NAMES = {
    "SUPER_ADMIN":  "Super Admin",
    "ADMIN":        "Admin Fuel",
    "GROUP_LEADER": "Group Leader",
    "PENERIMAAN":   "Tim Penerimaan",
    "FUELMAN":      "Fuelman",
    "DRIVER":       "Driver",
    "VENDOR":       "Portal Vendor",
}

# Perms table — keys are table names. None = read all.
PERMS = {
    "master_vendor":     {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER","VENDOR"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "master_unit":       {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER","VENDOR"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "master_jalur":      {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER","VENDOR"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "master_main_tank":  {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER","VENDOR"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "master_fuel_truck": {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER","VENDOR"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "ft_mandar_ocean":   {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","VENDOR"}, "write": {"SUPER_ADMIN","ADMIN","PENERIMAAN"}},
    "unit_alias":        {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "shift_route_config":{"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER"}, "write": {"SUPER_ADMIN","ADMIN","PENERIMAAN"}},
    "penerimaan_mo":     {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN"}, "write": {"SUPER_ADMIN","ADMIN","PENERIMAAN"}},
    "transfer_fuel":     {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","FUELMAN","DRIVER"}, "write": {"SUPER_ADMIN","ADMIN","FUELMAN"}},
    "flowmeter_ft":      {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","FUELMAN","DRIVER"}, "write": {"SUPER_ADMIN","ADMIN","FUELMAN"}},
    "hour_meter":        {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","FUELMAN","DRIVER"}, "write": {"SUPER_ADMIN","ADMIN","FUELMAN","DRIVER"}},
    "pengurasan":        {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN"}, "write": {"SUPER_ADMIN","ADMIN","PENERIMAAN"}},
    "sounding_main_tank":{"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER"}, "write": {"SUPER_ADMIN","ADMIN","PENERIMAAN"}},
    "cleanliness":       {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER"}, "write": {"SUPER_ADMIN","ADMIN","PENERIMAAN","FUELMAN"}},
    "closing_stock":     {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "closing_stock_line":{"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "refuelling":        {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","VENDOR"}, "write": {"SUPER_ADMIN","ADMIN"}},  # VENDOR own via RLS
    "voucher_bib":       {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "fuel_import_row":   {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "import_batch":      {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "audit_trail":       {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER"}, "write": set()},
    "app_user":          {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "evidence":          {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER"}, "write": {"SUPER_ADMIN","ADMIN","PENERIMAAN","FUELMAN","DRIVER"}},
    "v_closing_line":    {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER"}, "write": set()},  # view only
    "v_pengurasan":      {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN"}, "write": set()},
    "v_rekonsiliasi":    {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER"}, "write": set()},
    "sounding_table":    {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER"}, "write": set()},
    "app_config":        {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER"}, "write": {"SUPER_ADMIN","ADMIN"}},
    "ref_lookup":        {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER","VENDOR"}, "write": {"SUPER_ADMIN","ADMIN"}},
    # Frontend compatibility alias; physical object is v_closing_line.
    "closing_line":      {"read": {"SUPER_ADMIN","ADMIN","GROUP_LEADER","PENERIMAAN","FUELMAN","DRIVER"}, "write": set()},
}

# Columns that PostgreSQL computes — strip from POST/PATCH payloads.
GENERATED = {
    "ft_mandar_ocean": {"status"},
    "shift_route_config": {"deviasi"},
    "penerimaan_mo": {"selisih_tera_cm", "total_fm_l"},
    "transfer_fuel": {"total_fm_l", "sounding_aktual_l", "deviasi_l", "deviasi_pct",
                      "volume_awal_l", "volume_akhir_l"},
    "flowmeter_ft": {"total_l"},
    "pengurasan": {"total_fm_l", "selisih_sounding_l", "deviasi_l",
                   "volume_awal_l", "volume_akhir_l"},
    "sounding_main_tank": {"selisih_l"},
    "cleanliness": {"status"},
    "voucher_bib": {"status"},
    "closing_stock_line": {"total_administrasi_l", "deviasi_total_l", "deviasi_pct"},
}

# Columns the client should never send (server-managed)
SERVER_OWNED = {
    "app_user": {"password_hash", "failed_logins", "locked_until", "last_login",
                 "created_at", "updated_at", "id"},
    "ft_mandar_ocean": {"status", "created_at", "updated_at"},
    "default": {"created_at", "updated_at", "id"},
}

ph = PasswordHasher()

# ---------------------------------------------------------------------------
# Lifespan: connection pool
# ---------------------------------------------------------------------------
pool: Optional[asyncpg.Pool] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10, command_timeout=20,
                                     server_settings={"search_path": "fcc,public"})
    # Wire fuel_bridge module ke pool yang sama
    try:
        from fuel_bridge import set_fuel_pool
        set_fuel_pool(pool)
    except Exception as e:
        print(f"fuel_bridge import warning: {e}", flush=True)
    yield
    await pool.close()

app = FastAPI(title="Fuel Control Center API", lifespan=lifespan)

# Include fuel_bridge router (endpoint /api/fuel/*)
try:
    from fuel_bridge import fuel_router
    app.include_router(fuel_router)
    print("fuel_bridge mounted at /api/fuel", flush=True)
except Exception as e:
    print(f"fuel_bridge mount warning: {e}", flush=True)

# ---------------------------------------------------------------------------
# Session helpers (signed cookie, no JWT — keeps it simple and stateless)
# ---------------------------------------------------------------------------
def make_session(username: str, role: str, vendor_kode) -> str:
    payload = json.dumps({"u": username, "r": role, "v": vendor_kode, "t": int(dt.datetime.utcnow().timestamp())})
    sig = hmac.new(SESSION_KEY.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + base64.urlsafe_b64encode(sig).decode()

def read_session(token: str) -> Optional[dict]:
    if not token or "." not in token: return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = base64.urlsafe_b64decode(payload_b64).decode()
        sig = base64.urlsafe_b64decode(sig_b64)
        if not hmac.compare_digest(hmac.new(SESSION_KEY.encode(), payload.encode(), hashlib.sha256).digest(), sig):
            return None
        data = json.loads(payload)
        # Expire after 12 hours
        if int(dt.datetime.utcnow().timestamp()) - data["t"] > 12*3600: return None
        return data
    except Exception:
        return None

async def current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get("fcc_session")
    if not token: return None
    sess = read_session(token)
    if not sess: return None
    # Verify user still exists and is active
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT username, nama, role, vendor_kode, status FROM fcc.app_user WHERE username=$1", sess["u"])
    if not row or row["status"] != "ACTIVE": return None
    return {"username": row["username"], "nama": row["nama"], "role": row["role"], "vendor_kode": row["vendor_kode"]}

async def require_user(request: Request) -> dict:
    user = await current_user(request)
    if not user: raise HTTPException(401, "Belum login.")
    return user

def require_perm(user: dict, table: str, action: str):
    """action in {'read','write'}"""
    if table not in PERMS:
        raise HTTPException(400, f"Tabel {table} tidak dikenali.")
    allowed = PERMS[table].get(action, set())
    if action == "write" and "DELETE" not in allowed and user["role"] != "SUPER_ADMIN":
        # DELETE only for SUPER_ADMIN — write checks already cover admin
        pass
    if user["role"] not in allowed:
        raise HTTPException(403, f"Peran {user['role']} tidak diizinkan {action} pada {table}.")

# ---------------------------------------------------------------------------
# Error translasi
# ---------------------------------------------------------------------------
PG_ERR_MSG = {
    "23505": ("UNIQUE_VIOLATION",   "Nilai kolom {} sudah dipakai."),
    "23503": ("FK_VIOLATION",       "Nilai kolom {} merujuk ke data yang belum ada."),
    "23514": ("CHECK_VIOLATION",    "Nilai kolom {} melanggar aturan tabel."),
    "23502": ("NOT_NULL_VIOLATION", "Kolom {} wajib diisi."),
    "22P02": ("INVALID_TEXT",       "Format nilai tidak valid untuk kolom {}."),
}

def translate_pg_error(e: asyncpg.exceptions.PostgresError, table: str = "", columns: dict = None) -> dict:
    code = getattr(e, "sqlstate", None) or "ERROR"
    detail = getattr(e, "detail", None) or ""
    # column_name is unreliable in asyncpg; extract from detail or constraint_name
    field = getattr(e, "column_name", None) or ""
    constraint = getattr(e, "constraint_name", None) or ""
    if not field:
        # Parse "(column)=value" from detail like 'Key (kode)=(A25001) already exists.'
        import re
        m = re.search(r'\(([^)=]+)\)=', detail or str(e))
        if m: field = m.group(1)
    if not field:
        # Try to extract from raw message
        m = re.search(r'column "([^"]+)"', str(e))
        if m: field = m.group(1)
    # Last resort: if we have columns map and a UNIQUE on a column, use that
    if not field and code == "23505" and columns:
        for k, v in columns.items():
            if "unique" in (v or "").lower(): field = k; break
    if not field:
        field = "input"
    label, msg = PG_ERR_MSG.get(code, ("ERROR", str(e)))
    return {"error": {"code": label, "message": msg.format(field), "field": field, "constraint": constraint or ""}}

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
async def with_user_session(conn, user: dict):
    # asyncpg can't bind parameters to SET LOCAL (treated as DDL-ish).
    # Sanitize manually since values come from authenticated session (server-controlled).
    actor = str(user["username"]).replace("'", "''")
    role  = str(user["role"]).replace("'", "''")
    vendor = str(user.get("vendor_kode") or "").replace("'", "''")
    await conn.execute(f"SET LOCAL app.actor = '{actor}'")
    await conn.execute(f"SET LOCAL app.role  = '{role}'")
    await conn.execute(f"SET LOCAL app.vendor = '{vendor}'")

def strip_generated(table: str, body: dict) -> dict:
    skip = GENERATED.get(table, set())
    body = {k: v for k, v in body.items() if k not in skip}
    skip2 = SERVER_OWNED.get(table, SERVER_OWNED["default"])
    body = {k: v for k, v in body.items() if k not in skip2}
    return body

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.post("/api/auth/login")
async def login(request: Request, response: Response):
    body = await request.json()
    username = body.get("username","").strip()
    password = body.get("password","")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT username, nama, role, vendor_kode, password_hash, must_change_pw, status
            FROM fcc.app_user WHERE username=$1
        """, username)
    if not row:
        return JSONResponse({"error":{"code":"AUTH","message":"Username atau password salah."}}, status_code=401)
    if row["status"] != "ACTIVE":
        return JSONResponse({"error":{"code":"AUTH","message":"Akun non-aktif."}}, status_code=401)
    try:
        ph.verify(row["password_hash"], password)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return JSONResponse({"error":{"code":"AUTH","message":"Username atau password salah."}}, status_code=401)
    user = {"username": row["username"], "nama": row["nama"], "role": row["role"], "vendor_kode": row["vendor_kode"]}
    token = make_session(user["username"], user["role"], user["vendor_kode"])
    response.set_cookie("fcc_session", token, httponly=True, samesite="lax", max_age=12*3600)
    return {"user": user, "must_change_pw": row["must_change_pw"]}

@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie("fcc_session")
    return {"ok": True}

@app.post("/api/auth/change_password")
async def change_password_self(request: Request, response: Response, user=Depends(require_user)):
    """User change their own password."""
    body = await request.json()
    new_pw = body.get("new_password") or body.get("password") or ""
    if len(new_pw) < 6:
        raise HTTPException(400, "Password minimal 6 karakter.")
    new_hash = ph.hash(new_pw)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE fcc.app_user SET password_hash=$1, must_change_pw=false, updated_at=now() WHERE username=$2",
            new_hash, user["username"]
        )
    return {"ok": True, "user": user}

@app.post("/api/auth/register")
async def register(request: Request, response: Response):
    """Buat user baru. Pertama jadi SUPER_ADMIN, berikutnya FIELD.
    Mirip Supabase signUp() untuk app.js lapangan."""
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    full_name = body.get("full_name") or username
    if not username or len(password) < 6:
        raise HTTPException(400, "Username dan password (min 6 char) wajib.")

    async with pool.acquire() as conn:
        existing = await conn.fetchval("SELECT count(*) FROM fcc.app_user")
        is_first = (existing == 0)
        role = "SUPER_ADMIN" if is_first else "FIELD"
        existing_user = await conn.fetchval("SELECT id FROM fcc.app_user WHERE username=$1", username)
        if existing_user:
            raise HTTPException(409, "Username sudah dipakai.")
        new_hash = ph.hash(password)
        await conn.execute(
            """INSERT INTO fcc.app_user (username, nama, role, status, password_hash, must_change_pw)
               VALUES ($1, $2, $3, 'ACTIVE', $4, true)""",
            username, full_name, role, new_hash
        )

    # Auto-login
    token = make_session(username, role, None)
    response.set_cookie("fcc_session", token, httponly=True, samesite="lax", max_age=12*3600)
    return {"user": {"username": username, "nama": full_name, "role": role, "vendor_kode": None}, "must_change_pw": False}

@app.get("/api/auth/me")
async def me(user=Depends(require_user)):
    return {"user": user}

# ---------------------------------------------------------------------------
# Bootstrap: master data untuk dropdown
# ---------------------------------------------------------------------------
@app.get("/api/ref/bootstrap")
async def bootstrap(user=Depends(require_user)):
    require_perm(user, "master_vendor", "read")
    out = {}
    async with pool.acquire() as conn:
        out["master_vendor"]     = [dict(r) for r in await conn.fetch("SELECT kode, nama, kategori, status FROM fcc.master_vendor ORDER BY nama")]
        out["master_main_tank"]  = [dict(r) for r in await conn.fetch("SELECT kode, nama, kapasitas_l, status FROM fcc.master_main_tank ORDER BY kode")]
        out["master_fuel_truck"] = [dict(r) for r in await conn.fetch("SELECT kode, nama, tipe, kapasitas_l, status FROM fcc.master_fuel_truck ORDER BY kode")]
        out["master_jalur"]      = [dict(r) for r in await conn.fetch("SELECT kode, nama, tujuan, peruntukan, site, status FROM fcc.master_jalur ORDER BY kode")]
        out["ft_mandar_ocean"]   = [dict(r) for r in await conn.fetch("SELECT id_ft, no_lambung, no_polisi, kapasitas_l, masa_berlaku, status FROM fcc.ft_mandar_ocean ORDER BY id_ft")]
        out["ref_lookup"]        = [dict(r) for r in await conn.fetch("SELECT jenis, kode, label FROM fcc.ref_lookup WHERE aktif=true ORDER BY jenis, urutan, kode")]
        out["master_unit_total"] = await conn.fetchval("SELECT count(*) FROM fcc.master_unit")
    return out

@app.get("/api/master_unit")
async def master_unit_search(q: str = "", limit: int = 50, offset: int = 0, user=Depends(require_user)):
    require_perm(user, "master_unit", "read")
    q = (q or "").strip()
    async with pool.acquire() as conn:
        if q:
            rows = await conn.fetch(
                "SELECT kode, nama, vendor_kode, kategori, status FROM fcc.master_unit "
                "WHERE (kode ILIKE $1 OR nama ILIKE $1) ORDER BY kode LIMIT $2 OFFSET $3",
                f"%{q}%", limit, offset)
            total = await conn.fetchval(
                "SELECT count(*) FROM fcc.master_unit WHERE (kode ILIKE $1 OR nama ILIKE $1)",
                f"%{q}%")
        else:
            rows = await conn.fetch(
                "SELECT kode, nama, vendor_kode, kategori, status FROM fcc.master_unit "
                "ORDER BY kode LIMIT $1 OFFSET $2", limit, offset)
            total = await pool.fetchval("SELECT count(*) FROM fcc.master_unit")
    return {"data": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

# ---------------------------------------------------------------------------
# Sounding lookup (must come BEFORE /api/{table} generic)
# ---------------------------------------------------------------------------
@app.get("/api/sounding/volume")
async def sounding_volume(aset: str, dip: float, user=Depends(require_user)):
    async with pool.acquire() as conn:
        v = await conn.fetchval("SELECT fcc.volume_from_dip($1, $2::numeric)", aset, dip)
    return {"aset": aset, "dip": dip, "volume_l": float(v) if v is not None else None}

# ---------------------------------------------------------------------------
# Reporting (aggregat) — must come BEFORE /api/{table} generic
# ---------------------------------------------------------------------------
@app.get("/api/report/summary")
async def report_summary(dari: str, sampai: str, user=Depends(require_user)):
    """Aggregate KPIs from PG FCC + Supabase (hybrid).

    PG tables:   transfer_fuel, penerimaan_mo, pengurasan, cleanliness
    Supabase:    fuel_v_transfer_fuel, fuel_v_fuel_truck_monitoring (FLOWMETER + HM)
    """
    async with pool.acquire() as conn:
        await with_user_session(conn, user)
        rows = await conn.fetch(f"""
            SELECT 'transfer_fuel' AS modul, count(*) AS n,
                   coalesce(sum(total_fm_l),0) AS total_l,
                   coalesce(sum(CASE WHEN deviasi_pct>5 THEN 1 ELSE 0 END),0) AS over_threshold
            FROM transfer_fuel WHERE tanggal BETWEEN '{dari}'::date AND '{sampai}'::date
            UNION ALL
            SELECT 'penerimaan_mo', count(*),
                   coalesce(sum(total_fm_l),0),
                   coalesce(sum(CASE WHEN selisih_tera_cm > 0.5 THEN 1 ELSE 0 END),0)
            FROM penerimaan_mo WHERE tanggal BETWEEN '{dari}'::date AND '{sampai}'::date
            UNION ALL
            SELECT 'pengurasan', count(*),
                   coalesce(sum(total_fm_l),0),
                   coalesce(sum(CASE WHEN status='WARNING' THEN 1 ELSE 0 END),0)
            FROM pengurasan WHERE tanggal BETWEEN '{dari}'::date AND '{sampai}'::date
            UNION ALL
            SELECT 'cleanliness', count(*), 0,
                   coalesce(sum(CASE WHEN status='WARNING' THEN 1 ELSE 0 END),0)
            FROM cleanliness WHERE tanggal BETWEEN '{dari}'::date AND '{sampai}'::date
        """)
    pg_data = [dict(r) for r in rows]
    sources = {"pg": pg_data, "supabase": []}
    # Try to fetch Supabase aggregates too (if configured)
    if _supa_ready():
        try:
            qq = [("select", "total_fm_liter,status_deviasi,tanggal"),
                  ("order", "tanggal.desc"), ("limit", "1000")]
            if dari: qq.append(("tanggal", f"gte.{dari}"))
            if sampai: qq.append(("tanggal", f"lte.{sampai}"))
            _, _, tr_rows = await _supa_http("GET", "fuel_v_transfer_fuel", qq)
            active = [r for r in (tr_rows or []) if not r.get("voided_at")]
            supa_summary = {
                "modul": "transfer_fuel_supa",
                "n": len(active),
                "total_l": sum(float(r.get("total_fm_liter") or 0) for r in active),
                "over_threshold": sum(1 for r in active
                                       if (r.get("status_deviasi") or "").upper() in ("WARNING","CRITICAL")),
            }
            sources["supabase"].append(supa_summary)
        except Exception as e:
            sources["supabase_error"] = str(e)
    return {"data": pg_data, "supa_data": sources["supabase"],
            "dari": dari, "sampai": sampai, "sources": ["pg"] + (["supabase"] if sources["supabase"] else [])}

# -------------------------------------------------------------------------------
# Rekonsiliasi SS6 vs SAP — uses v_rekonsiliasi view
# -------------------------------------------------------------------------------
@app.get("/api/report/rekonsil")
async def report_rekonsil(dari: str = "", sampai: str = "",
                          unit_standar: str = "", status: str = "",
                          vendor_kode: str = "", limit: int = 200,
                          user=Depends(require_user)):
    """Delta SS6 vs SAP per (tanggal, unit_standar). All filters optional."""
    from datetime import date as _date
    where = []
    params = []
    def addp(v): params.append(v); return f"${len(params)}"
    if dari:
        try: parts = dari.split("-"); dari_d = _date(int(parts[0]), int(parts[1]), int(parts[2]))
        except: dari_d = dari
        i = addp(dari_d); where.append(f"tanggal >= {i}")
    if sampai:
        try: parts = sampai.split("-"); sampai_d = _date(int(parts[0]), int(parts[1]), int(parts[2]))
        except: sampai_d = sampai
        i = addp(sampai_d); where.append(f"tanggal <= {i}")
    if unit_standar:
        i = addp(unit_standar); where.append(f"unit_standar = {i}")
    if status:
        i = addp(status); where.append(f"status = {i}")
    if vendor_kode:
        i = addp(vendor_kode); where.append(f"vendor_kode = {i}")
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT * FROM v_rekonsiliasi {wsql} ORDER BY tanggal DESC, abs_delta_l DESC NULLS LAST LIMIT {limit}"
    cnt = f"SELECT count(*) FROM v_rekonsiliasi {wsql}"
    # Aggregated counts
    summary_sql = f"""
    SELECT status, count(*) AS n, coalesce(sum(abs_delta_l),0) AS total_abs_delta
    FROM v_rekonsiliasi {wsql} GROUP BY status ORDER BY status
    """
    async with pool.acquire() as conn:
        await with_user_session(conn, user)
        rows = await conn.fetch(sql, *params)
        total = await conn.fetchval(cnt, *params)
        summary = await conn.fetch(summary_sql, *params)
    return {
        "data": [dict(r) for r in rows],
        "total": total, "limit": limit,
        "summary": [dict(r) for r in summary],
        "filters": {"dari": dari, "sampai": sampai, "unit_standar": unit_standar,
                    "status": status, "vendor_kode": vendor_kode}
    }

# ---------------------------------------------------------------------------
# Generic CRUD
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Health (must come BEFORE /api/{table} so it's not matched as a table name)
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    async with pool.acquire() as conn:
        v = await conn.fetchval("SELECT version()")
    return {"ok": True, "service": "fcc-api", "version": "v8-pg", "db": v[:60]}

# ---------------------------------------------------------------------------
# Change password — SUPER_ADMIN resets any user's password
# ---------------------------------------------------------------------------
@app.post("/api/app_user/{uid}/change_password")
async def change_password(uid: int, request: Request, user=Depends(require_user)):
    if user["role"] != "SUPER_ADMIN":
        raise HTTPException(403, "Hanya SUPER_ADMIN boleh reset password user lain.")
    body = await request.json()
    new_password = body.get("new_password", "")
    confirm = body.get("confirm_password", "")
    if not new_password or len(new_password) < 8:
        raise HTTPException(400, "Password minimal 8 karakter.")
    if new_password != confirm:
        raise HTTPException(400, "Password dan konfirmasi tidak cocok.")
    # Hash with argon2 (same as initial user creation)
    new_hash = ph.hash(new_password)
    async with pool.acquire() as conn:
        await with_user_session(conn, user)
        updated = await conn.execute(
            "UPDATE app_user SET password_hash=$1, must_change_pw=true, failed_logins=0, locked_until=NULL, updated_at=now() WHERE id=$2",
            new_hash, uid
        )
        if updated == "UPDATE 0":
            raise HTTPException(404, "User tidak ditemukan.")
        # Audit
        username = await conn.fetchval("SELECT username FROM app_user WHERE id=$1", uid)
    # Audit trail handled by trg_app_user_audit trigger
    return {"ok": True, "username": username, "message": f"Password untuk {username} berhasil direset. User harus ganti password saat login."}


# ---------------------------------------------------------------------------
# Photo evidence — store compressed base64 in PG fcc.evidence
# Works for: transfer_fuel, flowmeter_ft, hour_meter, pengurasan, sounding_main_tank,
#          cleanliness, penerimaan_mo
# ---------------------------------------------------------------------------
import hashlib as _hashlib

@app.post("/api/evidence/upload")
async def evidence_upload(request: Request, user=Depends(require_user)):
    """Upload a photo (compressed base64) to fcc.photo.
    Body: { modul: 'transfer_fuel'|'flowmeter_ft'|'hour_meter'|'pengurasan'|'sounding_main_tank'|'cleanliness'|'penerimaan_mo', record_id: id, photo_type: 'fm_awal'|'fm_akhir'|'fm'|'hm'|'sounding_intank'|'sounding_aktual_1'|'cleanliness_before'|'cleanliness_after'|'penerimaan_sampel'|'penerimaan_fm', base64: 'data:image/jpeg;base64,...' }
    """
    body = await request.json()
    modul = body.get("modul", "")
    record_id = str(body.get("record_id", ""))
    photo_type = body.get("photo_type", "sampel")
    base64_data = body.get("base64", "")
    if not modul or not record_id:
        raise HTTPException(400, "modul dan record_id wajib diisi.")
    if not base64_data.startswith("data:image/"):
        raise HTTPException(400, "base64 harus data:image/...")
    raw = base64_data.split(",", 1)[1] if "," in base64_data else base64_data
    try:
        size = len(_b64.b64decode(raw))
    except Exception:
        raise HTTPException(400, "base64 invalid.")
    if size > 3 * 1024 * 1024:
        raise HTTPException(400, f"Foto terlalu besar ({size//1024} KB). Compress dulu max 3 MB.")
    mime = "image/jpeg"
    if base64_data.startswith("data:image/png"):
        mime = "image/png"
    elif base64_data.startswith("data:image/webp"):
        mime = "image/webp"
    async with pool.acquire() as conn:
        await with_user_session(conn, user)
        try:
            photo_id = await conn.fetchval(
                """INSERT INTO photo (modul, record_id, photo_type, base64_data, size_bytes, mime_type, uploaded_by)
                   VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
                modul, record_id, photo_type, base64_data, size, mime, user["username"]
            )
        except Exception as e:
            raise HTTPException(500, f"DB insert failed: {e}")
    return {"ok": True, "id": photo_id, "size_bytes": size, "photo_type": photo_type, "mime_type": mime}


@app.get("/api/evidence/list")
async def evidence_list(modul: str, record_id: str, user=Depends(require_user)):
    """List all photos for a (modul, record_id). Excludes the base64 data (use get for that)."""
    async with pool.acquire() as conn:
        await with_user_session(conn, user)
        rows = await conn.fetch(
            "SELECT id, photo_type, size_bytes, mime_type, uploaded_by, uploaded_at FROM photo WHERE modul=$1 AND record_id=$2 ORDER BY id",
            modul, record_id
        )
    return [
        {
            "id": r["id"],
            "photo_type": r["photo_type"],
            "size_bytes": r["size_bytes"],
            "mime_type": r["mime_type"],
            "uploaded_by": r["uploaded_by"],
            "uploaded_at": r["uploaded_at"].isoformat() if r["uploaded_at"] else None,
        }
        for r in rows
    ]




@app.get("/api/evidence/{evid_id}")
async def evidence_get(evid_id: int, user=Depends(require_user)):
    """Get single photo metadata + base64 data URL."""
    async with pool.acquire() as conn:
        await with_user_session(conn, user)
        row = await conn.fetchrow(
            "SELECT id, modul, record_id, photo_type, base64_data, size_bytes, mime_type, uploaded_by, uploaded_at FROM photo WHERE id=$1",
            evid_id
        )
    if not row:
        raise HTTPException(404, "Photo not found")
    return {
        "id": row["id"],
        "modul": row["modul"],
        "record_id": row["record_id"],
        "photo_type": row["photo_type"],
        "data_url": row["base64_data"],
        "size_bytes": row["size_bytes"],
        "mime_type": row["mime_type"],
        "uploaded_by": row["uploaded_by"],
        "uploaded_at": row["uploaded_at"].isoformat() if row["uploaded_at"] else None,
    }



# ---------------------------------------------------------------------------
# Photo upload — multipart/form-data to Supabase Storage + fuel_attachment_log
# Used by: transfer_fuel, flowmeter_ft, hour_meter, pengurasan, sounding_main_tank,
#          cleanliness, penerimaan_mo (koreksi lapangan via dashboard)
# ---------------------------------------------------------------------------
import base64 as _b64
import uuid as _uuid

@app.post("/api/upload/photo")
async def upload_photo(request: Request, user=Depends(require_user)):
    """Upload a single photo. Returns { attachment_id, url, storage_path }.
    Form fields:
      - file: binary image (jpg/png/webp, max 5 MB)
      - site_code: PPA-BIB
      - photo_type: 'sampel' | 'flowmeter_awal' | 'flowmeter_akhir' | 'sounding_aktual' | 'intank' | 'before' | 'after' | etc.
      - fk_table: 'transfer_fuel' | 'flowmeter_ft' | 'hour_meter' | 'pengurasan' | 'sounding_main_tank' | 'cleanliness' | 'penerimaan_mo'
      - fk_id: ID of the row (auto-set after parent INSERT)
    """
    form = await request.form()
    f = form.get("file")
    if not f:
        raise HTTPException(400, "File tidak boleh kosong.")
    content = await f.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "File terlalu besar (max 5 MB).")
    site_code = form.get("site_code", "PPA-BIB")
    photo_type = form.get("photo_type", "other")
    fk_table = form.get("fk_table", "")
    fk_id = form.get("fk_id", "")
    ext = (f.filename or "photo.jpg").split(".")[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"
    mime = f.content_type or "image/jpeg"
    # Upload to Supabase Storage
    file_path = f"fcc/{site_code}/{fk_table or 'misc'}/{_uuid.uuid4().hex}.{ext}"
    upload_url = f"{SUPA_URL}/storage/v1/object/fuel-control-photos/{file_path}"
    try:
        req = urllib.request.Request(upload_url, method="POST", data=content, headers={
            "apikey": SUPA_SECRET,
            "Authorization": f"Bearer {SUPA_SECRET}",
            "Content-Type": mime,
            "x-upsert": "true",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status not in (200, 201):
                raise HTTPException(502, f"Storage upload failed: {r.status}")
    except Exception as e:
        raise HTTPException(502, f"Storage error: {e}")
    # Insert into fuel_attachment_log
    payload = {
        "site_code": site_code,
        "photo_type": photo_type,
        "bucket_name": "fuel-control-photos",
        "storage_path": file_path,
        "mime_type": mime,
        "file_size_bytes": len(content),
        "uploaded_by": user["username"],
    }
    # Map fk_table -> FK column
    fk_col_map = {
        "transfer_fuel": "transfer_fuel_id",
        "flowmeter_ft": "monitoring_id",
        "hour_meter": "monitoring_id",
        "pengurasan": None,  # no FK column yet
        "sounding_main_tank": None,
        "cleanliness": None,
        "penerimaan_mo": None,
    }
    fk_col = fk_col_map.get(fk_table)
    if fk_col and fk_id:
        payload[fk_col] = fk_id
    insert_url = f"{SUPA_URL}/rest/v1/fuel_attachment_log"
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(insert_url, method="POST", data=data, headers={
            "apikey": SUPA_SECRET,
            "Authorization": f"Bearer {SUPA_SECRET}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read())
            att_id = rows[0]["id"] if rows else None
    except Exception as e:
        raise HTTPException(500, f"DB insert failed: {e}")
    public_url = f"{SUPA_URL}/storage/v1/object/public/fuel-control-photos/{file_path}"
    return {
        "ok": True,
        "attachment_id": att_id,
        "storage_path": file_path,
        "url": public_url,
        "file_size_bytes": len(content),
    }


@app.get("/api/attachment/{att_id}")
async def get_attachment(att_id: str, user=Depends(require_user)):
    """Get attachment metadata + signed URL."""
    url = f"{SUPA_URL}/rest/v1/fuel_attachment_log?id=eq.{att_id}&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": SUPA_SECRET, "Authorization": f"Bearer {SUPA_SECRET}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read())
    except Exception as e:
        raise HTTPException(500, str(e))
    if not rows:
        raise HTTPException(404, "Attachment not found")
    att = rows[0]
    return {
        "id": att["id"],
        "storage_path": att.get("storage_path"),
        "bucket_name": att.get("bucket_name"),
        "photo_type": att.get("photo_type"),
        "url": f"{SUPA_URL}/storage/v1/object/public/{att.get('bucket_name')}/{att.get('storage_path')}",
    }
SORT_WHITELIST = {"id","kode","tanggal","nama","created_at","updated_at"}

# Fields that must never be returned to browsers.
PUBLIC_EXCLUDE = {
    "app_user": {"password_hash", "failed_logins", "locked_until"},
}
DB_TABLE_ALIASES = {"closing_line": "v_closing_line"}

def _db_table(table: str) -> str:
    return DB_TABLE_ALIASES.get(table, table)


def _qident(name: str) -> str:
    """Quote an identifier discovered from PostgreSQL metadata."""
    return '"' + str(name).replace('"', '""') + '"'


def _normalize_type(dtype: str) -> str:
    return (dtype or "").lower().strip()


def _coerce_value(value: Any, dtype: str):
    """Convert JSON values into Python types accepted by asyncpg."""
    t = _normalize_type(dtype)
    if value is None:
        return None
    if value == "" and not (t == "text" or t.startswith("character")):
        return None
    if t in ("smallint", "integer", "bigint"):
        return int(value)
    if t.startswith("numeric") or t in ("decimal", "real", "double precision"):
        return Decimal(str(value))
    if t == "date":
        return value if isinstance(value, dt.date) and not isinstance(value, dt.datetime) else dt.date.fromisoformat(str(value)[:10])
    if t.startswith("timestamp"):
        if isinstance(value, dt.datetime):
            return value
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if t.startswith("time"):
        return value if isinstance(value, dt.time) else dt.time.fromisoformat(str(value))
    if t == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "t", "yes", "ya"}
    if t in ("json", "jsonb"):
        if isinstance(value, str):
            # Validate JSON text, then send canonical JSON text to asyncpg.
            return json.dumps(json.loads(value), ensure_ascii=False)
        return json.dumps(value, ensure_ascii=False)
    return value


def _encode_row_key(values: list) -> str:
    raw = json.dumps(values, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "rk_" + token


def _decode_row_key(rid: str, pk_cols: list[tuple[str, str]]) -> list:
    """Decode new rk_ keys and keep backward compatibility with legacy IDs."""
    if not pk_cols:
        raise HTTPException(400, "Tabel tidak memiliki primary key yang dapat diedit.")
    raw_values = None
    if rid.startswith("rk_"):
        token = rid[3:]
        token += "=" * (-len(token) % 4)
        try:
            raw_values = json.loads(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
        except Exception:
            raise HTTPException(400, "Kunci baris tidak valid.")
        if not isinstance(raw_values, list):
            raw_values = [raw_values]
    elif len(pk_cols) == 1:
        raw_values = [rid]
    else:
        # Compatibility for the previous client that joined composite keys with '/'.
        raw_values = rid.split("/")
    if len(raw_values) != len(pk_cols):
        raise HTTPException(400, "Jumlah komponen primary key tidak sesuai.")
    try:
        return [_coerce_value(v, dtype) for v, (_, dtype) in zip(raw_values, pk_cols)]
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"Format primary key tidak valid: {exc}")


def _decorate_row(row, pk_cols: list[tuple[str, str]], table: str = "") -> dict:
    data = dict(row)
    for field in PUBLIC_EXCLUDE.get(table, set()):
        data.pop(field, None)
    if pk_cols:
        values = [data.get(col) for col, _ in pk_cols]
        data["__row_key"] = _encode_row_key(values)
        data["__pk_display"] = " / ".join("" if v is None else str(v) for v in values)
        data["__pk_columns"] = [col for col, _ in pk_cols]
    return data


async def _table_columns(table: str):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT column_name, data_type, ordinal_position
            FROM information_schema.columns
            WHERE table_schema='fcc' AND table_name=$1
            ORDER BY ordinal_position
        """, _db_table(table))
    return [(r["column_name"], r["data_type"]) for r in rows]


async def _table_pk_cols(table: str):
    """Return primary key columns in the exact order defined by PostgreSQL."""
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch("""
                SELECT a.attname AS column_name,
                       format_type(a.atttypid, a.atttypmod) AS data_type,
                       keycols.ordinality
                FROM pg_index i
                CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS keycols(attnum, ordinality)
                JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=keycols.attnum
                WHERE i.indrelid=('fcc.' || $1)::regclass AND i.indisprimary
                ORDER BY keycols.ordinality
            """, _db_table(table))
        except asyncpg.exceptions.PostgresError:
            return []
    return [(r["column_name"], r["data_type"]) for r in rows]


async def _effective_pk_cols(table: str, type_map: Optional[dict] = None):
    pk_cols = await _table_pk_cols(table)
    if pk_cols:
        return pk_cols
    type_map = type_map or dict(await _table_columns(table))
    if "id" in type_map:
        return [("id", type_map["id"])]
    return []


async def _table_type_map(table: str):
    return dict(await _table_columns(table))


# ---------------------------------------------------------------------------
# SUPABASE HYBRID BRIDGE — /api/supa/...
# Read-only bridge to Supabase fuel_* tables used by the field-input web.
# Source selection is done at the frontend via DATA_SOURCE mapping; this
# server does NOT replace any PostgreSQL endpoint.
# (Routes are registered BEFORE /api/{table} generic to avoid being captured.)
# ---------------------------------------------------------------------------

SUPA_URL       = os.environ.get("FCC_SUPABASE_URL", "").rstrip("/")
SUPA_SECRET    = os.environ.get("FCC_SUPABASE_SECRET_KEY", "")
SUPA_ACTOR_UUID= os.environ.get("FCC_SUPABASE_ACTOR_UUID", "")
SUPA_FIELD_URL = os.environ.get("FCC_FIELD_WEB_URL", "")
SUPA_SITE      = os.environ.get("FCC_FIELD_SITE_CODE", "PPA-BIB")

# Allowlist of Supabase relations readable through the bridge.
# Keys are FCC PostgreSQL table names (frontend naming). Values map to Supabase relations.
SUPA_TABLES = {
    "master_jalur":          {"supa": "fuel_master_jalur",      "pk": "id", "search": ["jalur_code", "jalur_name", "status"]},
    "master_main_tank":      {"supa": "fuel_master_tandon",     "pk": "id", "search": ["tandon_code", "tandon_name", "status"]},
    "master_fuel_truck":     {"supa": "fuel_master_fuel_truck", "pk": "id", "search": ["unit_code", "unit_name", "unit_type", "status"]},
    "fm_awal_settings":      {"supa": "fuel_fm_awal_settings",  "pk": "id", "search": ["mode", "notes"]},
    "sounding_table":        {"supa": "fuel_tera_tangki_grid",  "pk": "unit_code", "search": ["unit_code", "source_label", "source_sheet", "source_file"]},
    "transfer_fuel":         {"supa": "fuel_v_transfer_fuel",   "pk": "id", "search": ["petugas_name", "jalur_code", "tandon_code", "fuel_truck_code", "shift", "status_deviasi"], "date_col": "tanggal"},
    "flowmeter_ft":          {"supa": "fuel_v_fuel_truck_monitoring", "supa_table": "fuel_tx_fuel_truck_monitoring", "pk": "id", "search": ["petugas_name", "fuel_truck_code", "shift"], "date_col": "tanggal", "filter": {"monitoring_type": "FLOWMETER"}},
    "hour_meter":            {"supa": "fuel_v_fuel_truck_monitoring", "supa_table": "fuel_tx_fuel_truck_monitoring", "pk": "id", "search": ["petugas_name", "fuel_truck_code", "shift"], "date_col": "tanggal", "filter": {"monitoring_type": "HM"}},
    "fuel_attachment_log":   {"supa": "fuel_attachment_log",    "pk": "id", "search": ["photo_type", "storage_path", "bucket_name"]},
    "fuel_profiles":         {"supa": "fuel_profiles",          "pk": "id", "search": ["full_name", "email", "nrp", "role", "status"]},
}


def _supa_ready():
    return bool(SUPA_URL) and bool(SUPA_SECRET)


async def _supa_http(method: str, path: str, query=None, body=None, prefer=None):
    """Make a server-side request to Supabase REST API. Secret never leaves the server."""
    if not _supa_ready():
        raise HTTPException(503, "Supabase belum dikonfigurasi di server.")
    url = f"{SUPA_URL}/rest/v1/{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    headers = {
        "apikey": SUPA_SECRET,
        "Authorization": f"Bearer {SUPA_SECRET}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        loop = asyncio.get_event_loop()
        def _do():
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, dict(r.headers), r.read()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            status, hdrs, body_bytes = await loop.run_in_executor(ex, _do)
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        status = e.code
        hdrs = dict(e.headers)
    except Exception as e:
        raise HTTPException(502, f"Supabase error: {e}")
    if status >= 400:
        try:
            err_body = json.loads(body_bytes)
        except Exception:
            err_body = {"detail": body_bytes.decode("utf-8", "ignore")[:300]}
        raise HTTPException(status, err_body.get("message") or err_body.get("detail") or "Supabase error")
    try:
        return status, hdrs, json.loads(body_bytes) if body_bytes else None
    except Exception:
        return status, hdrs, body_bytes


def _supa_total(headers, fallback):
    cr = headers.get("content-range", "")
    if "/" in cr:
        try:
            return int(cr.split("/", 1)[1])
        except Exception:
            pass
    return fallback


def _supa_attach_pks(rows, pk_cols):
    """Add __row_key, __pk_display, __pk_columns to each row so the frontend
    Data Manager can use them for the kebab action buttons."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            out.append(r); continue
        values = [r.get(c) for c in pk_cols]
        # base64url encode similar to PG
        import base64
        key_raw = "/".join("" if v is None else str(v) for v in values).encode()
        r["__row_key"] = base64.urlsafe_b64encode(key_raw).decode().rstrip("=")
        r["__pk_display"] = " / ".join("" if v is None else str(v) for v in values)
        r["__pk_columns"] = list(pk_cols)
        out.append(r)
    return out


@app.get("/api/supa/health")
async def supa_health(user=Depends(require_user)):
    if not _supa_ready():
        return {"configured": False, "ok": False, "detail": "FCC_SUPABASE_URL atau SECRET_KEY belum di-set."}
    try:
        status, _, data = await _supa_http("GET", "fuel_master_jalur",
                                           [("select", "id"), ("limit", "1")],
                                           prefer="count=exact")
        return {"configured": True, "ok": True, "status": status, "sample_rows": len(data or [])}
    except HTTPException as e:
        return {"configured": True, "ok": False, "status": e.status_code, "detail": e.detail}


@app.get("/api/supa/config")
async def supa_config(user=Depends(require_user)):
    return {
        "configured": _supa_ready(),
        "url": SUPA_URL,
        "site_code": SUPA_SITE,
        "field_web_url": SUPA_FIELD_URL,
        "actor_uuid_set": bool(SUPA_ACTOR_UUID),
        "tables": {k: {"pk": v["pk"]} for k, v in SUPA_TABLES.items()},
    }


@app.get("/api/supa/{table}")
async def supa_list(table: str, request: Request, user=Depends(require_user)):
    if table not in SUPA_TABLES:
        raise HTTPException(404, f"Tabel Supabase '{table}' tidak ada di allowlist.")
    meta = SUPA_TABLES[table]
    supa_table = meta["supa"]
    qp = request.query_params
    # Hybrid: SUPER_ADMIN sees all rows (including INACTIVE).
    # Other roles get auto-filter ACTIVE — but only if the table has a 'status' column.
    is_super = user.get("role") == "SUPER_ADMIN"
    if not is_super and not qp.get("status") and not qp.get("q"):
        known = await _supa_known_columns(supa_table)
        if known and "status" in known:
            qp = MultiDict(list(qp.multi_items()) + [("status", "ACTIVE")])
    limit = min(max(int(qp.get("limit", 100)), 1), 500)
    offset = max(int(qp.get("offset", 0)), 0)
    sort = (qp.get("sort") or "created_at").strip()
    dir_ = "desc" if (qp.get("dir") or "desc").lower() == "desc" else "asc"
    query = [("select", "*"), ("order", f"{sort}.{dir_}"), ("limit", limit), ("offset", offset)]
    # Static filters from allowlist (e.g. flowmeter_ft maps to monitoring_type=FLOWMETER)
    for col, val in (meta.get("filter") or {}).items():
        query.append((col, f"eq.{val}"))
    q = (qp.get("q") or "").strip()
    if q and meta.get("search"):
        ors = ",".join(f"{c}.ilike.*{q}*" for c in meta["search"])
        query.append(("or", f"({ors})"))
    if qp.get("status"):
        query.append(("status", f"eq.{qp['status']}"))
    date_col = meta.get("date_col")
    if date_col:
        if qp.get("tanggal_dari"):
            query.append((date_col, f"gte.{qp['tanggal_dari']}"))
        if qp.get("tanggal_sampai"):
            query.append((date_col, f"lte.{qp['tanggal_sampai']}"))
    try:
        status, headers, data = await _supa_http("GET", supa_table, query, prefer="count=exact")
    except HTTPException as e:
        if meta.get("optional") and e.status_code in (404, 400):
            return {"data": [], "total": 0, "limit": limit, "offset": offset, "optional_missing": True,
                    "pk_columns": [meta["pk"]], "source": "supabase", "supa_table": supa_table}
        raise
    rows = data or []
    # Backend-side attach __row_key/__pk_columns using Supabase column names.
    rows = _supa_attach_pks(rows, [meta["pk"]])
    # Resolve FK UUIDs to codes for views
    rows = await _resolve_fks_for_view(table, rows)
    return {
        "data": rows,
        "total": _supa_total(headers, len(rows)),
        "limit": limit,
        "offset": offset,
        "source": "supabase",
        "supa_table": supa_table,
        "pk_columns": [meta["pk"]],
        "sort": sort,
        "dir": dir_,
    }


@app.get("/api/supa/{table}/{rid}")
async def supa_get(table: str, rid: str, user=Depends(require_user)):
    if table not in SUPA_TABLES:
        raise HTTPException(404, f"Tabel Supabase '{table}' tidak ada di allowlist.")
    meta = SUPA_TABLES[table]
    supa_table = meta.get("supa_table") or meta["supa"]
    pk = meta["pk"]
    query = [("select", "*"), (pk, f"eq.{rid}"), ("limit", "1")]
    for col, val in (meta.get("filter") or {}).items():
        query.append((col, f"eq.{val}"))
    status, _, data = await _supa_http("GET", supa_table, query)
    if not data:
        raise HTTPException(404, "Data Supabase tidak ditemukan.")
    rows = _supa_attach_pks(data, [pk])
    return rows[0]


@app.get("/api/supa/report/summary")
async def supa_report_summary(dari: str = "", sampai: str = "", user=Depends(require_user)):
    def q():
        qq = [("select", "*")]
        if dari: qq.append(("tanggal", f"gte.{dari}"))
        if sampai: qq.append(("tanggal", f"lte.{sampai}"))
        qq.append(("order", "created_at.desc"))
        qq.append(("limit", "1000"))
        return qq
    transfers = await _supa_http("GET", "fuel_v_transfer_fuel", q())
    monitoring = await _supa_http("GET", "fuel_v_fuel_truck_monitoring", q())
    tr_rows = transfers[2] or []
    mo_rows = monitoring[2] or []
    active_tr = [r for r in tr_rows if not r.get("voided_at")]
    flow = [r for r in mo_rows if (r.get("monitoring_type") or "").upper() == "FLOWMETER"]
    hm = [r for r in mo_rows if (r.get("monitoring_type") or "").upper() == "HM"]
    return {
        "data": {"transfers": tr_rows, "monitoring": mo_rows},
        "summary": {
            "transfer_count": len(active_tr),
            "total_fm_liter": sum(float(r.get("total_fm_liter") or 0) for r in active_tr),
            "warning": sum(1 for r in active_tr if (r.get("status_deviasi") or "").upper() == "WARNING"),
            "critical": sum(1 for r in active_tr if (r.get("status_deviasi") or "").upper() == "CRITICAL"),
            "flowmeter_count": len(flow),
            "hm_count": len(hm),
        },
        "filters": {"dari": dari, "sampai": sampai},
        "source": "supabase",
    }


@app.post("/api/supa/tera/volume")
async def supa_tera_volume(body: dict, user=Depends(require_user)):
    unit = (body.get("unit_code") or body.get("aset") or "").strip()
    dip = body.get("dip_cm") or body.get("dip")
    if not unit or dip is None:
        raise HTTPException(400, "Butuh unit_code dan dip_cm.")
    try:
        dip = float(dip)
    except Exception:
        raise HTTPException(400, "dip_cm harus angka.")
    status, _, rows = await _supa_http(
        "GET", "fuel_tera_tangki_grid",
        [("select", "volumes_json,dip_min,dip_step,point_count"), ("unit_code", f"eq.{unit}"), ("limit", "1")],
    )
    if not rows:
        return {"unit_code": unit, "dip_cm": dip, "volume_l": None, "detail": "unit_code tidak ditemukan di fuel_tera_tangki_grid"}
    row = rows[0]
    vols = row.get("volumes_json") or []
    dip_min = float(row.get("dip_min") or 0)
    dip_step = float(row.get("dip_step") or 0.1)
    if dip_step <= 0 or not vols:
        return {"unit_code": unit, "dip_cm": dip, "volume_l": None, "detail": "data tera kosong"}
    idx = int(round((dip - dip_min) / dip_step))
    if idx < 0 or idx >= len(vols):
        return {"unit_code": unit, "dip_cm": dip, "volume_l": None,
                "detail": f"dip di luar range {dip_min}–{dip_min + dip_step * (len(vols)-1)}"}
    return {"unit_code": unit, "dip_cm": dip, "volume_l": float(vols[idx]), "source": "supabase"}


@app.get("/api/supa/route-config")
async def supa_route_config(tanggal: str = "", shift: str = "", user=Depends(require_user)):
    qq = [("select", "*"), ("status", "eq.VALIDATED"), ("limit", "100")]
    if tanggal:
        qq.append(("tanggal", f"eq.{tanggal}"))
    if shift:
        qq.append(("shift", f"eq.{shift}"))
    qq.append(("order", "tanggal.desc"))
    try:
        status, _, rows = await _supa_http("GET", "fuel_v_route_config", qq)
    except HTTPException as e:
        if e.status_code in (404, 400):
            return {"data": [], "total": 0, "optional_missing": True}
        raise
    return {"data": rows or [], "total": len(rows or []), "source": "supabase"}


# Fields blocked from POST/PATCH for Supabase (server-managed + computed).
_SUPA_SERVER_OWNED = {
    "fuel_master_jalur":      {"created_at", "updated_at", "id"},
    "fuel_master_tandon":     {"created_at", "updated_at", "id"},
    "fuel_master_fuel_truck": {"created_at", "updated_at", "id"},
    "fuel_fm_awal_settings":  {"created_at", "updated_at", "id"},
    "fuel_tera_tangki_grid":  {"created_at", "updated_at"},
    "fuel_route_config":      {"created_at", "updated_at", "id"},
    "fuel_v_route_config":    set(),
    "fuel_profiles":          {"created_at", "updated_at", "id"},
    "fuel_attachment_log":    {"created_at", "updated_at", "id"},
    "fuel_v_transfer_fuel":   set(),  # view: read-only at DB level
    "fuel_v_fuel_truck_monitoring": set(),
    "fuel_tx_transfer_fuel":  {"created_at", "updated_at", "id"},
    "fuel_tx_fuel_truck_monitoring": {"created_at", "updated_at", "id"},
}

# Backend-side column mapping (FCC frontend name -> Supabase column name).


async def _resolve_fks_for_view(table, rows):
    """Resolve FK UUIDs to human-readable codes for views.
    E.g. jalur_id -> jalur_code, tandon_id -> tandon_code, fuel_truck_id -> unit_code.
    """
    if not rows:
        return rows
    fk_map = {
        "transfer_fuel": {"jalur_id": "jalur_code", "tandon_id": "tandon_code", "fuel_truck_id": "fuel_truck_code"},
        "flowmeter_ft": {"fuel_truck_id": "fuel_truck_code"},
        "hour_meter": {"fuel_truck_id": "fuel_truck_code"},
        "penerimaan_mo": {"jalur_id": "jalur_code", "tandon_id": "tandon_code", "fuel_truck_id": "fuel_truck_code", "ft_mandar_id": "ft_mandar_code"},
        "refuelling": {"vendor_id": "vendor_code", "unit_id": "unit_code"},
        "voucher_bib": {"vendor_id": "vendor_code"},
        "sounding_main_tank": {"tandon_id": "tandon_code"},
    }
    mapping = fk_map.get(table, {})
    if not mapping:
        return rows

    # Collect all UUIDs to fetch
    uuid_to_resolve = {}
    for fk_col, code_col in mapping.items():
        for row in rows:
            uuid_val = row.get(fk_col)
            if uuid_val and uuid_val not in uuid_to_resolve:
                uuid_to_resolve[uuid_val] = code_col

    if not uuid_to_resolve:
        return rows

    # Determine which master table to query for each code_col
    code_to_master = {
        "jalur_code": "fuel_master_jalur",
        "tandon_code": "fuel_master_tandon",
        "fuel_truck_code": "fuel_master_fuel_truck",
        "vendor_code": "fuel_master_vendor",
        "unit_code": "fuel_master_unit",
        "ft_mandar_code": "fuel_master_fuel_truck",
    }

    # Fetch each code column in bulk
    uuid_to_code = {}
    for code_col, master_table in code_to_master.items():
        # Filter UUIDs that need this code
        uuids_for_this = [u for u, c in uuid_to_resolve.items() if c == code_col]
        if not uuids_for_this:
            continue
        # Build query: id IN (...)
        # Use OR with eq.id. multiple times (PostgREST limitation)
        for u in uuids_for_this:
            try:
                req = urllib.request.Request(
                    f"{SUPA_URL}/rest/v1/{master_table}?id=eq.{u}&select={code_col}",
                    headers={"apikey": SUPA_SECRET, "Authorization": f"Bearer {SUPA_SECRET}"}
                )
                loop = asyncio.get_event_loop()
                def _do():
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return json.loads(r.read())
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    rs = await loop.run_in_executor(ex, _do)
                if rs:
                    uuid_to_code[u] = rs[0].get(code_col, "")
            except Exception:
                pass

    # Apply mappings + add code-named fields (UUID still there for FK)
    for row in rows:
        for fk_col, code_col in mapping.items():
            uuid_val = row.get(fk_col)
            if uuid_val and uuid_val in uuid_to_code:
                row[code_col] = uuid_to_code[uuid_val]
            # Also set the bare field name (e.g. 'jalur') to the code value
            # This is what the form expects
            bare_name = fk_col.replace('_id', '')
            if bare_name != fk_col:
                row[bare_name] = row.get(code_col) or uuid_to_code.get(uuid_val, '')
            # Special alias: tandon_id also maps to 'main_tank' field
            if fk_col == 'tandon_id':
                row['main_tank'] = row.get('tandon_code') or uuid_to_code.get(uuid_val, '')
            # Special alias: fuel_truck_id also maps to 'fuel_truck' field
            if fk_col == 'fuel_truck_id':
                row['fuel_truck'] = row.get('fuel_truck_code') or uuid_to_code.get(uuid_val, '')

    return rows


_SUPA_COL_MAP = {
    # Master Jalur
    "kode": "jalur_code", "nama": "jalur_name", "peruntukan": "peruntukan",
    "tujuan": "tujuan", "site": "site_code", "status": "status",
    "urutan": "sort_order",
    # Master Tandon
    "kapasitas_l": "kapasitas_l",
    # Master Fuel Truck
    "jenis": "unit_type",
    # FM Awal
    "fm_awal_manual": "fm_awal_manual",
    "catatan": "notes",
    # Sounding
    "sumber_label": "source_label", "sumber_sheet": "source_sheet", "sumber_file": "source_file",
    "dip_min": "dip_min", "dip_step": "dip_step", "dip_maks": "max_dip",
    "jumlah_titik": "point_count",
    # Identifiers preserved
    "id": "id",
    # Transaction fields
    "petugas": "petugas_name",
    "fuel_truck": "fuel_truck_code",
    "nilai_hm": "hm_value",
    "fm_in": "fm_in",
    "fm_out": "fm_out",
    "shift": "shift",
    "tanggal": "tanggal",
    "petugas_fuelman": "petugas_name",
    "status": "status",
    "kondisi": "notes",
}

# Cache Supabase schema columns per table (populated on first insert/update).
_SUPA_SCHEMA_CACHE = {}


async def _supa_known_columns(supa_table: str):
    """Fetch column list for a Supabase table via /rest/v1 introspection."""
    if supa_table in _SUPA_SCHEMA_CACHE:
        return _SUPA_SCHEMA_CACHE[supa_table]
    if not _supa_ready():
        return None
    # Try fetching one row to read column names (cheaper than RPC).
    try:
        url = f"{SUPA_URL}/rest/v1/{supa_table}?limit=1"
        req = urllib.request.Request(url, headers={"apikey": SUPA_SECRET, "Authorization": f"Bearer {SUPA_SECRET}"})
        loop = asyncio.get_event_loop()
        def _do():
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            body = await loop.run_in_executor(ex, _do)
        data = json.loads(body)
        if isinstance(data, list) and data:
            cols = set(data[0].keys())
        elif isinstance(data, dict):
            cols = set(data.keys())
        else:
            cols = set()
        _SUPA_SCHEMA_CACHE[supa_table] = cols
        return cols
    except Exception:
        return None



async def _resolve_fk_remote(body: dict, supa_url: str, supa_key: str):
    """Resolve FCC-style text refs to Supabase UUIDs. Works on post-COL_MAP state."""
    resolved = dict(body)
    # fuel_truck_id: needs UUID from fuel_truck_code or fuel_truck
    ft_value = resolved.get('fuel_truck_code') or resolved.get('fuel_truck')
    ft_value = str(ft_value) if ft_value else ''
    if ft_value and '-' not in ft_value:
        try:
            req = urllib.request.Request(
                f"{supa_url}/rest/v1/fuel_master_fuel_truck?unit_code=eq.{ft_value}&select=id&limit=1",
                headers={"apikey": supa_key, "Authorization": f"Bearer {supa_key}"}
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                rows = json.loads(r.read())
                if rows:
                    resolved['fuel_truck_id'] = rows[0]['id']
        except Exception:
            pass
    # Clean up text fields (not in underlying table)
    resolved.pop('fuel_truck', None)
    resolved.pop('fuel_truck_code', None)
    return resolved



_TABLE_COL_MAP = {
    # Master Jalur
    "master_jalur": {"kode": "jalur_code", "nama": "jalur_name", "tujuan": "tujuan", "peruntukan": "jalur_type", "status": "status"},
    # Master Main Tank
    "master_main_tank": {"kode": "tandon_code", "nama": "tandon_name", "kapasitas_l": "kapasitas_l", "status": "status"},
    # Master Fuel Truck
    "master_fuel_truck": {"kode": "unit_code", "nama": "unit_name", "tipe": "unit_type", "kapasitas_l": "kapasitas_l", "status": "status", "jenis": "unit_type"},
    # Master Vendor
    "master_vendor": {"kode": "vendor_code", "nama": "vendor_name", "kategori": "kategori", "status": "status"},
    # Transfer Fuel
    "transfer_fuel": {"kode": "kode", "tanggal": "tanggal", "shift": "shift", "petugas": "petugas_name", "fm_awal": "fm_awal", "fm_akhir": "fm_akhir", "sounding_awal_cm": "sounding_awal_cm", "sounding_akhir_cm": "sounding_akhir_cm", "volume_awal_l": "volume_awal_l", "volume_akhir_l": "volume_akhir_l", "catatan": "notes", "status": "status", "main_tank": "main_tank_code", "fuel_truck": "fuel_truck_code", "jalur": "jalur_code", "vendor_kode": "vendor_code"},
    # Flowmeter FT
    "flowmeter_ft": {"kode": "kode", "tanggal": "tanggal", "shift": "shift", "petugas": "petugas_name", "fm_in": "fm_in", "fm_out": "fm_out", "catatan": "notes", "fuel_truck": "fuel_truck_code"},
    # Hour Meter
    "hour_meter": {"kode": "kode", "tanggal": "tanggal", "shift": "shift", "petugas": "petugas_name", "hm_value": "hm_value", "kondisi": "notes", "fuel_truck": "fuel_truck_code"},
    # Sounding Main Tank
    "sounding_main_tank": {"kode": "kode", "tanggal": "tanggal", "shift": "shift", "petugas": "petugas_name", "intank_cm": "intank_cm", "aktual_cm": "aktual_cm", "main_tank": "main_tank_code"},
    # Pengurasan
    "pengurasan": {"kode": "kode", "tanggal": "tanggal", "shift": "shift", "petugas": "petugas_name", "jenis_aset": "asset_type", "aset": "asset_code", "volume_awal_l": "volume_awal_l", "volume_akhir_l": "volume_akhir_l"},
    # Penerimaan MO
    "penerimaan_mo": {"kode": "kode", "tanggal": "tanggal", "shift": "shift", "petugas": "petugas_name", "vendor_kode": "vendor_code", "volume_l": "volume_l", "jalur": "jalur_code"},
    # Refuelling
    "refuelling": {"kode": "kode", "tanggal": "tanggal", "shift": "shift", "petugas": "petugas_name", "vendor_kode": "vendor_code", "unit_kode": "unit_code", "volume_l": "volume_l", "no_voucher": "no_voucher", "fuel_truck": "fuel_truck_code"},
    # Voucher BIB
    "voucher_bib": {"kode": "kode", "tanggal": "tanggal", "shift": "shift", "vendor_kode": "vendor_code", "no_voucher": "no_voucher", "volume_l": "volume_l"},
}


async def _supa_clean(table: str, body: dict, insert: bool):
    """Strip server-owned fields AND columns that don't exist in Supabase live schema.

    For write ops (insert=True), use 'supa_table' (the underlying real table) if
    specified. Don't apply schema-cache filter for writes since cache may be stale.
    """
    meta = SUPA_TABLES.get(table, {})
    # For writes, prefer underlying table (supa_table) over view
    supa_table = meta.get("supa_table") or meta.get("supa")
    if not supa_table:
        raise HTTPException(404, f"Tabel '{table}' tidak ada di allowlist Supabase.")
    owned = _SUPA_SERVER_OWNED.get(supa_table, {"created_at", "updated_at", "id"})
    cleaned = {}
    # For writes, skip the schema-cache filter entirely to avoid stale-cache issues.
    # Supabase will reject columns that don't exist.
    known = None
    table_map = _TABLE_COL_MAP.get(table, {})
    for k, v in (body or {}).items():
        if k in owned: continue
        if v is None: continue
        # Per-table mapping first, then global fallback
        real_key = table_map.get(k) or _SUPA_COL_MAP.get(k, k)
        if known is not None and real_key not in known:
            continue  # Column doesn't exist in Supabase; skip silently
        cleaned[real_key] = v
    if insert and "site_code" not in cleaned and "site_code" not in owned:
        cleaned.setdefault("site_code", SUPA_SITE)
    # created_by must be a UUID from fuel_profiles. Use a hard-coded SUPER_ADMIN profile id
    # or look up by username (cached).
    _SUPER_ADMIN_PROFILE_ID = "6a5cf14c-100f-4a23-b3c3-90942188eab9"  # fadli raihan SUPER_ADMIN
    if insert and supa_table == "fuel_tx_fuel_truck_monitoring":
        cleaned.setdefault("created_by", _SUPER_ADMIN_PROFILE_ID)
    # Remove irrelevant fields for monitoring tables
    if supa_table == "fuel_tx_fuel_truck_monitoring":
        cleaned.pop("jalur_code", None)
        cleaned.pop("jalur", None)
        cleaned.pop("kapasitas_l", None)
        cleaned.pop("kode", None)
        cleaned.pop("status", None)
        cleaned.pop("catatan", None)  # this maps to notes; let it through if mapped
        # Actually catatan -> notes, keep notes
        cleaned["notes"] = cleaned.pop("catatan", cleaned.get("notes", "")) or ""
    if supa_table == "fuel_tx_transfer_fuel":
        cleaned.pop("kode", None)
    # Strip photo_* fields (handled separately via /api/evidence/upload)
    if supa_table in ("fuel_tx_transfer_fuel", "fuel_tx_fuel_truck_monitoring"):
        for k in list(cleaned.keys()):
            if k.startswith('foto_') or k.endswith('_photo') or k == 'photo_url':
                cleaned.pop(k)
    return cleaned, supa_table


@app.post("/api/supa/{table}")
async def supa_create(table: str, request: Request, user=Depends(require_user)):
    """Insert into Supabase. SUPER_ADMIN only."""
    if user["role"] != "SUPER_ADMIN":
        raise HTTPException(403, "Hanya SUPER_ADMIN boleh insert ke Supabase dari dashboard.")
    if table not in SUPA_TABLES:
        raise HTTPException(404, f"Tabel '{table}' tidak ada di allowlist Supabase.")
    cleaned, supa_table = await _supa_clean(table, await request.json(), insert=True)
    # Resolve FK references
    cleaned = await _resolve_fk_remote(cleaned, SUPA_URL, SUPA_SECRET)
    if not cleaned:
        raise HTTPException(400, "Payload kosong setelah filter server-owned fields.")
    # For monitoring tables, set monitoring_type
    if table in ('flowmeter_ft', 'hour_meter'):
        cleaned['monitoring_type'] = 'FLOWMETER' if table == 'flowmeter_ft' else 'HM'
    try:
        status, _, data = await _supa_http(
            "POST", supa_table, body=cleaned,
            prefer="return=representation"
        )
    except HTTPException:
        raise
    if not data:
        raise HTTPException(500, "Insert Supabase tidak mengembalikan row.")
    return data[0] if isinstance(data, list) else data


@app.patch("/api/supa/{table}/{rid}")
async def supa_patch(table: str, rid: str, request: Request, user=Depends(require_user)):
    """Update Supabase row by primary key. SUPER_ADMIN only."""
    if user["role"] != "SUPER_ADMIN":
        raise HTTPException(403, "Hanya SUPER_ADMIN boleh update Supabase dari dashboard.")
    if table not in SUPA_TABLES:
        raise HTTPException(404, f"Tabel '{table}' tidak ada di allowlist Supabase.")
    meta = SUPA_TABLES[table]
    supa_table = meta.get("supa_table") or meta["supa"]
    pk = meta["pk"]
    # For monitoring tables, write to underlying table (views are read-only)
    meta = SUPA_TABLES[table]
    supa_table = meta.get("supa_table") or meta["supa"]
    cleaned, _ = await _supa_clean(table, await request.json(), insert=False)
    # Override cleaned to strip irrelevant fields for monitoring tables
    if not cleaned:
        raise HTTPException(400, "Payload kosong.")
    # Decode __row_key (base64url) back to raw PK value.
    import base64
    try:
        pad = "=" * (-len(rid) % 4)
        raw = base64.urlsafe_b64decode(rid + pad).decode("utf-8", "ignore")
        # raw is "val1/val2/..." or just "val1" for single PK
        rid_value = raw.split("/")[-1] if "/" in raw else raw
    except Exception:
        rid_value = rid  # fallback to raw
    query = [(pk, f"eq.{rid_value}")]
    for col, val in (meta.get("filter") or {}).items():
        query.append((col, f"eq.{val}"))
    try:
        status, _, data = await _supa_http(
            "PATCH", supa_table, query, body=cleaned,
            prefer="return=representation"
        )
    except HTTPException:
        raise
    if not data:
        raise HTTPException(404, "Data tidak ditemukan atau tidak berubah.")
    return data[0] if isinstance(data, list) else data


@app.delete("/api/supa/{table}/{rid}")
async def supa_delete(table: str, rid: str, user=Depends(require_user)):
    """Delete Supabase row by primary key. SUPER_ADMIN only.

    If FK constraints block hard delete, fall back to soft-delete
    (status=INACTIVE) and return a 409 with `soft_deleted: true`
    so the frontend can show an informative message.
    """
    if user["role"] != "SUPER_ADMIN":
        raise HTTPException(403, "Hanya SUPER_ADMIN boleh delete di Supabase dari dashboard.")
    if table not in SUPA_TABLES:
        raise HTTPException(404, f"Tabel '{table}' tidak ada di allowlist Supabase.")
    meta = SUPA_TABLES[table]
    supa_table = meta.get("supa_table") or meta["supa"]
    pk = meta["pk"]
    import base64
    try:
        pad = "=" * (-len(rid) % 4)
        raw = base64.urlsafe_b64decode(rid + pad).decode("utf-8", "ignore")
        rid_value = raw.split("/")[-1] if "/" in raw else raw
    except Exception:
        rid_value = rid
    query = [(pk, f"eq.{rid_value}")]
    for col, val in (meta.get("filter") or {}).items():
        query.append((col, f"eq.{val}"))
    # Try hard delete first
    try:
        status, _, data = await _supa_http(
            "DELETE", supa_table, query,
            prefer="return=representation"
        )
    except HTTPException as e:
        # FK violation etc. → fallback to soft-delete
        if e.status_code not in (409, 400):
            raise
        # Fall through to soft-delete below
        data = None
    if data:
        return {"deleted": True, "row_key": rid, "data": data, "soft_deleted": False}
    # Soft-delete: set status=INACTIVE
    if "status" in (await _supa_known_columns(supa_table) or set()):
        patch_q = [(pk, f"eq.{rid_value}")]
        for col, val in (meta.get("filter") or {}).items():
            patch_q.append((col, f"eq.{val}"))
        try:
            pstatus, _, pdata = await _supa_http(
                "PATCH", supa_table, patch_q, body={"status": "INACTIVE"},
                prefer="return=representation"
            )
            if pdata:
                return {
                    "deleted": False,
                    "soft_deleted": True,
                    "row_key": rid,
                    "data": pdata,
                    "reason": "Data dipakai sebagai referensi (FK). Dialihkan ke nonaktif (status=INACTIVE)."
                }
        except Exception:
            pass
    raise HTTPException(404, "Data tidak ditemukan atau constraint memblokir delete.")


@app.get("/api/{table}")
async def list_rows(table: str, request: Request, user=Depends(require_user)):
    require_perm(user, table, "read")
    qp = request.query_params
    try:
        limit = max(1, min(int(qp.get("limit", 100)), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(qp.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0

    col_meta = await _table_columns(table)
    if not col_meta:
        raise HTTPException(404, f"Tabel {table} tidak ditemukan.")
    type_map = dict(col_meta)
    excluded = PUBLIC_EXCLUDE.get(table, set())
    public_cols = [c for c, _ in col_meta if c not in excluded]
    have = set(type_map)
    pk_cols = await _effective_pk_cols(table, type_map)

    requested_sort = qp.get("sort") or "created_at"
    if requested_sort not in public_cols:
        requested_sort = next((c for c in ("created_at", "tanggal", "kode", "nama", "id") if c in public_cols), public_cols[0])
    dir_ = "DESC" if qp.get("dir", "desc").lower() == "desc" else "ASC"

    where = []
    params = []
    def addp(v):
        params.append(v)
        return f"${len(params)}"

    q = (qp.get("q") or "").strip()
    if q:
        i = addp(f"%{q}%")
        searchable = [c for c, dtype in col_meta
                      if c not in excluded and _normalize_type(dtype) not in {"bytea"}]
        if searchable:
            where.append("(" + " OR ".join(f"CAST({_qident(c)} AS text) ILIKE {i}" for c in searchable) + ")")
    if (status := qp.get("status")) and "status" in have:
        i = addp(status)
        where.append(f"{_qident('status')} = {i}")
    if (d := qp.get("tanggal_dari")) and "tanggal" in have:
        try:
            d = dt.date.fromisoformat(d)
        except ValueError:
            pass
        i = addp(d)
        where.append(f"{_qident('tanggal')} >= {i}")
    if (e := qp.get("tanggal_sampai")) and "tanggal" in have:
        try:
            e = dt.date.fromisoformat(e)
        except ValueError:
            pass
        i = addp(e)
        where.append(f"{_qident('tanggal')} <= {i}")
    if (u := qp.get("unit")) and "unit_kode" in have:
        i = addp(u)
        where.append(f"{_qident('unit_kode')} = {i}")
    for qk, dbcol in (("sumber", "sumber"), ("batch_id", "batch_id"),
                      ("vendor_kode", "vendor_kode"), ("unit_standar", "unit_standar"),
                      ("tanggal", "tanggal"), ("shift", "shift")):
        if qk not in qp or dbcol not in have:
            continue
        value = qp[qk]
        try:
            value = _coerce_value(value, type_map[dbcol])
        except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError):
            raise HTTPException(400, f"Filter {qk} tidak valid.")
        i = addp(value)
        where.append(f"{_qident(dbcol)} = {i}")

    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    select_list = ", ".join(_qident(c) for c in public_cols)
    sql = (f"SELECT {select_list} FROM fcc.{_qident(_db_table(table))} {wsql} "
           f"ORDER BY {_qident(requested_sort)} {dir_} LIMIT {limit} OFFSET {offset}")
    cnt_sql = f"SELECT count(*) FROM fcc.{_qident(_db_table(table))} {wsql}"

    async with pool.acquire() as conn:
        async with conn.transaction():
            await with_user_session(conn, user)
            try:
                rows = await conn.fetch(sql, *params)
                total = await conn.fetchval(cnt_sql, *params)
                status_options = []
                status_counts = []
                if "status" in have:
                    status_options = [r["status"] for r in await conn.fetch(
                        f"SELECT DISTINCT CAST({_qident('status')} AS text) AS status "
                        f"FROM fcc.{_qident(_db_table(table))} WHERE {_qident('status')} IS NOT NULL ORDER BY 1")]
                    status_counts = [dict(r) for r in await conn.fetch(
                        f"SELECT CAST({_qident('status')} AS text) AS status, count(*) AS n "
                        f"FROM fcc.{_qident(_db_table(table))} {wsql} GROUP BY {_qident('status')} ORDER BY 1", *params)]
            except asyncpg.exceptions.PostgresError as exc:
                return JSONResponse(translate_pg_error(exc), status_code=400)

    return {
        "data": [_decorate_row(r, pk_cols, table) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": requested_sort,
        "dir": dir_.lower(),
        "pk_columns": [c for c, _ in pk_cols],
        "status_options": status_options,
        "status_counts": status_counts,
    }


@app.get("/api/{table}/{rid}")
async def get_row(table: str, rid: str, user=Depends(require_user)):
    require_perm(user, table, "read")
    types = await _table_type_map(table)
    pk_cols = await _effective_pk_cols(table, types)
    vals = _decode_row_key(rid, pk_cols)
    where = " AND ".join(f"{_qident(c)} = ${i+1}" for i, (c, _) in enumerate(pk_cols))
    excluded = PUBLIC_EXCLUDE.get(table, set())
    select_cols = [c for c in types if c not in excluded]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await with_user_session(conn, user)
            row = await conn.fetchrow(
                f"SELECT {', '.join(_qident(c) for c in select_cols)} FROM fcc.{_qident(_db_table(table))} WHERE {where}", *vals)
    if not row:
        raise HTTPException(404, "Tidak ditemukan.")
    return _decorate_row(row, pk_cols, table)


@app.post("/api/{table}")
async def create_row(table: str, request: Request, user=Depends(require_user)):
    require_perm(user, table, "write")
    body = strip_generated(table, await request.json())
    types = await _table_type_map(table)
    cols = [c for c in body if c in types and c not in PUBLIC_EXCLUDE.get(table, set())]
    try:
        vals = [_coerce_value(body[c], types[c]) for c in cols]
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"Format nilai tidak valid: {exc}")
    if cols:
        placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
        sql = (f"INSERT INTO fcc.{_qident(_db_table(table))} ({', '.join(_qident(c) for c in cols)}) "
               f"VALUES ({placeholders}) RETURNING *")
    else:
        sql = f"INSERT INTO fcc.{_qident(_db_table(table))} DEFAULT VALUES RETURNING *"
    async with pool.acquire() as conn:
        async with conn.transaction():
            await with_user_session(conn, user)
            try:
                row = await conn.fetchrow(sql, *vals)
            except asyncpg.exceptions.PostgresError as exc:
                return JSONResponse(translate_pg_error(exc, table, types), status_code=400)
    pk_cols = await _effective_pk_cols(table, types)
    return _decorate_row(row, pk_cols, table)


@app.patch("/api/{table}/{rid}")
async def patch_row(table: str, rid: str, request: Request, user=Depends(require_user)):
    require_perm(user, table, "write")
    body = strip_generated(table, await request.json())
    types = await _table_type_map(table)
    pk_cols = await _effective_pk_cols(table, types)
    old_pk_vals = _decode_row_key(rid, pk_cols)

    update_cols = [c for c in body if c in types and c not in PUBLIC_EXCLUDE.get(table, set())]
    if not update_cols:
        raise HTTPException(400, "Tidak ada kolom valid yang berubah.")
    try:
        update_vals = [_coerce_value(body[c], types[c]) for c in update_cols]
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"Format nilai tidak valid: {exc}")

    sets = ", ".join(f"{_qident(c)} = ${i+1}" for i, c in enumerate(update_cols))
    where = " AND ".join(
        f"{_qident(c)} = ${len(update_vals)+i+1}" for i, (c, _) in enumerate(pk_cols))
    sql = f"UPDATE fcc.{_qident(_db_table(table))} SET {sets} WHERE {where} RETURNING *"
    params = update_vals + old_pk_vals

    async with pool.acquire() as conn:
        async with conn.transaction():
            await with_user_session(conn, user)
            try:
                row = await conn.fetchrow(sql, *params)
            except asyncpg.exceptions.PostgresError as exc:
                return JSONResponse(translate_pg_error(exc, table, types), status_code=400)
    if not row:
        raise HTTPException(404, "Tidak ditemukan atau sudah berubah oleh pengguna lain.")
    return _decorate_row(row, pk_cols, table)


@app.delete("/api/{table}/{rid}")
async def delete_row(table: str, rid: str, user=Depends(require_user)):
    if user["role"] != "SUPER_ADMIN":
        raise HTTPException(403, "Hanya SUPER_ADMIN boleh menghapus permanen.")
    if table in {"audit_trail", "v_closing_line", "v_rekonsiliasi", "v_pengurasan", "sounding_table", "closing_line"}:
        raise HTTPException(403, f"Tabel {table} read-only — tidak boleh dihapus.")
    types = await _table_type_map(table)
    pk_cols = await _effective_pk_cols(table, types)
    vals = _decode_row_key(rid, pk_cols)
    where = " AND ".join(f"{_qident(c)} = ${i+1}" for i, (c, _) in enumerate(pk_cols))

    async with pool.acquire() as conn:
        async with conn.transaction():
            await with_user_session(conn, user)
            try:
                res = await conn.execute(f"DELETE FROM fcc.{_qident(_db_table(table))} WHERE {where}", *vals)
            except asyncpg.exceptions.ForeignKeyViolationError as exc:
                tbl_match = re.search(r'foreign key constraint "([^"]+)"', str(exc))
                tab_match = re.search(r'table "([^"]+)"', str(exc))
                constraint = tbl_match.group(1) if tbl_match else "unknown"
                referencer = tab_match.group(1) if tab_match else "tabel lain"
                raise HTTPException(
                    409,
                    f"Tidak bisa hapus: data ini masih direferensikan oleh {referencer} "
                    f"(constraint {constraint}). Hapus atau ubah data dependent dulu."
                )
            except asyncpg.exceptions.PostgresError as exc:
                return JSONResponse(translate_pg_error(exc, table, types), status_code=409)
    if not res.endswith(" 1"):
        raise HTTPException(404, "Data tidak ditemukan atau sudah dihapus.")
    return {"deleted": True, "row_key": rid}

# ---------------------------------------------------------------------------
# Closing stock (header + lines) — transaksi tunggal
# ---------------------------------------------------------------------------
@app.put("/api/closing/{tanggal}/{shift}")
async def put_closing(tanggal: str, shift: str, request: Request, user=Depends(require_user)):
    require_perm(user, "closing_stock", "write")
    body = await request.json()
    header = body.get("header") or {}
    lines  = body.get("lines") or []

    async with pool.acquire() as conn:
        async with conn.transaction():
            await with_user_session(conn, user)
            # Reject if CLOSED
            existing = await conn.fetchrow(
                "SELECT id, status FROM fcc.closing_stock WHERE tanggal=$1::date AND shift=$2",
                tanggal, shift)
            if existing and existing["status"] == "CLOSED":
                raise HTTPException(409, "Closing sudah CLOSED. Tidak boleh diubah.")
            if existing:
                hid = existing["id"]
                await conn.execute("""
                    UPDATE fcc.closing_stock SET penanggung_jawab=$1, status=$2
                    WHERE id=$3
                """, header.get("penanggung_jawab","-"), header.get("status","DRAFT"), hid)
            else:
                hid = await conn.fetchval("""
                    INSERT INTO fcc.closing_stock (tanggal, shift, penanggung_jawab, status)
                    VALUES ($1::date, $2, $3, $4) RETURNING id
                """, tanggal, shift, header.get("penanggung_jawab","-"), header.get("status","DRAFT"))
            # Wipe and re-insert lines (simpler than diffing for prototype)
            await conn.execute("DELETE FROM fcc.closing_stock_line WHERE closing_id=$1", hid)
            for ln in lines:
                await conn.execute("""
                    INSERT INTO fcc.closing_stock_line
                    (closing_id, aset, jenis, stock_awal_l, penerimaan_l, transfer_masuk_l,
                     transfer_keluar_l, refuelling_l, sounding_aktual_cm, aktual_l)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """, hid, ln["aset"], ln["jenis"],
                     ln.get("stock_awal_l",0), ln.get("penerimaan_l",0),
                     ln.get("transfer_masuk_l",0), ln.get("transfer_keluar_l",0),
                     ln.get("refuelling_l",0),
                     ln.get("sounding_aktual_cm"), ln.get("aktual_l"))
    return {"ok": True, "closing_id": hid}


# ============================================================================
# SS6 IFCU Integration — fetch refuelling data from ppa-bib.net
# ============================================================================
# Login: POST https://ppa-bib.net/auth with p_nrp & p_password
# Fetch: GET https://ppa-bib.net/operation/export_ifcu/{date_from}/{date_to}/{shift}
# Returns XLS file with refuelling data

SS6_BASE_URL = "https://ppa-bib.net"
SS6_AUTH_PATH = "/auth"
SS6_EXPORT_PATH = "/operation/export_ifcu/{date_from}/{date_to}/{shift}"

# Cache session per username to avoid re-login
_ss6_session_cache = {}  # {username: (session_cookie, expires_at)}
_ss6_cache_ttl = 7200  # 2 hours (matches SS6 session timeout)

def _parse_xls_to_rows(xls_bytes: bytes) -> list:
    """Parse XLS file bytes to list of dicts.
    Use libreoffice to convert to XLSX first, then openpyxl."""
    import subprocess, tempfile, os
    with tempfile.NamedTemporaryFile(suffix='.xls', delete=False) as f_in:
        f_in.write(xls_bytes)
        xls_path = f_in.name
    xlsx_path = xls_path.replace('.xls', '.xlsx')
    try:
        # Convert XLS to XLSX
        subprocess.run(['soffice', '--headless', '--convert-to', 'xlsx', '--outdir',
                        os.path.dirname(xls_path), xls_path], capture_output=True, timeout=60)
        if not os.path.exists(xlsx_path):
            return []
        # Parse XLSX
        import openpyxl
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        rows = []
        for ws in wb.worksheets:
            headers = None
            for i, row in enumerate(ws.iter_rows(values_only=True), 1):
                if i == 1:
                    headers = [str(c) if c is not None else f'col_{j}' for j, c in enumerate(row)]
                    continue
                if all(c is None for c in row):
                    continue
                rec = {}
                for j, c in enumerate(row):
                    key = headers[j] if j < len(headers) else f'col_{j}'
                    rec[key] = c
                rows.append(rec)
        return rows
    finally:
        try: os.unlink(xls_path)
        except: pass
        try: os.unlink(xlsx_path)
        except: pass

def _normalize_ss6_row(row: dict) -> dict:
    """Normalize SS6 row to our standard format matching SS6_REFUEL_SAMPLE in dashboard."""
    # Date format: "07.08.2026" → "2026-08-07"
    raw_date = str(row.get('Date') or '')
    date_iso = ''
    if raw_date and '.' in raw_date:
        parts = raw_date.split('.')
        if len(parts) == 3:
            date_iso = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    # Vol: "45,0" → 45.0
    raw_vol = str(row.get('Vol') or '0').replace(',', '.').replace(' ', '')
    try: vol = float(raw_vol)
    except: vol = 0
    # HM: can be empty string
    raw_hm = str(row.get('HM') or '').strip()
    try:
        if raw_hm and raw_hm.lower() != 'none':
            hm = float(raw_hm)
        else:
            hm = None
    except:
        hm = None
    # Shift: int 1/2
    try: shift = int(row.get('Shift') or 1)
    except: shift = 1
    return {
        'id': str(row.get('Transaction ID') or ''),
        'unit': str(row.get('Unit') or ''),
        'material': str(row.get('Material') or ''),
        'date': date_iso,
        'shift': shift,
        'time': str(row.get('Time') or ''),
        'vol': vol,
        'hm': hm if hm is not None else '',
        'gasStation': str(row.get('Gas Station') or ''),
        'location': str(row.get('Location') or ''),
        'fm': str(row.get('FM') or ''),
        'inputBy': str(row.get('Input By') or ''),
        'created': str(row.get('Created') or ''),
        'createdTime': str(row.get('Time') or '')
    }

def _get_ss6_session(username: str, password: str) -> Optional[str]:
    """Login to SS6, return pa_sessions cookie value. Cache for 2 hours."""
    import time as _time
    now = _time.time()
    cached = _ss6_session_cache.get(username)
    if cached and cached[1] > now:
        return cached[0]
    # Login
    try:
        data = urllib.parse.urlencode({'p_nrp': username, 'p_password': password}).encode()
        req = urllib.request.Request(
            f"{SS6_BASE_URL}{SS6_AUTH_PATH}",
            data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'Mozilla/5.0'},
            method='POST'
        )
        # Use a CookieJar to handle redirects
        import http.cookiejar
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPRedirectHandler()
        )
        resp = opener.open(req, timeout=30)
        # After login, we get pa_sessions cookie
        session_cookie = None
        for cookie in cj:
            if cookie.name == 'pa_sessions':
                session_cookie = cookie.value
                break
        if not session_cookie:
            return None
        _ss6_session_cache[username] = (session_cookie, now + _ss6_cache_ttl)
        return session_cookie
    except Exception as e:
        print(f"SS6 login error: {e}", flush=True)
        return None


@app.get("/api/refuelling/ss6/fetch")
async def ss6_fetch(date_from: str, date_to: str, shift: str = "All",
                    ss6_nrp: str = "", ss6_pwd: str = "",
                    user=Depends(require_user)):
    """Fetch refuelling data from SS6 IFCU.
    Args:
        date_from: YYYY-MM-DD
        date_to: YYYY-MM-DD
        shift: 1, 2, or All
        ss6_nrp: (optional) NRP for SS6 login, override VPS user
        ss6_pwd: (optional) password for SS6 login
    Returns: list of SS6 refuelling rows normalized to dashboard format
    """
    # Validate date format
    try:
        for d in [date_from, date_to]:
            dt.date.fromisoformat(d)
    except ValueError:
        raise HTTPException(400, f"Invalid date format. Use YYYY-MM-DD.")
    if shift not in ('1', '2', 'All'):
        raise HTTPException(400, "shift must be 1, 2, or All")

    # Determine SS6 NRP: explicit param > VPS user
    if not ss6_nrp:
        ss6_nrp = user.get('username') or user.get('nrp')
    if not ss6_nrp:
        raise HTTPException(401, "Tidak ada NRP SS6. Set ?ss6_nrp=... atau login VPS dengan NRP yang valid.")

    # Get password: explicit param > env var
    if not ss6_pwd:
        ss6_pwd = os.environ.get(f'FCC_SS6_PWD_{ss6_nrp.upper()}') or os.environ.get('FCC_SS6_DEFAULT_PWD')
    if not ss6_pwd:
        raise HTTPException(503,
            f"SS6 password not configured. Set ?ss6_pwd=... atau env FCC_SS6_PWD_{ss6_nrp.upper()} atau FCC_SS6_DEFAULT_PWD")

    # Login to SS6
    session = _get_ss6_session(ss6_nrp, ss6_pwd)
    if not session:
        raise HTTPException(502, "SS6 login gagal. Periksa kredensial.")

    # Fetch XLS
    url = f"{SS6_BASE_URL}{SS6_EXPORT_PATH.format(date_from=date_from, date_to=date_to, shift=shift)}"
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Cookie': f'pa_sessions={session}'
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            xls_bytes = resp.read()
    except Exception as e:
        raise HTTPException(502, f"SS6 fetch gagal: {e}")

    if not xls_bytes or len(xls_bytes) < 100:
        raise HTTPException(502, "SS6 response kosong atau invalid")

    # Parse XLS
    raw_rows = _parse_xls_to_rows(xls_bytes)
    if not raw_rows:
        return {"ok": True, "rows": [], "count": 0, "source": "ss6_ifcu",
                "date_from": date_from, "date_to": date_to, "shift": shift}

    # Normalize & filter by shift if needed
    rows = [_normalize_ss6_row(r) for r in raw_rows]
    if shift in ('1', '2'):
        rows = [r for r in rows if r['shift'] == int(shift)]

    # Stats
    total_vol = sum(r['vol'] for r in rows)
    unique_units = len(set(r['unit'] for r in rows if r['unit']))
    unique_stations = len(set(r['gasStation'] for r in rows if r['gasStation']))
    fuelmen = len(set(r['fm'] for r in rows if r['fm']))

    return {
        "ok": True,
        "rows": rows,
        "count": len(rows),
        "total_vol": total_vol,
        "unique_units": unique_units,
        "unique_stations": unique_stations,
        "fuelmen": fuelmen,
        "source": "ss6_ifcu",
        "date_from": date_from,
        "date_to": date_to,
        "shift": shift,
    }


if __name__ == "__main__":
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")