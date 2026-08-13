from __future__ import annotations

import gzip
import hashlib
import os
import pickle
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any


class ValidationCacheError(RuntimeError):
    pass


def _safe_owner(owner: str) -> str:
    return hashlib.sha256(str(owner or "").encode("utf-8")).hexdigest()[:16]


def _token_name(token: str) -> str:
    if not token or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in token):
        raise ValidationCacheError("Validation token tidak valid.")
    return token


def _path(cache_dir: Path, token: str) -> Path:
    return cache_dir / f"{_token_name(token)}.pkl.gz"


def cleanup_expired(cache_dir: Path, ttl_seconds: int) -> int:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - max(60, int(ttl_seconds))
    removed = 0
    for pattern in ("*.pkl.gz", "*.claim"):
        for path in cache_dir.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
    return removed


def save_validation(
    cache_dir: Path,
    ttl_seconds: int,
    owner: str,
    payload: dict[str, Any],
) -> tuple[str, float, int]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cleanup_expired(cache_dir, ttl_seconds)
    token = secrets.token_urlsafe(24).replace("=", "")
    now = time.time()
    envelope = {
        "version": 1,
        "owner_hash": _safe_owner(owner),
        "created_at": now,
        "expires_at": now + max(60, int(ttl_seconds)),
        "payload": payload,
    }
    destination = _path(cache_dir, token)
    fd, temp_name = tempfile.mkstemp(prefix="fcc-valid-", suffix=".tmp", dir=str(cache_dir))
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=3) as compressed:
                pickle.dump(envelope, compressed, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temp_name, destination)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass
    return token, float(envelope["expires_at"]), destination.stat().st_size


def load_validation(
    cache_dir: Path,
    ttl_seconds: int,
    owner: str,
    token: str,
) -> dict[str, Any]:
    cleanup_expired(cache_dir, ttl_seconds)
    path = _path(cache_dir, token)
    if not path.exists():
        raise ValidationCacheError("Validation cache tidak ditemukan/expired. Validate file ulang.")
    try:
        with gzip.open(path, "rb") as handle:
            envelope = pickle.load(handle)
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise ValidationCacheError("Validation cache rusak. Validate file ulang.") from exc
    if float(envelope.get("expires_at") or 0) <= time.time():
        path.unlink(missing_ok=True)
        raise ValidationCacheError("Validation token sudah expired. Validate file ulang.")
    if envelope.get("owner_hash") != _safe_owner(owner):
        raise ValidationCacheError("Validation token bukan milik user aktif.")
    return dict(envelope.get("payload") or {})


def delete_validation(cache_dir: Path, token: str) -> None:
    try:
        _path(cache_dir, token).unlink(missing_ok=True)
    except (OSError, ValidationCacheError):
        pass


def claim_validation(cache_dir: Path, token: str) -> Path:
    """Atomically claim a token so double-click/concurrent Commit cannot replay it."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    claim = cache_dir / f"{_token_name(token)}.claim"
    try:
        fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValidationCacheError("Validation token sedang dipakai Commit lain. Tunggu hasil Commit/refresh Batch History.") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(str(time.time()))
    return claim


def release_claim(claim: Path | None) -> None:
    if claim is None:
        return
    try:
        claim.unlink(missing_ok=True)
    except OSError:
        pass
