from __future__ import annotations

import difflib
import functools
import re
import textwrap
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import dream
from .config import Project, data_root, detect_project, load_config
from .runtime import Attachment, resolve_attachments, stores_dir
from .store import Store
from .transcripts import condense
from .validate import parse_frontmatter

Line = tuple
ERROR_HEADER = re.compile(r"^(\d{4}-\d\d-\d\dT\S+Z) hook (\S+)$")
STATUS_STYLE = {"applied": "ok", "accepted": "ok", "no_changes": "dim", "proposed": "warn", "pending": "warn",
                "partially_applied": "warn", "rejected": "bad", "failed": "bad", "stale": "bad", "running": "accent"}
TALLY = (("applied", "+"), ("pending", "?"), ("rejected", "x"), ("stale", "!"))


@dataclass
class Snapshot:
    cwd: Path
    project: Project
    config: dict
    sources: list
    attachments: list
    files: dict
    logs: dict
    waiting: list
    project_state: dict
    dreams: list
    running: Optional[dict]
    errors: list
    now: datetime


@dataclass
class Node:
    kind: str
    att: Attachment
    rel: str = ""

    @property
    def key(self) -> tuple:
        return self.att.name, self.kind, self.rel


def take(cwd: Path) -> Snapshot:
    project = detect_project(cwd)
    config, sources = load_config(project)
    attachments = resolve_attachments(config, project)
    return Snapshot(
        cwd=cwd, project=project, config=config, sources=sources, attachments=attachments,
        files={a.name: a.files() for a in attachments},
        logs={a.name: list(a.store.log()) if a.store else [] for a in attachments},
        waiting=dream.unconsumed_sessions(project),
        project_state=dream.load_state()["projects"].get(project.id, {}),
        dreams=dream.list_dreams(project), running=dream.running(project),
        errors=_hook_errors(), now=datetime.now(timezone.utc),
    )


def _hook_errors() -> list:
    path = data_root() / "errors.log"
    if not path.exists():
        return []
    headers = [m.groups() for m in map(ERROR_HEADER.match, path.read_text(errors="replace").splitlines()) if m]
    return headers[-5:]


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def local(value: Optional[str]) -> str:
    if not value:
        return "never"
    try:
        return parse_ts(value).astimezone().strftime("%b %d %H:%M")
    except ValueError:
        return value


def span(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def who(actor: dict) -> str:
    detail = (actor.get("session") or "")[:8] or actor.get("dream") or actor.get("tool") or ""
    return f"{actor.get('kind', '?')}:{detail}" if detail else actor.get("kind", "?")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def wrap(text: str, width: int, style: str = "", indent: str = "") -> list:
    rows = []
    for para in str(text).splitlines() or [""]:
        chunks = textwrap.wrap(para, max(width - len(indent), 10), break_on_hyphens=False) or [""]
        rows += [(indent + chunk, style) for chunk in chunks]
    return rows


def section(title: str) -> list:
    return [("", ""), (title.upper(), "title")]


def blob_text(store: Store, digest: Optional[str]) -> str:
    if not digest:
        return ""
    try:
        return store.get_blob(digest).decode(errors="replace")
    except FileNotFoundError:
        return "(version missing from object store)"


def diff_lines(before: str, after: str, width: int) -> list:
    rows = []
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), "before", "after", lineterm=""):
        if line.startswith(("+++", "---")):
            style = "dim"
        elif line.startswith("+"):
            style = "add"
        elif line.startswith("-"):
            style = "del"
        else:
            style = "accent" if line.startswith("@@") else ""
        rows += [(chunk, style) for chunk in textwrap.wrap(line, max(width, 20), subsequent_indent="  ", drop_whitespace=False) or [""]]
    return rows or [("(no textual change)", "dim")]


def pending_count(snap: Snapshot) -> int:
    return sum(op["status"] == "pending" for record in snap.dreams for op in record.get("ops", []))


def next_dream(snap: Snapshot) -> tuple:
    schedule = snap.config["dream"]["schedule"]
    if snap.running is not None:
        return "running now", "accent"
    if not schedule.get("enabled"):
        return "schedule off, run on demand (n)", "dim"
    stamps = [snap.project_state.get("last_dream_at"), snap.project_state.get("last_attempt_at")]
    last = max(filter(None, stamps), default=None)
    remaining = parse_ts(last) + timedelta(hours=schedule["interval_hours"]) - snap.now if last else timedelta(0)
    missing = schedule["min_sessions"] - len(snap.waiting)
    reasons = []
    if remaining > timedelta(0):
        reasons.append(f"in {span(remaining)}")
    if missing > 0:
        reasons.append(f"after {plural(missing, 'more session')}")
    if not reasons:
        return "due, starts with the next Claude Code session", "warn"
    return " and ".join(reasons), ""


def overview(snap: Snapshot, width: int) -> list:
    rows = [(snap.project.label, "title"),
            (f"project {snap.project.id} · data {data_root()}", "dim"),
            (f"config  {' → '.join(snap.sources)}", "dim")]
    rows += section("Stores")
    rows.append((f"  {'store':<20} {'access':<11} {'kind':<7} {'files':>5} {'versions':>8}  last change", "dim"))
    for att in snap.attachments:
        log = snap.logs[att.name]
        if log:
            change = f"{local(log[-1]['ts'])}  {who(log[-1]['actor'])}"
        else:
            change = "external directory" if att.store is None else "—"
        rows.append((f"  {att.name:<20} {att.mode:<11} {att.kind:<7} {len(snap.files[att.name]):>5} "
                     f"{len(log) if att.store else '-':>8}  {change}", ""))

    dream_config, schedule = snap.config["dream"], snap.config["dream"]["schedule"]
    rows += section("Dreaming")
    cadence = f"every {schedule['interval_hours']}h, min {plural(schedule['min_sessions'], 'session')}" if schedule["enabled"] else "off"
    rows.append((f"  mode {dream_config['mode']} · model {dream_config['model']} · schedule {cadence}", "dim"))
    if snap.running is not None:
        started = snap.running.get("started_at")
        elapsed = f", {span(snap.now - parse_ts(started))} so far" if started else ""
        rows.append((f"  ● dreaming now over {plural(len(snap.running.get('sessions', [])), 'session')}{elapsed}", "accent"))
    last = snap.dreams[0] if snap.dreams else None
    if last:
        rows.append((f"  last dream    {local(last.get('started_at'))} · {last.get('status')} · {last.get('trigger')}",
                     STATUS_STYLE.get(last.get("status"), "")))
    else:
        rows.append(("  last dream    never", "dim"))
    text, style = next_dream(snap)
    rows.append((f"  next dream    {text}", style))
    rows.append((f"  waiting       {plural(len(snap.waiting), 'session')}", ""))
    pending = pending_count(snap)
    rows.append((f"  needs review  {plural(pending, 'change')}" + ("  → Dreams tab (3)" if pending else ""),
                 "warn" if pending else ""))

    rows += section("Recent activity")
    activity = sorted(((entry, name) for name, log in snap.logs.items() for entry in log),
                      key=lambda pair: (pair[0]["ts"], pair[0]["seq"]), reverse=True)[:12]
    if not activity:
        rows.append(("  no memory changes yet", "dim"))
    for entry, name in activity:
        conflict = "  CONFLICT" if entry.get("conflict") else ""
        style = "bad" if conflict else "del" if entry["op"] == "delete" else ""
        rows.append((f"  {local(entry['ts'])}  {entry['op']:<6} {name}/{entry['path']}  {who(entry['actor'])}{conflict}", style))

    if snap.errors:
        rows += section("Hook errors")
        rows += [(f"  {local(ts)}  {event}", "bad") for ts, event in snap.errors]
        rows.append((f"  details: {data_root() / 'errors.log'}", "dim"))
    return rows


def memory_nodes(snap: Snapshot) -> list:
    nodes = []
    for att in snap.attachments:
        nodes.append(Node("store", att))
        dirs = set()
        for rel in snap.files[att.name]:
            parts = rel.split("/")
            for depth in range(1, len(parts)):
                directory = "/".join(parts[:depth])
                if directory not in dirs:
                    dirs.add(directory)
                    nodes.append(Node("dir", att, directory))
            nodes.append(Node("file", att, rel))
    return nodes


def node_label(snap: Snapshot, node: Node) -> Line:
    if node.kind == "store":
        return f"{node.att.name}  ({len(snap.files[node.att.name])})", "title"
    indent = "  " * (node.rel.count("/") + 1)
    name = node.rel.rsplit("/", 1)[-1]
    return (f"{indent}{name}/", "dim") if node.kind == "dir" else (f"{indent}{name}", "")


def _describe(att: Attachment, rel: str) -> str:
    try:
        meta, _ = parse_frontmatter((att.tree / rel).read_text(errors="replace"))
    except OSError:
        return ""
    return str(meta.get("description") or "")


def _file_index(snap: Snapshot, att: Attachment, prefix: str, width: int) -> list:
    rows = []
    for rel in snap.files[att.name]:
        if prefix and not rel.startswith(prefix + "/"):
            continue
        rows.append((f"  {rel}", ""))
        description = _describe(att, rel)
        if description:
            rows += wrap(description, width, "dim", "      ")
    return rows or [("  (no files)", "dim")]


def node_view(snap: Snapshot, node: Node, width: int) -> list:
    att = node.att
    if node.kind == "file":
        return file_view(snap, att, node.rel, width)
    if node.kind == "dir":
        return [(f"{att.name}/{node.rel}/", "title"), ("", "")] + _file_index(snap, att, node.rel, width)
    rows = [(att.name, "title")]
    if att.store:
        meta = att.store.meta
        rows += wrap(meta.get("description", ""), width)
        rows.append((f"{att.kind} · {att.mode} · {plural(len(snap.files[att.name]), 'file')} · "
                     f"{plural(len(snap.logs[att.name]), 'version')} · created {local(meta.get('created_at'))}", "dim"))
    else:
        rows.append(("read-only directory attached from the project", "dim"))
    rows.append((str(att.tree), "dim"))
    if att.kind == "policy":
        rows.append(("Claude can read but not write this store; it changes through dreaming and your review.", "accent"))
    return rows + [("", "")] + _file_index(snap, att, "", width)


def file_view(snap: Snapshot, att: Attachment, rel: str, width: int) -> list:
    path = att.tree / rel
    text = path.read_text(errors="replace") if path.exists() else ""
    meta, _ = parse_frontmatter(text)
    history = [e for e in snap.logs[att.name] if e["path"] == rel]
    rows = [(f"{att.name}/{rel}", "title")]
    if meta.get("description"):
        rows += wrap(meta["description"], width)
    facts = [f"{key} {meta[key]}" for key in ("type", "confidence") if meta.get(key)]
    facts += [f"{len(text.encode())} bytes", plural(len(history), "version") if att.store else "not versioned"]
    rows.append((" · ".join(facts), "dim"))
    if history:
        rows.append((f"last change {local(history[-1]['ts'])} by {who(history[-1]['actor'])}"
                     + ("  → for history" if att.store else ""), "dim"))
    rows.append(("─" * min(width, 72), "dim"))
    lines = text.splitlines()
    frontmatter_end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), -1) if lines[:1] == ["---"] else -1
    for i, line in enumerate(lines):
        rows += wrap(line, width, "dim" if i <= frontmatter_end else "")
    return rows


def history_view(snap: Snapshot, att: Attachment, rel: str, index: int, width: int) -> tuple:
    history = list(reversed([e for e in snap.logs[att.name] if e["path"] == rel]))
    rows = [(f"{att.name}/{rel} · history, newest first", "title"), ("", "")]
    if not history:
        return rows + [("no version history for this file", "dim")], None, 0
    index = min(index, len(history) - 1)
    for i, entry in enumerate(history):
        extra = f"  {entry['reason']}" if entry.get("reason") else ""
        extra += "  CONFLICT" if entry.get("conflict") else ""
        rows.append((f"{'›' if i == index else ' '} #{entry['seq']:<4} {local(entry['ts'])}  {entry['op']:<6} "
                     f"{who(entry['actor'])}{extra}", "sel" if i == index else ""))
    entry = history[index]
    rows += [("", ""), (f"what #{entry['seq']} changed", "title")]
    rows += diff_lines(blob_text(att.store, entry.get("parent")), blob_text(att.store, entry.get("hash")), width)
    return rows, 2 + index, len(history)


def dream_rows(snap: Snapshot) -> list:
    running = [{"running": True, "id": "(running)", **snap.running}] if snap.running is not None else []
    return running + snap.dreams


def dream_label(record: dict) -> Line:
    if record.get("running"):
        return f"{local(record.get('started_at'))}  ● running", "accent"
    counts = Counter(op["status"] for op in record.get("ops", []))
    tally = " ".join(f"{mark}{counts[status]}" for status, mark in TALLY if counts[status])
    status = record.get("status", "?")
    return f"{local(record.get('started_at'))}  {status:<17} {tally}", STATUS_STYLE.get(status, "")


def dream_view(snap: Snapshot, record: dict, index: Optional[int], width: int) -> tuple:
    if record.get("running"):
        started = record.get("started_at")
        rows = [("Dreaming now", "accent"), ("", "")]
        if started:
            rows.append((f"started {local(started)}, {span(snap.now - parse_ts(started))} ago · "
                         f"{plural(len(record.get('sessions', [])), 'session')} to review", ""))
        rows += wrap("The dreamer works on a copy of your stores. Memory is only touched after it finishes and "
                     "each change passes validation.", width, "dim")
        return rows, None, 0

    dream_dir = dream.dreams_dir() / record["id"]
    status = record.get("status", "?")
    rows = [(f"Dream {record['id']}", "title"),
            (f"{status} · trigger {record.get('trigger')} · mode {record.get('mode')}", STATUS_STYLE.get(status, "")),
            (f"{local(record.get('started_at'))} → {local(record.get('finished_at'))} · "
             f"{plural(len(record.get('sessions', [])), 'session')} reviewed"
             + (f", {len(record['skipped_sessions'])} skipped" if record.get("skipped_sessions") else ""), "dim")]
    if record.get("summary"):
        rows += [("", "")] + wrap(record["summary"], width)
    if record.get("error"):
        rows += [("", ""), (record["error"], "bad")]
        out = dream_dir / "dreamer.out"
        if out.exists():
            rows += [("dreamer output (last lines):", "dim")]
            rows += [(line, "dim") for line in out.read_text(errors="replace").splitlines()[-20:]]
    ops = record.get("ops", [])
    if not ops:
        return rows + [("", ""), ("no changes proposed", "dim")], None, 0

    rows += [("", ""), ("CHANGES", "title")]
    anchor = None
    for i, op in enumerate(ops):
        selected = index is not None and i == index
        if selected:
            anchor = len(rows)
        rows.append((f"{'›' if selected else ' '} #{op['id']:<3} {op['status']:<9} {op['op']:<6} {op['store']}/{op['path']}",
                     "sel" if selected else STATUS_STYLE.get(op["status"], "")))
    if index is None:
        return rows + [("", ""), ("→ to inspect each change", "dim")], None, len(ops)

    op = ops[min(index, len(ops) - 1)]
    rows += [("", ""), (f"#{op['id']} {op['op']} {op['store']}/{op['path']}", "title")]
    if op.get("rationale"):
        rows += wrap(op["rationale"], width)
    if op.get("evidence"):
        rows += wrap("evidence: " + ", ".join(op["evidence"]), width, "dim")
    rows += [(f"✗ {problem}", "bad") for problem in op.get("problems", [])]
    if op["status"] == "pending":
        rows.append(("a accept · x reject", "warn"))
    store = Store(stores_dir() / op["store_id"])
    rows += [("", "")] + diff_lines(blob_text(store, op.get("base")), blob_text(store, op.get("hash")), width)
    return rows, anchor, len(ops)


def session_label(record: dict) -> Line:
    return f"{local(record.get('ended_at'))}  {str(record.get('session_id'))[:8]}  {Path(record.get('cwd') or '').name}", ""


@functools.lru_cache(maxsize=16)
def _digest(path: str, mtime: float, limit: int) -> str:
    return condense(Path(path), limit)


def session_view(snap: Snapshot, record: dict, width: int) -> list:
    rows = [(f"session {record.get('session_id')}", "title"),
            (f"ended {local(record.get('ended_at'))} · {record.get('reason') or 'unknown reason'}", "dim"),
            (f"cwd {record.get('cwd')}", "dim"),
            ("wrote to " + (", ".join(record.get("touched") or []) or "no memory stores"), "dim"),
            ("", ""), ("WHAT THE DREAMER WILL READ", "title")]
    transcript = Path(record.get("transcript_path") or "")
    if not transcript.is_file():
        return rows + [("(transcript no longer available)", "dim")]
    limit = snap.config["dream"]["digest_chars_per_session"]
    digest = _digest(str(transcript), transcript.stat().st_mtime, limit)
    for line in digest.splitlines():
        style = "accent" if line.startswith("USER") else "bad" if line.startswith("[tool error") else "dim" if line.startswith("[tool") else ""
        rows += wrap(line, width, style)
    return rows


def sessions_header(snap: Snapshot) -> str:
    return f"{plural(len(snap.waiting), 'session')} waiting · {len(snap.project_state.get('consumed', []))} already dreamed over"
