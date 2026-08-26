from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kimai_import_export.config import load_dotenv
from kimai_import_export.project_tasks import ImportFailure, load_token


class CredentialLoadingTests(unittest.TestCase):
    def test_loads_project_dotenv_without_overriding_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment_file = Path(temporary, ".env")
            environment_file.write_text(
                "# local configuration\n"
                "KIMAI_API_TOKEN=dotenv-token\n"
                "KIMAI_BASE_URL='https://time.example.org'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"KIMAI_API_TOKEN": "existing-token"}, clear=True):
                self.assertEqual(environment_file, load_dotenv(environment_file))
                self.assertEqual("existing-token", os.environ["KIMAI_API_TOKEN"])
                self.assertEqual("https://time.example.org", os.environ["KIMAI_BASE_URL"])

    def test_explicit_token_file_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary, "kimai-token.txt")
            token_file.write_text("file-token\n", encoding="utf-8")
            with patch.dict(os.environ, {"KIMAI_API_TOKEN": "environment-token"}, clear=True):
                self.assertEqual("file-token", load_token(token_file))

    def test_reads_token_from_environment(self) -> None:
        with patch.dict(os.environ, {"KIMAI_API_TOKEN": "environment-token"}, clear=True):
            self.assertEqual("environment-token", load_token())

    def test_reads_path_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary, "kimai-token.txt")
            token_file.write_text("file-token", encoding="utf-8")
            with patch.dict(os.environ, {"KIMAI_TOKEN_FILE": str(token_file)}, clear=True):
                self.assertEqual("file-token", load_token())

    def test_loads_named_kimai_dotenv_for_token_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment_file = Path(temporary, ".env.kimai")
            environment_file.write_text(
                "KIMAI_API_TOKEN=dotenv-kimai-token\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {}, clear=True):
                load_dotenv(environment_file)
                self.assertEqual("dotenv-kimai-token", load_token())

    def test_requires_an_explicit_credential_source(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ImportFailure, "No API token configured"):
                load_token()
