# FCC V12.2 — Canonical SS6–SAP Volume Patch

## Basis

Patch ini dibuat dari baseline V12.1 dan hasil audit normalisasi SS6–SAP terbaru yang menunjukkan kontrak sign SAP MB51 harus dipisahkan antara nilai source dan nilai pembanding.

## Perubahan utama

1. `fuel_import_row` sekarang memiliki:
   - `quantity_source_l`: nilai sumber persis;
   - `volume_net_l`: nilai comparable untuk reconciliation.
2. SAP MB51:
   - issue 201/261 yang negatif menjadi volume net positif;
   - reversal 202/262 yang positif menjadi volume net negatif.
3. SS6: source quantity = volume net.
4. `liter` tetap dipertahankan untuk backward-compatible audit, tetapi tidak lagi dipakai sebagai sumber angka reconciliation.
5. Reconciliation API, Reporting Dashboard, Monthly Report, voucher validation, dan `fcc.v_rekonsiliasi` memakai `volume_net_l`.
6. Toleransi MATCH diselaraskan menjadi `abs(delta) <= 0.01 L`.
7. Mapping status sekarang `MAPPED | UNMAPPED | AMBIGUOUS`.
8. UNMAPPED dan AMBIGUOUS dapat di-commit sebagai raw exception, namun tidak masuk numeric reconciliation.
9. Alias lookup memakai normalized key, tetapi `unit_standar` yang disimpan adalah canonical master code, bukan kode yang sudah dibuang separatornya.
10. Batch menyimpan `baris_ambiguous` dan frontend menampilkan Mapped / Unmapped / Ambiguous / Technical Reject secara terpisah.

## Migration

Wajib jalankan:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 01_database/08_reporting_canonical_volume_v12_2.sql
```

Migration 08 melakukan backfill aman terhadap batch lama:

- `quantity_source_l = liter`;
- SAP_MB51 `volume_net_l = -liter`;
- legacy SAP non-MB51 mempertahankan behavior positif dengan `abs(liter)`;
- SS6 `volume_net_l = liter`.

## Acceptance gate

Sebelum Commit production:

1. `/api/v1/health` harus menunjukkan:
   - `schema_contract.ok = true`;
   - `reporting_import.commit_ready = true`;
   - `reporting_import.quantity_contract = SOURCE_SIGNED_PLUS_CANONICAL_NET`;
   - `reporting_import.reconciliation_quantity = volume_net_l`.
2. Validate SAP harus menunjukkan source total signed dan net total dengan arah berlawanan untuk MB51.
3. Reversal 202/262 harus mengurangi net usage, bukan diubah menjadi positif oleh `abs(sum(...))`.
4. AMBIGUOUS alias harus muncul di Exception Center dan tidak boleh masuk MATCH/SELISIH numeric.
5. Re-upload source+period tetap menghasilkan satu batch COMMITTED aktif.
6. Reconciliation dan Monthly Report harus memakai tolerance 0.01 L.
