from __future__ import annotations

import contextlib
import difflib
import fcntl
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import Project, data_root, detect_project, load_config
from .runtime import Attachment, resolve_attachments, stores_dir
from .store import Actor, ConflictError, Store, atomic_write, hash_file, now_iso
from .sessions import DREAM_WORKSPACE_PREFIX, inventory, recorded
from .transcripts import condense, is_substantive
from .validate import parse_frontmatter, validate_memory

PROMPT_FILE = Path(__file__).parent / "prompts" / "dreamer.md"
# dontAsk denies anything not allowed here. Read(./**) also scopes Glob and Grep; Edit(./**) covers Write.
DREAM_ALLOWED = ["Read(./**)", "Edit(./**)"]
DREAM_DISALLOWED = ["Bash", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"]
WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}
MIN_EVIDENCE_PREFIX = 8
BACKLOG_GAP = timedelta(hours=1)
FAILURE_RETRIES = 3


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


def session_inventory(project: Project, config: dict) -> tuple[list, list]:
    project_state = load_state()["projects"].get(project.id, {})
    return inventory(project.id, project_state, config["dream"])


def unconsumed_sessions(project: Project, config: dict) -> list[dict]:
    return session_inventory(project, config)[0]


def dream_gap(project_state: dict, config: dict, waiting: int) -> timedelta:
    """A backlog larger than one dream can take is worked off hourly. A failure (an expired login, a refused write)
    is retried hourly a few times, then falls back to the normal interval so a persistent fault doesn't burn money."""
    succeeded = (project_state.get("last_dream_at") or "") >= (project_state.get("last_attempt_at") or "")
    if waiting > config["dream"]["max_sessions"] and succeeded:
        return BACKLOG_GAP
    if 0 < project_state.get("failures", 0) <= FAILURE_RETRIES:
        return BACKLOG_GAP
    return timedelta(hours=config["dream"]["schedule"]["interval_hours"])


def last_try(project_state: dict) -> Optional[str]:
    return max(filter(None, [project_state.get("last_dream_at"), project_state.get("last_attempt_at")]), default=None)


def is_due(project: Project, config: dict) -> bool:
    schedule = config["dream"]["schedule"]
    if not schedule.get("enabled"):
        return False
    project_state = load_state()["projects"].get(project.id, {})
    last = last_try(project_state)
    if last and datetime.now(timezone.utc) - _parse_ts(last) < BACKLOG_GAP:
        return False
    waiting = len(unconsumed_sessions(project, config))
    if last and datetime.now(timezone.utc) - _parse_ts(last) < dream_gap(project_state, config, waiting):
        return False
    return waiting >= schedule["min_sessions"]


def maybe_schedule(project: Project, config: dict, cwd: str) -> bool:
    return is_due(project, config) and spawn(project, cwd, "schedule")


def spawn(project: Project, cwd: str, trigger: str) -> bool:
    if _lock_held(project):
        return False
    log = open(dreams_dir() / f"{project.id}.log", "a")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).parents[2] / "bin" / "reverie"), "--cwd", cwd, "dream", "--trigger", trigger],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
    )
    return True


def _running_path(project: Project) -> Path:
    return dreams_dir() / f"{project.id}.running"


def running(project: Project) -> Optional[dict]:
    if not _lock_held(project):
        return None
    path = _running_path(project)
    dream_dir = dreams_dir() / (path.read_text().strip() if path.exists() else "-")
    if (dream_dir / "result.json").exists():
        return None
    if (dream_dir / "input.json").exists():
        return json.loads((dream_dir / "input.json").read_text())
    return {"project": project.id}


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


def _run_dreamer(workspace: Path, config: dict) -> subprocess.CompletedProcess:
    claude = os.environ.get("REVERIE_CLAUDE_BIN", "claude")
    prompt = PROMPT_FILE.read_text() + "\n\nStart now: read TASK.md."
    cmd = [claude, "-p", prompt,
           "--model", config["dream"]["model"],
           "--max-turns", str(config["dream"]["max_turns"]),
           "--permission-mode", "dontAsk",
           "--allowedTools", *DREAM_ALLOWED,
           "--disallowedTools", *DREAM_DISALLOWED,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "project",
           "--output-format", "json"]
    env = {**os.environ, "REVERIE_DISABLE": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    try:
        return subprocess.run(cmd, cwd=workspace, env=env, capture_output=True, text=True,
                              timeout=config["dream"]["timeout_minutes"] * 60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, _as_text(e.stdout), f"timed out after {config['dream']['timeout_minutes']}m")
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", f"could not start {claude}: {e}")


def _dreamer_result(stdout: str) -> dict:
    """The final `result` event of `claude -p --output-format json`, which is one object or a list of events."""
    candidates = []
    try:
        data = json.loads(stdout)
        candidates = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        for line in stdout.splitlines():
            try:
                candidates.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    results = [c for c in candidates if isinstance(c, dict) and c.get("type") == "result"]
    return results[-1] if results else {}


def _dreamer_failure(result: subprocess.CompletedProcess, outcome: dict, wrote_changes: bool) -> Optional[str]:
    """A refused write always fails the dream: its proposal is incomplete. Anything else (an error result, a
    non-zero exit such as max turns) fails it only if the dreamer never got as far as writing changes.json."""
    refused_writes = [d for d in outcome.get("permission_denials") or [] if d.get("tool_name") in WRITE_TOOLS]
    if refused_writes:
        tools = ", ".join(sorted({d["tool_name"] for d in refused_writes}))
        return f"dreamer was refused {len(refused_writes)} write(s): {tools}"
    if wrote_changes:
        return None
    if outcome.get("is_error"):
        return f"dreamer error: {outcome.get('result') or outcome.get('terminal_reason') or 'unknown'}"
    if result.returncode != 0:
        return f"dreamer exited {result.returncode}"
    return None


def _refused_reads(outcome: dict) -> list[str]:
    return [f"refused {d.get('tool_name', '?')}: {json.dumps(d.get('tool_input') or {})[:160]}"
            for d in outcome.get("permission_denials") or [] if d.get("tool_name") not in WRITE_TOOLS]


def _as_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


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
    """Distinct real sessions the change cites. A citation counts only if it names exactly one known session."""
    cited = {str(e) for e in change.get("evidence") or []}
    if content:
        meta, _ = parse_frontmatter(content)
        if isinstance(meta.get("evidence"), list):
            cited |= {str(e) for e in meta["evidence"]}
    matched = set()
    for citation in cited:
        if citation in known_sessions:
            matched.add(citation)
            continue
        if len(citation) < MIN_EVIDENCE_PREFIX:
            continue
        candidates = [s for s in known_sessions if s.startswith(citation)]
        if len(candidates) == 1:
            matched.add(candidates[0])
    return matched


def _classify(op: dict, change: Optional[dict], att: Attachment, content: Optional[str],
              session_ids: set[str], config: dict) -> tuple[str, list[str]]:
    problems: list[str] = []
    if change is None or not change.get("rationale"):
        problems.append("no rationale in changes.json")
    if content is not None:
        problems += validate_memory(op["path"], content, config["limits"])
    if att.kind == "policy" and op["op"] == "put":
        evidence = _evidence(change or {}, content, session_ids | _historic_sessions())
        if len(evidence) < config["dream"]["promote_min_sessions"]:
            problems.append(f"policy change cites {len(evidence)} known session(s), needs {config['dream']['promote_min_sessions']}")
    if problems:
        return "rejected", problems
    needs_review = config["dream"]["mode"] == "review" or (att.kind == "policy" and not config["dream"]["auto_promote"])
    return ("pending" if needs_review else "accepted"), []


def _historic_sessions() -> set[str]:
    ids = {r["session_id"] for r in recorded()}
    for project_state in load_state()["projects"].values():
        ids |= set(project_state.get("consumed", []))
    return ids


def _diff_ops(dream_dir: Path, attachments: list[Attachment], base: dict, session_ids: set[str],
              config: dict) -> tuple[list[dict], str]:
    """Diffs against the stores as the dreamer saw them, so a session writing mid-dream isn't read as a delete."""
    index, summary = _rationale_index(dream_dir)
    ops = []
    for att in attachments:
        base_state = att.store.state(at_seq=base[att.name]["seq"])
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
                att.store.put_blob(proposed[rel].read_bytes())
            change = index.get((att.name, rel))
            op["status"], op["problems"] = _classify(op, change, att, content, session_ids, config)
            op["evidence"] = sorted((change or {}).get("evidence") or [])
            op["rationale"] = (change or {}).get("rationale", "")
            ops.append(op)
    _hold_promoted_notes(ops, {a.name: a for a in attachments})
    return ops, summary


def _memory_name(store: Store, digest: Optional[str], rel: str) -> str:
    if digest:
        meta, _ = parse_frontmatter(store.get_blob(digest).decode(errors="replace"))
        if isinstance(meta.get("name"), str):
            return meta["name"]
    return Path(rel).stem


def _hold_promoted_notes(ops: list[dict], attachments: dict[str, Attachment]) -> None:
    """A promotion deletes a note and creates a policy file; the delete must not outrun the policy review."""
    policy_puts = [op for op in ops if op["op"] == "put" and attachments[op["store"]].kind == "policy"]
    any_pending = any(op["status"] == "pending" for op in policy_puts)
    rejected = {key for op in policy_puts if op["status"] == "rejected"
                for key in (Path(op["path"]).stem, _memory_name(attachments[op["store"]].store, op["hash"], op["path"]))}
    for op in ops:
        if op["op"] != "delete" or op["status"] != "accepted" or attachments[op["store"]].kind == "policy":
            continue
        keys = {Path(op["path"]).stem, _memory_name(attachments[op["store"]].store, op["base"], op["path"])}
        if keys & rejected:
            op["status"], op["problems"] = "rejected", ["its promotion to policy was rejected"]
        elif any_pending:
            op["status"], op["problems"] = "pending", ["held until this dream's policy changes are reviewed"]


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


def _render_report(dream_id: str, summary: str, ops: list[dict], error: str = "", warnings: tuple = ()) -> str:
    out = [f"# Dream {dream_id}", ""]
    if error:
        out += [f"**Failed:** {error}", ""]
    if summary or not error:
        out += [summary or "_(no summary)_", ""]
    if warnings:
        out += ["Warnings:", *(f"- {w}" for w in warnings), ""]
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
    atomic_write(dream_dir / "REPORT.md", _render_report(record["id"], record.get("summary", ""), record["ops"],
                                                         warnings=record.get("warnings", ())).encode())


def _fail(dream_dir: Path, record: dict, error: str, summary: str) -> dict:
    record.update(status="failed", ops=[], error=error, summary=summary, finished_at=now_iso())
    atomic_write(dream_dir / "result.json", (json.dumps(record, indent=2) + "\n").encode())
    atomic_write(dream_dir / "REPORT.md", _render_report(record["id"], summary, [], error, record.get("warnings", ())).encode())
    _record_outcome(record, [], failed=True)
    return record


def _pick_sessions(waiting: list[dict], config: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """(reviewed, deferred, trivial). Sessions with nothing to learn from — a missing transcript, a lone /model —
    take no slot and are consumed unread, so they neither pad a dream nor keep the backlog loop running."""
    reviewed, trivial = [], []
    for s in reversed(waiting):
        if len(reviewed) == config["dream"]["max_sessions"]:
            break
        s["digest"] = condense(Path(s.get("transcript_path") or ""), config["dream"]["digest_chars_per_session"],
                               since=s.get("reviewed_until"))
        (reviewed if is_substantive(s["digest"]) else trivial).append(s)
    picked = {id(s) for s in reviewed + trivial}
    deferred = [s for s in waiting if id(s) not in picked]
    return list(reversed(reviewed)), deferred, trivial


def _record_outcome(record: dict, consumed: list[dict], failed: bool) -> None:
    state = load_state()
    project_state = state["projects"].setdefault(record["project"], {"consumed": []})
    project_state["consumed"] = sorted(set(project_state["consumed"]) | {s["session_id"] for s in consumed})
    project_state.setdefault("consumed_mtime", {}).update({s["session_id"]: s["mtime"] for s in consumed if s.get("mtime")})
    if failed:
        project_state["failures"] = project_state.get("failures", 0) + 1
    else:
        project_state["failures"] = 0
        project_state["last_dream_at"] = now_iso()
    _save_state(state)


def run(cwd: Path, mode: Optional[str] = None, trigger: str = "manual") -> dict:
    project = detect_project(cwd)
    config, _ = load_config(project)
    if mode:
        config["dream"]["mode"] = mode
    attachments = [a for a in resolve_attachments(config, project) if a.store]
    with _project_lock(project):
        dream_id = _new_dream_id()
        atomic_write(_running_path(project), dream_id.encode())
        _mark_attempt(project)
        waiting = unconsumed_sessions(project, config)
        for s in waiting:
            transcript = Path(s.get("transcript_path") or "")
            s["mtime"] = transcript.stat().st_mtime if transcript.is_file() else None
        sessions, deferred, trivial = _pick_sessions(waiting, config)
        dream_dir = dreams_dir() / dream_id
        dream_dir.mkdir(parents=True)
        for att in attachments:
            att.store.sync_external(Actor(kind="external", tool="external-edit"))
        record = {"id": dream_id, "project": project.id, "trigger": trigger, "mode": config["dream"]["mode"],
                  "cwd": str(cwd), "started_at": now_iso(), "sessions": [s["session_id"] for s in sessions],
                  "deferred_sessions": [s["session_id"] for s in deferred],
                  "skipped_sessions": [s["session_id"] for s in trivial],
                  "base": {a.name: {"store_id": a.store.root.name, "seq": a.store.head_seq()} for a in attachments}}
        atomic_write(dream_dir / "input.json", (json.dumps(record, indent=2) + "\n").encode())

        if not sessions and trigger == "schedule":
            record.update(status="no_changes", ops=[], summary="No session had anything to review.", finished_at=now_iso())
            _finish(dream_dir, record)
            _record_outcome(record, trivial, failed=False)
            return record

        # Claude Code refuses unattended writes under its own config dir, where the plugin data lives.
        with tempfile.TemporaryDirectory(prefix=DREAM_WORKSPACE_PREFIX) as tmp:
            workspace = Path(tmp)
            (workspace / "digest").mkdir()
            for att in attachments:
                (workspace / "proposal" / att.name).mkdir(parents=True)
                att.store.materialize(workspace / "proposal" / att.name, at_seq=record["base"][att.name]["seq"])
            for s in sessions:
                digest = s["digest"]
                if s.get("reviewed_until"):
                    digest = "(resumed session: only activity since the last dream is shown)\n\n" + digest
                (workspace / "digest" / f"{s['session_id']}.md").write_text(digest)
            _write_task(workspace, attachments, sessions, config)

            result = _run_dreamer(workspace, config)
            for name in ("TASK.md", "digest", "proposal", "changes.json"):
                source = workspace / name
                if source.is_dir():
                    shutil.copytree(source, dream_dir / name)
                elif source.exists():
                    shutil.copy2(source, dream_dir / name)
        (dream_dir / "dreamer.out").write_text(result.stdout + ("\n--- stderr ---\n" + result.stderr if result.stderr else ""))
        outcome = _dreamer_result(result.stdout)
        record["warnings"] = _refused_reads(outcome)
        error = _dreamer_failure(result, outcome, (dream_dir / "changes.json").exists())
        if error:
            final_message = str(outcome.get("result") or "")
            return _fail(dream_dir, record, error, "" if final_message and final_message in error else final_message)

        ops, summary = _diff_ops(dream_dir, attachments, record["base"], set(record["sessions"]), config)
        for op in ops:
            if op["status"] == "accepted":
                _apply_op(op, dream_id)
        record.update(ops=ops, summary=summary or str(outcome.get("result") or ""), finished_at=now_iso())
        _finish(dream_dir, record)
        _record_outcome(record, sessions + trivial, failed=False)
        return record


def _mark_attempt(project: Project) -> None:
    state = load_state()
    state["projects"].setdefault(project.id, {"consumed": []})["last_attempt_at"] = now_iso()
    _save_state(state)


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
    with open(dreams_dir() / dream_id / "decide.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
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

