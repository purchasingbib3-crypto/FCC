#!/usr/bin/env python3
"""
Fuel Control Center PPA-BIB — REST API v4
PostgreSQL backend with Phase 1 schema.
Endpoints match the Phase 1 reconciliation, work queue, and dashboard RPCs.
"""
import os
import hashlib
import json
from datetime import date, datetime
from typing import Optional, List
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Config ──
API_KEY = os.environ.get("FUEL_API_KEY", "fcc-ppa-bib-2026-juni-secret-key-7f3a9b")
DB_DSN = os.environ.get("FCC_DB_DSN", "")

# ── Connection pool ──
_pool = None

def get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn=2, maxconn=10,
            host="/var/run/postgresql", port=5432,
            dbname="fuel_control_center", user="postgres",
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

# ── Auth ──
async def verify_api_key(request: Request):
    key = request.headers.get("X-API-Key", "")
    auth = request.headers.get("Authorization", "")
    if key == API_KEY:
        return True
    if auth.startswith("Bearer ") and auth[7:] == API_KEY:
        return True
    raise HTTPException(status_code=401, detail="Invalid or missing API key.")

# ── Helpers ──
def paginate(page: int, per_page: int):
    return (page - 1) * per_page, per_page

def fmt(n, decimals=0):
    if n is None or n is None: return "—"
    try:
        return f"{float(n):,.{decimals}f}"
    except:
        return str(n)

def fmt_l(n):
    return f"{fmt(n, 0)} L"

# ── FastAPI ──
app = FastAPI(
    title="Fuel Control Center PPA-BIB API v4",
    description="PostgreSQL Phase 1 — reconciliation, work queue, master data, SS6/SAP",
    version="4.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ════════════════════════════════════════════════════════════
# HEALTH
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/health")
async def health():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM public.master_unit")
            units = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM public.fuel_issue_ss6")
            ss6 = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM public.fuel_issue_sap")
            sap = cur.fetchone()[0]
            return {"status": "ok", "database": "postgresql", "units": units, "ss6_rows": ss6, "sap_rows": sap}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# ════════════════════════════════════════════════════════════
# DASHBOARD
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/dashboard", dependencies=[Depends(verify_api_key)])
async def dashboard(period: str = "2026-07-01"):
    with get_db() as conn:
        cur = conn.cursor()
        p = date.fromisoformat(period)

        # SS6 totals for this month
        cur.execute("""
            SELECT count(*), COALESCE(sum(volume_liter), 0),
                   count(*) FILTER (WHERE unit_std IS NULL AND unit_key IS NOT NULL) as unaliased,
                   count(*) FILTER (WHERE unit_key IS NULL) as blank_unit,
                   count(*) FILTER (WHERE volume_liter = 0) as zero_vol
            FROM public.fuel_issue_ss6
            WHERE trx_date >= %s AND trx_date < (%s + interval '1 month')::date
        """, (p, p))
        ss6_rows, ss6_vol, ss6_unaliased, ss6_blank, ss6_zero = cur.fetchone()

        # SAP totals
        cur.execute("""
            SELECT count(*), COALESCE(-sum(qty_liter), 0), COALESCE(sum(abs(qty_liter)), 0),
                   count(*) FILTER (WHERE unit_std IS NULL) as unresolved,
                   count(*) FILTER (WHERE flag_dobel_input) as dobel,
                   count(*) FILTER (WHERE flag_salah_unit) as salah
            FROM public.fuel_issue_sap
            WHERE trx_date >= %s AND trx_date < (%s + interval '1 month')::date
        """, (p, p))
        sap_rows, sap_net, sap_abs, sap_unresolved, dobel, salah = cur.fetchone()

        # Reconciliation summary
        cur.execute("""
            SELECT severity, status, count(*),
                   COALESCE(sum(ss6_liter), 0), COALESCE(sum(sap_liter), 0),
                   COALESCE(sum(deviation_liter), 0)
            FROM public.reconciliation_finding
            WHERE period_month = %s
            GROUP BY severity, status ORDER BY severity
        """, (p,))
        recon = cur.fetchall()

        # Work queue summary
        cur.execute("""
            SELECT issue_type, count(*), COALESCE(sum(occurrences), 0), COALESCE(sum(total_liter), 0)
            FROM public.work_queue_item
            WHERE period_month = %s
            GROUP BY issue_type ORDER BY issue_type
        """, (p,))
        wq = cur.fetchall()

        # Asset breakdown
        cur.execute("SELECT asset_type, count(*) FROM public.master_asset WHERE is_active GROUP BY 1")
        assets = cur.fetchall()

        # Master counts
        cur.execute("SELECT count(*) FROM public.master_unit")
        total_units = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.unit_alias WHERE is_active")
        total_aliases = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.master_asset WHERE is_active")
        total_assets = cur.fetchone()[0]

        # SAP mode
        cur.execute("SELECT setting_value #>> '{}' FROM public.app_setting WHERE setting_key = 'reconciliation.sap_quantity_mode'")
        sap_mode = cur.fetchone()[0] if cur.rowcount else "ABSOLUTE_LEGACY"

        sap_effective = sap_net if sap_mode == "SIGNED_NET" else sap_abs
        dev = ss6_vol - sap_effective
        dev_pct = abs(dev) / max(abs(ss6_vol), abs(sap_effective), 1) * 100 if (ss6_vol or sap_effective) else 0

        return {
            "period": period,
            "ss6_rows": ss6_rows, "ss6_volume": float(ss6_vol),
            "sap_rows": sap_rows, "sap_net": float(sap_net), "sap_absolute": float(sap_abs),
            "sap_effective": float(sap_effective), "sap_mode": sap_mode,
            "deviation": float(dev), "deviation_pct": round(dev_pct, 3),
            "ss6_unaliased": ss6_unaliased, "ss6_blank_unit": ss6_blank, "ss6_zero_vol": ss6_zero,
            "sap_unresolved": sap_unresolved, "sap_dobel": dobel, "sap_salah_unit": salah,
            "reconciliation": [
                {"severity": r[0], "status": r[1], "findings": r[2],
                 "ss6_l": float(r[3]), "sap_l": float(r[4]), "deviation_l": float(r[5])}
                for r in recon
            ],
            "work_queue": [
                {"issue_type": r[0], "items": r[1], "occurrences": int(r[2]), "total_liter": float(r[3] or 0)}
                for r in wq
            ],
            "asset_breakdown": [{"type": r[0], "count": r[1]} for r in assets],
            "total_units": total_units, "total_aliases": total_aliases, "total_assets": total_assets,
        }

# ════════════════════════════════════════════════════════════
# SS6 TRANSACTIONS (paginated)
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/ss6", dependencies=[Depends(verify_api_key)])
async def ss6(
    page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=500),
    unit: Optional[str] = None, date_filter: Optional[str] = Query(None, alias="date"),
    storage_location: Optional[str] = None, shift: Optional[int] = None,
    unaliased: Optional[bool] = None,
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["1=1"]
        params = []
        if unit:
            where.append("(unit_raw ILIKE %s OR unit_key ILIKE %s)")
            params += [f"%{unit}%", f"%{unit}%"]
        if date_filter:
            where.append("trx_date = %s")
            params.append(date_filter)
        if storage_location:
            where.append("storage_loc ILIKE %s")
            params.append(f"%{storage_location}%")
        if shift:
            where.append("shift = %s")
            params.append(shift)
        if unaliased:
            where.append("unit_std IS NULL AND unit_key IS NOT NULL")

        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM public.fuel_issue_ss6 WHERE {w}", params)
        total = cur.fetchone()[0]

        offset, limit = paginate(page, per_page)
        cur.execute(f"""
            SELECT id, trx_date, shift, unit_raw, unit_std, volume_liter, storage_loc,
                   unit_key IS NULL as is_blank_unit, unit_std IS NULL AND unit_key IS NOT NULL as is_unaliased
            FROM public.fuel_issue_ss6 WHERE {w}
            ORDER BY trx_date DESC, id DESC LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = cur.fetchall()

        return {
            "data": [
                {"id": r[0], "date": r[1].isoformat() if r[1] else None, "shift": r[2],
                 "unit": r[3], "unit_std": r[4], "vol": float(r[5]), "storage_location": r[6],
                 "blank_unit": r[7], "unaliased": r[8]}
                for r in rows
            ],
            "pagination": {"page": page, "per_page": per_page, "total": total,
                           "total_pages": (total + per_page - 1) // per_page,
                           "has_next": offset + per_page < total, "has_prev": page > 1}
        }

# ════════════════════════════════════════════════════════════
# SAP TRANSACTIONS (paginated)
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/sap", dependencies=[Depends(verify_api_key)])
async def sap(
    page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=500),
    unit_sap: Optional[str] = None, date_filter: Optional[str] = Query(None, alias="date"),
    storage_location: Optional[str] = None, unresolved: Optional[bool] = None,
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["1=1"]
        params = []
        if unit_sap:
            where.append("(unit_derived ILIKE %s OR unit_derived_key ILIKE %s OR unit_std ILIKE %s)")
            params += [f"%{unit_sap}%", f"%{unit_sap}%", f"%{unit_sap}%"]
        if date_filter:
            where.append("trx_date = %s")
            params.append(date_filter)
        if storage_location:
            where.append("storage_loc ILIKE %s")
            params.append(f"%{storage_location}%")
        if unresolved:
            where.append("unit_std IS NULL")

        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM public.fuel_issue_sap WHERE {w}", params)
        total = cur.fetchone()[0]

        offset, limit = paginate(page, per_page)
        cur.execute(f"""
            SELECT id, trx_date, qty_liter, order_no, order_text, unit_derived, unit_std,
                   storage_loc, flag_dobel_input, flag_salah_unit
            FROM public.fuel_issue_sap WHERE {w}
            ORDER BY trx_date DESC, id DESC LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = cur.fetchall()

        return {
            "data": [
                {"id": r[0], "date": r[1].isoformat() if r[1] else None, "qty": float(r[2]),
                 "order": r[3], "text": r[4], "unit_derived": r[5], "unit_std": r[6],
                 "storage_location": r[7], "flag_dobel": r[8], "flag_salah": r[9]}
                for r in rows
            ],
            "pagination": {"page": page, "per_page": per_page, "total": total,
                           "total_pages": (total + per_page - 1) // per_page,
                           "has_next": offset + per_page < total, "has_prev": page > 1}
        }

# ════════════════════════════════════════════════════════════
# RECONCILIATION (paginated)
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/reconcile", dependencies=[Depends(verify_api_key)])
async def reconcile(
    page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=500),
    period: str = "2026-07-01",
    severity: Optional[str] = None, status: Optional[str] = None,
    search: Optional[str] = None,
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["f.period_month = %s"]
        params = [date.fromisoformat(period)]
        if severity:
            where.append("f.severity = %s")
            params.append(severity.upper())
        if status:
            where.append("f.status = %s")
            params.append(status.upper())
        if search:
            where.append("(f.unit_std ILIKE %s OR f.storage_loc ILIKE %s)")
            params += [f"%{search}%", f"%{search}%"]

        w = " AND ".join(where)
        cur.execute(f"""
            SELECT count(*) FROM public.reconciliation_finding f
            LEFT JOIN public.master_unit u ON u.unit_std = f.unit_std
            WHERE {w}
        """, params)
        total = cur.fetchone()[0]

        offset, limit = paginate(page, per_page)
        cur.execute(f"""
            SELECT f.id, f.period_month, f.unit_std, u.vendor_code,
                   u.kategori_1, u.kategori_2, u.klasifikasi,
                   f.storage_loc, f.ss6_liter, f.sap_net_liter, f.sap_absolute_liter,
                   f.sap_liter, f.deviation_liter, f.deviation_pct, f.severity, f.status,
                   f.disposition_note
            FROM public.reconciliation_finding f
            LEFT JOIN public.master_unit u ON u.unit_std = f.unit_std
            WHERE {w}
            ORDER BY CASE f.severity WHEN 'CRITICAL' THEN 1 WHEN 'WARNING' THEN 2 WHEN 'WATCH' THEN 3 ELSE 4 END,
                     abs(f.deviation_liter) DESC, f.unit_std
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = cur.fetchall()

        return {
            "data": [
                {"id": r[0], "period": r[1].isoformat() if r[1] else None, "unit_std": r[2],
                 "vendor": r[3], "kategori_1": r[4], "kategori_2": r[5], "klasifikasi": r[6],
                 "storage_loc": r[7], "ss6_liter": float(r[8]), "sap_net": float(r[9]),
                 "sap_absolute": float(r[10]), "sap_effective": float(r[11]),
                 "deviation": float(r[12]), "deviation_pct": float(r[13]),
                 "severity": r[14], "status": r[15], "disposition_note": r[16]}
                for r in rows
            ],
            "pagination": {"page": page, "per_page": per_page, "total": total,
                           "total_pages": (total + per_page - 1) // per_page,
                           "has_next": offset + per_page < total, "has_prev": page > 1}
        }

# ════════════════════════════════════════════════════════════
# WORK QUEUE (paginated)
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/work-queue", dependencies=[Depends(verify_api_key)])
async def work_queue(
    page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=500),
    period: str = "2026-07-01",
    issue_type: Optional[str] = None, status: Optional[str] = None,
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["period_month = %s"]
        params = [date.fromisoformat(period)]
        if issue_type:
            where.append("issue_type = %s")
            params.append(issue_type.upper())
        if status:
            where.append("status = %s")
            params.append(status.upper())

        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM public.work_queue_item WHERE {w}", params)
        total = cur.fetchone()[0]

        offset, limit = paginate(page, per_page)
        cur.execute(f"""
            SELECT id, period_month, issue_type, issue_key, source, severity, status,
                   occurrences, total_liter, payload, disposition_note, first_seen_at, last_seen_at
            FROM public.work_queue_item WHERE {w}
            ORDER BY CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'WARNING' THEN 2 WHEN 'WATCH' THEN 3 ELSE 4 END,
                     issue_type, occurrences DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = cur.fetchall()

        return {
            "data": [
                {"id": r[0], "period": r[1].isoformat() if r[1] else None, "issue_type": r[2],
                 "issue_key": r[3], "source": r[4], "severity": r[5], "status": r[6],
                 "occurrences": r[7], "total_liter": float(r[8] or 0), "payload": r[9],
                 "disposition_note": r[10], "first_seen": r[11].isoformat() if r[11] else None,
                 "last_seen": r[12].isoformat() if r[12] else None}
                for r in rows
            ],
            "pagination": {"page": page, "per_page": per_page, "total": total,
                           "total_pages": (total + per_page - 1) // per_page,
                           "has_next": offset + per_page < total, "has_prev": page > 1}
        }

# ════════════════════════════════════════════════════════════
# MASTER DATA
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/units", dependencies=[Depends(verify_api_key)])
async def units(
    page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=500),
    search: Optional[str] = None, vendor: Optional[str] = None,
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["is_active = true"]
        params = []
        if search:
            where.append("unit_std ILIKE %s")
            params.append(f"%{search}%")
        if vendor:
            where.append("vendor_code = %s")
            params.append(vendor)
        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM public.master_unit WHERE {w}", params)
        total = cur.fetchone()[0]
        offset, limit = paginate(page, per_page)
        cur.execute(f"""
            SELECT unit_std, vendor_code, kategori_1, kategori_2, klasifikasi, std_km_per_liter, is_active
            FROM public.master_unit WHERE {w} ORDER BY unit_std LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = cur.fetchall()
        return {
            "data": [{"unit_std": r[0], "vendor": r[1], "kategori_1": r[2], "kategori_2": r[3],
                      "klasifikasi": r[4], "std_km_per_liter": float(r[5]) if r[5] else None, "active": r[6]}
                     for r in rows],
            "pagination": {"page": page, "per_page": per_page, "total": total,
                           "total_pages": (total + per_page - 1) // per_page,
                           "has_next": offset + per_page < total, "has_prev": page > 1}
        }

@app.get("/api/v1/aliases", dependencies=[Depends(verify_api_key)])
async def aliases(
    page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=500),
    search: Optional[str] = None, source: Optional[str] = None,
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["is_active = true"]
        params = []
        if search:
            where.append("(alias ILIKE %s OR unit_std ILIKE %s)")
            params += [f"%{search}%", f"%{search}%"]
        if source:
            where.append("source = %s")
            params.append(source.upper())
        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM public.unit_alias WHERE {w}", params)
        total = cur.fetchone()[0]
        offset, limit = paginate(page, per_page)
        cur.execute(f"""
            SELECT id, unit_std, alias, alias_key, source, is_active
            FROM public.unit_alias WHERE {w} ORDER BY unit_std, source LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = cur.fetchall()
        return {
            "data": [{"id": r[0], "unit_std": r[1], "alias": r[2], "alias_key": r[3], "source": r[4], "active": r[5]}
                     for r in rows],
            "pagination": {"page": page, "per_page": per_page, "total": total,
                           "total_pages": (total + per_page - 1) // per_page,
                           "has_next": offset + per_page < total, "has_prev": page > 1}
        }

@app.get("/api/v1/assets", dependencies=[Depends(verify_api_key)])
async def assets():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT asset_code, asset_type, asset_name, lambung_ss6, lambung_sap,
                   capacity_liter, dip_max_cm, vendor_code, is_active
            FROM public.master_asset ORDER BY asset_code
        """)
        rows = cur.fetchall()
        return {"data": [{"asset_code": r[0], "asset_type": r[1], "asset_name": r[2],
                          "lambung_ss6": r[3], "lambung_sap": r[4], "capacity": float(r[5]) if r[5] else None,
                          "dip_max_cm": float(r[6]) if r[6] else None, "vendor": r[7], "active": r[8]}
                         for r in rows]}

@app.get("/api/v1/vendors", dependencies=[Depends(verify_api_key)])
async def vendors():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT vendor_code, vendor_name, vendor_category, is_active FROM public.master_vendor ORDER BY vendor_code")
        rows = cur.fetchall()
        return {"data": [{"vendor_code": r[0], "vendor_name": r[1], "category": r[2], "active": r[3]} for r in rows]}

@app.get("/api/v1/storage-locations", dependencies=[Depends(verify_api_key)])
async def storage_locations():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT storage_loc, storage_name, location_type, is_active FROM public.master_storage_location ORDER BY storage_loc")
        rows = cur.fetchall()
        return {"data": [{"storage_loc": r[0], "name": r[1], "type": r[2], "active": r[3]} for r in rows]}

@app.get("/api/v1/calibration", dependencies=[Depends(verify_api_key)])
async def calibration(
    asset_code: Optional[str] = None,
    page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=1000),
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["1=1"]
        params = []
        if asset_code:
            where.append("asset_code = %s")
            params.append(asset_code)
        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM public.tank_calibration WHERE {w}", params)
        total = cur.fetchone()[0]
        offset, limit = paginate(page, per_page)
        cur.execute(f"""
            SELECT asset_code, dip_cm, volume_liter FROM public.tank_calibration
            WHERE {w} ORDER BY asset_code, dip_cm LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = cur.fetchall()
        return {
            "data": [{"asset_code": r[0], "dip_cm": float(r[1]), "volume_liter": float(r[2])} for r in rows],
            "pagination": {"page": page, "per_page": per_page, "total": total,
                           "total_pages": (total + per_page - 1) // per_page}
        }

# ════════════════════════════════════════════════════════════
# SETTINGS & CONFIG
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/settings", dependencies=[Depends(verify_api_key)])
async def settings():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT setting_key, setting_value, description, is_public FROM public.app_setting ORDER BY setting_key")
        rows = cur.fetchall()
        return {"data": {"key": r[0], "value": r[1], "description": r[2], "is_public": r[3]} for r in rows}

@app.get("/api/v1/summary", dependencies=[Depends(verify_api_key)])
async def summary():
    with get_db() as conn:
        cur = conn.cursor()
        stats = {}
        for table in ['master_vendor','master_unit','unit_alias','master_asset',
                       'master_storage_location','tank_calibration',
                       'fuel_issue_ss6','fuel_issue_sap',
                       'reconciliation_finding','work_queue_item','import_batch']:
            cur.execute(f"SELECT count(*) FROM public.{table}")
            stats[table] = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(sum(volume_liter),0) FROM public.fuel_issue_ss6")
        stats["ss6_total_liter"] = float(cur.fetchone()[0])
        cur.execute("SELECT COALESCE(-sum(qty_liter),0), COALESCE(sum(abs(qty_liter)),0) FROM public.fuel_issue_sap")
        row = cur.fetchone()
        stats["sap_signed_net"] = float(row[0])
        stats["sap_absolute"] = float(row[1])
        return stats

# ════════════════════════════════════════════════════════════
# IMPORT BATCHES
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/imports", dependencies=[Depends(verify_api_key)])
async def imports():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, source, original_filename, source_file_sha256, period_start, period_end,
                   status, rows_total, rows_valid, rows_inserted, rows_duplicate, rows_rejected,
                   created_at, committed_at
            FROM public.import_batch ORDER BY id
        """)
        rows = cur.fetchall()
        return {"data": [
            {"id": r[0], "source": r[1], "filename": r[2], "sha256": r[3][:16]+"..." if r[3] else None,
             "period_start": r[4].isoformat() if r[4] else None, "period_end": r[5].isoformat() if r[5] else None,
             "status": r[6], "rows_total": r[7], "rows_valid": r[8], "rows_inserted": r[9],
             "rows_duplicate": r[10], "rows_rejected": r[11],
             "created_at": r[12].isoformat() if r[12] else None, "committed_at": r[13].isoformat() if r[13] else None}
            for r in rows
        ]}

# ════════════════════════════════════════════════════════════
# USERS
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/users", dependencies=[Depends(verify_api_key)])
async def users(
    page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=500),
    search: Optional[str] = None, role: Optional[str] = None,
):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["1=1"]
        params = []
        if search:
            where.append("(nrp ILIKE %s OR display_name ILIKE %s)")
            params += [f"%{search}%", f"%{search}%"]
        if role:
            where.append("role = %s")
            params.append(role.upper())
        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM public.app_user WHERE {w}", params)
        total = cur.fetchone()[0]
        offset, limit = paginate(page, per_page)
        cur.execute(f"""
            SELECT id, nrp, display_name, role, vendor_code, is_active, last_login_at
            FROM public.app_user WHERE {w} ORDER BY display_name LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = cur.fetchall()
        return {
            "data": [{"id": str(r[0]), "nrp": r[1], "display_name": r[2], "role": r[3],
                      "vendor": r[4], "active": r[5], "last_login": r[6].isoformat() if r[6] else None}
                     for r in rows],
            "pagination": {"page": page, "per_page": per_page, "total": total,
                           "total_pages": (total + per_page - 1) // per_page,
                           "has_next": offset + per_page < total, "has_prev": page > 1}
        }

# ── Run ──
if __name__ == "__main__":
    import uvicorn
    print(f"\n  Fuel Control Center API v4 (PostgreSQL)")
    print(f"  Database: fuel_control_center @ /var/run/postgresql")
    print(f"  API Key: {API_KEY}")
    print(f"  Docs: http://localhost:8000/docs\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)