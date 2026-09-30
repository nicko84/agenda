import os

# Tests never read the machine's real settings (~/.config/agenda/env).
os.environ["AGENDA_ENV"] = os.devnull
