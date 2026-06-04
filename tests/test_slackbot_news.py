"""Deterministic tests for the Slack bot's premium news commands (`news` / `crawl`).

No network: the per-user HTTP client is stubbed, so we pin the formatting + the gating/empty
paths of ``_news_list`` and ``_news_crawl`` (which are thin glue over the lockedin /api/news
endpoints).
"""
from __future__ import annotations

import json
import unittest

from lockedin import slackbot


class _Resp:
    def __init__(self, status=200, payload=None, lines=None):
        self.status_code = status
        self._payload = payload
        self._lines = lines or []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")
        return self

    def json(self):
        return self._payload

    def iter_lines(self):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _HTTP:
    def __init__(self, get=None, stream=None):
        self._get, self._stream = get, stream

    def get(self, url, *a, **k):
        return self._get

    def stream(self, method, url, *a, **k):
        return self._stream


def _collect():
    msgs = []
    return msgs, msgs.append


class SlackNewsTest(unittest.TestCase):
    # ----- `news` --------------------------------------------------------------
    def test_news_list_groups_with_reasons(self):
        payload = {"bubbles": [{"slug": "diffusion-models", "name": "Diffusion Models"}],
                   "items": [{"title": "Fast samplers", "url": "https://arxiv.org/abs/1",
                              "bubble_slug": "diffusion-models", "source": "cs.LG",
                              "published": "2026-06-02", "reason": "improves sampling speed"}]}
        msgs, say = _collect()
        slackbot._news_list(_HTTP(get=_Resp(payload=payload)), say)
        out = "\n".join(msgs)
        self.assertIn("Diffusion Models", out)
        self.assertIn("Fast samplers", out)
        self.assertIn("improves sampling speed", out)   # the reason is shown

    def test_news_list_premium_gate(self):
        msgs, say = _collect()
        slackbot._news_list(_HTTP(get=_Resp(status=403)), say)
        self.assertIn("premium", "\n".join(msgs).lower())

    def test_news_list_empty(self):
        msgs, say = _collect()
        slackbot._news_list(_HTTP(get=_Resp(payload={"items": [], "bubbles": []})), say)
        self.assertIn("No news yet", "\n".join(msgs))

    # ----- `crawl` -------------------------------------------------------------
    def test_crawl_reports_found(self):
        lines = [
            'data: ' + json.dumps({"type": "item", "item": {
                "title": "New Diffusion Paper", "url": "https://arxiv.org/abs/2", "reason": "matches"}}),
            'data: ' + json.dumps({"type": "done", "added": 1, "stopped": False, "cost_usd": 0.03}),
        ]
        msgs, say = _collect()
        slackbot._news_crawl(_HTTP(stream=_Resp(lines=lines)), say)
        out = "\n".join(msgs)
        self.assertIn("Found 1 new paper", out)
        self.assertIn("New Diffusion Paper", out)
        self.assertIn("matches", out)

    def test_crawl_disabled_kill_switch(self):
        msgs, say = _collect()
        slackbot._news_crawl(_HTTP(stream=_Resp(status=503)), say)
        self.assertIn("disabled by the operator", "\n".join(msgs))

    def test_crawl_none_found(self):
        lines = ['data: ' + json.dumps({"type": "done", "added": 0, "stopped": "timeout"})]
        msgs, say = _collect()
        slackbot._news_crawl(_HTTP(stream=_Resp(lines=lines)), say)
        self.assertIn("no new papers", "\n".join(msgs).lower())


class _RouteHTTP:
    """Fake client routing by URL, recording the bodies sent to /api/news/chat."""
    def __init__(self):
        self.status = {"default_since": "2026-06-01", "today": "2026-06-04", "session": None}
        self.lines = []
        self.bodies = []

    def get(self, url, *a, **k):
        if url.endswith("/api/news/status"):
            return _Resp(payload=self.status)
        return _Resp(payload={"items": [], "bubbles": []})

    def stream(self, method, url, *a, **k):
        self.bodies.append(k.get("json"))
        return _Resp(lines=list(self.lines))

    def post(self, url, *a, **k):
        return _Resp(payload={"pointer": "2026-06-04"})


class SlackCrawlWizardTest(unittest.TestCase):
    def setUp(self):
        slackbot.URL = "http://x"
        self.uid = "U1"
        self.http = _RouteHTTP()
        self.http.lines = ['data: ' + json.dumps({"type": "item", "item": {
                               "title": "P", "url": "https://u", "reason": "r"}}),
                           'data: ' + json.dumps({"type": "done", "added": 1, "stopped": False})]
        slackbot._sessions[self.uid] = self.http
        slackbot._news_flow.pop(self.uid, None)
        slackbot._news_steer.discard(self.uid)
        self.msgs = []

    def tearDown(self):
        for d in (slackbot._sessions, slackbot._news_flow):
            d.pop(self.uid, None)
        slackbot._news_steer.discard(self.uid)

    def _send(self, t):
        self.msgs.clear()
        slackbot.handle({"user": self.uid, "text": t}, self.msgs.append)
        return "\n".join(self.msgs)

    def test_wizard_then_steer_then_accept(self):
        out = self._send("crawl")
        self.assertIn(self.uid, slackbot._news_flow)
        self.assertIn("2026-06-01", out)                 # default "from" shown
        self.assertIn("From", out)

        out = self._send("2026-05-01")                   # override from
        self.assertIn("2026-05-01", out)
        self.assertIn("to", out.lower())                 # now asks for "to"

        out = self._send("ok")                           # keep default "to" → runs the crawl
        self.assertIn("Found 1 new paper", out)
        self.assertIn(self.uid, slackbot._news_steer)    # now steering
        body = self.http.bodies[-1]
        self.assertEqual(body["since"], "2026-05-01")    # chosen range was sent
        self.assertEqual(body["until"], "2026-06-04")

        self._send("focus on diffusion")                 # steering follow-up
        self.assertEqual(self.http.bodies[-1]["message"], "focus on diffusion")

        out = self._send("accept")                       # save & advance
        self.assertIn("Saved", out)
        self.assertNotIn(self.uid, slackbot._news_steer)

    def test_wizard_cancel(self):
        self._send("crawl")
        out = self._send("cancel")
        self.assertIn("cancelled", out.lower())
        self.assertNotIn(self.uid, slackbot._news_flow)


if __name__ == "__main__":
    unittest.main()
