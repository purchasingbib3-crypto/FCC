# FCC PPA-BIB — Reporting Dashboard V9 Audit & Patch Report

**Audit date:** 12 Agustus 2026  
**Baseline:** Main Dashboard V8 / `fcc-bundle-final2.zip` lineage  
**Primary scope:** Dashboard Reporting, Monthly Report, Reconciliation, Exception Center, Upload Data, Master Data Reporting

## Executive Summary

Reporting pada baseline sebelumnya belum menjadi workflow production yang utuh. Backend sudah memiliki bagian import/reconciliation, tetapi frontend canonical tidak memiliki dashboard reporting terpadu dan beberapa kontrak data dapat menghasilkan report kosong, mapping ambigu, atau batch parsial.

V9 membuat satu workflow reporting yang konsisten dengan UI Dashboard Field/Main Dashboard dan mengunci integritas data sebelum batch dipakai reconciliation.

### Scope yang benar-benar didukung bundle

Schema aktif saat ini hanya mendukung source **SS6** dan **SAP** pada `fuel_import_row.sumber`. Tidak ada kontrak tabel/kolom aktif untuk ZPME, MB51, atau MB52. Karena itu V9 **tidak mengarang dukungan ZPME/MB51/MB52**. Dashboard menyatakan scope ini secara eksplisit.

## Audit Data Snapshot

Audit dilakukan terhadap `01_database/02_data_only.sql` pada baseline terbaru.

- `import_batch`: **0 row aktif pada snapshot**.
- `fuel_import_row`: **0 row pada snapshot**.
- `master_unit`: **1.871 row aktif**.
- `unit_alias`: **1.891 row aktif**, mewakili 1.872 `unit_standar` unik.
- `voucher_bib`: **3 row**.

Audit trail masih menunjukkan proses import reconciliation legacy pada 3–4 Agustus 2026, termasuk batch `SS6+SAP` periode `JUNI 2026`, tetapi batch tersebut kemudian dihapus. Artinya dump aktif yang diberikan memang tidak memiliki source reconciliation yang dapat langsung ditampilkan. Setelah V9 deploy, SS6 dan SAP harus diupload ulang sebagai **dua source terpisah** untuk periode yang benar sebelum reconciliation dapat menghasilkan angka.

### Master alias findings

Ditemukan **2 master unit aktif tanpa alias aktif**:

- `PENERIMAAN 202`
- `TT-1621`

Ditemukan **3 alias aktif yang menunjuk standard yang tidak ada pada master unit aktif**:

- `014BIBKABPPAVI2026`
- `DTSANY`
- `WM0465`

Ditemukan **4 collision setelah normalisasi alias**. Normalisasi mengabaikan spasi, `-`, `_`, `.`, `/`, dan karakter separator serupa:

1. `BIBMNKFT01` → `BIB-MNK-FT- 01` dan `BIB-MNK-FT-01`
2. `BIBMNKMMU101` → `BIB MNK MMU 101` dan `BIBMNKMMU101`
3. `TR2032` → `TR 2032` dan `TR-2032`
4. `TT1618` → `TT 1618` dan `TT-1618`

Baseline lama berpotensi memilih salah satu mapping secara tidak deterministik. V9 menolak alias ambigu dan tidak pernah menebak unit.

## Temuan Logic yang Dipatch

### 1. Reporting UI tidak tersedia pada frontend canonical

Frontend canonical sekarang mempunyai urutan menu:

1. Dashboard Reporting
2. Monthly Report
3. Reconciliation
4. Exception Center
5. Upload Data
6. Master Data

UI menggunakan design system yang sama dengan Dashboard Field/Main Dashboard: sidebar navy, active red PPA, white cards, Poppins, KPI soft color, filter ringkas, responsive mobile, search/filter/pagination pada tabel.

### 2. Role frontend/backend tidak parity untuk master/reporting

`SUPERVISOR` sebelumnya mempunyai `master.read` di capability matrix tetapi GET master backend masih memakai hardcoded roles. GET master sekarang menggunakan `master.read` dan Reporting menggunakan `reporting.read`.

- `GROUP_LEADER`: reporting read.
- `SUPERVISOR`: reporting read.
- `FIELD`: tidak mendapat menu reporting.
- Upload dan perubahan master tetap hanya ADMIN/SUPER_ADMIN.

### 3. Mapping alias dapat ambigu

Import sekarang membangun normalized alias → set of standards. Bila satu normalized alias menunjuk lebih dari satu standard, row ditolak sebagai `AMBIGUOUS_UNIT_ALIAS`. `UNIT_ALIAS_NOT_FOUND` juga tetap fail-closed; unit tidak ditebak.

Create/update Unit Alias juga divalidasi server. Collision menghasilkan HTTP 409.

### 4. File valid dapat gagal karena header tidak berada di row pertama

Parser lama menganggap row pertama adalah header. Export SS6/SAP sering memiliki title/filter metadata di atas tabel. Parser V9 mencari header valid pada **50 row pertama** dan menyimpan `source_row` asli.

### 5. `.xls` dapat membuat startup backend gagal

`xlsx_import.py` melakukan `import xlrd`, tetapi dependency tersebut tidak ada di `requirements.txt`. V9:

- membuat import `xlrd` optional sehingga `.xlsx` tidak mematikan startup;
- menambah `xlrd==2.0.1` pada requirements untuk dukungan legacy `.xls`;
- mempertahankan LibreOffice conversion sebagai jalur pertama bila tersedia.

### 6. Periode UI tidak diverifikasi terhadap tanggal file

Validate/Commit sekarang mensyaratkan `YYYY-MM` dan seluruh tanggal row harus masuk periode yang dipilih. File Agustus tidak dapat di-commit sebagai Juli.

### 7. Partial batch dapat menjadi source report

Baseline membolehkan valid rows di-commit walaupun masih ada rejected rows. V9 **memblokir Commit jika rejected_rows > 0**. Admin harus memperbaiki master/file lalu Validate ulang.

Frontend juga mengunci tombol Commit sampai:

- file + periode yang sama sudah Validate;
- `valid_rows > 0`;
- `rejected_rows = 0`.

### 8. Re-upload source+period belum memiliki status histori yang jelas

V9 menambahkan status `SUPERSEDED`. Saat source+period diupload ulang:

- batch COMMITTED lama → SUPERSEDED;
- batch baru → COMMITTED;
- histori lama tetap auditable.

Migration `05_reporting_reliability_v9.sql` juga menormalkan duplicate COMMITTED lama dengan mempertahankan batch terbaru sebagai active.

### 9. Race condition dapat menghasilkan dua batch COMMITTED

Commit menggunakan PostgreSQL transaction advisory lock per `source+period`, dan database memiliki partial unique index:

`ux_import_batch_active_source_period (sumber, periode) WHERE status='COMMITTED'`.

Dengan demikian hanya satu batch active per source/periode yang dapat menjadi source reconciliation.

### 10. Reconciliation dibatasi 5.000 row tanpa pagination sebenarnya

Endpoint `/api/v1/reconciliation` sekarang mempunyai `limit`, `offset`, `q`, filter status/unit, serta total dan summary berdasarkan seluruh hasil filter. UI menggunakan server pagination 20/page. Export CSV melakukan loop per 5.000 row sehingga export tidak hanya page yang sedang terlihat.

### 11. Monthly net-zero dapat menyembunyikan mismatch harian

Monthly Report mengakumulasi jumlah hari exception. Unit hanya `MATCH` bila **tidak ada mismatch harian sama sekali**. Jadi +100 L di satu hari dan -100 L di hari lain tidak berubah hijau hanya karena delta bulanan net = 0.

### 12. Exception tersebar dan sulit dimonitor

Exception Center V9 menggabungkan:

- `RECONCILIATION`
- `IMPORT_BATCH`
- `SOURCE_MISSING`
- `MASTER_ALIAS_MISSING`
- `MASTER_ALIAS_ORPHAN`
- `MASTER_ALIAS_COLLISION`
- `VOUCHER`

Critical selalu diurutkan lebih dahulu. Tersedia search, type, severity, pagination, dan CSV.

## UI/UX Reporting V9

### Dashboard Reporting

Menampilkan:

- SS6 volume
- SAP volume
- delta
- match rate
- exception
- master issue
- source readiness SS6/SAP
- master health
- daily trend
- recent exception
- batch history

Nilai delta 0/MATCH memakai indikator hijau.

### Monthly Report

Filter periode, search unit, kategori, vendor, status; KPI, tabel 20/page, dan export CSV.

### Reconciliation

Tabel harian SS6 ↔ SAP dengan tanggal, unit, vendor, kategori, shift SS6, SS6, SAP, delta, status; server search/filter/pagination dan full CSV export.

### Upload Data

Dua card terpisah untuk SS6 dan SAP. Workflow operator dibuat jelas: **1. Validate → 2. Commit**. Reject reason dan sample reject ditampilkan tanpa perlu membuka log backend.

### Master Data

Master Unit dan Unit Alias memakai search, pagination 20/page, master health strip, edit/add untuk Admin, read-only untuk role reporting lain. Collision alias ditolak server.

## Database Migration V9

Untuk database existing setelah migration V7, jalankan:

```bash
psql -d fcc -f 01_database/05_reporting_reliability_v9.sql
```

Migration menambah `SUPERSEDED`, menormalkan duplicate COMMITTED lama, dan membuat unique active source/period index.

## Verification

Final gate dari bundle V9:

- Main Dashboard contracts: **77/77 PASS**
- Reporting Dashboard contracts: **86/86 PASS**
- Reporting parser behavior: **9/9 PASS**
- Total assertions: **172 PASS / 0 FAIL**
- Python compile: **PASS**
- Frontend JavaScript syntax: **PASS**

Parser behavior test mencakup SS6/SAP dengan metadata row di atas header, locale number, negative SAP quantity, shift/SLOC, serta missing-header error.

## Batas Verifikasi Workspace

Workspace audit tidak memiliki PostgreSQL production maupun credential SS6/SAP live. Karena itu status production tidak boleh dinyatakan selesai hanya berdasarkan static/parser tests. Post-deploy acceptance harus mencakup migration V9, health/schema gate, upload SS6 dan SAP nyata, Validate/Commit, re-upload, reconciliation, exception, dan role/capability live.
