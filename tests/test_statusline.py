import fcntl
import json
import re
import unittest
from pathlib import Path

from helpers import ReverieTestCase, memory

from reverie.config import detect_project

ANSI = re.compile(r"\033\[[0-9;]*m")


class StatuslineTest(ReverieTestCase):
    def statusline(self, session="s-1") -> str:
        body = {"session_id": session, "workspace": {"current_dir": str(self.project)}}
        return ANSI.sub("", self.cli("statusline", stdin=json.dumps(body)).stdout.strip())

    def notes_dir(self) -> Path:
        context = self.hook("session-start", source="startup")["hookSpecificOutput"]["additionalContext"]
        return Path(re.search(r"## project\.notes .*\nDirectory: `([^`]+)`", context).group(1))

    def write(self, path: Path, content: str, session="s-1"):
        tool_input = {"file_path": str(path), "content": content}
        self.hook("pre-write", session=session, tool_name="Write", tool_input=tool_input)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.hook("post-tool", session=session, tool_name="Write", tool_input=tool_input)

    def test_empty_when_nothing_to_show(self):
        self.assertEqual(self.statusline(), "")

    def test_counts_distinct_files_saved_by_this_session(self):
        notes = self.notes_dir()
        self.write(notes / "a.md", memory("a"))
        self.write(notes / "a.md", memory("a", body="Updated fact."))
        self.write(notes / "b.md", memory("b"))
        self.assertEqual(self.statusline(), "💭 2 saved")
        self.assertEqual(self.statusline(session="s-2"), "")

    def test_shows_pending_review_and_running_dream(self):
        project = detect_project(self.project)
        dream_dir = self.data / "dreams" / "d-1"
        dream_dir.mkdir(parents=True)
        ops = [{"id": 1, "status": "pending"}, {"id": 2, "status": "applied"}]
        (dream_dir / "result.json").write_text(json.dumps({"id": "d-1", "project": project.id, "ops": ops}))
        self.assertEqual(self.statusline(), "💭 1 to review")
        with open(self.data / "dreams" / f"{project.id}.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.statusline(), "💭 dreaming… · 1 to review")

    def test_bad_input_prints_nothing(self):
        result = self.cli("statusline", stdin="not json")
        self.assertEqual((result.stdout, result.stderr), ("", ""))

    def test_shim_runs_the_cli_and_session_start_keeps_it(self):
        shim = Path(self.cli("statusline", "--shim").stdout.strip())
        self.assertIn("statusline", shim.read_text())
        shim.write_text("stale")
        self.notes_dir()
        self.assertIn("bin/reverie", shim.read_text())

    def test_hint_once_and_only_while_not_wired(self):
        def hinted():
            context = self.hook("session-start", source="startup")["hookSpecificOutput"]["additionalContext"]
            return "/reverie:statusline" in context

        self.assertTrue(hinted())
        self.assertFalse(hinted())

    def test_no_hint_when_status_line_already_shows_reverie(self):
        home = Path(self.env["CLAUDE_CONFIG_DIR"])
        home.mkdir(parents=True, exist_ok=True)
        script = home / "statusline.sh"
        script.write_text("#!/bin/sh\nprintf '%s' \"$input\" | ~/.claude/plugins/data/reverie-reverie/statusline.sh\n")
        (home / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": f"sh {script}"}}))
        context = self.hook("session-start", source="startup")["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("/reverie:statusline", context)


if __name__ == "__main__":
    unittest.main()
