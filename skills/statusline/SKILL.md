---
name: statusline
description: Add Reverie's segment to the user's Claude Code status line — dreaming, memories saved this session, dreamed changes awaiting review.
disable-model-invocation: true
---

Reverie's status line script (stable across plugin updates):

!`"${CLAUDE_PLUGIN_ROOT}/bin/reverie" statusline --shim`

It reads the status line JSON on stdin and prints one short segment, or nothing when there is nothing to show:
`💭 dreaming… · 2 saved · 1 to review`.

1. Read the `statusLine` entry in `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`).
2. If there is none, propose setting it to `{"type": "command", "command": "[ -x <script path above> ] && <script path above>"}`.
   The guard keeps the status line quiet if Reverie is uninstalled, which deletes the script.
3. If there is one, read the script it runs and propose the smallest change that appends Reverie's output to it,
   feeding it the same stdin JSON — for a shell script, capture stdin once (`input=$(cat)`) and run
   `[ -x <script path above> ] && printf '%s' "$input" | <script path above>`. Keep the user's separators and layout.
4. Show the exact change and apply it only after the user approves. Never rewrite the rest of their status line.
