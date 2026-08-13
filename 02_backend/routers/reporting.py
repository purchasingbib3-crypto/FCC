from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import fetch_all
from ..dependencies import require_capability
from ..security import SessionUser
from ..services.xlsx_import import normalize_unit

router = APIRouter(prefix="/api/v1/reporting", tags=["reporting-dashboard"])

_PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_MATCH_TOLERANCE_L = 0.01


def _period_bounds(period: str | None) -> tuple[str, date, date]:
    value = (period or date.today().strftime("%Y-%m")).strip()
    match = _PERIOD_RE.fullmatch(value)
    if not match:
        raise HTTPException(status_code=422, detail="Periode harus berformat YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        next_month = date(year, month + 1, 1)
        end = date.fromordinal(next_month.toordinal() - 1)
    return value, start, end


def _source_coverage(period: str) -> dict[str, Any]:
    rows = fetch_all(
        """
        SELECT fir.sumber,
               min(fir.tanggal) AS date_from,
               max(fir.tanggal) AS date_to,
               count(*) AS rows,
               count(*) FILTER (WHERE fir.mapping_status='MAPPED') AS mapped_rows,
               count(*) FILTER (WHERE fir.mapping_status='UNMAPPED') AS unmapped_rows,
               count(*) FILTER (WHERE fir.mapping_status='AMBIGUOUS') AS ambiguous_rows
        FROM fcc.fuel_import_row fir
        JOIN fcc.import_batch ib ON ib.id=fir.batch_id
        WHERE ib.status='COMMITTED' AND ib.periode=%s
        GROUP BY fir.sumber
        """,
        (period,),
    )
    sources = {str(row.get("sumber") or "").upper(): row for row in rows}
    ss6, sap = sources.get("SS6"), sources.get("SAP")
    common_start = max(ss6["date_from"], sap["date_from"]) if ss6 and sap else None
    common_end = min(ss6["date_to"], sap["date_to"]) if ss6 and sap else None
    has_overlap = bool(common_start and common_end and common_start <= common_end)
    partial = bool(ss6 and sap and (ss6["date_from"] != sap["date_from"] or ss6["date_to"] != sap["date_to"]))
    result_sources: dict[str, Any] = {}
    for key, row in sources.items():
        total = int(row.get("rows") or 0)
        mapped = int(row.get("mapped_rows") or 0)
        result_sources[key] = {
            **row,
            "mapping_coverage_pct": round(mapped / total * 100, 3) if total else 0.0,
        }
    return {
        "sources": result_sources,
        "common_start": common_start,
        "common_end": common_end,
        "has_overlap": has_overlap,
        "partial": partial,
        "comparable_label": f"{common_start.isoformat()} s/d {common_end.isoformat()}" if has_overlap else "BELUM ADA OVERLAP",
    }


def _reconciliation_rows(period: str) -> list[dict[str, Any]]:
    # Unmapped raw rows are intentionally excluded from numeric reconciliation.
    rows = fetch_all(
        """
        WITH src AS (
          SELECT fir.tanggal, fir.unit_standar, fir.sumber, fir.volume_net_l, fir.quantity_source_l,
                 fir.shift, fir.storage_location,
                 mu.nama AS unit_nama, mu.vendor_kode, mu.kategori
          FROM fcc.fuel_import_row fir
          JOIN fcc.import_batch ib ON ib.id=fir.batch_id
          LEFT JOIN fcc.master_unit mu ON mu.kode=fir.unit_standar
          WHERE ib.status='COMMITTED' AND ib.periode=%s AND fir.mapping_status='MAPPED' AND fir.unit_standar IS NOT NULL
        ), grouped AS (
          SELECT tanggal, unit_standar,
                 max(unit_nama) AS unit_nama,
                 max(vendor_kode) AS vendor_kode,
                 max(kategori) AS kategori,
                 sum(volume_net_l) FILTER (WHERE sumber='SS6') AS ss6_l,
                 sum(volume_net_l) FILTER (WHERE sumber='SAP') AS sap_l,
                 count(*) FILTER (WHERE sumber='SS6') AS ss6_rows,
                 count(*) FILTER (WHERE sumber='SAP') AS sap_rows,
                 string_agg(DISTINCT NULLIF(shift,''), ', ' ORDER BY NULLIF(shift,'')) FILTER (WHERE sumber='SS6') AS shift_ss6,
                 string_agg(DISTINCT NULLIF(storage_location,''), ', ' ORDER BY NULLIF(storage_location,'')) FILTER (WHERE sumber='SS6') AS storage_ss6,
                 string_agg(DISTINCT NULLIF(storage_location,''), ', ' ORDER BY NULLIF(storage_location,'')) FILTER (WHERE sumber='SAP') AS storage_sap
          FROM src GROUP BY tanggal, unit_standar
        )
        SELECT *,
          COALESCE(sap_l,0)-COALESCE(ss6_l,0) AS delta_l,
          abs(COALESCE(sap_l,0)-COALESCE(ss6_l,0)) AS abs_delta_l,
          CASE
           WHEN COALESCE(ss6_l,0)=0 AND COALESCE(sap_l,0)<>0 THEN 'HANYA SAP'
           WHEN COALESCE(sap_l,0)=0 AND COALESCE(ss6_l,0)<>0 THEN 'HANYA SS6'
           WHEN abs(COALESCE(sap_l,0)-COALESCE(ss6_l,0)) <= %s THEN 'MATCH'
           ELSE 'SELISIH' END AS status
        FROM grouped
        ORDER BY abs(COALESCE(sap_l,0)-COALESCE(ss6_l,0)) DESC, tanggal, unit_standar
        """,
        (period, _MATCH_TOLERANCE_L),
    )
    coverage = _source_coverage(period)
    if coverage["has_overlap"]:
        start, end = coverage["common_start"], coverage["common_end"]
        for row in rows:
            if row.get("tanggal") < start or row.get("tanggal") > end:
                row["status"] = "OUTSIDE COVERAGE"
                row["coverage_status"] = "OUTSIDE"
            else:
                row["coverage_status"] = "COMPARABLE"
    return rows


def _batch_rows(period: str) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT id,kode,sumber,nama_file,periode,total_baris,baris_valid,baris_tolak,status,imported_by,imported_at,
               source_format,date_from,date_to,baris_mapped,baris_unmapped,baris_ambiguous
        FROM fcc.import_batch
        WHERE periode=%s
        ORDER BY imported_at DESC,id DESC
        """,
        (period,),
    )


def _unmapped_rows(period: str) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT fir.sumber,fir.alias_unit,count(*) AS rows,min(fir.tanggal) AS date_from,max(fir.tanggal) AS date_to,
               min(fir.source_row) AS first_source_row
        FROM fcc.fuel_import_row fir
        JOIN fcc.import_batch ib ON ib.id=fir.batch_id
        WHERE ib.status='COMMITTED' AND ib.periode=%s AND fir.mapping_status='UNMAPPED'
        GROUP BY fir.sumber,fir.alias_unit
        ORDER BY count(*) DESC,fir.sumber,fir.alias_unit
        """,
        (period,),
    )


def _ambiguous_rows(period: str) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT fir.sumber,fir.alias_unit,count(*) AS rows,min(fir.tanggal) AS date_from,max(fir.tanggal) AS date_to,
               min(fir.source_row) AS first_source_row
        FROM fcc.fuel_import_row fir
        JOIN fcc.import_batch ib ON ib.id=fir.batch_id
        WHERE ib.status='COMMITTED' AND ib.periode=%s AND fir.mapping_status='AMBIGUOUS'
        GROUP BY fir.sumber,fir.alias_unit
        ORDER BY count(*) DESC,fir.sumber,fir.alias_unit
        """,
        (period,),
    )


def _master_diagnostics() -> dict[str, Any]:
    units = fetch_all("SELECT kode,nama,vendor_kode,kategori,status FROM fcc.master_unit WHERE status='ACTIVE' ORDER BY kode")
    aliases = fetch_all(
        """
        SELECT id,unit_standar,alias_ss6,alias_sap,vendor_kode,kategori,status
        FROM fcc.unit_alias WHERE status='ACTIVE' ORDER BY unit_standar,id
        """
    )
    master_codes = {str(row.get("kode") or "") for row in units if row.get("kode")}
    standards_with_alias = {str(row.get("unit_standar") or "") for row in aliases if row.get("unit_standar")}
    missing = [row for row in units if str(row.get("kode") or "") not in standards_with_alias]
    orphan = [row for row in aliases if str(row.get("unit_standar") or "") not in master_codes]

    normalized: dict[str, set[str]] = defaultdict(set)
    alias_examples: dict[str, set[str]] = defaultdict(set)
    for row in aliases:
        standard = str(row.get("unit_standar") or "")
        for field in ("unit_standar", "alias_ss6", "alias_sap"):
            raw = row.get(field)
            key = normalize_unit(raw)
            if not key:
                continue
            normalized[key].add(standard)
            alias_examples[key].add(str(raw))
    collisions = [
        {"normalized_alias": key, "standards": sorted(values), "examples": sorted(alias_examples[key])}
        for key, values in normalized.items() if len(values) > 1
    ]
    collisions.sort(key=lambda row: row["normalized_alias"])
    return {
        "active_units": len(units),
        "active_alias_rows": len(aliases),
        "missing_alias_count": len(missing),
        "orphan_alias_count": len(orphan),
        "collision_count": len(collisions),
        "missing_alias": missing,
        "orphan_alias": orphan,
        "collisions": collisions,
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(row.get("status") or "UNKNOWN") for row in rows)
    comparable = [row for row in rows if str(row.get("status") or "") != "OUTSIDE COVERAGE"]
    total = len(comparable)
    match = sum(1 for row in comparable if str(row.get("status") or "") == "MATCH")
    ss6_l = sum(float(row.get("ss6_l") or 0) for row in comparable)
    sap_l = sum(float(row.get("sap_l") or 0) for row in comparable)
    return {
        "rows": len(rows),
        "comparable_rows": total,
        "outside_coverage_rows": counts.get("OUTSIDE COVERAGE", 0),
        "ss6_l": round(ss6_l, 3),
        "sap_l": round(sap_l, 3),
        "delta_l": round(sap_l - ss6_l, 3),
        "abs_delta_l": round(sum(float(row.get("abs_delta_l") or 0) for row in comparable), 3),
        "match_rate_pct": round(match / total * 100, 3) if total else 0,
        "status_counts": dict(counts),
    }


def _daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    daily: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("tanggal") or "")
        outside = str(row.get("status") or "") == "OUTSIDE COVERAGE"
        item = daily.setdefault(
            key,
            {"tanggal": row.get("tanggal"), "ss6_l": 0.0, "sap_l": 0.0, "delta_l": 0.0, "rows": 0, "exceptions": 0, "outside_coverage": outside},
        )
        item["ss6_l"] += float(row.get("ss6_l") or 0)
        item["sap_l"] += float(row.get("sap_l") or 0)
        item["rows"] += 1
        item["outside_coverage"] = item["outside_coverage"] or outside
        if not outside and str(row.get("status") or "") != "MATCH":
            item["exceptions"] += 1
    for item in daily.values():
        item["delta_l"] = round(item["sap_l"] - item["ss6_l"], 3)
        item["ss6_l"] = round(item["ss6_l"], 3)
        item["sap_l"] = round(item["sap_l"], 3)
    return [daily[key] for key in sorted(daily)]


@router.get("/overview")
def reporting_overview(period: str | None = None, _: SessionUser = Depends(require_capability("reporting.read"))) -> dict:
    period_value, _, _ = _period_bounds(period)
    rows = _reconciliation_rows(period_value)
    batches = _batch_rows(period_value)
    master = _master_diagnostics()
    coverage = _source_coverage(period_value)
    unmapped = _unmapped_rows(period_value)
    ambiguous = _ambiguous_rows(period_value)
    active_sources = {str(row.get("sumber") or "").upper() for row in batches if str(row.get("status") or "").upper() == "COMMITTED"}
    summary = _summary(rows)
    exceptions = [row for row in rows if str(row.get("status") or "") not in {"MATCH", "OUTSIDE COVERAGE"}]
    return {
        "ok": True,
        "period": period_value,
        "scope": {
            "sources": ["SS6", "SAP MB51"],
            "supports_mb51": True,
            "supports_zpme": False,
            "supports_mb52": False,
            "note": "SAP upload aktif menggunakan MB51. quantity_source_l mempertahankan sign source; KPI/reconciliation hanya memakai volume_net_l. ZPME16 dan MB52 belum menjadi source aktif.",
        },
        "readiness": {
            "ss6_committed": "SS6" in active_sources,
            "sap_committed": "SAP" in active_sources,
            "ready": {"SS6", "SAP"}.issubset(active_sources) and coverage["has_overlap"],
            "partial_coverage": coverage["partial"],
            "coverage": coverage,
            "unmapped_alias_groups": len(unmapped),
            "unmapped_rows": sum(int(row.get("rows") or 0) for row in unmapped),
            "ambiguous_alias_groups": len(ambiguous),
            "ambiguous_rows": sum(int(row.get("rows") or 0) for row in ambiguous),
        },
        "summary": summary,
        "daily": _daily(rows),
        "recent_exceptions": exceptions[:20],
        "batches": batches[:20],
        "master_health": {key: master[key] for key in ("active_units", "active_alias_rows", "missing_alias_count", "orphan_alias_count", "collision_count")},
    }


@router.get("/monthly")
def monthly_report(
    period: str | None = None,
    category: str | None = None,
    vendor: str | None = None,
    q: str | None = None,
    _: SessionUser = Depends(require_capability("reporting.read")),
) -> dict:
    period_value, _, _ = _period_bounds(period)
    all_rows = _reconciliation_rows(period_value)
    source_rows = [row for row in all_rows if str(row.get("status") or "") != "OUTSIDE COVERAGE"]
    coverage = _source_coverage(period_value)
    grouped: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        key = str(row.get("unit_standar") or "-")
        item = grouped.setdefault(
            key,
            {"unit_standar": row.get("unit_standar"), "unit_nama": row.get("unit_nama"), "vendor_kode": row.get("vendor_kode"), "kategori": row.get("kategori"), "ss6_l": 0.0, "sap_l": 0.0, "days": 0, "exception_days": 0, "only_ss6_days": 0, "only_sap_days": 0},
        )
        item["ss6_l"] += float(row.get("ss6_l") or 0)
        item["sap_l"] += float(row.get("sap_l") or 0)
        item["days"] += 1
        status = str(row.get("status") or "")
        if status != "MATCH":
            item["exception_days"] += 1
        if status == "HANYA SS6":
            item["only_ss6_days"] += 1
        if status == "HANYA SAP":
            item["only_sap_days"] += 1

    rows: list[dict[str, Any]] = []
    for item in grouped.values():
        item["ss6_l"] = round(item["ss6_l"], 3)
        item["sap_l"] = round(item["sap_l"], 3)
        item["delta_l"] = round(item["sap_l"] - item["ss6_l"], 3)
        item["abs_delta_l"] = round(abs(item["delta_l"]), 3)
        if item["only_ss6_days"] and not item["sap_l"]:
            item["status"] = "HANYA SS6"
        elif item["only_sap_days"] and not item["ss6_l"]:
            item["status"] = "HANYA SAP"
        # monthly net-zero never hides day-level mismatch: MATCH requires every comparable day to match.
        elif item["exception_days"] == 0 and item["abs_delta_l"] <= _MATCH_TOLERANCE_L:
            item["status"] = "MATCH"
        else:
            item["status"] = "SELISIH"
        rows.append(item)

    if category:
        rows = [row for row in rows if str(row.get("kategori") or "").upper() == category.upper()]
    if vendor:
        rows = [row for row in rows if str(row.get("vendor_kode") or "").upper() == vendor.upper()]
    if q:
        needle = q.strip().lower()
        rows = [row for row in rows if needle in " ".join(str(row.get(key) or "") for key in ("unit_standar", "unit_nama", "vendor_kode", "kategori")).lower()]
    rows.sort(key=lambda row: (-float(row.get("abs_delta_l") or 0), str(row.get("unit_standar") or "")))
    categories = sorted({str(row.get("kategori") or "") for row in grouped.values() if row.get("kategori")})
    vendors = sorted({str(row.get("vendor_kode") or "") for row in grouped.values() if row.get("vendor_kode")})
    return {
        "ok": True,
        "period": period_value,
        "coverage": coverage,
        "summary": _summary(all_rows),
        "data": rows,
        "total": len(rows),
        "daily": _daily(all_rows),
        "filters": {"categories": categories, "vendors": vendors},
        "logic_note": "Monthly Report hanya menghitung tanggal dalam common coverage SS6-SAP dan hanya mapping_status=MAPPED. SAP memakai volume_net_l (-signed MB51 source quantity). Status tetap SELISIH bila ada mismatch harian, walaupun delta net menjadi 0.",
    }


@router.get("/exceptions")
def exception_center(
    period: str | None = None,
    exception_type: str | None = Query(default=None, alias="type"),
    q: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    _: SessionUser = Depends(require_capability("reporting.read")),
) -> dict:
    period_value, period_start, period_end = _period_bounds(period)
    reconciliation = _reconciliation_rows(period_value)
    batches = _batch_rows(period_value)
    master = _master_diagnostics()
    coverage = _source_coverage(period_value)
    unmapped = _unmapped_rows(period_value)
    ambiguous = _ambiguous_rows(period_value)
    output: list[dict[str, Any]] = []

    for row in reconciliation:
        status = str(row.get("status") or "")
        if status in {"MATCH", "OUTSIDE COVERAGE"}:
            continue
        output.append({
            "type": "RECONCILIATION",
            "severity": "CRITICAL" if status in {"HANYA SS6", "HANYA SAP"} else "WARNING",
            "status": status,
            "tanggal": row.get("tanggal"),
            "reference": row.get("unit_standar"),
            "description": f"SS6 {float(row.get('ss6_l') or 0):,.3f} L · SAP {float(row.get('sap_l') or 0):,.3f} L · Δ {float(row.get('delta_l') or 0):,.3f} L",
            "unit_standar": row.get("unit_standar"), "vendor_kode": row.get("vendor_kode"), "kategori": row.get("kategori"),
        })

    if coverage["partial"]:
        ss6 = coverage["sources"].get("SS6", {})
        sap = coverage["sources"].get("SAP", {})
        output.append({
            "type": "SOURCE_COVERAGE",
            "severity": "WARNING",
            "status": "PARTIAL_COVERAGE",
            "tanggal": coverage.get("common_start"),
            "reference": period_value,
            "description": f"SS6 {ss6.get('date_from')}–{ss6.get('date_to')} · SAP {sap.get('date_from')}–{sap.get('date_to')} · comparable {coverage.get('common_start')}–{coverage.get('common_end')}. Di luar overlap tidak dihitung sebagai mismatch.",
        })

    for row in unmapped:
        output.append({
            "type": "IMPORT_ALIAS_UNMAPPED",
            "severity": "WARNING",
            "status": "UNMAPPED_ALIAS",
            "tanggal": row.get("date_from"),
            "reference": row.get("alias_unit"),
            "description": f"{row.get('sumber')} alias {row.get('alias_unit')} belum ada mapping · {int(row.get('rows') or 0)} baris ({row.get('date_from')}–{row.get('date_to')}).",
        })

    for row in ambiguous:
        output.append({
            "type": "IMPORT_ALIAS_AMBIGUOUS",
            "severity": "CRITICAL",
            "status": "AMBIGUOUS_ALIAS",
            "tanggal": row.get("date_from"),
            "reference": row.get("alias_unit"),
            "description": f"{row.get('sumber')} alias {row.get('alias_unit')} mempunyai lebih dari satu kandidat master · {int(row.get('rows') or 0)} baris. Tidak ikut reconciliation sampai collision diperbaiki.",
        })

    for row in batches:
        rejected = int(row.get("baris_tolak") or 0)
        if rejected > 0:
            output.append({"type": "IMPORT_BATCH", "severity": "CRITICAL", "status": "TECHNICAL_REJECT", "tanggal": row.get("imported_at"), "reference": row.get("kode"), "description": f"{row.get('sumber')} · {rejected} technical reject · {row.get('nama_file')}"})

    active_sources = {str(row.get("sumber") or "").upper() for row in batches if str(row.get("status") or "").upper() == "COMMITTED"}
    for source in ("SS6", "SAP"):
        if source not in active_sources:
            output.append({"type": "SOURCE_MISSING", "severity": "CRITICAL", "status": "BELUM UPLOAD", "tanggal": period_start, "reference": source, "description": f"Belum ada batch {source} COMMITTED untuk periode {period_value}."})

    for row in master["missing_alias"]:
        output.append({"type": "MASTER_ALIAS_MISSING", "severity": "WARNING", "status": "MISSING_ALIAS", "tanggal": None, "reference": row.get("kode"), "description": f"Unit aktif {row.get('kode')} belum memiliki UNIT_ALIAS aktif."})
    for row in master["orphan_alias"]:
        output.append({"type": "MASTER_ALIAS_ORPHAN", "severity": "WARNING", "status": "ORPHAN_ALIAS", "tanggal": None, "reference": row.get("unit_standar"), "description": f"UNIT_ALIAS menunjuk unit_standar {row.get('unit_standar')} yang tidak ada pada master unit aktif."})
    for row in master["collisions"]:
        output.append({"type": "MASTER_ALIAS_COLLISION", "severity": "CRITICAL", "status": "AMBIGUOUS_ALIAS", "tanggal": None, "reference": row.get("normalized_alias"), "description": f"Alias normalisasi sama mengarah ke beberapa unit: {', '.join(row.get('standards') or [])}"})

    vouchers = fetch_all("SELECT id,no_voucher,tanggal,unit_kode,liter,status,remark FROM fcc.voucher_bib WHERE tanggal BETWEEN %s AND %s AND status <> 'MATCH' ORDER BY tanggal,id", (period_start, period_end))
    for row in vouchers:
        status = str(row.get("status") or "")
        output.append({"type": "VOUCHER", "severity": "CRITICAL" if status in {"BELUM MATCH", "SELISIH"} else "WARNING", "status": status, "tanggal": row.get("tanggal"), "reference": row.get("no_voucher"), "description": f"{row.get('unit_kode')} · {float(row.get('liter') or 0):,.3f} L · {row.get('remark') or status}"})

    if exception_type:
        output = [row for row in output if str(row.get("type") or "").upper() == exception_type.upper()]
    if q:
        needle = q.strip().lower()
        output = [row for row in output if needle in " ".join(str(row.get(key) or "") for key in ("type", "status", "reference", "description", "unit_standar", "vendor_kode", "kategori")).lower()]
    severity_rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    output.sort(key=lambda row: (severity_rank.get(str(row.get("severity") or ""), 9), str(row.get("tanggal") or ""), str(row.get("reference") or "")))
    counts = Counter(str(row.get("type") or "UNKNOWN") for row in output)
    severity = Counter(str(row.get("severity") or "UNKNOWN") for row in output)
    return {"ok": True, "period": period_value, "total": len(output), "summary": {"type_counts": dict(counts), "severity_counts": dict(severity)}, "data": output[:limit], "truncated": len(output) > limit, "coverage": coverage}


@router.get("/master-health")
def master_health(_: SessionUser = Depends(require_capability("reporting.read"))) -> dict:
    return {"ok": True, **_master_diagnostics()}
