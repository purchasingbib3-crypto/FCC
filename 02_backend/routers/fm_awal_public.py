"""Public read-only endpoint untuk FM Awal default lookup.

Endpoint ini BYPASS authentication agar field-app bisa ambil FM Awal
default tanpa harus login dulu. Hanya baca dari tabel fcc.fuel_fm_awal_settings,
fcc.fuel_route_master, dan fcc.transfer_fuel.

Logika prioritas (mirror fuel_bridge._tera_volume):
  1. Manual override aktif (fuel_fm_awal_settings.mode = 'MANUAL')
  2. FM akhir dari transfer_fuel terakhir di jalur yang sama
  3. Default 0

Kenapa perlu: field-app butuh auto-fill FM Awal saat user pilih Jalur
di form Transfer, dan field-app sering dipakai saat user belum login
(session timeout / cold start / static page view).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..db import fetch_one

router = APIRouter(prefix="/api/v1/fm-awal", tags=["fm-awal"])


def _resolve_default_fm_awal(jalur_id: str) -> dict:
    """Mirror fuel_bridge logic tanpa auth.

    Returns dict dengan key: fm_value, source, last_transfer_id, message
    """
    # 1) Manual override
    setting = fetch_one(
        "SELECT mode, fm_awal_manual FROM fcc.fuel_fm_awal_settings WHERE jalur_id=%s LIMIT 1",
        (jalur_id,),
    )
    if setting and setting.get("mode") == "MANUAL" and setting.get("fm_awal_manual") is not None:
        return {
            "fm_value": float(setting["fm_awal_manual"]),
            "source": "MANUAL",
            "last_transfer_id": None,
            "message": "FM Awal dikunci manual oleh Admin.",
        }

    # 2) FM akhir dari transfer_fuel terakhir di jalur yang sama
    #    Jalur_id di transfer_fuel berupa text (legacy) atau uuid (current).
    #    Kita coba dua-duanya: text match via fuel_master_jalur.jalur_code,
    #    ATAU exact uuid match (kalau transfer_fuel pakai uuid).
    jalur = fetch_one(
        "SELECT jalur_code FROM fcc.fuel_master_jalur WHERE id=%s",
        (jalur_id,),
    )
    last_fm = None
    last_transfer_id = None

    if jalur:
        code = jalur.get("jalur_code")
        # Coba match by jalur_code (legacy fcc.transfer_fuel.jalur)
        aliases = [code]
        import re as _re
        mm = _re.match(r"^JALUR (\d+)$", code or "")
        if mm:
            aliases.append("JLR-" + mm.group(1))

        # Cek fcc.transfer_fuel legacy dulu
        row = fetch_one(
            "SELECT fm_akhir, id FROM fcc.transfer_fuel WHERE jalur = ANY(%s) ORDER BY tanggal DESC, created_at DESC LIMIT 1",
            (aliases,),
        )
        if row and row.get("fm_akhir") is not None:
            last_fm = float(row["fm_akhir"])
            last_transfer_id = row.get("id")

    # Kalau belum ketemu, coba fcc.fuel_tx_transfer_fuel (current transaction)
    if last_fm is None:
        row = fetch_one(
            "SELECT fm_akhir, id FROM fcc.fuel_tx_transfer_fuel WHERE jalur_id=%s AND fm_akhir IS NOT NULL ORDER BY tanggal DESC, updated_at DESC LIMIT 1",
            (jalur_id,),
        )
        if row and row.get("fm_akhir") is not None:
            last_fm = float(row["fm_akhir"])
            last_transfer_id = row.get("id")

    if last_fm is not None:
        return {
            "fm_value": last_fm,
            "source": "AUTO_TRANSFER_LAST",
            "last_transfer_id": str(last_transfer_id) if last_transfer_id else None,
            "message": "FM Awal otomatis dari transaksi terakhir.",
        }

    # 3) Default
    return {
        "fm_value": 0,
        "source": "AUTO_DEFAULT",
        "last_transfer_id": None,
        "message": "Belum ada transaksi sebelumnya. Default 0.",
    }


@router.get("/default")
def fm_awal_default_public(
    jalur_id: str = Query(..., description="UUID jalur (dari fuel_master_jalur)"),
):
    """Public FM Awal default lookup — bypass auth."""
    if not jalur_id.strip():
        raise HTTPException(status_code=400, detail="jalur_id wajib diisi")

    result = _resolve_default_fm_awal(jalur_id.strip())
    return result
