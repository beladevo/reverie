# Reverie — Product Guide

**Version:** 0.1.0  **Repository:** https://github.com/beladevo/reverie  **License:** MIT

---

## 1. Summary

Reverie gives Claude Code a long-term memory that carries across sessions and gets better on its own.

Out of the box, each Claude Code session starts with no knowledge of earlier work, apart from what is in
`CLAUDE.md`. Corrections you have already given, conventions you have already explained, and mistakes
already diagnosed have to be repeated. Reverie closes that gap in three ways:

1. **Capture:** Claude records what it learns during a session: preferences, project conventions,
   recurring mistakes and their fixes.
2. **Recall:** that knowledge is loaded at the start of every later session.
3. **Curation:** a background "dreaming" process reviews recent sessions against stored memory. It
   consolidates duplicates, corrects stale entries, removes noise, and proposes proven lessons as rules.

All memory is kept as plain Markdown files with a full version history. Reverie is packaged as one Claude
Code plugin and can be turned on or off with a single command.

---

## 2. Capabilities

| Capability | What it provides |
|---|---|
| **Persistent memory** | Knowledge survives across sessions, worktrees and restarts. |
| **Automatic recall** | An index of relevant memory is loaded into every session at startup. Nothing to configure. |
| **Guided capture** | Claude is told when to save something (corrections, preferences, non-obvious fixes) and when not to (task progress, secrets, facts the code already shows). |
| **Full version history** | Every change is kept as a separate, unchangeable version. Nothing is overwritten or lost. |
| **Rollback** | Restore a single file, or a whole store, to any earlier version. |
| **Audit trail** | Every version records who made it: which session and tool, which dream, or which human edit. |
| **Safe concurrent use** | Several sessions can share memory. A session that hasn't seen another session's change is stopped and asked to re-read first. |
| **Trusted vs raw knowledge** | Curated *policy* is kept apart from raw *notes*. Claude can't write policy directly. |
| **Quality controls** | Every write is checked for format, size, file count and leaked credentials. |
| **Dreaming** | Scheduled or on-demand curation of memory, based on evidence from real sessions. |
| **Human review** | Policy changes, and optionally all changes, wait for your approval. |
| **Dashboard** | A live terminal view of stores, files and their history, dreams and waiting sessions, with review built in. |
| **Export** | A complete archive (with history) or a single readable Markdown document. |
| **Clean on/off switch** | Disabling the plugin stops all behaviour. It never edits your settings, `CLAUDE.md` or system scheduler. |

---

## 3. How it works

### 3.1 Memory stores

Memory is organised into **stores**. A store is a folder of small Markdown files, one fact per file.
Three stores are attached by default:

| Store | Scope | Claude's access | Contents |
|---|---|---|---|
| `user.preferences` | You, across every project | Read and write | How you like to work: style, tooling, communication |
| `project.notes` | The current project | Read and write | Observations: mistakes and fixes, patterns, project facts |
| `project.policy` | The current project | **Read only** | Curated, trusted rules for the project |

Project stores are tied to the repository (by its git remote), so every clone and worktree of the same
repository shares one memory.

Each memory file follows a standard format:

```markdown
---
name: vitest-module-mocks
description: vi.mock is hoisted; factories cannot use test-file variables
type: mistake          # preference | convention | pattern | mistake | fact | reference
confidence: high       # low | medium | high
evidence: [session ids that support this]
---

`vi.mock()` is hoisted above imports, so a factory that references a test-file constant throws.

**Why:** cost two debugging rounds in this repository.
**How to apply:** put values the factory needs in `vi.hoisted()`.
```

### 3.2 During a session

```
Session starts ─► Reverie loads the memory index and the usage rules into Claude's context
      │
Claude works ─► reads relevant memory files when needed
      │
Claude learns something ─► writes a memory file
      │                      ├─ checked first: allowed store? valid format? no secrets? up to date?
      │                      └─ saved as a new version, tagged with the session
Session ends ─► the session is queued for the next dream
```

- **Loading:** at startup Claude receives a short index of every memory file (its path and a one-line
  description) plus the full text of short policy files. This is capped at about 2,000 tokens. Claude
  opens individual files only when they are relevant.
- **Writing:** Claude uses its normal file tools. Reverie checks each write before it happens and saves
  a version after it happens. A rejected write comes back with the reason, so Claude can fix it.
- **One memory system:** while Reverie is enabled, writes aimed at Claude Code's built-in auto-memory
  are redirected to Reverie, so knowledge isn't split between two places.

### 3.3 Version history and concurrency

- Every saved version of a file is kept permanently in the store's history, with a log entry: when, what,
  which version came before, and who made it.
- Before Claude changes a file, it must have read the current version in the same session. If another
  session or a dream changed it since, the write is refused until Claude re-reads the file and merges.
- If you edit memory files directly in an editor, Reverie detects the change and records it as an
  external edit. A pending dream change to that file is then marked *stale* instead of overwriting your edit.
- Rollback never rewrites history. It adds a new version with the earlier content.

### 3.4 Dreaming

Dreaming is the curation step. It runs outside your working sessions and changes memory only through
validated, evidence-backed edits.

```
Collect     snapshot of each store + sessions not yet reviewed
   │
Condense    each session transcript reduced to requests, answers, actions and errors
   │
Analyse     a separate, restricted Claude run reviews memory against the sessions
   │
Propose     an edited copy of memory + a change list with a rationale and evidence for each change
   │
Validate    format, size, secrets, evidence requirements, conflicts
   │
Apply       note changes applied automatically; policy changes queued for your review
```

**What the dreamer looks for:**

- **Recurring mistakes:** the same failure or correction in two or more sessions.
- **Confirmed patterns:** approaches you accepted or asked for repeatedly.
- **Duplicates:** files that say the same thing are merged into one.
- **Stale information:** memory that newer sessions contradict is corrected or removed.
- **Low-value entries:** vague notes, task-progress notes, or facts the code already shows are removed.
- **Promotion:** a well-supported note is proposed as a policy rule.

**Safeguards:**

- Every change needs a written rationale. A change without one is rejected.
- A new or changed policy rule must be supported by at least **3 distinct sessions** (configurable).
- Policy changes always wait for human review unless you explicitly allow automatic promotion.
- When a note is promoted, the note stays in place until you decide on the promotion.
- The dreaming run can only read and write inside its own working folder. It cannot use the shell, the
  web, MCP servers or other plugins.
- Your live memory is never edited in place. Changes are applied as new versions, so the state before
  any dream can always be restored.
- A failed dream changes nothing and doesn't immediately retry.

**When dreams run:**

- **Automatically:** at the start of a session, once 24 hours have passed since the last dream *and* at
  least 5 sessions are waiting. The dream runs in the background and does not slow the session down.
- **On demand:** with `/reverie:dream`.

Each dream produces a report (`REPORT.md`) listing every change, its rationale, its evidence and a diff.

---

## 4. Getting started

### 4.1 Requirements

- Claude Code 2.1.275 or later
- Python 3.9 or later, and `git`
- macOS or Linux

### 4.2 Install

In Claude Code:

```
/plugin install reverie --marketplace beladevo/reverie
```

Or from a terminal:

```bash
claude plugin marketplace add beladevo/reverie
claude plugin install reverie@reverie
```

To enable Reverie for everyone working on a repository, add this to the repository's
`.claude/settings.json`:

```json
{ "enabledPlugins": { "reverie@reverie": true } }
```

### 4.3 Enable, disable, remove

| Action | Command | Effect |
|---|---|---|
| Disable | `/plugin disable reverie@reverie` | All behaviour stops. Memory stays on disk. |
| Enable | `/plugin enable reverie@reverie` | Resumes with existing memory. |
| Uninstall | `claude plugin uninstall reverie@reverie` | Removes the plugin **and its memory**. Add `--keep-data` to keep the memory, or export it first. |

---

## 5. Day-to-day use

No routine action is needed. Work as usual:

- **Correct Claude or state a preference.** It records it, and later sessions apply it.
- **Diagnose a non-obvious problem together.** The cause and the fix are recorded.
- **Start a new session.** Earlier knowledge is already loaded.

### 5.1 Commands

| Command | Purpose |
|---|---|
| `/reverie:status` | Attached stores, file counts, configuration, dream schedule, items awaiting review |
| `/reverie:dream` | Run a dream now. Add `--mode review` to only propose changes. |
| `/reverie:review` | Go through proposed changes and accept or reject each one |
| `/reverie:history` | View a store's history, read an earlier version, or roll back |
| `/reverie:export` | Export all memory, with or without history |
| `/reverie:dashboard` | Snapshot, plus the command that opens the live dashboard |

### 5.2 Command line

The same operations are available in a terminal through `bin/reverie` in the repository. While the
plugin is enabled, Claude can also run them as `reverie`.

```bash
reverie status                                    # overview
reverie log project.notes                         # history of a store
reverie show project.notes testing/mocks.md@12    # a file as of version 12
reverie rollback project.notes --to-seq 12        # restore a store to version 12
reverie dream --mode review                       # propose changes only
reverie dreams                                    # list dreams
reverie review <dream-id>                         # read a dream's report
reverie accept <dream-id> --ops 1,3               # apply selected changes
reverie reject <dream-id>                         # reject the remaining changes
reverie export ~/memory.tar.gz                    # full archive with history
reverie export ~/memory.md --format md            # one readable document
```

### 5.3 Dashboard

`reverie dashboard` opens a live, full-screen view in the terminal. It refreshes every two seconds and
needs its own terminal window; `/reverie:dashboard` gives you the exact command.

| Tab | What you see |
|---|---|
| 1 Overview | Each store with file and version counts, the last dream, when the next one runs and what it is waiting for, changes awaiting review, recent memory changes, hook errors |
| 2 Memory | Every store as a file tree. Select a file to read it; press → for its version history and the diff each version made |
| 3 Dreams | Each dream's summary and changes, with the rationale, evidence, problems and diff of each. `a` accepts and `x` rejects a pending change |
| 4 Sessions | Sessions waiting to be dreamed over, and the condensed transcript the dreamer will read |

Keys: `1`–`4` switch tabs, ↑↓ move, → opens, ← goes back, PgUp/PgDn scroll, `n` starts a dream in the
background, `q` quits. `reverie dashboard --once` prints the overview and exits.

### 5.4 Recommended routine

| When | Action |
|---|---|
| When Reverie mentions pending changes at session start | Run `/reverie:review` and decide on them |
| Weekly | Open the dashboard and skim the latest dream |
| Before uninstalling or switching machines | Run `/reverie:export` |
| If something unwanted was learned | Delete or edit the file, or roll back with `/reverie:history` |

---

## 6. Configuration

Settings are optional. Put them in:

- `~/.claude/plugins/data/reverie-reverie/config.json`: applies to all projects
- `<repository>/.claude/reverie.json`: applies to one project and overrides the file above

| Setting | Default | Meaning |
|---|---|---|
| `dream.mode` | `auto` | `auto` applies validated note changes; `review` queues every change |
| `dream.schedule.enabled` | `true` | Run dreams automatically in the background |
| `dream.schedule.interval_hours` | `24` | Minimum time between automatic dreams |
| `dream.schedule.min_sessions` | `5` | Minimum new sessions before an automatic dream |
| `dream.model` | `sonnet` | Model used for dreaming |
| `dream.promote_min_sessions` | `3` | Distinct sessions required to support a policy rule |
| `dream.auto_promote` | `false` | Apply policy changes without review (not recommended) |
| `inject.max_tokens` | `2000` | Maximum memory loaded at session start |
| `limits.file_bytes` | `4096` | Maximum size of one memory file |
| `limits.files_per_store` | `300` | Maximum files per store |
| `redirect_builtin_memory` | `true` | Redirect Claude Code's built-in memory writes to Reverie |

**Sharing team rules.** A team can commit a read-only policy folder to its repository and attach it:

```json
{
  "attach": [
    {"store": "user.preferences", "mode": "read_write"},
    {"store": "project.policy",   "mode": "read_only"},
    {"store": "project.notes",    "mode": "read_write"},
    {"store": "team.policy",      "path": ".claude/reverie/policy"}
  ]
}
```

---

## 7. Data, security and governance

**Where data lives.** Everything is stored on your machine under
`~/.claude/plugins/data/reverie-reverie/`:

| Path | Contents |
|---|---|
| `stores/<store>/tree/` | The current memory files (readable and editable) |
| `stores/<store>/.rv/log.jsonl` | Version log of the store, which only ever grows |
| `stores/<store>/.rv/objects/` | Every version of every file |
| `dreams/<id>/` | Each dream's inputs, proposal and report |
| `sessions.jsonl` | Sessions queued for dreaming |

Nothing is sent anywhere except the dreaming run. That run uses your existing Claude Code login and counts
against your usage.

**Controls in place:**

| Risk | Control |
|---|---|
| Credentials stored in memory | Writes that look like keys, tokens, passwords or credential URLs are refused |
| Untrusted content becoming permanent | Claude is told to write lessons in its own words and never copy tool output. Policy needs multi-session evidence and human approval. |
| Memory growing without limit | Size and count limits per file and store; a cap on what loads at session start; pruning during dreams |
| Wrong knowledge reinforced | Every change must cite evidence; contradiction checks; review mode available |
| Lost or overwritten knowledge | Every version is kept; rollback; external edits recorded before any overwrite |
| Plugin faults affecting your work | Internal errors are logged and never block Claude's actions |

**Known limitations (v0.1):**

- Read-only protection covers Claude's file tools. A change made to a policy file through shell commands is
  detected and recorded, but not blocked.
- Windows isn't supported yet.
- Claude Code's built-in memory is redirected, not switched off. To turn it off completely, set
  `"autoMemoryEnabled": false` in your Claude Code settings.

---

## 8. Troubleshooting

| Symptom | Resolution |
|---|---|
| Memory doesn't seem to load | Run `/reverie:status` to confirm stores are attached and the plugin is enabled |
| "Read … before changing it" | Expected. Claude must read the current version before editing it. |
| "changed since you last read it" | Another session or a dream updated the file. Claude should re-read it and merge. |
| A write was rejected | The message lists the reasons (format, size, secret, read-only). Fix them and retry. |
| Dreams never run | Check `sessions waiting` and `due` in `/reverie:status`; confirm `schedule.enabled` |
| A dream failed | Its folder under `dreams/` holds `dreamer.out` with the details |
| Hook errors | Listed in `errors.log` in the data directory, and shown by `/reverie:status` |
