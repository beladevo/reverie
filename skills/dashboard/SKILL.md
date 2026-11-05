---
name: dashboard
description: Open the Reverie dashboard, a live terminal view of memory stores, files and their history, dreams and waiting sessions.
disable-model-invocation: true
---

!`"${CLAUDE_PLUGIN_ROOT}/bin/reverie" dashboard --once`

The interactive dashboard needs its own terminal window. Give the user this command to run there:

!`echo "python3 \"${CLAUDE_PLUGIN_ROOT}/bin/reverie\" dashboard"`

Say that the path changes when the plugin updates, so re-run `/reverie:dashboard` to get it again.
Then summarise the snapshot above in two or three lines: when the next dream is, and anything that needs
the user (changes waiting for review, hook errors).
