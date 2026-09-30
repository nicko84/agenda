"""Durable agenda between Nick and his agent sessions.

A session's terminal is a stream and Nick is only there some of the time, so anything
said while he was away scrolls off, and after compaction a session can forget it ever
asked. Here every exchange is an item with a thread and a status instead of a line of
chat.

Who is who:
- Nick answers. Only he can answer, accept, ack, done, defer or drop.
- Sessions raise, orchestrators and lanes alike. Every item has one owner (the session
  that acts on the outcome) and any number of watchers that want to hear how it ends.
  Orchestrators can act on any item; a lane only on its own.
- While an item is open it waits on exactly one side: Nick, or its owner.

Rules that keep messages from being lost or doubled:
- Each watcher has its own read cursor, and only `inbox()` moves it.
- A session can't reply to, close, update, withdraw or hand off an item that has news
  it hasn't read, so it never settles anything on stale information.
- One open item per topic. An item can carry several topics, e.g. `ci-runner-kill` and
  later `pr webapp#4573`. Raising an open topic joins that item as a watcher
  instead of opening a second one; the owner raising it again posts a progress update.
  Raising something that merely looks the same (it shares a #number, or most of its
  title) is refused until the session joins the existing item or says it is distinct.
- A settled topic stays settled. Raising a topic that a closed item already carries
  is refused with the earlier resolution, unless the session says `reask`.
- Silence is never consent. A decision can carry a default and a deadline; when the
  deadline passes unanswered, the agenda tells the owner the default applies.
- No secrets. Items are read aloud and shown on a phone, so token-shaped text is refused.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Self

from .identity import NICK, Actor

SYSTEM = "system"  # bookkeeping; nobody is woken for it
AGENDA = "agenda"  # the agenda itself speaking, e.g. a default applying; news to everyone
OWNER = "owner"  # waiting_on value: the item's owning session
QUESTION = "question"  # message kind: Nick asking the owner something, not deciding

SESSION_KINDS = ("decision", "question", "task", "fyi")  # task: needs Nick at a computer
NICK_KINDS = ("ask", "instruction")

OPEN = "open"
CLOSED = "closed"
WITHDRAWN = "withdrawn"

ORCHESTRATOR = "orchestrator"
LANE = "lane"

# How an item ended.
ANSWERED = "answered"  # Nick answered here and the owner acted
DONE = "done"  # the owner carried out Nick's ask or instruction
DEFAULTED = "default"  # no answer by the deadline; the owner went with the default
ELSEWHERE = "elsewhere"  # settled outside the agenda: in a terminal, on GitHub, by Nick acting himself
MOOT = "moot"  # withdrawn: superseded, a no-op, no longer needed
ACKNOWLEDGED = "acknowledged"
DROPPED = "dropped"

SCHEMA_VERSION = 4

# summary is written to be heard (a sentence or two); detail is markdown for a screen; briefing is for a
# stand-in (the voice agent) who must answer Nick's follow-up questions without the owning session.
# project and feature group items so a call goes project by project; context says what the item is and why
# it has come up, for someone who hasn't followed the session; quick marks a high-confidence recommendation
# that a quick sign-off can settle.
SCHEMA = """
CREATE TABLE sessions (
  id         TEXT PRIMARY KEY,
  agent      TEXT,
  label      TEXT,
  role       TEXT NOT NULL DEFAULT 'lane' CHECK (role IN ('orchestrator', 'lane')),
  pane_id    TEXT,
  tab_id     TEXT,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL
);
CREATE TABLE items (
  id             INTEGER PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('decision', 'question', 'task', 'fyi', 'ask', 'instruction')),
  raised_by      TEXT NOT NULL,
  owner          TEXT NOT NULL REFERENCES sessions(id),
  title          TEXT NOT NULL,
  summary        TEXT NOT NULL,
  detail         TEXT,
  briefing       TEXT,
  options        TEXT NOT NULL DEFAULT '[]',
  recommendation TEXT,
  default_option TEXT,
  default_at     TEXT,
  lane           TEXT,
  project        TEXT,
  feature        TEXT,
  context        TEXT,
  quick          INTEGER NOT NULL DEFAULT 0,
  blocking       INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'withdrawn')),
  waiting_on     TEXT CHECK (waiting_on IN ('nick', 'owner')),
  waiting_since  TEXT,
  deferred_until TEXT,
  outcome        TEXT,
  resolution     TEXT,
  closed_by      TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  closed_at      TEXT,
  CHECK ((status = 'open') = (waiting_on IS NOT NULL)),
  CHECK ((default_option IS NULL) = (default_at IS NULL))
);
CREATE INDEX items_open ON items (status, waiting_on);
CREATE TABLE item_topics (
  topic   TEXT NOT NULL,
  item_id INTEGER NOT NULL REFERENCES items(id),
  PRIMARY KEY (topic, item_id)
);
CREATE TABLE watchers (
  item_id   INTEGER NOT NULL REFERENCES items(id),
  session   TEXT NOT NULL REFERENCES sessions(id),
  read_upto INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (item_id, session)
);
CREATE INDEX watchers_session ON watchers (session);
CREATE TABLE messages (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id),
  author     TEXT NOT NULL,
  body       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  kind       TEXT CHECK (kind IN ('question'))
);
CREATE INDEX messages_item ON messages (item_id);
CREATE TABLE nudges (
  session TEXT PRIMARY KEY,
  upto    INTEGER NOT NULL,
  at      TEXT NOT NULL
);
CREATE TABLE projects (
  key        TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  brief      TEXT,
  updated_by TEXT,
  updated_at TEXT
);
CREATE TABLE attachments (
  id       INTEGER PRIMARY KEY,
  item_id  INTEGER NOT NULL REFERENCES items(id),
  name     TEXT NOT NULL,
  path     TEXT,
  url      TEXT,
  mime     TEXT,
  added_by TEXT NOT NULL,
  added_at TEXT NOT NULL,
  CHECK ((path IS NULL) != (url IS NULL))
)
"""


class AgendaError(Exception):
    """A request the agenda refuses; the message is shown to whoever asked."""


class LooksLikeDuplicate(AgendaError):
    """A raise that resembles open items; join one (`into`) or say it is `distinct`."""

    def __init__(self, candidates: list[Item]):
        self.candidates = candidates
        listed = "; ".join(f"#{i.id} {i.title!r} (owner {i.name_of(i.owner)})" for i in candidates)
        super().__init__(
            f"this looks like an open item: {listed}. Join it with --into ID and you'll hear how it ends, "
            "or pass --distinct if it really is a different question"
        )


@dataclass
class Message:
    id: int
    item_id: int
    author: str
    body: str
    created_at: str
    kind: str | None = None  # "question" for Nick asking rather than deciding

    @property
    def is_question(self) -> bool:
        return self.kind == QUESTION


@dataclass
class Attachment:
    id: int
    item_id: int
    name: str
    path: str | None  # a copy kept with the agenda, so it outlives the session's scratch folder
    url: str | None  # or a link: a Paper board, a preview deploy
    mime: str | None
    added_by: str
    added_at: str

    @property
    def is_image(self) -> bool:
        return bool(self.path and (self.mime or "").startswith("image/"))


@dataclass
class Project:
    key: str
    name: str
    brief: str | None
    updated_by: str | None
    updated_at: str | None


@dataclass
class Session:
    id: str
    agent: str | None
    label: str | None
    role: str
    pane_id: str | None
    tab_id: str | None
    first_seen: str
    last_seen: str

    @property
    def name(self) -> str:
        return self.label or self.id[:8]


@dataclass
class Item:
    id: int
    kind: str
    raised_by: str
    owner: str
    title: str
    summary: str
    detail: str | None
    briefing: str | None
    options: list[str]
    recommendation: str | None
    default_option: str | None
    default_at: str | None
    lane: str | None
    project: str | None
    feature: str | None
    context: str | None
    quick: bool
    blocking: bool
    status: str
    waiting_on: str | None
    waiting_since: str | None
    deferred_until: str | None
    outcome: str | None
    resolution: str | None
    closed_by: str | None
    created_at: str
    updated_at: str
    closed_at: str | None
    topics: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    watchers: dict[str, int] = field(default_factory=dict)  # session id -> read_upto
    names: dict[str, str] = field(default_factory=dict)  # session id -> label, for display

    @property
    def from_nick(self) -> bool:
        return self.raised_by == NICK

    @property
    def nick_has_answered(self) -> bool:
        """Nick has said something that decides or acknowledges; his questions don't count."""
        return any(m.author == NICK and not m.is_question for m in self.messages)

    @property
    def answers_nicks_question(self) -> bool:
        """The owner has replied since Nick last asked something."""
        asked = [m.id for m in self.messages if m.author == NICK and m.is_question]
        return bool(asked) and any(m.author == self.owner and m.id > asked[-1] for m in self.messages)

    def name_of(self, who: str) -> str:
        if who in (NICK, SYSTEM, AGENDA):
            return who
        return self.names.get(who) or who[:8]

    def news_for(self, session: str) -> list[Message]:
        """Messages this session hasn't read, from anyone but itself."""
        upto = self.watchers.get(session, 0)
        return [m for m in self.messages if m.id > upto and m.author not in (session, SYSTEM)]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["owner_name"] = self.name_of(self.owner)
        return data


def default_path() -> Path:
    if env := os.environ.get("AGENDA_DB"):
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "agenda" / "agenda.db"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    # One fixed format, so stored times compare correctly as strings in SQL.
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_topic(topic: str) -> str:
    """Case and spacing never split a topic: 'PR webapp #4579' -> 'pr webapp#4579',
    'Member Hub Support:W3' -> 'member hub support: w3'."""
    flat = " ".join(topic.lower().split())
    return re.sub(r"\s*#\s*", "#", re.sub(r"\s*:\s*", ": ", flat)).strip()


def queue_rank(item: Item) -> int:
    """Blocking work first, then answers to Nick's own asks, then decisions, questions, tasks, FYIs."""
    if item.blocking:
        return 0
    if item.from_nick or item.answers_nicks_question:
        return 1
    return {"decision": 2, "question": 3, "task": 4, "fyi": 5}[item.kind]


class Agenda:
    def __init__(self, path: str | os.PathLike | None = None, clock: Callable[[], datetime] = now_utc):
        self.path = Path(path) if path is not None else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        # Sessions, hooks and front ends all write here; WAL plus a busy timeout lets
        # them take turns instead of failing.
        self._db = sqlite3.connect(self.path, timeout=5.0, autocommit=True)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def now(self) -> datetime:
        return self._clock()

    # --- Sessions -----------------------------------------------------------

    def touch(self, actor: Actor) -> None:
        """Record that a session is alive and where to reach it. Nick needs no row."""
        if actor.is_nick:
            return
        now = self._now()
        with self._tx():
            self._db.execute(
                "INSERT INTO sessions (id, agent, label, pane_id, tab_id, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET agent = coalesce(excluded.agent, agent),"
                " label = coalesce(excluded.label, label), pane_id = coalesce(excluded.pane_id, pane_id),"
                " tab_id = coalesce(excluded.tab_id, tab_id), last_seen = excluded.last_seen",
                (actor.id, actor.agent, actor.label, actor.pane_id, actor.tab_id, now, now),
            )

    def register(self, actor: Actor, *, role: str = ORCHESTRATOR, label: str | None = None) -> Session:
        if actor.is_nick:
            raise AgendaError("only a session registers; Nick is always Nick")
        if role not in (ORCHESTRATOR, LANE):
            raise AgendaError(f"role is {ORCHESTRATOR} or {LANE}, not {role!r}")
        self.touch(actor)
        with self._tx():
            self._db.execute(
                "UPDATE sessions SET role = ?, label = coalesce(?, label) WHERE id = ?",
                (role, _optional(label), actor.id),
            )
        return self.session(actor.id)

    def session(self, session_id: str) -> Session:
        row = self._db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise AgendaError(f"no session {session_id!r}")
        return Session(**dict(row))

    def sessions(self) -> list[Session]:
        rows = self._db.execute("SELECT * FROM sessions ORDER BY role = 'orchestrator' DESC, last_seen DESC")
        return [Session(**dict(r)) for r in rows]

    def find_session(self, name: str) -> Session:
        """By id, id prefix (6+ chars) or herdr tab label.

        Labels repeat (tabs get reused and renamed), so the most recently seen session
        wins, unless another with that label was also seen in the last hour.
        """
        name = name.strip()
        exact = self._db.execute(
            "SELECT * FROM sessions WHERE id = ? OR (length(?) >= 6 AND id LIKE ? || '%')", (name, name, name)
        ).fetchall()
        if len(exact) == 1:
            return Session(**dict(exact[0]))
        rows = self._db.execute(
            "SELECT * FROM sessions WHERE lower(label) = lower(?) ORDER BY last_seen DESC", (name,)
        ).fetchall()
        if not rows:
            known = ", ".join(s.name for s in self.sessions()) or "none yet"
            raise AgendaError(f"no session called {name!r} (known: {known})")
        found = [Session(**dict(r)) for r in rows]
        recent = iso(self._clock() - timedelta(hours=1))
        if len(found) > 1 and found[1].last_seen >= recent:
            ids = ", ".join(s.id[:8] for s in found if s.last_seen >= recent)
            raise AgendaError(f"more than one live session is called {name!r}; use an id prefix: {ids}")
        return found[0]

    def projects(self) -> list[tuple[Project, int]]:
        """Known projects and how many open items each has."""
        rows = self._db.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM items i WHERE i.status = 'open' AND i.project = p.name) AS open"
            " FROM projects p ORDER BY open DESC, p.name"
        ).fetchall()
        return [(Project(**{k: r[k] for k in Project.__dataclass_fields__}), r["open"]) for r in rows]

    def project(self, name: str) -> Project | None:
        row = self._db.execute("SELECT * FROM projects WHERE key = ?", (_project_key(name),)).fetchone()
        return Project(**dict(row)) if row else None

    def set_brief(self, actor: Actor, name: str, brief: str) -> Project:
        """A project's state of play, for the start of its part of a call; kept current by its orchestrator."""
        self._require_session(actor, "brief a project")
        brief = _required(brief, "brief")
        self.touch(actor)
        with self._tx():
            display = self._project_name(_required(name, "project"))
            self._db.execute(
                "UPDATE projects SET brief = ?, updated_by = ?, updated_at = ? WHERE key = ?",
                (brief, actor.id, self._now(), _project_key(display)),
            )
        return self.project(display)

    def request_filing(self) -> int:
        """Ask the owners of open items that lack a project, feature or context to add them (once per item)."""
        text = (
            "Nick's agenda calls now go project by project, then feature by feature, and each item opens with a line "
            "of context. Please file this item: `agenda update ID --project NAME --feature NAME --context "
            "'what this is and why it has come up'` (see `agenda projects` for names in use). Add --quick if the "
            "recommendation is a safe quick sign-off, and attach screenshots or board links with --attach if it "
            "needs a design sign-off."
        )
        with self._tx():
            rows = self._db.execute(
                "SELECT id FROM items WHERE status = ? AND raised_by != ?"
                " AND (project IS NULL OR feature IS NULL OR context IS NULL)"
                " AND id NOT IN (SELECT item_id FROM messages WHERE author = ? AND body LIKE 'Nick''s agenda calls now go%')",
                (OPEN, NICK, AGENDA),
            ).fetchall()
            for row in rows:
                self._add_message(row["id"], AGENDA, text.replace("update ID", f"update {row['id']}"))
        return len(rows)

    def attachment(self, attachment_id: int) -> Attachment:
        row = self._db.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        if row is None:
            raise AgendaError(f"no attachment {attachment_id}")
        return Attachment(**dict(row))

    def record_note(self, item_id: int, text: str) -> None:
        """Bookkeeping on an item's thread that wakes nobody (e.g. screenshots emailed)."""
        with self._tx():
            self._add_message(item_id, SYSTEM, text)

    def default_orchestrator(self) -> Session:
        rows = self._db.execute("SELECT * FROM sessions WHERE role = ? ORDER BY last_seen DESC", (ORCHESTRATOR,))
        found = [Session(**dict(r)) for r in rows]
        if len(found) == 1:
            return found[0]
        if not found:
            raise AgendaError("no orchestrator has registered yet, so there is no one to send this to")
        raise AgendaError("more than one orchestrator; say which with --to: " + ", ".join(s.name for s in found))

    def news_counts(self) -> list[tuple[Session, int]]:
        """Sessions with unread news and how many items it is on; what a nudger delivers."""
        self.sweep()
        rows = self._db.execute(
            "SELECT w.session, COUNT(DISTINCT w.item_id) AS n FROM watchers w"
            " JOIN messages m ON m.item_id = w.item_id AND m.id > w.read_upto"
            " AND m.author NOT IN (w.session, ?) GROUP BY w.session",
            (SYSTEM,),
        ).fetchall()
        return [(self.session(r["session"]), r["n"]) for r in rows]

    def unnudged(self) -> list[tuple[Session, int]]:
        """Sessions with unread news newer than the last time anyone nudged them: (session, newest news id)."""
        self.sweep()
        rows = self._db.execute(
            "SELECT w.session, MAX(m.id) AS last FROM watchers w"
            " JOIN messages m ON m.item_id = w.item_id AND m.id > w.read_upto AND m.author NOT IN (w.session, ?)"
            " LEFT JOIN nudges n ON n.session = w.session"
            " GROUP BY w.session HAVING MAX(m.id) > coalesce(MAX(n.upto), 0)",
            (SYSTEM,),
        ).fetchall()
        return [(self.session(r["session"]), r["last"]) for r in rows]

    def unread(self, session_id: str, *, since_nudge: bool = False) -> tuple[list[Item], int]:
        """Items with news this session hasn't read, and the newest news id. Read-only: hooks call this
        for every session on the machine, so it must not register anyone or take a write lock.

        With since_nudge, only items whose news is newer than the session's last nudge.
        """
        self.sweep()
        sql = (
            "SELECT w.item_id, MAX(m.id) AS last FROM watchers w"
            " JOIN messages m ON m.item_id = w.item_id AND m.id > w.read_upto AND m.author NOT IN (?, ?)"
        )
        params: list = [session_id, SYSTEM]
        if since_nudge:
            sql += " AND m.id > (SELECT coalesce(MAX(upto), 0) FROM nudges WHERE session = ?)"
            params.append(session_id)
        rows = self._db.execute(
            sql + " WHERE w.session = ? GROUP BY w.item_id ORDER BY MIN(m.id)", (*params, session_id)
        ).fetchall()
        return [self._get(r["item_id"]) for r in rows], max((r["last"] for r in rows), default=0)

    def latest_message_id(self) -> int:
        return self._db.execute("SELECT coalesce(MAX(id), 0) FROM messages").fetchone()[0]

    def knows(self, session_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is not None

    def relocate(self, actor: Actor) -> None:
        """Keep a known session's pane current (herdr restarts renumber panes); never registers anyone."""
        row = self._db.execute("SELECT pane_id, tab_id FROM sessions WHERE id = ?", (actor.id,)).fetchone()
        if row is None or not actor.pane_id or (row["pane_id"], row["tab_id"]) == (actor.pane_id, actor.tab_id):
            return
        with self._tx():
            self._db.execute(
                "UPDATE sessions SET pane_id = ?, tab_id = coalesce(?, tab_id), last_seen = ? WHERE id = ?",
                (actor.pane_id, actor.tab_id, self._now(), actor.id),
            )

    def record_nudge(self, session_id: str, upto: int) -> None:
        with self._tx():
            self._db.execute(
                "INSERT INTO nudges (session, upto, at) VALUES (?, ?, ?)"
                " ON CONFLICT (session) DO UPDATE SET upto = max(upto, excluded.upto), at = excluded.at",
                (session_id, upto, self._now()),
            )

    # --- Sessions raise and act ---------------------------------------------

    def raise_item(
        self,
        actor: Actor,
        kind: str,
        title: str,
        summary: str,
        *,
        topics: Iterable[str] = (),
        into: int | None = None,
        distinct: bool = False,
        reask: bool = False,
        detail: str | None = None,
        briefing: str | None = None,
        options: Iterable[str] = (),
        recommendation: str | None = None,
        default: str | None = None,
        default_at: datetime | None = None,
        lane: str | None = None,
        project: str | None = None,
        feature: str | None = None,
        context: str | None = None,
        quick: bool = False,
        attach: Iterable[str] = (),
        blocking: bool = False,
        cc: Iterable[str] = (),
    ) -> tuple[Item, bool]:
        """Put a topic on Nick's agenda. Returns (item, joined an existing item).

        A new item needs a project, a feature and a line of context, because Nick's calls go project by
        project and every item has to make sense to someone who hasn't followed the session.
        """
        self._require_session(actor, "raise")
        if kind not in SESSION_KINDS:
            raise AgendaError(f"sessions raise {', '.join(SESSION_KINDS)}, not {kind!r}")
        if blocking and kind == "fyi":
            raise AgendaError("an fyi can't be blocking; raise a decision or question if work is waiting on Nick")
        default = _optional(default)
        if (default is None) != (default_at is None):
            raise AgendaError("a default needs a deadline and a deadline needs a default (--default with --by)")
        if default_at is not None:
            if kind == "fyi":
                raise AgendaError("an fyi has nothing to default to")
            if default_at <= self._clock():
                raise AgendaError("the default's deadline must be in the future")
        title, summary = _required(title, "title"), _required(summary, "summary")
        topics = _clean_topics(topics)
        project, feature, context = _optional(project), _optional(feature), _optional(context, "context")
        if quick and (kind not in ("decision", "question") or not _optional(recommendation)):
            raise AgendaError("--quick is for a decision or question with a recommendation Nick can simply accept")
        files = [_attachment_source(a) for a in attach]
        self.touch(actor)
        watchers = [self.find_session(name).id for name in cc]
        with self._tx():
            existing = self._open(into, action="join") if into else self._open_for_topics(topics)
            settled = None if existing or into else self._settled_for_topics(topics)
            if settled is not None and not reask:
                raise AgendaError(
                    f"that topic was settled in #{settled.id} ({settled.outcome}, {settled.closed_at[:10]}): "
                    f"{settled.resolution or 'see agenda show ' + str(settled.id)}. Don't ask again unless something "
                    "has changed; if it has, raise with --reask and say what changed"
                )
            if existing is None and not distinct:
                open_like, settled_like = self._lookalikes(title, summary, topics)
                if open_like:
                    raise LooksLikeDuplicate(open_like)
                if settled_like and settled is None:
                    if not reask:
                        listed = "; ".join(
                            f"#{i.id} {i.title!r}, settled {i.closed_at[:10]}: {i.resolution or i.outcome}"
                            for i in settled_like
                        )
                        raise AgendaError(
                            f"this looks like something already settled: {listed}. Don't ask again unless something "
                            "has changed (then --reask, saying what changed); if it is a different question, --distinct"
                        )
                    settled = settled_like[0]
            if existing is not None:
                self._join(actor, existing, summary=summary, topics=topics, blocking=blocking, watchers=watchers)
                fill = {k: v for k, v in (("project", project), ("feature", feature), ("context", context)) if v}
                if fill and not all(getattr(existing, k) for k in fill):
                    self._classify(existing, **{k: v for k, v in fill.items() if not getattr(existing, k)})
                self._attach(existing.id, actor, files)
                return self._get(existing.id), True
            missing = [n for n, v in (("--project", project), ("--feature", feature), ("--context", context)) if not v]
            if missing:
                raise AgendaError(
                    f"a new item needs {', '.join(missing)}: Nick's calls go project by project and feature by "
                    "feature, and each item opens with a line of context (what it is, why it has come up). "
                    "`agenda projects` lists the project names already in use"
                )
            project = self._project_name(project)
            feature = self._feature_name(project, feature)
            now = self._now()
            cur = self._db.execute(
                "INSERT INTO items (kind, raised_by, owner, title, summary, detail, briefing, options, recommendation,"
                " default_option, default_at, lane, project, feature, context, quick, blocking, waiting_on,"
                " waiting_since, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind, actor.id, actor.id, title, summary, _optional(detail), _optional(briefing, "briefing"),
                    json.dumps(_clean_options(options)), _optional(recommendation), default,
                    iso(default_at) if default_at else None, _optional(lane), project, feature, context, int(quick),
                    int(blocking), NICK, now, now, now,
                ),
            )
            self._add_topics(cur.lastrowid, topics)
            self._attach(cur.lastrowid, actor, files)
            for session in (actor.id, *watchers):
                self._watch(cur.lastrowid, session)
            if settled is not None:
                self._add_message(
                    cur.lastrowid, SYSTEM, f"Asked before in #{settled.id}, settled {settled.closed_at[:10]}: "
                    f"{settled.resolution or settled.outcome}"
                )
        return self._get(cur.lastrowid), False

    def watch(self, actor: Actor, item_id: int) -> Item:
        """Hear how an item ends without owning it."""
        self._require_session(actor, "watch")
        self.touch(actor)
        with self._tx():
            item = self._get(item_id)
            self._watch(item.id, actor.id)
        return self._get(item_id)

    def update(
        self,
        actor: Actor,
        item_id: int,
        *,
        title: str | None = None,
        summary: str | None = None,
        detail: str | None = None,
        briefing: str | None = None,
        options: Iterable[str] | None = None,
        recommendation: str | None = None,
        lane: str | None = None,
        topics: Iterable[str] = (),
        blocking: bool | None = None,
        project: str | None = None,
        feature: str | None = None,
        context: str | None = None,
        quick: bool | None = None,
        attach: Iterable[str] = (),
    ) -> Item:
        """Revise an item as things change, instead of raising a duplicate. Topics and attachments are added,
        never removed. On Nick's own asks only the filing (project, feature, context, topics, attachments) can
        change; his words stay his."""
        text_fields = (title, summary, detail, briefing, options, recommendation, lane, blocking, quick)
        if topics and all(v is None for v in (*text_fields, project, feature, context)) and not attach:
            return self._add_topic_aliases(actor, item_id, topics)
        files = [_attachment_source(a) for a in attach]
        with self._tx():
            item = self._acting(actor, item_id, action="update")
            if item.from_nick and any(v is not None for v in text_fields):
                raise AgendaError(f"#{item.id} is Nick's own {item.kind}; reply to it instead")
            filing = {k: v for k, v in (("project", project), ("feature", feature), ("context", context)) if v is not None}
            if filing:
                self._classify(item, **filing)
            if files:
                self._attach(item.id, actor, files)
            if topics:
                self._add_topics(item.id, [t for t in _clean_topics(topics) if t not in item.topics])
            if not any(v is not None for v in text_fields):
                if not (filing or files or topics):
                    raise AgendaError("nothing to update")
                self._set(item.id)
                return self._get(item_id)
            changes: dict = {}
            if title is not None:
                changes["title"] = _required(title, "title")
            if summary is not None:
                changes["summary"] = _required(summary, "summary")
            if detail is not None:
                changes["detail"] = _optional(detail)
            if briefing is not None:
                changes["briefing"] = _optional(briefing, "briefing")
            if options is not None:
                changes["options"] = json.dumps(_clean_options(options))
            if recommendation is not None:
                changes["recommendation"] = _optional(recommendation)
            if lane is not None:
                changes["lane"] = _optional(lane)
            if blocking is not None:
                if blocking and item.kind == "fyi":
                    raise AgendaError("an fyi can't be blocking")
                changes["blocking"] = int(blocking)
            if quick is not None:
                if quick and (item.kind not in ("decision", "question") or not (recommendation or item.recommendation)):
                    raise AgendaError("--quick is for a decision or question with a recommendation Nick can simply accept")
                changes["quick"] = int(quick)
            if not changes:
                raise AgendaError("nothing to update")
            fields = ", ".join([*changes, *(["filing"] if filing else []), *(["topics"] if topics else [])])
            # Keep what each text said before, so an ask can't drift without a trace.
            for name in ("title", "summary", "detail", "briefing", "recommendation", "options"):
                before = getattr(item, name)
                before = json.dumps(before) if name == "options" else before
                if name in changes and before and changes[name] != before:
                    self._add_message(item.id, SYSTEM, f"The {name} was: {before}")
            if blocking:
                changes["deferred_until"] = None  # newly blocking work resurfaces
            self._set(item.id, **changes)
            self._add_message(item.id, SYSTEM, f"{self._name(actor.id)} updated the {fields}.")
        return self._get(item_id)

    def _add_topic_aliases(self, actor: Actor, item_id: int, topics: Iterable[str]) -> Item:
        """Topics only: allowed on Nick's asks and on closed items too, so a settled decision can be found
        (and stays settled) under the other wordings sessions use for it."""
        self._require_session(actor, "add topics")
        self.touch(actor)
        with self._tx():
            item = self._get(item_id)
            if actor.id != item.owner and self._role(actor.id) != ORCHESTRATOR:
                raise AgendaError(f"#{item.id} belongs to {self._name(item.owner)}; ask its owner to add the topic")
            new = [t for t in _clean_topics(topics) if t not in item.topics]
            if not new:
                raise AgendaError("nothing to update")
            self._add_topics(item.id, new)
            self._add_message(item.id, SYSTEM, f"{self._name(actor.id)} added the topic(s): {', '.join(new)}.")
        return self._get(item_id)

    def reply(self, actor: Actor, item_id: int, text: str) -> Item:
        """Say something back to Nick; hands the item to him."""
        text = _required(text, "reply")
        with self._tx():
            item = self._acting(actor, item_id, action="reply to")
            self._add_message(item.id, actor.id, text)
            self._hand_to(item, NICK)
        return self._get(item_id)

    def note(self, actor: Actor, item_id: int, text: str) -> Item:
        """Add context to the thread without changing whose turn it is. Watchers hear it."""
        self._require_session(actor, "add a note")
        text = _required(text, "note")
        self.touch(actor)
        with self._tx():
            item = self._open(item_id, action="add a note to")
            self._watch(item.id, actor.id)
            self._add_message(item.id, actor.id, text)
            self._set(item.id)
        return self._get(item_id)

    def close_item(self, actor: Actor, item_id: int, note: str | None = None) -> Item:
        """Settled, either after acting on the outcome or because it was settled somewhere else.

        Closing something still waiting on Nick needs a note saying how it was settled,
        in his words where there are any, so the record shows the outcome.
        """
        note = _optional(note)
        with self._tx():
            item = self._acting(actor, item_id, action="close")
            if item.waiting_on == NICK:
                if not note:
                    raise AgendaError(
                        f"#{item.id} is still waiting on Nick; say how it was settled, e.g. "
                        f"`agenda close {item.id} \"Nick said in the terminal: merge it\"`, or withdraw it if it's moot"
                    )
                outcome, resolution = ELSEWHERE, note
            else:
                decisive = next(
                    (m for m in reversed(item.messages) if m.author in (NICK, AGENDA) and not m.is_question), None
                )
                if item.from_nick:
                    outcome = DONE
                elif decisive is not None and decisive.author == AGENDA:
                    outcome = DEFAULTED
                else:
                    outcome = ANSWERED
                resolution = note or (decisive.body if decisive else None)
            self._add_message(item.id, actor.id, f"Closed: {note}" if note else "Closed.")
            self._finish(item, CLOSED, actor.id, outcome, resolution)
        return self._get(item_id)

    def withdraw(self, actor: Actor, item_id: int, reason: str) -> Item:
        """Retract a topic that no longer matters (superseded, a no-op), so Nick is never asked it."""
        reason = _required(reason, "reason")
        with self._tx():
            item = self._acting(actor, item_id, action="withdraw")
            if item.waiting_on != NICK or item.from_nick:
                raise AgendaError(
                    f"can't withdraw #{item.id}: only a session's item that is waiting on Nick; close it instead"
                )
            self._add_message(item.id, actor.id, f"Withdrawn: {reason}")
            self._finish(item, WITHDRAWN, actor.id, MOOT, reason)
        return self._get(item_id)

    def handoff(self, actor: Actor, item_id: int, to: str) -> Item:
        """Make another session the owner, e.g. the lane that will do the work."""
        target = self.find_session(to)
        with self._tx():
            item = self._acting(actor, item_id, action="hand off")
            if target.id == item.owner:
                raise AgendaError(f"#{item.id} is already owned by {target.name}")
            self._watch(item.id, target.id)
            self._add_message(item.id, actor.id, f"Handed to {target.name}.")
            self._set(item.id, owner=target.id)
        return self._get(item_id)

    def inbox(self, actor: Actor, *, mark: bool = True) -> list[Item]:
        """Items with news this session hasn't read, oldest news first.

        The returned items are a snapshot from before marking, so `news_for(actor.id)`
        on them still lists what was new.
        """
        self._require_session(actor, "read an inbox")
        self.touch(actor)
        self.sweep()
        with self._tx():
            rows = self._db.execute(
                "SELECT w.item_id, MAX(m.id) AS last FROM watchers w"
                " JOIN messages m ON m.item_id = w.item_id AND m.id > w.read_upto AND m.author NOT IN (?, ?)"
                " WHERE w.session = ? GROUP BY w.item_id ORDER BY MIN(m.id)",
                (actor.id, SYSTEM, actor.id),
            ).fetchall()
            items = [self._get(r["item_id"]) for r in rows]
            if mark:
                for r in rows:
                    self._db.execute(
                        "UPDATE watchers SET read_upto = ? WHERE item_id = ? AND session = ?",
                        (r["last"], r["item_id"], actor.id),
                    )
        return items

    def inbox_count(self, actor: Actor) -> int:
        return next((n for s, n in self.news_counts() if s.id == actor.id), 0)

    def owed(self, actor: Actor | None = None) -> list[Item]:
        """Open items waiting on their owner: this session's, or every session's."""
        self.sweep()
        sql, params = "SELECT * FROM items WHERE status = ? AND waiting_on = ?", (OPEN, OWNER)
        if actor is not None:
            sql, params = sql + " AND owner = ?", (*params, actor.id)
        return self._items(sql + " ORDER BY waiting_since, id", params)

    def mine(self, actor: Actor) -> list[Item]:
        """Every open item this session owns or watches, whoever's turn it is."""
        return self._items(
            "SELECT i.* FROM items i JOIN watchers w ON w.item_id = i.id"
            " WHERE w.session = ? AND i.status = ? ORDER BY i.id",
            (actor.id, OPEN),
        )

    # --- Nick ---------------------------------------------------------------

    def ask(
        self,
        actor: Actor,
        text: str,
        *,
        kind: str = "ask",
        title: str | None = None,
        to: str | None = None,
        topics: Iterable[str] = (),
    ) -> Item:
        """File a question or instruction for a session (an orchestrator by default)."""
        self._require_nick(actor, "ask")
        if kind not in NICK_KINDS:
            raise AgendaError(f"Nick files {', '.join(NICK_KINDS)}, not {kind!r}")
        text, topics = _required(text, "text"), _clean_topics(topics)
        with self._tx():
            if existing := self._open_for_topics(topics):
                # Asking again about an open topic continues that thread.
                self._add_message(existing.id, NICK, text)
                self._hand_to(existing, OWNER)
                return self._get(existing.id)
            target = self.find_session(to) if to else self.default_orchestrator()
            now = self._now()
            cur = self._db.execute(
                "INSERT INTO items (kind, raised_by, owner, title, summary, waiting_on, waiting_since,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, NICK, target.id, _optional(title) or _shorten(text, 60), text, OWNER, now, now, now),
            )
            self._add_topics(cur.lastrowid, topics)
            self._watch(cur.lastrowid, target.id)
            self._add_message(cur.lastrowid, NICK, text)
        return self._get(cur.lastrowid)

    def question(self, actor: Actor, item_id: int, text: str) -> Item:
        """Nick asks the owner something about an item without deciding it; the owner replies and it comes back."""
        self._require_nick(actor, "ask about an item")
        text = _required(text, "question")
        with self._tx():
            item = self._open(item_id, action="ask about")
            self._add_message(item.id, NICK, text, kind=QUESTION)
            self._hand_to(item, OWNER)
        return self._get(item_id)

    def answer(self, actor: Actor, item_id: int, text: str) -> Item:
        self._require_nick(actor, "answer")
        text = _required(text, "answer")
        with self._tx():
            item = self._open(item_id, action="answer")
            self._add_message(item.id, NICK, text)
            self._hand_to(item, OWNER)
        return self._get(item_id)

    def accept(self, actor: Actor, item_id: int) -> Item:
        """Answer with the session's own recommendation."""
        self._require_nick(actor, "accept")
        with self._tx():
            item = self._open(item_id, action="accept")
            if not item.recommendation:
                raise AgendaError(f"#{item.id} has no recommendation to accept; answer it instead")
            self._add_message(item.id, NICK, f"Go with your recommendation: {item.recommendation}")
            self._hand_to(item, OWNER)
        return self._get(item_id)

    def ack(self, actor: Actor, item_id: int) -> Item:
        """Heard it, nothing more to say: closes the item without waking any session."""
        self._require_nick(actor, "acknowledge")
        with self._tx():
            item = self._open(item_id, waiting_on=NICK, action="acknowledge")
            if item.kind == "task" and not item.nick_has_answered:
                raise AgendaError(f"#{item.id} is a task; once you've done it, use done so its owner can carry on")
            if not item.from_nick and item.kind in ("decision", "question") and not item.nick_has_answered:
                raise AgendaError(f"#{item.id} is a {item.kind} that still needs your answer; use answer or accept")
            self._add_message(item.id, SYSTEM, "Nick acknowledged this.")
            self._finish(item, CLOSED, NICK, ACKNOWLEDGED, None)
        return self._get(item_id)

    def done(self, actor: Actor, item_id: int, note: str | None = None) -> Item:
        """Nick did it himself (a task, or something he just went and did); the owner checks and carries on."""
        self._require_nick(actor, "mark done")
        with self._tx():
            item = self._open(item_id, action="mark done")
            self._add_message(item.id, NICK, _join("Done.", _optional(note)))
            self._hand_to(item, OWNER)
        return self._get(item_id)

    def defer(self, actor: Actor, item_id: int, until: datetime) -> Item:
        self._require_nick(actor, "defer")
        if until <= self._clock():
            raise AgendaError("defer until a time in the future")
        with self._tx():
            item = self._open(item_id, waiting_on=NICK, action="defer")
            self._add_message(item.id, SYSTEM, "Nick deferred this.")
            self._set(item.id, deferred_until=iso(until))
        return self._get(item_id)

    def drop(self, actor: Actor, item_id: int, reason: str | None = None) -> Item:
        """Nick closes an item outright. Sessions are told, unless none has read it yet."""
        self._require_nick(actor, "drop")
        reason = _optional(reason)
        with self._tx():
            item = self._open(item_id, action="drop")
            unseen = item.from_nick and all(upto == 0 for upto in item.watchers.values())
            if unseen:
                self._mark_read(item.id, None)
                self._add_message(item.id, SYSTEM, _join("Nick dropped this before anyone read it.", reason))
            else:
                self._add_message(item.id, NICK, _join("Dropped.", reason))
            self._finish(item, CLOSED, NICK, DROPPED, reason)
        return self._get(item_id)

    def queue(self, *, include_deferred: bool = False) -> list[Item]:
        """What is waiting on Nick, in the order to raise it."""
        self.sweep()
        sql = "SELECT * FROM items WHERE status = ? AND waiting_on = ?"
        params: tuple = (OPEN, NICK)
        if not include_deferred:
            sql += " AND (deferred_until IS NULL OR deferred_until <= ?)"
            params += (self._now(),)
        return sorted(self._items(sql, params), key=lambda i: (queue_rank(i), i.waiting_since or "", i.id))

    def deferred_count(self) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM items WHERE status = ? AND waiting_on = ? AND deferred_until > ?",
            (OPEN, NICK, self._now()),
        ).fetchone()[0]

    # --- Either -------------------------------------------------------------

    def get(self, item_id: int) -> Item:
        self.sweep()
        return self._get(item_id)

    def read(self, actor: Actor, item_id: int) -> Item:
        """`show`: the whole item. A session reading it becomes a watcher and has now seen everything on it."""
        item = self.get(item_id)
        if not actor.is_nick:
            self.touch(actor)
            with self._tx():
                self._watch(item.id, actor.id)
                self._mark_read(item.id, actor.id)
        return item

    def history(self, limit: int = 20) -> list[Item]:
        return self._items(
            "SELECT * FROM items WHERE status != ? ORDER BY closed_at DESC, id DESC LIMIT ?", (OPEN, limit)
        )

    def sweep(self) -> list[int]:
        """Apply defaults whose deadline has passed unanswered; returns the item ids."""
        due = "SELECT id FROM items WHERE status = ? AND waiting_on = ? AND default_at <= ?"
        if not self._db.execute(due, (OPEN, NICK, self._now())).fetchone():
            return []  # the common case: no write lock needed
        with self._tx():
            rows = self._db.execute(
                "SELECT id FROM items WHERE status = ? AND waiting_on = ? AND default_at <= ?",
                (OPEN, NICK, self._now()),
            ).fetchall()
            for row in rows:
                item = self._get(row["id"])
                self._add_message(
                    item.id, AGENDA, f"No answer from Nick by the deadline, so the default applies: {item.default_option}"
                )
                self._hand_to(item, OWNER)
                self._set(item.id, default_at=None, default_option=None)
        return [r["id"] for r in rows]

    # --- Internals ----------------------------------------------------------

    def _now(self) -> str:
        return iso(self._clock())

    @contextmanager
    def _tx(self) -> Iterator[None]:
        # IMMEDIATE takes the write lock up front, so two writers can't both read
        # an item and then act on the same stale state. Nested calls join the outer one.
        if self._db.in_transaction:
            yield
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    def _migrate(self) -> None:
        if self._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            return  # every command and hook opens the agenda, so the common case takes no write lock
        with self._tx():
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version > SCHEMA_VERSION:
                raise AgendaError(f"{self.path} is schema v{version}; this agenda only knows v{SCHEMA_VERSION}")
            if version == 1:
                # v1 had a single anonymous "orchestrator", which can't be mapped onto
                # owner sessions. It was only live for an afternoon, so start over if empty.
                if self._db.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
                    raise AgendaError(f"{self.path} is a v1 agenda with items; move it aside to start v2")
                self._db.execute("DROP TABLE messages")
                self._db.execute("DROP TABLE items")
                version = 0
            if version == 0:
                for statement in SCHEMA.split(";"):
                    self._db.execute(statement)
            if version == 2:
                # Briefings and Nick's questions (30 Sep); the live agenda already held items, so add in place.
                self._db.execute("ALTER TABLE items ADD COLUMN briefing TEXT")
                self._db.execute("ALTER TABLE messages ADD COLUMN kind TEXT CHECK (kind IN ('question'))")
                self._db.execute("CREATE TABLE nudges (session TEXT PRIMARY KEY, upto INTEGER NOT NULL, at TEXT NOT NULL)")
                version = 3
            if version == 3:
                # Calls that run like a meeting (30 Sep): project, feature, context, quick sign-offs, attachments.
                for column in ("project TEXT", "feature TEXT", "context TEXT", "quick INTEGER NOT NULL DEFAULT 0"):
                    self._db.execute(f"ALTER TABLE items ADD COLUMN {column}")
                for statement in SCHEMA.split(";"):
                    if "CREATE TABLE projects" in statement or "CREATE TABLE attachments" in statement:
                        self._db.execute(statement)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _get(self, item_id: int) -> Item:
        row = self._db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise AgendaError(f"no item #{item_id}")
        return self._hydrate([row])[0]

    def _items(self, sql: str, params: tuple) -> list[Item]:
        return self._hydrate(self._db.execute(sql, params).fetchall())

    def _hydrate(self, rows: list[sqlite3.Row]) -> list[Item]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        messages: dict[int, list[Message]] = defaultdict(list)
        for row in self._db.execute(f"SELECT * FROM messages WHERE item_id IN ({marks}) ORDER BY id", ids):
            messages[row["item_id"]].append(Message(**dict(row)))
        watchers: dict[int, dict[str, int]] = defaultdict(dict)
        for row in self._db.execute(f"SELECT * FROM watchers WHERE item_id IN ({marks})", ids):
            watchers[row["item_id"]][row["session"]] = row["read_upto"]
        topics: dict[int, list[str]] = defaultdict(list)
        for row in self._db.execute(f"SELECT * FROM item_topics WHERE item_id IN ({marks}) ORDER BY rowid", ids):
            topics[row["item_id"]].append(row["topic"])
        attachments: dict[int, list[Attachment]] = defaultdict(list)
        for row in self._db.execute(f"SELECT * FROM attachments WHERE item_id IN ({marks}) ORDER BY id", ids):
            attachments[row["item_id"]].append(Attachment(**dict(row)))
        names = {r["id"]: r["label"] for r in self._db.execute("SELECT id, label FROM sessions") if r["label"]}
        items = []
        for row in rows:
            data = dict(row)
            data["options"] = json.loads(data["options"])
            data["blocking"] = bool(data["blocking"])
            data["quick"] = bool(data["quick"])
            items.append(
                Item(
                    **data, topics=topics[row["id"]], attachments=attachments[row["id"]], messages=messages[row["id"]],
                    watchers=watchers[row["id"]], names=names,
                )
            )
        return items

    def _name(self, session_id: str) -> str:
        row = self._db.execute("SELECT label FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return (row and row["label"]) or session_id[:8]

    def _role(self, session_id: str) -> str | None:
        row = self._db.execute("SELECT role FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return row and row["role"]

    def _require_nick(self, actor: Actor, action: str) -> None:
        if not actor.is_nick:
            raise AgendaError(
                f"only Nick can {action}. If he gave you his answer somewhere else, record it with "
                "`agenda close ID \"Nick said: ...\"` (or `agenda note ID ...` to add it without closing)"
            )

    def _require_session(self, actor: Actor, action: str) -> None:
        if actor.is_nick:
            raise AgendaError(f"sessions {action}; Nick uses answer, accept, ack, defer, drop and ask")

    def _open(self, item_id: int, *, action: str, waiting_on: str | None = None) -> Item:
        item = self._get(item_id)
        if item.status != OPEN:
            raise AgendaError(f"#{item.id} is already {item.status}")
        if waiting_on and item.waiting_on != waiting_on:
            whom = "Nick" if item.waiting_on == NICK else self._name(item.owner)
            raise AgendaError(f"can't {action} #{item.id}: it is waiting on {whom}")
        return item

    def _acting(self, actor: Actor, item_id: int, *, action: str) -> Item:
        """An open item this session may settle: its own, or any item for an orchestrator."""
        self._require_session(actor, action)
        self.touch(actor)
        self.sweep()
        item = self._open(item_id, action=action)
        if actor.id != item.owner and self._role(actor.id) != ORCHESTRATOR:
            raise AgendaError(
                f"#{item.id} belongs to {self._name(item.owner)}; add context with `agenda note {item.id} ...` "
                "or ask its owner"
            )
        if news := item.news_for(actor.id):
            raise AgendaError(
                f"#{item.id} has {len(news)} message(s) you haven't read; run `agenda show {item.id}` first"
            )
        return item

    def _open_for_topics(self, topics: list[str]) -> Item | None:
        if not topics:
            return None
        marks = ",".join("?" * len(topics))
        rows = self._db.execute(
            f"SELECT DISTINCT i.* FROM items i JOIN item_topics t ON t.item_id = i.id"
            f" WHERE i.status = ? AND t.topic IN ({marks})",
            (OPEN, *topics),
        ).fetchall()
        if len(rows) > 1:
            listed = ", ".join(f"#{r['id']}" for r in rows)
            raise AgendaError(f"those topics are open on different items ({listed}); join one with --into ID")
        return self._hydrate(rows)[0] if rows else None

    def _settled_for_topics(self, topics: list[str]) -> Item | None:
        """The most recent closed item on any of these topics (withdrawn ones don't count)."""
        if not topics:
            return None
        marks = ",".join("?" * len(topics))
        row = self._db.execute(
            f"SELECT i.* FROM items i JOIN item_topics t ON t.item_id = i.id"
            f" WHERE i.status = ? AND t.topic IN ({marks}) ORDER BY i.closed_at DESC, i.id DESC LIMIT 1",
            (CLOSED, *topics),
        ).fetchone()
        return self._hydrate([row])[0] if row else None

    def _add_topics(self, item_id: int, topics: list[str]) -> None:
        for topic in topics:
            other = self._open_for_topics([topic])
            if other is not None and other.id != item_id:
                raise AgendaError(f"topic {topic!r} is already open as #{other.id}")
            self._db.execute("INSERT OR IGNORE INTO item_topics (topic, item_id) VALUES (?, ?)", (topic, item_id))

    def _lookalikes(self, title: str, summary: str, topics: list[str]) -> tuple[list[Item], list[Item]]:
        """(open items it resembles, recently settled items it resembles).

        Settled items don't match on a shared #number alone: a new question about a PR whose last question
        was settled is normal.
        """
        refs, words, topic_words = _refs(" ".join([title, summary, *topics])), _words(title), _words(" ".join(topics))
        since = iso(self._clock() - timedelta(days=30))
        open_like, settled_like = [], []
        for item in self._items(
            "SELECT * FROM items WHERE status = ? OR (status = ? AND closed_at >= ?) ORDER BY id",
            (OPEN, CLOSED, since),
        ):
            similar = _similar(words, _words(item.title)) or _similar(topic_words, _words(" ".join(item.topics)))
            if item.status == OPEN and (similar or refs & _refs(" ".join([item.title, item.summary, *item.topics]))):
                open_like.append(item)
            elif item.status == CLOSED and similar:
                settled_like.append(item)
        return open_like, settled_like

    def _join(
        self, actor: Actor, item: Item, *, summary: str, topics: list[str], blocking: bool, watchers: list[str]
    ) -> None:
        for session in (actor.id, *watchers):
            self._watch(item.id, session)
        self._add_topics(item.id, [t for t in topics if t not in item.topics])
        changes: dict = {}
        if actor.id == item.owner:
            # The owner raising its own topic again is progress: one item, latest word on top.
            if summary != item.summary:
                changes["summary"] = summary
                self._add_message(item.id, actor.id, f"Update: {summary}")
        else:
            self._add_message(item.id, actor.id, f"Also raised by {self._name(actor.id)}: {summary}")
        if blocking and not item.blocking and item.kind != "fyi":
            changes.update(blocking=1, deferred_until=None)
            self._add_message(item.id, SYSTEM, f"{self._name(actor.id)} marked this blocking.")
        self._set(item.id, **changes)

    @property
    def files_dir(self) -> Path:
        return self.path.parent / "files"

    def _project_name(self, name: str) -> str:
        """The project's established spelling, registering it the first time: 'member hub' becomes 'Member Hub'."""
        key = _project_key(name)
        if not key:
            raise AgendaError("the project name needs letters or digits")
        row = self._db.execute("SELECT name FROM projects WHERE key = ?", (key,)).fetchone()
        if row:
            return row["name"]
        self._db.execute("INSERT INTO projects (key, name) VALUES (?, ?)", (key, name.strip()))
        return name.strip()

    def _feature_name(self, project: str, feature: str) -> str:
        key = _project_key(feature)
        for row in self._db.execute("SELECT DISTINCT feature FROM items WHERE project = ? AND feature IS NOT NULL", (project,)):
            if _project_key(row["feature"]) == key:
                return row["feature"]
        return feature.strip()

    def _classify(self, item: Item, *, project: str | None = None, feature: str | None = None, context: str | None = None) -> None:
        changes: dict = {}
        if project:
            changes["project"] = self._project_name(project)
        target = changes.get("project") or item.project
        if feature:
            if not target:
                raise AgendaError("give the project along with the feature")
            changes["feature"] = self._feature_name(target, feature)
        if context:
            changes["context"] = _optional(context, "context")
        if changes:
            self._set(item.id, **changes)

    def _attach(self, item_id: int, actor: Actor, sources: list[tuple[str, str]]) -> None:
        """Copy files into the agenda's own folder (sessions' scratch folders get cleaned) or keep a link."""
        for kind, value in sources:
            if kind == "url":
                self._db.execute(
                    "INSERT INTO attachments (item_id, name, url, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
                    (item_id, value, value, actor.id, self._now()),
                )
                continue
            source = Path(value)
            folder = self.files_dir / str(item_id)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{len(list(folder.iterdir())) + 1}-{source.name}"
            shutil.copyfile(source, target)
            self._db.execute(
                "INSERT INTO attachments (item_id, name, path, mime, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, source.name, str(target), mimetypes.guess_type(source.name)[0], actor.id, self._now()),
            )

    def _watch(self, item_id: int, session: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO watchers (item_id, session) VALUES (?, ?)", (item_id, session))

    def _add_message(self, item_id: int, author: str, body: str, kind: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO messages (item_id, author, body, created_at, kind) VALUES (?, ?, ?, ?, ?)",
            (item_id, author, body, self._now(), kind),
        )

    def _mark_read(self, item_id: int, session: str | None) -> None:
        """Move read cursors to the latest message: one session's, or every watcher's."""
        last = self._db.execute("SELECT MAX(id) FROM messages WHERE item_id = ?", (item_id,)).fetchone()[0] or 0
        sql, params = "UPDATE watchers SET read_upto = ? WHERE item_id = ?", (last, item_id)
        if session is not None:
            sql, params = sql + " AND session = ?", (*params, session)
        self._db.execute(sql, params)

    def _set(self, item_id: int, **columns) -> None:
        columns["updated_at"] = self._now()
        assignments = ", ".join(f"{name} = ?" for name in columns)
        self._db.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*columns.values(), item_id))

    def _hand_to(self, item: Item, who: str) -> None:
        changes: dict = {"waiting_on": who, "deferred_until": None}
        if item.waiting_on != who:
            changes["waiting_since"] = self._now()
        self._set(item.id, **changes)

    def _finish(self, item: Item, status: str, by: str, outcome: str, resolution: str | None) -> None:
        self._set(
            item.id, status=status, waiting_on=None, waiting_since=None, deferred_until=None,
            default_option=None, default_at=None, outcome=outcome, resolution=resolution,
            closed_by=by, closed_at=self._now(),
        )


# A #number, or a number after pr/issue/pull. Support tickets share the #N space with PRs,
# so a match is only ever grounds to ask "is this the same?", never to merge on its own.
_REF = re.compile(r"#(\d{2,})|\b(?:pr|issue|pull)\s*#?\s*(\d{2,})\b", re.IGNORECASE)
_STOP = frozenset(["the", "a", "an", "and", "or", "to", "of", "for", "on", "in", "is", "it", "this", "that", "with", "we", "do", "should", "now", "be"])


def _refs(text: str) -> set[str]:
    return {a or b for a, b in _REF.findall(text)}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


def _project_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


MAX_ATTACHMENT = 15 * 1024 * 1024


def _attachment_source(value: str) -> tuple[str, str]:
    """('url', link) or ('file', absolute path); files must exist and be small enough to email."""
    value = (value or "").strip()
    if re.match(r"https?://", value):
        return "url", _no_secrets(value, "link")
    path = Path(value).expanduser()
    if not path.is_file():
        raise AgendaError(f"can't attach {value!r}: no such file (give a file path or an http(s) link)")
    if path.stat().st_size > MAX_ATTACHMENT:
        raise AgendaError(f"can't attach {path.name}: over 15 MB; attach a smaller render or a link")
    return "file", str(path.resolve())


def _similar(a: set[str], b: set[str]) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= 0.6


def _clean_topics(topics: Iterable[str]) -> list[str]:
    out: list[str] = []
    for topic in topics:
        if (norm := normalize_topic(topic or "")) and norm not in out:
            out.append(norm)
    return out


# Common token shapes. The agenda is read aloud and shown on a phone, so a secret must live in a
# secrets manager and be referred to by name.
_SECRET = re.compile(
    r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}|\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bre_[A-Za-z0-9]{20,}|\bdp\.(?:st|pt|sa|ct)\.[A-Za-z0-9_]{20,}|\b[a-z0-9]+(?:_[a-z0-9]+)*_pat_[A-Za-z0-9_]{8,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."
)


def _no_secrets(value: str, name: str) -> str:
    if _SECRET.search(value):
        raise AgendaError(f"the {name} looks like it contains a secret; keep it in your secrets manager and refer to it by name")
    return value


def _required(value: str | None, name: str) -> str:
    value = (value or "").strip()
    if not value:
        raise AgendaError(f"{name} can't be empty")
    return _no_secrets(value, name)


def _optional(value: str | None, name: str = "text") -> str | None:
    return _no_secrets((value or "").strip(), name) or None


def _clean_options(options: Iterable[str]) -> list[str]:
    return [_no_secrets(o.strip(), "option") for o in options if o and o.strip()]


def _shorten(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1].rstrip() + "…"


def _join(first: str, rest: str | None) -> str:
    return f"{first} {rest}" if rest else first
