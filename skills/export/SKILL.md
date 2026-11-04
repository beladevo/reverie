---
name: export
description: Export Reverie memory — all stores with full history as a tarball, or the current memory as one Markdown file.
argument-hint: "<out-file> [--format tar|md] [--store <store>]"
disable-model-invocation: true
---

Run with Bash: `"${CLAUDE_PLUGIN_ROOT}/bin/reverie" export $ARGUMENTS`

If no output path was given, use `./reverie-export-<today>.tar.gz`. Report the path it prints.
