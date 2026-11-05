#!/usr/bin/env python3
"""V06b reflection gate: pause/resume LivingMemory reflection by quota tier.

Runs from its own systemd timer (120s) on the VPS. Reads quota_state.json +
governor.json, decides via the shared ladder (quota_probe.decide), and — only
on an actual tier transition (hysteresis lives in governor_state.json) —
writes summary_trigger_rounds (pause=9999 / resume=10) into the LivingMemory
plugin config AND restarts AstrBot in the same invocation.

Discipline (V04 lesson): AstrBot rewrites its plugin configs from memory at
save events, so the config write and `kill -9 $(MainPID)` MUST happen in one
run; a later drift check re-asserts if the value disagrees with intent.
Config file is utf-8-sig (BOM preserved). Journal-clean check logged after
each restart.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quota_probe as qp  # noqa: E402  (shared ladder, same directory)

DEFAULT_CONFIG = "/root/agy-bridge/governor.json"


def lm_get(cfg_path):
    """Return (current_summary_trigger_rounds, had_bom) or (None, False)."""
    try:
        with open(cfg_path, "rb") as f:
            raw = f.read()
    except OSError:
        return None, False
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    d = json.loads(raw.decode("utf-8-sig"))
    val = d.get("reflection_engine", {}).get("summary_trigger_rounds")
    return val, had_bom


def lm_set(cfg_path, value, had_bom):
    """Atomic rewrite of reflection_engine.summary_trigger_rounds."""
    with open(cfg_path, "rb") as f:
        raw = f.read()
    d = json.loads(raw.decode("utf-8-sig"))
    d.setdefault("reflection_engine", {})["summary_trigger_rounds"] = value
    enc = "utf-8-sig" if had_bom else "utf-8"
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding=enc) as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg_path)


def astrbot_mainpid(service):
    p = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value",
                        service], stdout=subprocess.PIPE, text=True,
                       timeout=10)
    pid = p.stdout.strip()
    return int(pid) if pid.isdigit() else 0


def journal_clean(service, lines=40):
    """Return Traceback count in recent journal (best effort)."""
    try:
        p = subprocess.run(
            ["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=15)
        return sum(1 for l in p.stdout.splitlines() if "Traceback" in l)
    except Exception:
        return -1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg_path = DEFAULT_CONFIG
    if "--config" in argv:
        cfg_path = argv[argv.index("--config") + 1]
    cfg = qp.load_json(cfg_path) or {}
    state = qp.load_json(cfg.get("state_file",
                                 "/root/agy-bridge/quota_state.json"))
    gstate_path = cfg.get("governor_state_file",
                          "/root/agy-bridge/governor_state.json")
    prev_tier = (qp.load_json(gstate_path) or {}).get("tier")
    verdict = qp.decide(state, cfg, prev_tier)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def stamp(cur):
        cur["tier"] = verdict["tier"]
        cur["tier_updated_at"] = ts
        cur["last_verdict"] = verdict
        cur["last_gate_run"] = ts
        return cur
    qp.update_governor_state(gstate_path, stamp)

    want_pause = verdict["reflection"] == "pause"
    want_hold = verdict["reflection"] == "hold"
    lm_cfg_path = cfg["livingmemory_config"]
    service = cfg.get("astrbot_service", "dafeiyu-astrbot")
    pause_val = int(cfg.get("reflection_pause_value", 9999))
    resume_val = int(cfg.get("reflection_resume_value", 10))
    target = pause_val if want_pause else resume_val

    gstate = qp.load_json(gstate_path) or {}
    applied = gstate.get("reflection_paused")
    if want_hold:
        print(f"reflection_gate: {ts} stale/hold tier={verdict['tier']} "
              f"(prev={prev_tier}) — no action")
        return 0
    cur_val, had_bom = lm_get(lm_cfg_path)
    intended_paused = (cur_val == pause_val)
    drift = cur_val != target
    # act only on real transitions; applied=None means "never acted yet",
    # in which case config drift alone decides (no boot-time restart churn).
    changed_state = applied is not None and applied != want_pause
    if not drift and not changed_state:
        print(f"reflection_gate: {ts} tier={verdict['tier']} "
              f"summary_trigger_rounds={cur_val} — no action")
        return 0

    # act: config write + kill -9 MainPID in ONE invocation (memory-override
    # discipline). On boot AstrBot re-reads the config, so the kill applies it.
    lm_set(lm_cfg_path, target, had_bom)
    pid = astrbot_mainpid(service)
    killed = False
    if pid > 0:
        os.kill(pid, 9)
        killed = True
    time.sleep(8)  # systemd RestartSec + boot window
    tbs = journal_clean(service)
    act = "pause" if want_pause else "resume"
    print(f"reflection_gate: {ts} tier={verdict['tier']} -> {act} "
          f"(rounds {cur_val}->{target}) pid={pid} killed={killed} "
          f"tracebacks_in_last_40={tbs}")

    def mark(cur):
        cur["reflection_paused"] = want_pause
        cur["reflection_applied_rounds"] = target
        cur["reflection_last_action"] = f"{ts}:{act}:tb={tbs}"
        return cur
    qp.update_governor_state(gstate_path, mark)
    if tbs > 0:
        print(f"reflection_gate: WARN {tbs} Traceback lines after restart",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
