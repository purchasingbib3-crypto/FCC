# V12.3.1 — Frontend Fixes for FAST Import

Branch: `fix-v12-3-1-fast-frontend`
Purpose: Frontend improvements untuk V12.3 FAST Import + UX fixes

## Frontend Fixes Applied

### 1. Photo Source Tabs (Kamera / Galeri / File)
- ✅ Form Transfer: FM Awal, FM Akhir
- ✅ Form Flowmeter: FM IN, FM OUT
- ✅ Form HM: HM Foto
- ✅ Form Penerimaan: Sample, Hasil
- ✅ Form Sounding: Intank + 5 Aktual
- ✅ Form Drainage: FM, Hasil
- ✅ Form Cleanliness: Before, After

### 2. Tera Selisih Signed Color
- ✅ Plus (merah) = aktual > master > 0
- ✅ Minus (merah) = aktual < master
- ✅ 0 (hijau) = MATCH
- ✅ Warning/Error status

### 3. FAST Import UI (V12.3)
- ✅ Show `validation_token` di response Validate
- ✅ Show `commit_mode: TOKEN_NO_REPARSE`
- ✅ Show `timings_ms` per stage
- ✅ Show `parser_engine: CALAMINE`
- ✅ Show `commit_cache_hit: true` after Commit

### 4. Sounding to Liter (Public Endpoint)
- ✅ Replace `sb.rpc('fuel_get_tera_volume', ...)` dengan `/api/v1/sounding/volume`
- ✅ Support MAINTANK (TA11/TA12/FS10) + Fuel Truck
- ✅ Public endpoint (no auth required)

### 5. Auto-fill petugas_name
- ✅ Backend `insert_rows` auto-fill `petugas_name`, `nama_driver`, `nrp`
- ✅ Frontend `getTeraVolumeDb` pakai public endpoint

### 6. Dropdown FT Mandar Ocean (Penerimaan)
- ✅ Pakai `<select>` simple (sama dengan `drnAsset`, `clnAsset`)
- ✅ Search box filter

### 7. Logo Routing
- ✅ `/` → V7 Reporting Dashboard (preview)
- ✅ `/field/` → V8 Field Dashboard

## Files Updated

| File | Size | Changes |
|------|------|---------|
| `03_frontend/index.html` | 347 KB | V12.4 frontend + Hermes patches |
| `03_frontend/app.js` | 70 KB | postToBackend helper |
| `03_frontend/fcc-client.js` | 12 KB | Supabase shim |
| `03_frontend/styles.css` | 13 KB | Photo source tabs, color-coded signed |

## Production Deployment

```bash
# Update branch
git checkout fix-v12-3-1-fast-frontend
git pull origin fix-v12-3-1-fast-frontend

# Copy frontend ke VPS
cp 03_frontend/* /home/ubuntu/fcc-field/

# Hard refresh browser (Ctrl+Shift+R)
# https://fogdcbib.web.id/field/
```

## Browser Test

1. Open https://fogdcbib.web.id/field/
2. Login dengan NRP (e.g., 81230108 / 81230108)
3. Buka menu **Transfer** → tab **📷 Kamera** / **🖼️ Galeri** / **📁 File** untuk pilih foto
4. Submit transfer → verify `petugas_name: BAGAS SATRIAN HAKIM` auto-fill
5. Buka menu **Reporting** → Upload SS6 → verify `parser_engine: CALAMINE` + `timings_ms`
6. Commit → verify `commit_cache_hit: true`
