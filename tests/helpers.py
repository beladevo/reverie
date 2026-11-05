import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "reverie"
sys.path.insert(0, str(ROOT / "lib"))


def memory(name: str, body: str = "Some durable fact.", type_: str = "fact", evidence=None) -> str:
    lines = ["---", f"name: {name}", f"description: about {name}", f"type: {type_}", "confidence: high"]
    if evidence:
        lines.append(f"evidence: [{', '.join(evidence)}]")
    return "\n".join(lines + ["---", "", body, ""])


class ReverieTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name).resolve()
        self.data = tmp / "data"
        self.project = tmp / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_DATA", "REVERIE_DISABLE")}
        self.env["REVERIE_DATA"] = str(self.data)
        self.env["CLAUDE_CONFIG_DIR"] = str(tmp / "claude-home")
        self._saved_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._tmp.cleanup()

    def cli(self, *args, stdin: str = "", check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(BIN), "--cwd", str(self.project), *args],
                                input=stdin, capture_output=True, text=True, env=self.env)
        if check and result.returncode != 0:
            raise AssertionError(f"reverie {' '.join(args)} failed: {result.stderr}")
        return result

    def hook(self, event: str, session: str = "s-1", **payload) -> dict:
        body = {"session_id": session, "cwd": str(self.project), "transcript_path": "", **payload}
        result = self.cli("hook", event, stdin=json.dumps(body))
        errors = self.data / "errors.log"
        if errors.exists():
            raise AssertionError(errors.read_text())
        return json.loads(result.stdout) if result.stdout.strip() else {}
