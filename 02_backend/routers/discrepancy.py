from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg import sql

from ..config import get_settings
from ..db import connection, fetch_all, qualified
from ..dependencies import current_user, require_roles
from ..models import DiscrepancyPatch
from ..security import SessionUser
from ..services.discrepancy import aggregate, enrich_row, summary
from ..services.shift import normalize_shift

router = APIRouter(prefix="/api/v1/discrepancy", tags=["discrepancy"])
settings = get_settings()


def _query_rows(date_from: date, date_to: date, shift: str | None) -> list[dict]:
    where = [sql.SQL("tanggal BETWEEN %s AND %s")]
    params: list = [date_from, date_to]
    if shift:
        where.append(sql.SQL("shift=%s"))
        params.append(normalize_shift(shift))
    query = sql.SQL("SELECT * FROM {} WHERE {} ORDER BY tanggal, CASE shift WHEN 'SHIFT_1' THEN 1 ELSE 2 END").format(
        qualified("v_fuel_discrepancy_shift"), sql.SQL(" AND ").join(where)
    )
    return [enrich_row(r) for r in fetch_all(query, params)]


@router.get("")
def list_discrepancy(
    date_from: date = Query(alias="from", default_factory=lambda: date.today() - timedelta(days=31)),
    date_to: date = Query(alias="to", default_factory=date.today),
    shift: str | None = None,
    period: str = "SHIFT",
    status: str | None = None,
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN", "GROUP_LEADER")),
) -> dict:
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="Tanggal akhir tidak boleh sebelum tanggal awal")
    rows = _query_rows(date_from, date_to, shift)
    if status:
        rows = [r for r in rows if str(r.get("final_status", "")).upper() == status.upper()]
    return {
        "ok": True,
        "period": period.upper(),
        "rows": rows,
        "series": {
            "shift": rows,
            "daily": aggregate(rows, "DAILY"),
            "weekly": aggregate(rows, "WEEKLY"),
            "monthly": aggregate(rows, "MONTHLY"),
        },
        "selected": aggregate(rows, period),
        "summary": summary(rows),
        "thresholds": {
            "daily_pct": settings.discrepancy_daily_target_pct,
            "weekly_pct": settings.discrepancy_weekly_target_pct,
            "mtd_pct": settings.discrepancy_mtd_target_pct,
            "stock_min_l": settings.discrepancy_stock_min_l,
            "fuel_availability_days": settings.fuel_availability_target_days,
        },
        "opening_source": settings.discrepancy_opening_source,
        "requested_by": user.username,
    }


@router.get("/{operation_date}/{shift}")
def discrepancy_detail(
    operation_date: date,
    shift: str,
    _: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN", "GROUP_LEADER")),
) -> dict:
    rows = _query_rows(operation_date, operation_date, normalize_shift(shift))
    if not rows:
        raise HTTPException(status_code=404, detail="Data discrepancy belum tersedia")
    return {"ok": True, "data": rows[0]}


@router.patch("/{operation_date}/{shift}")
def patch_discrepancy(
    operation_date: date,
    shift: str,
    payload: DiscrepancyPatch,
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
) -> dict:
    shift = normalize_shift(shift)
    data = payload.model_dump(exclude_unset=True)
    # These columns are NOT NULL in fuel_discrepancy_manual. Treat an explicit
    # JSON null as "leave current/default value" instead of generating a DB error.
    for protected in ("ba_l", "adjustment_l", "pica_status"):
        if data.get(protected, object()) is None:
            data.pop(protected, None)
    override_fields = {
        "stock_awal_override_l",
        "penerimaan_override_l",
        "fuel_keluar_override_l",
        "stock_aktual_override_l",
    }
    if not settings.allow_discrepancy_overrides and override_fields.intersection(data):
        raise HTTPException(
            status_code=403,
            detail="Override angka otomatis dinonaktifkan. Aktifkan FCC_ALLOW_DISCREPANCY_OVERRIDES hanya setelah approval.",
        )
    if not data:
        return discrepancy_detail(operation_date, shift, user)

    columns = ["site_code", "tanggal", "shift", *data.keys(), "input_by", "updated_by"]
    values = [settings.site_code, operation_date, shift, *data.values(), user.username, user.username]
    update_cols = [k for k in data.keys()] + ["updated_by", "updated_at"]
    set_sql = sql.SQL(", ").join(
        sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(k), sql.Identifier(k))
        if k != "updated_at"
        else sql.SQL("updated_at=now()")
        for k in update_cols
    )
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (site_code,tanggal,shift) DO UPDATE SET {} RETURNING *").format(
        qualified("fuel_discrepancy_manual"),
        sql.SQL(",").join(map(sql.Identifier, columns)),
        sql.SQL(",").join([sql.Placeholder()] * len(columns)),
        set_sql,
    )
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, values)
            saved = cur.fetchone()
    detail = _query_rows(operation_date, operation_date, shift)
    return {"ok": True, "manual": saved, "data": detail[0] if detail else None}


@router.get("/export.csv")
def export_discrepancy_csv(
    date_from: date = Query(alias="from", default_factory=lambda: date.today() - timedelta(days=31)),
    date_to: date = Query(alias="to", default_factory=date.today),
    shift: str | None = None,
    _: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN", "GROUP_LEADER")),
) -> Response:
    rows = _query_rows(date_from, date_to, shift)
    output = io.StringIO()
    fieldnames = [
        "tanggal", "shift", "kode", "stock_awal_l", "opening_source", "penerimaan_l", "ba_l",
        "adjustment_l", "fuel_keluar_l", "stock_akhir_buku_l", "stock_aktual_l", "discrepancy_l",
        "daily_deviasi_pct", "weekly_deviasi_pct", "mtd_discre_pct", "fuel_availability_days",
        "stock_status", "daily_status", "weekly_status", "mtd_status", "fuel_availability_status",
        "final_status", "cuaca", "remark", "pica_status", "pica_owner", "pica_due_date", "pica_note",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore", delimiter=";")
    writer.writeheader()
    writer.writerows(rows)
    payload = "\ufeffsep=;\r\n" + output.getvalue()
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="fuel_discrepancy_{date_from}_{date_to}.csv"'},
    )
