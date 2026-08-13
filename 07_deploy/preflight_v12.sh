#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo '[1/6] Required environment variables'
: "${FCC_DATABASE_URL:?FCC_DATABASE_URL belum diset}"
: "${FCC_SESSION_SECRET:?FCC_SESSION_SECRET belum diset}"
if (( ${#FCC_SESSION_SECRET} < 32 )); then echo 'FCC_SESSION_SECRET harus >=32 karakter' >&2; exit 1; fi

echo '[2/6] Dependencies'
python - <<'PY'
import fastapi, openpyxl, multipart
from python_calamine import CalamineWorkbook
try:
    import xlrd
except Exception as exc:
    raise SystemExit(f'xlrd missing: {exc}')
print('fastapi=',fastapi.__version__)
print('openpyxl=',openpyxl.__version__)
print('xlrd=',xlrd.__version__)
print('python-calamine=OK')
print('python-multipart=OK')
PY

echo '[3/6] Static regression suite'
bash 06_tests/run_all_checks.sh

echo '[4/6] Effective Nginx upload settings (if nginx exists)'
if command -v nginx >/dev/null 2>&1; then
  nginx -T 2>&1 | grep -E 'client_max_body_size|proxy_(connect|send|read)_timeout|proxy_request_buffering|proxy_pass' || true
else
  echo 'nginx not installed — skip'
fi

echo '[5/6] Listening ports'
ss -ltnp 2>/dev/null | grep -E ':(8001|8765)\b' || true

echo '[6/6] Next gate'
echo 'After services are restarted run: bash 06_tests/check_upload_connectivity.sh'
