#!/usr/bin/env python3
"""Extract and syntax-check the web UI script using Node.js."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = root / "app/src/main/python/otakudesu_gothic.py"
text = source.read_text(encoding="utf-8")
match = re.search(r"PAGE\s*=\s*r'''[\s\S]*?<script>([\s\S]*?)</script>", text)
if not match:
    raise SystemExit("Inline application script not found")
node = shutil.which("node")
if not node:
    raise SystemExit("Node.js is required for the inline JavaScript syntax check")
with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as file:
    file.write(match.group(1))
    script_path = file.name
try:
    subprocess.run([node, "--check", script_path], check=True)
finally:
    Path(script_path).unlink(missing_ok=True)
print("Inline JavaScript syntax: OK")
