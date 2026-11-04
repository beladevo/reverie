import json
import re
import unittest
from pathlib import Path

from helpers import ReverieTestCase, memory


class HooksTest(ReverieTestCase):
    def store_dir(self, suffix: str) -> Path:
        context = self.hook("session-start", source="startup")["hookSpecificOutput"]["additionalContext"]
        return Path(re.search(rf"## project\.{suffix} .*\nDirectory: `([^`]+)`", context).group(1))

    def write(self, path, content, session="s-1"):
        tool_input = {"file_path": str(path), "content": content}
        pre = self.hook("pre-write", session=session, tool_name="Write", tool_input=tool_input)
        if pre:
            return pre["hookSpecificOutput"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.hook("post-tool", session=session, tool_name="Write", tool_input=tool_input)
        return None

    def read(self, path, session="s-1"):
        self.hook("post-tool", session=session, tool_name="Read", tool_input={"file_path": str(path)})

    def log(self, store="project.notes"):
        return self.cli("log", store).stdout

    def test_session_start_injects_contract_and_stores(self):
        context = self.hook("session-start", source="startup")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("# Reverie memory", context)
        self.assertIn("project.notes — read_write", context)
        self.assertIn("project.policy — read_only", context)

    def test_valid_write_is_versioned_with_session(self):
        notes = self.store_dir("notes")
        denied = self.write(notes / "testing/mocks.md", memory("mocks"))
        self.assertIsNone(denied)
        self.assertIn("put    testing/mocks.md", self.log())
        self.assertIn("agent:s-1", self.log())

    def test_read_only_store_rejects_writes(self):
        policy = self.store_dir("policy")
        denied = self.write(policy / "rules.md", memory("rules"))
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn("read-only", denied["permissionDecisionReason"])

    def test_invalid_or_secret_content_is_rejected(self):
        notes = self.store_dir("notes")
        self.assertIn("frontmatter", self.write(notes / "a.md", "just text")["permissionDecisionReason"])
        leaked = memory("creds", body="token ghp_" + "a" * 36)
        self.assertIn("secret", self.write(notes / "creds.md", leaked)["permissionDecisionReason"])

    def test_team_policy_at_custom_path_is_read_only(self):
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "reverie.json").write_text(json.dumps({"attach": [
            {"store": "project.notes", "mode": "read_write"},
            {"store": "team.policy", "path": "docs/team-policy"}]}))
        denied = self.write(self.project / "docs" / "team-policy" / "x.md", memory("x"))
        self.assertIn("read-only", denied["permissionDecisionReason"])

    def test_writes_outside_stores_are_ignored(self):
        self.assertIsNone(self.write(self.project / "src.md", "anything"))

    def test_optimistic_concurrency_between_sessions(self):
        notes = self.store_dir("notes")
        target = notes / "a.md"
        self.write(target, memory("a", "v1"), session="s-1")

        self.assertIn("Read", self.write(target, memory("a", "v2"), session="s-2")["permissionDecisionReason"])
        self.read(target, session="s-2")
        self.read(target, session="s-3")
        self.assertIsNone(self.write(target, memory("a", "v2"), session="s-2"))

        denied = self.write(target, memory("a", "v3"), session="s-3")
        self.assertIn("changed since you last read it", denied["permissionDecisionReason"])
        self.read(target, session="s-3")
        self.assertIsNone(self.write(target, memory("a", "v3"), session="s-3"))

    def test_edit_is_validated_against_resulting_content(self):
        notes = self.store_dir("notes")
        target = notes / "a.md"
        self.write(target, memory("a"))
        tool_input = {"file_path": str(target), "old_string": "type: fact", "new_string": "type: gossip"}
        denied = self.hook("pre-write", tool_name="Edit", tool_input=tool_input)
        self.assertIn("type must be", denied["hookSpecificOutput"]["permissionDecisionReason"])

    def test_session_end_records_session_for_dreaming(self):
        self.hook("session-end", session="s-9", reason="prompt_input_exit", transcript_path="/tmp/t.jsonl")
        record = json.loads((self.data / "sessions.jsonl").read_text().splitlines()[-1])
        self.assertEqual(record["session_id"], "s-9")
        self.assertTrue(record["project"].startswith("p-"))

    def test_builtin_auto_memory_writes_are_redirected(self):
        claude_home = self.data.parent / "claude-home"
        self.env["CLAUDE_CONFIG_DIR"] = str(claude_home)
        target = claude_home / "projects" / "-x-proj" / "memory" / "no-comments.md"
        denied = self.write(target, "---\nname: x\n---\nbody")
        self.assertIn("Reverie is this session's memory system", denied["permissionDecisionReason"])
        self.assertIn(".notes/tree", denied["permissionDecisionReason"])

    def test_parallel_reads_all_count_as_seen(self):
        import concurrent.futures
        notes = self.store_dir("notes")
        paths = [notes / f"f{i}.md" for i in range(6)]
        for p in paths:
            self.write(p, memory(p.stem), session="writer")
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            list(pool.map(lambda p: self.read(p, session="reader"), paths))
        for p in paths:
            self.assertIsNone(self.write(p, memory(p.stem, "updated"), session="reader"), p)

    def test_disabled_env_makes_hooks_noop(self):
        self.env["REVERIE_DISABLE"] = "1"
        self.assertEqual(self.hook("session-start", source="startup"), {})


if __name__ == "__main__":
    unittest.main()
