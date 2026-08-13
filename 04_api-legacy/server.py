#!/usr/bin/env python3
"""
Fuel Control Center PPA-BIB — REST API
FastAPI backend with API key auth, pagination, and endpoints for all modules.
"""
import sqlite3
import os
from contextlib import contextmanager
from typing import Optional, List
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fuel_control.db")

# ── API Key ──
# Generate: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
API_KEY = os.environ.get("FUEL_API_KEY", "fcc-ppa-bib-2026-juni-secret-key-7f3a9b")

app = FastAPI(
    title="Fuel Control Center PPA-BIB API",
    description="REST API for Fuel Control Center — data from Excel reconciliation June 2026",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database connection ──
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# ── Auth ──
async def verify_api_key(request: Request):
    auth = request.headers.get("Authorization", "")
    key = request.headers.get("X-API-Key", "")
    if key and key == API_KEY:
        return True
    if auth.startswith("Bearer ") and auth[7:] == API_KEY:
        return True
    if auth == API_KEY:
        return True
    raise HTTPException(status_code=401, detail="Invalid or missing API key. Use 'X-API-Key' header or 'Authorization: Bearer <key>'.")

# ── Helpers ──
def query_rows(conn, sql, params=None, page=1, per_page=50):
    """Execute query with pagination, return rows + meta."""
    offset = (page - 1) * per_page
    params = params or []

    # Get total count
    count_sql = f"SELECT COUNT(*) as total FROM ({sql})"
    total = conn.execute(count_sql, params).fetchone()["total"]

    # Get page data
    page_sql = f"{sql} LIMIT ? OFFSET ?"
    rows = conn.execute(page_sql, params + [per_page, offset]).fetchall()

    return {
        "data": [dict(r) for r in rows],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": (total + per_page - 1) // per_page,
            "has_next": offset + per_page < total,
            "has_prev": page > 1,
        },
    }

def query_all(conn, sql, params=None):
    """Execute query, return all rows as list of dicts."""
    rows = conn.execute(sql, params or []).fetchall()
    return [dict(r) for r in rows]

def query_one(conn, sql, params=None):
    """Execute query, return single row as dict."""
    row = conn.execute(sql, params or []).fetchone()
    return dict(row) if row else None

def query_scalar(conn, sql, params=None):
    """Execute query, return scalar value."""
    return conn.execute(sql, params or []).fetchone()[0]

# ════════════════════════════════════════════
# ENDPOINTS
# ════════════════════════════════════════════

@app.get("/api/v1/health")
async def health():
    """Health check — no auth required."""
    with get_db() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {"status": "ok", "database": "fuel_control.db", "tables": [t["name"] for t in tables]}

@app.get("/api/v1/summary", dependencies=[Depends(verify_api_key)])
async def get_summary():
    """Dashboard summary — counts for all modules."""
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM dashboard_summary").fetchall()
        return {r["key"]: r["value"] for r in rows}

@app.get("/api/v1/config", dependencies=[Depends(verify_api_key)])
async def get_config():
    """Dashboard configuration."""
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM dashboard_config").fetchall()
        return {r["key"]: r["value"] for r in rows}

# ── Assets ──
@app.get("/api/v1/assets", dependencies=[Depends(verify_api_key)])
async def get_assets(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    search: Optional[str] = None,
    asset_type: Optional[str] = None,
    vendor: Optional[str] = None,
):
    with get_db() as conn:
        sql = "SELECT * FROM master_assets WHERE 1=1"
        params = []
        if search:
            sql += " AND (unit_standar LIKE ? OR ss6_id LIKE ? OR sap_id LIKE ?)"
            p = f"%{search}%"
            params += [p, p, p]
        if asset_type:
            sql += " AND asset_type = ?"
            params.append(asset_type)
        if vendor:
            sql += " AND vendor = ?"
            params.append(vendor)
        sql += " ORDER BY unit_standar"
        return query_rows(conn, sql, params, page, per_page)

# ── Aliases ──
@app.get("/api/v1/aliases", dependencies=[Depends(verify_api_key)])
async def get_aliases(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    search: Optional[str] = None,
):
    with get_db() as conn:
        sql = "SELECT * FROM master_aliases WHERE 1=1"
        params = []
        if search:
            sql += " AND (unit_standar LIKE ? OR ss6_id LIKE ? OR sap_id LIKE ?)"
            p = f"%{search}%"
            params += [p, p, p]
        sql += " ORDER BY unit_standar"
        return query_rows(conn, sql, params, page, per_page)

# ── SS6 Transactions ──
@app.get("/api/v1/ss6", dependencies=[Depends(verify_api_key)])
async def get_ss6(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    unit: Optional[str] = None,
    date: Optional[str] = None,
    storage_location: Optional[str] = None,
    shift: Optional[int] = None,
):
    with get_db() as conn:
        sql = "SELECT * FROM ss6_transactions WHERE 1=1"
        params = []
        if unit:
            sql += " AND (unit LIKE ? OR normalized LIKE ?)"
            params += [f"%{unit}%", f"%unit.upper().replace(' ','')%"]
        if date:
            sql += " AND date = ?"
            params.append(date)
        if storage_location:
            sql += " AND storage_location = ?"
            params.append(storage_location)
        if shift is not None:
            sql += " AND shift = ?"
            params.append(shift)
        sql += " ORDER BY date DESC, id DESC"
        return query_rows(conn, sql, params, page, per_page)

# ── SAP Transactions ──
@app.get("/api/v1/sap", dependencies=[Depends(verify_api_key)])
async def get_sap(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    unit_sap: Optional[str] = None,
    date: Optional[str] = None,
    storage_location: Optional[str] = None,
):
    with get_db() as conn:
        sql = "SELECT * FROM sap_transactions WHERE 1=1"
        params = []
        if unit_sap:
            sql += " AND (unit_sap LIKE ? OR normalized LIKE ?)"
            p = f"%{unit_sap}%"
            params += [p, p]
        if date:
            sql += " AND date = ?"
            params.append(date)
        if storage_location:
            sql += " AND storage_location = ?"
            params.append(storage_location)
        sql += " ORDER BY date DESC, id DESC"
        return query_rows(conn, sql, params, page, per_page)

# ── Closings ──
@app.get("/api/v1/closings", dependencies=[Depends(verify_api_key)])
async def get_closings(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    fuel_truck: Optional[str] = None,
    date: Optional[str] = None,
    shift: Optional[int] = None,
):
    with get_db() as conn:
        sql = "SELECT * FROM closings WHERE 1=1"
        params = []
        if fuel_truck:
            sql += " AND fuel_truck = ?"
            params.append(fuel_truck)
        if date:
            sql += " AND date = ?"
            params.append(date)
        if shift is not None:
            sql += " AND shift = ?"
            params.append(shift)
        sql += " ORDER BY date DESC, fuel_truck"
        return query_rows(conn, sql, params, page, per_page)

# ── Vouchers ──
@app.get("/api/v1/vouchers", dependencies=[Depends(verify_api_key)])
async def get_vouchers(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    no_lambung: Optional[str] = None,
    date: Optional[str] = None,
    search: Optional[str] = None,
):
    with get_db() as conn:
        sql = "SELECT * FROM vouchers WHERE 1=1"
        params = []
        if no_lambung:
            sql += " AND no_lambung = ?"
            params.append(no_lambung)
        if date:
            sql += " AND date = ?"
            params.append(date)
        if search:
            sql += " AND (nomor LIKE ? OR no_lambung LIKE ? OR nama_unit LIKE ?)"
            p = f"%{search}%"
            params += [p, p, p]
        sql += " ORDER BY date DESC, id DESC"
        return query_rows(conn, sql, params, page, per_page)

# ── Penerimaans ──
@app.get("/api/v1/penerimaans", dependencies=[Depends(verify_api_key)])
async def get_penerimaans(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    date: Optional[str] = None,
    storage_location: Optional[str] = None,
    movement_type: Optional[str] = None,
):
    with get_db() as conn:
        sql = "SELECT * FROM penerimaans WHERE 1=1"
        params = []
        if date:
            sql += " AND date = ?"
            params.append(date)
        if storage_location:
            sql += " AND storage_location = ?"
            params.append(storage_location)
        if movement_type:
            sql += " AND movement_type = ?"
            params.append(movement_type)
        sql += " ORDER BY date DESC, id DESC"
        return query_rows(conn, sql, params, page, per_page)

# ── Rekapans ──
@app.get("/api/v1/rekapans", dependencies=[Depends(verify_api_key)])
async def get_rekapans(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    fuel_truck: Optional[str] = None,
    date: Optional[str] = None,
):
    with get_db() as conn:
        sql = "SELECT * FROM rekapans WHERE 1=1"
        params = []
        if fuel_truck:
            sql += " AND fuel_truck = ?"
            params.append(fuel_truck)
        if date:
            sql += " AND date = ?"
            params.append(date)
        sql += " ORDER BY date DESC, fuel_truck"
        return query_rows(conn, sql, params, page, per_page)

# ── Sarana ──
@app.get("/api/v1/sarana", dependencies=[Depends(verify_api_key)])
async def get_sarana(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    search: Optional[str] = None,
    status: Optional[str] = None,
):
    with get_db() as conn:
        sql = "SELECT * FROM sarana_consumption WHERE 1=1"
        params = []
        if search:
            sql += " AND unit_standar LIKE ?"
            params.append(f"%{search}%")
        if status:
            sql += " AND status = ?"
            params.append(status.upper())
        sql += " ORDER BY unit_standar"
        return query_rows(conn, sql, params, page, per_page)

# ── Fuel Trucks ──
@app.get("/api/v1/fuel-trucks", dependencies=[Depends(verify_api_key)])
async def get_fuel_trucks():
    with get_db() as conn:
        return query_all(conn, "SELECT * FROM fuel_trucks ORDER BY unit_ss6")

# ── Users ──
@app.get("/api/v1/users", dependencies=[Depends(verify_api_key)])
async def get_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    search: Optional[str] = None,
    role: Optional[str] = None,
):
    with get_db() as conn:
        sql = "SELECT * FROM users WHERE 1=1"
        params = []
        if search:
            sql += " AND (nrp LIKE ? OR nama LIKE ?)"
            p = f"%{search}%"
            params += [p, p]
        if role:
            sql += " AND role = ?"
            params.append(role)
        sql += " ORDER BY nama"
        return query_rows(conn, sql, params, page, per_page)

# ── BIB Mapping ──
@app.get("/api/v1/bib-mapping", dependencies=[Depends(verify_api_key)])
async def get_bib_mapping():
    with get_db() as conn:
        return query_all(conn, "SELECT * FROM bib_mapping ORDER BY voucher")

# ── Reconciliation ──
@app.get("/api/v1/reconcile", dependencies=[Depends(verify_api_key)])
async def get_reconcile(
    date: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
):
    """Reconciliation SS6 vs SAP aggregated by normalized unit + date."""
    with get_db() as conn:
        # Aggregate SS6
        ss6_sql = """
            SELECT normalized, date,
                   GROUP_CONCAT(DISTINCT unit) as ss6_units,
                   SUM(vol) as ss6_vol,
                   storage_location
            FROM ss6_transactions
        """
        if date:
            ss6_sql += " WHERE date = ?"
            ss6_params = [date]
        else:
            ss6_params = []
        ss6_sql += " GROUP BY normalized, date"
        ss6_map = {}
        for r in conn.execute(ss6_sql, ss6_params).fetchall():
            key = f"{r['normalized']}_{r['date']}"
            ss6_map[key] = dict(r)

        # Aggregate SAP
        sap_sql = """
            SELECT normalized, date,
                   GROUP_CONCAT(DISTINCT unit_sap) as sap_units,
                   SUM(ABS(qty)) as sap_vol,
                   storage_location
            FROM sap_transactions
        """
        if date:
            sap_sql += " WHERE date = ?"
            sap_params = [date]
        else:
            sap_params = []
        sap_sql += " GROUP BY normalized, date"
        sap_map = {}
        for r in conn.execute(sap_sql, sap_params).fetchall():
            key = f"{r['normalized']}_{r['date']}"
            sap_map[key] = dict(r)

        # Merge
        all_keys = sorted(set(list(ss6_map.keys()) + list(sap_map.keys())))
        results = []
        for key in all_keys:
            s6 = ss6_map.get(key, {})
            sp = sap_map.get(key, {})
            results.append({
                "normalized": s6.get("normalized") or sp.get("normalized", ""),
                "date": s6.get("date") or sp.get("date", ""),
                "ss6_units": s6.get("ss6_units", ""),
                "ss6_vol": s6.get("ss6_vol", 0),
                "sap_units": sp.get("sap_units", ""),
                "sap_vol": sp.get("sap_vol", 0),
                "difference": (s6.get("ss6_vol", 0) or 0) - (sp.get("sap_vol", 0) or 0),
                "storage_location": s6.get("storage_location") or sp.get("storage_location", ""),
            })

        # Paginate
        total = len(results)
        offset = (page - 1) * per_page
        page_data = results[offset:offset + per_page]
        return {
            "data": page_data,
            "pagination": {
                "page": page, "per_page": per_page, "total": total,
                "total_pages": (total + per_page - 1) // per_page,
                "has_next": offset + per_page < total, "has_prev": page > 1,
            },
        }

# ── Dashboard metrics ──
@app.get("/api/v1/dashboard", dependencies=[Depends(verify_api_key)])
async def get_dashboard():
    """Aggregated metrics for dashboard."""
    with get_db() as conn:
        total_ss6 = query_scalar(conn, "SELECT COALESCE(SUM(vol),0) FROM ss6_transactions")
        total_sap = query_scalar(conn, "SELECT COALESCE(SUM(ABS(qty)),0) FROM sap_transactions")
        diff = total_ss6 - total_sap
        pct = abs(diff / total_sap * 100) if total_sap > 0 else 0

        # Latest closings
        latest_date = query_scalar(conn, "SELECT MAX(date) FROM closings WHERE date != ''")
        latest_actual = query_scalar(conn, f"SELECT COALESCE(SUM(stock_aktual),0) FROM closings WHERE date = '{latest_date}'")
        latest_adm = query_scalar(conn, f"SELECT COALESCE(SUM(stock_akhir_adm),0) FROM closings WHERE date = '{latest_date}'")

        # Asset type breakdown
        type_rows = conn.execute("SELECT asset_type, COUNT(*) as count FROM master_assets GROUP BY asset_type ORDER BY count DESC").fetchall()

        return {
            "ss6_total": total_ss6,
            "sap_total": total_sap,
            "difference": diff,
            "difference_pct": round(pct, 3),
            "latest_closing_date": latest_date,
            "latest_actual_stock": latest_actual,
            "latest_admin_stock": latest_adm,
            "latest_deviasi": latest_actual - latest_adm,
            "asset_breakdown": [dict(r) for r in type_rows],
            "total_units": query_scalar(conn, "SELECT COUNT(*) FROM master_assets"),
            "total_ss6_records": query_scalar(conn, "SELECT COUNT(*) FROM ss6_transactions"),
            "total_sap_records": query_scalar(conn, "SELECT COUNT(*) FROM sap_transactions"),
            "total_closings": query_scalar(conn, "SELECT COUNT(*) FROM closings"),
            "total_vouchers": query_scalar(conn, "SELECT COUNT(*) FROM vouchers"),
            "total_sarana": query_scalar(conn, "SELECT COUNT(*) FROM sarana_consumption"),
            "total_users": query_scalar(conn, "SELECT COUNT(*) FROM users"),
        }

# ── Run ──
if __name__ == "__main__":
    import uvicorn
    print(f"\n  Fuel Control Center API")
    print(f"  Database: {DB_PATH}")
    print(f"  API Key: {API_KEY}")
    print(f"  Docs: http://localhost:8000/docs")
    print(f"  Health: http://localhost:8000/api/v1/health\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)