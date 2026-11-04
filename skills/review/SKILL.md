---
name: review
description: Review memory changes proposed by Reverie dreaming and accept or reject them.
argument-hint: "[dream-id]"
disable-model-invocation: true
---

Recent dreams for this project:

!`"${CLAUDE_PLUGIN_ROOT}/bin/reverie" dreams`

1. Pick the dream to review: `$ARGUMENTS` if given, otherwise the newest one whose status is
   `proposed` or `partially_applied`. If none, say there is nothing to review and stop.
2. Run `"${CLAUDE_PLUGIN_ROOT}/bin/reverie" review <dream-id>` with Bash and show the user each **pending** op: what it changes, the
   rationale, the evidence, and the diff. Keep it short.
3. Ask the user which ops to accept. Then run, with Bash:
   - `"${CLAUDE_PLUGIN_ROOT}/bin/reverie" accept <dream-id> --ops 1,3` (omit `--ops` to accept all pending)
   - `"${CLAUDE_PLUGIN_ROOT}/bin/reverie" reject <dream-id> --ops 2`
4. Report the result. Ops marked `stale` were changed after the dream started — suggest `/reverie:dream` again.

Never accept ops the user did not approve.
