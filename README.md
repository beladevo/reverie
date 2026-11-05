# Reverie

Persistent, versioned memory for Claude Code — and a background "dreaming" job that keeps it clean.

New here? Read the [user guide](docs/GUIDE.md).

- **Plain Markdown.** Memory is a directory of small Markdown files. Claude reads and writes them with its
  normal tools; you can open them in any editor.
- **Every write is a version.** Each change is stored content-addressed with who made it (session, tool,
  dream or human). Nothing is overwritten; rollback appends a new version.
- **Safe concurrency.** A session must have read the current version of a file before changing it, so
  parallel sessions can't silently clobber each other.
- **Trusted vs raw.** `policy` stores are read-only to the agent. `notes` stores collect raw observations.
  Notes become policy only with evidence from several sessions.
- **Dreaming.** Between sessions, a headless Claude reviews recent transcripts against memory: records
  recurring mistakes, merges duplicates, fixes stale facts, prunes noise, proposes promotions. Every change
  is validated and must cite evidence.
- **One toggle.** It's a single Claude Code plugin. Disable it and every behavior stops; it never edits your
  settings, `CLAUDE.md`, shell profile or crontab.

## Install

Requires Claude Code 2.1.275+, Python 3.9+ and `git`. macOS or Linux.

```
/plugin install reverie --marketplace beladevo/reverie
```

or, from a shell:

```bash
claude plugin marketplace add beladevo/reverie
claude plugin install reverie@reverie
```

Project-scoped (everyone on a repo gets it): add to the repo's `.claude/settings.json`

```json
{ "enabledPlugins": { "reverie@reverie": true } }
```

## Enable / disable / uninstall

| | |
|---|---|
| Turn off | `/plugin disable reverie@reverie` — no hooks, no injection, no dreaming. Memory stays on disk. |
| Turn on | `/plugin enable reverie@reverie` — picks up where it left off. |
| Remove | `claude plugin uninstall reverie@reverie` — also deletes all memory data. Add `--keep-data` to keep it, or `/reverie:export` first. |

## Use

Nothing to do: memory is injected at session start and Claude writes to it when you correct it, state a
preference, or it learns something non-obvious. Commands:

| Command | |
|---|---|
| `/reverie:status` | attached stores, config, dream schedule, pending reviews |
| `/reverie:dream` | dream now (`--mode review` to only propose) |
| `/reverie:review` | walk through proposed changes and accept/reject them |
| `/reverie:history` | version log, view old versions, roll back a file or store |
| `/reverie:export` | tarball with full history, or one Markdown file |
| `/reverie:dashboard` | snapshot, plus the command for the live dashboard |

The same operations are available as a CLI: `reverie --help` (on Claude's Bash `PATH` while the plugin is
enabled, or `bin/reverie` in this repo).

## Dashboard

`reverie dashboard` opens a live terminal view (stdlib curses, refreshes every 2 seconds):

| Tab | Shows |
|---|---|
| 1 Overview | stores with file and version counts, last and next dream, waiting sessions, recent changes, hook errors |
| 2 Memory | every store as a file tree; open a file to read it, → for its version history with a diff per version |
| 3 Dreams | each dream's summary and changes with rationale, evidence, problems and diff; `a` / `x` accepts or rejects a pending change |
| 4 Sessions | sessions waiting to be dreamed over, and the condensed transcript the dreamer will read |

`n` starts a dream in the background from any tab. `reverie dashboard --once` prints the overview and exits.

## Stores

| Store | Kind | Agent access | For |
|---|---|---|---|
| `user.preferences` | notes | read/write | how you like to work, in every project |
| `project.notes` | notes | read/write | this project's mistakes, fixes, patterns, facts |
| `project.policy` | policy | read-only | curated rules; changed only by dreaming (+ review) |

Project stores are keyed by the git remote (or repo path), so all worktrees of a repo share them. Data lives
in `~/.claude/plugins/data/reverie-reverie/`:

```
stores/<store>/tree/          the Markdown files
stores/<store>/.rv/log.jsonl  append-only version log
stores/<store>/.rv/objects/   content-addressed versions
dreams/<id>/REPORT.md         what each dream changed and why, with diffs
sessions.jsonl                sessions waiting to be dreamed over
```

A team can also share a committed, read-only policy directory:
`{"attach": [..., {"store": "team.policy", "path": ".claude/reverie/policy"}]}`.

## Configure

`~/.claude/plugins/data/reverie-reverie/config.json` (all projects) and `<repo>/.claude/reverie.json`
(one project) override the defaults:

```json
{
  "attach": [
    {"store": "user.preferences", "mode": "read_write"},
    {"store": "project.policy", "mode": "read_only"},
    {"store": "project.notes", "mode": "read_write"}
  ],
  "redirect_builtin_memory": true,
  "inject": {"max_tokens": 2000, "full_policy_bytes": 1500},
  "limits": {"file_bytes": 4096, "files_per_store": 300, "max_depth": 3, "notes_ttl_days": 90},
  "dream": {
    "mode": "auto",
    "schedule": {"enabled": true, "interval_hours": 24, "min_sessions": 5},
    "model": "sonnet",
    "max_turns": 40,
    "max_sessions": 20,
    "promote_min_sessions": 3,
    "auto_promote": false,
    "timeout_minutes": 30
  }
}
```

- `dream.mode`: `auto` applies validated note changes immediately; `review` queues everything.
  Policy changes always wait for review unless `auto_promote` is `true`.
- Scheduled dreams start in the background at session start when due. They use your Claude Code login
  and quota. Set `dream.schedule.enabled: false` to only dream on demand.
- `redirect_builtin_memory`: while Reverie is enabled, writes to Claude Code's built-in auto-memory
  directory are redirected to Reverie stores, so there's one memory system. To silence the built-in one
  entirely, set `"autoMemoryEnabled": false` in your settings (a plugin cannot change that for you).

## Develop

```bash
python3 -m unittest discover -s tests     # stdlib only, no installs
claude --plugin-dir .                     # run Claude Code with your working copy
claude plugin validate .
```

Design notes: [docs/DESIGN.md](docs/DESIGN.md).

## License

MIT
