import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import daily_pipeline
import linkedin_ai_job_filter
from scrape_all import classify_role_domain


class RoleDomainTests(unittest.TestCase):
    def test_substrings_do_not_count_as_ai(self):
        # Previously "ai" in "ClaimCenter"/"Blockchain"/"Retail"/"Email" and "ml" in "HTML" matched.
        for title in [
            "Senior Guidewire ClaimCenter Developer (Remote)",
            "Senior Retail Operations Lead",
            "Senior Email Marketing Manager",
            "Senior HTML Designer",
        ]:
            self.assertNotIn(
                classify_role_domain(title),
                {"Artificial Intelligence", "Machine Learning", "Generative AI"},
                title,
            )
        self.assertEqual(
            classify_role_domain("Senior Software Engineer - Go & Rust, Blockchain Infrastructure"),
            "Software Engineering",
        )

    def test_real_ai_titles(self):
        self.assertEqual(classify_role_domain("Senior AI Engineer"), "Artificial Intelligence")
        self.assertEqual(classify_role_domain("Sr. AI/ML Engineer - Remote"), "Machine Learning")
        self.assertEqual(classify_role_domain("Staff LLM Engineer"), "Generative AI")
        self.assertEqual(classify_role_domain("Senior MLOps Engineer"), "MLOps")
        self.assertEqual(classify_role_domain("Lead Data Scientist"), "Data Science / Machine Learning")
        self.assertEqual(classify_role_domain("Senior Accountant"), "Other")


class ScrapeOverwriteTests(unittest.TestCase):
    def test_empty_scrape_does_not_overwrite_input(self):
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "input_jobs.csv"
            inp.write_text("keep me\n", encoding="utf-8")
            with mock.patch.object(linkedin_ai_job_filter, "scrape_linkedin_search", return_value=[]):
                code = linkedin_ai_job_filter.main(["--scrape", "--input", str(inp)])
            self.assertEqual(code, 1)
            self.assertEqual(inp.read_text(encoding="utf-8"), "keep me\n")


class PipelineHelperTests(unittest.TestCase):
    def test_tab_name_uses_karachi_date(self):
        # 19:30 UTC on 18 Nov is 00:30 on 19 Nov in Karachi (UTC+5).
        self.assertEqual(daily_pipeline.tab_name_for(datetime(2026, 11, 18, 19, 30, tzinfo=timezone.utc)), "19 Nov")
        self.assertEqual(daily_pipeline.tab_name_for(datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc)), "03 Mar")

    def test_tab_name_evening_runs_belong_to_next_day(self):
        utc = timezone.utc
        # 9:07 PM PKT Sun 22 Nov -> Monday's tab
        self.assertEqual(daily_pipeline.tab_name_for(datetime(2026, 11, 22, 16, 7, tzinfo=utc)), "23 Nov")
        # 10:43 PM PKT backup trigger on 31 Dec -> new year
        self.assertEqual(daily_pipeline.tab_name_for(datetime(2026, 12, 31, 17, 43, tzinfo=utc)), "01 Jan")
        # Run delayed past midnight keeps the same tab
        self.assertEqual(daily_pipeline.tab_name_for(datetime(2026, 11, 22, 21, 30, tzinfo=utc)), "23 Nov")
        # 5:59 PM PKT stays on the same day; 6:00 PM rolls over
        self.assertEqual(daily_pipeline.tab_name_for(datetime(2026, 11, 22, 12, 59, tzinfo=utc)), "22 Nov")
        self.assertEqual(daily_pipeline.tab_name_for(datetime(2026, 11, 22, 13, 0, tzinfo=utc)), "23 Nov")

    def test_dedupe_keeps_first_occurrence_order(self):
        urls = ["https://a.com/1", "https://b.com/2/", "https://A.com/1", "https://b.com/2", "", "https://c.com/3"]
        self.assertEqual(
            daily_pipeline.dedupe_keep_order(urls),
            ["https://a.com/1", "https://b.com/2/", "https://c.com/3"],
        )

    def test_post_skipped_without_secrets(self):
        with mock.patch.dict("os.environ", {"TRUESOFTS_INGEST_URL": "", "TRUESOFTS_INGEST_TOKEN": ""}):
            self.assertIsNone(daily_pipeline.post_urls({"urls": []}))


if __name__ == "__main__":
    unittest.main()
