# FCC — Fuel Control Center

Baseline source: **FCC V12.3 FAST Import Engine** (13 Aug 2026).

This repository contains the production-oriented Fuel Control Center source for Field Operations, Main Dashboard, Reporting, SAP/SS6 reconciliation, PostgreSQL migrations, deployment examples, and regression tests.

## Current baseline

- Field input reliability and mobile-first UX
- Main Dashboard control room
- Reporting Dashboard / Monthly Report / Reconciliation / Exception Center
- SAP MB51 + SS6 real-source parsing
- Canonical quantity contract (`quantity_source_l` + `volume_net_l`)
- PostgreSQL COPY commit engine
- Parse-once validation cache/token fast import path
- V12.3 regression gate

See `05_docs/README.md`, `05_docs/V12_3_FAST_IMPORT_PATCH_REPORT.md`, and `05_docs/HERMES_V12_3_GUARDRAIL_PROMPT.md`.

> Large database/data snapshot files are intentionally not stored in normal Git history. Restore operational data from the controlled VPS/database backup process.
