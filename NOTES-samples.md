# PLE 样例 harness 横向对比（sample_submissions/ 全读）

> 对象：`ple/sample_submissions/` 下 8 个目录（7 个可提交 + 1 个仅本地）。
> 代码基线：`ple` 仓库 `sample_submissions/`，只读，未改动 `ple/` 任何文件。
> 场景事实（来自 `README.md` / `docs/rules.md` / `eval/dev_set.yaml`）：85 任务，dev 29 题，每题最长 **6 小时**，交付物是真实工程文件（`.py` / `.zip` / `.ifc` / `/app/files/` / `/app/tools/.apps_data/` / memo），评分 = 自动 checks（硬约束一旦违反该项封顶 0.4）+ LLM rubric。

---

## 0. 先说结论

| 问题 | 结论 |
|---|---|
| 有几个样例 | 8 个目录；`local-model` 是 `custom-openai` 的同一份代码（`serve.py` md5 相同）换成本地模型，**不能提交** |
| 实际不同实现 | 7 份：`minimal`、`plan-verify`、`node-protocol`、`custom-openai`、`hermes-opus`、`pi-opus`、`agy-gemini` |
| 三种世界观 | ① 自己写 loop（minimal / plan-verify / node-protocol / custom-openai）② 包别人的 agent（hermes / pi / agy）③ 协议直写（node-protocol） |
| 最完整 | `custom-openai`（1800 行，MCP + 流式 + 两级压缩 + provider 兼容层 + deadline 管理） |
| **推荐起点** | **`custom-openai`**，再把 `plan-verify` 的 verifier 段移植进来（理由见 §5） |
| 全体共同实现的协议最低要求 | 见 §3 的 11 条 checklist —— 少一条就可能整题 0 分 |

总阅读量：7 份独有代码 ≈ **6 998 行**（含共享 `taskbridge.py` 1 528 行；7 份 `taskbridge.py` 内容完全一致，md5 `254c7cb5…`）。

---

## 1. 横向对比主表

### 1.1 模型与身份

| 样例 | `submission.yaml` 的 `model_api` | 绑定模型 | 能否换模型 | 说明 |
|---|---|---|---|---|
| `minimal` | `google-cloud/gemini-3.8-flash` | Gemini 3.8 Flash | 任意（走 OpenAI 兼容口） | 官方给"从零写 harness"的起点 |
| `plan-verify` | `google-cloud/claude-opus-5-5` | Claude Opus 5.5 | 任意 | minimal + 三角色 |
| `node-protocol` | `google-cloud/claude-opus-5-5` | Claude Opus 5.5 | 任意 | JS 直写协议，不依赖 taskbridge |
| `custom-openai` | `google-cloud/gpt-oss-120b` | gpt-oss 120B | 任意 | `env: COMPACT_AT_TOKENS: 90000` 是为 131k 窗口调的 |
| `local-model` | `openai-compatible/gpt-oss-120b` + `model_base_url` | 自建 vLLM | 任意 | **仅本地**，`kit check` 永远不 ready |
| `hermes-opus` | `google-cloud/claude-opus-5-5` | Claude Opus 5.5 | 任意（Claude 走原生 Anthropic，其余走 OpenAI 兼容） | pin Hermes v0.22.2 + commit 校验 |
| `pi-opus` | `google-cloud/claude-opus-5-5` | Claude Opus 5.5 | 任意（Claude/Gemini 走原生 API） | pin Pi 1.1.0 |
| `agy-gemini` | `google-cloud/gemini-3.8-flash` | Gemini 3.8 Flash | **只有 Gemini** | pin Antigravity CLI 1.4.10，走原生 Gemini API |

### 1.2 核心策略（单轮 / 多轮 / self-check / plan）

| 样例 | 循环形态 | 轮次 | plan-then-execute | self-check / 验证回路 | 结束条件 |
|---|---|---|---|---|---|
| `minimal` | 单会话 tool loop，3 工具 | 单轮连续 | ✗（仅系统提示"先读任务和文件"） | ✗ 只在 prompt 里要求"写完再打开检查" | 模型不带 tool 回复；空回复/截断最多催 3 次 |
| `plan-verify` | **三角色三会话**：planner → worker → verifier(+fix) | 多轮 | ✔ **显式 planner 阶段**（只读 bash+read_file，产出交付物/路径/格式/方法/校验方式） | ✔ **独立 verifier 会话**，干净上下文（只有 task+plan+worker 摘要），只读工具，回 `PASS` 或编号缺口；缺口回灌 worker，`MAX_FIX_ROUNDS=2` | verifier PASS / 时间不够 / 2 轮修完 |
| `node-protocol` | 单会话 tool loop，3 工具 | 单轮连续 | ✗ | ✗ | 同上（MAX_NUDGES=3） |
| `custom-openai` | 单会话 tool loop，4 工具 + MCP 工具 | 单轮连续，但带 **compaction 断点** | ✗ 用系统提示的"How to work"清单替代（读全→小步→按任务声明的方式自检→写早→回读） | ✗ 无独立审计会话；只在 prompt 层要求回读与自检 | 模型不带 tool 回复（空轮最多 2 次）/ 到 finish_margin |
| `hermes-opus` | 借 Hermes（500 turns 上限、子代理、compaction 0.85） | 外部 agent 决定 | Hermes 内部 | Hermes 自带（token 花在自我验证上，官方 README 提到它的 wrap-up notice 让模型"停止验证并交付"） | Hermes 自己结束 / run_budget 到点被 kill |
| `pi-opus` | 借 Pi（--print --mode json，Pi 管 retry/compaction） | 外部 agent 决定 | Pi 内部 | ✗ 只有 `NOTE` 追加提示 | Pi 自己结束 / 距deadline 90s 被 kill |
| `agy-gemini` | 借 agy 子进程（自建 stall 看门狗 + `--conversation` 续跑） | 最多 `MAX_RUNS=4` 次 run | agy 内部 | ✗ | agy 结束 / 40 分钟无输出被 stall 杀 / 4 次 run 用完 |

### 1.3 代码量与构建复杂度

| 样例 | 自写代码（行 / 字节） | 文件构成 | 依赖 | build 命令风险 |
|---|---|---|---|---|
| `minimal` | **373 / 12.9 KB** | `serve.py` | openai==3.26.1, websockets==15.0.1 | 最低（uv + 2 包） |
| `plan-verify` | **518 / 18.2 KB** | `serve.py` | 同上 | 最低 |
| `node-protocol` | **412 / 21.1 KB** | `serve.mjs` | ws 8.22.0, undici 7.30.1 | 低（`npm ci`） |
| `custom-openai` | **1800 / 61.4 KB** | `serve.py`（单文件，27 个函数 + `Agent`/`Reply`/`MCPServer` 三个类） | openai, websockets | 低 |
| `local-model` | 0（≡ custom-openai） | 同上 + `host/Caddyfile` + `host/cloudflared.yml` + `local.env.example` | 同上 | 低，但需自建模型网关 |
| `hermes-opus` | **712 / 24.6 KB** | `serve.py` 366 + `task_env/__init__.py` 265（Hermes 终端后端插件）+ `thinking_patch.py` 81 | Hermes 全量（`uv sync --locked`，py3.14）+ websockets | **最高**：`git clone` → 校验 commit `d923a5e…` → 删 .git/tests/website → `uv sync` → 打源码 patch（锚点找不到就 build 失败）→ `compileall` |
| `pi-opus` | **550 / 19.2 KB** | `serve.py` 21 + `harness.py` 332 + `task-tools.ts` 197（Pi 扩展） | `@earendil-works/pi-coding-agent` 1.1.0 + websockets | 中（`npm ci` 拉整包 npm 依赖树） |
| `agy-gemini` | **1105 / 36.3 KB** | `serve.py` 571 + `task_mcp.py` 505（stdio MCP server）+ `install.sh` 29 | 下载 agy 二进制（sha512 校验）+ websockets | 中：外网下载二进制 + 校验和；`model_api` 锁死 Gemini |

### 1.4 协议能力矩阵（能提交则必须能跑）

| 能力 | minimal | plan-verify | node-protocol | custom-openai | hermes | pi | agy |
|---|---|---|---|---|---|---|---|
| 任务容器 `exec` | ✔ | ✔ | ✔ | ✔ | ✔（终端后端） | ✔（TS 扩展） | ✔（MCP `shell`） |
| `read_file`/`write_file` | ✔ | ✔ | ✔ | ✔ | ✔（走终端工具） | ✔ | ✔ |
| **任务 MCP 服务器** | ✗ | ✗ | ✗ | ✔ 双档 | ✔ | ✔ | ✔ |
| 任务 MCP `http` 通道 | ✗ | ✗ | ✗ | ✔ | ✔ | ✔ | ✔ |
| 流式模型调用 | ✗ | ✗ | ✔（手写 SSE） | ✔ | 外部 | 外部 | 外部 |
| 上下文压缩 | 仅 400 截断 | 仅 400 截断 | 仅 400 截断 | **两级 + 摘要 notes** | 外部 0.85 | 外部 | 外部（token 预算） |
| 从 400 学 context 窗口 | ✗ | ✗ | ✗ | ✔ `WINDOW_RE`/`PROMPT_RE` | — | — | — |
| `max_tokens`/`reasoning_effort` 自动回退 | ✗ | ✗ | ✗ | ✔ | — | — | — |
| Responses API（gpt-6） | ✗ | ✗ | ✗ | ✔ | — | — | — |
| cancel 语义 | taskbridge | taskbridge | 自写 | taskbridge | taskbridge | taskbridge | taskbridge |
| `kit check` 结果 | mcp 阶段 WARN | mcp 阶段 WARN | mcp 阶段 WARN | 全绿 | 全绿 | 全绿 | 全绿 |
| 可提交性 | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔（仅 Gemini） |

---

## 2. 逐个样例要点（读后笔记）

### 2.1 `minimal`（373 行，Gemini 3.8 Flash）
- 全部逻辑在 `solve()`：system + instruction + 3 工具（bash / read_file / write_file），模型不带 tool 就收工。
- 值得抄的小而硬的东西：
  - `WRAP_UP_SEC = 240`：到点前 4 分钟插一条 "Time is almost up…" 并 `tool_choice="none"`（`serve.py:38-42`）。
  - 每个 tool result 末尾追加 `[N minutes left]`（`serve.py:366-369`）——把 deadline 变成模型能看见的信号，而非只靠 harness 掐。
  - 工具结果中段截断到 20 000 字符（`clip`，头尾都留）。
  - 400 上下文超限 → 把"较老的一半长 tool result"砍到 1 000 字符再重试（`shorten`，`serve.py:251-279`）。
  - Claude 缓存标记：只在 system 和第 1 条以外的最新一条打 `cache_control`（2 / 4 个断点，`serve.py:138-167`）——不打就是每轮全价。
  - 助手消息用 `message.model_dump(exclude_none=True)` 原样回传（`serve.py:340`），否则 **Gemini 会因缺 thought signature 拒收**。
- 明确省略：MCP、流式、摘要式压缩（官方 README 自己写的）。

### 2.2 `plan-verify`（518 行，Claude Opus 5.5）
- 把 `minimal` 的 loop 抽成 `agent(session, messages, tools, until, final=False)`，然后三角色各跑一段：
  - planner：`READ_ONLY = TOOLS[:2]`（只有 bash、read_file），硬约束"不许建/改文件"（`serve.py:93,148`）。
  - worker：拿到 `task + "## Plan\n" + plan`。
  - verifier：**全新会话**，只有 `task + plan + worker 摘要`（不带 worker 的中间推理），只读工具，要求"回单个词 PASS，否则编号缺口"。
- 时间预算是最细的一处：plan 花到总时 10%、worker 撑到剩 25%、verify 到剩 12.5%、fix 到剩 4 分钟；每步开跑要求 `MIN_STEP_SEC=120` 余量（`serve.py:74-82, 447-471`）。
- PASS 判定做了容错：`verdict.strip("*_. \r\n\t").upper() in ("", "PASS")`（`serve.py:499`）——模型爱加 `**PASS**` 或句号。
- 缺口回灌后 `summary = await agent(...) or summary`，避免修崩了丢摘要（`serve.py:511-513`）。
- 明确省略：MCP、流式。代价：3 段会话 ≈ 3 倍 token。

### 2.3 `node-protocol`（412 行 JS，Claude Opus 5.5）
- 不依赖 `taskbridge.py`，`serve.mjs` 自己开 `WebSocketServer({host:"0.0.0.0", port:$PORT, maxPayload:16MiB})`，自己生成 id、自己维护 `waiting` Map、自己 `cancel`。
- 这一份是**"协议到底要什么"的最好教材**：
  - `required()` 在发请求前拒空串 / NUL / 未配对代理（`serve.mjs:83-89`）——畸形帧累计 20 个就掉线。
  - Node 内置 `fetch` 静默 5 分钟断流 → 换成 `undici` 的 `EnvHttpProxyAgent`，`headersTimeout/bodyTimeout = 30min`（`serve.mjs:48-52`）。
  - 手写 SSE 重组 `readReply()`（`serve.mjs:215-280`）：合并 delta、按 index/id 归并 tool_calls、保 `reasoning_content` / Claude `thinking_blocks`（带签名才留）/ Gemini `extra_content`。
  - 流没给 finish_reason 也没 `[DONE]` → 当截断重试，不把半截回复当答案（`serve.mjs:266-270`）。
  - cancel 时 abort fetch、拒掉所有 pending、10 秒内发 `done`（`serve.mjs:134-139`）。
- 单轮策略与 `minimal` 等价，没有 MCP。

### 2.4 `custom-openai`（1800 行，默认 gpt-oss 120B；`local-model` 是同一份）
官方自称"最完整的参考 harness"，事实如此。四块能力：

1. **工具层**：`bash`（可传 `timeout_sec`，按 `exec_timeout_sec` 与"距 finish_margin 的剩余时间"双重夹紧，`serve.py:1571-1577`）、`read_file`（`\0` 探测 → 判二进制并让模型改用 bash，`1594-1598`）、`write_file`（`append`，用于分段写大文件）、`edit_file`（精确串替换，重复出现要 `replace_all`）。
2. **MCP 双档**：`tools/list` 的 schema 总量 < 40 000 字符且 < 128 个 → 直接当 function 暴露；否则只把"服务器→工具名"目录写进 system prompt，另给 `mcp_find`（按词打分排序）与 `mcp_call`（`serve.py:951-1050`）。这是对"某些任务 tools/list 几百个工具、响应 500 KB"的正解。
3. **上下文治理**（三级降级，`compact()`/`cut_unread()`，`1661-1779`）：先截"模型已读过的"老 tool result（保留最近 12 条）→ 仍不够就把前 N 轮摘要成 notes（`SUMMARY_PROMPT` 要求写清交付物路径/状态/数字/待办）→ 还不够就截"未读"结果。并且 `remedy()` 会按顺序尝试：少要输出 token → 压缩 → 再少要 token → 截未读（`1118-1152`）。
4. **provider 兼容层**（最"工程"的一段）：`max_completion_tokens`→`max_tokens` 回退、`reasoning_effort` 被拒就丢掉、输出上限被拒就减半、`/v1/chat/completions` 指路 `/v1/responses` 就切 Responses API（`gpt-6` 默认就切）、从 400 报文里 regex 抠出 context 窗口与 prompt token 数（`learn_context`，`1237-1251`）。
5. 其它：`finish_margin = min(180s, 剩余/4)`；流被 deadline 截断时把已流出的文本当摘要（`1373-1380`）；`estimate_prompt_tokens()` 按 4 字符/token 估；工具名清洗（`<|` 漏出的 chat-template token）与 64 字符截断去重。

### 2.5 `local-model`
`serve.py` 与 `custom-openai` **字节相同**（md5 `18704ba5…`）。差异只有 `submission.yaml`（`openai-compatible/` + `model_base_url` + `MAX_OUTPUT_TOKENS: 16000`，因为 gpt-oss 窗口 131 072）+ `host/Caddyfile`（Bearer 校验网守）+ `host/cloudflared.yml`。**不能提交**，但说明了"同一份 harness 可换任何模型"。

### 2.6 `hermes-opus`（712 行适配层 + Hermes v0.22.2）
- 路子：不改 Hermes，只给它一个**自定义终端后端** `task_env/`，Hermes 的所有 file 工具本来就跑在终端上，于是自动落进任务容器（`task_env/__init__.py:179-204`）。
- 两处必须打的补丁（否则答案质量明显掉）：
  - `thinking_patch.py`：Hermes 默认对非 `api.anthropic.com` 的 base_url **剥掉历史 thinking 块**，网关下等于每轮让 Claude 重推一遍；patch 注入早退，让 `HERMES_NATIVE_THINKING_BASE_URL` 匹配时返回 `"native"`；锚点必须恰好出现 1 次，否则 build 失败（fail-fast）。
  - `summary_route()`：Hermes 的 Anthropic 适配器拒绝非 Anthropic URL，压缩摘要会失败并丢中间轮 → 把压缩改走 OpenAI 兼容端点（`serve.py:165-187`）。
- 时间：`--run-budget = wrap_up/0.8`（Hermes 在 80% 处发 wrap-up 通知），`stop_before_sec=60`（`serve.py:115-130, 334-346`）。
- 关掉 19 个 toolset（web/browser/vision/memory/cron/clarify…，`serve.py:81-99`）。
- 坑位处理很硬：`cache/terminal-output` 放一个**空文件**，让 Hermes 不把长输出存本地文件（否则模型会去读 serve 容器的路径，`serve.py:288-291`）；`INLINE_BYTES=33_000`（100 KB 输出截断按 3 字节/非法 UTF-8 算）+ `ARG_BYTES=100_000`（Linux 单参数 128 KiB 上限）→ 超长命令改写脚本文件（`task_env/__init__.py:45-50, 130-148`）。

### 2.7 `pi-opus`（550 行：21 + 332 + 197）
- `serve.py` 只 21 行（`taskbridge.run(harness.solve)`），真正工作在 `harness.py`（每任务写 `models.json`/`settings.json`/`mcp.json` 到临时 HOME，`--print --mode json` 起 subprocess，事件流解析成 log/usage/final_message）。
- `task-tools.ts` 把 Pi 的 `read/write/edit/bash` 四个工具的操作面换成 relay：
  - `inTask()` 用 `Object.create(ctx, {cwd:{value:workdir}})` 继承 Pi 的 tool context 只改 cwd（`task-tools.ts:100-107`）——因为 Pi 的 cwd 是它自己的 scratch 目录，默认所有相对路径都会错。
  - 关掉 Pi 自带的 `grep/find/ls`：它们会在 serve 容器里跑（README 明说）。
  - `isLocalTemp()`：Pi 把长命令输出写本地 `/tmp/pi-bash-*.log` 再让模型读，这类读必须留在本地（`134-136, 145-152`）。
- `SETTINGS`：retry 8 次（2s→60s）、`httpIdleTimeoutMs=3 600 000`、`cacheWarming:"streaming"`（长命令期间保持 Claude 缓存热）。
- `COMPAT` 四项关掉（strict tools / mid-convo effort / mid-convo system message / mid-convo tool changes），因为 Google Cloud 那条 Claude 路由不收。
- `UNKNOWN_MODEL` 兜底（128k / 16k / 纯文本），否则 Pi 目录里没的模型没 limits。

### 2.8 `agy-gemini`（1105 行，Antigravity CLI 1.4.10，仅 Gemini）
- `agy` 自带工具会作用于 serve 容器 → 设 `tools: []`，改由**自建 stdio MCP server** `task_mcp.py` 提供 `shell/read_file/write_file/edit_file`，再经 `Relay.mcp_url()` 把任务自己的 MCP 服务器也挂进来。
- `task_mcp.py` 里两个值得抄的细节：
  - `read_file` 对 png/jpeg/gif/webp 返回 **MCP image block**，让 Gemini 直接"看"工程图纸；≤5 MB，超了让人先缩图（`296-312`）。
  - `write_file`/`edit_file` 共用一把 `threading.Lock`，避免并发编辑互相覆盖（`52, 348-385`）。
- 子进程管理最完整：`Watch` 看门狗（40 分钟无 stdout 就杀进程组，`335-368`）+ `resume_wait()` 区分"stall / 传输断 / 429"（stall 与断线立即续跑，429 等 `60s × n`）+ `--conversation <id>` 在同一会话续，最多 4 次（`MAX_RUNS`）。
- 其它坑：`MAX_ARGV_PROMPT=100_000`，超长 prompt 改走 `--input-format stream-json` 走 stdin（`132-149`）；`-byom` 自定义模型必须显式给 `maxTokens` 预算（agy 预留 ~70k）才会压缩；`TRANSPORT`/`RATE_LIMIT` 两个正则是对 Go 二进制错误文本的硬编码适配。
- 代价：eager 加载全部 MCP schema，官方 README 自己写"264 个工具 ≈ 每轮多 75k input tokens"。

---

## 3. 它们共同实现的协议细节 = harness 的「最低要求」

这 11 条是 7 份实现**无一例外**都做了的（`taskbridge.py` 里的是共享版；`node-protocol` 手写了一份等价物）。少一条就可能整题 0 分。

| # | 要求 | 证据/数值 |
|---|---|---|
| 1 | `0.0.0.0:$PORT` 起 WebSocket，**120 s 内**能接受连接，一连接一任务 | `taskbridge.serve_forever`（`:649-666`）；`serve.mjs:388` |
| 2 | 首帧必须是 `task`，读 `instruction` / `workdir` / `limits` / `mcp_servers` | `Session.__init__`（`:86-108`） |
| 3 | 请求自带唯一 `id`（≤128 字符、非空、无未配对代理），按 id 匹配 `result`；**最多 8 个并发**，超出会被评估器暂停读帧（连 `done` 都排在后面） | `protocol.md:41-48` |
| 4 | 四个原语：`exec` / `read_file` / `write_file` / `http`（后者的 URL 只能落在 `mcp_servers` 之下） | `Session.exec/read_file/write_file/http`（`:221-382`） |
| 5 | 容量限制：单帧 **16 MiB**、read/write **10 MB**（写超了要 `append` 分片，`WRITE_CHUNK=4 MiB`）、exec 输出 **100 KB** 头尾保留、`done.final_message` 截到 **1 MB** | `taskbridge.py:59-72`、`_frame`/`_final`（`:418-456`） |
| 6 | 送请求**之前**自校验：空串 / NUL / 非法 UTF-8 会被判畸形帧，累计 **20 个**结束会话 | `_text`（`:459-483`）；`serve.mjs` 的 `required()` |
| 7 | 单向消息 `log` / `usage` / `done` / `error`；**正常结束必须发 `done`**（含 `final_message`），否则任务算失败 | `_session`（`:550-647`） |
| 8 | `cancel` 必须处理：停在飞的模型调用，**10 秒内**发 `done`（可用已在磁盘的文件 + 手上最好的摘要） | `_session` 的 cancel 分支（`:621-641`）；`serve.mjs:134-139` |
| 9 | 模型侧只读环境变量：`MODEL_NAME`、`OPENAI_BASE_URL`/`OPENAI_API_KEY`（= `MODEL_BASE_URL`/`MODEL_API_KEY`）；**请求该模型最高 reasoning 档**（Claude/GLM/GPT-6/DeepSeek 用 `max`，Gemini 等用 `high`） | 7 份的 `EFFORT` 常量逐字相同 |
| 10 | 时间预算：用 `limits.agent_timeout_sec` 建 deadline，**留 60–240 s 收尾**，把剩余时间写进 prompt/tool result | `time_left()`；`WRAP_UP_SEC=240`、`STOP_BEFORE_SEC=60/90`、`FINISH_MARGIN_SEC=180` |
| 11 | 交付物纪律：先写盘再 `done`（grader 读文件 + `final_message`，进程会在收尾时被杀） | `SYSTEM` prompt（`minimal:72-76`，`custom-openai:132-147`） |

顺带（非协议但所有能用的实现都会做）：**并发安全**。多任务同时跑、同一 harness 只读挂载，每任务独立临时 HOME / 独立 session。

---

## 4. 哪些地方体现「工程性问题需要特殊处理」

| 工程问题 | 表现 | 各样例的处理（谁做了 / 谁没做） |
|---|---|---|
| **文件读取：二进制 vs 文本** | 交付物有 `.ifc`/`.zip`/`.xlsx`/`.docx`，`read_file` 直接解 UTF-8 会吐垃圾或炸 | ✔ custom-openai 用 `\0` 探测前 8 KB 并让模型改用 bash（`:1594`）；✔ agy `task_mcp.py` 同样探测 + 图片走 MCP image block；✔ hermes 所有 file 工具天然走终端；✘ minimal/plan-verify 直接 `decode("utf-8","replace")` |
| **大输出 / 100 KB 截断** | exec 超 100 KB 中段被砍，模型以为命令没输出 | ✔ hermes 把输出重定向到容器临时文件，超 33 KB 再 `read_file` 取回（`task_env:105-176`）；✔ custom-openai 提示"写进文件再分段读"；✔ pi 识别自己的本地临时日志文件；其余只靠截断提示 |
| **长上下文（6 小时 + CAD 目录）** | 6 小时任务、上百次工具调用，prompt 必爆 | ✔ custom-openai 三级降级 + 从 400 学窗口；✔ hermes 0.85 阈值压缩（但需绕开 Anthropic 适配器限制）；✔ pi/agy 靠各自 compaction（agy 要给 `-byom` 显式预算）；△ minimal/plan-verify/node 只有"400 时砍老结果"这一档 |
| **CAD / BIM / IFC、Office 二进制** | `.ifc`、`submission.zip`、`.xlsx` 交付物 | ✔ 只有 custom-openai 的 SYSTEM 明确写："office 与二进制格式用代码（python3 + 容器里的库）生成真实文件"（`:141-142`）；其余靠模型自觉。**没有任何样例做 IFC 语义校验** |
| **单位/量纲换算** | 结构/电气/机械题几乎全是单位陷阱 | ✘ **全都没有程序化处理**，只有 custom-openai SYSTEM 一句 "check units"。这是明显的可加分区（如：让 verifier 阶段跑一个量纲一致性脚本） |
| **验证回路** | checks 类任务有硬约束（违反即封顶 0.4），rubric 类要看完整性 | ✔ plan-verify 有独立只读 verifier + 2 轮 fix；✔ hermes 的 wrap-up 语义（"停止验证，交付"）说明它内部会自我验证；✘ 其余靠 prompt。**推荐把 plan-verify 的 verifier 补到 custom-openai 上** |
| **MCP 工具爆炸** | 某些任务 `tools/list` 几百个、响应 500 KB | ✔ custom-openai 双档（直暴露 vs `mcp_find`/`mcp_call`）；✔ hermes/pi 交给各自 agent 的 MCP 客户端；△ agy eager 全量（每轮多 ~75k input tokens） |
| **模型 provider 怪癖** | 这些是"看不见但会致命"的工程债 | ① Gemini thought signature 必须原样回传（minimal `model_dump`、custom-openai `extra_fields`、node `merged`）② Claude signed `thinking_blocks` 不能丢也不能改（custom-openai `drop_thinking(unless=signed)`）③ DeepSeek 要回 `reasoning_content` ④ Claude 只认打了标的 4 个缓存断点，样例只用 2 个 ⑤ `max_completion_tokens` ↔ `max_tokens` ⑥ `reasoning_effort` 可能被拒 ⑦ gpt-6 只能走 Responses API ⑧ 部分端点拒 `strict` tools / 中途改 system |
| **长静默（max effort 一次 15 分钟）** | 非流式调用可能 15 分钟无字节；Node 内置 fetch 5 分钟断 | ✔ minimal/plan-verify 用 `timeout=1800`；✔ node 换 `undici` 30 分钟；✔ hermes/pi 显式抬 request/stream-idle 超时；✔ custom-openai `timeout=900` + 流式 |
| **超长指令 / 超长参数** | 任务书可能很长，Linux 单参数上限 128 KiB | ✔ agy 转 stdin（`--input-format stream-json`）；✔ hermes 转脚本文件；✔ 自己写 loop 的把 instruction 放 messages，天然无此问题 |
| **子进程 agent 的进程组回收** | 包 agent 的样例必须保证到点杀干净，否则输出被后续写入污染 | ✔ `taskbridge.run_process` `start_new_session=True` + `killpg(SIGTERM→KILL)` + `stop_before_sec`；✔ agy 额外加 `Watch` 看门狗与 `MAX_ARGV` 处理 |
| **并发/幂等** | 8 并发请求、write 分片 append、并发编辑 | ✔ `taskbridge.Relay` 一请求一连接 + 明确 HTTP framing 校验；✔ agy 用锁串行 write/edit |
| **无网环境** | serve 容器只能到模型端点；task 容器完全无网 | 所有样例都只调 `MODEL_BASE_URL`；`Relay` 明确把 `127.0.0.1` 加进 `NO_PROXY` 绕开出网代理（`taskbridge.py:726-741`） |

---

## 5. 推荐起点：`custom-openai` + 移植 `plan-verify` 的 verifier

### 5.1 结论
**从 `sample_submissions/custom-openai/` 起步**（`cp -r` 出去），第一轮改造只做一件事：把 `plan-verify` 的 `agent()` / `conversation()` / verifier 段搬进来，在 `Agent.finish()` 之前插一轮"只读审计 + 最多 1 次修复"。

不要选 `pi-opus`（尽管仓库 `README.md` 的 Quick start 用的是它）——那是"最快得到一个能跑的提交"的路径，不是"最高分"的路径。

### 5.2 为什么是 `custom-openai`

1. **它是唯一"完整度接近包 agent、但代码仍归我们"的样例。**
   MCP 双档发现、流式、两级压缩、provider 兼容层、deadline 夹紧、400 补救全都在一个 1800 行的文件里，函数边界清楚（`Reply` / `Agent` / `MCPServer`）。要改 prompt、加工具、调预算，改 10 行就生效。
   反过来，hermes/pi/agy 的行为埋在 vendor 代码里：hermes 甚至需要在 install 阶段**打源码 patch**（锚点变了 build 就失败），pi 的 compaction/prompt 是黑盒，agy 是闭源二进制。

2. **MCP 是硬需求，而 3 个"小而美"的样例都没有。**
   `minimal` / `plan-verify` / `node-protocol` 都明确省略 MCP（`kit check` 会在 `mcp` 阶段 WARN）。但 dev set 的交付物里直接出现 `/app/tools/.apps_data/`（`engineers-estimate-check`、`footing-settlement-verification`、`solar-supplemental-review`、`solar-witness-test-package`），说明这些题带 apps（文档/表格类）MCP 服务器；缺 MCP 等于这些题只能用 bash 硬造，白白丢分。

3. **6 小时 + 大文件 ⇒ 上下文一定会爆，只有它有三档降级。**
   `compact()` 先截已读、再摘要成 notes、`cut_unread()` 截未读，还能从 400 报文里学窗口大小反向调 `compact_at()`/`output_limit()`。`plan-verify` 只有"400 时砍一半老结果"，跑 IFC/xlsx 这类型任务会撞墙。

4. **模型可 A/B，成本最低。**
   同一份 `serve.py` 在 `local-model` 里原封不动跑自建 vLLM（字节相同），说明它对任何 OpenAI 兼容端点都成立。我们要在 `claude-opus-5-5` / `gemini-4-argon` / `qwen3-coder-480b` 之间做选题是有决定意义的实验——换模型只改 `submission.yaml`，不用碰 harness。

5. **它有"工程交付物"意识，正好对上 PLE 的评分方式。**
   SYSTEM 里已经写了：按任务声明的检查方式自检、用代码生成 docx/xlsx/pdf、先写早再改、收尾回读每个交付物。这几句在 `minimal` 里只有一句笼统的"打开再看看"。

6. **加 verifier 的改造成本很低、收益很直接。**
   `plan-verify` 的 `agent()` 已经是"带 step deadline 的通用 loop"，`conversation()` 只是拼 system+user；插进 `custom-openai` 大致 150 行：在 `run()` 里 `finish_margin` 前留出 ~12.5% 时间，起一个只给 `bash` + `read_file` 的干净会话（只有 task + 交付物清单 + worker 摘要），要它回 `PASS` 或编号缺口，缺口拼成一条 user 消息回灌主会话再跑一轮。这正是评分规则里"checks 类任务硬约束一违反就封顶 0.4"最需要的保险。

### 5.3 不选另外几家的具体原因

| 候选 | 为什么不当起点 |
|---|---|
| `minimal` | 太薄：无 MCP、无流式、无压缩，加回来等于重写 `custom-openai`。适合"读懂协议"，不适合"直接往上堆分" |
| `plan-verify` | 策略最好（唯一显式验证闭环），但同样是 minimal 的底子：无 MCP、无流式、压缩只有一档；而且 3 段会话 ≈ 3 倍 token。**当"零件库"用，不当底座** |
| `node-protocol` | 价值在"协议直写"的参照；JS 生态里没有 `taskbridge` 那套 Relay/MCPClient，要自己补一堆。除非我们想用 TS 写 harness |
| `local-model` | 同 `custom-openai`，但 `openai-compatible/` + 自建网关**不能提交** |
| `hermes-opus` | 成熟 agent（子代理、500 turns、压缩）确实强，但：build 最重（clone + commit 校验 + 源码 patch + `uv sync` py3.14）、行为不可控、patch 锚点随版本漂移就 build 失败。**作为"对照组"值得跑一次看分数上限** |
| `pi-opus` | 同上，且需要绕 Pi 的 cwd/临时文件语义（`task-tools.ts` 里那些 `Object.create`、`isLocalTemp` 就是坑的化石）。Quick start 选它是因为"拷完就能跑"，不是因为它分高 |
| `agy-gemini` | 只有 Gemini、闭源二进制、eager 全量 MCP schema（每轮多 ~75k input tokens）。它的 `Watch` + `resume_wait` + 图片 MCP block 三个设计值得抄进我们的实现 |

### 5.4 落地路线（建议 3 步，每步都可验证）

1. **基线（当天可完成）**：`cp -r ple/sample_submissions/custom-openai ../my-harness`，`model_api` 换成 `google-cloud/claude-opus-5-5`（rubric 类任务占多数，长文写作 Opus 更稳），跑 `uv run python -m kit check ../my-harness --cancel-test`，要求 smoke / mcp / cancel 全绿。
   *不要改 `taskbridge.py`* —— 协议层已被 kit 验证过，改了只会引入低级失分。
2. **加 verifier**：移植 `plan-verify` 的 verifier（只读工具、干净上下文、`PASS` 容错判定、1 轮 fix）。用 `eval/run.py --tasks engineers-estimate-check,footing-settlement-verification,solar-supplemental-review` 这种 rubric 题小样本先量收益，再决定是否上到 29 题。
3. **补 PLE 特有钩子**（按收益排序）：
   - 交付物清单化：SYSTEM 里要求 "结束前对每个交付物执行一次 `ls -l` + 打开回读"，并把它变成 verifier 的检查项之一；
   - 单位/量纲自检：让 verifier 用一段固定脚本核对结果量级（这是所有样例的空白区）；
   - 二进制交付物：把 `submission.zip` / `.ifc` / `.xlsx` 的生成方式写进 prompt（custom-openai 已有雏形），并要求生成后 `python3 -c` 打开验证。

---

## 6. 附：核对用的事实清单

- 8 个目录：`agy-gemini`、`custom-openai`、`hermes-opus`、`local-model`、`minimal`、`node-protocol`、`pi-opus`、`plan-verify`；另有共享 `taskbridge.py`（1 528 行）与 `README.md`。
- 7 份 `taskbridge.py` 完全相同：md5 `254c7cb533b60eacdc214c183c89bf28`。
- `custom-openai/serve.py` 与 `local-model/serve.py` 完全相同：md5 `18704ba59182ffaa3443dc8aa0a33fd7`，61 423 字节。
- 自写代码行数：minimal 373 / plan-verify 518 / node-protocol 412 / custom-openai 1 800 / hermes-opus 712 / pi-opus 550 / agy-gemini 1 105。
- dev 29 题，`time_limit_min` 全为 360（6 小时）；评分类型分布（`checks` / `checks-judge` / `rubric`）三种并存，交付物路径含 `.py`、`.zip`、`.ifc`、`/app/files/`、`/filesystem/`、`/app/tools/.apps_data/`。
- 本笔记只读 `ple/`，未修改其中任何文件。
