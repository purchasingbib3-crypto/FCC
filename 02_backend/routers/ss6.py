from __future__ import annotations

import hashlib
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from psycopg import sql

from ..config import get_settings
from ..db import connection, fetch_all, fetch_one, qualified
from ..dependencies import current_user, require_roles
from ..models import SS6FetchRequest, SS6SaveRequest
from ..security import SessionUser
from ..services.ss6_client import fetch_export, store
from ..services.xlsx_import import normalize_unit

router = APIRouter(prefix="/api/v1/ss6", tags=["ss6-temporary"])
settings = get_settings()


def _master_maps() -> tuple[dict[str, dict], dict[str, str]]:
    units = fetch_all(
        sql.SQL("SELECT kode,nama,vendor_kode,kategori,status FROM {} WHERE status='ACTIVE'").format(
            qualified("master_unit")
        )
    )
    aliases = fetch_all(
        sql.SQL("SELECT unit_standar,alias_ss6,alias_sap FROM {} WHERE status='ACTIVE'").format(
            qualified("unit_alias")
        )
    )
    by_unit = {normalize_unit(u["kode"]): u for u in units}
    alias_map: dict[str, str] = {}
    for row in aliases:
        standard = normalize_unit(row["unit_standar"])
        for value in (row.get("unit_standar"), row.get("alias_ss6"), row.get("alias_sap")):
            key = normalize_unit(value)
            if key:
                alias_map[key] = standard
    for key in by_unit:
        alias_map.setdefault(key, key)
    return by_unit, alias_map


def _fuel_truck_map() -> dict[str, str]:
    rows = fetch_all(
        sql.SQL("SELECT kode,nama FROM {} WHERE status='ACTIVE'").format(qualified("master_fuel_truck"))
    )
    mapping: dict[str, str] = {}
    for row in rows:
        code = str(row["kode"])
        mapping[normalize_unit(code)] = code
        mapping[normalize_unit(row.get("nama"))] = code
    return mapping


def _enrich(rows: list[dict]) -> list[dict]:
    masters, aliases = _master_maps()
    trucks = _fuel_truck_map()
    output: list[dict] = []
    for row in rows:
        standard = aliases.get(row["unit_normalized"])
        master = masters.get(standard or "")
        truck = trucks.get(normalize_unit(row.get("gas_station")))
        fingerprint = hashlib.sha256(
            f"{row['date']}|{row['shift']}|{row['unit_normalized']}|{row['volume_l']:.3f}|{row.get('time','')}".encode()
        ).hexdigest()[:32]
        item = {
            **row,
            "row_id": row.get("transaction_id") or fingerprint,
            "unit_standar": master["kode"] if master else None,
            "vendor_kode": master.get("vendor_kode") if master else None,
            "kategori": master.get("kategori") if master else None,
            "fuel_truck": truck,
            "mapping_status": "READY" if master and truck else (
                "PERLU MAPPING UNIT & FT" if not master and not truck else "PERLU MAPPING UNIT" if not master else "PERLU MAPPING FT"
            ),
        }
        output.append(item)
    return output


def _sla(rows: list[dict]) -> dict:
    masters, _ = _master_maps()
    active_population = list(masters.values())
    groups = {
        "A2B_TRACK": {"A2B", "TRACK", "EXCAVATOR"},
        "A2B_WHEEL": {"TRUCK", "HEAVY DUTY", "DUMP TRUCK", "A2B WHEEL"},
        "SUPPORT_NON_A2B": {"SARANA", "NON A2B", "SUPPORT"},
        "A2S": {"A2S", "DOZER", "GRADER"},
    }
    result: dict[str, dict] = {}
    for group, terms in groups.items():
        population = [u for u in active_population if any(t in str(u.get("kategori") or "").upper() for t in terms)]
        pop_codes = {u["kode"] for u in population}
        tx = [r for r in rows if r.get("unit_standar") in pop_codes]
        refueled = {r["unit_standar"] for r in tx if r.get("unit_standar")}
        achievement = (len(refueled) / len(pop_codes) * 100) if pop_codes else 0
        result[group] = {
            "population": len(pop_codes),
            "refueled_units": len(refueled),
            "not_refueled_units": max(0, len(pop_codes) - len(refueled)),
            "frequency": len(tx),
            "volume_l": round(sum(float(r["volume_l"]) for r in tx), 3),
            "achievement_pct": round(achievement, 3),
        }
    return result


@router.post("/fetch-temp")
async def fetch_temp(
    payload: SS6FetchRequest,
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN", "GROUP_LEADER")),
) -> dict:
    if payload.date_to < payload.date_from:
        raise HTTPException(status_code=422, detail="Tanggal akhir tidak boleh sebelum tanggal awal")
    try:
        rows, meta = await fetch_export(payload.date_from.isoformat(), payload.date_to.isoformat(), payload.shift)
        enriched = _enrich(rows)
        token = await store.put(enriched, {**meta, "requested_by": user.username})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "token": token,
        "expires_in_seconds": settings.ss6_temp_ttl_seconds,
        "meta": meta,
        "sla": _sla(enriched),
        "rows": enriched[:2000],
        "temporary": True,
        "database_write": False,
    }


@router.get("/temp/{token}")
async def get_temp(token: str, _: SessionUser = Depends(current_user)) -> dict:
    payload = await store.get(token)
    if not payload:
        raise HTTPException(status_code=404, detail="Temporary SS6 tidak ditemukan atau sudah kedaluwarsa")
    return {
        "ok": True,
        "meta": payload.meta,
        "sla": _sla(payload.rows),
        "rows": payload.rows,
        "temporary": True,
        "database_write": False,
    }


@router.delete("/temp/{token}")
async def clear_temp(token: str, _: SessionUser = Depends(current_user)) -> dict:
    await store.delete(token)
    return {"ok": True}


@router.post("/save-selected")
async def save_selected(
    payload: SS6SaveRequest,
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
) -> dict:
    temp = await store.get(payload.token)
    if not temp:
        raise HTTPException(status_code=404, detail="Temporary SS6 tidak ditemukan atau sudah kedaluwarsa")
    selected = [r for r in temp.rows if r["row_id"] in set(payload.row_ids)]
    if not selected:
        raise HTTPException(status_code=422, detail="Tidak ada row terpilih")
    not_ready = [r for r in selected if r["mapping_status"] != "READY"]
    if not_ready:
        raise HTTPException(
            status_code=422,
            detail={"message": "Masih ada row PERLU MAPPING", "rows": not_ready[:50]},
        )

    inserted = 0
    skipped = 0
    with connection() as conn:
        with conn.cursor() as cur:
            for row in selected:
                voucher = row.get("transaction_id") or f"SS6-{row['row_id']}"
                cur.execute(
                    sql.SQL("SELECT 1 FROM {} WHERE no_voucher=%s").format(qualified("refuelling")),
                    (voucher,),
                )
                if cur.fetchone():
                    skipped += 1
                    continue
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {} (no_voucher,tanggal,shift,vendor_kode,unit_kode,fuel_truck,volume_l,petugas,status,created_at,updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'VALID',now(),now())"
                    ).format(qualified("refuelling")),
                    (
                        voucher,
                        row["date"],
                        row["shift"],
                        row["vendor_kode"],
                        row["unit_standar"],
                        row["fuel_truck"],
                        row["volume_l"],
                        row.get("fuelman") or row.get("input_by") or user.nama,
                    ),
                )
                inserted += 1
    return {"ok": True, "selected": len(selected), "inserted": inserted, "skipped_duplicate": skipped}
