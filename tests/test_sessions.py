import json
import os
import subprocess
import time
import unittest
from pathlib import Path

from helpers import ROOT, ReverieTestCase

from reverie import dream
from reverie.config import detect_project, load_config

FAKE_CLAUDE = ROOT / "tests" / "fake_claude.py"
HOUR = 3600


class SessionDiscoveryTest(ReverieTestCase):
    def setUp(self):
        super().setUp()
        self.projects = Path(self.env["CLAUDE_CONFIG_DIR"]) / "projects"
        git = ["git", "-C", str(self.project), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
        self.worktree = self.project.parent / "wt-feature"
        subprocess.run(git + ["worktree", "add", "-q", str(self.worktree)], check=True)
        self.other = self.project.parent / "other"
        subprocess.run(["git", "init", "-q", str(self.other)], check=True)

    def transcript(self, sid, cwd, age_seconds, text="use vi.hoisted please"):
        path = self.projects / str(cwd).replace("/", "-") / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in [
            {"type": "permission-mode", "sessionId": sid},
            {"type": "user", "cwd": str(cwd), "sessionId": sid, "message": {"role": "user", "content": text}},
            {"type": "assistant", "cwd": str(cwd), "sessionId": sid, "message": {"content": [{"type": "text", "text": "ok"}]}},
        ]) + "\n")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def inventory(self):
        project = detect_project(self.project)
        waiting, active = dream.session_inventory(project, load_config(project)[0])
        return [s["session_id"] for s in waiting], [s["session_id"] for s in active]

    def test_finds_sessions_from_every_worktree_but_not_other_repos(self):
        self.transcript("s-main", self.project, HOUR)
        self.transcript("s-worktree", self.worktree, 2 * HOUR)
        self.transcript("s-gone", self.project / ".claude" / "worktrees" / "deleted", 3 * HOUR)
        self.transcript("s-other", self.other, HOUR)
        self.transcript("s-old", self.project, 8 * 24 * HOUR)
        self.transcript("s-live", self.worktree, 60)
        waiting, active = self.inventory()
        self.assertEqual(waiting, ["s-gone", "s-worktree", "s-main"])
        self.assertEqual(active, ["s-live"])

    def test_ended_session_is_not_listed_twice_or_held_back(self):
        path = self.transcript("s-ended", self.project, 30)
        self.hook("session-end", session="s-ended", transcript_path=str(path))
        waiting, active = self.inventory()
        self.assertEqual((waiting, active), (["s-ended"], []))

    def test_dream_consumes_discovered_sessions_and_resumes_bring_them_back(self):
        FAKE_CLAUDE.chmod(0o755)
        self.env["REVERIE_CLAUDE_BIN"] = str(FAKE_CLAUDE)
        from helpers import memory
        self.cli("put", "project.notes", "testing/mocks-a.md", stdin=memory("mocks-a"))
        self.cli("put", "project.notes", "testing/mocks-b.md", stdin=memory("mocks-b"))
        path = self.transcript("s-long", self.worktree, 2 * HOUR)
        self.transcript("s-short", self.project, 2 * HOUR)

        out = self.cli("dream", "--mode", "review").stdout
        record = json.loads((self.data / "dreams" / out.split()[1].rstrip(":") / "result.json").read_text())
        self.assertEqual(sorted(record["sessions"]), ["s-long", "s-short"])
        self.assertEqual(self.inventory(), ([], []))

        with open(path, "a") as f:
            f.write(json.dumps({"type": "user", "cwd": str(self.worktree), "message": {"content": "again"}}) + "\n")
        self.assertEqual(self.inventory(), ([], ["s-long"]))
        stamp = time.time() - HOUR
        os.utime(path, (stamp, stamp))
        self.assertEqual(self.inventory(), (["s-long"], []))

    def test_backlog_is_deferred_and_worked_off_hourly(self):
        FAKE_CLAUDE.chmod(0o755)
        self.env["REVERIE_CLAUDE_BIN"] = str(FAKE_CLAUDE)
        from helpers import memory
        self.cli("put", "project.notes", "testing/mocks-a.md", stdin=memory("mocks-a"))
        self.cli("put", "project.notes", "testing/mocks-b.md", stdin=memory("mocks-b"))
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "reverie.json").write_text(json.dumps(
            {"dream": {"max_sessions": 2, "schedule": {"min_sessions": 1}}}))
        for i in range(5):
            self.transcript(f"s-{i}", self.project, (10 - i) * HOUR)

        out = self.cli("dream", "--mode", "review").stdout
        record = json.loads((self.data / "dreams" / out.split()[1].rstrip(":") / "result.json").read_text())
        self.assertEqual(record["sessions"], ["s-3", "s-4"])
        self.assertEqual(record["deferred_sessions"], ["s-0", "s-1", "s-2"])
        self.assertEqual(self.inventory()[0], ["s-0", "s-1", "s-2"])

        project = detect_project(self.project)
        config = load_config(project)[0]
        self.assertFalse(dream.is_due(project, config))
        state = dream.load_state()
        state["projects"][project.id]["last_dream_at"] = "2000-01-01T00:00:00Z"
        state["projects"][project.id]["last_attempt_at"] = "2000-01-01T00:00:00Z"
        dream._save_state(state)
        self.assertTrue(dream.is_due(project, config))

    def test_lookback_zero_turns_discovery_off(self):
        self.transcript("s-main", self.project, HOUR)
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "reverie.json").write_text(json.dumps({"dream": {"lookback_days": 0}}))
        self.assertEqual(self.inventory(), ([], []))


if __name__ == "__main__":
    unittest.main()
