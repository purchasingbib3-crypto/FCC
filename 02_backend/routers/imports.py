from __future__ import annotations

from collections import Counter
import asyncio
from datetime import date, datetime, timezone
import hashlib
import logging
import re
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from psycopg import DatabaseError, errors, sql

from ..config import get_settings
from ..db import connection, fetch_all, qualified
from ..dependencies import require_capability, require_roles
from ..security import SessionUser
from ..services.xlsx_import import ParsedRow, current_parser_engine, normalize_unit, parse_reconciliation_file
from ..services.import_validation_cache import ValidationCacheError, claim_validation, delete_validation, load_validation, release_claim, save_validation

router = APIRouter(prefix="/api/v1", tags=["import-reconciliation"])
settings = get_settings()
log = logging.getLogger("fcc.imports")

_PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_MATCH_TOLERANCE_L = 0.01


def _validate_period(period: str, parsed: list[ParsedRow]) -> str:
    """Resolve upload period from the file itself.

    V10 used the UI-selected month as a hard gate.  That caused a perfectly valid
    July file to fail validation when the Upload page opened in August.  For FCC
    reconciliation the source file is authoritative: when all parsed rows belong
    to one month, that month becomes the batch period automatically.

    Multi-month workbooks remain blocked because one COMMITTED batch represents
    exactly one source + month and must not silently mix periods.
    """
    if not parsed:
        raise ValueError("Tidak ada baris transaksi fuel yang dapat dibaca dari file.")

    requested = str(period or "").strip()
    if requested and not _PERIOD_RE.fullmatch(requested):
        raise ValueError("periode harus berformat YYYY-MM")

    months = sorted({row.tanggal.strftime("%Y-%m") for row in parsed})
    if len(months) != 1:
        sample = ", ".join(months[:6])
        raise ValueError(
            "File memuat lebih dari satu periode bulan "
            f"({sample}). Pisahkan file per bulan sebelum Validate/Commit."
        )

    # File content is the source of truth.  A stale/default month in the UI must
    # never make a valid workbook fail validation.
    return months[0]


def _alias_map() -> tuple[dict[str, str], dict[str, dict[str, Any]], set[str]]:
    # V12.4: master_unit dengan alias_ss6, alias_sap sebagai ARRAY. Unnest jadi individual rows.
    rows = fetch_all(
        sql.SQL("""
            SELECT mu.kode AS unit_standar,
                   mu.nama,
                   mu.vendor_kode,
                   mu.kategori,
                   mu.status,
                   alias_element.alias_value,
                   alias_element.alias_kind
            FROM fcc.master_unit mu
            CROSS JOIN LATERAL (
                SELECT alias_ss6 AS alias_value, 'ss6' AS alias_kind FROM unnest(mu.alias_ss6) AS alias_ss6
                UNION ALL
                SELECT alias_sap AS alias_value, 'sap' AS alias_kind FROM unnest(mu.alias_sap) AS alias_sap
            ) alias_element
            WHERE mu.status = 'ACTIVE'
        """)
    )
    aliases_by_key: dict[str, set[str]] = {}
    masters: dict[str, dict[str, Any]] = {}
    for row in rows:
        canonical = str(row.get("unit_standar") or "").replace("\xa0", " ").strip()
        if not canonical or not normalize_unit(canonical):
            continue
        if canonical not in masters:
            masters[canonical] = {
                "unit_standar": canonical,
                "nama": row.get("nama"),
                "vendor_kode": row.get("vendor_kode"),
                "kategori": row.get("kategori"),
            }
        alias_value = row.get("alias_value")
        if alias_value:
            alias = normalize_unit(alias_value)
            if alias:
                aliases_by_key.setdefault(alias, set()).add(canonical)
    ambiguous = {alias for alias, standards in aliases_by_key.items() if len(standards) > 1}
    aliases = {alias: next(iter(standards)) for alias, standards in aliases_by_key.items() if len(standards) == 1}
    return aliases, masters, ambiguous


def _row_record(row: ParsedRow, standard: str | None) -> dict[str, Any]:
    return {
        "sumber": row.source,
        "tanggal": row.tanggal,
        "alias_unit": row.alias_unit,
        "unit_standar": standard,
        # Source quantity is preserved exactly for audit. Reconciliation must use
        # volume_net_l, never the signed SAP source quantity.
        "liter": row.liter,
        "quantity_source_l": row.quantity_source_l if row.quantity_source_l or row.liter == 0 else row.liter,
        "volume_net_l": row.volume_net_l,
        "shift": row.shift,
        "storage_location": row.storage_location,
        "source_row": row.source_row,
        "source_format": row.source_format,
        "source_record_id": row.source_record_id,
        "movement_type": row.movement_type,
        "material": row.material,
        "uom": row.uom,
    }


def _validate_rows(parsed: list[ParsedRow]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split rows into MAPPED, UNMAPPED, AMBIGUOUS and technical rejects.

    Master-data gaps are committable raw exceptions.  Both UNMAPPED and
    AMBIGUOUS retain the source row with unit_standar=NULL and therefore never
    enter numeric reconciliation.  AMBIGUOUS is intentionally not auto-guessed:
    operators must resolve the alias collision in Master Data and re-upload.

    Only technical integrity failures (currently duplicate source record IDs)
    block Commit.
    """
    aliases, _, ambiguous_keys = _alias_map()
    mapped: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_record_ids: set[tuple[str, str]] = set()

    for row in parsed:
        normalized_alias = normalize_unit(row.alias_unit)
        standard = aliases.get(normalized_alias)
        record = _row_record(row, standard)

        source_id = str(row.source_record_id or "").strip()
        if source_id:
            key = (row.source, source_id)
            if key in seen_record_ids:
                record["reason"] = "DUPLICATE_SOURCE_RECORD"
                rejected.append(record)
                continue
            seen_record_ids.add(key)

        if normalized_alias in ambiguous_keys:
            record["unit_standar"] = None
            record["mapping_status"] = "AMBIGUOUS"
            record["reason"] = "AMBIGUOUS_UNIT_ALIAS"
            ambiguous.append(record)
            continue
        if not standard:
            record["mapping_status"] = "UNMAPPED"
            record["reason"] = "UNIT_ALIAS_NOT_FOUND"
            unmapped.append(record)
            continue
        record["mapping_status"] = "MAPPED"
        mapped.append(record)
    return mapped, unmapped, ambiguous, rejected


def _assert_commit_runtime_contract() -> None:
    """Fail before a large write if the reporting commit migration is incomplete.

    V12 schema checks verified columns, but they did not guard the legacy per-row
    audit trigger on fuel_import_row.  That trigger doubles the number of writes
    for 25k-40k row source files and can make a valid Commit die as HTTP 500.
    Migration 07 removes that raw-row trigger; import_batch remains audited as the
    authoritative summary event.
    """
    rows = fetch_all(
        """
        SELECT tgname
        FROM pg_trigger t
        JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relname='fuel_import_row'
          AND NOT t.tgisinternal AND t.tgenabled <> 'D'
        """,
        (settings.database_schema,),
    )
    active = {str(row.get("tgname") or "") for row in rows}
    column_rows = fetch_all(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema=%s AND table_name='fuel_import_row'
             AND column_name IN ('quantity_source_l','volume_net_l')""",
        (settings.database_schema,),
    )
    import_columns = {str(row.get("column_name") or "") for row in column_rows}
    batch_columns = {str(row.get("column_name") or "") for row in fetch_all(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema=%s AND table_name='import_batch' AND column_name='baris_ambiguous'""",
        (settings.database_schema,),
    )}
    constraint_rows = fetch_all(
        """SELECT pg_get_constraintdef(c.oid) AS definition
           FROM pg_constraint c
           JOIN pg_class t ON t.oid=c.conrelid
           JOIN pg_namespace n ON n.oid=t.relnamespace
           WHERE n.nspname=%s AND t.relname='fuel_import_row'
             AND c.conname='fuel_import_row_mapping_status_check'""",
        (settings.database_schema,),
    )
    mapping_constraint = " ".join(str(row.get("definition") or "") for row in constraint_rows).upper()
    if (
        import_columns != {"quantity_source_l", "volume_net_l"}
        or batch_columns != {"baris_ambiguous"}
        or "AMBIGUOUS" not in mapping_constraint
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Migration canonical quantity/mapping belum diterapkan lengkap. Jalankan "
                "01_database/08_reporting_canonical_volume_v12_2.sql lalu restart backend."
            ),
        )
    if "trg_fuel_import_row_audit" in active:
        raise HTTPException(
            status_code=409,
            detail=(
                "Migration Reporting Commit belum diterapkan. Jalankan "
                "01_database/07_reporting_commit_reliability_v12_1.sql lalu restart backend. "
                "Trigger audit per-row fuel_import_row masih aktif dan dapat membuat Commit file besar gagal."
            ),
        )


def _copy_import_rows(cur: Any, batch_id: int, source: str, rows: list[dict[str, Any]]) -> int:
    """Bulk COPY raw reconciliation rows in one server-side stream."""
    copy_sql = sql.SQL(
        "COPY {} (batch_id,sumber,tanggal,alias_unit,unit_standar,liter,quantity_source_l,volume_net_l,shift,storage_location,source_row,created_at,source_format,source_record_id,movement_type,material,uom,mapping_status) "
        "FROM STDIN"
    ).format(qualified("fuel_import_row"))
    with cur.copy(copy_sql) as copy:
        for row in rows:
            copy.write_row((
                batch_id,
                source,
                row["tanggal"],
                row["alias_unit"],
                row["unit_standar"],
                row["liter"],
                row.get("quantity_source_l", row["liter"]),
                row.get("volume_net_l", row["liter"]),
                row.get("shift") or None,
                row.get("storage_location") or None,
                row.get("source_row") or None,
                datetime.now(timezone.utc),
                row.get("source_format") or None,
                row.get("source_record_id") or None,
                row.get("movement_type") or None,
                row.get("material") or None,
                row.get("uom") or None,
                row.get("mapping_status") or ("MAPPED" if row.get("unit_standar") else "UNMAPPED"),
            ))
    return len(rows)


def _validation_payload(source: str, period: str, filename: str | None, parsed: list[ParsedRow], mapped: list[dict[str, Any]], unmapped: list[dict[str, Any]], ambiguous: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> dict[str, Any]:
    committable = len(mapped) + len(unmapped) + len(ambiguous)
    dates = [row.tanggal for row in parsed]
    source_formats = Counter(str(row.source_format or "UNKNOWN") for row in parsed)
    unmapped_counts = Counter(str(row.get("alias_unit") or "-") for row in unmapped)
    ambiguous_counts = Counter(str(row.get("alias_unit") or "-") for row in ambiguous)
    source_total = round(sum(float(row.quantity_source_l if row.quantity_source_l or row.liter == 0 else row.liter) for row in parsed), 3)
    net_total = round(sum(float(row.volume_net_l) for row in parsed), 3)
    return {
        "ok": True,
        "source": source,
        "period": period,
        "filename": filename,
        "source_format": source_formats.most_common(1)[0][0] if source_formats else "UNKNOWN",
        "source_format_counts": dict(source_formats),
        "date_from": min(dates).isoformat() if dates else None,
        "date_to": max(dates).isoformat() if dates else None,
        "total_rows": len(parsed),
        "mapped_rows": len(mapped),
        "unmapped_rows": len(unmapped),
        "ambiguous_rows": len(ambiguous),
        "valid_rows": committable,
        "rejected_rows": len(rejected),
        "mapping_coverage_pct": round((len(mapped) / committable * 100), 3) if committable else 0.0,
        "commit_allowed": committable > 0 and not rejected,
        "quantity_source_total_l": source_total,
        "volume_net_total_l": net_total,
        "quantity_semantics": "SAP signed source quantity dipertahankan; reconciliation memakai volume_net_l. SS6 volume_net_l=source, SAP_MB51 volume_net_l=-source.",
        "reason_counts": dict(Counter(str(row.get("reason") or "UNKNOWN") for row in rejected + unmapped + ambiguous)),
        "top_unmapped_aliases": [
            {"alias": alias, "rows": count} for alias, count in unmapped_counts.most_common(30)
        ],
        "top_ambiguous_aliases": [
            {"alias": alias, "rows": count} for alias, count in ambiguous_counts.most_common(30)
        ],
        "sample_valid": mapped[:20],
        "sample_unmapped": unmapped[:50],
        "sample_ambiguous": ambiguous[:50],
        "sample_rejected": rejected[:50],
        "note": "UNMAPPED dan AMBIGUOUS boleh di-commit sebagai raw master-data exception dan tidak ikut reconciliation. Hanya technical reject yang memblokir Commit.",
    }


def _parse_validate_sync(content: bytes, filename: str, source: str, requested_period: str) -> tuple[list[ParsedRow], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str, str, dict[str, float]]:
    parse_started = time.perf_counter()
    parsed = parse_reconciliation_file(content, filename, source)
    parse_ms = (time.perf_counter() - parse_started) * 1000
    parser_engine = current_parser_engine()
    resolved_period = _validate_period(requested_period, parsed)
    mapping_started = time.perf_counter()
    mapped, unmapped, ambiguous, rejected = _validate_rows(parsed)
    mapping_ms = (time.perf_counter() - mapping_started) * 1000
    return parsed, mapped, unmapped, ambiguous, rejected, resolved_period, parser_engine, {
        "parse_excel": round(parse_ms, 1),
        "map_validate": round(mapping_ms, 1),
    }


@router.post("/import/reconciliation/validate")
async def validate_reconciliation_import(
    source: str = Form(...),
    period: str = Form(...),
    file: UploadFile = File(...),
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
) -> dict:
    source = source.upper().strip()
    if source not in {"SS6", "SAP"}:
        raise HTTPException(status_code=422, detail="Source harus SS6 atau SAP")

    request_started = time.perf_counter()
    content = await file.read()
    upload_read_ms = (time.perf_counter() - request_started) * 1000
    if len(content) > settings.reconciliation_max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File maksimal {settings.reconciliation_max_upload_mb} MB")

    content_sha256 = hashlib.sha256(content).hexdigest()
    requested_period = str(period or "").strip()
    try:
        parsed, mapped, unmapped, ambiguous, rejected, period, parser_engine, parse_timings = await asyncio.to_thread(
            _parse_validate_sync, content, file.filename or "upload.xlsx", source, requested_period
        )
        parse_ms = parse_timings["parse_excel"]
        mapping_ms = parse_timings["map_validate"]
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    summary_started = time.perf_counter()
    payload = _validation_payload(source, period, file.filename, parsed, mapped, unmapped, ambiguous, rejected)
    payload["requested_period"] = requested_period or None
    payload["period_autocorrected"] = bool(requested_period and requested_period != period)
    payload["file_sha256"] = content_sha256
    payload["parser_engine"] = parser_engine
    summary_ms = (time.perf_counter() - summary_started) * 1000

    cache_started = time.perf_counter()
    cache_payload = {
        "source": source,
        "period": period,
        "requested_period": requested_period or None,
        "filename": file.filename or "upload.xlsx",
        "file_sha256": content_sha256,
        "summary": payload,
        "mapped": mapped,
        "unmapped": unmapped,
        "ambiguous": ambiguous,
        "rejected": rejected,
    }
    token, expires_at, cache_bytes = await asyncio.to_thread(
        save_validation,
        settings.import_validation_cache_dir_resolved,
        settings.import_validation_cache_ttl_seconds,
        user.username,
        cache_payload,
    )
    cache_ms = (time.perf_counter() - cache_started) * 1000
    total_ms = (time.perf_counter() - request_started) * 1000

    payload["validation_token"] = token
    payload["validation_cache_expires_at"] = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
    payload["validation_cache_bytes"] = cache_bytes
    payload["commit_mode"] = "TOKEN_NO_REPARSE"
    payload["timings_ms"] = {
        "read_upload": round(upload_read_ms, 1),
        "parse_excel": round(parse_ms, 1),
        "map_validate": round(mapping_ms, 1),
        "build_summary": round(summary_ms, 1),
        "cache_for_commit": round(cache_ms, 1),
        "total_server": round(total_ms, 1),
    }
    return payload


@router.post("/import/reconciliation/commit")
async def commit_reconciliation_import(
    source: str = Form(...),
    period: str = Form(...),
    validation_token: str = Form(""),
    file: UploadFile | None = File(None),
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
) -> dict:
    source = source.upper().strip()
    if source not in {"SS6", "SAP"}:
        raise HTTPException(status_code=422, detail="Source harus SS6 atau SAP")

    request_started = time.perf_counter()
    cache_hit = False
    cache_claim = None
    parser_engine = "VALIDATION_CACHE"
    requested_period = str(period or "").strip()
    filename = file.filename if file else "upload.xlsx"

    if validation_token:
        try:
            cached = await asyncio.to_thread(
                load_validation,
                settings.import_validation_cache_dir_resolved,
                settings.import_validation_cache_ttl_seconds,
                user.username,
                validation_token,
            )
        except ValidationCacheError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if str(cached.get("source") or "").upper() != source:
            raise HTTPException(status_code=409, detail="Validation token tidak sesuai source. Validate ulang file.")
        try:
            cache_claim = await asyncio.to_thread(claim_validation, settings.import_validation_cache_dir_resolved, validation_token)
        except ValidationCacheError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        period = str(cached.get("period") or "")
        filename = str(cached.get("filename") or filename)
        mapped = list(cached.get("mapped") or [])
        unmapped = list(cached.get("unmapped") or [])
        ambiguous = list(cached.get("ambiguous") or [])
        rejected = list(cached.get("rejected") or [])
        base_result = dict(cached.get("summary") or {})
        cache_hit = True
    else:
        if file is None:
            raise HTTPException(status_code=422, detail="validation_token atau file wajib tersedia untuk Commit.")
        content = await file.read()
        if len(content) > settings.reconciliation_max_upload_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File maksimal {settings.reconciliation_max_upload_mb} MB")
        try:
            parsed, mapped, unmapped, ambiguous, rejected, period, parser_engine, _ = await asyncio.to_thread(
                _parse_validate_sync, content, filename, source, requested_period
            )
            base_result = _validation_payload(source, period, filename, parsed, mapped, unmapped, ambiguous, rejected)
            base_result["requested_period"] = requested_period or None
            base_result["period_autocorrected"] = bool(requested_period and requested_period != period)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    committable = mapped + unmapped + ambiguous
    if not committable:
        await asyncio.to_thread(release_claim, cache_claim)
        raise HTTPException(status_code=422, detail="Tidak ada baris yang dapat di-commit.")
    if rejected:
        await asyncio.to_thread(release_claim, cache_claim)
        reasons = Counter(str(row.get("reason") or "UNKNOWN") for row in rejected)
        reason_text = ", ".join(f"{key}={value}" for key, value in sorted(reasons.items()))
        raise HTTPException(
            status_code=409,
            detail=f"Commit diblokir karena {len(rejected)} technical reject ({reason_text}). Perbaiki duplicate source record lalu Validate ulang.",
        )

    try:
        _assert_commit_runtime_contract()
    except Exception:
        await asyncio.to_thread(release_claim, cache_claim)
        raise

    date_from = base_result.get("date_from")
    date_to = base_result.get("date_to")
    source_format = str(base_result.get("source_format") or "UNKNOWN")
    total_rows = int(base_result.get("total_rows") or len(committable))
    batch_code = f"IMP-{date.today():%Y%m%d}-{uuid4().hex[:10].upper()}"
    operation_id = uuid4().hex[:12].upper()
    batch_id = 0
    db_started = time.perf_counter()

    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"fcc-import:{source}:{period}",))
                cur.execute(
                    sql.SQL("UPDATE {} SET status='SUPERSEDED' WHERE sumber=%s AND periode=%s AND status='COMMITTED'").format(
                        qualified("import_batch")
                    ),
                    (source, period),
                )
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {} (kode,sumber,nama_file,periode,total_baris,baris_valid,baris_tolak,status,imported_by,imported_at,source_format,date_from,date_to,baris_mapped,baris_unmapped,baris_ambiguous) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,'COMMITTED',%s,now(),%s,%s,%s,%s,%s,%s) RETURNING id"
                    ).format(qualified("import_batch")),
                    (
                        batch_code, source, filename, period, total_rows,
                        len(committable), len(rejected), user.username, source_format,
                        date_from or None, date_to or None, len(mapped), len(unmapped), len(ambiguous),
                    ),
                )
                batch_id = int(cur.fetchone()["id"])
                _copy_import_rows(cur, batch_id, source, committable)
    except (errors.UndefinedColumn, errors.UndefinedTable) as exc:
        await asyncio.to_thread(release_claim, cache_claim)
        log.exception("reporting commit schema mismatch operation=%s source=%s period=%s", operation_id, source, period)
        raise HTTPException(
            status_code=409,
            detail=(
                f"Database Reporting belum sesuai migration (operation {operation_id}). "
                "Jalankan migration 05, 06, 07, lalu 08 dan restart backend."
            ),
        ) from exc
    except errors.UniqueViolation as exc:
        await asyncio.to_thread(release_claim, cache_claim)
        log.exception("reporting commit unique conflict operation=%s source=%s period=%s", operation_id, source, period)
        raise HTTPException(
            status_code=409,
            detail=f"Commit conflict/duplicate terdeteksi (operation {operation_id}). Refresh Batch History lalu Validate ulang.",
        ) from exc
    except (errors.CheckViolation, errors.NotNullViolation, errors.InvalidTextRepresentation) as exc:
        await asyncio.to_thread(release_claim, cache_claim)
        log.exception("reporting commit data contract error operation=%s source=%s period=%s", operation_id, source, period)
        raise HTTPException(
            status_code=422,
            detail=f"Data lolos parser tetapi ditolak kontrak database (operation {operation_id}, SQLSTATE {getattr(exc, 'sqlstate', '-')}). Cek log backend.",
        ) from exc
    except DatabaseError as exc:
        await asyncio.to_thread(release_claim, cache_claim)
        log.exception("reporting commit database error operation=%s source=%s period=%s rows=%s", operation_id, source, period, len(committable))
        raise HTTPException(
            status_code=500,
            detail=f"Database Commit gagal (operation {operation_id}, SQLSTATE {getattr(exc, 'sqlstate', '-')}). Tidak ada partial commit; transaksi sudah rollback. Cek journal backend menggunakan operation ID ini.",
        ) from exc

    db_ms = (time.perf_counter() - db_started) * 1000
    total_ms = (time.perf_counter() - request_started) * 1000
    result = dict(base_result)
    result.update({
        "batch_id": batch_id,
        "batch_code": batch_code,
        "status": "COMMITTED",
        "committed_rows": len(committable),
        "operation_id": operation_id,
        "requested_period": requested_period or base_result.get("requested_period"),
        "period_autocorrected": bool((requested_period or base_result.get("requested_period")) and str(requested_period or base_result.get("requested_period")) != period),
        "commit_cache_hit": cache_hit,
        "parser_engine": parser_engine,
        "commit_timings_ms": {
            "database_copy": round(db_ms, 1),
            "total_server": round(total_ms, 1),
        },
    })
    if validation_token:
        await asyncio.to_thread(delete_validation, settings.import_validation_cache_dir_resolved, validation_token)
    await asyncio.to_thread(release_claim, cache_claim)
    return result


@router.get("/import/batches")
def list_batches(_: SessionUser = Depends(require_capability("reporting.read"))) -> dict:
    rows = fetch_all(
        sql.SQL("SELECT * FROM {} ORDER BY imported_at DESC LIMIT 200").format(qualified("import_batch"))
    )
    return {"ok": True, "data": rows}


def _coverage(period: str | None) -> dict[str, Any]:
    filters = ["ib.status='COMMITTED'"]
    params: list[Any] = []
    if period:
        filters.append("ib.periode=%s")
        params.append(period)
    rows = fetch_all(
        f"""
        SELECT fir.sumber, min(fir.tanggal) AS date_from, max(fir.tanggal) AS date_to,
               count(*) AS rows,
               count(*) FILTER (WHERE fir.mapping_status='MAPPED') AS mapped_rows,
               count(*) FILTER (WHERE fir.mapping_status='UNMAPPED') AS unmapped_rows,
               count(*) FILTER (WHERE fir.mapping_status='AMBIGUOUS') AS ambiguous_rows
        FROM fcc.fuel_import_row fir
        JOIN fcc.import_batch ib ON ib.id=fir.batch_id
        WHERE {' AND '.join(filters)}
        GROUP BY fir.sumber
        """,
        params,
    )
    sources = {str(row.get("sumber") or "").upper(): row for row in rows}
    ss6, sap = sources.get("SS6"), sources.get("SAP")
    common_start = max(ss6["date_from"], sap["date_from"]) if ss6 and sap else None
    common_end = min(ss6["date_to"], sap["date_to"]) if ss6 and sap else None
    has_overlap = bool(common_start and common_end and common_start <= common_end)
    return {
        "sources": sources,
        "common_start": common_start,
        "common_end": common_end,
        "has_overlap": has_overlap,
        "partial": bool(ss6 and sap and (ss6["date_from"] != sap["date_from"] or ss6["date_to"] != sap["date_to"])),
    }


@router.get("/reconciliation")
def reconciliation(
    period: str | None = None,
    status: str | None = None,
    unit: str | None = None,
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    _: SessionUser = Depends(require_capability("reporting.read")),
) -> dict:
    filters = ["ib.status='COMMITTED'", "fir.mapping_status='MAPPED'", "fir.unit_standar IS NOT NULL"]
    params: list[Any] = []
    if period:
        filters.append("ib.periode=%s")
        params.append(period)
    where = " AND ".join(filters)
    query = f"""
    WITH src AS (
      SELECT fir.id AS import_row_id, fir.tanggal, fir.unit_standar, fir.sumber, fir.volume_net_l, fir.quantity_source_l,
             fir.shift, fir.storage_location, fir.source_row,
             mu.nama AS unit_nama, mu.vendor_kode, mu.kategori
      FROM fcc.fuel_import_row fir
      JOIN fcc.import_batch ib ON ib.id=fir.batch_id
      LEFT JOIN fcc.master_unit mu ON mu.kode=fir.unit_standar
      WHERE {where}
    ), grouped AS (
      SELECT tanggal, unit_standar,
             max(unit_nama) AS unit_nama,
             max(vendor_kode) AS vendor_kode,
             max(kategori) AS kategori,
             sum(volume_net_l) FILTER (WHERE sumber='SS6') AS ss6_l,
             sum(volume_net_l) FILTER (WHERE sumber='SAP') AS sap_l,
             count(*) FILTER (WHERE sumber='SS6') AS ss6_rows,
             count(*) FILTER (WHERE sumber='SAP') AS sap_rows,
             string_agg(DISTINCT NULLIF(shift,''), ', ' ORDER BY NULLIF(shift,'')) FILTER (WHERE sumber='SS6') AS shift_ss6,
             string_agg(DISTINCT NULLIF(storage_location,''), ', ' ORDER BY NULLIF(storage_location,'')) FILTER (WHERE sumber='SS6') AS storage_ss6,
             string_agg(DISTINCT NULLIF(storage_location,''), ', ' ORDER BY NULLIF(storage_location,'')) FILTER (WHERE sumber='SAP') AS storage_sap
      FROM src GROUP BY tanggal, unit_standar
    )
    SELECT *,
      COALESCE(sap_l,0)-COALESCE(ss6_l,0) AS delta_l,
      abs(COALESCE(sap_l,0)-COALESCE(ss6_l,0)) AS abs_delta_l,
      CASE
       WHEN COALESCE(ss6_l,0)=0 AND COALESCE(sap_l,0)<>0 THEN 'HANYA SAP'
       WHEN COALESCE(sap_l,0)=0 AND COALESCE(ss6_l,0)<>0 THEN 'HANYA SS6'
       WHEN abs(COALESCE(sap_l,0)-COALESCE(ss6_l,0)) <= {_MATCH_TOLERANCE_L} THEN 'MATCH'
       ELSE 'SELISIH' END AS status
    FROM grouped
    ORDER BY abs(COALESCE(sap_l,0)-COALESCE(ss6_l,0)) DESC, tanggal, unit_standar
    """
    rows = fetch_all(query, params)
    coverage = _coverage(period)
    common_start, common_end = coverage["common_start"], coverage["common_end"]
    if coverage["has_overlap"]:
        for row in rows:
            if row.get("tanggal") < common_start or row.get("tanggal") > common_end:
                row["status"] = "OUTSIDE COVERAGE"
                row["coverage_status"] = "OUTSIDE"
            else:
                row["coverage_status"] = "COMPARABLE"

    if status:
        rows = [r for r in rows if str(r.get("status") or "").upper() == status.upper()]
    if unit:
        needle = normalize_unit(unit)
        rows = [r for r in rows if needle in normalize_unit(r.get("unit_standar"))]
    if q:
        needle = str(q).strip().lower()
        rows = [
            r for r in rows
            if needle in " ".join(
                str(r.get(key) or "")
                for key in ("tanggal", "unit_standar", "unit_nama", "vendor_kode", "kategori", "shift_ss6", "storage_ss6", "storage_sap")
            ).lower()
        ]

    status_counts = Counter(str(row.get("status") or "UNKNOWN") for row in rows)
    comparable = [row for row in rows if str(row.get("status") or "") != "OUTSIDE COVERAGE"]
    total = len(rows)
    page_rows = rows[offset : offset + limit]
    return {
        "ok": True,
        "data": page_rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "coverage": coverage,
        "summary": {
            "rows": total,
            "comparable_rows": len(comparable),
            "outside_coverage_rows": status_counts.get("OUTSIDE COVERAGE", 0),
            "ss6_l": sum(float(r.get("ss6_l") or 0) for r in comparable),
            "sap_l": sum(float(r.get("sap_l") or 0) for r in comparable),
            "delta_l": sum(float(r.get("delta_l") or 0) for r in comparable),
            "abs_delta_l": sum(float(r.get("abs_delta_l") or 0) for r in comparable),
            "status_counts": dict(status_counts),
        },
    }


@router.get("/import/unmapped")
def list_unmapped_import_rows(
    period: str | None = None,
    source: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    _: SessionUser = Depends(require_capability("reporting.read")),
) -> dict:
    filters = ["ib.status='COMMITTED'", "fir.mapping_status IN ('UNMAPPED','AMBIGUOUS')"]
    params: list[Any] = []
    if period:
        filters.append("ib.periode=%s")
        params.append(period)
    if source:
        filters.append("fir.sumber=%s")
        params.append(source.upper())
    rows = fetch_all(
        f"""
        SELECT fir.sumber,fir.mapping_status,fir.alias_unit,count(*) AS rows,min(fir.tanggal) AS date_from,max(fir.tanggal) AS date_to,
               min(fir.source_row) AS first_source_row
        FROM fcc.fuel_import_row fir
        JOIN fcc.import_batch ib ON ib.id=fir.batch_id
        WHERE {' AND '.join(filters)}
        GROUP BY fir.sumber,fir.mapping_status,fir.alias_unit
        ORDER BY count(*) DESC,fir.sumber,fir.alias_unit
        LIMIT %s
        """,
        [*params, limit],
    )
    return {"ok": True, "total": len(rows), "data": rows}
