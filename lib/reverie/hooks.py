from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Optional

from . import dream
from .config import claude_home, data_root, detect_project, load_config
from .inject import build_context
from .runtime import Attachment, SessionState, attachment_for, resolve_attachments
from .store import Actor, hash_file, now_iso
from .validate import validate_memory

WRITE_TOOLS = ("Write", "Edit", "MultiEdit")


def _emit(event: str, **fields) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}}))


def _context(payload: dict):
    cwd = Path(payload.get("cwd") or os.getcwd())
    project = detect_project(cwd)
    config, _ = load_config(project)
    return project, config, resolve_attachments(config, project)


def _apply_edit(current: str, old: str, new: str, replace_all: bool) -> Optional[str]:
    if old not in current:
        return None
    return current.replace(old, new) if replace_all else current.replace(old, new, 1)


def _proposed_content(tool: str, tool_input: dict, path: Path) -> Optional[str]:
    if tool == "Write":
        return tool_input.get("content", "")
    current = path.read_text() if path.exists() else ""
    edits = tool_input.get("edits") if tool == "MultiEdit" else [tool_input]
    for edit in edits or []:
        current = _apply_edit(current, edit.get("old_string", ""), edit.get("new_string", ""), bool(edit.get("replace_all")))
        if current is None:
            return None
    return current


def session_start(payload: dict) -> None:
    project, config, attachments = _context(payload)
    actor = Actor(kind="external", tool="external-edit")
    for attachment in attachments:
        if attachment.store:
            attachment.store.sync_external(actor)
    extra = ""
    pending = dream.pending_review(project)
    if pending:
        extra = f"\n_{pending} dreamed memory change(s) await review — mention `/reverie:review` to the user once._\n"
    context = build_context(attachments, config, extra)
    if payload.get("source", "startup") == "startup":
        dream.maybe_schedule(project, config, payload.get("cwd") or os.getcwd())
    _emit("SessionStart", additionalContext=context)


def _deny(reason: str) -> None:
    _emit("PreToolUse", permissionDecision="deny", permissionDecisionReason=reason)


def _occ_problem(attachment: Attachment, path: Path, state: SessionState) -> Optional[str]:
    on_disk = hash_file(path)
    if on_disk is None:
        return None
    seen = state.seen(path)
    if seen is None:
        return f"Read {path} before changing it — memory files may have been changed by other sessions."
    if seen != on_disk:
        return (f"{path} changed since you last read it (another session or the dreaming job updated it). "
                "Read it again and merge your change into the current version.")
    return None


def _notes_store(attachments: list[Attachment]) -> Optional[Attachment]:
    writable = [a for a in attachments if a.writable]
    return next((a for a in writable if a.name.startswith("project.")), writable[0] if writable else None)


def _is_builtin_memory(path: Path) -> bool:
    parts = path.resolve().parts
    projects = (claude_home() / "projects").resolve().parts
    return parts[:len(projects)] == projects and "memory" in parts[len(projects):]


def _may_be_memory(path: Path) -> bool:
    """Cheap pre-filter so ordinary file reads and writes don't pay for git calls and config loading."""
    if path.suffix == ".md":
        return True
    resolved = str(path.resolve())
    return any(resolved.startswith(str(root.resolve()) + os.sep) for root in (data_root(), claude_home())) \
        or f"{os.sep}.claude{os.sep}reverie{os.sep}" in resolved


def pre_write(payload: dict) -> None:
    tool_input = payload.get("tool_input") or {}
    raw_path = tool_input.get("file_path")
    if not raw_path or not _may_be_memory(Path(raw_path)):
        return
    path = Path(raw_path)
    _, config, attachments = _context(payload)
    notes = _notes_store(attachments)
    if config.get("redirect_builtin_memory") and notes and _is_builtin_memory(path):
        return _deny(f"Reverie is this session's memory system. Write this memory to {notes.tree} "
                     f"(or {next((a.tree for a in attachments if a.name == 'user.preferences'), notes.tree)} "
                     "for preferences that apply to every project) using the Reverie format, not the built-in memory directory.")
    attachment = attachment_for(path, attachments)
    if attachment is None:
        return
    if not attachment.writable:
        hint = f" Write an observation to {notes.tree} instead; dreaming promotes proven notes." if notes else ""
        return _deny(f"Memory store '{attachment.name}' is read-only.{hint}")
    state = SessionState(payload.get("session_id", ""))
    occ = _occ_problem(attachment, path, state)
    if occ:
        return _deny(occ)
    proposed = _proposed_content(payload.get("tool_name", ""), tool_input, path)
    if proposed is None:
        return
    rel = attachment.store.rel(path)
    problems = validate_memory(rel, proposed, config["limits"])
    if not path.exists() and len(attachment.files()) >= config["limits"]["files_per_store"]:
        problems.append(f"store '{attachment.name}' is full ({config['limits']['files_per_store']} files); update or merge an existing file")
    if problems:
        _deny("Memory write rejected:\n- " + "\n- ".join(problems))


def post_tool(payload: dict) -> None:
    tool = payload.get("tool_name", "")
    raw_path = (payload.get("tool_input") or {}).get("file_path")
    if not raw_path or not _may_be_memory(Path(raw_path)):
        return
    path = Path(raw_path)
    _, _, attachments = _context(payload)
    attachment = attachment_for(path, attachments)
    if attachment is None:
        return
    session_id = payload.get("session_id", "")
    if tool == "Read":
        with SessionState.locked(session_id) as state:
            state.mark_seen(path, hash_file(path))
        return
    if tool not in WRITE_TOOLS or not attachment.writable:
        return
    actor = Actor(kind="agent", session=session_id, tool=tool, cwd=payload.get("cwd"))
    with SessionState.locked(session_id) as state:
        entry = attachment.store.record(attachment.store.rel(path), actor, parent_seen=state.seen(path))
        state.mark_seen(path, hash_file(path))
        state.touch(attachment.name)
    if entry.get("conflict"):
        _emit("PostToolUse", additionalContext=(
            f"Reverie: {path} was changed by another writer while you edited it. Both versions are in history "
            f"(`reverie log {attachment.store.root.name} --path {entry['path']}`); re-read and reconcile."))


def session_end(payload: dict) -> None:
    cwd = Path(payload.get("cwd") or os.getcwd())
    project = detect_project(cwd)
    state = SessionState(payload.get("session_id", ""))
    record = {
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "cwd": str(cwd),
        "project": project.id,
        "reason": payload.get("reason"),
        "touched": state.data.get("touched", []),
        "ended_at": now_iso(),
    }
    with open(data_root() / "sessions.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
    if state.path.exists():
        state.path.unlink()


HANDLERS = {
    "session-start": session_start,
    "pre-write": pre_write,
    "post-tool": post_tool,
    "session-end": session_end,
}


def run(event: str) -> int:
    """Hooks must never break the session: any internal failure is logged and the tool call proceeds."""
    if os.environ.get("REVERIE_DISABLE"):
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        HANDLERS[event](payload)
    except Exception:
        try:
            with open(data_root() / "errors.log", "a") as f:
                f.write(f"{now_iso()} hook {event}\n{traceback.format_exc()}\n")
        except OSError:
            pass
    return 0
