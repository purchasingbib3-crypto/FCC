# FCC Reporting V10 — Audit Format Nyata SAP & SS6

Tanggal audit: 12 Agustus 2026

Sumber contoh yang diperiksa:

- `SAP.XLSX`
- `SS6-BIB-IFCU_20260701-20260715 (1).xls`

Audit ini digunakan untuk mengganti asumsi format Reporting V9 dengan kontrak file produksi yang nyata.

## 1. Profil SS6 aktual

Sheet utama: `SS6 Refueling`

Header aktual:

1. Transaction ID
2. Unit
3. Material
4. Date
5. Shift
6. Time
7. Vol
8. HM
9. Gas Station
10. Location
11. FM
12. Input By
13. Created
14. Time

Temuan:

- ±25.537 transaksi aktual.
- Coverage tanggal: **01 Juli 2026 – 15 Juli 2026**.
- Shift 1: ±13.097 row.
- Shift 2: ±12.440 row.
- `Transaction ID` unik pada sample yang diperiksa.
- Contoh ID: `DA03/FLO/2607/FUEL/00001`.
- Volume menggunakan decimal comma pada export, contoh `40,0`.
- Unit contoh: `BS-1220`.
- Gas Station contoh: `FS10`.
- Material pada sample SS6: `1300000000`.

Implikasi: parser tidak boleh mengasumsikan source SS6 hanya `DATE + UNIT + VOL`; Transaction ID harus disimpan untuk audit/dedupe dan `.xls` harus didukung sebagai BIFF8 asli.

## 2. Profil SAP aktual

Sheet: `Sheet1`

Header aktual mencakup:

`Material, Material Description, Plant, Storage Location, Movement Type, Special Stock, Material Document, Material Doc.Item, Posting Date, Qty in Un. of Entry, Unit of Entry, Amount in LC, Document Header Text, User name, Purchase Order, Sales Order Item, Sales Order, Order, Movement Type Text, Document Date, Reservation, Cost Center, Reference, Text, Time of Entry, Entry Date`

Temuan:

- ±39.799 material movement row aktual.
- Coverage tanggal: **02 Juli 2026 – 31 Juli 2026**.
- Movement Type:
  - 261: ±31.123 row
  - 201: ±8.052 row
  - 202: ±565 row
  - 262: ±59 row
- Quantity memakai signed movement: issue negatif, reversal positif.
- Material sample dominan: `1300000006` / `SOLAR B50`.
- `Material Document + Material Doc.Item` unik pada sample yang diperiksa.
- Tidak ada kolom `UNIT SAP` langsung pada file ini.

### Rule derive unit SAP yang ditemukan

**Movement 261/262** → gunakan kolom `Order`.

Contoh:

- `D85190-1201B` → `D85190`
- `H78185-1201B` → `H78185`
- `LT52506-1201` → `LT52506`
- suffix yang ditemukan: `-1201B`, `-1201`, `-120B`, `-1201D`, `-1201BT`, termasuk variasi trailing `.`.

**Movement 201/202** → gunakan kolom `Text`.

Contoh:

- `LV-5085.KM-55048` → `LV-5085`
- `BUS-1112.KM-216936` → `BUS-1112`
- `EX8293B` → `EX8293B`

`Document Header Text` pada 201/202 sering berisi generic `UPLOAD MIGO`, sehingga bukan source unit yang aman.

## 3. Masalah pada Reporting V9 yang terbukti dari file nyata

1. Parser SAP V9 meminta direct `UNIT SAP/UNIT`; file produksi MB51 tidak memilikinya.
2. V9 belum derive unit dari `Order/Text` berdasarkan Movement Type.
3. V9 belum menyimpan source transaction/document ID untuk duplicate guard.
4. V9 menganggap alias tidak ditemukan sebagai hard reject seluruh batch.
5. Dengan master snapshot bundle saat ini, file nyata masih mempunyai gap alias beberapa persen; hard reject seluruh batch membuat workflow upload tidak praktis.
6. Coverage SS6 dan SAP berbeda. Bila semua tanggal Juli langsung dibandingkan, 1 Juli dan 16–31 Juli menghasilkan `HANYA source` palsu walaupun sebenarnya source lawan tidak mencakup tanggal tersebut.
7. UI V9 masih menjelaskan SAP sebagai file `POSTING DATE + UNIT + QTY`, tidak sesuai file produksi.

## 4. Patch V10

### Parser

- `SS6_REFUELING` dideteksi dari header nyata.
- `SAP_MB51` dideteksi dari signature MB51.
- `.xls` dibaca langsung dengan `xlrd`.
- 261/262 derive unit dari Order.
- 201/202 derive unit dari Text.
- Signed SAP quantity dipertahankan.
- Movement lain di luar 201/202/261/262 tidak masuk reconciliation fuel.
- Source record ID disimpan:
  - SS6: Transaction ID
  - SAP: Material Document + Item

### Mapping

- MAPPED → ikut reconciliation.
- UNMAPPED → boleh di-commit sebagai raw exception; `unit_standar=NULL`; **tidak ikut angka reconciliation**.
- AMBIGUOUS alias → technical reject, Commit blocked.
- Duplicate source record → technical reject, Commit blocked.
- Tidak ada auto-guess mapping.

### Coverage

Dengan dua contoh file ini:

- SS6 coverage: **01–15 Jul 2026**
- SAP coverage: **02–31 Jul 2026**
- Common/comparable coverage: **02–15 Jul 2026**

Row di luar 02–15 Jul diberi `OUTSIDE COVERAGE`, bukan `HANYA SS6/HANYA SAP`.

Match Rate dan Monthly Report hanya memakai common coverage.

### Database

Migration baru: `01_database/06_reporting_real_sources_v10.sql`

Metadata baru meliputi:

- source_format
- source_record_id
- movement_type
- material
- uom
- mapping_status
- date_from/date_to batch
- baris_mapped/baris_unmapped

### UI

Upload Data sekarang menampilkan:

- format terdeteksi;
- date coverage;
- total rows;
- mapped;
- unmapped;
- technical reject;
- mapping coverage %;
- top unmapped aliases.

Dashboard Reporting menampilkan source coverage, common coverage, unmapped count, dan mengakui **SAP MB51 sudah didukung**, sementara ZPME16/MB52 tetap belum aktif.

## 5. Mapping coverage pada master snapshot bundle

Dari profiling audit terhadap master alias yang tersimpan di bundle saat ini:

- SS6: sekitar **97,8%** row dapat dipetakan; sekitar **563 row** belum terpetakan.
- SAP: sekitar **95,2%** row dapat dipetakan; sekitar **1.901 row** belum terpetakan.

Contoh alias yang muncul pada gap master antara lain `D85196`, beberapa seri `SRT137–SRT177`, `GD856`, `D85197`, dan lainnya.

Angka ini adalah hasil profiling sample terhadap snapshot master bundle, bukan keputusan mapping. V10 sengaja tidak menebak unit standar untuk alias tersebut.

## 6. Test lokal yang dijalankan

- Python backend compile: PASS.
- Main Dashboard regression: **77/77 PASS**.
- Reporting V10 real-source static contract: **68/68 PASS**.
- SAP derive-unit helper:
  - 261 `D85190-1201B` → `D85190`: PASS
  - 262 `H78185-1201BT` → `H78185`: PASS
  - 201 `LV-5085.KM-55048` → `LV-5085`: PASS
  - 202 `EX8293B` → `EX8293B`: PASS
- Frontend JavaScript syntax: PASS.

Full runtime parser acceptance untuk `.xls` harus dijalankan di VPS setelah `pip install -r 06_env/requirements.txt` karena environment audit lokal tidak memiliki package `xlrd` aktif. Format file SS6/SAP sendiri sudah diprofiling secara read-only pada audit ini.

## 7. Gate Hermes setelah deploy

Hermes belum boleh menyatakan DONE sebelum:

1. migration 06 sukses;
2. xlrd tersedia;
3. Validate file SS6 nyata terdeteksi `SS6_REFUELING`;
4. Validate SAP nyata terdeteksi `SAP_MB51`;
5. technical reject = 0;
6. unmapped ditampilkan, tidak dibuang dan tidak masuk reconciliation;
7. kedua source COMMITTED tunggal;
8. common coverage tampil **02–15 Jul** untuk pasangan contoh ini;
9. sample 201/202/261/262 dicocokkan raw file → DB → reconciliation;
10. browser/mobile/backend log bersih.
