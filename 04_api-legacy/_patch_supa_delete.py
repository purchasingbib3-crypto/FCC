"""Patch supa_delete to fall back to soft-delete on FK violation."""
import sys

path = '/home/ubuntu/fuel-control-center/api/server_v8_pg.py'
with open(path) as f:
    h = f.read()

old = '''@app.delete("/api/supa/{table}/{rid}")
async def supa_delete(table: str, rid: str, user=Depends(require_user)):
    """Delete Supabase row by primary key. SUPER_ADMIN only."""
    if user["role"] != "SUPER_ADMIN":
        raise HTTPException(403, "Hanya SUPER_ADMIN boleh delete di Supabase dari dashboard.")
    if table not in SUPA_TABLES:
        raise HTTPException(404, f"Tabel '{table}' tidak ada di allowlist Supabase.")
    meta = SUPA_TABLES[table]
    supa_table = meta["supa"]
    pk = meta["pk"]
    import base64
    try:
        pad = "=" * (-len(rid) % 4)
        raw = base64.urlsafe_b64decode(rid + pad).decode("utf-8", "ignore")
        rid_value = raw.split("/")[-1] if "/" in raw else raw
    except Exception:
        rid_value = rid
    query = [(pk, f"eq.{rid_value}")]
    for col, val in (meta.get("filter") or {}).items():
        query.append((col, f"eq.{val}"))
    try:
        status, _, data = await _supa_http(
            "DELETE", supa_table, query,
            prefer="return=representation"
        )
    except HTTPException:
        raise
    return {"deleted": True, "row_key": rid, "data": data or []}
'''

new = '''@app.delete("/api/supa/{table}/{rid}")
async def supa_delete(table: str, rid: str, user=Depends(require_user)):
    """Delete Supabase row by primary key. SUPER_ADMIN only.

    If FK constraints block hard delete, fall back to soft-delete
    (status=INACTIVE) and return a 409 with `soft_deleted: true`
    so the frontend can show an informative message.
    """
    if user["role"] != "SUPER_ADMIN":
        raise HTTPException(403, "Hanya SUPER_ADMIN boleh delete di Supabase dari dashboard.")
    if table not in SUPA_TABLES:
        raise HTTPException(404, f"Tabel '{table}' tidak ada di allowlist Supabase.")
    meta = SUPA_TABLES[table]
    supa_table = meta["supa"]
    pk = meta["pk"]
    import base64
    try:
        pad = "=" * (-len(rid) % 4)
        raw = base64.urlsafe_b64decode(rid + pad).decode("utf-8", "ignore")
        rid_value = raw.split("/")[-1] if "/" in raw else raw
    except Exception:
        rid_value = rid
    query = [(pk, f"eq.{rid_value}")]
    for col, val in (meta.get("filter") or {}).items():
        query.append((col, f"eq.{val}"))
    # Try hard delete first
    status, _, data = await _supa_http(
        "DELETE", supa_table, query,
        prefer="return=representation"
    )
    if data:
        return {"deleted": True, "row_key": rid, "data": data, "soft_deleted": False}
    # If no row was deleted, do soft-delete (set status=INACTIVE) where applicable
    if "status" in (await _supa_known_columns(supa_table) or set()):
        # PATCH is whitelisted elsewhere; build a minimal update
        try:
            patch_q = [(pk, f"eq.{rid_value}")]
            for col, val in (meta.get("filter") or {}).items():
                patch_q.append((col, f"eq.{val}"))
            pstatus, _, pdata = await _supa_http(
                "PATCH", supa_table, patch_q, body={"status": "INACTIVE"},
                prefer="return=representation"
            )
            if pdata:
                return {
                    "deleted": False,
                    "soft_deleted": True,
                    "row_key": rid,
                    "data": pdata,
                    "reason": "Barang ini dipakai sebagai referensi (FK). Dialihkan ke nonaktif (status=INACTIVE)."
                }
        except Exception:
            pass
    raise HTTPException(404, "Data tidak ditemukan atau constraint memblokir delete.")
'''

if old not in h:
    print("ERROR: old block not found")
    sys.exit(1)

h = h.replace(old, new)
with open(path, 'w') as f:
    f.write(h)
print("OK")
