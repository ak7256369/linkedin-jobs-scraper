import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linkedin_ai_job_filter import DIRECTOR_VP_RE, check_record, non_engineering_reason, url_slug_text


class NonEngineeringTests(unittest.TestCase):
    def test_non_engineering_titles_rejected(self):
        for title in [
            "Senior Product Designer, Developer Platform",
            "Senior Talent Acquisition Partner - Robotics & ML",
            "Senior Account Manager, AI Solutions",
            "AI Specialist & Executive Program Lead",
            "Senior AI & Transformation Enablement Specialist",
            "Principal Product Specialist - AI Security",
            "AI Services Lead - Client Services",
            "Senior Sales Engineer, AI Platform",
        ]:
            self.assertTrue(non_engineering_reason(title), title)

    def test_engineering_titles_kept(self):
        for title in [
            "Senior AI Software Engineer, AI Enablement",
            "Senior Software Engineer",
            "Staff Machine Learning Engineer, Homefeed & Search Quality",
            "Sr Staff Systems Design Engineer",
            "Senior Engineering Manager, Backend/Platform",
            "Senior Marketing Data Engineer",
            "Sr. AI Solutions Architect",
            "Member of Technical Staff, AI",
            "Senior Product Engineer",
        ]:
            self.assertEqual(non_engineering_reason(title), "", title)

    def test_ai_team_manager_roles_kept(self):
        for title in [
            "Engineering Manager, AI Platform",
            "Manager, Machine Learning Engineering",
            "Staff Product Manager, AI Developer Platform",
            "Lead Product Manager, Agentic AI",
            "Principal Technical Program Manager - Computer Vision",
            "Senior Project Manager, AI Infrastructure",
        ]:
            self.assertEqual(non_engineering_reason(title), "", title)
        import resolve_ats_links
        self.assertTrue(resolve_ats_links.SENIOR_RE.search("Engineering Manager, AI Platform"))


class DirectorVpSlugTests(unittest.TestCase):
    def test_vp_in_workday_slug(self):
        for url in [
            "https://situsamc.wd1.myworkdayjobs.com/SitusAMC/job/Remote/Vice-President--Cloud-Platform-Engineer_JR03322",
            "https://situsamc.wd1.myworkdayjobs.com/SitusAMC/job/Remote/VP--Site-Reliability-Engineer_JR03313",
            "https://x.wd5.myworkdayjobs.com/c/job/Remote/Director-of-AI_R1",
        ]:
            self.assertTrue(DIRECTOR_VP_RE.search(url_slug_text(url)), url)

    def test_normal_urls_not_flagged(self):
        for url in [
            "https://jobs.ashbyhq.com/Vultr/215bd046-ace9-406b-baf7-5eb6c035bd8c",
            "https://job-boards.greenhouse.io/reddit/jobs/8205402",
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Senior-Software-Engineer-Aerial-Platform_JR2028821",
        ]:
            self.assertIsNone(DIRECTOR_VP_RE.search(url_slug_text(url)), url)

    def test_check_record_uses_url_slug(self):
        row = {
            "job_title": "Lead Site Reliability Engineer - Remote",
            "company": "SitusAMC",
            "country": "United States",
            "workplace_type": "Remote",
            "seniority": "Senior",
            "role_domain": "Software Engineering",
            "linkedin_job_url": "https://www.linkedin.com/jobs/view/4300000001",
            "external_apply_url": "https://situsamc.wd1.myworkdayjobs.com/SitusAMC/job/Remote/VP--Site-Reliability-Engineer_JR03313",
            "verified_badge": "yes",
            "posted_minutes_ago": "60",
        }
        self.assertTrue(any("Director / VP" in r for r in check_record(row)))


if __name__ == "__main__":
    unittest.main()
