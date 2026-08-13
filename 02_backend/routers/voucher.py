from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from psycopg import sql

from ..db import fetch_all, qualified
from ..dependencies import require_roles
from ..security import SessionUser
from ..services.xlsx_import import normalize_unit

router = APIRouter(prefix="/api/v1/voucher", tags=["voucher-validation"])


def _round_liter(value: Any) -> float:
    return round(float(value or 0), 2)


def _day_diff(a: date, b: date) -> int:
    return (a - b).days


def _source_rows() -> list[dict[str, Any]]:
    return fetch_all(
        sql.SQL(
            "SELECT fir.id,fir.sumber,fir.tanggal,fir.unit_standar,fir.volume_net_l,fir.quantity_source_l,fir.source_row "
            "FROM {} fir JOIN {} ib ON ib.id=fir.batch_id "
            "WHERE ib.status='COMMITTED' AND fir.mapping_status='MAPPED' AND fir.sumber IN ('SS6','SAP') "
            "ORDER BY fir.tanggal,fir.id"
        ).format(qualified("fuel_import_row"), qualified("import_batch"))
    )


def _voucher_rows() -> list[dict[str, Any]]:
    return fetch_all(
        sql.SQL("SELECT * FROM {} ORDER BY tanggal,no_voucher").format(qualified("voucher_bib"))
    )


def _build_index(rows: list[dict[str, Any]], source: str) -> dict[tuple[str, float], list[dict[str, Any]]]:
    index: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("sumber") != source:
            continue
        key = (normalize_unit(row.get("unit_standar")), _round_liter(row.get("volume_net_l")))
        # Reversal/negative net rows are audit data, not voucher-consumption candidates.
        if not key[0] or key[1] <= 0:
            continue
        index[key].append({**row, "used": False})
    return index


def _best_match(
    voucher: dict[str, Any],
    index: dict[tuple[str, float], list[dict[str, Any]]],
    tolerance_days: int,
) -> dict[str, Any] | None:
    key = (normalize_unit(voucher.get("unit_kode")), _round_liter(voucher.get("liter")))
    candidates = index.get(key, [])
    best: dict[str, Any] | None = None
    best_score: tuple[int, int] | None = None
    for candidate in candidates:
        if candidate["used"]:
            continue
        diff = _day_diff(candidate["tanggal"], voucher["tanggal"])
        if abs(diff) > tolerance_days:
            continue
        score = (abs(diff), int(candidate["id"]))
        if best_score is None or score < best_score:
            best = candidate
            best_score = score
    if best:
        best["used"] = True
        return {
            "id": best["id"],
            "tanggal": best["tanggal"],
            "date_diff": _day_diff(best["tanggal"], voucher["tanggal"]),
            "liter": _round_liter(best["volume_net_l"]),
            "quantity_source_l": best.get("quantity_source_l"),
            "source_row": best.get("source_row"),
        }
    return None


def _status(ss6: dict | None, sap: dict | None) -> str:
    if ss6 and sap:
        return "MATCH BEDA TANGGAL" if ss6["date_diff"] or sap["date_diff"] else "MATCH"
    if ss6:
        return "MATCH SS6 SAJA"
    if sap:
        return "MATCH SAP SAJA"
    return "BELUM MATCH"


def _remark(ss6: dict | None, sap: dict | None, tolerance: int) -> str:
    notes: list[str] = []
    if not ss6:
        notes.append(f"Tidak ditemukan di SS6 dalam toleransi H±{tolerance}")
    elif ss6["date_diff"]:
        notes.append(f"SS6 beda tanggal {ss6['date_diff']:+d} hari")
    if not sap:
        notes.append(f"Tidak ditemukan di SAP dalam toleransi H±{tolerance}")
    elif sap["date_diff"]:
        notes.append(f"SAP beda tanggal {sap['date_diff']:+d} hari")
    return " | ".join(notes) if notes else "OK"


@router.get("/validation")
def validation(
    tolerance_days: int = Query(default=1, ge=0, le=7),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    status: str | None = None,
    unit: str | None = None,
    _: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN", "GROUP_LEADER")),
) -> dict:
    vouchers = _voucher_rows()
    if date_from:
        vouchers = [v for v in vouchers if v["tanggal"] >= date_from]
    if date_to:
        vouchers = [v for v in vouchers if v["tanggal"] <= date_to]

    sources = _source_rows()
    ss6_index = _build_index(sources, "SS6")
    sap_index = _build_index(sources, "SAP")

    output: list[dict[str, Any]] = []
    for voucher in vouchers:
        ss6 = _best_match(voucher, ss6_index, tolerance_days)
        sap = _best_match(voucher, sap_index, tolerance_days)
        item = {
            **voucher,
            "ss6_match_id": ss6["id"] if ss6 else None,
            "sap_match_id": sap["id"] if sap else None,
            "ss6_tanggal": ss6["tanggal"] if ss6 else None,
            "sap_tanggal": sap["tanggal"] if sap else None,
            "ss6_liter": ss6["liter"] if ss6 else 0,
            "sap_liter": sap["liter"] if sap else 0,
            "ss6_date_diff": ss6["date_diff"] if ss6 else None,
            "sap_date_diff": sap["date_diff"] if sap else None,
            "validation_status": _status(ss6, sap),
            "validation_remark": _remark(ss6, sap, tolerance_days),
        }
        output.append(item)

    if status:
        output = [x for x in output if x["validation_status"] == status.upper()]
    if unit:
        needle = normalize_unit(unit)
        output = [x for x in output if needle in normalize_unit(x.get("unit_kode"))]

    counts: dict[str, int] = defaultdict(int)
    for row in output:
        counts[row["validation_status"]] += 1
    normal_matches = counts.get("MATCH", 0)
    return {
        "ok": True,
        "tolerance_days": tolerance_days,
        "data": output,
        "total": len(output),
        "summary": {
            "total_liter": round(sum(float(x.get("liter") or 0) for x in output), 3),
            "status_counts": dict(counts),
            "match_rate_pct": round(normal_matches / len(output) * 100, 3) if output else 0,
        },
        "matching_rule": "unit_standar + liter(2 decimal) + nearest date H±N; each source row used once",
    }
