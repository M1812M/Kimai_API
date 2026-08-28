#!/usr/bin/env python3
"""Read-only, resumable Clockify emergency backup and offline verification."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .clockify_times import REQUIRED_CLOCKIFY_COLUMNS, read_clockify_entries
from .config import load_dotenv
from .project_tasks import clean


TOOL_VERSION = "0.4.0"
SCHEMA_VERSION = 1
DEFAULT_API_BASE_URL = "https://api.clockify.me/api/v1"
DEFAULT_REPORTS_BASE_URL = "https://reports.api.clockify.me/v1"
DEFAULT_AUDIT_BASE_URL = "https://auditlog-api.api.clockify.me/v1"
DEFAULT_OUTPUT_ROOT = Path("data") / "clockify-backups"
EARLIEST = datetime(1970, 1, 1, tzinfo=timezone.utc)
PAGE_SIZE = 200
REPORT_PAGE_SIZE = 1000
MAX_REPORT_RANGE = timedelta(days=21)
MAX_CHANGE_RANGE = timedelta(days=92)
ENTITY_TYPES = (
    "CLIENTS",
    "PROJECTS",
    "TAGS",
    "TASKS",
    "SCHEDULED_ASSIGNMENT",
    "TIME_ENTRY",
    "TIME_ENTRY_RATE",
    "TIME_ENTRY_CUSTOM_FIELD_VALUE",
    "CUSTOM_FIELDS",
    "USER",
    "USER_GROUPS",
    "INVOICES",
    "APPROVAL_REQUESTS",
    "BALANCE",
    "HOLIDAYS",
    "PTO_POLICY",
    "TIME_OFF_REQUEST",
)
AUDIT_ACTIONS = (
    "CREATE_TIME_PERSONAL_TIMER",
    "CREATE_TIME_PERSONAL_MANUAL",
    "CREATE_TIME_IMPORT",
    "CREATE_TIME_KIOSK",
    "CREATE_TIME_FOR_OTHER",
    "RESTORE_TIME",
    "RESTORE_TIME_FOR_OTHER",
    "UPDATE_TIME_PERSONAL",
    "UPDATE_TIME_FOR_OTHER",
    "DELETE_TIME_PERSONAL",
    "DELETE_TIME_FOR_OTHER",
    "CREATE_PROJECT",
    "CREATE_PROJECT_IMPORT",
    "CREATE_PROJECT_QUICKBOOKS",
    "UPDATE_PROJECT",
    "DELETE_PROJECT",
    "CREATE_TASK",
    "CREATE_TASK_IMPORT",
    "UPDATE_TASK",
    "DELETE_TASK",
    "CREATE_CLIENT",
    "CREATE_CLIENT_IMPORT",
    "CREATE_CLIENT_QUICKBOOKS",
    "UPDATE_CLIENT",
    "DELETE_CLIENT",
    "CREATE_TAG",
    "CREATE_TAG_IMPORT",
    "UPDATE_TAG",
    "DELETE_TAG",
    "CREATE_EXPENSE",
    "CREATE_EXPENSE_FOR_OTHER",
    "RESTORE_EXPENSE",
    "RESTORE_EXPENSE_FOR_OTHER",
    "UPDATE_EXPENSE",
    "UPDATE_EXPENSE_FOR_OTHER",
    "DELETE_EXPENSE",
    "DELETE_EXPENSE_FOR_OTHER",
)
RANGE_ERROR_CODES = {400, 413, 414, 422}
OPTION_NOT_ENABLED_CODES = {402, 404}
TRANSIENT_CODES = {429, 500, 502, 503, 504}

SECRET_KEY_PATTERN = re.compile(
    r"(?:api[-_]?key|authorization|cookie|password|secret|token)", re.IGNORECASE
)
SAFE_SEGMENT_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
POST_ALLOWLIST = (
    re.compile(r"^/v1/workspaces/[^/]+/reports/(?:detailed|expenses/detailed)$"),
    re.compile(r"^/v1/workspaces/[^/]+/audit-log$"),
    re.compile(r"^/api/v1/workspaces/[^/]+/users/info$"),
    re.compile(r"^/api/v1/workspaces/[^/]+/time-off/requests$"),
    re.compile(r"^/api/v1/workspaces/[^/]+/scheduling/assignments/projects/totals$"),
)
COLLECTION_KEYS = (
    "timeentries",
    "timeEntries",
    "entries",
    "users",
    "clients",
    "projects",
    "tasks",
    "tags",
    "expenses",
    "invoices",
    "requests",
    "assignments",
    "groups",
    "items",
    "data",
    "response",
)

CSV_HEADERS = (
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
    "Clockify Entry ID",
    "Clockify User ID",
    "Clockify Project ID",
    "Clockify Task ID",
    "Start UTC",
    "End UTC",
    "Type",
    "Locked",
    "Hourly Rate",
    "Cost Rate",
    "Custom Fields JSON",
)


class BackupFailure(RuntimeError):
    """A safe, user-facing backup error."""


class AuthenticationFailure(BackupFailure):
    """Clockify rejected the API key."""


class ReadOnlyViolation(BackupFailure):
    """A request was blocked by the backup client's safety boundary."""


class HttpFailure(BackupFailure):
    def __init__(self, status: int, method: str, endpoint: str, detail: str) -> None:
        self.status = status
        self.method = method
        self.endpoint = endpoint
        self.detail = detail
        super().__init__(f"Clockify returned HTTP {status} for {method} {endpoint}: {detail}")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]
    content_type: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Expected an ISO-8601 UTC timestamp, for example 2026-08-27T12:00:00Z."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_segment(value: str) -> str:
    segment = SAFE_SEGMENT_PATTERN.sub("_", value.strip()).strip("._")
    return segment or "unknown"


def replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(10):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(content)
    replace_with_retry(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def redact_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if SECRET_KEY_PATTERN.search(str(key))
                else redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def decode_json(response: HttpResponse, endpoint: str) -> Any:
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupFailure(f"Clockify returned invalid JSON for {endpoint}.") from exc


def extract_items(data: Any, item_keys: Sequence[str] = ()) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, Mapping):
        return []
    for key in (*item_keys, *COLLECTION_KEYS):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def object_id(value: Mapping[str, Any]) -> str:
    return clean(value.get("id") or value.get("_id") or value.get("timeEntryId"))


def deduplicate(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_payloads: set[str] = set()
    for item in items:
        materialized = dict(item)
        identifier = object_id(materialized)
        if identifier:
            if identifier in seen_ids:
                continue
            seen_ids.add(identifier)
        else:
            fingerprint = json.dumps(materialized, ensure_ascii=False, sort_keys=True)
            if fingerprint in seen_payloads:
                continue
            seen_payloads.add(fingerprint)
        result.append(materialized)
    return result


class ReadOnlyClockifyClient:
    """HTTP client that cannot send undocumented or mutating requests."""

    def __init__(
        self,
        api_key: str,
        *,
        api_base_url: str = DEFAULT_API_BASE_URL,
        reports_base_url: str = DEFAULT_REPORTS_BASE_URL,
        audit_base_url: str = DEFAULT_AUDIT_BASE_URL,
        timeout: float = 30.0,
        request_delay: float = 0.0,
        max_retries: int = 6,
        event_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        if not api_key or any(character.isspace() for character in api_key):
            raise BackupFailure("CLOCKIFY_API_KEY is missing or contains whitespace.")
        self._api_key = api_key
        self.base_urls = {
            "api": self._validate_base_url(api_base_url),
            "reports": self._validate_base_url(reports_base_url),
            "audit": self._validate_base_url(audit_base_url),
        }
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_retries = max_retries
        self.event_sink = event_sink

    @staticmethod
    def _validate_base_url(value: str) -> str:
        normalized = value.rstrip("/")
        if urlparse(normalized).scheme != "https":
            raise BackupFailure("Every Clockify API base URL must use HTTPS.")
        return normalized

    @staticmethod
    def _validate_method(method: str, url: str) -> None:
        normalized = method.upper()
        if normalized == "GET":
            return
        if normalized != "POST":
            raise ReadOnlyViolation(
                f"Backup mode blocks HTTP {normalized}; only GET and allowlisted read-only POST are permitted."
            )
        path = urlparse(url).path
        if not any(pattern.fullmatch(path) for pattern in POST_ALLOWLIST):
            raise ReadOnlyViolation(f"Backup mode blocks non-allowlisted POST endpoint: {path}")

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        base: str = "api",
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        accept: str = "application/json",
    ) -> HttpResponse:
        if base not in self.base_urls:
            raise BackupFailure(f"Unknown Clockify API base kind: {base}")
        endpoint = endpoint.lstrip("/")
        url = f"{self.base_urls[base]}/{endpoint}"
        if params:
            url += "?" + urlencode(params, doseq=True)
        normalized_method = method.upper()
        self._validate_method(normalized_method, url)
        payload = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        headers = {
            "Accept": accept,
            "X-Api-Key": self._api_key,
            "User-Agent": f"CDI-Clockify-emergency-backup/{TOOL_VERSION}",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(1, self.max_retries + 2):
            if self.request_delay:
                time.sleep(self.request_delay)
            request = Request(
                url, data=payload, headers=headers, method=normalized_method
            )
            started = time.monotonic()
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    response_headers = dict(response.headers.items())
                    status = int(getattr(response, "status", 200))
                self._log_event(
                    normalized_method,
                    endpoint,
                    base,
                    status,
                    attempt,
                    len(raw),
                    time.monotonic() - started,
                )
                return HttpResponse(
                    status,
                    raw,
                    response_headers,
                    response_headers.get("Content-Type", "application/octet-stream"),
                )
            except HTTPError as exc:
                raw_error = exc.read(8192)
                detail = raw_error.decode("utf-8", errors="replace").replace(
                    self._api_key, "[REDACTED]"
                )
                self._log_event(
                    normalized_method,
                    endpoint,
                    base,
                    exc.code,
                    attempt,
                    len(raw_error),
                    time.monotonic() - started,
                )
                if exc.code == 401:
                    raise AuthenticationFailure(
                        "Clockify rejected CLOCKIFY_API_KEY (HTTP 401)."
                    ) from exc
                if exc.code in TRANSIENT_CODES and attempt <= self.max_retries:
                    retry_after = exc.headers.get("Retry-After", "")
                    try:
                        wait_seconds = float(retry_after)
                    except ValueError:
                        wait_seconds = min(60.0, 2.0 ** (attempt - 1))
                    time.sleep(max(0.0, wait_seconds))
                    continue
                raise HttpFailure(
                    exc.code,
                    normalized_method,
                    endpoint,
                    detail or str(exc.reason),
                ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                self._log_event(
                    normalized_method,
                    endpoint,
                    base,
                    None,
                    attempt,
                    0,
                    time.monotonic() - started,
                )
                if attempt <= self.max_retries:
                    time.sleep(min(60.0, 2.0 ** (attempt - 1)))
                    continue
                reason = getattr(exc, "reason", exc)
                raise BackupFailure(
                    f"Could not connect to Clockify for {normalized_method} {endpoint}: {reason}"
                ) from exc
        raise AssertionError("retry loop did not terminate")

    def _log_event(
        self,
        method: str,
        endpoint: str,
        base: str,
        status: int | None,
        attempt: int,
        response_bytes: int,
        elapsed: float,
    ) -> None:
        if self.event_sink is not None:
            self.event_sink(
                {
                    "timestamp": iso_z(utc_now()),
                    "method": method,
                    "base": base,
                    "endpoint": endpoint,
                    "status": status,
                    "attempt": attempt,
                    "response_bytes": response_bytes,
                    "elapsed_seconds": round(elapsed, 3),
                }
            )


class BackupSession:
    def __init__(
        self,
        run_dir: Path,
        client: ReadOnlyClockifyClient,
        *,
        cutoff: datetime,
        requested_workspace_ids: Sequence[str],
        resume: bool,
    ) -> None:
        self.run_dir = run_dir
        self.client = client
        self.manifest_path = run_dir / "manifest.json"
        self.requests_log = run_dir / "logs" / "requests.jsonl"
        self.gaps_log = run_dir / "logs" / "gaps.jsonl"
        if resume:
            try:
                self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackupFailure(f"Cannot resume invalid backup directory: {run_dir}") from exc
            if self.manifest.get("status") == "COMPLETE":
                raise BackupFailure("This backup is already COMPLETE and cannot be resumed.")
            expected_cutoff = parse_utc(str(self.manifest.get("cutoff_utc", "")))
            if expected_cutoff != cutoff:
                raise BackupFailure("Resume cutoff does not match the existing manifest.")
        else:
            self.manifest = {
                "schema_version": SCHEMA_VERSION,
                "tool_version": TOOL_VERSION,
                "run_id": run_dir.name,
                "status": "RUNNING",
                "started_at_utc": iso_z(utc_now()),
                "cutoff_utc": iso_z(cutoff),
                "range_start_utc": iso_z(EARLIEST),
                "requested_workspace_ids": list(requested_workspace_ids),
                "discovered_workspace_ids": [],
                "selected_workspace_ids": [],
                "workspaces": {},
                "datasets": {},
                "gaps": [],
                "manual_gaps": [
                    {
                        "dataset": "auto-tracker",
                        "reason": "Auto Tracker data is local to each computer and is not exposed by the public API.",
                    },
                    {
                        "dataset": "screenshots-and-gps",
                        "reason": "No documented public API download endpoint is available; check manually or with Clockify support if enabled.",
                    },
                ],
                "assets_expected": [],
                "assets_downloaded": [],
                "asset_gaps": [],
                "read_only_policy": {
                    "allowed_methods": ["GET", "allowlisted read-only POST"],
                    "blocked_methods": ["PUT", "PATCH", "DELETE"],
                    "credentials_recorded": False,
                },
            }
            self.run_dir.mkdir(parents=True, exist_ok=False)
            self.save_manifest()

    def request_event(self, event: Mapping[str, Any]) -> None:
        append_jsonl(self.requests_log, event)

    def save_manifest(self) -> None:
        atomic_json(self.manifest_path, self.manifest)

    def _dataset_record(self, key: str, *, core: bool) -> dict[str, Any]:
        record = self.manifest["datasets"].setdefault(
            key,
            {
                "status": "RUNNING",
                "core": core,
                "pages": 0,
                "items": 0,
                "pagination_complete": False,
                "files": [],
            },
        )
        return record

    def _raw_path(
        self, workspace_id: str, dataset: str, page: int, extension: str = ".json"
    ) -> Path:
        parts = [safe_segment(part) for part in dataset.split("/") if part]
        return self.run_dir / "raw" / safe_segment(workspace_id) / Path(*parts) / (
            f"page-{page:06d}{extension}"
        )

    def _store_response(
        self,
        workspace_id: str,
        dataset: str,
        page: int,
        response: HttpResponse,
        *,
        redact: bool = False,
        extension: str | None = None,
    ) -> Path:
        selected_extension = extension
        if selected_extension is None:
            selected_extension = (
                ".json" if "json" in response.content_type.casefold() else ".bin"
            )
        content = response.body
        if redact:
            data = decode_json(response, dataset)
            content = (
                json.dumps(redact_secrets(data), ensure_ascii=False, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
        path = self._raw_path(workspace_id, dataset, page, selected_extension)
        atomic_write(path, content)
        return path

    def _complete_dataset(
        self,
        key: str,
        *,
        pages: int,
        items: int,
        files: Sequence[Path],
        redacted: bool = False,
        core: bool,
    ) -> None:
        record = self._dataset_record(key, core=core)
        record.update(
            {
                "status": "complete",
                "pages": pages,
                "items": items,
                "pagination_complete": True,
                "files": [path.relative_to(self.run_dir).as_posix() for path in files],
                "redacted": redacted,
                "completed_at_utc": iso_z(utc_now()),
            }
        )
        self.save_manifest()

    def _record_gap(
        self,
        key: str,
        exc: Exception,
        *,
        core: bool,
        recommendation: str,
    ) -> None:
        status = getattr(exc, "status", None)
        gap_status = (
            "not_enabled"
            if status in OPTION_NOT_ENABLED_CODES and not core
            else "permission_denied"
            if status == 403
            else "failed"
        )
        gap = {
            "timestamp": iso_z(utc_now()),
            "dataset": key,
            "status": gap_status,
            "core": core,
            "http_status": status,
            "reason": str(exc),
            "recommendation": recommendation,
        }
        self.manifest["gaps"].append(gap)
        record = self._dataset_record(key, core=core)
        record.update(
            {
                "status": gap_status,
                "pagination_complete": False,
                "error": str(exc),
                "http_status": status,
            }
        )
        append_jsonl(self.gaps_log, gap)
        self.save_manifest()

    def load_completed_items(
        self, key: str, *, item_keys: Sequence[str] = ()
    ) -> list[dict[str, Any]] | None:
        record = self.manifest["datasets"].get(key)
        if not record or record.get("status") != "complete":
            return None
        result: list[dict[str, Any]] = []
        for relative in record.get("files", []):
            path = self.run_dir / relative
            if path.suffix.casefold() != ".json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackupFailure(f"Completed dataset file is unreadable: {path}") from exc
            result.extend(extract_items(data, item_keys))
            if isinstance(data, Mapping) and not extract_items(data, item_keys):
                result.append(dict(data))
        return result

    def fetch_json(
        self,
        key: str,
        workspace_id: str,
        endpoint: str,
        *,
        core: bool,
        base: str = "api",
        params: Mapping[str, Any] | None = None,
        redact: bool = False,
    ) -> Any | None:
        completed = self.load_completed_items(key)
        if completed is not None:
            if len(completed) == 1:
                return completed[0]
            return completed
        try:
            response = self.client.request(
                "GET", endpoint, base=base, params=params
            )
            data = decode_json(response, endpoint)
            path = self._store_response(
                workspace_id, key, 1, response, redact=redact
            )
            item_count = len(data) if isinstance(data, list) else 1
            self._complete_dataset(
                key,
                pages=1,
                items=item_count,
                files=[path],
                redacted=redact,
                core=core,
            )
            return redact_secrets(data) if redact else data
        except AuthenticationFailure:
            raise
        except (BackupFailure, HttpFailure) as exc:
            self._record_gap(
                key,
                exc,
                core=core,
                recommendation="Check the Clockify plan, API-key permissions, and endpoint availability.",
            )
            return None

    def fetch_collection(
        self,
        key: str,
        workspace_id: str,
        endpoint: str,
        *,
        core: bool,
        base: str = "api",
        params: Mapping[str, Any] | None = None,
        item_keys: Sequence[str] = (),
        page_size: int = PAGE_SIZE,
        page_parameter: str = "page",
        page_size_parameter: str = "page-size",
        start_page: int = 1,
        redact: bool = False,
        raise_range_errors: bool = False,
    ) -> list[dict[str, Any]]:
        completed = self.load_completed_items(key, item_keys=item_keys)
        if completed is not None:
            return deduplicate(completed)
        record = self._dataset_record(key, core=core)
        existing_files = [self.run_dir / value for value in record.get("files", [])]
        all_items: list[dict[str, Any]] = []
        for path in existing_files:
            if path.suffix.casefold() == ".json" and path.is_file():
                all_items.extend(
                    extract_items(json.loads(path.read_text(encoding="utf-8")), item_keys)
                )
        page = int(record.get("next_page", start_page + len(existing_files)))
        files = list(existing_files)
        try:
            while True:
                query = dict(params or {})
                query[page_parameter] = page
                query[page_size_parameter] = page_size
                response = self.client.request("GET", endpoint, base=base, params=query)
                data = decode_json(response, endpoint)
                page_items = extract_items(data, item_keys)
                path = self._store_response(
                    workspace_id, key, page, response, redact=redact
                )
                files.append(path)
                all_items.extend(redact_secrets(page_items) if redact else page_items)
                record.update(
                    {
                        "status": "RUNNING",
                        "pages": len(files),
                        "next_page": page + 1,
                        "items": len(all_items),
                        "files": [
                            item.relative_to(self.run_dir).as_posix() for item in files
                        ],
                    }
                )
                self.save_manifest()
                last_page_header = next(
                    (
                        str(value).casefold()
                        for header, value in response.headers.items()
                        if header.casefold() == "last-page"
                    ),
                    "",
                )
                total = None
                if isinstance(data, Mapping):
                    for total_key in ("total", "count"):
                        if isinstance(data.get(total_key), int):
                            total = int(data[total_key])
                            break
                if (
                    last_page_header == "true"
                    or (total is not None and len(all_items) >= total)
                    or len(page_items) < page_size
                ):
                    break
                page += 1
                if page > 100000:
                    raise BackupFailure(f"Pagination did not terminate for {endpoint}.")
            result = deduplicate(all_items)
            self._complete_dataset(
                key,
                pages=len(files),
                items=len(result),
                files=files,
                redacted=redact,
                core=core,
            )
            return result
        except AuthenticationFailure:
            raise
        except HttpFailure as exc:
            if raise_range_errors and exc.status in RANGE_ERROR_CODES:
                self.manifest["datasets"].pop(key, None)
                self.save_manifest()
                raise
            self._record_gap(
                key,
                exc,
                core=core,
                recommendation="Check the Clockify plan, API-key permissions, and endpoint availability.",
            )
            return []
        except BackupFailure as exc:
            self._record_gap(
                key,
                exc,
                core=core,
                recommendation="Resume the backup after the connection or API issue is resolved.",
            )
            return []

    def fetch_post_collection(
        self,
        key: str,
        workspace_id: str,
        endpoint: str,
        *,
        body: Mapping[str, Any],
        core: bool,
        base: str,
        item_keys: Sequence[str],
        page_size: int = PAGE_SIZE,
        page_parameter: str = "page",
        page_size_parameter: str = "pageSize",
        raise_range_errors: bool = False,
    ) -> list[dict[str, Any]]:
        completed = self.load_completed_items(key, item_keys=item_keys)
        if completed is not None:
            return deduplicate(completed)
        files: list[Path] = []
        all_items: list[dict[str, Any]] = []
        page = 1
        try:
            while True:
                request_body = dict(body)
                detailed = request_body.get("detailedFilter")
                if isinstance(detailed, Mapping):
                    request_body["detailedFilter"] = {
                        **detailed,
                        "page": page,
                        "pageSize": page_size,
                    }
                else:
                    request_body.update(
                        {page_parameter: page, page_size_parameter: page_size}
                    )
                response = self.client.request(
                    "POST", endpoint, base=base, body=request_body
                )
                data = decode_json(response, endpoint)
                page_items = extract_items(data, item_keys)
                path = self._store_response(workspace_id, key, page, response)
                files.append(path)
                all_items.extend(page_items)
                total = None
                if isinstance(data, Mapping):
                    for total_key in ("total", "count"):
                        if isinstance(data.get(total_key), int):
                            total = int(data[total_key])
                            break
                if (total is not None and len(all_items) >= total) or len(page_items) < page_size:
                    break
                page += 1
                if page > 100000:
                    raise BackupFailure(f"Pagination did not terminate for {endpoint}.")
            result = deduplicate(all_items)
            self._complete_dataset(
                key, pages=len(files), items=len(result), files=files, core=core
            )
            return result
        except AuthenticationFailure:
            raise
        except HttpFailure as exc:
            if raise_range_errors and exc.status in RANGE_ERROR_CODES:
                raise
            self._record_gap(
                key,
                exc,
                core=core,
                recommendation="Check the Clockify plan and the permitted report interval.",
            )
            return []


def _range_label(start: datetime, end: datetime) -> str:
    return f"{start:%Y%m%dT%H%M%SZ}_{end:%Y%m%dT%H%M%SZ}"


def _split_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    midpoint = start + (end - start) / 2
    midpoint = midpoint.replace(microsecond=0)
    if midpoint <= start:
        midpoint = start + timedelta(seconds=1)
    return midpoint, midpoint


def fetch_time_entries_adaptive(
    session: BackupSession,
    workspace_id: str,
    user_id: str,
    start: datetime,
    end: datetime,
    *,
    namespace: str = "time-entries",
) -> list[dict[str, Any]]:
    label = _range_label(start, end)
    key = f"{workspace_id}/{namespace}/user-{safe_segment(user_id)}/{label}"
    endpoint = f"workspaces/{workspace_id}/user/{user_id}/time-entries"
    try:
        return session.fetch_collection(
            key,
            workspace_id,
            endpoint,
            core=True,
            params={"start": iso_z(start), "end": iso_z(end), "hydrated": "true"},
            page_size=PAGE_SIZE,
            raise_range_errors=True,
        )
    except HttpFailure as exc:
        if exc.status not in RANGE_ERROR_CODES or end - start <= timedelta(days=1):
            session._record_gap(
                key,
                exc,
                core=True,
                recommendation="Retry this exact user/time range or export it manually from Detailed Report.",
            )
            return []
        midpoint, _ = _split_range(start, end)
        left = fetch_time_entries_adaptive(
            session, workspace_id, user_id, start, midpoint, namespace=namespace
        )
        right = fetch_time_entries_adaptive(
            session, workspace_id, user_id, midpoint, end, namespace=namespace
        )
        return deduplicate([*left, *right])


def fetch_detailed_report_adaptive(
    session: BackupSession,
    workspace_id: str,
    start: datetime,
    end: datetime,
    *,
    capability_probe: bool = True,
) -> list[dict[str, Any]]:
    if capability_probe and end - start > timedelta(days=1):
        probe_start = max(start, end - timedelta(days=1))
        probe_rows = fetch_detailed_report_adaptive(
            session,
            workspace_id,
            probe_start,
            end,
            capability_probe=False,
        )
        probe_key = (
            f"{workspace_id}/detailed-report-json/"
            f"{_range_label(probe_start, end)}"
        )
        if session.manifest["datasets"].get(probe_key, {}).get("status") != "complete":
            return probe_rows
        bounds = session.manifest.setdefault("report_bounds", {})
        bound_record = bounds.get(workspace_id, {})
        effective_start_text = clean(bound_record.get("earliest_supported_utc"))
        if effective_start_text:
            effective_start = parse_utc(effective_start_text)
        else:
            oldest_end = min(end, start + timedelta(days=1))
            fetch_detailed_report_adaptive(
                session,
                workspace_id,
                start,
                oldest_end,
                capability_probe=False,
            )
            oldest_key = (
                f"{workspace_id}/detailed-report-json/"
                f"{_range_label(start, oldest_end)}"
            )
            if (
                session.manifest["datasets"].get(oldest_key, {}).get("status")
                == "complete"
            ):
                effective_start = start
            else:
                unsupported = start
                supported = probe_start
                while supported - unsupported > timedelta(seconds=1):
                    midpoint, _ = _split_range(unsupported, supported)
                    midpoint_end = min(end, midpoint + timedelta(days=1))
                    fetch_detailed_report_adaptive(
                        session,
                        workspace_id,
                        midpoint,
                        midpoint_end,
                        capability_probe=False,
                    )
                    midpoint_key = (
                        f"{workspace_id}/detailed-report-json/"
                        f"{_range_label(midpoint, midpoint_end)}"
                    )
                    if (
                        session.manifest["datasets"]
                        .get(midpoint_key, {})
                        .get("status")
                        == "complete"
                    ):
                        supported = midpoint
                    else:
                        unsupported = midpoint
                effective_start = supported
            bounds[workspace_id] = {
                "requested_start_utc": iso_z(start),
                "earliest_supported_utc": iso_z(effective_start),
                "determined_at_utc": iso_z(utc_now()),
            }
            session.save_manifest()
        return deduplicate(
            [
                *fetch_detailed_report_adaptive(
                    session,
                    workspace_id,
                    effective_start,
                    end,
                    capability_probe=False,
                ),
                *probe_rows,
            ]
        )
    label = _range_label(start, end)
    key = f"{workspace_id}/detailed-report-json/{label}"
    if end - start > MAX_REPORT_RANGE:
        midpoint, _ = _split_range(start, end)
        return deduplicate(
            [
                *fetch_detailed_report_adaptive(
                    session,
                    workspace_id,
                    start,
                    midpoint,
                    capability_probe=False,
                ),
                *fetch_detailed_report_adaptive(
                    session,
                    workspace_id,
                    midpoint,
                    end,
                    capability_probe=False,
                ),
            ]
        )
    body = {
        "dateRangeStart": iso_z(start),
        "dateRangeEnd": iso_z(end),
        "detailedFilter": {
            "page": 1,
            "pageSize": REPORT_PAGE_SIZE,
            "sortColumn": "DATE",
            "sortOrder": "ASCENDING",
        },
        "exportType": "JSON",
    }
    try:
        result = session.fetch_post_collection(
            key,
            workspace_id,
            f"workspaces/{workspace_id}/reports/detailed",
            body=body,
            core=True,
            base="reports",
            item_keys=("timeentries", "timeEntries"),
            page_size=REPORT_PAGE_SIZE,
            raise_range_errors=True,
        )
    except HttpFailure as exc:
        if exc.status not in RANGE_ERROR_CODES or end - start <= timedelta(days=1):
            session._record_gap(
                key,
                exc,
                core=True,
                recommendation="Export this interval manually from Clockify Detailed Report.",
            )
            return []
        midpoint, _ = _split_range(start, end)
        return deduplicate(
            [
                *fetch_detailed_report_adaptive(
                    session,
                    workspace_id,
                    start,
                    midpoint,
                    capability_probe=False,
                ),
                *fetch_detailed_report_adaptive(
                    session,
                    workspace_id,
                    midpoint,
                    end,
                    capability_probe=False,
                ),
            ]
        )

    csv_key = f"{workspace_id}/detailed-report-csv/{label}"
    if session.manifest["datasets"].get(csv_key, {}).get("status") != "complete":
        csv_body = dict(body)
        csv_body["exportType"] = "CSV"
        try:
            response = session.client.request(
                "POST",
                f"workspaces/{workspace_id}/reports/detailed",
                base="reports",
                body=csv_body,
                accept="*/*",
            )
            extension = ".zip" if response.body.startswith(b"PK") else ".csv"
            path = session._store_response(
                workspace_id, csv_key, 1, response, extension=extension
            )
            session._complete_dataset(
                csv_key, pages=1, items=len(result), files=[path], core=False
            )
        except (BackupFailure, HttpFailure) as exc:
            session._record_gap(
                csv_key,
                exc,
                core=False,
                recommendation="Use the Clockify UI to export Detailed Report as CSV/XLSX.",
            )
    return result


def fetch_entity_changes_adaptive(
    session: BackupSession,
    workspace_id: str,
    change_kind: str,
    start: datetime,
    end: datetime,
    *,
    namespace: str = "entity-changes",
    capability_probe: bool = True,
) -> list[dict[str, Any]]:
    if capability_probe and end - start > timedelta(days=1):
        probe_start = max(start, end - timedelta(days=1))
        probe_rows = fetch_entity_changes_adaptive(
            session,
            workspace_id,
            change_kind,
            probe_start,
            end,
            namespace=namespace,
            capability_probe=False,
        )
        probe_key = (
            f"{workspace_id}/{namespace}/{change_kind}/"
            f"{_range_label(probe_start, end)}"
        )
        if session.manifest["datasets"].get(probe_key, {}).get("status") != "complete":
            return probe_rows
        return deduplicate(
            [
                *fetch_entity_changes_adaptive(
                    session,
                    workspace_id,
                    change_kind,
                    start,
                    end,
                    namespace=namespace,
                    capability_probe=False,
                ),
                *probe_rows,
            ]
        )
    label = _range_label(start, end)
    key = f"{workspace_id}/{namespace}/{change_kind}/{label}"
    completed = session.load_completed_items(key)
    if completed is not None:
        return deduplicate(completed)
    if end - start > MAX_CHANGE_RANGE:
        midpoint, _ = _split_range(start, end)
        return deduplicate(
            [
                *fetch_entity_changes_adaptive(
                    session,
                    workspace_id,
                    change_kind,
                    start,
                    midpoint,
                    namespace=namespace,
                    capability_probe=False,
                ),
                *fetch_entity_changes_adaptive(
                    session,
                    workspace_id,
                    change_kind,
                    midpoint,
                    end,
                    namespace=namespace,
                    capability_probe=False,
                ),
            ]
        )
    try:
        return session.fetch_collection(
            key,
            workspace_id,
            f"workspaces/{workspace_id}/entities/{change_kind}",
            core=False,
            params={
                "type": list(ENTITY_TYPES),
                "start": iso_z(start),
                "end": iso_z(end),
            },
            page_size=200,
            page_size_parameter="limit",
            start_page=0,
            raise_range_errors=True,
        )
    except HttpFailure as exc:
        if exc.status not in RANGE_ERROR_CODES or end - start <= timedelta(days=1):
            session._record_gap(
                key,
                exc,
                core=False,
                recommendation="The experimental Entity Changes endpoint is best effort; keep the full raw snapshot as the baseline.",
            )
            return []
        midpoint, _ = _split_range(start, end)
        return deduplicate(
            [
                *fetch_entity_changes_adaptive(
                    session,
                    workspace_id,
                    change_kind,
                    start,
                    midpoint,
                    namespace=namespace,
                    capability_probe=False,
                ),
                *fetch_entity_changes_adaptive(
                    session,
                    workspace_id,
                    change_kind,
                    midpoint,
                    end,
                    namespace=namespace,
                    capability_probe=False,
                ),
            ]
        )


def fetch_audit_log_adaptive(
    session: BackupSession,
    workspace_id: str,
    start: datetime,
    end: datetime,
    *,
    capability_probe: bool = True,
) -> list[dict[str, Any]]:
    if capability_probe and end - start > timedelta(days=1):
        probe_start = max(start, end - timedelta(days=1))
        probe_rows = fetch_audit_log_adaptive(
            session,
            workspace_id,
            probe_start,
            end,
            capability_probe=False,
        )
        probe_key = f"{workspace_id}/audit-log/{_range_label(probe_start, end)}"
        if session.manifest["datasets"].get(probe_key, {}).get("status") != "complete":
            return probe_rows
        return deduplicate(
            [
                *fetch_audit_log_adaptive(
                    session,
                    workspace_id,
                    start,
                    end,
                    capability_probe=False,
                ),
                *probe_rows,
            ]
        )
    label = _range_label(start, end)
    key = f"{workspace_id}/audit-log/{label}"
    completed = session.load_completed_items(
        key, item_keys=("response", "auditLog", "items", "data")
    )
    if completed is not None:
        return deduplicate(completed)
    if end - start > MAX_CHANGE_RANGE:
        midpoint, _ = _split_range(start, end)
        return deduplicate(
            [
                *fetch_audit_log_adaptive(
                    session,
                    workspace_id,
                    start,
                    midpoint,
                    capability_probe=False,
                ),
                *fetch_audit_log_adaptive(
                    session,
                    workspace_id,
                    midpoint,
                    end,
                    capability_probe=False,
                ),
            ]
        )
    try:
        return session.fetch_post_collection(
            key,
            workspace_id,
            f"workspaces/{workspace_id}/audit-log",
            body={
                "start": iso_z(start),
                "end": iso_z(end),
                "authors": None,
                "actions": list(AUDIT_ACTIONS),
            },
            core=False,
            base="audit",
            item_keys=("response", "auditLog", "items", "data"),
            page_size=50,
            page_parameter="page",
            page_size_parameter="page-size",
            raise_range_errors=True,
        )
    except HttpFailure as exc:
        if exc.status not in RANGE_ERROR_CODES or end - start <= timedelta(days=1):
            session._record_gap(
                key,
                exc,
                core=False,
                recommendation="Export this audit-log interval manually if your Clockify plan provides it.",
            )
            return []
        midpoint, _ = _split_range(start, end)
        return deduplicate(
            [
                *fetch_audit_log_adaptive(
                    session,
                    workspace_id,
                    start,
                    midpoint,
                    capability_probe=False,
                ),
                *fetch_audit_log_adaptive(
                    session,
                    workspace_id,
                    midpoint,
                    end,
                    capability_probe=False,
                ),
            ]
        )


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    content = "".join(
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )
    atomic_write(path, content.encode("utf-8"))


def _parse_api_datetime(value: Any) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal_duration(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "0.000000"
    return f"{(end - start).total_seconds() / 3600:.6f}"


def normalize_workspace(
    session: BackupSession,
    workspace_id: str,
    users: Sequence[Mapping[str, Any]],
    projects: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
    tags: Sequence[Mapping[str, Any]],
    custom_fields: Sequence[Mapping[str, Any]],
    time_entries: Sequence[Mapping[str, Any]],
    entities: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    target = session.run_dir / "normalized" / safe_segment(workspace_id)
    collections = {
        "users": users,
        "projects": projects,
        "tasks": tasks,
        "tags": tags,
        "custom-fields": custom_fields,
        **entities,
    }
    for name, rows in collections.items():
        write_jsonl(target / f"{name}.jsonl", deduplicate(rows))
    entries = deduplicate(time_entries)
    write_jsonl(target / "time-entries.jsonl", entries)

    users_by_id = {object_id(item): item for item in users if object_id(item)}
    projects_by_id = {object_id(item): item for item in projects if object_id(item)}
    tasks_by_id = {object_id(item): item for item in tasks if object_id(item)}
    tags_by_id = {object_id(item): item for item in tags if object_id(item)}
    csv_path = target / "time-entries.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = csv_path.with_name(csv_path.name + ".part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for item in entries:
            interval = item.get("timeInterval")
            interval = interval if isinstance(interval, Mapping) else {}
            start = _parse_api_datetime(interval.get("start"))
            end = _parse_api_datetime(interval.get("end"))
            if start is None or end is None:
                continue
            user_id = clean(item.get("userId"))
            project_id = clean(item.get("projectId"))
            task_id = clean(item.get("taskId"))
            user = users_by_id.get(user_id, {})
            project = projects_by_id.get(project_id, {})
            task = tasks_by_id.get(task_id, {})
            tag_names = [
                clean(tags_by_id.get(clean(tag_id), {}).get("name")) or clean(tag_id)
                for tag_id in item.get("tagIds", [])
                if clean(tag_id)
            ]
            writer.writerow(
                {
                    "Project": clean(project.get("name")),
                    "Task": clean(task.get("name")),
                    "User": clean(user.get("name")),
                    "Email": clean(user.get("email")),
                    "Start Date": start.date().isoformat(),
                    "Start Time": start.time().isoformat(),
                    "End Date": end.date().isoformat(),
                    "End Time": end.time().isoformat(),
                    "Duration (decimal)": _decimal_duration(start, end),
                    "Description": clean(item.get("description")),
                    "Billable": "Yes" if bool(item.get("billable")) else "No",
                    "Tags": "; ".join(tag_names),
                    "Clockify Entry ID": object_id(item),
                    "Clockify User ID": user_id,
                    "Clockify Project ID": project_id,
                    "Clockify Task ID": task_id,
                    "Start UTC": iso_z(start),
                    "End UTC": iso_z(end),
                    "Type": clean(item.get("type")),
                    "Locked": str(bool(item.get("isLocked"))).lower(),
                    "Hourly Rate": json.dumps(item.get("hourlyRate"), ensure_ascii=False),
                    "Cost Rate": json.dumps(item.get("costRate"), ensure_ascii=False),
                    "Custom Fields JSON": json.dumps(
                        item.get("customFieldValues"), ensure_ascii=False, sort_keys=True
                    ),
                }
            )
    replace_with_retry(temporary, csv_path)


def _asset_extension(content_type: str, fallback: str) -> str:
    lowered = content_type.casefold()
    if "pdf" in lowered:
        return ".pdf"
    if "png" in lowered:
        return ".png"
    if "jpeg" in lowered or "jpg" in lowered:
        return ".jpg"
    return fallback


def download_assets(
    session: BackupSession,
    workspace_id: str,
    expenses: Sequence[Mapping[str, Any]],
    invoices: Sequence[Mapping[str, Any]],
) -> None:
    for expense in expenses:
        expense_id = object_id(expense)
        file_id = clean(expense.get("fileId"))
        if not expense_id or not file_id:
            continue
        identity = f"receipt:{workspace_id}:{expense_id}:{file_id}"
        session.manifest["assets_expected"].append(identity)
        try:
            response = session.client.request(
                "GET", f"workspaces/{workspace_id}/expenses/{expense_id}/files/{file_id}"
            )
            extension = _asset_extension(response.content_type, ".bin")
            path = (
                session.run_dir
                / "assets"
                / "receipts"
                / safe_segment(workspace_id)
                / f"{safe_segment(expense_id)}-{safe_segment(file_id)}{extension}"
            )
            atomic_write(path, response.body)
            session.manifest["assets_downloaded"].append(identity)
        except (BackupFailure, HttpFailure) as exc:
            gap = {"asset": identity, "reason": str(exc)}
            session.manifest["asset_gaps"].append(gap)
            append_jsonl(session.gaps_log, {"timestamp": iso_z(utc_now()), **gap})
    for invoice in invoices:
        invoice_id = object_id(invoice)
        if not invoice_id:
            continue
        identity = f"invoice:{workspace_id}:{invoice_id}"
        session.manifest["assets_expected"].append(identity)
        try:
            response = session.client.request(
                "GET",
                f"workspaces/{workspace_id}/invoices/{invoice_id}/export",
                params={"userLocale": "en"},
                accept="application/pdf,application/octet-stream",
            )
            extension = _asset_extension(response.content_type, ".pdf")
            path = (
                session.run_dir
                / "assets"
                / "invoices"
                / safe_segment(workspace_id)
                / f"{safe_segment(invoice_id)}{extension}"
            )
            atomic_write(path, response.body)
            session.manifest["assets_downloaded"].append(identity)
        except (BackupFailure, HttpFailure) as exc:
            gap = {"asset": identity, "reason": str(exc)}
            session.manifest["asset_gaps"].append(gap)
            append_jsonl(session.gaps_log, {"timestamp": iso_z(utc_now()), **gap})
    session.save_manifest()


def backup_workspace(
    session: BackupSession, workspace: Mapping[str, Any], cutoff: datetime
) -> None:
    workspace_id = object_id(workspace)
    if not workspace_id:
        raise BackupFailure("Clockify returned a workspace without an ID.")
    session.manifest["workspaces"].setdefault(
        workspace_id,
        {"name": clean(workspace.get("name")), "status": "RUNNING"},
    )
    session.save_manifest()
    prefix = workspace_id
    session.fetch_json(
        f"{prefix}/workspace-info",
        workspace_id,
        f"workspaces/{workspace_id}",
        core=True,
    )
    users = session.fetch_collection(
        f"{prefix}/users",
        workspace_id,
        f"workspaces/{workspace_id}/users",
        core=True,
        params={"status": "ALL", "includeRoles": "true", "memberships": "ALL"},
    )
    detailed_users = session.fetch_post_collection(
        f"{prefix}/users-info",
        workspace_id,
        f"workspaces/{workspace_id}/users/info",
        body={
            "status": "ALL",
            "includeRoles": True,
            "memberships": "ALL",
            "sortColumn": "ID",
            "sortOrder": "ASCENDING",
        },
        core=False,
        base="api",
        item_keys=("users",),
        page_size=200,
    )
    users_by_id = {
        object_id(item): item for item in detailed_users if object_id(item)
    }
    for user in users:
        detail = users_by_id.pop(object_id(user), None)
        if detail:
            user.update(detail)
    users = deduplicate([*users, *users_by_id.values()])
    clients = deduplicate(
        [
            *session.fetch_collection(
                f"{prefix}/clients-active",
                workspace_id,
                f"workspaces/{workspace_id}/clients",
                core=True,
                params={"archived": "false"},
            ),
            *session.fetch_collection(
                f"{prefix}/clients-archived",
                workspace_id,
                f"workspaces/{workspace_id}/clients",
                core=True,
                params={"archived": "true"},
            ),
        ]
    )
    projects = deduplicate(
        [
            *session.fetch_collection(
                f"{prefix}/projects-active",
                workspace_id,
                f"workspaces/{workspace_id}/projects",
                core=True,
                params={"archived": "false"},
            ),
            *session.fetch_collection(
                f"{prefix}/projects-archived",
                workspace_id,
                f"workspaces/{workspace_id}/projects",
                core=True,
                params={"archived": "true"},
            ),
        ]
    )
    tags = deduplicate(
        [
            *session.fetch_collection(
                f"{prefix}/tags-active",
                workspace_id,
                f"workspaces/{workspace_id}/tags",
                core=True,
                params={"archived": "false"},
            ),
            *session.fetch_collection(
                f"{prefix}/tags-archived",
                workspace_id,
                f"workspaces/{workspace_id}/tags",
                core=True,
                params={"archived": "true"},
            ),
        ]
    )
    custom_fields: list[dict[str, Any]] = []
    for status in ("VISIBLE", "INVISIBLE", "INACTIVE"):
        custom_fields.extend(
            session.fetch_collection(
                f"{prefix}/custom-fields-{status.casefold()}",
                workspace_id,
                f"workspaces/{workspace_id}/custom-fields",
                core=True,
                params={"status": status},
            )
        )
    custom_fields = deduplicate(custom_fields)

    tasks: list[dict[str, Any]] = []
    for project in projects:
        project_id = object_id(project)
        if not project_id:
            continue
        session.fetch_json(
            f"{prefix}/project-details/{safe_segment(project_id)}",
            workspace_id,
            f"workspaces/{workspace_id}/projects/{project_id}",
            core=True,
        )
        tasks.extend(
            session.fetch_collection(
                f"{prefix}/project-tasks/{safe_segment(project_id)}",
                workspace_id,
                f"workspaces/{workspace_id}/projects/{project_id}/tasks",
                core=True,
            )
        )
        project_custom_fields = session.fetch_json(
            f"{prefix}/project-custom-fields/{safe_segment(project_id)}",
            workspace_id,
            f"workspaces/{workspace_id}/projects/{project_id}/custom-fields",
            core=False,
        )
        if project_custom_fields is not None:
            custom_fields.extend(extract_items(project_custom_fields))
    tasks = deduplicate(tasks)
    custom_fields = deduplicate(custom_fields)

    all_time_entries: list[dict[str, Any]] = []
    for user in users:
        user_id = object_id(user)
        if not user_id:
            continue
        all_time_entries.extend(
            fetch_time_entries_adaptive(
                session, workspace_id, user_id, EARLIEST, cutoff
            )
        )
    all_time_entries.extend(
        session.fetch_collection(
            f"{prefix}/in-progress-time-entries",
            workspace_id,
            f"workspaces/{workspace_id}/time-entries/status/in-progress",
            core=True,
            page_size=1000,
        )
    )
    all_time_entries = deduplicate(all_time_entries)
    detailed_report_entries = fetch_detailed_report_adaptive(
        session, workspace_id, EARLIEST, cutoff
    )

    groups = session.fetch_collection(
        f"{prefix}/user-groups",
        workspace_id,
        f"workspaces/{workspace_id}/user-groups",
        core=False,
        params={"includeTeamManagers": "true"},
    )
    expense_categories = deduplicate(
        [
            *session.fetch_collection(
                f"{prefix}/expense-categories-active",
                workspace_id,
                f"workspaces/{workspace_id}/expenses/categories",
                core=False,
                params={"archived": "false"},
            ),
            *session.fetch_collection(
                f"{prefix}/expense-categories-archived",
                workspace_id,
                f"workspaces/{workspace_id}/expenses/categories",
                core=False,
                params={"archived": "true"},
            ),
        ]
    )
    expenses = session.fetch_collection(
        f"{prefix}/expenses",
        workspace_id,
        f"workspaces/{workspace_id}/expenses",
        core=False,
        item_keys=("expenses",),
    )
    for expense in expenses:
        expense_id = object_id(expense)
        if expense_id:
            detail = session.fetch_json(
                f"{prefix}/expense-details/{safe_segment(expense_id)}",
                workspace_id,
                f"workspaces/{workspace_id}/expenses/{expense_id}",
                core=False,
            )
            if isinstance(detail, Mapping):
                expense.update(detail)
    invoices = session.fetch_collection(
        f"{prefix}/invoices",
        workspace_id,
        f"workspaces/{workspace_id}/invoices",
        core=False,
        item_keys=("invoices",),
        page_size=50,
    )
    session.fetch_json(
        f"{prefix}/invoice-settings",
        workspace_id,
        f"workspaces/{workspace_id}/invoices/settings",
        core=False,
    )
    for invoice in invoices:
        invoice_id = object_id(invoice)
        if not invoice_id:
            continue
        detail = session.fetch_json(
            f"{prefix}/invoice-details/{safe_segment(invoice_id)}",
            workspace_id,
            f"workspaces/{workspace_id}/invoices/{invoice_id}",
            core=False,
        )
        if isinstance(detail, Mapping):
            invoice.update(detail)
        session.fetch_collection(
            f"{prefix}/invoice-payments/{safe_segment(invoice_id)}",
            workspace_id,
            f"workspaces/{workspace_id}/invoices/{invoice_id}/payments",
            core=False,
        )
    approvals = session.fetch_collection(
        f"{prefix}/approval-requests",
        workspace_id,
        f"workspaces/{workspace_id}/approval-requests",
        core=False,
    )
    holidays_data = session.fetch_json(
        f"{prefix}/holidays",
        workspace_id,
        f"workspaces/{workspace_id}/holidays",
        core=False,
    )
    holidays = extract_items(holidays_data) if holidays_data is not None else []
    policies_data = session.fetch_json(
        f"{prefix}/time-off-policies",
        workspace_id,
        f"workspaces/{workspace_id}/time-off/policies",
        core=False,
    )
    policies = extract_items(policies_data) if policies_data is not None else []
    for policy in policies:
        policy_id = object_id(policy)
        if policy_id:
            session.fetch_json(
                f"{prefix}/time-off-policy-balances/{safe_segment(policy_id)}",
                workspace_id,
                f"workspaces/{workspace_id}/time-off/balance/policy/{policy_id}",
                core=False,
            )
    for user in users:
        user_id = object_id(user)
        if user_id:
            session.fetch_json(
                f"{prefix}/time-off-user-balances/{safe_segment(user_id)}",
                workspace_id,
                f"workspaces/{workspace_id}/time-off/balance/user/{user_id}",
                core=False,
            )
    time_off_requests = session.fetch_post_collection(
        f"{prefix}/time-off-requests",
        workspace_id,
        f"workspaces/{workspace_id}/time-off/requests",
        body={
            "start": iso_z(EARLIEST),
            "end": iso_z(cutoff),
            "statuses": ["ALL"],
        },
        core=False,
        base="api",
        item_keys=("requests",),
    )
    scheduling = session.fetch_collection(
        f"{prefix}/scheduling-assignments",
        workspace_id,
        f"workspaces/{workspace_id}/scheduling/assignments/all",
        core=False,
        params={"start": iso_z(EARLIEST), "end": iso_z(cutoff)},
    )
    shared_reports = session.fetch_collection(
        f"{prefix}/shared-reports",
        workspace_id,
        f"workspaces/{workspace_id}/shared-reports",
        core=False,
        base="reports",
        page_parameter="page",
        page_size_parameter="pageSize",
    )
    for shared_report in shared_reports:
        shared_report_id = object_id(shared_report)
        if not shared_report_id:
            continue
        detail = session.fetch_json(
            f"{prefix}/shared-report-details/{safe_segment(shared_report_id)}",
            workspace_id,
            f"shared-reports/{shared_report_id}",
            core=False,
            base="reports",
        )
        if isinstance(detail, Mapping):
            shared_report.update(detail)
    templates = session.fetch_collection(
        f"{prefix}/templates",
        workspace_id,
        f"workspaces/{workspace_id}/templates",
        core=False,
        params={"hydrated": "true", "cleansed": "false"},
    )
    webhooks_data = session.fetch_json(
        f"{prefix}/webhooks-redacted",
        workspace_id,
        f"workspaces/{workspace_id}/webhooks",
        core=False,
        redact=True,
    )
    webhooks = extract_items(webhooks_data, ("webhooks",)) if webhooks_data else []
    audit_log = fetch_audit_log_adaptive(
        session, workspace_id, EARLIEST, cutoff
    )
    entity_changes: list[dict[str, Any]] = []
    for change_kind in ("created", "updated", "deleted"):
        entity_changes.extend(
            fetch_entity_changes_adaptive(
                session, workspace_id, change_kind, EARLIEST, cutoff
            )
        )
    download_assets(session, workspace_id, expenses, invoices)
    normalize_workspace(
        session,
        workspace_id,
        users,
        projects,
        tasks,
        tags,
        custom_fields,
        all_time_entries,
        {
            "clients": clients,
            "user-groups": groups,
            "expense-categories": expense_categories,
            "expenses": expenses,
            "invoices": invoices,
            "approval-requests": approvals,
            "holidays": holidays,
            "time-off-policies": policies,
            "time-off-requests": time_off_requests,
            "scheduling-assignments": scheduling,
            "shared-reports": shared_reports,
            "templates": templates,
            "webhooks-redacted": webhooks,
            "detailed-report-time-entries": detailed_report_entries,
            "audit-log": audit_log,
            "entity-changes": entity_changes,
        },
    )
    session.manifest["workspaces"][workspace_id].update(
        {
            "status": "complete",
            "users": len(users),
            "projects": len(projects),
            "tasks": len(tasks),
            "time_entries": len(all_time_entries),
        }
    )
    session.save_manifest()


def closing_delta(session: BackupSession, cutoff: datetime) -> None:
    delta_end = utc_now()
    for workspace_id, workspace_record in session.manifest["workspaces"].items():
        users = session.load_completed_items(f"{workspace_id}/users") or []
        for user in users:
            user_id = object_id(user)
            if user_id:
                fetch_time_entries_adaptive(
                    session,
                    workspace_id,
                    user_id,
                    cutoff,
                    delta_end,
                    namespace="closing-delta",
                )
        for change_kind in ("created", "updated", "deleted"):
            fetch_entity_changes_adaptive(
                session,
                workspace_id,
                change_kind,
                cutoff,
                delta_end,
                namespace="closing-delta-entity-changes",
            )
        workspace_record["closing_delta_end_utc"] = iso_z(delta_end)
    session.save_manifest()


def internal_verification(run_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    discovered = set(manifest.get("discovered_workspace_ids", []))
    selected = set(manifest.get("selected_workspace_ids", [])) or discovered
    recorded = set(manifest.get("workspaces", {}))
    if not selected.issubset(discovered):
        errors.append(
            f"Selected workspace IDs were not discovered: {sorted(selected - discovered)}"
        )
    if selected != recorded:
        errors.append(
            f"Workspace manifest mismatch: selected={sorted(selected)}, recorded={sorted(recorded)}"
        )
    incomplete = [
        key
        for key, value in manifest.get("datasets", {}).items()
        if value.get("core") and value.get("status") != "complete"
    ]
    if incomplete:
        errors.append("Incomplete core datasets: " + ", ".join(sorted(incomplete)))
    core_gaps = [
        str(gap.get("dataset"))
        for gap in manifest.get("gaps", [])
        if gap.get("core") and gap.get("status") != "not_enabled"
    ]
    if core_gaps:
        errors.append("Core backup gaps: " + ", ".join(sorted(set(core_gaps))))
    duplicate_ids: list[str] = []
    for workspace_id in recorded:
        normalized_dir = run_dir / "normalized" / safe_segment(workspace_id)
        path = normalized_dir / "time-entries.jsonl"
        seen: set[str] = set()
        rest_entries: list[dict[str, Any]] = []
        if not path.is_file():
            errors.append(f"Missing normalized time-entry JSONL for {workspace_id}")
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                errors.append(f"Non-object time-entry JSONL row for {workspace_id}")
                continue
            rest_entries.append(value)
            identifier = object_id(value)
            if identifier and identifier in seen:
                duplicate_ids.append(identifier)
            seen.add(identifier)
        csv_path = path.with_suffix(".csv")
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                missing = REQUIRED_CLOCKIFY_COLUMNS - set(reader.fieldnames or [])
                has_rows = next(reader, None) is not None
            if missing:
                errors.append(
                    f"Normalized CSV is missing importer columns in {csv_path}: "
                    + ", ".join(sorted(missing))
                )
            elif has_rows:
                read_clockify_entries(csv_path)
        except Exception as exc:  # importer emits several domain-specific errors
            errors.append(f"Offline importer rejected {csv_path}: {exc}")

        entity_ids: dict[str, set[str]] = {}
        for entity_name in ("users", "projects", "tasks", "tags", "custom-fields"):
            entity_path = normalized_dir / f"{entity_name}.jsonl"
            ids: set[str] = set()
            if entity_path.is_file():
                for line in entity_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        entity = json.loads(line)
                        if isinstance(entity, Mapping) and object_id(entity):
                            ids.add(object_id(entity))
            entity_ids[entity_name] = ids
        orphan_counts = {name: 0 for name in entity_ids}
        for entry in rest_entries:
            references = {
                "users": [clean(entry.get("userId"))],
                "projects": [clean(entry.get("projectId"))],
                "tasks": [clean(entry.get("taskId"))],
                "tags": [clean(value) for value in entry.get("tagIds", [])],
                "custom-fields": [
                    clean(value.get("customFieldId") or value.get("id"))
                    for value in entry.get("customFieldValues", [])
                    if isinstance(value, Mapping)
                ],
            }
            for name, identifiers in references.items():
                orphan_counts[name] += sum(
                    1
                    for identifier in identifiers
                    if identifier and identifier not in entity_ids[name]
                )
        for name, count in orphan_counts.items():
            if count:
                errors.append(
                    f"Orphaned {name} references in {workspace_id} time entries: {count}"
                )

        report_entries: list[dict[str, Any]] = []
        report_prefix = f"{workspace_id}/detailed-report-json/"
        for key, dataset in manifest.get("datasets", {}).items():
            if not key.startswith(report_prefix) or dataset.get("status") != "complete":
                continue
            for relative in dataset.get("files", []):
                report_path = run_dir / relative
                if report_path.suffix.casefold() != ".json":
                    continue
                try:
                    report_data = json.loads(report_path.read_text(encoding="utf-8"))
                    report_entries.extend(
                        extract_items(report_data, ("timeentries", "timeEntries"))
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    errors.append(f"Unreadable Detailed Report page {relative}: {exc}")
        completed_rest = [
            entry
            for entry in rest_entries
            if isinstance(entry.get("timeInterval"), Mapping)
            and clean(entry["timeInterval"].get("end"))
        ]
        rest_ids = {object_id(entry) for entry in completed_rest if object_id(entry)}
        report_ids = {object_id(entry) for entry in report_entries if object_id(entry)}
        if rest_ids and not report_ids:
            errors.append(
                f"Detailed Report reconciliation found no entry IDs for {workspace_id}, "
                f"while REST contains {len(rest_ids)} completed entries."
            )
        elif rest_ids:
            missing_from_report = rest_ids - report_ids
            if missing_from_report:
                errors.append(
                    f"Detailed Report is missing {len(missing_from_report)} REST entry IDs for {workspace_id}."
                )
            report_only = report_ids - rest_ids
            if report_only:
                warnings.append(
                    f"Detailed Report contains {len(report_only)} additional entry IDs for {workspace_id}; "
                    "these may belong to former users and are preserved in the raw report."
                )
            rest_by_id = {object_id(entry): entry for entry in completed_rest if object_id(entry)}
            report_by_id = {object_id(entry): entry for entry in report_entries if object_id(entry)}
            duration_mismatches = 0
            for identifier in rest_ids & report_ids:
                rest_interval = rest_by_id[identifier].get("timeInterval")
                report_interval = report_by_id[identifier].get("timeInterval")
                if not isinstance(rest_interval, Mapping) or not isinstance(report_interval, Mapping):
                    continue
                rest_start = _parse_api_datetime(rest_interval.get("start"))
                rest_end = _parse_api_datetime(rest_interval.get("end"))
                report_start = _parse_api_datetime(report_interval.get("start"))
                report_end = _parse_api_datetime(report_interval.get("end"))
                if None not in (rest_start, rest_end, report_start, report_end):
                    rest_seconds = int((rest_end - rest_start).total_seconds())
                    report_seconds = int((report_end - report_start).total_seconds())
                    if rest_seconds != report_seconds:
                        duration_mismatches += 1
            if duration_mismatches:
                errors.append(
                    f"Detailed Report duration mismatches for {workspace_id}: {duration_mismatches}"
                )
    if duplicate_ids:
        errors.append("Duplicate time-entry IDs: " + ", ".join(sorted(set(duplicate_ids))))
    expected = set(manifest.get("assets_expected", []))
    downloaded = set(manifest.get("assets_downloaded", []))
    explained = {item.get("asset") for item in manifest.get("asset_gaps", [])}
    unexplained = expected - downloaded - explained
    if unexplained:
        errors.append("Unexplained missing assets: " + ", ".join(sorted(unexplained)))
    if manifest.get("asset_gaps"):
        warnings.append(f"Asset download gaps: {len(manifest['asset_gaps'])}")
    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at_utc": iso_z(utc_now()),
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "warnings": warnings,
        "archive_verification": "Performed by verify-clockify-backup after archive creation.",
    }


def write_checksums(run_dir: Path) -> Path:
    checksum_path = run_dir / "checksums.sha256"
    rows: list[str] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path == checksum_path or path.name.endswith(".part"):
            continue
        rows.append(f"{sha256_file(path)}  {path.relative_to(run_dir).as_posix()}")
    atomic_write(checksum_path, ("\n".join(rows) + "\n").encode("utf-8"))
    return checksum_path


def create_archive(run_dir: Path) -> tuple[Path, Path]:
    archive_path = run_dir.parent / f"{run_dir.name}.zip"
    temporary = archive_path.with_name(archive_path.name + ".part")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(run_dir.rglob("*")):
            if path.is_file() and not path.name.endswith(".part"):
                archive.write(path, path.relative_to(run_dir).as_posix())
    replace_with_retry(temporary, archive_path)
    hash_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    atomic_write(
        hash_path,
        f"{sha256_file(archive_path)}  {archive_path.name}\n".encode("utf-8"),
    )
    return archive_path, hash_path


def finalize_backup(session: BackupSession) -> int:
    session.manifest["finished_at_utc"] = iso_z(utc_now())
    verification = internal_verification(session.run_dir, session.manifest)
    has_core_failure = verification["status"] != "PASS"
    has_permission_gap = any(
        gap.get("status") == "permission_denied" for gap in session.manifest["gaps"]
    )
    has_asset_gaps = bool(session.manifest.get("asset_gaps"))
    session.manifest["status"] = (
        "PARTIAL" if has_core_failure or has_permission_gap or has_asset_gaps else "COMPLETE"
    )
    session.save_manifest()
    verification = internal_verification(session.run_dir, session.manifest)
    atomic_json(session.run_dir / "verification.json", verification)
    write_checksums(session.run_dir)
    archive_path, hash_path = create_archive(session.run_dir)
    print(f"Backup status: {session.manifest['status']}")
    print(session.run_dir.resolve())
    print(archive_path.resolve())
    print(hash_path.resolve())
    print("READ-ONLY: no Clockify or Kimai data was changed.")
    return 0 if session.manifest["status"] == "COMPLETE" else 3


def verify_checksums(run_dir: Path) -> list[str]:
    errors: list[str] = []
    checksum_path = run_dir / "checksums.sha256"
    if not checksum_path.is_file():
        return ["checksums.sha256 is missing"]
    for line_number, line in enumerate(
        checksum_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            expected, relative = line.split("  ", 1)
        except ValueError:
            errors.append(f"Invalid checksum line {line_number}")
            continue
        path = run_dir / Path(relative)
        if not path.is_file():
            errors.append(f"Missing checksummed file: {relative}")
        elif sha256_file(path) != expected:
            errors.append(f"Checksum mismatch: {relative}")
    return errors


def verify_archive(run_dir: Path) -> list[str]:
    errors: list[str] = []
    archive_path = run_dir.parent / f"{run_dir.name}.zip"
    hash_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    if not archive_path.is_file():
        return [f"Archive is missing: {archive_path}"]
    try:
        with zipfile.ZipFile(archive_path) as archive:
            corrupt = archive.testzip()
            if corrupt:
                errors.append(f"ZIP member is corrupt: {corrupt}")
    except zipfile.BadZipFile as exc:
        errors.append(f"Invalid ZIP archive: {exc}")
    if not hash_path.is_file():
        errors.append(f"Archive checksum is missing: {hash_path}")
    else:
        expected = hash_path.read_text(encoding="utf-8").split()[0]
        if sha256_file(archive_path) != expected:
            errors.append("ZIP SHA-256 mismatch")
    return errors


def verify_backup(run_dir: Path) -> tuple[bool, dict[str, Any]]:
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupFailure(f"Invalid or missing manifest in {run_dir}") from exc
    result = internal_verification(run_dir, manifest)
    result["errors"].extend(verify_checksums(run_dir))
    result["errors"].extend(verify_archive(run_dir))
    result["status"] = "PASS" if not result["errors"] else "FAIL"
    return result["status"] == "PASS", result


def load_clockify_key(env_path: Path) -> str:
    load_dotenv(env_path)
    key = os.environ.get("CLOCKIFY_API_KEY", "").strip()
    if not key:
        raise BackupFailure(
            f"CLOCKIFY_API_KEY is missing. Add it to {env_path} or set the environment variable."
        )
    if any(character.isspace() for character in key):
        raise BackupFailure("CLOCKIFY_API_KEY contains whitespace.")
    return key


def workspace_needs_snapshot(
    manifest: Mapping[str, Any], workspace_id: str, *, resume: bool
) -> bool:
    if not resume:
        return True
    workspace = manifest.get("workspaces", {}).get(workspace_id, {})
    return workspace.get("status") != "complete"


def build_backup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a complete, read-only Clockify emergency backup."
    )
    parser.add_argument("--workspace-id", action="append", default=[])
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--cutoff", type=parse_utc)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--request-delay", type=float, default=0.0)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--clockify-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--reports-base-url", default=DEFAULT_REPORTS_BASE_URL)
    parser.add_argument("--audit-base-url", default=DEFAULT_AUDIT_BASE_URL)
    return parser


def build_verify_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a Clockify emergency backup completely offline."
    )
    parser.add_argument("backup_directory", type=Path)
    return parser


def backup_main(argv: Sequence[str] | None = None) -> int:
    args = build_backup_parser().parse_args(argv)
    if args.timeout <= 0 or args.request_delay < 0:
        print("ERROR: --timeout must be positive and --request-delay non-negative.", file=sys.stderr)
        return 2
    session: BackupSession | None = None
    try:
        if args.resume:
            run_dir = args.resume.resolve()
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            cutoff = parse_utc(str(manifest["cutoff_utc"]))
        else:
            cutoff = args.cutoff or utc_now()
            run_id = cutoff.strftime("%Y%m%dT%H%M%SZ")
            run_dir = (args.output_root / run_id).resolve()
            suffix = 1
            while run_dir.exists():
                run_dir = (args.output_root / f"{run_id}-{suffix}").resolve()
                suffix += 1
        key = load_clockify_key(args.env_file)
        client = ReadOnlyClockifyClient(
            key,
            api_base_url=args.clockify_base_url,
            reports_base_url=args.reports_base_url,
            audit_base_url=args.audit_base_url,
            timeout=args.timeout,
            request_delay=args.request_delay,
        )
        session = BackupSession(
            run_dir,
            client,
            cutoff=cutoff,
            requested_workspace_ids=args.workspace_id,
            resume=bool(args.resume),
        )
        client.event_sink = session.request_event
        current_user = session.fetch_json(
            "global/current-user", "_global", "user", core=True
        )
        workspaces_data = session.fetch_json(
            "global/workspaces", "_global", "workspaces", core=True
        )
        if current_user is None or not isinstance(workspaces_data, list):
            raise BackupFailure("Could not retrieve the core Clockify account data.")
        workspaces = [item for item in workspaces_data if isinstance(item, Mapping)]
        discovered_ids = [object_id(item) for item in workspaces if object_id(item)]
        session.manifest["discovered_workspace_ids"] = discovered_ids
        requested = set(session.manifest.get("requested_workspace_ids", []))
        selected = [
            item for item in workspaces if not requested or object_id(item) in requested
        ]
        missing = requested - {object_id(item) for item in selected}
        if missing:
            raise BackupFailure(
                "Requested workspace IDs are not accessible: " + ", ".join(sorted(missing))
            )
        session.manifest["selected_workspace_ids"] = [object_id(item) for item in selected]
        session.save_manifest()
        for workspace in selected:
            workspace_id = object_id(workspace)
            if not workspace_needs_snapshot(
                session.manifest, workspace_id, resume=bool(args.resume)
            ):
                continue
            backup_workspace(session, workspace, cutoff)
        closing_delta(session, cutoff)
        return finalize_backup(session)
    except AuthenticationFailure as exc:
        if session is not None:
            session.manifest["status"] = "FAILED_AUTH"
            session.manifest["finished_at_utc"] = iso_z(utc_now())
            session.save_manifest()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (BackupFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        if session is not None:
            gap = {
                "timestamp": iso_z(utc_now()),
                "dataset": "backup-run",
                "status": "failed",
                "core": True,
                "reason": str(exc),
                "recommendation": "Fix the reported issue and resume this backup directory.",
            }
            session.manifest["gaps"].append(gap)
            session.manifest["status"] = "PARTIAL"
            session.save_manifest()
            append_jsonl(session.gaps_log, gap)
            try:
                finalize_backup(session)
            except Exception:
                pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3 if session is not None else 2
    except KeyboardInterrupt:
        if session is not None:
            session.manifest["status"] = "INTERRUPTED"
            session.save_manifest()
        print("Interrupted. Resume with --resume and the backup directory.", file=sys.stderr)
        return 130


def verify_main(argv: Sequence[str] | None = None) -> int:
    args = build_verify_parser().parse_args(argv)
    try:
        success, result = verify_backup(args.backup_directory.resolve())
    except BackupFailure as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Offline verification: {result['status']}")
    for warning in result["warnings"]:
        print(f"WARNING: {warning}")
    for error in result["errors"]:
        print(f"ERROR: {error}")
    return 0 if success else 3
