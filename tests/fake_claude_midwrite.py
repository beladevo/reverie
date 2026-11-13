#!/usr/bin/env python3
"""A dreamer that changes nothing while a live session writes a new memory in the meantime."""
import json
import os
import subprocess
import sys
from pathlib import Path

note = "---\nname: fresh\ndescription: d\ntype: fact\nconfidence: high\n---\n\nwritten mid-dream\n"
subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / "bin" / "reverie"),
                "--cwd", os.environ["TEST_PROJECT"], "put", "project.notes", "fresh.md"],
               input=note, text=True, check=True, env={**os.environ, "REVERIE_DISABLE": ""})
Path("changes.json").write_text(json.dumps({"changes": []}))
print(json.dumps({"type": "result", "result": "nothing to change"}))
