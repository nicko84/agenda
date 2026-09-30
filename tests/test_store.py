import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenda.identity import NICK, NICK_ACTOR, Actor
from agenda.store import (
    ACKNOWLEDGED,
    AGENDA,
    ANSWERED,
    DEFAULTED,
    DONE,
    DROPPED,
    ELSEWHERE,
    MOOT,
    OWNER,
    WITHDRAWN,
    Agenda,
    AgendaError,
    LooksLikeDuplicate,
)

MERGE_QUEUE = Actor("11111111-aaaa", "claude", "merge-queue", "w1:p1", "w1:t1")
HUB = Actor("22222222-bbbb", "claude", "hub orchestrator", "w1:p1", "w1:t1")
LEDGER = Actor("33333333-cccc", "claude", "ledger", "w1:p1", "w1:t1")
CI = Actor("44444444-dddd", "codex", "ci", "w1:p1", "w1:t1")
FILED = {"project": "Clinic App", "feature": "Merge queue", "context": "Test context."}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class AgendaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agenda.db"
        self.clock = Clock()
        self.agenda = Agenda(self.path, clock=self.clock)
        self.agenda.register(MERGE_QUEUE)
        # Most tests aren't about filing; give every raise a project, feature and context unless it says otherwise.
        raise_item = self.agenda.raise_item

        def filed(*args, **kwargs):
            for key, value in FILED.items():
                kwargs.setdefault(key, value)
            return raise_item(*args, **kwargs)

        self.agenda.raise_item = filed

    def tearDown(self):
        self.agenda.close()
        self.tmp.cleanup()

    def decision(self, actor=MERGE_QUEUE, **overrides):
        fields = {"options": ["Merge now", "Wait for Sam"], "recommendation": "Wait for Sam"}
        fields.update(overrides)
        item, _ = self.agenda.raise_item(
            actor, "decision", "Merge webapp #4579?", "Merge the posting table now or wait for Sam?", **fields
        )
        return item

    def ids(self, items):
        return [item.id for item in items]

    def news(self, actor):
        return {item.id: [m.body for m in item.news_for(actor.id)] for item in self.agenda.inbox(actor)}


class RaiseAndAnswer(AgendaTest):
    def test_raised_item_waits_on_nick_and_is_owned_by_its_raiser(self):
        item = self.decision()
        self.assertEqual((item.waiting_on, item.owner, item.raised_by), (NICK, MERGE_QUEUE.id, MERGE_QUEUE.id))
        self.assertEqual(self.ids(self.agenda.queue()), [item.id])
        self.assertEqual(item.name_of(item.owner), "merge-queue")
        self.assertEqual(self.agenda.inbox(MERGE_QUEUE), [])

    def test_answer_reaches_the_owner_once(self):
        item = self.decision()
        self.agenda.answer(NICK_ACTOR, item.id, "wait for Sam")
        self.assertEqual(self.agenda.queue(), [])
        self.assertEqual(self.ids(self.agenda.owed(MERGE_QUEUE)), [item.id])
        self.assertEqual(self.agenda.inbox_count(MERGE_QUEUE), 1)
        self.assertEqual(self.news(MERGE_QUEUE), {item.id: ["wait for Sam"]})
        self.assertEqual(self.news(MERGE_QUEUE), {})
        self.assertEqual(self.ids(self.agenda.owed(MERGE_QUEUE)), [item.id])  # read, still owed

    def test_only_nick_answers_and_only_sessions_raise(self):
        item = self.decision()
        with self.assertRaisesRegex(AgendaError, "only Nick"):
            self.agenda.answer(MERGE_QUEUE, item.id, "approve my own thing")
        with self.assertRaisesRegex(AgendaError, "only Nick"):
            self.agenda.accept(LEDGER, item.id)
        with self.assertRaises(AgendaError):
            self.agenda.raise_item(NICK_ACTOR, "fyi", "Hi", "Nick doesn't raise.")

    def test_peek_leaves_news_unread(self):
        self.agenda.ask(NICK_ACTOR, "status of the ledger lane?")
        self.assertEqual(len(self.agenda.inbox(MERGE_QUEUE, mark=False)), 1)
        self.assertEqual(len(self.agenda.inbox(MERGE_QUEUE, mark=False)), 1)

    def test_bad_input(self):
        with self.assertRaises(AgendaError):
            self.agenda.raise_item(MERGE_QUEUE, "fyi", "Shipped", "Release is live.", blocking=True)
        with self.assertRaises(AgendaError):
            self.agenda.raise_item(MERGE_QUEUE, "ask", "Nope", "Only Nick asks.")
        with self.assertRaises(AgendaError):
            self.agenda.raise_item(MERGE_QUEUE, "decision", "Title", "   ")


class NickAsks(AgendaTest):
    def test_an_ask_goes_to_the_one_orchestrator(self):
        ask = self.agenda.ask(NICK_ACTOR, "why is CI red on main?")
        self.assertEqual((ask.owner, ask.waiting_on), (MERGE_QUEUE.id, OWNER))
        self.assertEqual(self.news(MERGE_QUEUE), {ask.id: ["why is CI red on main?"]})
        self.agenda.reply(MERGE_QUEUE, ask.id, "Flaky shard 2 test; rerun is green.")
        self.assertEqual(self.ids(self.agenda.queue()), [ask.id])
        self.assertEqual(self.agenda.ack(NICK_ACTOR, ask.id).outcome, ACKNOWLEDGED)

    def test_with_two_orchestrators_nick_says_which(self):
        self.agenda.register(HUB)
        with self.assertRaisesRegex(AgendaError, "more than one orchestrator"):
            self.agenda.ask(NICK_ACTOR, "anything?")
        ask = self.agenda.ask(NICK_ACTOR, "is the brand kit live?", to="Hub Orchestrator")
        self.assertEqual(ask.owner, HUB.id)
        self.assertEqual(self.agenda.inbox(MERGE_QUEUE), [])

    def test_owner_carrying_out_an_instruction_closes_it_done(self):
        ask = self.agenda.ask(NICK_ACTOR, "rerun the reshoot on #402", kind="instruction")
        self.agenda.inbox(MERGE_QUEUE)
        self.assertEqual(self.agenda.close_item(MERGE_QUEUE, ask.id, "rerun green").outcome, DONE)

    def test_asking_again_about_an_open_topic_continues_the_thread(self):
        first = self.agenda.ask(NICK_ACTOR, "is #4573 needed?", topics=["pr webapp#4573"])
        again = self.agenda.ask(NICK_ACTOR, "did we resolve the CI issues or not?", topics=["PR webapp#4573"])
        self.assertEqual(first.id, again.id)
        self.assertEqual(len(self.news(MERGE_QUEUE)[first.id]), 2)


class NothingIsLost(AgendaTest):
    def test_a_session_cannot_settle_on_news_it_has_not_read(self):
        item = self.decision()
        self.agenda.answer(NICK_ACTOR, item.id, "wait for Sam")
        self.agenda.inbox(MERGE_QUEUE)
        self.agenda.answer(NICK_ACTOR, item.id, "actually, merge now if CI is green")
        for act in (
            lambda: self.agenda.close_item(MERGE_QUEUE, item.id, "waited"),
            lambda: self.agenda.reply(MERGE_QUEUE, item.id, "ok, waiting"),
            lambda: self.agenda.update(MERGE_QUEUE, item.id, summary="new"),
        ):
            with self.assertRaisesRegex(AgendaError, "haven't read"):
                act()
        self.assertEqual(self.news(MERGE_QUEUE), {item.id: ["actually, merge now if CI is green"]})
        self.assertEqual(self.agenda.close_item(MERGE_QUEUE, item.id, "merged").outcome, ANSWERED)

    def test_closing_while_waiting_on_nick_needs_his_words(self):
        item = self.decision()
        with self.assertRaisesRegex(AgendaError, "how it was settled"):
            self.agenda.close_item(MERGE_QUEUE, item.id)
        closed = self.agenda.close_item(MERGE_QUEUE, item.id, "Nick said in the terminal: go with option 1")
        self.assertEqual((closed.outcome, closed.resolution), (ELSEWHERE, "Nick said in the terminal: go with option 1"))
        self.assertEqual(self.agenda.queue(), [])

    def test_state_survives_reopening(self):
        item = self.decision()
        self.agenda.answer(NICK_ACTOR, item.id, "wait")
        self.agenda.close()
        self.agenda = Agenda(self.path, clock=self.clock)
        self.assertEqual(self.agenda.get(item.id).waiting_on, OWNER)
        self.assertEqual(self.agenda.inbox_count(MERGE_QUEUE), 1)

    def test_mine_lists_everything_owned_or_watched_after_a_compaction(self):
        own = self.decision()
        theirs, _ = self.agenda.raise_item(LEDGER, "question", "Ledger floor?", "Floor aging at zero?")
        self.agenda.watch(MERGE_QUEUE, theirs.id)
        self.assertEqual(self.ids(self.agenda.mine(MERGE_QUEUE)), [own.id, theirs.id])


class LanesAndOrchestrators(AgendaTest):
    def test_a_lane_settles_only_its_own_items_and_an_orchestrator_any(self):
        lane_item, _ = self.agenda.raise_item(LEDGER, "question", "Ledger floor?", "Floor aging at zero?")
        orch_item = self.decision()
        with self.assertRaisesRegex(AgendaError, "belongs to merge-queue"):
            self.agenda.withdraw(LEDGER, orch_item.id, "moot")
        self.assertEqual(self.agenda.withdraw(MERGE_QUEUE, lane_item.id, "the posting table replaces it").outcome, MOOT)

    def test_handoff_moves_ownership_and_the_old_owner_keeps_watching(self):
        item = self.decision()
        self.agenda.touch(LEDGER)
        handed = self.agenda.handoff(MERGE_QUEUE, item.id, "ledger")
        self.assertEqual(handed.owner, LEDGER.id)
        self.assertIn(MERGE_QUEUE.id, handed.watchers)
        self.agenda.answer(NICK_ACTOR, item.id, "wait for Sam")
        self.assertIn("wait for Sam", self.news(LEDGER)[item.id])
        self.assertEqual(self.news(MERGE_QUEUE)[item.id], ["wait for Sam"])
        self.agenda.close_item(LEDGER, item.id, "waited; merged Thursday")
        self.assertEqual(self.news(MERGE_QUEUE)[item.id], ["Closed: waited; merged Thursday"])

    def test_a_note_adds_context_without_changing_whose_turn_it_is(self):
        item = self.decision()
        noted = self.agenda.note(LEDGER, item.id, "Sam is out until Thursday.")
        self.assertEqual(noted.waiting_on, NICK)
        self.assertIn(LEDGER.id, noted.watchers)
        self.assertEqual(self.news(MERGE_QUEUE), {item.id: ["Sam is out until Thursday."]})


class Dedupe(AgendaTest):
    def test_the_same_topic_from_five_lanes_is_one_item(self):
        lanes = [Actor(f"lane-{n}-0000", "claude", f"lane {n}") for n in range(5)]
        first, joined = self.agenda.raise_item(
            lanes[0], "question", "Who merges?", "May the orchestrator admin-merge?", topics=["merge authority"]
        )
        self.assertFalse(joined)
        for lane in lanes[1:]:
            again, joined = self.agenda.raise_item(
                lane, "question", "Merge rights", "Can I merge my own PR?", topics=["Merge  Authority"]
            )
            self.assertTrue(joined)
            self.assertEqual(again.id, first.id)
        self.assertEqual(len(self.agenda.queue()), 1)
        self.assertEqual(len(self.news(lanes[0])[first.id]), 4)  # the owner hears who else raised it

        self.agenda.answer(NICK_ACTOR, first.id, "the orchestrator merges; lanes don't")
        for lane in lanes:
            self.assertIn("the orchestrator merges; lanes don't", self.news(lane)[first.id])
        self.agenda.close_item(lanes[0], first.id, "told everyone")
        for lane in lanes[1:]:
            self.assertEqual(self.news(lane)[first.id], ["Closed: told everyone"])

    def test_the_owner_raising_again_is_a_progress_update(self):
        first, _ = self.agenda.raise_item(MERGE_QUEUE, "fyi", "Merge train", "3 of 24 merged.", topics=["merge train"])
        again, joined = self.agenda.raise_item(MERGE_QUEUE, "fyi", "Merge train", "9 of 24 merged.", topics=["merge train"])
        self.assertTrue(joined)
        self.assertEqual((again.id, again.summary), (first.id, "9 of 24 merged."))
        self.assertEqual(len(self.agenda.queue()), 1)

    def test_a_shared_number_is_refused_until_joined_or_marked_distinct(self):
        item = self.decision()
        with self.assertRaises(LooksLikeDuplicate) as refused:
            self.agenda.raise_item(LEDGER, "question", "PR 4579 CodeRabbit", "Accept CodeRabbit's note on PR 4579?")
        self.assertEqual(self.ids(refused.exception.candidates), [item.id])
        _, was_joined = self.agenda.raise_item(
            LEDGER, "question", "PR 4579 CodeRabbit", "Accept CodeRabbit's note?", into=item.id
        )
        self.assertTrue(was_joined)
        distinct, was_joined = self.agenda.raise_item(
            LEDGER, "question", "PR 4579 CodeRabbit", "Accept CodeRabbit's note?", distinct=True
        )
        self.assertFalse(was_joined)
        self.assertNotEqual(distinct.id, item.id)

    def test_a_near_identical_title_is_refused(self):
        self.agenda.raise_item(LEDGER, "task", "Search Console access for Acme Dental", "Add me as a reader.")
        with self.assertRaises(LooksLikeDuplicate):
            self.agenda.raise_item(CI, "task", "Search Console access Acme Dental", "Enable the API.")

    def test_topics_open_on_two_items_must_be_joined_explicitly(self):
        self.agenda.raise_item(MERGE_QUEUE, "fyi", "Runner kills", "Runners die.", topics=["ci-runner-kill"])
        self.agenda.raise_item(MERGE_QUEUE, "fyi", "Main is red", "Main red after 4568.", topics=["main red"])
        with self.assertRaisesRegex(AgendaError, "different items"):
            self.agenda.raise_item(CI, "fyi", "CI", "Everything's broken.", topics=["ci-runner-kill", "main red"])

    def test_a_topic_alias_added_later_catches_the_pr_raise(self):
        item, _ = self.agenda.raise_item(MERGE_QUEUE, "fyi", "Runner kills", "Runners die.", topics=["ci-runner-kill"])
        self.agenda.update(MERGE_QUEUE, item.id, topics=["pr webapp#4573"])
        joined, was_joined = self.agenda.raise_item(
            CI, "fyi", "Heap cap", "Heap cap fix is up.", topics=["pr webapp#4573"]
        )
        self.assertTrue(was_joined)
        self.assertEqual(joined.id, item.id)
        self.assertEqual(joined.topics, ["ci-runner-kill", "pr webapp#4573"])

    def test_a_settled_topic_is_not_asked_again(self):
        item, _ = self.agenda.raise_item(
            MERGE_QUEUE, "question", "Acme Dental leaving?", "Is Acme Dental leaving?", topics=["practice acme dental: leaving"]
        )
        self.agenda.answer(NICK_ACTOR, item.id, "Acme Dental is the Insights demo; not leaving. Locked.")
        self.agenda.inbox(MERGE_QUEUE)
        self.agenda.close_item(MERGE_QUEUE, item.id)
        with self.assertRaisesRegex(AgendaError, "settled in #1 .*Locked"):
            self.agenda.raise_item(LEDGER, "question", "Is Acme Dental leaving", "Are they leaving?", topics=["practice Acme Dental: leaving"])
        again, _ = self.agenda.raise_item(
            LEDGER, "question", "Acme Dental cancelled", "They sent a cancellation today; still not leaving?",
            topics=["practice acme dental: leaving"], reask=True, distinct=True,
        )
        self.assertIn("Asked before in #1", again.messages[0].body)

    def test_a_withdrawn_topic_can_be_raised_again(self):
        item, _ = self.agenda.raise_item(MERGE_QUEUE, "decision", "Deploy?", "Say deploy.", topics=["deploy production"])
        self.agenda.withdraw(MERGE_QUEUE, item.id, "Nick deployed himself")
        _, joined = self.agenda.raise_item(
            MERGE_QUEUE, "decision", "Deploy eu?", "Deploy eu-prod too?", topics=["deploy production"], distinct=True
        )
        self.assertFalse(joined)
        self.assertEqual(self.agenda.get(item.id).status, WITHDRAWN)


class Reading(AgendaTest):
    def test_an_orchestrator_reads_a_lane_item_with_show_before_settling_it(self):
        item, _ = self.agenda.raise_item(LEDGER, "question", "Who merges?", "Can lanes merge?", topics=["merge authority"])
        self.agenda.answer(NICK_ACTOR, item.id, "the merge queue does all merges")
        with self.assertRaisesRegex(AgendaError, "agenda show 1"):
            self.agenda.close_item(MERGE_QUEUE, item.id, "told the lanes")
        read = self.agenda.read(MERGE_QUEUE, item.id)
        self.assertIn(MERGE_QUEUE.id, self.agenda.get(item.id).watchers)
        self.assertEqual(read.messages[-1].body, "the merge queue does all merges")
        self.assertEqual(self.agenda.close_item(MERGE_QUEUE, item.id, "told the lanes").outcome, ANSWERED)
        self.assertEqual(self.news(LEDGER)[item.id], ["the merge queue does all merges", "Closed: told the lanes"])

    def test_nick_reading_changes_nothing(self):
        item = self.decision()
        self.agenda.read(NICK_ACTOR, item.id)
        self.assertEqual(set(self.agenda.get(item.id).watchers), {MERGE_QUEUE.id})


class SettledStaysSettled(AgendaTest):
    def settle_leaving(self):
        item, _ = self.agenda.raise_item(
            MERGE_QUEUE, "question", "Is Acme Dental leaving Clinic App?", "Is Acme Dental leaving?", topics=["practice acme dental: leaving"]
        )
        self.agenda.answer(NICK_ACTOR, item.id, "Not leaving. Locked.")
        self.agenda.inbox(MERGE_QUEUE)
        self.agenda.close_item(MERGE_QUEUE, item.id)
        return item

    def test_a_reworded_topic_is_still_refused(self):
        self.settle_leaving()
        with self.assertRaisesRegex(AgendaError, "already settled: #1 .*Not leaving"):
            self.agenda.raise_item(
                LEDGER, "question", "Acme Dental status", "Are they going?", topics=["practice Acme Dental Orthodontics: leaving"]
            )

    def test_a_different_question_passes_with_distinct(self):
        self.settle_leaving()
        _, joined = self.agenda.raise_item(
            LEDGER, "question", "Is Acme Dental leaving Clinic App?", "Their Insights login is broken.", topics=["access piq: acme dental"],
            distinct=True,
        )
        self.assertFalse(joined)

    def test_topic_aliases_can_be_added_to_a_closed_item_and_to_nicks_ask(self):
        settled = self.settle_leaving()
        self.agenda.update(MERGE_QUEUE, settled.id, topics=["practice acme: leaving"])
        with self.assertRaisesRegex(AgendaError, "settled in #1"):
            self.agenda.raise_item(CI, "question", "Acme", "Leaving?", topics=["practice acme: leaving"])
        ask = self.agenda.ask(NICK_ACTOR, "did we resolve CI?")
        self.agenda.inbox(MERGE_QUEUE)
        self.assertIn("pr webapp#4573", self.agenda.update(MERGE_QUEUE, ask.id, topics=["pr webapp#4573"]).topics)
        with self.assertRaisesRegex(AgendaError, "Nick's own"):
            self.agenda.update(MERGE_QUEUE, ask.id, summary="reworded")


class QuestionsAndBriefings(AgendaTest):
    def test_a_question_goes_to_the_owner_without_deciding_anything(self):
        item = self.decision()
        asked = self.agenda.question(NICK_ACTOR, item.id, "Did Sam sign off?")
        self.assertEqual(asked.waiting_on, OWNER)
        self.assertTrue(asked.messages[-1].is_question)
        self.assertFalse(asked.nick_has_answered)
        news = self.agenda.inbox(MERGE_QUEUE)
        self.assertTrue(news[0].news_for(MERGE_QUEUE.id)[0].is_question)

        self.agenda.reply(MERGE_QUEUE, item.id, "Yes, at 14:10.")
        back = self.agenda.get(item.id)
        self.assertEqual(back.waiting_on, NICK)
        self.assertTrue(back.answers_nicks_question)
        with self.assertRaisesRegex(AgendaError, "still needs your answer"):
            self.agenda.ack(NICK_ACTOR, item.id)  # a question isn't an answer
        with self.assertRaisesRegex(AgendaError, "only Nick"):
            self.agenda.question(MERGE_QUEUE, item.id, "asking myself")

    def test_a_reply_to_nicks_question_comes_second_in_his_queue(self):
        fyi, _ = self.agenda.raise_item(MERGE_QUEUE, "fyi", "Release live", "production has it.")
        item = self.decision()
        blocking, _ = self.agenda.raise_item(MERGE_QUEUE, "question", "Which DB?", "Stuck.", blocking=True)
        self.agenda.question(NICK_ACTOR, item.id, "Did Sam sign off?")
        self.agenda.inbox(MERGE_QUEUE)
        self.agenda.reply(MERGE_QUEUE, item.id, "Yes.")
        self.assertEqual(self.ids(self.agenda.queue()), [blocking.id, item.id, fyi.id])

    def test_closing_after_a_question_records_the_answer_not_the_question(self):
        item = self.decision()
        self.agenda.answer(NICK_ACTOR, item.id, "wait for Sam")
        self.agenda.question(NICK_ACTOR, item.id, "and tell me when he's back?")
        self.agenda.inbox(MERGE_QUEUE)
        self.assertEqual(self.agenda.close_item(MERGE_QUEUE, item.id).resolution, "wait for Sam")

    def test_briefings_are_kept_and_their_history_too(self):
        item, _ = self.agenda.raise_item(
            MERGE_QUEUE, "decision", "Deploy eu-prod?", "Tonight or tomorrow?", briefing="Q: what's in it? A: #4542"
        )
        self.assertEqual(item.briefing, "Q: what's in it? A: #4542")
        self.agenda.update(MERGE_QUEUE, item.id, briefing="Q: what's in it? A: #4542, #4543")
        self.assertIn("The briefing was: Q: what's in it? A: #4542", [m.body for m in self.agenda.get(item.id).messages])
        with self.assertRaisesRegex(AgendaError, "secret"):
            self.agenda.update(MERGE_QUEUE, item.id, briefing="key " + "re" + "_" + "c" * 26)


class Nudges(AgendaTest):
    def test_a_session_is_nudged_once_per_piece_of_news(self):
        item = self.decision()
        self.assertEqual(self.agenda.unnudged(), [])
        self.agenda.answer(NICK_ACTOR, item.id, "wait")
        [(session, upto)] = self.agenda.unnudged()
        self.assertEqual(session.id, MERGE_QUEUE.id)
        self.agenda.record_nudge(session.id, upto)
        self.assertEqual(self.agenda.unnudged(), [])
        self.agenda.question(NICK_ACTOR, item.id, "and Sam?")
        self.assertEqual([s.id for s, _ in self.agenda.unnudged()], [MERGE_QUEUE.id])
        self.agenda.inbox(MERGE_QUEUE)
        self.assertEqual(self.agenda.unnudged(), [])  # read news needs no nudge


class Filing(AgendaTest):
    def raw_raise(self, *args, **kwargs):
        return Agenda.raise_item(self.agenda, *args, **kwargs)

    def test_a_new_item_needs_project_feature_and_context(self):
        with self.assertRaisesRegex(AgendaError, "--project, --feature, --context"):
            self.raw_raise(MERGE_QUEUE, "decision", "Deploy?", "Tonight or tomorrow?")
        item, _ = self.raw_raise(
            MERGE_QUEUE, "decision", "Deploy?", "Tonight?", project="Clinic App", feature="Releases",
            context="The release went to production at 14:33 and is healthy.",
        )
        self.assertEqual((item.project, item.feature), ("Clinic App", "Releases"))

    def test_project_and_feature_names_keep_their_first_spelling(self):
        self.raw_raise(MERGE_QUEUE, "fyi", "One", "First.", project="Member Hub", feature="Campaigns", context="c")
        second, _ = self.raw_raise(LEDGER, "fyi", "Two", "Second thing.", project="member-hub", feature="campaigns ", context="c",
                                   distinct=True)
        self.assertEqual((second.project, second.feature), ("Member Hub", "Campaigns"))
        self.assertEqual([p.name for p, _ in self.agenda.projects()], ["Member Hub"])

    def test_quick_needs_a_recommendation(self):
        with self.assertRaisesRegex(AgendaError, "--quick"):
            self.agenda.raise_item(MERGE_QUEUE, "decision", "Deploy?", "Tonight?", quick=True)
        item = self.decision(quick=True)
        self.assertTrue(item.quick)

    def test_attachments_are_kept_with_the_agenda_or_linked(self):
        shot = Path(self.tmp.name) / "board.png"
        shot.write_bytes(b"\x89PNG fake")
        item = self.decision(attach=[str(shot), "https://app.paper.design/file/abc?board=54"], distinct=True)
        self.assertEqual([a.name for a in item.attachments], ["board.png", "https://app.paper.design/file/abc?board=54"])
        stored = Path(item.attachments[0].path)
        self.assertTrue(stored.is_file() and stored.is_relative_to(self.agenda.files_dir))
        self.assertTrue(item.attachments[0].is_image)
        shot.unlink()  # the session's scratch copy going away doesn't matter
        self.assertTrue(stored.is_file())
        with self.assertRaisesRegex(AgendaError, "no such file"):
            self.decision(attach=["/nope/missing.png"], distinct=True)

    def test_joining_fills_in_missing_filing(self):
        ask = self.agenda.ask(NICK_ACTOR, "is CI green?", topics=["ci main"])
        self.assertIsNone(ask.project)
        self.agenda.inbox(MERGE_QUEUE)
        filed = self.agenda.update(MERGE_QUEUE, ask.id, project="Clinic App", feature="CI", context="Main went red at 06:00.")
        self.assertEqual((filed.project, filed.feature), ("Clinic App", "CI"))
        with self.assertRaisesRegex(AgendaError, "Nick's own"):
            self.agenda.update(MERGE_QUEUE, ask.id, summary="reworded")

    def test_briefs_and_project_counts(self):
        self.decision()
        project = self.agenda.set_brief(MERGE_QUEUE, "clinic app", "Tonight's merge train is 24 PRs; 9 are in.")
        self.assertEqual((project.name, project.brief), ("Clinic App", "Tonight's merge train is 24 PRs; 9 are in."))
        self.assertEqual([(p.name, n) for p, n in self.agenda.projects()], [("Clinic App", 1)])
        with self.assertRaises(AgendaError):
            self.agenda.set_brief(NICK_ACTOR, "Clinic App", "Nick doesn't write briefs")

    def test_unfiled_items_get_one_request_to_their_owner(self):
        unfiled = self.agenda.ask(NICK_ACTOR, "status?")  # Nick's asks are filed by their owner too, but not requested
        item, _ = Agenda.raise_item(self.agenda, MERGE_QUEUE, "fyi", "x", "y", project="P", feature="F", context="c")
        self.agenda._db.execute("UPDATE items SET context = NULL WHERE id = ?", (item.id,))
        self.assertEqual(self.agenda.request_filing(), 1)
        self.assertEqual(self.agenda.request_filing(), 0)  # asked once
        news = self.news(MERGE_QUEUE)
        self.assertIn(f"agenda update {item.id} --project", news[item.id][0])
        self.assertNotIn("agenda update", " ".join(news.get(unfiled.id, [])))


class Topics(unittest.TestCase):
    def test_spacing_and_case_never_split_a_topic(self):
        from agenda.store import normalize_topic

        self.assertEqual(normalize_topic("Member Hub Support:W3"), normalize_topic("member hub support: w3"))
        self.assertEqual(normalize_topic("PR webapp #4579 "), "pr webapp#4579")


class Outcomes(AgendaTest):
    def test_a_default_applies_at_its_deadline_and_the_owner_is_told(self):
        item = self.decision(default="Wait for Sam", default_at=self.clock.now + timedelta(hours=2))
        self.clock.advance(hours=1)
        self.assertEqual(self.ids(self.agenda.queue()), [item.id])
        self.clock.advance(hours=2)
        self.assertEqual(self.agenda.queue(), [])
        news = self.news(MERGE_QUEUE)[item.id]
        self.assertIn("the default applies: Wait for Sam", news[0])
        self.assertEqual(self.agenda.get(item.id).messages[-1].author, AGENDA)
        self.assertEqual(self.agenda.close_item(MERGE_QUEUE, item.id).outcome, DEFAULTED)

    def test_defaults_need_a_deadline(self):
        with self.assertRaisesRegex(AgendaError, "deadline"):
            self.decision(default="Wait for Sam")
        with self.assertRaisesRegex(AgendaError, "fyi"):
            self.agenda.raise_item(
                MERGE_QUEUE, "fyi", "x", "y", default="z", default_at=self.clock.now + timedelta(hours=1)
            )

    def test_a_task_is_marked_done_not_acked(self):
        task, _ = self.agenda.raise_item(HUB, "task", "Paste the GoCardless secret", "Needs you at a computer.")
        with self.assertRaisesRegex(AgendaError, "use done"):
            self.agenda.ack(NICK_ACTOR, task.id)
        done = self.agenda.done(NICK_ACTOR, task.id, "it's in Doppler as GOCARDLESS_WEBHOOK_SECRET")
        self.assertEqual(done.waiting_on, OWNER)
        self.assertEqual(self.news(HUB)[task.id], ["Done. it's in Doppler as GOCARDLESS_WEBHOOK_SECRET"])

    def test_ack_needs_an_answer_for_decisions_but_not_fyis(self):
        decision = self.decision()
        with self.assertRaises(AgendaError):
            self.agenda.ack(NICK_ACTOR, decision.id)
        fyi, _ = self.agenda.raise_item(MERGE_QUEUE, "fyi", "Shipped", "Release is live.")
        self.assertEqual(self.agenda.ack(NICK_ACTOR, fyi.id).outcome, ACKNOWLEDGED)
        self.assertEqual(self.agenda.inbox(MERGE_QUEUE), [])  # an ack wakes nobody

    def test_accept_uses_the_recommendation(self):
        item = self.agenda.accept(NICK_ACTOR, self.decision().id)
        self.assertEqual(item.messages[-1].body, "Go with your recommendation: Wait for Sam")
        bare = self.decision(recommendation=None, topics=["other"], distinct=True)
        with self.assertRaises(AgendaError):
            self.agenda.accept(NICK_ACTOR, bare.id)

    def test_dropping_an_unread_ask_is_silent_but_a_raised_item_tells_its_owner(self):
        ask = self.agenda.ask(NICK_ACTOR, "never mind this")
        self.assertEqual(self.agenda.drop(NICK_ACTOR, ask.id).outcome, DROPPED)
        self.assertEqual(self.agenda.inbox(MERGE_QUEUE), [])
        item = self.decision()
        self.agenda.drop(NICK_ACTOR, item.id, "we're not doing this")
        self.assertEqual(self.news(MERGE_QUEUE), {item.id: ["Dropped. we're not doing this"]})

    def test_defer_hides_until_the_time_passes_and_a_reply_resurfaces_it(self):
        item = self.decision()
        self.agenda.defer(NICK_ACTOR, item.id, self.clock.now + timedelta(hours=1))
        self.assertEqual((self.agenda.queue(), self.agenda.deferred_count()), ([], 1))
        self.agenda.reply(MERGE_QUEUE, item.id, "Sam is back early.")
        self.assertEqual(self.ids(self.agenda.queue()), [item.id])

    def test_rewording_keeps_the_old_wording(self):
        item = self.decision()
        updated = self.agenda.update(MERGE_QUEUE, item.id, summary="Merge #4579 once Sam approves?")
        self.assertTrue(any("The summary was: Merge the posting table now" in m.body for m in updated.messages))
        detailed = self.agenda.update(MERGE_QUEUE, item.id, detail="v2")
        self.agenda.update(MERGE_QUEUE, item.id, detail="v3")
        self.assertIn("The detail was: v2", [m.body for m in self.agenda.get(detailed.id).messages])
        self.assertEqual(self.agenda.inbox(MERGE_QUEUE), [])  # bookkeeping wakes nobody


class Sessions(AgendaTest):
    def test_labels_that_repeat_resolve_to_the_live_session_or_refuse(self):
        old = Actor("55555555-eeee", "claude", "lane-2c")
        self.agenda.touch(old)
        self.clock.advance(hours=3)
        new = Actor("66666666-ffff", "claude", "lane-2c")
        self.agenda.touch(new)
        self.assertEqual(self.agenda.find_session("lane-2c").id, new.id)
        self.clock.advance(minutes=5)
        self.agenda.touch(old)
        with self.assertRaisesRegex(AgendaError, "more than one live session"):
            self.agenda.find_session("lane-2c")
        self.assertEqual(self.agenda.find_session("555555").id, old.id)

    def test_news_counts_say_who_to_nudge(self):
        item = self.decision()
        self.agenda.note(LEDGER, item.id, "Sam is out.")
        self.agenda.answer(NICK_ACTOR, item.id, "wait")
        counts = {s.name: n for s, n in self.agenda.news_counts()}
        self.assertEqual(counts, {"merge-queue": 1, "ledger": 1})

    def test_secrets_are_refused(self):
        # Built at runtime so no secret scanner mistakes the test for a leak.
        fake_anthropic = "sk-" + "ant-api03-" + "a" * 24
        fake_github = "gh" + "p_" + "b" * 32
        with self.assertRaisesRegex(AgendaError, "secret"):
            self.agenda.raise_item(MERGE_QUEUE, "task", "Key", f"Use {fake_anthropic} please")
        item = self.decision()
        with self.assertRaisesRegex(AgendaError, "secret"):
            self.agenda.answer(NICK_ACTOR, item.id, f"token is {fake_github}")

    def test_queue_order(self):
        fyi, _ = self.agenda.raise_item(MERGE_QUEUE, "fyi", "Shipped", "Release is live.")
        self.clock.advance(minutes=1)
        task, _ = self.agenda.raise_item(MERGE_QUEUE, "task", "Paste a token", "At a computer.")
        self.clock.advance(minutes=1)
        question, _ = self.agenda.raise_item(MERGE_QUEUE, "question", "Naming?", "What do we call the tab?")
        self.clock.advance(minutes=1)
        decision = self.decision()
        self.clock.advance(minutes=1)
        ask = self.agenda.ask(NICK_ACTOR, "why is CI red?")
        self.agenda.inbox(MERGE_QUEUE)
        self.agenda.reply(MERGE_QUEUE, ask.id, "flaky test")
        self.clock.advance(minutes=1)
        blocking, _ = self.agenda.raise_item(MERGE_QUEUE, "question", "Which DB?", "Stuck until you pick.", blocking=True)
        self.assertEqual(
            self.ids(self.agenda.queue()), [blocking.id, ask.id, decision.id, question.id, task.id, fyi.id]
        )


class Migration(unittest.TestCase):
    def test_an_empty_v1_database_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agenda.db"
            db = sqlite3.connect(path)
            db.executescript("CREATE TABLE items (id INTEGER); CREATE TABLE messages (id INTEGER); PRAGMA user_version = 1;")
            db.close()
            with Agenda(path) as agenda:
                self.assertEqual(agenda.queue(), [])

    def test_a_v2_database_keeps_its_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agenda.db"
            db = sqlite3.connect(path)
            db.executescript(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT, label TEXT, role TEXT NOT NULL DEFAULT 'lane',"
                " pane_id TEXT, tab_id TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);"
                "CREATE TABLE items (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, raised_by TEXT NOT NULL, owner TEXT NOT NULL,"
                " title TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT, options TEXT NOT NULL DEFAULT '[]',"
                " recommendation TEXT, default_option TEXT, default_at TEXT, lane TEXT, blocking INTEGER NOT NULL DEFAULT 0,"
                " status TEXT NOT NULL DEFAULT 'open', waiting_on TEXT, waiting_since TEXT, deferred_until TEXT,"
                " outcome TEXT, resolution TEXT, closed_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                " closed_at TEXT);"
                "CREATE TABLE item_topics (topic TEXT NOT NULL, item_id INTEGER NOT NULL, PRIMARY KEY (topic, item_id));"
                "CREATE TABLE watchers (item_id INTEGER NOT NULL, session TEXT NOT NULL, read_upto INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (item_id, session));"
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, author TEXT NOT NULL,"
                " body TEXT NOT NULL, created_at TEXT NOT NULL);"
                "INSERT INTO sessions VALUES ('s1', 'claude', 'merge-queue', 'orchestrator', NULL, NULL, 't', 't');"
                "INSERT INTO items (kind, raised_by, owner, title, summary, waiting_on, waiting_since, created_at, updated_at)"
                " VALUES ('fyi', 's1', 's1', 'Release live', 'production has it.', 'nick', '2026-09-30T12:00:00Z',"
                " '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z');"
                "INSERT INTO watchers VALUES (1, 's1', 0);"
                "INSERT INTO messages (item_id, author, body, created_at) VALUES (1, 's1', 'Update: live', 't');"
                "PRAGMA user_version = 2;"
            )
            db.close()
            with Agenda(path) as agenda:
                item = agenda.get(1)
                self.assertEqual((item.title, item.briefing, item.messages[0].kind), ("Release live", None, None))
                agenda.question(NICK_ACTOR, 1, "which clinics?")
                self.assertEqual([s.id for s, _ in agenda.unnudged()], ["s1"])
            db = sqlite3.connect(path)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            columns = {r[1] for r in db.execute("PRAGMA table_info(items)")}
            self.assertLessEqual({"briefing", "project", "feature", "context", "quick"}, columns)
            db.close()

    def test_a_v1_database_with_items_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agenda.db"
            db = sqlite3.connect(path)
            db.executescript(
                "CREATE TABLE items (id INTEGER); CREATE TABLE messages (id INTEGER);"
                " INSERT INTO items VALUES (1); PRAGMA user_version = 1;"
            )
            db.close()
            with self.assertRaisesRegex(AgendaError, "move it aside"):
                Agenda(path)


if __name__ == "__main__":
    unittest.main()
