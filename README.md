# Kimai import/export API project

A small, installable Python project for reviewing and migrating CDI data from
Clockify to Kimai. It uses only the Python standard library at runtime.

## Tools

- `python kimai_api.py backup-clockify` creates a complete, local, read-only
  emergency backup of every Clockify workspace accessible to the API key.
- `python kimai_api.py verify-clockify-backup <folder>` verifies checksums,
  archive readability, normalized data, references, and core completeness
  without contacting Clockify or Kimai.
- `python kimai_api.py import .\data\` is the main project/activity import
  command. It previews by default and reads `KIMAI_API_TOKEN` from `.env`.
- `kimai-prepare-clockify-migration` reads Clockify and Kimai and creates the
  five local catalogs, mapping templates, and duplicate/unmapped audit report.
  It never changes either service.
- `kimai-import-clockify` previews reviewed Clockify mappings and creates Kimai
  timesheets only with both `--apply` and `--confirm-live-import`.
- `kimai-import-project-tasks` validates project/task CSV exports, previews
  missing Kimai projects and activities, and creates them only with `--apply`.
- `kimai-export` creates a read-only Kimai project/activity catalog.

The detailed migration procedure is in
[`docs/CLOCKIFY_TIME_IMPORT.md`](docs/CLOCKIFY_TIME_IMPORT.md).

## Project structure

```text
.
|-- src/kimai_import_export/   Python package and API commands
|-- tests/                     Unit tests with no live API calls
|-- docs/                      Operational workflows
|-- .github/workflows/         GitHub Actions test matrix
|-- kimai_api.py               Main Python script
|-- data/                      Git-ignored inputs and Clockify backups
|-- .env                       Git-ignored Clockify and Kimai configuration
`-- pyproject.toml             Package metadata and command entry points
```

Real credential files, source CSVs, mapping files, and generated exports are
excluded from Git.

## Setup

Python 3.10 or newer is required. Create a virtual environment and install the
project in editable mode:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

## Import project and activity CSV files

The root-level Python script works without using a generated `.exe` launcher.
From the project folder, validate all CSV files without contacting Kimai:

```powershell
python kimai_api.py import .\data\ --offline
```

Run the live read-only preview to show only missing projects and activities:

```powershell
python kimai_api.py import .\data\
```

Nothing is created unless `--apply` is included. For example, after reviewing
the preview, create non-billable missing records with:

```powershell
python kimai_api.py import .\data\ --non-billable --apply
```

Use `--billable` instead when the new records should be billable.

## API configuration

Keep both credentials in the one Git-ignored `.env` file in the project root:

```text
KIMAI_API_TOKEN=your-kimai-key
KIMAI_BASE_URL=https://time.cdintl.org
KIMAI_CUSTOMER=CDI
CLOCKIFY_API_KEY=your-clockify-key
```

Use normal `KEY=value` syntax, without Markdown links or backslashes. The file
is never committed, and neither command prints or writes either API key.

## Complete Clockify emergency backup

Start the backup from the project folder. No virtual-environment activation is
needed when `python` already works on the computer:

```powershell
python kimai_api.py backup-clockify
```

The default destination is `data\clockify-backups\<UTC-run-id>`. The command
also creates a sibling ZIP and SHA-256 file. A partial result is preserved and
returns exit code `3`; continue it with:

```powershell
python kimai_api.py backup-clockify --resume .\data\clockify-backups\<run-id>
```

Verify a completed or partial backup entirely offline:

```powershell
python kimai_api.py verify-clockify-backup .\data\clockify-backups\<run-id>
```

The detailed data scope, status rules, and manual Clockify safety exports are
documented in [`docs/CLOCKIFY_BACKUP.md`](docs/CLOCKIFY_BACKUP.md).

## Create the five migration files

After preserving the emergency backup, choose the inclusive historical date
range to audit, then run:

```powershell
kimai-prepare-clockify-migration `
  --start-date 2025-01-01 `
  --end-date 2025-12-31
```

The default output folder is `clockify-migration` and contains:

1. `clockify-project-task-catalog.csv`
2. `kimai-project-activity-catalog.csv`
3. `clockify-to-kimai-mapping.csv`
4. `clockify-user-to-kimai-user.csv`
5. `offline-duplicate-unmapped-entry-report.csv`

The command makes GET requests only. Suggested matches stay at `Status=review`;
review them before changing any rows to `approved`. Existing output files are
not overwritten unless `--replace` is explicitly supplied.

The preparation command reads both keys from `.env`. Use `--workspace-id` if
the Clockify key's active workspace is not the intended one. Use
`--clockify-base-url` for a regional Clockify API URL. The default
wall-clock offset is `+06:00`; override it with `--utc-offset` when needed.

## Safe import workflow

After reviewing the mappings, first run the importer without `--apply`:

```powershell
kimai-import-clockify `
  .\clockify-migration\offline-duplicate-unmapped-entry-report.csv `
  --mapping-dir .\clockify-migration
```

This runs a live, read-only Kimai preflight. The detailed guide explains the
small-pilot import step. Nothing is created unless `--apply` is explicitly
present, and timesheet creation additionally requires `--confirm-live-import`.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The tests use fake keys and mocked HTTP calls. They do not contact Clockify or
Kimai.

## Versioning

Changes are tracked with Git. Use annotated tags such as `v0.4.0` for tested
releases; the GitHub workflow runs compilation, unit tests, and command-help
checks on pushes and pull requests.
