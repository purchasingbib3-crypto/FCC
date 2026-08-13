--
-- PostgreSQL database dump
--

\restrict GhGgB4hPEIFl7xATe8bjYxQnqZWrHb1q1OyCzkFh3Wu1cQq5RLKiaFdzSI9U6sZ

-- Dumped from database version 16.14 (Ubuntu 16.14-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.14 (Ubuntu 16.14-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: fcc; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA fcc;


--
-- Name: fuel_app_role; Type: TYPE; Schema: fcc; Owner: -
--

CREATE TYPE fcc.fuel_app_role AS ENUM (
    'SUPER_ADMIN',
    'ADMIN',
    'SUPERVISOR',
    'FIELD',
    'FUELMAN',
    'DRIVER',
    'VENDOR',
    'GROUP_LEADER',
    'PENERIMAAN'
);


--
-- Name: fuel_fm_setting_mode; Type: TYPE; Schema: fcc; Owner: -
--

CREATE TYPE fcc.fuel_fm_setting_mode AS ENUM (
    'AUTO',
    'MANUAL'
);


--
-- Name: fuel_monitoring_type; Type: TYPE; Schema: fcc; Owner: -
--

CREATE TYPE fcc.fuel_monitoring_type AS ENUM (
    'FLOWMETER',
    'HM'
);


--
-- Name: fuel_photo_type; Type: TYPE; Schema: fcc; Owner: -
--

CREATE TYPE fcc.fuel_photo_type AS ENUM (
    'TRANSFER_FM_AWAL',
    'TRANSFER_FM_AKHIR',
    'MONITORING_FM_IN',
    'MONITORING_FM_OUT',
    'MONITORING_HM'
);


--
-- Name: fuel_record_status; Type: TYPE; Schema: fcc; Owner: -
--

CREATE TYPE fcc.fuel_record_status AS ENUM (
    'ACTIVE',
    'INACTIVE',
    'DRAFT',
    'COMMITTED',
    'CANCELLED',
    'LOCKED'
);


--
-- Name: fuel_shift_type; Type: TYPE; Schema: fcc; Owner: -
--

CREATE TYPE fcc.fuel_shift_type AS ENUM (
    'SHIFT_1',
    'SHIFT_2'
);


--
-- Name: audit_row(); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.audit_row() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE v_diff jsonb; v_id text;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    SELECT jsonb_object_agg(n.key, jsonb_build_object('dari', o.value, 'ke', n.value))
      INTO v_diff
      FROM jsonb_each(to_jsonb(NEW)) n
      JOIN jsonb_each(to_jsonb(OLD)) o ON o.key = n.key
     WHERE n.value IS DISTINCT FROM o.value
       AND n.key NOT IN ('updated_at');
    IF v_diff IS NULL THEN RETURN NEW; END IF;   -- tidak ada perubahan nyata
    v_id := to_jsonb(NEW) ->> 'id';
  ELSIF TG_OP = 'INSERT' THEN
    v_diff := to_jsonb(NEW); v_id := to_jsonb(NEW) ->> 'id';
  ELSE
    v_diff := to_jsonb(OLD); v_id := to_jsonb(OLD) ->> 'id';
  END IF;

  INSERT INTO fcc.audit_trail (aktor, aksi, modul, record_id, perubahan, ip_device)
  VALUES (fcc.current_actor(), TG_OP, TG_TABLE_NAME, COALESCE(v_id,'-'), v_diff,
          NULLIF(current_setting('app.device', true), ''));

  RETURN COALESCE(NEW, OLD);
END $$;


--
-- Name: current_actor(); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.current_actor() RETURNS text
    LANGUAGE sql STABLE
    AS $$
  SELECT COALESCE(NULLIF(current_setting('app.actor', true), ''), session_user)
$$;


--
-- Name: fill_ft_status(); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.fill_ft_status() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.masa_berlaku IS NULL THEN
        NEW.status := 'PERLU DATA';
    ELSIF NEW.masa_berlaku < CURRENT_DATE THEN
        NEW.status := 'EXPIRED';
    ELSE
        NEW.status := 'ACTIVE';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: fill_transfer_volume(); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.fill_transfer_volume() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.volume_awal_l  := fcc.volume_from_dip(NEW.main_tank, NEW.sounding_awal_cm);
  NEW.volume_akhir_l := fcc.volume_from_dip(NEW.main_tank, NEW.sounding_akhir_cm);
  IF NEW.volume_awal_l IS NULL OR NEW.volume_akhir_l IS NULL THEN
    RAISE EXCEPTION 'Tabel sounding untuk % belum ada. Lengkapi master dulu.', NEW.main_tank;
  END IF;
  RETURN NEW;
END $$;


--
-- Name: fuel_get_default_fm_awal(text, uuid); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.fuel_get_default_fm_awal(p_site_code text, p_jalur_id uuid) RETURNS TABLE(fm_value numeric, source text, last_transfer_id uuid)
    LANGUAGE plpgsql STABLE
    AS $_$
DECLARE
    v_mode fcc.fuel_fm_setting_mode;
    v_manual numeric;
    v_last_fm_akhir numeric;
    v_last_id uuid;
    v_jalur_code text;
    v_legacy_aliases text[];
BEGIN
    -- 1) Cek settings table (manual override)
    SELECT mode, fm_awal_manual INTO v_mode, v_manual
    FROM fcc.fuel_fm_awal_settings
    WHERE site_code = p_site_code AND jalur_id = p_jalur_id;

    IF v_mode = 'MANUAL' AND v_manual IS NOT NULL THEN
        fm_value := v_manual;
        source := 'MANUAL';
        last_transfer_id := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    -- 2) AUTO: ambil fm_akhir dari fuel_tx_transfer_fuel terbaru (UUID-based)
    SELECT ftfm.fm_akhir, ftfm.id INTO v_last_fm_akhir, v_last_id
    FROM fcc.fuel_tx_transfer_fuel ftfm
    WHERE ftfm.site_code = p_site_code
      AND ftfm.jalur_id = p_jalur_id
      AND ftfm.voided_at IS NULL
    ORDER BY ftfm.tanggal DESC, ftfm.created_at DESC
    LIMIT 1;

    -- 3) Fallback ke transfer_fuel legacy by jalur_code (with JLR-N aliases)
    IF v_last_fm_akhir IS NULL THEN
        SELECT mj.jalur_code INTO v_jalur_code
        FROM fcc.fuel_master_jalur mj WHERE mj.id = p_jalur_id;

        IF v_jalur_code IS NOT NULL THEN
            -- Build aliases: 'JALUR 1' -> ['JALUR 1', 'JLR-1']
            v_legacy_aliases := ARRAY[v_jalur_code];
            IF v_jalur_code ~ '^JALUR ([0-9]+)$' THEN
                v_legacy_aliases := v_legacy_aliases || ARRAY['JLR-' || substring(v_jalur_code from '^JALUR ([0-9]+)$')];
            END IF;

            SELECT tf.fm_akhir INTO v_last_fm_akhir
            FROM fcc.transfer_fuel tf
            WHERE tf.jalur = ANY(v_legacy_aliases)
            ORDER BY tf.tanggal DESC, tf.created_at DESC
            LIMIT 1;
        END IF;
    END IF;

    IF v_last_fm_akhir IS NULL THEN
        fm_value := 0;
        source := 'AUTO_DEFAULT';
        last_transfer_id := NULL;
    ELSE
        fm_value := v_last_fm_akhir;
        source := CASE WHEN v_last_id IS NOT NULL THEN 'AUTO_LAST_TRANSFER' ELSE 'AUTO_LEGACY_TRANSFER' END;
        last_transfer_id := v_last_id;
    END IF;
    RETURN NEXT;
END;
$_$;


--
-- Name: fuel_get_tera_volume(uuid, numeric); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.fuel_get_tera_volume(p_fuel_truck_id uuid, p_dip_value numeric) RETURNS TABLE(volume_l numeric, interpolated boolean, source text)
    LANGUAGE plpgsql STABLE
    AS $$
DECLARE
    v_record RECORD;
    v_dip numeric;
    v_idx integer;
    v_lower numeric;
    v_upper numeric;
    v_lower_vol numeric;
    v_upper_vol numeric;
BEGIN
    -- Cari profile tera untuk truck ini
    SELECT g.volumes_json, g.dip_step, g.dip_min, g.max_dip, g.unit_code
      INTO v_record
    FROM fcc.fuel_tera_tangki_grid g
    LEFT JOIN fcc.fuel_master_fuel_truck ft ON ft.id = g.fuel_truck_id
    WHERE g.fuel_truck_id = p_fuel_truck_id
       OR g.unit_code = (SELECT unit_code FROM fcc.fuel_master_fuel_truck WHERE id = p_fuel_truck_id)
    ORDER BY g.fuel_truck_id NULLS LAST
    LIMIT 1;

    IF v_record.volumes_json IS NULL OR jsonb_array_length(v_record.volumes_json) = 0 THEN
        volume_l := NULL;
        interpolated := false;
        source := 'NO_PROFILE';
        RETURN NEXT;
        RETURN;
    END IF;

    v_dip := GREATEST(p_dip_value, v_record.dip_min);

    -- Jika di luar range, interpolate di ujung
    IF v_dip >= v_record.max_dip THEN
        v_idx := jsonb_array_length(v_record.volumes_json) - 1;
        SELECT (vol->>'volume_liter')::numeric INTO volume_l
        FROM jsonb_array_elements(v_record.volumes_json) WITH ORDINALITY AS t(vol, ord)
        WHERE ord = v_idx + 1;
        interpolated := false;
        source := 'CLAMPED_MAX';
        RETURN NEXT;
        RETURN;
    END IF;

    -- Index dari dip_value
    v_idx := LEAST(GREATEST(FLOOR((v_dip - v_record.dip_min) / v_record.dip_step)::integer, 0),
                   jsonb_array_length(v_record.volumes_json) - 1);

    -- Cek apakah exact match
    IF (v_record.dip_min + v_idx * v_record.dip_step) = v_dip THEN
        SELECT (vol->>'volume_liter')::numeric INTO volume_l
        FROM jsonb_array_elements(v_record.volumes_json) WITH ORDINALITY AS t(vol, ord)
        WHERE ord = v_idx + 1;
        interpolated := false;
        source := 'EXACT';
        RETURN NEXT;
        RETURN;
    END IF;

    -- Interpolasi linear
    v_lower := v_record.dip_min + v_idx * v_record.dip_step;
    v_upper := v_lower + v_record.dip_step;

    SELECT (vol->>'volume_liter')::numeric INTO v_lower_vol
    FROM jsonb_array_elements(v_record.volumes_json) WITH ORDINALITY AS t(vol, ord)
    WHERE ord = v_idx + 1;

    SELECT (vol->>'volume_liter')::numeric INTO v_upper_vol
    FROM jsonb_array_elements(v_record.volumes_json) WITH ORDINALITY AS t(vol, ord)
    WHERE ord = v_idx + 2;

    volume_l := v_lower_vol + ((v_dip - v_lower) / (v_upper - v_lower)) * (v_upper_vol - v_lower_vol);
    interpolated := true;
    source := 'INTERPOLATED';
    RETURN NEXT;
END;
$$;


--
-- Name: fuel_public_staged_nrp_lookup(text); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.fuel_public_staged_nrp_lookup(p_nrp text) RETURNS TABLE(nrp text, full_name text, jabatan text, is_found boolean)
    LANGUAGE plpgsql STABLE
    AS $$
DECLARE
    v_found boolean := false;
    v_full_name text;
    v_jabatan text;
BEGIN
    -- 1) Cek fuel_user_staging dulu (data master dari admin)
    SELECT s.full_name, COALESCE(s.jabatan,'')
      INTO v_full_name, v_jabatan
    FROM fcc.fuel_user_staging s
    WHERE s.nrp = p_nrp
    LIMIT 1;

    IF v_full_name IS NOT NULL THEN
        v_found := true;
        RETURN QUERY SELECT p_nrp, v_full_name, v_jabatan, v_found;
        RETURN;
    END IF;

    -- 2) Fallback: cek app_user (user yang sudah login di dashboard utama)
    SELECT u.nama, COALESCE(u.role::text, '')
      INTO v_full_name, v_jabatan
    FROM fcc.app_user u
    WHERE u.username = p_nrp
      AND u.status = 'ACTIVE'
    LIMIT 1;

    IF v_full_name IS NOT NULL THEN
        v_found := true;
    END IF;

    RETURN QUERY SELECT p_nrp, v_full_name, v_jabatan, v_found;
END;
$$;


--
-- Name: fuel_set_updated_at(); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.fuel_set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;


--
-- Name: set_updated_at(); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;


--
-- Name: sounding_metadata(text); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.sounding_metadata(p_aset text) RETURNS TABLE(aset text, dip_min numeric, dip_max numeric, volume_max_l numeric, point_count bigint)
    LANGUAGE sql STABLE
    AS $$
    SELECT p_aset,
           MIN(dip_cm)::NUMERIC,
           MAX(dip_cm)::NUMERIC,
           MAX(volume_l)::NUMERIC,
           COUNT(*)::BIGINT
    FROM fcc.sounding_table
    WHERE aset = p_aset AND status='ACTIVE';
$$;


--
-- Name: touch_fuel_discrepancy_updated_at(); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.touch_fuel_discrepancy_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;


--
-- Name: validate_fuel_route_master(); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.validate_fuel_route_master() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: volume_from_dip(text, numeric); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.volume_from_dip(p_aset text, p_dip_cm numeric) RETURNS numeric
    LANGUAGE sql STABLE
    AS $$
    SELECT volume_l
      FROM fcc.sounding_table
     WHERE aset = p_aset
       AND dip_cm = p_dip_cm
       AND status = 'ACTIVE'
     LIMIT 1;
$$;


--
-- Name: volume_from_dip_interp(text, numeric); Type: FUNCTION; Schema: fcc; Owner: -
--

CREATE FUNCTION fcc.volume_from_dip_interp(p_aset text, p_dip_cm numeric) RETURNS numeric
    LANGUAGE plpgsql STABLE
    AS $$
DECLARE
    v_lo_dip NUMERIC;
    v_lo_vol NUMERIC;
    v_hi_dip NUMERIC;
    v_hi_vol NUMERIC;
BEGIN
    SELECT dip_cm, volume_l INTO v_lo_dip, v_lo_vol
      FROM fcc.sounding_table
     WHERE aset = p_aset AND dip_cm <= p_dip_cm AND status='ACTIVE'
     ORDER BY dip_cm DESC LIMIT 1;

    SELECT dip_cm, volume_l INTO v_hi_dip, v_hi_vol
      FROM fcc.sounding_table
     WHERE aset = p_aset AND dip_cm >= p_dip_cm AND status='ACTIVE'
     ORDER BY dip_cm ASC LIMIT 1;

    IF v_lo_dip IS NULL OR v_hi_dip IS NULL THEN RETURN NULL;
    ELSIF v_lo_dip = v_hi_dip THEN RETURN v_lo_vol;
    ELSE
        RETURN ROUND(v_lo_vol + (p_dip_cm - v_lo_dip)/(v_hi_dip - v_lo_dip) * (v_hi_vol - v_lo_vol), 3);
    END IF;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: app_config; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.app_config (
    parameter text NOT NULL,
    nilai text,
    tipe text DEFAULT 'string'::text NOT NULL,
    keterangan text,
    rahasia boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT app_config_tipe_check CHECK ((tipe = ANY (ARRAY['string'::text, 'number'::text, 'boolean'::text, 'json'::text])))
);


--
-- Name: COLUMN app_config.rahasia; Type: COMMENT; Schema: fcc; Owner: -
--

COMMENT ON COLUMN fcc.app_config.rahasia IS 'Kalau true, nilai TIDAK boleh dikirim ke browser. Simpan di env server.';


--
-- Name: app_user; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.app_user (
    id bigint NOT NULL,
    username text NOT NULL,
    nama text NOT NULL,
    role text NOT NULL,
    vendor_kode text,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    password_hash text NOT NULL,
    must_change_pw boolean DEFAULT true NOT NULL,
    failed_logins integer DEFAULT 0 NOT NULL,
    locked_until timestamp with time zone,
    last_login timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT app_user_role_check CHECK ((role = ANY (ARRAY['SUPER_ADMIN'::text, 'ADMIN'::text, 'SUPERVISOR'::text, 'GROUP_LEADER'::text, 'PENERIMAAN'::text, 'FUELMAN'::text, 'DRIVER'::text, 'VENDOR'::text, 'FIELD'::text]))),
    CONSTRAINT app_user_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'INACTIVE'::text]))),
    CONSTRAINT vendor_wajib_utk_role_vendor CHECK (((role <> 'VENDOR'::text) OR (vendor_kode IS NOT NULL)))
);


--
-- Name: app_user_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.app_user ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.app_user_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: audit_trail; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.audit_trail (
    id bigint NOT NULL,
    waktu timestamp with time zone DEFAULT now() NOT NULL,
    aktor text NOT NULL,
    aksi text NOT NULL,
    modul text NOT NULL,
    record_id text NOT NULL,
    perubahan jsonb NOT NULL,
    ip_device text,
    CONSTRAINT audit_trail_aksi_check CHECK ((aksi = ANY (ARRAY['INSERT'::text, 'UPDATE'::text, 'DELETE'::text])))
);


--
-- Name: audit_trail_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.audit_trail ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.audit_trail_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: cleanliness; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.cleanliness (
    id bigint NOT NULL,
    kode text NOT NULL,
    tanggal date NOT NULL,
    jam time without time zone,
    shift text,
    jenis text,
    jalur text,
    aset text,
    pressure_bar numeric(6,2),
    fm_akhir numeric(14,3),
    before_4 integer,
    before_6 integer,
    before_14 integer,
    after_4 integer NOT NULL,
    after_6 integer NOT NULL,
    after_14 integer NOT NULL,
    bf_water_sat numeric(6,2),
    af_water_sat numeric(6,2),
    lpm numeric(8,2),
    rpm numeric(8,2),
    status text GENERATED ALWAYS AS (
CASE
    WHEN ((after_4 < 18) AND (after_6 < 16) AND (after_14 < 13)) THEN 'OK'::text
    ELSE 'WARNING'::text
END) STORED,
    petugas text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_request_id uuid,
    CONSTRAINT cleanliness_jenis_check CHECK ((jenis = ANY (ARRAY['MAINTANK'::text, 'FUEL_TRUCK_PPA'::text, 'MANDAR_OCEAN'::text]))),
    CONSTRAINT cleanliness_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text])))
);


--
-- Name: COLUMN cleanliness.status; Type: COMMENT; Schema: fcc; Owner: -
--

COMMENT ON COLUMN fcc.cleanliness.status IS 'Ambang ISO 4406 site: 4µ<18, 6µ<16, 14µ<13.';


--
-- Name: cleanliness_filter_cost; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.cleanliness_filter_cost (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    replacement_date date NOT NULL,
    filter_cost numeric(14,2) NOT NULL,
    cost_per_l numeric(10,4) NOT NULL,
    notes text,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: cleanliness_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.cleanliness ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.cleanliness_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: closing_stock; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.closing_stock (
    id bigint NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    penanggung_jawab text NOT NULL,
    closed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT closed_wajib_waktu CHECK (((status <> 'CLOSED'::text) OR (closed_at IS NOT NULL))),
    CONSTRAINT closing_stock_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text]))),
    CONSTRAINT closing_stock_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'CLOSED'::text, 'REOPENED'::text])))
);


--
-- Name: closing_stock_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.closing_stock ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.closing_stock_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: closing_stock_line; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.closing_stock_line (
    id bigint NOT NULL,
    closing_id bigint NOT NULL,
    aset text NOT NULL,
    jenis text NOT NULL,
    stock_awal_l numeric(14,3) DEFAULT 0 NOT NULL,
    penerimaan_l numeric(14,3) DEFAULT 0 NOT NULL,
    transfer_masuk_l numeric(14,3) DEFAULT 0 NOT NULL,
    transfer_keluar_l numeric(14,3) DEFAULT 0 NOT NULL,
    refuelling_l numeric(14,3) DEFAULT 0 NOT NULL,
    total_administrasi_l numeric(14,3) GENERATED ALWAYS AS (((((stock_awal_l + penerimaan_l) + transfer_masuk_l) - transfer_keluar_l) - refuelling_l)) STORED,
    sounding_aktual_cm numeric(7,2),
    aktual_l numeric(14,3),
    deviasi_total_l numeric(14,3) GENERATED ALWAYS AS ((aktual_l - ((((stock_awal_l + penerimaan_l) + transfer_masuk_l) - transfer_keluar_l) - refuelling_l))) STORED,
    deviasi_pct numeric(8,4) GENERATED ALWAYS AS (
CASE
    WHEN (((((stock_awal_l + penerimaan_l) + transfer_masuk_l) - transfer_keluar_l) - refuelling_l) = (0)::numeric) THEN NULL::numeric
    ELSE (((aktual_l - ((((stock_awal_l + penerimaan_l) + transfer_masuk_l) - transfer_keluar_l) - refuelling_l)) / ((((stock_awal_l + penerimaan_l) + transfer_masuk_l) - transfer_keluar_l) - refuelling_l)) * (100)::numeric)
END) STORED,
    CONSTRAINT closing_stock_line_jenis_check CHECK ((jenis = ANY (ARRAY['MAINTANK'::text, 'FUEL_TRUCK'::text])))
);


--
-- Name: closing_stock_line_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.closing_stock_line ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.closing_stock_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: evidence; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.evidence (
    id bigint NOT NULL,
    modul text NOT NULL,
    record_id bigint NOT NULL,
    peran text NOT NULL,
    path text NOT NULL,
    sha256 text,
    ukuran_byte bigint,
    uploaded_by text NOT NULL,
    uploaded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: evidence_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.evidence ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.evidence_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: flowmeter_ft; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.flowmeter_ft (
    id bigint NOT NULL,
    kode text NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    fuel_truck text NOT NULL,
    petugas text NOT NULL,
    fm_in numeric(14,3) NOT NULL,
    fm_out numeric(14,3) NOT NULL,
    total_l numeric(14,3) GENERATED ALWAYS AS ((fm_out - fm_in)) STORED,
    catatan text,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT flowmeter_ft_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text]))),
    CONSTRAINT flowmeter_ft_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'VALID'::text, 'WARNING'::text, 'VOID'::text]))),
    CONSTRAINT fm_ft_naik CHECK ((fm_out >= fm_in))
);


--
-- Name: flowmeter_ft_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.flowmeter_ft ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.flowmeter_ft_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ft_mandar_ocean; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.ft_mandar_ocean (
    id_ft text NOT NULL,
    no_lambung text NOT NULL,
    no_polisi text NOT NULL,
    kapasitas_l numeric(12,2),
    t2_depan_cm numeric(7,2),
    t2_belakang_cm numeric(7,2),
    status text DEFAULT 'PERLU DATA'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    masa_berlaku date,
    expired_komisioning date,
    CONSTRAINT ft_mandar_ocean_kapasitas_l_check CHECK (((kapasitas_l > (0)::numeric) OR (kapasitas_l IS NULL))),
    CONSTRAINT ft_mandar_ocean_status_check CHECK ((status = ANY (ARRAY['PERLU DATA'::text, 'EXPIRED'::text, 'ACTIVE'::text, 'INACTIVE'::text])))
);


--
-- Name: COLUMN ft_mandar_ocean.status; Type: COMMENT; Schema: fcc; Owner: -
--

COMMENT ON COLUMN fcc.ft_mandar_ocean.status IS 'Diisi trigger dari masa_berlaku. SQL asli pakai GENERATED + CURRENT_DATE, ditolak Postgres (non-immutable).';


--
-- Name: fuel_attachment_log; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_attachment_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    photo_type fcc.fuel_photo_type NOT NULL,
    bucket_name text DEFAULT 'fuel-control-photos'::text NOT NULL,
    storage_path text NOT NULL,
    mime_type text,
    file_size_bytes bigint,
    transfer_fuel_id uuid,
    monitoring_id uuid,
    uploaded_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fuel_discrepancy_manual; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_discrepancy_manual (
    id bigint NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    stock_awal_override_l numeric,
    penerimaan_override_l numeric,
    fuel_keluar_override_l numeric,
    stock_aktual_override_l numeric,
    ba_l numeric DEFAULT 0 NOT NULL,
    adjustment_l numeric DEFAULT 0 NOT NULL,
    cuaca text,
    remark text,
    pica_status text DEFAULT 'OPEN'::text NOT NULL,
    pica_owner text,
    pica_due_date date,
    pica_note text,
    input_by text,
    updated_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fuel_discrepancy_manual_pica_status_check CHECK ((pica_status = ANY (ARRAY['OPEN'::text, 'IN_PROGRESS'::text, 'CLOSED'::text, 'N/A'::text]))),
    CONSTRAINT fuel_discrepancy_manual_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text])))
);


--
-- Name: fuel_discrepancy_manual_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

CREATE SEQUENCE fcc.fuel_discrepancy_manual_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fuel_discrepancy_manual_id_seq; Type: SEQUENCE OWNED BY; Schema: fcc; Owner: -
--

ALTER SEQUENCE fcc.fuel_discrepancy_manual_id_seq OWNED BY fcc.fuel_discrepancy_manual.id;


--
-- Name: fuel_fm_awal_settings; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_fm_awal_settings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    jalur_id uuid NOT NULL,
    mode fcc.fuel_fm_setting_mode DEFAULT 'AUTO'::fcc.fuel_fm_setting_mode NOT NULL,
    fm_awal_manual numeric,
    notes text,
    updated_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fuel_import_row; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_import_row (
    id bigint NOT NULL,
    batch_id bigint NOT NULL,
    sumber text NOT NULL,
    tanggal date NOT NULL,
    alias_unit text NOT NULL,
    unit_standar text,
    liter numeric(14,3) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    shift text,
    storage_location text,
    source_row integer,
    source_format text,
    source_record_id text,
    movement_type text,
    material text,
    uom text,
    mapping_status text DEFAULT 'MAPPED'::text NOT NULL,
    quantity_source_l numeric(14,3) NOT NULL,
    volume_net_l numeric(14,3) NOT NULL,
    CONSTRAINT fuel_import_row_mapping_status_check CHECK ((mapping_status = ANY (ARRAY['MAPPED'::text, 'UNMAPPED'::text, 'AMBIGUOUS'::text]))),
    CONSTRAINT fuel_import_row_sumber_check CHECK ((sumber = ANY (ARRAY['SS6'::text, 'SAP'::text])))
);


--
-- Name: fuel_import_row_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.fuel_import_row ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.fuel_import_row_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: fuel_master_fuel_truck; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_master_fuel_truck (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    unit_code text NOT NULL,
    unit_name text NOT NULL,
    unit_type text,
    kapasitas_l bigint,
    status fcc.fuel_record_status DEFAULT 'ACTIVE'::fcc.fuel_record_status NOT NULL,
    updated_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fuel_master_jalur; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_master_jalur (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    jalur_code text NOT NULL,
    jalur_name text NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    status fcc.fuel_record_status DEFAULT 'ACTIVE'::fcc.fuel_record_status NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fuel_master_tandon; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_master_tandon (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    tandon_code text NOT NULL,
    tandon_name text NOT NULL,
    kapasitas_l bigint,
    status fcc.fuel_record_status DEFAULT 'ACTIVE'::fcc.fuel_record_status NOT NULL,
    updated_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fuel_profiles; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_profiles (
    id uuid NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    nrp text,
    login_nrp text,
    email text,
    full_name text NOT NULL,
    role fcc.fuel_app_role DEFAULT 'FIELD'::fcc.fuel_app_role NOT NULL,
    status fcc.fuel_record_status DEFAULT 'ACTIVE'::fcc.fuel_record_status NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    app_user_id bigint
);


--
-- Name: fuel_route_config; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_route_config (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    jalur_id uuid NOT NULL,
    tandon_id uuid NOT NULL,
    peruntukan text DEFAULT 'TRANSFER'::text NOT NULL,
    fm_akhir_shift_sebelumnya numeric,
    fm_aktual_awal numeric,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    validated_by uuid,
    validated_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fuel_route_config_peruntukan_check CHECK ((peruntukan = ANY (ARRAY['TRANSFER'::text, 'RECEIVING'::text]))),
    CONSTRAINT fuel_route_config_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text]))),
    CONSTRAINT fuel_route_config_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'VALIDATED'::text, 'REJECTED'::text, 'INACTIVE'::text])))
);


--
-- Name: fuel_route_master; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_route_master (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    jalur_id uuid NOT NULL,
    tandon_id uuid NOT NULL,
    peruntukan text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fuel_route_master_peruntukan_check CHECK ((peruntukan = ANY (ARRAY['TRANSFER'::text, 'RECEIVING'::text])))
);


--
-- Name: fuel_route_master_invalid_archive; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_route_master_invalid_archive (
    id uuid,
    site_code text,
    jalur_id uuid,
    tandon_id uuid,
    peruntukan text,
    active boolean,
    notes text,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    archived_at timestamp with time zone,
    archive_reason text
);


--
-- Name: fuel_supply_plan; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_supply_plan (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    vendor_kode text NOT NULL,
    planned_l numeric(14,2) NOT NULL,
    planned_ritase integer NOT NULL,
    notes text,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fuel_supply_plan_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text]))),
    CONSTRAINT fuel_supply_plan_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'APPROVED'::text, 'DONE'::text])))
);


--
-- Name: fuel_tera_tangki_grid; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_tera_tangki_grid (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    unit_code text NOT NULL,
    fuel_truck_id uuid,
    dip_min numeric DEFAULT 0 NOT NULL,
    dip_step numeric DEFAULT 1 NOT NULL,
    max_dip numeric DEFAULT 0 NOT NULL,
    point_count integer DEFAULT 0 NOT NULL,
    volumes_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    source_sheet text,
    source_label text,
    source_file text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fuel_tx_fuel_truck_monitoring; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_tx_fuel_truck_monitoring (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    tanggal date NOT NULL,
    shift fcc.fuel_shift_type NOT NULL,
    monitoring_type fcc.fuel_monitoring_type NOT NULL,
    petugas_name text NOT NULL,
    fuel_truck_id uuid NOT NULL,
    fm_in numeric,
    fm_out numeric,
    hm_value numeric,
    notes text,
    voided_at timestamp with time zone,
    voided_by uuid,
    void_reason text,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_request_id uuid,
    CONSTRAINT fuel_monitoring_consistency CHECK ((((monitoring_type = 'FLOWMETER'::fcc.fuel_monitoring_type) AND (fm_in IS NOT NULL) AND (fm_out IS NOT NULL)) OR ((monitoring_type = 'HM'::fcc.fuel_monitoring_type) AND (hm_value IS NOT NULL))))
);


--
-- Name: fuel_tx_transfer_fuel; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_tx_transfer_fuel (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_code text DEFAULT 'PPA-BIB'::text NOT NULL,
    tanggal date NOT NULL,
    shift fcc.fuel_shift_type NOT NULL,
    petugas_name text NOT NULL,
    jalur_id uuid NOT NULL,
    tandon_id uuid NOT NULL,
    fuel_truck_id uuid NOT NULL,
    fm_awal numeric NOT NULL,
    fm_akhir numeric NOT NULL,
    tera_unit_awal numeric,
    tera_unit_akhir numeric,
    volume_tera_unit_awal numeric,
    volume_tera_unit_akhir numeric,
    catatan_deviasi text,
    no_urut integer,
    voided_at timestamp with time zone,
    voided_by uuid,
    void_reason text,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_request_id uuid
);


--
-- Name: fuel_user_staging; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.fuel_user_staging (
    nrp text NOT NULL,
    full_name text NOT NULL,
    jabatan text,
    default_role fcc.fuel_app_role DEFAULT 'FIELD'::fcc.fuel_app_role NOT NULL,
    imported_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fuel_v_fuel_truck_monitoring; Type: VIEW; Schema: fcc; Owner: -
--

CREATE VIEW fcc.fuel_v_fuel_truck_monitoring AS
 SELECT m.id,
    m.site_code,
    m.tanggal,
    m.shift,
    m.monitoring_type,
    m.petugas_name,
    m.fuel_truck_id,
    ft.unit_code AS fuel_truck_code,
    ft.unit_name AS fuel_truck_name,
    m.fm_in,
    m.fm_out,
    (m.fm_out - m.fm_in) AS total_fm_liter,
    m.hm_value,
    m.notes,
    m.voided_at,
    m.voided_by,
    m.void_reason,
    m.created_by,
    m.created_at,
    m.updated_at
   FROM (fcc.fuel_tx_fuel_truck_monitoring m
     LEFT JOIN fcc.fuel_master_fuel_truck ft ON ((ft.id = m.fuel_truck_id)));


--
-- Name: fuel_v_route_config; Type: VIEW; Schema: fcc; Owner: -
--

CREATE VIEW fcc.fuel_v_route_config AS
 SELECT m.id,
    m.site_code,
    CURRENT_DATE AS tanggal,
    s.shift,
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
        CASE
            WHEN (m.active AND (j.status = 'ACTIVE'::fcc.fuel_record_status) AND (t.status = 'ACTIVE'::fcc.fuel_record_status)) THEN 'VALIDATED'::text
            ELSE 'INACTIVE'::text
        END AS status,
    NULL::uuid AS validated_by,
    NULL::timestamp with time zone AS validated_at,
    m.notes,
    m.created_at,
    m.updated_at
   FROM (((fcc.fuel_route_master m
     JOIN fcc.fuel_master_jalur j ON ((j.id = m.jalur_id)))
     JOIN fcc.fuel_master_tandon t ON ((t.id = m.tandon_id)))
     CROSS JOIN ( VALUES ('SHIFT_1'::text), ('SHIFT_2'::text)) s(shift));


--
-- Name: fuel_v_transfer_fuel; Type: VIEW; Schema: fcc; Owner: -
--

CREATE VIEW fcc.fuel_v_transfer_fuel AS
 SELECT t.id,
    t.site_code,
    t.tanggal,
    t.shift,
    t.petugas_name,
    t.jalur_id,
    j.jalur_code,
    j.jalur_name,
    t.tandon_id,
    td.tandon_code,
    td.tandon_name,
    t.fuel_truck_id,
    ft.unit_code AS fuel_truck_code,
    ft.unit_name AS fuel_truck_name,
    t.fm_awal,
    t.fm_akhir,
    (t.fm_akhir - t.fm_awal) AS total_fm_liter,
    t.tera_unit_awal,
    t.tera_unit_akhir,
    (COALESCE(t.volume_tera_unit_akhir, (0)::numeric) - COALESCE(t.volume_tera_unit_awal, (0)::numeric)) AS total_volume_tera_liter,
    t.volume_tera_unit_awal,
    t.volume_tera_unit_akhir,
        CASE
            WHEN ((COALESCE(t.volume_tera_unit_akhir, (0)::numeric) - COALESCE(t.volume_tera_unit_awal, (0)::numeric)) = (0)::numeric) THEN NULL::numeric
            ELSE round(((((t.fm_akhir - t.fm_awal) - (COALESCE(t.volume_tera_unit_akhir, (0)::numeric) - COALESCE(t.volume_tera_unit_awal, (0)::numeric))) / NULLIF((COALESCE(t.volume_tera_unit_akhir, (0)::numeric) - COALESCE(t.volume_tera_unit_awal, (0)::numeric)), (0)::numeric)) * (100)::numeric), 2)
        END AS deviasi_tera_percent,
    ((t.fm_akhir - t.fm_awal) - (COALESCE(t.volume_tera_unit_akhir, (0)::numeric) - COALESCE(t.volume_tera_unit_awal, (0)::numeric))) AS selisih_fm_vs_tera,
        CASE
            WHEN (abs(((((t.fm_akhir - t.fm_awal) - (COALESCE(t.volume_tera_unit_akhir, (0)::numeric) - COALESCE(t.volume_tera_unit_awal, (0)::numeric))) / NULLIF((COALESCE(t.volume_tera_unit_akhir, (0)::numeric) - COALESCE(t.volume_tera_unit_awal, (0)::numeric)), (0)::numeric)) * (100)::numeric)) <= (1)::numeric) THEN 'OK'::text
            WHEN (abs(((((t.fm_akhir - t.fm_awal) - (COALESCE(t.volume_tera_unit_akhir, (0)::numeric) - COALESCE(t.volume_tera_unit_awal, (0)::numeric))) / NULLIF((COALESCE(t.volume_tera_unit_akhir, (0)::numeric) - COALESCE(t.volume_tera_unit_awal, (0)::numeric)), (0)::numeric)) * (100)::numeric)) <= (5)::numeric) THEN 'WARNING'::text
            ELSE 'CRITICAL'::text
        END AS status_deviasi,
    t.catatan_deviasi,
    t.no_urut,
    t.voided_at,
    t.voided_by,
    t.void_reason,
    t.created_by,
    t.created_at,
    t.updated_at
   FROM (((fcc.fuel_tx_transfer_fuel t
     LEFT JOIN fcc.fuel_master_jalur j ON ((j.id = t.jalur_id)))
     LEFT JOIN fcc.fuel_master_tandon td ON ((td.id = t.tandon_id)))
     LEFT JOIN fcc.fuel_master_fuel_truck ft ON ((ft.id = t.fuel_truck_id)));


--
-- Name: hour_meter; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.hour_meter (
    id bigint NOT NULL,
    kode text NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    fuel_truck text NOT NULL,
    nilai_hm numeric(12,2) NOT NULL,
    kondisi text,
    petugas text NOT NULL,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hour_meter_nilai_hm_check CHECK ((nilai_hm >= (0)::numeric)),
    CONSTRAINT hour_meter_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text]))),
    CONSTRAINT hour_meter_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'OK'::text, 'WARNING'::text, 'VOID'::text])))
);


--
-- Name: hour_meter_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.hour_meter ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.hour_meter_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: import_batch; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.import_batch (
    id bigint NOT NULL,
    kode text NOT NULL,
    sumber text NOT NULL,
    nama_file text NOT NULL,
    periode text NOT NULL,
    total_baris integer DEFAULT 0 NOT NULL,
    baris_valid integer DEFAULT 0 NOT NULL,
    baris_tolak integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'UPLOADED'::text NOT NULL,
    imported_by text NOT NULL,
    imported_at timestamp with time zone DEFAULT now() NOT NULL,
    source_format text,
    date_from date,
    date_to date,
    baris_mapped integer DEFAULT 0 NOT NULL,
    baris_unmapped integer DEFAULT 0 NOT NULL,
    baris_ambiguous integer DEFAULT 0 NOT NULL,
    CONSTRAINT import_batch_status_check CHECK ((status = ANY (ARRAY['UPLOADED'::text, 'VALIDATED'::text, 'COMMITTED'::text, 'SUPERSEDED'::text, 'REJECTED'::text])))
);


--
-- Name: import_batch_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.import_batch ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.import_batch_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: master_fuel_truck; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.master_fuel_truck (
    kode text NOT NULL,
    nama text NOT NULL,
    tipe text,
    kapasitas_l numeric(12,2),
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT master_fuel_truck_kapasitas_l_check CHECK ((kapasitas_l > (0)::numeric)),
    CONSTRAINT master_fuel_truck_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'INACTIVE'::text])))
);


--
-- Name: master_jalur; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.master_jalur (
    kode text NOT NULL,
    nama text NOT NULL,
    tujuan text,
    peruntukan text NOT NULL,
    site text DEFAULT 'PPA-BIB'::text NOT NULL,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT master_jalur_peruntukan_check CHECK ((peruntukan = ANY (ARRAY['TRANSFER'::text, 'RECEIVING'::text]))),
    CONSTRAINT master_jalur_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'INACTIVE'::text])))
);


--
-- Name: master_main_tank; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.master_main_tank (
    kode text NOT NULL,
    nama text NOT NULL,
    kapasitas_l numeric(12,2) NOT NULL,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT master_main_tank_kapasitas_l_check CHECK ((kapasitas_l > (0)::numeric)),
    CONSTRAINT master_main_tank_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'INACTIVE'::text])))
);


--
-- Name: master_unit; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.master_unit (
    kode text NOT NULL,
    nama text NOT NULL,
    vendor_kode text NOT NULL,
    kategori text NOT NULL,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT master_unit_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'INACTIVE'::text])))
);


--
-- Name: master_vendor; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.master_vendor (
    kode text NOT NULL,
    nama text NOT NULL,
    kategori text NOT NULL,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT master_vendor_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'INACTIVE'::text])))
);


--
-- Name: penerimaan_mo; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.penerimaan_mo (
    id bigint NOT NULL,
    kode text NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    id_ft text NOT NULL,
    no_polisi text,
    nama_driver text,
    jalur text NOT NULL,
    main_tank text NOT NULL,
    jam_start time without time zone,
    jam_stop time without time zone,
    fm_awal numeric(14,3) NOT NULL,
    fm_akhir numeric(14,3) NOT NULL,
    total_fm_l numeric(14,3) GENERATED ALWAYS AS ((fm_akhir - fm_awal)) STORED,
    petugas text NOT NULL,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    catatan text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    vendor_kode text,
    tera_depan_cm numeric(7,2) DEFAULT 0 NOT NULL,
    tera_belakang_cm numeric(7,2) DEFAULT 0 NOT NULL,
    selisih_tera_cm numeric(7,2),
    no_do text,
    kapasitas_l numeric(12,2),
    client_request_id uuid,
    tera_master_depan_cm numeric(7,2),
    tera_master_belakang_cm numeric(7,2),
    selisih_t_depan_cm numeric(7,2),
    selisih_t_belakang_cm numeric(7,2),
    selisih_t_depan_pct numeric(5,2),
    selisih_t_belakang_pct numeric(5,2),
    tera_status text,
    CONSTRAINT fm_penerimaan_naik CHECK ((fm_akhir >= fm_awal)),
    CONSTRAINT jam_penerimaan_urut CHECK (((jam_stop IS NULL) OR (jam_start IS NULL) OR (jam_stop >= jam_start))),
    CONSTRAINT penerimaan_mo_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text]))),
    CONSTRAINT penerimaan_mo_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'VALID'::text, 'WARNING'::text, 'VOID'::text]))),
    CONSTRAINT penerimaan_mo_tera_status_check CHECK (((tera_status IS NULL) OR (tera_status = ANY (ARRAY['OK'::text, 'WARNING'::text, 'CRITICAL'::text]))))
);


--
-- Name: penerimaan_mo_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

CREATE SEQUENCE fcc.penerimaan_mo_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: penerimaan_mo_id_seq; Type: SEQUENCE OWNED BY; Schema: fcc; Owner: -
--

ALTER SEQUENCE fcc.penerimaan_mo_id_seq OWNED BY fcc.penerimaan_mo.id;


--
-- Name: pengurasan; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.pengurasan (
    id bigint NOT NULL,
    kode text NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    jenis_aset text NOT NULL,
    aset text NOT NULL,
    sounding_awal_cm numeric(7,2) NOT NULL,
    sounding_akhir_cm numeric(7,2) NOT NULL,
    volume_awal_l numeric(14,3),
    volume_akhir_l numeric(14,3),
    fm_awal numeric(14,3) NOT NULL,
    fm_akhir numeric(14,3) NOT NULL,
    total_fm_l numeric(14,3) GENERATED ALWAYS AS ((fm_akhir - fm_awal)) STORED,
    selisih_sounding_l numeric(14,3) GENERATED ALWAYS AS ((volume_awal_l - volume_akhir_l)) STORED,
    deviasi_l numeric(14,3) GENERATED ALWAYS AS (((fm_akhir - fm_awal) - (volume_awal_l - volume_akhir_l))) STORED,
    petugas text NOT NULL,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    catatan text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_request_id uuid,
    CONSTRAINT pengurasan_jenis_aset_check CHECK ((jenis_aset = ANY (ARRAY['FUEL_TRUCK'::text, 'MAINTANK'::text]))),
    CONSTRAINT pengurasan_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text]))),
    CONSTRAINT pengurasan_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'OK'::text, 'WARNING'::text, 'TIDAK VALID'::text, 'VOID'::text])))
);


--
-- Name: pengurasan_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.pengurasan ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.pengurasan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: photo; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.photo (
    id bigint NOT NULL,
    modul text NOT NULL,
    record_id text NOT NULL,
    photo_type text NOT NULL,
    base64_data text NOT NULL,
    size_bytes integer,
    mime_type text,
    uploaded_by text NOT NULL,
    uploaded_at timestamp with time zone DEFAULT now() NOT NULL,
    storage_path text
);


--
-- Name: photo_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

CREATE SEQUENCE fcc.photo_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: photo_id_seq; Type: SEQUENCE OWNED BY; Schema: fcc; Owner: -
--

ALTER SEQUENCE fcc.photo_id_seq OWNED BY fcc.photo.id;


--
-- Name: ref_lookup; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.ref_lookup (
    jenis text NOT NULL,
    kode text NOT NULL,
    label text NOT NULL,
    keterangan text,
    urutan integer DEFAULT 0 NOT NULL,
    aktif boolean DEFAULT true NOT NULL
);


--
-- Name: TABLE ref_lookup; Type: COMMENT; Schema: fcc; Owner: -
--

COMMENT ON TABLE fcc.ref_lookup IS 'Pengganti sheet 02_REFERENSI. Semua dropdown ambil dari sini.';


--
-- Name: refuelling; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.refuelling (
    id bigint NOT NULL,
    no_voucher text NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    vendor_kode text NOT NULL,
    unit_kode text NOT NULL,
    fuel_truck text NOT NULL,
    volume_l numeric(12,3) NOT NULL,
    petugas text NOT NULL,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT refuelling_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text]))),
    CONSTRAINT refuelling_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'VALID'::text, 'VOID'::text]))),
    CONSTRAINT refuelling_volume_l_check CHECK ((volume_l > (0)::numeric))
);


--
-- Name: refuelling_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.refuelling ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.refuelling_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: shift_route_config; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.shift_route_config (
    id bigint NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    jalur text NOT NULL,
    main_tank text NOT NULL,
    fm_akhir_shift_sebelumnya numeric(14,3) NOT NULL,
    fm_aktual_awal numeric(14,3) NOT NULL,
    deviasi numeric(14,3) GENERATED ALWAYS AS ((fm_aktual_awal - fm_akhir_shift_sebelumnya)) STORED,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    validated_by text,
    validated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT shift_route_config_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text]))),
    CONSTRAINT shift_route_config_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'VALIDATED'::text, 'REJECTED'::text])))
);


--
-- Name: shift_route_config_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.shift_route_config ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.shift_route_config_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sounding_main_tank; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.sounding_main_tank (
    id bigint NOT NULL,
    kode text NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    main_tank text NOT NULL,
    petugas text NOT NULL,
    intank_cm numeric(7,2) NOT NULL,
    intank_l numeric(14,3),
    aktual_cm numeric(7,2) NOT NULL,
    aktual_l numeric(14,3),
    selisih_l numeric(14,3) GENERATED ALWAYS AS ((aktual_l - intank_l)) STORED,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_request_id uuid,
    intank_cm_master numeric(7,2),
    aktual_cm_master numeric(7,2),
    selisih_cm_intank numeric(7,2),
    selisih_cm_aktual numeric(7,2),
    sounding_status text,
    CONSTRAINT sounding_main_tank_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text]))),
    CONSTRAINT sounding_main_tank_sounding_status_check CHECK (((sounding_status IS NULL) OR (sounding_status = ANY (ARRAY['OK'::text, 'WARNING'::text, 'CRITICAL'::text, 'NO_MASTER'::text])))),
    CONSTRAINT sounding_main_tank_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'VALID'::text, 'WARNING'::text, 'VOID'::text])))
);


--
-- Name: sounding_main_tank_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.sounding_main_tank ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.sounding_main_tank_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sounding_table; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.sounding_table (
    aset text NOT NULL,
    dip_cm numeric(6,1) NOT NULL,
    volume_l numeric(12,3) NOT NULL,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT sounding_table_dip_cm_check CHECK ((dip_cm >= (0)::numeric)),
    CONSTRAINT sounding_table_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'INACTIVE'::text]))),
    CONSTRAINT sounding_table_volume_l_check CHECK ((volume_l >= (0)::numeric))
);


--
-- Name: COLUMN sounding_table.dip_cm; Type: COMMENT; Schema: fcc; Owner: -
--

COMMENT ON COLUMN fcc.sounding_table.dip_cm IS 'numeric, BUKAN float. float8 bikin 136.86 tidak pernah ketemu persis di tabel step 0,1.';


--
-- Name: transfer_fuel; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.transfer_fuel (
    id bigint NOT NULL,
    kode text NOT NULL,
    tanggal date NOT NULL,
    shift text NOT NULL,
    jalur text NOT NULL,
    main_tank text NOT NULL,
    fuel_truck text NOT NULL,
    petugas text NOT NULL,
    fm_awal numeric(14,3) NOT NULL,
    fm_akhir numeric(14,3) NOT NULL,
    sounding_awal_cm numeric(7,2) NOT NULL,
    sounding_akhir_cm numeric(7,2) NOT NULL,
    volume_awal_l numeric(14,3),
    volume_akhir_l numeric(14,3),
    total_fm_l numeric(14,3) GENERATED ALWAYS AS ((fm_akhir - fm_awal)) STORED,
    sounding_aktual_l numeric(14,3) GENERATED ALWAYS AS ((volume_awal_l - volume_akhir_l)) STORED,
    deviasi_l numeric(14,3) GENERATED ALWAYS AS (((fm_akhir - fm_awal) - (volume_awal_l - volume_akhir_l))) STORED,
    deviasi_pct numeric(8,4) GENERATED ALWAYS AS (
CASE
    WHEN ((fm_akhir - fm_awal) = (0)::numeric) THEN NULL::numeric
    ELSE ((abs(((fm_akhir - fm_awal) - (volume_awal_l - volume_akhir_l))) / (fm_akhir - fm_awal)) * (100)::numeric)
END) STORED,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    catatan_deviasi text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fm_transfer_naik CHECK ((fm_akhir >= fm_awal)),
    CONSTRAINT sounding_transfer_turun CHECK ((sounding_akhir_cm <= sounding_awal_cm)),
    CONSTRAINT transfer_fuel_shift_check CHECK ((shift = ANY (ARRAY['SHIFT_1'::text, 'SHIFT_2'::text]))),
    CONSTRAINT transfer_fuel_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'OK'::text, 'WARNING'::text, 'VOID'::text])))
);


--
-- Name: transfer_fuel_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.transfer_fuel ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.transfer_fuel_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: unit_alias; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.unit_alias (
    id bigint NOT NULL,
    unit_standar text NOT NULL,
    alias_ss6 text,
    alias_sap text,
    vendor_kode text,
    kategori text,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT unit_alias_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'INACTIVE'::text])))
);


--
-- Name: unit_alias_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.unit_alias ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.unit_alias_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: v_closing_line; Type: VIEW; Schema: fcc; Owner: -
--

CREATE VIEW fcc.v_closing_line AS
 SELECT l.id,
    h.id AS closing_id,
    h.tanggal,
    h.shift,
    h.status,
    h.penanggung_jawab,
    h.closed_at,
    l.aset,
    l.jenis,
    l.stock_awal_l AS stock_awal,
    l.penerimaan_l AS penerimaan,
    l.transfer_masuk_l AS transfer_in,
    l.transfer_keluar_l AS transfer_out,
    l.refuelling_l AS refuelling_out,
    l.total_administrasi_l AS administrasi,
    l.aktual_l AS aktual,
    l.deviasi_total_l AS deviasi,
    l.deviasi_pct,
    l.sounding_aktual_cm
   FROM (fcc.closing_stock_line l
     JOIN fcc.closing_stock h ON ((h.id = l.closing_id)));


--
-- Name: v_fuel_discrepancy_shift; Type: VIEW; Schema: fcc; Owner: -
--

CREATE VIEW fcc.v_fuel_discrepancy_shift AS
 WITH keys AS (
         SELECT 'PPA-BIB'::text AS site_code,
            cs.tanggal,
            cs.shift
           FROM fcc.closing_stock cs
        UNION
         SELECT 'PPA-BIB'::text AS text,
            p.tanggal,
            p.shift
           FROM fcc.penerimaan_mo p
          WHERE (COALESCE(p.status, 'VALID'::text) <> ALL (ARRAY['VOID'::text, 'DRAFT'::text]))
        UNION
         SELECT 'PPA-BIB'::text AS text,
            r.tanggal,
            r.shift
           FROM fcc.refuelling r
          WHERE (COALESCE(r.status, 'VALID'::text) <> ALL (ARRAY['VOID'::text, 'DRAFT'::text]))
        UNION
         SELECT m.site_code,
            m.tanggal,
            m.shift
           FROM fcc.fuel_discrepancy_manual m
        ), current_closing AS (
         SELECT cs.tanggal,
            cs.shift,
            cs.id AS closing_id,
            cs.status AS closing_status,
            sum(COALESCE(csl.stock_awal_l, (0)::numeric)) AS closing_stock_awal_l,
            sum(COALESCE(csl.penerimaan_l, (0)::numeric)) AS closing_penerimaan_l,
            sum(COALESCE(csl.refuelling_l, (0)::numeric)) AS closing_fuel_keluar_l,
                CASE
                    WHEN ((count(csl.id) = 0) OR (count(*) FILTER (WHERE ((csl.id IS NOT NULL) AND (csl.aktual_l IS NULL))) > 0)) THEN NULL::numeric
                    ELSE sum(COALESCE(csl.aktual_l, (0)::numeric))
                END AS closing_aktual_l,
            (count(csl.id))::integer AS asset_count,
            (count(csl.id) FILTER (WHERE (csl.aktual_l IS NOT NULL)))::integer AS actual_count
           FROM (fcc.closing_stock cs
             LEFT JOIN fcc.closing_stock_line csl ON ((csl.closing_id = cs.id)))
          GROUP BY cs.tanggal, cs.shift, cs.id, cs.status
        ), receipts AS (
         SELECT penerimaan_mo.tanggal,
            penerimaan_mo.shift,
            sum(COALESCE(penerimaan_mo.total_fm_l, (penerimaan_mo.fm_akhir - penerimaan_mo.fm_awal), (0)::numeric)) AS penerimaan_l,
            (count(*))::integer AS penerimaan_rows
           FROM fcc.penerimaan_mo
          WHERE (COALESCE(penerimaan_mo.status, 'VALID'::text) <> ALL (ARRAY['VOID'::text, 'DRAFT'::text]))
          GROUP BY penerimaan_mo.tanggal, penerimaan_mo.shift
        ), fuel_out AS (
         SELECT refuelling.tanggal,
            refuelling.shift,
            sum(COALESCE(refuelling.volume_l, (0)::numeric)) AS fuel_keluar_l,
            (count(*))::integer AS fuel_keluar_rows
           FROM fcc.refuelling
          WHERE (COALESCE(refuelling.status, 'VALID'::text) <> ALL (ARRAY['VOID'::text, 'DRAFT'::text]))
          GROUP BY refuelling.tanggal, refuelling.shift
        ), base AS (
         SELECT k.site_code,
            k.tanggal,
            k.shift,
            (to_char((k.tanggal)::timestamp with time zone, 'YYMMDD'::text) ||
                CASE
                    WHEN (k.shift = 'SHIFT_1'::text) THEN '-S1'::text
                    ELSE '-S2'::text
                END) AS kode,
            (EXTRACT(isoyear FROM k.tanggal))::integer AS iso_year,
            (EXTRACT(week FROM k.tanggal))::integer AS iso_week,
            (date_trunc('week'::text, (k.tanggal)::timestamp with time zone))::date AS week_start,
            (date_trunc('month'::text, (k.tanggal)::timestamp with time zone))::date AS month_start,
            (EXTRACT(day FROM k.tanggal))::integer AS day_of_month,
            TRIM(BOTH FROM to_char((k.tanggal)::timestamp with time zone, 'Day'::text)) AS hari,
            m.id AS manual_id,
            m.ba_l,
            m.adjustment_l,
            m.stock_awal_override_l,
            m.penerimaan_override_l,
            m.fuel_keluar_override_l,
            m.stock_aktual_override_l,
            m.cuaca,
            m.remark,
            m.pica_status,
            m.pica_owner,
            m.pica_due_date,
            m.pica_note,
            m.input_by,
            m.updated_by,
            m.updated_at,
            cc.closing_id,
            cc.closing_status,
            cc.closing_stock_awal_l,
            cc.closing_penerimaan_l,
            cc.closing_fuel_keluar_l,
            cc.closing_aktual_l,
            cc.asset_count,
            cc.actual_count,
            rc.penerimaan_l AS transaction_penerimaan_l,
            rc.penerimaan_rows,
            fo.fuel_keluar_l AS transaction_fuel_keluar_l,
            fo.fuel_keluar_rows,
                CASE
                    WHEN (k.shift = 'SHIFT_2'::text) THEN k.tanggal
                    ELSE (k.tanggal - 1)
                END AS prev_tanggal,
                CASE
                    WHEN (k.shift = 'SHIFT_2'::text) THEN 'SHIFT_1'::text
                    ELSE 'SHIFT_2'::text
                END AS prev_shift
           FROM ((((keys k
             LEFT JOIN fcc.fuel_discrepancy_manual m ON (((m.site_code = k.site_code) AND (m.tanggal = k.tanggal) AND (m.shift = k.shift))))
             LEFT JOIN current_closing cc ON (((cc.tanggal = k.tanggal) AND (cc.shift = k.shift))))
             LEFT JOIN receipts rc ON (((rc.tanggal = k.tanggal) AND (rc.shift = k.shift))))
             LEFT JOIN fuel_out fo ON (((fo.tanggal = k.tanggal) AND (fo.shift = k.shift))))
        ), with_previous AS (
         SELECT b.site_code,
            b.tanggal,
            b.shift,
            b.kode,
            b.iso_year,
            b.iso_week,
            b.week_start,
            b.month_start,
            b.day_of_month,
            b.hari,
            b.manual_id,
            b.ba_l,
            b.adjustment_l,
            b.stock_awal_override_l,
            b.penerimaan_override_l,
            b.fuel_keluar_override_l,
            b.stock_aktual_override_l,
            b.cuaca,
            b.remark,
            b.pica_status,
            b.pica_owner,
            b.pica_due_date,
            b.pica_note,
            b.input_by,
            b.updated_by,
            b.updated_at,
            b.closing_id,
            b.closing_status,
            b.closing_stock_awal_l,
            b.closing_penerimaan_l,
            b.closing_fuel_keluar_l,
            b.closing_aktual_l,
            b.asset_count,
            b.actual_count,
            b.transaction_penerimaan_l,
            b.penerimaan_rows,
            b.transaction_fuel_keluar_l,
            b.fuel_keluar_rows,
            b.prev_tanggal,
            b.prev_shift,
            pc.closing_id AS prev_closing_id,
            pc.closing_status AS prev_closing_status,
            pc.closing_aktual_l AS prev_closing_aktual_l,
            pm.stock_aktual_override_l AS prev_manual_aktual_l,
            ( SELECT sum(COALESCE(pl.total_administrasi_l, ((((pl.stock_awal_l + pl.penerimaan_l) + pl.transfer_masuk_l) - pl.transfer_keluar_l) - pl.refuelling_l), (0)::numeric)) AS sum
                   FROM (fcc.closing_stock pcs
                     JOIN fcc.closing_stock_line pl ON ((pl.closing_id = pcs.id)))
                  WHERE ((pcs.tanggal = b.prev_tanggal) AND (pcs.shift = b.prev_shift) AND (pcs.status = 'CLOSED'::text))) AS prev_closing_book_l
           FROM ((base b
             LEFT JOIN current_closing pc ON (((pc.tanggal = b.prev_tanggal) AND (pc.shift = b.prev_shift) AND (pc.closing_status = 'CLOSED'::text))))
             LEFT JOIN fcc.fuel_discrepancy_manual pm ON (((pm.site_code = b.site_code) AND (pm.tanggal = b.prev_tanggal) AND (pm.shift = b.prev_shift))))
        ), calculated AS (
         SELECT wp.site_code,
            wp.tanggal,
            wp.shift,
            wp.kode,
            wp.iso_year,
            wp.iso_week,
            wp.week_start,
            wp.month_start,
            wp.day_of_month,
            wp.hari,
            wp.manual_id,
            wp.ba_l,
            wp.adjustment_l,
            wp.stock_awal_override_l,
            wp.penerimaan_override_l,
            wp.fuel_keluar_override_l,
            wp.stock_aktual_override_l,
            wp.cuaca,
            wp.remark,
            wp.pica_status,
            wp.pica_owner,
            wp.pica_due_date,
            wp.pica_note,
            wp.input_by,
            wp.updated_by,
            wp.updated_at,
            wp.closing_id,
            wp.closing_status,
            wp.closing_stock_awal_l,
            wp.closing_penerimaan_l,
            wp.closing_fuel_keluar_l,
            wp.closing_aktual_l,
            wp.asset_count,
            wp.actual_count,
            wp.transaction_penerimaan_l,
            wp.penerimaan_rows,
            wp.transaction_fuel_keluar_l,
            wp.fuel_keluar_rows,
            wp.prev_tanggal,
            wp.prev_shift,
            wp.prev_closing_id,
            wp.prev_closing_status,
            wp.prev_closing_aktual_l,
            wp.prev_manual_aktual_l,
            wp.prev_closing_book_l,
            COALESCE(wp.stock_awal_override_l, wp.prev_closing_aktual_l, wp.prev_manual_aktual_l, (0)::numeric) AS stock_awal_l,
                CASE
                    WHEN (wp.stock_awal_override_l IS NOT NULL) THEN 'MANUAL_OVERRIDE'::text
                    WHEN (wp.prev_closing_aktual_l IS NOT NULL) THEN 'PREVIOUS_CLOSED_ACTUAL'::text
                    WHEN (wp.prev_manual_aktual_l IS NOT NULL) THEN 'PREVIOUS_MANUAL_ACTUAL'::text
                    ELSE 'NO_SOURCE'::text
                END AS opening_source,
            COALESCE(wp.penerimaan_override_l, NULLIF(wp.transaction_penerimaan_l, (0)::numeric), wp.closing_penerimaan_l, (0)::numeric) AS penerimaan_l,
                CASE
                    WHEN (wp.penerimaan_override_l IS NOT NULL) THEN 'MANUAL_OVERRIDE'::text
                    WHEN (NULLIF(wp.transaction_penerimaan_l, (0)::numeric) IS NOT NULL) THEN 'PENERIMAAN_MO'::text
                    WHEN (wp.closing_penerimaan_l IS NOT NULL) THEN 'CLOSING_LINE_FALLBACK'::text
                    ELSE 'NO_SOURCE'::text
                END AS penerimaan_source,
            COALESCE(wp.fuel_keluar_override_l, NULLIF(wp.transaction_fuel_keluar_l, (0)::numeric), wp.closing_fuel_keluar_l, (0)::numeric) AS fuel_keluar_l,
                CASE
                    WHEN (wp.fuel_keluar_override_l IS NOT NULL) THEN 'MANUAL_OVERRIDE'::text
                    WHEN (NULLIF(wp.transaction_fuel_keluar_l, (0)::numeric) IS NOT NULL) THEN 'REFUELLING_DB'::text
                    WHEN (wp.closing_fuel_keluar_l IS NOT NULL) THEN 'CLOSING_LINE_FALLBACK'::text
                    ELSE 'NO_SOURCE'::text
                END AS fuel_keluar_source,
            COALESCE(wp.stock_aktual_override_l, wp.closing_aktual_l) AS stock_aktual_l,
                CASE
                    WHEN (wp.stock_aktual_override_l IS NOT NULL) THEN 'MANUAL_OVERRIDE'::text
                    WHEN (wp.closing_aktual_l IS NOT NULL) THEN 'CLOSING_ACTUAL'::text
                    ELSE 'WAITING_ACTUAL'::text
                END AS actual_source
           FROM with_previous wp
        ), metrics AS (
         SELECT c.site_code,
            c.tanggal,
            c.shift,
            c.kode,
            c.iso_year,
            c.iso_week,
            c.week_start,
            c.month_start,
            c.day_of_month,
            c.hari,
            c.manual_id,
            c.ba_l,
            c.adjustment_l,
            c.stock_awal_override_l,
            c.penerimaan_override_l,
            c.fuel_keluar_override_l,
            c.stock_aktual_override_l,
            c.cuaca,
            c.remark,
            c.pica_status,
            c.pica_owner,
            c.pica_due_date,
            c.pica_note,
            c.input_by,
            c.updated_by,
            c.updated_at,
            c.closing_id,
            c.closing_status,
            c.closing_stock_awal_l,
            c.closing_penerimaan_l,
            c.closing_fuel_keluar_l,
            c.closing_aktual_l,
            c.asset_count,
            c.actual_count,
            c.transaction_penerimaan_l,
            c.penerimaan_rows,
            c.transaction_fuel_keluar_l,
            c.fuel_keluar_rows,
            c.prev_tanggal,
            c.prev_shift,
            c.prev_closing_id,
            c.prev_closing_status,
            c.prev_closing_aktual_l,
            c.prev_manual_aktual_l,
            c.prev_closing_book_l,
            c.stock_awal_l,
            c.opening_source,
            c.penerimaan_l,
            c.penerimaan_source,
            c.fuel_keluar_l,
            c.fuel_keluar_source,
            c.stock_aktual_l,
            c.actual_source,
            ((((c.stock_awal_l + c.penerimaan_l) + COALESCE(c.ba_l, (0)::numeric)) + COALESCE(c.adjustment_l, (0)::numeric)) - c.fuel_keluar_l) AS stock_akhir_buku_l,
                CASE
                    WHEN (c.stock_aktual_l IS NULL) THEN NULL::numeric
                    ELSE (c.stock_aktual_l - ((((c.stock_awal_l + c.penerimaan_l) + COALESCE(c.ba_l, (0)::numeric)) + COALESCE(c.adjustment_l, (0)::numeric)) - c.fuel_keluar_l))
                END AS discrepancy_l
           FROM calculated c
        ), windowed AS (
         SELECT m.site_code,
            m.tanggal,
            m.shift,
            m.kode,
            m.iso_year,
            m.iso_week,
            m.week_start,
            m.month_start,
            m.day_of_month,
            m.hari,
            m.manual_id,
            m.ba_l,
            m.adjustment_l,
            m.stock_awal_override_l,
            m.penerimaan_override_l,
            m.fuel_keluar_override_l,
            m.stock_aktual_override_l,
            m.cuaca,
            m.remark,
            m.pica_status,
            m.pica_owner,
            m.pica_due_date,
            m.pica_note,
            m.input_by,
            m.updated_by,
            m.updated_at,
            m.closing_id,
            m.closing_status,
            m.closing_stock_awal_l,
            m.closing_penerimaan_l,
            m.closing_fuel_keluar_l,
            m.closing_aktual_l,
            m.asset_count,
            m.actual_count,
            m.transaction_penerimaan_l,
            m.penerimaan_rows,
            m.transaction_fuel_keluar_l,
            m.fuel_keluar_rows,
            m.prev_tanggal,
            m.prev_shift,
            m.prev_closing_id,
            m.prev_closing_status,
            m.prev_closing_aktual_l,
            m.prev_manual_aktual_l,
            m.prev_closing_book_l,
            m.stock_awal_l,
            m.opening_source,
            m.penerimaan_l,
            m.penerimaan_source,
            m.fuel_keluar_l,
            m.fuel_keluar_source,
            m.stock_aktual_l,
            m.actual_source,
            m.stock_akhir_buku_l,
            m.discrepancy_l,
                CASE
                    WHEN ((m.discrepancy_l IS NULL) OR (m.fuel_keluar_l = (0)::numeric)) THEN NULL::numeric
                    ELSE ((m.discrepancy_l / NULLIF(m.fuel_keluar_l, (0)::numeric)) * (100)::numeric)
                END AS daily_deviasi_pct,
            sum(m.fuel_keluar_l) OVER (PARTITION BY m.site_code, m.iso_year, m.iso_week) AS weekly_gi_l,
            sum(COALESCE(m.discrepancy_l, (0)::numeric)) OVER (PARTITION BY m.site_code, m.iso_year, m.iso_week) AS weekly_discrepancy_l,
            sum(m.fuel_keluar_l) OVER (PARTITION BY m.site_code, m.month_start) AS monthly_gi_l,
            sum(COALESCE(m.discrepancy_l, (0)::numeric)) OVER (PARTITION BY m.site_code, m.month_start) AS monthly_discrepancy_l,
            first_value(m.stock_awal_l) OVER (PARTITION BY m.site_code, m.month_start ORDER BY m.tanggal,
                CASE m.shift
                    WHEN 'SHIFT_1'::text THEN 1
                    ELSE 2
                END) AS stock_awal_bulan_l,
            sum(m.fuel_keluar_l) OVER (PARTITION BY m.site_code, m.month_start ORDER BY m.tanggal,
                CASE m.shift
                    WHEN 'SHIFT_1'::text THEN 1
                    ELSE 2
                END ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS mtd_fuel_keluar_l
           FROM metrics m
        )
 SELECT site_code,
    tanggal,
    shift,
    kode,
    hari,
    iso_year,
    iso_week,
    week_start,
    month_start,
    day_of_month,
    stock_awal_l,
    opening_source,
    penerimaan_l,
    penerimaan_source,
    COALESCE(ba_l, (0)::numeric) AS ba_l,
    COALESCE(adjustment_l, (0)::numeric) AS adjustment_l,
    fuel_keluar_l,
    fuel_keluar_source,
    stock_akhir_buku_l,
    stock_aktual_l,
    actual_source,
    discrepancy_l,
    daily_deviasi_pct,
    weekly_gi_l,
    weekly_discrepancy_l,
        CASE
            WHEN (weekly_gi_l = (0)::numeric) THEN NULL::numeric
            ELSE ((weekly_discrepancy_l / NULLIF(weekly_gi_l, (0)::numeric)) * (100)::numeric)
        END AS weekly_deviasi_pct,
    monthly_gi_l,
    monthly_discrepancy_l,
        CASE
            WHEN (monthly_gi_l = (0)::numeric) THEN NULL::numeric
            ELSE ((monthly_discrepancy_l / NULLIF(monthly_gi_l, (0)::numeric)) * (100)::numeric)
        END AS mtd_discre_pct,
    stock_awal_bulan_l,
        CASE
            WHEN (day_of_month = 0) THEN NULL::numeric
            ELSE (monthly_gi_l / (day_of_month)::numeric)
        END AS average_used_daily_l,
        CASE
            WHEN ((monthly_gi_l = (0)::numeric) OR (day_of_month = 0)) THEN NULL::numeric
            ELSE (stock_akhir_buku_l / NULLIF((monthly_gi_l / (day_of_month)::numeric), (0)::numeric))
        END AS fuel_availability_days,
    discrepancy_l AS gain_loss_l,
    closing_id,
    closing_status,
    asset_count,
    actual_count,
    penerimaan_rows,
    fuel_keluar_rows,
    prev_tanggal,
    prev_shift,
    prev_closing_id,
    prev_closing_status,
    cuaca,
    remark,
    COALESCE(pica_status, 'N/A'::text) AS pica_status,
    pica_owner,
    pica_due_date,
    pica_note,
    input_by,
    updated_by,
    updated_at
   FROM windowed w;


--
-- Name: VIEW v_fuel_discrepancy_shift; Type: COMMENT; Schema: fcc; Owner: -
--

COMMENT ON VIEW fcc.v_fuel_discrepancy_shift IS 'Per-shift site discrepancy. Opening defaults to previous CLOSED actual; receipt from penerimaan_mo; out from refuelling; actual from closing_stock_line.';


--
-- Name: v_penerimaan_tera_check; Type: VIEW; Schema: fcc; Owner: -
--

CREATE VIEW fcc.v_penerimaan_tera_check AS
 SELECT r.id,
    r.tanggal,
    r.shift,
    r.id_ft,
    mo.no_polisi,
    r.kode,
    r.jalur,
    r.main_tank,
    r.fm_awal,
    r.fm_akhir,
    r.total_fm_l,
    mo.t2_depan_cm AS tera_master_depan_cm,
    mo.t2_belakang_cm AS tera_master_belakang_cm,
    r.tera_depan_cm AS tera_aktual_depan_cm,
    r.tera_belakang_cm AS tera_aktual_belakang_cm,
    (r.tera_depan_cm - mo.t2_depan_cm) AS selisih_t_depan_cm,
    (r.tera_belakang_cm - mo.t2_belakang_cm) AS selisih_t_belakang_cm,
        CASE
            WHEN ((mo.t2_depan_cm IS NOT NULL) AND (mo.t2_depan_cm > (0)::numeric)) THEN round((((r.tera_depan_cm - mo.t2_depan_cm) / mo.t2_depan_cm) * (100)::numeric), 2)
            ELSE NULL::numeric
        END AS selisih_t_depan_pct,
        CASE
            WHEN ((mo.t2_belakang_cm IS NOT NULL) AND (mo.t2_belakang_cm > (0)::numeric)) THEN round((((r.tera_belakang_cm - mo.t2_belakang_cm) / mo.t2_belakang_cm) * (100)::numeric), 2)
            ELSE NULL::numeric
        END AS selisih_t_belakang_pct,
        CASE
            WHEN ((mo.t2_depan_cm IS NULL) OR (r.tera_depan_cm IS NULL)) THEN 'NO_MASTER'::text
            WHEN (abs((r.tera_depan_cm - mo.t2_depan_cm)) <= 1.0) THEN 'OK'::text
            WHEN (abs((r.tera_depan_cm - mo.t2_depan_cm)) <= 3.0) THEN 'WARNING'::text
            ELSE 'CRITICAL'::text
        END AS tera_status,
    r.created_at
   FROM (fcc.penerimaan_mo r
     JOIN fcc.ft_mandar_ocean mo ON ((mo.id_ft = r.id_ft)));


--
-- Name: v_pengurasan; Type: VIEW; Schema: fcc; Owner: -
--

CREATE VIEW fcc.v_pengurasan AS
 SELECT id,
    kode,
    tanggal,
    shift,
    jenis_aset,
    aset,
    sounding_awal_cm,
    sounding_akhir_cm,
    volume_awal_l,
    volume_akhir_l,
    fm_awal,
    fm_akhir,
    total_fm_l,
    selisih_sounding_l,
    deviasi_l,
    petugas,
    status,
    catatan,
    created_at,
    updated_at,
    ( SELECT count(*) AS count
           FROM fcc.evidence e
          WHERE ((e.modul = 'pengurasan'::text) AND (e.record_id = p.id))) AS evidence_count
   FROM fcc.pengurasan p;


--
-- Name: v_rekonsiliasi; Type: VIEW; Schema: fcc; Owner: -
--

CREATE VIEW fcc.v_rekonsiliasi AS
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


--
-- Name: voucher_bib; Type: TABLE; Schema: fcc; Owner: -
--

CREATE TABLE fcc.voucher_bib (
    id bigint NOT NULL,
    no_voucher text NOT NULL,
    tanggal date NOT NULL,
    unit_kode text NOT NULL,
    liter numeric(12,3) NOT NULL,
    tanggal_ss6 date,
    tanggal_sap date,
    status text GENERATED ALWAYS AS (
CASE
    WHEN ((tanggal_ss6 IS NULL) OR (tanggal_sap IS NULL)) THEN 'BELUM MATCH'::text
    WHEN (tanggal_ss6 = tanggal_sap) THEN 'MATCH'::text
    WHEN (abs((tanggal_sap - tanggal_ss6)) <= 1) THEN 'MATCH BEDA TANGGAL'::text
    ELSE 'SELISIH'::text
END) STORED,
    remark text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: voucher_bib_id_seq; Type: SEQUENCE; Schema: fcc; Owner: -
--

ALTER TABLE fcc.voucher_bib ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME fcc.voucher_bib_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: fuel_discrepancy_manual id; Type: DEFAULT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_discrepancy_manual ALTER COLUMN id SET DEFAULT nextval('fcc.fuel_discrepancy_manual_id_seq'::regclass);


--
-- Name: penerimaan_mo id; Type: DEFAULT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.penerimaan_mo ALTER COLUMN id SET DEFAULT nextval('fcc.penerimaan_mo_id_seq'::regclass);


--
-- Name: photo id; Type: DEFAULT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.photo ALTER COLUMN id SET DEFAULT nextval('fcc.photo_id_seq'::regclass);


--
-- Name: app_config app_config_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.app_config
    ADD CONSTRAINT app_config_pkey PRIMARY KEY (parameter);


--
-- Name: app_user app_user_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.app_user
    ADD CONSTRAINT app_user_pkey PRIMARY KEY (id);


--
-- Name: audit_trail audit_trail_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.audit_trail
    ADD CONSTRAINT audit_trail_pkey PRIMARY KEY (id);


--
-- Name: cleanliness_filter_cost cleanliness_filter_cost_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.cleanliness_filter_cost
    ADD CONSTRAINT cleanliness_filter_cost_pkey PRIMARY KEY (id);


--
-- Name: cleanliness cleanliness_kode_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.cleanliness
    ADD CONSTRAINT cleanliness_kode_key UNIQUE (kode);


--
-- Name: cleanliness cleanliness_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.cleanliness
    ADD CONSTRAINT cleanliness_pkey PRIMARY KEY (id);


--
-- Name: closing_stock_line closing_stock_line_closing_id_aset_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.closing_stock_line
    ADD CONSTRAINT closing_stock_line_closing_id_aset_key UNIQUE (closing_id, aset);


--
-- Name: closing_stock_line closing_stock_line_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.closing_stock_line
    ADD CONSTRAINT closing_stock_line_pkey PRIMARY KEY (id);


--
-- Name: closing_stock closing_stock_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.closing_stock
    ADD CONSTRAINT closing_stock_pkey PRIMARY KEY (id);


--
-- Name: closing_stock closing_stock_tanggal_shift_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.closing_stock
    ADD CONSTRAINT closing_stock_tanggal_shift_key UNIQUE (tanggal, shift);


--
-- Name: evidence evidence_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.evidence
    ADD CONSTRAINT evidence_pkey PRIMARY KEY (id);


--
-- Name: flowmeter_ft flowmeter_ft_kode_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.flowmeter_ft
    ADD CONSTRAINT flowmeter_ft_kode_key UNIQUE (kode);


--
-- Name: flowmeter_ft flowmeter_ft_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.flowmeter_ft
    ADD CONSTRAINT flowmeter_ft_pkey PRIMARY KEY (id);


--
-- Name: flowmeter_ft flowmeter_ft_tanggal_shift_fuel_truck_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.flowmeter_ft
    ADD CONSTRAINT flowmeter_ft_tanggal_shift_fuel_truck_key UNIQUE (tanggal, shift, fuel_truck);


--
-- Name: ft_mandar_ocean ft_mandar_ocean_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.ft_mandar_ocean
    ADD CONSTRAINT ft_mandar_ocean_pkey PRIMARY KEY (id_ft);


--
-- Name: fuel_attachment_log fuel_attachment_log_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_attachment_log
    ADD CONSTRAINT fuel_attachment_log_pkey PRIMARY KEY (id);


--
-- Name: fuel_discrepancy_manual fuel_discrepancy_manual_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_discrepancy_manual
    ADD CONSTRAINT fuel_discrepancy_manual_pkey PRIMARY KEY (id);


--
-- Name: fuel_discrepancy_manual fuel_discrepancy_manual_site_code_tanggal_shift_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_discrepancy_manual
    ADD CONSTRAINT fuel_discrepancy_manual_site_code_tanggal_shift_key UNIQUE (site_code, tanggal, shift);


--
-- Name: fuel_fm_awal_settings fuel_fm_awal_settings_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_fm_awal_settings
    ADD CONSTRAINT fuel_fm_awal_settings_pkey PRIMARY KEY (id);


--
-- Name: fuel_fm_awal_settings fuel_fm_awal_settings_site_code_jalur_id_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_fm_awal_settings
    ADD CONSTRAINT fuel_fm_awal_settings_site_code_jalur_id_key UNIQUE (site_code, jalur_id);


--
-- Name: fuel_import_row fuel_import_row_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_import_row
    ADD CONSTRAINT fuel_import_row_pkey PRIMARY KEY (id);


--
-- Name: fuel_master_fuel_truck fuel_master_fuel_truck_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_master_fuel_truck
    ADD CONSTRAINT fuel_master_fuel_truck_pkey PRIMARY KEY (id);


--
-- Name: fuel_master_fuel_truck fuel_master_fuel_truck_site_code_unit_code_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_master_fuel_truck
    ADD CONSTRAINT fuel_master_fuel_truck_site_code_unit_code_key UNIQUE (site_code, unit_code);


--
-- Name: fuel_master_jalur fuel_master_jalur_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_master_jalur
    ADD CONSTRAINT fuel_master_jalur_pkey PRIMARY KEY (id);


--
-- Name: fuel_master_jalur fuel_master_jalur_site_code_jalur_code_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_master_jalur
    ADD CONSTRAINT fuel_master_jalur_site_code_jalur_code_key UNIQUE (site_code, jalur_code);


--
-- Name: fuel_master_tandon fuel_master_tandon_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_master_tandon
    ADD CONSTRAINT fuel_master_tandon_pkey PRIMARY KEY (id);


--
-- Name: fuel_master_tandon fuel_master_tandon_site_code_tandon_code_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_master_tandon
    ADD CONSTRAINT fuel_master_tandon_site_code_tandon_code_key UNIQUE (site_code, tandon_code);


--
-- Name: fuel_profiles fuel_profiles_nrp_unique; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_profiles
    ADD CONSTRAINT fuel_profiles_nrp_unique UNIQUE (nrp);


--
-- Name: fuel_profiles fuel_profiles_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_profiles
    ADD CONSTRAINT fuel_profiles_pkey PRIMARY KEY (id);


--
-- Name: fuel_route_config fuel_route_config_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_route_config
    ADD CONSTRAINT fuel_route_config_pkey PRIMARY KEY (id);


--
-- Name: fuel_route_config fuel_route_config_site_code_tanggal_shift_jalur_id_peruntuk_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_route_config
    ADD CONSTRAINT fuel_route_config_site_code_tanggal_shift_jalur_id_peruntuk_key UNIQUE (site_code, tanggal, shift, jalur_id, peruntukan);


--
-- Name: fuel_route_master fuel_route_master_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_route_master
    ADD CONSTRAINT fuel_route_master_pkey PRIMARY KEY (id);


--
-- Name: fuel_supply_plan fuel_supply_plan_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_supply_plan
    ADD CONSTRAINT fuel_supply_plan_pkey PRIMARY KEY (id);


--
-- Name: fuel_supply_plan fuel_supply_plan_tanggal_shift_vendor_kode_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_supply_plan
    ADD CONSTRAINT fuel_supply_plan_tanggal_shift_vendor_kode_key UNIQUE (tanggal, shift, vendor_kode);


--
-- Name: fuel_tera_tangki_grid fuel_tera_tangki_grid_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_tera_tangki_grid
    ADD CONSTRAINT fuel_tera_tangki_grid_pkey PRIMARY KEY (id);


--
-- Name: fuel_tx_fuel_truck_monitoring fuel_tx_fuel_truck_monitoring_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_tx_fuel_truck_monitoring
    ADD CONSTRAINT fuel_tx_fuel_truck_monitoring_pkey PRIMARY KEY (id);


--
-- Name: fuel_tx_transfer_fuel fuel_tx_transfer_fuel_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_tx_transfer_fuel
    ADD CONSTRAINT fuel_tx_transfer_fuel_pkey PRIMARY KEY (id);


--
-- Name: fuel_user_staging fuel_user_staging_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_user_staging
    ADD CONSTRAINT fuel_user_staging_pkey PRIMARY KEY (nrp);


--
-- Name: hour_meter hour_meter_kode_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.hour_meter
    ADD CONSTRAINT hour_meter_kode_key UNIQUE (kode);


--
-- Name: hour_meter hour_meter_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.hour_meter
    ADD CONSTRAINT hour_meter_pkey PRIMARY KEY (id);


--
-- Name: hour_meter hour_meter_tanggal_shift_fuel_truck_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.hour_meter
    ADD CONSTRAINT hour_meter_tanggal_shift_fuel_truck_key UNIQUE (tanggal, shift, fuel_truck);


--
-- Name: import_batch import_batch_kode_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.import_batch
    ADD CONSTRAINT import_batch_kode_key UNIQUE (kode);


--
-- Name: import_batch import_batch_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.import_batch
    ADD CONSTRAINT import_batch_pkey PRIMARY KEY (id);


--
-- Name: master_fuel_truck master_fuel_truck_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.master_fuel_truck
    ADD CONSTRAINT master_fuel_truck_pkey PRIMARY KEY (kode);


--
-- Name: master_jalur master_jalur_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.master_jalur
    ADD CONSTRAINT master_jalur_pkey PRIMARY KEY (kode);


--
-- Name: master_main_tank master_main_tank_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.master_main_tank
    ADD CONSTRAINT master_main_tank_pkey PRIMARY KEY (kode);


--
-- Name: master_unit master_unit_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.master_unit
    ADD CONSTRAINT master_unit_pkey PRIMARY KEY (kode);


--
-- Name: master_vendor master_vendor_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.master_vendor
    ADD CONSTRAINT master_vendor_pkey PRIMARY KEY (kode);


--
-- Name: penerimaan_mo penerimaan_mo_kode_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.penerimaan_mo
    ADD CONSTRAINT penerimaan_mo_kode_key UNIQUE (kode);


--
-- Name: penerimaan_mo penerimaan_mo_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.penerimaan_mo
    ADD CONSTRAINT penerimaan_mo_pkey PRIMARY KEY (id);


--
-- Name: pengurasan pengurasan_kode_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.pengurasan
    ADD CONSTRAINT pengurasan_kode_key UNIQUE (kode);


--
-- Name: pengurasan pengurasan_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.pengurasan
    ADD CONSTRAINT pengurasan_pkey PRIMARY KEY (id);


--
-- Name: photo photo_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.photo
    ADD CONSTRAINT photo_pkey PRIMARY KEY (id);


--
-- Name: ref_lookup ref_lookup_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.ref_lookup
    ADD CONSTRAINT ref_lookup_pkey PRIMARY KEY (jenis, kode);


--
-- Name: refuelling refuelling_no_voucher_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.refuelling
    ADD CONSTRAINT refuelling_no_voucher_key UNIQUE (no_voucher);


--
-- Name: refuelling refuelling_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.refuelling
    ADD CONSTRAINT refuelling_pkey PRIMARY KEY (id);


--
-- Name: shift_route_config shift_route_config_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.shift_route_config
    ADD CONSTRAINT shift_route_config_pkey PRIMARY KEY (id);


--
-- Name: shift_route_config shift_route_config_tanggal_shift_jalur_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.shift_route_config
    ADD CONSTRAINT shift_route_config_tanggal_shift_jalur_key UNIQUE (tanggal, shift, jalur);


--
-- Name: sounding_main_tank sounding_main_tank_kode_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.sounding_main_tank
    ADD CONSTRAINT sounding_main_tank_kode_key UNIQUE (kode);


--
-- Name: sounding_main_tank sounding_main_tank_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.sounding_main_tank
    ADD CONSTRAINT sounding_main_tank_pkey PRIMARY KEY (id);


--
-- Name: sounding_main_tank sounding_main_tank_tanggal_shift_main_tank_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.sounding_main_tank
    ADD CONSTRAINT sounding_main_tank_tanggal_shift_main_tank_key UNIQUE (tanggal, shift, main_tank);


--
-- Name: sounding_table sounding_table_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.sounding_table
    ADD CONSTRAINT sounding_table_pkey PRIMARY KEY (aset, dip_cm);


--
-- Name: transfer_fuel transfer_fuel_kode_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.transfer_fuel
    ADD CONSTRAINT transfer_fuel_kode_key UNIQUE (kode);


--
-- Name: transfer_fuel transfer_fuel_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.transfer_fuel
    ADD CONSTRAINT transfer_fuel_pkey PRIMARY KEY (id);


--
-- Name: unit_alias unit_alias_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.unit_alias
    ADD CONSTRAINT unit_alias_pkey PRIMARY KEY (id);


--
-- Name: voucher_bib voucher_bib_no_voucher_key; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.voucher_bib
    ADD CONSTRAINT voucher_bib_no_voucher_key UNIQUE (no_voucher);


--
-- Name: voucher_bib voucher_bib_pkey; Type: CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.voucher_bib
    ADD CONSTRAINT voucher_bib_pkey PRIMARY KEY (id);


--
-- Name: app_user_username_uq; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX app_user_username_uq ON fcc.app_user USING btree (upper(username));


--
-- Name: audit_trail_modul_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX audit_trail_modul_idx ON fcc.audit_trail USING btree (modul, waktu DESC);


--
-- Name: cleanliness_client_request_uidx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX cleanliness_client_request_uidx ON fcc.cleanliness USING btree (client_request_id) WHERE (client_request_id IS NOT NULL);


--
-- Name: cleanliness_filter_cost_replacement_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX cleanliness_filter_cost_replacement_idx ON fcc.cleanliness_filter_cost USING btree (replacement_date DESC);


--
-- Name: evidence_owner_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX evidence_owner_idx ON fcc.evidence USING btree (modul, record_id);


--
-- Name: fuel_attachment_monitor_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_attachment_monitor_idx ON fcc.fuel_attachment_log USING btree (monitoring_id);


--
-- Name: fuel_attachment_transfer_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_attachment_transfer_idx ON fcc.fuel_attachment_log USING btree (transfer_fuel_id);


--
-- Name: fuel_import_row_batch_source_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_import_row_batch_source_idx ON fcc.fuel_import_row USING btree (batch_id, sumber, source_row);


--
-- Name: fuel_import_unresolved_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_import_unresolved_idx ON fcc.fuel_import_row USING btree (alias_unit) WHERE (unit_standar IS NULL);


--
-- Name: fuel_profiles_app_user_id_uidx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX fuel_profiles_app_user_id_uidx ON fcc.fuel_profiles USING btree (app_user_id) WHERE (app_user_id IS NOT NULL);


--
-- Name: fuel_profiles_nrp_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_profiles_nrp_idx ON fcc.fuel_profiles USING btree (nrp);


--
-- Name: fuel_profiles_role_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_profiles_role_idx ON fcc.fuel_profiles USING btree (role);


--
-- Name: fuel_route_master_site_jalur_uidx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX fuel_route_master_site_jalur_uidx ON fcc.fuel_route_master USING btree (site_code, jalur_id);


--
-- Name: fuel_tera_tangki_grid_site_unit_uidx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX fuel_tera_tangki_grid_site_unit_uidx ON fcc.fuel_tera_tangki_grid USING btree (site_code, unit_code);


--
-- Name: fuel_tera_tangki_grid_unit_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_tera_tangki_grid_unit_idx ON fcc.fuel_tera_tangki_grid USING btree (site_code, unit_code);


--
-- Name: fuel_tx_monitor_tgl_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_tx_monitor_tgl_idx ON fcc.fuel_tx_fuel_truck_monitoring USING btree (tanggal);


--
-- Name: fuel_tx_monitor_truck_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_tx_monitor_truck_idx ON fcc.fuel_tx_fuel_truck_monitoring USING btree (fuel_truck_id);


--
-- Name: fuel_tx_monitoring_client_request_uidx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX fuel_tx_monitoring_client_request_uidx ON fcc.fuel_tx_fuel_truck_monitoring USING btree (client_request_id) WHERE (client_request_id IS NOT NULL);


--
-- Name: fuel_tx_transfer_client_request_uidx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX fuel_tx_transfer_client_request_uidx ON fcc.fuel_tx_transfer_fuel USING btree (client_request_id) WHERE (client_request_id IS NOT NULL);


--
-- Name: fuel_tx_transfer_fuel_jalur_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_tx_transfer_fuel_jalur_idx ON fcc.fuel_tx_transfer_fuel USING btree (jalur_id);


--
-- Name: fuel_tx_transfer_fuel_tandon_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_tx_transfer_fuel_tandon_idx ON fcc.fuel_tx_transfer_fuel USING btree (tandon_id);


--
-- Name: fuel_tx_transfer_fuel_tgl_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_tx_transfer_fuel_tgl_idx ON fcc.fuel_tx_transfer_fuel USING btree (tanggal);


--
-- Name: fuel_tx_transfer_fuel_truck_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX fuel_tx_transfer_fuel_truck_idx ON fcc.fuel_tx_transfer_fuel USING btree (fuel_truck_id);


--
-- Name: idx_fcc_photo_modul_record; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX idx_fcc_photo_modul_record ON fcc.photo USING btree (modul, record_id, uploaded_at DESC);


--
-- Name: idx_fcc_photo_uploaded_at; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX idx_fcc_photo_uploaded_at ON fcc.photo USING btree (uploaded_at DESC);


--
-- Name: idx_fuel_discrepancy_manual_period; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX idx_fuel_discrepancy_manual_period ON fcc.fuel_discrepancy_manual USING btree (site_code, tanggal, shift);


--
-- Name: idx_fuel_discrepancy_manual_pica; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX idx_fuel_discrepancy_manual_pica ON fcc.fuel_discrepancy_manual USING btree (pica_status, pica_due_date);


--
-- Name: ix_fuel_import_row_mapping_exception; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX ix_fuel_import_row_mapping_exception ON fcc.fuel_import_row USING btree (batch_id, mapping_status, alias_unit) WHERE (mapping_status = ANY (ARRAY['UNMAPPED'::text, 'AMBIGUOUS'::text]));


--
-- Name: ix_fuel_import_row_mapping_status; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX ix_fuel_import_row_mapping_status ON fcc.fuel_import_row USING btree (batch_id, mapping_status, tanggal);


--
-- Name: ix_fuel_import_row_source_format; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX ix_fuel_import_row_source_format ON fcc.fuel_import_row USING btree (batch_id, source_format, movement_type);


--
-- Name: master_unit_vendor_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX master_unit_vendor_idx ON fcc.master_unit USING btree (vendor_kode, kategori);


--
-- Name: penerimaan_mo_client_request_uidx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX penerimaan_mo_client_request_uidx ON fcc.penerimaan_mo USING btree (client_request_id) WHERE (client_request_id IS NOT NULL);


--
-- Name: penerimaan_mo_tgl_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX penerimaan_mo_tgl_idx ON fcc.penerimaan_mo USING btree (tanggal DESC, shift);


--
-- Name: pengurasan_client_request_uidx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX pengurasan_client_request_uidx ON fcc.pengurasan USING btree (client_request_id) WHERE (client_request_id IS NOT NULL);


--
-- Name: photo_owner_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX photo_owner_idx ON fcc.photo USING btree (modul, record_id);


--
-- Name: photo_type_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX photo_type_idx ON fcc.photo USING btree (modul, record_id, photo_type);


--
-- Name: refuelling_vendor_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX refuelling_vendor_idx ON fcc.refuelling USING btree (vendor_kode, tanggal DESC);


--
-- Name: sounding_main_tank_client_request_uidx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX sounding_main_tank_client_request_uidx ON fcc.sounding_main_tank USING btree (client_request_id) WHERE (client_request_id IS NOT NULL);


--
-- Name: transfer_fuel_tgl_idx; Type: INDEX; Schema: fcc; Owner: -
--

CREATE INDEX transfer_fuel_tgl_idx ON fcc.transfer_fuel USING btree (tanggal DESC, shift, fuel_truck);


--
-- Name: unit_alias_sap_uq; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX unit_alias_sap_uq ON fcc.unit_alias USING btree (upper(alias_sap)) WHERE ((alias_sap IS NOT NULL) AND (status = 'ACTIVE'::text));


--
-- Name: unit_alias_ss6_uq; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX unit_alias_ss6_uq ON fcc.unit_alias USING btree (upper(alias_ss6)) WHERE ((alias_ss6 IS NOT NULL) AND (status = 'ACTIVE'::text));


--
-- Name: ux_fuel_import_row_source_record; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX ux_fuel_import_row_source_record ON fcc.fuel_import_row USING btree (batch_id, source_record_id) WHERE ((source_record_id IS NOT NULL) AND (source_record_id <> ''::text));


--
-- Name: ux_import_batch_active_source_period; Type: INDEX; Schema: fcc; Owner: -
--

CREATE UNIQUE INDEX ux_import_batch_active_source_period ON fcc.import_batch USING btree (sumber, periode) WHERE (status = 'COMMITTED'::text);


--
-- Name: app_config trg_app_config_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_app_config_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.app_config FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: app_config trg_app_config_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_app_config_touch BEFORE UPDATE ON fcc.app_config FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: app_user trg_app_user_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_app_user_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.app_user FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: app_user trg_app_user_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_app_user_touch BEFORE UPDATE ON fcc.app_user FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: cleanliness trg_cleanliness_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_cleanliness_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.cleanliness FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: cleanliness trg_cleanliness_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_cleanliness_touch BEFORE UPDATE ON fcc.cleanliness FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: closing_stock trg_closing_stock_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_closing_stock_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.closing_stock FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: closing_stock_line trg_closing_stock_line_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_closing_stock_line_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.closing_stock_line FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: closing_stock trg_closing_stock_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_closing_stock_touch BEFORE UPDATE ON fcc.closing_stock FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: evidence trg_evidence_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_evidence_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.evidence FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: flowmeter_ft trg_flowmeter_ft_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_flowmeter_ft_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.flowmeter_ft FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: flowmeter_ft trg_flowmeter_ft_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_flowmeter_ft_touch BEFORE UPDATE ON fcc.flowmeter_ft FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: ft_mandar_ocean trg_ft_mandar_ocean_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_ft_mandar_ocean_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.ft_mandar_ocean FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: ft_mandar_ocean trg_ft_mandar_ocean_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_ft_mandar_ocean_touch BEFORE UPDATE ON fcc.ft_mandar_ocean FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: fuel_attachment_log trg_fuel_attachment_log_updated_at; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_fuel_attachment_log_updated_at BEFORE UPDATE ON fcc.fuel_attachment_log FOR EACH ROW EXECUTE FUNCTION fcc.fuel_set_updated_at();


--
-- Name: fuel_discrepancy_manual trg_fuel_discrepancy_manual_updated_at; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_fuel_discrepancy_manual_updated_at BEFORE UPDATE ON fcc.fuel_discrepancy_manual FOR EACH ROW EXECUTE FUNCTION fcc.touch_fuel_discrepancy_updated_at();


--
-- Name: fuel_fm_awal_settings trg_fuel_fm_awal_settings_updated_at; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_fuel_fm_awal_settings_updated_at BEFORE UPDATE ON fcc.fuel_fm_awal_settings FOR EACH ROW EXECUTE FUNCTION fcc.fuel_set_updated_at();


--
-- Name: fuel_master_fuel_truck trg_fuel_master_fuel_truck_updated_at; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_fuel_master_fuel_truck_updated_at BEFORE UPDATE ON fcc.fuel_master_fuel_truck FOR EACH ROW EXECUTE FUNCTION fcc.fuel_set_updated_at();


--
-- Name: fuel_master_jalur trg_fuel_master_jalur_updated_at; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_fuel_master_jalur_updated_at BEFORE UPDATE ON fcc.fuel_master_jalur FOR EACH ROW EXECUTE FUNCTION fcc.fuel_set_updated_at();


--
-- Name: fuel_master_tandon trg_fuel_master_tandon_updated_at; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_fuel_master_tandon_updated_at BEFORE UPDATE ON fcc.fuel_master_tandon FOR EACH ROW EXECUTE FUNCTION fcc.fuel_set_updated_at();


--
-- Name: fuel_profiles trg_fuel_profiles_updated_at; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_fuel_profiles_updated_at BEFORE UPDATE ON fcc.fuel_profiles FOR EACH ROW EXECUTE FUNCTION fcc.fuel_set_updated_at();


--
-- Name: fuel_route_config trg_fuel_route_config_updated_at; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_fuel_route_config_updated_at BEFORE UPDATE ON fcc.fuel_route_config FOR EACH ROW EXECUTE FUNCTION fcc.fuel_set_updated_at();


--
-- Name: fuel_tera_tangki_grid trg_fuel_tera_tangki_grid_updated_at; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_fuel_tera_tangki_grid_updated_at BEFORE UPDATE ON fcc.fuel_tera_tangki_grid FOR EACH ROW EXECUTE FUNCTION fcc.fuel_set_updated_at();


--
-- Name: fuel_tx_fuel_truck_monitoring trg_fuel_tx_fuel_truck_monitoring_updated_at; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_fuel_tx_fuel_truck_monitoring_updated_at BEFORE UPDATE ON fcc.fuel_tx_fuel_truck_monitoring FOR EACH ROW EXECUTE FUNCTION fcc.fuel_set_updated_at();


--
-- Name: fuel_tx_transfer_fuel trg_fuel_tx_transfer_fuel_updated_at; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_fuel_tx_transfer_fuel_updated_at BEFORE UPDATE ON fcc.fuel_tx_transfer_fuel FOR EACH ROW EXECUTE FUNCTION fcc.fuel_set_updated_at();


--
-- Name: hour_meter trg_hour_meter_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_hour_meter_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.hour_meter FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: hour_meter trg_hour_meter_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_hour_meter_touch BEFORE UPDATE ON fcc.hour_meter FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: import_batch trg_import_batch_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_import_batch_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.import_batch FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: master_fuel_truck trg_master_fuel_truck_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_master_fuel_truck_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.master_fuel_truck FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: master_fuel_truck trg_master_fuel_truck_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_master_fuel_truck_touch BEFORE UPDATE ON fcc.master_fuel_truck FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: master_jalur trg_master_jalur_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_master_jalur_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.master_jalur FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: master_jalur trg_master_jalur_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_master_jalur_touch BEFORE UPDATE ON fcc.master_jalur FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: master_main_tank trg_master_main_tank_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_master_main_tank_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.master_main_tank FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: master_main_tank trg_master_main_tank_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_master_main_tank_touch BEFORE UPDATE ON fcc.master_main_tank FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: master_unit trg_master_unit_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_master_unit_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.master_unit FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: master_unit trg_master_unit_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_master_unit_touch BEFORE UPDATE ON fcc.master_unit FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: master_vendor trg_master_vendor_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_master_vendor_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.master_vendor FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: master_vendor trg_master_vendor_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_master_vendor_touch BEFORE UPDATE ON fcc.master_vendor FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: penerimaan_mo trg_penerimaan_mo_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_penerimaan_mo_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.penerimaan_mo FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: penerimaan_mo trg_penerimaan_mo_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_penerimaan_mo_touch BEFORE UPDATE ON fcc.penerimaan_mo FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: pengurasan trg_pengurasan_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_pengurasan_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.pengurasan FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: pengurasan trg_pengurasan_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_pengurasan_touch BEFORE UPDATE ON fcc.pengurasan FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: ref_lookup trg_ref_lookup_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_ref_lookup_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.ref_lookup FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: refuelling trg_refuelling_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_refuelling_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.refuelling FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: refuelling trg_refuelling_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_refuelling_touch BEFORE UPDATE ON fcc.refuelling FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: shift_route_config trg_shift_route_config_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_shift_route_config_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.shift_route_config FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: shift_route_config trg_shift_route_config_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_shift_route_config_touch BEFORE UPDATE ON fcc.shift_route_config FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: sounding_main_tank trg_sounding_main_tank_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_sounding_main_tank_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.sounding_main_tank FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: sounding_main_tank trg_sounding_main_tank_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_sounding_main_tank_touch BEFORE UPDATE ON fcc.sounding_main_tank FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: sounding_table trg_sounding_table_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_sounding_table_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.sounding_table FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: sounding_table trg_sounding_table_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_sounding_table_touch BEFORE UPDATE ON fcc.sounding_table FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: transfer_fuel trg_transfer_fuel_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_transfer_fuel_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.transfer_fuel FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: transfer_fuel trg_transfer_fuel_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_transfer_fuel_touch BEFORE UPDATE ON fcc.transfer_fuel FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: transfer_fuel trg_transfer_volume; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_transfer_volume BEFORE INSERT OR UPDATE ON fcc.transfer_fuel FOR EACH ROW EXECUTE FUNCTION fcc.fill_transfer_volume();


--
-- Name: unit_alias trg_unit_alias_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_unit_alias_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.unit_alias FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: unit_alias trg_unit_alias_touch; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_unit_alias_touch BEFORE UPDATE ON fcc.unit_alias FOR EACH ROW EXECUTE FUNCTION fcc.set_updated_at();


--
-- Name: fuel_route_master trg_validate_fuel_route_master; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_validate_fuel_route_master BEFORE INSERT OR UPDATE OF jalur_id, tandon_id, peruntukan, active ON fcc.fuel_route_master FOR EACH ROW EXECUTE FUNCTION fcc.validate_fuel_route_master();


--
-- Name: voucher_bib trg_voucher_bib_audit; Type: TRIGGER; Schema: fcc; Owner: -
--

CREATE TRIGGER trg_voucher_bib_audit AFTER INSERT OR DELETE OR UPDATE ON fcc.voucher_bib FOR EACH ROW EXECUTE FUNCTION fcc.audit_row();


--
-- Name: closing_stock_line closing_stock_line_closing_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.closing_stock_line
    ADD CONSTRAINT closing_stock_line_closing_id_fkey FOREIGN KEY (closing_id) REFERENCES fcc.closing_stock(id) ON DELETE CASCADE;


--
-- Name: flowmeter_ft flowmeter_ft_fuel_truck_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.flowmeter_ft
    ADD CONSTRAINT flowmeter_ft_fuel_truck_fkey FOREIGN KEY (fuel_truck) REFERENCES fcc.master_fuel_truck(kode);


--
-- Name: fuel_attachment_log fuel_attachment_log_monitoring_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_attachment_log
    ADD CONSTRAINT fuel_attachment_log_monitoring_id_fkey FOREIGN KEY (monitoring_id) REFERENCES fcc.fuel_tx_fuel_truck_monitoring(id) ON DELETE CASCADE;


--
-- Name: fuel_attachment_log fuel_attachment_log_transfer_fuel_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_attachment_log
    ADD CONSTRAINT fuel_attachment_log_transfer_fuel_id_fkey FOREIGN KEY (transfer_fuel_id) REFERENCES fcc.fuel_tx_transfer_fuel(id) ON DELETE CASCADE;


--
-- Name: fuel_fm_awal_settings fuel_fm_awal_settings_jalur_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_fm_awal_settings
    ADD CONSTRAINT fuel_fm_awal_settings_jalur_id_fkey FOREIGN KEY (jalur_id) REFERENCES fcc.fuel_master_jalur(id) ON UPDATE CASCADE;


--
-- Name: fuel_route_config fuel_route_config_jalur_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_route_config
    ADD CONSTRAINT fuel_route_config_jalur_id_fkey FOREIGN KEY (jalur_id) REFERENCES fcc.fuel_master_jalur(id) ON UPDATE CASCADE;


--
-- Name: fuel_route_config fuel_route_config_tandon_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_route_config
    ADD CONSTRAINT fuel_route_config_tandon_id_fkey FOREIGN KEY (tandon_id) REFERENCES fcc.fuel_master_tandon(id) ON UPDATE CASCADE;


--
-- Name: fuel_route_master fuel_route_master_jalur_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_route_master
    ADD CONSTRAINT fuel_route_master_jalur_id_fkey FOREIGN KEY (jalur_id) REFERENCES fcc.fuel_master_jalur(id) ON UPDATE CASCADE;


--
-- Name: fuel_route_master fuel_route_master_tandon_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_route_master
    ADD CONSTRAINT fuel_route_master_tandon_id_fkey FOREIGN KEY (tandon_id) REFERENCES fcc.fuel_master_tandon(id) ON UPDATE CASCADE;


--
-- Name: fuel_tera_tangki_grid fuel_tera_tangki_grid_fuel_truck_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_tera_tangki_grid
    ADD CONSTRAINT fuel_tera_tangki_grid_fuel_truck_id_fkey FOREIGN KEY (fuel_truck_id) REFERENCES fcc.fuel_master_fuel_truck(id) ON UPDATE CASCADE;


--
-- Name: fuel_tx_fuel_truck_monitoring fuel_tx_fuel_truck_monitoring_fuel_truck_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_tx_fuel_truck_monitoring
    ADD CONSTRAINT fuel_tx_fuel_truck_monitoring_fuel_truck_id_fkey FOREIGN KEY (fuel_truck_id) REFERENCES fcc.fuel_master_fuel_truck(id) ON UPDATE CASCADE;


--
-- Name: fuel_tx_transfer_fuel fuel_tx_transfer_fuel_fuel_truck_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_tx_transfer_fuel
    ADD CONSTRAINT fuel_tx_transfer_fuel_fuel_truck_id_fkey FOREIGN KEY (fuel_truck_id) REFERENCES fcc.fuel_master_fuel_truck(id) ON UPDATE CASCADE;


--
-- Name: fuel_tx_transfer_fuel fuel_tx_transfer_fuel_jalur_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_tx_transfer_fuel
    ADD CONSTRAINT fuel_tx_transfer_fuel_jalur_id_fkey FOREIGN KEY (jalur_id) REFERENCES fcc.fuel_master_jalur(id) ON UPDATE CASCADE;


--
-- Name: fuel_tx_transfer_fuel fuel_tx_transfer_fuel_tandon_id_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.fuel_tx_transfer_fuel
    ADD CONSTRAINT fuel_tx_transfer_fuel_tandon_id_fkey FOREIGN KEY (tandon_id) REFERENCES fcc.fuel_master_tandon(id) ON UPDATE CASCADE;


--
-- Name: hour_meter hour_meter_fuel_truck_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.hour_meter
    ADD CONSTRAINT hour_meter_fuel_truck_fkey FOREIGN KEY (fuel_truck) REFERENCES fcc.master_fuel_truck(kode);


--
-- Name: master_unit master_unit_vendor_kode_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.master_unit
    ADD CONSTRAINT master_unit_vendor_kode_fkey FOREIGN KEY (vendor_kode) REFERENCES fcc.master_vendor(kode);


--
-- Name: penerimaan_mo penerimaan_mo_id_ft_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.penerimaan_mo
    ADD CONSTRAINT penerimaan_mo_id_ft_fkey FOREIGN KEY (id_ft) REFERENCES fcc.ft_mandar_ocean(id_ft) ON UPDATE CASCADE;


--
-- Name: penerimaan_mo penerimaan_mo_jalur_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.penerimaan_mo
    ADD CONSTRAINT penerimaan_mo_jalur_fkey FOREIGN KEY (jalur) REFERENCES fcc.master_jalur(kode);


--
-- Name: penerimaan_mo penerimaan_mo_main_tank_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.penerimaan_mo
    ADD CONSTRAINT penerimaan_mo_main_tank_fkey FOREIGN KEY (main_tank) REFERENCES fcc.master_main_tank(kode);


--
-- Name: penerimaan_mo penerimaan_mo_vendor_kode_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.penerimaan_mo
    ADD CONSTRAINT penerimaan_mo_vendor_kode_fkey FOREIGN KEY (vendor_kode) REFERENCES fcc.master_vendor(kode);


--
-- Name: refuelling refuelling_fuel_truck_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.refuelling
    ADD CONSTRAINT refuelling_fuel_truck_fkey FOREIGN KEY (fuel_truck) REFERENCES fcc.master_fuel_truck(kode);


--
-- Name: refuelling refuelling_unit_kode_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.refuelling
    ADD CONSTRAINT refuelling_unit_kode_fkey FOREIGN KEY (unit_kode) REFERENCES fcc.master_unit(kode);


--
-- Name: refuelling refuelling_vendor_kode_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.refuelling
    ADD CONSTRAINT refuelling_vendor_kode_fkey FOREIGN KEY (vendor_kode) REFERENCES fcc.master_vendor(kode);


--
-- Name: shift_route_config shift_route_config_jalur_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.shift_route_config
    ADD CONSTRAINT shift_route_config_jalur_fkey FOREIGN KEY (jalur) REFERENCES fcc.master_jalur(kode);


--
-- Name: shift_route_config shift_route_config_main_tank_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.shift_route_config
    ADD CONSTRAINT shift_route_config_main_tank_fkey FOREIGN KEY (main_tank) REFERENCES fcc.master_main_tank(kode);


--
-- Name: sounding_main_tank sounding_main_tank_main_tank_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.sounding_main_tank
    ADD CONSTRAINT sounding_main_tank_main_tank_fkey FOREIGN KEY (main_tank) REFERENCES fcc.master_main_tank(kode);


--
-- Name: transfer_fuel transfer_fuel_fuel_truck_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.transfer_fuel
    ADD CONSTRAINT transfer_fuel_fuel_truck_fkey FOREIGN KEY (fuel_truck) REFERENCES fcc.master_fuel_truck(kode);


--
-- Name: transfer_fuel transfer_fuel_jalur_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.transfer_fuel
    ADD CONSTRAINT transfer_fuel_jalur_fkey FOREIGN KEY (jalur) REFERENCES fcc.master_jalur(kode);


--
-- Name: transfer_fuel transfer_fuel_main_tank_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.transfer_fuel
    ADD CONSTRAINT transfer_fuel_main_tank_fkey FOREIGN KEY (main_tank) REFERENCES fcc.master_main_tank(kode);


--
-- Name: unit_alias unit_alias_unit_standar_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.unit_alias
    ADD CONSTRAINT unit_alias_unit_standar_fkey FOREIGN KEY (unit_standar) REFERENCES fcc.master_unit(kode) ON UPDATE CASCADE;


--
-- Name: unit_alias unit_alias_vendor_kode_fkey; Type: FK CONSTRAINT; Schema: fcc; Owner: -
--

ALTER TABLE ONLY fcc.unit_alias
    ADD CONSTRAINT unit_alias_vendor_kode_fkey FOREIGN KEY (vendor_kode) REFERENCES fcc.master_vendor(kode);


--
-- Name: refuelling; Type: ROW SECURITY; Schema: fcc; Owner: -
--

ALTER TABLE fcc.refuelling ENABLE ROW LEVEL SECURITY;

--
-- Name: refuelling refuelling_internal; Type: POLICY; Schema: fcc; Owner: -
--

CREATE POLICY refuelling_internal ON fcc.refuelling TO fcc_app USING ((COALESCE(NULLIF(current_setting('app.role'::text, true), ''::text), 'ADMIN'::text) <> 'VENDOR'::text));


--
-- Name: refuelling refuelling_vendor; Type: POLICY; Schema: fcc; Owner: -
--

CREATE POLICY refuelling_vendor ON fcc.refuelling FOR SELECT TO fcc_app USING (((current_setting('app.role'::text, true) = 'VENDOR'::text) AND (vendor_kode = current_setting('app.vendor'::text, true))));


--
-- PostgreSQL database dump complete
--

\unrestrict GhGgB4hPEIFl7xATe8bjYxQnqZWrHb1q1OyCzkFh3Wu1cQq5RLKiaFdzSI9U6sZ

