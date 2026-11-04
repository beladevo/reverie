#!/usr/bin/env python3
"""Stands in for `claude -p` in tests: edits the proposal the way a dreamer would."""
import json
import os
from pathlib import Path

assert os.environ.get("REVERIE_DISABLE") == "1", "dreamer must run with Reverie hooks disabled"

notes = Path("proposal/project.notes")
policy = Path("proposal/project.policy")
sessions = sorted(p.stem for p in Path("digest").glob("*.md"))


def memory(name, body, evidence):
    return (f"---\nname: {name}\ndescription: about {name}\ntype: mistake\nconfidence: high\n"
            f"evidence: [{', '.join(evidence)}]\n---\n\n{body}\n")


(notes / "testing/mocks-a.md").unlink()
(notes / "testing/mocks-b.md").unlink()
(notes / "testing/vitest-mocks.md").write_text(memory("vitest-mocks", "Use vi.hoisted for factory values.", sessions[:2]))
(policy / "no-console.md").write_text(memory("no-console", "Never use console.", sessions[:1]))
(notes / "Bad Name.md").write_text("no frontmatter")

changes = [
    {"store": "project.notes", "path": "testing/mocks-a.md", "action": "delete", "rationale": "merged", "evidence": sessions[:1]},
    {"store": "project.notes", "path": "testing/mocks-b.md", "action": "delete", "rationale": "merged", "evidence": sessions[:1]},
    {"store": "project.notes", "path": "testing/vitest-mocks.md", "action": "create", "rationale": "merge of a+b", "evidence": sessions[:2]},
    {"store": "project.policy", "path": "no-console.md", "action": "create", "rationale": "promote", "evidence": sessions[:1]},
    {"store": "project.notes", "path": "Bad Name.md", "action": "create", "rationale": "oops", "evidence": sessions[:1]},
]
Path("changes.json").write_text(json.dumps({"summary": "Merged mock notes.", "changes": changes}))
print(json.dumps({"type": "result", "result": "done"}))
