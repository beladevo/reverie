from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import dream
from .config import claude_home, data_root, detect_project
from .runtime import SessionState
from .store import atomic_write

BIN = Path(__file__).resolve().parents[2] / "bin" / "reverie"
YELLOW, MAGENTA, RESET = "\033[33m", "\033[35m", "\033[0m"


def segment(payload: dict) -> str:
    cwd = (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd") or os.getcwd()
    project = detect_project(Path(cwd))
    parts = []
    if dream.running(project) is not None:
        parts.append(f"{MAGENTA}dreaming…{RESET}")
    saved = len(SessionState(payload.get("session_id", "")).data.get("written", []))
    if saved:
        parts.append(f"{saved} saved")
    pending = dream.pending_review(project)
    if pending:
        parts.append(f"{YELLOW}{pending} to review{RESET}")
    return "💭 " + " · ".join(parts) if parts else ""


def shim_path() -> Path:
    return data_root() / "statusline.sh"


def write_shim() -> Path:
    """Status line commands outlive plugin updates, but the plugin's install path changes with each version,
    so the user's status line calls this shim in the stable data dir and SessionStart keeps it pointed here."""
    path = shim_path()
    content = f'#!/bin/sh\nexec python3 "{BIN}" statusline\n'
    if not path.exists() or path.read_text() != content:
        atomic_write(path, content.encode())
        path.chmod(0o755)
    return path


def refresh_shim() -> None:
    if shim_path().exists():
        write_shim()


def is_wired() -> bool:
    try:
        command = json.loads((claude_home() / "settings.json").read_text()).get("statusLine", {}).get("command", "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return False
    if "reverie" in command:
        return True
    for token in re.findall(r"[^\s'\"]+", command):
        script = Path(os.path.expanduser(token))
        try:
            if script.is_file() and "reverie" in script.read_text(errors="replace"):
                return True
        except OSError:
            continue
    return False


def take_hint() -> bool:
    """True once per data dir, and only while the user's status line doesn't show Reverie yet."""
    marker = data_root() / "state" / "statusline-hinted"
    if marker.exists() or is_wired():
        return False
    atomic_write(marker, b"")
    return True
