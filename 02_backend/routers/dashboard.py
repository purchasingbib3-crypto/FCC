from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql

from ..config import get_settings
from ..db import fetch_all, fetch_one, qualified
from ..dependencies import require_capability
from ..security import SessionUser
from ..services.discrepancy import aggregate, enrich_row, summary

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
settings = get_settings()


def _safe_scalar(query: sql.Composed | str, params: tuple = ()) -> float:
    row = fetch_one(query, params)
    return float(next(iter(row.values())) or 0) if row else 0.0


def _shift_value(value: str | None) -> str | None:
    normalized = str(value or "").strip().upper()
    if not normalized or normalized in {"ALL", "SEMUA"}:
        return None
    if normalized not in {"SHIFT_1", "SHIFT_2"}:
        raise HTTPException(status_code=422, detail="shift harus SHIFT_1, SHIFT_2, atau kosong")
    return normalized


def _validate_period(date_from: date, date_to: date, max_days: int = 92) -> None:
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="Tanggal awal tidak boleh melebihi tanggal akhir")
    if (date_to - date_from).days > max_days:
        raise HTTPException(status_code=422, detail=f"Rentang dashboard maksimum {max_days + 1} hari")


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _daily_bucket() -> dict[str, Any]:
    return {
        "penerimaan_l": 0.0,
        "transfer_l": 0.0,
        "refuelling_l": 0.0,
        "drainage_l": 0.0,
        "flowmeter_l": 0.0,
        "flowmeter_rows": 0,
        "hm_rows": 0,
        "sounding_rows": 0,
        "cleanliness_rows": 0,
    }


@router.get("/overview")
def dashboard_overview(
    date_from: date = Query(alias="from", default_factory=date.today),
    date_to: date = Query(alias="to", default_factory=date.today),
    shift: str | None = Query(default=None),
    limit: int = Query(default=120, ge=20, le=500),
    _: SessionUser = Depends(require_capability("dashboard.read")),
) -> dict:
    """Canonical dashboard utama.

    One endpoint aggregates all active field input families plus closing/discrepancy.
    The browser does not need to fan out many table requests just to build the main dashboard.
    """
    _validate_period(date_from, date_to)
    shift_value = _shift_value(shift)
    range_params = (date_from, date_to, shift_value, shift_value)

    transfer = fetch_all(
        sql.SQL(
            "SELECT tanggal,count(*) AS rows,sum(COALESCE(total_fm_liter,0)) AS volume_l,"
            "count(*) FILTER (WHERE status_deviasi='WARNING') AS warning_rows,"
            "count(*) FILTER (WHERE status_deviasi='CRITICAL') AS critical_rows "
            "FROM {} WHERE tanggal BETWEEN %s AND %s "
            "AND (%s::text IS NULL OR shift::text=%s::text) AND voided_at IS NULL "
            "GROUP BY tanggal ORDER BY tanggal"
        ).format(qualified("fuel_v_transfer_fuel")),
        range_params,
    )
    monitoring = fetch_all(
        sql.SQL(
            "SELECT tanggal,monitoring_type::text AS monitoring_type,count(*) AS rows,"
            "sum(COALESCE(total_fm_liter,0)) AS volume_l "
            "FROM {} WHERE tanggal BETWEEN %s AND %s "
            "AND (%s::text IS NULL OR shift::text=%s::text) AND voided_at IS NULL "
            "GROUP BY tanggal,monitoring_type ORDER BY tanggal,monitoring_type"
        ).format(qualified("fuel_v_fuel_truck_monitoring")),
        range_params,
    )
    receipts = fetch_all(
        sql.SQL(
            "SELECT tanggal,count(*) AS rows,sum(COALESCE(total_fm_l,fm_akhir-fm_awal,0)) AS volume_l,"
            "count(*) FILTER (WHERE COALESCE(tera_status,status)='WARNING') AS warning_rows,"
            "count(*) FILTER (WHERE COALESCE(tera_status,status)='CRITICAL') AS critical_rows "
            "FROM {} WHERE tanggal BETWEEN %s AND %s "
            "AND (%s::text IS NULL OR shift=%s::text) AND COALESCE(status,'VALID') NOT IN ('VOID','DRAFT') "
            "GROUP BY tanggal ORDER BY tanggal"
        ).format(qualified("penerimaan_mo")),
        range_params,
    )
    drainage = fetch_all(
        sql.SQL(
            "SELECT tanggal,count(*) AS rows,sum(COALESCE(total_fm_l,0)) AS volume_l,"
            "count(*) FILTER (WHERE status='WARNING') AS warning_rows,"
            "count(*) FILTER (WHERE status='TIDAK VALID') AS critical_rows "
            "FROM {} WHERE tanggal BETWEEN %s AND %s "
            "AND (%s::text IS NULL OR shift=%s::text) AND COALESCE(status,'OK')<>'VOID' "
            "GROUP BY tanggal ORDER BY tanggal"
        ).format(qualified("pengurasan")),
        range_params,
    )
    sounding = fetch_all(
        sql.SQL(
            "SELECT tanggal,count(*) AS rows,"
            "count(*) FILTER (WHERE COALESCE(sounding_status,status)='WARNING') AS warning_rows,"
            "count(*) FILTER (WHERE COALESCE(sounding_status,status)='CRITICAL') AS critical_rows "
            "FROM {} WHERE tanggal BETWEEN %s AND %s "
            "AND (%s::text IS NULL OR shift=%s::text) AND COALESCE(status,'VALID')<>'VOID' "
            "GROUP BY tanggal ORDER BY tanggal"
        ).format(qualified("sounding_main_tank")),
        range_params,
    )
    cleanliness = fetch_all(
        sql.SQL(
            "SELECT tanggal,count(*) AS rows,count(*) FILTER (WHERE status='OK') AS ok_rows,"
            "count(*) FILTER (WHERE status='WARNING') AS warning_rows "
            "FROM {} WHERE tanggal BETWEEN %s AND %s "
            "AND (%s::text IS NULL OR shift=%s::text) "
            "GROUP BY tanggal ORDER BY tanggal"
        ).format(qualified("cleanliness")),
        range_params,
    )
    refuel = fetch_all(
        sql.SQL(
            "SELECT tanggal,count(*) AS rows,sum(COALESCE(volume_l,0)) AS volume_l "
            "FROM {} WHERE tanggal BETWEEN %s AND %s "
            "AND (%s::text IS NULL OR shift=%s::text) AND COALESCE(status,'VALID') NOT IN ('VOID','DRAFT') "
            "GROUP BY tanggal ORDER BY tanggal"
        ).format(qualified("refuelling")),
        range_params,
    )
    closing = fetch_all(
        sql.SQL(
            "SELECT h.tanggal,h.shift,h.status,sum(l.total_administrasi_l) AS administrasi_l,"
            "sum(l.aktual_l) AS aktual_l,sum(l.deviasi_total_l) AS deviasi_l "
            "FROM {} h JOIN {} l ON l.closing_id=h.id "
            "WHERE h.tanggal BETWEEN %s AND %s AND (%s::text IS NULL OR h.shift=%s::text) "
            "GROUP BY h.tanggal,h.shift,h.status ORDER BY h.tanggal,h.shift"
        ).format(qualified("closing_stock"), qualified("closing_stock_line")),
        range_params,
    )
    discrepancy_rows = [
        enrich_row(r)
        for r in fetch_all(
            sql.SQL(
                "SELECT * FROM {} WHERE tanggal BETWEEN %s AND %s "
                "AND (%s::text IS NULL OR shift=%s::text) ORDER BY tanggal,shift"
            ).format(qualified("v_fuel_discrepancy_shift")),
            range_params,
        )
    ]

    daily: dict[str, dict[str, Any]] = {}

    def day(row: dict[str, Any]) -> dict[str, Any]:
        key = str(row["tanggal"])
        if key not in daily:
            daily[key] = {"tanggal": row["tanggal"], **_daily_bucket()}
        return daily[key]

    for row in receipts:
        day(row)["penerimaan_l"] = _as_float(row.get("volume_l"))
    for row in transfer:
        day(row)["transfer_l"] = _as_float(row.get("volume_l"))
    for row in refuel:
        day(row)["refuelling_l"] = _as_float(row.get("volume_l"))
    for row in drainage:
        day(row)["drainage_l"] = _as_float(row.get("volume_l"))
    for row in monitoring:
        bucket = day(row)
        if str(row.get("monitoring_type") or "").upper() == "FLOWMETER":
            bucket["flowmeter_rows"] = _as_int(row.get("rows"))
            bucket["flowmeter_l"] = _as_float(row.get("volume_l"))
        elif str(row.get("monitoring_type") or "").upper() == "HM":
            bucket["hm_rows"] = _as_int(row.get("rows"))
    for row in sounding:
        day(row)["sounding_rows"] = _as_int(row.get("rows"))
    for row in cleanliness:
        day(row)["cleanliness_rows"] = _as_int(row.get("rows"))

    warning_count = (
        sum(_as_int(r.get("warning_rows")) for r in transfer)
        + sum(_as_int(r.get("warning_rows")) for r in receipts)
        + sum(_as_int(r.get("warning_rows")) for r in drainage)
        + sum(_as_int(r.get("warning_rows")) for r in sounding)
        + sum(_as_int(r.get("warning_rows")) for r in cleanliness)
    )
    critical_count = (
        sum(_as_int(r.get("critical_rows")) for r in transfer)
        + sum(_as_int(r.get("critical_rows")) for r in receipts)
        + sum(_as_int(r.get("critical_rows")) for r in drainage)
        + sum(_as_int(r.get("critical_rows")) for r in sounding)
    )
    cleanliness_rows = sum(_as_int(r.get("rows")) for r in cleanliness)
    cleanliness_ok = sum(_as_int(r.get("ok_rows")) for r in cleanliness)
    discrepancy_kpi = summary(discrepancy_rows)

    # Recent activity covers every Field input module + refuelling.
    recent_selects = [
        sql.SQL(
            "SELECT 'TRANSFER'::text AS modul,id::text AS record_id,'transfer_fuel'::text AS evidence_modul,"
            "id::text AS evidence_record,tanggal,shift::text AS shift,COALESCE(fuel_truck_code,'-')::text AS asset,"
            "COALESCE(petugas_name,'')::text AS petugas,COALESCE(status_deviasi,'-')::text AS status,"
            "COALESCE(total_fm_liter,0)::numeric AS volume_l,created_at "
            "FROM {} WHERE tanggal BETWEEN %s AND %s AND voided_at IS NULL"
        ).format(qualified("fuel_v_transfer_fuel")),
        sql.SQL(
            "SELECT 'FLOWMETER'::text AS modul,id::text AS record_id,'flowmeter_ft'::text AS evidence_modul,"
            "id::text AS evidence_record,tanggal,shift::text AS shift,COALESCE(fuel_truck_code,'-')::text AS asset,"
            "COALESCE(petugas_name,'')::text AS petugas,'VALID'::text AS status,"
            "COALESCE(total_fm_liter,0)::numeric AS volume_l,created_at "
            "FROM {} WHERE tanggal BETWEEN %s AND %s AND monitoring_type::text='FLOWMETER' AND voided_at IS NULL"
        ).format(qualified("fuel_v_fuel_truck_monitoring")),
        sql.SQL(
            "SELECT 'HM'::text AS modul,id::text AS record_id,'hour_meter'::text AS evidence_modul,"
            "id::text AS evidence_record,tanggal,shift::text AS shift,COALESCE(fuel_truck_code,'-')::text AS asset,"
            "COALESCE(petugas_name,'')::text AS petugas,'VALID'::text AS status,"
            "NULL::numeric AS volume_l,created_at "
            "FROM {} WHERE tanggal BETWEEN %s AND %s AND monitoring_type::text='HM' AND voided_at IS NULL"
        ).format(qualified("fuel_v_fuel_truck_monitoring")),
        sql.SQL(
            "SELECT 'RECEIVING'::text AS modul,id::text AS record_id,'penerimaan_mo'::text AS evidence_modul,"
            "kode::text AS evidence_record,tanggal,shift::text AS shift,"
            "(COALESCE(id_ft,'-') || ' → ' || COALESCE(main_tank,'-'))::text AS asset,"
            "COALESCE(petugas,'')::text AS petugas,COALESCE(tera_status,status,'-')::text AS status,"
            "COALESCE(total_fm_l,0)::numeric AS volume_l,created_at "
            "FROM {} WHERE tanggal BETWEEN %s AND %s AND COALESCE(status,'VALID')<>'VOID'"
        ).format(qualified("penerimaan_mo")),
        sql.SQL(
            "SELECT 'DRAINAGE'::text AS modul,id::text AS record_id,'pengurasan'::text AS evidence_modul,"
            "kode::text AS evidence_record,tanggal,shift::text AS shift,COALESCE(aset,'-')::text AS asset,"
            "COALESCE(petugas,'')::text AS petugas,COALESCE(status,'-')::text AS status,"
            "COALESCE(total_fm_l,0)::numeric AS volume_l,created_at "
            "FROM {} WHERE tanggal BETWEEN %s AND %s AND COALESCE(status,'OK')<>'VOID'"
        ).format(qualified("pengurasan")),
        sql.SQL(
            "SELECT 'SOUNDING'::text AS modul,id::text AS record_id,'sounding_main_tank'::text AS evidence_modul,"
            "kode::text AS evidence_record,tanggal,shift::text AS shift,COALESCE(main_tank,'-')::text AS asset,"
            "COALESCE(petugas,'')::text AS petugas,COALESCE(sounding_status,status,'-')::text AS status,"
            "COALESCE(aktual_l,0)::numeric AS volume_l,created_at "
            "FROM {} WHERE tanggal BETWEEN %s AND %s AND COALESCE(status,'VALID')<>'VOID'"
        ).format(qualified("sounding_main_tank")),
        sql.SQL(
            "SELECT 'CLEANLINESS'::text AS modul,id::text AS record_id,'cleanliness'::text AS evidence_modul,"
            "kode::text AS evidence_record,tanggal,COALESCE(shift,'-')::text AS shift,COALESCE(aset,'-')::text AS asset,"
            "COALESCE(petugas,'')::text AS petugas,COALESCE(status,'-')::text AS status,"
            "NULL::numeric AS volume_l,created_at "
            "FROM {} WHERE tanggal BETWEEN %s AND %s"
        ).format(qualified("cleanliness")),
        sql.SQL(
            "SELECT 'REFUELING'::text AS modul,id::text AS record_id,NULL::text AS evidence_modul,"
            "NULL::text AS evidence_record,tanggal,shift::text AS shift,COALESCE(unit_kode,'-')::text AS asset,"
            "COALESCE(petugas,'')::text AS petugas,COALESCE(status,'VALID')::text AS status,"
            "COALESCE(volume_l,0)::numeric AS volume_l,created_at "
            "FROM {} WHERE tanggal BETWEEN %s AND %s AND COALESCE(status,'VALID') NOT IN ('VOID','DRAFT')"
        ).format(qualified("refuelling")),
    ]
    recent_sql = sql.SQL(
        "SELECT * FROM ({}) AS activity "
        "WHERE (%s::text IS NULL OR shift=%s::text) "
        "ORDER BY created_at DESC NULLS LAST LIMIT %s"
    ).format(sql.SQL(" UNION ALL ").join(recent_selects))
    recent_params: list[Any] = []
    for _select in recent_selects:
        recent_params.extend([date_from, date_to])
    recent_params.extend([shift_value, shift_value, limit])
    recent = fetch_all(recent_sql, recent_params)

    return {
        "ok": True,
        "period": {"from": date_from, "to": date_to, "shift": shift_value or "ALL"},
        "kpi": {
            "penerimaan_l": sum(_as_float(r.get("volume_l")) for r in receipts),
            "penerimaan_rows": sum(_as_int(r.get("rows")) for r in receipts),
            "transfer_l": sum(_as_float(r.get("volume_l")) for r in transfer),
            "transfer_rows": sum(_as_int(r.get("rows")) for r in transfer),
            "refuelling_l": sum(_as_float(r.get("volume_l")) for r in refuel),
            "refuelling_rows": sum(_as_int(r.get("rows")) for r in refuel),
            "drainage_l": sum(_as_float(r.get("volume_l")) for r in drainage),
            "drainage_rows": sum(_as_int(r.get("rows")) for r in drainage),
            "flowmeter_rows": sum(_as_int(r.get("rows")) for r in monitoring if str(r.get("monitoring_type") or "").upper() == "FLOWMETER"),
            "hm_rows": sum(_as_int(r.get("rows")) for r in monitoring if str(r.get("monitoring_type") or "").upper() == "HM"),
            "sounding_rows": sum(_as_int(r.get("rows")) for r in sounding),
            "cleanliness_rows": cleanliness_rows,
            "cleanliness_ok_pct": cleanliness_ok / max(1, cleanliness_rows) * 100,
            "warning_count": warning_count,
            "critical_count": critical_count,
            "closing_count": len(closing),
            "closing_closed": sum(1 for r in closing if str(r.get("status") or "").upper() == "CLOSED"),
            "discrepancy": discrepancy_kpi,
        },
        "series": {
            "daily": [daily[key] for key in sorted(daily)],
            "discrepancy_shift": discrepancy_rows,
            "discrepancy_daily": aggregate(discrepancy_rows, "DAILY"),
            "closing": closing,
        },
        "recent": recent,
    }


@router.get("/summary")
def dashboard_summary(
    date_from: date = Query(alias="from", default_factory=lambda: date.today() - timedelta(days=6)),
    date_to: date = Query(alias="to", default_factory=date.today),
    _: SessionUser = Depends(require_capability("dashboard.read")),
) -> dict:
    _validate_period(date_from, date_to)
    receipts = fetch_all(
        sql.SQL(
            "SELECT tanggal,sum(COALESCE(total_fm_l,fm_akhir-fm_awal,0)) AS volume_l,count(*) AS rows "
            "FROM {} WHERE tanggal BETWEEN %s AND %s AND COALESCE(status,'VALID') NOT IN ('VOID','DRAFT') GROUP BY tanggal ORDER BY tanggal"
        ).format(qualified("penerimaan_mo")),
        (date_from, date_to),
    )
    refuel = fetch_all(
        sql.SQL(
            "SELECT tanggal,sum(volume_l) AS volume_l,count(*) AS rows FROM {} "
            "WHERE tanggal BETWEEN %s AND %s AND COALESCE(status,'VALID') NOT IN ('VOID','DRAFT') GROUP BY tanggal ORDER BY tanggal"
        ).format(qualified("refuelling")),
        (date_from, date_to),
    )
    discrepancy_rows = [
        enrich_row(r)
        for r in fetch_all(
            sql.SQL("SELECT * FROM {} WHERE tanggal BETWEEN %s AND %s ORDER BY tanggal,shift").format(
                qualified("v_fuel_discrepancy_shift")
            ),
            (date_from, date_to),
        )
    ]
    cleanliness = fetch_all(
        sql.SQL(
            "SELECT tanggal,jenis,count(*) AS rows,"
            "avg(after_4) AS avg_after_4,avg(after_6) AS avg_after_6,avg(after_14) AS avg_after_14,"
            "count(*) FILTER (WHERE status='OK') AS ok_rows "
            "FROM {} WHERE tanggal BETWEEN %s AND %s GROUP BY tanggal,jenis ORDER BY tanggal,jenis"
        ).format(qualified("cleanliness")),
        (date_from, date_to),
    )
    closing = fetch_all(
        sql.SQL(
            "SELECT h.tanggal,h.shift,h.status,sum(l.total_administrasi_l) AS administrasi_l,"
            "sum(l.aktual_l) AS aktual_l,sum(l.deviasi_total_l) AS deviasi_l "
            "FROM {} h JOIN {} l ON l.closing_id=h.id WHERE h.tanggal BETWEEN %s AND %s "
            "GROUP BY h.tanggal,h.shift,h.status ORDER BY h.tanggal,h.shift"
        ).format(qualified("closing_stock"), qualified("closing_stock_line")),
        (date_from, date_to),
    )
    return {
        "ok": True,
        "period": {"from": date_from, "to": date_to},
        "kpi": {
            "penerimaan_l": sum(float(x["volume_l"] or 0) for x in receipts),
            "fuel_keluar_l": sum(float(x["volume_l"] or 0) for x in refuel),
            "discrepancy": summary(discrepancy_rows),
            "cleanliness_ok_pct": (
                sum(int(x["ok_rows"] or 0) for x in cleanliness) / max(1, sum(int(x["rows"] or 0) for x in cleanliness)) * 100
            ),
            "closing_count": len(closing),
        },
        "series": {
            "receipts": receipts,
            "refuelling": refuel,
            "discrepancy_shift": discrepancy_rows,
            "discrepancy_daily": aggregate(discrepancy_rows, "DAILY"),
            "cleanliness": cleanliness,
            "closing": closing,
        },
    }


@router.get("/cleanliness")
def cleanliness_dashboard(
    date_from: date = Query(alias="from", default_factory=lambda: date.today() - timedelta(days=30)),
    date_to: date = Query(alias="to", default_factory=date.today),
    _: SessionUser = Depends(require_capability("dashboard.read")),
) -> dict:
    _validate_period(date_from, date_to)
    rows = fetch_all(
        sql.SQL("SELECT * FROM {} WHERE tanggal BETWEEN %s AND %s ORDER BY tanggal,jam").format(
            qualified("cleanliness")
        ),
        (date_from, date_to),
    )
    costs: list[dict[str, Any]] = []
    try:
        costs = fetch_all(
            sql.SQL("SELECT * FROM {} ORDER BY replacement_date DESC,id DESC LIMIT 500").format(
                qualified("cleanliness_filter_cost")
            )
        )
    except Exception:
        costs = []
    return {"ok": True, "data": rows, "filter_cost": costs}


@router.get("/supply-sla")
def supply_sla(
    date_from: date = Query(alias="from", default_factory=lambda: date.today() - timedelta(days=30)),
    date_to: date = Query(alias="to", default_factory=date.today),
    _: SessionUser = Depends(require_capability("dashboard.read")),
) -> dict:
    _validate_period(date_from, date_to)
    actual = fetch_all(
        sql.SQL(
            "SELECT p.tanggal,p.shift,p.vendor_kode,count(*) AS actual_ritase,"
            "sum(COALESCE(p.total_fm_l,p.fm_akhir-p.fm_awal,0)) AS actual_l,"
            "min(p.jam_start) AS first_start,max(p.jam_stop) AS last_stop "
            "FROM {} p WHERE p.tanggal BETWEEN %s AND %s AND COALESCE(p.status,'VALID') NOT IN ('VOID','DRAFT') "
            "GROUP BY p.tanggal,p.shift,p.vendor_kode ORDER BY p.tanggal,p.shift,p.vendor_kode"
        ).format(qualified("penerimaan_mo")),
        (date_from, date_to),
    )
    plans: list[dict[str, Any]] = []
    try:
        plans = fetch_all(
            sql.SQL(
                "SELECT DISTINCT ON (tanggal,shift,vendor_kode) * FROM {} "
                "WHERE tanggal BETWEEN %s AND %s AND status IN ('APPROVED','DONE') "
                "ORDER BY tanggal,shift,vendor_kode,"
                "CASE WHEN status='APPROVED' THEN 0 ELSE 1 END,updated_at DESC"
            ).format(qualified("fuel_supply_plan")),
            (date_from, date_to),
        )
    except Exception:
        plans = []
    plan_map = {(p["tanggal"], p["shift"], p["vendor_kode"]): p for p in plans}
    output = []
    keys = set(plan_map) | {(a["tanggal"], a["shift"], a["vendor_kode"]) for a in actual}
    actual_map = {(a["tanggal"], a["shift"], a["vendor_kode"]): a for a in actual}
    for key in sorted(keys):
        plan = plan_map.get(key, {})
        act = actual_map.get(key, {})
        planned_l = float(plan.get("planned_l") or 0)
        actual_l = float(act.get("actual_l") or 0)
        output.append(
            {
                "tanggal": key[0],
                "shift": key[1],
                "vendor_kode": key[2],
                "planned_l": planned_l,
                "actual_l": actual_l,
                "volume_achievement_pct": actual_l / planned_l * 100 if planned_l else None,
                "planned_ritase": int(plan.get("planned_ritase") or 0),
                "actual_ritase": int(act.get("actual_ritase") or 0),
                "ritase_achievement_pct": (
                    int(act.get("actual_ritase") or 0) / int(plan.get("planned_ritase") or 1) * 100
                    if int(plan.get("planned_ritase") or 0) else None
                ),
                "first_start": act.get("first_start"),
                "last_stop": act.get("last_stop"),
                "status": "ON TARGET" if planned_l and actual_l >= planned_l else "OUT OF TARGET" if planned_l else "NO PLAN",
            }
        )
    return {"ok": True, "data": output}


@router.get("/history")
def history(
    limit: int = Query(default=200, ge=1, le=2000),
    _: SessionUser = Depends(require_capability("history.read")),
) -> dict:
    # Include the current Field V7 transaction families; the legacy-only history
    # endpoint previously hid Transfer/Flowmeter/HM from Dashboard Utama.
    unions = [
        sql.SQL(
            "SELECT 'TRANSFER'::text AS modul,id::text AS record_id,tanggal,shift::text AS shift,"
            "COALESCE(fuel_truck_code,'-')::text AS asset,COALESCE(petugas_name,'')::text AS petugas,"
            "COALESCE(status_deviasi,'-')::text AS status,created_at FROM {} WHERE voided_at IS NULL"
        ).format(qualified("fuel_v_transfer_fuel")),
        sql.SQL(
            "SELECT monitoring_type::text AS modul,id::text AS record_id,tanggal,shift::text AS shift,"
            "COALESCE(fuel_truck_code,'-')::text AS asset,COALESCE(petugas_name,'')::text AS petugas,"
            "'VALID'::text AS status,created_at FROM {} WHERE voided_at IS NULL"
        ).format(qualified("fuel_v_fuel_truck_monitoring")),
        sql.SQL(
            "SELECT 'PENERIMAAN'::text AS modul,kode::text AS record_id,tanggal,shift::text AS shift,"
            "COALESCE(main_tank,'-')::text AS asset,COALESCE(petugas,'')::text AS petugas,"
            "COALESCE(tera_status,status,'-')::text AS status,created_at FROM {} WHERE COALESCE(status,'VALID')<>'VOID'"
        ).format(qualified("penerimaan_mo")),
        sql.SQL(
            "SELECT 'PENGURASAN'::text AS modul,kode::text AS record_id,tanggal,shift::text AS shift,"
            "COALESCE(aset,'-')::text AS asset,COALESCE(petugas,'')::text AS petugas,"
            "COALESCE(status,'-')::text AS status,created_at FROM {} WHERE COALESCE(status,'OK')<>'VOID'"
        ).format(qualified("pengurasan")),
        sql.SQL(
            "SELECT 'SOUNDING'::text AS modul,kode::text AS record_id,tanggal,shift::text AS shift,"
            "COALESCE(main_tank,'-')::text AS asset,COALESCE(petugas,'')::text AS petugas,"
            "COALESCE(sounding_status,status,'-')::text AS status,created_at FROM {} WHERE COALESCE(status,'VALID')<>'VOID'"
        ).format(qualified("sounding_main_tank")),
        sql.SQL(
            "SELECT 'CLEANLINESS'::text AS modul,kode::text AS record_id,tanggal,COALESCE(shift,'-')::text AS shift,"
            "COALESCE(aset,'-')::text AS asset,COALESCE(petugas,'')::text AS petugas,"
            "COALESCE(status,'-')::text AS status,created_at FROM {}"
        ).format(qualified("cleanliness")),
        sql.SQL(
            "SELECT 'REFUELING'::text AS modul,no_voucher::text AS record_id,tanggal,shift::text AS shift,"
            "COALESCE(unit_kode,'-')::text AS asset,COALESCE(petugas,'')::text AS petugas,"
            "COALESCE(status,'VALID')::text AS status,created_at FROM {} WHERE COALESCE(status,'VALID') NOT IN ('VOID','DRAFT')"
        ).format(qualified("refuelling")),
    ]
    query = sql.SQL(" UNION ALL ").join(unions) + sql.SQL(" ORDER BY created_at DESC NULLS LAST LIMIT %s")
    rows = fetch_all(query, (limit,))
    return {"ok": True, "data": rows}
