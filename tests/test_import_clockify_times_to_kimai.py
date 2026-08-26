from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from kimai_import_export.clockify_times import (
    ACTIVITY_MAPPING_HEADERS,
    USER_MAPPING_HEADERS,
    ActivityMapping,
    ClockifyEntry,
    LocalKimaiEntry,
    UserMapping,
    build_offline_plan,
    mapping_template_rows,
    read_clockify_entries,
    write_csv,
)


class ClockifyTimeImporterTests(unittest.TestCase):
    def write_clockify_csv(self, directory: Path, rows: list[dict[str, str]]) -> Path:
        path = directory / "clockify.csv"
        headers = [
            "Project", "Task", "User", "Email", "Start Date", "Start Time",
            "End Date", "End Time", "Duration (decimal)", "Description", "Billable", "Tags",
        ]
        write_csv(path, headers, rows)
        return path

    def source_row(self, **updates: str) -> dict[str, str]:
        row = {
            "Project": "KG.CommT",
            "Task": "Digital Workspace",
            "User": "Example User",
            "Email": "user@example.org",
            "Start Date": "2026-01-12",
            "Start Time": "08:00",
            "End Date": "2026-01-12",
            "End Time": "09:30",
            "Duration (decimal)": "1.50",
            "Description": "Testing",
            "Billable": "No",
            "Tags": "",
        }
        row.update(updates)
        return row

    def test_parses_valid_clockify_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entries = read_clockify_entries(
                self.write_clockify_csv(Path(temporary), [self.source_row()])
            )
        self.assertEqual(1, len(entries))
        self.assertEqual(Decimal("1.50"), entries[0].duration_hours)
        self.assertEqual(datetime(2026, 1, 12, 8, 0), entries[0].begin)
        self.assertFalse(entries[0].billable)

    def test_parses_api_generated_timestamps_with_seconds(self) -> None:
        row = self.source_row(
            **{
                "Start Time": "08:00:15.125",
                "End Time": "09:30:15.125",
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            entries = read_clockify_entries(
                self.write_clockify_csv(Path(temporary), [row])
            )

        self.assertEqual(15, entries[0].begin.second)
        self.assertEqual(15, entries[0].end.second)
        self.assertEqual(125000, entries[0].begin.microsecond)

    def test_rejects_duration_that_does_not_match_times(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_clockify_csv(
                Path(temporary), [self.source_row(**{"Duration (decimal)": "1.00"})]
            )
            with self.assertRaisesRegex(Exception, "does not match"):
                read_clockify_entries(path)

    def test_offline_plan_requires_approved_mappings(self) -> None:
        source = ClockifyEntry(
            2, "KG.CommT", "Digital Workspace", "Example", "user@example.org",
            datetime(2026, 1, 12, 8), datetime(2026, 1, 12, 9), Decimal("1.00"),
            "Testing", False, "", "fingerprint",
        )
        plan = build_offline_plan(
            [source],
            {("kg.commt", "digital workspace"): ActivityMapping(
                "KG.CommT", "Digital Workspace", "[KG.CommT] Kyrgyzstan - CommT",
                "Digital Workspace", "review", "",
            )},
            {"user@example.org": UserMapping(
                "user@example.org", "user@example.org", "approved", ""
            )},
            [],
        )
        self.assertEqual("blocked", plan[0].status)
        self.assertIn("not been approved", plan[0].reason)

    def test_offline_plan_detects_exact_local_duplicate(self) -> None:
        source = ClockifyEntry(
            2, "KG.CommT", "Digital Workspace", "Example", "user@example.org",
            datetime(2026, 1, 12, 8), datetime(2026, 1, 12, 9), Decimal("1.00"),
            "Testing", False, "", "fingerprint",
        )
        plan = build_offline_plan(
            [source],
            {("kg.commt", "digital workspace"): ActivityMapping(
                "KG.CommT", "Digital Workspace", "[KG.CommT] Kyrgyzstan - CommT",
                "Digital Workspace", "approved", "",
            )},
            {"user@example.org": UserMapping(
                "user@example.org", "user@example.org", "approved", ""
            )},
            [LocalKimaiEntry(
                4, "user@example.org", "[KG.CommT] Kyrgyzstan - CommT",
                "Digital Workspace", datetime(2026, 1, 12, 8), datetime(2026, 1, 12, 9), "Testing",
            )],
        )
        self.assertEqual("already_in_local_kimai_export", plan[0].status)

    def test_template_suggests_exact_code_and_activity(self) -> None:
        source = ClockifyEntry(
            2, "KG.CommT", "Цифровое Рабочее Пространство / Digital Workspace", "Example", "user@example.org",
            datetime(2026, 1, 12, 8), datetime(2026, 1, 12, 9), Decimal("1.00"),
            "Testing", False, "", "fingerprint",
        )
        local = LocalKimaiEntry(
            4, "user@example.org", "[KG.CommT] Kyrgyzstan - CommT",
            "Цифровая среда / Digital Workspace", datetime(2026, 8, 4, 8), datetime(2026, 8, 4, 9), "Testing",
        )
        activity_rows, user_rows = mapping_template_rows([source], [local])
        self.assertEqual("[KG.CommT] Kyrgyzstan - CommT", activity_rows[0]["Kimai Project"])
        self.assertEqual("Цифровая среда / Digital Workspace", activity_rows[0]["Kimai Activity"])
        self.assertEqual("user@example.org", user_rows[0]["Kimai User E-mail"])


if __name__ == "__main__":
    unittest.main()
