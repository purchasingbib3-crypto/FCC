from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext

from .config import get_settings
from .identity import field_actor_id

settings = get_settings()
pwd_context = CryptContext(schemes=["argon2", "bcrypt", "pbkdf2_sha256"], deprecated="auto")
serializer = URLSafeTimedSerializer(settings.session_secret, salt="fcc-session-v1")


@dataclass(frozen=True)
class SessionUser:
    id: str
    username: str
    nama: str
    role: str
    vendor_kode: str | None = None
    must_change_pw: bool = False
    field_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        from .permissions import capabilities_for
        return {
            "id": self.id,
            "username": self.username,
            "nama": self.nama,
            "role": self.role,
            "vendor_kode": self.vendor_kode,
            "must_change_pw": self.must_change_pw,
            "field_id": self.field_id or field_actor_id(self.username),
            "capabilities": capabilities_for(self.role),
        }


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, stored: str) -> bool:
    value = (stored or "").strip()
    if not value:
        return False
    if value.startswith("PLAIN:"):
        return hmac.compare_digest(password, value[6:])
    if value.startswith("SHA256:"):
        value = value[7:]
    if len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value):
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest.lower(), value.lower())
    try:
        return pwd_context.verify(password, value)
    except Exception:
        # Emergency compatibility only. Migrate plaintext hashes after login.
        return hmac.compare_digest(password, value)


def issue_session(user: SessionUser) -> str:
    payload = {**user.to_dict(), "iat": int(datetime.now(timezone.utc).timestamp())}
    return serializer.dumps(payload)


def read_session(token: str) -> SessionUser:
    try:
        payload = serializer.loads(token, max_age=settings.session_max_age_seconds)
    except SignatureExpired as exc:
        raise PermissionError("Session kedaluwarsa") from exc
    except BadSignature as exc:
        raise PermissionError("Session tidak valid") from exc
    return SessionUser(
        id=str(payload["id"]),
        username=str(payload["username"]),
        nama=str(payload.get("nama") or payload["username"]),
        role=str(payload["role"]),
        vendor_kode=payload.get("vendor_kode"),
        must_change_pw=bool(payload.get("must_change_pw")),
        field_id=str(payload.get("field_id") or field_actor_id(str(payload["username"]))),
    )
