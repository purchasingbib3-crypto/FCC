# FCC Reporting V10.1 — Failed to Fetch Hotfix

## Problem
Reporting V10 can show `Failed to fetch` before Excel validation. That message means the browser did not receive an HTTP response from the validation API.

## Hotfix
- canonical static paths are derived from bundle instead of old VPS hardcoded paths;
- API host/port are environment configurable;
- reverse-proxy upload timeout increased from 30s to 300s by default;
- proxy diagnostic now checks `/api/v1/health`;
- frontend distinguishes API-down from upload timeout/limit;
- `/api/v1/health` reports `.xls`/xlrd availability.

## Environment
Optional overrides:
- `FCC_API_HOST=127.0.0.1`
- `FCC_API_PORT=8001`
- `FCC_PROXY_TIMEOUT=300`
- `FCC_STATIC_DIR=/path/to/03_frontend`

## Nginx guardrail
If Nginx is in front of FCC, configure at least:

```nginx
client_max_body_size 60m;
proxy_connect_timeout 30s;
proxy_send_timeout 300s;
proxy_read_timeout 300s;
proxy_request_buffering off;
```

## VPS acceptance
1. `curl -fsS http://127.0.0.1:8001/api/v1/health`
2. `curl -fsS http://127.0.0.1:8765/api/v1/health`
3. Verify `reporting_import.xls_supported=true` for SS6 `.xls`.
4. Validate SAP/SS6 in browser.
