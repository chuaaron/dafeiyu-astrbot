#!/usr/bin/env bash
# V06b: one WeChat owner notice per quota reset window via wxclawbot (path B,
# never via agy). Idempotent: stamps governor_state.json under flock so
# repeated triggers within the same window send exactly once. After two
# consecutive failures for a window it gives up (logs, does not block 429s).
# Never reads or prints any token value.
set -u
CFG="${GOVERNOR_CONFIG:-/root/agy-bridge/governor.json}"
exec /usr/bin/python3 - "$CFG" <<'PY'
import fcntl, glob, json, os, subprocess, sys, time

cfg_path = sys.argv[1]
try:
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
except Exception as e:
    print(f"notify_owner: cannot load config: {e}", file=sys.stderr)
    sys.exit(1)

if not cfg.get("notify_enabled", True):
    sys.exit(0)

state_path = cfg.get("state_file", "/root/agy-bridge/quota_state.json")
gstate_path = cfg.get("governor_state_file",
                      "/root/agy-bridge/governor_state.json")
log_path = cfg.get("notify_log", "/root/agy-bridge/governor_notify.log")

def log(msg):
    line = "%s notify_owner: %s" % (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    print(line)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

def load(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

state = load(state_path) or {}
groups = state.get("groups") or {}
gem = groups.get("gemini") or {}
b5 = gem.get("5h") or {}
bw = gem.get("weekly") or {}
lad = (cfg.get("ladder") or {})
f5 = b5.get("frac"); fw = bw.get("frac")
# governing window = whichever hard floor is breached (5h wins ties)
if (f5 is not None and f5 < float(lad.get("hard_floor_5h_below", 0.10))) \
        or not bw:
    win, reset_iso, frac_txt = "5h", b5.get("reset_iso"), f5
else:
    win, reset_iso, frac_txt = "weekly", bw.get("reset_iso"), fw
if reset_iso is None:
    log("no reset_time in state — skipping notice")
    sys.exit(1)
key = "%s|%s" % (win, reset_iso)
pct = ("%.0f%%" % (frac_txt * 100)) if isinstance(frac_txt, (int, float)) else "?"

lock_path = gstate_path + ".lock"
with open(lock_path, "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    gstate = load(gstate_path)
    if not isinstance(gstate, dict):
        gstate = {}
    notified = gstate.get("hard_floor_notice") or {}
    if notified.get("window") == key:
        log("dedup: window %s already notified at %s" % (key, notified.get("at")))
        sys.exit(0)
    if notified.get("failed_window") == key:
        log("window %s already failed twice — giving up until next window" % key)
        sys.exit(0)

    acct = cfg.get("wxclawbot_account")
    if not acct:
        cands = [p for p in sorted(glob.glob(os.path.join(
            cfg.get("wxclawbot_accounts_dir",
                    "/root/.openclaw/openclaw-weixin/accounts"), "*.json")))
            if not p.endswith(".context-tokens.json")]
        acct = cands[0] if cands else None
    if not acct or not os.path.exists(acct):
        log("no wxclawbot account file found — cannot notify")
        sys.exit(1)
    bot_id = os.path.basename(acct)[:-5]  # strip .json; never read contents

    text = ("【大肥鱼·额度governor】Gemini 额度见底：5h 剩余 %s"
            "（weekly 剩余 %s），%s 窗口重置时间 %s。"
            "bridge 已启用 429 硬门槛（带 Retry-After），本窗口只提醒这一次。"
            % (("%.0f%%" % (f5 * 100)) if isinstance(f5, (int, float)) else "?",
               ("%.0f%%" % (fw * 100)) if isinstance(fw, (int, float)) else "?",
               win, reset_iso))
    cmd = [cfg.get("wxclawbot_bin", "/usr/local/bin/wxclawbot"),
           "send", "--account", bot_id, "--text", text, "--json"]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, timeout=60)
        ok = False
        try:
            # wxclawbot --json prints the result object; accept it from
            # stdout (search first '{' in case of preamble noise)
            out = p.stdout or ""
            doc = json.loads(out[out.index("{"):out.rindex("}") + 1])
            ok = p.returncode == 0 and doc.get("ok") is True
        except (ValueError, json.JSONDecodeError):
            pass
        detail = "rc=%d ok=%s" % (p.returncode, ok)
        if not ok:
            # log only non-secret tail (no token values are ever emitted)
            detail += " stdout_tail=%r stderr_tail=%r" % (
                (p.stdout or "")[-160:], (p.stderr or "")[-160:])
    except Exception as e:
        ok = False
        detail = "exception=%s" % e

    if ok:
        gstate["hard_floor_notice"] = {"window": key, "at": time.time(),
                                       "detail": detail}
        log("SENT for window %s (%s)" % (key, detail))
    else:
        fails = notified.get("fails") or {}
        n = int(fails.get(key, 0)) + 1
        fails[key] = n
        gstate["hard_floor_notice"] = {"window": notified.get("window"),
                                       "at": notified.get("at"),
                                       "fails": fails}
        if n >= 2:
            gstate["hard_floor_notice"]["failed_window"] = key
            log("FAILED twice for window %s (%s) — giving up; 429s continue"
                % (key, detail))
        else:
            log("send failed for window %s (%s) — attempt %d/2" % (key, detail, n))

    tmp = gstate_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(gstate, f)
    os.replace(tmp, gstate_path)
    fcntl.flock(lk, fcntl.LOCK_UN)
sys.exit(0 if ok else 1)
PY
