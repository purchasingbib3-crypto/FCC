from __future__ import annotations

from psycopg import sql

from .config import get_settings
from .db import qualified
from .identity import field_actor_id

settings = get_settings()


def ensure_profile(conn, app_user: dict) -> str:
    """Provision/mirror the field profile from canonical fcc.app_user.

    app_user is the only source of truth for role/status. Existing fuel_profiles
    rows are preserved by NRP and linked through app_user_id; new rows use the
    deterministic field UUID. Returns the actual profile UUID.
    """
    username = str(app_user.get("username") or "").strip()
    if not username:
        raise ValueError("app_user.username kosong")
    role = str(app_user.get("role") or "FIELD").upper()
    status = str(app_user.get("status") or "ACTIVE").upper()
    profile_status = "ACTIVE" if status == "ACTIVE" else "INACTIVE"
    fallback_id = field_actor_id(username)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT id FROM {} WHERE app_user_id=%s OR lower(COALESCE(nrp,''))=lower(%s) OR lower(COALESCE(login_nrp,''))=lower(%s) ORDER BY CASE WHEN app_user_id=%s THEN 0 ELSE 1 END LIMIT 1").format(
                qualified("fuel_profiles")
            ),
            (app_user["id"], username, username, app_user["id"]),
        )
        existing = cur.fetchone()
        if existing:
            profile_id = str(existing["id"])
            cur.execute(
                sql.SQL("UPDATE {} SET app_user_id=%s,site_code=%s,nrp=%s,login_nrp=%s,full_name=%s,role=%s,status=%s,updated_at=now() WHERE id=%s").format(
                    qualified("fuel_profiles")
                ),
                (app_user["id"], settings.site_code, username, username, app_user.get("nama") or username, role, profile_status, profile_id),
            )
            return profile_id
        cur.execute(
            sql.SQL("INSERT INTO {} (id,app_user_id,site_code,nrp,login_nrp,full_name,role,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),now()) RETURNING id").format(
                qualified("fuel_profiles")
            ),
            (fallback_id, app_user["id"], settings.site_code, username, username, app_user.get("nama") or username, role, profile_status),
        )
        return str(cur.fetchone()["id"])
