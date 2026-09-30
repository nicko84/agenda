import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agenda import settings

ENV = """# comment
AGENDA_MAIL_FROM="Agenda <agenda@example.com>"  # sender
AGENDA_MAIL_TO=owner@example.com # who
AGENDA_PAGE_URL='https://host:1'
"""


class Settings(unittest.TestCase):
    def test_file_values_quotes_comments_and_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "env"
            env.write_text(ENV)
            with mock.patch.dict(os.environ, {"AGENDA_ENV": str(env), "AGENDA_PAGE_URL": ""}):
                self.assertEqual(settings.get("AGENDA_MAIL_FROM"), "Agenda <agenda@example.com>")
                self.assertEqual(settings.get("AGENDA_MAIL_TO"), "owner@example.com")
                self.assertEqual(settings.get("AGENDA_PAGE_URL"), "https://host:1")
                with mock.patch.dict(os.environ, {"AGENDA_MAIL_TO": "other@example.com"}):
                    self.assertEqual(settings.get("AGENDA_MAIL_TO"), "other@example.com")
                self.assertEqual(settings.get("MISSING", "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
