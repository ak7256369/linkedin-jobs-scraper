import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from linkedin_ai_job_filter import (
    BLACKLISTED_COMPANIES,
    canonicalize_url,
    check_record,
    create_sample_file,
    create_template,
    is_ai_domain,
    is_blacklisted_company,
    is_external_url,
    is_linkedin_job_url,
    is_remote_workplace,
    is_us_country,
    main,
    normalize_header,
    parse_minutes,
    process,
    read_input,
)


class FilterTests(unittest.TestCase):
    def base(self):
        return {
            "job_title": "Senior Machine Learning Engineer",
            "company": "Example AI Inc.",
            "country": "United States",
            "workplace_type": "Fully Remote",
            "seniority": "Senior",
            "role_domain": "Machine Learning",
            "linkedin_job_url": "https://www.linkedin.com/jobs/view/1234567890/",
            "external_apply_url": "https://jobs.example.com/role/123",
            "verified_badge": "yes",
            "posted_minutes_ago": "120",
            "notes": "",
        }

    def test_valid_record_passes(self):
        self.assertEqual(check_record(self.base()), [])

    # Seniority tests
    def test_seniority_explicit_and_in_title(self):
        # Senior in seniority field
        row = self.base()
        row["job_title"] = "Machine Learning Engineer"
        row["seniority"] = "Senior"
        self.assertEqual(check_record(row), [])

        # Senior abbreviations in title
        for title in [
            "Senior AI Engineer",
            "Sr. AI Engineer",
            "Sr AI Engineer",
            "(Senior) AI Engineer",
            "AI Engineer (Sr.)",
            "Staff/Senior AI Engineer",
            "Machine Learning Engineer, Sr",
            "Machine Learning Engineer - Sr",
            "Senior-level Engineer",
        ]:
            row = self.base()
            row["job_title"] = title
            row["seniority"] = ""
            self.assertEqual(check_record(row), [], f"Failed for title: {title}")

        # Senior abbreviations in seniority column
        for sen in ["Senior", "senior", "Sr.", "sr", "Senior / Lead", "Senior-level"]:
            row = self.base()
            row["job_title"] = "AI Engineer"
            row["seniority"] = sen
            self.assertEqual(check_record(row), [], f"Failed for seniority: {sen}")

    def test_non_senior_is_rejected(self):
        row = self.base()
        row["job_title"] = "Machine Learning Engineer"
        row["seniority"] = "Mid-level"
        reasons = check_record(row)
        self.assertTrue(any("Senior" in reason for reason in reasons))

        # Check sub-words like ASR / CSR don't count as Sr
        for title in ["ASR Engineer", "CSR Lead", "Broadcast Engineer"]:
            row = self.base()
            row["job_title"] = title
            row["seniority"] = ""
            reasons = check_record(row)
            self.assertTrue(any("Senior" in reason for reason in reasons), f"Should reject title: {title}")

    # Workplace type tests
    def test_workplace_type_variants(self):
        for wp in [
            "Remote",
            "Fully Remote",
            "100% Remote",
            "Remote (US)",
            "Remote - USA",
            "Remote/US",
            "US Remote",
            "Remote (US Only)",
            "Work From Home",
            "WFH",
        ]:
            row = self.base()
            row["workplace_type"] = wp
            self.assertEqual(check_record(row), [], f"Failed for workplace: {wp}")
            self.assertTrue(is_remote_workplace(wp))

    def test_hybrid_and_onsite_rejected(self):
        for wp in [
            "Hybrid",
            "Hybrid Remote",
            "On-site",
            "In-office",
            "Office",
            "Remote (2 days in office)",
            "In-person",
        ]:
            row = self.base()
            row["workplace_type"] = wp
            self.assertIn("work mode is not explicitly fully remote", check_record(row))
            self.assertFalse(is_remote_workplace(wp))

    # Country tests
    def test_country_variants(self):
        for country in [
            "United States",
            "USA",
            "US",
            "U.S.",
            "U.S.A.",
            "United States of America",
            "United States (Remote)",
            "San Francisco, CA, United States",
            "New York, NY, US",
            "Austin, TX, USA",
        ]:
            row = self.base()
            row["country"] = country
            self.assertEqual(check_record(row), [], f"Failed for country: {country}")
            self.assertTrue(is_us_country(country))

    def test_foreign_countries_rejected(self):
        for country in ["Canada", "UK", "Germany", "India", "Mauritius", "Cyprus", "Non-US", "Outside US"]:
            row = self.base()
            row["country"] = country
            self.assertIn("country is not United States", check_record(row))
            self.assertFalse(is_us_country(country))

    # Role domain tests
    def test_ai_role_domains(self):
        for domain in [
            "AI",
            "Artificial Intelligence",
            "Machine Learning",
            "ML",
            "Deep Learning",
            "Generative AI",
            "Gen-AI",
            "Gen AI",
            "LLM",
            "LLMs",
            "Large Language Model",
            "Large Language Models",
            "NLP",
            "Computer Vision",
            "MLOps",
            "Reinforcement Learning",
            "RL",
            "AI / ML",
            "Data Science / Machine Learning",
            "AI Engineer",
            "ML Engineer",
            "Machine Learning Engineer",
            "AI Research Scientist",
            "Agentic AI",
            "Applied AI Specialist",
        ]:
            self.assertTrue(is_ai_domain(domain), f"Should accept domain: {domain}")

    def test_non_ai_domains_rejected(self):
        for domain in ["Web Development", "Sales", "Accounting", "DevOps", "Technology", "Retail", "Email Marketing"]:
            self.assertFalse(is_ai_domain(domain), f"Should reject domain: {domain}")

    # Verified badge tests
    def test_verified_badge(self):
        for badge in ["yes", "y", "true", "1", "verified"]:
            row = self.base()
            row["verified_badge"] = badge
            self.assertEqual(check_record(row), [], f"Failed for badge: {badge}")

        for badge in ["no", "false", "0", "unknown", ""]:
            row = self.base()
            row["verified_badge"] = badge
            self.assertIn("LinkedIn verified badge not confirmed as yes", check_record(row))

    # Minutes posted tests
    def test_posted_minutes_ago(self):
        self.assertEqual(parse_minutes("0"), 0)
        self.assertEqual(parse_minutes("120"), 120)
        self.assertEqual(parse_minutes("1440"), 1440)
        self.assertEqual(parse_minutes("120.0"), 120)       # Excel export float support
        self.assertIsNone(parse_minutes("120.5"))          # Fractions rejected
        self.assertIsNone(parse_minutes("-5"))             # Negatives rejected
        self.assertIsNone(parse_minutes("abc"))
        self.assertIsNone(parse_minutes(""))
        # Natural language formats
        self.assertEqual(parse_minutes("Just now"), 0)
        self.assertEqual(parse_minutes("now"), 0)
        self.assertEqual(parse_minutes("30m"), 30)
        self.assertEqual(parse_minutes("45 mins ago"), 45)
        self.assertEqual(parse_minutes("1 hour ago"), 60)
        self.assertEqual(parse_minutes("2 hours ago"), 120)
        self.assertEqual(parse_minutes("2h"), 120)
        self.assertEqual(parse_minutes("1.5 hours ago"), 90)
        self.assertEqual(parse_minutes("1 day ago"), 1440)
        self.assertEqual(parse_minutes("2 days ago"), 2880)

        row = self.base()
        row["posted_minutes_ago"] = "1441"
        self.assertIn("job is older than 24 hours", check_record(row))

        row["posted_minutes_ago"] = "2 days ago"
        self.assertIn("job is older than 24 hours", check_record(row))

    # URL validation tests
    def test_linkedin_job_url_validation(self):
        valid_urls = [
            "https://www.linkedin.com/jobs/view/1234567890/",
            "https://www.linkedin.com/jobs/view/1234567890",
            "https://linkedin.com/jobs/view/senior-ai-engineer-12345",
            "https://in.linkedin.com/jobs/view/4483715326?refId=xyz",
        ]
        for url in valid_urls:
            self.assertTrue(is_linkedin_job_url(url), f"Should accept: {url}")

        invalid_urls = [
            "https://www.linkedin.com/jobs/view",        # Missing job ID
            "https://www.linkedin.com/jobs/view/",       # Missing job ID
            "https://www.linkedin.com/jobs/search/?keywords=ai",
            "http://www.linkedin.com/jobs/view/123",    # Not HTTPS
            "https://otherdomain.com/jobs/view/123",
            "",
        ]
        for url in invalid_urls:
            self.assertFalse(is_linkedin_job_url(url), f"Should reject: {url}")

    def test_external_apply_url_validation(self):
        valid_urls = [
            "https://jobs.lever.co/company/role-123",
            "https://boards.greenhouse.io/company/jobs/12345",
            "https://careers.example.com/apply",
        ]
        for url in valid_urls:
            self.assertTrue(is_external_url(url), f"Should accept: {url}")

        invalid_urls = [
            "https://www.linkedin.com/jobs/view/123",
            "https://lnkd.in/abc1234",                  # LinkedIn shortlink
            "https://sub.lnkd.in/abc1234",
            "https://licdn.com/image.png",
            "http://careers.example.com/apply",         # Not HTTPS
            "https://localhost/apply",                  # No dot in host
            "https://user:pass@example.com/apply",      # Credentials in URL
            "",
        ]
        for url in invalid_urls:
            self.assertFalse(is_external_url(url), f"Should reject: {url}")

    # Canonicalize and deduplication tests
    def test_canonicalize_url(self):
        self.assertEqual(
            canonicalize_url("https://www.linkedin.com/jobs/view/12345/?trackingId=abc"),
            "https://www.linkedin.com/jobs/view/12345",
        )
        self.assertEqual(
            canonicalize_url("https://jobs.example.com/apply/123/"),
            "https://jobs.example.com/apply/123",
        )

    def test_process_end_to_end_and_deduplication(self):
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "input.csv"
            out = Path(td) / "jobs.csv"
            rej = Path(td) / "rejected.csv"

            rows = [
                # Row 1: Valid
                self.base(),
                # Row 2: Duplicate of Row 1 with trailing slash and query param
                {
                    **self.base(),
                    "linkedin_job_url": "https://www.linkedin.com/jobs/view/1234567890?trackingId=999",
                    "external_apply_url": "https://jobs.example.com/role/123/",
                },
                # Row 3: Invalid (hybrid)
                {
                    **self.base(),
                    "linkedin_job_url": "https://www.linkedin.com/jobs/view/999999/",
                    "workplace_type": "Hybrid",
                },
            ]

            with inp.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)

            accepted, rejected = process(inp, out, rej)
            self.assertEqual(accepted, 1)
            self.assertEqual(rejected, 2)

            with out.open("r", encoding="utf-8-sig") as f:
                out_rows = list(csv.DictReader(f))
                self.assertEqual(len(out_rows), 1)
                self.assertEqual(out_rows[0]["filter_status"], "PASS")

            with rej.open("r", encoding="utf-8-sig") as f:
                rej_rows = list(csv.DictReader(f))
                self.assertEqual(len(rej_rows), 2)
                self.assertIn("duplicate job/application URL pair", rej_rows[0]["rejection_reasons"])
                self.assertIn("work mode is not explicitly fully remote", rej_rows[1]["rejection_reasons"])

    def test_read_input_header_tolerance_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "input.csv"
            # CSV with uppercase headers, spaces in header names, and trailing blank/comma rows
            content = "Job Title,Company,Country,Workplace Type,Seniority,Role Domain,LinkedIn Job URL,External Apply URL,Verified Badge,Posted Minutes Ago,Notes\n"
            content += "Senior AI Engineer,Co,US,Remote,Senior,AI,https://www.linkedin.com/jobs/view/1/,https://example.com/1,yes,10,\n"
            content += "\n"                                 # Blank row
            content += ",,,,,,,,,,\n"                       # Comma-only row
            content += "   ,   ,   ,   ,   ,   ,   ,   ,   ,   ,   \n"  # Spaces-only row
            inp.write_text(content, encoding="utf-8-sig")

            rows = read_input(inp)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["job_title"], "Senior AI Engineer")
            self.assertEqual(rows[0]["company"], "Co")
            self.assertEqual(rows[0]["external_apply_url"], "https://example.com/1")

    def test_normalize_header(self):
        self.assertEqual(normalize_header("Job Title"), "job_title")
        self.assertEqual(normalize_header("  JOB_TITLE  "), "job_title")
        self.assertEqual(normalize_header("Workplace-Type"), "workplace_type")
        self.assertEqual(normalize_header("LinkedIn Job URL"), "linkedin_job_url")

    def test_create_template_and_samples(self):
        with tempfile.TemporaryDirectory() as td:
            tpl = Path(td) / "template.csv"
            create_template(tpl)
            self.assertTrue(tpl.exists())
            with tpl.open("r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                headers = next(reader)
                self.assertIn("job_title", headers)
                self.assertIn("external_apply_url", headers)

            smp = Path(td) / "samples.csv"
            create_sample_file(smp)
            self.assertTrue(smp.exists())
            rows = read_input(smp)
            self.assertGreater(len(rows), 5)

    def test_main_cli_sample_flag(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "jobs.csv"
            rej = Path(td) / "rejected.csv"
            exit_code = main(["--sample", "--output", str(out), "--rejected", str(rej)])
            self.assertEqual(exit_code, 0)
            self.assertTrue(out.exists())
            self.assertTrue(rej.exists())
            with out.open("r", encoding="utf-8-sig") as f:
                out_rows = list(csv.DictReader(f))
                self.assertGreaterEqual(len(out_rows), 1)

    def test_blacklisted_companies(self):
        blocked_cases = [
            ("ClickUp", "Senior AI Engineer", "https://clickup.com/careers/1"),
            ("clickup", "AI Developer", ""),
            ("Jobright.ai", "Senior Data Scientist", "https://jobright.ai/apply"),
            ("Jobright", "AI Engineer", ""),
            ("Alignerr", "AI Specialist", "https://alignerr.com/jobs"),
            ("The Ladders", "Senior Machine Learning Engineer", "https://theladders.com/job/1"),
            ("Ladders", "Senior ML Engineer", ""),
            ("Rex.zone", "Senior AI Engineer", "https://rex.zone/job/1"),
            ("Rex Zone", "Lead AI Engineer", ""),
            ("Jobtogether", "Senior Python / AI Engineer", "https://jobtogether.com/1"),
            ("Jobgether", "Senior Engineer (Applied AI / ML)", "https://jobgether.com/offer/123"),
            ("Dayforce", "Senior AI Platform Engineer", "https://dayforcehcm.com/careers"),
            ("Micro1", "Senior AI Software Engineer", "https://micro1.ai/jobs"),
            ("micro-1", "Senior ML Engineer", ""),
            ("Data Annotation", "Senior AI Reviewer", "https://dataannotation.tech/jobs"),
            ("Dataannotation.tech", "AI Specialist", ""),
            ("data annoataion", "AI Evaluator", ""),
            ("Turing", "Senior ML Engineer", "https://turing.com/jobs"),
            ("Turing Enterprises", "Lead AI Engineer", ""),
            ("ChatGPT Jobs", "Senior AI Engineer", "https://chatgptjobs.io/job/1"),
            ("Synthesia", "Senior AI Engineer", "https://jobs.ashbyhq.com/synthesia/123"),
            ("Synthresis", "Senior AI Engineer", ""),
            ("Synthesis", "Senior AI Engineer", "https://synthesis.com/careers"),
        ]
        for company, title, url in blocked_cases:
            is_blocked, reason = is_blacklisted_company(company, title, url)
            self.assertTrue(is_blocked, f"Expected {company} / {url} to be blacklisted, but was not")

        allowed_cases = [
            ("Google", "Senior AI Engineer - ChatGPT Integration", "https://careers.google.com/1"),
            ("Microsoft", "Principal Applied Scientist", "https://careers.microsoft.com/2"),
            ("NVIDIA", "Senior Research Scientist, Deep Learning", "https://nvidia.com/3"),
            ("Databricks", "Staff Machine Learning Engineer", "https://databricks.com/4"),
            ("Anthropic", "Senior Staff AI Engineer", "https://jobs.lever.co/anthropic/5"),
        ]
        for company, title, url in allowed_cases:
            is_blocked, reason = is_blacklisted_company(company, title, url)
            self.assertFalse(is_blocked, f"Expected {company} to be allowed, but got blocked: {reason}")

    def test_check_record_rejects_blacklisted_companies(self):
        for bad_company in ["ClickUp", "jobright.ai", "Alignerr", "Ladders", "Rex.zone", "Jobtogether", "Dayforce", "micro1", "data annotation", "Turing", "ChatGPT Jobs", "Synthesia"]:
            row = self.base()
            row["company"] = bad_company
            reasons = check_record(row)
            self.assertTrue(
                any("blacklisted" in r.lower() for r in reasons),
                f"Expected check_record to reject company '{bad_company}', got reasons: {reasons}",
            )

    def test_director_vp_vice_rejected(self):
        bad_titles = [
            "Director of AI",
            "Senior Director, Data Science",
            "VP of Software Engineering",
            "Vice President of Technology",
            "Vice President, Artificial Intelligence",
            "Associate Director, Machine Learning",
            "VP, Product & AI",
        ]
        for title in bad_titles:
            row = self.base()
            row["job_title"] = title
            reasons = check_record(row)
            self.assertTrue(
                any("Director / VP / Vice" in r for r in reasons),
                f"Expected check_record to reject title '{title}', got reasons: {reasons}"
            )

    def test_senior_software_roles_accepted(self):
        good_roles = [
            ("Senior Software Engineer", "Software Engineering"),
            ("Staff Software Engineer", "Software"),
            ("Principal Software Engineer", "Software Development"),
            ("Lead Software Engineer", "Software"),
            ("Senior Backend Software Engineer", "Backend"),
            ("Senior Full Stack Software Engineer", "Full Stack"),
        ]
        for title, domain in good_roles:
            row = self.base()
            row["job_title"] = title
            row["role_domain"] = domain
            reasons = check_record(row)
            self.assertEqual(reasons, [], f"Expected '{title}' to pass, but got: {reasons}")


if __name__ == "__main__":
    unittest.main()
