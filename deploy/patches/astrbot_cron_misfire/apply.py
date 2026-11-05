#!/usr/bin/env python3
"""Idempotent patcher: AstrBot cron misfire_grace_time 30 -> 300.

Community report (older AstrBot) said CronJobManager scheduled APScheduler
jobs with misfire_grace_time=30, so cron jobs missed during busy/agent-slow
window were skipped entirely. Verified on this VPS: AstrBot 4.29.4 already
ships misfire_grace_time=300 upstream (dist-info RECORD hash matches disk),
so on 4.29.4 this patcher is a verified no-op. It stays as the artifact to
re-assert 300 if a future install/regression reintroduces 30.

Usage (with the AstrBot tool-env python):
    /root/.local/share/uv/tools/astrbot/bin/python apply.py [--dry-run] [--force]

Behavior:
  - locates astrbot/core/cron/manager.py via importlib (module containing
    CronJobManager + misfire_grace_time);
  - refuses to patch if installed astrbot version != TARGET_VERSION unless --force;
  - rewrites misfire_grace_time=30 -> misfire_grace_time=300,  # dafeiyu-patch: misfire-grace 300
  - if value already 300: no-op (marker not forced, keeps file hash == upstream);
  - prints a unified diff, writes, then re-reads and verifies; exit non-zero on mismatch.
"""
import argparse
import difflib
import importlib.util
import re
import sys
from pathlib import Path

TARGET_VERSION = "4.29.4"
MARKER = "# dafeiyu-patch: misfire-grace 300"
PATTERN = re.compile(r"(misfire_grace_time=)(\d+)(,?)")


def locate_manager() -> Path:
    spec = importlib.util.find_spec("astrbot.core.cron.manager")
    if spec is None or not spec.origin:
        # fallback: search site-packages for CronJobManager + misfire_grace_time
        import site

        for sp in site.getsitepackages() + [site.getusersitepackages()]:
            cand = Path(sp) / "astrbot" / "core" / "cron" / "manager.py"
            if cand.is_file() and "CronJobManager" in cand.read_text(encoding="utf-8"):
                return cand
        sys.exit("FATAL: could not locate astrbot cron manager module")
    path = Path(spec.origin)
    text = path.read_text(encoding="utf-8")
    if "CronJobManager" not in text or "misfire_grace_time" not in text:
        sys.exit(f"FATAL: {path} does not look like the cron manager module")
    return path


def installed_version() -> str:
    try:
        import astrbot

        return getattr(astrbot, "__version__", "unknown")
    except Exception:
        pass
    try:
        from importlib.metadata import version

        return version("astrbot")
    except Exception:
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="patch regardless of version")
    args = ap.parse_args()

    ver = installed_version()
    print(f"installed astrbot version: {ver}")
    if ver != TARGET_VERSION and not args.force:
        sys.exit(f"REFUSING to patch: expected {TARGET_VERSION}, found {ver} (use --force)")

    path = locate_manager()
    print(f"target file: {path}")
    original = path.read_text(encoding="utf-8")

    matches = list(PATTERN.finditer(original))
    if not matches:
        sys.exit("FATAL: no misfire_grace_time assignment found")
    values = {m.group(2) for m in matches}
    print(f"found misfire_grace_time value(s): {sorted(values)}")

    if MARKER in original:
        print("marker present: patch already applied by this tool — no-op")
        return
    if values == {"300"}:
        print("already 300 (upstream default on this version) — no change needed")
        return
    if values != {"30"}:
        sys.exit(f"FATAL: unexpected value(s) {sorted(values)}; refusing to guess — inspect manually")

    patched = PATTERN.sub(lambda m: f"{m.group(1)}300{m.group(3)}  {MARKER}", original)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path) + " (patched)",
        )
    )
    print(diff)
    if args.dry_run:
        print("dry-run: nothing written")
        return

    backup = path.with_suffix(path.suffix + ".bak-dafeiyu")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
        print(f"backup: {backup}")
    path.write_text(patched, encoding="utf-8")

    verify = path.read_text(encoding="utf-8")
    if MARKER not in verify or "misfire_grace_time=300" not in verify:
        sys.exit("FATAL: post-write verification failed")
    if "misfire_grace_time=30," in verify.replace("misfire_grace_time=300", ""):
        sys.exit("FATAL: stale value=30 remains")
    print("OK: patched and verified on disk (value 300 + marker present)")


if __name__ == "__main__":
    main()
