from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from psycopg import sql

from ..config import get_settings
from ..db import connection, fetch_all, fetch_one, qualified
from ..dependencies import current_user
from ..security import SessionUser
from ..services.storage import extension_for

router = APIRouter(prefix="/api/fuel", tags=["field-bridge"])
settings = get_settings()


TABLE_MAP: dict[str, str] = {
    # Current field family
    "fuel_profiles": "fuel_profiles",
    "fuel_master_jalur": "fuel_master_jalur",
    "fuel_master_tandon": "fuel_master_tandon",
    "fuel_master_fuel_truck": "fuel_master_fuel_truck",
    "fuel_fm_awal_settings": "fuel_fm_awal_settings",
    "fuel_tera_tangki_grid": "fuel_tera_tangki_grid",
    "fuel_tx_transfer_fuel": "fuel_tx_transfer_fuel",
    "fuel_tx_fuel_truck_monitoring": "fuel_tx_fuel_truck_monitoring",
    "fuel_route_config": "fuel_route_config",
    "fuel_attachment_log": "fuel_attachment_log",
    "fuel_v_route_config": "fuel_v_route_config",
    "fuel_v_transfer_fuel": "fuel_v_transfer_fuel",
    "fuel_v_fuel_truck_monitoring": "fuel_v_fuel_truck_monitoring",
    # Canonical/legacy business family
    "master_vendor": "master_vendor",
    "master_unit": "master_unit",
    "ft_mandar_ocean": "ft_mandar_ocean",
    "master_jalur": "master_jalur",
    "master_main_tank": "master_main_tank",
    "master_fuel_truck": "master_fuel_truck",
    "penerimaan_mo": "penerimaan_mo",
    "pengurasan": "pengurasan",
    "sounding_main_tank": "sounding_main_tank",
    "cleanliness": "cleanliness",
    "sounding_table": "sounding_table",
    "voucher_bib": "voucher_bib",
    "shift_route_config": "shift_route_config",
    "app_user": "app_user",
    "refuelling": "refuelling",
    "transfer_fuel": "transfer_fuel",        # legacy transfer data
    "closing_stock": "closing_stock",        # closing stock header
    "closing_stock_line": "closing_stock_line",  # closing stock lines
    "fuel_discrepancy_manual": "fuel_discrepancy_manual",  # discrepancy manual entry
}

READ_ONLY = {"fuel_v_route_config", "fuel_v_transfer_fuel", "fuel_v_fuel_truck_monitoring", "sounding_table", "voucher_bib", "refuelling"}
WRITE_ROLES: dict[str, set[str]] = {
    "fuel_tx_transfer_fuel": {"SUPER_ADMIN", "ADMIN", "FUELMAN", "FIELD", "SUPERVISOR"},
    "fuel_tx_fuel_truck_monitoring": {"SUPER_ADMIN", "ADMIN", "FUELMAN", "DRIVER", "FIELD", "SUPERVISOR"},
    "fuel_route_config": {"SUPER_ADMIN", "ADMIN", "PENERIMAAN"},
    "penerimaan_mo": {"SUPER_ADMIN", "ADMIN", "PENERIMAAN", "FIELD", "SUPERVISOR"},
    "pengurasan": {"SUPER_ADMIN", "ADMIN", "PENERIMAAN", "FIELD", "SUPERVISOR"},
    "sounding_main_tank": {"SUPER_ADMIN", "ADMIN", "PENERIMAAN", "FIELD", "SUPERVISOR"},
    "cleanliness": {"SUPER_ADMIN", "ADMIN", "PENERIMAAN", "FUELMAN", "DRIVER", "FIELD", "SUPERVISOR"},
    "fuel_profiles": {"SUPER_ADMIN", "ADMIN"},
    "fuel_master_jalur": {"SUPER_ADMIN", "ADMIN"},
    "fuel_master_tandon": {"SUPER_ADMIN", "ADMIN"},
    "fuel_master_fuel_truck": {"SUPER_ADMIN", "ADMIN"},
    "fuel_fm_awal_settings": {"SUPER_ADMIN", "ADMIN"},
    "fuel_tera_tangki_grid": {"SUPER_ADMIN", "ADMIN"},
    "transfer_fuel": {"SUPER_ADMIN", "ADMIN", "FUELMAN", "FIELD", "SUPERVISOR"},
    "closing_stock": {"SUPER_ADMIN", "ADMIN", "GROUP_LEADER"},
    "closing_stock_line": {"SUPER_ADMIN", "ADMIN", "GROUP_LEADER"},
    "fuel_discrepancy_manual": {"SUPER_ADMIN", "ADMIN", "GROUP_LEADER"},
    "ft_mandar_ocean": {"SUPER_ADMIN", "ADMIN", "PENERIMAAN"},
}


def _resolve(name: str) -> str:
    if name not in TABLE_MAP:
        raise HTTPException(status_code=404, detail="Table tidak diizinkan")
    return TABLE_MAP[name]


@lru_cache(maxsize=128)
def _columns(table: str) -> dict[str, dict[str, Any]]:
    rows = fetch_all(
        """
        SELECT column_name,data_type,is_nullable,column_default,is_generated,is_identity
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s
        ORDER BY ordinal_position
        """,
        (settings.database_schema, table),
    )
    return {r["column_name"]: r for r in rows}


@lru_cache(maxsize=128)
def _pk_columns(table: str) -> tuple[str, ...]:
    rows = fetch_all(
        """
        SELECT a.attname AS column_name
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
        WHERE i.indrelid=%s::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey,a.attnum)
        """,
        (f"{settings.database_schema}.{table}",),
    )
    return tuple(r["column_name"] for r in rows)


def _assert_write(table_alias: str, user: SessionUser) -> None:
    if table_alias in READ_ONLY:
        raise HTTPException(status_code=405, detail="Resource read-only")
    allowed = WRITE_ROLES.get(table_alias, {"SUPER_ADMIN", "ADMIN"})
    if user.role not in allowed:
        raise HTTPException(status_code=403, detail="Role tidak mempunyai izin tulis ke tabel ini")


def _select_columns(table: str, requested: str | None) -> list[str]:
    available = _columns(table)
    if not available:
        raise HTTPException(status_code=404, detail=f"Tabel/view {table} tidak ada di schema fcc")
    if not requested or requested == "*":
        return list(available)
    cols = [c.strip() for c in requested.split(",") if c.strip()]
    bad = [c for c in cols if c not in available]
    if bad:
        raise HTTPException(status_code=422, detail=f"Kolom tidak dikenal: {bad}")
    return cols


def _redact_sensitive(table: str, rows: list[dict] | dict | None) -> list[dict] | dict | None:
    """Remove sensitive columns (e.g. password hashes) from responses.
    Always returns a list. Callers that need a single row should use [0]."""
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    SENSITIVE = {"app_user": ["password_hash"], "fuel_profiles": []}
    if table not in SENSITIVE:
        return rows if isinstance(rows, list) else [rows]
    for row in rows:
        if not isinstance(row, dict):
            continue
        for col in SENSITIVE[table]:
            if col in row and row[col]:
                row[col] = "***REDACTED***"
    return rows if isinstance(rows, list) else [rows]


def _filters(request: Request, table: str) -> tuple[list[sql.Composed], list[Any]]:
    available = _columns(table)
    clauses: list[sql.Composed] = []
    params: list[Any] = []
    ops = {
        "eq": sql.SQL("{}=%s"),
        "neq": sql.SQL("{}<>%s"),
        "gt": sql.SQL("{}>%s"),
        "gte": sql.SQL("{}>=%s"),
        "lt": sql.SQL("{}<%s"),
        "lte": sql.SQL("{}<=%s"),
        "like": sql.SQL("{} LIKE %s"),
        "ilike": sql.SQL("{} ILIKE %s"),
    }
    for key, value in request.query_params.multi_items():
        if "." not in key:
            continue
        op, col = key.split(".", 1)
        if col not in available:
            raise HTTPException(status_code=422, detail=f"Filter column tidak dikenal: {col}")
        ident = sql.Identifier(col)
        if op in ops:
            clauses.append(ops[op].format(ident))
            params.append(value)
        elif op == "in":
            vals = [x for x in value.split(",") if x != ""]
            clauses.append(sql.SQL("{}=ANY(%s)").format(ident))
            params.append(vals)
        elif op == "is":
            if value.lower() == "null":
                clauses.append(sql.SQL("{} IS NULL").format(ident))
            elif value.lower() in {"true", "false"}:
                clauses.append(sql.SQL("{} IS %s").format(ident))
                params.append(value.lower() == "true")
    return clauses, params


def _clean_payload(table: str, payload: dict[str, Any]) -> dict[str, Any]:
    cols = _columns(table)
    output = {}
    for key, value in payload.items():
        info = cols.get(key)
        if not info:
            continue
        if info.get("is_generated") == "ALWAYS" or str(info.get("is_identity") or "").upper() == "YES":
            continue
        output[key] = value
    # Auto-fill volume_tera_unit_awal/akhir dari sounding_table kalau tera_unit_*
    # di-set tapi volume_tera_unit_* belum diisi. Ini agar view fcc.fuel_v_transfer_fuel
    # punya data volume_tera_unit_* real-time.
    if table == "fuel_tx_transfer_fuel":
        unit_uuid = output.get("fuel_truck_id")
        if unit_uuid:
            for tera_col, vol_col in [("tera_unit_awal", "volume_tera_unit_awal"),
                                       ("tera_unit_akhir", "volume_tera_unit_akhir")]:
                tera = output.get(tera_col)
                if tera is not None and output.get(vol_col) is None:
                    # Lookup fuel_truck_id UUID → unit_code, then sounding_table
                    unit_row = fetch_one(
                        "SELECT unit_code FROM fcc.fuel_master_fuel_truck WHERE id::text = %s LIMIT 1",
                        (str(unit_uuid),),
                    )
                    if unit_row:
                        vol_row = fetch_one(
                            "SELECT volume_l FROM fcc.sounding_table WHERE aset = %s AND dip_cm = %s LIMIT 1",
                            (unit_row["unit_code"], float(tera)),
                        )
                        if vol_row:
                            output[vol_col] = vol_row["volume_l"]

    # Auto-fill tera_master_*, selisih_t_*, tera_status untuk penerimaan_mo.
    # Bandingkan tera aktual (input) vs tera master (ft_mandar_ocean).
    if table == "penerimaan_mo":
        id_ft = output.get("id_ft")
        if id_ft:
            # Lookup tera master dari ft_mandar_ocean
            master = fetch_one(
                "SELECT t2_depan_cm, t2_belakang_cm FROM fcc.ft_mandar_ocean WHERE id_ft = %s LIMIT 1",
                (id_ft,),
            )
            if master:
                tera_master_depan = master.get("t2_depan_cm")
                tera_master_belakang = master.get("t2_belakang_cm")
                tera_aktual_depan = output.get("tera_depan_cm")
                tera_aktual_belakang = output.get("tera_belakang_cm")

                # Set tera_master_* (untuk audit trail)
                if "tera_master_depan_cm" in (info.get("column_name") for info in _columns(table).values()):
                    if output.get("tera_master_depan_cm") is None:
                        output["tera_master_depan_cm"] = tera_master_depan
                if "tera_master_belakang_cm" in (info.get("column_name") for info in _columns(table).values()):
                    if output.get("tera_master_belakang_cm") is None:
                        output["tera_master_belakang_cm"] = tera_master_belakang

                # Compute selisih (aktual - master). Positif = over.
                if tera_master_depan is not None and tera_aktual_depan is not None:
                    selisih = float(tera_aktual_depan) - float(tera_master_depan)
                    if "selisih_t_depan_cm" in (info.get("column_name") for info in _columns(table).values()):
                        output["selisih_t_depan_cm"] = round(selisih, 2)
                    if "selisih_t_depan_pct" in (info.get("column_name") for info in _columns(table).values()) and float(tera_master_depan) != 0:
                        output["selisih_t_depan_pct"] = round((selisih / float(tera_master_depan)) * 100, 2)
                if tera_master_belakang is not None and tera_aktual_belakang is not None:
                    selisih = float(tera_aktual_belakang) - float(tera_master_belakang)
                    if "selisih_t_belakang_cm" in (info.get("column_name") for info in _columns(table).values()):
                        output["selisih_t_belakang_cm"] = round(selisih, 2)
                    if "selisih_t_belakang_pct" in (info.get("column_name") for info in _columns(table).values()) and float(tera_master_belakang) != 0:
                        output["selisih_t_belakang_pct"] = round((selisih / float(tera_master_belakang)) * 100, 2)

                # tera_status berdasarkan |selisih_t_depan_cm|
                if "tera_status" in (info.get("column_name") for info in _columns(table).values()):
                    if tera_master_depan is None or tera_aktual_depan is None:
                        output["tera_status"] = "NO_MASTER"
                    else:
                        diff = abs(float(tera_aktual_depan) - float(tera_master_depan))
                        if diff <= 1.0:
                            output["tera_status"] = "OK"
                        elif diff <= 3.0:
                            output["tera_status"] = "WARNING"
                        else:
                            output["tera_status"] = "CRITICAL"

    # Auto-fill sounding master (intank_cm_master, aktual_cm_master) untuk sounding_main_tank.
    # Master = row sounding terakhir untuk (main_tank, shift) sebelumnya. Snapshot untuk audit.
    if table == "sounding_main_tank":
        tank = output.get("main_tank")
        tanggal = output.get("tanggal")
        shift = output.get("shift")
        if tank and tanggal and shift:
            # Cari row sounding sebelumnya (tanggal < hari ini, atau shift sebelumnya)
            prev = fetch_one(
                """
                SELECT intank_cm, aktual_cm FROM fcc.sounding_main_tank
                WHERE main_tank = %s
                  AND tanggal <= %s
                  AND NOT (tanggal = %s AND shift = %s)
                ORDER BY tanggal DESC, shift DESC LIMIT 1
                """,
                (tank, tanggal, tanggal, shift),
            )
            if prev:
                if "intank_cm_master" in (info.get("column_name") for info in _columns(table).values()):
                    if output.get("intank_cm_master") is None:
                        output["intank_cm_master"] = prev.get("intank_cm")
                if "aktual_cm_master" in (info.get("column_name") for info in _columns(table).values()):
                    if output.get("aktual_cm_master") is None:
                        output["aktual_cm_master"] = prev.get("aktual_cm")

            # Hitung selisih_cm_* (aktual - master)
            intank_aktual = output.get("intank_cm")
            aktual_aktual = output.get("aktual_cm")
            intank_master = output.get("intank_cm_master")
            aktual_master = output.get("aktual_cm_master")

            if intank_master is not None and intank_aktual is not None:
                if "selisih_cm_intank" in (info.get("column_name") for info in _columns(table).values()):
                    output["selisih_cm_intank"] = round(float(intank_aktual) - float(intank_master), 2)
            if aktual_master is not None and aktual_aktual is not None:
                if "selisih_cm_aktual" in (info.get("column_name") for info in _columns(table).values()):
                    output["selisih_cm_aktual"] = round(float(aktual_aktual) - float(aktual_master), 2)

            # sounding_status berdasarkan max(|selisih_cm_intank|, |selisih_cm_aktual|)
            if "sounding_status" in (info.get("column_name") for info in _columns(table).values()):
                intank_master = output.get("intank_cm_master")
                aktual_master = output.get("aktual_cm_master")
                if (intank_master is None and aktual_master is None) or (intank_aktual is None and aktual_aktual is None):
                    output["sounding_status"] = "NO_MASTER"
                else:
                    diffs = []
                    if intank_master is not None and intank_aktual is not None:
                        diffs.append(abs(float(intank_aktual) - float(intank_master)))
                    if aktual_master is not None and aktual_aktual is not None:
                        diffs.append(abs(float(aktual_aktual) - float(aktual_master)))
                    if not diffs:
                        output["sounding_status"] = "NO_MASTER"
                    else:
                        maxDiff = max(diffs)
                        if maxDiff <= 1.0:
                            output["sounding_status"] = "OK"
                        elif maxDiff <= 3.0:
                            output["sounding_status"] = "WARNING"
                        else:
                            output["sounding_status"] = "CRITICAL"
    return output


@router.get("/{table_alias}")
def list_rows(table_alias: str, request: Request, _: SessionUser = Depends(current_user)) -> list[dict]:
    table = _resolve(table_alias)
    columns = _select_columns(table, request.query_params.get("select"))
    clauses, params = _filters(request, table)
    where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("")
    order_parts = []
    for part in (request.query_params.get("order") or "").split(","):
        if not part:
            continue
        bits = part.split(".")
        col = bits[0]
        if col not in _columns(table):
            continue
        direction = sql.SQL("DESC") if len(bits) > 1 and bits[1].lower() == "desc" else sql.SQL("ASC")
        order_parts.append(sql.SQL("{} {}").format(sql.Identifier(col), direction))
    order = sql.SQL(" ORDER BY ") + sql.SQL(",").join(order_parts) if order_parts else sql.SQL("")
    limit = min(5000, max(1, int(request.query_params.get("limit", "500"))))
    offset = max(0, int(request.query_params.get("offset", "0")))
    query = sql.SQL("SELECT {} FROM {}{}{} LIMIT %s OFFSET %s").format(
        sql.SQL(",").join(map(sql.Identifier, columns)), qualified(table), where, order
    )
    try:
        rows = fetch_all(query, [*params, limit, offset])
    except Exception as exc:
        # If filter value has invalid type for column (e.g. UUID cast failure),
        # return empty list instead of 500. This matches PostgREST behavior.
        msg = str(exc).lower()
        if "invalid input syntax" in msg or "invalidtextrepresentation" in msg or "uuid" in msg:
            return []
        raise
    return _redact_sensitive(table, rows)


@router.post("/{table_alias}")
def insert_rows(
    table_alias: str,
    payload: dict[str, Any] | list[dict[str, Any]],
    user: SessionUser = Depends(current_user),
) -> Any:
    table = _resolve(table_alias)
    _assert_write(table_alias, user)
    records = payload if isinstance(payload, list) else [payload]
    saved = []
    with connection() as conn:
        with conn.cursor() as cur:
            for raw in records:
                data = _clean_payload(table, raw)
                if not data:
                    raise HTTPException(status_code=422, detail="Payload tidak memiliki kolom valid")
                # Auto-fill *_by (created_by, updated_by, voided_by, validated_by) from user.id.
                # These columns are UUID in DB but user.id is BIGINT. We need UUID.
                # Lookup fuel_profiles.id (UUID) by app_user_id (BIGINT) — profile.id is UUID.
                by_uuid_cols = ['created_by', 'updated_by', 'voided_by', 'validated_by']
                cols_info = _columns(table)
                user_uuid = None
                if any(c in cols_info for c in by_uuid_cols):
                    r = fetch_one(
                        "SELECT id::text AS uid FROM fcc.fuel_profiles "
                        "WHERE app_user_id = %s LIMIT 1",
                        (user.id,),
                    )
                    user_uuid = r.get('uid') if r else None
                if user_uuid:
                    for c in by_uuid_cols:
                        if c in cols_info and c not in data:
                            data[c] = user_uuid
                columns = list(data)
                query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
                    qualified(table),
                    sql.SQL(",").join(map(sql.Identifier, columns)),
                    sql.SQL(",").join([sql.Placeholder()] * len(columns)),
                )
                try:
                    cur.execute(query, [data[c] for c in columns])
                    saved.append(cur.fetchone())
                    import logging
                    logging.getLogger("fcc.fuel_bridge").info(f"insert_rows: saved.append row id={saved[-1]}")
                except Exception as exc:
                    msg = str(exc).lower()
                    if "invalid input syntax" in msg or "invalidtextrepresentation" in msg or "uuid" in msg:
                        # Invalid type cast — return empty result
                        import logging
                        logging.getLogger("fcc.fuel_bridge").warning(f"insert_rows: UUID cast failed for {data}: {exc}")
                        saved.append(None)
                    else:
                        import logging
                        logging.getLogger("fcc.fuel_bridge").error(f"insert_rows: {exc}")
                        raise HTTPException(status_code=409, detail=str(exc)) from exc
    saved = [s for s in saved if s is not None]
    if not saved:
        # All rows had invalid UUID cast — return empty list for non-list payload,
        # or original empty list for list payload
        return [] if isinstance(payload, list) else {"error": "no rows saved", "data": []}
    return _redact_sensitive(table, saved if isinstance(payload, list) else saved[0])


@router.patch("/{table_alias}")
def update_rows(
    table_alias: str,
    request: Request,
    payload: dict[str, Any],
    user: SessionUser = Depends(current_user),
) -> list[dict]:
    table = _resolve(table_alias)
    _assert_write(table_alias, user)
    clauses, params = _filters(request, table)
    if not clauses:
        raise HTTPException(status_code=422, detail="PATCH wajib mempunyai filter eq.*")
    data = _clean_payload(table, payload)
    if not data:
        raise HTTPException(status_code=422, detail="Tidak ada kolom valid")
    setters = sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(c)) for c in data)
    query = sql.SQL("UPDATE {} SET {} WHERE {} RETURNING *").format(
        qualified(table), setters, sql.SQL(" AND ").join(clauses)
    )
    with connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(query, [*data.values(), *params])
                rows = list(cur.fetchall())
            except Exception as exc:
                msg = str(exc).lower()
                if "invalid input syntax" in msg or "invalidtextrepresentation" in msg or "uuid" in msg:
                    return []
                raise
            _redact_sensitive(table, rows)
            return rows


@router.post("/{table_alias}/upsert")
def upsert_rows(
    table_alias: str,
    request: Request,
    payload: dict[str, Any] | list[dict[str, Any]],
    user: SessionUser = Depends(current_user),
) -> list[dict]:
    table = _resolve(table_alias)
    _assert_write(table_alias, user)
    conflict = request.query_params.get("onConflict")
    if not conflict or conflict not in _columns(table):
        raise HTTPException(status_code=422, detail="onConflict wajib dan harus kolom valid")
    records = payload if isinstance(payload, list) else [payload]
    saved = []
    with connection() as conn:
        with conn.cursor() as cur:
            for raw in records:
                data = _clean_payload(table, raw)
                columns = list(data)
                update_cols = [c for c in columns if c != conflict]
                query = sql.SQL(
                    "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {} RETURNING *"
                ).format(
                    qualified(table),
                    sql.SQL(",").join(map(sql.Identifier, columns)),
                    sql.SQL(",").join([sql.Placeholder()] * len(columns)),
                    sql.Identifier(conflict),
                    sql.SQL(",").join(
                        sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c)) for c in update_cols
                    ),
                )
                cur.execute(query, [data[c] for c in columns])
                saved.append(cur.fetchone())
    return _redact_sensitive(table, saved)


@router.delete("/{table_alias}")
def delete_rows(
    table_alias: str,
    request: Request,
    user: SessionUser = Depends(current_user),
) -> dict:
    table = _resolve(table_alias)
    _assert_write(table_alias, user)
    if user.role != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Delete hanya SUPER_ADMIN")
    clauses, params = _filters(request, table)
    if not clauses:
        raise HTTPException(status_code=422, detail="DELETE wajib mempunyai filter")
    with connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    sql.SQL("DELETE FROM {} WHERE {}").format(qualified(table), sql.SQL(" AND ").join(clauses)),
                    params,
                )
                return {"ok": True, "deleted": cur.rowcount}
            except Exception as exc:
                msg = str(exc).lower()
                if "invalid input syntax" in msg or "invalidtextrepresentation" in msg or "uuid" in msg:
                    return {"ok": True, "deleted": 0}
                raise


@router.post("/rpc/{name}")
def rpc(name: str, payload: dict[str, Any], _: SessionUser = Depends(current_user)) -> Any:
    if name == "fuel_public_staged_nrp_lookup":
        nrp = str(payload.get("p_nrp") or "").strip()
        row = fetch_one(
            sql.SQL(
                "SELECT username AS nrp,nama AS full_name,role AS default_role,status FROM {} "
                "WHERE lower(username)=lower(%s) LIMIT 1"
            ).format(qualified("app_user")),
            (nrp,),
        )
        return [row] if row else []
    if name == "fuel_get_tera_volume":
        unit_code = str(payload.get("p_unit_code") or payload.get("p_aset") or "")
        dip = float(payload.get("p_dip_cm") or payload.get("p_dip") or 0)
        return [{"volume_l": _tera_volume(unit_code, dip)}]
    if name == "fuel_get_default_fm_awal":
        jalur_id = payload.get("p_jalur_id")
        # 1) Check fuel_fm_awal_settings (mode + manual override)
        setting = fetch_one(
            sql.SQL(
                "SELECT mode, fm_awal_manual FROM {} WHERE site_code='PPA-BIB' AND jalur_id=%s LIMIT 1"
            ).format(qualified("fuel_fm_awal_settings")),
            (jalur_id,),
        )
        if setting and setting.get("mode") == "MANUAL" and setting.get("fm_awal_manual") is not None:
            return [{"fm_value": float(setting["fm_awal_manual"]), "source": "MANUAL", "last_transfer_id": None}]
        # 2) AUTO: try fuel_route_config fm_aktual_awal
        row = fetch_one(
            sql.SQL(
                "SELECT r.fm_aktual_awal, r.id::text AS rid FROM {} r WHERE r.jalur_id=%s AND r.fm_aktual_awal IS NOT NULL ORDER BY r.tanggal DESC, r.updated_at DESC LIMIT 1"
            ).format(qualified("fuel_route_config")),
            (jalur_id,),
        )
        if row and row.get("fm_aktual_awal") is not None:
            return [{"fm_value": float(row["fm_aktual_awal"]), "source": "AUTO_ROUTE_CONFIG", "last_transfer_id": row.get("rid")}]
        # 3) Fallback to transfer_fuel legacy via jalur_code (with JLR-N aliases)
        import re as _re
        jalur = fetch_one(sql.SQL("SELECT jalur_code FROM {} WHERE id=%s").format(qualified("fuel_master_jalur")), (jalur_id,))
        if jalur:
            code = jalur["jalur_code"]
            aliases = [code]
            mm = _re.match(r'^JALUR (\d+)$', code or '')
            if mm: aliases.append('JLR-' + mm.group(1))
            legacy = fetch_one(
                sql.SQL("SELECT fm_akhir FROM {} WHERE jalur = ANY(%s) ORDER BY tanggal DESC, created_at DESC LIMIT 1").format(qualified("transfer_fuel")),
                (aliases,),
            )
            if legacy:
                return [{"fm_value": float(legacy["fm_akhir"]), "source": "AUTO_LEGACY_TRANSFER", "last_transfer_id": None}]
        return [{"fm_value": 0, "source": "AUTO_DEFAULT", "last_transfer_id": None}]
    raise HTTPException(status_code=404, detail="RPC tidak dikenal")


def _tera_volume(unit_code: str, dip: float) -> float | None:
    # Current JSON grid first.
    try:
        row = fetch_one(
            sql.SQL(
                "SELECT dip_min,dip_step,volumes_json FROM {} WHERE unit_code=%s ORDER BY updated_at DESC LIMIT 1"
            ).format(qualified("fuel_tera_tangki_grid")),
            (unit_code,),
        )
        if row and row.get("volumes_json"):
            values = row["volumes_json"]
            if isinstance(values, str):
                values = json.loads(values)
            minimum = float(row.get("dip_min") or 0)
            step = float(row.get("dip_step") or 1)
            position = (dip - minimum) / step
            if position < 0 or position > len(values) - 1:
                return None
            lower = int(position)
            upper = min(lower + 1, len(values) - 1)
            frac = position - lower
            return float(values[lower]) + frac * (float(values[upper]) - float(values[lower]))
    except Exception:
        pass
    # Legacy point table interpolation.
    lower = fetch_one(
        sql.SQL(
            "SELECT dip_cm,volume_l FROM {} WHERE aset=%s AND status='ACTIVE' AND dip_cm<=%s ORDER BY dip_cm DESC LIMIT 1"
        ).format(qualified("sounding_table")),
        (unit_code, dip),
    )
    upper = fetch_one(
        sql.SQL(
            "SELECT dip_cm,volume_l FROM {} WHERE aset=%s AND status='ACTIVE' AND dip_cm>=%s ORDER BY dip_cm ASC LIMIT 1"
        ).format(qualified("sounding_table")),
        (unit_code, dip),
    )
    if not lower or not upper:
        return None
    if float(upper["dip_cm"]) == float(lower["dip_cm"]):
        return float(lower["volume_l"])
    ratio = (dip - float(lower["dip_cm"])) / (float(upper["dip_cm"]) - float(lower["dip_cm"]))
    return float(lower["volume_l"]) + ratio * (float(upper["volume_l"]) - float(lower["volume_l"]))


@router.post("/storage/{bucket}/{path:path}")
async def storage_upload(
    bucket: str,
    path: str,
    request: Request,
    user: SessionUser = Depends(current_user),
) -> dict:
    raw = await request.body()
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File terlalu besar")
    safe_bucket = re.sub(r"[^A-Za-z0-9_.-]+", "_", bucket)
    safe_parts = [re.sub(r"[^A-Za-z0-9_.-]+", "_", p) for p in Path(path).parts if p not in {".", ".."}]
    relative = Path("field-storage") / safe_bucket / Path(*safe_parts)
    target = (settings.evidence_dir_resolved / relative).resolve()
    root = settings.evidence_dir_resolved
    if root not in target.parents:
        raise HTTPException(status_code=422, detail="Path tidak valid")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return {"ok": True, "path": relative.as_posix(), "size": len(raw), "uploaded_by": user.username}
