#!/usr/bin/env python3
"""Static regression gate for FCC V12.3.1 FAST Import frontend."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "03_frontend" / "v12_3_1_fast_import_patch.js"
MAIN = ROOT / "02_backend" / "main.py"
PROXY = ROOT / "04_api-legacy" / "static_proxy_v12_3_1.py"
SERVICE = ROOT / "07_deploy" / "fcc-static-proxy.service.example"

checks: list[tuple[str, bool]] = []
js = PATCH.read_text(encoding="utf-8")
main = MAIN.read_text(encoding="utf-8")
proxy = PROXY.read_text(encoding="utf-8")
service = SERVICE.read_text(encoding="utf-8")

checks.extend([
    ("runtime patch exists", PATCH.is_file()),
    ("validate uploads workbook once", "if (action === 'validate') {\n        form.append('file', file, file.name);" in js),
    ("commit sends validation token", "form.append('validation_token', preview.validation_token);" in js),
    ("token commit message", "File TIDAK akan di-upload/parse ulang." in js),
    ("ambiguous is raw exception", "AMBIGUOUS:</b> boleh di-commit sebagai raw master-data exception" in js),
    ("engine checks parse-once mode", "PARSE_ONCE_CACHE_TOKEN" in js),
    ("fastapi injects patch", "v12_3_1_fast_import_patch.js" in main),
    ("legacy root bookmark patched", '@app.get("/index.html"' in main),
    ("legacy field bookmark patched", '@app.get("/field/index.html"' in main),
    ("static proxy injects patch", "v12_3_1_fast_import_patch.js" in proxy),
    ("service runs wrapper", "static_proxy_v12_3_1.py" in service),
])

failed = [name for name, ok in checks if not ok]
for name, ok in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {name}")

if failed:
    raise SystemExit(f"V12.3.1 frontend gate FAILED: {', '.join(failed)}")
print(f"V12.3.1 frontend gate PASS: {len(checks)}/{len(checks)}")
