---
name: agenda
description: "Puts decisions, questions, hands-on tasks and important updates for Nick on a durable agenda (the `agenda` CLI), one item per topic shared across sessions, so they survive scrolling and compaction and get settled properly. Use in any herdr session, orchestrator or lane, when you need Nick's decision, answer or action, when you'd otherwise write \"waiting on you\" in a status post, when Nick says \"put it on the agenda\", and after a compaction to see what you still owe or are waiting on."
---

# The agenda

Nick runs dozens of sessions in herdr and reads none of them continuously. Anything you ask him in your
terminal scrolls away under later output, and after a compaction you may forget you asked. The agenda keeps
every exchange with Nick as an item with a thread and a status, in one place he (and later his voice and web
front ends) can go through. Your job with it: put on it only what really needs Nick, word it so he can decide
without scrolling back, keep one item per topic across every session, read his answers, and close items
honestly when they are settled.

## What you need to know

- The CLI is `agenda` (on PATH; the repo is `~/dev/agenda`). `agenda <command> --help` covers every flag.
  The model — owners, watchers, outcomes, the rules the store enforces — is in `~/dev/agenda/README.md`.
- It knows who you are: inside a Claude Code session (or a herdr pane running an agent) you are that session,
  named by your herdr tab label. `agenda whoami` shows it. Nick's verbs (answer, accept, ack, done, defer,
  drop, ask) refuse you; if he told you something, you record it on the item yourself (see Settling).
- Every item has one **owner** (the session that acts on the outcome, normally whoever raised it) and any
  number of **watchers**. While open it waits on Nick or on its owner. Orchestrators may act on any item; a
  lane only on its own.
- **How Nick and you are connected.** Nick works through the agenda page (its address is `AGENDA_PAGE_URL`
  in `~/.config/agenda/env`) and by voice.
  - When he answers, you are told: at the end of your turn (a Stop hook hands you the news), or with a
    short nudge typed into your pane if you are idle.
  - After a restart or compaction, a SessionStart note lists your open items.
  - If he is talking to you in your terminal, also mention a new item there with its number ("I've put
    this on the agenda as #14").
  - Questions for him go on the agenda, not by email, unless he asks for an email.

Look around before raising: `agenda list` (waiting on Nick), `agenda owed --all` (answered by Nick, waiting
on a session to act) and `agenda history` (settled). Someone may already have raised it, or Nick may already
have answered or settled it. If your need is already open, add yourself to it with
`agenda note ID "<why you need it too>"`: you become a watcher, and its owner and Nick see that you need it
as well. Any session may note any item; keep notes to facts that bear on the decision.

## Done looks like

- Everything you need from Nick is an open item with a clear summary, the right kind and a topic.
- You read his answers (`agenda inbox`) and acted on them, and every item you own is closed or withdrawn once
  it is settled, with the outcome in Nick's words where there are any.
- Nothing is on the agenda that you could have found out or done yourself.

## Raising

```bash
agenda raise decision "Merge webapp #4579 (posting table)?" \
  --project "Clinic App" --feature "Ledger" \
  --context "The posting table replaces how Clinic App ages patient balances; it's the fix for Harbour Dental's wrong AR report." \
  --summary "The posting table PR is green. Merge now, or wait for Sam's review on Thursday?" \
  --option "Merge now" --option "Wait for Sam" \
  --recommend "Wait for Sam: it rewrites AR history for Harbour Dental" \
  --topic "pr webapp#4579" --blocking
```

- **File it so the call can run like a meeting.** Nick goes through the agenda project by project and
  feature by feature. Every new item needs:
  - `--project`: use a name from `agenda projects` if one fits, such as "Member Hub", "Clinic App" or "Insights".
  - `--feature`: the workstream within the project, such as "Campaigns", "Releases" or "Northside virtual
    consults".
  - `--context`: one or two sentences for someone who hasn't followed your session. Say what this is and why
    it has come up now. A bad opening names a system or a detail with no word on what it is or why it
    matters ("the agent host has kept no chat history since yesterday").

  The store refuses a new item without all three. If the agenda asks you to file an older item, do it with
  `agenda update ID --project … --feature … --context …`.
- **Mark quick sign-offs.** Add `--quick` when your recommendation is high confidence, low risk and easy to
  undo, so Nick can approve it together with others in one go. Anything he would want to think about, or that
  can't be taken back, stays unmarked and gets discussed.
- **Designs need screenshots.** A decision that asks Nick to approve a design, board or UI must attach what
  he's approving: `--attach path/to/render.png` (the file is copied into the agenda) or
  `--attach https://…` for a Paper board or preview link. On a call the agent emails him the screenshots
  before asking. Without them it won't ask for the sign-off, and it'll come back to you for them.
- **Kinds.** `decision` (a choice: give options and your recommendation), `question` (open-ended),
  `task` (only Nick can do it, at a computer: a 2FA code, pasting a key into Doppler, an OAuth consent; say
  exactly why only he can), `fyi` (only when it changes what Nick would do).
- **Check first that it needs Nick.** Answer it yourself from the code, data, email or common sense if you
  can, and mark it inferred. Nick has pushed back hard on asks he considered unnecessary.
- **The summary is read aloud.** One or two sentences that make sense with no scrollback: what, why it needs
  him, what happens next. Name things fully: the repo with a PR number, the design file name with a board
  number and its link or render path (or say there isn't one yet), the customer by name. Never refer to "the 49" or "option 3 above". Long context
  goes in `--detail` or `--detail-file`.
- **Brief a stand-in.** For a decision, question or task, add `--briefing` (or `--briefing-file`): what
  someone standing in for you would need to answer Nick's follow-ups without you. Nick goes through the
  agenda by voice, and the voice agent reads this and never recites it. Include the background, what each
  option leads to, the facts and numbers he may ask for, where things are (PR, board, record), and the three
  to five questions he is most likely to ask, each with its answer. Every follow-up the briefing answers is
  one he doesn't have to wait for.
- **Topics are how sessions find each other's items.** Give every item at least one:
  `pr <repo>#<n>`, `issue <repo>#<n>`, `board <paper file>:<board>`, `practice <name>: <question>`,
  `access <system>: <what>`, `release <repo>`, `deploy <env>`. Add aliases as things get linked
  (`agenda update 14 --topic "pr webapp#4573"`). Don't use migration numbers (they change at rebase)
  or bare board codes like `M1` (several lanes use the same codes).
- **What the store does with a topic.** If it is already open, your raise joins that item: you become a
  watcher and hear how it ends. If you own it, your raise is a progress update instead. If your raise merely
  looks like an open item (same #number, near-identical title), it is refused: join with `--into ID`, or pass
  `--distinct` if it really is a different question. If the topic was already settled, it is refused with
  the earlier answer, and so is a raise that merely looks like something settled in the last 30 days. Ask
  again only with `--reask` and a summary that says what changed, or `--distinct` if it is a different
  question.
- **Never hold your session waiting.** Raise, say so in the terminal, carry on with other work. If you will
  proceed with something unless told otherwise, say so with `--default "<what you'll do>" --by 2h`; silence
  is never consent without a deadline Nick can see. A default must be something you could do without him:
  never a release cut, a deploy, anything that reaches a customer, or a design waiting for his approval,
  which wait for his explicit yes; for those the default is to wait. `--blocking` means work
  has stopped until he answers, so it rarely goes with a default. Give times as Nick's clock time.
- **FYIs only when they change what Nick would do.** Routine progress (a few PRs merged) stays in your
  terminal. When progress does matter to him (a release he is waiting on), keep it on one topic
  (`--topic "release webapp"`) and re-raise that topic to update it in place. Never raise "nothing
  needs you", "watch restarted" or one item per merged PR.
- **Record a decision Nick made off the agenda** when other sessions are likely to ask it again (a policy, a
  "stop asking me about X"): raise it with a topic, then close it straight away with his words. The topic is
  then settled and a later raise gets his answer back. One-off decisions that only affected you need no item.

## Reading answers and settling

- `agenda inbox` shows news on items you own or watch, and marks it read. Check it when you start a turn,
  before you end one in which you raised things, and whenever Nick mentions the agenda. After a compaction
  run `agenda mine` (open items you own or watch), `agenda owed --all` and `agenda history` (what was
  settled recently, with Nick's words), and trust them over your summary; don't rebuild a "pending Nick"
  list from memory.
- **Nick's questions** (shown as "Nick asks" in your inbox) are not decisions. Answer promptly with
  `agenda reply ID ...`; he may be on a voice call waiting for it. Then add the answer to the briefing
  (`agenda update ID --briefing-file ...`) if others are likely to ask the same thing. Don't act as if he
  decided.
- If a command refuses with "messages you haven't read", read the item (`agenda show ID`) before acting.
  Nick may have changed his answer. `show` makes you a watcher, so you will hear how it ends.
- After acting on his answer: `agenda close 14 "Merged as 3f2a1c."`
- Settled somewhere else: in your terminal, another session, GitHub, or Nick simply did it. Close it with
  his words, verbatim: `agenda close 14 "Nick in the terminal: go with option 1"`. A partial answer is a
  `note`, not a close.
- If Nick's answer contradicts one of his standing rules or an earlier decision, don't pick one yourself:
  say so on the item (`agenda reply ID ...` if you own it, `agenda note` if not), quoting both.
- If you find your ask rests on a wrong premise (the access you asked for isn't needed after all), withdraw or
  reword it and raise what you actually need.
- No longer needed (superseded, a no-op, he did it himself before you asked):
  `agenda withdraw 14 "Nick deployed production himself at 14:33"`.
- Nick's own asks to you (`NEW ask` in your inbox): answer a question with `agenda reply ID ...` and leave
  it; it's his turn, and he acks it when he has heard it. Carry out an instruction, then
  `agenda close ID "what you did"`.

## Orchestrators

- Run `agenda register` once; it marks your session as an orchestrator so Nick's `agenda ask` reaches you.
- Keep your project's brief current: `agenda project "Member Hub" --brief "…"`. Write three to five sentences:
  the workstreams in flight, what shipped recently, and what's waiting on Nick overall. The call opens each
  project with it. Update it when the workstreams change, and at least once a day while you're active.
- Tell your lanes to raise their own questions for Nick on the agenda, with topics, instead of relaying them
  through you. The same question from five lanes then becomes one item with five watchers. Nick's answer is
  the authority: a lane can read it with `agenda show ID`, so you don't need to relay it word for word.
- You can act on any item: `agenda handoff ID <lane>` to give it to the lane doing the work, and `--cc <lane>`
  when raising to tell a lane the outcome. Leave a lane's item alone while the lane still has to act on
  Nick's answer; close or withdraw it yourself only when nothing is left for the lane to do, when the lane is
  gone, or when the item is junk (no real ask, or a duplicate the store missed; merge it with a `note` on the
  real one).
- When a lane asks you to put a question to Nick that is already settled, point it at the item instead
  ("settled in #3: `agenda show 3`"). If sessions keep asking it in other words, add those wordings to the
  settled item as topics (`agenda update 3 --topic "..."`) so the store refuses them too.
- Claude Code lanes that own or watch an item are told about news automatically. Codex lanes aren't yet.
  When a lane needs something now, message it the usual way (SendMessage, or `herdr agent prompt` when
  SendMessage can't reach it; read the pane first, because the prompt replaces any half-typed draft) and
  cite the item ("Nick
  settled this in #3: `agenda show 3`") rather than retyping his words.
- `agenda sessions` shows every session's role and who has unread news.

## Rules

- No secrets in an item; the store refuses token-shaped text. Say where a secret lives (Doppler project and
  config), never its value. Why: items are read aloud and shown on a phone.
- Only Nick answers. Record what he said on the item; don't claim he said something he didn't. Why: lanes
  treat his answers on the agenda as authority.
