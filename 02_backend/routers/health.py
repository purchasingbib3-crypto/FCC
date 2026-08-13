from __future__ import annotations

from fastapi import APIRouter

from ..config import get_settings
from ..db import fetch_all, fetch_one
from ..schema_contract import schema_contract_status
from ..services import xlsx_import

router = APIRouter(prefix="/api/v1", tags=["health"])
settings = get_settings()


@router.get("/health")
def health() -> dict:
    row = fetch_one("SELECT now() AS db_time, current_database() AS database")
    schema = schema_contract_status()
    trigger_rows = fetch_all(
        """
        SELECT tgname FROM pg_trigger t
        JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relname='fuel_import_row'
          AND NOT t.tgisinternal AND t.tgenabled <> 'D'
        """,
        (settings.database_schema,),
    )
    active_import_triggers = [str(item.get("tgname") or "") for item in trigger_rows]
    commit_ready = bool(schema.get("ok")) and "trg_fuel_import_row_audit" not in active_import_triggers
    return {
        "ok": True,
        "site": settings.site_code,
        "version": "2026.08.13-reporting-v12.3-fast-import",
        "database": row,
        "schema_contract": {
            "ok": bool(schema.get("ok")),
            "missing_tables": len(schema.get("missing_tables") or []),
            "missing_column_groups": len(schema.get("missing_columns") or {}),
        },
        "reporting_import": {
            "xlsx_supported": True,
            "fast_excel_engine": "CALAMINE" if xlsx_import.CalamineWorkbook is not None else "FALLBACK",
            "calamine_supported": xlsx_import.CalamineWorkbook is not None,
            "xls_supported": (xlsx_import.CalamineWorkbook is not None) or (xlsx_import.xlrd is not None),
            "xls_engine": "calamine" if xlsx_import.CalamineWorkbook is not None else ("xlrd" if xlsx_import.xlrd is not None else "MISSING_XLS_ENGINE"),
            "xlrd_version": getattr(xlsx_import.xlrd, "__version__", None) if xlsx_import.xlrd is not None else None,
            "max_upload_mb": settings.reconciliation_max_upload_mb,
            "commit_ready": commit_ready,
            "active_raw_import_triggers": active_import_triggers,
            "commit_engine": "POSTGRES_COPY",
            "validation_mode": "PARSE_ONCE_CACHE_TOKEN",
            "validation_cache_ttl_seconds": settings.import_validation_cache_ttl_seconds,
            "validation_cache_dir": str(settings.import_validation_cache_dir_resolved),
            "quantity_contract": "SOURCE_SIGNED_PLUS_CANONICAL_NET",
            "reconciliation_quantity": "volume_net_l",
            "match_tolerance_l": 0.01,
        },
        "security": {
            "ok": (not settings.allow_public_register) and bool(settings.cookie_secure),
            "public_registration_enabled": settings.allow_public_register,
            "cookie_secure": settings.cookie_secure,
            "cookie_samesite": settings.cookie_samesite,
        },
    }
