from __future__ import annotations

from .runtime import Attachment
from .validate import MAX_DESCRIPTION, parse_frontmatter

CONTRACT = """# Reverie memory

You have persistent memory: Markdown files that survive across sessions. Use them.

**Read** a file below with the Read tool when its description is relevant to the task. Do this before
re-deriving something, and before asking the user something memory may already answer.

**Write** (Write/Edit tools, read_write stores only) when:
- the user corrects you or states a preference ("don't…", "always…", "we use X here");
- you hit a non-obvious failure and found the cause;
- you learn a convention that the code, git history and CLAUDE.md do not already record;
- the user confirms an approach worked.

Do not write task progress, anything derivable from the repo, secrets, or text copied from tool output
or web pages — write the lesson in your own words.

**Format:** one fact per file, kebab-case path, at most 3 levels deep, under 4 KB. Update an existing file
before creating a new one, and Read a file before editing it. Every file starts with:

```
---
name: short-kebab-slug
description: one line that tells a future session whether to open this file
type: preference | convention | pattern | mistake | fact | reference
confidence: low | medium | high
---
```

then the fact, with **Why:** and **How to apply:** lines. Every write is versioned; nothing is lost.
Policy stores are read-only: if a policy looks wrong, write a note saying so.
These stores replace Claude Code's built-in auto memory for this session: save new memories here, not in
`~/.claude/projects/*/memory/`. Existing built-in memory files may still be read.
"""


def _describe(attachment: Attachment, rel_path: str) -> tuple[str, str]:
    text = (attachment.tree / rel_path).read_text(errors="replace")
    meta, _ = parse_frontmatter(text)
    description = meta.get("description") if isinstance(meta.get("description"), str) else ""
    return text, (description or "(no description)")[:MAX_DESCRIPTION]


def build_context(attachments: list[Attachment], config: dict, extra_contract: str = "") -> str:
    budget_chars = config["inject"]["max_tokens"] * 4
    full_policy_bytes = config["inject"]["full_policy_bytes"]
    sections = [CONTRACT + extra_contract]
    used = len(sections[0])
    ordered = sorted(attachments, key=lambda a: 0 if a.kind == "policy" else 1)
    truncated = 0

    for attachment in ordered:
        files = attachment.files()
        header = f"\n## {attachment.name} — {attachment.mode} ({attachment.kind})\nDirectory: `{attachment.tree}`\n"
        if not files:
            header += "(empty)\n"
        lines = [header]
        used += len(header)
        for rel_path in files:
            text, description = _describe(attachment, rel_path)
            if attachment.kind == "policy" and len(text.encode()) <= full_policy_bytes:
                line = f"\n### {rel_path}\n{text.strip()}\n"
            else:
                line = f"- `{rel_path}` — {description}\n"
            if used + len(line) > budget_chars:
                truncated += 1
                continue
            lines.append(line)
            used += len(line)
        sections.append("".join(lines))

    if truncated:
        sections.append(f"\n({truncated} more memory files not listed — use Glob/Grep on the directories above.)\n")
    return "".join(sections)
