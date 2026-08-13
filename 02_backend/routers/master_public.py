"""Public read-only endpoints untuk master data legacy.

Bundle user 2026-08-11 mengidentifikasi bahwa tabel master (master_main_tank,
master_fuel_truck, master_jalur, master_vendor, ft_mandar_ocean, fuel_route_config)
TIDAK ADA di Supabase. Frontend harus baca dari Postgres lokal via endpoint
public ini (no auth) supaya field-app bisa kerja tanpa harus login.

Endpoint ini HANYA baca data master sederhana (kode, nama, kapasitas), tidak
expose data sensitif (audit, password, dll).
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ..db import fetch_all

router = APIRouter(prefix="/api/v1/master", tags=["master"])


@router.get("/legacy-tank")
def legacy_tanks(status: str | None = Query(None, description="Filter ACTIVE/INACTIVE")):
    """Legacy main tanks (master_main_tank). Format sama dengan Supabase fuel_master_tandon."""
    where = ""
    params: list = []
    if status:
        where = " WHERE status = %s"
        params = [status]
    rows = fetch_all(
        f"SELECT kode, nama, kapasitas_l, status FROM fcc.master_main_tank{where} ORDER BY kode",
        tuple(params) if params else None,
    )

    # Lookup UUID dari fuel_master_tandon
    uuid_rows = fetch_all(
        "SELECT tandon_code, id::text AS uid FROM fcc.fuel_master_tandon"
    )
    uuid_map = {r["tandon_code"]: r["uid"] for r in uuid_rows}

    # Return dalam 2 format: legacy ('kode') dan Supabase ('tandon_code') untuk kompatibilitas frontend
    return {
        "data": [
            {
                "kode": r["kode"],
                "tandon_code": r["kode"],
                "nama": r["nama"],
                "tandon_name": r["nama"],
                "id": uuid_map.get(r["kode"]),  # UUID untuk Supabase-style payload
                "kapasitas_l": float(r["kapasitas_l"]) if r["kapasitas_l"] is not None else None,
                "status": r["status"],
            }
            for r in rows
        ]
    }


@router.get("/legacy-jalur")
def legacy_jalurs(status: str | None = Query(None, description="Filter ACTIVE/INACTIVE")):
    """Legacy jalur (master_jalur). Sertakan UUID dari fuel_master_jalur kalau ada."""
    where = ""
    params: list = []
    if status:
        where = " WHERE status = %s"
        params = [status]
    rows = fetch_all(
        f"SELECT kode, nama, status FROM fcc.master_jalur{where} ORDER BY kode",
        tuple(params) if params else None,
    )

    # Lookup UUID dari fuel_master_jalur (mapping text code → UUID)
    uuid_rows = fetch_all(
        "SELECT jalur_code, id::text AS uid FROM fcc.fuel_master_jalur"
    )
    uuid_map = {r["jalur_code"]: r["uid"] for r in uuid_rows}

    return {
        "data": [
            {
                "kode": r["kode"],
                "jalur_code": r["kode"],
                "nama": r["nama"],
                "jalur_name": r["nama"],
                "id": uuid_map.get(r["kode"]),  # UUID untuk Supabase-style payload
                "status": r["status"],
            }
            for r in rows
        ]
    }


@router.get("/legacy-fuel-truck")
def legacy_fuel_trucks(status: str | None = Query(None, description="Filter ACTIVE/INACTIVE")):
    """Legacy fuel trucks (master_fuel_truck)."""
    where = ""
    params: list = []
    if status:
        where = " WHERE status = %s"
        params = [status]
    rows = fetch_all(
        f"SELECT kode, nama, tipe, kapasitas_l, status FROM fcc.master_fuel_truck{where} ORDER BY kode",
        tuple(params) if params else None,
    )

    # Lookup UUID dari fuel_master_fuel_truck (mapping text code → UUID)
    uuid_rows = fetch_all(
        "SELECT unit_code, id::text AS uid FROM fcc.fuel_master_fuel_truck"
    )
    uuid_map = {r["unit_code"]: r["uid"] for r in uuid_rows}

    return {
        "data": [
            {
                "kode": r["kode"],
                "unit_code": r["kode"],
                "nama": r["nama"],
                "unit_name": r["nama"],
                "id": uuid_map.get(r["kode"]),  # UUID untuk Supabase-style payload
                "tipe": r["tipe"],
                "kapasitas_l": float(r["kapasitas_l"]) if r["kapasitas_l"] is not None else None,
                "status": r["status"],
            }
            for r in rows
        ]
    }


@router.get("/legacy-vendor")
def legacy_vendors(status: str | None = Query(None)):
    """Legacy vendors (master_vendor)."""
    where = ""
    params: list = []
    if status:
        where = " WHERE status = %s"
        params = [status]
    rows = fetch_all(
        f"SELECT kode, nama, kategori, status FROM fcc.master_vendor{where} ORDER BY kode",
        tuple(params) if params else None,
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/ft-mandar-ocean")
def ft_mandar_ocean_all():
    """FT Mandar Ocean (ft_mandar_ocean) — untuk dropdown di form receiving."""
    rows = fetch_all(
        "SELECT id_ft, no_polisi, kapasitas_l, t2_depan_cm, t2_belakang_cm, expired_komisioning, masa_berlaku, status "
        "FROM fcc.ft_mandar_ocean ORDER BY id_ft"
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/route-config")
def route_config_all(
    peruntukan: str | None = Query(None, description="TRANSFER/RECEIVING"),
    status: str | None = Query(None, description="VALIDATED/DRAFT"),
):
    """fuel_route_config dengan join ke master_jalur & master_main_tank.
    Frontend pakai 'jalur_code' dan 'tandon_code' (lowercase)."""
    where_clauses = []
    params: list = []
    if peruntukan:
        where_clauses.append("rc.peruntukan = %s")
        params.append(peruntukan)
    if status:
        where_clauses.append("rc.status = %s")
        params.append(status)

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT
            rc.id,
            rc.tanggal,
            rc.shift,
            rc.peruntukan,
            rc.status,
            rc.fm_akhir_shift_sebelumnya,
            rc.fm_aktual_awal,
            rc.jalur_id::text AS jalur_id,
            rc.tandon_id::text AS tandon_id,
            mj.kode AS jalur_code,
            mt.kode AS tandon_code
        FROM fcc.fuel_route_config rc
        LEFT JOIN fcc.master_jalur mj ON mj.kode = rc.jalur_id::text
        LEFT JOIN fcc.master_main_tank mt ON mt.kode = rc.tandon_id::text
        {where_sql}
        ORDER BY rc.tanggal DESC, rc.shift DESC
        LIMIT 500
    """
    rows = fetch_all(sql, tuple(params) if params else None)
    return {"data": [dict(r) for r in rows]}


@router.get("/fm-awal-settings")
def fm_awal_settings_all():
    """fuel_fm_awal_settings per jalur."""
    rows = fetch_all(
        "SELECT id, jalur_id::text AS jalur_id, mode, fm_awal_manual, notes, "
        "created_at, updated_at FROM fcc.fuel_fm_awal_settings ORDER BY created_at"
    )
    # Tambah field null-safe
    return {
        "data": [
            {
                **dict(r),
                "fm_awal_manual": float(r["fm_awal_manual"]) if r.get("fm_awal_manual") is not None else None,
            }
            for r in rows
        ]
    }
