-- FCC V12.1 — Reporting Commit reliability
-- Raw import rows are immutable detail. The import_batch row is the audited business event.
BEGIN;

-- Per-row audit on 25k–40k source rows doubles write volume and can make a valid
-- reconciliation commit fail/time out. Preserve batch-level audit, remove raw-row audit.
DROP TRIGGER IF EXISTS trg_fuel_import_row_audit ON fcc.fuel_import_row;

-- Keep only one active batch per source/period. Migration 05 normally created this;
-- repeat defensively for deployments that applied patches out of order.
WITH ranked AS (
  SELECT id, row_number() OVER (PARTITION BY sumber, periode ORDER BY imported_at DESC, id DESC) AS rn
  FROM fcc.import_batch
  WHERE status='COMMITTED'
)
UPDATE fcc.import_batch ib
SET status='SUPERSEDED'
FROM ranked r
WHERE ib.id=r.id AND r.rn>1;

CREATE UNIQUE INDEX IF NOT EXISTS ux_import_batch_active_source_period
  ON fcc.import_batch(sumber, periode)
  WHERE status='COMMITTED';

COMMIT;
