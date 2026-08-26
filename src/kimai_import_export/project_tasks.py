#!/usr/bin/env python3
"""Import reviewed projects and their approved tasks into Kimai.

The expected CSV is the manager-review export used by CDI:

    [PROJECT-CODE] Project name,,
    Manager / Ответственный,Status / Статус,
    Valid? / Актуально?,Task / Задача,Comment or correction / Комментарий
    ☑,Approved task,
    ☐,Rejected task,

Only checked rows are imported. In Kimai, these tasks are project-specific
activities. The script accepts the complete Google Sheets export containing
multiple project sections, or a folder containing several such CSV exports.
It is idempotent: it reuses an existing customer/project and skips activities
that already exist under that project.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import load_dotenv


load_dotenv()
DEFAULT_BASE_URL = os.environ.get("KIMAI_BASE_URL", "https://time.cdintl.org")
DEFAULT_CUSTOMER = os.environ.get("KIMAI_CUSTOMER", "CDI")
DEFAULT_CSV = "project-task-list.csv"
DEFAULT_TOKEN_FILE: str | None = None
MAX_ACTIVITY_LENGTH = 150
SELECTED_MARKERS = {"☑", "✅", "✓", "✔", "true", "yes", "1", "x", "[x]"}
UNSELECTED_MARKERS = {"", "☐", "false", "no", "0", "[ ]"}


class ImportFailure(RuntimeError):
    """A safe, user-facing import error."""


@dataclass(frozen=True)
class ApprovedTask:
    name: str
    comment: str
    line_number: int


@dataclass(frozen=True)
class ReviewExport:
    project_code: str
    project_name: str
    project_title: str
    tasks: tuple[ApprovedTask, ...]
    ignored_task_count: int


def clean(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value)).replace("\u00a0", " ").strip()


def comparison_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", clean(value)).casefold()
    value = value.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", value).strip()


def marker_state(value: str) -> bool | None:
    marker = comparison_key(value)
    if marker in {comparison_key(item) for item in SELECTED_MARKERS}:
        return True
    if marker in {comparison_key(item) for item in UNSELECTED_MARKERS}:
        return False
    return None


def _read_rows(csv_path: Path) -> list[list[str]]:
    try:
        text = csv_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ImportFailure(f"CSV file not found: {csv_path}") from exc
    except UnicodeDecodeError as exc:
        raise ImportFailure(
            f"{csv_path} is not UTF-8. Download it from Google Sheets as CSV."
        ) from exc

    if not text.strip():
        raise ImportFailure(f"CSV file is empty: {csv_path}")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect))


def _legacy_parse_review_csv(csv_path: Path) -> ReviewExport:
    rows = _read_rows(csv_path)
    first_index = next(
        (index for index, row in enumerate(rows) if row and clean(row[0])), None
    )
    if first_index is None:
        raise ImportFailure("The CSV contains no project header.")

    project_title = clean(rows[first_index][0])
    title_match = re.fullmatch(r"\[([^\]]+)\]\s*(.+)", project_title)
    if title_match is None:
        raise ImportFailure(
            "The first populated cell must be '[PROJECT-CODE] Project name'."
        )
    project_code = clean(title_match.group(1))
    project_name = clean(title_match.group(2))
    if not project_code or len(project_name) < 3:
        raise ImportFailure("The project code or project name is missing.")
    if len(project_title) > 150:
        raise ImportFailure("The combined project title exceeds Kimai's 150-character limit.")

    header_index = None
    for index, row in enumerate(rows[first_index + 1 :], start=first_index + 1):
        first = comparison_key(row[0] if row else "")
        second = comparison_key(row[1] if len(row) > 1 else "")
        if ("valid" in first or "актуально" in first) and (
            "task" in second or "задача" in second
        ):
            header_index = index
            break
    if header_index is None:
        raise ImportFailure(
            "Could not find the 'Valid? / Актуально?' and 'Task / Задача' header row."
        )

    approved: list[ApprovedTask] = []
    ignored = 0
    seen: dict[str, int] = {}
    unknown_markers: list[str] = []

    for index, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        marker = clean(row[0] if row else "")
        task_name = clean(row[1] if len(row) > 1 else "")
        comment = clean(row[2] if len(row) > 2 else "")
        if not marker and not task_name and not comment:
            continue

        selected = marker_state(marker)
        if selected is None:
            unknown_markers.append(f"line {index}: {marker!r}")
            continue
        if not selected:
            if task_name:
                ignored += 1
            continue
        if not task_name:
            raise ImportFailure(f"Line {index} is checked but has no task name.")
        if not 3 <= len(task_name) <= MAX_ACTIVITY_LENGTH:
            raise ImportFailure(
                f"Line {index} task length must be 3-{MAX_ACTIVITY_LENGTH} characters: {task_name!r}"
            )

        key = comparison_key(task_name)
        if key in seen:
            raise ImportFailure(
                f"Duplicate checked task on lines {seen[key]} and {index}: {task_name!r}"
            )
        seen[key] = index
        approved.append(ApprovedTask(task_name, comment, index))

    if unknown_markers:
        raise ImportFailure(
            "Unknown checkbox values were found; use only ☑ or ☐: "
            + "; ".join(unknown_markers)
        )
    if not approved:
        raise ImportFailure("No checked tasks were found. Nothing can be imported.")

    return ReviewExport(
        project_code=project_code,
        project_name=project_name,
        project_title=project_title,
        tasks=tuple(approved),
        ignored_task_count=ignored,
    )


def _project_header(row: list[str]) -> tuple[str, str, str] | None:
    """Return code/name/title for a project row, or None for other sheet rows."""
    title = clean(row[0] if row else "")
    match = re.fullmatch(r"\[([^\]]+)\]\s*(.+)", title)
    if match is None:
        return None
    code = clean(match.group(1))
    name = clean(match.group(2))
    if not code or len(name) < 3:
        raise ImportFailure(f"Invalid project header: {title!r}")
    if len(title) > 150:
        raise ImportFailure("The combined project title exceeds Kimai's 150-character limit.")
    return code, name, title


def _is_task_header(row: list[str]) -> bool:
    first = comparison_key(row[0] if row else "")
    second = comparison_key(row[1] if len(row) > 1 else "")
    return "valid" in first and "task" in second


def _parse_project_section(rows: list[list[str]], start: int, end: int) -> ReviewExport:
    header = _project_header(rows[start])
    if header is None:  # pragma: no cover - caller locates headers
        raise ImportFailure(f"Line {start + 1} is not a project header.")
    project_code, project_name, project_title = header
    header_index = next(
        (index for index in range(start + 1, end) if _is_task_header(rows[index])), None
    )
    if header_index is None:
        raise ImportFailure(f"Could not find the task header for project {project_code!r}.")

    approved: list[ApprovedTask] = []
    ignored = 0
    seen: dict[str, int] = {}
    unknown_markers: list[str] = []
    for index in range(header_index + 1, end):
        row = rows[index]
        marker = clean(row[0] if row else "")
        task_name = clean(row[1] if len(row) > 1 else "")
        comment = clean(row[2] if len(row) > 2 else "")
        if not marker and not task_name and not comment:
            continue
        selected = marker_state(marker)
        # Region/subsection labels (for example "Bishkek") can appear between
        # project blocks in a sheet export. They are not task rows.
        if not task_name and not comment and selected is None:
            continue
        if selected is None:
            unknown_markers.append(f"line {index + 1}: {marker!r}")
            continue
        if not selected:
            if task_name:
                ignored += 1
            continue
        if not task_name:
            raise ImportFailure(f"Line {index + 1} is checked but has no task name.")
        if not 3 <= len(task_name) <= MAX_ACTIVITY_LENGTH:
            raise ImportFailure(
                f"Line {index + 1} task length must be 3-{MAX_ACTIVITY_LENGTH} characters: {task_name!r}"
            )
        key = comparison_key(task_name)
        if key in seen:
            raise ImportFailure(
                f"Duplicate checked task on lines {seen[key]} and {index + 1}: {task_name!r}"
            )
        seen[key] = index + 1
        approved.append(ApprovedTask(task_name, comment, index + 1))

    if unknown_markers:
        raise ImportFailure(
            "Unknown checkbox values were found; use only ☑ or ☐: " + "; ".join(unknown_markers)
        )
    return ReviewExport(project_code, project_name, project_title, tuple(approved), ignored)


def parse_review_csvs(csv_path: Path) -> tuple[ReviewExport, ...]:
    """Parse every project section in a Google Sheets CSV export."""
    rows = _read_rows(csv_path)
    starts = [index for index, row in enumerate(rows) if _project_header(row) is not None]
    if not starts:
        raise ImportFailure("The CSV contains no project sections ('[PROJECT-CODE] Project name').")
    return tuple(
        _parse_project_section(rows, start, starts[position + 1] if position + 1 < len(starts) else len(rows))
        for position, start in enumerate(starts)
    )


def parse_review_csv(csv_path: Path) -> ReviewExport:
    """Backward-compatible parser for a CSV containing exactly one project."""
    reviews = parse_review_csvs(csv_path)
    if len(reviews) != 1:
        raise ImportFailure(
            f"The CSV contains {len(reviews)} project sections; use parse_review_csvs for a multi-project export."
        )
    if not reviews[0].tasks:
        raise ImportFailure("No checked tasks were found. Nothing can be imported.")
    return reviews[0]


def find_csv_files(source_path: Path) -> tuple[Path, ...]:
    """Return one CSV file, or all CSV files directly inside a folder."""
    if source_path.is_file():
        if source_path.suffix.casefold() != ".csv":
            raise ImportFailure(f"Input file is not a CSV file: {source_path}")
        return (source_path,)
    if source_path.is_dir():
        csv_files = sorted(
            (
                path
                for path in source_path.iterdir()
                if path.is_file() and path.suffix.casefold() == ".csv"
            ),
            key=lambda path: (path.name.casefold(), path.name),
        )
        if not csv_files:
            raise ImportFailure(f"No CSV files were found in folder: {source_path}")
        return tuple(csv_files)
    raise ImportFailure(f"CSV file or folder not found: {source_path}")


def load_token(token_file: Path | None = None) -> str:
    if token_file is None:
        environment_token = os.environ.get("KIMAI_API_TOKEN", "").strip()
        if environment_token:
            if any(character.isspace() for character in environment_token):
                raise ImportFailure("KIMAI_API_TOKEN contains whitespace.")
            return environment_token

        environment_token_file = os.environ.get("KIMAI_TOKEN_FILE", "").strip()
        if not environment_token_file:
            raise ImportFailure(
                "No API token configured. Use --token-file, KIMAI_TOKEN_FILE, "
                "or KIMAI_API_TOKEN."
            )
        token_file = Path(environment_token_file)

    try:
        token = token_file.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError as exc:
        raise ImportFailure(f"API token file not found: {token_file}") from exc
    except PermissionError as exc:
        raise ImportFailure(f"Cannot read the API token file: {token_file}") from exc
    except UnicodeDecodeError as exc:
        raise ImportFailure(f"API token file is not valid UTF-8: {token_file}") from exc
    if not token or any(character.isspace() for character in token):
        raise ImportFailure("The API token file is empty or contains whitespace.")
    return token


class KimaiApi:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        base_url = base_url.rstrip("/")
        if not base_url.startswith("https://"):
            raise ImportFailure("Kimai URL must use HTTPS.")
        self.base_url = base_url
        self._token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        endpoint = endpoint.lstrip("/")
        url = f"{self.base_url}/api/{endpoint}"
        if params:
            url += "?" + urlencode(params, doseq=True)

        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "CDI-Kimai-project-task-import/1.0",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                response_headers = dict(response.headers.items())
        except HTTPError as exc:
            body = exc.read(8192).decode("utf-8", errors="replace")
            body = body.replace(self._token, "[REDACTED]")
            raise ImportFailure(
                f"Kimai API returned HTTP {exc.code} for {method} /api/{endpoint}: {body or exc.reason}"
            ) from exc
        except URLError as exc:
            raise ImportFailure(
                f"Could not connect to Kimai for {method} /api/{endpoint}: {exc.reason}"
            ) from exc

        if not raw:
            return None, response_headers
        try:
            return json.loads(raw.decode("utf-8")), response_headers
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImportFailure(
                f"Kimai returned an invalid JSON response for {method} /api/{endpoint}."
            ) from exc

    def get(self, endpoint: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", endpoint, params=params)[0]

    def post(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
        return self._request("POST", endpoint, payload=payload)[0]

    def collection(
        self, endpoint: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        base_params = dict(params or {})
        page = 1
        items: list[dict[str, Any]] = []
        while True:
            request_params = {**base_params, "page": page, "size": 500}
            data, headers = self._request("GET", endpoint, params=request_params)
            if isinstance(data, list):
                page_items = data
            elif isinstance(data, dict):
                page_items = next(
                    (
                        data[key]
                        for key in ("data", "items", "results")
                        if isinstance(data.get(key), list)
                    ),
                    None,
                )
                if page_items is None:
                    raise ImportFailure(
                        f"Unexpected collection response from /api/{endpoint}."
                    )
            else:
                raise ImportFailure(f"Unexpected collection response from /api/{endpoint}.")
            items.extend(item for item in page_items if isinstance(item, dict))

            total_pages_text = next(
                (
                    value
                    for key, value in headers.items()
                    if key.casefold() == "x-total-pages"
                ),
                "1",
            )
            try:
                total_pages = int(total_pages_text)
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages:
                return items
            page += 1


def entity_id(entity: Mapping[str, Any], label: str) -> int:
    try:
        return int(entity["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ImportFailure(f"Kimai returned a {label} without a valid ID.") from exc


def exact_named(items: Iterable[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    key = comparison_key(name)
    return [item for item in items if comparison_key(clean(item.get("name"))) == key]


def find_customer(api: KimaiApi, customer_name: str) -> Mapping[str, Any]:
    customers = api.collection(
        "customers", {"term": customer_name, "visible": 3, "orderBy": "name"}
    )
    matches = exact_named(customers, customer_name)
    if not matches:
        raise ImportFailure(
            f"Kimai customer {customer_name!r} was not found. This script will not create customers."
        )
    if len(matches) > 1:
        raise ImportFailure(f"More than one Kimai customer is named {customer_name!r}.")
    return matches[0]


def find_project(
    api: KimaiApi,
    customer_id: int,
    review: ReviewExport,
    project_id_override: int | None,
) -> Mapping[str, Any] | None:
    if project_id_override is not None:
        project = api.get(f"projects/{project_id_override}")
        if not isinstance(project, dict):
            raise ImportFailure(f"Kimai project ID {project_id_override} was not found.")
        returned_customer = project.get("customer")
        if isinstance(returned_customer, dict):
            returned_customer = returned_customer.get("id")
        if returned_customer is not None and int(returned_customer) != customer_id:
            raise ImportFailure(
                f"Project ID {project_id_override} does not belong to the selected customer."
            )
        return project

    projects = api.collection("projects", {"customer": customer_id, "visible": 3})
    acceptable_names = {
        comparison_key(review.project_title),
        comparison_key(review.project_name),
    }
    matches = [
        project
        for project in projects
        if comparison_key(clean(project.get("name"))) in acceptable_names
    ]
    if len(matches) > 1:
        choices = ", ".join(
            f"ID {item.get('id')}: {clean(item.get('name'))!r}" for item in matches
        )
        raise ImportFailure(
            "Several existing projects match the CSV. Use --project-id to choose: " + choices
        )
    return matches[0] if matches else None


def existing_activity_map(
    api: KimaiApi, project_id: int
) -> dict[str, Mapping[str, Any]]:
    activities = api.collection(
        "activities", {"project": project_id, "visible": 3, "globals": "false"}
    )
    result: dict[str, Mapping[str, Any]] = {}
    for activity in activities:
        name = clean(activity.get("name"))
        if not name:
            continue
        key = comparison_key(name)
        if key in result:
            raise ImportFailure(
                f"Kimai contains duplicate activities named {name!r} in project ID {project_id}."
            )
        result[key] = activity
    return result


def _legacy_print_csv_summary(review: ReviewExport, csv_path: Path) -> None:
    print(f"CSV: {csv_path.resolve()}")
    print(f"Project code: {review.project_code}")
    print(f"Project name: {review.project_name}")
    print(f"Approved tasks: {len(review.tasks)}")
    print(f"Unchecked tasks ignored: {review.ignored_task_count}")
    for task in review.tasks:
        suffix = f" — {task.comment}" if task.comment else ""
        print(f"  + {task.name}{suffix}")


def _legacy_run(args: argparse.Namespace) -> int:
    if args.offline and args.apply:
        raise ImportFailure("--offline and --apply cannot be used together.")
    if args.timeout <= 0:
        raise ImportFailure("--timeout must be greater than zero.")
    if args.project_id is not None and args.project_id <= 0:
        raise ImportFailure("--project-id must be greater than zero.")

    csv_path = Path(args.csv_file)
    review = parse_review_csv(csv_path)
    print_csv_summary(review, csv_path)

    if args.offline:
        print("\nOffline validation passed. No API call was made.")
        return 0

    token = load_token(Path(args.token_file) if args.token_file else None)
    api = KimaiApi(args.base_url, token, args.timeout)
    version = api.get("version")
    version_text = clean(version.get("version")) if isinstance(version, dict) else "unknown"
    print(f"\nConnected to Kimai {version_text} at {args.base_url.rstrip('/')}")

    customer = find_customer(api, args.customer)
    customer_id = entity_id(customer, "customer")
    print(f"Customer: {clean(customer.get('name'))} (ID {customer_id})")

    project = find_project(api, customer_id, review, args.project_id)
    activity_map: dict[str, Mapping[str, Any]] = {}
    if project is None:
        print(f"Project: would create {review.project_title!r}")
        missing_tasks = list(review.tasks)
    else:
        project_id = entity_id(project, "project")
        print(f"Project: using {clean(project.get('name'))!r} (ID {project_id})")
        activity_map = existing_activity_map(api, project_id)
        missing_tasks = [
            task for task in review.tasks if comparison_key(task.name) not in activity_map
        ]

    existing_count = len(review.tasks) - len(missing_tasks)
    print(f"Existing approved activities: {existing_count}")
    print(f"Activities to create: {len(missing_tasks)}")
    for task in missing_tasks:
        print(f"  + {task.name}")

    if not args.apply:
        print("\nDRY RUN: no Kimai data was changed.")
        print("Run again with --apply and either --billable or --non-billable.")
        return 0

    if args.billable is None and (project is None or missing_tasks):
        raise ImportFailure(
            "Choose --billable or --non-billable before creating the project or activities."
        )

    # Creation is deliberately gated by both conditions. parse_review_csv already
    # rejects an empty approved-task list, but keep this invariant next to the
    # project POST so future changes cannot create an empty project accidentally.
    if project is None and not missing_tasks:
        raise ImportFailure("No new approved tasks remain; the project will not be created.")

    if project is None:
        created = api.post(
            "projects",
            {
                "name": review.project_title,
                "customer": customer_id,
                "comment": f"Official project code: {review.project_code}",
                "visible": True,
                "billable": args.billable,
                "globalActivities": False,
            },
        )
        if not isinstance(created, dict):
            raise ImportFailure("Kimai did not return the created project.")
        project = created
        project_id = entity_id(project, "project")
        print(f"Created project ID {project_id}: {clean(project.get('name'))}")
    else:
        project_id = entity_id(project, "project")

    created_count = 0
    for task in missing_tasks:
        payload: dict[str, Any] = {
            "name": task.name,
            "project": project_id,
            "visible": True,
            "billable": args.billable,
        }
        if task.comment:
            payload["comment"] = task.comment
        created = api.post("activities", payload)
        if not isinstance(created, dict):
            raise ImportFailure(f"Kimai did not return the created activity {task.name!r}.")
        created_count += 1
        print(f"Created activity ID {entity_id(created, 'activity')}: {task.name}")

    print(
        f"\nCompleted: project ID {project_id}; "
        f"created {created_count} activities; skipped {existing_count} existing activities."
    )
    return 0


def print_csv_summary(reviews: tuple[ReviewExport, ...], csv_path: Path) -> None:
    print(f"CSV: {csv_path.resolve()}")
    print(f"Project sections: {len(reviews)}")
    for review in reviews:
        if not review.tasks:
            print(f"SKIP [{review.project_code}] {review.project_name}: no checked tasks; project will not be created")
            continue
        print(f"\n[{review.project_code}] {review.project_name}")
        print(f"Approved tasks: {len(review.tasks)}")
        print(f"Unchecked tasks ignored: {review.ignored_task_count}")
        for task in review.tasks:
            suffix = f" — {task.comment}" if task.comment else ""
            print(f"  + {task.name}{suffix}")


def run(args: argparse.Namespace) -> int:
    if args.offline and args.apply:
        raise ImportFailure("--offline and --apply cannot be used together.")
    if args.timeout <= 0:
        raise ImportFailure("--timeout must be greater than zero.")
    if args.project_id is not None and args.project_id <= 0:
        raise ImportFailure("--project-id must be greater than zero.")

    source_path = Path(args.csv_file)
    csv_files = find_csv_files(source_path)
    active_reviews: list[tuple[Path, ReviewExport]] = []
    for csv_path in csv_files:
        reviews = parse_review_csvs(csv_path)
        print_csv_summary(reviews, csv_path)
        active_reviews.extend((csv_path, review) for review in reviews if review.tasks)

    if not active_reviews:
        raise ImportFailure("No checked tasks were found in any CSV file. Nothing can be imported.")
    if args.project_id is not None and len(active_reviews) != 1:
        raise ImportFailure(
            "--project-id can only be used when the input contains exactly one project with checked tasks."
        )

    if args.offline:
        print("\nOffline validation passed. No API call was made.")
        return 0

    token = load_token(Path(args.token_file) if args.token_file else None)
    api = KimaiApi(args.base_url, token, args.timeout)
    version = api.get("version")
    version_text = clean(version.get("version")) if isinstance(version, dict) else "unknown"
    print(f"\nConnected to Kimai {version_text} at {args.base_url.rstrip('/')}")

    customer = find_customer(api, args.customer)
    customer_id = entity_id(customer, "customer")
    print(f"Customer: {clean(customer.get('name'))} (ID {customer_id})")

    total_created = 0
    total_existing = 0
    for csv_path, review in active_reviews:
        print(f"\nSource CSV: {csv_path.resolve()}")
        project_override = args.project_id if len(active_reviews) == 1 else None
        project = find_project(api, customer_id, review, project_override)
        activity_map: dict[str, Mapping[str, Any]] = {}
        if project is None:
            print(f"\nProject: would create {review.project_title!r}")
            missing_tasks = list(review.tasks)
        else:
            project_id = entity_id(project, "project")
            print(f"\nProject: using {clean(project.get('name'))!r} (ID {project_id})")
            activity_map = existing_activity_map(api, project_id)
            missing_tasks = [task for task in review.tasks if comparison_key(task.name) not in activity_map]

        existing_count = len(review.tasks) - len(missing_tasks)
        total_existing += existing_count
        print(f"Existing approved activities: {existing_count}")
        print(f"Activities to create: {len(missing_tasks)}")
        for task in missing_tasks:
            print(f"  + {task.name}")

        if not args.apply:
            continue
        if args.billable is None and (project is None or missing_tasks):
            raise ImportFailure(
                "Choose --billable or --non-billable before creating the project or activities."
            )
        if project is None and not missing_tasks:
            raise ImportFailure("No new approved tasks remain; the project will not be created.")

        if project is None:
            created = api.post(
                "projects",
                {
                    "name": review.project_title,
                    "customer": customer_id,
                    "comment": f"Official project code: {review.project_code}",
                    "visible": True,
                    "billable": args.billable,
                    "globalActivities": False,
                },
            )
            if not isinstance(created, dict):
                raise ImportFailure("Kimai did not return the created project.")
            project = created
            project_id = entity_id(project, "project")
            print(f"Created project ID {project_id}: {clean(project.get('name'))}")
        else:
            project_id = entity_id(project, "project")

        for task in missing_tasks:
            payload: dict[str, Any] = {
                "name": task.name,
                "project": project_id,
                "visible": True,
                "billable": args.billable,
            }
            if task.comment:
                payload["comment"] = task.comment
            created = api.post("activities", payload)
            if not isinstance(created, dict):
                raise ImportFailure(f"Kimai did not return the created activity {task.name!r}.")
            total_created += 1
            print(f"Created activity ID {entity_id(created, 'activity')}: {task.name}")

    if not args.apply:
        print("\nDRY RUN: no Kimai data was changed.")
        print("Run again with --apply and either --billable or --non-billable.")
        return 0
    print(f"\nCompleted: created {total_created} activities; skipped {total_existing} existing activities.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import checked tasks from a Google Sheets CSV file or a folder of CSV files into Kimai.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  kimai-import-project-tasks --offline\n"
            "  kimai-import-project-tasks project-task-list-administration.csv --offline\n"
            "  kimai-import-project-tasks .\\project-task-csv --offline\n"
            "  kimai-import-project-tasks --non-billable\n"
            "  kimai-import-project-tasks --non-billable --apply\n"
            "\nWithout --apply, the script connects to Kimai but performs a dry run."
        ),
    )
    parser.add_argument(
        "csv_file",
        nargs="?",
        default=DEFAULT_CSV,
        help=(
            "Google Sheets CSV export, or folder containing CSV exports "
            f"(searches that folder only; default: {DEFAULT_CSV})"
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Kimai base URL")
    parser.add_argument("--customer", default=DEFAULT_CUSTOMER, help="Existing customer name")
    parser.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        help=(
            "UTF-8 file containing the API token; otherwise use KIMAI_TOKEN_FILE "
            "or KIMAI_API_TOKEN"
        ),
    )
    parser.add_argument(
        "--project-id",
        type=int,
        help="Use a specific existing Kimai project ID instead of matching by name",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--offline", action="store_true", help="Validate only; do not read the token or contact Kimai"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Create the missing project/activities"
    )
    billable = parser.add_mutually_exclusive_group()
    billable.add_argument(
        "--billable", dest="billable", action="store_true", help="Create billable entities"
    )
    billable.add_argument(
        "--non-billable",
        dest="billable",
        action="store_false",
        help="Create non-billable entities",
    )
    parser.set_defaults(billable=None)
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
