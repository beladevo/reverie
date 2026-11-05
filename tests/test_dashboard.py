import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import unittest

from helpers import BIN, ROOT, ReverieTestCase, memory

from reverie import dashboard, dream
from reverie.config import detect_project

FAKE_CLAUDE = ROOT / "tests" / "fake_claude.py"


def text(rows):
    return "\n".join(row[0] for row in rows)


class DashboardCase(ReverieTestCase):
    def setUp(self):
        super().setUp()
        FAKE_CLAUDE.chmod(0o755)
        self.env["REVERIE_CLAUDE_BIN"] = str(FAKE_CLAUDE)
        self.cli("put", "project.notes", "testing/mocks-a.md", stdin=memory("mocks-a", "vi.mock is hoisted"))
        self.cli("put", "project.notes", "testing/mocks-b.md", stdin=memory("mocks-b", "factories can't close over vars"))
        self.transcript = self.data.parent / "t.jsonl"
        self.transcript.write_text("\n".join(json.dumps(e) for e in [
            {"type": "user", "message": {"role": "user", "content": "use vi.hoisted please"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
        ]))
        for sid in ("s-aaa", "s-bbb", "s-ccc"):
            self.end_session(sid)

    def end_session(self, sid):
        self.hook("session-end", session=sid, transcript_path=str(self.transcript))

    def snap(self):
        return dashboard.take(self.project)

    def dream_now(self, mode):
        out = self.cli("dream", "--mode", mode).stdout
        return out.split()[1].rstrip(":")


class DashboardTest(DashboardCase):
    def test_once_prints_overview(self):
        out = self.cli("dashboard", "--once").stdout
        self.assertIn("project.notes", out)
        self.assertIn("last dream    never", out)
        self.assertIn("next dream    after 2 more sessions", out)
        self.assertIn("waiting       3 sessions", out)
        self.assertIn("put    project.notes/testing/mocks-b.md  human:cli", out)

    def test_next_dream_waits_for_interval_after_a_dream(self):
        self.dream_now("auto")
        for sid in ("s-ddd", "s-eee", "s-fff", "s-ggg", "s-hhh"):
            self.end_session(sid)
        text_, _ = dashboard.next_dream(self.snap())
        self.assertRegex(text_, r"^in 2[34]h")

        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "reverie.json").write_text(json.dumps({"dream": {"schedule": {"enabled": False}}}))
        self.assertEqual(dashboard.next_dream(self.snap())[0], "schedule off, run on demand (n)")

    def test_memory_tree_file_and_history(self):
        self.cli("put", "project.notes", "testing/mocks-a.md", stdin=memory("mocks-a", "vi.mock is hoisted above imports"))
        snap = self.snap()
        nodes = dashboard.memory_nodes(snap)
        labels = [(n.att.name, n.kind, n.rel) for n in nodes]
        self.assertIn(("project.notes", "dir", "testing"), labels)
        self.assertLess(labels.index(("project.notes", "dir", "testing")), labels.index(("project.notes", "file", "testing/mocks-a.md")))

        node = next(n for n in nodes if n.rel == "testing/mocks-a.md")
        self.assertIn("vi.mock is hoisted above imports", text(dashboard.node_view(snap, node, 80)))
        self.assertIn("2 versions", text(dashboard.node_view(snap, node, 80)))

        rows, anchor, count = dashboard.history_view(snap, node.att, node.rel, 0, 80)
        self.assertEqual(count, 2)
        self.assertEqual(rows[anchor][1], "sel")
        self.assertIn(("+vi.mock is hoisted above imports", "add"), rows)
        self.assertIn(("-vi.mock is hoisted", "del"), rows)

    def test_dream_view_shows_ops_with_problems_and_diff(self):
        dream_id = self.dream_now("review")
        snap = self.snap()
        record = next(r for r in dashboard.dream_rows(snap) if r["id"] == dream_id)
        self.assertIn("?3", dashboard.dream_label(record)[0])

        rows, anchor, count = dashboard.dream_view(snap, record, None, 80)
        self.assertIsNone(anchor)
        self.assertEqual(count, len(record["ops"]))
        self.assertIn("Merged mock notes.", text(rows))

        bad = next(i for i, op in enumerate(record["ops"]) if op["path"] == "Bad Name.md")
        rows, anchor, _ = dashboard.dream_view(snap, record, bad, 80)
        self.assertIn("Bad Name.md", rows[anchor][0])
        self.assertTrue(any(style == "bad" and t.startswith("✗") for t, style in rows))
        self.assertIn(("+no frontmatter", "add"), rows)

    def test_running_dream_is_detected(self):
        project = detect_project(self.project)
        self.assertIsNone(dream.running(project))
        with dream._project_lock(project):
            (dream.dreams_dir() / "20991231T000000Z-beef").mkdir()
            (dream.dreams_dir() / "20991231T000000Z-beef" / "input.json").write_text(json.dumps(
                {"project": project.id, "started_at": "2026-11-05T10:00:00Z", "sessions": ["s-aaa"]}))
            snap = self.snap()
            self.assertEqual(snap.running["sessions"], ["s-aaa"])
            self.assertEqual(dashboard.next_dream(snap)[0], "running now")
            self.assertTrue(dashboard.dream_rows(snap)[0]["running"])
            self.assertIn("dreaming now over 1 session", text(dashboard.overview(snap, 100)))

    def test_session_view_shows_what_the_dreamer_reads(self):
        snap = self.snap()
        rows = dashboard.session_view(snap, snap.waiting[0], 80)
        self.assertIn(("USER: use vi.hoisted please", "accent"), rows)


class DashboardTerminalTest(DashboardCase):
    def run_tui(self, keys, rows=40, cols=120):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        proc = subprocess.Popen([sys.executable, str(BIN), "--cwd", str(self.project), "dashboard"],
                                stdin=slave, stdout=slave, stderr=slave, env={**self.env, "TERM": "xterm-256color"})
        os.close(slave)
        output, sent, deadline = b"", False, time.time() + 20
        try:
            while time.time() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                if not sent and b"Overview" in output:
                    for key in keys:
                        os.write(master, key.encode())
                    sent = True
                if proc.poll() is not None:
                    break
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)
        return proc.wait(5), output.decode(errors="replace")

    def test_navigates_every_tab_and_quits(self):
        code, output = self.run_tui("2jjjjl" "j" "h3" "4l" "1q")
        self.assertEqual(code, 0, output[-2000:])
        self.assertNotIn("Traceback", output)
        self.assertIn("history, newest first", output)
        self.assertIn("WHAT THE DREAMER WILL READ", output)

    def test_accepts_a_pending_change(self):
        dream_id = self.dream_now("review")
        record = json.loads((self.data / "dreams" / dream_id / "result.json").read_text())
        target = next(i for i, op in enumerate(record["ops"]) if op["status"] == "pending")
        code, output = self.run_tui("3l" + "j" * target + "ay" + "q")
        self.assertEqual(code, 0, output[-2000:])
        record = json.loads((self.data / "dreams" / dream_id / "result.json").read_text())
        self.assertEqual(record["ops"][target]["status"], "applied")



if __name__ == "__main__":
    unittest.main()
