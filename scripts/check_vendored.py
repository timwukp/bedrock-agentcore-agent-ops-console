"""Pre-merge drift check for the vendored lease files (src/VENDORED.json). Needs network, so it is a script, not a
unit test. Compares each pinned file with the upstream repo at a ref and exits 1 on any difference.

Usage: python3 scripts/check_vendored.py [<ref>]   (default ref: main)
Set GITHUB_TOKEN to lift the anonymous rate limit."""
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pins = json.loads((ROOT / "src/VENDORED.json").read_text())
ref = sys.argv[1] if len(sys.argv) > 1 else "main"
bad = 0
# The lease files (top level) and the tool guard's files (the "guard" block) come from different repos.
blocks = [pins] + [b for b in pins.values() if isinstance(b, dict) and "source" in b and "files" in b]
entries = [(b["source"].split()[0], name, meta) for b in blocks for name, meta in b["files"].items()]
for repo, name, meta in entries:
    url = f"https://api.github.com/repos/{repo}/contents/{meta['upstreamPath']}?ref={ref}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.raw", "User-Agent": "check-vendored"})
    if os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    try:
        upstream = urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001 — a check that cannot run must not report clean
        print(f"ERROR {name}: cannot read upstream at {ref}: {e}")
        bad += 1
        continue
    local = (ROOT / "src" / name).read_bytes()
    up, lo, pin = hashlib.sha256(upstream).hexdigest(), hashlib.sha256(local).hexdigest(), meta["sha256"]
    state = "ok" if up == lo == pin else f"DRIFT upstream={up[:12]} local={lo[:12]} pin={pin[:12]}"
    print(f"{name}: {state}")
    bad += state != "ok"
print(f"{len(entries) - bad}/{len(entries)} vendored files match their upstream repos @{ref}")
sys.exit(1 if bad else 0)
