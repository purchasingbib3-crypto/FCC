-- FCC V12.2 — Canonical SAP/SS6 quantity semantics + ambiguous alias preservation
-- Source quantity remains auditable; reconciliation compares volume_net_l only.
BEGIN;

ALTER TABLE fcc.fuel_import_row
  ADD COLUMN IF NOT EXISTS quantity_source_l numeric(14,3),
  ADD COLUMN IF NOT EXISTS volume_net_l numeric(14,3);

-- Backfill legacy rows safely:
-- * SS6: source and comparable volume are identical.
-- * SAP_MB51: source is signed MB51; comparable usage reverses sign.
-- * older SAP_DIRECT / unknown legacy SAP: retain historical positive-usage behavior.
UPDATE fcc.fuel_import_row
SET quantity_source_l = COALESCE(quantity_source_l, liter),
    volume_net_l = COALESCE(
      volume_net_l,
      CASE
        WHEN sumber='SAP' AND source_format='SAP_MB51' THEN -liter
        WHEN sumber='SAP' THEN abs(liter)
        ELSE liter
      END
    )
WHERE quantity_source_l IS NULL OR volume_net_l IS NULL;

ALTER TABLE fcc.fuel_import_row
  ALTER COLUMN quantity_source_l SET NOT NULL,
  ALTER COLUMN volume_net_l SET NOT NULL;

ALTER TABLE fcc.fuel_import_row DROP CONSTRAINT IF EXISTS fuel_import_row_mapping_status_check;
ALTER TABLE fcc.fuel_import_row
  ADD CONSTRAINT fuel_import_row_mapping_status_check
  CHECK (mapping_status IN ('MAPPED','UNMAPPED','AMBIGUOUS'));

ALTER TABLE fcc.import_batch
  ADD COLUMN IF NOT EXISTS baris_ambiguous integer NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_fuel_import_row_mapping_exception
  ON fcc.fuel_import_row(batch_id, mapping_status, alias_unit)
  WHERE mapping_status IN ('UNMAPPED','AMBIGUOUS');

-- Database view must follow the same semantic contract as API reporting.
CREATE OR REPLACE VIEW fcc.v_rekonsiliasi AS
 SELECT r.tanggal,
    r.unit_standar,
    u.nama AS unit_nama,
    u.vendor_kode,
    u.kategori,
    r.ss6_l,
    r.sap_l,
    round((COALESCE(r.sap_l, (0)::numeric) - COALESCE(r.ss6_l, (0)::numeric)), 3) AS delta_l,
    abs(round((COALESCE(r.sap_l, (0)::numeric) - COALESCE(r.ss6_l, (0)::numeric)), 3)) AS abs_delta_l,
        CASE
            WHEN (r.ss6_l IS NULL) THEN 'HANYA SAP'::text
            WHEN (r.sap_l IS NULL) THEN 'HANYA SS6'::text
            WHEN (abs((r.sap_l - r.ss6_l)) <= 0.01) THEN 'MATCH'::text
            ELSE 'SELISIH'::text
        END AS status
   FROM (( SELECT fuel_import_row.tanggal,
            fuel_import_row.unit_standar,
            sum(fuel_import_row.volume_net_l) FILTER (WHERE (fuel_import_row.sumber = 'SS6'::text)) AS ss6_l,
            sum(fuel_import_row.volume_net_l) FILTER (WHERE (fuel_import_row.sumber = 'SAP'::text)) AS sap_l
           FROM fcc.fuel_import_row
          WHERE ((fuel_import_row.unit_standar IS NOT NULL) AND (fuel_import_row.mapping_status = 'MAPPED'::text))
          GROUP BY fuel_import_row.tanggal, fuel_import_row.unit_standar) r
     LEFT JOIN fcc.master_unit u ON ((u.kode = r.unit_standar)));

COMMIT;
