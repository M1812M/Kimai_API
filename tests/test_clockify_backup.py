from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from kimai_import_export.clockify_backup import (
    AUDIT_ACTIONS,
    BackupSession,
    HttpFailure,
    ReadOnlyClockifyClient,
    ReadOnlyViolation,
    atomic_write,
    deduplicate,
    extract_items,
    finalize_backup,
    fetch_audit_log_adaptive,
    fetch_detailed_report_adaptive,
    fetch_entity_changes_adaptive,
    fetch_time_entries_adaptive,
    internal_verification,
    redact_secrets,
    verify_backup,
)


class FakeResponse:
    def __init__(self, payload, headers=None, status=200):
        self.payload = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )
        self.headers = headers or {"Content-Type": "application/json"}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class ReadOnlyClientTests(unittest.TestCase):
    def client(self, **updates):
        return ReadOnlyClockifyClient("fake-key", request_delay=0, **updates)

    def test_blocks_every_mutating_http_method_before_network(self):
        client = self.client()
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                with self.assertRaises(ReadOnlyViolation):
                    client.request(method, "workspaces/ws1/projects/p1")

    def test_blocks_non_allowlisted_post(self):
        with self.assertRaises(ReadOnlyViolation):
            self.client().request(
                "POST", "workspaces/ws1/projects", body={"name": "never"}
            )

    @patch("kimai_import_export.clockify_backup.urlopen")
    def test_allows_documented_read_only_report_post(self, mocked_urlopen):
        mocked_urlopen.return_value = FakeResponse({"timeentries": []})

        response = self.client().request(
            "POST",
            "workspaces/ws1/reports/detailed",
            base="reports",
            body={"exportType": "JSON"},
        )

        self.assertEqual({"timeentries": []}, json.loads(response.body))

    @patch("kimai_import_export.clockify_backup.urlopen")
    def test_allows_documented_read_only_user_info_post(self, mocked_urlopen):
        mocked_urlopen.return_value = FakeResponse([])

        response = self.client().request(
            "POST",
            "workspaces/ws1/users/info",
            body={"status": "ALL", "page": 1, "pageSize": 200},
        )

        self.assertEqual([], json.loads(response.body))

    @patch("kimai_import_export.clockify_backup.time.sleep")
    @patch("kimai_import_export.clockify_backup.urlopen")
    def test_retries_rate_limit_without_exposing_key(self, mocked_urlopen, mocked_sleep):
        rate_limit = HTTPError(
            "https://api.clockify.test/api/v1/user",
            429,
            "rate limited",
            {"Retry-After": "0"},
            io.BytesIO(b"try later"),
        )
        mocked_urlopen.side_effect = [rate_limit, FakeResponse({"id": "u1"})]

        response = self.client(max_retries=1).request("GET", "user")

        self.assertEqual({"id": "u1"}, json.loads(response.body))
        self.assertEqual(2, mocked_urlopen.call_count)
        mocked_sleep.assert_called_once_with(0.0)
        rate_limit.close()


class BackupSessionTests(unittest.TestCase):
    @patch("kimai_import_export.clockify_backup.time.sleep")
    def test_atomic_write_retries_a_temporary_windows_file_lock(self, mocked_sleep):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "manifest.json")
            original_replace = Path.replace
            attempts = 0

            def flaky_replace(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("temporarily locked")
                return original_replace(source, destination)

            with patch.object(Path, "replace", autospec=True, side_effect=flaky_replace):
                atomic_write(target, b"preserved")

            self.assertEqual(b"preserved", target.read_bytes())
            self.assertEqual(2, attempts)
            mocked_sleep.assert_called_once_with(0.05)

    def test_last_page_pagination_and_resume_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary, "run")
            client = ReadOnlyClockifyClient("fake-key")
            with patch(
                "kimai_import_export.clockify_backup.urlopen",
                side_effect=[
                    FakeResponse([{"id": "one"}], {"Last-Page": "false", "Content-Type": "application/json"}),
                    FakeResponse([{"id": "two"}], {"Last-Page": "true", "Content-Type": "application/json"}),
                ],
            ):
                session = BackupSession(
                    run_dir,
                    client,
                    cutoff=datetime(2026, 8, 27, tzinfo=timezone.utc),
                    requested_workspace_ids=[],
                    resume=False,
                )
                client.event_sink = session.request_event

                rows = session.fetch_collection(
                    "ws1/users", "ws1", "workspaces/ws1/users", core=True, page_size=1
                )

            self.assertEqual(["one", "two"], [row["id"] for row in rows])
            self.assertEqual(
                "complete", session.manifest["datasets"]["ws1/users"]["status"]
            )
            resumed_client = ReadOnlyClockifyClient("fake-key")
            resumed = BackupSession(
                run_dir,
                resumed_client,
                cutoff=datetime(2026, 8, 27, tzinfo=timezone.utc),
                requested_workspace_ids=[],
                resume=True,
            )
            self.assertEqual(
                rows,
                resumed.fetch_collection(
                    "ws1/users", "ws1", "workspaces/ws1/users", core=True
                ),
            )

    def test_redacts_nested_secret_fields(self):
        redacted = redact_secrets(
            {
                "id": "hook",
                "authToken": "secret",
                "nested": {"authorization": "Bearer secret", "name": "kept"},
            }
        )

        self.assertEqual("[REDACTED]", redacted["authToken"])
        self.assertEqual("[REDACTED]", redacted["nested"]["authorization"])
        self.assertEqual("kept", redacted["nested"]["name"])

    def test_deduplicates_overlapping_range_boundaries_by_id(self):
        rows = deduplicate(
            [
                {"id": "entry-1", "description": "first"},
                {"id": "entry-1", "description": "same boundary"},
                {"id": "entry-2"},
            ]
        )

        self.assertEqual(["entry-1", "entry-2"], [row["id"] for row in rows])

    def test_large_rejected_time_range_is_split_and_merged(self):
        class FakeSession:
            def __init__(self):
                self.calls = []

            def fetch_collection(self, key, workspace_id, endpoint, **kwargs):
                start = datetime.fromisoformat(kwargs["params"]["start"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(kwargs["params"]["end"].replace("Z", "+00:00"))
                self.calls.append((start, end))
                if end - start > timedelta(days=10):
                    raise HttpFailure(400, "GET", endpoint, "range too large")
                return [{"id": f"{start.date()}-{end.date()}"}]

            def _record_gap(self, *args, **kwargs):
                raise AssertionError("A splittable range must not become a gap")

        session = FakeSession()
        rows = fetch_time_entries_adaptive(
            session,
            "ws1",
            "user1",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 21, tzinfo=timezone.utc),
        )

        self.assertEqual(2, len(rows))
        self.assertEqual(3, len(session.calls))

    def test_audit_log_uses_all_documented_actions_and_null_authors(self):
        class FakeSession:
            def load_completed_items(self, *args, **kwargs):
                return None

            def fetch_post_collection(self, *args, **kwargs):
                self.kwargs = kwargs
                return [{"id": "audit-1"}]

        session = FakeSession()
        rows = fetch_audit_log_adaptive(
            session,
            "ws1",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        self.assertEqual([{"id": "audit-1"}], rows)
        self.assertIsNone(session.kwargs["body"]["authors"])
        self.assertEqual(list(AUDIT_ACTIONS), session.kwargs["body"]["actions"])
        self.assertEqual("page-size", session.kwargs["page_size_parameter"])

    def test_entity_changes_start_at_page_zero_and_response_wrapper_is_read(self):
        class FakeSession:
            def load_completed_items(self, *args, **kwargs):
                return None

            def fetch_collection(self, *args, **kwargs):
                self.kwargs = kwargs
                return extract_items({"response": [{"id": "deleted-1"}]})

        session = FakeSession()
        rows = fetch_entity_changes_adaptive(
            session,
            "ws1",
            "deleted",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        self.assertEqual([{"id": "deleted-1"}], rows)
        self.assertEqual(0, session.kwargs["start_page"])

    def test_failed_recent_audit_probe_prevents_historical_request_storm(self):
        class FakeSession:
            def __init__(self):
                self.calls = 0
                self.manifest = {"datasets": {}}

            def fetch_post_collection(self, key, *args, **kwargs):
                self.calls += 1
                raise HttpFailure(400, "POST", "audit-log", "not available")

            def load_completed_items(self, *args, **kwargs):
                return None

            def _record_gap(self, key, *args, **kwargs):
                self.manifest["datasets"][key] = {"status": "failed"}

        session = FakeSession()
        rows = fetch_audit_log_adaptive(
            session,
            "ws1",
            datetime(1970, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        self.assertEqual([], rows)
        self.assertEqual(1, session.calls)

    def test_failed_recent_report_probe_prevents_historical_request_storm(self):
        class FakeSession:
            def __init__(self):
                self.calls = 0
                self.manifest = {"datasets": {}}

            def fetch_post_collection(self, key, *args, **kwargs):
                self.calls += 1
                raise HttpFailure(400, "POST", "detailed", "not available")

            def _record_gap(self, key, *args, **kwargs):
                self.manifest["datasets"][key] = {"status": "failed"}

        session = FakeSession()
        rows = fetch_detailed_report_adaptive(
            session,
            "ws1",
            datetime(1970, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        self.assertEqual([], rows)
        self.assertEqual(1, session.calls)

    def test_entity_changes_use_bounded_historical_windows(self):
        class FakeSession:
            def __init__(self):
                self.calls = 0

            def load_completed_items(self, *args, **kwargs):
                return None

            def fetch_collection(self, *args, **kwargs):
                self.calls += 1
                return [{"id": f"change-{self.calls}"}]

        session = FakeSession()
        start = datetime(2020, 1, 1, tzinfo=timezone.utc)
        rows = fetch_entity_changes_adaptive(
            session,
            "ws1",
            "created",
            start,
            start + timedelta(days=92 * 4),
            capability_probe=False,
        )

        self.assertEqual(4, len(rows))
        self.assertEqual(4, session.calls)

    def test_webhook_response_is_redacted_before_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary, "run")
            client = ReadOnlyClockifyClient("fake-key")
            with patch(
                "kimai_import_export.clockify_backup.urlopen",
                return_value=FakeResponse(
                    {"webhooks": [{"id": "hook1", "authToken": "do-not-store"}]}
                ),
            ):
                session = BackupSession(
                    run_dir,
                    client,
                    cutoff=datetime(2026, 8, 27, tzinfo=timezone.utc),
                    requested_workspace_ids=[],
                    resume=False,
                )
                session.fetch_json(
                    "ws1/webhooks-redacted",
                    "ws1",
                    "workspaces/ws1/webhooks",
                    core=False,
                    redact=True,
                )

            stored = next((run_dir / "raw" / "ws1").rglob("*.json")).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("do-not-store", stored)
            self.assertIn("[REDACTED]", stored)

    def test_unavailable_optional_module_is_not_a_partial_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary, "run")
            session = BackupSession(
                run_dir,
                ReadOnlyClockifyClient("fake-key"),
                cutoff=datetime(2026, 8, 27, tzinfo=timezone.utc),
                requested_workspace_ids=[],
                resume=False,
            )
            session._record_gap(
                "ws1/expenses",
                HttpFailure(404, "GET", "workspaces/ws1/expenses", "not enabled"),
                core=False,
                recommendation="No action needed if the module was never used.",
            )

            self.assertEqual(
                "not_enabled", session.manifest["datasets"]["ws1/expenses"]["status"]
            )


class BackupVerificationTests(unittest.TestCase):
    def make_complete_run(self, root: Path) -> Path:
        run_dir = root / "run"
        client = ReadOnlyClockifyClient("fake-key")
        session = BackupSession(
            run_dir,
            client,
            cutoff=datetime(2026, 8, 27, tzinfo=timezone.utc),
            requested_workspace_ids=[],
            resume=False,
        )
        session.manifest["discovered_workspace_ids"] = ["ws1"]
        session.manifest["selected_workspace_ids"] = ["ws1"]
        session.manifest["workspaces"] = {"ws1": {"name": "One", "status": "complete"}}
        normalized = run_dir / "normalized" / "ws1"
        normalized.mkdir(parents=True)
        (normalized / "time-entries.jsonl").write_text("", encoding="utf-8")
        (normalized / "time-entries.csv").write_text(
            "Project,Task,User,Email,Start Date,Start Time,End Date,End Time,Duration (decimal),Description,Billable,Tags\n",
            encoding="utf-8-sig",
        )
        session.save_manifest()
        precheck = internal_verification(run_dir, session.manifest)
        self.assertEqual("PASS", precheck["status"], precheck)
        self.assertEqual(0, finalize_backup(session), precheck)
        return run_dir

    def test_checksum_and_zip_verification_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self.make_complete_run(Path(temporary))

            success, result = verify_backup(run_dir)

            self.assertTrue(success, result)

    def test_corruption_is_detected_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self.make_complete_run(Path(temporary))
            (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

            success, result = verify_backup(run_dir)

            self.assertFalse(success)
            self.assertTrue(any("Checksum mismatch" in item for item in result["errors"]))


if __name__ == "__main__":
    unittest.main()
