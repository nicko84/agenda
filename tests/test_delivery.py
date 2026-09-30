import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agenda import deliver, hooks
from agenda.identity import NICK_ACTOR, Actor
from agenda.store import Agenda

FILED = {"project": "Clinic App", "feature": "Merge queue", "context": "Test context."}
ORCH = Actor("11111111-aaaa", "claude", "merge-queue", "w1:p1", "w1:t1")
BOX = "─" * 40


def screen(inner: str) -> str:
    return f"…transcript…\n{BOX}\n{inner}\n{BOX}\n  ⏵⏵ bypass permissions on"


class TypedText(unittest.TestCase):
    def test_suggestions_are_not_drafts_but_plain_text_is(self):
        self.assertEqual(deliver.typed_text(screen("❯ ")), "")
        self.assertEqual(deliver.typed_text(screen("❯ \x1b[0m\x1b[2mopen a draft PR\x1b[0m")), "")
        self.assertEqual(deliver.typed_text(screen("❯ half a thought")), "half a thought")
        self.assertEqual(deliver.typed_text(screen("❯ \x1b[38;2;255;255;255mtyped\x1b[0m")), "typed")
        self.assertIsNone(deliver.typed_text("no input box here"))


class AgendaCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agenda = Agenda(Path(self.tmp.name) / "agenda.db")
        self.agenda.register(ORCH)
        self.item, _ = self.agenda.raise_item(ORCH, "decision", "Deploy eu-prod?", "Tonight or tomorrow?", **FILED)

    def tearDown(self):
        self.agenda.close()
        self.tmp.cleanup()


class StopHook(AgendaCase):
    def payload(self, **extra):
        return {"session_id": ORCH.id, "hook_event_name": "Stop", **extra}

    def test_silent_without_news_or_for_unknown_sessions(self):
        self.assertIsNone(hooks.stop(self.payload(), self.agenda))
        self.assertIsNone(hooks.stop({"session_id": "never-used-the-agenda"}, self.agenda))
        self.assertFalse(self.agenda.knows("never-used-the-agenda"))  # a hook never registers anyone

    def test_news_keeps_the_session_going_once(self):
        self.agenda.answer(NICK_ACTOR, self.item.id, "tomorrow")
        result = hooks.stop(self.payload(), self.agenda)
        self.assertEqual(result["decision"], "block")
        self.assertIn('#1 decision "Deploy eu-prod?" — Nick: tomorrow', result["reason"])
        self.assertIsNone(hooks.stop(self.payload(), self.agenda))  # told once
        self.assertIsNone(hooks.stop(self.payload(stop_hook_active=True), self.agenda))
        self.agenda.question(NICK_ACTOR, self.item.id, "and eu first?")
        self.assertIn("Nick asks: and eu first?", hooks.stop(self.payload(), self.agenda)["reason"])

    def test_the_idle_nudger_skips_news_the_hook_already_gave(self):
        self.agenda.answer(NICK_ACTOR, self.item.id, "tomorrow")
        hooks.stop(self.payload(), self.agenda)
        self.assertEqual(self.agenda.unnudged(), [])


class SessionStartHook(AgendaCase):
    def test_reminds_a_session_of_its_open_items(self):
        context = hooks.session_start({"session_id": ORCH.id, "source": "compact"}, self.agenda)
        text = context["hookSpecificOutput"]["additionalContext"]
        self.assertIn('#1 decision "Deploy eu-prod?" — waiting on Nick', text)
        self.assertIsNone(hooks.session_start({"session_id": "stranger"}, self.agenda))

    def test_keeps_the_pane_current(self):
        with mock.patch.dict("os.environ", {"HERDR_PANE_ID": "w9:p9", "HERDR_TAB_ID": "w9:t9"}):
            hooks.session_start({"session_id": ORCH.id, "source": "resume"}, self.agenda)
        self.assertEqual(self.agenda.session(ORCH.id).pane_id, "w9:p9")


class Deliver(AgendaCase):
    def fake_herdr(self, *, status="idle", session=None, box="❯ "):
        sent = []

        def herdr(*args, timeout=5):
            if args[:2] == ("pane", "get"):
                return {"pane": {"agent_status": status, "agent_session": {"value": session or ORCH.id}}}
            if args[:2] == ("pane", "read"):
                return screen(box)
            if args[:2] == ("agent", "prompt"):
                sent.append((args[2], args[3]))
                return {"agent": {}}
            return None

        return herdr, sent

    def run_once(self, **kwargs):
        herdr, sent = self.fake_herdr(**kwargs)
        with mock.patch.object(deliver, "herdr", herdr):
            deliver.deliver_once(self.agenda, log=lambda *_: None)
        return sent

    def test_an_idle_session_is_nudged_once(self):
        self.agenda.answer(NICK_ACTOR, self.item.id, "tomorrow")
        sent = self.run_once()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "w1:p1")
        self.assertIn("Nick: tomorrow", sent[0][1])
        self.assertEqual(self.run_once(), [])

    def test_never_types_over_a_draft_a_busy_pane_or_someone_elses_session(self):
        self.agenda.answer(NICK_ACTOR, self.item.id, "tomorrow")
        self.assertEqual(self.run_once(box="❯ half a thought"), [])
        self.assertEqual(self.run_once(status="working"), [])
        self.assertEqual(self.run_once(session="another-session"), [])
        self.assertEqual(self.run_once(box="no box"), [])
        self.assertEqual(len(self.run_once()), 1)  # still owed, delivered once it's safe

    def test_a_suggestion_in_the_box_is_fine(self):
        self.agenda.answer(NICK_ACTOR, self.item.id, "tomorrow")
        self.assertEqual(len(self.run_once(box="❯ \x1b[2mopen a PR\x1b[0m")), 1)


if __name__ == "__main__":
    unittest.main()
