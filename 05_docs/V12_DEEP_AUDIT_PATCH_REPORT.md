# FCC V12 — Deep Audit Patch Report

## Source of truth

Patched from the user-supplied `fcc-bundle-FINAL-FULL.zip` after deep audit. The bundle was proven to be hybrid: backend version metadata referenced the V10.1 hotfix while `04_api-legacy/static_proxy.py` was still the older V10/V11 proxy with hardcoded paths/port, `/api/health`, and a 30-second upstream timeout.

## P0 fixes implemented

### Upload transport / `Failed to fetch`

- Replaced hardcoded static proxy contract with bundle-relative/env-driven paths.
- `FCC_API_HOST`, `FCC_API_PORT`, `FCC_PROXY_TIMEOUT` are now honored.
- Health diagnostic corrected from `/api/health` to `/api/v1/health`.
- Upstream timeout default increased to 300 seconds.
- Static proxy has a 60 MB body safety limit and can read Content-Length or chunked request bodies.
- Request `Transfer-Encoding` is not forwarded after the body is decoded.
- Frontend multipart request now performs a health probe when `fetch()` itself rejects:
  - API health fails -> report backend/reverse-proxy unreachable;
  - API health works -> report upload path/timeout/body-size/TLS issue.
- Added deploy Nginx contract with 60 MB body limit and 300-second upload/upstream timeouts.

### Validation period regression

- Merged V11 file-authoritative month logic.
- A stale UI month can no longer reject a valid single-month workbook.
- `requested_period`, resolved `period`, and `period_autocorrected` are returned.
- Frontend switches the period field to the detected file month before enabling Commit.
- Multi-month workbooks remain hard-blocked.

### Security regression in FINAL-FULL

- Removed public HTTP first-user `SUPER_ADMIN` bootstrap.
- Initial `SUPER_ADMIN` is CLI-only through `bootstrap_admin.py`.
- Public registration remains fail-closed by default and can never create ADMIN/SUPER_ADMIN.
- Login lockout now actively increments `failed_logins`, sets `locked_until`, blocks the lock window, and clears lockout after success.

## P1 fixes implemented

- Health now reports `.xlsx/.xls` engine readiness, `xlrd` version, max reconciliation upload size, schema-contract boolean, and security posture.
- CORS has an explicit `FCC_CORS_ORIGINS` escape hatch while same-origin remains the preferred architecture.
- Reconciliation upload maximum is configuration driven (`FCC_RECONCILIATION_MAX_UPLOAD_MB`, default 50 MB).
- `run_proxy.py` package path was repaired.
- Optional FastAPI proxy timeout now follows configuration.
- `.env.example` now reflects production HTTPS/security defaults instead of development cookie defaults.
- Build/version metadata is consistent across runtime and tests.

## Existing V10 real-source logic retained

- SS6 real export parser.
- SAP MB51 parser.
- Movement whitelist 201/202/261/262.
- 261/262 unit from Order; 201/202 unit from Text.
- Signed SAP quantity preserved.
- UNMAPPED rows may be committed as raw exceptions.
- AMBIGUOUS/duplicate source IDs are technical rejects and block commit.
- Source+period advisory lock and SUPERSEDED re-upload behavior.
- Common coverage and OUTSIDE COVERAGE logic.

## Real source verification performed in audit workspace

SAP source file was parsed directly:

```text
rows       39,799
format     SAP_MB51
coverage   2026-07-02..2026-07-31
261        31,123
201         8,052
202           565
262            59
```

The workspace did not have `xlrd` available and had no network package installation access. The supplied SS6 `.xls` was converted read-only with LibreOffice only for parser-semantic verification; the resulting workbook produced:

```text
rows       25,476
format     SS6_REFUELING
coverage   2026-07-01..2026-07-15
unique source_record_id 25,476
```

**Direct `.xls` acceptance with `xlrd==2.0.1` remains a mandatory VPS gate.**

## Automated release test result

V12 static suite covers:

- Main Dashboard contracts: 77 checks.
- Reporting Dashboard contracts: 86 checks.
- Real-source Reporting contracts: 76 checks.
- Synthetic parser behavior: 18 checks.
- V12 transport/security/release contracts: 34 checks after schema/security additions.
- Python compile and frontend JS syntax.

Live PostgreSQL, live SS6 authentication, Nginx/Cloudflare public path, cookies, and real `.xls` direct parsing cannot be truthfully certified from the offline workspace and must be accepted on the VPS.
