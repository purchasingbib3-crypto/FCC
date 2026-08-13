# FCC V12.3 FAST Import Engine — Patch Report

## Scope
V12.3 is built from V12.2 Canonical Volume. It does not change the canonical SAP/SS6 business rules, reconciliation sign convention, mapping states, or database schema. The patch is limited to the upload Validate/Commit pipeline and operational responsiveness.

## Root cause of slow validation
V12.2 performed a full Excel parse and canonical mapping during Validate, then repeated the same parse and mapping again during Commit. `openpyxl`/`xlrd` were also the only readers, and the CPU-heavy synchronous parse ran inside an async FastAPI request handler.

## Changes
1. Preferred Excel reader is `python-calamine==0.8.2` for `.xls` and `.xlsx`; `xlrd`/`openpyxl` remain automatic fallbacks.
2. Validate parses the workbook once, applies mapping once, and writes the canonical committable rows to a compressed owner-bound validation cache.
3. Validate returns `validation_token`, expiration time, parser engine, SHA-256, and stage timings.
4. Commit receives `validation_token` and does not upload or parse the workbook again. It directly performs the existing PostgreSQL COPY transaction.
5. Workbook parse + mapping is executed via `asyncio.to_thread()` so long Excel parsing does not block unrelated FCC HTTP requests.
6. Frontend displays FAST VALIDATE timing and FAST COMMIT timing and states explicitly when Commit uses a cache hit.
7. Cache defaults: `/tmp/fcc-import-validation-cache`, TTL 1800 seconds. Both are configurable from environment.
8. Health exposes `fast_excel_engine`, `calamine_supported`, `validation_mode`, cache TTL, and the existing commit readiness gate.
9. No new SQL migration is introduced. V12.3 requires migrations through `08_reporting_canonical_volume_v12_2.sql` exactly as V12.2 did.

## Reliability behavior
- A missing/expired validation token returns an actionable 409 and requires Validate again.
- Tokens are bound to the validating username.
- Successful Commit deletes the token.
- Fallback file Commit remains available at API level for compatibility, but the canonical frontend always uses token Commit.
- Calamine failure falls back to the previous readers rather than failing the import solely because the fast engine has a problem.

## Local verification
- Full regression suite: all prior Dashboard, Reporting, parser, transport/security, commit, and canonical-volume checks pass.
- New V12.3 fast-import checks verify token cache round-trip, owner isolation, Commit-without-file contract, frontend token usage, health markers, and parser fallback contract.
- Current workspace cannot install `python-calamine` from the network, so the real SAP sample was benchmarked with the OPENPYXL fallback. The SAP sample parsed 39,799 MB51 rows in ~4.3 seconds in this workspace. Production must verify `fast_excel_engine=CALAMINE` after installing requirements.

## Production acceptance
1. `pip install -r 06_env/requirements.txt`
2. Restart API/static proxy.
3. `/api/v1/health` must show:
   - `commit_ready=true`
   - `fast_excel_engine=CALAMINE`
   - `validation_mode=PARSE_ONCE_CACHE_TOKEN`
   - `reconciliation_quantity=volume_net_l`
4. Validate SAP/SS6 and record `timings_ms`.
5. Commit and confirm `commit_cache_hit=true`.
6. Verify only one COMMITTED batch exists for source+period and row counts match Validate.
