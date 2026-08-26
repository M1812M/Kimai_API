#!/usr/bin/env python3
"""Create a read-only CSV and text list of all Kimai projects and activities."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Mapping

from .project_tasks import (
    DEFAULT_BASE_URL,
    DEFAULT_TOKEN_FILE,
    ImportFailure,
    KimaiApi,
    clean,
    entity_id,
    find_customer,
    load_token,
)


def customer_name(project: Mapping[str, Any]) -> str:
    customer = project.get("customer")
    if isinstance(customer, Mapping):
        return clean(customer.get("name"))
    return clean(customer)


def team_names(item: Mapping[str, Any]) -> str:
    teams = item.get("teams")
    if not isinstance(teams, list):
        return ""
    return "; ".join(clean(team.get("name")) for team in teams if isinstance(team, Mapping))


def activities_for_project(api: KimaiApi, project_id: int) -> list[dict[str, Any]]:
    activities = api.collection(
        "activities", {"project": project_id, "visible": 3, "globals": "false"}
    )
    return sorted(activities, key=lambda item: clean(item.get("name")).casefold())


def collect_catalog(
    api: KimaiApi, customer_filter: str | None = None
) -> tuple[str, list[dict[str, Any]], int]:
    version = api.get("version")
    version_text = clean(version.get("version")) if isinstance(version, Mapping) else "unknown"
    project_params: dict[str, Any] = {"visible": 3}
    filtered_customer_name = ""
    if customer_filter:
        customer = find_customer(api, customer_filter)
        project_params["customer"] = entity_id(customer, "customer")
        filtered_customer_name = clean(customer.get("name"))
    projects = api.collection("projects", project_params)
    projects.sort(
        key=lambda item: (
            customer_name(item).casefold(),
            clean(item.get("name")).casefold(),
        )
    )

    rows: list[dict[str, Any]] = []
    for project in projects:
        project_id = entity_id(project, "project")
        name = clean(project.get("name"))
        customer = customer_name(project) or filtered_customer_name
        activities = activities_for_project(api, project_id)
        if not activities:
            rows.append(
                {
                    "Customer": customer,
                    "Project": name,
                    "Project ID": project_id,
                    "Project visible": clean(project.get("visible")),
                    "Project teams": team_names(project),
                    "Activity": "",
                    "Activity ID": "",
                    "Activity visible": "",
                    "Activity teams": "",
                }
            )
            continue
        for activity in activities:
            activity_id = entity_id(activity, "activity")
            activity_name = clean(activity.get("name"))
            rows.append(
                {
                    "Customer": customer,
                    "Project": name,
                    "Project ID": project_id,
                    "Project visible": clean(project.get("visible")),
                    "Project teams": team_names(project),
                    "Activity": activity_name,
                    "Activity ID": activity_id,
                    "Activity visible": clean(activity.get("visible")),
                    "Activity teams": team_names(activity),
                }
            )
    return version_text, rows, len(projects)


def write_catalog(csv_path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0]) if rows else ["Customer", "Project", "Project ID"]
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def export(args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        raise ImportFailure("--timeout must be greater than zero.")

    token = load_token(Path(args.token_file) if args.token_file else None)
    api = KimaiApi(args.base_url, token, args.timeout)
    version_text, rows, project_count = collect_catalog(api, args.customer)

    csv_path = Path(args.output).resolve()
    text_path = csv_path.with_suffix(".txt")
    write_catalog(csv_path, rows)

    lines = [f"Kimai {version_text}: {project_count} projects", ""]
    current_project: tuple[str, str, str] | None = None
    for row in rows:
        project_key = (
            clean(row.get("Customer")),
            clean(row.get("Project")),
            clean(row.get("Project ID")),
        )
        if project_key != current_project:
            if current_project is not None:
                lines.append("")
            lines.append(f"{project_key[0]} | {project_key[1]} (Project ID {project_key[2]})")
            current_project = project_key
        activity = clean(row.get("Activity"))
        activity_id = clean(row.get("Activity ID"))
        lines.append(
            f"  - {activity} (Activity ID {activity_id})"
            if activity
            else "  - No project-specific activities"
        )

    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Read-only export completed from Kimai {version_text}.")
    activity_count = sum(1 for row in rows if row["Activity"])
    print(
        f"Projects: {project_count}; project-specific activities: {activity_count}"
    )
    print(f"CSV: {csv_path}")
    print(f"Text: {text_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export all Kimai projects and their project-specific activities."
    )
    parser.add_argument(
        "--output",
        default="kimai-project-activity-catalog.csv",
        help="CSV output file (default: kimai-project-activity-catalog.csv)",
    )
    parser.add_argument(
        "--customer",
        help="Optional exact existing Kimai customer name used to filter projects",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Kimai base URL")
    parser.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        help=(
            "UTF-8 file containing the raw Kimai API token; otherwise use "
            "KIMAI_API_TOKEN, KIMAI_TOKEN_FILE, or ./kimai.env"
        ),
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    try:
        return export(build_parser().parse_args())
    except ImportFailure as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
