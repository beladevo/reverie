from __future__ import annotations

import re
from pathlib import PurePosixPath

REQUIRED_FIELDS = ("name", "description", "type")
MAX_DESCRIPTION = 200
TYPES = ("preference", "convention", "pattern", "mistake", "fact", "reference")
SEGMENT = re.compile(r"^[a-z0-9][a-z0-9-]*$")

SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style key", re.compile(r"\bsk-(proj-)?[A-Za-z0-9]{32,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("Credential assignment", re.compile(
        r"(?i)[a-z0-9_.-]*(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token)[a-z0-9_.-]*"
        r"['\"]?\s*[:=]\s*['\"]?[^\s'\"<>{}]{8,}")),
    ("Credentials in URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    meta: dict = {}
    current_key = None
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        if line.startswith(("  ", "\t")) and current_key and line.strip().startswith("- "):
            meta.setdefault(current_key, [])
            if isinstance(meta[current_key], list):
                meta[current_key].append(line.strip()[2:].strip())
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        current_key = key
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        elif value == "":
            meta[key] = []
        else:
            meta[key] = value.strip("'\"")
    body = text[end + 4:]
    return meta, body.lstrip("\n")


def find_secrets(text: str) -> list[str]:
    return [label for label, pattern in SECRET_PATTERNS if pattern.search(text)]


def validate_path(rel_path: str, max_depth: int) -> list[str]:
    problems = []
    parts = PurePosixPath(rel_path).parts
    if not rel_path.endswith(".md"):
        problems.append("memory files must be .md")
    if len(parts) > max_depth:
        problems.append(f"path is deeper than {max_depth} levels")
    for part in parts:
        stem = part[:-3] if part.endswith(".md") else part
        if not SEGMENT.match(stem):
            problems.append(f"'{part}' is not kebab-case")
    return problems


def validate_memory(rel_path: str, text: str, limits: dict) -> list[str]:
    problems = validate_path(rel_path, limits.get("max_depth", 3))
    size = len(text.encode())
    if size > limits.get("file_bytes", 4096):
        problems.append(f"file is {size} bytes, limit is {limits.get('file_bytes', 4096)}; split it into focused files")
    meta, body = parse_frontmatter(text)
    if not meta:
        problems.append("missing YAML frontmatter (--- name/description/type ---)")
    else:
        for field in REQUIRED_FIELDS:
            if not meta.get(field):
                problems.append(f"frontmatter is missing '{field}'")
        description = meta.get("description")
        if isinstance(description, str) and len(description) > MAX_DESCRIPTION:
            problems.append(f"description is {len(description)} characters, limit is {MAX_DESCRIPTION}; it is injected into every session")
        if meta.get("type") and meta["type"] not in TYPES:
            problems.append(f"type must be one of {', '.join(TYPES)}")
    if not body.strip():
        problems.append("body is empty")
    for label in find_secrets(text):
        problems.append(f"looks like it contains a secret ({label}); never store credentials in memory")
    return problems
