"""
Fuel Bridge — local Postgres `fcc.fuel_*` ↔ Web Lapangan (formerly Supabase)
===========================================================================
Module terpisah yang di-include ke server_v8_pg.py.
Menyediakan endpoint `/api/fuel/*` dengan API shape yang kompatibel dengan
Supabase client (sb.from('table').select/insert/update/delete/upsert) +
RPC + storage upload.

Tidak mengganggu endpoint existing (`/api/supa/*`, `/api/{table}`, dll).
"""
from fastapi import APIRouter, Request, Response, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import JSONResponse
import asyncpg, json, base64, hashlib, secrets, os, datetime as dt
from typing import Any, Optional

# Tabel yang dipakai app.js lapangan (mapping Supabase → Postgres lokal)
FUEL_TABLES = {
    "fuel_master_jalur":            {"pk": "id", "schema": "fuel_master_jalur"},
    "fuel_master_tandon":           {"pk": "id", "schema": "fuel_master_tandon"},
    "fuel_master_fuel_truck":       {"pk": "id", "schema": "fuel_master_fuel_truck"},
    "fuel_fm_awal_settings":        {"pk": "id", "schema": "fuel_fm_awal_settings"},
    "fuel_tera_tangki_grid":        {"pk": "id", "schema": "fuel_tera_tangki_grid"},
    "fuel_tx_transfer_fuel":        {"pk": "id", "schema": "fuel_tx_transfer_fuel"},
    "fuel_tx_fuel_truck_monitoring":{"pk": "id", "schema": "fuel_tx_fuel_truck_monitoring"},
    "fuel_attachment_log":          {"pk": "id", "schema": "fuel_attachment_log"},
    "fuel_profiles":                {"pk": "id", "schema": "fuel_profiles"},
    "shift_route_config":           {"pk": "id", "schema": "shift_route_config"},
    "app_user":                     {"pk": "id", "schema": "app_user"},
    "cleanliness":                  {"pk": "id", "schema": "cleanliness"},
    # Views (read-only)
    "fuel_v_transfer_fuel":         {"pk": "id", "schema": "fuel_v_transfer_fuel", "view": True},
    "fuel_v_fuel_truck_monitoring": {"pk": "id", "schema": "fuel_v_fuel_truck_monitoring", "view": True},
    "fuel_v_route_config":          {"pk": "id", "schema": "fuel_v_route_config", "view": True},

    # ---------------------------------------------------------------------
    # Aliases — legacy PG table names without fuel_ prefix.
    # Frontend (fcc-field/index.html) uses these names for local PG tables
    # that exist alongside the Supabase fuel_* tables. They map to the SAME
    # underlying schemas (legacy PG tables, no fuel_ prefix in column names).
    # ---------------------------------------------------------------------
    "master_jalur":      {"pk": "kode", "schema": "master_jalur"},
    "master_main_tank":  {"pk": "kode", "schema": "master_main_tank"},
    "master_fuel_truck": {"pk": "kode", "schema": "master_fuel_truck"},
    "master_unit":       {"pk": "kode", "schema": "master_unit"},
    "master_vendor":     {"pk": "kode", "schema": "master_vendor"},
    "master_jalur_v":    {"pk": "kode", "schema": "v_master_jalur"},
    "ft_mandar_ocean":   {"pk": "id_ft", "schema": "ft_mandar_ocean"},
    "unit_alias":        {"pk": "unit_standar", "schema": "unit_alias"},
    "shift_route_config":{"pk": "kode", "schema": "shift_route_config"},
    "sounding_table":    {"pk": "id", "schema": "sounding_table"},
    "penerimaan_mo":     {"pk": "id", "schema": "penerimaan_mo"},
    "transfer_fuel":     {"pk": "kode", "schema": "transfer_fuel"},
    "flowmeter_ft":      {"pk": "kode", "schema": "flowmeter_ft"},
    "hour_meter":        {"pk": "kode", "schema": "hour_meter"},
    "pengurasan":        {"pk": "kode", "schema": "pengurasan"},
    "sounding_main_tank":{"pk": "kode", "schema": "sounding_main_tank"},
    "cleanliness":       {"pk": "id", "schema": "cleanliness"},
    "refuelling":        {"pk": "no_voucher", "schema": "refuelling"},
    "voucher_bib":       {"pk": "no_voucher", "schema": "voucher_bib"},
    "closing_stock":     {"pk": "aset", "schema": "closing_stock"},
    "closing_stock_line":{"pk": "id", "schema": "closing_stock_line"},
    "fuel_attachment_log":{"pk": "id", "schema": "fuel_attachment_log"},
    "ref_lookup":        {"pk": "kode", "schema": "ref_lookup"},
    "fuel_import_row":   {"pk": "id", "schema": "fuel_import_row"},
    "import_batch":      {"pk": "id", "schema": "import_batch"},
    "audit_trail":       {"pk": "id", "schema": "audit_trail"},
    "app_config":        {"pk": "kode", "schema": "app_config"},
    "v_closing_line":    {"pk": "id", "schema": "v_closing_line"},
    "v_pengurasan":      {"pk": "kode", "schema": "v_pengurasan"},
    "v_rekonsiliasi":    {"pk": "kode", "schema": "v_rekonsiliasi"},
    "v_transfer_fuel":   {"pk": "kode", "schema": "v_transfer_fuel"},
    "v_flowmeter_ft":    {"pk": "kode", "schema": "v_flowmeter_ft"},
    "v_hour_meter":      {"pk": "kode", "schema": "v_hour_meter"},
}

FUEL_SITE_CODE = "PPA-BIB"

# Router — di-include di main app dengan prefix /api/fuel
fuel_router = APIRouter(prefix="/api/fuel", tags=["fuel-bridge"])

# pool di-inject dari main app via set_pool()
_fuel_pool: Optional[asyncpg.Pool] = None

def set_fuel_pool(pool: asyncpg.Pool):
    global _fuel_pool
    _fuel_pool = pool

def _get_pool() -> asyncpg.Pool:
    """Get the pool — prefer module-level, fall back to server_v8_pg.global."""
    if _fuel_pool is not None:
        return _fuel_pool
    import server_v8_pg
    if server_v8_pg.pool is not None:
        return server_v8_pg.pool
    raise RuntimeError("Database pool not initialized. Server may still be starting.")

async def _resolve_user_from_session(request: Request):
    """Ambil user dari cookie session (sama seperti require_user)."""
    import server_v8_pg
    pool = _get_pool()
    token = request.cookies.get("fcc_session")
    if not token:
        raise HTTPException(401, "Belum login.")
    sess = server_v8_pg.read_session(token)
    if not sess:
        raise HTTPException(401, "Session invalid.")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username, nama, role, vendor_kode, status FROM fcc.app_user WHERE username=$1",
            sess["u"],
        )
    if not row or row["status"] != "ACTIVE":
        raise HTTPException(401, "Akun non-aktif.")
    return {"username": row["username"], "nama": row["nama"], "role": row["role"], "vendor_kode": row["vendor_kode"]}

async def _table_exists(schema: str) -> bool:
    async with _get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT to_regclass($1) AS r", f"fcc.{schema}"
        )
        return row["r"] is not None

def _q(s: str) -> str:
    """Quote identifier. Star is wildcard."""
    if s == '*':
        return '*'
    return '"' + s.replace('"', '""') + '"'


# HEALTH
# ============================================================================
@fuel_router.get("/health")
async def fuel_health():
    """Cek apakah schema fcc.fuel_* siap dipakai."""
    try:
        async with _get_pool().acquire() as conn:
            tables = []
            for t in FUEL_TABLES:
                schema = FUEL_TABLES[t]["schema"]
                exists = await conn.fetchval(
                    "SELECT to_regclass($1) IS NOT NULL", f"fcc.{schema}"
                )
                tables.append({"name": t, "exists": bool(exists)})
            all_ok = all(t["exists"] for t in tables)
            return {
                "ok": all_ok,
                "engine": "postgres-local",
                "schema": "fcc",
                "site_code": FUEL_SITE_CODE,
                "tables": tables,
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ============================================================================
# LISTENER pattern: handle /api/fuel/{table} and /api/fuel/{table}/{id}
# ============================================================================

@fuel_router.get("/rpc/{rpc_name}")
async def fuel_rpc_get(rpc_name: str, request: Request):
    """RPC GET — beberapa klien Supabase pakai GET untuk RPC."""
    return await _fuel_rpc_dispatch(rpc_name, request)

@fuel_router.post("/rpc/{rpc_name}")
async def fuel_rpc_post(rpc_name: str, request: Request):
    return await _fuel_rpc_dispatch(rpc_name, request)

async def _fuel_rpc_dispatch(rpc_name: str, request: Request):
    user = await _resolve_user_from_session(request)
    body = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}

    rpc_map = {
        "fuel_get_default_fm_awal":   ("SELECT * FROM fcc.fuel_get_default_fm_awal($1::text,$2::uuid)",  ["p_site_code","p_jalur_id"]),
        "fuel_get_tera_volume":       ("SELECT * FROM fcc.fuel_get_tera_volume($1::uuid,$2::numeric)", ["p_fuel_truck_id","p_dip_value"]),
        "fuel_public_staged_nrp_lookup": ("SELECT * FROM fcc.fuel_public_staged_nrp_lookup($1::text)", ["p_nrp"]),
    }

    if rpc_name not in rpc_map:
        raise HTTPException(404, f"RPC {rpc_name} tidak dikenal.")

    sql_tpl, arg_keys = rpc_map[rpc_name]
    args = [body.get(k) for k in arg_keys]
    if any(a is None for a in args):
        raise HTTPException(400, f"RPC {rpc_name} butuh argumen: {arg_keys}")

    async with _get_pool().acquire() as conn:
        try:
            rows = await conn.fetch(sql_tpl, *args)
            return [dict(r) for r in rows]
        except asyncpg.exceptions.PostgresError as e:
            raise HTTPException(400, f"RPC error: {e}")

# ============================================================================
# CRUD: list / get / insert / update / upsert / delete
# ============================================================================

def _parse_query_params(qs: dict) -> dict:
    """Parse PostgREST-style query params jadi opsi.
    Contoh: select=col1,col2 → fields
            order=col.asc → order_by
            limit=10, offset=0
            eq[col]=value → filter
    """
    out = {"select": None, "order": [], "limit": None, "offset": None, "filters": []}
    for k, v in qs.items():
        if k == "select":
            out["select"] = v.split(",")
        elif k == "order":
            for part in v.split(","):
                if "." in part:
                    col, direction = part.rsplit(".", 1)
                    out["order"].append((col, direction.upper()))
                else:
                    out["order"].append((part, "ASC"))
        elif k == "limit":
            try: out["limit"] = int(v)
            except: pass
        elif k == "offset":
            try: out["offset"] = int(v)
            except: pass
        elif "." in k:
            op, col = k.split(".", 1)
            out["filters"].append((op, col, v))
    return out

def _sql_op(op: str) -> str:
    return {
        "eq": "=", "neq": "<>", "gt": ">", "gte": ">=",
        "lt": "<", "lte": "<=", "like": "ILIKE", "ilike": "ILIKE",
        "in": "IN", "is": "IS"
    }.get(op, "=")

@fuel_router.get("/{table}")
async def fuel_list(table: str, request: Request):
    user = await _resolve_user_from_session(request)
    if table not in FUEL_TABLES:
        raise HTTPException(404, f"Tabel {table} tidak dikenal.")
    meta = FUEL_TABLES[table]
    schema = meta["schema"]
    schema_fqn = f"fcc.{_q(schema)}"

    qs = dict(request.query_params)
    opts = _parse_query_params(qs)

    # SELECT clause
    if opts["select"]:
        # Validasi kolom ada
        async with _get_pool().acquire() as conn:
            cols_info = await conn.fetch("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='fcc' AND table_name=$1
            """, schema)
            existing = {c["column_name"] for c in cols_info}
            selected = [c for c in opts["select"] if c in existing]
            if not selected:
                selected = ["*"]
            select_sql = ", ".join(_q(c) for c in selected)
    else:
        select_sql = "*"

    # WHERE clause
    where_parts = []
    where_args = []
    arg_i = 1
    # Cache tipe kolom untuk cast yang tepat
    col_types_cache = {}
    async with _get_pool().acquire() as conn:
        col_type_rows = await conn.fetch("""
            SELECT column_name, data_type, udt_name FROM information_schema.columns
            WHERE table_schema='fcc' AND table_name=$1
        """, schema)
    col_types = {r["column_name"]: (r["data_type"], r["udt_name"]) for r in col_type_rows}

    for op, col, val in opts["filters"]:
        col_info = col_types.get(col, ("text", "text"))
        col_type, col_udt = col_info
        if op == "in":
            vals = val.split(",")
            placeholders = ",".join(f"${arg_i + i}" for i in range(len(vals)))
            # Cast each value to the column type
            cast_str = f"::{col_udt}" if col_udt not in ("text", "varchar", "") else ""
            where_parts.append(f"{_q(col)}::text IN ({placeholders})")
            where_args.extend(vals)
            arg_i += len(vals)
        else:
            sql_op = _sql_op(op)
            # Tentukan cast sesuai tipe
            if col_udt.startswith("fuel_"):
                # Custom enum
                cast_str = f"::{col_udt}"
                typed_val = val
            elif col_udt == "uuid":
                cast_str = "::uuid"
                typed_val = val
            elif col_udt == "date":
                # Convert string YYYY-MM-DD ke Python date object
                from datetime import date as _date
                try:
                    parts = val.split("-")
                    typed_val = _date(int(parts[0]), int(parts[1]), int(parts[2]))
                    cast_str = ""
                except Exception:
                    typed_val = val
                    cast_str = "::date"
            elif col_udt in ("timestamp with time zone", "timestamptz"):
                from datetime import datetime as _dt
                try:
                    typed_val = _dt.fromisoformat(val.replace("Z", "+00:00"))
                    cast_str = ""
                except Exception:
                    typed_val = val
                    cast_str = "::timestamptz"
            elif col_udt in ("numeric", "double precision", "real", "numeric"):
                cast_str = "::numeric"
                try: typed_val = float(val)
                except: typed_val = val
            elif col_udt in ("bigint", "int8"):
                cast_str = "::bigint"
                try: typed_val = int(val)
                except: typed_val = val
            elif col_udt in ("integer", "int4"):
                cast_str = "::integer"
                try: typed_val = int(val)
                except: typed_val = val
            elif col_udt in ("smallint", "int2"):
                cast_str = "::smallint"
                try: typed_val = int(val)
                except: typed_val = val
            elif col_udt in ("boolean",):
                cast_str = "::boolean"
                typed_val = val
            else:
                cast_str = "::text"
                typed_val = val
            if op == "is":
                cast_str = ""  # IS NULL/TRUE/FALSE doesn't need cast
            where_parts.append(f"{_q(col)} {sql_op} ${arg_i}{cast_str}")
            where_args.append(typed_val)
            arg_i += 1

    where_sql = " WHERE " + " AND ".join(where_parts) if where_parts else ""

    # ORDER BY
    order_sql = ""
    if opts["order"]:
        order_parts = [f"{_q(c)} {d}" for c, d in opts["order"]]
        order_sql = " ORDER BY " + ", ".join(order_parts)

    # LIMIT / OFFSET
    limit_sql = f" LIMIT {opts['limit']}" if opts["limit"] else ""
    offset_sql = f" OFFSET {opts['offset']}" if opts["offset"] else ""

    sql = f"SELECT {select_sql} FROM {schema_fqn}{where_sql}{order_sql}{limit_sql}{offset_sql}"

    async with _get_pool().acquire() as conn:
        try:
            if where_args:
                rows = await conn.fetch(sql, *where_args)
            else:
                rows = await conn.fetch(sql)
            return [dict(r) for r in rows]
        except asyncpg.exceptions.PostgresError as e:
            raise HTTPException(400, f"Query error: {e}")

@fuel_router.get("/{table}/{rid}")
async def fuel_get(table: str, rid: str, request: Request):
    user = await _resolve_user_from_session(request)
    if table not in FUEL_TABLES:
        raise HTTPException(404, f"Tabel {table} tidak dikenal.")
    meta = FUEL_TABLES[table]
    pk = meta["pk"]

    async with _get_pool().acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM fcc.{_q(meta['schema'])} WHERE {_q(pk)} = $1::uuid", rid
        )
    if not row:
        raise HTTPException(404, "Row tidak ditemukan.")
    return dict(row)

@fuel_router.post("/{table}")
async def fuel_insert(table: str, request: Request):
    user = await _resolve_user_from_session(request)
    if table not in FUEL_TABLES:
        raise HTTPException(404, f"Tabel {table} tidak dikenal.")
    meta = FUEL_TABLES[table]
    if meta.get("view"):
        raise HTTPException(400, f"{table} adalah view, tidak bisa insert.")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body harus JSON.")

    # Kalau single object, wrap ke list agar Supabase client kompatibel
    # sb.from('t').insert({...}).select().single() — balikin {data, error}
    # Kita balikin row langsung + count
    rows_in = body if isinstance(body, list) else [body]

    async with _get_pool().acquire() as conn:
        # Detect column types for proper coercion
        col_types = {}
        for r in rows_in:
            for col in r:
                if col not in col_types:
                    row_t = await conn.fetchrow("""
                        SELECT data_type FROM information_schema.columns
                        WHERE table_schema='fcc' AND table_name=$1 AND column_name=$2
                    """, meta["schema"], col)
                    col_types[col] = row_t["data_type"] if row_t else "text"

        inserted = []
        for r in rows_in:
            r = {k: v for k, v in r.items() if k not in ("created_at", "updated_at")}
            if not r:
                continue
            # Auto-generate id untuk tabel yang butuh UUID PK
            if "id" not in r or r["id"] is None or r["id"] == "":
                tbl_cols = await conn.fetch("""
                    SELECT column_name, data_type FROM information_schema.columns
                    WHERE table_schema='fcc' AND table_name=$1 AND column_name='id'
                """, meta["schema"])
                if tbl_cols and tbl_cols[0]["data_type"] == "uuid":
                    r["id"] = str(await conn.fetchval("SELECT gen_random_uuid()"))
            # Coerce date/timestamp strings to native Python objects
            vals = []
            for col, val in r.items():
                ct = col_types.get(col, "text")
                if val is None:
                    vals.append(None)
                elif ct == "date" and isinstance(val, str):
                    from datetime import date as _date
                    parts = val.split("-")
                    if len(parts) == 3:
                        vals.append(_date(int(parts[0]), int(parts[1]), int(parts[2])))
                    else:
                        vals.append(val)
                elif ct in ("timestamp with time zone", "timestamptz") and isinstance(val, str):
                    from datetime import datetime as _dt
                    try:
                        vals.append(_dt.fromisoformat(val.replace("Z", "+00:00")))
                    except Exception:
                        vals.append(val)
                elif ct == "uuid" and isinstance(val, str):
                    vals.append(val)
                else:
                    vals.append(val)
            cols = list(r.keys())
            placeholders = ",".join(f"${i+1}" for i in range(len(cols)))
            sql = f"""
                INSERT INTO fcc.{_q(meta['schema'])} ({','.join(_q(c) for c in cols)})
                VALUES ({placeholders})
                RETURNING *
            """
            try:
                row = await conn.fetchrow(sql, *vals)
                inserted.append(dict(row))
            except asyncpg.exceptions.PostgresError as e:
                raise HTTPException(400, f"Insert error: {e}")

    # PostgREST kompat: balikin single object kalau input single
    return inserted[0] if len(inserted) == 1 and not isinstance(body, list) else inserted

@fuel_router.patch("/{table}")
async def fuel_update(table: str, request: Request):
    user = await _resolve_user_from_session(request)
    if table not in FUEL_TABLES:
        raise HTTPException(404, f"Tabel {table} tidak dikenal.")
    meta = FUEL_TABLES[table]
    if meta.get("view"):
        raise HTTPException(400, f"{table} adalah view, tidak bisa update.")

    qs = dict(request.query_params)
    opts = _parse_query_params(qs)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body harus JSON.")

    if not opts["filters"]:
        raise HTTPException(400, "Update harus pakai filter (eq=col=val).")

    # Filter
    async with _get_pool().acquire() as conn:
        col_type_rows = await conn.fetch("""
            SELECT column_name, data_type, udt_name FROM information_schema.columns
            WHERE table_schema='fcc' AND table_name=$1
        """, meta["schema"])
    col_types = {r["column_name"]: r["udt_name"] for r in col_type_rows}

    where_parts = []
    where_args = []
    arg_i = 1
    for op, col, val in opts["filters"]:
        col_udt = col_types.get(col, "text")
        sql_op = _sql_op(op)
        cast_str = f"::{col_udt}" if col_udt.startswith("fuel_") or col_udt in ("uuid",) else "::text"
        where_parts.append(f"{_q(col)} {sql_op} ${arg_i}{cast_str}")
        where_args.append(val)
        arg_i += 1
    where_sql = " AND ".join(where_parts)

    # Set clause
    body = {k: v for k, v in body.items() if k not in ("id", "created_at", "updated_at")}
    set_parts = []
    set_args = []
    for i, (k, v) in enumerate(body.items()):
        set_parts.append(f"{_q(k)} = ${arg_i + i}")
        set_args.append(v)
    set_sql = ", ".join(set_parts)

    sql = f"""
        UPDATE fcc.{_q(meta['schema'])} SET {set_sql}
        WHERE {where_sql}
        RETURNING *
    """
    args = where_args + set_args

    async with _get_pool().acquire() as conn:
        try:
            rows = await conn.fetch(sql, *args)
            return [dict(r) for r in rows]
        except asyncpg.exceptions.PostgresError as e:
            raise HTTPException(400, f"Update error: {e}")

@fuel_router.post("/{table}/upsert")
async def fuel_upsert(table: str, request: Request):
    """POST /api/fuel/{table}/upsert — PostgREST pattern."""
    user = await _resolve_user_from_session(request)
    if table not in FUEL_TABLES:
        raise HTTPException(404, f"Tabel {table} tidak dikenal.")
    meta = FUEL_TABLES[table]
    if meta.get("view"):
        raise HTTPException(400, f"{table} adalah view, tidak bisa upsert.")

    qs = dict(request.query_params)
    # onConflict=param → column untuk conflict resolution
    on_conflict = qs.get("onConflict", "").split(",") if qs.get("onConflict") else None

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body harus JSON.")

    rows_in = body if isinstance(body, list) else [body]

    async with _get_pool().acquire() as conn:
        results = []
        for r in rows_in:
            if on_conflict:
                # UPDATE WHERE conflict_col IN (...)
                conflict_vals = [r.get(c) for c in on_conflict if r.get(c) is not None]
                if conflict_vals:
                    where_parts = " AND ".join(
                        f"{_q(c)} = ${i+1}" for i, c in enumerate(on_conflict) if r.get(c) is not None
                    )
                    upd = await conn.fetchrow(f"""
                        UPDATE fcc.{_q(meta['schema'])} SET
                            {','.join(f"{_q(k)}=EXCLUDED.{_q(k)}" for k in r if k not in on_conflict and k not in ('created_at','updated_at'))}
                        WHERE {where_parts}
                        RETURNING *
                    """, *conflict_vals)
                    if upd:
                        results.append(dict(upd))
                        continue
            # INSERT
            r_clean = {k: v for k, v in r.items() if k not in ("created_at", "updated_at")}
            cols = list(r_clean.keys())
            vals = list(r_clean.values())
            placeholders = ",".join(f"${i+1}" for i in range(len(cols)))
            try:
                row = await conn.fetchrow(f"""
                    INSERT INTO fcc.{_q(meta['schema'])} ({','.join(_q(c) for c in cols)})
                    VALUES ({placeholders})
                    ON CONFLICT DO NOTHING
                    RETURNING *
                """, *vals)
                if row:
                    results.append(dict(row))
            except asyncpg.exceptions.PostgresError as e:
                raise HTTPException(400, f"Upsert error: {e}")

    return results[0] if len(results) == 1 and not isinstance(body, list) else results

@fuel_router.delete("/{table}")
async def fuel_delete(table: str, request: Request):
    user = await _resolve_user_from_session(request)
    if table not in FUEL_TABLES:
        raise HTTPException(404, f"Tabel {table} tidak dikenal.")
    meta = FUEL_TABLES[table]
    if meta.get("view"):
        raise HTTPException(400, f"{table} adalah view, tidak bisa delete.")

    qs = dict(request.query_params)
    opts = _parse_query_params(qs)
    if not opts["filters"]:
        raise HTTPException(400, "Delete harus pakai filter (eq=col=val).")

    async with _get_pool().acquire() as conn:
        col_type_rows = await conn.fetch("""
            SELECT column_name, data_type, udt_name FROM information_schema.columns
            WHERE table_schema='fcc' AND table_name=$1
        """, meta["schema"])
    col_types = {r["column_name"]: r["udt_name"] for r in col_type_rows}

    where_parts = []
    where_args = []
    arg_i = 1
    for op, col, val in opts["filters"]:
        col_udt = col_types.get(col, "text")
        sql_op = _sql_op(op)
        cast_str = f"::{col_udt}" if col_udt.startswith("fuel_") or col_udt in ("uuid",) else "::text"
        where_parts.append(f"{_q(col)} {sql_op} ${arg_i}{cast_str}")
        where_args.append(val)
        arg_i += 1
    where_sql = " AND ".join(where_parts)

    sql = f"DELETE FROM fcc.{_q(meta['schema'])} WHERE {where_sql} RETURNING id"

    async with _get_pool().acquire() as conn:
        try:
            rows = await conn.fetch(sql, *where_args)
            return [dict(r) for r in rows]
        except asyncpg.exceptions.PostgresError as e:
            raise HTTPException(400, f"Delete error: {e}")

# ============================================================================
# PHOTO UPLOAD (storage replacement)
# ============================================================================
FUEL_PHOTO_DIR = "/home/ubuntu/fcc-photos"

@fuel_router.post("/storage/{bucket}/{path:path}")
async def fuel_storage_upload(bucket: str, path: str, request: Request):
    """Menerima upload blob, simpan ke filesystem lokal, return shape seperti
    Supabase storage.upload()."""
    user = await _resolve_user_from_session(request)

    content_type = request.headers.get("content-type", "application/octet-stream")
    body_bytes = await request.body()
    if not body_bytes:
        raise HTTPException(400, "Body kosong.")

    # Simpan ke /home/ubuntu/fcc-photos/{bucket}/{path}
    full_path = os.path.join(FUEL_PHOTO_DIR, bucket, path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(body_bytes)

    # Return shape seperti Supabase
    return {
        "path": f"{bucket}/{path}",
        "id": hashlib.md5(body_bytes).hexdigest(),
        "size": len(body_bytes),
        "mime": content_type,
    }

@fuel_router.get("/storage/{bucket}/{path:path}")
async def fuel_storage_get(bucket: str, path: str):
    """Serve foto dari filesystem lokal."""
    full_path = os.path.join(FUEL_PHOTO_DIR, bucket, path)
    if not os.path.isfile(full_path):
        raise HTTPException(404, "File tidak ditemukan.")
    with open(full_path, "rb") as f:
        data = f.read()
    # Guess content-type
    ctype = "application/octet-stream"
    if path.endswith(".jpg") or path.endswith(".jpeg"):
        ctype = "image/jpeg"
    elif path.endswith(".png"):
        ctype = "image/png"
    return Response(content=data, media_type=ctype)