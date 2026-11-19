import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import daily_pipeline
import scrape_all


def card(job_id, title="Senior Software Engineer", company="Acme"):
    return (
        '<div class="job-search-card">'
        f'<h3 class="base-search-card__title">{title}</h3>'
        f'<h4 class="base-search-card__subtitle">{company}</h4>'
        '<span class="job-search-card__location">United States</span>'
        '<time>2 hours ago</time>'
        f'<a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/senior-swe-{job_id}?x=1"></a>'
        '</div>'
    )


def page(ids):
    return "".join(card(i) for i in ids)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_429(retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("u", 429, "Too Many Requests", headers, None)


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patches = [
            mock.patch.object(scrape_all, "_sleep", side_effect=self.sleeps.append),
            mock.patch.object(scrape_all, "_pace", scrape_all.PACE_MIN),
            mock.patch.object(scrape_all, "THROTTLE_HITS", 0),
            mock.patch.object(scrape_all, "FETCH_FAILURES", 0),
            mock.patch.object(scrape_all, "INCOMPLETE", []),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_429_backs_off_long_then_succeeds(self):
        responses = [http_429(), http_429("45"), FakeResp(b"<html>ok</html>")]

        def fake_urlopen(req, timeout=None):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            html = scrape_all.fetch_page("https://example.invalid", "ua")
        self.assertEqual(html, "<html>ok</html>")
        self.assertEqual(scrape_all.THROTTLE_HITS, 2)
        # First wait is the exponential base (>= ~17 s with jitter), second honours Retry-After: 45.
        self.assertGreaterEqual(self.sleeps[0], scrape_all.BACKOFF_BASE * 0.85)
        self.assertGreaterEqual(self.sleeps[1], 45 * 0.85)

    def test_throttled_keyword_is_resumed_not_dropped(self):
        # Offset 0 works; offset 10 is throttled on every attempt of the first pass,
        # then works in the resume pass. Final page is short (< 10 cards) -> end.
        state = {"throttled_left": scrape_all.MAX_429_ATTEMPTS, "starts": []}
        first = page(range(1000000, 1000010))
        second = page(range(2000000, 2000004))

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            state["starts"].append(url.split("start=")[1].split("&")[0])
            if "start=0" in url:
                return FakeResp(first.encode())
            if state["throttled_left"]:
                state["throttled_left"] -= 1
                raise http_429()
            return FakeResp(second.encode())

        with tempfile.TemporaryDirectory() as td, \
                mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            jobs = scrape_all.scrape_all_jobs(["senior software remote"], out_path=str(Path(td) / "o.csv"))

        # The resume pass restarted at offset 10, not 0, after a cool-down.
        self.assertEqual(state["starts"].count("0"), 1)
        self.assertIn(scrape_all.RESUME_COOLDOWN, self.sleeps)
        self.assertEqual(len(jobs), 14)
        self.assertEqual(scrape_all.FETCH_FAILURES, 0)
        self.assertEqual(scrape_all.INCOMPLETE, [])

    def test_permanently_throttled_keyword_is_reported(self):
        def fake_urlopen(req, timeout=None):
            raise http_429()

        with tempfile.TemporaryDirectory() as td, \
                mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            jobs = scrape_all.scrape_all_jobs(["kw"], out_path=str(Path(td) / "o.csv"))
        self.assertEqual(jobs, [])
        self.assertEqual(scrape_all.FETCH_FAILURES, 1)
        self.assertEqual(scrape_all.INCOMPLETE, [{"keyword": "kw", "offset": 0}])


class ShardTests(unittest.TestCase):
    def test_shards_cover_every_keyword_once(self):
        kws = [f"k{i}" for i in range(14)]
        parts = [daily_pipeline.shard_keywords(kws, i, 4) for i in range(4)]
        self.assertEqual(sorted(sum(parts, [])), sorted(kws))
        self.assertTrue(all(3 <= len(p) <= 4 for p in parts))
        with self.assertRaises(ValueError):
            daily_pipeline.shard_keywords(kws, 4, 4)

    def test_merge_dedupes_across_shards(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            row = lambda jid, t: {"job_title": t, "company": "C",
                                  "linkedin_job_url": f"https://www.linkedin.com/jobs/view/x-{jid}"}
            scrape_all.save_all_scraped([row(1234567, "A"), row(2345678, "B")], str(d / "scraped_0.csv"))
            scrape_all.save_all_scraped([row(2345678, "B dup"), row(3456789, "C")], str(d / "scraped_1.csv"))
            (d / "scraped_1.json").write_text(json.dumps(
                {"incomplete": [{"keyword": "kw", "offset": 40}], "throttle_hits": 3, "fetch_failures": 1}))
            jobs, meta = daily_pipeline.merge_shards(d)
        self.assertEqual([j["job_title"] for j in jobs], ["A", "B", "C"])
        self.assertEqual(meta["shards"], 2)
        self.assertEqual(meta["throttle_hits"], 3)
        self.assertEqual(meta["incomplete"], [{"keyword": "kw", "offset": 40}])


if __name__ == "__main__":
    unittest.main()
