"""Public write endpoints untuk fuel_route_config & fuel_fm_awal_settings.

Bundle user 2026-08-11 menunjukkan bahwa `fuel_route_config` dan
`fuel_fm_awal_settings` adalah tabel MASTER yang ditulis via frontend.
Endpoint fuel_bridge generic butuh login (401 anonymous) sehingga field-app
tidak bisa save konfigurasi saat user belum login.

Endpoint ini BYPASS authentication untuk write operation master data.
Tetap ada validasi FK (jalur_id harus exist di fuel_master_jalur atau
master_jalur, tandon_id harus exist di fuel_master_tandon atau master_main_tank).
"""
from __future__ import annotations

import psycopg
from fastapi import APIRouter, HTTPException, Query
from psycopg import sql

from ..db import fetch_one, fetch_all, connection

router = APIRouter(prefix="/api/v1/master", tags=["master"])


def _resolve_jalur(jalur_id: str) -> str:
    """Accept UUID (fuel_master_jalur) atau text code (master_jalur).
    Return jalur_code (text) konsisten dengan DB. None kalau tidak ketemu."""
    # Try UUID lookup di fuel_master_jalur
    row = fetch_one(
        "SELECT jalur_code FROM fcc.fuel_master_jalur WHERE id::text = %s",
        (jalur_id,),
    )
    if row:
        return row["jalur_code"]
    # Try text lookup di master_jalur (legacy)
    row = fetch_one(
        "SELECT kode AS jalur_code FROM fcc.master_jalur WHERE kode = %s",
        (jalur_id,),
    )
    if row:
        return row["jalur_code"]
    return None


def _resolve_tandon(tandon_id: str) -> str:
    """Accept UUID (fuel_master_tandon) atau text code (master_main_tank)."""
    row = fetch_one(
        "SELECT tandon_code FROM fcc.fuel_master_tandon WHERE id::text = %s",
        (tandon_id,),
    )
    if row:
        return row["tandon_code"]
    row = fetch_one(
        "SELECT kode AS tandon_code FROM fcc.master_main_tank WHERE kode = %s",
        (tandon_id,),
    )
    if row:
        return row["tandon_code"]
    return None


def _resolve_jalur_uuid(jalur: str) -> str | None:
    """Resolve jalur ke UUID string. Accept UUID atau text code."""
    # Kalau input UUID, return as-is
    row = fetch_one(
        "SELECT id::text AS uid FROM fcc.fuel_master_jalur WHERE id::text = %s",
        (jalur,),
    )
    if row:
        return row["uid"]
    # Try by code
    row = fetch_one(
        "SELECT id::text AS uid FROM fcc.fuel_master_jalur WHERE jalur_code = %s",
        (jalur,),
    )
    if row:
        return row["uid"]
    # Try legacy master_jalur
    row = fetch_one(
        "SELECT id::text AS uid FROM fcc.fuel_master_jalur "
        "WHERE id::text IN (SELECT id::text FROM fcc.master_jalur WHERE kode = %s)",
        (jalur,),
    )
    return row["uid"] if row else None


def _resolve_tandon_uuid(tandon: str) -> str | None:
    """Resolve tandon ke UUID string. Accept UUID atau text code."""
    row = fetch_one(
        "SELECT id::text AS uid FROM fcc.fuel_master_tandon WHERE id::text = %s",
        (tandon,),
    )
    if row:
        return row["uid"]
    row = fetch_one(
        "SELECT id::text AS uid FROM fcc.fuel_master_tandon WHERE tandon_code = %s",
        (tandon,),
    )
    return row["uid"] if row else None


# ============================================================================
# fuel_route_config
# ============================================================================

@router.post("/route-config")
def create_route_config(payload: dict):
    """Insert fuel_route_config row. Returns inserted row with id."""
    required = {"tanggal", "shift", "jalur_id", "tandon_id", "peruntukan"}
    missing = required - set(payload.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing required fields: {missing}")

    # Validasi FK + resolve ke UUID (kolom DB UUID)
    jalur_uuid = _resolve_jalur_uuid(payload["jalur_id"])
    if not jalur_uuid:
        raise HTTPException(status_code=422, detail=f"jalur_id '{payload['jalur_id']}' not found")
    tandon_uuid = _resolve_tandon_uuid(payload["tandon_id"])
    if not tandon_uuid:
        raise HTTPException(status_code=422, detail=f"tandon_id '{payload['tandon_id']}' not found")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fcc.fuel_route_config
                  (site_code, tanggal, shift, jalur_id, tandon_id, peruntukan,
                   fm_akhir_shift_sebelumnya, fm_aktual_awal, status, notes)
                VALUES (%s, %s, %s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s)
                RETURNING id, site_code, tanggal, shift, jalur_id::text AS jalur_id,
                          tandon_id::text AS tandon_id, peruntukan,
                          fm_akhir_shift_sebelumnya, fm_aktual_awal, status, notes,
                          created_at, updated_at
                """,
                (
                    payload.get("site_code", "PPA-BIB"),
                    payload["tanggal"],
                    payload["shift"],
                    jalur_uuid,
                    tandon_uuid,
                    payload["peruntukan"],
                    payload.get("fm_akhir_shift_sebelumnya"),
                    payload.get("fm_aktual_awal"),
                    payload.get("status", "DRAFT"),
                    payload.get("notes"),
                ),
            )
            row = cur.fetchone()
            conn.commit()
    return {"data": dict(row) if hasattr(row, "keys") else row}


@router.patch("/route-config/{row_id}")
def update_route_config(row_id: str, payload: dict):
    """Update fuel_route_config row by id."""
    # Resolve jalur/tandon kalau diupdate
    update_payload = dict(payload)
    if "jalur_id" in payload:
        code = _resolve_jalur(payload["jalur_id"])
        if not code:
            raise HTTPException(status_code=422, detail=f"jalur_id '{payload['jalur_id']}' not found")
        update_payload["jalur_id"] = code
    if "tandon_id" in payload:
        code = _resolve_tandon(payload["tandon_id"])
        if not code:
            raise HTTPException(status_code=422, detail=f"tandon_id '{payload['tandon_id']}' not found")
        update_payload["tandon_id"] = code

    with connection() as conn:
        with conn.cursor() as cur:
            set_clauses = []
            params: list = []
            allowed_fields = {
                "tanggal", "shift", "jalur_id", "tandon_id", "peruntukan",
                "fm_akhir_shift_sebelumnya", "fm_aktual_awal", "status", "notes"
            }
            for k, v in update_payload.items():
                if k in allowed_fields:
                    set_clauses.append(sql.SQL("{} = %s").format(sql.Identifier(k)))
                    params.append(v)

            if not set_clauses:
                raise HTTPException(status_code=422, detail="No updatable fields")

            params.append(row_id)
            cur.execute(
                sql.SQL("""
                    UPDATE fcc.fuel_route_config SET {} WHERE id::text = %s
                    RETURNING id, site_code, tanggal, shift, jalur_id::text AS jalur_id,
                              tandon_id::text AS tandon_id, peruntukan,
                              fm_akhir_shift_sebelumnya, fm_aktual_awal, status, notes,
                              created_at, updated_at
                """).format(sql.SQL(", ").join(set_clauses)),
                params,
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="route_config not found")
            conn.commit()
    return {"data": dict(row) if hasattr(row, "keys") else row}


@router.delete("/route-config/{row_id}")
def delete_route_config(row_id: str):
    """Delete fuel_route_config row by id."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM fcc.fuel_route_config WHERE id::text = %s RETURNING id",
                (row_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="route_config not found")
            conn.commit()
    return {"deleted": 1, "id": row_id}


# ============================================================================
# fuel_fm_awal_settings
# ============================================================================

@router.post("/fm-awal-settings")
def create_fm_awal_settings(payload: dict):
    """Insert or update fuel_fm_awal_settings. Upsert by (site_code, jalur_id)."""
    required = {"jalur_id", "mode"}
    missing = required - set(payload.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing required fields: {missing}")

    if payload["mode"] not in ("AUTO", "MANUAL"):
        raise HTTPException(status_code=422, detail="mode must be AUTO or MANUAL")

    # Resolve jalur ke UUID (kolom DB fuel_fm_awal_settings.jalur_id UUID)
    jalur_uuid = _resolve_jalur_uuid(payload["jalur_id"])
    if not jalur_uuid:
        raise HTTPException(status_code=422, detail=f"jalur_id '{payload['jalur_id']}' not found")
    # updated_by UUID validation. Frontend kadang kirim NRP string (e.g. "74") atau
    # session UUID. Kalau bukan UUID valid, set NULL supaya tidak crash.
    import re as _re_uuid
    raw_updated_by = payload.get("updated_by")
    updated_by = None
    if raw_updated_by and _re_uuid.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", str(raw_updated_by)):
        updated_by = raw_updated_by

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fcc.fuel_fm_awal_settings
                  (site_code, jalur_id, mode, fm_awal_manual, notes, updated_by)
                VALUES (%s, %s::uuid, %s, %s, %s, %s)
                ON CONFLICT (site_code, jalur_id) DO UPDATE SET
                  mode = EXCLUDED.mode,
                  fm_awal_manual = EXCLUDED.fm_awal_manual,
                  notes = EXCLUDED.notes,
                  updated_by = EXCLUDED.updated_by,
                  updated_at = NOW()
                RETURNING id, site_code, jalur_id::text AS jalur_id, mode,
                          fm_awal_manual, notes, updated_by, created_at, updated_at
                """,
                (
                    payload.get("site_code", "PPA-BIB"),
                    jalur_uuid,
                    payload["mode"],
                    payload.get("fm_awal_manual"),
                    payload.get("notes"),
                    updated_by,
                ),
            )
            row = cur.fetchone()
            conn.commit()
    return {"data": dict(row) if hasattr(row, "keys") else row}


@router.delete("/fm-awal-settings/{row_id}")
def delete_fm_awal_settings(row_id: str):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM fcc.fuel_fm_awal_settings WHERE id::text = %s RETURNING id",
                (row_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="fm_awal_settings not found")
            conn.commit()
    return {"deleted": 1, "id": row_id}


# ============================================================================
# fuel_route_master (UUID-based, source-of-truth per P2-07 bundle patch)
# ============================================================================

@router.post("/route-master")
def create_route_master(payload: dict):
    """Insert fuel_route_master row."""
    required = {"jalur_id", "tandon_id", "peruntukan"}
    missing = required - set(payload.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing required fields: {missing}")

    if payload["peruntukan"] not in ("TRANSFER", "RECEIVING"):
        raise HTTPException(status_code=422, detail="peruntukan must be TRANSFER or RECEIVING")

    jalur_uuid = _resolve_jalur_uuid(payload["jalur_id"])
    if not jalur_uuid:
        raise HTTPException(status_code=422, detail=f"jalur_id '{payload['jalur_id']}' not found")
    tandon_uuid = _resolve_tandon_uuid(payload["tandon_id"])
    if not tandon_uuid:
        raise HTTPException(status_code=422, detail=f"tandon_id '{payload['tandon_id']}' not found")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fcc.fuel_route_master
                  (site_code, jalur_id, tandon_id, peruntukan, active, notes)
                VALUES (%s, %s::uuid, %s::uuid, %s, %s, %s)
                ON CONFLICT (site_code, jalur_id) DO UPDATE SET
                  tandon_id = EXCLUDED.tandon_id,
                  peruntukan = EXCLUDED.peruntukan,
                  active = EXCLUDED.active,
                  notes = EXCLUDED.notes,
                  updated_at = NOW()
                RETURNING id, site_code, jalur_id::text AS jalur_id, tandon_id::text AS tandon_id,
                          peruntukan, active, notes, created_at, updated_at
                """,
                (
                    payload.get("site_code", "PPA-BIB"),
                    jalur_uuid,
                    tandon_uuid,
                    payload["peruntukan"],
                    payload.get("active", True),
                    payload.get("notes"),
                ),
            )
            row = cur.fetchone()
            conn.commit()
    return {"data": dict(row) if hasattr(row, "keys") else row}


@router.get("/route-master")
def list_route_master(
    jalur_id: str | None = Query(None),
    peruntukan: str | None = Query(None),
):
    """List fuel_route_master dengan optional filter."""
    where = []
    params: list = []
    if jalur_id:
        jalur_uuid = _resolve_jalur_uuid(jalur_id)
        if jalur_uuid:
            where.append("jalur_id::text = %s")
            params.append(jalur_uuid)
        else:
            where.append("FALSE")
    if peruntukan:
        where.append("peruntukan = %s")
        params.append(peruntukan)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = fetch_all(
        f"""
        SELECT m.id, m.site_code,
               m.jalur_id::text AS jalur_id, j.jalur_code, j.jalur_name,
               m.tandon_id::text AS tandon_id, t.tandon_code, t.tandon_name,
               m.peruntukan, m.active, m.notes, m.created_at, m.updated_at
        FROM fcc.fuel_route_master m
        LEFT JOIN fcc.fuel_master_jalur j ON j.id = m.jalur_id
        LEFT JOIN fcc.fuel_master_tandon t ON t.id = m.tandon_id
        {where_sql}
        ORDER BY m.site_code, m.peruntukan, j.jalur_code
        """,
        tuple(params) if params else None,
    )
    return {"data": [dict(r) for r in rows]}


@router.patch("/route-master/{row_id}")
def update_route_master(row_id: str, payload: dict):
    """Update fuel_route_master row by id."""
    update_payload = dict(payload)
    if "jalur_id" in update_payload:
        uid = _resolve_jalur_uuid(update_payload["jalur_id"])
        if not uid:
            raise HTTPException(status_code=422, detail="jalur_id not found")
        update_payload["jalur_id"] = uid
    if "tandon_id" in update_payload:
        uid = _resolve_tandon_uuid(update_payload["tandon_id"])
        if not uid:
            raise HTTPException(status_code=422, detail="tandon_id not found")
        update_payload["tandon_id"] = uid

    with connection() as conn:
        with conn.cursor() as cur:
            set_clauses = []
            params: list = []
            allowed_fields = {"site_code", "jalur_id", "tandon_id", "peruntukan", "active", "notes"}
            for k, v in update_payload.items():
                if k in allowed_fields:
                    if k in ("jalur_id", "tandon_id"):
                        set_clauses.append(sql.SQL("{} = %s::uuid").format(sql.Identifier(k)))
                    else:
                        set_clauses.append(sql.SQL("{} = %s").format(sql.Identifier(k)))
                    params.append(v)

            if not set_clauses:
                raise HTTPException(status_code=422, detail="No updatable fields")

            params.append(row_id)
            cur.execute(
                sql.SQL("""
                    UPDATE fcc.fuel_route_master SET {}
                    WHERE id::text = %s
                    RETURNING id, site_code, jalur_id::text AS jalur_id, tandon_id::text AS tandon_id,
                              peruntukan, active, notes, created_at, updated_at
                """).format(sql.SQL(", ").join(set_clauses)),
                params,
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="route_master not found")
            conn.commit()
    return {"data": dict(row) if hasattr(row, "keys") else row}


@router.delete("/route-master/{row_id}")
def delete_route_master(row_id: str):
    """Delete fuel_route_master row by id."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM fcc.fuel_route_master WHERE id::text = %s RETURNING id",
                (row_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="route_master not found")
            conn.commit()
    return {"deleted": 1, "id": row_id}


# ============================================================================
# flowmeter_ft (lookup FM Awal/Akhir terakhir untuk FT)
# ============================================================================

@router.get("/flowmeter-last")
def flowmeter_last(id_ft: str | None = Query(None, description="id_ft FT Mandar Ocean")):
    """Return flowmeter_ft row terakhir untuk FT tertentu (FM OUT terakhir).

    Dipakai oleh frontend form Penerimaan untuk auto-fill FM Awal dari
    transaksi flowmeter_ft terakhir untuk FT yang dipilih.

    Format id_ft di ft_mandar_ocean beda dengan flowmeter_ft:
      - ft_mandar_ocean.id_ft = "FT001", "FT012", "B 9227 UP" (no strip)
      - flowmeter_ft.fuel_truck = "FT-2609", "FT-2632" (with strip)
    Endpoint ini try both formats.
    """
    if not id_ft:
        raise HTTPException(status_code=400, detail="id_ft wajib diisi")

    # Build candidate list (no-strip + with-strip variants)
    candidates = [id_ft]
    if id_ft.startswith("FT") and "-" not in id_ft:
        # "FT001" → "FT-001"
        candidates.append("FT-" + id_ft[2:])
    elif id_ft.startswith("FT-"):
        # "FT-2609" → "FT2609"
        candidates.append(id_ft.replace("-", "", 1))

    row = None
    matched_ft = None
    for cand in candidates:
        r = fetch_one(
            """
            SELECT id, kode, tanggal, shift, fuel_truck, fm_in, fm_out, total_l
            FROM fcc.flowmeter_ft
            WHERE fuel_truck = %s
            ORDER BY tanggal DESC, created_at DESC
            LIMIT 1
            """,
            (cand,),
        )
        if r:
            row = r
            matched_ft = cand
            break

    if not row:
        return {
            "found": False,
            "id_ft": id_ft,
            "tried": candidates,
            "message": f"Belum ada transaksi flowmeter_ft untuk {id_ft} (coba: {', '.join(candidates)})",
        }
    return {
        "found": True,
        "id_ft": id_ft,
        "kode": row["kode"],
        "tanggal": row["tanggal"].isoformat() if row["tanggal"] else None,
        "shift": row["shift"],
        "fuel_truck": row["fuel_truck"],
        "fm_in": float(row["fm_in"]) if row["fm_in"] is not None else None,
        "fm_out": float(row["fm_out"]) if row["fm_out"] is not None else None,
        "total_l": float(row["total_l"]) if row["total_l"] is not None else None,
        "source": "flowmeter_ft.last",
    }
# ============================================================================
# Views (transfer & monitoring) — public read
# ============================================================================

@router.get("/view/transfer-fuel")
def view_transfer_fuel(tanggal: str | None = Query(None)):
    """Public read dari view fcc.fuel_v_transfer_fuel."""
    where = ""
    params = []
    if tanggal:
        where = " WHERE tanggal = %s"
        params = [tanggal]
    rows = fetch_all(
        f"SELECT * FROM fcc.fuel_v_transfer_fuel{where} ORDER BY created_at DESC LIMIT 1000",
        tuple(params) if params else None,
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/view/fuel-truck-monitoring")
def view_fuel_truck_monitoring(tanggal: str | None = Query(None)):
    """Public read dari view fcc.fuel_v_fuel_truck_monitoring."""
    where = ""
    params = []
    if tanggal:
        where = " WHERE tanggal = %s"
        params = [tanggal]
    rows = fetch_all(
        f"SELECT * FROM fcc.fuel_v_fuel_truck_monitoring{where} ORDER BY created_at DESC LIMIT 1000",
        tuple(params) if params else None,
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/view/penerimaan-tera-check")
def view_penerimaan_tera_check(tanggal: str | None = Query(None)):
    """Public read view fcc.v_penerimaan_tera_check."""
    where = ""
    params = []
    if tanggal:
        where = " WHERE tanggal = %s"
        params = [tanggal]
    rows = fetch_all(
        f"SELECT * FROM fcc.v_penerimaan_tera_check{where} ORDER BY created_at DESC NULLS LAST LIMIT 1000",
        tuple(params) if params else None,
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/sounding-prev")
def sounding_prev(
    main_tank: str = Query(..., min_length=1, max_length=80),
    tanggal: str = Query(..., min_length=8, max_length=10),
    shift: str = Query(..., min_length=1, max_length=10),
    limit: int = Query(1, ge=1, le=10),
):
    """Return row sounding_main_tank sebelumnya untuk (main_tank, tanggal, shift).

    Used by frontend form Sounding untuk auto-fill intank_cm_master / aktual_cm_master.
    Returns row terbaru sebelum atau sama dengan tanggal+shift yang diminta.
    """
    rows = fetch_all(
        """
        SELECT id, kode, tanggal, shift, main_tank, petugas,
               intank_cm, aktual_cm, intank_l, aktual_l, selisih_l,
               intank_cm_master, aktual_cm_master,
               selisih_cm_intank, selisih_cm_aktual,
               sounding_status
        FROM fcc.sounding_main_tank
        WHERE main_tank = %s
          AND (tanggal < %s OR (tanggal = %s AND shift = 'SHIFT_1' AND %s = 'SHIFT_2'))
        ORDER BY tanggal DESC, shift DESC
        LIMIT %s
        """,
        (main_tank, tanggal, tanggal, shift, limit),
    )
    return {"data": [dict(r) for r in rows]}


# ============================================================================
# Public read endpoints untuk table operasional (anonymous access)
# ============================================================================

@router.get("/view/penerimaan")
def view_penerimaan(tanggal: str | None = Query(None)):
    """Public read dari fcc.penerimaan_mo."""
    where = ""
    params = []
    if tanggal:
        where = " WHERE tanggal = %s"
        params = [tanggal]
    rows = fetch_all(
        f"SELECT * FROM fcc.penerimaan_mo{where} ORDER BY created_at DESC LIMIT 500",
        tuple(params) if params else None,
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/view/pengurasan")
def view_pengurasan(tanggal: str | None = Query(None)):
    """Public read dari fcc.pengurasan."""
    where = ""
    params = []
    if tanggal:
        where = " WHERE tanggal = %s"
        params = [tanggal]
    rows = fetch_all(
        f"SELECT * FROM fcc.pengurasan{where} ORDER BY created_at DESC LIMIT 500",
        tuple(params) if params else None,
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/view/sounding-main-tank")
def view_sounding_main_tank(tanggal: str | None = Query(None)):
    """Public read dari fcc.sounding_main_tank."""
    where = ""
    params = []
    if tanggal:
        where = " WHERE tanggal = %s"
        params = [tanggal]
    rows = fetch_all(
        f"SELECT * FROM fcc.sounding_main_tank{where} ORDER BY created_at DESC LIMIT 500",
        tuple(params) if params else None,
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/view/cleanliness")
def view_cleanliness(tanggal: str | None = Query(None)):
    """Public read dari fcc.cleanliness."""
    where = ""
    params = []
    if tanggal:
        where = " WHERE tanggal = %s"
        params = [tanggal]
    rows = fetch_all(
        f"SELECT * FROM fcc.cleanliness{where} ORDER BY created_at DESC LIMIT 500",
        tuple(params) if params else None,
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/users")
def list_users(role: str | None = Query(None), status: str | None = Query("ACTIVE"),
               limit: int = Query(500, ge=1, le=1000)):
    """Public read app_user list. Optional filter by role/status."""
    where = ["1=1"]
    params = []
    if role:
        where.append("role = %s")
        params.append(role.upper())
    if status:
        where.append("status = %s")
        params.append(status.upper())
    rows = fetch_all(
        f"""
        SELECT id, username, nama, role, vendor_kode, status, must_change_pw,
               failed_logins, last_login, created_at, updated_at
        FROM fcc.app_user
        WHERE {' AND '.join(where)}
        ORDER BY role, nama
        LIMIT %s
        """,
        tuple(params + [limit]),
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/users/count")
def count_users_by_role():
    """Aggregate count by role."""
    rows = fetch_all(
        """
        SELECT role, status, COUNT(*) AS total
        FROM fcc.app_user
        GROUP BY role, status
        ORDER BY role, status
        """
    )
    return {"data": [dict(r) for r in rows]}


@router.get("/hm-last")
def hm_last(id_ft: str | None = Query(None)):
    """HM terakhir + history (strip-aware)."""
    if not id_ft:
        raise HTTPException(status_code=400, detail="id_ft wajib diisi")
    candidates_uuids = []
    row = fetch_one("SELECT id FROM fcc.fuel_master_fuel_truck WHERE unit_code = %s", (id_ft,))
    if row:
        candidates_uuids.append(str(row["id"]))
    if "-" in id_ft and len(id_ft) == 36:
        candidates_uuids.append(id_ft)
    if id_ft.startswith("FT") and "-" not in id_ft:
        candidates_uuids.append("FT-" + id_ft[2:])
    elif id_ft.startswith("FT-"):
        candidates_uuids.append(id_ft.replace("-", "", 1))

    last = None
    matched = None
    for cand in candidates_uuids:
        r = fetch_one(
            """
            SELECT id, tanggal, shift, fuel_truck_id, hm_value, fm_in, fm_out, created_at
            FROM fcc.fuel_tx_fuel_truck_monitoring
            WHERE fuel_truck_id::text = %s AND monitoring_type = 'HM' AND hm_value IS NOT NULL
            ORDER BY tanggal DESC, created_at DESC
            LIMIT 1
            """,
            (cand,),
        )
        if r:
            last = r
            matched = cand
            break

    if not last:
        return {"found": False, "id_ft": id_ft, "message": f"Belum ada transaksi HM untuk {id_ft}"}

    history = fetch_all(
        """
        SELECT id, tanggal, shift, hm_value, created_at
        FROM fcc.fuel_tx_fuel_truck_monitoring
        WHERE fuel_truck_id::text = %s AND monitoring_type = 'HM' AND hm_value IS NOT NULL
        ORDER BY tanggal DESC, created_at DESC
        LIMIT 5
        """,
        (matched,),
    )
    return {
        "found": True,
        "id_ft": id_ft,
        "matched_uuid": matched,
        "last": {
            "id": str(last["id"]),
            "tanggal": last["tanggal"].isoformat() if last["tanggal"] else None,
            "shift": last["shift"],
            "hm_value": float(last["hm_value"]) if last["hm_value"] is not None else None,
            "created_at": last["created_at"].isoformat() if last["created_at"] else None,
        },
        "history": [
            {
                "id": str(h["id"]),
                "tanggal": h["tanggal"].isoformat() if h["tanggal"] else None,
                "shift": h["shift"],
                "hm_value": float(h["hm_value"]) if h["hm_value"] is not None else None,
            }
            for h in history
        ],
    }
