from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

NOISE = re.compile(r"<(system-reminder|local-command-stdout|local-command-caveat)>.*?</\1>", re.S)
TOOL_INPUT_KEYS = ("file_path", "command", "pattern", "path", "url", "query", "description")


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " …"


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tool_summary(block: dict) -> str:
    tool_input = block.get("input") or {}
    detail = next((str(tool_input[k]) for k in TOOL_INPUT_KEYS if tool_input.get(k)), "")
    return f"[tool {block.get('name', '?')}] {_clip(detail, 160)}"


def _render_entry(entry: dict) -> list[str]:
    """Each transcript line is one event. Unknown event types are skipped, so format drift degrades gracefully."""
    if entry.get("isMeta") or entry.get("isSidechain"):
        return []
    kind = entry.get("type")
    content = (entry.get("message") or {}).get("content")
    lines: list[str] = []
    if kind == "user":
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    lines.append(f"[tool error] {_clip(_text_of(block.get('content')), 400)}")
        text = NOISE.sub("", _text_of(content)).strip()
        if text:
            lines.append(f"USER: {_clip(text, 1500)}")
    elif kind == "assistant" and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                lines.append(f"ASSISTANT: {_clip(block['text'], 800)}")
            elif block.get("type") == "tool_use":
                lines.append(_tool_summary(block))
    return lines


def _before(entry: dict, since: Optional[float]) -> bool:
    stamp = entry.get("timestamp")
    if since is None or not isinstance(stamp, str):
        return False
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() <= since
    except ValueError:
        return False


def is_substantive(digest: str) -> bool:
    """A session worth a dreamer's time has the user asking something and the assistant acting on it."""
    lines = digest.splitlines()
    return any(l.startswith("USER: ") for l in lines) and any(l.startswith(("ASSISTANT: ", "[tool ")) for l in lines)


def condense(transcript: Path, max_chars: int, since: Optional[float] = None) -> str:
    """Reduce a session transcript to what a reviewer needs: what was asked, said, tried, and what failed."""
    lines: list[str] = []
    if not transcript.is_file():
        return ""
    try:
        with open(transcript, errors="replace") as f:
            for raw in f:
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and not _before(entry, since):
                    lines.extend(_render_entry(entry))
    except FileNotFoundError:
        return ""
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    head = max_chars // 3
    return text[:head] + "\n\n[… middle of session omitted …]\n\n" + text[-(max_chars - head):]
