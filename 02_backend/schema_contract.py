from __future__ import annotations

from dataclasses import dataclass

from .config import get_settings
from .db import fetch_all

settings = get_settings()

REQUIRED: dict[str, set[str]] = {
    "app_user": {"id", "username", "role", "status", "password_hash", "failed_logins", "locked_until"},
    "fuel_profiles": {"id", "app_user_id", "nrp", "role", "status"},
    "fuel_import_row": {"batch_id", "sumber", "tanggal", "alias_unit", "unit_standar", "liter", "quantity_source_l", "volume_net_l", "shift", "storage_location", "source_row", "source_format", "source_record_id", "movement_type", "material", "uom", "mapping_status"},
    "import_batch": {"id", "sumber", "periode", "status", "total_baris", "baris_valid", "baris_tolak", "imported_at", "source_format", "date_from", "date_to", "baris_mapped", "baris_unmapped", "baris_ambiguous"},
    "master_unit": {"kode", "nama", "vendor_kode", "kategori", "status"},
    "unit_alias": {"id", "unit_standar", "alias_ss6", "alias_sap", "status"},
    "voucher_bib": {"id", "no_voucher", "tanggal", "unit_kode", "liter", "status"},
    "fuel_route_master": {"id", "site_code", "jalur_id", "tandon_id", "peruntukan", "active"},
    "fuel_tera_tangki_grid": {"site_code", "unit_code", "volumes_json"},
    "fuel_supply_plan": {"tanggal", "shift", "vendor_kode", "planned_l", "planned_ritase"},
    "cleanliness_filter_cost": {"replacement_date", "filter_cost", "cost_per_l"},
    "photo": {"modul", "record_id", "photo_type", "storage_path", "base64_data"},
    "fuel_tx_transfer_fuel": {"id", "jalur_id", "tandon_id", "fuel_truck_id", "fm_awal", "fm_akhir", "client_request_id"},
    "fuel_tx_fuel_truck_monitoring": {"id", "monitoring_type", "fuel_truck_id", "client_request_id"},
    "penerimaan_mo": {"id", "kode", "id_ft", "fm_awal", "fm_akhir", "client_request_id"},
    "pengurasan": {"id", "kode", "jenis_aset", "aset", "fm_awal", "fm_akhir", "client_request_id"},
    "sounding_main_tank": {"id", "kode", "main_tank", "intank_cm", "aktual_cm", "client_request_id"},
    "cleanliness": {"id", "kode", "jenis", "aset", "after_4", "after_6", "after_14", "client_request_id"},
}


def schema_contract_status() -> dict:
    rows = fetch_all(
        """
        SELECT table_name,column_name
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name = ANY(%s)
        """,
        (settings.database_schema, list(REQUIRED)),
    )
    seen: dict[str, set[str]] = {}
    for row in rows:
        seen.setdefault(row["table_name"], set()).add(row["column_name"])
    missing_tables = sorted(table for table in REQUIRED if table not in seen)
    missing_columns = {
        table: sorted(columns - seen.get(table, set()))
        for table, columns in REQUIRED.items()
        if columns - seen.get(table, set())
    }
    return {
        "ok": not missing_tables and not missing_columns,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
    }


def validate_schema_contract() -> None:
    status = schema_contract_status()
    if not status["ok"]:
        raise RuntimeError(
            "FCC database schema contract tidak terpenuhi. Jalankan migration 01_database/03_patch_all_20260811.sql, 04_field_reliability_v7.sql, 05_reporting_reliability_v9.sql, 06_reporting_real_sources_v10.sql, 07_reporting_commit_reliability_v12_1.sql, lalu 08_reporting_canonical_volume_v12_2.sql. "
            f"missing_tables={status['missing_tables']} missing_columns={status['missing_columns']}"
        )
