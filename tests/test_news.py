"""Deterministic regression tests for the interactive news crawl chat.

No network / no LLM: ``news._agent_events`` (the single subprocess seam) is monkeypatched with
canned Claude Code NDJSON events, so the stream → @@ITEM parse → live-save → dedup → session →
accept/discard pipeline is pinned exactly.

Run: ``LOCKEDIN_HOME=/tmp/li_news uv run python -m unittest tests.test_news -v``
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from lockedin import auth, bubbles, models, news, paths

ITEM1 = {"bubble": "Diffusion Models", "title": "Fast samplers",
         "url": "https://arxiv.org/abs/2401.00001", "published": "2026-06-01",
         "source": "arXiv cs.LG", "reason": "matches diffusion"}
ITEM2 = {"bubble": "No Such Bubble", "title": "Unrelated thing",
         "url": "https://example.com/post", "published": "2026-06-02",
         "source": "a blog", "reason": "no matching bubble"}


def canned(items, *, subtype="error_max_turns"):
    """Build a Claude Code stream-json event list: a tool_use + a text block with @@ITEM lines,
    then a final result event."""
    text = "".join(f"@@ITEM {json.dumps(it)}\n" for it in items) + "Found some; there may be more.\n"
    return [
        {"type": "system", "subtype": "init", "session_id": "test"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://arxiv.org/list/cs.LG/recent"}},
            {"type": "text", "text": text},
        ]}},
        {"type": "result", "subtype": subtype, "stop_reason": "max_turns",
         "is_error": subtype != "success", "total_cost_usd": 0.12},
    ]


class NewsChatTest(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("LOCKEDIN_HOME", "LOCKEDIN_NEWS_ENABLED")}
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["LOCKEDIN_HOME"] = self._tmp.name
        os.environ["LOCKEDIN_NEWS_ENABLED"] = "1"
        self._orig = news._agent_events
        # stub the active model so bubble-summary generation is fast + deterministic (no Ollama)
        self._orig_complete = models.complete
        self.complete_calls = []
        def fake_complete(home, msgs, system=None, temperature=0.2, *, claude_token=""):
            self.complete_calls.append(msgs)
            return "Scope summary: diffusion models, score-based generative modeling, samplers."
        models.complete = fake_complete
        auth.create_user("alice", "pw1234")
        self.home = paths.user_home("alice")
        with paths.use_root(self.home):
            bubbles.create_bubble("Diffusion Models")
            news.set_instructions([{"text": "monitor arXiv cs.LG"}])

    def tearDown(self):
        news._agent_events = self._orig
        models.complete = self._orig_complete
        self._tmp.cleanup()
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def _patch(self, events):
        news._agent_events = lambda cmd, timeout: iter(events)

    def _run(self, message, **kw):
        return list(news.chat_stream(self.home, message, **kw))

    def _items(self, include_dismissed=False):
        with paths.use_root(self.home):
            return news.list_items(include_dismissed=include_dismissed)

    def _ctx(self, fn):
        with paths.use_root(self.home):
            return fn()

    # ----- live save + streaming + bubble map + max-turns ---------------------
    def test_chat_saves_items_live(self):
        self._patch(canned([ITEM1, ITEM2]))
        evs = self._run("crawl", model="claude-haiku-4-5")
        items = [e for e in evs if e["type"] == "item"]
        done = [e for e in evs if e["type"] == "done"][0]
        self.assertEqual(len(items), 2)
        self.assertEqual(done["added"], 2)
        self.assertEqual(done["stopped"], "max_turns")            # surfaced, not silent
        self.assertTrue(any(e["type"] == "activity" for e in evs))  # tool_use → activity
        self.assertTrue(any(e["type"] == "delta" for e in evs))     # prose → delta
        stored = {i["title"]: i for i in self._items()}
        self.assertEqual(stored["Fast samplers"]["bubble_slug"], "diffusion-models")
        self.assertEqual(stored["Unrelated thing"]["bubble_slug"], "")  # unmatched

    # ----- dedup across a resumed turn ----------------------------------------
    def test_dedup_on_continue(self):
        self._patch(canned([ITEM1]))
        self._run("crawl")
        self._patch(canned([ITEM1]))            # same item again on "continue"
        evs = self._run("continue")
        self.assertEqual([e for e in evs if e["type"] == "item"], [])
        self.assertEqual(len(self._items()), 1)

    # ----- session tagging + discard (clean slate) ----------------------------
    def test_session_tagging_and_discard(self):
        self._patch(canned([ITEM1, ITEM2]))
        self._run("crawl")
        sess = self._ctx(news.get_session)
        self.assertIsNotNone(sess)
        self.assertTrue(all(i.get("session") == sess["uuid"] for i in self._items()))
        res = self._ctx(news.discard_session)
        self.assertEqual(res["removed"], 2)
        self.assertEqual(self._items(), [])
        self.assertEqual(self._ctx(lambda: news.load_items()["seen"]), [])  # keys freed
        self.assertIsNone(self._ctx(news.get_session))

    # ----- accept advances the GLOBAL pointer to the range end ----------------
    def test_accept_advances_pointer(self):
        self._patch(canned([ITEM1]))
        self._run("crawl")
        until = self._ctx(news.get_session)["until"]
        self._ctx(news.accept_session)
        self.assertEqual(self._ctx(news.get_pointer), until)          # global pointer, not per-instruction
        self.assertNotIn("last_checked", self._ctx(news.load_instructions)[0])  # instructions are plain text
        self.assertIsNone(self._ctx(news.get_session))
        # the advanced pointer seeds the next default crawl range
        self.assertEqual(self._ctx(news._default_since), until)

    # ----- typed "I'm happy" accepts WITHOUT invoking the agent ---------------
    def test_accept_shortcut_skips_agent(self):
        self._patch(canned([ITEM1]))
        self._run("crawl")                       # establishes an active session

        def boom(cmd, timeout):
            raise AssertionError("agent must not run on accept")
        news._agent_events = boom
        evs = self._run("I'm happy")
        done = [e for e in evs if e["type"] == "done"][0]
        self.assertTrue(done.get("accepted"))
        self.assertIsNone(self._ctx(news.get_session))

    # ----- custom range is honored + accept moves the global pointer to its end ----
    def test_custom_range(self):
        self._patch(canned([ITEM1]))
        self._run("crawl", since="2025-01-01", until="2025-02-01")
        sess = self._ctx(news.get_session)
        self.assertEqual(sess["since"], "2025-01-01")
        self.assertEqual(sess["until"], "2025-02-01")
        self._ctx(news.accept_session)
        self.assertEqual(self._ctx(news.get_pointer), "2025-02-01")

    # ----- instructions are plain text (no per-instruction date) --------------
    def test_instructions_are_plain_text(self):
        out = self._ctx(lambda: news.set_instructions(
            [{"text": "monitor arXiv cs.LG", "enabled": True, "last_checked": "2024-12-25"}]))
        self.assertNotIn("last_checked", out[0])        # stripped — instructions carry no date
        self.assertEqual(out[0]["text"], "monitor arXiv cs.LG")

    # ----- the full interaction (activities + items) is stored for history ----
    def test_interaction_recorded(self):
        self._patch([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://arxiv.org/list/cs.LG/recent"}},
                {"type": "text", "text": "@@ITEM " + json.dumps(ITEM1) + "\nFound 1.\n"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0},
        ])
        self._run("crawl")
        msgs = self._ctx(news.get_session)["messages"]
        roles = [m["role"] for m in msgs]
        self.assertIn("activity", roles)                                   # tool step recorded
        self.assertTrue(any("fetching" in m["text"] for m in msgs if m["role"] == "activity"))
        self.assertTrue(any(m["text"].startswith("➕ added") for m in msgs))  # item marker recorded
        self.assertIn("assistant", roles)                                  # prose recorded

    # ----- bubble scope summaries drive matching (not titles) -----------------
    def test_bubble_summary_in_prompt(self):
        cap = {}
        def fake(cmd, timeout):
            cap["cmd"] = cmd
            return iter([{"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0}])
        news._agent_events = fake
        self._run("")  # Go
        self.assertIn("Scope summary: diffusion models", cap["cmd"][2])  # summary, not just the title

    def test_bubble_summary_cached_and_refreshed(self):
        with paths.use_root(self.home):
            news.refresh_bubble_summaries(self.home)
            n1 = len(self.complete_calls)
            self.assertEqual(n1, 1)                          # one approved bubble summarized
            news.refresh_bubble_summaries(self.home)         # unchanged → served from cache
            self.assertEqual(len(self.complete_calls), n1)
            bubbles.approve_bubble("diffusion-models", "now focus on fast ODE samplers")  # content changed
            news.refresh_bubble_summaries(self.home)
            self.assertEqual(len(self.complete_calls), n1 + 1)  # fingerprint changed → regenerated

    # ----- safety net: items only in a json block / result payload still get added ----
    def test_reconcile_json_block(self):
        text = "I found one paper.\n```json\n" + json.dumps({"items": [ITEM1]}) + "\n```\n"
        self._patch([
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
            {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0},
        ])
        evs = self._run("crawl")
        self.assertEqual(len([e for e in evs if e["type"] == "item"]), 1)
        self.assertEqual(len(self._items()), 1)

    def test_reconcile_from_result_text(self):
        self._patch([
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "working…\n"}]}},
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "@@ITEM " + json.dumps(ITEM2), "total_cost_usd": 0.0},
        ])
        evs = self._run("crawl")
        self.assertEqual(len([e for e in evs if e["type"] == "item"]), 1)

    # ----- crawl conversation is archived to history on accept/discard --------
    def test_history_archived_on_accept(self):
        self._patch(canned([ITEM1]))
        self._run("crawl")
        self._ctx(news.accept_session)
        chats = self._ctx(news.list_chat_sessions)
        self.assertEqual(len(chats), 1)
        rec = self._ctx(lambda: news.get_chat_session(chats[0]["id"]))
        self.assertTrue(rec and rec["messages"])
        self.assertEqual(rec["n_items"], 1)
        self.assertTrue(self._ctx(lambda: news.delete_chat_session(chats[0]["id"])))
        self.assertEqual(self._ctx(news.list_chat_sessions), [])

    def test_history_archived_on_discard(self):
        self._patch(canned([ITEM1]))
        self._run("crawl")
        self._ctx(news.discard_session)
        self.assertEqual(len(self._ctx(news.list_chat_sessions)), 1)  # transcript kept
        self.assertEqual(self._items(), [])                            # items dropped

    # ----- token + cost are reported per turn and accumulated in the session --
    def test_done_reports_tokens_and_cost(self):
        self._patch([
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "@@ITEM " + json.dumps(ITEM1) + "\nok\n"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.05,
             "usage": {"input_tokens": 100, "output_tokens": 50,
                       "cache_creation_input_tokens": 10, "cache_read_input_tokens": 5}},
        ])
        done = [e for e in self._run("crawl") if e["type"] == "done"][0]
        self.assertEqual(done["tokens"], 165)
        self.assertEqual(done["cost_usd"], 0.05)
        self.assertEqual(self._ctx(news.get_session)["tokens"], 165)

    # ----- "Go!": an empty first message crawls from the saved instructions ----
    def test_go_without_typing(self):
        self._patch(canned([ITEM1]))
        evs = self._run("")                       # empty message = the Go! button
        self.assertEqual(len([e for e in evs if e["type"] == "item"]), 1)
        self.assertIsNotNone(self._ctx(news.get_session))   # a session was started

    # ----- running flag is set during a turn and cleared after (for reconnect) --
    def test_running_flag_lifecycle(self):
        seen = {}

        def fake(cmd, timeout):
            seen["mid"] = (news.get_session() or {}).get("running")   # set before the agent runs
            yield {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "@@ITEM " + json.dumps(ITEM1) + "\n"}]}}
            yield {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0}
        news._agent_events = fake
        list(news.chat_stream(self.home, "crawl"))
        self.assertTrue(seen.get("mid"))                              # running True mid-turn
        self.assertFalse(self._ctx(news.get_session)["running"])      # cleared when the turn ends

    # ----- gates ---------------------------------------------------------------
    def test_kill_switch_blocks_chat(self):
        os.environ["LOCKEDIN_NEWS_ENABLED"] = ""
        evs = self._run("crawl")
        self.assertEqual(evs[0]["type"], "error")

    def test_entitlement(self):
        auth.create_user("bob", "pw1234")
        auth.set_news_enabled("alice", True)
        self.assertTrue(auth.is_news_enabled("alice"))
        self.assertFalse(auth.is_news_enabled("bob"))

    def test_model_options(self):
        opts = news.model_options()
        self.assertTrue(opts and all("id" in m and "est_usd" in m for m in opts))


if __name__ == "__main__":
    unittest.main()
