#!/usr/bin/env python3
"""Task 7: scan deploy/ dirs for weixin_oc token-prefix leaks.
Token is fetched from the VPS over ssh into the env var SCAN_TOK by the
caller; this script never prints it (only reports file paths)."""
import os, sys

tok = os.environ.get("SCAN_TOK", "")
if len(tok) < 8:
    sys.exit("SCAN_TOK missing/short — aborting (refusing to scan blind)")
prefix = tok[:8]
hits = []
base = sys.argv[1] if len(sys.argv) > 1 else "deploy"
for root, dirs, files in os.walk(base):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        p = os.path.join(root, f)
        try:
            data = open(p, "rb").read()
        except Exception:
            continue
        if prefix.encode() in data:
            hits.append(p)
print(f"scan base={base} pattern=<token-prefix, {len(prefix)} chars> leaks={len(hits)}")
for h in hits:
    print("LEAK:", h)
