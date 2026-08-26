from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import kimai_api


class KimaiApiScriptTests(unittest.TestCase):
    @patch("kimai_api.project_tasks.main", return_value=0)
    def test_import_command_forwards_arguments(self, mocked_main):
        result = kimai_api.main(["import", ".\\data\\", "--offline"])

        self.assertEqual(0, result)
        mocked_main.assert_called_once_with([".\\data\\", "--offline"])

    @patch("kimai_api.project_tasks.main", return_value=0)
    def test_import_uses_project_local_raw_token_file(self, mocked_main):
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary, "kimai.env")
            token_file.write_text("fake-token", encoding="utf-8")
            with patch("kimai_api.KIMAI_TOKEN_FILE", token_file):
                result = kimai_api.main(["import", ".\\data\\"])

        self.assertEqual(0, result)
        mocked_main.assert_called_once_with(
            [".\\data\\", "--token-file", str(token_file)]
        )

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


if __name__ == "__main__":
    unittest.main()
