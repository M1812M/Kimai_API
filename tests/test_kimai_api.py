from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import kimai_api


class KimaiApiScriptTests(unittest.TestCase):
    @patch("kimai_api.project_tasks.main", return_value=0)
    def test_import_command_forwards_arguments(self, mocked_main):
        result = kimai_api.main(["import", ".\\data\\", "--offline"])

        self.assertEqual(0, result)
        mocked_main.assert_called_once_with([".\\data\\", "--offline"])

    @patch("kimai_api.project_tasks.main", return_value=0)
    def test_live_import_forwards_without_generated_launcher(self, mocked_main):
        result = kimai_api.main(["import", ".\\data\\"])

        self.assertEqual(0, result)
        mocked_main.assert_called_once_with([".\\data\\"])

    def test_help_is_available_without_a_command(self):
        output = io.StringIO()

        with redirect_stdout(output):
            result = kimai_api.main([])

        self.assertEqual(0, result)
        self.assertIn("python kimai_api.py import", output.getvalue())

    def test_unknown_command_fails_cleanly(self):
        output = io.StringIO()

        with redirect_stderr(output):
            result = kimai_api.main(["unknown"])

        self.assertEqual(2, result)
        self.assertIn("unknown command", output.getvalue())

    @patch("kimai_api.clockify_backup.backup_main", return_value=0)
    def test_backup_command_forwards_arguments(self, mocked_main):
        result = kimai_api.main(["backup-clockify", "--workspace-id", "ws1"])

        self.assertEqual(0, result)
        mocked_main.assert_called_once_with(["--workspace-id", "ws1"])

    @patch("kimai_api.clockify_backup.verify_main", return_value=0)
    def test_verify_command_forwards_arguments(self, mocked_main):
        result = kimai_api.main(["verify-clockify-backup", ".\\data\\run"])

        self.assertEqual(0, result)
        mocked_main.assert_called_once_with([".\\data\\run"])


if __name__ == "__main__":
    unittest.main()
