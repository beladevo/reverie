# Reverie

Persistent, versioned memory for Claude Code — and a background "dreaming" job that
cleans it up.

- Memory is plain Markdown files the agent reads and writes with its normal tools.
- Every write is an immutable, content-addressed version with a full audit trail.
- Trusted `policy` stores are kept apart from raw `notes`.
- Dreaming reads recent sessions + memory and proposes a cleaner version: merges
  duplicates, records recurring mistakes, drops stale facts, promotes proven patterns.
  You review it, or let it apply automatically.
- Shipped as a single Claude Code plugin. `/plugin disable reverie` turns everything off.

**Status:** design phase. See [docs/DESIGN.md](docs/DESIGN.md).

## License

MIT
