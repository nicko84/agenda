import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from agenda import mail, plan, voice
from agenda.identity import NICK_ACTOR, Actor
from agenda.store import Agenda
from agenda.web import Server

FILED = {"project": "Clinic App", "feature": "Merge queue", "context": "Test context."}
ORCH = Actor("11111111-aaaa", "claude", "merge-queue")


class AgentConfig(unittest.TestCase):
    def test_the_model_settings_that_would_break_calls(self):
        prompt = voice.agent_config()["conversation_config"]["agent"]["prompt"]
        self.assertEqual(prompt["llm"], "claude-sonnet-5")
        self.assertIsNone(prompt["temperature"])  # Sonnet 5 rejects sampling parameters
        self.assertEqual(prompt["backup_llm_config"], {"preference": "override", "order": ["claude-haiku-4-5"]})
        self.assertIn("{{agenda_overview}}", prompt["prompt"])

    def test_every_tool_the_page_implements_is_declared(self):
        page = (Path(voice.__file__).parent / "static" / "index.html").read_text()
        for tool in voice.agent_config()["conversation_config"]["agent"]["prompt"]["tools"]:
            self.assertIn(f"  {tool['name']}: safely(", page, tool["name"])

    def test_a_call_needs_a_token_and_keeps_no_audio(self):
        platform = voice.agent_config()["platform_settings"]
        self.assertTrue(platform["auth"]["enable_auth"])
        self.assertFalse(platform["privacy"]["record_voice"])


class WhatTheAgentReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agenda.db")
        self.agenda = Agenda(self.db)
        self.agenda.register(ORCH)
        self.item, _ = self.agenda.raise_item(
            ORCH, "decision", "Deploy eu-prod?", "Tonight or tomorrow morning?", recommendation="tomorrow",
            briefing="Q: what's in it? A: #4542 and #4543.", blocking=True, **FILED,
        )

    def tearDown(self):
        self.agenda.close()
        self.tmp.cleanup()

    def test_overview_and_opening(self):
        text = voice.overview(self.agenda)
        self.assertIn("1 items in 1 projects (1 blocking", text)
        self.assertIn("PROJECT 1: Clinic App (1 items)", text)
        self.assertIn("FEATURE: Merge queue (1)\n    Blocking:\n      #1 decision from merge-queue", text)
        self.assertIn("Context: Test context. Ask: Deploy eu-prod?", text)
        self.assertIn("Recommends: tomorrow.", text)
        self.assertEqual(voice.opening(self.agenda), "Hi Nick. 1 item today: Clinic App (1). 1 is blocking. Shall I go project by project, starting with Clinic App?")

    def test_an_owner_reply_to_nicks_question_is_in_the_overview(self):
        self.agenda.question(NICK_ACTOR, self.item.id, "what's in it?")
        self.agenda.inbox(ORCH)
        self.agenda.reply(ORCH, self.item.id, "Two PRs.")
        self.assertIn("Latest from merge-queue: Two PRs.", voice.overview(self.agenda))

    def test_item_text_carries_the_briefing_and_thread(self):
        self.agenda.question(NICK_ACTOR, self.item.id, "what's in it?")
        text = voice.item_text(self.agenda.get(self.item.id), self.agenda.now())
        self.assertIn("Briefing:\nQ: what's in it? A: #4542 and #4543.", text)
        self.assertIn("- Nick asked: what's in it?", text)

    def test_the_page_serves_it_and_start_fails_cleanly_without_a_key(self):
        server = Server(("127.0.0.1", 0), db=self.db, require_user=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/api/voice/items/1") as response:
                self.assertIn("Deploy eu-prod?", json.loads(response.read())["text"])
            with mock.patch.dict("os.environ", {"ELEVENLABS_API_KEY": ""}), self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + "/api/voice/start")
            self.assertEqual(err.exception.code, 503)
            err.exception.close()
        finally:
            server.shutdown()
            server.server_close()


class CallPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agenda = Agenda(Path(self.tmp.name) / "agenda.db")
        self.agenda.register(ORCH)

    def tearDown(self):
        self.agenda.close()
        self.tmp.cleanup()

    def raise_(self, project, feature, title, kind="decision", **kw):
        kw.setdefault("recommendation", "yes" if kind in ("decision", "question") else None)
        item, _ = self.agenda.raise_item(
            ORCH, kind, title, f"{title} summary", project=project, feature=feature, context="c", distinct=True, **kw
        )
        return item

    def test_project_then_feature_then_quick_before_discussion(self):
        a = self.raise_("Clinic App", "Releases", "Deploy eu")
        b = self.raise_("Member Hub", "Campaigns", "Pilot boards")
        c = self.raise_("Member Hub", "Campaigns", "Suggestions on", quick=True)
        d = self.raise_("Member Hub", "SEO", "Coverage list")
        e = self.raise_("Member Hub", "Campaigns", "Shipped K3", kind="fyi")
        projects = plan.plan(self.agenda)
        self.assertEqual([p.name for p in projects], ["Member Hub", "Clinic App"])  # busiest first
        self.assertEqual([f.name for f in projects[0].features], ["Campaigns", "SEO"])
        self.assertEqual(plan.ordered_ids(projects), [c.id, b.id, e.id, d.id, a.id])

    def test_blocking_projects_come_first_and_unfiled_last(self):
        self.raise_("Member Hub", "Campaigns", "One")
        self.raise_("Member Hub", "Campaigns", "Two")
        urgent = self.raise_("Clinic App", "Releases", "Hotfix", blocking=True)
        unfiled = self.agenda.ask(NICK_ACTOR, "anything else?")
        self.agenda.inbox(ORCH)
        self.agenda.reply(ORCH, unfiled.id, "Nothing else.")  # back in Nick's queue, still unfiled
        projects = plan.plan(self.agenda)
        self.assertEqual([p.name for p in projects], ["Clinic App", "Member Hub", plan.UNFILED])
        self.assertEqual(plan.ordered_ids(projects)[0], urgent.id)
        self.assertEqual(plan.ordered_ids(projects)[-1], unfiled.id)

    def test_screenshots_email_goes_only_to_nick_with_the_files(self):
        shot = Path(self.tmp.name) / "K2f.png"
        shot.write_bytes(b"png")
        item = self.raise_("Member Hub", "Campaigns", "Approve K2f", attach=[str(shot), "https://paper.design/b/54"])
        sent = []
        with mock.patch.dict("os.environ", {"AGENDA_MAIL_TO": "owner@example.com"}):
            self.assertEqual(mail.recipient(), "owner@example.com")
        message = mail.send_screenshots(self.agenda, [item.id], send=lambda *args: sent.append(args))
        subject, body, files = sent[0]
        self.assertIn(f"#{item.id}", subject)
        self.assertIn("https://paper.design/b/54", body)
        self.assertEqual([Path(f).name for f in files], ["1-K2f.png"])
        self.assertIn("Emailed 1 screenshot", message)
        self.assertIn("Screenshots emailed", self.agenda.get(item.id).messages[-1].body)
        bare = self.raise_("Member Hub", "SEO", "No visuals")
        with self.assertRaisesRegex(Exception, "ask the owner to attach"):
            mail.send_screenshots(self.agenda, [bare.id], send=lambda *args: None)
        with mock.patch.dict("os.environ", {"AGENDA_MAIL_TO": ""}), self.assertRaisesRegex(Exception, "AGENDA_MAIL_TO"):
            mail.recipient()

    def test_the_page_serves_stored_files_only(self):
        shot = Path(self.tmp.name) / "K2f.png"
        shot.write_bytes(b"\x89PNG")
        self.raise_("Member Hub", "Campaigns", "Approve K2f", attach=[str(shot)])
        server = Server(("127.0.0.1", 0), db=str(self.agenda.path), require_user=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/api/files/1") as response:
                self.assertEqual((response.read(), response.headers["Content-Type"]), (b"\x89PNG", "image/png"))
            with urllib.request.urlopen(base + "/api/state") as response:
                state = json.loads(response.read())
            self.assertEqual(state["plan"][0]["features"][0]["buckets"][0]["title"], "To discuss")
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + "/api/files/99")
            err.exception.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
