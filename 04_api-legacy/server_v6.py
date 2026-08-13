#!/usr/bin/env python3
"""Fuel Control Center API v6 — Schema fcc (V6)."""
import os
from contextlib import contextmanager
from typing import Optional
import psycopg2
import psycopg2.pool
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

API_KEY = os.environ.get("FUEL_API_KEY", "fcc-ppa-bib-2026-juni-secret-key-7f3a9b")
_pool = None

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

async def verify_api_key(request: Request):
    key = request.headers.get("X-API-Key", "")
    auth = request.headers.get("Authorization", "")
    if key == API_KEY: return True
    if auth.startswith("Bearer ") and auth[7:] == API_KEY: return True
    raise HTTPException(status_code=401, detail="Invalid API key")

app = FastAPI(title="FCC PPA-BIB API v6", version="6.0")
app.add_middleware(CORSMiddleware, allow_origin_regex=".*",
                   allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

def paginate(page, per_page):
    return (page - 1) * per_page, per_page

@app.get("/api/v1/health")
async def health():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            counts = {}
            for t in ['master_unit','master_vendor','unit_alias','master_jalur',
                     'master_main_tank','master_fuel_truck','ft_mandar_ocean',
                     'sounding_table','app_user']:
                cur.execute(f"SELECT count(*) FROM fcc.{t}")
                counts[t] = cur.fetchone()[0]
            return {"status":"ok","database":"postgresql_v6","schema":"fcc","tables":counts}
    except Exception as e:
        return {"status":"error","detail":str(e)}

@app.get("/api/v1/login", dependencies=[Depends(verify_api_key)])
async def login(username: str, password: str):
    import hashlib
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT id, username, nama, role, vendor_kode, status
            FROM fcc.app_user WHERE username = %s AND password_hash = %s""",
            (username, pwd_hash))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Username atau password salah")
        cur.execute("UPDATE fcc.app_user SET last_login = now() WHERE id = %s", (row[0],))
        conn.commit()
        return {"id": row[0], "username": row[1], "nama": row[2],
                "role": row[3], "vendor": row[4], "active": row[5] == "ACTIVE"}

@app.get("/api/v1/users", dependencies=[Depends(verify_api_key)])
async def users(page: int = Query(1,ge=1), per_page: int = Query(50,ge=1,le=500),
                search: Optional[str] = None, role: Optional[str] = None):
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
        return {
            "data":[{"id":r[0],"username":r[1],"nama":r[2],"role":r[3],
                     "vendor":r[4],"active":r[5]=="ACTIVE",
                     "last_login":r[6].isoformat() if r[6] else None} for r in rows],
            "pagination":{"page":page,"per_page":per_page,"total":total,
                          "total_pages":(total+per_page-1)//per_page,
                          "has_next":offset+per_page<total,"has_prev":page>1}
        }

@app.get("/api/v1/vendors", dependencies=[Depends(verify_api_key)])
async def vendors():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT kode, nama, kategori, status FROM fcc.master_vendor ORDER BY kode")
        rows = cur.fetchall()
        return {"data":[{"kode":r[0],"nama":r[1],"kategori":r[2],"active":r[3]=="ACTIVE"} for r in rows]}

@app.get("/api/v1/units", dependencies=[Depends(verify_api_key)])
async def units(page: int = Query(1,ge=1), per_page: int = Query(50,ge=1,le=500),
               search: Optional[str] = None, vendor: Optional[str] = None):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["status = 'ACTIVE'"]; params = []
        if search:
            where.append("(kode ILIKE %s OR nama ILIKE %s)")
            params += [f"%{search}%", f"%{search}%"]
        if vendor:
            where.append("vendor_kode = %s"); params.append(vendor)
        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM fcc.master_unit WHERE {w}", params)
        total = cur.fetchone()[0]
        offset, limit = paginate(page, per_page)
        cur.execute(f"""SELECT kode, nama, vendor_kode, kategori, status
            FROM fcc.master_unit WHERE {w} ORDER BY kode LIMIT %s OFFSET %s""",
            params + [limit, offset])
        rows = cur.fetchall()
        return {
            "data":[{"kode":r[0],"nama":r[1],"vendor":r[2],"kategori":r[3],"active":r[4]=="ACTIVE"} for r in rows],
            "pagination":{"page":page,"per_page":per_page,"total":total,
                          "total_pages":(total+per_page-1)//per_page,
                          "has_next":offset+per_page<total,"has_prev":page>1}
        }

@app.get("/api/v1/aliases", dependencies=[Depends(verify_api_key)])
async def aliases(page: int = Query(1,ge=1), per_page: int = Query(50,ge=1,le=500),
                 search: Optional[str] = None):
    with get_db() as conn:
        cur = conn.cursor()
        where = ["status = 'ACTIVE'"]; params = []
        if search:
            where.append("(alias_ss6 ILIKE %s OR alias_sap ILIKE %s OR unit_standar ILIKE %s)")
            params += [f"%{search}%", f"%{search}%", f"%{search}%"]
        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) FROM fcc.unit_alias WHERE {w}", params)
        total = cur.fetchone()[0]
        offset, limit = paginate(page, per_page)
        cur.execute(f"""SELECT id, unit_standar, alias_ss6, alias_sap, vendor_kode, kategori, status
            FROM fcc.unit_alias WHERE {w} ORDER BY unit_standar LIMIT %s OFFSET %s""",
            params + [limit, offset])
        rows = cur.fetchall()
        return {
            "data":[{"id":r[0],"unit_standar":r[1],"alias_ss6":r[2],"alias_sap":r[3],
                     "vendor":r[4],"kategori":r[5],"active":r[6]=="ACTIVE"} for r in rows],
            "pagination":{"page":page,"per_page":per_page,"total":total,
                          "total_pages":(total+per_page-1)//per_page,
                          "has_next":offset+per_page<total,"has_prev":page>1}
        }

@app.get("/api/v1/jalur", dependencies=[Depends(verify_api_key)])
async def jalur():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT kode, nama, tujuan, peruntukan, site, status FROM fcc.master_jalur ORDER BY kode")
        rows = cur.fetchall()
        return {"data":[{"kode":r[0],"nama":r[1],"tujuan":r[2],"peruntukan":r[3],
                         "site":r[4],"active":r[5]=="ACTIVE"} for r in rows]}

@app.get("/api/v1/main-tanks", dependencies=[Depends(verify_api_key)])
async def main_tanks():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT kode, nama, kapasitas_l, status FROM fcc.master_main_tank ORDER BY kode")
        rows = cur.fetchall()
        return {"data":[{"kode":r[0],"nama":r[1],"kapasitas":float(r[2]),"active":r[3]=="ACTIVE"} for r in rows]}

@app.get("/api/v1/fuel-trucks", dependencies=[Depends(verify_api_key)])
async def fuel_trucks():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT kode, nama, tipe, kapasitas_l, status FROM fcc.master_fuel_truck ORDER BY kode")
        rows = cur.fetchall()
        return {"data":[{"kode":r[0],"nama":r[1],"tipe":r[2],
                         "kapasitas":float(r[3]) if r[3] else None,"active":r[4]=="ACTIVE"} for r in rows]}

@app.get("/api/v1/ft-mandar-ocean", dependencies=[Depends(verify_api_key)])
async def ft_mandar_ocean():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id_ft, no_lambung, no_polisi, kapasitas_l, masa_berlaku, status FROM fcc.ft_mandar_ocean ORDER BY id_ft")
        rows = cur.fetchall()
        return {"data":[{"id_ft":r[0],"no_lambung":r[1],"no_polisi":r[2],
                         "kapasitas":float(r[3]) if r[3] else None,
                         "masa_berlaku":r[4].isoformat() if r[4] else None,
                         "status":r[5]} for r in rows]}

@app.get("/api/v1/sounding", dependencies=[Depends(verify_api_key)])
async def sounding(asset: Optional[str] = None,
                  page: int = Query(1,ge=1), per_page: int = Query(100,ge=1,le=1000)):
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
        return {
            "data":[{"aset":r[0],"dip_cm":float(r[1]),"volume_l":float(r[2]),"active":r[3]=="ACTIVE"} for r in rows],
            "pagination":{"page":page,"per_page":per_page,"total":total,
                          "total_pages":(total+per_page-1)//per_page}
        }

@app.get("/api/v1/shift-route-config", dependencies=[Depends(verify_api_key)])
async def shift_route_config():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT id, tanggal, shift, jalur, main_tank, fm_akhir_shift_sebelumnya,
            fm_aktual_awal, deviasi, status FROM fcc.shift_route_config ORDER BY tanggal DESC""")
        rows = cur.fetchall()
        return {"data":[{"id":r[0],"tanggal":r[1].isoformat(),"shift":r[2],"jalur":r[3],
                         "main_tank":r[4],"fm_akhir":float(r[5]),"fm_aktual":float(r[6]),
                         "deviasi":float(r[7]) if r[7] else 0,"status":r[8]} for r in rows]}

@app.get("/api/v1/config", dependencies=[Depends(verify_api_key)])
async def config():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT parameter, nilai, tipe, keterangan, rahasia FROM fcc.app_config ORDER BY parameter")
        rows = cur.fetchall()
        return {"data":[{"key":r[0],"value":r[1],"type":r[2],"description":r[3],"secret":r[4]} for r in rows]}

@app.get("/api/v1/summary", dependencies=[Depends(verify_api_key)])
async def summary():
    with get_db() as conn:
        cur = conn.cursor()
        s = {}
        for t in ['master_unit','master_vendor','unit_alias','master_jalur',
                 'master_main_tank','master_fuel_truck','ft_mandar_ocean',
                 'sounding_table','shift_route_config','app_user']:
            cur.execute(f"SELECT count(*) FROM fcc.{t}")
            s[t] = cur.fetchone()[0]
        return s

@app.get("/api/v1/dashboard", dependencies=[Depends(verify_api_key)])
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
        return s

if __name__ == "__main__":
    import uvicorn
    print(f"FCC PPA-BIB API v6 (schema fcc) -- API key: {API_KEY}")
    uvicorn.run(app, host="0.0.0.0", port=8000)