import json
import time
import unittest
from pathlib import Path

from helpers import ROOT, ReverieTestCase, memory

FAKE_CLAUDE = ROOT / "tests" / "fake_claude.py"


class DreamTest(ReverieTestCase):
    def setUp(self):
        super().setUp()
        FAKE_CLAUDE.chmod(0o755)
        self.env["REVERIE_CLAUDE_BIN"] = str(FAKE_CLAUDE)
        self.cli("put", "project.notes", "testing/mocks-a.md", stdin=memory("mocks-a", "vi.mock is hoisted"))
        self.cli("put", "project.notes", "testing/mocks-b.md", stdin=memory("mocks-b", "factories can't close over vars"))
        transcript = self.data.parent / "t.jsonl"
        transcript.write_text("\n".join(json.dumps(e) for e in [
            {"type": "user", "message": {"role": "user", "content": "use vi.hoisted please"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
        ]))
        for sid in ("s-aaa", "s-bbb", "s-ccc"):
            self.hook("session-end", session=sid, transcript_path=str(transcript))

    def dream(self, *args):
        out = self.cli("dream", *args).stdout
        dream_id = out.split()[1].rstrip(":")
        return json.loads((self.data / "dreams" / dream_id / "result.json").read_text())

    def ops(self, record):
        return {(op["store"], op["path"]): op for op in record["ops"]}

    def test_auto_mode_applies_notes_and_queues_policy(self):
        record = self.dream("--mode", "auto")
        ops = self.ops(record)
        self.assertEqual(ops[("project.notes", "testing/vitest-mocks.md")]["status"], "applied")
        self.assertEqual(ops[("project.notes", "testing/mocks-a.md")]["status"], "applied")
        self.assertEqual(ops[("project.notes", "Bad Name.md")]["status"], "rejected")
        policy = ops[("project.policy", "no-console.md")]
        self.assertEqual(policy["status"], "rejected")
        self.assertIn("needs 3", policy["problems"][0])

        log = self.cli("log", "project.notes").stdout
        self.assertIn("dream:" + record["id"], log)
        self.assertIn("put    testing/mocks-a.md", log, "pre-dream versions stay in history")
        self.assertEqual(self.cli("show", "project.notes", "testing/mocks-a.md@1").stdout, memory("mocks-a", "vi.mock is hoisted"))

        self.assertEqual(record["sessions"], ["s-aaa", "s-bbb", "s-ccc"])
        self.assertIn("vi.hoisted", (self.data / "dreams" / record["id"] / "digest" / "s-aaa.md").read_text())
        self.assertIn("sessions waiting: 0", self.cli("status").stdout)

    def test_review_mode_waits_for_acceptance(self):
        record = self.dream("--mode", "review")
        self.assertEqual(record["status"], "proposed")
        self.assertTrue((Path(self.data) / "stores").exists())
        notes_before = self.cli("log", "project.notes").stdout
        self.assertNotIn("dream:", notes_before)

        pending = [op["id"] for op in record["ops"] if op["status"] == "pending"]
        self.cli("accept", record["id"], "--ops", str(pending[0]))
        self.cli("reject", record["id"])
        final = json.loads((self.data / "dreams" / record["id"] / "result.json").read_text())
        statuses = sorted(op["status"] for op in final["ops"])
        self.assertIn("applied", statuses)
        self.assertNotIn("pending", statuses)

    def test_ops_on_files_changed_mid_dream_go_stale(self):
        record = self.dream("--mode", "review")
        self.cli("put", "project.notes", "testing/mocks-a.md", stdin=memory("mocks-a", "edited meanwhile"))
        op = next(op for op in record["ops"] if op["path"] == "testing/mocks-a.md")
        self.cli("accept", record["id"], "--ops", str(op["id"]))
        final = json.loads((self.data / "dreams" / record["id"] / "result.json").read_text())
        self.assertEqual(next(o for o in final["ops"] if o["id"] == op["id"])["status"], "stale")
        self.assertIn("edited meanwhile", self.cli("show", "project.notes", "testing/mocks-a.md").stdout)

    def test_session_start_schedules_a_background_dream_when_due(self):
        (self.data / "config.json").write_text(json.dumps({"dream": {"schedule": {"min_sessions": 3}}}))
        self.hook("session-start", source="startup")
        deadline = time.time() + 20
        while time.time() < deadline and not list((self.data / "dreams").glob("*/result.json")):
            time.sleep(0.2)
        records = list((self.data / "dreams").glob("*/result.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())["trigger"], "schedule")
        self.hook("session-start", source="startup")
        time.sleep(1)
        self.assertEqual(len(list((self.data / "dreams").glob("*/result.json"))), 1, "not due again until interval passes")

    def test_promotion_keeps_the_note_until_the_policy_is_reviewed(self):
        promote = ROOT / "tests" / "fake_claude_promote.py"
        promote.write_text("""#!/usr/bin/env python3
import json
from pathlib import Path
sessions = sorted(p.stem for p in Path("digest").glob("*.md"))
note = Path("proposal/project.notes/testing/mocks-a.md")
text = note.read_text().replace("confidence: high", "confidence: high\\nevidence: [" + ", ".join(sessions) + "]")
note.unlink()
Path("proposal/project.policy/testing").mkdir(parents=True)
Path("proposal/project.policy/testing/mocks-a.md").write_text(text)
Path("changes.json").write_text(json.dumps({"changes": [
    {"store": "project.notes", "path": "testing/mocks-a.md", "action": "delete", "rationale": "promoted", "evidence": sessions},
    {"store": "project.policy", "path": "testing/mocks-a.md", "action": "create", "rationale": "promoted", "evidence": sessions}]}))
""")
        promote.chmod(0o755)
        self.addCleanup(promote.unlink)
        self.env["REVERIE_CLAUDE_BIN"] = str(promote)
        record = self.dream("--mode", "auto")
        ops = self.ops(record)
        self.assertEqual(ops[("project.policy", "testing/mocks-a.md")]["status"], "pending")
        self.assertEqual(ops[("project.notes", "testing/mocks-a.md")]["status"], "pending")
        self.assertIn("vi.mock is hoisted", self.cli("show", "project.notes", "testing/mocks-a.md").stdout)
        self.cli("accept", record["id"])
        self.assertIn("vi.mock is hoisted", self.cli("show", "project.policy", "testing/mocks-a.md").stdout)
        self.assertEqual(self.cli("show", "project.notes", "testing/mocks-a.md", check=False).returncode, 1)

    def test_note_delete_is_rejected_with_its_rejected_promotion(self):
        from reverie.dream import _hold_promoted_notes
        from reverie.runtime import open_store
        from reverie.store import Actor
        from types import SimpleNamespace
        notes = open_store("p-x.notes")
        policy = open_store("p-x.policy")
        base = notes.write("a.md", memory("mocks-a").encode(), Actor(kind="agent"))["hash"]
        new = policy.put_blob(memory("renamed").encode())
        atts = {"n": SimpleNamespace(store=notes, kind="notes"), "p": SimpleNamespace(store=policy, kind="policy")}
        ops = [{"store": "p", "op": "put", "path": "mocks-a.md", "hash": new, "status": "rejected", "problems": []},
               {"store": "n", "op": "delete", "path": "a.md", "base": base, "hash": None, "status": "accepted", "problems": []}]
        _hold_promoted_notes(ops, atts)
        self.assertEqual(ops[1]["status"], "rejected")
        ops[0]["status"], ops[1]["status"] = "pending", "accepted"
        ops[0]["path"] = "other.md"
        _hold_promoted_notes(ops, atts)
        self.assertEqual(ops[1]["status"], "pending")

    def test_policy_gate_ignores_fake_or_ambiguous_citations(self):
        from reverie.dream import _evidence
        known = {"abc12345-0000", "abc12345-1111", "def67890-2222"}
        self.assertEqual(_evidence({"evidence": ["", "a", "ab", "abc"]}, None, known), set())
        self.assertEqual(_evidence({"evidence": ["abc12345"]}, None, known), set(), "ambiguous prefix")
        self.assertEqual(_evidence({"evidence": ["abc12345-0000", "abc12345-0000", "def67890"]}, None, known),
                         {"abc12345-0000", "def67890-2222"})

    def test_crlf_proposal_is_stored_under_its_real_hash(self):
        crlf = ROOT / "tests" / "fake_claude_crlf.py"
        crlf.write_text(FAKE_CLAUDE.read_text().replace(
            'Path("changes.json")',
            '(notes / "testing/vitest-mocks.md").write_bytes((notes / "testing/vitest-mocks.md").read_bytes().replace(b"\\n", b"\\r\\n"))\nPath("changes.json")'))
        crlf.chmod(0o755)
        self.addCleanup(crlf.unlink)
        self.env["REVERIE_CLAUDE_BIN"] = str(crlf)
        record = self.dream("--mode", "auto")
        self.assertEqual(self.ops(record)[("project.notes", "testing/vitest-mocks.md")]["status"], "applied")
        notes_store = next((self.data / "stores").glob("*.notes"))
        self.assertIn(b"\r\n", (notes_store / "tree/testing/vitest-mocks.md").read_bytes())

    def test_dreamer_timeout_or_missing_binary_backs_off(self):
        self.env["REVERIE_CLAUDE_BIN"] = str(self.data.parent / "no-such-claude")
        self.assertIn("failed", self.cli("dream").stdout)
        self.assertIn("due: False", self.cli("status").stdout, "a failed attempt must not retry on every session start")
        self.assertIn("sessions waiting: 3", self.cli("status").stdout)

    def fake_result(self, name: str, result: dict) -> None:
        script = ROOT / "tests" / f"fake_claude_{name}.py"
        script.write_text(f"#!/usr/bin/env python3\nprint({json.dumps(json.dumps(result))})\n")
        script.chmod(0o755)
        self.addCleanup(script.unlink)
        self.env["REVERIE_CLAUDE_BIN"] = str(script)

    def add_session(self, sid: str, transcript: Path) -> None:
        self.hook("session-end", session=sid, transcript_path=str(transcript))

    def test_refused_writes_fail_the_dream_and_keep_sessions_waiting(self):
        self.fake_result("denied", {"type": "result", "result": "Both Write calls were denied.",
                                    "permission_denials": [{"tool_name": "Write", "tool_input": {}}]})
        record = self.dream()
        self.assertEqual(record["status"], "failed")
        self.assertIn("refused 1 tool call(s): Write", record["error"])
        report = (self.data / "dreams" / record["id"] / "REPORT.md").read_text()
        self.assertIn("**Failed:**", report)
        self.assertIn("Both Write calls were denied.", report)
        self.assertIn("sessions waiting: 3", self.cli("status").stdout)

    def test_dreamer_error_result_fails_the_dream(self):
        self.fake_result("autherr", {"type": "result", "is_error": True, "result": "OAuth session expired"})
        record = self.dream()
        self.assertEqual(record["status"], "failed")
        self.assertIn("OAuth session expired", record["error"])

    def test_failures_retry_hourly_then_back_off(self):
        from datetime import timedelta
        from reverie.config import detect_project, load_config
        from reverie.dream import dream_gap
        config, _ = load_config(detect_project(self.project))
        failed = {"last_dream_at": "2026-01-01T00:00:00Z", "last_attempt_at": "2026-01-02T00:00:00Z"}
        self.assertEqual(dream_gap({**failed, "failures": 1}, config, 5), timedelta(hours=1))
        self.assertEqual(dream_gap({**failed, "failures": 4}, config, 5), timedelta(hours=24))

    def test_trivial_sessions_are_consumed_without_a_slot(self):
        lone_command = self.data.parent / "lone-command.jsonl"
        lone_command.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "/model"}}))
        self.add_session("s-ddd", lone_command)
        self.add_session("s-eee", self.data.parent / "missing.jsonl")
        record = self.dream()
        self.assertEqual(record["sessions"], ["s-aaa", "s-bbb", "s-ccc"])
        self.assertEqual(sorted(record["skipped_sessions"]), ["s-ddd", "s-eee"])
        self.assertNotIn("s-ddd.md", [p.name for p in (self.data / "dreams" / record["id"] / "digest").iterdir()])
        self.assertIn("sessions waiting: 0", self.cli("status").stdout)

    def test_only_trivial_sessions_skip_the_dreamer(self):
        self.dream()
        lone_command = self.data.parent / "lone-command.jsonl"
        lone_command.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "/model"}}))
        self.add_session("s-ddd", lone_command)
        self.env["REVERIE_CLAUDE_BIN"] = "false"
        record = self.dream()
        self.assertEqual(record["status"], "no_changes")
        self.assertEqual(record["skipped_sessions"], ["s-ddd"])

    def test_memory_written_mid_dream_is_not_proposed_for_deletion(self):
        midwrite = ROOT / "tests" / "fake_claude_midwrite.py"
        midwrite.chmod(0o755)
        self.env["REVERIE_CLAUDE_BIN"] = str(midwrite)
        self.env["TEST_PROJECT"] = str(self.project)
        record = self.dream()
        self.assertEqual(record["ops"], [])
        self.assertEqual(record["status"], "no_changes")
        self.assertIn("written mid-dream", self.cli("show", "project.notes", "fresh.md").stdout)

    def test_failed_dreamer_changes_nothing(self):
        self.env["REVERIE_CLAUDE_BIN"] = "false"
        out = self.cli("dream").stdout
        self.assertIn("failed", out)
        self.assertIn("sessions waiting: 3", self.cli("status").stdout)


if __name__ == "__main__":
    unittest.main()
