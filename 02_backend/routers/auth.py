from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from psycopg import sql

from ..config import get_settings
from ..db import connection, fetch_one, qualified
from ..dependencies import COOKIE_NAME, current_user
from ..models import LoginRequest, PasswordChangeRequest, RegisterRequest
from ..security import SessionUser, hash_password, issue_session, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in {"1", "TRUE", "T", "YES", "YA", "Y", "ON"}


def _next_id_if_required(conn) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_default,is_identity
            FROM information_schema.columns
            WHERE table_schema=%s AND table_name='app_user' AND column_name='id'
            """,
            (settings.database_schema,),
        )
        meta = cur.fetchone()
        if meta and (meta.get("column_default") or str(meta.get("is_identity") or "").upper() == "YES"):
            return None
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('fcc.app_user'))")
        cur.execute(sql.SQL("SELECT COALESCE(max(id),0)+1 AS id FROM {}").format(qualified("app_user")))
        return int(cur.fetchone()["id"])


def _user_from_row(row: dict) -> SessionUser:
    return SessionUser(
        id=str(row["id"]),
        username=str(row["username"]),
        nama=str(row.get("nama") or row["username"]),
        role=str(row["role"]),
        vendor_kode=row.get("vendor_kode"),
        must_change_pw=_as_bool(row.get("must_change_pw")),
    )


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )


@router.post("/login")
def login(payload: LoginRequest, response: Response) -> dict:
    row = fetch_one(
        sql.SQL(
            "SELECT id, username, nama, role, vendor_kode, status, password_hash, must_change_pw, "
            "COALESCE(failed_logins,0) AS failed_logins, locked_until "
            "FROM {} WHERE lower(username)=lower(%s) LIMIT 1"
        ).format(qualified("app_user")),
        (payload.username.strip(),),
    )
    # Keep the public error generic enough not to expose account lifecycle details.
    if not row or str(row.get("status", "")).upper() != "ACTIVE":
        raise HTTPException(status_code=401, detail="Username/password tidak valid atau akun tidak aktif")

    locked_until = row.get("locked_until")
    if locked_until is not None:
        if getattr(locked_until, "tzinfo", None) is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > datetime.now(timezone.utc):
            raise HTTPException(
                status_code=429,
                detail=f"Terlalu banyak percobaan login. Coba kembali setelah {locked_until.astimezone().strftime('%H:%M')}",
            )

    if not verify_password(payload.password, str(row.get("password_hash") or "")):
        # Expired lock windows start a fresh counter; active windows were rejected above.
        current_failed = int(row.get("failed_logins") or 0)
        if row.get("locked_until") is not None:
            current_failed = 0
        failures = current_failed + 1
        should_lock = failures >= settings.login_max_attempts
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "UPDATE {} SET failed_logins=%s, "
                        "locked_until=CASE WHEN %s THEN now() + (%s * interval '1 minute') ELSE NULL END, "
                        "updated_at=now() WHERE id=%s"
                    ).format(qualified("app_user")),
                    (failures, should_lock, settings.login_lock_minutes, row["id"]),
                )
        if should_lock:
            raise HTTPException(
                status_code=429,
                detail=f"Terlalu banyak percobaan login. Akun dikunci {settings.login_lock_minutes} menit.",
            )
        remaining = max(0, settings.login_max_attempts - failures)
        raise HTTPException(status_code=401, detail=f"Username/password tidak valid. Sisa percobaan: {remaining}")

    user = _user_from_row(row)
    _set_cookie(response, issue_session(user))
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "UPDATE {} SET last_login=now(), failed_logins=0, locked_until=NULL, updated_at=now() WHERE id=%s"
                ).format(qualified("app_user")),
                (row["id"],),
            )
    return {"ok": True, "user": user.to_dict()}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: SessionUser = Depends(current_user)) -> dict:
    return {"ok": True, "user": user.to_dict()}


@router.post("/change_password")
def change_password(
    payload: PasswordChangeRequest,
    response: Response,
    user: SessionUser = Depends(current_user),
) -> dict:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("UPDATE {} SET password_hash=%s, must_change_pw=false, updated_at=now() WHERE id=%s").format(
                    qualified("app_user")
                ),
                (hash_password(payload.new_password), user.id),
            )
    refreshed = SessionUser(**{**user.to_dict(), "must_change_pw": False})
    _set_cookie(response, issue_session(refreshed))
    return {"ok": True, "user": refreshed.to_dict()}


@router.post("/admin/reset_password/{username}")
def admin_reset_password(
    username: str,
    payload: PasswordChangeRequest,
    user: SessionUser = Depends(current_user),
) -> dict:
    """SUPER_ADMIN can reset password for any user (by username)."""
    if str(user.role).upper() != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Hanya SUPER_ADMIN yang boleh reset password user lain.")
    if not payload.new_password or len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password minimal 8 karakter.")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("UPDATE {} SET password_hash=%s, must_change_pw=true, failed_logins=0, locked_until=NULL, updated_at=now() WHERE lower(username)=lower(%s) RETURNING id, username").format(
                    qualified("app_user")
                ),
                (hash_password(payload.new_password), username),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"User '{username}' tidak ditemukan.")
    return {"ok": True, "id": row["id"], "username": row["username"]}


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest) -> dict:
    """Optional public registration for non-privileged accounts only.

    SUPER_ADMIN bootstrap is deliberately *not* exposed over HTTP.  On a fresh
    database use ``python 02_backend/bootstrap_admin.py`` once.  This prevents the
    first unauthenticated request from claiming the installation.
    """
    if not settings.allow_public_register:
        raise HTTPException(
            status_code=403,
            detail="Registrasi publik dinonaktifkan. Gunakan bootstrap_admin.py untuk instalasi awal atau menu Admin untuk user berikutnya.",
        )
    role = settings.default_register_role.upper()
    if role in {"SUPER_ADMIN", "ADMIN"}:
        raise HTTPException(status_code=500, detail="FCC_DEFAULT_REGISTER_ROLE tidak boleh ADMIN/SUPER_ADMIN")
    status_value = "INACTIVE"
    with connection() as conn:
        with conn.cursor() as cur:
            try:
                next_id = _next_id_if_required(conn)
                values = (
                    payload.username.strip(), payload.full_name.strip(), role, status_value,
                    hash_password(payload.password), True,
                )
                if next_id is None:
                    cur.execute(
                        sql.SQL(
                            "INSERT INTO {} (username,nama,role,status,password_hash,must_change_pw,failed_logins,created_at,updated_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,0,now(),now()) "
                            "RETURNING id,username,nama,role,vendor_kode,status,must_change_pw"
                        ).format(qualified("app_user")),
                        values,
                    )
                else:
                    cur.execute(
                        sql.SQL(
                            "INSERT INTO {} (id,username,nama,role,status,password_hash,must_change_pw,failed_logins,created_at,updated_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,0,now(),now()) "
                            "RETURNING id,username,nama,role,vendor_kode,status,must_change_pw"
                        ).format(qualified("app_user")),
                        (next_id, *values),
                    )
                row = cur.fetchone()
            except Exception as exc:
                if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                    raise HTTPException(status_code=409, detail="Username sudah ada") from exc
                raise
    return {"ok": True, "user": _user_from_row(row).to_dict(), "status": status_value}

