# Clockify emergency backup

This command makes a local, read-only snapshot before Clockify access or paid
features are lost. It never sends `PUT`, `PATCH`, or `DELETE`. `POST` is allowed
only for documented read-only report and search endpoints.

## Run it

From the project folder:

```powershell
python kimai_api.py backup-clockify
```

The script reads `CLOCKIFY_API_KEY` from `.env` at runtime. It does not read the
Kimai token for this command. By default all workspaces returned by Clockify are
included. To limit a deliberate second run, repeat `--workspace-id`:

```powershell
python kimai_api.py backup-clockify --workspace-id WORKSPACE_ID
```

If the connection or computer stops, preserve the run directory and resume it:

```powershell
python kimai_api.py backup-clockify --resume .\data\clockify-backups\<run-id>
```

Exit codes are `0` for `COMPLETE`, `3` for a preserved `PARTIAL` backup, `2`
for configuration/authentication failure, and `130` for an interruption.

## What is saved

Each run stores byte-for-byte API response pages under `raw`, normalized JSONL
and migration-compatible CSV under `normalized`, receipts and invoice exports
under `assets`, and sanitized request/gap logs under `logs`. Webhook data is the
one deliberate raw-data exception: fields that look like reusable secrets are
redacted before being written.

The snapshot includes all accessible workspaces; users and memberships;
clients; active and archived projects, tasks, tags, and custom fields; hydrated
time entries from 1970 to the fixed cutoff; running entries; Detailed Report
JSON and CSV; expenses and receipts; invoices, payments, settings, and PDF
exports; approvals; holidays; time-off policies, balances, and requests;
scheduling; shared reports; deprecated templates; redacted webhooks; audit log;
and experimental created/updated/deleted entity changes. Paid or inaccessible
modules are recorded explicitly rather than silently skipped.

Large rejected date intervals are recursively split. Successful pages are
checkpointed immediately using `.part` files followed by atomic replacement.
HTTP 429 and transient server/network failures are retried with backoff.

## Output

```text
data\clockify-backups\<run-id>\
  manifest.json
  verification.json
  checksums.sha256
  raw\...
  normalized\...
  assets\receipts\...
  assets\invoices\...
  logs\requests.jsonl
  logs\gaps.jsonl
data\clockify-backups\<run-id>.zip
data\clockify-backups\<run-id>.zip.sha256
```

The entire `data` directory and `KEYS.txt` are ignored by Git. The archive can
contain personal, contractual, and financial information and should be treated
as confidential.

## Offline verification

```powershell
python kimai_api.py verify-clockify-backup .\data\clockify-backups\<run-id>
```

This checks the manifest/workspace coverage, core pagination completion,
duplicate entry IDs, Detailed Report IDs and durations, orphaned references,
asset gaps, importer-compatible CSV, every file checksum, ZIP readability, and
the ZIP SHA-256. It makes no API request.

## Manual emergency exports

The API cannot preserve everything. Before losing paid access, also export in
the Clockify interface:

1. Detailed Report without filters as CSV and XLSX, for the largest available
   range.
2. Expense Report and all receipts, if expenses were used.
3. Project overview as CSV/XLSX.
4. Every invoice as PDF.

Auto Tracker observations exist only on each user's computer and older local
observations can disappear after seven days; users should convert them to time
entries immediately. The documented public API does not provide central
downloads for Auto Tracker observations, screenshots, or GPS data.
