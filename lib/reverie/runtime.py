from __future__ import annotations

import contextlib
import fcntl
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .config import STORE_DESCRIPTIONS, Project, data_root, resolve_store_id, store_kind_for
from .store import Store, atomic_write


@dataclass
class Attachment:
    name: str
    mode: str
    store: Optional[Store]
    external: Optional[Path] = None

    @property
    def tree(self) -> Path:
        return self.external if self.external else self.store.tree

    @property
    def kind(self) -> str:
        return self.store.kind if self.store else "policy"

    @property
    def writable(self) -> bool:
        return self.mode == "read_write" and self.store is not None

    def contains(self, path: Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.tree.resolve())
            return True
        except ValueError:
            return False

    def files(self) -> list[str]:
        if self.store:
            return self.store.tree_files()
        if not self.external.exists():
            return []
        return sorted(p.relative_to(self.external).as_posix() for p in self.external.rglob("*.md") if p.is_file())


def stores_dir() -> Path:
    return data_root() / "stores"


def open_store(store_id: str, create: bool = True) -> Store:
    store = Store(stores_dir() / store_id)
    if create and not store.exists():
        suffix = store_id.rsplit(".", 1)[-1]
        scope = "project" if store_id.startswith("p-") else "user"
        Store.create(store.root, store_id, store_kind_for(store_id), scope, STORE_DESCRIPTIONS.get(suffix, ""))
    return store


def list_stores() -> list[Store]:
    root = stores_dir()
    if not root.exists():
        return []
    return [Store(p) for p in sorted(root.iterdir()) if (p / "store.json").exists()]


def resolve_attachments(config: dict, project: Optional[Project]) -> list[Attachment]:
    attachments = []
    for spec in config.get("attach", []):
        mode = spec.get("mode", "read_only")
        if spec.get("path"):
            base = project.root if project else Path.cwd()
            attachments.append(Attachment(spec["store"], "read_only", None, (base / spec["path"]).resolve()))
            continue
        try:
            store_id = resolve_store_id(spec["store"], project)
        except ValueError:
            continue
        attachments.append(Attachment(spec["store"], mode, open_store(store_id)))
    return attachments


def attachment_for(path: Path, attachments: list[Attachment]) -> Optional[Attachment]:
    for attachment in attachments:
        if attachment.contains(path):
            return attachment
    return None


def _safe_session(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")


class SessionState:
    """Per-session bookkeeping: the hash of each memory file as this session last saw it.

    Parallel tool calls fire concurrent hooks, so mutations go through `with SessionState.locked(id)`.
    """

    def __init__(self, session_id: str):
        self.path = data_root() / "state" / "sessions" / f"{_safe_session(session_id)}.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"seen": {}, "touched": []}

    @classmethod
    @contextlib.contextmanager
    def locked(cls, session_id: str) -> Iterator["SessionState"]:
        lock = data_root() / "state" / "sessions" / f"{_safe_session(session_id)}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                state = cls(session_id)
                yield state
                state.save()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def seen(self, abs_path: Path) -> Optional[str]:
        return self.data["seen"].get(str(abs_path))

    def mark_seen(self, abs_path: Path, digest: Optional[str]) -> None:
        self.data["seen"][str(abs_path)] = digest

    def touch(self, store_name: str) -> None:
        if store_name not in self.data["touched"]:
            self.data["touched"].append(store_name)

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.data).encode())
