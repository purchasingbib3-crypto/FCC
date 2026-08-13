from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql

from ..db import connection, fetch_all, fetch_one, qualified
from ..dependencies import current_user
from ..models import EvidenceUpload
from ..security import SessionUser
from ..services.storage import decode_data_url, read_as_data_url, save_evidence

router = APIRouter(prefix="/api/evidence", tags=["evidence"])


@lru_cache(maxsize=1)
def _photo_storage_mode() -> str:
    """Return the live fcc.photo contract without guessing its generation.

    Existing FCC deployments use a bigint/base64 table. A fresh deployment may
    use the optional UUID/filesystem metadata table. The API deliberately
    supports both so Hermes does not replace, rename, or double-write photo
    storage merely because the column names differ.
    """
    rows = fetch_all(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='fcc' AND table_name='photo'
        """
    )
    columns = {str(row["column_name"]) for row in rows}
    if {"base64_data", "size_bytes", "uploaded_at"}.issubset(columns):
        return "base64"
    if {"storage_path", "file_size_bytes", "created_at"}.issubset(columns):
        return "filesystem"
    return "missing"


def _missing_table_error() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail=(
            "Kontrak fcc.photo tidak dikenali. Jangan menebak tabel baru. "
            "Jalankan database/002_optional_photo.sql hanya bila tabel benar-benar belum ada, "
            "lalu ulangi preflight."
        ),
    )


@router.post("/upload")
def upload(payload: EvidenceUpload, user: SessionUser = Depends(current_user)) -> dict:
    mode = _photo_storage_mode()
    if mode == "missing":
        raise _missing_table_error()

    if mode == "base64":
        try:
            mime_type, raw = decode_data_url(payload.base64)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {} (modul,record_id,photo_type,base64_data,size_bytes,mime_type,uploaded_by,uploaded_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,now()) RETURNING id"
                    ).format(qualified("photo")),
                    (
                        payload.modul,
                        payload.record_id,
                        payload.photo_type,
                        payload.base64,
                        len(raw),
                        mime_type,
                        user.username,
                    ),
                )
                row = cur.fetchone()
        return {"ok": True, "id": str(row["id"]), "photo_type": payload.photo_type, "size": len(raw)}

    try:
        stored = save_evidence(payload.modul, payload.record_id, payload.photo_type, payload.base64)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {} (site_code,modul,record_id,photo_type,storage_path,mime_type,file_size_bytes,uploaded_by,created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()) RETURNING id"
                    ).format(qualified("photo")),
                    (
                        "PPA-BIB",
                        payload.modul,
                        payload.record_id,
                        payload.photo_type,
                        stored.relative_path,
                        stored.mime_type,
                        stored.size,
                        user.username,
                    ),
                )
                row = cur.fetchone()
            except Exception as exc:
                try:
                    stored.path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise _missing_table_error() from exc
    return {"ok": True, "id": str(row["id"]), "photo_type": payload.photo_type, "size": stored.size}


@router.get("/list")
def list_evidence(
    modul: str = Query(..., min_length=1, max_length=80),
    record_id: str = Query(..., min_length=1, max_length=180),
    _: SessionUser = Depends(current_user),
) -> list[dict]:
    mode = _photo_storage_mode()
    if mode == "missing":
        return []
    if mode == "base64":
        query = sql.SQL(
            "SELECT id,modul,record_id,photo_type,mime_type,size_bytes AS file_size_bytes,"
            "uploaded_by,uploaded_at AS created_at FROM {} "
            "WHERE modul=%s AND record_id=%s ORDER BY uploaded_at,id"
        ).format(qualified("photo"))
    else:
        query = sql.SQL(
            "SELECT id,modul,record_id,photo_type,mime_type,file_size_bytes,uploaded_by,created_at "
            "FROM {} WHERE modul=%s AND record_id=%s ORDER BY created_at,id"
        ).format(qualified("photo"))
    rows = fetch_all(query, (modul, record_id))
    for row in rows:
        row["id"] = str(row["id"])
    return rows


@router.get("/{photo_id}")
def get_evidence(photo_id: str, _: SessionUser = Depends(current_user)) -> dict:
    mode = _photo_storage_mode()
    if mode == "missing":
        raise HTTPException(status_code=404, detail="Foto tidak ditemukan")
    if mode == "base64":
        row = fetch_one(
            sql.SQL("SELECT base64_data,mime_type FROM {} WHERE id=%s").format(qualified("photo")),
            (photo_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Foto tidak ditemukan")
        return {"ok": True, "data_url": row["base64_data"]}

    row = fetch_one(
        sql.SQL("SELECT storage_path,mime_type FROM {} WHERE id=%s").format(qualified("photo")),
        (photo_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Foto tidak ditemukan")
    try:
        data_url = read_as_data_url(row["storage_path"], row.get("mime_type"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="File foto tidak ditemukan di storage") from exc
    return {"ok": True, "data_url": data_url}


# ============================================================================
# Public evidence endpoints (no auth) — untuk History view display
# ============================================================================

@router.get("/public/list")
def list_evidence_public(
    modul: str = Query(..., min_length=1, max_length=80),
    record_id: str = Query(..., min_length=1, max_length=180),
) -> dict:
    """Public list evidence per record (no auth). Return {data: [...], count: int}."""
    mode = _photo_storage_mode()
    if mode == "missing":
        return {"data": [], "count": 0}
    if mode == "base64":
        query = sql.SQL(
            "SELECT id,modul,record_id,photo_type,mime_type,size_bytes AS file_size_bytes,"
            "uploaded_by,uploaded_at AS created_at FROM {} "
            "WHERE modul=%s AND record_id=%s ORDER BY uploaded_at,id"
        ).format(qualified("photo"))
    else:
        query = sql.SQL(
            "SELECT id,modul,record_id,photo_type,mime_type,file_size_bytes,uploaded_by,created_at "
            "FROM {} WHERE modul=%s AND record_id=%s ORDER BY created_at,id"
        ).format(qualified("photo"))
    rows = fetch_all(query, (modul, record_id))
    for row in rows:
        row["id"] = str(row["id"])
    return {"data": [dict(r) for r in rows], "count": len(rows)}


@router.get("/public/{photo_id}")
def get_evidence_public(photo_id: str) -> dict:
    """Public get evidence image (no auth). Return data URL for inline display."""
    mode = _photo_storage_mode()
    if mode == "missing":
        raise HTTPException(status_code=404, detail="Foto tidak ditemukan")
    if mode == "base64":
        row = fetch_one(
            sql.SQL("SELECT base64_data,mime_type,photo_type,modul,record_id FROM {} WHERE id=%s").format(qualified("photo")),
            (photo_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Foto tidak ditemukan")
        return {"ok": True, "data_url": row["base64_data"], "mime_type": row.get("mime_type"), "photo_type": row.get("photo_type")}

    row = fetch_one(
        sql.SQL("SELECT storage_path,mime_type,photo_type,modul,record_id FROM {} WHERE id=%s").format(qualified("photo")),
        (photo_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Foto tidak ditemukan")
    try:
        data_url = read_as_data_url(row["storage_path"], row.get("mime_type"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="File foto tidak ditemukan di storage") from exc
    return {"ok": True, "data_url": data_url, "mime_type": row.get("mime_type"), "photo_type": row.get("photo_type")}
