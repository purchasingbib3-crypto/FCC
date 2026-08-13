from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from psycopg import sql

try:
    from .db import connection, open_pool, close_pool, qualified
    from .profile_sync import ensure_profile
    from .security import hash_password
except ImportError:  # direct script execution
    import importlib
    bundle_root = Path(__file__).resolve().parent.parent
    if str(bundle_root) not in sys.path:
        sys.path.insert(0, str(bundle_root))
    db = importlib.import_module("02_backend.db")
    profile_sync = importlib.import_module("02_backend.profile_sync")
    security = importlib.import_module("02_backend.security")
    connection, open_pool, close_pool, qualified = db.connection, db.open_pool, db.close_pool, db.qualified
    ensure_profile, hash_password = profile_sync.ensure_profile, security.hash_password


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time FCC SUPER_ADMIN bootstrap")
    parser.add_argument("--username", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--password")
    args = parser.parse_args()
    password = args.password or getpass.getpass("Password (min 8 chars): ")
    if len(password) < 8:
        raise SystemExit("Password minimal 8 karakter")
    open_pool()
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT count(*)::int AS n FROM {}").format(qualified("app_user")))
                if int(cur.fetchone()["n"]) != 0:
                    raise SystemExit("Bootstrap dibatalkan: app_user sudah berisi data. Gunakan menu Admin untuk user berikutnya.")
                cur.execute(
                    sql.SQL("INSERT INTO {} (username,nama,role,status,password_hash,must_change_pw,failed_logins,created_at,updated_at) VALUES (%s,%s,'SUPER_ADMIN','ACTIVE',%s,true,0,now(),now()) RETURNING *").format(qualified("app_user")),
                    (args.username.strip(), args.name.strip(), hash_password(password)),
                )
                row = cur.fetchone()
                ensure_profile(conn, row)
        print(f"SUPER_ADMIN {args.username} berhasil dibuat. Login lalu ganti password.")
    finally:
        close_pool()


if __name__ == "__main__":
    main()
