# Kimai import/export API project

A small, installable Python project for reviewing and migrating CDI data from
Clockify to Kimai. It uses only the Python standard library at runtime.

## Tools

- `python kimai_api.py import .\data\` is the main project/activity import
  command. It previews by default and reads the raw Kimai token from
  `kimai.env`.
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
|-- data/                      Local project/activity CSV files
|-- kimai.env.example          Raw Kimai credential template
|-- clockify.env.example       Clockify credential template
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

## Two separate API keys

Keep the two credentials in separate, Git-ignored files in the project root:

```text
clockify.env   CLOCKIFY-API=your-clockify-key
kimai.env      your-raw-kimai-key
```

The existing `clockify.env` remains the Clockify credential and keeps its
`CLOCKIFY-API=` prefix. The Kimai file contains only the raw key, without
`KIMAI_API_TOKEN=` or quotation marks:

```powershell
Copy-Item kimai.env.example kimai.env
```

Environment variables `KIMAI_API_TOKEN`, `KIMAI_TOKEN_FILE`, `KIMAI_BASE_URL`,
and `KIMAI_CUSTOMER` remain optional alternatives. Neither script prints or
writes an API key.

## Create the five migration files

Choose the inclusive historical date range to audit, then run:

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

Use `--workspace-id` if the Clockify key's active workspace is not the intended
one. Use `--clockify-base-url` for a regional Clockify API URL. The default
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

Changes are tracked with Git. Use annotated tags such as `v0.2.1` for tested
releases; the GitHub workflow runs compilation, unit tests, and command-help
checks on pushes and pull requests.
