from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql

from ..db import connection, fetch_all, fetch_one, qualified
from ..dependencies import current_user, require_capability, require_roles
from ..security import SessionUser, hash_password
from ..services.xlsx_import import normalize_unit

router = APIRouter(prefix="/api/v1/master", tags=["master-data"])


@dataclass(frozen=True)
class MasterSpec:
    table: str
    pk: str
    columns: tuple[str, ...]
    default_order: str
    generated: tuple[str, ...] = ()


SPECS: dict[str, MasterSpec] = {
    "users": MasterSpec(
        "app_user",
        "id",
        (
            "id", "username", "nama", "role", "vendor_kode", "status", "must_change_pw",
            "failed_logins", "locked_until", "last_login", "created_at", "updated_at",
        ),
        "username",
        ("id", "failed_logins", "locked_until", "last_login", "created_at", "updated_at"),
    ),
    "vendors": MasterSpec("master_vendor", "kode", ("kode", "nama", "kategori", "status", "created_at", "updated_at"), "kode", ("created_at", "updated_at")),
    "units": MasterSpec("master_unit", "kode", ("kode", "nama", "vendor_kode", "kategori", "status", "created_at", "updated_at"), "kode", ("created_at", "updated_at")),
    "receiving-trucks": MasterSpec(
        "ft_mandar_ocean",
        "id_ft",
        (
            "id_ft", "no_lambung", "no_polisi", "kapasitas_l", "t2_depan_cm", "t2_belakang_cm",
            "status", "masa_berlaku", "expired_komisioning", "created_at", "updated_at",
        ),
        "id_ft",
        ("created_at", "updated_at"),
    ),
    "main-tanks": MasterSpec("master_main_tank", "kode", ("kode", "nama", "kapasitas_l", "status", "created_at", "updated_at"), "kode", ("created_at", "updated_at")),
    "fuel-trucks": MasterSpec("master_fuel_truck", "kode", ("kode", "nama", "tipe", "kapasitas_l", "status", "created_at", "updated_at"), "kode", ("created_at", "updated_at")),
    "routes": MasterSpec("master_jalur", "kode", ("kode", "nama", "tujuan", "peruntukan", "site", "status", "created_at", "updated_at"), "kode", ("created_at", "updated_at")),

    "supply-plans": MasterSpec(
        "fuel_supply_plan",
        "id",
        ("id", "tanggal", "shift", "vendor_kode", "planned_l", "planned_ritase", "cutoff_time", "notes", "status", "created_by", "created_at", "updated_at"),
        "tanggal DESC, shift, vendor_kode",
        ("id", "created_by", "created_at", "updated_at"),
    ),
    "filter-costs": MasterSpec(
        "cleanliness_filter_cost",
        "id",
        ("id", "asset_scope", "asset_code", "jalur_code", "replacement_date", "filter_cost", "lifetime_days", "fuelpass_l", "cost_per_l", "status", "notes", "created_by", "created_at", "updated_at"),
        "replacement_date DESC, id DESC",
        ("id", "cost_per_l", "created_by", "created_at", "updated_at"),
    ),
}



def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in {"1", "TRUE", "T", "YES", "YA", "Y", "ON"}


def _supply_numeric_pk_if_required(conn, spec: MasterSpec, data: dict[str, Any]) -> None:
    if spec.pk != "id" or "id" in data:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_default,is_identity,data_type
            FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s AND column_name='id'
            """,
            ("fcc", spec.table),
        )
        meta = cur.fetchone()
        if not meta:
            return
        if meta.get("column_default") or str(meta.get("is_identity") or "").upper() == "YES":
            return
        if str(meta.get("data_type") or "").lower() not in {"bigint", "integer", "smallint"}:
            return
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"fcc.{spec.table}",))
        cur.execute(sql.SQL("SELECT COALESCE(max(id),0)+1 AS id FROM {}").format(qualified(spec.table)))
        data["id"] = int(cur.fetchone()["id"])


def _spec(resource: str) -> MasterSpec:
    if resource not in SPECS:
        raise HTTPException(status_code=404, detail="Master resource tidak dikenal")
    return SPECS[resource]


def _validate_unit_alias_contract(conn, data: dict[str, Any], current_pk: str | None = None) -> None:
    """Fail closed when normalized aliases can resolve to multiple master units.

    Reconciliation deliberately strips spaces/hyphens/punctuation. Therefore two
    active standards such as ``TR 2032`` and ``TR-2032`` are ambiguous and must
    be fixed in master data rather than silently choosing one.
    """
    current: dict[str, Any] = {}
    with conn.cursor() as cur:
        if current_pk:
            cur.execute(
                sql.SQL("SELECT id,unit_standar,alias_ss6,alias_sap,status FROM {} WHERE id=%s").format(qualified("unit_alias")),
                (current_pk,),
            )
            current = cur.fetchone() or {}
        merged = {**current, **data}
        standard = str(merged.get("unit_standar") or "").strip()
        if not standard:
            raise HTTPException(status_code=422, detail="unit_standar wajib diisi")
        cur.execute(
            sql.SQL("SELECT kode,status FROM {} WHERE kode=%s LIMIT 1").format(qualified("master_unit")),
            (standard,),
        )
        master = cur.fetchone()
        if not master or str(master.get("status") or "").upper() != "ACTIVE":
            raise HTTPException(status_code=422, detail=f"Master unit aktif tidak ditemukan: {standard}")

        candidate_values = [standard, merged.get("alias_ss6"), merged.get("alias_sap")]
        candidate_keys = {normalize_unit(value) for value in candidate_values if normalize_unit(value)}
        if not candidate_keys:
            raise HTTPException(status_code=422, detail="Minimal satu alias unit harus valid")

        cur.execute(sql.SQL("SELECT kode FROM {} WHERE status='ACTIVE'").format(qualified("master_unit")))
        for row in cur.fetchall():
            other = str(row.get("kode") or "")
            if other == standard:
                continue
            if normalize_unit(other) in candidate_keys:
                raise HTTPException(
                    status_code=409,
                    detail=f"Alias ambigu: normalisasi bertabrakan dengan master unit aktif {other}. Nonaktifkan/koreksi master duplikat terlebih dahulu.",
                )

        cur.execute(
            sql.SQL("SELECT id,unit_standar,alias_ss6,alias_sap FROM {} WHERE status='ACTIVE'").format(qualified("unit_alias"))
        )
        for row in cur.fetchall():
            if current_pk and str(row.get("id")) == str(current_pk):
                continue
            other_standard = str(row.get("unit_standar") or "")
            if other_standard == standard:
                continue
            other_keys = {
                normalize_unit(value)
                for value in (row.get("unit_standar"), row.get("alias_ss6"), row.get("alias_sap"))
                if normalize_unit(value)
            }
            overlap = candidate_keys & other_keys
            if overlap:
                key = sorted(overlap)[0]
                raise HTTPException(
                    status_code=409,
                    detail=f"Alias ambigu: {key} juga mengarah ke unit {other_standard}. Perbaiki collision sebelum menyimpan.",
                )


@router.get("")
def list_resources(_: SessionUser = Depends(require_capability("master.read"))) -> dict:
    return {"ok": True, "resources": {k: {"table": v.table, "pk": v.pk, "columns": v.columns} for k, v in SPECS.items()}}


@router.get("/{resource}")
def list_master(
    resource: str,
    q: str = "",
    status: str = "",
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    _: SessionUser = Depends(require_capability("master.read")),
) -> dict:
    spec = _spec(resource)
    filters: list[sql.Composed] = []
    params: list[Any] = []
    if q:
        text_cols = [c for c in spec.columns if c not in spec.generated]
        filters.append(
            sql.SQL("(")
            + sql.SQL(" OR ").join(
                sql.SQL("COALESCE({}::text,'') ILIKE %s").format(sql.Identifier(c)) for c in text_cols
            )
            + sql.SQL(")")
        )
        params.extend([f"%{q}%"] * len(text_cols))
    if status and "status" in spec.columns:
        filters.append(sql.SQL("status=%s"))
        params.append(status)
    where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(filters) if filters else sql.SQL("")
    # default_order is hardcoded registry text, never user input.
    query = sql.SQL("SELECT {} FROM {}{} ORDER BY {} LIMIT %s OFFSET %s").format(
        sql.SQL(",").join(map(sql.Identifier, spec.columns)),
        qualified(spec.table),
        where,
        sql.SQL(spec.default_order),
    )
    count_query = sql.SQL("SELECT count(*)::int AS n FROM {}{}").format(qualified(spec.table), where)
    rows = fetch_all(query, [*params, limit, offset])
    count = fetch_one(count_query, params) or {"n": 0}
    return {"ok": True, "resource": resource, "table": f"fcc.{spec.table}", "data": rows, "total": count["n"], "limit": limit, "offset": offset}


@router.post("/{resource}")
def create_master(
    resource: str,
    payload: dict[str, Any],
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
) -> dict:
    spec = _spec(resource)
    data = {k: v for k, v in payload.items() if k in spec.columns and k not in spec.generated}
    if resource == "users":
        password = str(payload.get("password") or "")
        if len(password) < 8:
            raise HTTPException(status_code=422, detail="Password awal minimal 8 karakter")
        data["password_hash"] = hash_password(password)
        data["must_change_pw"] = _as_bool(data.get("must_change_pw", True))
        allowed_extra = {"password_hash"}
    else:
        allowed_extra = set()
    if resource in {"supply-plans", "filter-costs"}:
        data["created_by"] = user.username
    if not data:
        raise HTTPException(status_code=422, detail="Tidak ada kolom yang dapat disimpan")
    with connection() as conn:
        if resource == "unit-aliases":
            _validate_unit_alias_contract(conn, data)
        _supply_numeric_pk_if_required(conn, spec, data)
        columns = list(data)
        values = [data[k] for k in columns]
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            qualified(spec.table),
            sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join([sql.Placeholder()] * len(columns)),
        )
        with conn.cursor() as cur:
            try:
                cur.execute(query, values)
                row = cur.fetchone()
            except Exception as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
    row.pop("password_hash", None)
    return {"ok": True, "data": row, "created_by": user.username}


@router.patch("/{resource}/{pk}")
def update_master(
    resource: str,
    pk: str,
    payload: dict[str, Any],
    user: SessionUser = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
) -> dict:
    spec = _spec(resource)
    data = {k: v for k, v in payload.items() if k in spec.columns and k not in spec.generated and k != spec.pk}
    if resource == "users" and payload.get("password"):
        data["password_hash"] = hash_password(str(payload["password"]))
        data["must_change_pw"] = True
    if not data:
        raise HTTPException(status_code=422, detail="Tidak ada perubahan valid")
    if resource == "unit-aliases":
        with connection() as conn:
            _validate_unit_alias_contract(conn, data, current_pk=pk)
    setters = sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in data)
    if "updated_at" in spec.columns:
        setters = setters + sql.SQL(",updated_at=now()")
    query = sql.SQL("UPDATE {} SET {} WHERE {}=%s RETURNING *").format(
        qualified(spec.table), setters, sql.Identifier(spec.pk)
    )
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, [*data.values(), pk])
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Data tidak ditemukan")
    row.pop("password_hash", None)
    return {"ok": True, "data": row, "updated_by": user.username}


@router.delete("/{resource}/{pk}")
def delete_master(
    resource: str,
    pk: str,
    _: SessionUser = Depends(require_roles("SUPER_ADMIN")),
) -> dict:
    spec = _spec(resource)
    # Prefer a constraint-compatible soft-delete state.
    if "status" in spec.columns:
        inactive_value = "CANCELLED" if resource in {"supply-plans", "filter-costs"} else "INACTIVE"
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("UPDATE {} SET status=%s{} WHERE {}=%s RETURNING *").format(
                        qualified(spec.table),
                        sql.SQL(",updated_at=now()") if "updated_at" in spec.columns else sql.SQL(""),
                        sql.Identifier(spec.pk),
                    ),
                    (inactive_value, pk),
                )
                row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Data tidak ditemukan")
        return {"ok": True, "soft_deleted": True, "data": row}
    raise HTTPException(status_code=409, detail="Resource ini tidak mendukung soft delete")
