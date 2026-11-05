#!/usr/bin/env python3
"""V06b agy quota probe + governor ladder.

Two roles, one module (so bridge/tests share the ladder):
  1. CLI probe: run `agy --print "/usage" --output-format json` STANDALONE
     (never inside a stream-json stream — official headless docs forbid it),
     guard the JSON shape, and atomically write /root/agy-bridge/quota_state.json.
     On any rejection the previous state file is kept untouched and exit != 0
     with the reason on stderr.
  2. Pure ladder logic: decide(state, cfg, prev_tier, now) -> verdict dict.
     No I/O, no module-level mutable state — importable by bridge.py and tests.

Probe command (verified 2026-11-05, see GOVERNOR_V06_DESIGN.md D.5):
    cd /root && timeout 60 env HOME=/root TERM=dumb LANG=C.UTF-8 \
        PATH=/root/.local/bin:/usr/bin:/bin \
        agy --print "/usage" --output-format json --dangerously-skip-permissions
NEVER pass --disable-slash-commands: it kills slash expansion and /usage is
then consumed as a plain prompt (burns quota).
"""
import fcntl
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

DEFAULT_CONFIG = "/root/agy-bridge/governor.json"

PROBE_ARGV = [
    "agy", "--print", "/usage", "--output-format", "json",
    "--dangerously-skip-permissions",
]
PROBE_CWD = "/root"
PROBE_ENV = {
    "HOME": "/root",
    "TERM": "dumb",
    "LANG": "C.UTF-8",
    "PATH": "/root/.local/bin:/usr/bin:/bin",
}
PROBE_TIMEOUT_S = 60

TIERS = ("normal", "effort_low", "reflection_pause", "hard_gate")


class ProbeReject(Exception):
    """Probe output failed a shape/token guard — keep previous state."""


def iso_to_epoch(s):
    if not isinstance(s, str):
        raise ProbeReject(f"reset_time not a string: {s!r}")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise ProbeReject(f"unparsable reset_time: {s!r}")


def parse_probe_json(obj):
    """Validate one probe payload and reduce it to a groups mapping.

    Guards (design E, version coupling): accept ONLY command.name == "usage"
    with usage.total_tokens == 0 (otherwise the CLI burned the slash command
    as a real prompt). Skip buckets with disabled:true. Returns
    (groups, skipped) where groups = {gemini|3p: {5h|weekly: bucket}}.
    """
    if not isinstance(obj, dict):
        raise ProbeReject("probe output is not a JSON object")
    if obj.get("status") != "SUCCESS":
        raise ProbeReject(f"status={obj.get('status')!r}")
    cmd = obj.get("command") or {}
    if cmd.get("name") != "usage":
        raise ProbeReject(
            f"command.name={cmd.get('name')!r} — not a /usage result")
    usage = obj.get("usage") or {}
    if usage.get("total_tokens", 0) != 0:
        raise ProbeReject(
            f"usage.total_tokens={usage.get('total_tokens')} != 0 — "
            "/usage was consumed as a model prompt; refusing to trust payload")
    groups = {}
    skipped = []
    for g in (cmd.get("data") or {}).get("groups") or []:
        for b in g.get("buckets") or []:
            bid = b.get("id") or ""
            if b.get("disabled"):
                skipped.append({"id": bid, "reason": "disabled"})
                continue
            if bid.startswith("gemini-"):
                grp = "gemini"
            elif bid.startswith("3p-"):
                grp = "3p"
            else:
                skipped.append({"id": bid, "reason": "unknown-group"})
                continue
            window = b.get("window")
            if window not in ("5h", "weekly"):
                skipped.append({"id": bid, "reason": f"window={window!r}"})
                continue
            frac = b.get("remaining_fraction")
            if not isinstance(frac, (int, float)):
                raise ProbeReject(f"bucket {bid}: remaining_fraction={frac!r}")
            reset_epoch = iso_to_epoch(b.get("reset_time"))
            groups.setdefault(grp, {})[window] = {
                "frac": float(frac),
                "reset_epoch": reset_epoch,
                "reset_iso": b.get("reset_time"),
            }
    if "gemini" not in groups or "5h" not in groups["gemini"]:
        raise ProbeReject("no usable gemini 5h bucket in payload")
    return groups, skipped


def build_state(groups, skipped, now=None):
    now = time.time() if now is None else now
    return {
        "fetched_at": now,
        "fetched_at_iso": datetime.fromtimestamp(
            now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ok": True,
        "source": "agy --print /usage --output-format json",
        "groups": groups,
        "skipped_buckets": skipped,
    }


def run_probe(config=None):
    """Execute the probe command and return the parsed payload dict."""
    cfg = config or {}
    argv = cfg.get("probe_argv") or PROBE_ARGV
    env = dict(os.environ)
    env.update(cfg.get("probe_env") or PROBE_ENV)
    try:
        p = subprocess.run(argv, cwd=cfg.get("probe_cwd", PROBE_CWD),
                           env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True,
                           timeout=cfg.get("probe_timeout_s", PROBE_TIMEOUT_S))
    except subprocess.TimeoutExpired:
        raise ProbeReject(f"probe timed out after {PROBE_TIMEOUT_S}s")
    except OSError as e:
        raise ProbeReject(f"probe exec failed: {e}")
    if p.returncode != 0:
        tail = (p.stderr or "").strip()[-300:]
        raise ProbeReject(f"agy exit={p.returncode} stderr_tail={tail!r}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise ProbeReject(f"probe stdout not JSON: {e}; head={p.stdout[:200]!r}")


# ---------------------------------------------------------------------------
# Pure ladder logic (owner-approved V06b thresholds live in governor.json)
# ---------------------------------------------------------------------------

def is_stale(state, cfg, now=None):
    """True when quota_state is missing/unparsable/fetched_at too old."""
    now = time.time() if now is None else now
    if not isinstance(state, dict):
        return True
    fetched = state.get("fetched_at")
    if not isinstance(fetched, (int, float)):
        return True
    return (now - fetched) > float(cfg.get("stale_s", 600))


def _bucket(state, group, window):
    try:
        return state["groups"][group][window]
    except (KeyError, TypeError):
        return None


def decide(state, cfg, prev_tier, now=None):
    """Pure ladder decision. Returns a verdict dict:

    tier          one of TIERS (hysteresis: exit thresholds = entry + margin;
                  reflection restore uses reflection_restore_above)
    stale         True if quota_state is stale/unknown (never hard-gates)
    frac_5h / frac_weekly   floats or None
    reset_5h / reset_weekly epoch floats or None
    retry_after_s int or None (only tier == hard_gate on fresh data)
    inject_effort "low" or None
    reflection    "pause" | "resume"
    """
    now = time.time() if now is None else now
    ladder = cfg.get("ladder", {})
    margin = float(ladder.get("hysteresis_margin", 0.05))
    t_low = float(ladder.get("effort_low_below", 0.40))
    t_pause = float(ladder.get("reflection_pause_below", 0.25))
    t_restore = float(ladder.get("reflection_restore_above", t_pause + margin))
    t_hard5 = float(ladder.get("hard_floor_5h_below", 0.10))
    t_hardw = float(ladder.get("hard_floor_weekly_below", 0.15))
    low_exit = t_low + margin
    hard_exit5 = t_hard5 + margin
    hard_exitw = t_hardw + margin
    effort = cfg.get("effort_inject", "low")

    b5 = _bucket(state, "gemini", "5h")
    bw = _bucket(state, "gemini", "weekly")
    stale = is_stale(state, cfg, now) or b5 is None
    f5 = b5["frac"] if b5 else None
    fw = bw["frac"] if bw else None

    if stale:
        # conservative tier: inject low effort, NEVER 429 on stale data.
        # reflection holds its previous state (unknown quota must not
        # silently resume a pause entered on fresh data).
        tier = "effort_low"
        hard = False
        retry = None
        prev_s = prev_tier if prev_tier in TIERS else "normal"
        reflection = ("hold" if prev_s in ("reflection_pause", "hard_gate")
                      else "resume")
    else:
        assert b5 is not None  # missing gemini-5h implies stale branch above
        f5 = float(b5["frac"])
        in_hard = (f5 < t_hard5) or (fw is not None and fw < t_hardw)
        exit_hard = (f5 >= hard_exit5) and (fw is None or fw >= hard_exitw)
        prev = prev_tier if prev_tier in TIERS else "normal"
        if in_hard or (prev == "hard_gate" and not exit_hard):
            tier = "hard_gate"
        elif f5 < t_pause or (prev in ("reflection_pause", "hard_gate")
                              and f5 < t_restore):
            tier = "reflection_pause"
        elif f5 < t_low or (prev in ("effort_low", "reflection_pause",
                                     "hard_gate") and f5 < low_exit):
            tier = "effort_low"
        else:
            tier = "normal"
        hard = tier == "hard_gate"
        retry = None
        reflection = ("pause" if tier in ("reflection_pause", "hard_gate")
                      else "resume")
        if hard:
            # governing window: whichever floor was breached (5h wins ties;
            # hysteresis-hold uses the 5h window — chat is 5h-bound)
            if f5 < t_hard5 or bw is None:
                reset = b5["reset_epoch"]
            else:
                reset = bw["reset_epoch"]
            retry = max(int(cfg.get("retry_after_floor_s", 30)),
                        math.ceil(reset - now))

    return {
        "tier": tier,
        "stale": stale,
        "frac_5h": f5,
        "frac_weekly": fw,
        "reset_5h": b5["reset_epoch"] if b5 else None,
        "reset_weekly": bw["reset_epoch"] if bw else None,
        "retry_after_s": retry,
        "inject_effort": None if tier == "normal" else effort,
        "reflection": ("pause" if tier in ("reflection_pause", "hard_gate")
                       else "resume") if not stale else reflection,
    }


# ---------------------------------------------------------------------------
# Shared file helpers (atomic write + locked governor_state.json merge)
# ---------------------------------------------------------------------------

def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def update_governor_state(path, mutate):
    """Locked read-modify-write of governor_state.json. `mutate(dict)->dict`
    is applied to the current contents; result is written back atomically.
    Safe for concurrent writers (bridge notifier vs reflection-gate timer)."""
    lock_path = path + ".lock"
    with open(lock_path, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            cur = load_json(path)
            if not isinstance(cur, dict):
                cur = {}
            new = mutate(dict(cur))
            atomic_write_json(path, new)
            return new
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg_path = DEFAULT_CONFIG
    if "--config" in argv:
        cfg_path = argv[argv.index("--config") + 1]
    cfg = load_json(cfg_path) or {}
    out = cfg.get("state_file", "/root/agy-bridge/quota_state.json")
    try:
        payload = run_probe(cfg)
        groups, skipped = parse_probe_json(payload)
        state = build_state(groups, skipped)
    except ProbeReject as e:
        # keep-previous-on-failure: state file is NOT touched
        sys.stderr.write(f"quota_probe: REJECTED — {e}\n")
        return 1
    except Exception as e:  # anything else also keeps previous state
        sys.stderr.write(f"quota_probe: FAILED — {type(e).__name__}: {e}\n")
        return 2
    atomic_write_json(out, state)
    g = state["groups"]["gemini"]["5h"]
    print(f"quota_probe: ok gemini-5h={g['frac']:.3f} "
          f"weekly={(state['groups']['gemini'].get('weekly') or {}).get('frac')}"
          f" reset={g['reset_iso']} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
