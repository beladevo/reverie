---
name: history
description: Inspect or roll back Reverie memory — version log, a file at an earlier version, or restore a file or a whole store.
argument-hint: "[store] [path]"
disable-model-invocation: true
---

Stores:

!`"${CLAUDE_PLUGIN_ROOT}/bin/reverie" stores`

Use these with Bash, based on what the user asked (`$ARGUMENTS`). Store names: `user.preferences`,
`project.notes`, `project.policy`, or a full id from the list above.

- History: `"${CLAUDE_PLUGIN_ROOT}/bin/reverie" log <store> [--path <path>]`
- A file at a version: `"${CLAUDE_PLUGIN_ROOT}/bin/reverie" show <store> <path>@<seq>`
- Roll back one file: `"${CLAUDE_PLUGIN_ROOT}/bin/reverie" rollback <store> --to-seq <seq> --path <path>`
- Roll back a whole store: `"${CLAUDE_PLUGIN_ROOT}/bin/reverie" rollback <store> --to-seq <seq>`

Rollback never rewrites history; it appends new versions. Before a rollback, show the user what will
change (use `show`) and confirm.
