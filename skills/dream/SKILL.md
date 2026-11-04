---
name: dream
description: Run Reverie dreaming now — curate memory from recent sessions (merge duplicates, record recurring mistakes, prune stale notes, propose promotions).
argument-hint: "[--mode auto|review]"
disable-model-invocation: true
---

Run this with the Bash tool (it calls a headless Claude and can take several minutes — use a 30-minute timeout):

```
"${CLAUDE_PLUGIN_ROOT}/bin/reverie" dream $ARGUMENTS
```

Then tell the user what happened: how many sessions were reviewed, what was applied, what is pending
review and what was rejected (with the reason). Point to the REPORT.md path it prints.
If ops are pending, offer `/reverie:review`.
