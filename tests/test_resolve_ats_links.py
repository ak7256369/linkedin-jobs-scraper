import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import resolve_ats_links


class ResolveAtsLinksTests(unittest.TestCase):
    def setUp(self):
        resolve_ats_links._cache.clear()
        resolve_ats_links._key_locks.clear()

    def test_resolve_external_posting_passes_remote_us_senior_job(self):
        row = {
            "job_title": "Senior AI Engineer",
            "company": "Acme AI",
            "apply_url": "https://www.linkedin.com/jobs/view/123456",
            "posted_minutes_ago": "45",
            "role_domain": "AI Engineering",
        }
        posting = {
            "ats": "ashby",
            "title": "Senior AI Engineer",
            "location": "Remote - US",
            "wpt": "Remote",
            "url": "https://jobs.ashbyhq.com/acmeai/abc-123",
            "desc": "Build production LLM agents across US remote teams.",
        }

        with (
            patch.object(resolve_ats_links, "find_tier1_boards", return_value=[[posting]]),
            patch.object(resolve_ats_links, "find_enterprise_candidates", return_value=[]),
            patch.object(resolve_ats_links, "workday_search", return_value=[]),
            patch.object(resolve_ats_links, "discover_company_career_info") as mock_disc,
        ):
            res = resolve_ats_links.resolve_external_posting(row)

        mock_disc.assert_not_called()
        self.assertIsNotNone(res)
        self.assertEqual(res["filter_status"], "PASS")
        self.assertEqual(res["remote_status"], "REMOTE")
        self.assertEqual(res["external_apply_url"], "https://jobs.ashbyhq.com/acmeai/abc-123")
        self.assertEqual(res["rejection_reasons"], "")

    def test_resolve_external_posting_rejects_hybrid_non_us_blacklisted_and_director(self):
        cases = [
            (
                "hybrid",
                {"job_title": "Senior ML Engineer", "company": "Acme AI", "role_domain": "AI Engineering"},
                {
                    "ats": "ashby",
                    "title": "Senior ML Engineer",
                    "location": "San Francisco, CA",
                    "wpt": "Hybrid",
                    "url": "https://jobs.ashbyhq.com/acmeai/hybrid-1",
                    "desc": "Hybrid 3 days in office.",
                },
                "HYBRID",
                "external remote/US check failed",
            ),
            (
                "non_us",
                {"job_title": "Senior Software Engineer", "company": "Acme AI", "role_domain": "Software Engineering"},
                {
                    "ats": "ashby",
                    "title": "Senior Software Engineer",
                    "location": "Remote - Canada",
                    "wpt": "Remote",
                    "url": "https://jobs.ashbyhq.com/acmeai/ca-1",
                    "desc": "Remote within Canada.",
                },
                "NON_US",
                "NON_US",
            ),
            (
                "blacklisted",
                {"job_title": "Senior Software Engineer", "company": "Turing", "role_domain": "Software Engineering"},
                {
                    "ats": "company_website",
                    "title": "Senior Software Engineer",
                    "location": "Remote - US",
                    "wpt": "Remote",
                    "url": "https://careers.example.com/jobs/999",
                    "desc": "Fully remote US role.",
                },
                "REMOTE",
                "Turing",
            ),
            (
                "director",
                {"job_title": "Director of Software Engineering", "company": "Acme AI", "role_domain": "Software Engineering"},
                {
                    "ats": "ashby",
                    "title": "Director of Software Engineering",
                    "location": "Remote - US",
                    "wpt": "Remote",
                    "url": "https://jobs.ashbyhq.com/acmeai/dir-1",
                    "desc": "Lead engineering org.",
                },
                "REMOTE",
                "Director / VP / Vice",
            ),
        ]

        for label, row, posting, expected_remote, expected_reason in cases:
            with self.subTest(case=label):
                resolve_ats_links._cache.clear()
                with (
                    patch.object(resolve_ats_links, "find_tier1_boards", return_value=[[posting]]),
                    patch.object(resolve_ats_links, "find_enterprise_candidates", return_value=[]),
                    patch.object(resolve_ats_links, "workday_search", return_value=[]),
                ):
                    res = resolve_ats_links.resolve_external_posting(row)
                self.assertIsNotNone(res)
                self.assertEqual(res["filter_status"], "REJECT")
                self.assertEqual(res["remote_status"], expected_remote)
                self.assertIn(expected_reason, res["rejection_reasons"])

    def test_unmatched_job_returns_none_with_no_linkedin_fallback(self):
        row = {
            "job_title": "Senior AI Research Engineer",
            "company": "Stealth Startup",
            "apply_url": "https://www.linkedin.com/jobs/view/999999",
            "posted_minutes_ago": "20",
            "role_domain": "AI Engineering",
        }
        empty_disc = {
            "domains": [],
            "ats_slugs": [],
            "workday": [],
            "avature": [],
            "oracle": [],
            "eightfold": [],
            "talentbrew": [],
            "jibe": [],
            "direct_jobs": [],
        }
        with (
            patch.object(resolve_ats_links, "find_tier1_boards", return_value=[]),
            patch.object(resolve_ats_links, "find_enterprise_candidates", return_value=[]),
            patch.object(resolve_ats_links, "workday_search", return_value=[]),
            patch.object(resolve_ats_links, "discover_company_career_info", return_value=empty_disc),
        ):
            res = resolve_ats_links.resolve_external_posting(row)

        self.assertIsNone(res)

    def test_extract_career_signals_detects_ats_workday_jsonld_and_links(self):
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "Staff AI Platform Engineer",
         "jobLocationType": "TELECOMMUTE",
         "jobLocation": "United States",
         "url": "https://acme.com/careers/jobs/staff-ai-platform",
         "description": "<p>Remote across the US.</p>"}
        </script>
        </head><body>
          <iframe src="https://boards.greenhouse.io/embed/job_board?for=acmeinc"></iframe>
          <a href="https://job-boards.greenhouse.io/courtyardinc/jobs/4230146009">Staff Software Engineer</a>
          <a href="https://jobs.ashbyhq.com/acmeashby">Ashby</a>
          <a href="https://jobs.eu.lever.co/acmelever">Lever</a>
          <a href="https://acme.wd5.myworkdayjobs.com/en-US/Acme_Careers">Workday</a>
          <a href="/jobs/senior-backend-engineer">Senior Backend Engineer</a>
        </body></html>
        """
        sig = resolve_ats_links.extract_career_signals("https://acme.com/careers", html)

        self.assertIn(("greenhouse", "acmeinc"), sig["ats_slugs"])
        self.assertIn(("greenhouse", "courtyardinc"), sig["ats_slugs"])
        self.assertIn(("ashby", "acmeashby"), sig["ats_slugs"])
        self.assertIn(("lever", "acmelever"), sig["ats_slugs"])
        self.assertIn(("acme.wd5.myworkdayjobs.com", "acme", "Acme_Careers"), sig["workday"])

        titles = {j["title"]: j for j in sig["direct_jobs"]}
        self.assertNotIn("Staff Software Engineer", titles)
        self.assertIn("Staff AI Platform Engineer", titles)
        self.assertEqual(titles["Staff AI Platform Engineer"]["wpt"], "Remote")
        self.assertEqual(
            titles["Staff AI Platform Engineer"]["url"],
            "https://acme.com/careers/jobs/staff-ai-platform",
        )
        self.assertIn("Senior Backend Engineer", titles)
        self.assertEqual(
            titles["Senior Backend Engineer"]["url"],
            "https://acme.com/jobs/senior-backend-engineer",
        )

    def test_deduplicate_prefers_pass_over_reject_and_preserves_distinct_queries(self):
        rows = [
            {
                "filter_status": "REJECT",
                "company": "Acme",
                "job_title": "Senior Engineer",
                "external_apply_url": "https://boards.greenhouse.io/acme/jobs/1?gh_jid=101",
            },
            {
                "filter_status": "PASS",
                "company": "Acme",
                "job_title": "Senior Engineer",
                "external_apply_url": "HTTPS://Boards.Greenhouse.io/acme/jobs/1/?gh_jid=101",
            },
            {
                "filter_status": "PASS",
                "company": "Acme",
                "job_title": "Senior Engineer II",
                "external_apply_url": "https://boards.greenhouse.io/acme/jobs/1?gh_jid=202",
            },
        ]
        deduped = resolve_ats_links.deduplicate_results(rows)
        self.assertEqual(len(deduped), 2)
        by_url = {r["external_apply_url"]: r for r in deduped}
        self.assertIn("HTTPS://Boards.Greenhouse.io/acme/jobs/1/?gh_jid=101", by_url)
        self.assertEqual(
            by_url["HTTPS://Boards.Greenhouse.io/acme/jobs/1/?gh_jid=101"]["filter_status"],
            "PASS",
        )
        self.assertIn("https://boards.greenhouse.io/acme/jobs/1?gh_jid=202", by_url)

    def test_best_match_preserves_team_parentheticals(self):
        postings = [
            {
                "ats": "greenhouse",
                "title": "Senior Software Engineer (Core Systems)",
                "location": "Remote - US",
                "url": "https://boards.greenhouse.io/acme/jobs/1",
            },
            {
                "ats": "greenhouse",
                "title": "Senior Software Engineer (Developer Experience)",
                "location": "Remote - US",
                "url": "https://boards.greenhouse.io/acme/jobs/2",
            },
        ]
        match, score = resolve_ats_links.best_match(
            "Senior Software Engineer (Developer Experience)", postings
        )
        self.assertIsNotNone(match)
        self.assertAlmostEqual(score, 1.0)
        self.assertEqual(match["url"], "https://boards.greenhouse.io/acme/jobs/2")

    def test_cached_single_flight_executes_once(self):
        calls = 0
        count_lock = threading.Lock()

        def slow_fn():
            nonlocal calls
            with count_lock:
                calls += 1
            time.sleep(0.05)
            return ["board"]

        results = []

        def worker():
            results.append(resolve_ats_links.cached(("test_single_flight", "acme"), slow_fn))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(calls, 1)
        self.assertEqual(results, [["board"]] * 5)

    def test_classify_remote_us_handles_norway_and_title_remote_or_hybrid(self):
        status_no, _ = resolve_ats_links.classify_remote_us(
            "greenhouse", "Norway", "", "Distributed, Global", "", title="Principal Java Engineer"
        )
        self.assertEqual(status_no, "NON_US")

        status_title_remote, _ = resolve_ats_links.classify_remote_us(
            "ashby",
            "NAMER and LatAm, United States",
            "",
            "",
            "Join our cloud infra team.",
            title="Senior Software Engineer - Cloud Infrastructure - (US Remote)",
        )
        self.assertEqual(status_title_remote, "REMOTE")

        status_title_hybrid, _ = resolve_ats_links.classify_remote_us(
            "greenhouse",
            "United States",
            "",
            "",
            "",
            title="Senior AI Engineer (Hybrid)",
        )
        self.assertEqual(status_title_hybrid, "HYBRID")


if __name__ == "__main__":
    unittest.main()
