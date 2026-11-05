#!/usr/bin/env python3
"""dafeiyu Mac media worker (V03).

Stdlib-only HTTP service on the Mac that the 2GB VPS calls over tailscale for
heavy ffmpeg work (frame extraction, audio chunking) and (future) TTS.

Security posture:
  - binds the tailscale IP only (default 100.76.36.107:8020); refuses to start
    on 0.0.0.0 unless ALLOW_PUBLIC=1 is set explicitly.
  - every endpoint except GET /healthz requires
    `Authorization: Bearer <token>` with token read from ~/.dafeiyu-admin/media_token.
  - request bodies are raw media bytes; parameters come via query string.

Endpoints:
  GET  /healthz                     -> {"application":"dafeiyu-mac-media"} (no auth)
  POST /extract_frames?max_frames=16&max_duration_s=300&width=720
  POST /audio_chunks?max_duration_s=300&chunk_s=60
  POST /tts?voice=Cherry            -> 503 {"error":"dashscope-key-missing"} until
                                       ~/.dafeiyu-admin/dashscope.key exists.

Limits: uploads <= 64 MiB (413 otherwise); each response JSON <= 3 MiB
(middle frames / trailing chunks dropped to fit, reported in *_dropped);
one ffmpeg job at a time under a global lock (others queue, best-effort FIFO);
each ffmpeg/ffprobe subprocess killed after 120 s (5xx with stderr excerpt on failure).
"""

import base64
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("MEDIA_WORKER_HOST", "100.76.36.107")
PORT = int(os.environ.get("MEDIA_WORKER_PORT", "8020"))
ALLOW_PUBLIC = os.environ.get("ALLOW_PUBLIC", "") == "1"

TOKEN_PATH = os.environ.get("MEDIA_WORKER_TOKEN_PATH",
                            os.path.expanduser("~/.dafeiyu-admin/media_token"))
DASHSCOPE_KEY_PATH = os.environ.get("DASHSCOPE_KEY_PATH",
                                    os.path.expanduser("~/.dafeiyu-admin/dashscope.key"))

FFMPEG = os.environ.get("FFMPEG_BIN", "/opt/homebrew/bin/ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "/opt/homebrew/bin/ffprobe")

MAX_UPLOAD = 64 * 1024 * 1024          # 64 MiB body cap -> 413
RESPONSE_CAP = 3 * 1024 * 1024         # 3 MiB total JSON cap
FFMPEG_TIMEOUT = 120                   # seconds per subprocess

JOBS_LOCK = threading.Lock()           # one ffmpeg job at a time (queue is best-effort FIFO)


class JobError(Exception):
    def __init__(self, message, stderr=""):
        super().__init__(message)
        self.stderr = (stderr or "")[:500]


def load_token() -> str:
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        print(f"FATAL: cannot read token file {TOKEN_PATH}: {e}", flush=True)
        sys.exit(1)


TOKEN = ""


def run_cmd(argv, timeout=FFMPEG_TIMEOUT):
    """Run a subprocess, capture output, kill cleanly on timeout."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise JobError(f"timeout after {timeout}s: {os.path.basename(argv[0])}",
                       err.decode("utf-8", "replace"))
    if proc.returncode != 0:
        raise JobError(
            f"{os.path.basename(argv[0])} exited {proc.returncode}",
            err.decode("utf-8", "replace"),
        )
    return out


def probe_duration(path) -> float:
    out = run_cmd([FFPROBE, "-v", "error",
                   "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float(out.decode().strip())
    except ValueError:
        raise JobError(f"ffprobe returned non-numeric duration: {out[:80]!r}")


def fit_json(payload_obj, drop_fn):
    """Serialize payload; while over RESPONSE_CAP, call drop_fn() to shed
    lowest-priority items (it must mutate payload_obj in place). Returns
    (bytes, dropped_count)."""
    dropped = 0
    while True:
        data = json.dumps(payload_obj).encode("utf-8")
        if len(data) <= RESPONSE_CAP or not drop_fn():
            return json.dumps(payload_obj).encode("utf-8"), dropped
        dropped += 1


def do_extract_frames(body: bytes, params) -> dict:
    max_frames = int(params.get("max_frames", ["16"])[0])
    max_duration_s = float(params.get("max_duration_s", ["300"])[0])
    width = int(params.get("width", ["720"])[0])
    max_frames = max(1, min(max_frames, 128))
    width = max(64, min(width, 1920))

    tmp = tempfile.mkdtemp(prefix="dafeiyu-frames-")
    try:
        src = os.path.join(tmp, "input.media")
        with open(src, "wb") as f:
            f.write(body)
        duration = probe_duration(src)
        window = min(duration, max_duration_s)
        fps = max_frames / window if window > 0 else 1.0

        out_pat = os.path.join(tmp, "frame_%04d.jpg")
        run_cmd([FFMPEG, "-y", "-v", "error", "-i", src,
                 "-t", f"{window:.6f}",
                 "-vf", f"fps={fps:.8f},scale={width}:-2",
                 "-frames:v", str(max_frames),
                 "-q:v", "3",  # mjpeg ~quality 80+
                 "-f", "image2", out_pat])

        frames = []
        names = sorted(n for n in os.listdir(tmp) if n.startswith("frame_"))
        for i, name in enumerate(names):
            with open(os.path.join(tmp, name), "rb") as f:
                jpg = f.read()
            t_s = round(i * (window / max_frames), 3)
            frames.append({"t_s": t_s, "jpg_b64": base64.b64encode(jpg).decode()})

        payload = {"duration_s": round(duration, 3), "frames": frames, "frames_dropped": 0}

        def drop_middle():
            if not payload["frames"]:
                return False
            mid = len(payload["frames"]) // 2
            payload["frames"].pop(mid)
            payload["frames_dropped"] += 1
            return True

        data, _ = fit_json(payload, drop_middle)
        result = json.loads(data)
        result["bytes"] = len(data)
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def do_audio_chunks(body: bytes, params) -> dict:
    max_duration_s = float(params.get("max_duration_s", ["300"])[0])
    chunk_s = int(params.get("chunk_s", ["60"])[0])
    chunk_s = max(1, min(chunk_s, 600))

    tmp = tempfile.mkdtemp(prefix="dafeiyu-audio-")
    try:
        src = os.path.join(tmp, "input.media")
        with open(src, "wb") as f:
            f.write(body)
        duration = probe_duration(src)
        window = min(duration, max_duration_s)

        out_pat = os.path.join(tmp, "chunk_%03d.mp3")
        run_cmd([FFMPEG, "-y", "-v", "error", "-i", src,
                 "-t", f"{window:.6f}",
                 "-vn", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "32k",
                 "-f", "segment", "-segment_time", str(chunk_s),
                 out_pat])

        chunks = []
        names = sorted(n for n in os.listdir(tmp) if n.startswith("chunk_"))
        for i, name in enumerate(names):
            with open(os.path.join(tmp, name), "rb") as f:
                mp3 = f.read()
            chunks.append({"t0_s": i * chunk_s, "mp3_b64": base64.b64encode(mp3).decode()})

        payload = {"chunks": chunks, "chunks_dropped": 0}
        expected_total = -(-window // chunk_s)  # ceil

        def drop_tail():
            if not payload["chunks"]:
                return False
            payload["chunks"].pop()
            payload["chunks_dropped"] = int(expected_total) - len(payload["chunks"])
            return True

        data, _ = fit_json(payload, drop_tail)
        result = json.loads(data)
        result["bytes"] = len(data)
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


DASHSCOPE_URL = ("https://dashscope.aliyuncs.com/api/v1/services/"
                 "aigc/multimodal-generation/generation")


def do_tts(body: bytes, params) -> dict:
    """DashScope qwen3-tts-flash, transcoded WAV->MP3 locally (contract says
    mp3_b64). First live round-trip succeeded 2026-11-05 03:40 once the owner
    dropped ~/.dafeiyu-admin/dashscope.key; with the key absent it 503s."""
    if not os.path.exists(DASHSCOPE_KEY_PATH):
        raise JobError("dashscope-key-missing")
    with open(DASHSCOPE_KEY_PATH, "r", encoding="utf-8") as f:
        key = f.read().strip()
    voice = params.get("voice", ["Cherry"])[0]
    text = body.decode("utf-8", "replace")

    req_body = json.dumps({
        "model": "qwen3-tts-flash",
        "input": {"text": text, "voice": voice},
    }).encode("utf-8")
    req = urllib.request.Request(
        DASHSCOPE_URL, data=req_body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=FFMPEG_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise JobError(f"dashscope HTTP {e.code}", e.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        raise JobError(f"dashscope unreachable: {e.reason}")

    audio = (data.get("output") or {}).get("audio") or {}
    url = audio.get("url")
    if not url:
        raise JobError("dashscope response missing output.audio.url",
                       json.dumps(data)[:500])
    try:
        with urllib.request.urlopen(url, timeout=FFMPEG_TIMEOUT) as audio_resp:
            raw = audio_resp.read()
    except urllib.error.URLError as e:
        raise JobError(f"audio download failed: {e.reason}")

    # DashScope hands back WAV; the contract promises mp3_b64 -> transcode.
    tmp = tempfile.mkdtemp(prefix="dafeiyu-tts-")
    try:
        src = os.path.join(tmp, "tts_in")
        dst = os.path.join(tmp, "tts.mp3")
        with open(src, "wb") as f:
            f.write(raw)
        run_cmd([FFMPEG, "-y", "-v", "error", "-i", src,
                 "-vn", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", dst])
        with open(dst, "rb") as f:
            mp3 = f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"mp3_b64": base64.b64encode(mp3).decode(), "bytes": len(mp3)}


class Handler(BaseHTTPRequestHandler):
    server_version = "dafeiyu-media-worker/0.1"

    def log_message(self, format, *args):  # noqa: A002 - match base signature
        print(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}",
              flush=True)

    # ---- helpers -------------------------------------------------------
    def _send_json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # client gave up mid-response; nothing to report back to
            self.close_connection = True

    def _authorized(self) -> bool:
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got, f"Bearer {TOKEN}")

    def _read_body(self):
        """Return raw body bytes, or None after sending 413 itself."""
        length = self.headers.get("Content-Length")
        if length is None:
            # No Content-Length: read until EOF, capped.
            if int(self.headers.get("Transfer-Length", "0") or 0) > MAX_UPLOAD:
                self._send_json(413, {"error": "upload too large", "max_bytes": MAX_UPLOAD})
                return None
            remaining = MAX_UPLOAD + 1
            buf = bytearray()
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                buf.extend(chunk)
                remaining -= len(chunk)
            body = bytes(buf)
        else:
            n = int(length)
            if n > MAX_UPLOAD:
                self._send_json(413, {"error": "upload too large", "max_bytes": MAX_UPLOAD})
                return None
            buf = bytearray()
            while len(buf) < n:
                chunk = self.rfile.read(min(65536, n - len(buf)))
                if not chunk:
                    break
                buf.extend(chunk)
            body = bytes(buf)
        if len(body) > MAX_UPLOAD:
            self._send_json(413, {"error": "upload too large", "max_bytes": MAX_UPLOAD})
            return None
        return body

    # ---- routes --------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json(200, {"application": "dafeiyu-mac-media"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if route not in ("/extract_frames", "/audio_chunks", "/tts"):
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        body = self._read_body()
        if body is None:
            return
        if not body:
            self._send_json(400, {"error": "empty body"})
            return
        params = parse_qs(parsed.query)

        routes = {"/extract_frames": do_extract_frames,
                  "/audio_chunks": do_audio_chunks,
                  "/tts": do_tts}
        try:
            with JOBS_LOCK:  # one heavy job at a time
                result = routes[route](body, params)
            self._send_json(200, result)
        except JobError as e:
            code = 503 if str(e) == "dashscope-key-missing" else 500
            self._send_json(code, {"error": str(e), "stderr": e.stderr})
        except Exception as e:  # noqa: BLE001 - last-resort 500
            self._send_json(500, {"error": f"{type(e).__name__}: {e}", "stderr": ""})
            raise


def main():
    global TOKEN
    if HOST in ("0.0.0.0", "", "::") and not ALLOW_PUBLIC:
        print(f"FATAL: refusing to bind {HOST!r} without ALLOW_PUBLIC=1", flush=True)
        sys.exit(1)
    if not os.path.exists(FFMPEG) or not os.path.exists(FFPROBE):
        print("FATAL: ffmpeg/ffprobe not found (expected /opt/homebrew/bin)", flush=True)
        sys.exit(1)
    TOKEN = load_token()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"dafeiyu-media-worker listening on http://{HOST}:{PORT} "
          f"(auth={os.path.basename(TOKEN_PATH)})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
