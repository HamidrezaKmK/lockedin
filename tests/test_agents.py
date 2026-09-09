"""Agents: named CLI conversations a mark can be handed to, and the jobs that carry it.

Deterministic — no network, no model, no vendor CLI. The state machine, the one key space for
both mark surfaces, the uniform reply, and what the sync layer publishes about it.

Run: ``LOCKEDIN_HOME=/tmp/li_test uv run python -m unittest tests.test_agents -v``
"""
from __future__ import annotations

import base64
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

from lockedin import agents, bubbles, paths, presence, scientist_sync, service, talks

from tests.test_editing_logic import create_review_comment, temp_home
from tests.test_presence import temp_base


DECK = """\
<!-- slide: kind=setup, date=2026-09-06, v=1 -->
# What we're changing

Sample the noise level uniformly in log-SNR space.

---

<!-- slide: kind=derivation, date=2026-09-06, v=1 -->
# The residual term survives

Here I assume $w(\\lambda) \\to \\text{const}$, which kills the variance term.
"""

PAGE = "# Overview\n\nThe variance term vanishes in the limit. This is the whole argument.\n"


class AgentFixture(unittest.TestCase):
    def setUp(self):
        self.ctx = temp_home()
        self.home = self.ctx.__enter__()
        with paths.use_root(self.home):
            self.slug = bubbles.create_bubble("Agent demo")
            bubbles.approve_bubble(self.slug)
            bubbles.ensure_pages(self.slug)
            self.talk = talks.create_talk(self.slug, "Variance", date="2026-09-06", body=DECK)
            self.sync_id = talks.ensure_sync_ids(self.slug)[0]["sync_id"]
            self.page = bubbles.list_pages(self.slug)[0]["page_slug"]
        service.save_page(self.home, self.slug, self.page, PAGE)
        self.thread = create_review_comment(
            self.home, self.slug, self.page, "hamid", "why does it vanish?",
            {"quote": "The variance term vanishes in the limit", "start": PAGE.index("The variance")})
        with paths.use_root(self.home):
            self.note = talks.add_note(self.slug, self.talk, slide=1, kind="bad", author="hamid",
                                       quote="which kills the variance term", text="Not in the tail.")

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def register(self, name="Ada", conversation="conv-ada", worker_id="w1", **kw):
        with paths.use_root(self.home):
            return agents.register_agent(self.slug, name=name, role="reviewer", goal="be right",
                                         vendor="agy", conversation=conversation,
                                         worker_id=worker_id, **kw)

    @property
    def page_key(self):
        return f"page:{self.page}:{self.thread['id']}"

    @property
    def talk_key(self):
        return f"{self.sync_id}:{self.note['id']}"

    def make_marks(self, n):
        """``n`` distinct real marks on the talk deck, for cap tests that need to fill a queue."""
        keys = []
        with paths.use_root(self.home):
            for i in range(n):
                note = talks.add_note(self.slug, self.talk, slide=1, kind="bad", author="hamid",
                                      quote="which kills the variance term", text=f"note {i}")
                keys.append(f"{self.sync_id}:{note['id']}")
        return keys


class Registry(AgentFixture):
    def test_registering_the_same_conversation_twice_refreshes_rather_than_duplicates(self):
        first = self.register(name="Ada")
        second = self.register(name="Ada", role="skeptic") if False else None
        with paths.use_root(self.home):
            second = agents.register_agent(self.slug, name="Ada", role="skeptic", goal="g",
                                           vendor="agy", conversation="conv-ada", worker_id="w1")
            rows = agents.list_agents(self.slug)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "skeptic")

    def test_two_agents_cannot_share_a_name_on_one_bubble(self):
        self.register(name="Ada", conversation="c1")
        with self.assertRaises(agents.Conflict):
            self.register(name="ada", conversation="c2")

    def test_bad_input_is_refused_before_anything_is_written(self):
        with paths.use_root(self.home):
            with self.assertRaises(agents.AgentError):
                agents.register_agent(self.slug, name="", role="", goal="", vendor="agy",
                                      conversation="c", worker_id="w")
            with self.assertRaises(agents.AgentError):
                agents.register_agent(self.slug, name="Ada", role="", goal="", vendor="cursor",
                                      conversation="c", worker_id="w")
            self.assertFalse(paths.bubble_agents_path(self.slug).exists())

    def test_an_agent_can_be_found_by_name_or_id_and_retired(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            self.assertEqual(agents.get_agent(self.slug, "ada")["id"], agent["id"])
            job = agents.create_job(self.slug, agent_id="Ada", mark_key=self.page_key)
            agents.remove_agent(self.slug, agent["id"])
            self.assertEqual(agents.list_agents(self.slug), [])
            self.assertEqual(agents.get_job(self.slug, job["id"])["status"], "cancelled")

    def test_reset_forgets_the_conversation_and_marks_the_next_turn_fresh(self):
        agent = self.register()
        with paths.use_root(self.home):
            reset = agents.reset_agent(self.slug, agent["id"])
        self.assertEqual(reset["conversation"], "")
        self.assertTrue(reset["fresh"])


class Jobs(AgentFixture):
    def test_a_direct_message_is_a_real_turn_and_keeps_its_reply_with_the_agent(self):
        agent = self.register(name="Ada", registered_by="hamid")
        with paths.use_root(self.home):
            job = agents.create_message(self.slug, agent_id=agent["id"],
                                        text="Compare the two arguments.\nKeep the answer brief.",
                                        created_by="hamid")
            beat = agents.heartbeat(self.slug, worker_id="w1",
                                    agents=[{"id": agent["id"], "attached": False}],
                                    running_job_ids=[])
            agents.start_job(self.slug, job["id"], worker_id="w1")
            done = agents.reply_job(self.slug, job["id"], text="The second is tighter.")
            view = agents.overview(self.slug, viewer="hamid")
        self.assertEqual(job["kind"], "direct")
        self.assertIn("\n", job["instruction"])
        self.assertEqual(beat["jobs"][0]["mark"], {"surface": "direct"})
        self.assertEqual(done["result"]["reply_text"], "The second is tighter.")
        self.assertEqual(view["agents"][0]["turns_today"], 1)
        self.assertEqual(view["agents"][0]["messages"][0]["id"], job["id"])
        self.assertNotIn("", view["jobs"]["by_mark"])

    def test_an_empty_direct_message_is_refused(self):
        agent = self.register()
        with paths.use_root(self.home), self.assertRaises(agents.AgentError):
            agents.create_message(self.slug, agent_id=agent["id"], text="  ")

    def test_a_job_needs_a_real_mark_on_either_surface(self):
        agent = self.register()
        with paths.use_root(self.home):
            page_job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            talk_job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.talk_key)
            with self.assertRaises(agents.NotFound):
                agents.create_job(self.slug, agent_id=agent["id"], mark_key=f"page:{self.page}:nope")
            with self.assertRaises(agents.NotFound):
                agents.create_job(self.slug, agent_id=agent["id"], mark_key=f"{self.sync_id}:n99")
            with self.assertRaises(agents.AgentError):
                agents.create_job(self.slug, agent_id=agent["id"], mark_key="garbage")
        self.assertEqual((page_job["status"], talk_job["status"]), ("queued", "queued"))
        self.assertEqual(page_job["id"], "j-000001")
        self.assertEqual(talk_job["id"], "j-000002")

    def test_the_same_mark_is_not_queued_twice_for_one_agent(self):
        agent = self.register()
        with paths.use_root(self.home):
            agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            with self.assertRaises(agents.Conflict):
                agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)

    def test_one_agent_runs_one_turn_at_a_time_and_only_on_its_own_worker(self):
        agent = self.register(worker_id="w1")
        with paths.use_root(self.home):
            a = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            b = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.talk_key)
            with self.assertRaises(agents.Conflict):
                agents.start_job(self.slug, a["id"], worker_id="someone-else")
            agents.start_job(self.slug, a["id"], worker_id="w1")
            with self.assertRaises(agents.Conflict):
                agents.start_job(self.slug, b["id"], worker_id="w1")
            with self.assertRaises(agents.Conflict):
                agents.start_job(self.slug, a["id"], worker_id="w1")

    def test_reply_lands_in_the_page_thread_as_an_agent_turn_and_closes_the_job(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            done = agents.reply_job(self.slug, job["id"], text="I added the bound.")
            threads = bubbles.list_comments(self.slug, self.page)["threads"]
            # A late worker verdict never reopens a job the agent already answered.
            after = agents.finish_job(self.slug, job["id"], status="failed", exit_code=1,
                                      output_tail="boom")
            with self.assertRaises(agents.Conflict):
                agents.reply_job(self.slug, job["id"], text="again")
        self.assertEqual(done["status"], "done")
        self.assertTrue(done["result"]["confirmed"])
        last = threads[0]["messages"][-1]
        self.assertEqual((last["author"], last["body"], last.get("agent")), ("Ada", "I added the bound.", True))
        self.assertEqual(after["status"], "done")
        self.assertEqual(after["result"]["exit_code"], 1)

    def test_reply_lands_in_the_talk_note_too(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.talk_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.reply_job(self.slug, job["id"], text="Fixed slide 2.")
            note = talks.load_notes(self.slug, self.talk)["notes"][self.note["id"]]
        self.assertEqual([(m["author"], m["body"]) for m in note["messages"]],
                         [("hamid", "Not in the tail."), ("Ada", "Fixed slide 2.")])
        self.assertTrue(note["messages"][-1]["agent"])

    def test_an_agent_turn_is_credited_to_the_agent_acting_for_a_person(self):
        """One thread must read the same however the agent answered.

        The in-deck reply block produces "Ada on behalf of hamid" through ``talks.absorb_push``;
        the direct ``agent reply`` path has to match it rather than signing a bare name.
        """
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key,
                                    created_by="hamid")
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.reply_job(self.slug, job["id"], text="Done.", actor="alice")
            threads = bubbles.list_comments(self.slug, self.page)["threads"]
        # The caller's authenticated user wins over whoever assigned the mark.
        self.assertEqual(threads[0]["messages"][-1]["author"], "Ada on behalf of alice")

    def test_the_credit_falls_back_to_whoever_assigned_the_mark(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key,
                                    created_by="hamid")
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.reply_job(self.slug, job["id"], text="Done.")
            threads = bubbles.list_comments(self.slug, self.page)["threads"]
        self.assertEqual(threads[0]["messages"][-1]["author"], "Ada on behalf of hamid")

    def test_with_nobody_to_act_for_the_agent_signs_its_own_name(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.reply_job(self.slug, job["id"], text="Done.")
            threads = bubbles.list_comments(self.slug, self.page)["threads"]
        self.assertEqual(threads[0]["messages"][-1]["author"], "Ada")

    def test_a_declined_job_is_credited_the_same_way(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.fail_job(self.slug, job["id"], reason="the claim is false", actor="hamid")
            threads = bubbles.list_comments(self.slug, self.page)["threads"]
        self.assertEqual(threads[0]["messages"][-1]["author"], "Ada on behalf of hamid")

    def test_a_clean_exit_without_any_reply_is_a_failure_not_a_success(self):
        agent = self.register()
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            verdict = agents.finish_job(self.slug, job["id"], status="done", exit_code=0)
        self.assertEqual(verdict["status"], "failed")
        self.assertIn("without replying", verdict["error"])

    def test_the_legacy_deck_reply_block_closes_a_running_job(self):
        agent = self.register()
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.talk_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            base = talks.read_deck(self.slug, self.talk).encode()
        reply = ("\n<!-- lockedin-reply: " + self.note["id"] + " -->\nDone on slide 2.\n"
                 "<!-- /lockedin-reply -->\n")
        write = {"path": f"reports/talks/{self.sync_id}/slides.md",
                 "content_b64": base64.b64encode(base + reply.encode()).decode(),
                 "base_revision": scientist_sync.revision(base)}
        self.assertEqual(scientist_sync.apply_writes(self.home, self.slug, [write], actor="hamid")["conflicts"], [])
        with paths.use_root(self.home):
            verdict = agents.finish_job(self.slug, job["id"], status="done", exit_code=0)
        self.assertEqual(verdict["status"], "done")
        self.assertTrue(verdict["result"]["confirmed"])

    def test_fail_explains_itself_in_the_thread(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            failed = agents.fail_job(self.slug, job["id"], reason="the claim is false as stated")
            threads = bubbles.list_comments(self.slug, self.page)["threads"]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("false as stated", threads[0]["messages"][-1]["body"])

    def test_a_cancelled_page_job_can_still_be_replied_to_and_reports_late(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.cancel_job(self.slug, job["id"])
            done = agents.reply_job(self.slug, job["id"], text="Finished it anyway.")
            threads = bubbles.list_comments(self.slug, self.page)["threads"]
        self.assertEqual((done["status"], done["late"], done["late_from"]), ("done", True, "cancelled"))
        agent_turns = [m for m in threads[0]["messages"] if m.get("agent")]
        self.assertEqual(len(agent_turns), 1)
        self.assertEqual(agent_turns[0]["body"], "Finished it anyway.")

    def test_a_late_reply_preserves_the_worker_failure_that_preceded_it(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.finish_job(self.slug, job["id"], status="failed", exit_code=-15,
                              error="the agent could not reach its model service")
            done = agents.reply_job(self.slug, job["id"], text="Finished interactively.")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["late_from"], "failed")
        self.assertEqual(done["late_error"], "the agent could not reach its model service")

    def test_a_cancelled_talk_job_can_still_be_replied_to_and_reports_late(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.talk_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.cancel_job(self.slug, job["id"])
            done = agents.reply_job(self.slug, job["id"], text="Fixed slide 2 anyway.")
            note = talks.load_notes(self.slug, self.talk)["notes"][self.note["id"]]
        self.assertEqual((done["status"], done["late"], done["late_from"]), ("done", True, "cancelled"))
        agent_turns = [m for m in note["messages"] if m.get("agent")]
        self.assertEqual(len(agent_turns), 1)
        self.assertEqual(agent_turns[0]["body"], "Fixed slide 2 anyway.")

    def test_replying_twice_to_a_late_reply_refuses_and_leaves_one_agent_turn(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.cancel_job(self.slug, job["id"])
            agents.reply_job(self.slug, job["id"], text="Finished it anyway.")
            with self.assertRaises(agents.Conflict):
                agents.reply_job(self.slug, job["id"], text="again")
            threads = bubbles.list_comments(self.slug, self.page)["threads"]
        agent_turns = [m for m in threads[0]["messages"] if m.get("agent")]
        self.assertEqual(len(agent_turns), 1)

    def test_a_normally_completed_job_still_refuses_a_second_reply(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.reply_job(self.slug, job["id"], text="Done.")
            with self.assertRaises(agents.Conflict):
                agents.reply_job(self.slug, job["id"], text="Done again.")

    def test_fail_job_on_a_cancelled_job_posts_the_reason_and_reports_late(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.cancel_job(self.slug, job["id"])
            failed = agents.fail_job(self.slug, job["id"], reason="could not finish after cancel")
            threads = bubbles.list_comments(self.slug, self.page)["threads"]
        self.assertEqual((failed["status"], failed["late"], failed["late_from"]), ("failed", True, "cancelled"))
        self.assertIn("could not finish after cancel", threads[0]["messages"][-1]["body"])

    def test_an_ordinary_reply_to_a_running_job_reports_late_false(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            done = agents.reply_job(self.slug, job["id"], text="Done.")
        self.assertEqual((done["status"], done["late"], done["late_from"]), ("done", False, ""))

    def test_a_restarted_worker_fails_its_orphaned_turns_on_first_heartbeat(self):
        agent = self.register(worker_id="w1")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            beat = agents.heartbeat(self.slug, worker_id="w1",
                                    agents=[{"id": agent["id"], "attached": False}],
                                    running_job_ids=[])
            status = agents.get_job(self.slug, job["id"])
        self.assertEqual(beat["jobs"], [])
        self.assertEqual(status["status"], "failed")
        self.assertIn("restarted", status["error"])

    def test_heartbeat_returns_only_this_workers_queued_jobs_with_the_mark_briefing(self):
        ada = self.register(name="Ada", conversation="c1", worker_id="w1")
        bob = self.register(name="Bob", conversation="c2", worker_id="w2")
        with paths.use_root(self.home):
            mine = agents.create_job(self.slug, agent_id=ada["id"], mark_key=self.page_key,
                                     instruction="show the bound")
            agents.create_job(self.slug, agent_id=bob["id"], mark_key=self.talk_key)
            beat = agents.heartbeat(self.slug, worker_id="w1",
                                    agents=[{"id": ada["id"], "attached": True}], running_job_ids=[])
            overview = agents.overview(self.slug, workers=[
                {"worker_id": "w1", "state": "live"}])
        self.assertEqual([j["id"] for j in beat["jobs"]], [mine["id"]])
        job = beat["jobs"][0]
        self.assertEqual(job["agent"]["name"], "Ada")
        self.assertEqual(job["mark"]["surface"], "page")
        self.assertEqual(job["mark"]["quote"], "The variance term vanishes in the limit")
        self.assertEqual(job["mark"]["messages"][0]["said"], "why does it vanish?")
        self.assertEqual(job["mark"]["detail_path"], f"feedback/pages/{self.page}.json")
        statuses = {a["name"]: a["status"] for a in overview["agents"]}
        self.assertEqual(statuses, {"Ada": "attached", "Bob": "offline"})

    def test_cancel_and_reassign(self):
        ada = self.register(name="Ada", conversation="c1", worker_id="w1")
        bob = self.register(name="Bob", conversation="c2", worker_id="w1")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=ada["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            with self.assertRaises(agents.Conflict):
                agents.reassign_job(self.slug, job["id"], agent_id=bob["id"])
            cancelled = agents.cancel_job(self.slug, job["id"])
            beat = agents.heartbeat(self.slug, worker_id="w1", agents=[], running_job_ids=[job["id"]])
            moved = agents.reassign_job(self.slug, job["id"], agent_id="bob")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(beat["cancelled"], [job["id"]])
        self.assertEqual((moved["status"], moved["agent_name"], moved["attempts"]), ("queued", "Bob", 2))

    def test_repeated_cancel_and_assign_while_chat_is_open_only_offers_the_last_job(self):
        """Stress the observed register-before-exit flow. Cancelled queued attempts must never
        leak back into dispatch when the chat eventually closes and the worker polls again."""
        ada = self.register(name="Ada", conversation="c1", worker_id="w1")
        with paths.use_root(self.home):
            cancelled_ids = []
            for _ in range(20):
                old = agents.create_job(self.slug, agent_id=ada["id"], mark_key=self.page_key)
                cancelled_ids.append(old["id"])
                agents.cancel_job(self.slug, old["id"])
            final = agents.create_job(self.slug, agent_id=ada["id"], mark_key=self.page_key)
            beat = agents.heartbeat(self.slug, worker_id="w1",
                                    agents=[{"id": ada["id"], "attached": False}], running_job_ids=[])
            snapshot = agents.overview(self.slug)
        self.assertEqual([job["id"] for job in beat["jobs"]], [final["id"]])
        self.assertEqual([job["id"] for job in snapshot["jobs"]["open"]], [final["id"]])
        self.assertTrue(all(job["status"] == "cancelled" for job in snapshot["jobs"]["recent"]
                            if job["id"] in cancelled_ids))

    def test_heartbeat_progress_and_budget_reach_the_owner_overview(self):
        ada = self.register(name="Ada", conversation="c1", worker_id="w1")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=ada["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            activity = {"job_id": job["id"], "started_at": "2026-09-07T17:20:56Z",
                        "last_output_at": "2026-09-07T17:21:04Z", "output_bytes": 812,
                        "deadline_at": "2026-09-07T17:40:56Z"}
            agents.heartbeat(self.slug, worker_id="w1", running_job_ids=[job["id"]],
                             agents=[{"id": ada["id"], "attached": False,
                                      "budget": {"hour_used": 3, "hour_cap": 20,
                                                 "day_used": 7, "day_cap": 100},
                                      "confinement": "landlock", "turn_timeout_seconds": 1200,
                                      "activity": activity}])
            view = agents.overview(self.slug, workers=[{"worker_id": "w1", "state": "live"}])
        row = view["agents"][0]
        self.assertEqual(row["budget"]["hour_used"], 3)
        self.assertEqual(row["confinement"], "landlock")
        self.assertEqual(row["turn_timeout_seconds"], 1200)
        self.assertEqual(view["jobs"]["open"][0]["activity"], activity)

    def test_a_queued_job_whose_mark_was_deleted_is_cancelled_at_heartbeat(self):
        agent = self.register(worker_id="w1")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.talk_key)
            talks.delete_note(self.slug, self.talk, self.note["id"])
            beat = agents.heartbeat(self.slug, worker_id="w1", agents=[], running_job_ids=[])
            status = agents.get_job(self.slug, job["id"])
        self.assertEqual(beat["jobs"], [])
        self.assertEqual(status["status"], "cancelled")

    def test_requeue_sends_a_running_job_back_to_queued_with_attempts_incremented(self):
        agent = self.register(worker_id="w1")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            requeued = agents.requeue_job(self.slug, job["id"], reason="chat was open")
        self.assertEqual(requeued["status"], "queued")
        self.assertEqual(requeued["attempts"], 2)
        self.assertEqual(requeued["error"], "chat was open")
        self.assertEqual(requeued["started_at"], "")

    def test_requeue_refuses_a_job_that_is_not_running(self):
        agent = self.register(worker_id="w1")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            with self.assertRaises(agents.Conflict):
                agents.requeue_job(self.slug, job["id"])

    def test_requeue_fails_the_job_once_attempts_exceed_the_cap(self):
        agent = self.register(worker_id="w1")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            job_id = job["id"]
            for _ in range(agents.MAX_JOB_ATTEMPTS - 1):
                agents.start_job(self.slug, job_id, worker_id="w1")
                job = agents.requeue_job(self.slug, job_id, reason="chat was open")
                self.assertEqual(job["status"], "queued")
            agents.start_job(self.slug, job_id, worker_id="w1")
            final = agents.requeue_job(self.slug, job_id, reason="chat was open")
        self.assertEqual(final["status"], "failed")
        self.assertIn("chat", final["error"])

    def test_finish_job_routes_a_requeue_status_through(self):
        agent = self.register(worker_id="w1")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            result = agents.finish_job(self.slug, job["id"], status="requeue",
                                       error=agents.BUSY_ERROR)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["error"], agents.BUSY_ERROR)

    def test_overview_groups_jobs_by_mark_for_the_cards(self):
        agent = self.register(name="Ada")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            view = agents.overview(self.slug)
        self.assertEqual([j["id"] for j in view["jobs"]["by_mark"][self.page_key]], [job["id"]])
        self.assertEqual(view["agents"][0]["open_jobs"], 1)
        self.assertGreater(view["jobs_mtime"], 0)

    def test_retired_agent_name_is_preserved_in_job_history(self):
        agent = self.register(name="Ada", worker_id="w1")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            agents.start_job(self.slug, job["id"], worker_id="w1")
            agents.reply_job(self.slug, job["id"], text="All good.")
            # Now retire the agent
            agents.remove_agent(self.slug, agent["id"])
            # Job history should still show Ada's name
            fetched = agents.get_job(self.slug, job["id"])
            view = agents.overview(self.slug)
        self.assertEqual(fetched["agent_name"], "Ada")
        self.assertEqual(view["jobs"]["recent"][0]["agent_name"], "Ada")

    def test_reassign_job_updates_agent_name(self):
        ada = self.register(name="Ada", conversation="c1", worker_id="w1")
        bob = self.register(name="Bob", conversation="c2", worker_id="w1")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=ada["id"], mark_key=self.page_key)
            self.assertEqual(agents.get_job(self.slug, job["id"])["agent_name"], "Ada")
            reassigned = agents.reassign_job(self.slug, job["id"], agent_id=bob["id"])
        self.assertEqual(reassigned["agent_name"], "Bob")

    def test_old_jobs_without_agent_name_still_work(self):
        # Simulate a job created before denormalization (no agent_name key)
        agent = self.register(name="Ada", worker_id="w1")
        with paths.use_root(self.home):
            # Create a job normally
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
            # Simulate an old job by removing the agent_name key manually
            from lockedin import paths as paths_module
            jobs_data = agents._jobs(self.slug)
            jobs_data["jobs"][job["id"]].pop("agent_name", None)
            agents._save_jobs(self.slug, jobs_data)
            # Now get_job should still work and report Ada's name from the live registry
            fetched = agents.get_job(self.slug, job["id"])
        self.assertEqual(fetched["agent_name"], "Ada")


class SyncExport(AgentFixture):
    def test_raw_agent_files_are_never_exported_but_the_indexes_are(self):
        agent = self.register(name="Ada", worker_id="w1")
        with paths.use_root(self.home):
            job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key)
        files = scientist_sync._files(self.home, self.slug)
        self.assertFalse(any(rel.startswith("reports/agents/") for rel in files))
        agent_index = json.loads(files["indexes/agents.json"])
        job_index = json.loads(files["indexes/jobs.json"])
        marks = json.loads(files["indexes/marks.json"])
        router = json.loads(files["index.json"])
        self.assertEqual(agent_index["by_worker"], {"w1": [agent["id"]]})
        self.assertEqual(job_index["open"], [job["id"]])
        pointer = job_index["by_id"][job["id"]]["pointer"]
        for key in ("surface", "id", "page", "source_path", "detail_path"):
            self.assertEqual(pointer[key], marks["by_key"][self.page_key][key])
        self.assertEqual(router["counts"]["open_jobs"], 1)
        self.assertEqual(router["indexes"]["jobs"], "indexes/jobs.json")


class HttpFlow(unittest.TestCase):
    def _setup(self):
        from lockedin import auth, workspaces
        auth.create_user("alice", "pw12")
        token = auth.new_scientist_token("alice", "lockedin-scientist")
        personal = workspaces.ensure_personal("alice", auth.load_accounts()["alice"])
        home = workspaces.workspace_home(personal["id"])
        service.ensure_workspace(home)
        service.create_bubble(home, "Diffusion")
        service.approve_bubble(home, "diffusion")
        with paths.use_root(home):
            bubbles.ensure_pages("diffusion")
            page = bubbles.list_pages("diffusion")[0]["page_slug"]
        service.save_page(home, "diffusion", page, PAGE)
        thread = create_review_comment(home, "diffusion", page, "alice", "why?",
                                       {"quote": "The variance term", "start": PAGE.index("The variance")})
        return token, personal["id"], home, f"page:{page}:{thread['id']}"

    def test_register_assign_heartbeat_reply_end_to_end(self):
        from lockedin import server
        from lockedin.scientist_cli import SCIENTIST_CLIENT_VERSION
        with temp_base():
            token, workspace_id, home, key = self._setup()
            scientist = {"Authorization": "Bearer " + token, "X-LockedIn-Workspace": workspace_id,
                         "X-LockedIn-Scientist-Version": SCIENTIST_CLIENT_VERSION,
                         "X-LockedIn-Worker": "w1", "X-LockedIn-Worker-Label": "demo"}
            with TestClient(server.build_app(), base_url="https://testserver") as client:
                client.post("/api/login", json={"username": "alice", "password": "pw12"})
                registered = client.post("/api/scientist/v2/bubbles/diffusion/agents", headers=scientist,
                                         json={"name": "Ada", "role": "reviewer", "goal": "g",
                                               "vendor": "agy", "conversation": "c1", "worker_id": "w1"})
                self.assertEqual(registered.status_code, 200, registered.text)
                agent_id = registered.json()["agent"]["id"]
                # The registration itself carried presence headers, so the worker is live and
                # its agent is idle — visible on the presence heartbeat the page already makes.
                snap = client.post("/api/bubbles/diffusion/presence").json()
                self.assertEqual([(a["name"], a["status"]) for a in snap["agents"]], [("Ada", "idle")])

                created = client.post("/api/bubbles/diffusion/jobs",
                                      json={"agent_id": agent_id, "mark_key": key, "instruction": "bound"})
                self.assertEqual(created.status_code, 200, created.text)
                job_id = created.json()["job"]["id"]
                self.assertEqual(client.post("/api/bubbles/diffusion/jobs",
                                             json={"agent_id": agent_id, "mark_key": key}).status_code, 409)
                self.assertEqual(client.post("/api/bubbles/diffusion/jobs",
                                             json={"agent_id": agent_id, "mark_key": "page:x:y"}).status_code, 404)

                beat = client.post("/api/scientist/v2/bubbles/diffusion/agents/heartbeat", headers=scientist,
                                   json={"worker_id": "w1", "agents": [{"id": agent_id, "attached": False}],
                                         "running_job_ids": [],
                                         # Compatibility shape used by workers already running
                                         # before per-agent telemetry was introduced.
                                         "budget": {"hour_used": 4, "hour_cap": 20,
                                                    "day_used": 9, "day_cap": 100},
                                         "confinement": "landlock"})
                self.assertEqual(beat.status_code, 200, beat.text)
                self.assertEqual([j["id"] for j in beat.json()["jobs"]], [job_id])
                self.assertEqual(beat.json()["jobs"][0]["mark"]["quote"], "The variance term")

                started = client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{job_id}/start",
                                      headers=scientist, json={"worker_id": "w1"})
                self.assertEqual(started.status_code, 200, started.text)
                view = client.get("/api/bubbles/diffusion/agents").json()
                self.assertEqual(view["agents"][0]["status"], "working")
                self.assertEqual(view["agents"][0]["budget"]["hour_used"], 4)
                self.assertEqual(view["agents"][0]["confinement"], "landlock")
                self.assertEqual(view["jobs"]["by_mark"][key][0]["status"], "running")

                replied = client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{job_id}/reply",
                                      headers=scientist, json={"text": "Added the bound."})
                self.assertEqual(replied.status_code, 200, replied.text)
                self.assertEqual(replied.json()["job"]["status"], "done")
                page = key.split(":")[1]
                threads = client.get(f"/api/bubbles/diffusion/pages/{page}/comments").json()["threads"]
                self.assertEqual(threads[0]["messages"][-1]["author"], "Ada on behalf of alice")
                self.assertTrue(threads[0]["messages"][-1]["agent"])
                poll = client.get(f"/api/bubbles/diffusion/poll?page={page}").json()
                self.assertGreater(poll["jobs_mtime"], 0)

                gone = client.delete(f"/api/bubbles/diffusion/agents/{agent_id}")
                self.assertEqual(gone.status_code, 200)
                self.assertEqual(client.get("/api/bubbles/diffusion/agents").json()["agents"], [])

    def test_web_user_can_queue_a_direct_agent_message(self):
        from lockedin import server
        from lockedin.scientist_cli import SCIENTIST_CLIENT_VERSION
        with temp_base():
            token, workspace_id, home, key = self._setup()
            scientist = {"Authorization": "Bearer " + token,
                         "X-LockedIn-Workspace": workspace_id,
                         "X-LockedIn-Scientist-Version": SCIENTIST_CLIENT_VERSION,
                         "X-LockedIn-Worker": "w1", "X-LockedIn-Worker-Label": "demo"}
            with TestClient(server.build_app(), base_url="https://testserver") as client:
                client.post("/api/login", json={"username": "alice", "password": "pw12"})
                registered = client.post("/api/scientist/v2/bubbles/diffusion/agents", headers=scientist,
                                         json={"name": "Ada", "role": "reviewer", "goal": "g",
                                               "vendor": "agy", "conversation": "c1", "worker_id": "w1"})
                agent_id = registered.json()["agent"]["id"]
                created = client.post(f"/api/bubbles/diffusion/agents/{agent_id}/messages",
                                      json={"text": "Give me a one-line status."})
                self.assertEqual(created.status_code, 200, created.text)
                job = created.json()["job"]
                self.assertEqual((job["kind"], job["status"]), ("direct", "queued"))
                beat = client.post("/api/scientist/v2/bubbles/diffusion/agents/heartbeat",
                                   headers=scientist,
                                   json={"worker_id": "w1", "agents": [{"id": agent_id}],
                                         "running_job_ids": []}).json()
                self.assertEqual(beat["jobs"][0]["mark"], {"surface": "direct"})
                client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{job['id']}/start",
                            headers=scientist, json={"worker_id": "w1"})
                replied = client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{job['id']}/reply",
                                      headers=scientist, json={"text": "Everything is current."})
                self.assertEqual(replied.json()["job"]["result"]["reply_text"],
                                 "Everything is current.")
                messages = client.get("/api/bubbles/diffusion/agents").json()["agents"][0]["messages"]
                self.assertEqual(messages[0]["instruction"], "Give me a one-line status.")

    def test_a_requeue_result_posts_the_job_back_to_queued_over_http(self):
        from lockedin import server
        from lockedin.scientist_cli import SCIENTIST_CLIENT_VERSION
        with temp_base():
            token, workspace_id, home, key = self._setup()
            scientist = {"Authorization": "Bearer " + token, "X-LockedIn-Workspace": workspace_id,
                         "X-LockedIn-Scientist-Version": SCIENTIST_CLIENT_VERSION,
                         "X-LockedIn-Worker": "w1", "X-LockedIn-Worker-Label": "demo"}
            with TestClient(server.build_app(), base_url="https://testserver") as client:
                client.post("/api/login", json={"username": "alice", "password": "pw12"})
                registered = client.post("/api/scientist/v2/bubbles/diffusion/agents", headers=scientist,
                                         json={"name": "Ada", "role": "reviewer", "goal": "g",
                                               "vendor": "agy", "conversation": "c1", "worker_id": "w1"})
                agent_id = registered.json()["agent"]["id"]
                created = client.post("/api/bubbles/diffusion/jobs",
                                      json={"agent_id": agent_id, "mark_key": key})
                job_id = created.json()["job"]["id"]
                client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{job_id}/start",
                           headers=scientist, json={"worker_id": "w1"})

                result = client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{job_id}/result",
                                     headers=scientist,
                                     json={"status": "requeue", "exit_code": 1,
                                           "output_tail": "thread-store conflict",
                                           "error": "the agent's chat was open"})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["job"]["status"], "queued")
                self.assertEqual(result.json()["job"]["attempts"], 2)

    def test_the_new_routes_refuse_an_outdated_client(self):
        from lockedin import server
        with temp_base():
            token, workspace_id, home, key = self._setup()
            with TestClient(server.build_app(), base_url="https://testserver") as client:
                response = client.post("/api/scientist/v2/bubbles/diffusion/agents/heartbeat",
                                       headers={"Authorization": "Bearer " + token,
                                                "X-LockedIn-Workspace": workspace_id,
                                                "X-LockedIn-Scientist-Version": "2020.01.01.1"},
                                       json={"worker_id": "w1"})
            self.assertEqual(response.status_code, 426)


class Ownership(AgentFixture):
    """Agents belong to one person and are invisible to everyone else."""

    def test_two_owners_can_each_register_an_agent_called_ada(self):
        with paths.use_root(self.home):
            ada1 = agents.register_agent(self.slug, name="Ada", role="r", goal="g", vendor="agy",
                                         conversation="c1", worker_id="w1", registered_by="hamid")
            ada2 = agents.register_agent(self.slug, name="Ada", role="r", goal="g", vendor="agy",
                                         conversation="c2", worker_id="w2", registered_by="hamid2")
        self.assertNotEqual(ada1["id"], ada2["id"])
        self.assertNotEqual(ada1["key"], ada2["key"])
        self.assertTrue(ada1["key"].startswith("hamid-"))
        self.assertTrue(ada2["key"].startswith("hamid2-"))
        with paths.use_root(self.home):
            view1 = agents.overview(self.slug, viewer="hamid")
            view2 = agents.overview(self.slug, viewer="hamid2")
        self.assertEqual([a["id"] for a in view1["agents"]], [ada1["id"]])
        self.assertEqual([a["id"] for a in view2["agents"]], [ada2["id"]])

    def test_a_member_cannot_see_or_touch_another_members_agent_over_http(self):
        from lockedin import auth, workspaces
        from lockedin.scientist_cli import SCIENTIST_CLIENT_VERSION
        with temp_base():
            auth.create_user("hamid", "pw12")
            auth.create_user("hamid2", "pw12")
            auth.set_approved("hamid2", True)
            shared = workspaces.create("hamid", "Shared")
            workspaces.invite("hamid", shared["id"], "hamid2")
            home = workspaces.workspace_home(shared["id"])
            service.ensure_workspace(home)
            service.create_bubble(home, "Diffusion")
            service.approve_bubble(home, "diffusion")
            with paths.use_root(home):
                bubbles.ensure_pages("diffusion")
                page = bubbles.list_pages("diffusion")[0]["page_slug"]
            service.save_page(home, "diffusion", page, PAGE)
            thread = create_review_comment(home, "diffusion", page, "hamid", "why?",
                                           {"quote": "The variance term",
                                            "start": PAGE.index("The variance")})
            key = f"page:{page}:{thread['id']}"
            token1 = auth.new_scientist_token("hamid", "w1")
            token2 = auth.new_scientist_token("hamid2", "w2")
            ws = {"X-LockedIn-Workspace": shared["id"]}
            hdr1 = {**ws, "Authorization": "Bearer " + token1,
                   "X-LockedIn-Scientist-Version": SCIENTIST_CLIENT_VERSION}
            hdr2 = {**ws, "Authorization": "Bearer " + token2,
                   "X-LockedIn-Scientist-Version": SCIENTIST_CLIENT_VERSION}
            with TestClient(server_app(), base_url="https://testserver") as client:
                client.post("/api/login", json={"username": "hamid", "password": "pw12"})
                reg = client.post("/api/scientist/v2/bubbles/diffusion/agents", headers=hdr1,
                                  json={"name": "Ada", "role": "r", "goal": "g", "vendor": "agy",
                                        "conversation": "c1", "worker_id": "w1"})
                self.assertEqual(reg.status_code, 200, reg.text)
                agent_id = reg.json()["agent"]["id"]

                # Registered by hamid2, this shows up nowhere hamid can see.
                reg2 = client.post("/api/scientist/v2/bubbles/diffusion/agents", headers=hdr2,
                                   json={"name": "Ada", "role": "r", "goal": "g", "vendor": "agy",
                                         "conversation": "c2", "worker_id": "w2"})
                bob_id = reg2.json()["agent"]["id"]
                self.assertEqual(reg2.status_code, 200, reg2.text)
                self.assertNotEqual(agent_id, bob_id)

                client.post("/api/logout")
                client.post("/api/login", json={"username": "hamid2", "password": "pw12"})
                seen = client.get("/api/bubbles/diffusion/agents", headers=ws).json()["agents"]
                self.assertEqual([a["id"] for a in seen], [bob_id])
                scientist_seen = client.get("/api/scientist/v2/bubbles/diffusion/agents",
                                            headers=hdr2).json()["agents"]
                self.assertEqual([a["id"] for a in scientist_seen], [bob_id])

                # hamid2 cannot retire hamid's agent by id: 404, not leaked.
                self.assertEqual(client.delete(f"/api/bubbles/diffusion/agents/{agent_id}",
                                               headers=ws).status_code, 404)
                # ...nor assign to it: 403.
                self.assertEqual(client.post("/api/bubbles/diffusion/jobs", headers=ws,
                                             json={"agent_id": agent_id, "mark_key": key}).status_code, 403)
                # ...nor touch it over the Scientist API with hamid2's own token.
                self.assertEqual(client.post(f"/api/scientist/v2/bubbles/diffusion/agents/{agent_id}",
                                             headers=hdr2, json={"role": "hacked"}).status_code, 404)
                self.assertEqual(client.post(f"/api/scientist/v2/bubbles/diffusion/agents/{agent_id}/reset",
                                             headers=hdr2, json={}).status_code, 404)
                self.assertEqual(client.delete(f"/api/scientist/v2/bubbles/diffusion/agents/{agent_id}",
                                               headers=hdr2).status_code, 404)

                # hamid assigns a real job to their own agent, then hamid2 cannot cancel/reassign it.
                client.post("/api/logout")
                client.post("/api/login", json={"username": "hamid", "password": "pw12"})
                created = client.post("/api/bubbles/diffusion/jobs", headers=ws,
                                      json={"agent_id": agent_id, "mark_key": key})
                self.assertEqual(created.status_code, 200, created.text)
                job_id = created.json()["job"]["id"]
                client.post("/api/logout")
                client.post("/api/login", json={"username": "hamid2", "password": "pw12"})
                self.assertEqual(client.post(f"/api/bubbles/diffusion/jobs/{job_id}/cancel",
                                             headers=ws).status_code, 403)
                self.assertEqual(client.post(f"/api/bubbles/diffusion/jobs/{job_id}/reassign", headers=ws,
                                             json={"agent_id": bob_id}).status_code, 403)
                self.assertEqual(client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{job_id}/start",
                                             headers=hdr2, json={"worker_id": "w2"}).status_code, 403)
                self.assertEqual(client.get(f"/api/scientist/v2/bubbles/diffusion/jobs/{job_id}",
                                            headers=hdr2).status_code, 404)
                foreign_beat = client.post("/api/scientist/v2/bubbles/diffusion/agents/heartbeat",
                                           headers=hdr2,
                                           json={"worker_id": "w2",
                                                 "agents": [{"id": bob_id, "attached": False}],
                                                 "running_job_ids": []})
                self.assertEqual(foreign_beat.status_code, 200, foreign_beat.text)
                self.assertEqual(foreign_beat.json()["jobs"], [])
                self.assertEqual(client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{job_id}/reply",
                                             headers=hdr2, json={"text": "sneaky"}).status_code, 403)
                self.assertEqual(client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{job_id}/result",
                                             headers=hdr2,
                                             json={"status": "done", "exit_code": 0}).status_code, 403)
                self.assertEqual(client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{job_id}/fail",
                                             headers=hdr2, json={"reason": "nope"}).status_code, 403)

                # Give hamid2 an ordinary queued job too, then hammer both directions at once.
                # Every request carries a valid collaborator token and exact real ids; only
                # ownership should stand between it and the other person's Agy conversation.
                own2 = client.post("/api/bubbles/diffusion/jobs", headers=ws,
                                   json={"agent_id": bob_id, "mark_key": key})
                self.assertEqual(own2.status_code, 200, own2.text)
                job2 = own2.json()["job"]["id"]

                def cross_probe(attacker_headers, foreign_agent, foreign_job):
                    return (
                        client.post(f"/api/scientist/v2/bubbles/diffusion/agents/{foreign_agent}",
                                    headers=attacker_headers, json={"role": "hacked"}).status_code,
                        client.delete(f"/api/scientist/v2/bubbles/diffusion/agents/{foreign_agent}",
                                      headers=attacker_headers).status_code,
                        client.get(f"/api/scientist/v2/bubbles/diffusion/jobs/{foreign_job}",
                                   headers=attacker_headers).status_code,
                        client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{foreign_job}/start",
                                    headers=attacker_headers, json={"worker_id": "hostile"}).status_code,
                        client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{foreign_job}/reply",
                                    headers=attacker_headers, json={"text": "hostile"}).status_code,
                        client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{foreign_job}/result",
                                    headers=attacker_headers,
                                    json={"status": "done", "exit_code": 0}).status_code,
                        client.post(f"/api/scientist/v2/bubbles/diffusion/jobs/{foreign_job}/fail",
                                    headers=attacker_headers, json={"reason": "hostile"}).status_code,
                    )

                attacks = [(hdr2, agent_id, job_id), (hdr1, bob_id, job2)] * 40
                with ThreadPoolExecutor(max_workers=16) as pool:
                    results = list(pool.map(lambda args: cross_probe(*args), attacks))
                self.assertTrue(all(statuses == (404, 404, 404, 403, 403, 403, 403)
                                    for statuses in results), results)

                # The 560 denied requests must have changed nothing on either side.
                own_job1 = client.get(f"/api/scientist/v2/bubbles/diffusion/jobs/{job_id}",
                                      headers=hdr1)
                own_job2 = client.get(f"/api/scientist/v2/bubbles/diffusion/jobs/{job2}",
                                      headers=hdr2)
                self.assertEqual(own_job1.json()["job"]["status"], "queued")
                self.assertEqual(own_job2.json()["job"]["status"], "queued")
                own_agents1 = client.get("/api/scientist/v2/bubbles/diffusion/agents",
                                         headers=hdr1).json()["agents"]
                own_agents2 = client.get("/api/scientist/v2/bubbles/diffusion/agents",
                                         headers=hdr2).json()["agents"]
                self.assertEqual([(a["name"], a["role"]) for a in own_agents1], [("Ada", "r")])
                self.assertEqual([(a["name"], a["role"]) for a in own_agents2], [("Ada", "r")])


class Caps(AgentFixture):
    def test_the_11th_open_job_for_one_agent_raises_too_many(self):
        agent = self.register(registered_by="hamid")
        keys = self.make_marks(agents.MAX_OPEN_JOBS_PER_AGENT + 1)
        with paths.use_root(self.home):
            for key in keys[:agents.MAX_OPEN_JOBS_PER_AGENT]:
                agents.create_job(self.slug, agent_id=agent["id"], mark_key=key, created_by="hamid")
            with self.assertRaises(agents.TooMany):
                agents.create_job(self.slug, agent_id=agent["id"], mark_key=keys[-1], created_by="hamid")

    def test_the_41st_open_job_for_one_owner_raises_too_many(self):
        # Five agents so no single one's open-job cap (10) trips before the owner-wide one (40).
        with paths.use_root(self.home):
            targets = [agents.register_agent(
                self.slug, name=f"A{i}", role="r", goal="g", vendor="agy",
                conversation=f"c{i}", worker_id="w1", registered_by="hamid") for i in range(5)]
        keys = self.make_marks(agents.MAX_OPEN_JOBS_PER_OWNER + 1)
        with paths.use_root(self.home):
            for i, key in enumerate(keys[:agents.MAX_OPEN_JOBS_PER_OWNER]):
                agents.create_job(self.slug, agent_id=targets[i % 5]["id"], mark_key=key,
                                  created_by="hamid")
            with self.assertRaises(agents.TooMany):
                agents.create_job(self.slug, agent_id=targets[0]["id"], mark_key=keys[-1],
                                  created_by="hamid")

    def test_the_61st_job_created_in_an_hour_raises_too_many_but_old_ones_do_not_count(self):
        agent = self.register(registered_by="hamid")
        keys = self.make_marks(agents.MAX_JOBS_PER_USER_PER_HOUR + 1)
        with paths.use_root(self.home):
            data = agents._jobs(self.slug)
            # Backdate a pile of fake jobs well outside the hour window; they must not count.
            old_ts = "2000-01-01T00:00:00+00:00"
            for i in range(agents.MAX_JOBS_PER_USER_PER_HOUR):
                jid = f"j-old{i:04d}"
                data["jobs"][jid] = {"id": jid, "agent_id": agent["id"], "owner": "hamid",
                                     "mark_key": "irrelevant", "status": "cancelled",
                                     "created_at": old_ts, "finished_at": old_ts, "attempts": 1,
                                     "result": {}}
            agents._save_jobs(self.slug, data)
            # Fresh creations, cancelled right away so the open-job caps never trip — only the
            # hourly creation cap is under test here.
            for key in keys[:agents.MAX_JOBS_PER_USER_PER_HOUR]:
                job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=key, created_by="hamid")
                agents.cancel_job(self.slug, job["id"])
            with self.assertRaises(agents.TooMany):
                agents.create_job(self.slug, agent_id=agent["id"], mark_key=keys[-1], created_by="hamid")


class SecureMode(AgentFixture):
    def test_secure_mode_stops_heartbeat_start_and_create_and_overview_shows_stopped(self):
        from lockedin import auth
        with temp_base():
            auth.create_user("hamid", "pw12")
            agent = self.register(registered_by="hamid", worker_id="w1")
            with paths.use_root(self.home):
                job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.page_key,
                                        created_by="hamid")
                agents.start_job(self.slug, job["id"], worker_id="w1", actor="hamid")
                auth.set_secure_mode("hamid", True)
                beat = agents.heartbeat(self.slug, worker_id="w1", agents=[],
                                        running_job_ids=[job["id"]], secure=True)
                self.assertEqual(beat, {"jobs": [], "cancelled": [job["id"]], "secure_mode": True})
                with self.assertRaises(agents.Conflict):
                    agents.start_job(self.slug, job["id"], worker_id="w1", actor="hamid")
                with self.assertRaises(agents.Conflict):
                    agents.create_job(self.slug, agent_id=agent["id"], mark_key=self.talk_key,
                                      created_by="hamid")
                view = agents.overview(self.slug, viewer="hamid")
                self.assertTrue(view["secure_mode"])
                self.assertEqual([a["status"] for a in view["agents"]], ["stopped"])

                auth.set_secure_mode("hamid", False)
                beat_off = agents.heartbeat(self.slug, worker_id="w1", agents=[], running_job_ids=[])
                self.assertFalse(beat_off["secure_mode"])
                view_off = agents.overview(self.slug, viewer="hamid")
                self.assertFalse(view_off["secure_mode"])

    def test_secure_mode_settings_route_and_me_round_trip(self):
        from lockedin import auth
        with temp_base():
            auth.create_user("hamid", "pw12")
            token = auth.new_scientist_token("hamid", "demo worker")
            with TestClient(server_app(), base_url="https://testserver") as client:
                client.post("/api/login", json={"username": "hamid", "password": "pw12"})
                self.assertEqual(client.get("/api/settings/secure-mode").json(), {"enabled": False})
                self.assertFalse(client.get("/api/me").json()["secure_mode"])
                put = client.put("/api/settings/secure-mode", json={"enabled": True})
                self.assertEqual(put.json(), {"enabled": True, "revoked_clients": 1,
                                              "stopped_agents": 0, "cancelled_jobs": 0,
                                              "removed_workers": 0})
                self.assertTrue(client.get("/api/me").json()["secure_mode"])
                self.assertIsNone(auth.scientist_token_user(token))
                with self.assertRaisesRegex(ValueError, "Secure mode is on"):
                    auth.new_scientist_token("hamid", "attacker")
                refused = client.put("/api/settings/secure-mode",
                                     json={"enabled": False, "current_password": "wrong"})
                self.assertEqual(refused.status_code, 403)
                self.assertTrue(client.get("/api/settings/secure-mode").json()["enabled"])
                resumed = client.put("/api/settings/secure-mode",
                                     json={"enabled": False, "current_password": "pw12"})
                self.assertEqual(resumed.json(), {"enabled": False, "revoked_clients": 0,
                                                  "stopped_agents": 0, "cancelled_jobs": 0,
                                                  "removed_workers": 0})
                self.assertFalse(client.get("/api/settings/secure-mode").json()["enabled"])

    def test_stop_owner_retains_persona_and_conversation_until_authorized_heartbeat(self):
        agent = self.register(registered_by="hamid", worker_id="w1")
        with paths.use_root(self.home):
            before = agents.get_agent(self.slug, agent["id"], owner="hamid")
            result = agents.stop_owner(self.slug, "hamid")
            self.assertEqual(result, {"agents": 1, "cancelled_jobs": 0})
            stopped = agents.get_agent(self.slug, agent["id"], owner="hamid")
            for key in ("name", "role", "goal", "personality", "conversation", "worker_id"):
                self.assertEqual(stopped.get(key), before.get(key))
            self.assertTrue(stopped["revive_required"])
            agents.heartbeat(self.slug, worker_id="w1",
                             agents=[{"id": agent["id"], "attached": False}],
                             running_job_ids=[], owner="hamid")
            revived = agents.get_agent(self.slug, agent["id"], owner="hamid")
            self.assertNotIn("revive_required", revived)
            self.assertNotIn("stopped_at", revived)


class BudgetAndConfinement(AgentFixture):
    def test_budget_and_confinement_ride_the_heartbeat_and_show_on_the_row(self):
        agent = self.register(worker_id="w1")
        budget = {"hour_used": 3, "hour_cap": 10, "day_used": 5, "day_cap": 40,
                  "exhausted": False, "resumes_at": ""}
        with paths.use_root(self.home):
            agents.heartbeat(self.slug, worker_id="w1",
                             agents=[{"id": agent["id"], "attached": False,
                                     "budget": budget, "confinement": "landlock"}],
                             running_job_ids=[])
            view = agents.overview(self.slug)
        row = view["agents"][0]
        self.assertEqual(row["budget"], budget)
        self.assertEqual(row["confinement"], "landlock")

    def test_turns_last_hour_and_today_count_from_started_at(self):
        agent = self.register(worker_id="w1")
        keys = self.make_marks(3)
        with paths.use_root(self.home):
            for key in keys:
                job = agents.create_job(self.slug, agent_id=agent["id"], mark_key=key)
                agents.start_job(self.slug, job["id"], worker_id="w1")
                agents.reply_job(self.slug, job["id"], text="done")
            data = agents._jobs(self.slug)
            # Push one job's started_at outside the last hour but still within today.
            import datetime as _dt
            stale = next(iter(data["jobs"].values()))
            stale["started_at"] = (_dt.datetime.now(_dt.timezone.utc)
                                   - _dt.timedelta(hours=2)).isoformat(timespec="seconds")
            agents._save_jobs(self.slug, data)
            view = agents.overview(self.slug)
        row = view["agents"][0]
        self.assertEqual(row["turns_today"], 3)
        self.assertEqual(row["turns_last_hour"], 2)


class SyncExportOwners(AgentFixture):
    def test_indexed_context_and_files_are_scoped_to_one_owner(self):
        with paths.use_root(self.home):
            ada = agents.register_agent(self.slug, name="Ada", role="r", goal="g", vendor="agy",
                                        conversation="c1", worker_id="w1", registered_by="hamid")
            bob = agents.register_agent(self.slug, name="Bob", role="r", goal="g", vendor="agy",
                                        conversation="c2", worker_id="w2", registered_by="hamid2")
            agents.create_job(self.slug, agent_id=ada["id"], mark_key=self.page_key, created_by="hamid")
            agents.create_job(self.slug, agent_id=bob["id"], mark_key=self.talk_key, created_by="hamid2")

            ctx = scientist_sync._indexed_context(self.slug, owner="hamid")
            # Unfiltered (default) still returns everything, as pre-existing callers rely on.
            everyone = scientist_sync._indexed_context(self.slug)
        agent_index = json.loads(ctx["indexes/agents.json"])
        job_index = json.loads(ctx["indexes/jobs.json"])
        router = json.loads(ctx["index.json"])
        self.assertEqual(list(agent_index["by_id"]), [ada["id"]])
        self.assertEqual(router["counts"]["agents"], 1)
        self.assertTrue(all(job["owner"] == "hamid" for job in job_index["by_id"].values()))

        files = scientist_sync._files(self.home, self.slug, owner="hamid")
        agent_index2 = json.loads(files["indexes/agents.json"])
        self.assertEqual(list(agent_index2["by_id"]), [ada["id"]])

        self.assertEqual(sorted(json.loads(everyone["indexes/agents.json"])["by_id"]),
                         sorted([ada["id"], bob["id"]]))


def server_app():
    from lockedin import server
    return server.build_app()


if __name__ == "__main__":
    unittest.main()
