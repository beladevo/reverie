from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULTS: dict = {
    "attach": [
        {"store": "user.preferences", "mode": "read_write"},
        {"store": "project.policy", "mode": "read_only"},
        {"store": "project.notes", "mode": "read_write"},
    ],
    "redirect_builtin_memory": True,
    "inject": {"max_tokens": 2000, "full_policy_bytes": 1500},
    "limits": {"file_bytes": 4096, "files_per_store": 300, "max_depth": 3, "notes_ttl_days": 90},
    "dream": {
        "mode": "auto",
        "schedule": {"enabled": True, "interval_hours": 24, "min_sessions": 5},
        "model": "sonnet",
        "max_turns": 40,
        "max_sessions": 20,
        "digest_chars_per_session": 12000,
        "promote_min_sessions": 3,
        "auto_promote": False,
        "timeout_minutes": 30,
    },
}

STORE_DESCRIPTIONS = {
    "preferences": "How the user likes to work: style, tone, tooling, workflow preferences.",
    "policy": "Curated, trusted conventions and rules for this project. Changed only by dreaming + review.",
    "notes": "Raw observations from sessions: mistakes, fixes, patterns, facts. Agent-writable.",
}


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def data_root() -> Path:
    """Hooks get CLAUDE_PLUGIN_DATA; terminal and Bash-tool runs of bin/reverie don't, so they work out the same dir."""
    explicit = os.environ.get("REVERIE_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    root = Path(explicit) if explicit else plugin_data_dir(Path(__file__)) or _most_active_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def plugin_data_dir(source: Path) -> Optional[Path]:
    """An installed plugin runs from plugins/cache/<marketplace>/<plugin>/<version>/; its data is plugins/data/<plugin>-<marketplace>."""
    try:
        marketplace, plugin = source.resolve().relative_to((claude_home() / "plugins" / "cache").resolve()).parts[:2]
    except ValueError:
        return None
    return claude_home() / "plugins" / "data" / f"{plugin}-{marketplace}"


def _last_session_at(directory: Path) -> float:
    sessions = directory / "sessions.jsonl"
    return sessions.stat().st_mtime if sessions.exists() else 0


def _most_active_data_dir() -> Path:
    installed = sorted((claude_home() / "plugins" / "data").glob("reverie-*"))
    return max(installed, key=_last_session_at) if installed else claude_home() / "reverie-data"


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _git(cwd: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def normalize_remote(url: str) -> str:
    url = re.sub(r"^[a-z+]+://", "", url.strip().lower())
    url = re.sub(r"^[^@/]+@", "", url)
    url = url.replace(":", "/")
    return re.sub(r"\.git$", "", url).rstrip("/")


@dataclass
class Project:
    id: str
    root: Path
    label: str


def detect_project(cwd: Path) -> Project:
    cwd = Path(cwd).resolve()
    common_dir = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    root = Path(common_dir).parent if common_dir else cwd
    toplevel = _git(cwd, "rev-parse", "--show-toplevel")
    remote = _git(cwd, "remote", "get-url", "origin")
    key = normalize_remote(remote) if remote else str(root)
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    return Project(id=f"p-{digest}", root=Path(toplevel) if toplevel else cwd, label=key)


def load_config(project: Optional[Project] = None) -> tuple[dict, list[str]]:
    sources = ["defaults"]
    config = copy.deepcopy(DEFAULTS)
    user_file = data_root() / "config.json"
    if user_file.exists():
        config = deep_merge(config, json.loads(user_file.read_text()))
        sources.append(str(user_file))
    if project:
        project_file = project.root / ".claude" / "reverie.json"
        if project_file.exists():
            config = deep_merge(config, json.loads(project_file.read_text()))
            sources.append(str(project_file))
    return config, sources


def resolve_store_id(name: str, project: Optional[Project]) -> str:
    if name.startswith("project."):
        if project is None:
            raise ValueError(f"{name} needs a project")
        return f"{project.id}.{name.split('.', 1)[1]}"
    return name


def store_kind_for(name: str) -> str:
    return "policy" if name.rsplit(".", 1)[-1] == "policy" else "notes"
