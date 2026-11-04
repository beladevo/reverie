from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

KINDS = ("policy", "notes")


class ConflictError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> Optional[str]:
    try:
        return hash_bytes(path.read_bytes())
    except FileNotFoundError:
        return None


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rv-tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@dataclass
class Actor:
    kind: str
    session: Optional[str] = None
    tool: Optional[str] = None
    cwd: Optional[str] = None
    dream: Optional[str] = None

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


class Store:
    """A directory of markdown files whose every change is an append-only, content-addressed version."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.tree = self.root / "tree"
        self.meta_dir = self.root / ".rv"
        self.objects = self.meta_dir / "objects"
        self.log_path = self.meta_dir / "log.jsonl"
        self.lock_path = self.meta_dir / "lock"

    @classmethod
    def create(cls, root: Path, name: str, kind: str, scope: str, description: str = "") -> "Store":
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        store = cls(root)
        store.tree.mkdir(parents=True, exist_ok=True)
        store.objects.mkdir(parents=True, exist_ok=True)
        store.log_path.touch(exist_ok=True)
        meta_file = store.root / "store.json"
        if not meta_file.exists():
            meta = {"name": name, "kind": kind, "scope": scope, "description": description, "created_at": now_iso()}
            atomic_write(meta_file, (json.dumps(meta, indent=2) + "\n").encode())
        return store

    def exists(self) -> bool:
        return (self.root / "store.json").exists()

    @property
    def meta(self) -> dict:
        return json.loads((self.root / "store.json").read_text())

    @property
    def name(self) -> str:
        return self.meta["name"]

    @property
    def kind(self) -> str:
        return self.meta["kind"]

    def contains(self, path: Path) -> bool:
        try:
            Path(os.path.realpath(path)).relative_to(os.path.realpath(self.tree))
            return True
        except ValueError:
            return False

    def rel(self, path: Path) -> str:
        return Path(os.path.realpath(path)).relative_to(os.path.realpath(self.tree)).as_posix()

    def abs(self, rel_path: str) -> Path:
        candidate = (self.tree / rel_path).resolve()
        if not str(candidate).startswith(str(self.tree.resolve()) + os.sep):
            raise ValueError(f"path escapes store: {rel_path}")
        return candidate

    def put_blob(self, data: bytes) -> str:
        digest = hash_bytes(data)
        hexd = digest.split(":", 1)[1]
        target = self.objects / hexd[:2] / hexd[2:]
        if not target.exists():
            atomic_write(target, data)
        return digest

    def get_blob(self, digest: str) -> bytes:
        hexd = digest.split(":", 1)[1]
        return (self.objects / hexd[:2] / hexd[2:]).read_bytes()

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def log(self) -> Iterator[dict]:
        if not self.log_path.exists():
            return
        with open(self.log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def head_seq(self) -> int:
        seq = 0
        for entry in self.log():
            seq = entry["seq"]
        return seq

    def state(self, at_seq: Optional[int] = None) -> dict[str, str]:
        files: dict[str, str] = {}
        for entry in self.log():
            if at_seq is not None and entry["seq"] > at_seq:
                break
            if entry["op"] == "delete":
                files.pop(entry["path"], None)
            else:
                files[entry["path"]] = entry["hash"]
        return files

    def history(self, rel_path: str) -> list[dict]:
        return [e for e in self.log() if e["path"] == rel_path]

    def _append(self, entry: dict) -> dict:
        entry = {"seq": self.head_seq() + 1, "ts": now_iso(), **entry}
        torn = False
        if self.log_path.exists() and self.log_path.stat().st_size:
            with open(self.log_path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                torn = f.read(1) != b"\n"
        with open(self.log_path, "a") as f:
            f.write(("\n" if torn else "") + json.dumps(entry, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry

    def _absorb_external(self, rel_path: str, target: Path) -> Optional[str]:
        """Version whatever is on disk before overwriting it, so an editor's unsaved-to-log change is never lost."""
        current = self.state().get(rel_path)
        on_disk = hash_file(target)
        if on_disk == current:
            return current
        if on_disk is None:
            self._append({"path": rel_path, "op": "delete", "hash": None, "parent": current,
                          "actor": {"kind": "external", "tool": "external-edit"}, "reason": None})
            return None
        try:
            digest = self.put_blob(target.read_bytes())
        except FileNotFoundError:
            return current
        self._append({"path": rel_path, "op": "put", "hash": digest, "parent": current,
                      "actor": {"kind": "external", "tool": "external-edit"}, "reason": None})
        return digest

    def write(self, rel_path: str, data: bytes, actor: Actor, if_match: Optional[str] = "*", reason: Optional[str] = None) -> dict:
        """Write through the store. if_match: "*" skips the check, None requires the file to be absent."""
        target = self.abs(rel_path)
        with self.locked():
            current = self._absorb_external(rel_path, target)
            if if_match != "*" and current != if_match:
                raise ConflictError(f"{rel_path}: expected {if_match}, found {current}")
            digest = self.put_blob(data)
            atomic_write(target, data)
            if digest == current:
                return {}
            return self._append({"path": rel_path, "op": "put", "hash": digest, "parent": current,
                                 "actor": actor.to_json(), "reason": reason})

    def delete(self, rel_path: str, actor: Actor, if_match: Optional[str] = "*", reason: Optional[str] = None) -> dict:
        target = self.abs(rel_path)
        with self.locked():
            current = self._absorb_external(rel_path, target)
            if if_match != "*" and current != if_match:
                raise ConflictError(f"{rel_path}: expected {if_match}, found {current}")
            if current is None:
                return {}
            with contextlib.suppress(FileNotFoundError):
                target.unlink()
            self._prune_empty_dirs(target.parent)
            return self._append({"path": rel_path, "op": "delete", "hash": None, "parent": current,
                                 "actor": actor.to_json(), "reason": reason})

    def record(self, rel_path: str, actor: Actor, parent_seen: Optional[str] = None) -> dict:
        """Version a file that was already changed on disk by someone else (the agent's own tools, an editor).

        Returns the log entry; entry["conflict"] is set when the session based its change on a stale version.
        """
        target = self.abs(rel_path)
        with self.locked():
            current = self.state().get(rel_path)
            if not target.exists():
                if current is None:
                    return {}
                return self._append({"path": rel_path, "op": "delete", "hash": None, "parent": current,
                                     "actor": actor.to_json(), "reason": None})
            data = target.read_bytes()
            digest = self.put_blob(data)
            if digest == current:
                return {}
            entry = {"path": rel_path, "op": "put", "hash": digest, "parent": current,
                     "actor": actor.to_json(), "reason": None}
            if parent_seen is not None and parent_seen != current:
                entry["conflict"] = {"seen": parent_seen}
            return self._append(entry)

    def sync_external(self, actor: Actor) -> list[dict]:
        """Version edits made outside Claude Code, so the log always matches the tree."""
        entries = []
        known = self.state()
        on_disk = {p: None for p in self.tree_files()}
        for rel_path in sorted(set(known) | set(on_disk)):
            on_disk_hash = hash_file(self.tree / rel_path)
            if on_disk_hash != known.get(rel_path):
                entry = self.record(rel_path, actor)
                if entry:
                    entries.append(entry)
        return entries

    def tree_files(self) -> list[str]:
        if not self.tree.exists():
            return []
        return sorted(
            p.relative_to(self.tree).as_posix()
            for p in self.tree.rglob("*.md")
            if p.is_file() and not p.name.startswith(".rv-tmp-")
        )

    def rollback_path(self, rel_path: str, to_seq: int, actor: Actor) -> dict:
        target_hash = self.state(at_seq=to_seq).get(rel_path)
        reason = f"rollback to seq {to_seq}"
        if target_hash is None:
            return self.delete(rel_path, actor, reason=reason)
        return self.write(rel_path, self.get_blob(target_hash), actor, reason=reason)

    def rollback_store(self, to_seq: int, actor: Actor) -> list[dict]:
        target = self.state(at_seq=to_seq)
        current = self.state()
        entries = []
        for rel_path in sorted(set(target) | set(current)):
            if target.get(rel_path) != current.get(rel_path):
                entry = self.rollback_path(rel_path, to_seq, actor)
                if entry:
                    entries.append(entry)
        return entries

    def materialize(self, dest: Path, at_seq: Optional[int] = None) -> None:
        for rel_path, digest in self.state(at_seq).items():
            atomic_write(dest / rel_path, self.get_blob(digest))

    def _prune_empty_dirs(self, directory: Path) -> None:
        tree = self.tree.resolve()
        directory = directory.resolve()
        while directory != tree and directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
            directory = directory.parent
