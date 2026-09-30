import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agenda.identity import Actor
from agenda.store import Agenda
from agenda.web import Server

FILED = {"project": "Clinic App", "feature": "Merge queue", "context": "Test context."}
ORCH = Actor("11111111-aaaa", "claude", "merge-queue")
NICK_LOGIN = "owner@example.com"


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agenda.db")
        with Agenda(self.db) as agenda:
            agenda.register(ORCH)
            agenda.raise_item(ORCH, "decision", "Deploy eu-prod?", "Tonight or tomorrow?", recommendation="tomorrow", **FILED)
            agenda.raise_item(ORCH, "fyi", "Release live", "production has the release.", **FILED)
        self.server = Server(("127.0.0.1", 0), db=self.db, require_user=NICK_LOGIN)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, path, body=None, login=NICK_LOGIN):
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **({"Tailscale-User-Login": login} if login else {})},
        )
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                return response.status, json.loads(raw) if response.headers["Content-Type"].startswith("application/json") else raw
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_only_the_required_login_gets_in(self):
        self.assertEqual(self.call("/api/state", login=None)[0], 403)
        self.assertEqual(self.call("/api/state", login="someone@else.com")[0], 403)
        self.assertEqual(self.call("/api/items/1/accept", {}, login=None)[0], 403)
        status, page = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Agenda</title>", page)

    def test_state_and_nicks_actions(self):
        status, state = self.call("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual([i["title"] for i in state["queue"]], ["Deploy eu-prod?", "Release live"])
        self.assertEqual(state["orchestrators"], ["merge-queue"])

        status, after = self.call("/api/items/1/accept", {})
        self.assertEqual(status, 200)
        self.assertEqual(after["item"]["waiting_on"], "owner")
        self.assertEqual([i["id"] for i in after["owed"]], [1])

        self.assertEqual(self.call("/api/items/2/defer", {"until": "2h"})[1]["deferred"][0]["id"], 2)
        status, asked = self.call("/api/items/1/question", {"text": "what's in it?"})
        self.assertEqual((status, asked["item"]["messages"][-1]["kind"]), (200, "question"))
        status, asked = self.call("/api/ask", {"text": "is CI green?"})
        self.assertEqual((status, asked["item"]["owner_name"]), (200, "merge-queue"))

    def test_refusals_come_back_as_errors(self):
        status, body = self.call("/api/items/1/ack", {})
        self.assertEqual(status, 400)
        self.assertIn("still needs your answer", body["error"])
        self.assertEqual(self.call("/api/items/1/explode", {})[0], 404)
        self.assertEqual(self.call("/api/items/1/answer", {"text": ""})[0], 400)


if __name__ == "__main__":
    unittest.main()
