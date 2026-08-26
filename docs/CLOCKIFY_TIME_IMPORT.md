# Historical Clockify time import

`kimai-import-clockify` imports completed Clockify time entries into
existing Kimai data. It does **not** create users, projects or activities.

## Direct API preparation

Store the two API keys separately in the project root:

```text
.env.clockify   CLOCKIFY_API_KEY=your-clockify-key
.env.kimai      KIMAI_API_TOKEN=your-kimai-key
```

Both files are excluded from Git and use normal `KEY=value` dotenv syntax.
`.env.clockify` is read only as a Clockify credential and is never treated as a
Kimai token.

Choose the inclusive date range and run:

```powershell
kimai-prepare-clockify-migration `
  --start-date 2025-01-01 `
  --end-date 2025-12-31
```

This performs read-only GET requests against both APIs and writes these files
to `clockify-migration`:

- `clockify-project-task-catalog.csv`
- `kimai-project-activity-catalog.csv`
- `clockify-to-kimai-mapping.csv`
- `clockify-user-to-kimai-user.csv`
- `offline-duplicate-unmapped-entry-report.csv`

The final report is "offline" in the sense that it is a local CSV for review
and does not change either service. Its creation does require API access so it
can compare the selected Clockify entries with current Kimai timesheets.
Automatic project/activity and user matches are suggestions only and remain at
`Status=review`. Confirm each mapping before changing it to `approved`. The
preparation command refuses to overwrite existing outputs unless `--replace`
is intentionally supplied.

The importer is safe by default:

- `--offline` reads only CSV files and never reads the Kimai API token.
- Without `--apply`, the normal mode makes read-only API requests and performs
  a fresh live preflight.
- `--apply --confirm-live-import` is required before a time entry can be
  created.

## First offline preparation

Run this from the repository root, using paths to your local exports:

```powershell
$clockifyCsv = 'C:\path\to\Clockify_Time_Report_Detailed.csv'
$kimaiTimesheetCsv = 'C:\path\to\kimai-timesheet-export.csv'

kimai-import-clockify `
  $clockifyCsv `
  --offline `
  --kimai-timesheet-csv $kimaiTimesheetCsv `
  --write-mapping-templates
```

This older manual-CSV route also creates the folder `clockify-migration` with:

- `clockify-to-kimai-mapping.csv` — one row for every old
  Clockify project/task combination.
- `clockify-user-to-kimai-user.csv` — one row for every Clockify user.
- `clockify-timesheet-import-preview.csv` — each source booking and its
  current status.
- `clockify-timesheet-import-summary.txt` — a short overview.

The supplied Kimai time export can only make suggestions; it is not a complete
list of active Kimai projects and activities. For a fuller offline reference,
first run the existing read-only project/activity export:

```powershell
kimai-export --output .\clockify-migration\kimai-project-activity-catalog.csv
```

Then add this option to the first command:

```powershell
--kimai-project-activity-csv .\clockify-migration\kimai-project-activity-catalog.csv
```

Review every mapping. Enter the current Kimai names and change `Status` to
`approved` only after confirming it. Use `skip` for entries which must never be
imported. Keep all other rows as `review`. The template command refuses to
overwrite mapping files that already exist. This protects your review work. Use
`--replace-mapping-templates` only when you intentionally want to discard the
old templates and start again.

## Live preflight (no changes)

After approving the relevant mappings, run the same command **without**
`--offline`:

```powershell
kimai-import-clockify `
  $clockifyCsv `
  --mapping-dir .\clockify-migration
```

The live preflight reads the current Kimai user list, projects, project-specific
activities and existing timesheets. It blocks an import when a user has not
registered, a project/activity does not exist or is hidden, team access cannot
be confirmed, or a matching/possibly matching time entry already exists.

The Kimai API token must belong to an administrator or another account permitted
to read the required records. Keep the raw token in the local, Git-ignored
`.env.kimai` file. `KIMAI_API_TOKEN`, `KIMAI_TOKEN_FILE`, and `--token-file`
remain supported alternatives.

## First live import: small pilot

After checking the generated preview, use a small project-limited pilot:

```powershell
kimai-import-clockify `
  $clockifyCsv `
  --mapping-dir .\clockify-migration `
  --source-project KG.CommT `
  --limit 5 `
  --apply --confirm-live-import
```

Before creating anything, this command repeats the live preflight. Kimai has
to accept the API account's permission to create a time entry for the mapped
user. If it rejects that operation, the script stops and reports the API error;
it does not attempt to work around Kimai permissions.

Use the detailed preview after every pilot. The importer refuses to continue
while selected entries remain blocked. It does not delete or alter existing
Kimai entries.
