import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from kimai_import_export.project_tasks import (
    ImportFailure,
    KimaiApi,
    comparison_key,
    find_csv_files,
    parse_review_csv,
    parse_review_csvs,
)


SAMPLE = """[SK-Health.OH] South Kyrgyzstan – One Health – Health Education,,
Manager / Ответственный,Status / Статус,
Valid? / Актуально?,Task / Задача,Comment or correction / Комментарий
☑,Упр. проектом / Project Mgmt,
☐,План. и отчетн. / Planning & Reporting,
☑,Встреча по проекту / Project Meeting,"Keep, approved"
☑,Перевод видео / Video Translation,
"""


class ReviewCsvTests(unittest.TestCase):
    def parse(self, content: str = SAMPLE):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "project-task-list.csv")
            path.write_text(content, encoding="utf-8")
            return parse_review_csv(path)

    def test_parses_project_and_checked_tasks(self):
        review = self.parse()
        self.assertEqual("SK-Health.OH", review.project_code)
        self.assertEqual(
            "South Kyrgyzstan – One Health – Health Education", review.project_name
        )
        self.assertEqual(3, len(review.tasks))
        self.assertEqual(1, review.ignored_task_count)
        self.assertEqual("Keep, approved", review.tasks[1].comment)

    def test_rejects_unknown_checkbox_marker(self):
        with self.assertRaisesRegex(ImportFailure, "Unknown checkbox"):
            self.parse(SAMPLE.replace("☐,План", "maybe,План"))

    def test_rejects_duplicate_checked_task(self):
        duplicate = SAMPLE + "☑,Перевод видео / Video Translation,\n"
        with self.assertRaisesRegex(ImportFailure, "Duplicate checked task"):
            self.parse(duplicate)

    def test_rejects_when_no_task_is_checked(self):
        unchecked = SAMPLE.replace("☑,", "☐,")
        with self.assertRaisesRegex(ImportFailure, "No checked tasks"):
            self.parse(unchecked)

    def test_parses_multiple_project_sections(self):
        second_project = """[KG.Finance] Kyrgyzstan – Finance,,
Valid? / Актуально?,Task / Задача,Comment or correction / Комментарий
☐,Финансы / Finance,
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "project-task-list.csv")
            path.write_text(SAMPLE + "\n" + second_project, encoding="utf-8")
            reviews = parse_review_csvs(path)

        self.assertEqual(2, len(reviews))
        self.assertEqual("SK-Health.OH", reviews[0].project_code)
        self.assertEqual(3, len(reviews[0].tasks))
        self.assertEqual("KG.Finance", reviews[1].project_code)
        self.assertEqual(0, len(reviews[1].tasks))

    def test_comparison_normalizes_dash_and_case(self):
        self.assertEqual(comparison_key("One – Health"), comparison_key("ONE - HEALTH"))

    def test_finds_all_csv_files_directly_in_a_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "z-last.csv").write_text(SAMPLE, encoding="utf-8")
            (folder / "A-first.CSV").write_text(SAMPLE, encoding="utf-8")
            (folder / "notes.txt").write_text("not a CSV", encoding="utf-8")
            nested = folder / "nested"
            nested.mkdir()
            (nested / "not-included.csv").write_text(SAMPLE, encoding="utf-8")

            csv_files = find_csv_files(folder)

        self.assertEqual(["A-first.CSV", "z-last.csv"], [path.name for path in csv_files])


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class KimaiApiTests(unittest.TestCase):
    @patch("kimai_import_export.project_tasks.urlopen")
    def test_posts_json_with_bearer_token(self, mocked_urlopen):
        mocked_urlopen.return_value = FakeResponse({"id": 42, "name": "Activity"})
        api = KimaiApi("https://time.example.org", "secret-token", 10)

        response = api.post(
            "activities",
            {"name": "Activity", "project": 5, "visible": True, "billable": False},
        )

        self.assertEqual(42, response["id"])
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual("Bearer secret-token", request.get_header("Authorization"))
        self.assertEqual("POST", request.method)
        self.assertEqual(
            {
                "name": "Activity",
                "project": 5,
                "visible": True,
                "billable": False,
            },
            json.loads(request.data.decode("utf-8")),
        )


if __name__ == "__main__":
    unittest.main()
