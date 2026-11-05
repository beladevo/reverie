from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
from pathlib import Path

from . import dream, hooks
from .config import data_root, detect_project, load_config, resolve_store_id
from .runtime import list_stores, open_store, resolve_attachments
from .store import Actor, ConflictError, Store


def _store(name_or_id: str, cwd: Path) -> Store:
    if name_or_id.startswith(("project.", "user.")):
        return open_store(resolve_store_id(name_or_id, detect_project(cwd)))
    store = Store(data_root() / "stores" / name_or_id)
    if not store.exists():
        sys.exit(f"no such store: {name_or_id} (see `reverie stores`)")
    return store


def _human(cwd: Path) -> Actor:
    return Actor(kind="human", tool="cli", cwd=str(cwd))


def cmd_status(args) -> None:
    project = detect_project(args.cwd)
    config, sources = load_config(project)
    print(f"data:     {data_root()}")
    print(f"project:  {project.id} ({project.label})")
    print(f"config:   {' → '.join(sources)}")
    print("\nattached stores:")
    for att in resolve_attachments(config, project):
        seq = att.store.head_seq() if att.store else "-"
        print(f"  {att.name:<20} {att.mode:<10} {att.kind:<6} files={len(att.files()):<4} seq={seq:<5} {att.tree}")
    schedule = config["dream"]["schedule"]
    print(f"\ndreaming: mode={config['dream']['mode']} schedule={'on' if schedule['enabled'] else 'off'} "
          f"(every {schedule['interval_hours']}h, min {schedule['min_sessions']} sessions)")
    print(f"  sessions waiting: {len(dream.unconsumed_sessions(project, config))}   due: {dream.is_due(project, config)}"
          f"   pending review: {dream.pending_review(project)}")
    last = dream.load_state()["projects"].get(project.id, {}).get("last_dream_at")
    print(f"  last dream: {last or 'never'}")
    errors = data_root() / "errors.log"
    if errors.exists() and errors.stat().st_size:
        print(f"\nhook errors logged in {errors}")
    if args.json:
        print(json.dumps(config, indent=2))


def cmd_stores(args) -> None:
    for store in list_stores():
        meta = store.meta
        print(f"{store.root.name:<32} {meta['kind']:<6} {meta['scope']:<7} files={len(store.tree_files()):<4} seq={store.head_seq()}")


def cmd_create(args) -> None:
    store_id = args.name if not args.name.startswith("project.") else f"{detect_project(args.cwd).id}.{args.name.split('.', 1)[1]}"
    store = Store.create(data_root() / "stores" / store_id, store_id, args.kind, args.scope, args.description or "")
    print(store.root)


def cmd_log(args) -> None:
    store = _store(args.store, args.cwd)
    entries = [e for e in store.log() if not args.path or e["path"] == args.path]
    for e in entries[-args.limit:]:
        actor = e["actor"]
        who = actor.get("kind") + ":" + (actor.get("session") or actor.get("dream") or actor.get("tool") or "")
        flag = " CONFLICT" if e.get("conflict") else ""
        print(f"{e['seq']:>5} {e['ts']} {e['op']:<6} {e['path']:<40} {who}{flag}" + (f"  ({e['reason']})" if e.get("reason") else ""))


def cmd_show(args) -> None:
    store = _store(args.store, args.cwd)
    path, _, seq = args.path.partition("@")
    digest = store.state(int(seq) if seq else None).get(path)
    if digest is None:
        sys.exit(f"{path} does not exist at {'seq ' + seq if seq else 'HEAD'}")
    sys.stdout.write(store.get_blob(digest).decode())


def cmd_put(args) -> None:
    store = _store(args.store, args.cwd)
    data = sys.stdin.buffer.read()
    if_match = None if args.if_match == "none" else args.if_match
    try:
        entry = store.write(args.path, data, _human(args.cwd), if_match=if_match)
    except ConflictError as e:
        sys.exit(f"conflict: {e}")
    print(entry.get("hash") or "unchanged")


def cmd_rollback(args) -> None:
    store = _store(args.store, args.cwd)
    if args.path:
        entries = [store.rollback_path(args.path, args.to_seq, _human(args.cwd))]
    else:
        entries = store.rollback_store(args.to_seq, _human(args.cwd))
    entries = [e for e in entries if e]
    print(f"{len(entries)} file(s) restored to seq {args.to_seq}" if entries else "nothing to roll back")


def cmd_export(args) -> None:
    out = Path(args.out).expanduser().resolve()
    stores = [_store(args.store, args.cwd)] if args.store else list_stores()
    if args.format == "md":
        parts = []
        for store in stores:
            parts.append(f"# {store.root.name} ({store.kind})\n")
            for rel in store.tree_files():
                parts.append(f"\n## {rel}\n\n{(store.tree / rel).read_text()}\n")
        out.write_text("".join(parts))
    else:
        with tarfile.open(out, "w:gz") as tar:
            for store in stores:
                tar.add(store.root, arcname=f"stores/{store.root.name}")
            for extra in ("sessions.jsonl", "config.json"):
                if (data_root() / extra).exists():
                    tar.add(data_root() / extra, arcname=extra)
    print(out)


def cmd_dream(args) -> None:
    try:
        record = dream.run(args.cwd, mode=args.mode, trigger=args.trigger)
    except RuntimeError as e:
        sys.exit(str(e))
    _print_dream(record)


def _print_dream(record: dict) -> None:
    print(f"dream {record['id']}: {record.get('status')}  sessions={len(record.get('sessions', []))}")
    if record.get("summary"):
        print(f"\n{record['summary']}\n")
    for op in record.get("ops", []):
        print(f"  #{op['id']:<3} {op['status']:<9} {op['op']:<6} {op['store']}/{op['path']}")
        for problem in op.get("problems", []):
            print(f"         - {problem}")
    print(f"\nreport: {dream.dreams_dir() / record['id'] / 'REPORT.md'}")


def cmd_dreams(args) -> None:
    project = None if args.all else detect_project(args.cwd)
    for record in dream.list_dreams(project)[: args.limit]:
        counts: dict = {}
        for op in record.get("ops", []):
            counts[op["status"]] = counts.get(op["status"], 0) + 1
        summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"{record['id']}  {record.get('status', '?'):<17} {record.get('trigger', ''):<8} {summary}")


def cmd_review(args) -> None:
    dream_dir, record = dream.load(args.dream)
    print((dream_dir / "REPORT.md").read_text())


def cmd_decide(args) -> None:
    ops = [int(x) for x in args.ops.split(",")] if args.ops else None
    _print_dream(dream.decide(args.dream, ops, accept=args.command == "accept"))


def cmd_dashboard(args) -> None:
    if args.once or not (sys.stdin.isatty() and sys.stdout.isatty()):
        from . import dashboard

        for text, _ in dashboard.overview(dashboard.take(args.cwd), 100):
            print(text)
        return
    from . import tui

    tui.run(args.cwd)


def cmd_hook(args) -> None:
    sys.exit(hooks.run(args.event))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reverie", description="Versioned memory + dreaming for Claude Code")
    parser.add_argument("--cwd", type=Path, default=Path(os.getcwd()), help="project directory (default: current)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="effective config, attached stores, dream state")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    sub.add_parser("stores", help="list all stores").set_defaults(fn=cmd_stores)

    p = sub.add_parser("create", help="create a store")
    p.add_argument("name")
    p.add_argument("--kind", choices=["policy", "notes"], default="notes")
    p.add_argument("--scope", choices=["user", "project"], default="user")
    p.add_argument("--description")
    p.set_defaults(fn=cmd_create)

    p = sub.add_parser("log", help="version history of a store")
    p.add_argument("store")
    p.add_argument("--path")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("show", help="print a file, optionally at a version: path@seq")
    p.add_argument("store")
    p.add_argument("path")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("put", help="write stdin to a memory file (optimistic: --if-match <hash>|none|*)")
    p.add_argument("store")
    p.add_argument("path")
    p.add_argument("--if-match", default="*")
    p.set_defaults(fn=cmd_put)

    p = sub.add_parser("rollback", help="restore a file or a whole store to an earlier seq (appends new versions)")
    p.add_argument("store")
    p.add_argument("--to-seq", type=int, required=True)
    p.add_argument("--path")
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("export", help="export stores as a tarball or one markdown file")
    p.add_argument("out")
    p.add_argument("--store")
    p.add_argument("--format", choices=["tar", "md"], default="tar")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("dream", help="run a dream for this project now")
    p.add_argument("--mode", choices=["auto", "review"])
    p.add_argument("--trigger", default="manual")
    p.set_defaults(fn=cmd_dream)

    p = sub.add_parser("dreams", help="list dreams")
    p.add_argument("--all", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_dreams)

    p = sub.add_parser("review", help="show a dream's report with diffs")
    p.add_argument("dream")
    p.set_defaults(fn=cmd_review)

    for name in ("accept", "reject"):
        p = sub.add_parser(name, help=f"{name} pending ops of a dream")
        p.add_argument("dream")
        p.add_argument("--ops", help="comma-separated op ids (default: all pending)")
        p.set_defaults(fn=cmd_decide)

    p = sub.add_parser("dashboard", help="live terminal dashboard: stores, files and history, dreams, sessions")
    p.add_argument("--once", action="store_true", help="print the overview and exit")
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("hook", help="internal: Claude Code hook entrypoint")
    p.add_argument("event", choices=sorted(hooks.HANDLERS))
    p.set_defaults(fn=cmd_hook)
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.cwd = args.cwd.resolve()
    args.fn(args)
