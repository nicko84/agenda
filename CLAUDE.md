# agenda

Durable queue between Nick and his agent sessions in herdr. Read README.md first; it has the model, the
rules the store enforces (and the evidence behind each), and what comes next. The session-facing guidance
lives only in `skills/agenda/SKILL.md`.

- Standard library only, run by `/usr/bin/python3`. Sessions call this from `~`, where mise pins no python
  or node, and hooks will call it too. Don't add dependencies.
- `agenda/store.py` holds every rule and is the library front ends import. `cli.py` and `render.py` only
  parse and present, and `identity.py` only decides who is calling. Keep rules out of the CLI.
- Every mutating store method takes an `Actor`. Nick's verbs refuse sessions and session verbs refuse Nick;
  keep it that way, because lanes treat Nick's answers on the agenda as authority.
- Schema changes bump `SCHEMA_VERSION` and add a step in `_migrate`. The live database is
  `~/.local/share/agenda/agenda.db`; never test against it (tests use temp dirs, and set `AGENDA_DB` plus
  `AGENDA_SESSION` for anything that shells out).
- The repo is local only, with no remote.

Test: `python3 -m unittest`
