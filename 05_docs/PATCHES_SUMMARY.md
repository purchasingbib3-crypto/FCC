# Patches Summary — 11 Agustus 2026

## Backend Patches (`/opt/fcc-staging/app/`)

### `db.py`
- Connection pool, qualified(), fetch_one/all helpers

### `routers/fuel_bridge.py` (auto-fill di `_clean_payload`)
- `fuel_tx_transfer_fuel`: `volume_tera_unit_awal/akhir` di-fill otomatis dari `sounding_table` via JOIN `fuel_master_fuel_truck`
- `penerimaan_mo`: `tera_master_depan/belakang_cm`, `selisih_t_depan/belakang_cm`, `selisih_t_depan/belakang_pct`, `tera_status` di-fill otomatis dari `ft_mandar_ocean`
- `sounding_main_tank`: `intank_cm_master`, `aktual_cm_master`, `selisih_cm_intank/aktual`, `sounding_status` di-fill otomatis dari row sebelumnya
- `*_by` (created_by, voided_by, updated_by): BIGINT → UUID via `fuel_profiles.id` lookup

### `routers/sounding_public.py` (NEW, 138 lines)
- `GET /api/v1/sounding/volume` — strip-aware lookup (TA11/FT2632)
- `GET /api/v1/sounding/curve` — sampled curve cache

### `routers/fm_awal_public.py` (NEW, 113 lines)
- `GET /api/v1/fm-awal/default` — FM Awal priority: manual override → current transfer → legacy transfer

### `routers/master_public.py` (NEW, 186 lines)
- `GET /api/v1/master/legacy-jalur/tank/fuel-truck/vendor` — master data + UUID mapping
- `GET /api/v1/master/ft-mandar-ocean` — FT Mandar Ocean (86 ACTIVE)

### `routers/master_write_public.py` (NEW, 583 lines)
- `POST/PATCH/DELETE /api/v1/master/route-master` — route master CRUD
- `POST /api/v1/master/fm-awal-settings` — FM Awal upsert
- `GET /api/v1/master/sounding-prev` — sounding sebelumnya untuk (tank, tanggal, shift)
- `GET /api/v1/master/flowmeter-last` — strip-aware FM lookup
- `GET /api/v1/master/view/transfer-fuel|fuel-truck-monitoring|penerimaan|pengurasan|sounding-main-tank|cleanliness|penerimaan-tera-check` — view & table read endpoints

### `routers/evidence.py`
- `GET /api/evidence/public/list` — anonymous list per record
- `GET /api/evidence/public/{id}` — anonymous get image data URL

### `security.py`
- `SessionUser.to_dict()` include `capabilities` (auto-extended from `permissions.py`)

### `permissions.py`
- FIELD: transfer.write, flowmeter.write, hm.write, receiving.write, drainage.write, sounding.write, cleanliness.write, dashboard.read, history.read
- SUPERVISOR: All FIELD + closing.read + discrepancy.read + master.read
- PENERIMAAN: receiving.write, drainage.write, sounding.write
- DRIVER: hm.write, cleanliness.write
- FUELMAN: transfer.write, flowmeter.write, cleanliness.write
- ADMIN/SUPER_ADMIN: ['*']

### `fuel_bridge.py` WRITE_ROLES
- FIELD/SUPERVISOR ditambahkan ke fuel_tx_transfer_fuel, fuel_tx_fuel_truck_monitoring, penerimaan_mo, pengurasan, sounding_main_tank

### `main.py`
- Hybrid: register canonical layout + endpoint publik
- Schema contract V7 validation di startup
- Frontend path detection (candidates: 03_frontend, frontend)

## Database Patches

### Schema migrations (`01_database/03_patch_all_20260811.sql`, `04_field_reliability_v7.sql`)
- UNIQUE constraint `fuel_tera_tangki_grid(site_code, unit_code)`
- `client_request_id` column di 6 tabel (idempotency)
- `fuel_profiles.app_user_id` linking
- Route purpose trigger (JALUR 1/2/3=TRANSFER, 5/6/7=RECEIVING)
- Tera master fields di `penerimaan_mo` (8 kolom baru)
- Sounding master fields di `sounding_main_tank` (6 kolom baru)

### Data patches
- Rename FS11-FS15 → TA11-TA15 (cascade ke tabel transaksi)
- Replace `ft_mandar_ocean` (81 → 88 rows dengan t2_depan_cm/t2_belakang_cm)

## Frontend Patches (`/home/ubuntu/fcc-field/index.html`)

### Capability gating (line ~3528)
- Anonymous user: show all 7 tombol Input Operasional
- Login user dengan role apapun: show all (FIELD default role)

### Form Penerimaan (`renderReceiving` line ~2893)
- 2 field master (auto-fill dari `ft_mandar_ocean`): `rcvTeraMasterFront`, `rcvTeraMasterRear`
- 2 field aktual (user input): `rcvTeraFront`, `rcvTeraRear`
- Σ selisih field: `rcvTeraDiff` (bold, color-coded) + `rcvTeraBreakdown` (hint)
- Status real-time: OK (≤1) / WARNING (>1,≤3) / CRITICAL (>3)
- Submit via `postToBackend('penerimaan_mo', ...)` → backend auto-fill

### Form Sounding (`renderSounding` line ~2992)
- 2 field master (auto-fetch dari row sebelumnya): `sndIntankMaster`, `sndAktualMaster`
- 2 field aktual (user input): `sndIntank`, `sndAktual`
- Δ breakdown card
- Status field: `sndStatusField` (OK/WARNING/CRITICAL)

### Helper `postToBackend(table, payload)` (line ~773)
- Pakai `fetch('/api/fuel/{table}', {credentials: 'include'})` bukan sb.from (tabel Supabase tidak ada)
- Error handling otomatis

### `loadReceivingRows`, `loadSoundingRows`, `loadDrainageRows`, `loadCleanlinessRows`
- Pakai endpoint publik `/api/v1/master/view/*` (bukan sb.from)
- Tabel dengan kolom status extra (tera_status, sounding_status) di-display di history

### `legacy_renderHistory_1()` & `loadHistory()`
- Tab `transfer` & `monitoring` pakai endpoint publik `/api/v1/master/view/*`
- Tombol Evidence di tiap row → modal preview inline

### `submitSounding`, `submitDrainage`, `submitCleanliness`
- Pakai `postToBackend()` dengan `client_request_id: crypto.randomUUID()`
- Show success toast dengan status info

### `submitReceiving`
- Pakai `postToBackend('penerimaan_mo', payload)` dengan `crypto.randomUUID()` client_request_id
- Show success toast dengan tera_status (OK/WARNING/CRITICAL)

### Event listener
- `updateReceivingCalc`: real-time Σ selisih + status dengan color (green/orange/red)
- `updateReceivingFt`: auto-fill master + clear aktual saat ganti FT
- `updateSoundingCalc` → `loadSoundingMaster`: fetch previous row → set master fields

## Verification

| Test | Result |
|---|---|
| Bundle V7 static reliability (114 tests) | ✅ 114/114 PASS |
| Node syntax check (frontend) | ✅ PASS |
| Backend health endpoint | ✅ 200 |
| Schema contract validation | ✅ OK |
| Sounding TA11 dip 100 | ✅ 74.853 L |
| FT-2609 / FT2609 flowmeter-last | ✅ 56.320 L (strip-aware) |
| 7 form input end-to-end (FIELD user) | ✅ 7/7 success |
| Tera OK (Δ=0.20+0.20) | ✅ Σ=0.4 cm, status=OK |
| Tera WARNING (Δ=2.50+0) | ✅ Σ=2.5 cm, status=WARNING |
| Tera CRITICAL (Δ=5.50+1.80) | ✅ Σ=7.3 cm, status=CRITICAL |
| Sounding auto-fill master | ✅ intank_cm_master, aktual_cm_master |
| Evidence list public | ✅ 2 files per record |
| History view pakai endpoint publik | ✅ rows loaded |

---

# Main Dashboard V8 — 12 Agustus 2026

## Audit Temuan

- Dashboard canonical masih memakai tampilan/logic transfer-only sementara CSS Field V7 sudah tersedia.
- Runtime IIFE di bagian akhir `index.html` pernah menimpa `renderDashboard/loadDashboard/renderDashboardKpi`, sehingga perbaikan di function declaration tidak selalu aktif.
- Frontend belum memakai endpoint agregasi Dashboard backend dan melakukan pola baca yang tidak konsisten.
- Authorization Dashboard backend masih berbasis role list, tidak parity dengan capability matrix.
- `fuel_supply_plan` dicari dengan status `ACTIVE`, padahal schema hanya `DRAFT/APPROVED/DONE`.
- Dashboard history belum memasukkan current Transfer + Flowmeter/HM.
- `previewSounding()` dan `loadSoundingMaster()` membentuk async recursion; ini dapat menghasilkan page error walaupun user sedang berada di Dashboard.
- Flowmeter/HM berhasil submit tetapi tidak selalu me-refresh Dashboard Utama.

## Patch

- Tambah canonical `GET /api/v1/dashboard/overview` untuk Penerimaan, Transfer, Refueling, Flowmeter, HM, Drainage, Sounding, Cleanliness, Closing dan Discrepancy.
- Dashboard route menggunakan `require_capability('dashboard.read')`; history menggunakan `history.read`.
- Dashboard frontend disamakan dengan Field V7: sidebar navy/red, hero/filter, quick input, 8 KPI, trend 7 hari terakhir, readiness, exception, coverage, unified table.
- Default periode MTD + preset Hari Ini/Bulan Berjalan + filter shift.
- Unified table: search, module tab, status filter, evidence, export CSV, pagination 20 baris/page dan hingga 500 recent rows dari server.
- Runtime dashboard override legacy dihapus; function canonical menjadi source runtime.
- Fake anonymous FIELD fallback dihapus.
- Supply SLA hanya membaca plan `APPROVED`/`DONE`.
- Zero-outflow discrepancy menggunakan `discrepancy_zero_outflow_tolerance_l`.
- Recursion Sounding dihapus dan DOM guard ditambah.
- Submit Transfer, Receiving, Flowmeter, HM, Drainage, Sounding, Cleanliness me-refresh Dashboard setelah sukses.
- Backend version: `2026.08.12-main-dashboard-v8`.

## Verification

- `06_tests/run_dashboard_main_checks.py`: 77/77 PASS.
- Python compile: PASS.
- Frontend `node --check`: PASS.
- Browser smoke mock production-shape: Dashboard overview loaded, pagination 20/20/5, filter module/search, quick navigation 7 input pages, no page error, mobile width 390 tanpa horizontal overflow, auth-required path PASS.

---

# Reporting Dashboard V9 — 12 Agustus 2026

## Audit Findings

- Frontend canonical belum memiliki workflow Reporting lengkap.
- Snapshot aktif memiliki 0 `import_batch` dan 0 `fuel_import_row`; audit log menunjukkan legacy import `SS6+SAP` telah dihapus.
- 2 active master unit tanpa alias, 3 orphan alias standard, 4 normalized alias collision.
- Parser import mengasumsikan header row pertama.
- `.xls` support mengimpor `xlrd` tetapi dependency tidak tersedia.
- Periode UI tidak dibandingkan dengan tanggal row file.
- Commit dapat berjalan walaupun terdapat rejected row.
- Re-upload belum memiliki status SUPERSEDED dan belum dilindungi unique active batch + concurrency lock.
- Reconciliation belum mempunyai backend pagination/search penuh.
- Monthly net delta 0 berisiko menyembunyikan mismatch harian tanpa guard.
- GET master role gating tidak parity dengan capability `master.read`.

## Patch V9

- Tambah canonical Reporting menu: Dashboard, Monthly, Reconciliation, Exception Center, Upload, Master.
- Tambah `/api/v1/reporting/overview`, `/monthly`, `/exceptions`, `/master-health`.
- Reporting scope eksplisit SS6/SAP; tidak mengarang ZPME/MB51/MB52.
- Tambah `reporting.read` ke GROUP_LEADER/SUPERVISOR; FIELD tetap tidak mendapat Reporting.
- GET master menggunakan `master.read`; write tetap Admin.
- Parser mencari header sampai 50 row pertama.
- `xlrd` optional at import + dependency 2.0.1 untuk legacy `.xls`.
- Validasi period format dan row-date consistency.
- Alias collision dan missing alias fail-closed.
- Commit partial diblokir.
- Batch lama menjadi SUPERSEDED; advisory lock + unique partial index menjamin satu COMMITTED per source+period.
- Migration baru `05_reporting_reliability_v9.sql`.
- Reconciliation server pagination/search; full CSV export.
- Monthly status tidak menutupi mismatch harian karena net-zero.
- Master table Reporting memakai search + pagination 20/page.

## Verification

- Main Dashboard: 77/77 PASS.
- Reporting static contracts: 86/86 PASS.
- Reporting parser behavior: 9/9 PASS.
- Total: 172/172 assertions PASS.
- Python compile: PASS.
- Frontend JS syntax: PASS.

## Reporting Real Sources V10 — 2026-08-12

- Parser disesuaikan ke file produksi SS6 Refueling `.xls` dan SAP MB51 `.xlsx`.
- `.xls` dibaca langsung dengan xlrd; tidak lagi bergantung LibreOffice conversion.
- SAP 261/262 derive unit dari Order; 201/202 dari Text; signed quantity dipertahankan.
- Source record ID disimpan untuk duplicate guard.
- Alias missing menjadi raw UNMAPPED yang committable namun dikeluarkan dari angka reconciliation.
- Ambiguous alias / duplicate source record tetap hard reject.
- Source date coverage + common coverage diperkenalkan; di luar overlap = OUTSIDE COVERAGE.
- Monthly/Match Rate hanya common coverage.
- Exception Center menampilkan SOURCE_COVERAGE dan IMPORT_ALIAS_UNMAPPED.
- Migration `06_reporting_real_sources_v10.sql` menambahkan metadata source/mapping/batch coverage.

## V12 Consolidated — 2026-08-13

- Consolidated V10.1 transport hotfix and V11 file-authoritative validation.
- Fixed static proxy path/port/health/timeout regression found in FINAL-FULL.
- Added proxy body limit + chunked upload handling.
- Added upload engine readiness and period auto-correction in canonical frontend.
- Added explicit CORS env escape hatch while preserving same-origin preference.
- Removed HTTP first-user SUPER_ADMIN bootstrap; use bootstrap_admin.py.
- Activated failed login lockout and reset-on-success.
- Added schema/security/import-engine health gates.
- Restored coherent `06_tests` release suite and deployment examples.
