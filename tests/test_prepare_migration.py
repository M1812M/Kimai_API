from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from kimai_import_export.prepare_migration import (
    ActivitySuggestion,
    ClockifyApi,
    ClockifyTimeEntry,
    UserSuggestion,
    build_activity_mappings,
    build_audit_rows,
    build_user_mappings,
    build_parser,
    kimai_timestamp,
    load_clockify_api_key,
    output_paths,
    parse_utc_offset,
    run,
)
from kimai_import_export.project_tasks import ImportFailure


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class ClockifyCredentialTests(unittest.TestCase):
    def test_reads_exact_clockify_assignment(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, ".env.clockify")
            path.write_text("CLOCKIFY_API_KEY=test-key\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual("test-key", load_clockify_api_key(path))

    def test_rejects_a_kimai_assignment_in_clockify_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, ".env.clockify")
            path.write_text("KIMAI_API_TOKEN=test-key\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ImportFailure, "CLOCKIFY_API_KEY"):
                    load_clockify_api_key(path)


class ClockifyApiTests(unittest.TestCase):
    @patch("kimai_import_export.prepare_migration.urlopen")
    def test_uses_api_key_and_last_page_pagination(self, mocked_urlopen):
        mocked_urlopen.side_effect = [
            FakeResponse([{"id": "one"}], {"Last-Page": "false"}),
            FakeResponse([{"id": "two"}], {"Last-Page": "true"}),
        ]
        api = ClockifyApi("https://api.clockify.test/api/v1", "secret", 10)

        result = api.collection("workspaces/ws/projects", page_size=1)

        self.assertEqual([{"id": "one"}, {"id": "two"}], result)
        first_request = mocked_urlopen.call_args_list[0].args[0]
        second_request = mocked_urlopen.call_args_list[1].args[0]
        self.assertEqual("secret", first_request.get_header("X-api-key"))
        self.assertIn("page=1", first_request.full_url)
        self.assertIn("page-size=1", first_request.full_url)
        self.assertIn("page=2", second_request.full_url)


class MappingTests(unittest.TestCase):
    def test_exact_project_code_and_activity_are_suggested_but_not_approved(self):
        clockify = [
            {
                "Project ID": "cp1",
                "Project": "KG.Health",
                "Task ID": "ct1",
                "Task": "Training",
            }
        ]
        kimai = [
            {
                "Project ID": 10,
                "Project": "[KG.Health] Health education",
                "Activity ID": 20,
                "Activity": "Обучение / Training",
            }
        ]

        rows, suggestions = build_activity_mappings(clockify, kimai)

        self.assertEqual("10", rows[0]["Kimai Project ID"])
        self.assertEqual("20", rows[0]["Kimai Activity ID"])
        self.assertEqual("review", rows[0]["Status"])
        self.assertEqual(20, suggestions[("kg.health", "training")].activity_id)

    def test_exact_user_email_is_suggested_but_not_approved(self):
        clockify = [
            {"id": "cu1", "name": "Clockify User", "email": "User@Example.org"}
        ]
        kimai = [
            {"id": 30, "username": "kimai-user", "email": "user@example.org"}
        ]

        rows, suggestions = build_user_mappings(clockify, kimai)

        self.assertEqual("30", rows[0]["Kimai User ID"])
        self.assertEqual("review", rows[0]["Status"])
        self.assertEqual(30, suggestions["user@example.org"].user_id)


class AuditTests(unittest.TestCase):
    local_zone = parse_utc_offset("+06:00")

    @staticmethod
    def entry(entry_id="ce1", description="Work"):
        return ClockifyTimeEntry(
            entry_id=entry_id,
            user_id="cu1",
            user_name="Clockify User",
            user_email="user@example.org",
            project_id="cp1",
            project_name="KG.Health",
            task_id="ct1",
            task_name="Training",
            start=datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, 4, 0, tzinfo=timezone.utc),
            description=description,
            billable=True,
            tags="Migration",
        )

    def test_naive_kimai_timestamp_uses_selected_local_offset(self):
        parsed = kimai_timestamp("2026-01-02T09:00:00", self.local_zone)

        self.assertEqual(
            datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc), parsed
        )

    def test_reports_existing_exact_kimai_entry(self):
        activity = {
            ("kg.health", "training"): ActivitySuggestion(
                "[KG.Health] Health education", 10, "Training", 20, "exact"
            )
        }
        users = {
            "user@example.org": UserSuggestion(
                "user@example.org", 30, "kimai-user", "exact"
            )
        }
        kimai = [
            {
                "user": {"id": 30},
                "project": {"id": 10},
                "activity": {"id": 20},
                "begin": "2026-01-02T09:00:00+06:00",
                "end": "2026-01-02T10:00:00+06:00",
                "description": "Work",
            }
        ]

        rows = build_audit_rows(
            [self.entry()], activity, users, kimai, self.local_zone
        )

        self.assertEqual("already_in_kimai", rows[0]["Audit Status"])

    def test_reports_duplicate_clockify_fingerprints_before_mapping(self):
        rows = build_audit_rows(
            [self.entry("ce1"), self.entry("ce2")],
            {},
            {},
            [],
            self.local_zone,
        )

        self.assertEqual(
            ["duplicate_clockify_source", "duplicate_clockify_source"],
            [row["Audit Status"] for row in rows],
        )

    def test_reports_unmapped_entry(self):
        rows = build_audit_rows([self.entry()], {}, {}, [], self.local_zone)

        self.assertEqual("unmapped", rows[0]["Audit Status"])
        self.assertIn("Kimai user", rows[0]["Audit Reason"])


class PreparationWorkflowTests(unittest.TestCase):
    def test_read_only_run_writes_all_five_outputs(self):
        class FakeClockify:
            def get(self, endpoint, params=None):
                if endpoint == "workspaces":
                    return [{"id": "ws1", "name": "CDI Clockify"}]
                if endpoint == "user":
                    return {"activeWorkspace": "ws1"}
                raise AssertionError(f"Unexpected Clockify GET: {endpoint}")

            def collection(self, endpoint, params=None, **kwargs):
                if endpoint == "workspaces/ws1/projects":
                    return [
                        {
                            "id": "cp1",
                            "name": "KG.Health",
                            "archived": False,
                            "billable": True,
                        }
                    ]
                if endpoint == "workspaces/ws1/projects/cp1/tasks":
                    return [
                        {
                            "id": "ct1",
                            "name": "Training",
                            "status": "ACTIVE",
                            "billable": True,
                        }
                    ]
                if endpoint == "workspaces/ws1/tags":
                    return (
                        [{"id": "tag1", "name": "Migration"}]
                        if params == {"archived": "false"}
                        else []
                    )
                if endpoint == "workspaces/ws1/users":
                    return [
                        {
                            "id": "cu1",
                            "name": "Clockify User",
                            "email": "user@example.org",
                            "status": "ACTIVE",
                        }
                    ]
                if endpoint == "workspaces/ws1/user/cu1/time-entries":
                    return [
                        {
                            "id": "ce1",
                            "projectId": "cp1",
                            "taskId": "ct1",
                            "tagIds": ["tag1"],
                            "description": "Work",
                            "billable": True,
                            "timeInterval": {
                                "start": "2026-01-02T03:00:15Z",
                                "end": "2026-01-02T04:00:15Z",
                            },
                        }
                    ]
                raise AssertionError(f"Unexpected Clockify collection: {endpoint}")

        class FakeKimai:
            def get(self, endpoint, params=None):
                if endpoint == "version":
                    return {"version": "test"}
                raise AssertionError(f"Unexpected Kimai GET: {endpoint}")

            def collection(self, endpoint, params=None):
                if endpoint == "customers":
                    return [{"id": 1, "name": "CDI"}]
                if endpoint == "projects":
                    return [
                        {
                            "id": 10,
                            "name": "[KG.Health] Health education",
                            "customer": {"id": 1, "name": "CDI"},
                            "visible": True,
                            "teams": [],
                        }
                    ]
                if endpoint == "activities":
                    return [
                        {
                            "id": 20,
                            "name": "Training",
                            "visible": True,
                            "teams": [],
                        }
                    ]
                if endpoint == "users":
                    return [
                        {
                            "id": 30,
                            "username": "kimai-user",
                            "email": "user@example.org",
                        }
                    ]
                if endpoint == "timesheets":
                    return []
                raise AssertionError(f"Unexpected Kimai collection: {endpoint}")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            credential = root / ".env"
            credential.write_text(
                "KIMAI_API_TOKEN=fake-kimai\n"
                "KIMAI_BASE_URL=https://time.example.org\n"
                "CLOCKIFY_API_KEY=fake-key\n",
                encoding="utf-8",
            )
            output = root / "migration"
            args = build_parser().parse_args(
                [
                    "--start-date",
                    "2026-01-01",
                    "--end-date",
                    "2026-01-31",
                    "--clockify-env",
                    str(credential),
                    "--output-dir",
                    str(output),
                ]
            )
            with (
                patch(
                    "kimai_import_export.prepare_migration.ClockifyApi",
                    return_value=FakeClockify(),
                ),
                patch(
                    "kimai_import_export.prepare_migration.KimaiApi",
                    return_value=FakeKimai(),
                ),
                patch.dict(os.environ, {"KIMAI_API_TOKEN": "fake-kimai"}),
            ):
                self.assertEqual(0, run(args))

            paths = output_paths(output)
            self.assertEqual(5, len(paths))
            self.assertTrue(all(path.is_file() for path in paths.values()))
            with paths["audit"].open(encoding="utf-8-sig", newline="") as handle:
                audit_rows = list(csv.DictReader(handle))
            self.assertEqual("mapped_review_required", audit_rows[0]["Audit Status"])
            self.assertEqual("Migration", audit_rows[0]["Tags"])


if __name__ == "__main__":
    unittest.main()
