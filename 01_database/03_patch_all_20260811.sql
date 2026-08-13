-- FCC Complete Reliability Patch — 2026-08-11
-- Safe to run on a restored bundle and on an existing FCC database.
-- Apply AFTER 01_schema_only.sql (and before/after sounding data is both safe).

BEGIN;

-- ---------------------------------------------------------------------------
-- Identity / roles
-- ---------------------------------------------------------------------------
ALTER TABLE fcc.app_user DROP CONSTRAINT IF EXISTS app_user_role_check;
ALTER TABLE fcc.app_user
  ADD CONSTRAINT app_user_role_check CHECK (
    role = ANY (ARRAY[
      'SUPER_ADMIN','ADMIN','SUPERVISOR','GROUP_LEADER','PENERIMAAN',
      'FUELMAN','DRIVER','VENDOR','FIELD'
    ]::text[])
  );

ALTER TABLE fcc.fuel_profiles ADD COLUMN IF NOT EXISTS app_user_id bigint;
CREATE UNIQUE INDEX IF NOT EXISTS fuel_profiles_app_user_id_uidx
  ON fcc.fuel_profiles(app_user_id) WHERE app_user_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Import/reconciliation schema contract
-- ---------------------------------------------------------------------------
ALTER TABLE fcc.fuel_import_row ADD COLUMN IF NOT EXISTS shift text;
ALTER TABLE fcc.fuel_import_row ADD COLUMN IF NOT EXISTS storage_location text;
ALTER TABLE fcc.fuel_import_row ADD COLUMN IF NOT EXISTS source_row integer;
ALTER TABLE fcc.fuel_import_row
  DROP CONSTRAINT IF EXISTS fuel_import_row_batch_id_sumber_tanggal_alias_unit_key;
CREATE INDEX IF NOT EXISTS fuel_import_row_batch_source_idx
  ON fcc.fuel_import_row(batch_id, sumber, source_row);

-- ---------------------------------------------------------------------------
-- Pure route master. Legacy fuel_route_config remains for audit/history only.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fcc.fuel_route_master (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_code text NOT NULL DEFAULT 'PPA-BIB',
  jalur_id uuid NOT NULL REFERENCES fcc.fuel_master_jalur(id) ON UPDATE CASCADE,
  tandon_id uuid NOT NULL REFERENCES fcc.fuel_master_tandon(id) ON UPDATE CASCADE,
  peruntukan text NOT NULL CHECK (peruntukan IN ('TRANSFER','RECEIVING')),
  active boolean NOT NULL DEFAULT true,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(site_code, jalur_id, peruntukan)
);

INSERT INTO fcc.fuel_route_master(site_code,jalur_id,tandon_id,peruntukan,active,notes,created_at,updated_at)
SELECT DISTINCT ON (site_code,jalur_id,peruntukan)
       site_code,jalur_id,tandon_id,peruntukan,
       (COALESCE(status,'VALIDATED') <> 'INACTIVE') AS active,
       COALESCE(notes,'Migrated from fuel_route_config'),created_at,updated_at
FROM fcc.fuel_route_config
ORDER BY site_code,jalur_id,peruntukan,updated_at DESC,created_at DESC
ON CONFLICT (site_code,jalur_id,peruntukan) DO UPDATE
SET tandon_id=EXCLUDED.tandon_id,
    active=EXCLUDED.active,
    notes=EXCLUDED.notes,
    updated_at=GREATEST(fcc.fuel_route_master.updated_at,EXCLUDED.updated_at);

CREATE OR REPLACE VIEW fcc.fuel_v_route_config AS
SELECT m.id,
       m.site_code,
       DATE '2000-01-01' AS tanggal,
       'SHIFT_1'::text AS shift,
       m.peruntukan,
       m.jalur_id,
       j.jalur_code,
       j.jalur_name,
       m.tandon_id,
       t.tandon_code,
       t.tandon_name,
       NULL::numeric AS fm_akhir_shift_sebelumnya,
       NULL::numeric AS fm_aktual_awal,
       NULL::numeric AS deviasi,
       CASE WHEN m.active THEN 'VALIDATED'::text ELSE 'INACTIVE'::text END AS status,
       NULL::uuid AS validated_by,
       NULL::timestamptz AS validated_at,
       m.notes,
       m.created_at,
       m.updated_at
FROM fcc.fuel_route_master m
JOIN fcc.fuel_master_jalur j ON j.id=m.jalur_id
JOIN fcc.fuel_master_tandon t ON t.id=m.tandon_id;

-- Tera grid needs deterministic composite upsert. Preserve any historical
-- duplicates before keeping the newest row per site/unit.
CREATE TABLE IF NOT EXISTS fcc.fuel_tera_tangki_grid_duplicates_archive AS
SELECT g.*, now()::timestamptz AS archived_at
FROM fcc.fuel_tera_tangki_grid g
WHERE false;

WITH ranked AS (
  SELECT id, row_number() OVER (
    PARTITION BY site_code,unit_code
    ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
  ) AS rn
  FROM fcc.fuel_tera_tangki_grid
), duplicates AS (
  SELECT g.* FROM fcc.fuel_tera_tangki_grid g
  JOIN ranked r ON r.id=g.id
  WHERE r.rn > 1
)
INSERT INTO fcc.fuel_tera_tangki_grid_duplicates_archive
SELECT d.*, now() FROM duplicates d
WHERE NOT EXISTS (
  SELECT 1 FROM fcc.fuel_tera_tangki_grid_duplicates_archive a WHERE a.id=d.id
);

WITH ranked AS (
  SELECT id, row_number() OVER (
    PARTITION BY site_code,unit_code
    ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
  ) AS rn
  FROM fcc.fuel_tera_tangki_grid
)
DELETE FROM fcc.fuel_tera_tangki_grid g
USING ranked r
WHERE g.id=r.id AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS fuel_tera_tangki_grid_site_unit_uidx
  ON fcc.fuel_tera_tangki_grid(site_code,unit_code);

-- ---------------------------------------------------------------------------
-- Analytics tables referenced by backend/master API
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fcc.fuel_supply_plan (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tanggal date NOT NULL,
  shift text NOT NULL CHECK (shift IN ('SHIFT_1','SHIFT_2')),
  vendor_kode text NOT NULL,
  planned_l numeric(14,3) NOT NULL DEFAULT 0 CHECK (planned_l >= 0),
  planned_ritase integer NOT NULL DEFAULT 0 CHECK (planned_ritase >= 0),
  cutoff_time time,
  notes text,
  status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','CANCELLED','INACTIVE')),
  created_by text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tanggal,shift,vendor_kode)
);

CREATE TABLE IF NOT EXISTS fcc.cleanliness_filter_cost (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  asset_scope text NOT NULL,
  asset_code text,
  jalur_code text,
  replacement_date date NOT NULL,
  filter_cost numeric(16,2) NOT NULL DEFAULT 0 CHECK (filter_cost >= 0),
  lifetime_days integer CHECK (lifetime_days IS NULL OR lifetime_days >= 0),
  fuelpass_l numeric(16,3) CHECK (fuelpass_l IS NULL OR fuelpass_l >= 0),
  cost_per_l numeric(18,6) GENERATED ALWAYS AS (
    CASE WHEN COALESCE(fuelpass_l,0) > 0 THEN filter_cost/fuelpass_l ELSE NULL END
  ) STORED,
  status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','CANCELLED','INACTIVE')),
  notes text,
  created_by text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cleanliness_filter_cost_replacement_idx
  ON fcc.cleanliness_filter_cost(replacement_date DESC);

-- ---------------------------------------------------------------------------
-- Evidence: new writes go to filesystem metadata. Existing base64 stays readable.
-- ---------------------------------------------------------------------------
ALTER TABLE fcc.photo ADD COLUMN IF NOT EXISTS site_code text DEFAULT 'PPA-BIB';
ALTER TABLE fcc.photo ADD COLUMN IF NOT EXISTS storage_path text;
ALTER TABLE fcc.photo ADD COLUMN IF NOT EXISTS file_size_bytes integer;
ALTER TABLE fcc.photo ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
ALTER TABLE fcc.photo ALTER COLUMN base64_data DROP NOT NULL;
UPDATE fcc.photo
SET file_size_bytes=COALESCE(file_size_bytes,size_bytes),
    created_at=COALESCE(created_at,uploaded_at)
WHERE file_size_bytes IS NULL OR created_at IS NULL;
CREATE INDEX IF NOT EXISTS photo_record_lookup_idx ON fcc.photo(modul,record_id,photo_type);

COMMIT;
