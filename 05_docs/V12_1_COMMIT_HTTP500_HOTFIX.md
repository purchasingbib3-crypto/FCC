# FCC V12.1 — Reporting Commit HTTP500 Hotfix

## Root cause addressed
Validation is read-only, while Commit writes tens of thousands of raw rows. V12 inserted
`fuel_import_row` one row at a time and the database still had an AFTER INSERT audit trigger
on that raw table. A SAP MB51 commit of ~39,799 rows therefore caused ~39,799 raw writes plus
~39,799 audit writes in one request, before index/transaction overhead.

## Fix
- migration `07_reporting_commit_reliability_v12_1.sql` drops raw-row audit trigger;
- `import_batch` remains audited and is the authoritative import audit event;
- backend Commit uses PostgreSQL COPY streaming instead of one INSERT per row;
- backend preflights the trigger and returns HTTP 409 with migration instructions instead of
  entering a known-bad write path;
- database exceptions return an operation ID and SQLSTATE while transaction rollback prevents
  partial active batches.

## Deploy order
Run migrations 05, 06, then 07. Restart backend. Re-Validate the same file, then Commit.
