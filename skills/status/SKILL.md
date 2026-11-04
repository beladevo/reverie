---
name: status
description: Show Reverie memory status — attached stores, effective config, dreaming schedule and pending reviews.
disable-model-invocation: true
---

!`"${CLAUDE_PLUGIN_ROOT}/bin/reverie" status`

Summarise the status above for the user in a few lines: which stores are attached and their size,
whether a dream is due, and whether anything awaits review (if so, suggest `/reverie:review`).
If hook errors are logged, show the last few lines of that file.
