from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import get_settings

settings = get_settings()
DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,(?P<data>.+)$", re.DOTALL)


@dataclass
class StoredFile:
    path: Path
    relative_path: str
    mime_type: str
    size: int
    sha256: str


def decode_data_url(value: str) -> tuple[str, bytes]:
    match = DATA_URL_RE.match(value.strip())
    if not match:
        raise ValueError("Format foto harus data URL base64")
    mime = match.group("mime").lower()
    if not mime.startswith("image/"):
        raise ValueError("Hanya file image yang diizinkan")
    try:
        raw = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Base64 foto tidak valid") from exc
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise ValueError(f"Foto melebihi {settings.max_upload_mb} MB")
    return mime, raw


def extension_for(mime: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime, ".bin")


def save_evidence(modul: str, record_id: str, photo_type: str, data_url: str) -> StoredFile:
    mime, raw = decode_data_url(data_url)
    digest = hashlib.sha256(raw).hexdigest()
    safe_record = re.sub(r"[^A-Za-z0-9_.-]+", "_", record_id)[:120]
    safe_module = re.sub(r"[^A-Za-z0-9_.-]+", "_", modul)[:60]
    safe_type = re.sub(r"[^A-Za-z0-9_.-]+", "_", photo_type)[:60]
    relative = Path(safe_module) / safe_record / f"{safe_type}_{digest[:16]}{extension_for(mime)}"
    absolute = settings.evidence_dir_resolved / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    if not absolute.exists():
        absolute.write_bytes(raw)
    return StoredFile(absolute, relative.as_posix(), mime, len(raw), digest)


def read_as_data_url(relative_path: str, mime_type: str | None) -> str:
    root = settings.evidence_dir_resolved
    path = (root / relative_path).resolve()
    if root not in path.parents and path != root:
        raise ValueError("Storage path tidak valid")
    raw = path.read_bytes()
    mime = mime_type or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
