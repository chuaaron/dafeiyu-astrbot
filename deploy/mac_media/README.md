# Mac media worker（V03）

2GB VPS 不跑 ffmpeg；重媒体活（抽帧 / 音频切块 / 将来的 TTS）由这台 Mac 上的
纯标准库 HTTP 服务承担，经 tailscale 私网调用。

## 运行

```bash
# 前台调试（token 必须先存在，见下）
/opt/homebrew/bin/python3.11 deploy/mac_media/media_worker.py
# 环境变量覆盖默认值：
#   MEDIA_WORKER_HOST=100.76.36.107  MEDIA_WORKER_PORT=8020
#   ALLOW_PUBLIC=1  才允许绑 0.0.0.0（默认拒绝启动）
```

## 接口契约

- 除 `GET /healthz` 外全部要求 `Authorization: Bearer $(cat ~/.dafeiyu-admin/media_token)`。
- 请求体 = **原始媒体字节**；参数走 query string（无 multipart）。
- 上传上限 64 MiB（超限 413）；响应 JSON 总量上限 3 MiB（超限按优先级丢帧/丢尾块，
  并在 `frames_dropped` / `chunks_dropped` 里报告）。
- 同一时刻只跑一个 ffmpeg 作业（全局锁，其余排队，尽力 FIFO）；每个 ffmpeg/ffprobe
  子进程 120s 超时，超时杀进程并返回 5xx + 截断 500 字符的 stderr 摘录。
- 临时文件用 `tempfile.mkdtemp` 独立目录，`finally` 里必清理。

### `GET /healthz`
```json
{"application": "dafeiyu-mac-media"}
```
（无鉴权）

### `POST /extract_frames?max_frames=16&max_duration_s=300&width=720`
ffprobe 取时长 → ffmpeg `fps` 滤镜在 `min(时长, max_duration_s)` 窗口内均匀采样
（上限 `max_frames` 帧，JPEG 质量 `-q:v 3` ≈ 80+）。丢帧策略：从最中间开始丢
（保留首尾帧信息）。

```json
{"duration_s": 12.0, "frames": [{"t_s": 0.0, "jpg_b64": "..."}, ...], "frames_dropped": 0}
```

### `POST /audio_chunks?max_duration_s=300&chunk_s=60`
ffmpeg 抽音轨为 mp3（32k 单声道），按 `chunk_s` 秒切段。超 3 MiB 时截掉尾部块。

```json
{"chunks": [{"t0_s": 0, "mp3_b64": "..."}, ...], "chunks_dropped": 0}
```

### `POST /tts?voice=Cherry`（body = 待合成文本，UTF-8）
`~/.dafeiyu-admin/dashscope.key` 存在时调 DashScope `qwen3-tts-flash`
（urllib，取 `output.audio.url` 下载 WAV 后**本地 ffmpeg 转 mp3** 再 base64，
兑现 `mp3_b64` 契约）；key 不存在时 **503
`{"error":"dashscope-key-missing"}`**。2026-11-05 03:40 主人放入 key 后，
真实链路已完成跨机往返验证（见 VALIDATION.md V03）。

## launchd 安装

仓库内的 [`com.dafeiyu.media-worker.plist`](com.dafeiyu.media-worker.plist) 拷到
`~/Library/LaunchAgents/` 后：

```bash
launchctl bootout gui/$(id -u)/com.dafeiyu.media-worker 2>/dev/null  # 幂等
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dafeiyu.media-worker.plist
launchctl list | grep media-worker
```

KeepAlive + RunAtLoad：崩溃自动拉起、登录即启。日志：
`~/Library/Logs/dafeiyu-media-worker.log`。

## 安全姿态

- **只绑 tailscale IP**（100.76.36.107），非 0.0.0.0；启动时硬校验，公网绑定必须
  显式 `ALLOW_PUBLIC=1`。
- Bearer token（64 位 hex，`openssl rand -hex 32`），Mac 端存
  `~/.dafeiyu-admin/media_token`（0600），VPS 端唯一副本存
  `/root/.dafeiyu/media_token`（0600）。token 值不进仓库、不进日志。
- 鉴权比较用 `hmac.compare_digest`（防时序侧信道）。
- 无任何写文件/删除接口；只做"收字节 → ffmpeg → 回 JSON"。
