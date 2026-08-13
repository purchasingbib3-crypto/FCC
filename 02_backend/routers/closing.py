from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from psycopg import sql

from ..config import get_settings
from ..db import connection, fetch_all, fetch_one, qualified
from ..dependencies import current_user, require_roles
from ..security import SessionUser
from ..services.shift import normalize_shift, previous_shift

router = APIRouter(prefix="/api/v1/closing", tags=["closing"])
settings = get_settings()


class ActualPatch(BaseModel):
    asset: str = Field(min_length=1, max_length=120)
    sounding_aktual_cm: float | None = None
    aktual_l: float = Field(ge=0)
    note: str | None = Field(default=None, max_length=2000)


class FinalizeRequest(BaseModel):
    pica_note: str | None = Field(default=None, max_length=4000)


def _get_closing(operation_date: date, shift: str) -> dict | None:
    header = fetch_one(
        sql.SQL("SELECT * FROM {} WHERE tanggal=%s AND shift=%s").format(qualified("closing_stock")),
        (operation_date, shift),
    )
    if not header:
        return None
    lines = fetch_all(
        sql.SQL("SELECT * FROM {} WHERE closing_id=%s ORDER BY jenis,aset").format(qualified("closing_stock_line")),
        (header["id"],),
    )
    return {"header": header, "lines": lines}


def _active_assets() -> list[tuple[str, str]]:
    """Return a de-duplicated controlled union of active asset codes.

    Main Tank input/receiving currently uses the canonical legacy family, while
    current transfer uses the fuel_* UUID family. We only union the *codes* for
    Closing lines; no transaction is double-written and movement queries still
    apply current-first/legacy-fallback rules.
    """
    assets: dict[tuple[str, str], None] = {}

    for table, code_column, kind in (
        ("master_main_tank", "kode", "MAINTANK"),
        ("fuel_master_tandon", "tandon_code", "MAINTANK"),
        ("master_fuel_truck", "kode", "FUEL_TRUCK"),
        ("fuel_master_fuel_truck", "unit_code", "FUEL_TRUCK"),
    ):
        try:
            rows = fetch_all(
                sql.SQL("SELECT {} AS kode FROM {} WHERE status::text='ACTIVE' ORDER BY 1").format(
                    sql.Identifier(code_column), qualified(table)
                )
            )
        except Exception:
            continue
        for row in rows:
            code = str(row.get("kode") or "").strip()
            if code:
                assets[(code, kind)] = None

    return sorted(assets, key=lambda item: (item[1], item[0]))


def _previous_actual_map(operation_date: date, shift: str) -> dict[str, float]:
    prev_date, prev_shift = previous_shift(operation_date, shift)
    rows = fetch_all(
        sql.SQL(
            "SELECT l.aset,l.aktual_l FROM {} h JOIN {} l ON l.closing_id=h.id "
            "WHERE h.tanggal=%s AND h.shift=%s AND h.status='CLOSED'"
        ).format(qualified("closing_stock"), qualified("closing_stock_line")),
        (prev_date, prev_shift),
    )
    return {r["aset"]: float(r["aktual_l"] or 0) for r in rows}


def _period_movements(operation_date: date, shift: str) -> dict[str, dict[str, float]]:
    movement: dict[str, dict[str, float]] = {}

    def bucket(asset: str) -> dict[str, float]:
        return movement.setdefault(
            asset,
            {"penerimaan_l": 0.0, "transfer_masuk_l": 0.0, "transfer_keluar_l": 0.0, "refuelling_l": 0.0},
        )

    receipts = fetch_all(
        sql.SQL(
            "SELECT main_tank AS asset,sum(COALESCE(total_fm_l,fm_akhir-fm_awal,0)) AS qty "
            "FROM {} WHERE tanggal=%s AND shift=%s AND COALESCE(status,'VALID') NOT IN ('VOID','DRAFT') GROUP BY main_tank"
        ).format(qualified("penerimaan_mo")),
        (operation_date, shift),
    )
    for row in receipts:
        bucket(row["asset"])["penerimaan_l"] += float(row["qty"] or 0)

    # Current transfer family has priority for the selected shift. Legacy is fallback only.
    current_count = 0
    try:
        current = fetch_all(
            sql.SQL(
                "SELECT t.tandon_code AS main_tank,t.fuel_truck_code AS fuel_truck,"
                "sum(COALESCE(t.fm_akhir-t.fm_awal,0)) AS qty "
                "FROM {} t WHERE t.tanggal=%s AND t.shift=%s AND t.voided_at IS NULL "
                "GROUP BY t.tandon_code,t.fuel_truck_code"
            ).format(qualified("fuel_v_transfer_fuel")),
            (operation_date, shift),
        )
        current_count = len(current)
        for row in current:
            qty = float(row["qty"] or 0)
            bucket(row["main_tank"])["transfer_keluar_l"] += qty
            bucket(row["fuel_truck"])["transfer_masuk_l"] += qty
    except Exception:
        current_count = 0

    if current_count == 0:
        legacy = fetch_all(
            sql.SQL(
                "SELECT main_tank,fuel_truck,sum(COALESCE(total_fm_l,fm_akhir-fm_awal,0)) AS qty "
                "FROM {} WHERE tanggal=%s AND shift=%s AND COALESCE(status,'VALID') NOT IN ('VOID','DRAFT') "
                "GROUP BY main_tank,fuel_truck"
            ).format(qualified("transfer_fuel")),
            (operation_date, shift),
        )
        for row in legacy:
            qty = float(row["qty"] or 0)
            bucket(row["main_tank"])["transfer_keluar_l"] += qty
            bucket(row["fuel_truck"])["transfer_masuk_l"] += qty

    refuel = fetch_all(
        sql.SQL(
            "SELECT fuel_truck AS asset,sum(volume_l) AS qty FROM {} "
            "WHERE tanggal=%s AND shift=%s AND COALESCE(status,'VALID') NOT IN ('VOID','DRAFT') GROUP BY fuel_truck"
        ).format(qualified("refuelling")),
        (operation_date, shift),
    )
    for row in refuel:
        bucket(row["asset"])["refuelling_l"] += float(row["qty"] or 0)
    return movement


def _maintank_actual_map(operation_date: date, shift: str) -> dict[str, tuple[float | None, float | None]]:
    rows = fetch_all(
        sql.SQL(
            "SELECT DISTINCT ON (main_tank) main_tank,aktual_cm,aktual_l FROM {} "
            "WHERE tanggal=%s AND shift=%s AND COALESCE(status,'VALID') NOT IN ('VOID','DRAFT') "
            "ORDER BY main_tank,updated_at DESC,id DESC"
        ).format(qualified("sounding_main_tank")),
        (operation_date, shift),
    )
    return {r["main_tank"]: (r.get("aktual_cm"), r.get("aktual_l")) for r in rows}


@router.get("")
def get_closing(
    operation_date: date,
    shift: str,
    _: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN", "GROUP_LEADER")),
) -> dict:
    shift = normalize_shift(shift)
    data = _get_closing(operation_date, shift)
    if not data:
        raise HTTPException(status_code=404, detail="Closing belum dibuat")
    lines = data["lines"]
    return {
        "ok": True,
        **data,
        "summary": {
            "stock_awal_l": sum(float(x.get("stock_awal_l") or 0) for x in lines),
            "penerimaan_l": sum(float(x.get("penerimaan_l") or 0) for x in lines),
            "transfer_masuk_l": sum(float(x.get("transfer_masuk_l") or 0) for x in lines),
            "transfer_keluar_l": sum(float(x.get("transfer_keluar_l") or 0) for x in lines),
            "refuelling_l": sum(float(x.get("refuelling_l") or 0) for x in lines),
            "administrasi_l": sum(float(x.get("total_administrasi_l") or 0) for x in lines),
            "aktual_l": sum(float(x.get("aktual_l") or 0) for x in lines if x.get("aktual_l") is not None),
            "missing_actual": sum(1 for x in lines if x.get("aktual_l") is None),
        },
    }


@router.post("/ensure")
def ensure_closing(
    operation_date: date,
    shift: str,
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
) -> dict:
    shift = normalize_shift(shift)
    existing = _get_closing(operation_date, shift)
    if existing:
        return get_closing(operation_date, shift, user)

    opening = _previous_actual_map(operation_date, shift)
    movements = _period_movements(operation_date, shift)
    maintank_actual = _maintank_actual_map(operation_date, shift)
    assets = _active_assets()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {} (tanggal,shift,status,penanggung_jawab,created_at,updated_at) "
                    "VALUES (%s,%s,'DRAFT',%s,now(),now()) RETURNING id"
                ).format(qualified("closing_stock")),
                (operation_date, shift, user.nama),
            )
            closing_id = int(cur.fetchone()["id"])
            for asset, kind in assets:
                mv = movements.get(asset, {})
                actual_cm, actual_l = maintank_actual.get(asset, (None, None)) if kind == "MAINTANK" else (None, None)
                stock_awal = opening.get(asset, 0.0)
                penerimaan = mv.get("penerimaan_l", 0.0)
                transfer_in = mv.get("transfer_masuk_l", 0.0)
                transfer_out = mv.get("transfer_keluar_l", 0.0)
                refuelling = mv.get("refuelling_l", 0.0)
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {} (closing_id,aset,jenis,stock_awal_l,penerimaan_l,transfer_masuk_l,"
                        "transfer_keluar_l,refuelling_l,sounding_aktual_cm,aktual_l) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    ).format(qualified("closing_stock_line")),
                    (
                        closing_id,
                        asset,
                        kind,
                        stock_awal,
                        penerimaan,
                        transfer_in,
                        transfer_out,
                        refuelling,
                        actual_cm,
                        actual_l,
                    ),
                )
    return get_closing(operation_date, shift, user)


@router.patch("/{operation_date}/{shift}/actual")
def update_actual(
    operation_date: date,
    shift: str,
    payload: ActualPatch,
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
) -> dict:
    shift = normalize_shift(shift)
    closing = _get_closing(operation_date, shift)
    if not closing:
        ensure_closing(operation_date, shift, user)
        closing = _get_closing(operation_date, shift)
    if closing["header"]["status"] == "CLOSED":
        raise HTTPException(status_code=409, detail="Closing sudah CLOSED. Reopen harus melalui prosedur audit.")
    line = next((x for x in closing["lines"] if x["aset"] == payload.asset), None)
    if not line:
        raise HTTPException(status_code=404, detail="Aset tidak ditemukan pada closing")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "UPDATE {} SET sounding_aktual_cm=%s,aktual_l=%s WHERE id=%s"
                ).format(qualified("closing_stock_line")),
                (payload.sounding_aktual_cm, payload.aktual_l, line["id"]),
            )
            cur.execute(
                sql.SQL("UPDATE {} SET penanggung_jawab=%s,updated_at=now() WHERE id=%s").format(
                    qualified("closing_stock")
                ),
                (user.nama, closing["header"]["id"]),
            )
    return get_closing(operation_date, shift, user)


@router.post("/{operation_date}/{shift}/refresh-movement")
def refresh_movement(
    operation_date: date,
    shift: str,
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
) -> dict:
    shift = normalize_shift(shift)
    closing = _get_closing(operation_date, shift)
    if not closing:
        return ensure_closing(operation_date, shift, user)
    if closing["header"]["status"] == "CLOSED":
        raise HTTPException(status_code=409, detail="Closing CLOSED tidak boleh direfresh otomatis")
    opening = _previous_actual_map(operation_date, shift)
    movements = _period_movements(operation_date, shift)
    with connection() as conn:
        with conn.cursor() as cur:
            for line in closing["lines"]:
                asset = line["aset"]
                mv = movements.get(asset, {})
                stock_awal = opening.get(asset, float(line.get("stock_awal_l") or 0))
                penerimaan = mv.get("penerimaan_l", 0.0)
                transfer_in = mv.get("transfer_masuk_l", 0.0)
                transfer_out = mv.get("transfer_keluar_l", 0.0)
                refuelling = mv.get("refuelling_l", 0.0)
                cur.execute(
                    sql.SQL(
                        "UPDATE {} SET stock_awal_l=%s,penerimaan_l=%s,transfer_masuk_l=%s,transfer_keluar_l=%s,"
                        "refuelling_l=%s WHERE id=%s"
                    ).format(qualified("closing_stock_line")),
                    (stock_awal, penerimaan, transfer_in, transfer_out, refuelling, line["id"]),
                )
    return get_closing(operation_date, shift, user)


@router.post("/{operation_date}/{shift}/finalize")
def finalize(
    operation_date: date,
    shift: str,
    payload: FinalizeRequest,
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
) -> dict:
    shift = normalize_shift(shift)
    closing = _get_closing(operation_date, shift)
    if not closing:
        raise HTTPException(status_code=404, detail="Closing belum dibuat")
    missing = [x["aset"] for x in closing["lines"] if x.get("aktual_l") is None]
    if missing:
        raise HTTPException(status_code=422, detail={"message": "Aktual belum lengkap", "assets": missing})
    critical = [x for x in closing["lines"] if abs(float(x.get("deviasi_pct") or 0)) > 5]
    if critical and not (payload.pica_note or "").strip():
        raise HTTPException(status_code=422, detail="PICA wajib diisi karena terdapat deviasi > 5%")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "UPDATE {} SET status='CLOSED',penanggung_jawab=%s,closed_at=now(),updated_at=now() WHERE id=%s"
                ).format(qualified("closing_stock")),
                (user.nama, closing["header"]["id"]),
            )
            if payload.pica_note:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {} (site_code,tanggal,shift,remark,pica_status,pica_note,input_by,updated_by) "
                        "VALUES (%s,%s,%s,%s,'OPEN',%s,%s,%s) "
                        "ON CONFLICT (site_code,tanggal,shift) DO UPDATE SET remark=EXCLUDED.remark,pica_status='OPEN',"
                        "pica_note=EXCLUDED.pica_note,updated_by=EXCLUDED.updated_by,updated_at=now()"
                    ).format(qualified("fuel_discrepancy_manual")),
                    (settings.site_code, operation_date, shift, "Closing finalized with PICA", payload.pica_note, user.username, user.username),
                )
    return get_closing(operation_date, shift, user)
