from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import claude_home, data_root, detect_project
from .store import atomic_write

HEAD_LINES = 50


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def recorded() -> list[dict]:
    path = data_root() / "sessions.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("session_id"):
            records.append(record)
    return records


def _transcript_cwd(path: Path) -> Optional[str]:
    with open(path, errors="replace") as f:
        for i, line in enumerate(f):
            if i >= HEAD_LINES:
                break
            try:
                cwd = json.loads(line).get("cwd")
            except (json.JSONDecodeError, AttributeError):
                continue
            if cwd:
                return cwd
    return None


def _project_of(cwd: str) -> Optional[str]:
    """A removed worktree no longer exists on disk; its nearest surviving ancestor is still inside the repo."""
    path = Path(cwd)
    if str(path).startswith(str(data_root())):
        return None
    while not path.exists() and path != path.parent:
        path = path.parent
    return detect_project(path).id


def discover(lookback_days: float) -> list[dict]:
    """Every main-session transcript Claude Code wrote recently, from any directory or worktree."""
    if lookback_days <= 0:
        return []
    cutoff = time.time() - lookback_days * 86400
    cache_path = data_root() / "state" / "transcripts.json"
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        cache = {}
    projects: dict = {}
    found, changed = [], False
    for path in (claude_home() / "projects").glob("*/*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        info = cache.get(str(path))
        if info is None:
            cwd = _transcript_cwd(path)
            if not cwd:
                continue
            if cwd not in projects:
                projects[cwd] = _project_of(cwd)
            info = cache[str(path)] = {"cwd": cwd, "project": projects[cwd]}
            changed = True
        if info["project"]:
            found.append({"session_id": path.stem, "transcript_path": str(path), "cwd": info["cwd"],
                          "project": info["project"], "ended_at": _iso(mtime), "mtime": mtime,
                          "reason": "found in transcripts", "touched": []})
    if changed:
        atomic_write(cache_path, json.dumps(cache).encode())
    return found


def _ended_after(record: dict, mtime: float) -> bool:
    if record.get("reason") == "found in transcripts" or not record.get("ended_at"):
        return False
    ended = datetime.strptime(record["ended_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    return mtime <= ended + 60


def inventory(project_id: str, project_state: dict, dream_config: dict) -> tuple[list, list]:
    """(waiting, active): sessions ready to dream over, and ones still too recently active to read."""
    quiet = dream_config["quiet_minutes"] * 60
    now = time.time()
    consumed = set(project_state.get("consumed", []))
    consumed_mtime = project_state.get("consumed_mtime", {})
    by_id: dict = {}
    for record in recorded():
        if record.get("project") == project_id:
            by_id.setdefault(record["session_id"], dict(record))
    for record in discover(dream_config["lookback_days"]):
        if record["project"] != project_id:
            continue
        if record["session_id"] in by_id:
            by_id[record["session_id"]]["mtime"] = record["mtime"]
        else:
            by_id[record["session_id"]] = record
    waiting, active = [], []
    for sid, record in by_id.items():
        mtime = record.get("mtime")
        if mtime is not None and now - mtime < quiet and not _ended_after(record, mtime):
            active.append(record)
            continue
        if sid in consumed and (mtime is None or sid not in consumed_mtime or mtime <= consumed_mtime[sid] + 1):
            continue
        waiting.append(record)
    order = lambda r: r.get("ended_at") or ""
    return sorted(waiting, key=order), sorted(active, key=order)
