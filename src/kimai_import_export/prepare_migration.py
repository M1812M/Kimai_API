#!/usr/bin/env python3
"""Build read-only Clockify-to-Kimai migration catalogs and audit files."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .clockify_times import (
    english_label,
    make_source_fingerprint,
    normalized_email,
    project_code_from_kimai_name,
    source_pair_key,
    write_csv,
)
from .config import load_dotenv
from .export_projects_activities import collect_catalog, write_catalog
from .project_tasks import (
    DEFAULT_BASE_URL,
    DEFAULT_CUSTOMER,
    ImportFailure,
    KimaiApi,
    clean,
    comparison_key,
    entity_id,
    load_token,
)


DEFAULT_CLOCKIFY_BASE_URL = "https://api.clockify.me/api/v1"
DEFAULT_CLOCKIFY_ENV = ".env"
DEFAULT_OUTPUT_DIRECTORY = "clockify-migration"
DEFAULT_UTC_OFFSET = "+06:00"

CLOCKIFY_CATALOG_FILENAME = "clockify-project-task-catalog.csv"
KIMAI_CATALOG_FILENAME = "kimai-project-activity-catalog.csv"
ACTIVITY_MAPPING_FILENAME = "clockify-to-kimai-mapping.csv"
USER_MAPPING_FILENAME = "clockify-user-to-kimai-user.csv"
AUDIT_FILENAME = "offline-duplicate-unmapped-entry-report.csv"

CLOCKIFY_CATALOG_HEADERS = (
    "Workspace ID",
    "Workspace",
    "Project ID",
    "Project",
    "Project archived",
    "Project billable",
    "Task ID",
    "Task",
    "Task status",
    "Task billable",
)
CLOCKIFY_REPORT_HEADERS = (
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
)
ACTIVITY_MAPPING_CATALOG_HEADERS = (
    "Clockify Project ID",
    "Clockify Project",
    "Clockify Task ID",
    "Clockify Task",
    "Kimai Project ID",
    "Kimai Project",
    "Kimai Activity ID",
    "Kimai Activity",
    "Status",
    "Comment",
)
USER_MAPPING_CATALOG_HEADERS = (
    "Clockify User ID",
    "Clockify User",
    "Clockify User E-mail",
    "Clockify Status",
    "Kimai User ID",
    "Kimai User",
    "Kimai User E-mail",
    "Status",
    "Comment",
)
AUDIT_HEADERS = (
    *CLOCKIFY_REPORT_HEADERS,
    "Clockify Entry ID",
    "Clockify User ID",
    "Clockify Project ID",
    "Clockify Task ID",
    "Start UTC",
    "End UTC",
    "Suggested Kimai User",
    "Suggested Kimai User ID",
    "Suggested Kimai Project",
    "Suggested Kimai Project ID",
    "Suggested Kimai Activity",
    "Suggested Kimai Activity ID",
    "Audit Status",
    "Audit Reason",
    "Source fingerprint",
)


@dataclass(frozen=True)
class ClockifyTimeEntry:
    entry_id: str
    user_id: str
    user_name: str
    user_email: str
    project_id: str
    project_name: str
    task_id: str
    task_name: str
    start: datetime
    end: datetime | None
    description: str
    billable: bool
    tags: str


@dataclass(frozen=True)
class ActivitySuggestion:
    project_name: str
    project_id: int | None
    activity_name: str
    activity_id: int | None
    comment: str


@dataclass(frozen=True)
class UserSuggestion:
    email: str
    user_id: int | None
    name: str
    comment: str


def load_clockify_api_key(path: Path) -> str:
    """Load CLOCKIFY_API_KEY from the shared dotenv file without logging it."""

    if not path.is_file():
        raise ImportFailure(f"Clockify credential file not found: {path}")
    load_dotenv(path)
    api_key = os.environ.get("CLOCKIFY_API_KEY", "").strip()
    if not api_key:
        raise ImportFailure(f"CLOCKIFY_API_KEY is missing from {path}.")
    if any(character.isspace() for character in api_key):
        raise ImportFailure("Clockify API key contains whitespace.")
    return api_key


class ClockifyApi:
    """Small read-only Clockify API client with endpoint-aware pagination."""

    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        base_url = base_url.rstrip("/")
        if not base_url.startswith("https://"):
            raise ImportFailure("Clockify API URL must use HTTPS.")
        self.base_url = base_url
        self._api_key = api_key
        self.timeout = timeout

    def _request(
        self, endpoint: str, params: Mapping[str, Any] | None = None
    ) -> tuple[Any, Mapping[str, str]]:
        endpoint = endpoint.lstrip("/")
        url = f"{self.base_url}/{endpoint}"
        if params:
            url += "?" + urlencode(params, doseq=True)
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "X-Api-Key": self._api_key,
                "User-Agent": "CDI-Clockify-Kimai-migration/1.0",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                headers = dict(response.headers.items())
        except HTTPError as exc:
            body = exc.read(8192).decode("utf-8", errors="replace")
            body = body.replace(self._api_key, "[REDACTED]")
            raise ImportFailure(
                f"Clockify API returned HTTP {exc.code} for GET /{endpoint}: "
                f"{body or exc.reason}"
            ) from exc
        except URLError as exc:
            raise ImportFailure(
                f"Could not connect to Clockify for GET /{endpoint}: {exc.reason}"
            ) from exc

        try:
            return json.loads(raw.decode("utf-8")), headers
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImportFailure(
                f"Clockify returned invalid JSON for GET /{endpoint}."
            ) from exc

    def get(self, endpoint: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._request(endpoint, params)[0]

    def collection(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        *,
        page_size: int = 200,
    ) -> list[dict[str, Any]]:
        base_params = dict(params or {})
        page = 1
        items: list[dict[str, Any]] = []
        while True:
            data, headers = self._request(
                endpoint, {**base_params, "page": page, "page-size": page_size}
            )
            if not isinstance(data, list):
                raise ImportFailure(
                    f"Unexpected collection response from Clockify GET /{endpoint}."
                )
            page_items = [item for item in data if isinstance(item, dict)]
            items.extend(page_items)
            last_page = next(
                (
                    str(value).casefold()
                    for key, value in headers.items()
                    if key.casefold() == "last-page"
                ),
                "",
            )
            if last_page == "true" or len(data) < page_size:
                return items
            page += 1
            if page > 10000:
                raise ImportFailure(f"Clockify pagination did not terminate for /{endpoint}.")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected YYYY-MM-DD.") from exc


def parse_utc_offset(value: str) -> timezone:
    if len(value) != 6 or value[0] not in "+-" or value[3] != ":":
        raise argparse.ArgumentTypeError("UTC offset must look like +06:00 or -05:30.")
    try:
        hours = int(value[1:3])
        minutes = int(value[4:6])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("UTC offset must contain numbers.") from exc
    if hours > 23 or minutes > 59:
        raise argparse.ArgumentTypeError("UTC offset is outside the valid range.")
    delta = timedelta(hours=hours, minutes=minutes)
    if value[0] == "-":
        delta = -delta
    return timezone(delta)


def parse_api_datetime(value: Any, label: str) -> datetime:
    text = clean(value)
    if not text:
        raise ImportFailure(f"{label} is missing a timestamp.")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ImportFailure(f"{label} has an invalid timestamp: {text!r}.") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def select_workspace(
    api: ClockifyApi, requested_workspace_id: str | None
) -> Mapping[str, Any]:
    data = api.get("workspaces")
    if not isinstance(data, list):
        raise ImportFailure("Clockify returned an unexpected workspace list.")
    workspaces = [item for item in data if isinstance(item, Mapping)]
    workspace_id = clean(requested_workspace_id)
    if not workspace_id:
        user = api.get("user")
        if not isinstance(user, Mapping):
            raise ImportFailure("Clockify returned unexpected current-user data.")
        workspace_id = clean(user.get("activeWorkspace") or user.get("defaultWorkspace"))
    matches = [item for item in workspaces if clean(item.get("id")) == workspace_id]
    if len(matches) != 1:
        raise ImportFailure(
            f"Clockify workspace {workspace_id!r} was not found exactly once. "
            "Pass --workspace-id explicitly."
        )
    return matches[0]


def fetch_clockify_catalog(
    api: ClockifyApi, workspace: Mapping[str, Any]
) -> tuple[
    list[dict[str, str]],
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    workspace_id = clean(workspace.get("id"))
    workspace_name = clean(workspace.get("name"))
    projects = api.collection(f"workspaces/{workspace_id}/projects")
    projects.sort(key=lambda item: clean(item.get("name")).casefold())
    project_by_id: dict[str, Mapping[str, Any]] = {}
    task_by_id: dict[str, Mapping[str, Any]] = {}
    rows: list[dict[str, str]] = []
    for project in projects:
        project_id = clean(project.get("id"))
        if not project_id:
            raise ImportFailure("Clockify returned a project without an ID.")
        project_by_id[project_id] = project
        tasks = api.collection(
            f"workspaces/{workspace_id}/projects/{project_id}/tasks"
        )
        tasks.sort(key=lambda item: clean(item.get("name")).casefold())
        if not tasks:
            tasks = [{}]
        for task in tasks:
            task_id = clean(task.get("id"))
            if task_id:
                task_by_id[task_id] = task
            rows.append(
                {
                    "Workspace ID": workspace_id,
                    "Workspace": workspace_name,
                    "Project ID": project_id,
                    "Project": clean(project.get("name")),
                    "Project archived": str(bool(project.get("archived"))).lower(),
                    "Project billable": str(bool(project.get("billable"))).lower(),
                    "Task ID": task_id,
                    "Task": clean(task.get("name")),
                    "Task status": clean(task.get("status")),
                    "Task billable": (
                        str(bool(task.get("billable"))).lower() if task else ""
                    ),
                }
            )
    return rows, project_by_id, task_by_id


def fetch_clockify_tags(
    api: ClockifyApi, workspace_id: str
) -> dict[str, str]:
    """Return active and archived tag names indexed by their Clockify IDs."""

    tags: list[dict[str, Any]] = []
    for archived in (False, True):
        tags.extend(
            api.collection(
                f"workspaces/{workspace_id}/tags",
                {"archived": str(archived).lower()},
            )
        )
    return {
        clean(tag.get("id")): clean(tag.get("name"))
        for tag in tags
        if clean(tag.get("id"))
    }


def fetch_clockify_users(
    api: ClockifyApi, workspace_id: str
) -> tuple[list[dict[str, Any]], dict[str, Mapping[str, Any]]]:
    users = api.collection(
        f"workspaces/{workspace_id}/users",
        {"status": "ALL", "sort-column": "EMAIL", "sort-order": "ASCENDING"},
    )
    by_id: dict[str, Mapping[str, Any]] = {}
    for user in users:
        user_id = clean(user.get("id"))
        if user_id:
            by_id[user_id] = user
    return users, by_id


def utc_query_range(
    start_date: date, end_date: date, local_zone: timezone
) -> tuple[str, str]:
    local_start = datetime.combine(start_date, time.min, local_zone)
    local_end = datetime.combine(end_date + timedelta(days=1), time.min, local_zone)
    return (
        local_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        local_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def fetch_clockify_time_entries(
    api: ClockifyApi,
    workspace_id: str,
    users: Sequence[Mapping[str, Any]],
    project_by_id: Mapping[str, Mapping[str, Any]],
    task_by_id: Mapping[str, Mapping[str, Any]],
    tag_by_id: Mapping[str, str],
    start_date: date,
    end_date: date,
    local_zone: timezone,
) -> list[ClockifyTimeEntry]:
    start_query, end_query = utc_query_range(start_date, end_date, local_zone)
    result: list[ClockifyTimeEntry] = []
    for user in users:
        user_id = clean(user.get("id"))
        if not user_id:
            continue
        raw_entries = api.collection(
            f"workspaces/{workspace_id}/user/{user_id}/time-entries",
            {
                "start": start_query,
                "end": end_query,
                "hydrated": "false",
            },
        )
        for raw in raw_entries:
            interval = raw.get("timeInterval")
            if not isinstance(interval, Mapping):
                raise ImportFailure("Clockify returned a time entry without timeInterval.")
            start = parse_api_datetime(interval.get("start"), "Clockify time entry start")
            end_value = clean(interval.get("end"))
            end = parse_api_datetime(end_value, "Clockify time entry end") if end_value else None
            project_id = clean(raw.get("projectId"))
            task_id = clean(raw.get("taskId"))
            project = project_by_id.get(project_id, {})
            task = task_by_id.get(task_id, {})
            result.append(
                ClockifyTimeEntry(
                    entry_id=clean(raw.get("id")),
                    user_id=user_id,
                    user_name=clean(user.get("name")),
                    user_email=normalized_email(user.get("email")),
                    project_id=project_id,
                    project_name=clean(project.get("name")),
                    task_id=task_id,
                    task_name=clean(task.get("name")),
                    start=start.astimezone(timezone.utc),
                    end=end.astimezone(timezone.utc) if end else None,
                    description=clean(raw.get("description")),
                    billable=bool(raw.get("billable")),
                    tags="; ".join(
                        tag_by_id.get(clean(tag_id), clean(tag_id))
                        for tag_id in (raw.get("tagIds") or [])
                        if clean(tag_id)
                    ),
                )
            )
    return sorted(result, key=lambda item: (item.start, item.entry_id))


def kimai_project_rows_by_id(
    rows: Sequence[Mapping[str, Any]],
) -> dict[int, list[Mapping[str, Any]]]:
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            grouped[int(row["Project ID"])].append(row)
        except (KeyError, TypeError, ValueError):
            continue
    return grouped


def build_activity_mappings(
    clockify_rows: Sequence[Mapping[str, Any]],
    kimai_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, str]], dict[tuple[str, str], ActivitySuggestion]]:
    projects_by_id = kimai_project_rows_by_id(kimai_rows)
    projects_by_code: dict[str, set[int]] = defaultdict(set)
    projects_by_name: dict[str, set[int]] = defaultdict(set)
    for project_id, rows in projects_by_id.items():
        project_name = clean(rows[0].get("Project"))
        projects_by_name[comparison_key(project_name)].add(project_id)
        code = project_code_from_kimai_name(project_name)
        if code:
            projects_by_code[comparison_key(code)].add(project_id)

    pairs: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in clockify_rows:
        project = clean(row.get("Project"))
        task = clean(row.get("Task"))
        if project and task:
            pairs.setdefault(source_pair_key(project, task), row)

    output: list[dict[str, str]] = []
    suggestions: dict[tuple[str, str], ActivitySuggestion] = {}
    for key, row in sorted(
        pairs.items(),
        key=lambda item: (
            clean(item[1].get("Project")).casefold(),
            clean(item[1].get("Task")).casefold(),
        ),
    ):
        source_project = clean(row.get("Project"))
        source_task = clean(row.get("Task"))
        candidate_ids = set(projects_by_code.get(comparison_key(source_project), set()))
        candidate_ids.update(projects_by_name.get(comparison_key(source_project), set()))
        target_project = ""
        target_project_id: int | None = None
        target_activity = ""
        target_activity_id: int | None = None
        comments: list[str] = []
        if len(candidate_ids) == 1:
            target_project_id = next(iter(candidate_ids))
            project_rows = projects_by_id[target_project_id]
            target_project = clean(project_rows[0].get("Project"))
            comments.append("Unique Kimai project suggested from exact code or name")
            activity_matches = [
                item
                for item in project_rows
                if clean(item.get("Activity"))
                and comparison_key(english_label(clean(item.get("Activity"))))
                == comparison_key(english_label(source_task))
            ]
            unique_activity_ids = {
                int(item["Activity ID"])
                for item in activity_matches
                if clean(item.get("Activity ID")).isdigit()
            }
            if len(unique_activity_ids) == 1:
                target_activity_id = next(iter(unique_activity_ids))
                match = next(
                    item
                    for item in activity_matches
                    if int(item["Activity ID"]) == target_activity_id
                )
                target_activity = clean(match.get("Activity"))
                comments.append("Unique activity suggested from exact normalized name")
            else:
                comments.append("Choose the Kimai activity manually")
        elif candidate_ids:
            comments.append("Several Kimai projects match this Clockify project")
        else:
            comments.append("No exact Kimai project code or name match")

        suggestion = ActivitySuggestion(
            target_project,
            target_project_id,
            target_activity,
            target_activity_id,
            "; ".join(comments),
        )
        suggestions[key] = suggestion
        output.append(
            {
                "Clockify Project ID": clean(row.get("Project ID")),
                "Clockify Project": source_project,
                "Clockify Task ID": clean(row.get("Task ID")),
                "Clockify Task": source_task,
                "Kimai Project ID": str(target_project_id or ""),
                "Kimai Project": target_project,
                "Kimai Activity ID": str(target_activity_id or ""),
                "Kimai Activity": target_activity,
                "Status": "review",
                "Comment": suggestion.comment + "; confirm before changing Status to approved",
            }
        )
    return output, suggestions


def build_user_mappings(
    clockify_users: Sequence[Mapping[str, Any]], kimai_users: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, str]], dict[str, UserSuggestion]]:
    kimai_by_email: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for user in kimai_users:
        email = normalized_email(user.get("email"))
        if email:
            kimai_by_email[email].append(user)

    rows: list[dict[str, str]] = []
    suggestions: dict[str, UserSuggestion] = {}
    for user in sorted(clockify_users, key=lambda item: normalized_email(item.get("email"))):
        email = normalized_email(user.get("email"))
        matches = kimai_by_email.get(email, [])
        target_email = ""
        target_id: int | None = None
        target_name = ""
        if len(matches) == 1:
            target = matches[0]
            target_email = normalized_email(target.get("email"))
            target_id = entity_id(target, "user")
            target_name = clean(target.get("alias") or target.get("username"))
            comment = "Unique Kimai user suggested from exact email"
        elif matches:
            comment = "Several Kimai users share this email; choose manually"
        else:
            comment = "No Kimai user has this exact email"
        suggestion = UserSuggestion(target_email, target_id, target_name, comment)
        if email:
            suggestions[email] = suggestion
        rows.append(
            {
                "Clockify User ID": clean(user.get("id")),
                "Clockify User": clean(user.get("name")),
                "Clockify User E-mail": email,
                "Clockify Status": clean(user.get("status")),
                "Kimai User ID": str(target_id or ""),
                "Kimai User": target_name,
                "Kimai User E-mail": target_email,
                "Status": "review",
                "Comment": comment + "; confirm before changing Status to approved",
            }
        )
    return rows, suggestions


def decimal_hours(start: datetime, end: datetime) -> str:
    value = Decimal(str((end - start).total_seconds())) / Decimal(3600)
    return format(value.quantize(Decimal("0.000001")), "f").rstrip("0").rstrip(".")


def clockify_report_time(value: datetime) -> str:
    return value.strftime("%H:%M:%S.%f").rstrip("0").rstrip(".")


def report_base_row(entry: ClockifyTimeEntry, local_zone: timezone) -> dict[str, str]:
    local_start = entry.start.astimezone(local_zone)
    local_end = entry.end.astimezone(local_zone) if entry.end else None
    return {
        "Project": entry.project_name,
        "Task": entry.task_name,
        "User": entry.user_name,
        "Email": entry.user_email,
        "Start Date": local_start.strftime("%Y-%m-%d"),
        "Start Time": clockify_report_time(local_start),
        "End Date": local_end.strftime("%Y-%m-%d") if local_end else "",
        "End Time": clockify_report_time(local_end) if local_end else "",
        "Duration (decimal)": decimal_hours(entry.start, entry.end) if entry.end else "",
        "Description": entry.description,
        "Billable": "Yes" if entry.billable else "No",
        "Tags": entry.tags,
    }


def kimai_timestamp(value: Any, local_zone: timezone) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ImportFailure(
            f"Kimai timesheet timestamp has an invalid timestamp: {text!r}."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_zone)
    return parsed.astimezone(timezone.utc)


def kimai_reference_id(value: Any) -> int | None:
    if isinstance(value, Mapping):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_audit_rows(
    entries: Sequence[ClockifyTimeEntry],
    activity_suggestions: Mapping[tuple[str, str], ActivitySuggestion],
    user_suggestions: Mapping[str, UserSuggestion],
    kimai_timesheets: Sequence[Mapping[str, Any]],
    local_zone: timezone,
) -> list[dict[str, str]]:
    base_rows = [report_base_row(entry, local_zone) for entry in entries]
    fingerprints = [make_source_fingerprint(row) for row in base_rows]
    duplicate_fingerprints = {
        fingerprint
        for fingerprint, count in Counter(fingerprints).items()
        if count > 1
    }
    timesheets_by_target: dict[tuple[int, int, int], list[Mapping[str, Any]]] = defaultdict(list)
    for item in kimai_timesheets:
        key = (
            kimai_reference_id(item.get("user")),
            kimai_reference_id(item.get("project")),
            kimai_reference_id(item.get("activity")),
        )
        if all(identifier is not None for identifier in key):
            timesheets_by_target[key].append(item)  # type: ignore[arg-type]

    rows: list[dict[str, str]] = []
    for entry, base, fingerprint in zip(entries, base_rows, fingerprints):
        activity = activity_suggestions.get(
            source_pair_key(entry.project_name, entry.task_name)
        )
        user = user_suggestions.get(entry.user_email)
        reasons: list[str] = []
        if fingerprint in duplicate_fingerprints:
            status = "duplicate_clockify_source"
            reasons.append("The same Clockify source fingerprint occurs more than once")
        elif entry.end is None:
            status = "in_progress_not_importable"
            reasons.append("Clockify entry has no end time")
        else:
            if not entry.project_name:
                reasons.append("Clockify project is missing or was not returned by the catalog")
            if not entry.task_name:
                reasons.append("Clockify task is missing or was not returned by the catalog")
            if user is None or user.user_id is None:
                reasons.append("No unique exact-email Kimai user suggestion")
            if activity is None or activity.project_id is None:
                reasons.append("No unique Kimai project suggestion")
            if activity is None or activity.activity_id is None:
                reasons.append("No unique project-specific Kimai activity suggestion")
            status = "unmapped" if reasons else "mapped_review_required"

        if not reasons and entry.end and user and activity:
            target_key = (user.user_id, activity.project_id, activity.activity_id)
            exact_duplicate = False
            possible_duplicate = False
            for item in timesheets_by_target.get(target_key, []):
                existing_start = kimai_timestamp(item.get("begin"), local_zone)
                existing_end = kimai_timestamp(item.get("end"), local_zone)
                if existing_start is None or existing_end is None:
                    continue
                same_time = (
                    abs((existing_start - entry.start).total_seconds()) <= 1
                    and abs((existing_end - entry.end).total_seconds()) <= 1
                )
                if not same_time:
                    continue
                if comparison_key(clean(item.get("description"))) == comparison_key(
                    entry.description
                ):
                    exact_duplicate = True
                else:
                    possible_duplicate = True
            if exact_duplicate:
                status = "already_in_kimai"
                reasons.append("Exact user/project/activity/time/description match exists in Kimai")
            elif possible_duplicate:
                status = "possible_kimai_duplicate"
                reasons.append("Same user/project/activity/time exists with another description")
            else:
                reasons.append("Mappings are suggestions and still require approval")

        rows.append(
            {
                **base,
                "Clockify Entry ID": entry.entry_id,
                "Clockify User ID": entry.user_id,
                "Clockify Project ID": entry.project_id,
                "Clockify Task ID": entry.task_id,
                "Start UTC": entry.start.isoformat(),
                "End UTC": entry.end.isoformat() if entry.end else "",
                "Suggested Kimai User": user.email if user else "",
                "Suggested Kimai User ID": str(user.user_id or "") if user else "",
                "Suggested Kimai Project": activity.project_name if activity else "",
                "Suggested Kimai Project ID": (
                    str(activity.project_id or "") if activity else ""
                ),
                "Suggested Kimai Activity": activity.activity_name if activity else "",
                "Suggested Kimai Activity ID": (
                    str(activity.activity_id or "") if activity else ""
                ),
                "Audit Status": status,
                "Audit Reason": "; ".join(reasons),
                "Source fingerprint": fingerprint,
            }
        )
    return rows


def output_paths(directory: Path) -> dict[str, Path]:
    return {
        "clockify_catalog": directory / CLOCKIFY_CATALOG_FILENAME,
        "kimai_catalog": directory / KIMAI_CATALOG_FILENAME,
        "activity_mapping": directory / ACTIVITY_MAPPING_FILENAME,
        "user_mapping": directory / USER_MAPPING_FILENAME,
        "audit": directory / AUDIT_FILENAME,
    }


def run(args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        raise ImportFailure("--timeout must be greater than zero.")
    if args.end_date < args.start_date:
        raise ImportFailure("--end-date must not be before --start-date.")
    paths = output_paths(Path(args.output_dir))
    existing = [path for path in paths.values() if path.exists()]
    if existing and not args.replace:
        raise ImportFailure(
            "Refusing to overwrite existing migration artifact(s): "
            + ", ".join(path.name for path in existing)
            + ". Use --replace only after preserving any reviewed mappings."
        )

    clockify_key = load_clockify_api_key(Path(args.clockify_env))
    clockify_api = ClockifyApi(args.clockify_base_url, clockify_key, args.timeout)
    workspace = select_workspace(clockify_api, args.workspace_id)
    workspace_id = clean(workspace.get("id"))
    clockify_rows, project_by_id, task_by_id = fetch_clockify_catalog(
        clockify_api, workspace
    )
    tag_by_id = fetch_clockify_tags(clockify_api, workspace_id)
    clockify_users, _ = fetch_clockify_users(clockify_api, workspace_id)
    clockify_entries = fetch_clockify_time_entries(
        clockify_api,
        workspace_id,
        clockify_users,
        project_by_id,
        task_by_id,
        tag_by_id,
        args.start_date,
        args.end_date,
        args.utc_offset,
    )

    kimai_token = load_token(
        Path(args.kimai_token_file) if args.kimai_token_file else None
    )
    kimai_api = KimaiApi(args.kimai_base_url, kimai_token, args.timeout)
    kimai_version, kimai_rows, kimai_project_count = collect_catalog(
        kimai_api, args.kimai_customer
    )
    kimai_users = kimai_api.collection("users", {"visible": 3})
    kimai_timesheets = kimai_api.collection(
        "timesheets",
        {
            "user": "all",
            "begin": f"{args.start_date.isoformat()}T00:00:00",
            "end": f"{args.end_date.isoformat()}T23:59:59",
            "active": "0",
            "full": "true",
            "order": "ASC",
            "orderBy": "begin",
        },
    )

    activity_rows, activity_suggestions = build_activity_mappings(
        clockify_rows, kimai_rows
    )
    user_rows, user_suggestions = build_user_mappings(clockify_users, kimai_users)
    audit_rows = build_audit_rows(
        clockify_entries,
        activity_suggestions,
        user_suggestions,
        kimai_timesheets,
        args.utc_offset,
    )

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    write_csv(paths["clockify_catalog"], CLOCKIFY_CATALOG_HEADERS, clockify_rows)
    write_catalog(paths["kimai_catalog"], kimai_rows)
    write_csv(
        paths["activity_mapping"], ACTIVITY_MAPPING_CATALOG_HEADERS, activity_rows
    )
    write_csv(paths["user_mapping"], USER_MAPPING_CATALOG_HEADERS, user_rows)
    write_csv(paths["audit"], AUDIT_HEADERS, audit_rows)

    status_counts = Counter(row["Audit Status"] for row in audit_rows)
    print(
        f"Read-only catalogs completed for Clockify workspace "
        f"{clean(workspace.get('name'))!r} and Kimai {kimai_version}."
    )
    print(
        f"Clockify projects: {len(project_by_id)}; catalog rows: {len(clockify_rows)}; "
        f"users: {len(clockify_users)}; time entries: {len(clockify_entries)}."
    )
    print(
        f"Kimai {args.kimai_customer} projects: {kimai_project_count}; "
        f"catalog rows: {len(kimai_rows)}; users: {len(kimai_users)}; "
        f"timesheets checked: {len(kimai_timesheets)}."
    )
    for status, count in sorted(status_counts.items()):
        print(f"Audit {status}: {count}")
    for path in paths.values():
        print(path.resolve())
    print("READ-ONLY: no Clockify or Kimai data was changed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read Clockify and Kimai APIs and build the five local migration "
            "catalog, mapping, and duplicate/unmapped audit files."
        )
    )
    parser.add_argument("--start-date", type=parse_date, required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", type=parse_date, required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--utc-offset",
        type=parse_utc_offset,
        default=parse_utc_offset(DEFAULT_UTC_OFFSET),
        help=(
            "Wall-clock offset used for import-compatible date/time columns "
            f"(default: {DEFAULT_UTC_OFFSET})"
        ),
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--clockify-env", default=DEFAULT_CLOCKIFY_ENV)
    parser.add_argument("--clockify-base-url", default=DEFAULT_CLOCKIFY_BASE_URL)
    parser.add_argument("--workspace-id", help="Defaults to the API user's active workspace")
    parser.add_argument("--kimai-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--kimai-customer", default=DEFAULT_CUSTOMER)
    parser.add_argument(
        "--kimai-token-file",
        help=(
            "Raw Kimai token file; otherwise use KIMAI_API_TOKEN, "
            "KIMAI_TOKEN_FILE, or ./.env"
        ),
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace all five outputs after intentionally preserving reviewed mappings",
    )
    return parser


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
