#!/usr/bin/env python3
"""
Import all Excel data into SQLite database for the Fuel Control Center API.
Creates fuel_control.db with all tables from the reconciliation spreadsheet.
"""
import sqlite3
import re
import os
from datetime import datetime
from pathlib import Path

import openpyxl

XLSX_PATH = "/home/ubuntu/.hermes/cache/documents/doc_13a4d5de2aff_Salinan rekonsil unit-JUNI 2026.xlsx"
DB_PATH = "/home/ubuntu/fuel-control-center/api/fuel_control.db"

def normalize_unit(name):
    if not name or not isinstance(name, str):
        return ""
    return re.sub(r'[^A-Za-z0-9]', '', name).upper()

def ser(val):
    """Serialize Excel cell to DB-friendly value."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(val, float):
        if val == int(val):
            return int(val)
        return round(val, 2)
    if isinstance(val, str):
        if val.startswith("#") and (val.endswith("?") or val == "#N/A"):
            return None
        return val.strip()
    return val

def sheet_rows(ws):
    """Yield dict rows from worksheet."""
    rows_iter = ws.iter_rows(values_only=True)
    try:
        headers = [str(h).strip() if h else f"col_{i}" for i, h in enumerate(next(rows_iter))]
    except StopIteration:
        return
    headers = [h.replace(" ", "_").replace("/", "_").replace(".", "_") for h in headers]
    for row in rows_iter:
        if all(c is None or c == "" for c in row):
            continue
        yield {h: ser(v) for h, v in zip(headers, row)}

def create_tables(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS master_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        unit_standar TEXT UNIQUE,
        ss6_id TEXT, sap_id TEXT, vendor TEXT,
        kategori_1 TEXT, kategori_2 TEXT, klasifikasi TEXT,
        asset_type TEXT, status TEXT DEFAULT 'ACTIVE'
    );
    CREATE TABLE IF NOT EXISTS master_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        unit_standar TEXT, ss6_id TEXT, sap_id TEXT,
        vendor TEXT, kategori_1 TEXT, kategori_2 TEXT, klasifikasi TEXT,
        normalized TEXT
    );
    CREATE TABLE IF NOT EXISTS fuel_trucks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        unit_ss6 TEXT, unit_sap TEXT, display_name TEXT
    );
    CREATE TABLE IF NOT EXISTS ss6_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        unit TEXT, normalized TEXT, date TEXT, shift INTEGER,
        vol REAL, storage_location TEXT
    );
    CREATE TABLE IF NOT EXISTS sap_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, qty REAL, order_id TEXT, text_val TEXT,
        storage_location TEXT, unit_sap TEXT, normalized TEXT
    );
    CREATE TABLE IF NOT EXISTS closings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, year INTEGER, month INTEGER, day INTEGER,
        shift INTEGER, fuel_truck TEXT,
        stock_awal_adm REAL, in_vol REAL, out_vol REAL,
        stock_akhir_adm REAL, stock_aktual REAL, deviasi REAL
    );
    CREATE TABLE IF NOT EXISTS vouchers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nomor TEXT, date TEXT, liter REAL,
        no_lambung TEXT, nama_unit TEXT,
        odometer INTEGER, lambung_sap TEXT, kategori TEXT
    );
    CREATE TABLE IF NOT EXISTS bib_mapping (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voucher TEXT, ss6 TEXT, sap TEXT, kategori TEXT
    );
    CREATE TABLE IF NOT EXISTS rekapans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, year INTEGER, month INTEGER, day INTEGER,
        shift INTEGER, fuel_truck TEXT, qty_rekapan REAL
    );
    CREATE TABLE IF NOT EXISTS penerimaans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, qty REAL, unit TEXT, storage_location TEXT,
        movement_type TEXT, doc_text TEXT, material_doc TEXT,
        order_id TEXT, text_val TEXT
    );
    CREATE TABLE IF NOT EXISTS sarana_consumption (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        unit_standar TEXT UNIQUE, vendor TEXT, kategori_2 TEXT,
        total_liter REAL, km_awal REAL, km_akhir REAL, total_km REAL,
        km_per_liter REAL, standart REAL, status TEXT
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nrp TEXT, nama TEXT, role TEXT
    );
    CREATE TABLE IF NOT EXISTS dashboard_config (
        key TEXT PRIMARY KEY, value TEXT
    );
    CREATE TABLE IF NOT EXISTS dashboard_summary (
        key TEXT PRIMARY KEY, value TEXT
    );
    """)

def build():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    create_tables(conn)

    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True, read_only=True)

    # ── 1. UNIT_ALIAS → master_assets + master_aliases ──
    ws = wb["UNIT_ALIAS"]
    assets = {}
    aliases = []
    for r in sheet_rows(ws):
        us = r.get("UNIT_STANDAR", "")
        if not us:
            continue
        s6 = r.get("NO_LAMBUNG_SS6", "")
        sp = r.get("NO_LAMBUNG_SAP", "")
        vd = r.get("VENDOR", "")
        k1 = r.get("KATEGORI_1", "")
        k2 = r.get("KATEGORI_2", "")
        kl = r.get("KLASIFIKASI", "")

        if us not in assets:
            assets[us] = [us, s6, sp, vd, k1, k2, kl, "", "ACTIVE"]
        else:
            a = assets[us]
            if not a[1] and s6: a[1] = s6
            if not a[2] and sp: a[2] = sp

        aliases.append((us, s6, sp, vd, k1, k2, kl, normalize_unit(us)))

    # Determine asset_type
    for us, a in assets.items():
        k2u = (a[5] or "").upper()
        k1u = (a[4] or "").upper()
        if "MAIN TANK" in k2u or "MAINTANK" in k2u:
            a[7] = "MAIN TANK"
        elif "FUEL TRUCK" in k2u or "POMPA" in k2u or "FT" in k1u:
            a[7] = "FUEL TRUCK"
        elif "VENDOR" in k2u or "MANDAR" in (a[3] or "").upper():
            a[7] = "VENDOR FT"
        elif "BUS" in k2u or "BUS" in k1u:
            a[7] = "BUS"
        elif "DERIGEN" in k2u or "JERIGEN" in k2u:
            a[7] = "DERIGEN"
        else:
            a[7] = "SARANA"

    conn.executemany("INSERT OR IGNORE INTO master_assets (unit_standar,ss6_id,sap_id,vendor,kategori_1,kategori_2,klasifikasi,asset_type,status) VALUES (?,?,?,?,?,?,?,?,?)",
                     list(assets.values()))
    conn.executemany("INSERT INTO master_aliases (unit_standar,ss6_id,sap_id,vendor,kategori_1,kategori_2,klasifikasi,normalized) VALUES (?,?,?,?,?,?,?,?)",
                     aliases)
    print(f"  assets: {len(assets)}, aliases: {len(aliases)}")

    # ── 2. Fuel trucks ──
    ws = wb["DATABASE FUEL TRUCK & MAINTANK"]
    fts = []
    for r in sheet_rows(ws):
        s6 = str(r.get("NO_LAMBUNG_UNIT_SS6", "") or "")
        sp = str(r.get("NO_LAMBUNG_SAP", "") or "")
        if not s6 and not sp:
            continue
        fts.append((s6, sp, f"FT-{s6}" if s6 else f"SAP-{sp}"))
    conn.executemany("INSERT INTO fuel_trucks (unit_ss6,unit_sap,display_name) VALUES (?,?,?)", fts)
    print(f"  fuel_trucks: {len(fts)}")

    # ── 3. SS6 ──
    ws = wb["SS6"]
    batch = []
    count = 0
    for r in sheet_rows(ws):
        unit = r.get("Unit", "")
        if not unit:
            continue
        dt = r.get("Date", "")
        date_str = str(dt).split(" ")[0] if dt else ""
        shift = r.get("Shift")
        vol = r.get("Vol")
        sloc = r.get("Storage_Location", "")
        batch.append((unit, normalize_unit(unit), date_str,
                      int(shift) if shift and str(shift).replace(".0","").isdigit() else shift,
                      float(vol) if vol and str(vol).replace(".","").replace("-","").isdigit() else 0,
                      sloc))
        count += 1
        if len(batch) >= 5000:
            conn.executemany("INSERT INTO ss6_transactions (unit,normalized,date,shift,vol,storage_location) VALUES (?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO ss6_transactions (unit,normalized,date,shift,vol,storage_location) VALUES (?,?,?,?,?,?)", batch)
    print(f"  ss6: {count}")

    # ── 4. SAP ──
    ws = wb["SAP"]
    batch = []
    count = 0
    for r in sheet_rows(ws):
        dt = r.get("TANGGAL", "")
        date_str = str(dt).split(" ")[0] if dt else ""
        qty = r.get("QTY")
        order = r.get("Order", "")
        text = r.get("Text", "")
        sloc = r.get("Storage_Location", "")
        unit_sap = r.get("UNIT_SAP_FIX", "")
        if not dt and not unit_sap:
            continue
        batch.append((date_str,
                      float(qty) if qty and str(qty).replace(".","").replace("-","").isdigit() else 0,
                      order, text, sloc, unit_sap, normalize_unit(unit_sap)))
        count += 1
        if len(batch) >= 5000:
            conn.executemany("INSERT INTO sap_transactions (date,qty,order_id,text_val,storage_location,unit_sap,normalized) VALUES (?,?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO sap_transactions (date,qty,order_id,text_val,storage_location,unit_sap,normalized) VALUES (?,?,?,?,?,?,?)", batch)
    print(f"  sap: {count}")

    # ── 5. Closings ──
    ws = wb["DATABASE CLOSING"]
    batch = []
    for r in sheet_rows(ws):
        yr = r.get("YEAR")
        mo = r.get("MONTH")
        dy = r.get("TANGGAL")
        sh = r.get("SHIFT")
        ft = r.get("FUEL_TRUCK", "")
        if not ft:
            continue
        sa = r.get("STOCK_AWAL_ADMINISTRASI")
        iv = r.get("IN")
        ov = r.get("OUT")
        sk = r.get("STOCK_AKHIR")
        su = r.get("STOCK_AKTUAL")

        def num(v):
            if v is None or v == "": return 0
            try: return float(v)
            except: return 0

        date_str = f"{int(yr)}-{int(mo):02d}-{int(dy):02d}" if yr and mo and dy else ""
        dev = num(su) - num(sk)
        batch.append((date_str, int(yr) if yr else 0, int(mo) if mo else 0, int(dy) if dy else 0,
                      int(sh) if sh and str(sh).replace(".0","").isdigit() else sh,
                      str(ft).replace(".0",""), num(sa), num(iv), num(ov), num(sk), num(su), round(dev, 2)))
    conn.executemany("""INSERT INTO closings (date,year,month,day,shift,fuel_truck,stock_awal_adm,in_vol,out_vol,stock_akhir_adm,stock_aktual,deviasi)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", batch)
    print(f"  closings: {len(batch)}")

    # ── 6. Vouchers ──
    ws = wb["VOUCHER SARANA BIB"]
    batch = []
    for r in sheet_rows(ws):
        nomor = r.get("Nomor", "")
        tgl = r.get("Tanggal", "")
        liter = r.get("Liter")
        nl = r.get("No_Lambung", "")
        nu = r.get("Nama_Unit", "")
        odo = r.get("Odometer_Pengisian", "")
        ls = r.get("LAMBUNG_SAP", "")
        kat = r.get("KATEGORI", "")
        if not nomor and not tgl:
            continue
        date_str = str(tgl).split(" ")[0] if tgl else ""
        ls_val = None if (ls and str(ls).startswith("#")) else ls
        kat_val = None if (kat and str(kat).startswith("#")) else kat
        batch.append((nomor, date_str,
                      float(liter) if liter and str(liter).replace(".0","").replace("-","").isdigit() else 0,
                      nl, nu,
                      int(odo) if odo and str(odo).replace(".0","").replace("-","").isdigit() else 0,
                      ls_val, kat_val))
    conn.executemany("INSERT INTO vouchers (nomor,date,liter,no_lambung,nama_unit,odometer,lambung_sap,kategori) VALUES (?,?,?,?,?,?,?,?)", batch)
    print(f"  vouchers: {len(batch)}")

    # ── 7. BIB mapping ──
    ws = wb["BIB"]
    batch = []
    for r in sheet_rows(ws):
        v = str(r.get("VOUCHER", "") or "")
        s6 = str(r.get("SS6", "") or "")
        sp = str(r.get("SAP", "") or "")
        k = r.get("KATEGORI", "")
        if not v and not s6 and not sp:
            continue
        batch.append((v, s6, sp, k))
    conn.executemany("INSERT INTO bib_mapping (voucher,ss6,sap,kategori) VALUES (?,?,?,?)", batch)
    print(f"  bib_mapping: {len(batch)}")

    # ── 8. Rekapans ──
    ws = wb["REKAPAN"]
    batch = []
    for r in sheet_rows(ws):
        yr = r.get("YEAR")
        mo = r.get("MONTH")
        dy = r.get("TANGGAL")
        sh = r.get("SHIFT")
        ft = r.get("FT", "")
        qty = r.get("QTY_REKAPAN")
        if not ft:
            continue
        date_str = f"{int(yr)}-{int(mo):02d}-{int(dy):02d}" if yr and mo and dy else ""
        batch.append((date_str, int(yr) if yr else 0, int(mo) if mo else 0, int(dy) if dy else 0,
                      int(sh) if sh and str(sh).replace(".0","").isdigit() else sh,
                      str(ft).replace(".0",""),
                      float(qty) if qty and str(qty).replace(".0","").replace("-","").isdigit() else 0))
    conn.executemany("INSERT INTO rekapans (date,year,month,day,shift,fuel_truck,qty_rekapan) VALUES (?,?,?,?,?,?,?)", batch)
    print(f"  rekapans: {len(batch)}")

    # ── 9. Penerimaans ──
    ws = wb["PENERIMAAN"]
    batch = []
    for r in sheet_rows(ws):
        pd = r.get("Posting_Date", "")
        qty = r.get("Qty_in_Un__of_Entry", "")
        unit = r.get("Unit_of_Entry", "")
        sloc = r.get("Storage_Location", "")
        mvt = r.get("Movement_Type", "")
        dt = r.get("Document_Header_Text", "")
        md = r.get("Material_Document", "")
        order = r.get("Order", "")
        text = r.get("Text", "")
        date_str = str(pd).split(" ")[0] if pd else ""
        batch.append((date_str,
                      float(qty) if qty and str(qty).replace(".","").replace("-","").isdigit() else 0,
                      unit or "", sloc or "", str(mvt) if mvt else "",
                      dt or "", str(md) if md else "", order or "", text or ""))
    conn.executemany("""INSERT INTO penerimaans (date,qty,unit,storage_location,movement_type,doc_text,material_doc,order_id,text_val)
                        VALUES (?,?,?,?,?,?,?,?,?)""", batch)
    print(f"  penerimaans: {len(batch)}")

    # ── 10. Sarana ──
    ws = wb["SARANA"]
    seen = {}
    for r in sheet_rows(ws):
        unit = r.get("UNIT_STANDAR", "")
        if not unit or str(unit).startswith("="):
            continue
        def num(v):
            if v is None or v == "": return 0
            try: return float(v)
            except: return 0
        seen[unit] = (unit, r.get("VENDOR", ""), r.get("KATEGORI_2", ""),
                      num(r.get("TOTAL_LITER")), num(r.get("KM_AWAL")), num(r.get("KM_AKHIR")),
                      num(r.get("TOTAL_KM")), num(r.get("KM_LITER") or r.get("KM_PER_LITER", 0)),
                      num(r.get("STANDART")), r.get("STATUS", ""))
    conn.executemany("""INSERT OR REPLACE INTO sarana_consumption (unit_standar,vendor,kategori_2,total_liter,km_awal,km_akhir,total_km,km_per_liter,standart,status)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""", list(seen.values()))
    print(f"  sarana: {len(seen)}")

    # ── 11. Users ──
    ws = wb["DATABASE USER"]
    batch = []
    for r in sheet_rows(ws):
        nrp = str(r.get("NRP", "") or "").replace(".0", "")
        nama = r.get("NAMA", "")
        role = r.get("ROLE", "")
        if not nrp and not nama:
            continue
        batch.append((nrp, nama, role))
    conn.executemany("INSERT INTO users (nrp,nama,role) VALUES (?,?,?)", batch)
    print(f"  users: {len(batch)}")

    # ── 12. Dashboard config ──
    ws = wb["Dashboard_Setup"]
    for r in sheet_rows(ws):
        vals = list(r.values())
        if vals and vals[0]:
            conn.execute("INSERT OR REPLACE INTO dashboard_config (key,value) VALUES (?,?)",
                         (str(vals[0]).strip(), str(vals[1]).strip() if len(vals) > 1 and vals[1] else ""))

    # ── 13. Summary ──
    summary = {
        "total_units": conn.execute("SELECT COUNT(*) FROM master_assets").fetchone()[0],
        "total_aliases": conn.execute("SELECT COUNT(*) FROM master_aliases").fetchone()[0],
        "total_ss6_records": conn.execute("SELECT COUNT(*) FROM ss6_transactions").fetchone()[0],
        "total_sap_records": conn.execute("SELECT COUNT(*) FROM sap_transactions").fetchone()[0],
        "total_closings": conn.execute("SELECT COUNT(*) FROM closings").fetchone()[0],
        "total_vouchers": conn.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0],
        "total_sarana_records": conn.execute("SELECT COUNT(*) FROM sarana_consumption").fetchone()[0],
        "total_users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "total_fuel_trucks": conn.execute("SELECT COUNT(*) FROM fuel_trucks").fetchone()[0],
        "total_penerimaans": conn.execute("SELECT COUNT(*) FROM penerimaans").fetchone()[0],
        "total_rekapans": conn.execute("SELECT COUNT(*) FROM rekapans").fetchone()[0],
        "total_bib_mappings": conn.execute("SELECT COUNT(*) FROM bib_mapping").fetchone()[0],
        "month": "Juni 2026",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    for k, v in summary.items():
        conn.execute("INSERT OR REPLACE INTO dashboard_summary (key,value) VALUES (?,?)", (k, str(v)))

    conn.commit()

    print("\n=== DATABASE BUILD COMPLETE ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\nDatabase: {DB_PATH} ({os.path.getsize(DB_PATH) / 1024 / 1024:.1f} MB)")

    conn.close()

if __name__ == "__main__":
    build()