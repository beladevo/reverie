import os
import tempfile
import time
import unittest
from pathlib import Path

from helpers import ROOT  # noqa: F401

from reverie import config


class DataRootTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self._saved = dict(os.environ)
        for key in ("REVERIE_DATA", "CLAUDE_PLUGIN_DATA"):
            os.environ.pop(key, None)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.home)
        self.data = self.home / "plugins" / "data"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def test_installed_copy_uses_its_own_data_dir(self):
        source = self.home / "plugins" / "cache" / "reverie" / "reverie" / "0.2.0" / "lib" / "reverie" / "config.py"
        self.assertEqual(config.plugin_data_dir(source), self.data / "reverie-reverie")
        self.assertIsNone(config.plugin_data_dir(Path("/elsewhere/lib/reverie/config.py")))

    def test_custom_plugin_root_and_marketplace_names(self):
        source = self.home / "opt" / "cache" / "beladevo.tools" / "reverie" / "0.2.1" / "lib" / "reverie" / "config.py"
        self.assertEqual(config.plugin_data_dir(source), self.home / "opt" / "data" / "reverie-beladevo-tools")

    def test_checkout_without_sessions_avoids_the_inline_dir(self):
        for name in ("reverie-inline", "reverie-reverie"):
            (self.data / name).mkdir(parents=True)
        self.assertEqual(config.data_root(), self.data / "reverie-reverie")

    def test_checkout_picks_the_dir_with_the_latest_session(self):
        for name in ("reverie-inline", "reverie-reverie"):
            (self.data / name).mkdir(parents=True)
        (self.data / "reverie-reverie" / "sessions.jsonl").write_text("{}\n")
        time.sleep(0.01)
        (self.data / "reverie-inline" / "stores").mkdir()
        self.assertEqual(config.data_root(), self.data / "reverie-reverie")

    def test_explicit_env_wins(self):
        os.environ["CLAUDE_PLUGIN_DATA"] = str(self.home / "explicit")
        self.assertEqual(config.data_root(), self.home / "explicit")


if __name__ == "__main__":
    unittest.main()
