# Reverie — Design

Persistent, versioned memory for Claude Code, plus an out-of-band "dreaming" job that
turns raw session observations into clean, higher-signal knowledge.

Status: **implemented in v0.1.** §12 lists where the implementation differs from the original plan.

---

## 1. Principles

1. **Markdown on disk is the interface.** The agent reads and writes memory with its normal
   `Read` / `Write` / `Edit` tools. No custom memory API to learn.
2. **History is append-only.** Every write becomes an immutable, content-addressed version.
   Rollback is a new version that points at old content — nothing is ever rewritten.
3. **Trust is a property of the store, not the file.** `policy` stores are trusted and
   curated; `notes` stores are raw observations. Promotion between them is explicit.
4. **The plugin is the whole system.** Hooks, skills, commands, scripts and the dreamer all
   ship in the plugin. Disabling it removes every behavior. It never edits `settings.json`,
   `CLAUDE.md`, crontab or shell profiles.
5. **Zero runtime dependencies.** Python 3.9+ standard library only.

---

## 2. Architecture

```
                       ┌──────────────────── Claude Code session ────────────────────┐
                       │                                                              │
 SessionStart hook ───►│ inject: memory index + policy files + usage rules            │
                       │                                                              │
                       │ agent ── Read ──► store working tree (markdown)              │
                       │   │                     ▲                                    │
                       │   └─ Write/Edit ─► PreToolUse hook ─ deny if read_only,      │
                       │                     │                 stale (hash), invalid, │
                       │                     │                 secret, over budget    │
                       │                     ▼                                        │
                       │                PostToolUse hook ─► commit version            │
                       │                                     (objects/ + log.jsonl)   │
 SessionEnd hook ─────►│ record transcript path + touched stores in sessions.jsonl    │
                       └──────────────────────────────────────────────────────────────┘
                                                │
              /reverie:dream  or  "due" check at SessionStart (detached)
                                                ▼
 ┌──────────────────────────────── Dream job (out of band) ───────────────────────────┐
 │ collect ─► condense transcripts ─► analyze (headless `claude -p`, sandboxed) ─►     │
 │ proposal (new tree + changes.json + REPORT.md) ─► validate ─► apply | queue review │
 └─────────────────────────────────────────────────────────────────────────────────────┘
```

Components:

| Component | Kind | Responsibility |
|---|---|---|
| `scripts/reverie.py` | CLI + library | store ops, versioning, OCC, export, rollback, dream orchestration |
| `hooks/hooks.json` | hooks | wire lifecycle events to `reverie.py hook <event>` |
| `skills/memory/SKILL.md` | skill | when/what/how to write memory |
| `commands/*.md` | slash commands | `/reverie:status`, `dream`, `review`, `log`, `rollback`, `export`, `store` |
| `agents/dreamer.md` | agent | the dreaming prompt, reused by the headless run |

---

## 3. Data model

### 3.1 Locations

| What | Where | Why |
|---|---|---|
| Plugin code | `${CLAUDE_PLUGIN_ROOT}` | replaced on update, never holds data |
| All memory data | `${CLAUDE_PLUGIN_DATA}/` (`~/.claude/plugins/data/reverie-*/`) | survives updates and disable/enable; deleted on uninstall |
| Optional team policy | `<repo>/.claude/reverie/<store>/` | opt-in, committed, attached **read_only** |

Project stores are keyed by a project id: normalized git remote URL if present, else the
absolute repo root path, hashed to `p-<12 hex>`. This keeps worktrees of one repo on one store.

### 3.2 Store layout

```
${CLAUDE_PLUGIN_DATA}/
  config.json                       # user config (see §7)
  sessions.jsonl                    # one line per ended session: id, transcript, cwd, project, stores touched
  stores/
    <store-id>/                     # e.g. user.preferences, p-3fa9c1d2e4b0.policy
      store.json                    # name, kind (policy|notes), scope (user|project), created_at
      tree/                         # working tree = materialized HEAD. Agent reads/writes here.
        testing/vitest-mocks.md
        conventions/imports.md
      .rv/
        objects/ab/cdef…            # content-addressed blobs (sha256), immutable
        log.jsonl                   # append-only version log
        HEAD                        # seq number of last applied log entry
        lock                        # flock target for log appends (held for microseconds)
  dreams/
    <dream-id>/                     # 2026-11-04T0312Z-a1b2
      input.json                    # store snapshots (path→hash) + session ids consumed
      digest/                       # condensed transcripts fed to the dreamer
      proposal/<store-id>/tree/     # full proposed tree
      changes.json                  # structured ops with evidence
      REPORT.md                     # human-readable diff + rationale
      status                        # proposed | applied | partially_applied | rejected | failed
```

### 3.3 Memory file format

```markdown
---
name: vitest-module-mocks
description: vi.mock must be hoisted; mock factories cannot close over test vars
type: pattern            # preference | convention | pattern | mistake | fact | reference
confidence: high         # low | medium | high
evidence: [s-7f1c8f85, s-02ab91ee]
updated: 2026-11-04
---

`vi.mock()` calls are hoisted above imports. A factory that references a `const`
declared in the test file throws `Cannot access before initialization`.

**Why:** cost two sessions of debugging.
**How to apply:** use `vi.hoisted()` for values the factory needs.
```

Rules (enforced by the PreToolUse hook): frontmatter present with `name`, `description`,
`type`; body ≤ 4 KB; path ≤ 3 levels; kebab-case filenames; no secrets.

### 3.4 Version log entry

```json
{"seq": 42, "ts": "2026-11-04T10:21:07Z", "path": "testing/vitest-mocks.md",
 "op": "put", "hash": "sha256:9c1…", "parent": "sha256:4be…",
 "actor": {"kind": "agent", "session": "7f1c8f85-…", "tool": "Edit", "cwd": "/…/app"},
 "reason": null}
```

- `op`: `put` | `delete` | `rollback` (a `put` whose `hash` is an older blob, with `reason`).
- `actor.kind`: `agent` | `dream` (with `dream` id) | `human` (CLI or external edit) | `import`.
- A **snapshot** is just "the tree at `seq` N" — reconstructable by replaying the log, so no
  separate snapshot storage is needed. `reverie log`, `diff`, `show <path>@<seq>` read it.

### 3.5 Concurrency (optimistic, content-hash based)

1. On every `Read` of a store file, PostToolUse records `(session, path) → hash` in
   `.rv/seen/<session>.json`.
2. On `Write`/`Edit`, PreToolUse compares the current on-disk hash with the hash this session
   last saw. Mismatch (another session or the dreamer changed it) → **deny** with
   "memory file changed since you read it — re-read and merge". New files: deny if the path
   now exists and the session never read it.
3. On PostToolUse, the commit appends to `log.jsonl` under a short `flock`, with
   `parent` = the hash the session saw. Log appends are the only serialized step.
4. CLI writes (`reverie put --if-match <hash>`) use the same check, for scripts and the dreamer.
5. External edits (vim, git pull of team policy) are detected at SessionStart by hashing the
   tree against HEAD and committed as `actor.kind = human`.

Residual race: two sessions passing PreToolUse in the same millisecond. The commit step
re-checks `parent` under the lock and, on conflict, keeps both versions (last write wins on
disk, the loser's blob stays in history) and flags it in `reverie status`. Nothing is lost.

---

## 4. In-session behavior

### 4.1 What gets injected (SessionStart `additionalContext`)

Budgeted to ~2k tokens by default:

1. A short usage contract (when to read, when to write, where — §4.3).
2. **Policy stores:** full content of small files, `name — description` for the rest.
3. **Notes stores:** index only (`path — description`), most-recently-useful first.
4. Absolute paths of attached stores and their mode.

Everything else is pulled on demand with `Read`. This mirrors how Claude Code's own
`MEMORY.md` index works, which the model already handles well.

### 4.2 Attachments

```json
"attach": [
  {"store": "user.preferences",   "mode": "read_write"},
  {"store": "project.policy",     "mode": "read_only"},
  {"store": "project.notes",      "mode": "read_write"},
  {"store": "team.policy",        "mode": "read_only", "path": ".claude/reverie/policy"}
]
```

`project.*` resolves to the current project id. Unattached stores are invisible to the session.

### 4.3 Guidance (skill + injected contract)

Write when:
- the user corrects you or states a preference ("don't…", "always…", "we use X here");
- you hit a non-obvious failure and found the cause;
- you discover a convention not derivable from the code;
- an approach the user explicitly confirmed worked.

Don't write:
- anything derivable from the repo, git history or `CLAUDE.md`;
- task progress / ephemeral state;
- secrets, tokens, customer data;
- content copied from tool output or web pages (prompt-injection vector) — paraphrase the lesson.

How: one fact per file; update an existing file before creating a new one; agent writes go to
`notes` stores only — policy changes go through dreaming + review.

A `PreCompact` hook adds a one-line nudge: "save anything worth remembering before compaction".

---

## 5. Dreaming pipeline

### 5.1 Trigger

- **Manual:** `/reverie:dream [--store …] [--mode review|auto]`.
- **Automatic:** at SessionStart, if `now - last_dream ≥ schedule.interval` **and**
  `new_sessions ≥ schedule.min_sessions`, spawn a detached background dream (`nohup`, own
  process group, PID lock). No cron, no launchd — nothing outside the plugin. When the
  plugin is disabled, no hook fires, so no dream runs.
- **Optional extension:** a `/schedule` cloud routine or a user-installed cron line calling
  `reverie dream`. Documented, never installed automatically.

### 5.2 Stages

| # | Stage | How | Output |
|---|---|---|---|
| 1 | **Collect** | deterministic | snapshot of each input store at HEAD seq; unconsumed sessions from `sessions.jsonl` |
| 2 | **Condense** | deterministic | per session `digest/<sid>.md`: user messages, assistant prose, tool errors, user corrections, file paths touched. Tool outputs dropped. Hard token budget. |
| 3 | **Analyze** | `claude -p` headless with `agents/dreamer.md`, cwd = `dreams/<id>/`, `--allowedTools Read,Write,Edit,Glob,Grep` scoped to that dir, `REVERIE_DISABLE=1` so our hooks no-op, `--max-turns` + model from config | `proposal/` tree, `changes.json`, `REPORT.md` |
| 4 | **Validate** | deterministic | reject ops that: lack evidence, break file schema, contain secrets, exceed caps, or touch a store not in input. Policy promotions need evidence from ≥ `promote_min_sessions` distinct sessions. |
| 5 | **Apply / queue** | deterministic | `auto`: commit accepted ops as `actor.kind = dream`. `review`: leave as `proposed`; `/reverie:review` walks ops with diff, accept/reject each. Policy promotions are **always** review unless `auto_promote: true`. |

### 5.3 What the dreamer looks for

- **Recurring mistakes** — same failure or correction in ≥2 sessions → `mistake` memory.
- **Confirmed patterns** — approaches the user accepted without correction, repeatedly.
- **Duplicates** — files with overlapping meaning → merge, keep union of evidence.
- **Stale facts** — contradicted by newer sessions, or referencing paths/symbols that no
  longer exist → update or delete (with reason).
- **Low value** — never read, no evidence, older than `notes_ttl_days` → delete.
- **Promotion** — high-confidence, multi-session notes → proposed for `policy`.

### 5.4 `changes.json`

```json
{"dream": "2026-11-04T0312Z-a1b2", "ops": [
  {"id": 1, "store": "p-3fa9….notes", "op": "merge",
   "from": ["testing/mocks.md", "testing/vi-mock-hoist.md"], "to": "testing/vitest-mocks.md",
   "base": {"testing/mocks.md": "sha256:…", "testing/vi-mock-hoist.md": "sha256:…"},
   "evidence": ["s-7f1c8f85", "s-02ab91ee"], "rationale": "same lesson, two phrasings"},
  {"id": 2, "store": "p-3fa9….policy", "op": "promote", "from_store": "p-3fa9….notes",
   "path": "conventions/no-console.md", "evidence": ["s-…", "s-…", "s-…"], "rationale": "…"}
]}
```

`base` hashes make application optimistic too: if a file changed since the dream snapshot,
that op is skipped and marked `stale`, never force-applied. The input store is never modified
in place; applying produces new log entries, so "the store before this dream" is always
`reverie checkout --before-dream <id>` away.

---

## 6. Production concerns

| Concern | Mechanism |
|---|---|
| Audit trail | every log entry carries actor kind, session id, tool, cwd, dream id |
| Export | `reverie export [--store] [--format tar|md]` → tarball of trees + logs + objects, or one concatenated markdown file |
| Rollback | `reverie rollback <store> <path>@<seq>` or `--to-seq N` for a whole store; appends, never rewrites |
| Trusted vs raw | `policy` vs `notes` store kinds; agent cannot write `policy`; promotions need multi-session evidence + review |
| Pollution | schema + size caps on write; secret scanner; "no copied tool output" rule; per-store file cap; injection token budget; TTL + dream pruning |
| Unbounded growth | soft caps (warn in status) and hard caps (deny writes) per store; `reverie gc` prunes unreferenced blobs older than retention (opt-in, never default) |
| Recursion | dream run sets `REVERIE_DISABLE=1`; every hook exits 0 immediately when set |
| Hook safety | hooks never throw: any internal error → exit 0, log to `${CLAUDE_PLUGIN_DATA}/errors.log`. A broken plugin must not break a session. |
| Clean toggle | all state under `CLAUDE_PLUGIN_DATA`; no edits to user files; optional team policy is a file the user chooses to commit |

---

## 7. Configuration

`${CLAUDE_PLUGIN_DATA}/config.json`, overridable per project by `<repo>/.claude/reverie.json`
(project file is optional and user-created). Defaults:

```json
{
  "attach": [
    {"store": "user.preferences", "mode": "read_write"},
    {"store": "project.policy",   "mode": "read_only"},
    {"store": "project.notes",    "mode": "read_write"}
  ],
  "inject": {"max_tokens": 2000},
  "limits": {"file_bytes": 4096, "files_per_store": 300, "notes_ttl_days": 90},
  "dream": {
    "mode": "review",
    "schedule": {"enabled": true, "interval_hours": 24, "min_sessions": 5},
    "model": "sonnet",
    "max_turns": 40,
    "promote_min_sessions": 3,
    "auto_promote": false
  }
}
```

`/reverie:status` prints the effective config and where each value came from.

---

## 8. Plugin structure

```
reverie/                                  # GitHub repo = plugin + its own marketplace
├── .claude-plugin/
│   ├── plugin.json
│   └── marketplace.json                  # `/plugin install reverie --marketplace beladevo/reverie`
├── hooks/hooks.json                      # SessionStart, PreToolUse, PostToolUse, SessionEnd
├── skills/
│   ├── memory/SKILL.md                   # model-invoked: when/what/how to write memory
│   └── status|dream|review|history|export/SKILL.md   # user-invoked: /reverie:<name>
├── bin/reverie                           # CLI; on the Bash PATH while the plugin is enabled
├── lib/reverie/
│   ├── store.py       versioned content-addressed store, OCC, rollback
│   ├── config.py      defaults, config merge, project identity, data dir
│   ├── runtime.py     attachments, per-session seen-hashes
│   ├── validate.py    frontmatter, path rules, secret scanner
│   ├── inject.py      SessionStart context builder
│   ├── hooks.py       hook handlers (never fail the session)
│   ├── transcripts.py transcript condenser
│   ├── dream.py       dreaming pipeline, scheduling, review
│   ├── cli.py
│   └── prompts/dreamer.md
├── tests/                                # unittest, stdlib only; fake `claude` for dream tests
├── docs/DESIGN.md
└── .github/workflows/ci.yml
```

Hook wiring (sketch):

```json
{"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/reverie.py\" hook session-start"}]}],
  "PreToolUse":   [{"matcher": "Write|Edit|MultiEdit", "hooks": [{"type": "command", "command": "… hook pre-write"}]}],
  "PostToolUse":  [{"matcher": "Read|Write|Edit|MultiEdit", "hooks": [{"type": "command", "command": "… hook post-tool"}]}],
  "SessionEnd":   [{"hooks": [{"type": "command", "command": "… hook session-end"}]}]
}}
```

Hooks filter by path first: a tool call that doesn't touch a store path returns in < 10 ms.

Works user-scoped (`/plugin install reverie@reverie`) and project-scoped (listed in a repo's
`.claude/settings.json` `enabledPlugins`). Scope only changes *who* gets it; data location is
the same.

---

## 9. Tech stack

| Choice | Why |
|---|---|
| Python 3.9+, stdlib only | preinstalled on macOS/Linux dev machines; `hashlib`, `fcntl`, `json`, `tarfile` cover everything; no `pip install` at plugin install time |
| Own content-addressed store, not git | transparent (`ls`, `cat`), no index-lock contention between sessions, per-file history with session-level audit fields git commits don't model cleanly |
| `claude -p` for dreaming | reuses the user's auth and model access; no API key; dreamer runs in a sandboxed cwd with a tool allowlist |
| Detached process for scheduling | no OS-level residue; dies with nothing left behind when the plugin is disabled |
| `unittest` + GitHub Actions | zero test deps |

Windows: `fcntl` is POSIX-only. MVP targets macOS/Linux; Windows gets an `msvcrt` lock in a
follow-up.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Memory poisoning via prompt injection (web page → agent → memory → every future session) | agent writes only to `notes`; "paraphrase, don't copy" rule; policy needs multi-session evidence + human review; full audit to trace and roll back |
| Wrong memory confidently reinforced by dreaming | evidence required per op; dreamer must cite sessions; contradiction check against newer sessions; review mode default |
| Context bloat | injection budget; index-only for notes; dreaming dedupes and prunes |
| Secrets in memory | regex + entropy scanner on every write and every dream op; deny, don't redact |
| Hook latency / failures | fast path-filter; hooks never fail the tool call on internal error |
| Cost of background dreams | off unless `min_sessions` reached; model + `max_turns` capped; `schedule.enabled` toggle |
| Conflict with Claude Code's built-in auto memory | README explains choosing one; config flag to add "use Reverie stores, not the built-in memory dir" to the injected contract |
| Transcript format changes | condense stage parses defensively, skips unknown entries, has fixture tests per format version |
| Plugin env vars (`CLAUDE_PLUGIN_DATA`) missing on older Claude Code | fall back to `~/.claude/reverie-data/`; require a minimum version in README |

---

## 11. MVP scope vs extensions

**MVP (Phase 2):** stores + versioning + OCC + hooks + injection + skill + commands + manual
and threshold-scheduled dreaming with review/auto modes + export + rollback + tests + docs.

**Extension points:**
- Read-only MCP server exposing search across stores (for very large memories).
- Embedding-based dedupe in the dream validate stage.
- Usage telemetry per file (read counts) to rank injection and drive pruning.
- Team sync: push/pull a `policy` store to a git remote.
- Windows locking.
- Multiple dreamer "passes" (dedupe → consolidate → promote) with separate prompts.

---

## 12. Implementation notes (v0.1)

Where the shipped code differs from the plan above, and why:

- **Skills instead of `commands/`.** Claude Code now prefers skills for new plugins; user-facing operations
  are skills with `disable-model-invocation: true`, so they are still `/reverie:<name>`.
- **No dreamer subagent file.** A plugin `agents/` entry would be offered to every session as a subagent.
  The dreaming prompt lives in `lib/reverie/prompts/dreamer.md` and is used only by the headless run.
- **No PreCompact hook.** The injected contract and the `memory` skill cover "write before you forget";
  a compaction nudge can be added later.
- **Dreamer output.** The dreamer edits a full copy of each store under `proposal/` and writes
  `changes.json` (rationale + evidence per file). Operations are derived by diffing the proposal against
  the dream's base snapshot, so the dreamer cannot claim a change it didn't make or make one it didn't
  explain: a changed file without a `changes.json` entry is rejected.
- **Default dream mode is `auto`** for notes; policy changes still wait for review (`auto_promote: false`).
- **Built-in auto memory.** A plugin can't set `autoMemoryEnabled` (plugin `settings` only honours
  `agent` and `subagentStatusLine`). Instead, while enabled, PreToolUse redirects writes aimed at
  `~/.claude/projects/*/memory/` to the Reverie stores (`redirect_builtin_memory`). Verified live: before
  this, sessions saved preferences to the built-in memory and Reverie never saw them.
- **Data dir discovery.** Hooks get `CLAUDE_PLUGIN_DATA`; Bash-tool runs of `bin/reverie` don't, so the
  CLI finds `~/.claude/plugins/data/reverie-*` by name. `REVERIE_DATA` overrides both.
- **Child run isolation.** The dreamer runs `claude -p` in the dream directory with
  `--permission-mode acceptEdits`, an allowlist of file tools, Bash/Web/Agent disallowed, and
  `REVERIE_DISABLE=1` + `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` so it neither triggers Reverie hooks nor writes
  built-in memory. `--bare` was not used because it requires an API key instead of the user's login.
- **Dreamer sandbox** (verified live): `--permission-mode dontAsk` with only `Read(./**)` and `Edit(./**)`
  allowed — `Read` rules also scope Glob/Grep and `Edit` rules cover Write — plus
  `--setting-sources project` and `--strict-mcp-config` with no servers, so other plugins' hooks and MCP
  servers don't load. Reading or writing outside the dream directory is denied.
- **Policy evidence gate** counts distinct known sessions; a citation must name a session exactly or by an
  unambiguous prefix of at least 8 characters.
- **External edits are never lost.** Before any write through the store, an on-disk version that the log
  doesn't know is versioned first (`actor.kind = external`) and an `if_match` against the old version fails.
- **Failed dreams back off.** Every attempt records `last_attempt_at`; timeouts and a missing `claude` binary
  are recorded as failed dreams instead of crashing.
- **Sessions consumed** are tracked per project in `dreams/state.json`; a failed dream consumes nothing. When more than
  `max_sessions` are waiting, only the newest are digested and the older ones are marked consumed.

### Known gaps

- Read-only enforcement covers Claude's file tools, not shell commands. An agent that edits a policy file
  with `sed` through Bash is caught at the next session start and versioned as `external`, not blocked.
- No Windows support (POSIX `fcntl` locks); on Windows the hooks exit silently.
