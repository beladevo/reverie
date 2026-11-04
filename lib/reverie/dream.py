from __future__ import annotations

import contextlib
import difflib
import fcntl
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import Project, data_root, detect_project, load_config
from .runtime import Attachment, resolve_attachments, stores_dir
from .store import Actor, ConflictError, Store, atomic_write, hash_file, now_iso
from .transcripts import condense
from .validate import parse_frontmatter, validate_memory

PROMPT_FILE = Path(__file__).parent / "prompts" / "dreamer.md"
DREAM_TOOLS = "Read,Write,Edit,Glob,Grep"
BLOCKED_TOOLS = "Bash,WebFetch,WebSearch,Task,Agent,NotebookEdit"


def dreams_dir() -> Path:
    path = data_root() / "dreams"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path() -> Path:
    return dreams_dir() / "state.json"


def load_state() -> dict:
    path = _state_path()
    return json.loads(path.read_text()) if path.exists() else {"projects": {}}


def _save_state(state: dict) -> None:
    atomic_write(_state_path(), (json.dumps(state, indent=2) + "\n").encode())


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def unconsumed_sessions(project: Project) -> list[dict]:
    consumed = set(load_state()["projects"].get(project.id, {}).get("consumed", []))
    path = data_root() / "sessions.jsonl"
    if not path.exists():
        return []
    sessions, seen = [], set()
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = record.get("session_id")
        if record.get("project") != project.id or sid in consumed or sid in seen:
            continue
        seen.add(sid)
        sessions.append(record)
    return sessions


def is_due(project: Project, config: dict) -> bool:
    schedule = config["dream"]["schedule"]
    if not schedule.get("enabled"):
        return False
    last = load_state()["projects"].get(project.id, {}).get("last_dream_at")
    if last and datetime.now(timezone.utc) - _parse_ts(last) < timedelta(hours=schedule["interval_hours"]):
        return False
    return len(unconsumed_sessions(project)) >= schedule["min_sessions"]


def maybe_schedule(project: Project, config: dict, cwd: str) -> bool:
    if not is_due(project, config) or _lock_held(project):
        return False
    log = open(dreams_dir() / f"{project.id}.log", "a")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).parents[2] / "bin" / "reverie"), "--cwd", cwd, "dream", "--trigger", "schedule"],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
    )
    return True


def _lock_path(project: Project) -> Path:
    return dreams_dir() / f"{project.id}.lock"


def _lock_held(project: Project) -> bool:
    with open(_lock_path(project), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


@contextlib.contextmanager
def _project_lock(project: Project):
    with open(_lock_path(project), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("a dream is already running for this project") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _new_dream_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)


def _write_task(dream_dir: Path, attachments: list[Attachment], sessions: list[dict], config: dict) -> None:
    lines = ["# Dream task", "", "## Stores (edit under proposal/<store>/)", ""]
    for att in attachments:
        lines.append(f"- `{att.name}` — kind: {att.kind}, agent access: {att.mode}, files: {len(att.files())}")
    lines += ["", "## Sessions to review (digest/<id>.md)", ""]
    lines += [f"- `{s['session_id']}` — ended {s.get('ended_at')}, cwd {s.get('cwd')}" for s in sessions]
    lines += ["", "## Rules for this run", "",
              f"- A file created in or promoted to a policy store needs evidence from at least "
              f"{config['dream']['promote_min_sessions']} distinct sessions.",
              f"- Max file size: {config['limits']['file_bytes']} bytes. Max files per store: {config['limits']['files_per_store']}.",
              f"- Notes with no evidence newer than {config['limits']['notes_ttl_days']} days are candidates for deletion.",
              f"- Today is {now_iso()[:10]}.", ""]
    (dream_dir / "TASK.md").write_text("\n".join(lines))


def _run_dreamer(dream_dir: Path, config: dict) -> subprocess.CompletedProcess:
    claude = os.environ.get("REVERIE_CLAUDE_BIN", "claude")
    prompt = PROMPT_FILE.read_text() + "\n\nStart now: read TASK.md."
    cmd = [claude, "-p", prompt,
           "--model", config["dream"]["model"],
           "--max-turns", str(config["dream"]["max_turns"]),
           "--permission-mode", "acceptEdits",
           "--allowedTools", DREAM_TOOLS,
           "--disallowedTools", BLOCKED_TOOLS,
           "--output-format", "json"]
    env = {**os.environ, "REVERIE_DISABLE": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    return subprocess.run(cmd, cwd=dream_dir, env=env, capture_output=True, text=True,
                          timeout=config["dream"]["timeout_minutes"] * 60)


def _rationale_index(dream_dir: Path) -> tuple[dict, str]:
    path = dream_dir / "changes.json"
    if not path.exists():
        return {}, ""
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}, ""
    index = {}
    for change in data.get("changes", []):
        for p in change.get("paths") or [change.get("path")]:
            if p:
                index[(change.get("store"), p)] = change
    return index, data.get("summary", "")


def _evidence(change: dict, content: Optional[str], known_sessions: set[str]) -> set[str]:
    cited = set(change.get("evidence") or [])
    if content:
        meta, _ = parse_frontmatter(content)
        if isinstance(meta.get("evidence"), list):
            cited |= set(meta["evidence"])
    return {e for e in cited if any(e == s or s.startswith(e) for s in known_sessions)}


def _classify(op: dict, change: Optional[dict], att: Attachment, content: Optional[str],
              sessions: set[str], config: dict) -> tuple[str, list[str]]:
    problems: list[str] = []
    if change is None or not change.get("rationale"):
        problems.append("no rationale in changes.json")
    if content is not None:
        problems += validate_memory(op["path"], content, config["limits"])
    if att.kind == "policy" and op["op"] == "put":
        evidence = _evidence(change or {}, content, sessions | _historic_sessions())
        if len(evidence) < config["dream"]["promote_min_sessions"]:
            problems.append(f"policy change cites {len(evidence)} known session(s), needs {config['dream']['promote_min_sessions']}")
    if problems:
        return "rejected", problems
    needs_review = config["dream"]["mode"] == "review" or (att.kind == "policy" and not config["dream"]["auto_promote"])
    return ("pending" if needs_review else "accepted"), []


def _historic_sessions() -> set[str]:
    path = data_root() / "sessions.jsonl"
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text().splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            ids.add(json.loads(line).get("session_id") or "")
    return ids


def _diff_ops(dream_dir: Path, attachments: list[Attachment], sessions: set[str], config: dict) -> tuple[list[dict], str]:
    index, summary = _rationale_index(dream_dir)
    ops = []
    for att in attachments:
        base_state = att.store.state()
        proposal_root = dream_dir / "proposal" / att.name
        proposed = {p.relative_to(proposal_root).as_posix(): p for p in proposal_root.rglob("*") if p.is_file()} if proposal_root.exists() else {}
        for rel in sorted(set(base_state) | set(proposed)):
            new_hash = hash_file(proposed[rel]) if rel in proposed else None
            if new_hash == base_state.get(rel):
                continue
            content = proposed[rel].read_text(errors="replace") if rel in proposed else None
            op = {"id": len(ops) + 1, "store": att.name, "store_id": att.store.root.name, "path": rel,
                  "op": "put" if content is not None else "delete", "base": base_state.get(rel), "hash": new_hash}
            if content is not None:
                att.store.put_blob(content.encode())
            change = index.get((att.name, rel))
            op["status"], op["problems"] = _classify(op, change, att, content, sessions, config)
            op["evidence"] = sorted((change or {}).get("evidence") or [])
            op["rationale"] = (change or {}).get("rationale", "")
            ops.append(op)
    return ops, summary


def _store_of(op: dict) -> Store:
    return Store(stores_dir() / op["store_id"])


def _apply_op(op: dict, dream_id: str) -> None:
    store = _store_of(op)
    actor = Actor(kind="dream", dream=dream_id)
    reason = op.get("rationale") or None
    try:
        if op["op"] == "put":
            store.write(op["path"], store.get_blob(op["hash"]), actor, if_match=op["base"], reason=reason)
        else:
            store.delete(op["path"], actor, if_match=op["base"], reason=reason)
        op["status"] = "applied"
    except ConflictError:
        op["status"] = "stale"
        op["problems"] = ["memory changed after the dream started; re-dream to reconsider"]


def _render_report(dream_id: str, summary: str, ops: list[dict]) -> str:
    out = [f"# Dream {dream_id}", "", summary or "_(no summary)_", ""]
    for op in ops:
        out.append(f"## #{op['id']} {op['op']} `{op['store']}/{op['path']}` — {op['status']}")
        if op["rationale"]:
            out.append(f"\n{op['rationale']}")
        if op["evidence"]:
            out.append(f"\nEvidence: {', '.join(op['evidence'])}")
        if op["problems"]:
            out.append("\nProblems:\n" + "\n".join(f"- {p}" for p in op["problems"]))
        store = _store_of(op)
        before = store.get_blob(op["base"]).decode(errors="replace").splitlines() if op["base"] else []
        after = store.get_blob(op["hash"]).decode(errors="replace").splitlines() if op["hash"] else []
        diff = "\n".join(difflib.unified_diff(before, after, "before", "after", lineterm=""))
        out.append(f"\n```diff\n{diff}\n```\n")
    return "\n".join(out)


def _finish(dream_dir: Path, record: dict) -> None:
    pending = sum(op["status"] == "pending" for op in record["ops"])
    applied = sum(op["status"] == "applied" for op in record["ops"])
    if pending:
        record["status"] = "partially_applied" if applied else "proposed"
    else:
        record["status"] = "applied" if applied else "no_changes" if not record["ops"] else "rejected"
    atomic_write(dream_dir / "result.json", (json.dumps(record, indent=2) + "\n").encode())
    atomic_write(dream_dir / "REPORT.md", _render_report(record["id"], record.get("summary", ""), record["ops"]).encode())


def run(cwd: Path, mode: Optional[str] = None, trigger: str = "manual") -> dict:
    project = detect_project(cwd)
    config, _ = load_config(project)
    if mode:
        config["dream"]["mode"] = mode
    attachments = [a for a in resolve_attachments(config, project) if a.store]
    with _project_lock(project):
        sessions = unconsumed_sessions(project)[-config["dream"]["max_sessions"]:]
        dream_id = _new_dream_id()
        dream_dir = dreams_dir() / dream_id
        (dream_dir / "digest").mkdir(parents=True)
        for att in attachments:
            (dream_dir / "proposal" / att.name).mkdir(parents=True)
            att.store.sync_external(Actor(kind="human", tool="external-edit"))
            att.store.materialize(dream_dir / "proposal" / att.name)
        for s in sessions:
            digest = condense(Path(s.get("transcript_path") or ""), config["dream"]["digest_chars_per_session"])
            (dream_dir / "digest" / f"{s['session_id']}.md").write_text(digest or "(transcript unavailable)")
        record = {"id": dream_id, "project": project.id, "trigger": trigger, "mode": config["dream"]["mode"],
                  "cwd": str(cwd), "started_at": now_iso(), "sessions": [s["session_id"] for s in sessions],
                  "base": {a.name: {"store_id": a.store.root.name, "seq": a.store.head_seq()} for a in attachments}}
        atomic_write(dream_dir / "input.json", (json.dumps(record, indent=2) + "\n").encode())
        _write_task(dream_dir, attachments, sessions, config)

        result = _run_dreamer(dream_dir, config)
        (dream_dir / "dreamer.out").write_text(result.stdout + ("\n--- stderr ---\n" + result.stderr if result.stderr else ""))
        if result.returncode != 0:
            record.update(status="failed", ops=[], error=f"dreamer exited {result.returncode}")
            atomic_write(dream_dir / "result.json", (json.dumps(record, indent=2) + "\n").encode())
            return record

        ops, summary = _diff_ops(dream_dir, attachments, set(record["sessions"]), config)
        for op in ops:
            if op["status"] == "accepted":
                _apply_op(op, dream_id)
        record.update(ops=ops, summary=summary, finished_at=now_iso())
        _finish(dream_dir, record)

        state = load_state()
        project_state = state["projects"].setdefault(project.id, {"consumed": []})
        project_state["consumed"] = sorted(set(project_state["consumed"]) | set(record["sessions"]))
        project_state["last_dream_at"] = now_iso()
        _save_state(state)
        return record


def list_dreams(project: Optional[Project] = None) -> list[dict]:
    records = []
    for path in sorted(dreams_dir().glob("*/result.json"), reverse=True):
        record = json.loads(path.read_text())
        if project is None or record.get("project") == project.id:
            records.append(record)
    return records


def pending_review(project: Project) -> int:
    return sum(op["status"] == "pending" for r in list_dreams(project) for op in r.get("ops", []))


def load(dream_id: str) -> tuple[Path, dict]:
    dream_dir = dreams_dir() / dream_id
    return dream_dir, json.loads((dream_dir / "result.json").read_text())


def decide(dream_id: str, op_ids: Optional[list[int]], accept: bool) -> dict:
    dream_dir, record = load(dream_id)
    for op in record["ops"]:
        if op["status"] != "pending" or (op_ids and op["id"] not in op_ids):
            continue
        if accept:
            _apply_op(op, dream_id)
        else:
            op["status"] = "rejected"
            op["problems"] = op.get("problems", []) + ["rejected by reviewer"]
    _finish(dream_dir, record)
    return record

