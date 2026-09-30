import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenda.cli import main
from agenda.identity import NICK_ACTOR, Actor, detect
from agenda.store import AgendaError
from agenda.timeutil import ago, parse_until

ROOT = Path(__file__).resolve().parent.parent
ORCH = Actor("11111111-aaaa", "claude", "merge-queue")
LANE = Actor("33333333-cccc", "claude", "ledger")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agenda.db")

    def tearDown(self):
        self.tmp.cleanup()

    def agenda(self, actor, *argv):
        if argv and argv[0] == "raise" and "--project" not in argv:
            argv = (*argv, "--project", "Clinic App", "--feature", "Merge queue", "--context", "Test context.")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*argv, "--db", self.db], actor=actor)
        return code, out.getvalue(), err.getvalue()

    def ok(self, actor, *argv):
        code, out, err = self.agenda(actor, *argv)
        self.assertEqual(code, 0, err or out)
        return out

    def test_raise_list_answer_inbox_close(self):
        self.ok(ORCH, "register")
        self.assertIn("Raised #1", self.ok(
            ORCH, "raise", "decision", "Merge webapp #4579?", "--summary", "Merge now or wait?",
            "--option", "Merge now", "--option", "Wait", "--recommend", "Wait", "--blocking",
            "--topic", "pr webapp#4579",
        ))
        listing = self.ok(NICK_ACTOR, "list")
        self.assertIn("#1    BLOCKING decision · merge-queue", listing)
        self.assertIn("Recommends: Wait", listing)

        self.ok(NICK_ACTOR, "answer", "#1", "wait", "for", "Sam")
        self.assertEqual(self.ok(ORCH, "inbox", "--count").strip(), "1")
        self.assertIn("Nick, <1m ago: wait for Sam", self.ok(ORCH, "inbox"))
        self.assertEqual(self.ok(ORCH, "inbox", "--count").strip(), "0")
        self.assertIn("Closed #1 (answered)", self.ok(ORCH, "close", "1", "waited"))
        self.assertIn("→ waited", self.ok(NICK_ACTOR, "history"))

    def test_a_lane_joining_an_open_topic_is_told_so(self):
        self.ok(ORCH, "raise", "fyi", "Runner kills", "--summary", "Runners die.", "--topic", "ci-runner-kill")
        out = self.ok(LANE, "raise", "fyi", "Runners", "--summary", "Mine died too.", "--topic", "CI-runner-kill")
        self.assertIn("Joined #1, owned by merge-queue", out)

    def test_lookalikes_are_refused_with_a_way_forward(self):
        self.ok(ORCH, "raise", "decision", "Merge #4579?", "--summary", "Now or later?")
        code, _, err = self.agenda(LANE, "raise", "question", "#4579 CodeRabbit", "--summary", "Accept the nit?")
        self.assertEqual(code, 1)
        self.assertIn("--into ID", err)
        self.assertIn("Joined #1", self.ok(LANE, "raise", "question", "#4579 CodeRabbit", "--summary", "Nit?", "--into", "1"))

    def test_sessions_cannot_use_nicks_verbs(self):
        self.ok(ORCH, "raise", "decision", "Deploy?", "--summary", "Say deploy.", "--recommend", "deploy")
        code, _, err = self.agenda(ORCH, "accept", "1")
        self.assertEqual(code, 1)
        self.assertIn("only Nick can accept", err)

    def test_accepting_several_reports_each(self):
        self.ok(ORCH, "raise", "decision", "One", "--summary", "First?", "--recommend", "yes")
        self.ok(ORCH, "raise", "fyi", "Two", "--summary", "Second.")
        code, out, err = self.agenda(NICK_ACTOR, "accept", "1", "2")
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "Accepted the recommendation on #1.")
        self.assertIn("agenda: #2: #2 has no recommendation", err)

    def test_default_with_deadline(self):
        out = self.ok(
            ORCH, "raise", "decision", "Scope", "--summary", "All four clinics or only Northside?",
            "--default", "all four", "--by", "2h",
        )
        self.assertIn("Raised #1", out)
        self.assertIn("If no answer by", self.ok(NICK_ACTOR, "list"))

    def test_json_output(self):
        self.ok(ORCH, "register")
        self.ok(NICK_ACTOR, "ask", "why", "is", "CI", "red?")
        items = json.loads(self.ok(ORCH, "owed", "--json"))
        self.assertEqual(items[0]["kind"], "ask")
        self.assertEqual(items[0]["owner_name"], "merge-queue")
        self.assertEqual(items[0]["messages"][0]["author"], "nick")

    def test_detail_from_file(self):
        detail = Path(self.tmp.name) / "detail.md"
        detail.write_text("| a | b |\n|---|---|\n", encoding="utf-8")
        self.ok(ORCH, "raise", "question", "Table?", "--summary", "Look at this.", "--detail-file", str(detail))
        self.assertIn("| a | b |", self.ok(NICK_ACTOR, "show", "1"))

    def test_whoami_and_sessions(self):
        self.assertEqual(self.ok(NICK_ACTOR, "whoami").strip(), "Nick.")
        self.ok(ORCH, "register")
        self.assertIn("merge-queue (orchestrator", self.ok(ORCH, "whoami"))
        self.assertIn("merge-queue", self.ok(NICK_ACTOR, "sessions"))

    def test_refusals_exit_1_with_a_message(self):
        code, out, err = self.agenda(NICK_ACTOR, "ack", "7")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("agenda: no item #7", err)

    def test_bad_item_number_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as exit_, redirect_stderr(io.StringIO()):
            main(["show", "abc", "--db", self.db], actor=NICK_ACTOR)
        self.assertEqual(exit_.exception.code, 2)

    def test_entry_point_runs_from_another_directory_as_nick(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "HERDR", "AGENDA"))}
        result = subprocess.run(
            [str(ROOT / "bin" / "agenda"), "whoami", "--db", self.db],
            cwd=self.tmp.name, capture_output=True, text=True, check=False, env={**env, "PYTHONPATH": ""},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Nick.")


class IdentityTest(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(detect({}).is_nick)
        self.assertTrue(detect({"AGENDA_SESSION": "nick"}).is_nick)
        claude = detect({"CLAUDE_CODE_SESSION_ID": "abc-123"})
        self.assertEqual((claude.id, claude.agent), ("abc-123", "claude"))
        explicit = detect({"AGENDA_SESSION": "s-1", "AGENDA_LABEL": "test lane"})
        self.assertEqual((explicit.id, explicit.label), ("s-1", "test lane"))


class TimeTest(unittest.TestCase):
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

    def test_durations(self):
        self.assertEqual(parse_until("30m", self.now), self.now + timedelta(minutes=30))
        self.assertEqual(parse_until("2h", self.now), self.now + timedelta(hours=2))
        self.assertEqual(parse_until("1 day", self.now), self.now + timedelta(days=1))

    def test_tomorrow_is_8am_local(self):
        until = parse_until("tomorrow", self.now).astimezone()
        self.assertEqual((until.hour, until.minute), (8, 0))
        self.assertEqual(until.date(), self.now.astimezone().date() + timedelta(days=1))

    def test_iso_and_garbage(self):
        self.assertEqual(parse_until("2026-10-01T09:00:00+00:00", self.now), datetime(2026, 10, 1, 9, tzinfo=timezone.utc))
        with self.assertRaises(AgendaError):
            parse_until("whenever", self.now)

    def test_ago(self):
        self.assertEqual(ago("2026-09-30T11:59:30Z", self.now), "<1m")
        self.assertEqual(ago("2026-09-30T11:48:00Z", self.now), "12m")
        self.assertEqual(ago("2026-09-30T09:00:00Z", self.now), "3h")
        self.assertEqual(ago("2026-09-27T12:00:00Z", self.now), "3d")


if __name__ == "__main__":
    unittest.main()
