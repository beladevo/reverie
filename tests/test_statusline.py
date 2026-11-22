import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from helpers import BIN, ReverieTestCase, memory

from reverie.config import detect_project

ANSI = re.compile(r"\033\[[0-9;]*m")


class StatuslineTest(ReverieTestCase):
    def statusline(self, session="s-1") -> str:
        body = {"session_id": session, "workspace": {"current_dir": str(self.project)}}
        return ANSI.sub("", self.cli("statusline", stdin=json.dumps(body)).stdout.strip())

    def notes_dir(self) -> Path:
        context = self.hook("session-start", source="startup")["hookSpecificOutput"]["additionalContext"]
        return Path(re.search(r"## project\.notes .*\nDirectory: `([^`]+)`", context).group(1))

    def settings(self, settings: dict) -> Path:
        home = Path(self.env["CLAUDE_CONFIG_DIR"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.json").write_text(json.dumps(settings))
        return home

    def read(self, path: Path, session="s-1"):
        self.hook("post-tool", session=session, tool_name="Read", tool_input={"file_path": str(path)})

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
        running = self.data / "dreams" / f"{project.id}.running"
        running.write_text(f"d-2 {os.getpid()}")
        self.assertEqual(self.statusline(), "💭 dreaming… · 1 to review")
        dead = subprocess.Popen(["true"])
        dead.wait()
        running.write_text(f"d-2 {dead.pid}")
        self.assertEqual(self.statusline(), "💭 1 to review")

    def test_bad_input_prints_nothing(self):
        result = self.cli("statusline", stdin="not json")
        self.assertEqual((result.stdout, result.stderr), ("", ""))

    def test_shim_runs_the_cli_and_session_start_keeps_it(self):
        shim = Path(self.cli("statusline", "--shim").stdout.strip())
        self.assertIn("statusline", shim.read_text())
        shim.write_text("stale")
        self.notes_dir()
        self.assertIn("bin/reverie", shim.read_text())

    def test_shim_is_silent_once_its_target_is_gone(self):
        shim = Path(self.cli("statusline", "--shim").stdout.strip())
        shim.write_text(shim.read_text().replace(str(BIN), "/nonexistent/bin/reverie"))
        result = subprocess.run([str(shim)], input="{}", capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_deletes_are_not_counted_as_saved(self):
        notes = self.notes_dir()
        self.write(notes / "a.md", memory("a"))
        (notes / "b.md").write_text(memory("b"))
        self.hook("session-start", source="startup")
        self.read(notes / "b.md")
        (notes / "b.md").unlink()
        self.hook("post-tool", tool_name="Write", tool_input={"file_path": str(notes / "b.md"), "content": ""})
        self.assertEqual(self.statusline(), "💭 1 saved")

    def test_disabled_plugin_prints_nothing(self):
        notes = self.notes_dir()
        self.write(notes / "a.md", memory("a"))
        self.settings({"enabledPlugins": {"reverie@reverie": False}})
        self.assertEqual(self.statusline(), "")

    def test_hint_once_and_only_while_not_wired(self):
        def hinted():
            context = self.hook("session-start", source="startup")["hookSpecificOutput"]["additionalContext"]
            return "/reverie:statusline" in context

        self.assertTrue(hinted())
        self.assertFalse(hinted())

    def test_no_hint_when_status_line_already_shows_reverie(self):
        script = Path(self.env["CLAUDE_CONFIG_DIR"]) / "statusline.sh"
        self.settings({"statusLine": {"type": "command", "command": f"sh {script}"}})
        script.write_text("#!/bin/sh\nprintf '%s' \"$input\" | ~/.claude/plugins/data/reverie-reverie/statusline.sh\n")
        context = self.hook("session-start", source="startup")["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("/reverie:statusline", context)


if __name__ == "__main__":
    unittest.main()
