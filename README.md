# agenda

A durable queue of topics between Nick and his agent sessions in herdr: orchestrators and lanes.

This is the first piece of a hands-off way to steer herdr by voice. The sessions keep doing all the
thinking. Voice and a small web page will be thin front ends over this queue.

## Why

A session's terminal is a stream, and Nick is only there some of the time. An answer given while he was away
scrolls off, and after compaction a session can forget it ever asked. Reading two orchestrator sessions'
transcripts (30 Sep 2026) turned up four problems:

- **Asks got buried or went stale.** They waited hours when they were tacked onto the end of a status post.
  One kept asking Nick to "say deploy" after he had already deployed.
- **Compactions dropped open items.** Five compactions dropped about 14 open items, and one was recorded as
  "Answered: no" when it never was.
- **Duplicates.** Five lanes raised the same "who merges?" question within a minute. A question Nick had
  settled and locked kept coming back.
- **Lists rebuilt by hand.** Orchestrators rebuilt "pending Nick" lists by regex-searching lane transcripts.

## Model

- **Who is who is detected, not claimed.** Inside a Claude Code session, or a herdr pane running an agent
  (which also catches Codex), you are that session. It is keyed by its own session id and named by its herdr
  tab label. Anywhere else you are Nick. `AGENDA_SESSION` overrides this, for tests and for agents that
  herdr doesn't see.
- **Nick answers.** Only he can `answer`, `accept`, `ack`, `done`, `defer`, `drop` and `ask`, so a session
  can never approve its own recommendation.
- **Sessions raise.** A session raises a `decision`, `question`, `task` (needs Nick at a computer) or `fyi`.
  Nick files an `ask` or an `instruction` for one session. With several orchestrators registered he names
  which one; with one, it goes to that one.
- **Owner and watchers.** Every item has one owner, the session that acts on the outcome, and any number of
  watchers. While open it waits on Nick or on its owner. Orchestrators (`agenda register`) may act on any
  item; a lane only on its own.
- **How an item ends:**

  | Outcome | Meaning |
  | --- | --- |
  | `answered` | Nick answered and the owner acted |
  | `done` | an ask or instruction was carried out |
  | `default` | the deadline passed with no answer and the owner went with the default |
  | `elsewhere` | settled outside the agenda, with Nick's words in the note |
  | `moot` | withdrawn |
  | `acknowledged` | Nick heard it |
  | `dropped` | Nick closed it outright |

- **Nick's queue order:** blocking items first, then replies to his own asks, then decisions, questions,
  tasks and FYIs. Within each group, whatever has waited longest comes first. Deferred items stay hidden
  until their time.
- **Two texts per item.** `summary` is written to be heard (a sentence or two); `detail` is markdown for a
  screen.

Rules the store enforces, each from something that went wrong:

1. **Per-session read cursors.** Each watcher has its own, and only `inbox` moves it.
2. **No settling on unread news.** A session can't reply to, close, update, withdraw or hand off an item that
   has news it hasn't read. It is told to run `agenda inbox` first, so it never acts on an answer Nick has
   since changed.
3. **One open item per topic.** An item can carry several topics, for example `ci-runner-kill` and later
   `pr webapp#4573`.
   - Raising an open topic joins that item as a watcher.
   - When the owner raises its own topic again, that is a progress update: the summary is replaced in place.
   - Raising something that only *looks* the same (it shares a #number, or most of its title) is refused
     until the session joins with `--into` or says `--distinct`.
4. **Settled topics stay settled.** Raising a topic that a closed item carries is refused with the earlier
   resolution unless `--reask`.
5. **Silence is never consent.** A decision can carry `--default X --by 2h`. When the deadline passes
   unanswered, the agenda tells the owner that the default applies.
6. **Settled elsewhere is recorded.** Closing an item that is still waiting on Nick needs a note saying how it
   was settled, in his words.
7. **No secrets.** Token-shaped text is refused, because items are read aloud and shown on a phone.
8. **No drift.** Rewording keeps the old wording in the thread.

## Commands

```sh
# Nick
agenda list                      # what is waiting on me, in order (--deferred to include those)
agenda show 14                   # one item in full: owner, watchers, topics, thread, outcome
agenda answer 14 wait for Sam  # reply; hands it to the owner
agenda accept 14 15 16           # "go with your recommendation", one or many
agenda ack 14                    # heard it; closes it (decisions need an answer first)
agenda done 14 it's in Doppler   # I did the task; the owner checks and carries on
agenda defer 14 2h               # 30m, 2h, 1d, tomorrow (8am), or an ISO time
agenda drop 14 not doing this    # close outright; sessions are told
agenda ask --to merge-queue why is CI red   # or --instruction

# Sessions
agenda register                  # I'm an orchestrator (--lane to undo)
agenda inbox                     # news on my items; marks it read (--peek, --count)
agenda mine                      # everything I own or watch: read after a compaction
agenda owed [--all]              # waiting on me (or on anyone)
agenda raise decision "Merge webapp #4579?" --summary "..." \
  --option "Merge now" --option "Wait for Sam" --recommend "Wait for Sam" \
  --topic "pr webapp#4579" [--blocking] [--default "wait" --by 2h] [--cc ledger]
agenda update 14 --summary "..." --topic "alias"   # revise, add topic aliases
agenda reply 21 "Flaky shard 2 test; the rerun is green."
agenda note 14 "Sam is out until Thursday."      # context, no change of turn
agenda close 14 "Merged 3f2a1c."                   # or: "Nick in the terminal: ..."
agenda withdraw 14 "Nick deployed himself."
agenda handoff 14 ledger
agenda watch 14

# Anyone
agenda history · agenda sessions · agenda whoami
```

Every command takes `--json`. Item numbers can be written `14` or `#14`.

## The page

`agenda/web.py` serves Nick's view: "For you" cards with his verbs as buttons, "With sessions", and
"Settled". It has search and works at phone width.

- **Local:** loopback on :6110, run by the user service `deploy/agenda-web.service`, which is symlinked
  into `~/.config/systemd/user`.
- **Tailnet:** exposed with `tailscale serve --bg --https=10480 http://127.0.0.1:6110`, so the page is at
  `https://<machine>.<tailnet>.ts.net:10480`.
- **Who can use it:** every request must carry `Tailscale-User-Login` equal to `AGENDA_REQUIRE_USER`, which
  tailscale serve stamps on each proxied request. A request straight to :6110 gets a 403. The server won't
  start without that setting unless you pass `--allow-anyone` (for a scratch copy).

## For sessions

The `agenda` skill (`skills/agenda/SKILL.md`, symlinked to `~/.claude/skills/agenda`) teaches sessions
when to raise, how to word and key items, and how to settle them. It is the one home for that guidance.

## Storage

- **Database:** `~/.local/share/agenda/agenda.db` (override with `AGENDA_DB` or `--db`), SQLite in WAL mode.
- **Runtime:** standard-library Python under `/usr/bin/python3`, because sessions run from `~`, where mise
  pins no interpreter.
- **Install:** `bin/agenda` is symlinked into `~/.local/bin`.
- **Front ends** import `agenda.store.Agenda` and pass an `Actor` rather than shelling out.

## Setup

Needs Linux or macOS with `/usr/bin/python3` (3.12+; the standard library only), Claude Code sessions (herdr
for delivery to idle sessions), and Tailscale for the page. Voice needs an ElevenLabs account; screenshot
emails need Resend.

1. **Install the CLI and the skill.** Clone to `~/dev/agenda`, then:

   ```sh
   ln -s ~/dev/agenda/bin/agenda ~/.local/bin/agenda
   ln -s ~/dev/agenda/skills/agenda ~/.claude/skills/agenda
   ```

2. **Write your settings** to `~/.config/agenda/env`. The keys are listed in `agenda/settings.py`. Put no
   secrets there: API keys come from the environment or from Doppler.
3. **Run the page** with `deploy/agenda-web.service`, then expose it with `tailscale serve`.
4. **Deliver news to sessions.** Run `deploy/agenda-deliver.service`, and add the two hooks to
   `~/.claude/settings.json`:
   - `Stop` → `~/dev/agenda/bin/agenda-hook stop`
   - `SessionStart` (matcher `startup|resume|compact|clear`) → `~/dev/agenda/bin/agenda-hook session-start`
5. **Voice:** run `python3 -m agenda.voice sync` with `ELEVENLABS_API_KEY` set.

## Development

```sh
python3 -m unittest
```

## Delivery

When Nick acts on an item, the owning session and the sessions watching it are told three ways:

- **Stop hook** (`agenda-hook stop` in `~/.claude/settings.json`). When a turn ends and the session has
  news it hasn't been told about, the hook keeps it going with a short summary. Each piece of news is told
  once, and `stop_hook_active` guards against a loop.
- **Delivery service** (`deploy/agenda-deliver.service`). For sessions sitting idle, it types a nudge into
  the session's herdr pane every 15 seconds at most. It does this only when:
  - the pane still runs that session and is idle;
  - the input box has no text of Nick's. Dim suggestion text is fine; plain text is a draft, and
    `herdr agent prompt` would replace it.
  - there is news newer than the last nudge.
- **SessionStart hook** (`agenda-hook session-start`). After startup, resume, compact or clear, it lists
  the session's open items so a compaction can't lose them.

The hooks run in every Claude Code session on the machine. They only read, stay silent for sessions that
have never used the agenda, never register anyone, and exit 0 on any error. Codex sessions aren't nudged
yet.

## Calls run like a meeting

Every item is filed:

- **Project and feature**, required for new items. `agenda projects` lists the names in use.
- **Context**: one or two sentences on what this is and why it has come up.
- **Quick** (`--quick`): marks a high-confidence recommendation that a quick sign-off can settle.
- **Attachments** (`--attach FILE|URL`): screenshots are copied into `~/.local/share/agenda/files`. A design
  sign-off needs them.

Each orchestrator keeps a project brief current (`agenda project NAME --brief …`). `agenda/plan.py` turns
the queue into a meeting: projects in order, each project's features, and within each feature these groups
in turn:

1. blocking;
2. answers to Nick's questions;
3. quick sign-offs;
4. to discuss;
5. needs him at a computer;
6. updates.

The page and the voice agent both follow that order. The agent works through it like this:

- It opens with the shape of the call.
- It starts each project with its brief and the features it will cover.
- It gives each item its context before the ask.
- It batches quick sign-offs into one yes.
- Before a design sign-off, it emails Nick the screenshots (`agenda/mail.py`, through Resend, only ever to
  `AGENDA_MAIL_TO`). Without screenshots it asks the owner for them instead.

## Voice

The **Talk** button on the page starts a call with an ElevenLabs agent that walks Nick through the agenda.

- **Where it runs.** The agent runs in ElevenLabs' cloud: speech-to-text, then Claude Sonnet 5 (Haiku 4.5 as
  the backup), then Eleven v4 Turbo. It hosts the meeting; the sessions stay the brains.
- **What it reads.** At the start of a call it gets a compact overview (`agenda.voice.overview`). It fetches
  an item's full detail and briefing (`item_text`) only when it gets to that item.
- **How it acts.** Its tools run in Nick's browser and reach this server over the tailnet, so nothing is
  exposed to the internet:
  - it can accept, answer, ask the owner, mark heard or done, defer, drop, and ask a session;
  - it reads back every accept, answer and drop before recording it.
- **Replies during a call.** When a session answers a question Nick asked during the call, the page passes
  the reply to the agent as an "AGENDA UPDATE", and the agent raises it at the next pause.
- **Privacy.** The agent is private: a call needs a token that only this server mints. Audio isn't
  recorded, and transcripts are kept 30 days.
- **Changing the agent.** Its whole configuration is `agenda/voice.py` (prompt, model, voice, tools). After
  changing it, run:

  ```sh
  ELEVENLABS_API_KEY=... python3 -m agenda.voice sync   # or under `doppler run`; creates the agent the first time
  ```

  The agent id is kept in `~/.config/agenda/voice.json`. The page needs `ELEVENLABS_API_KEY` to mint call
  tokens: `bin/agenda-web` runs it under `doppler run` when `AGENDA_DOPPLER` is set.

**Measured on the first test call** (30 Sep, 3 minutes, against a scratch copy of the agenda):

- Sonnet 5 starts speaking in 0.5 to 0.9 s, and deciding on a tool call takes 1.0 to 1.7 s.
- From Nick finishing to hearing audio: about 2.5 s on a plain turn, and 2.1 to 3.3 s with a tool call.
- The LLM cost $0.18, and the call used about 1,400 ElevenLabs credits.

## Next

1. **Phone.** A phone number with a PIN, plus an outbound call when a blocking item has waited too long.
   The tools then run as webhooks instead of in the browser, which needs a public endpoint.
2. **Codex delivery.** Detect the Codex input box, and add a hook if Codex grows one.
3. **Stale items.** Check external state (a PR merged or closed, a deploy ran) and flag asks that events
   have overtaken.
