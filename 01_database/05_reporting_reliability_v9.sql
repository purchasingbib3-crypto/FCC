-- FCC Reporting Dashboard V9 reliability migration
BEGIN;

ALTER TABLE fcc.import_batch DROP CONSTRAINT IF EXISTS import_batch_status_check;
ALTER TABLE fcc.import_batch
  ADD CONSTRAINT import_batch_status_check
  CHECK (status = ANY (ARRAY['UPLOADED'::text,'VALIDATED'::text,'COMMITTED'::text,'SUPERSEDED'::text,'REJECTED'::text]));

-- If an older deployment already has multiple COMMITTED rows for the same source+period,
-- keep the newest active and preserve the older rows as audit history.
WITH ranked AS (
  SELECT id, row_number() OVER (PARTITION BY sumber, periode ORDER BY imported_at DESC, id DESC) AS rn
  FROM fcc.import_batch
  WHERE status = 'COMMITTED'
)
UPDATE fcc.import_batch ib
SET status = 'SUPERSEDED'
FROM ranked r
WHERE ib.id = r.id AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS ux_import_batch_active_source_period
  ON fcc.import_batch (sumber, periode)
  WHERE status = 'COMMITTED';

COMMIT;
