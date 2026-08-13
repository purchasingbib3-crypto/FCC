from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Iterable

from ..config import Settings, get_settings

settings = get_settings()


def _n(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def classify(value_pct: float | None, target_pct: float) -> str:
    if value_pct is None:
        return "WAITING DATA"
    return "ON TARGET" if abs(value_pct) <= target_pct else "OUT OF TARGET"


def enrich_row(row: dict[str, Any], cfg: Settings = settings) -> dict[str, Any]:
    item = dict(row)
    daily_pct = item.get("daily_deviasi_pct")
    weekly_pct = item.get("weekly_deviasi_pct")
    mtd_pct = item.get("mtd_discre_pct")
    cover_days = item.get("fuel_availability_days")
    stock_book = _n(item.get("stock_akhir_buku_l"))
    actual = item.get("stock_aktual_l")

    opening_source = str(item.get("opening_source") or "").strip().upper()
    opening_ready = opening_source not in {"", "NO_SOURCE"}
    item["stock_status"] = (
        "WAITING DATA" if not opening_ready
        else "ON TARGET" if stock_book >= cfg.discrepancy_stock_min_l
        else "OUT OF TARGET"
    )
    item["daily_status"] = classify(None if daily_pct is None else _n(daily_pct), cfg.discrepancy_daily_target_pct)
    item["weekly_status"] = classify(None if weekly_pct is None else _n(weekly_pct), cfg.discrepancy_weekly_target_pct)
    item["mtd_status"] = classify(None if mtd_pct is None else _n(mtd_pct), cfg.discrepancy_mtd_target_pct)
    item["fuel_availability_status"] = (
        "WAITING DATA" if cover_days is None
        else "ON TARGET" if _n(cover_days) >= cfg.fuel_availability_target_days
        else "OUT OF TARGET"
    )
    fuel_out = _n(item.get("fuel_keluar_l"))
    discrepancy_l = item.get("discrepancy_l")
    required_ready = actual is not None and opening_ready
    # Zero-outflow is still a valid reconciliation state. Percentage-based metrics
    # are undefined in that case, so use the configured absolute-liter tolerance
    # instead of leaving the dashboard permanently in WAITING DATA.
    if required_ready and fuel_out == 0 and discrepancy_l is not None:
        absolute_status = (
            "ON TARGET"
            if abs(_n(discrepancy_l)) <= cfg.discrepancy_zero_outflow_tolerance_l
            else "OUT OF TARGET"
        )
        item["daily_status"] = absolute_status
        if daily_pct is None:
            item["weekly_status"] = absolute_status
            item["mtd_status"] = absolute_status
    statuses = [
        item["stock_status"],
        item["daily_status"],
        item["weekly_status"],
        item["mtd_status"],
        item["fuel_availability_status"],
    ]
    item["final_status"] = "WAITING DATA" if not required_ready else (
        "ON TARGET" if all(s == "ON TARGET" for s in statuses) else "OUT OF TARGET"
    )
    item["opening_rule"] = cfg.discrepancy_opening_source
    return item


def aggregate(rows: Iterable[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    period = (period or "SHIFT").upper()
    if period == "SHIFT":
        return [dict(r) for r in rows]

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        d = row["tanggal"]
        if isinstance(d, str):
            d = date.fromisoformat(d)
        if period == "DAILY":
            key = d.isoformat()
            label = key
        elif period == "WEEKLY":
            iso = d.isocalendar()
            key = f"{iso.year}-W{iso.week:02d}"
            label = key
        elif period == "MONTHLY":
            key = f"{d.year}-{d.month:02d}"
            label = key
        else:
            raise ValueError("period harus SHIFT, DAILY, WEEKLY, atau MONTHLY")
        copy = dict(row)
        copy["_label"] = label
        groups[key].append(copy)

    output: list[dict[str, Any]] = []
    for key in sorted(groups):
        rr = sorted(groups[key], key=lambda x: (str(x["tanggal"]), str(x.get("shift") or "")))
        opening = _n(rr[0].get("stock_awal_l"))
        receipt = sum(_n(x.get("penerimaan_l")) for x in rr)
        ba = sum(_n(x.get("ba_l")) for x in rr)
        adjustment = sum(_n(x.get("adjustment_l")) for x in rr)
        fuel_out = sum(_n(x.get("fuel_keluar_l")) for x in rr)
        actual_values = [x.get("stock_aktual_l") for x in rr if x.get("stock_aktual_l") is not None]
        actual = _n(actual_values[-1]) if actual_values else None
        book = opening + receipt + ba + adjustment - fuel_out
        discrepancy = None if actual is None else actual - book
        pct = None if discrepancy is None or fuel_out == 0 else discrepancy / fuel_out * 100
        output.append(
            {
                "period": key,
                "label": rr[0]["_label"],
                "tanggal_awal": rr[0]["tanggal"],
                "tanggal_akhir": rr[-1]["tanggal"],
                "stock_awal_l": round(opening, 3),
                "penerimaan_l": round(receipt, 3),
                "ba_l": round(ba, 3),
                "adjustment_l": round(adjustment, 3),
                "fuel_keluar_l": round(fuel_out, 3),
                "stock_akhir_buku_l": round(book, 3),
                "stock_aktual_l": None if actual is None else round(actual, 3),
                "discrepancy_l": None if discrepancy is None else round(discrepancy, 3),
                "discrepancy_pct": None if pct is None else round(pct, 6),
                "row_count": len(rr),
            }
        )
    return output


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "rows": 0,
            "stock_awal_l": 0,
            "penerimaan_l": 0,
            "fuel_keluar_l": 0,
            "stock_akhir_buku_l": 0,
            "stock_aktual_l": None,
            "discrepancy_l": None,
            "discrepancy_pct": None,
            "on_target": 0,
            "out_of_target": 0,
            "waiting": 0,
        }
    first = rows[0]
    last = rows[-1]
    total_out = sum(_n(r.get("fuel_keluar_l")) for r in rows)
    total_discrepancy = sum(_n(r.get("discrepancy_l")) for r in rows if r.get("discrepancy_l") is not None)
    return {
        "rows": len(rows),
        "stock_awal_l": _n(first.get("stock_awal_l")),
        "penerimaan_l": sum(_n(r.get("penerimaan_l")) for r in rows),
        "fuel_keluar_l": total_out,
        "stock_akhir_buku_l": _n(last.get("stock_akhir_buku_l")),
        "stock_aktual_l": last.get("stock_aktual_l"),
        "discrepancy_l": round(total_discrepancy, 3),
        "discrepancy_pct": None if total_out == 0 else round(total_discrepancy / total_out * 100, 6),
        "on_target": sum(1 for r in rows if r.get("final_status") == "ON TARGET"),
        "out_of_target": sum(1 for r in rows if r.get("final_status") == "OUT OF TARGET"),
        "waiting": sum(1 for r in rows if r.get("final_status") == "WAITING DATA"),
    }
