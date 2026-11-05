# V06b 额度 Governor 设计笔记（V06a 调研产出，2026-11-05）

目的：确定 headless Antigravity CLI（`agy`，VPS `/root/.local/bin/agy`，ELF 二进制，1.5.1）真实暴露的额度信号，并给出可落地的节流方案。**本文档只做设计，不改动 VPS；实施属 V06b，待主人评审。**

---

## A. 信号源发现表

| # | 信号源 | 存在？ | 证据行 | 可靠性 |
|---|--------|--------|--------|--------|
| 1 | `agy -p /usage`（别名 `/quota`）文本输出 | ✅ | 实跑输出 `Gemini Models⇥Five Hour Limit Remaining⇥98%⇥2026-11-05T20:16:37Z` 等 4 行（tab 分隔，含每周/5h × Gemini/第三方 四个桶） | 高：真实运行验证；官方文档收录（见 #6） |
| 2 | `agy -p /usage --output-format json` | ✅ | 返回 `command.name="usage"`，`command.data.groups[].buckets[]` 含 `id`（gemini-weekly / gemini-5h / 3p-weekly / 3p-5h）、`window`、`remaining_fraction`（0–1 浮点）、`reset_time`（UTC ISO）、可选 `description`；顶层 `usage` 全零、`num_turns=0`、`conversation_id=""` → **不消耗模型 token** | 高：真实运行验证 + 官方文档 |
| 3 | 底层端点 | ✅（存在，不建议直用） | 二进制 strings：`/{$api_version}:fetchQuotaStatus`、`/v1internal:retrieveUserQuotaSummary`、`QuotaSummaryBucket/QuotaSummaryGroup/BudgetConfig` proto 符号；CLI 日志实锤 `Post "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"`；`cache.go` 有 singleflight 缓存，`quota_manager.go:45 doRefreshQuota` 在登录/`/usage` 时强刷 | 中：端点真实但主机名随环境变（日志见 daily- 前缀）、payload 未文档化；**绕开 agy 直调 = 自找脆性** |
| 4 | 本地 sqlite 状态 | ❌（无额度表） | `conversation_summaries.db` 只有 summaries/FTS；`conversations/<uuid>.db` 表为 steps/gen_metadata 等，gen_metadata blob 未检出 inputTokens/outputTokens 字段 | n/a：不要从本地库找额度 |
| 5 | 桥/宿主侧记账 | 部分 | bridge.py 把 agy result 的 usage 映射进 OpenAI `usage` 字段返回给客户端，但 **journal 不记录 token 数**（`journalctl -u agy-bridge --since today | grep -ci usage|tokens` = 0）；AstrBot 自己落库 `provider_stats`（token_input_other / token_input_cached / token_output + start/end）——今日 4 条 agy 轮次，每轮 input≈11.0–11.6k、output≈339–543 | 中：可作消耗观测；但 AstrBot 口径≈含自身开销，且「Quota is consumed proportionally to the cost of the tokens」（`/quota` 自带描述），token 数 ≠ 额度百分比，权重未知 |
| 6 | 官方文档 | ✅ | https://antigravity.google/docs/cli/commands/usage/ （`/usage`= `/quota`，"refreshes your model configuration and quota status from the backend"）；https://antigravity.google/docs/cli/headless/ （**明确警告：`/usage` 等斜杠命令不能送进 stream-json 流，必须作为独立 `agy -p` 命令跑**）；社区工具 abruption/agy-cli-usage README：原生 print-mode 配额 JSON 自 agy **1.1.11** 起提供，旧版会把 `/usage` 当普通 prompt 消耗推理额度 | 高 |
| 7 | OAuth 能力面 | ✅（仅供认知） | `/root/.gemini/antigravity-cli/antigravity-oauth-token`（0600，键：`token`/`auth_method`/`id_token_email`；值均未打印）；日志 `applyAuthResult: authMethod=consumer, quotaProject=`（空）→ consumer 通道，配额走 cloudcode-pa 内部端点，**非** Google Cloud Quota API，scope 名日志中不落盘 | n/a：证明 agy 本身持有足够凭证拉配额；V06b 不需要也不应自己拿 token 调外部 API |
| 8 | statusLine 推送通道 | 存疑 | 第三方文（dev.to, 2026-10）：settings.json `statusLine` 会收到含五个桶（多一个 `gemini-argon-wtu-weekly`，桶可带 `"disabled": true`）的 JSON payload | 低：仅交互 TUI 生效，headless 无 TUI；未在 VPS 验证；仅作前瞻备注 |

## B. 诚实结论：真实 quota % 能否 headless 读到？

**能。** `agy -p /usage --output-format json` 在本 VPS 实测 3.2s 返回四个桶的 `remaining_fraction` 与 `reset_time`，0 模型 token 消耗，且是官方文档化的原生能力（≥1.1.11；本机 1.5.1）。这不是代理指标，是桌面应用同一数据源（retrieveUserQuotaSummary）。V06b 不必发明估算器——直读即可。

窗口语义（来自 `/quota` 输出与官方文档）：每组（Gemini 系 / Claude+GPT 系）各有 5h 窗口与 weekly 窗口；`remaining_fraction` 是**剩余**比例（勿与"已用%"混）；消耗按 token 成本加权，非按请求数。

## C. Governor 选项（按推荐排序，全部零付费 API）

### 选项 1（推荐核心）：Direct-quota sidecar 探针
- **机制**：VPS 新增 `quota_probe`（脚本+systemd timer，每 60–120s）跑 `agy -p /usage --output-format json`（env：`HOME=/root TERM=dumb PATH=...`，timeout 60s），解析后原子写 `/root/agy-bridge/quota_state.json`：`{fetched_at, groups:{gemini:{5h:{frac,reset},weekly:{...}}, 3p:{...}}}`；bridge 加只读 `GET /v1/quota`（返回该文件内容）供 AstrBot 侧/观测台消费。
- **接线点**：新文件 + bridge.py 读文件，不改任何现有逻辑路径；**绝不触碰** `/root/agy-bridge/token` 与 oauth-token 文件。
- **失败模式**：① 配额服务自身 503（实测 W1104 10:55 出现过 `UNAVAILABLE retrieveUserQuotaSummary`）→ 文件陈旧：消费方必须检查 `fetched_at` 新鲜度（>10min 视为未知，按保守档处理）；② 探针与 bridge 同时拉起 agy：独立进程、不走 turn gate，2C 机器上可接受，但频率别低于 60s；③ agy 版本回退/升级改变 JSON 形状 → 探针内置守卫：`command.name=="usage"` 且顶层 `usage.total_tokens==0` 才接受，否则告警并保留上一份状态。
- **可靠性**：官方数据源；唯一真风险是 agy 内部 JSON 形状无兼容性承诺（文档存在但字段可能随版本漂移）→ 解析失败降级到选项 3 兜底。

### 选项 2（叠加在选项 1 之上）：降级阶梯（degradation ladder）
- **机制**：以 `gemini.5h.remaining_fraction`（主模型组）与 `gemini.weekly` 为输入，带滞回（进入/退出阈值分开，退出用 reset_time 对齐窗口）：
  - 5h < 40%：bridge 侧对本轮强制 `reasoning_effort` 降档（请求体已有通道：`req.get("reasoning_effort") or req.get("agy_effort")`，bridge.py:268）；
  - 5h < 25%：**暂停 LivingMemory 反思**（把 `summary_trigger_rounds` 调到极大或插件开关置位——V04 已知反思=每 10 轮 1 次 agy 调用，容量风暴下可达 10 分钟级）；**暂停主动联系队列**（V05 结论：主动路径最容易被 503 风暴吞掉，70 分钟重试链全白烧）；
  - 5h < 10% 或 weekly < 15%：chat 只保留最小回复（短 effort、提示大肥鱼"今天脑子不够用"），微信通知主人一次（复用路径 B wxclawbot，不经 agy）；
  - weekly 桶独立跟踪：weekly < 20% 时限流为"每会话每小时 N 轮"。
- **接线点**：判断逻辑放**桥中间件**（单点、对所有消费者生效），暂停动作放 AstrBot 侧（provider_stats 或观测台读 `/v1/quota` 后调插件配置）。
- **失败模式**：阈值抖动→滞回解决；百分比按成本加权、非线性→阈值留裕量；3p 组（claude 兜底模型）单独一档，避免 flash 耗尽时切 claude 反而撞 3p 门。
- **可靠性**：高（数据真实），阈值数值需一周实跑调参。

### 选项 3（仅作降级兜底）：吞吐估算器
- **机制**：从 AstrBot `provider_stats` 聚合当前窗口 token（窗口起点 = 最近一次 `reset_time` − 5h；5h 窗长按官方命名假定，**7d 窗同理**），对比"经验耗尽点"估计水位；纯本地，零额外进程。
- **失败模式**：cap 绝对值从未观测（本次实测 98% 剩余时近 5h 烧了约 46k input——提示 cap 很大、日常聊天远未触顶，估算器分辨率差）；成本权重未知；换模型即失真。
- **可靠性**：低——只在探针文件过期且无新鲜配额数据时当"有消耗即在烧"的粗保险。

## D. V06b 最小可行（MVI）

1. `deploy/agy_quota_probe/quota_probe.py`（新，随仓库走）+ systemd unit/timer 模板 → 父 agent 安装（子代理不装常驻，按仓库纪律）。输出 `/root/agy-bridge/quota_state.json`。
2. bridge.py v2.1：启动处理 `/v1/chat/completions` 前读 `quota_state.json`；`gemini.5h.frac < HARD_FLOOR`（建议 0.08）→ 429 + `Retry-After: ceil(reset_time - now)`（避免 70 分钟重试风暴的根输入）；`0.08 ≤ frac < 0.25` → 注入 `effort="low"`（请求未显式指定时）。两处都在现有 handler 顶部，不碰鉴权/会话逻辑。
3. AstrBot 侧：观测台/worker 读 `GET /v1/quota`；5h < 0.25 时经既有 provider 配置流程暂停 LivingMemory 反思与 V05 主动任务（写动作沿用「编辑 && kill -9 MainPID」同一 ssh 串纪律，防 4.29.4 内存态覆写）。
4. 模型/effort 默认值位置（供切换参考）：`/root/.gemini/antigravity-cli/settings.json` 键 `model`（值 `"Gemini 3.8 Flash (Medium)"`，显示名而非 id）、`agentMode`、`verbosity`；会话级覆盖走 `--model/--effort`（bridge 请求体字段已透传，**优先用它，别写 settings.json**——AstrBot 运行期会重写自己的配置，agy 的 settings.json 同样有被 agy 进程改回的风险）。
5. 探针命令本体（已实测，可抄）：
   ```bash
   cd /root && timeout 60 env HOME=/root TERM=dumb LANG=C.UTF-8 PATH=/root/.local/bin:/usr/bin:/bin \
     agy --print "/usage" --output-format json --dangerously-skip-permissions
   ```
   解析 `command.data.groups[*].buckets[*]`：`id/window/remaining_fraction/reset_time`；跳过带 `"disabled": true` 的桶。

## E. 未决风险

- **版本耦合**：print-mode 配额 JSON 无 semver 级兼容承诺；1.1.11 之前版本会把 `/usage` 烧成模型调用——探针守卫必须保留。
- **配额服务可用性**：容量风暴时 retrieveUserQuotaSummary 也会 503（已观测一次）→ "额度最黑的时刻恰是最难读到额度"的悖论：陈旧状态一律按保守档。
- **实测样本量**：今日仅 4 轮真实聊天 + 2 次探针（CET 17:19/17:22，5h 桶 98%）——未验证风暴期探针行为、未验证探针与 agy turn 并发、未观测 weekly 触顶形态。
- **成本权重未知**：不同模型/思考量对同一桶的扣分比例不同，token↔百分比无法互推；ladder 阈值只能经验调参。
- **statusLine 通道**（#8）未验证，暂不纳入方案。
- 探针每次运行会追加 `history.jsonl`/cli 日志（无害，但 `/root` 工作区会积累文件；V06b 固定用 `cd /root` 与只读命令即可）。

## 引用

- https://antigravity.google/docs/cli/commands/usage/
- https://antigravity.google/docs/cli/headless/（standalone slash-command 警告）
- https://antigravity.google/docs/cli/reference/（`/usage`=`/quota`，`--effort`，`--output-format`）
- https://github.com/abruption/agy-cli-usage （归档说明：1.1.11+ 原生 JSON 取代该工具；字段迁移对照）
- https://dev.to/wassim-azi/see-your-antigravity-cli-quotas-without-typing-usage-gemini-5h7d-claudegpt-and-argon-19fc （桶清单/statusLine/argon 桶，第三方，未验证）
