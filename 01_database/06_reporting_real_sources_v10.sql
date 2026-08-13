-- FCC Reporting V10 — real SS6 + SAP MB51 source contract
BEGIN;

ALTER TABLE fcc.import_batch ADD COLUMN IF NOT EXISTS source_format text;
ALTER TABLE fcc.import_batch ADD COLUMN IF NOT EXISTS date_from date;
ALTER TABLE fcc.import_batch ADD COLUMN IF NOT EXISTS date_to date;
ALTER TABLE fcc.import_batch ADD COLUMN IF NOT EXISTS baris_mapped integer NOT NULL DEFAULT 0;
ALTER TABLE fcc.import_batch ADD COLUMN IF NOT EXISTS baris_unmapped integer NOT NULL DEFAULT 0;

ALTER TABLE fcc.fuel_import_row ADD COLUMN IF NOT EXISTS source_format text;
ALTER TABLE fcc.fuel_import_row ADD COLUMN IF NOT EXISTS source_record_id text;
ALTER TABLE fcc.fuel_import_row ADD COLUMN IF NOT EXISTS movement_type text;
ALTER TABLE fcc.fuel_import_row ADD COLUMN IF NOT EXISTS material text;
ALTER TABLE fcc.fuel_import_row ADD COLUMN IF NOT EXISTS uom text;
ALTER TABLE fcc.fuel_import_row ADD COLUMN IF NOT EXISTS mapping_status text NOT NULL DEFAULT 'MAPPED';

ALTER TABLE fcc.fuel_import_row DROP CONSTRAINT IF EXISTS fuel_import_row_mapping_status_check;
ALTER TABLE fcc.fuel_import_row
  ADD CONSTRAINT fuel_import_row_mapping_status_check
  CHECK (mapping_status IN ('MAPPED','UNMAPPED'));

CREATE UNIQUE INDEX IF NOT EXISTS ux_fuel_import_row_source_record
  ON fcc.fuel_import_row(batch_id, source_record_id)
  WHERE source_record_id IS NOT NULL AND source_record_id <> '';

CREATE INDEX IF NOT EXISTS ix_fuel_import_row_mapping_status
  ON fcc.fuel_import_row(batch_id, mapping_status, tanggal);

CREATE INDEX IF NOT EXISTS ix_fuel_import_row_source_format
  ON fcc.fuel_import_row(batch_id, source_format, movement_type);

COMMIT;
