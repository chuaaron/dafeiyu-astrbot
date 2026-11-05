# V06b · agy quota probe + governor

额度 governor 旁路件（设计见 `deploy/wechat_home/GOVERNOR_V06_DESIGN.md` C/D 节）。
全部阈值在 `governor.json`，主人可直接改，无需动代码。

## 文件

| 文件 | 作用 | 部署位置（VPS） |
|------|------|----------------|
| `quota_probe.py` | 独立跑 `agy --print "/usage" --output-format json`（绝不进 stream-json 流；绝不带 `--disable-slash-commands`），守卫 `command.name=="usage"` 且 `usage.total_tokens==0`，跳过 `disabled` 桶，原子写 `quota_state.json`；失败保留上一份并非零退出。同时是**纯函数 ladder 的唯一实现**（`decide()`，bridge 与测试共用） | `/root/agy-bridge/quota_probe.py` |
| `governor.json` | 阈值：`stale_s=600`；effort=low 注入 `<0.40`；反思暂停 `<0.25`（恢复 `>0.30`）；硬门槛 5h`<0.10` 或 weekly`<0.15` → 429+Retry-After；滞回退出=进入+0.05；陈旧=保守档注入 low、**绝不 429** | `/root/agy-bridge/governor.json` |
| `reflection_gate.py` | 按 tier 暂停/恢复 LivingMemory 反思：写 `summary_trigger_rounds`（9999/10，utf-8-sig 保 BOM）+ **同一次运行内** `kill -9 MainPID`（防 4.29.4 内存态覆写），滞回状态在 `governor_state.json`，重启后 journal Traceback 自检 | `/root/agy-bridge/reflection_gate.py` |
| `notify_owner.sh` | wxclawbot（路径 B，不经 agy）微信通知：每 reset 窗口恰一次（flock + `governor_state.json` 盖章去重）；同一窗口失败 2 次即放弃并记日志，不阻塞 429。token 值全程不读不印 | `/root/agy-bridge/notify_owner.sh` |
| `bridge_v2.1.py` | v2.1 bridge 副本（溯源用；线上文件是 `/root/agy-bridge/bridge.py`，改前已备份 `bridge.py.bak-v06b`）。chat-completions 顶部（鉴权后、turn gate 前）：硬门槛 429+Retry-After（仅新鲜数据）+ 触发通知；未显式指定 effort 时注入 `low`；每轮一条 `[governor]` 结构化日志；新增只读 `GET /v1/quota`。Turn 类状态所有权不变，无新增模块级可变状态 | `/root/agy-bridge/bridge.py` |
| `systemd/` | `dafeiyu-quota-probe.{service,timer}`、`dafeiyu-reflection-gate.{service,timer}`：oneshot + `IntervalSec=120`、`Persistent=false`、`Restart=on-failure` | `/etc/systemd/system/` |

## 数据流

```
timer(120s) → quota_probe.py → /root/agy-bridge/quota_state.json
                                   ├→ bridge.py [governor gate：429/注入/日志，写 tier]
                                   ├→ GET /v1/quota（只读）
                                   └→ timer(120s) → reflection_gate.py
                                        → LivingMemory 配置 + AstrBot 重启（仅状态转换时）
bridge 硬门槛 → notify_owner.sh → wxclawbot send（每窗口一次，盖章去重）
```

`governor_state.json`（滞回 tier、反思 applied 状态、通知盖章）由三方经 flock
读-改-写合并，互不覆盖。

## 安装（VPS，root）

```bash
scp quota_probe.py reflection_gate.py governor.json notify_owner.sh \
    bridge_v2.1.py root@VPS:/root/agy-bridge/
ssh root@VPS 'cd /root/agy-bridge && cp -a bridge.py bridge.py.bak-v06b \
    && mv bridge_v2.1.py bridge.py && chmod +x notify_owner.sh'
scp systemd/dafeiyu-quota-probe.{service,timer} \
    systemd/dafeiyu-reflection-gate.{service,timer} root@VPS:/etc/systemd/system/
ssh root@VPS 'systemctl daemon-reload && \
    systemctl enable --now dafeiyu-quota-probe.timer dafeiyu-reflection-gate.timer && \
    systemctl restart agy-bridge'
```

验证：`systemctl list-timers 'dafeiyu-*'`；`python3 -c "import json;print(json.load(open('/root/agy-bridge/quota_state.json'))['fetched_at'])"` 距今 <120s；
`curl -H "Authorization: Bearer $(cat /root/agy-bridge/token)" http://100.102.85.99:8788/healthz /v1/quota`。

## 调参（主人）

只改 `/root/agy-bridge/governor.json`（下一轮 timer/请求即生效，无需重启服务）：
`ladder.*` 阈值、`stale_s`、`notify_enabled`（关掉微信提醒）、
`reflection_pause_value/resume_value`。阈值是按成本加权的经验值，
需要一周实跑观察后再调（V06b 诚实边界）。

## 测试（Mac）

```bash
/opt/homebrew/bin/python3.11 -B -m unittest discover -s tests -p 'test_governor.py'
```

fixture `tests/fixtures/agy_usage_probe.json` = VPS 真实探针输出（2026-11-05），
无任何账号/邮箱标识。
