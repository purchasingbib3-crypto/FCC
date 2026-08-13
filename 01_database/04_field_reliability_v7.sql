-- FCC Field Reliability V7 — 2026-08-11
-- Safe, idempotent hardening for Dashboard Field input flows.
-- Run AFTER 01_schema_only.sql + 02_data_only.sql + 03_patch_all_20260811.sql.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Canonical user/profile link. app_user owns role/status; fuel_profiles is a
--    field identity mirror only.
-- ---------------------------------------------------------------------------
UPDATE fcc.fuel_profiles p
SET app_user_id = u.id,
    full_name = COALESCE(NULLIF(u.nama,''), p.full_name),
    role = u.role::fcc.fuel_app_role,
    status = CASE WHEN u.status='ACTIVE' THEN 'ACTIVE'::fcc.fuel_record_status ELSE 'INACTIVE'::fcc.fuel_record_status END,
    updated_at = now()
FROM fcc.app_user u
WHERE (lower(COALESCE(p.nrp,'')) = lower(u.username)
    OR lower(COALESCE(p.login_nrp,'')) = lower(u.username))
  AND (p.app_user_id IS DISTINCT FROM u.id
    OR p.role IS DISTINCT FROM u.role::fcc.fuel_app_role
    OR p.status IS DISTINCT FROM CASE WHEN u.status='ACTIVE' THEN 'ACTIVE'::fcc.fuel_record_status ELSE 'INACTIVE'::fcc.fuel_record_status END
    OR p.full_name IS DISTINCT FROM COALESCE(NULLIF(u.nama,''), p.full_name));

CREATE UNIQUE INDEX IF NOT EXISTS fuel_profiles_app_user_id_uidx
  ON fcc.fuel_profiles(app_user_id) WHERE app_user_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. Repair Receiving capacities using the canonical Mandar Ocean master.
--    This fixes records written as 20/25 instead of 20,000/25,000 because an
--    Indonesian thousands separator was parsed as a decimal point in browser.
-- ---------------------------------------------------------------------------
UPDATE fcc.penerimaan_mo p
SET kapasitas_l = f.kapasitas_l,
    no_polisi = COALESCE(f.no_polisi, p.no_polisi),
    updated_at = now()
FROM fcc.ft_mandar_ocean f
WHERE f.id_ft = p.id_ft
  AND f.kapasitas_l IS NOT NULL
  AND p.kapasitas_l IS DISTINCT FROM f.kapasitas_l;

UPDATE fcc.penerimaan_mo p
SET vendor_kode = v.kode,
    updated_at = now()
FROM LATERAL (
  SELECT kode FROM fcc.master_vendor
  WHERE upper(nama) LIKE '%MANDAR%'
  ORDER BY kode LIMIT 1
) v
WHERE p.vendor_kode IS NULL;

-- ---------------------------------------------------------------------------
-- 3. Route Master: Jalur 1/2/3 = TRANSFER; Jalur 5/6/7 = RECEIVING.
--    A jalur can have exactly one runtime destination regardless of shift/date.
-- ---------------------------------------------------------------------------
-- Archive intentionally does NOT copy unique constraints/indexes from the runtime
-- table. Multiple archived rows may have the same jalur/site and must remain auditable.
CREATE TABLE IF NOT EXISTS fcc.fuel_route_master_invalid_archive AS
SELECT m.*, NULL::timestamptz AS archived_at, NULL::text AS archive_reason
FROM fcc.fuel_route_master m
WHERE false;
ALTER TABLE fcc.fuel_route_master_invalid_archive
  ADD COLUMN IF NOT EXISTS archived_at timestamptz;
ALTER TABLE fcc.fuel_route_master_invalid_archive
  ADD COLUMN IF NOT EXISTS archive_reason text;

INSERT INTO fcc.fuel_route_master_invalid_archive
SELECT m.*, now(), 'PURPOSE_MISMATCH'
FROM fcc.fuel_route_master m
JOIN fcc.fuel_master_jalur j ON j.id=m.jalur_id
WHERE (regexp_replace(upper(j.jalur_code),'[^0-9]','','g') IN ('1','2','3') AND m.peruntukan <> 'TRANSFER')
   OR (regexp_replace(upper(j.jalur_code),'[^0-9]','','g') IN ('5','6','7') AND m.peruntukan <> 'RECEIVING')
ON CONFLICT DO NOTHING;

DELETE FROM fcc.fuel_route_master m
USING fcc.fuel_master_jalur j
WHERE j.id=m.jalur_id
  AND ((regexp_replace(upper(j.jalur_code),'[^0-9]','','g') IN ('1','2','3') AND m.peruntukan <> 'TRANSFER')
    OR (regexp_replace(upper(j.jalur_code),'[^0-9]','','g') IN ('5','6','7') AND m.peruntukan <> 'RECEIVING'));

WITH ranked AS (
  SELECT id,
         row_number() OVER (
           PARTITION BY site_code,jalur_id
           ORDER BY active DESC,updated_at DESC NULLS LAST,created_at DESC NULLS LAST,id DESC
         ) AS rn
  FROM fcc.fuel_route_master
), duplicates AS (
  SELECT m.* FROM fcc.fuel_route_master m JOIN ranked r ON r.id=m.id WHERE r.rn>1
)
INSERT INTO fcc.fuel_route_master_invalid_archive
SELECT d.*, now(), 'DUPLICATE_JALUR'
FROM duplicates d
ON CONFLICT DO NOTHING;

WITH ranked AS (
  SELECT id,row_number() OVER (
    PARTITION BY site_code,jalur_id
    ORDER BY active DESC,updated_at DESC NULLS LAST,created_at DESC NULLS LAST,id DESC
  ) rn
  FROM fcc.fuel_route_master
)
DELETE FROM fcc.fuel_route_master m USING ranked r WHERE m.id=r.id AND r.rn>1;

ALTER TABLE fcc.fuel_route_master
  DROP CONSTRAINT IF EXISTS fuel_route_master_site_code_jalur_id_peruntukan_key;
CREATE UNIQUE INDEX IF NOT EXISTS fuel_route_master_site_jalur_uidx
  ON fcc.fuel_route_master(site_code,jalur_id);

CREATE OR REPLACE FUNCTION fcc.validate_fuel_route_master()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_code text;
  v_jalur_status text;
  v_tandon_status text;
  v_no text;
BEGIN
  SELECT jalur_code,status INTO v_code,v_jalur_status FROM fcc.fuel_master_jalur WHERE id=NEW.jalur_id;
  SELECT status INTO v_tandon_status FROM fcc.fuel_master_tandon WHERE id=NEW.tandon_id;
  IF v_code IS NULL OR v_jalur_status <> 'ACTIVE' THEN
    RAISE EXCEPTION 'Jalur tidak aktif/tidak ditemukan';
  END IF;
  IF v_tandon_status IS DISTINCT FROM 'ACTIVE' THEN
    RAISE EXCEPTION 'Tandon tujuan harus ACTIVE';
  END IF;
  v_no := regexp_replace(upper(v_code),'[^0-9]','','g');
  IF v_no IN ('1','2','3') AND NEW.peruntukan <> 'TRANSFER' THEN
    RAISE EXCEPTION '% wajib TRANSFER', v_code;
  END IF;
  IF v_no IN ('5','6','7') AND NEW.peruntukan <> 'RECEIVING' THEN
    RAISE EXCEPTION '% wajib RECEIVING', v_code;
  END IF;
  IF v_no NOT IN ('1','2','3','5','6','7') THEN
    RAISE EXCEPTION 'Jalur operasional hanya 1,2,3,5,6,7';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_validate_fuel_route_master ON fcc.fuel_route_master;
CREATE TRIGGER trg_validate_fuel_route_master
BEFORE INSERT OR UPDATE OF jalur_id,tandon_id,peruntukan,active
ON fcc.fuel_route_master
FOR EACH ROW EXECUTE FUNCTION fcc.validate_fuel_route_master();

-- Compatibility view: route master applies to both shifts. Current field UI no
-- longer filters this view by date, but older callers remain functional today.
CREATE OR REPLACE VIEW fcc.fuel_v_route_config AS
SELECT m.id,m.site_code,CURRENT_DATE AS tanggal,s.shift,m.peruntukan,
       m.jalur_id,j.jalur_code,j.jalur_name,m.tandon_id,t.tandon_code,t.tandon_name,
       NULL::numeric AS fm_akhir_shift_sebelumnya,NULL::numeric AS fm_aktual_awal,
       NULL::numeric AS deviasi,
       CASE WHEN m.active AND j.status='ACTIVE' AND t.status='ACTIVE' THEN 'VALIDATED' ELSE 'INACTIVE' END AS status,
       NULL::uuid AS validated_by,NULL::timestamptz AS validated_at,m.notes,m.created_at,m.updated_at
FROM fcc.fuel_route_master m
JOIN fcc.fuel_master_jalur j ON j.id=m.jalur_id
JOIN fcc.fuel_master_tandon t ON t.id=m.tandon_id
CROSS JOIN (VALUES ('SHIFT_1'::text),('SHIFT_2'::text)) s(shift);

-- ---------------------------------------------------------------------------
-- 4. Client idempotency for every Dashboard Field write. A browser retry or a
--    slow network must return the original row rather than create a duplicate.
-- ---------------------------------------------------------------------------
ALTER TABLE fcc.fuel_tx_transfer_fuel ADD COLUMN IF NOT EXISTS client_request_id uuid;
ALTER TABLE fcc.fuel_tx_fuel_truck_monitoring ADD COLUMN IF NOT EXISTS client_request_id uuid;
ALTER TABLE fcc.penerimaan_mo ADD COLUMN IF NOT EXISTS client_request_id uuid;
ALTER TABLE fcc.pengurasan ADD COLUMN IF NOT EXISTS client_request_id uuid;
ALTER TABLE fcc.sounding_main_tank ADD COLUMN IF NOT EXISTS client_request_id uuid;
ALTER TABLE fcc.cleanliness ADD COLUMN IF NOT EXISTS client_request_id uuid;

CREATE UNIQUE INDEX IF NOT EXISTS fuel_tx_transfer_client_request_uidx
  ON fcc.fuel_tx_transfer_fuel(client_request_id) WHERE client_request_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS fuel_tx_monitoring_client_request_uidx
  ON fcc.fuel_tx_fuel_truck_monitoring(client_request_id) WHERE client_request_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS penerimaan_mo_client_request_uidx
  ON fcc.penerimaan_mo(client_request_id) WHERE client_request_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS pengurasan_client_request_uidx
  ON fcc.pengurasan(client_request_id) WHERE client_request_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS sounding_main_tank_client_request_uidx
  ON fcc.sounding_main_tank(client_request_id) WHERE client_request_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS cleanliness_client_request_uidx
  ON fcc.cleanliness(client_request_id) WHERE client_request_id IS NOT NULL;

COMMIT;
