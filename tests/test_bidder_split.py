import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import daily_pipeline as dp


class SplitTests(unittest.TestCase):
    def test_three_blanks_in_middle(self):
        urls = [f"https://x/{i}" for i in range(157)]
        out = dp.split_for_bidders(urls, 3)
        self.assertEqual(len(out), 160)
        self.assertEqual(out[79:82], ["", "", ""])
        self.assertEqual([u for u in out if u], urls)

    def test_even_and_tiny(self):
        self.assertEqual(dp.split_for_bidders(["a", "b", "c", "d"], 3), ["a", "b", "", "", "", "c", "d"])
        self.assertEqual(dp.split_for_bidders(["a"], 3), ["a"])
        self.assertEqual(dp.split_for_bidders([], 3), [])


class FakeResp:
    status = 200

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class PostFallbackTests(unittest.TestCase):
    def test_retries_without_blanks_when_rejected(self):
        sent = []

        def fake_urlopen(req, timeout=0):
            urls = json.loads(req.data)["urls"]
            sent.append(urls)
            if "" in urls:
                raise urllib.error.HTTPError(req.full_url, 400, "bad", {},
                                             io.BytesIO(b'{"error": "urls[1] is not an http(s) address"}'))
            return FakeResp('{"inserted": 2}')

        env = {"TRUESOFTS_INGEST_URL": "https://example.test/ingest", "TRUESOFTS_INGEST_TOKEN": "t" * 40}
        with mock.patch.dict("os.environ", env), mock.patch("urllib.request.urlopen", fake_urlopen):
            result = dp.post_urls({"urls": ["https://a", "", "https://b"]})
        self.assertEqual(result, {"inserted": 2})
        self.assertEqual(sent, [["https://a", "", "https://b"], ["https://a", "https://b"]])


if __name__ == "__main__":
    unittest.main()
