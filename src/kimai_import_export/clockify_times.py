#!/usr/bin/env python3
"""Safely preview and import historical Clockify time entries into Kimai.

This tool is deliberately separate from the project/task importer:
it never creates projects or activities. Every imported time entry must point
to an already existing, visible Kimai project, project-specific activity and
registered Kimai user.

Workflow
========
1. Create editable mapping templates from a Clockify CSV (offline).
2. Review the user and activity mappings and change their status to
   ``approved`` only when they are correct.
3. Run the normal (read-only) live preflight. It checks Kimai users, projects,
   activities, team access and existing time entries again.
4. Only then use ``--apply --confirm-live-import``. This is the sole mode that
   sends POST requests to Kimai.

The script reads API tokens at runtime from a file or the KIMAI_API_TOKEN
environment variable. It never prints, stores or embeds a token.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .project_tasks import (
    DEFAULT_BASE_URL,
    DEFAULT_TOKEN_FILE,
    ImportFailure,
    KimaiApi,
    clean,
    comparison_key,
    entity_id,
    load_token,
)


ACTIVITY_MAPPING_HEADERS = (
    "Clockify Project",
    "Clockify Task",
    "Kimai Project",
    "Kimai Activity",
    "Status",
    "Comment",
)
USER_MAPPING_HEADERS = (
    "Clockify User E-mail",
    "Kimai User E-mail",
    "Status",
    "Comment",
)
PREVIEW_HEADERS = (
    "Clockify row",
    "Start",
    "End",
    "Duration (hours)",
    "Clockify user",
    "Clockify project",
    "Clockify task",
    "Kimai user",
    "Kimai project",
    "Kimai activity",
    "Description",
    "Status",
    "Reason",
    "Source fingerprint",
)
REQUIRED_CLOCKIFY_COLUMNS = {
    "Project",
    "Task",
    "User",
    "Email",
    "Start Date",
    "Start Time",
    "End Date",
    "End Time",
    "Duration (decimal)",
    "Description",
    "Billable",
    "Tags",
}
MAPPING_STATUSES = {"approved", "review", "skip"}
DEFAULT_MAPPING_DIRECTORY = "clockify-migration"
ACTIVITY_MAPPING_FILENAME = "clockify-to-kimai-mapping.csv"
USER_MAPPING_FILENAME = "clockify-user-to-kimai-user.csv"
LEGACY_ACTIVITY_MAPPING_FILENAME = "clockify-to-kimai-activity-mapping.csv"
LEGACY_USER_MAPPING_FILENAME = "clockify-to-kimai-user-mapping.csv"


@dataclass(frozen=True)
class ClockifyEntry:
    row_number: int
    project: str
    task: str
    user_name: str
    email: str
    begin: datetime
    end: datetime
    duration_hours: Decimal
    description: str
    billable: bool
    tags: str
    fingerprint: str


@dataclass(frozen=True)
class ActivityMapping:
    source_project: str
    source_task: str
    target_project: str
    target_activity: str
    status: str
    comment: str


@dataclass(frozen=True)
class UserMapping:
    source_email: str
    target_email: str
    status: str
    comment: str


@dataclass(frozen=True)
class LocalKimaiEntry:
    row_number: int
    email: str
    project: str
    activity: str
    begin: datetime
    end: datetime | None
    description: str


@dataclass(frozen=True)
class LocalKimaiProjectActivity:
    project: str
    activity: str


@dataclass(frozen=True)
class PlanEntry:
    source: ClockifyEntry
    target_email: str
    target_project: str
    target_activity: str
    status: str
    reason: str
    live_user: Mapping[str, Any] | None = None
    live_project: Mapping[str, Any] | None = None
    live_activity: Mapping[str, Any] | None = None


def normalized_email(value: Any) -> str:
    return clean(value).casefold()


def source_pair_key(project: str, task: str) -> tuple[str, str]:
    return comparison_key(project), comparison_key(task)


def parse_mapping_status(value: Any, *, label: str, row_number: int) -> str:
    status = comparison_key(clean(value) or "review")
    if status not in MAPPING_STATUSES:
        choices = ", ".join(sorted(MAPPING_STATUSES))
        raise ImportFailure(
            f"{label} mapping row {row_number} has invalid status {value!r}. "
            f"Use one of: {choices}."
        )
    return status


def read_csv_rows(path: Path, required_headers: set[str], label: str) -> list[dict[str, str]]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise ImportFailure(f"{label} file not found: {path}") from exc
    with handle:
        reader = csv.DictReader(handle)
        actual_headers = set(reader.fieldnames or [])
        missing = sorted(required_headers - actual_headers)
        if missing:
            raise ImportFailure(
                f"{label} is missing required column(s): {', '.join(missing)}."
            )
        return [{key: clean(value) for key, value in row.items()} for row in reader]


def parse_clockify_datetime(
    date_text: str, time_text: str, *, row_number: int, field: str
) -> datetime:
    value = f"{date_text} {time_text}"
    for format_string in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(value, format_string)
        except ValueError:
            pass
    raise ImportFailure(
        f"Clockify row {row_number} has invalid {field}: {date_text!r} {time_text!r}. "
        "Expected YYYY-MM-DD and HH:MM, HH:MM:SS, or HH:MM:SS.ffffff."
    )


def parse_decimal_hours(value: str, *, row_number: int) -> Decimal:
    try:
        duration = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ImportFailure(
            f"Clockify row {row_number} has invalid 'Duration (decimal)': {value!r}."
        ) from exc
    if duration <= 0:
        raise ImportFailure(
            f"Clockify row {row_number} must have a positive duration, not {value!r}."
        )
    return duration


def parse_billable(value: str, *, row_number: int) -> bool:
    key = comparison_key(value)
    if key in {"yes", "true", "1", "yes"}:
        return True
    if key in {"", "no", "false", "0"}:
        return False
    raise ImportFailure(
        f"Clockify row {row_number} has an unknown Billable value: {value!r}."
    )


def make_source_fingerprint(entry: Mapping[str, Any]) -> str:
    fields = (
        normalized_email(entry["Email"]),
        clean(entry["Project"]),
        clean(entry["Task"]),
        clean(entry["Start Date"]),
        clean(entry["Start Time"]),
        clean(entry["End Date"]),
        clean(entry["End Time"]),
        clean(entry["Description"]),
        clean(entry["Tags"]),
        clean(entry["Billable"]),
    )
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()[:16]


def read_clockify_entries(path: Path) -> list[ClockifyEntry]:
    rows = read_csv_rows(path, REQUIRED_CLOCKIFY_COLUMNS, "Clockify CSV")
    entries: list[ClockifyEntry] = []
    for offset, row in enumerate(rows, start=2):
        project = clean(row["Project"])
        task = clean(row["Task"])
        email = normalized_email(row["Email"])
        if not project or not task or not email:
            raise ImportFailure(
                f"Clockify row {offset} needs Project, Task and Email before it can be imported."
            )
        begin = parse_clockify_datetime(
            row["Start Date"], row["Start Time"], row_number=offset, field="start time"
        )
        end = parse_clockify_datetime(
            row["End Date"], row["End Time"], row_number=offset, field="end time"
        )
        if end <= begin:
            raise ImportFailure(
                f"Clockify row {offset} ends before (or at) its start time."
            )
        duration_hours = parse_decimal_hours(row["Duration (decimal)"], row_number=offset)
        actual_minutes = Decimal((end - begin).total_seconds()) / Decimal(60)
        declared_minutes = duration_hours * Decimal(60)
        if abs(actual_minutes - declared_minutes) > Decimal("1.1"):
            raise ImportFailure(
                f"Clockify row {offset} duration ({duration_hours}h) does not match its "
                f"start/end time ({actual_minutes / Decimal(60)}h)."
            )
        entries.append(
            ClockifyEntry(
                row_number=offset,
                project=project,
                task=task,
                user_name=clean(row["User"]),
                email=email,
                begin=begin,
                end=end,
                duration_hours=duration_hours,
                description=clean(row["Description"]),
                billable=parse_billable(row["Billable"], row_number=offset),
                tags=clean(row["Tags"]),
                fingerprint=make_source_fingerprint(row),
            )
        )
    if not entries:
        raise ImportFailure("Clockify CSV contains no time entries.")
    return sorted(entries, key=lambda item: (item.begin, item.row_number))


def read_activity_mappings(path: Path) -> dict[tuple[str, str], ActivityMapping]:
    if not path.exists():
        return {}
    rows = read_csv_rows(path, set(ACTIVITY_MAPPING_HEADERS), "Activity mapping CSV")
    mappings: dict[tuple[str, str], ActivityMapping] = {}
    for row_number, row in enumerate(rows, start=2):
        source_project = clean(row["Clockify Project"])
        source_task = clean(row["Clockify Task"])
        if not source_project or not source_task:
            raise ImportFailure(
                f"Activity mapping row {row_number} needs Clockify Project and Clockify Task."
            )
        mapping = ActivityMapping(
            source_project=source_project,
            source_task=source_task,
            target_project=clean(row["Kimai Project"]),
            target_activity=clean(row["Kimai Activity"]),
            status=parse_mapping_status(
                row["Status"], label="Activity", row_number=row_number
            ),
            comment=clean(row["Comment"]),
        )
        key = source_pair_key(source_project, source_task)
        if key in mappings:
            raise ImportFailure(
                f"Activity mapping contains the same Clockify project/task more than once: "
                f"{source_project!r} / {source_task!r}."
            )
        if mapping.status == "approved" and (
            not mapping.target_project or not mapping.target_activity
        ):
            raise ImportFailure(
                f"Approved activity mapping row {row_number} needs Kimai Project and Kimai Activity."
            )
        mappings[key] = mapping
    return mappings


def read_user_mappings(path: Path) -> dict[str, UserMapping]:
    if not path.exists():
        return {}
    rows = read_csv_rows(path, set(USER_MAPPING_HEADERS), "User mapping CSV")
    mappings: dict[str, UserMapping] = {}
    for row_number, row in enumerate(rows, start=2):
        source_email = normalized_email(row["Clockify User E-mail"])
        target_email = normalized_email(row["Kimai User E-mail"])
        if not source_email:
            raise ImportFailure(f"User mapping row {row_number} needs Clockify User E-mail.")
        mapping = UserMapping(
            source_email=source_email,
            target_email=target_email,
            status=parse_mapping_status(row["Status"], label="User", row_number=row_number),
            comment=clean(row["Comment"]),
        )
        if source_email in mappings:
            raise ImportFailure(
                f"User mapping contains {source_email!r} more than once."
            )
        if mapping.status == "approved" and not mapping.target_email:
            raise ImportFailure(
                f"Approved user mapping row {row_number} needs Kimai User E-mail."
            )
        mappings[source_email] = mapping
    return mappings


def parse_kimai_local_datetime(date_text: str, time_text: str, *, row_number: int) -> datetime:
    try:
        return datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ImportFailure(
            f"Kimai export row {row_number} has invalid date/time: {date_text!r} {time_text!r}."
        ) from exc


def read_local_kimai_export(path: Path | None) -> list[LocalKimaiEntry]:
    if path is None:
        return []
    required = {"Date", "From", "To", "Project", "Activity", "Description"}
    rows = read_csv_rows(path, required, "Kimai timesheet export")
    result: list[LocalKimaiEntry] = []
    for row_number, row in enumerate(rows, start=2):
        begin_text = clean(row["From"])
        if not begin_text:
            continue
        begin = parse_kimai_local_datetime(row["Date"], begin_text, row_number=row_number)
        end_text = clean(row["To"])
        end = (
            parse_kimai_local_datetime(row["Date"], end_text, row_number=row_number)
            if end_text
            else None
        )
        if end is not None and end <= begin:
            raise ImportFailure(
                f"Kimai export row {row_number} ends before (or at) its start time."
            )
        email = normalized_email(row.get("E-mail") or row.get("User"))
        result.append(
            LocalKimaiEntry(
                row_number=row_number,
                email=email,
                project=clean(row["Project"]),
                activity=clean(row["Activity"]),
                begin=begin,
                end=end,
                description=clean(row["Description"]),
            )
        )
    return result


def read_local_project_activity_export(path: Path | None) -> list[LocalKimaiProjectActivity]:
    """Read the CSV written by ``kimai-export``, if supplied."""
    if path is None:
        return []
    rows = read_csv_rows(path, {"Project", "Activity"}, "Kimai project/activity export")
    return [
        LocalKimaiProjectActivity(clean(row["Project"]), clean(row["Activity"]))
        for row in rows
        if clean(row["Project"])
    ]


def english_label(value: str) -> str:
    """Return a conservative English label for bilingual Kimai activity names."""
    pieces = [clean(piece) for piece in value.split("/")]
    return pieces[-1] if pieces else clean(value)


def project_code_from_kimai_name(name: str) -> str:
    name = clean(name)
    if name.startswith("[") and "]" in name:
        return clean(name[1 : name.index("]")])
    return ""


def mapping_template_rows(
    entries: Sequence[ClockifyEntry],
    local_entries: Sequence[LocalKimaiEntry],
    project_activity_entries: Sequence[LocalKimaiProjectActivity] = (),
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    local_projects: dict[str, set[str]] = defaultdict(set)
    activities_by_project: dict[str, set[str]] = defaultdict(set)
    local_users = {item.email for item in local_entries if item.email}
    activity_candidates: dict[str, set[str]] = defaultdict(set)
    def add_project_activity(project: str, activity: str) -> None:
        code = project_code_from_kimai_name(project)
        if code:
            local_projects[comparison_key(code)].add(project)
        if activity:
            activities_by_project[project].add(activity)
            activity_candidates[comparison_key(english_label(activity))].add(activity)

    for item in local_entries:
        add_project_activity(item.project, item.activity)
    for item in project_activity_entries:
        add_project_activity(item.project, item.activity)

    pairs = sorted({(item.project, item.task) for item in entries}, key=lambda row: (row[0].casefold(), row[1].casefold()))
    activity_rows: list[dict[str, str]] = []
    for source_project, source_task in pairs:
        candidates = sorted(local_projects.get(comparison_key(source_project), set()))
        target_project = candidates[0] if len(candidates) == 1 else ""
        target_activity = ""
        comments: list[str] = []
        if target_project:
            comments.append("Project suggested from the supplied Kimai timesheet export")
            activity_matches = sorted(
                activity
                for activity in activities_by_project[target_project]
                if comparison_key(english_label(activity))
                == comparison_key(english_label(source_task))
            )
            if len(activity_matches) == 1:
                target_activity = activity_matches[0]
                comments.append("Activity suggested from the supplied Kimai timesheet export")
        else:
            possible_activities = sorted(
                activity_candidates.get(comparison_key(source_task), set())
            )
            if len(possible_activities) == 1:
                comments.append(
                    "Possible activity seen in the supplied Kimai export: "
                    + possible_activities[0]
                )
        if len(candidates) > 1:
            comments.append("Several Kimai projects share this code; choose one manually")
        activity_rows.append(
            {
                "Clockify Project": source_project,
                "Clockify Task": source_task,
                "Kimai Project": target_project,
                "Kimai Activity": target_activity,
                "Status": "review",
                "Comment": "; ".join(comments) or "Choose the current Kimai project and activity",
            }
        )

    source_emails = sorted({item.email for item in entries})
    user_rows = [
        {
            "Clockify User E-mail": email,
            "Kimai User E-mail": email if email in local_users else "",
            "Status": "review",
            "Comment": (
                "Same email appears in the supplied Kimai timesheet export; confirm account and access"
                if email in local_users
                else "Enter the current Kimai account email after the user has registered"
            ),
        }
        for email in source_emails
    ]
    return activity_rows, user_rows


def write_csv(path: Path, headers: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(headers), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_mapping_templates(
    directory: Path,
    entries: Sequence[ClockifyEntry],
    local_entries: Sequence[LocalKimaiEntry],
    project_activity_entries: Sequence[LocalKimaiProjectActivity] = (),
    *,
    replace_existing: bool,
) -> tuple[Path, Path]:
    activity_rows, user_rows = mapping_template_rows(
        entries, local_entries, project_activity_entries
    )
    activity_path = directory / ACTIVITY_MAPPING_FILENAME
    user_path = directory / USER_MAPPING_FILENAME
    existing = [path for path in (activity_path, user_path) if path.exists()]
    if existing and not replace_existing:
        names = ", ".join(path.name for path in existing)
        raise ImportFailure(
            f"Refusing to overwrite existing mapping template(s): {names}. "
            "They may contain reviewed decisions. Use --replace-mapping-templates "
            "only when you intentionally want fresh templates."
        )
    write_csv(activity_path, ACTIVITY_MAPPING_HEADERS, activity_rows)
    write_csv(user_path, USER_MAPPING_HEADERS, user_rows)
    return activity_path, user_path


def mapping_path(directory: Path, filename: str, legacy_filename: str) -> Path:
    preferred = directory / filename
    legacy = directory / legacy_filename
    return legacy if not preferred.exists() and legacy.exists() else preferred


def source_duplicates(entries: Sequence[ClockifyEntry]) -> set[str]:
    counts = Counter(item.fingerprint for item in entries)
    return {fingerprint for fingerprint, count in counts.items() if count > 1}


def matches_local_exact(plan: PlanEntry, record: LocalKimaiEntry) -> bool:
    return (
        record.email == plan.target_email
        and comparison_key(record.project) == comparison_key(plan.target_project)
        and comparison_key(record.activity) == comparison_key(plan.target_activity)
        and record.begin == plan.source.begin
        and record.end == plan.source.end
        and comparison_key(record.description) == comparison_key(plan.source.description)
    )


def matches_local_collision(plan: PlanEntry, record: LocalKimaiEntry) -> bool:
    return (
        record.email == plan.target_email
        and comparison_key(record.project) == comparison_key(plan.target_project)
        and comparison_key(record.activity) == comparison_key(plan.target_activity)
        and record.begin == plan.source.begin
        and record.end == plan.source.end
    )


def build_offline_plan(
    entries: Sequence[ClockifyEntry],
    activity_mappings: Mapping[tuple[str, str], ActivityMapping],
    user_mappings: Mapping[str, UserMapping],
    local_entries: Sequence[LocalKimaiEntry],
) -> list[PlanEntry]:
    duplicate_fingerprints = source_duplicates(entries)
    plans: list[PlanEntry] = []
    for source in entries:
        activity_mapping = activity_mappings.get(source_pair_key(source.project, source.task))
        user_mapping = user_mappings.get(source.email)
        if source.fingerprint in duplicate_fingerprints:
            plans.append(
                PlanEntry(source, "", "", "", "blocked", "Duplicate Clockify source row"))
            continue
        if user_mapping is None:
            plans.append(PlanEntry(source, "", "", "", "blocked", "No user mapping"))
            continue
        if user_mapping.status == "skip":
            plans.append(
                PlanEntry(source, user_mapping.target_email, "", "", "skipped", "User mapping is marked skip")
            )
            continue
        if user_mapping.status != "approved":
            plans.append(
                PlanEntry(
                    source,
                    user_mapping.target_email,
                    "",
                    "",
                    "blocked",
                    "User mapping has not been approved",
                )
            )
            continue
        if activity_mapping is None:
            plans.append(
                PlanEntry(source, user_mapping.target_email, "", "", "blocked", "No activity mapping")
            )
            continue
        if activity_mapping.status == "skip":
            plans.append(
                PlanEntry(
                    source,
                    user_mapping.target_email,
                    activity_mapping.target_project,
                    activity_mapping.target_activity,
                    "skipped",
                    "Activity mapping is marked skip",
                )
            )
            continue
        if activity_mapping.status != "approved":
            plans.append(
                PlanEntry(
                    source,
                    user_mapping.target_email,
                    activity_mapping.target_project,
                    activity_mapping.target_activity,
                    "blocked",
                    "Activity mapping has not been approved",
                )
            )
            continue
        plan = PlanEntry(
            source,
            user_mapping.target_email,
            activity_mapping.target_project,
            activity_mapping.target_activity,
            "ready_for_live_preflight",
            "Mappings approved; live Kimai checks still required",
        )
        exact = next((item for item in local_entries if matches_local_exact(plan, item)), None)
        if exact is not None:
            plans.append(
                replace(
                    plan,
                    status="already_in_local_kimai_export",
                    reason=f"Matches Kimai export row {exact.row_number}",
                )
            )
            continue
        collision = next((item for item in local_entries if matches_local_collision(plan, item)), None)
        if collision is not None:
            plans.append(
                replace(
                    plan,
                    status="blocked",
                    reason=(
                        f"Possible duplicate: same user/project/activity/time as Kimai export row "
                        f"{collision.row_number}, but different description"
                    ),
                )
            )
            continue
        plans.append(plan)
    return plans


def index_by_name(items: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in items:
        name = clean(item.get("name"))
        if name:
            result[comparison_key(name)].append(item)
    return result


def index_users_by_email(items: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in items:
        email = normalized_email(item.get("email"))
        if email:
            result[email].append(item)
    return result


def visible(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return comparison_key(value) not in {"0", "false", "no", "off"}


def enabled_user(user: Mapping[str, Any]) -> bool:
    value = user.get("enabled")
    return True if value is None else visible(value)


def reference_id(value: Any) -> int | None:
    if isinstance(value, Mapping):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def team_ids(item: Mapping[str, Any]) -> set[int] | None:
    if "teams" not in item:
        return None
    teams = item.get("teams")
    if not isinstance(teams, list):
        return set()
    return {identifier for team in teams if (identifier := reference_id(team)) is not None}


def live_timesheet_identity(record: Mapping[str, Any]) -> tuple[int | None, int | None, int | None, str, str, str]:
    begin = clean(record.get("begin"))
    end = clean(record.get("end"))
    if "+" in begin:
        begin = begin.split("+", 1)[0]
    if "+" in end:
        end = end.split("+", 1)[0]
    begin = begin.replace("Z", "")
    end = end.replace("Z", "")
    return (
        reference_id(record.get("user")),
        reference_id(record.get("project")),
        reference_id(record.get("activity")),
        begin,
        end,
        comparison_key(clean(record.get("description"))),
    )


def html5_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def enforce_team_access(
    api: KimaiApi,
    user: Mapping[str, Any],
    project: Mapping[str, Any],
    activity: Mapping[str, Any],
) -> str | None:
    restrictions = [
        ("project", team_ids(project)),
        ("activity", team_ids(activity)),
    ]
    if not any(teams for _, teams in restrictions):
        return None
    full_user = api.get(f"users/{entity_id(user, 'user')}")
    if not isinstance(full_user, Mapping):
        return "Could not read the mapped Kimai user in full"
    user_teams = team_ids(full_user)
    if user_teams is None:
        return "Kimai did not provide the user's team assignments"
    for label, restricted_teams in restrictions:
        if restricted_teams and not (user_teams & restricted_teams):
            return f"Mapped user is not assigned to a team allowed for this {label}"
    return None


def apply_live_preflight(api: KimaiApi, plans: Sequence[PlanEntry]) -> tuple[list[PlanEntry], str]:
    pending = [item for item in plans if item.status == "ready_for_live_preflight"]
    if not pending:
        version = api.get("version")
        version_text = clean(version.get("version")) if isinstance(version, Mapping) else "unknown"
        return list(plans), version_text

    version = api.get("version")
    version_text = clean(version.get("version")) if isinstance(version, Mapping) else "unknown"
    users = api.collection("users", {"visible": 3})
    projects = api.collection("projects", {"visible": 3})
    users_by_email = index_users_by_email(users)
    projects_by_name = index_by_name(projects)

    project_activities: dict[int, dict[str, list[Mapping[str, Any]]]] = {}
    for project_name in sorted({item.target_project for item in pending}, key=str.casefold):
        matches = projects_by_name.get(comparison_key(project_name), [])
        if len(matches) != 1:
            continue
        project_id = entity_id(matches[0], "project")
        activities = api.collection(
            "activities", {"project": project_id, "visible": 3, "globals": "false"}
        )
        project_activities[project_id] = index_by_name(activities)

    earliest = min(item.source.begin for item in pending).date()
    latest = max(item.source.end for item in pending).date()
    live_timesheets = api.collection(
        "timesheets",
        {
            "user": "all",
            "begin": f"{earliest.isoformat()}T00:00:00",
            "end": f"{latest.isoformat()}T23:59:59",
            "active": "0",
            "full": "true",
            "order": "ASC",
            "orderBy": "begin",
        },
    )
    live_identity = {live_timesheet_identity(item) for item in live_timesheets}
    live_without_description = {
        identity[:5] for identity in live_identity
    }

    result: list[PlanEntry] = []
    access_cache: dict[tuple[int, int, int], str | None] = {}
    for plan in plans:
        if plan.status != "ready_for_live_preflight":
            result.append(plan)
            continue
        user_matches = users_by_email.get(plan.target_email, [])
        if len(user_matches) != 1:
            reason = "Mapped Kimai user was not found" if not user_matches else "Several Kimai users share the mapped email"
            result.append(replace(plan, status="blocked", reason=reason))
            continue
        user = user_matches[0]
        if not enabled_user(user):
            result.append(replace(plan, status="blocked", reason="Mapped Kimai user is disabled"))
            continue
        project_matches = projects_by_name.get(comparison_key(plan.target_project), [])
        if len(project_matches) != 1:
            reason = "Mapped Kimai project was not found" if not project_matches else "Several Kimai projects share the mapped name"
            result.append(replace(plan, status="blocked", reason=reason))
            continue
        project = project_matches[0]
        if not visible(project.get("visible")):
            result.append(replace(plan, status="blocked", reason="Mapped Kimai project is hidden"))
            continue
        project_id = entity_id(project, "project")
        activity_matches = project_activities.get(project_id, {}).get(
            comparison_key(plan.target_activity), []
        )
        if len(activity_matches) != 1:
            reason = "Mapped project-specific Kimai activity was not found" if not activity_matches else "Several matching Kimai activities were found"
            result.append(replace(plan, status="blocked", reason=reason))
            continue
        activity = activity_matches[0]
        if not visible(activity.get("visible")):
            result.append(replace(plan, status="blocked", reason="Mapped Kimai activity is hidden"))
            continue
        user_id = entity_id(user, "user")
        activity_id = entity_id(activity, "activity")
        access_key = (user_id, project_id, activity_id)
        if access_key not in access_cache:
            access_cache[access_key] = enforce_team_access(api, user, project, activity)
        access_issue = access_cache[access_key]
        if access_issue:
            result.append(replace(plan, status="blocked", reason=access_issue))
            continue
        identity = (
            user_id,
            project_id,
            activity_id,
            html5_datetime(plan.source.begin),
            html5_datetime(plan.source.end),
            comparison_key(plan.source.description),
        )
        if identity in live_identity:
            result.append(
                replace(
                    plan,
                    status="already_in_live_kimai",
                    reason="Matching time entry already exists in Kimai",
                    live_user=user,
                    live_project=project,
                    live_activity=activity,
                )
            )
            continue
        if identity[:5] in live_without_description:
            result.append(
                replace(
                    plan,
                    status="blocked",
                    reason="Possible duplicate in Kimai: same user/project/activity/time, different description",
                    live_user=user,
                    live_project=project,
                    live_activity=activity,
                )
            )
            continue
        result.append(
            replace(
                plan,
                status="ready_for_import",
                reason="Live user, project, activity, team access and duplicate checks passed",
                live_user=user,
                live_project=project,
                live_activity=activity,
            )
        )
    return result, version_text


def preview_row(item: PlanEntry) -> dict[str, str]:
    source = item.source
    return {
        "Clockify row": str(source.row_number),
        "Start": html5_datetime(source.begin),
        "End": html5_datetime(source.end),
        "Duration (hours)": f"{source.duration_hours:.2f}",
        "Clockify user": source.email,
        "Clockify project": source.project,
        "Clockify task": source.task,
        "Kimai user": item.target_email,
        "Kimai project": item.target_project,
        "Kimai activity": item.target_activity,
        "Description": source.description,
        "Status": item.status,
        "Reason": item.reason,
        "Source fingerprint": source.fingerprint,
    }


def write_preview(
    preview_path: Path,
    summary_path: Path,
    plans: Sequence[PlanEntry],
    *,
    mode: str,
    kimai_version: str | None,
) -> None:
    write_csv(preview_path, PREVIEW_HEADERS, (preview_row(item) for item in plans))
    counts = Counter(item.status for item in plans)
    planned_hours = sum((item.source.duration_hours for item in plans), Decimal(0))
    ready_hours = sum(
        (item.source.duration_hours for item in plans if item.status == "ready_for_import"),
        Decimal(0),
    )
    blocked = [item for item in plans if item.status == "blocked"]
    lines = [
        "Clockify to Kimai historical time import preview",
        f"Mode: {mode}",
        f"Kimai version: {kimai_version or 'not queried'}",
        f"Clockify entries reviewed: {len(plans)}",
        f"Clockify hours reviewed: {planned_hours:.2f}",
        f"Entries ready for import: {counts['ready_for_import']}",
        f"Hours ready for import: {ready_hours:.2f}",
        "",
        "Status counts:",
    ]
    lines.extend(f"- {status}: {count}" for status, count in sorted(counts.items()))
    if blocked:
        reasons = Counter(item.reason for item in blocked)
        lines.extend(["", "Blocking reasons:"])
        lines.extend(f"- {reason}: {count}" for reason, count in sorted(reasons.items()))
    lines.extend(
        [
            "",
            "No data was changed while this preview was written.",
            f"Detailed rows: {preview_path.name}",
        ]
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def filter_entries(entries: Sequence[ClockifyEntry], args: argparse.Namespace) -> list[ClockifyEntry]:
    selected = list(entries)
    if args.source_project:
        wanted = {comparison_key(value) for value in args.source_project}
        selected = [item for item in selected if comparison_key(item.project) in wanted]
    if args.source_email:
        wanted_emails = {normalized_email(value) for value in args.source_email}
        selected = [item for item in selected if item.email in wanted_emails]
    if args.limit is not None:
        if args.limit <= 0:
            raise ImportFailure("--limit must be greater than zero.")
        selected = selected[: args.limit]
    if not selected:
        raise ImportFailure("The selected Clockify filters contain no time entries.")
    return selected


def create_timesheets(api: KimaiApi, plans: Sequence[PlanEntry]) -> int:
    ready = [item for item in plans if item.status == "ready_for_import"]
    unresolved = [
        item
        for item in plans
        if item.status not in {"ready_for_import", "already_in_live_kimai", "already_in_local_kimai_export", "skipped"}
    ]
    if unresolved:
        raise ImportFailure(
            f"Refusing to import: {len(unresolved)} selected entries are still blocked. "
            "Review the preview CSV or narrow the input with --source-project / --source-email / --limit."
        )
    if not ready:
        print("No new time entries need to be created.")
        return 0
    created = 0
    for plan in ready:
        if plan.live_user is None or plan.live_project is None or plan.live_activity is None:
            raise ImportFailure("Internal safety check failed: a ready entry lacks live Kimai IDs.")
        payload: dict[str, Any] = {
            "begin": html5_datetime(plan.source.begin),
            "end": html5_datetime(plan.source.end),
            "project": entity_id(plan.live_project, "project"),
            "activity": entity_id(plan.live_activity, "activity"),
            "user": entity_id(plan.live_user, "user"),
            "description": plan.source.description,
            "billable": plan.source.billable,
        }
        if plan.source.tags:
            payload["tags"] = plan.source.tags
        api.post("timesheets", payload)
        created += 1
        print(
            f"Created {created}/{len(ready)}: Clockify row {plan.source.row_number} "
            f"({plan.source.begin:%Y-%m-%d %H:%M})"
        )
    return created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview and, only with explicit confirmation, import Clockify time entries into Kimai.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  kimai-import-clockify clockify-export.csv --offline --kimai-timesheet-csv kimai-export.csv --write-mapping-templates\n"
            "  kimai-import-clockify clockify-export.csv --mapping-dir .\\clockify-migration\n"
            "  kimai-import-clockify clockify-export.csv --mapping-dir .\\clockify-migration --source-project KG.CommT --limit 5 --apply --confirm-live-import\n\n"
            "Without --apply the script only performs a dry run. --offline never reads a token or contacts Kimai."
        ),
    )
    parser.add_argument("clockify_csv", help="Clockify Detailed Report CSV export")
    parser.add_argument(
        "--kimai-timesheet-csv",
        help="Optional local Kimai time export used only for offline suggestions and duplicate checks",
    )
    parser.add_argument(
        "--kimai-project-activity-csv",
        help=(
            "Optional CSV from kimai-export; used only to make "
            "more complete offline mapping suggestions"
        ),
    )
    parser.add_argument(
        "--mapping-dir",
        default=DEFAULT_MAPPING_DIRECTORY,
        help=f"Folder for the two editable mapping CSVs and preview files (default: {DEFAULT_MAPPING_DIRECTORY})",
    )
    parser.add_argument(
        "--write-mapping-templates",
        action="store_true",
        help="Write new editable mapping CSV templates, with suggestions where safely possible",
    )
    parser.add_argument(
        "--replace-mapping-templates",
        action="store_true",
        help="Intentionally replace existing mapping templates (only valid with --write-mapping-templates)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Offline preview only; do not read the token or contact Kimai",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Kimai base URL")
    parser.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        help=(
            "UTF-8 file containing the Kimai API token; otherwise use "
            "KIMAI_TOKEN_FILE or KIMAI_API_TOKEN"
        ),
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--source-project",
        action="append",
        help="Restrict to an exact Clockify project name; repeat for several projects",
    )
    parser.add_argument(
        "--source-email",
        action="append",
        help="Restrict to an exact Clockify user email; repeat for several users",
    )
    parser.add_argument(
        "--limit", type=int, help="Restrict to the earliest N selected Clockify entries (useful for a pilot)")
    parser.add_argument(
        "--apply", action="store_true", help="Create only entries that passed every live check"
    )
    parser.add_argument(
        "--confirm-live-import",
        action="store_true",
        help="Required together with --apply; acknowledges that Kimai will be changed",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        raise ImportFailure("--timeout must be greater than zero.")
    if args.offline and args.apply:
        raise ImportFailure("--offline and --apply cannot be used together.")
    if args.apply and not args.confirm_live_import:
        raise ImportFailure("--apply also requires --confirm-live-import.")
    if args.confirm_live_import and not args.apply:
        raise ImportFailure("--confirm-live-import only has meaning together with --apply.")
    if args.replace_mapping_templates and not args.write_mapping_templates:
        raise ImportFailure(
            "--replace-mapping-templates only has meaning together with --write-mapping-templates."
        )

    clockify_path = Path(args.clockify_csv)
    mapping_dir = Path(args.mapping_dir)
    entries = filter_entries(read_clockify_entries(clockify_path), args)
    local_entries = read_local_kimai_export(
        Path(args.kimai_timesheet_csv) if args.kimai_timesheet_csv else None
    )
    project_activity_entries = read_local_project_activity_export(
        Path(args.kimai_project_activity_csv) if args.kimai_project_activity_csv else None
    )
    if args.write_mapping_templates:
        activity_path, user_path = write_mapping_templates(
            mapping_dir,
            entries,
            local_entries,
            project_activity_entries,
            replace_existing=args.replace_mapping_templates,
        )
        print(f"Activity mapping template: {activity_path.resolve()}")
        print(f"User mapping template: {user_path.resolve()}")

    activity_mapping_path = mapping_path(
        mapping_dir, ACTIVITY_MAPPING_FILENAME, LEGACY_ACTIVITY_MAPPING_FILENAME
    )
    user_mapping_path = mapping_path(
        mapping_dir, USER_MAPPING_FILENAME, LEGACY_USER_MAPPING_FILENAME
    )
    plans = build_offline_plan(
        entries,
        read_activity_mappings(activity_mapping_path),
        read_user_mappings(user_mapping_path),
        local_entries,
    )
    preview_path = mapping_dir / "clockify-timesheet-import-preview.csv"
    summary_path = mapping_dir / "clockify-timesheet-import-summary.txt"
    if args.offline:
        write_preview(
            preview_path, summary_path, plans, mode="offline", kimai_version=None
        )
        print(f"Offline preview: {preview_path.resolve()}")
        print(f"Offline summary: {summary_path.resolve()}")
        print("OFFLINE DRY RUN: no token was read and no Kimai API call was made.")
        return 0

    token = load_token(Path(args.token_file) if args.token_file else None)
    api = KimaiApi(args.base_url, token, args.timeout)
    try:
        plans, version_text = apply_live_preflight(api, plans)
    except ImportFailure as exc:
        write_preview(
            preview_path,
            summary_path,
            plans,
            mode="offline fallback after failed live preflight",
            kimai_version=None,
        )
        raise ImportFailure(
            f"Live preflight could not be completed: {exc}\n"
            f"An offline preview was written to: {preview_path.resolve()}"
        ) from exc

    write_preview(preview_path, summary_path, plans, mode="live read-only preflight", kimai_version=version_text)
    print(f"Live preview: {preview_path.resolve()}")
    print(f"Live summary: {summary_path.resolve()}")
    if not args.apply:
        print("LIVE DRY RUN: Kimai was queried read-only; no data was changed.")
        return 0
    created = create_timesheets(api, plans)
    print(f"Completed live import: created {created} Kimai time entries.")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    try:
        return run(build_parser().parse_args())
    except ImportFailure as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
