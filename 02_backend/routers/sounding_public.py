"""Public read-only endpoint untuk sounding lookup MAINTANK.

Endpoint ini BYPASS authentication agar field-app bisa lookup volume
MAINTANK tanpa harus login dulu. Endpoint ini HANYA baca dari tabel
fcc.sounding_table (Postgres lokal), tidak expose data sensitif.

Kenapa perlu: field-app pakai frontend Supabase-style yang query
tabel `sounding_table` (yang tidak ada di Supabase), sehingga untuk
MAINTANK lookup gagal. Endpoint ini jadi fallback yang reliable.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from psycopg import sql

from ..db import fetch_one, qualified

router = APIRouter(prefix="/api/v1/sounding", tags=["sounding"])


def _volume_from_table(aset: str, dip: float) -> dict | None:
    """Mirror _tera_volume logic dari fuel_bridge, tanpa auth.

    Mendukung 2 format kode FT:
      - "FT2632" (tanpa strip, dari Supabase fuel_master_fuel_truck)
      - "FT-2632" (dengan strip, dari fcc.sounding_table Postgres lokal)
    Untuk MAINTANK (TA11/FS10): format konsisten antara Supabase & Postgres.
    """
    # Normalisasi: coba format asli dulu, lalu fallback ke versi dengan/tanpa strip
    aset_candidates = [aset]
    if aset.startswith("FT") and "-" not in aset:
        aset_candidates.append("FT-" + aset[2:])  # FT2632 → FT-2632
    elif aset.startswith("FT-"):
        aset_candidates.append(aset.replace("-", ""))  # FT-2632 → FT2632

    lower = upper = None
    aset_used = None
    for aset_try in aset_candidates:
        lower = fetch_one(
            sql.SQL("SELECT dip_cm,volume_l FROM {} WHERE aset=%s AND status='ACTIVE' AND dip_cm<=%s ORDER BY dip_cm DESC LIMIT 1").format(qualified("sounding_table")),
            (aset_try, dip),
        )
        upper = fetch_one(
            sql.SQL("SELECT dip_cm,volume_l FROM {} WHERE aset=%s AND status='ACTIVE' AND dip_cm>=%s ORDER BY dip_cm ASC LIMIT 1").format(qualified("sounding_table")),
            (aset_try, dip),
        )
        if lower and upper:
            aset_used = aset_try
            break
    if not lower or not upper or aset_used is None:
        return None
    dip_lo, vol_lo = float(lower["dip_cm"]), float(lower["volume_l"])
    dip_hi, vol_hi = float(upper["dip_cm"]), float(upper["volume_l"])
    if dip_hi == dip_lo:
        vol = vol_lo
        interpolated = False
    else:
        ratio = (dip - dip_lo) / (dip_hi - dip_lo)
        vol = vol_lo + ratio * (vol_hi - vol_lo)
        interpolated = dip_lo != dip
    return {
        "aset": aset,                # echo kode asli yang diminta user
        "aset_used": aset_used,      # kode yang berhasil ditemukan di DB
        "dip_cm": dip,
        "volume_l": round(vol, 3),
        "interpolated": interpolated,
        "source": "sounding_table",
        "dip_lo": dip_lo,
        "dip_hi": dip_hi,
    }


@router.get("/volume")
def sounding_volume_public(
    aset: str = Query(..., description="Kode aset, mis. TA11 atau FT2632"),
    dip: float = Query(..., description="Tinggi sounding (cm), boleh desimal"),
):
    """Public read-only endpoint untuk lookup dip → volume_l.

    Bypass authentication agar form input bisa lookup MAINTANK
    tanpa harus login user Supabase. Hanya baca data sounding.
    """
    if not aset.strip():
        raise HTTPException(status_code=400, detail="aset wajib diisi")
    try:
        dip_f = float(dip)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="dip harus angka")
    if dip_f < 0:
        raise HTTPException(status_code=400, detail="dip harus >= 0")

    result = _volume_from_table(aset.strip(), dip_f)
    if result is None:
        return {
            "aset": aset,
            "dip_cm": dip_f,
            "volume_l": None,
            "found": False,
            "source": "sounding_table",
            "message": f"Tidak ada data sounding untuk {aset}",
        }
    result["found"] = True
    return result


@router.get("/curve")
def sounding_curve_public(
    aset: str = Query(..., description="Kode aset"),
    step: float = Query(1.0, description="Sampling step dalam cm", ge=0.1, le=10.0),
):
    """Return sampled curve untuk client-side caching.

    Sampling step 1.0 cm default. Untuk MAINTANK ~771 points, FT ~190 points.
    Frontend cache pakai ini supaya tidak query per-dip.
    """
    if not aset.strip():
        raise HTTPException(status_code=400, detail="aset wajib diisi")

    # Get range. Normalisasi: coba format asli, fallback ke versi strip/no-strip.
    aset_candidates = [aset.strip()]
    if aset.strip().startswith("FT") and "-" not in aset.strip():
        aset_candidates.append("FT-" + aset.strip()[2:])
    elif aset.strip().startswith("FT-"):
        aset_candidates.append(aset.strip().replace("-", ""))

    row = None
    for aset_try in aset_candidates:
        row = fetch_one(
            sql.SQL("SELECT MIN(dip_cm) AS dip_min,MAX(dip_cm) AS dip_max,MAX(volume_l) AS vol_max,COUNT(*) AS cnt FROM {} WHERE aset=%s AND status='ACTIVE'").format(qualified("sounding_table")),
            (aset_try,),
        )
        if row and row.get("cnt"):
            break
    if not row or not row.get("cnt"):
        raise HTTPException(status_code=404, detail=f"Tidak ada data sounding untuk {aset}")

    dip_min = float(row["dip_min"])
    dip_max = float(row["dip_max"])
    vol_max = float(row["vol_max"])
    count = int(row["cnt"])

    # Sample
    points = []
    d = dip_min
    while d <= dip_max + 1e-9:
        r = _volume_from_table(aset.strip(), d)
        if r:
            points.append({"dip": round(d, 2), "volume": r["volume_l"]})
        d += step

    return {
        "aset": aset,
        "dip_min": dip_min,
        "dip_max": dip_max,
        "volume_max_l": vol_max,
        "point_count": count,
        "sampled_count": len(points),
        "step": step,
        "points": points,
    }
