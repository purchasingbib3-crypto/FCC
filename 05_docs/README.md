# FCC PPA-BIB — V12.3 FAST Import Engine

Bundle ini adalah **production-ready** deployment FCC (Fuel Control Center) PPA-BIB dengan state paling baru di VPS.

## 📋 Quick Info

- **Version**: V12.3 FAST Import Engine (build 2026-08-13)
- **Backend**: FastAPI 0.115 + psycopg 3 + passlib + python-calamine 0.8.2 + xlrd 2.0.1
- **Database**: PostgreSQL lokal (Unix socket `/var/run/postgresql`), schema `fcc`
- **Frontend**: Single `index.html` (~340 KB) + `app.js` (~70 KB)
- **User DB**: 190 user (55 DRIVER, 108 FUELMAN, 9 PENERIMAAN, 11 ADMIN, 5+ LEGACY)

## 🚀 Key Features (V12.3)

### FAST Import Engine (NEW V12.3)
- **`python-calamine`** (Rust-backed) untuk XLS/XLSX, 5x lebih cepat dari openpyxl
- **Parse-once cache**: Validate parses workbook sekali, simpan ke cache
- **`validation_token`** returned, Commit tidak perlu upload file lagi
- **`asyncio.to_thread()`** untuk non-blocking Excel parse
- **`commit_mode: TOKEN_NO_REPARSE`** + `commit_cache_hit=true`
- **Cache TTL**: 1800 detik (configurable)
- **Owner-bound**: token only bisa dipakai user yang create

### V12.2 Canonical Volume
- `quantity_source_l` + `volume_net_l` (signed canonical)
- SAP MB51: 201/261 issue → negative net, 202/262 reversal → positive net
- Tolerance MATCH: 0.01 L

### V12.1 Commit Hotfix
- DROP per-row audit trigger
- PostgreSQL COPY streaming (instead of INSERT per row)
- HTTP 409 preflight jika trigger masih ada

### V12 Deep Audit
- Login lockout (5 attempts → 15 min)
- CORS env-driven
- Schema contract validation
- Canonical frontend path

### V10 Real Source
- SS6_REFueling parser (real format)
- SAP_MB51 parser (movement types 201/202/261/262)
- 261/262 unit derived from `Order`
- 201/202 unit derived from `Text`
- Signed SAP quantity
- UNMAPPED boleh di-commit sebagai raw exception

### V9 Reporting
- Dashboard, Monthly Report, Reconciliation, Exception Center
- Master Health (collision, orphan, missing)

### V8 + Hermes Patches
- 8 KPI Dashboard
- Capability-based authorization
- Auto-fill `*_by` UUID dari `fuel_profiles`
- Auto-fill `petugas_name` dari `user.nama`
- Auto-fill `volume_tera_*` (Σ selisih signed)
- HM-last + Sound curve
- View endpoints (no auth)

## 📂 Bundle Structure

```
fcc-v12.3-final/
├── 01_database/                              (15 MB)
│   ├── 01_schema_only.sql                    (127 KB) Schema fcc.* lengkap
│   ├── 03_patch_all_20260811.sql             (7.7 KB) V7 P0-P2 patches
│   ├── 04_field_reliability_v7.sql           (8.8 KB) V7 field patches
│   ├── 05_reporting_reliability_v9.sql       (925 B) V9 reporting patches
│   ├── 06_reporting_real_sources_v10.sql     (1.6 KB) V10 real sources
│   ├── 07_reporting_commit_reliability_v12_1.sql (968 B) V12.1 commit hotfix
│   ├── 08_reporting_canonical_volume_v12_2.sql (2.9 KB) V12.2 canonical volume
│   ├── sounding_table.csv                    (6.3 MB) CSV sounding original
│   └── sounding_table_normalized.csv          (6.4 MB) CSV sounding TA/FT
│
├── 02_backend/                               (300 KB)
│   ├── main.py, run_api.py, run_proxy.py
│   ├── config.py, db.py, security.py, dependencies.py
│   ├── schema_contract.py, permissions.py
│   ├── profile_sync.py, bootstrap_admin.py
│   ├── identity.py, models.py, proxy.py
│   ├── routers/                              (17 files)
│   │   ├── dashboard.py (V8)
│   │   ├── reporting.py (V9/V10/V12.2)
│   │   ├── imports.py (V12.3 FAST + cache)
│   │   ├── fuel_bridge.py (auto-fill master + petugas_name)
│   │   ├── evidence.py, health.py, auth.py, dll
│   └── services/                              (6 files)
│       ├── xlsx_import.py (Calamine + xlrd fallback)
│       ├── import_validation_cache.py (V12.3 NEW)
│       └── (4 services lainnya)
│
├── 03_frontend/                              (350 KB)
│   ├── index.html (347 KB) — V12.3 + all Hermes patches
│   ├── app.js (69 KB) — postToBackend helper
│   ├── fcc-client.js, styles.css
│   └── assets/tera-tangki.json
│
├── 04_api-legacy/                            (300 KB)
│   └── 12 files (proxy_server, static_proxy, server_v6/v7/v8_pg, dll)
│
├── 05_docs/                                  (50 KB)
│   ├── README.md (file ini)
│   ├── V12_3_FAST_IMPORT_PATCH_REPORT.md
│   ├── V12_2_CANONICAL_VOLUME_PATCH_REPORT.md
│   ├── V12_1_COMMIT_HTTP500_HOTFIX.md
│   ├── V12_DEEP_AUDIT_PATCH_REPORT.md
│   ├── V10_1_FAILED_FETCH_HOTFIX.md
│   ├── REPORTING_V10_REAL_SOURCE_AUDIT.md
│   ├── REPORTING_V9_AUDIT_REPORT.md
│   ├── PATCHES_SUMMARY.md
│   └── BUILD_INFO.txt
│
├── 06_env/                                   (1 KB)
│   └── requirements.txt
│
└── 07_deploy/                                (8 KB)
    ├── fcc-api.service.example
    ├── fcc-static-proxy.service.example
    ├── nginx_fcc.conf.example
    └── preflight_v12.sh
```

## 🚀 Cara Restore (Fresh Deployment)

### 1. Setup Database
```bash
sudo -u postgres psql -d fcc < 01_database/01_schema_only.sql
# Data dump ADA DI BACKUP TERPISAH (75 MB), bukan di repo agar repo size manageable
# Restore data dari backup VPS

sudo -u postgres psql -d fcc < 01_database/03_patch_all_20260811.sql
sudo -u postgres psql -d fcc < 01_database/04_field_reliability_v7.sql
sudo -u postgres psql -d fcc < 01_database/05_reporting_reliability_v9.sql
sudo -u postgres psql -d fcc < 01_database/06_reporting_real_sources_v10.sql
sudo -u postgres psql -d fcc < 01_database/07_reporting_commit_reliability_v12_1.sql
sudo -u postgres psql -d fcc < 01_database/08_reporting_canonical_volume_v12_2.sql

# Sounding table (84,168 rows)
PGPASSWORD=*** psql -h /var/run/postgresql -U fcc_app -d fcc \
  -c "\copy fcc.sounding_table FROM '01_database/sounding_table_normalized.csv' CSV HEADER"
```

### 2. Setup Backend
```bash
cd 02_backend/
cp ../06_env/.env.example .env
# Edit .env: FCC_DATABASE_URL, FCC_SESSION_SECRET (random 32+ chars)

pip install -r ../06_env/requirements.txt
# Required: python-calamine==0.8.2, xlrd==2.0.1

python3 run_api.py  # port 8001
```

### 3. Setup Frontend
Backend `main.py` otomatis serve frontend via `/field` static mount. Frontend di-deploy di `/home/ubuntu/fcc-field/` (atau gunakan static_proxy).

### 4. Reverse Proxy (Cloudflare)
```bash
# Gunakan config dari 07_deploy/nginx_fcc.conf.example
# atau cloudflared tunnel
```

## 🔑 Login User (Password = NRP)

| NRP | Nama | Role |
|---|---|---|
| superadmin | Super Admin | SUPER_ADMIN |
| 81230108 | BAGAS SATRIAN HAKIM | ADMIN |
| 81230097 | ALDI ROY YUNAWAN | FUELMAN |
| 81230523 | MUHAMMAD GHOZALI | PENERIMAAN |
| 18053909 | ACHMAD ECHSANUDIN | DRIVER |

**Password = NRP** (contoh: NRP `81230108` → password `81230108`)

Total **190 user** ACTIVE.

## 🌐 Endpoint Penting (V12.3)

### Public (No Auth)
| Endpoint | Fungsi |
|----------|--------|
| `GET /api/v1/health` | Health + V12.3 metrics (fast_excel_engine, validation_mode, dll) |
| `GET /api/v1/dashboard/overview` | V8: 8 KPI dashboard |
| `GET /api/v1/sounding/volume?aset=&dip=` | Sounding lookup |
| `GET /api/v1/sounding/curve` | Sounding curve |
| `GET /api/v1/master/legacy-*` | Legacy master + UUID |
| `GET /api/v1/master/ft-mandar-ocean` | FT Mandar Ocean (88 ACTIVE) |
| `GET /api/v1/master/route-master` | Route master |
| `GET /api/v1/master/users` | List user |
| `GET /api/v1/master/users/count` | Count user |
| `GET /api/v1/master/hm-last` | HM terakhir |
| `GET /api/v1/master/view/*` | View endpoints |
| `GET /api/evidence/public/*` | List/Get evidence |
| `POST /api/auth/login` | Login |
| `POST /api/fuel/{table}` | Generic insert (auto-fill petugas_name) |

### Auth Required
| Endpoint | Fungsi |
|----------|--------|
| `GET /api/v1/reporting/overview` | V9: SS6+SAP scope |
| `GET /api/v1/reporting/monthly` | V9: Period aggregation |
| `GET /api/v1/reporting/exceptions` | V9: Exception center |
| `GET /api/v1/reporting/master-health` | V9: Master health |
| `POST /api/v1/import/reconciliation/validate` | **V12.3 FAST + TOKEN** |
| `POST /api/v1/import/reconciliation/commit` | **V12.3 TOKEN_NO_REPARSE** |
| `GET /api/v1/import/batches` | Batch history |

## 📊 V12.3 Acceptance Gate

✅ `pip install -r 06_env/requirements.txt` (python-calamine==0.8.2)
✅ Restart API
✅ `/api/v1/health`:
- `commit_ready=true`
- `fast_excel_engine=CALAMINE`
- `validation_mode=PARSE_ONCE_CACHE_TOKEN`
- `reconciliation_quantity=volume_net_l`
✅ Validate SAP/SS6: catat `timings_ms`
✅ Commit: `commit_cache_hit=true`
✅ Verify: 1 COMMITTED batch per source+period

## 🛠️ Validasi File SS6/SAP

| Tahap | Check |
|---|---|
| 1. Format Detection | Auto-detect SS6_REFUELING / SAP_MB51 / SAP_DIRECT (header 50 baris) |
| 2. Row Parsing | DATE, QTY (liter), UNIT alias, UOM=L only, Movement type {201,202,261,262} |
| 3. Periode | YYYY-MM match semua tanggal row (file-authoritative V12.1) |
| 4. Mapping | MAPPED (commit OK), UNMAPPED (raw exception OK), AMBIGUOUS (block), DUPLICATE (block) |
| 5. Commit | `rejected_rows === 0` dan `valid_rows > 0` |

## 📈 Live VPS State

| Service | Port | PID |
|---|---|---|
| FastAPI V12.3 | 8001 | (active) |
| static_proxy (Cloudflare) | 8765 | (active) |
| proxy_server (port 80) | 80 | (active) |
| PostgreSQL (local) | 5432 (Unix socket) | (active) |

Cloudflare: `https://fogdcbib.web.id/`

## 📞 Contact

Bundle ini di-deploy & di-maintain oleh **purchasingbib3-crypto** (PT PPA-BIB Fuel Operations).
