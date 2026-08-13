-- FCC V12.4 — Merge master_unit + unit_alias into single table
-- V12.4 migration: master_unit + unit_alias -> master_unit dengan ARRAY alias_ss6, alias_sap

BEGIN;

-- Create new unified table
CREATE TABLE fcc.master_unit_v124 (
    kode            text PRIMARY KEY,
    nama            text NOT NULL,
    vendor_kode     text NOT NULL,
    kategori        text NOT NULL,
    status          text NOT NULL DEFAULT 'ACTIVE',
    alias_ss6       text[] DEFAULT NULL,
    alias_sap       text[] DEFAULT NULL,
    alias_count     integer DEFAULT 0,
    created_at      timestamp with time zone NOT NULL DEFAULT now(),
    updated_at      timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT master_unit_v124_status_check CHECK (status IN ('ACTIVE', 'INACTIVE'))
);

-- Populate dari master_unit + unit_alias
INSERT INTO fcc.master_unit_v124 (kode, nama, vendor_kode, kategori, status, alias_ss6, alias_sap, alias_count)
SELECT 
    mu.kode,
    mu.nama,
    mu.vendor_kode,
    mu.kategori,
    mu.status,
    (
        SELECT ARRAY_AGG(ua.alias_ss6 ORDER BY ua.id)
        FROM fcc.unit_alias ua
        WHERE ua.unit_standar = mu.kode
          AND ua.alias_ss6 IS NOT NULL
          AND COALESCE(ua.status, 'ACTIVE') = 'ACTIVE'
    ) AS alias_ss6,
    (
        SELECT ARRAY_AGG(ua.alias_sap ORDER BY ua.id)
        FROM fcc.unit_alias ua
        WHERE ua.unit_standar = mu.kode
          AND ua.alias_sap IS NOT NULL
          AND COALESCE(ua.status, 'ACTIVE') = 'ACTIVE'
    ) AS alias_sap,
    (
        SELECT COUNT(*)
        FROM fcc.unit_alias ua
        WHERE ua.unit_standar = mu.kode
          AND COALESCE(ua.status, 'ACTIVE') = 'ACTIVE'
    ) AS alias_count
FROM fcc.master_unit mu
ON CONFLICT (kode) DO NOTHING;

-- Add orphan from refuelling
INSERT INTO fcc.master_unit_v124 (kode, nama, vendor_kode, kategori, status, alias_ss6, alias_sap, alias_count)
SELECT DISTINCT
    r.unit_kode,
    r.unit_kode as nama,
    COALESCE((SELECT mv.kode FROM fcc.master_vendor mv WHERE mv.kode LIKE '%MNK%' OR mv.kode LIKE '%BIB%' LIMIT 1), 'PPA') as vendor_kode,
    'FUEL_TRUCK' as kategori,
    'ACTIVE' as status,
    ARRAY[r.unit_kode] as alias_ss6,
    NULL::text[] as alias_sap,
    1 as alias_count
FROM fcc.refuelling r
WHERE r.unit_kode NOT IN (SELECT kode FROM fcc.master_unit_v124)
ON CONFLICT (kode) DO NOTHING;

-- Indexes
CREATE INDEX master_unit_v124_vendor_idx ON fcc.master_unit_v124(vendor_kode, kategori);
CREATE INDEX master_unit_v124_alias_ss6_idx ON fcc.master_unit_v124 USING gin(alias_ss6);
CREATE INDEX master_unit_v124_alias_sap_idx ON fcc.master_unit_v124 USING gin(alias_sap);

-- Switch (rename)
ALTER TABLE fcc.refuelling DROP CONSTRAINT IF EXISTS refuelling_unit_kode_fkey;
ALTER TABLE fcc.unit_alias DROP CONSTRAINT IF EXISTS unit_alias_unit_standar_fkey;
DROP TABLE fcc.unit_alias CASCADE;
ALTER TABLE fcc.master_unit DROP CONSTRAINT IF EXISTS master_unit_vendor_kode_fkey;
DROP TABLE fcc.master_unit CASCADE;
ALTER TABLE fcc.master_unit_v124 RENAME TO master_unit;
ALTER TABLE fcc.master_unit RENAME CONSTRAINT master_unit_v124_status_check TO master_unit_status_check;
ALTER TABLE fcc.master_unit RENAME CONSTRAINT master_unit_v124_vendor_fkey TO master_unit_vendor_kode_fkey;
ALTER TABLE fcc.master_unit_v124_vendor_idx RENAME TO master_unit_vendor_idx;
ALTER TABLE fcc.master_unit_v124_alias_ss6_idx RENAME TO master_unit_alias_ss6_idx;
ALTER TABLE fcc.master_unit_v124_alias_sap_idx RENAME TO master_unit_alias_sap_idx;

-- Restore FK refuelling
ALTER TABLE fcc.refuelling ADD CONSTRAINT refuelling_unit_kode_fkey 
    FOREIGN KEY (unit_kode) REFERENCES fcc.master_unit(kode);

-- Triggers
CREATE TRIGGER trg_master_unit_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.master_unit FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();
CREATE TRIGGER trg_master_unit_touch BEFORE UPDATE ON fcc.master_unit FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();

-- Update v_rekonsiliasi view to use new master_unit
-- (View dropped via CASCADE; recreate if needed)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_views WHERE schemaname='fcc' AND viewname='v_rekonsiliasi') THEN
        -- already exists
    ELSE
        -- recreate
        NULL;  -- placeholder
    END IF;
END$$;

-- Update schema_contract required columns
-- (this is application-level, not SQL)

COMMIT;
