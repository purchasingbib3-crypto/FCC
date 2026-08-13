# FCC PPA-BIB — Bundle V12.4 (FAST Import + Merged Unit)

## Quick Info

- **Backend**: V12.4 (FastAPI + Calamine + token commit)
- **Frontend**: V12.3.1 (FAST validate UI + photo source tabs)
- **Database**: PostgreSQL lokal, schema `fcc`
- **Latest migration**: `09_merge_unit_alias_v12_4.sql`

## Versions

| Version | Status | Branch |
|---------|--------|--------|
| V12.3 | stable | `main` |
| V12.4 (merged unit) | experimental | `v12.4-merged-unit-alias` |
| V12.3.1 (frontend fix) | production | `fix-v12-3-1-fast-frontend` |

## Live URLs

- `https://fogdcbib.web.id/` → V7 Reporting Dashboard (preview)
- `https://fogdcbib.web.id/field/` → V8 Field Dashboard (V12.3 + Hermes patches)

## Deploy to fogdcbib.web.id

```bash
# Use the frontend from fix-v12-3-1-fast-frontend branch
cd /home/ubuntu/FCC
git checkout fix-v12-3-1-fast-frontend
git pull origin fix-v12-3-1-fast-frontend

# Copy frontend ke path yang di-serve
cp 03_frontend/*.html /home/ubuntu/fcc-field/
cp 03_frontend/app.js /home/ubuntu/fcc-field/
cp 03_frontend/fcc-client.js /home/ubuntu/fcc-field/
cp 03_frontend/styles.css /home/ubuntu/fcc-field/

# Backend running di /opt/fcc-staging/
# Frontend served via static_proxy port 8765 + Cloudflare
```

## Login

URL: https://fogdcbib.web.id/field/
Username: `<NRP>` (e.g., 81230108)
Password: `<sama dengan NRP>`
