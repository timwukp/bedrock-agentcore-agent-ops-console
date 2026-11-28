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
repo = pins["source"].split()[0]
ref = sys.argv[1] if len(sys.argv) > 1 else "main"
bad = 0
for name, meta in pins["files"].items():
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
print(f"{len(pins['files']) - bad}/{len(pins['files'])} vendored files match {repo}@{ref}")
sys.exit(1 if bad else 0)
