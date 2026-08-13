# V12.4 — Merge master_unit + unit_alias Menjadi 1 Tabel

## Latar Belakang

V12.3 menggunakan 2 tabel terpisah:
- `master_unit` (1,871 rows): kode, nama, vendor_kode, kategori, status
- `unit_alias` (1,891 rows): unit_standar, alias_ss6, alias_sap, vendor_kode, kategori, status

Setiap unit bisa punya 1+ alias. Dan setiap alias punya reference ke master_unit via FK.

## Perubahan V12.4

V12.4 menggabungkan keduanya menjadi **1 tabel** `master_unit` dengan kolom ARRAY:

```sql
CREATE TABLE fcc.master_unit (
    kode        text PRIMARY KEY,
    nama        text NOT NULL,
    vendor_kode text NOT NULL,
    kategori    text NOT NULL,
    status      text NOT NULL,
    alias_ss6   text[] DEFAULT NULL,    -- ARRAY of SS6 aliases
    alias_sap   text[] DEFAULT NULL,    -- ARRAY of SAP aliases
    alias_count integer DEFAULT 0,
    created_at  timestamp with time zone,
    updated_at  timestamp with time zone
);
```

## Migration

File: `01_database/09_merge_unit_alias_v12_4.sql`

Proses:
1. CREATE `master_unit_v124` (temporary)
2. INSERT aggregated data dari `master_unit` + `unit_alias`
3. Add orphan rows dari `refuelling.unit_kode` (3 orphan)
4. DROP `unit_alias`, `master_unit`
5. RENAME `master_unit_v124` → `master_unit`
6. Restore FK `refuelling.unit_kode` → `master_unit.kode`
7. Restore triggers

## Hasil

- **1874 rows** (1,871 master_unit + 3 orphan dari refuelling)
- **1891 alias_values** (1872 SS6 + 1869 SAP)
- 1 unit = 1 row (multiple aliases per row)

## Backend Impact

| File | Perubahan |
|------|----------|
| `schema_contract.py` | Use `pg_attribute` + `c.relname` (for privilege); tambah `alias_ss6`, `alias_sap`, `alias_count` |
| `routers/master.py` | Hapus `unit-aliases` MasterSpec; `_validate_unit_alias_contract` jadi no-op |
| `routers/imports.py` | `_alias_map` pakai `unnest()` untuk get individual aliases |
| `routers/reporting.py` | `_master_diagnostics` pakai master_unit |
| `routers/ss6.py` | `_master_maps` pakai master_unit |
| `routers/fuel_bridge.py` | Hapus `"unit_alias": "unit_alias"` mapping |

## Alias Lookup Query (V12.4)

```sql
SELECT mu.kode AS unit_standar,
       mu.nama,
       mu.vendor_kode,
       mu.kategori,
       alias_element.alias_value,
       alias_element.alias_kind
FROM fcc.master_unit mu
CROSS JOIN LATERAL (
    SELECT alias_ss6 AS alias_value, 'ss6' AS alias_kind FROM unnest(mu.alias_ss6) AS alias_ss6
    UNION ALL
    SELECT alias_sap AS alias_value, 'sap' AS alias_kind FROM unnest(mu.alias_sap) AS alias_sap
) alias_element
WHERE mu.status = 'ACTIVE'
```

## Test Result

| Test | Hasil |
|------|-------|
| Migration apply | ✅ 1874 rows + 3 orphan |
| Schema contract | ✅ `ok: true, missing_tables: 0` |
| Validate SS6 (1 mapped + 1 unmapped) | ✅ 27 ms |
| Alias lookup `BS-1220` → `BUS-1220` | ✅ MAPPED |
| Parser engine | CALAMINE |
| Token + cache | `commit_mode: TOKEN_NO_REPARSE` |

## Production Acceptance

1. Apply migration `09_merge_unit_alias_v12_4.sql`
2. Restart backend (FastAPI akan auto-detect `schema_contract.ok=true`)
3. Verify `/api/v1/health`:
   - `schema_contract.ok: true, missing_tables: 0`
   - `commit_ready: true`
4. Test validate SS6/SAP dengan file kecil
5. Commit file besar → verify `commit_cache_hit: true`

## Known Limitations

- Tidak ada backward compatibility dengan `unit_alias` table
- Frontend masih reference `unit_alias` di state (perlu update nanti)
- `v_rekonsiliasi` view perlu di-recreate manual setelah merge
