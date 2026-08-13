from __future__ import annotations

from collections.abc import Callable

from fastapi import Cookie, Depends, HTTPException, status
from psycopg import sql

from .db import connection, fetch_one, qualified
from .profile_sync import ensure_profile
from .permissions import has_capability
from .security import SessionUser, read_session

COOKIE_NAME = "fcc_session"


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in {"1", "TRUE", "T", "YES", "YA", "Y", "ON"}


def current_user(fcc_session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> SessionUser:
    """Validate the signed cookie, then refresh role/status from canonical app_user.

    The cookie proves identity only. Authorization is always evaluated against the
    live database row so role changes/deactivation take effect immediately.
    """
    if not fcc_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login diperlukan")
    try:
        signed = read_session(fcc_session)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    row = fetch_one(
        sql.SQL(
            "SELECT id,username,nama,role,vendor_kode,status,must_change_pw "
            "FROM {} WHERE id=%s AND lower(username)=lower(%s) LIMIT 1"
        ).format(qualified("app_user")),
        (signed.id, signed.username),
    )
    if not row or str(row.get("status") or "").upper() != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Akun tidak aktif atau sudah dicabut")

    profile = fetch_one(
        sql.SQL(
            "SELECT id::text AS id FROM {} WHERE app_user_id=%s OR lower(COALESCE(nrp,''))=lower(%s) "
            "ORDER BY CASE WHEN app_user_id=%s THEN 0 ELSE 1 END LIMIT 1"
        ).format(qualified("fuel_profiles")),
        (row["id"], row["username"], row["id"]),
    )
    field_id = str(profile["id"]) if profile else None
    if not field_id:
        with connection() as conn:
            field_id = ensure_profile(conn, row)

    return SessionUser(
        id=str(row["id"]),
        username=str(row["username"]),
        nama=str(row.get("nama") or row["username"]),
        role=str(row["role"]),
        vendor_kode=row.get("vendor_kode"),
        must_change_pw=_as_bool(row.get("must_change_pw")),
        field_id=field_id,
    )


def require_roles(*roles: str) -> Callable[..., SessionUser]:
    allowed = {r.upper() for r in roles}

    def dependency(user: SessionUser = Depends(current_user)) -> SessionUser:
        if user.role.upper() not in allowed:
            raise HTTPException(status_code=403, detail=f"Akses ditolak. Role yang diizinkan: {', '.join(sorted(allowed))}")
        return user

    return dependency


def require_capability(capability: str) -> Callable[..., SessionUser]:
    """Authorize against the canonical role -> capability matrix."""
    required = str(capability or "").strip()

    def dependency(user: SessionUser = Depends(current_user)) -> SessionUser:
        if not has_capability(user.role, required):
            raise HTTPException(status_code=403, detail=f"Akses ditolak. Capability diperlukan: {required}")
        return user

    return dependency
