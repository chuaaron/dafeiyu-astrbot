#!/usr/bin/env python3
"""agy-bridge v2.1: OpenAI-compatible HTTP front for the headless Antigravity CLI.

v2.1 (V06b quota governor): per-request, read-only quota gate at the top of
/v1/chat/completions — hard-floor 429 + Retry-After (fresh data only), effort
injection when the request did not specify one, one structured [governor]
log line per turn, and GET /v1/quota. Ladder logic is imported from
quota_probe.py (same dir); auth/session/queue logic untouched.

Every request owns its subprocess and buffers; no module-level mutable state.
Driving style mirrors Enjoy.app:
  agy --input-format stream-json --output-format stream-json
      --disable-slash-commands --dangerously-skip-permissions
      [--model id] [--effort e] [--conversation uuid]
stdin:  one NDJSON line {"event":"user","message":{"content":[{"type":"text",...}]}}
stdout: init / step_update (text_delta on agent_response) / result events.
"""
import json
import os
import re
import selectors
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("AGYB_BIND", "100.102.85.99")
PORT = int(os.environ.get("AGYB_PORT", "8788"))
HOME = os.path.expanduser("~")
AGY = os.environ.get("AGY_BIN", os.path.join(HOME, ".local/bin/agy"))
WORKSPACE = os.environ.get("AGYB_WS", os.path.join(HOME, "agy-bridge/ws"))
TOKEN_FILE = os.path.join(HOME, "agy-bridge/token")
SESSIONS_FILE = os.path.join(HOME, "agy-bridge/sessions.json")
GOVERNOR_FILE = os.environ.get(
    "AGYB_GOVERNOR", os.path.join(HOME, "agy-bridge/governor.json"))
TURN_TIMEOUT = int(os.environ.get("AGYB_TIMEOUT", "900"))
QUEUE_TIMEOUT = int(os.environ.get("AGYB_QUEUE", "300"))

try:  # V06b: shared ladder (file lives next to bridge; fail open if absent)
    import quota_probe as qp
except Exception as _qp_err:  # pragma: no cover
    qp = None
    sys.stderr.write(f"WARNING: quota_probe import failed ({_qp_err}); "
                     "governor gate disabled\n")

VALID_MODEL = re.compile(r"^[A-Za-z0-9._-]+$")

_turn_gate = threading.Semaphore(1)
_sessions_lock = threading.Lock()


def load_token():
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    tok = uuid.uuid4().hex + uuid.uuid4().hex
    with open(TOKEN_FILE, "w") as f:
        f.write(tok)
    os.chmod(TOKEN_FILE, 0o600)
    return tok


TOKEN = load_token()


def load_sessions():
    try:
        return json.load(open(SESSIONS_FILE))
    except Exception:
        return {}


def save_sessions(s):
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, SESSIONS_FILE)


# ---- V06b quota governor helpers (pure reads; all state lives in files) ----

def governor_cfg():
    if qp is None:
        return {}
    return qp.load_json(GOVERNOR_FILE) or {}


def quota_verdict():
    """Compute (verdict, cfg) from quota_state.json + persisted bridge tier.
    Returns (None, cfg) when the ladder module is unavailable."""
    if qp is None:
        return None, {}
    cfg = governor_cfg()
    state = qp.load_json(cfg.get("state_file",
                                 "/root/agy-bridge/quota_state.json"))
    gstate_path = cfg.get("governor_state_file",
                          "/root/agy-bridge/governor_state.json")
    prev_tier = (qp.load_json(gstate_path) or {}).get("tier")
    verdict = qp.decide(state, cfg, prev_tier)
    # persist the tier for hysteresis (shared key with the reflection gate;
    # merged under flock, same inputs -> same value)
    def stamp(cur):
        cur["tier"] = verdict["tier"]
        return cur
    try:
        qp.update_governor_state(gstate_path, stamp)
    except Exception:
        pass
    return verdict, cfg


def fmt_frac(v):
    return "na" if v is None else "%.3f" % v


def trigger_owner_notice(cfg):
    """Fire-and-forget the once-per-window WeChat notice script. The script
    itself dedups via governor_state.json; failures never block 429s."""
    script = cfg.get("notify_script", "/root/agy-bridge/notify_owner.sh")
    if not os.path.exists(script):
        sys.stderr.write("[governor] notify script missing: %s\n" % script)
        return
    try:
        subprocess.Popen(["/bin/bash", script],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True,
                         env=dict(os.environ, HOME=HOME))
    except Exception as e:
        sys.stderr.write("[governor] notify spawn failed: %s\n" % e)


def text_of(content):
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return content or ""


def extract_prompt(messages):
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = text_of(msg.get("content", ""))
        if not content:
            continue
        if role == "system":
            parts.append("[host system note]\n" + content)
        elif role == "assistant":
            parts.append("[prior assistant turn]\n" + content)
        else:
            parts.append(content)
    return "\n\n".join(parts)


class Turn:
    """Owns one agy subprocess. iterate() yields ('delta', text) then finally
    ('result', dict). Resume-replay guard: results seen before this turn's
    user_input step are held as `replay` (used only if stdout ends otherwise)."""

    def __init__(self, prompt, model, effort, conversation):
        cmd = [AGY, "--input-format", "stream-json", "--output-format", "stream-json",
               "--disable-slash-commands", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if conversation:
            cmd += ["--conversation", conversation]
        env = dict(os.environ, TERM="dumb", LANG="C.UTF-8", HOME=HOME)
        self.proc = subprocess.Popen(cmd, cwd=WORKSPACE, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, bufsize=1, env=env)
        line = json.dumps({"event": "user",
                           "message": {"content": [{"type": "text", "text": prompt}]}})
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            pass
        self.deltas = []
        self.result = None
        self.timed_out = False
        self._saw_user_input = False
        self._replay = None
        self._deadline = time.monotonic() + TURN_TIMEOUT

    def _lines(self):
        sel = selectors.DefaultSelector()
        sel.register(self.proc.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    self.timed_out = True
                    return
                if sel.select(timeout=min(remaining, 2.0)):
                    line = self.proc.stdout.readline()
                    if line == "":  # EOF
                        return
                    yield line
                else:
                    yield None  # idle tick: no stdout within the select window
        finally:
            sel.close()

    def iterate(self):
        for raw in self._lines():
            if raw is None:
                yield "tick", None
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = ev.get("event")
            if kind == "step_update":
                su = ev.get("step_update", {})
                if su.get("step_type") == "user_input":
                    self._saw_user_input = True
                d = su.get("text_delta")
                if self._saw_user_input and su.get("step_type") == "agent_response" and d:
                    self.deltas.append(d)
                    yield "delta", d
            elif kind == "result":
                if not self._saw_user_input:
                    self._replay = ev.get("result", {})
                    continue
                self.result = ev.get("result", {})
                yield "result", self.result
                return
        if self.result is None:
            if self.timed_out:
                self.result = {"status": "ERROR", "error": f"timeout after {TURN_TIMEOUT}s"}
            elif self._replay is not None:
                self.result = self._replay
            else:
                self.result = {"status": "ERROR",
                               "error": self._stderr_tail() or "agy exited without result"}
        yield "result", self.result

    def _stderr_tail(self):
        try:
            return self.proc.stderr.read()[-400:]
        except Exception:
            return ""

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


class Handler(BaseHTTPRequestHandler):
    server_version = "agy-bridge/2.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _authed(self):
        return self.headers.get("Authorization", "") == "Bearer " + TOKEN

    def _client_gone(self):
        """Non-blocking FIN/RST probe on the client socket (no data consumed)."""
        try:
            return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False  # no pending event: connection alive
        except OSError:
            return True  # RST / already closed

    def _send(self, code, obj, extra_headers=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra_headers:
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": {"message": msg, "type": "agy_bridge_error"}})

    def do_GET(self):
        if not self._authed():
            return self._err(401, "bad token")
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if self.path.startswith("/v1/models"):
            try:
                p = subprocess.run([AGY, "models"], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, timeout=60,
                                   env=dict(os.environ, TERM="dumb", HOME=HOME))
                models = []
                for line in p.stdout.splitlines():
                    m = re.match(r"^([A-Za-z0-9._-]+)\t(.+)$", line)
                    if m:
                        models.append({"id": m.group(1), "object": "model",
                                       "owned_by": "antigravity", "name": m.group(2)})
                return self._send(200, {"object": "list", "data": models})
            except Exception as e:
                return self._err(500, f"agy models failed: {e}")
        if self.path.startswith("/v1/sessions"):
            return self._send(200, load_sessions())
        if self.path.startswith("/v1/quota"):
            if qp is None:
                return self._err(503, "quota ladder module unavailable")
            cfg = governor_cfg()
            state = qp.load_json(cfg.get("state_file",
                                         "/root/agy-bridge/quota_state.json"))
            # read-only view: do NOT persist the tier from a GET
            gstate = qp.load_json(cfg.get("governor_state_file",
                                          "/root/agy-bridge/governor_state.json"))
            verdict = qp.decide(state, cfg, (gstate or {}).get("tier"))
            return self._send(200, {
                "state": state,
                "verdict": verdict,
                "stale": qp.is_stale(state, cfg),
                "ladder": cfg.get("ladder", {}),
            })
        self._err(404, "not found")

    def do_POST(self):
        if not self._authed():
            return self._err(401, "bad token")
        if not self.path.startswith("/v1/chat/completions"):
            return self._err(404, "not found")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._err(400, "bad json body")

        messages = req.get("messages") or []
        model = req.get("model") or "gemini-3.8-flash-medium"
        if not VALID_MODEL.match(model):
            return self._err(400, f"invalid model name: {model}")
        effort = req.get("reasoning_effort") or req.get("agy_effort")
        if effort not in ("low", "medium", "high"):
            effort = None

        # ---- V06b quota gate (after auth, before the turn gate) ----
        # Both stream and non-stream paths flow through here, so one check
        # covers both. Body already read: no keep-alive desync on early 429.
        injected = None
        model_rewritten = False
        verdict, gcfg = quota_verdict()
        if verdict is not None:
            if verdict["tier"] == "hard_gate":
                retry = verdict.get("retry_after_s") or 300
                trigger_owner_notice(gcfg)
                self.log_message(
                    "[governor] action=blocked_429 tier=hard_gate "
                    "frac5h=%s weekly=%s stale=%s retry_after=%d",
                    fmt_frac(verdict["frac_5h"]), fmt_frac(verdict["frac_weekly"]),
                    verdict["stale"], retry)
                return self._send(429, {
                    "error": {"message": "agy quota hard floor: "
                              f"gemini remaining 5h={fmt_frac(verdict['frac_5h'])} "
                              f"weekly={fmt_frac(verdict['frac_weekly'])} — "
                              "resume after Retry-After",
                              "type": "quota_hard_floor"},
                }, extra_headers=[("Retry-After", retry)])
            if effort is None and verdict.get("inject_effort"):
                effort = verdict["inject_effort"]
                injected = effort
                # agy rejects --effort conflicting with the model's own
                # effort variant (e.g. gemini-3.8-flash-medium + low).
                remap = gcfg.get("low_effort_model_map") or {}
                if effort == "low" and model in remap:
                    model = remap[model]
                    model_rewritten = True
            self.log_message(
                "[governor] action=pass tier=%s frac5h=%s weekly=%s "
                "stale=%s requested_effort=%s injected_effort=%s model=%s "
                "model_rewritten=%s",
                verdict["tier"], fmt_frac(verdict["frac_5h"]),
                fmt_frac(verdict["frac_weekly"]), verdict["stale"],
                req.get("reasoning_effort") or req.get("agy_effort") or "none",
                injected or "none", model, model_rewritten)
        session = req.get("user") or self.headers.get("X-Agy-Session")
        stream = bool(req.get("stream"))

        conversation = None
        if session:
            if req.get("agy_new_conversation"):
                with _sessions_lock:
                    s = load_sessions()
                    s.pop(session, None)
                    save_sessions(s)
            else:
                with _sessions_lock:
                    conversation = load_sessions().get(session)
            if conversation:
                last_user = [m for m in messages if m.get("role") == "user"]
                prompt = text_of(last_user[-1].get("content")) if last_user else ""
            else:
                prompt = extract_prompt(messages)
        else:
            prompt = extract_prompt(messages)
        if not prompt:
            return self._err(400, "empty prompt")

        if not _turn_gate.acquire(timeout=QUEUE_TIMEOUT):
            return self._err(429, "bridge queue timeout")
        turn = None
        try:
            turn = Turn(prompt, model, effort, conversation)
            if stream:
                self._respond_stream(turn, model, session)
            else:
                gone = False
                for kind, _payload in turn.iterate():
                    if kind == "tick" and self._client_gone():
                        gone = True
                        break
                turn.close()
                if gone:
                    self.close_connection = True
                    return
                self._respond_plain(turn, model, session)
        except Exception as e:
            if turn is not None:
                turn.close()
            self._err(500, f"turn crashed: {e}")
        finally:
            _turn_gate.release()

    def _record_conversation(self, turn, session):
        conv = (turn.result or {}).get("conversation_id")
        if conv and session:
            with _sessions_lock:
                s = load_sessions()
                if s.get(session) != conv:
                    s[session] = conv
                    save_sessions(s)

    def _respond_plain(self, turn, model, session):
        result = turn.result or {"status": "ERROR", "error": "no result"}
        self._record_conversation(turn, session)
        text = result.get("response") or "".join(turn.deltas)
        usage = result.get("usage") or {}
        if result.get("status") != "SUCCESS" and not text.strip():
            return self._send(502, {"error": {"message": result.get("error") or "agy error",
                                              "type": "agy_error",
                                              "conversation_id": result.get("conversation_id")}})
        self._send(200, {
            "id": "chatcmpl-agy-" + uuid.uuid4().hex[:12],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                      "completion_tokens": usage.get("output_tokens", 0),
                      "total_tokens": usage.get("total_tokens", 0)},
            "conversation_id": result.get("conversation_id"),
        })

    def _respond_stream(self, turn, model, session):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        cid = "chatcmpl-agy-" + uuid.uuid4().hex[:12]

        def chunk(obj):
            data = b"data: " + json.dumps(obj).encode() + b"\n\n"
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        def keepalive():
            data = b": keepalive\n\n"
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        def base(extra=None):
            obj = {"id": cid, "object": "chat.completion.chunk",
                   "created": int(time.time()), "model": model}
            if extra:
                obj.update(extra)
            return obj

        try:
            chunk(base({"choices": [{"index": 0, "delta": {"role": "assistant"},
                                     "finish_reason": None}]}))
            last_beat = time.monotonic()
            for kind, payload in turn.iterate():
                if kind == "tick":
                    if self._client_gone():
                        turn.close()
                        self.close_connection = True
                        return
                    if time.monotonic() - last_beat >= 15:
                        keepalive()
                        last_beat = time.monotonic()
                    continue
                if kind == "delta":
                    chunk(base({"choices": [{"index": 0, "delta": {"content": payload},
                                             "finish_reason": None}]}))
                    last_beat = time.monotonic()
            turn.close()
            self._record_conversation(turn, session)
            result = turn.result or {}
            usage = result.get("usage") or {}
            if result.get("status") != "SUCCESS" and not turn.deltas:
                chunk(base({"error": result.get("error") or "agy error"}))
            chunk(base({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                                  "completion_tokens": usage.get("output_tokens", 0),
                                  "total_tokens": usage.get("total_tokens", 0)},
                        "conversation_id": result.get("conversation_id")}))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            turn.close()


def main():
    os.makedirs(WORKSPACE, exist_ok=True)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    sys.stderr.write(f"agy-bridge v2.1 on http://{BIND}:{PORT} "
                     f"(workspace={WORKSPACE}, "
                     f"governor={'on' if qp is not None else 'OFF'})\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
