# `custom-openai` 移植 + 改造方案（→ 带验证器自检的 harness）

> 基线：`ple/sample_submissions/custom-openai/`（`serve.py` 1 800 行，`taskbridge.py` 1 528 行，与全部 Python 样例字节相同 md5 `254c7cb5…`）。
> 对照：`NOTES-kit.md`（协议/评分手册）、`NOTES-samples.md`（8 样例横向对比）。
> 本文只读 `ple/`，**未修改其中任何文件**；落地时新建独立仓库（建议 `/Users/a0000/.dsh/work/ple/harness/`，与 `ple/` 平级），把 `ple/sample_submissions/custom-openai/` 拷出去改。
> 结论先行：**必需改动 15 项 ≈ 310 行（其中 45 行是整段可粘的提示词），可选 5 项 ≈ 100 行；最难的 1 项是「审计会话与主会话的状态归属分离」（E3+E4/E5/E6+E7 的组合），不是行数问题，是隐性耦合问题。**

---

## 0. TL;DR

| 问题 | 答案 |
|---|---|
| 底座 | `custom-openai`：唯一同时具备 MCP 双档、流式、三级压缩、provider 兼容层、deadline 夹紧的样例（2 180 行含 taskbridge） |
| 主循环在哪 | `Agent.run()`，`serve.py:823-917`；`while True` 在 `851-917`，出口 4 个（自然完成 / 输出截断续写 / 空轮 nudge 用尽 / deadline → `finish()`） |
| 三级压缩 | `compact()` `1661-1751` + `cut_unread()` `1753-1779`，触发点 `916-917`（`prompt_tokens > compact_at()`）；三级 = 截已读 → 摘要 notes → 截未读 |
| MCP 双档 | `connect_mcp()` `951-1008`；判据 `<40 000` 字符 schema 且 `≤128` 个工具 → 直暴露，否则 `mcp_find`/`mcp_call` |
| 反推窗口 | `learn_context()` `1237-1251` + `WINDOW_RE` `82-88` / `PROMPT_RE` `89-93`，从 400 文案里抠 `context_window` 与 `prompt_tokens` |
| deadline 夹紧 | 5 处：`finish_margin`（`833-837`）、循环首检（`852-854`）、`stream` 的 `wait_for(left-reserve)`（`1372`）、重试禁界（`1106-1112`）、`bash` cap（`1571-1577`） |
| 改成验证器 harness | 先做「worker + 只读审计（+最多 2 轮修复）」双阶段；**不**移植完整 planner（见 §8.1） |
| 改动清单 | E0–E15 必需（§10.1，15 项），E16–E20 可选（§10.2，5 项） |
| 最难一项 | **E3/E4/E5/E6/E7 的联合体**：让 `complete/stream/record/remedy` 能在「第二会话」上安全复用——因为 `remedy()`→`compact()`/`cut_unread()`/`learn_context()` 全部隐式绑定 `self.messages` 与 `self.prompt_tokens`，审计会话一旦撞 context-full，会把 **worker 的历史压掉**。详见 §8.3 |

---

## 1. 移植基线（一次性动作，0 行代码）

```bash
cd /Users/a0000/.dsh/work/ple
cp -r ple/sample_submissions/custom-openai harness     # 不要 cp local-model（不可提交）
cd harness && git init
rm -f README.md                                        # 官方说明，换成自己的
```

必改三件：

| 文件 | 改什么 | 理由 |
|---|---|---|
| `submission.yaml` | `model_api: google-cloud/claude-opus-5-5`；`env:` 删掉 `COMPACT_AT_TOKENS: "90000"`（回落到默认 150 000） | 原值是为 gpt-oss 131 k 窗口手调的；Opus 5.5 窗口更大，90 k 会**过早**压缩、白丢上下文（`serve.py:61`） |
| `.gitignore` | 保留原 4 条（`*.env`、`.venv/`、`__pycache__/`、`runs/`）；**不要**忽略 `artifacts/` | `artifacts/` 必须随同一 commit 提交 |
| `requirements.txt` | 原样：`openai==3.26.1` / `websockets==15.0.1`（钉版本） | 评测端复现用 |

`taskbridge.py` **一行都不要改**——协议层已被 `kit check` 的 smoke/listen/cancel 三阶段验证过；`NOTES-kit.md §12.A` 列的 8 条协议纪律（永远发 `done`、`ping_interval=None`、120 s 内 bind、in-flight 收敛、`id` 唯一、帧上限 16 MiB、`cancel` 处理、别把本地错误变成 malformed）全部已实现，改动只有风险没有收益。

---

## 2. 代码结构地图

`serve.py` 可调用单元实测：**15 个模块级函数 + 7 个类 + 34 个方法 = 56 个**（`NOTES-samples.md §2.4` 写的「27 个函数 + 3 个类」是估算，实际类有 7 个：`MCPServer` / `Reply` / `Agent` / `OutOfTime` / `CutOff` / `StreamBadRequest` / `StreamCut`；其中 4 个是异常类）。

### 2.1 常量与提示词（`43-161`）

| 行 | 单元 | 值 / 作用 | 改造 |
|---|---|---|---|
| 43–50 | `MODEL` / `MAX_EFFORT_MODELS` / `EFFORT` | `MODEL_NAME` 环境变量；`claude/glm/gpt-6/deepseek-v4` 用 `max`，其余 `high` | 不动 |
| 54–57 | `RESPONSES_MODELS` / `RESPONSES_ONLY` | gpt-6 强制走 `/v1/responses`，其他模型 400 指路时切 | 不动（换 Opus 后此分支不触发） |
| 60 | `CACHE_MARK` | `"claude" in MODEL` → 打 `cache_control` 标记 | 不动（换 Opus 自动生效，省钱关键） |
| **61** | `COMPACT_AT_TOKENS` | 默认 150 000；样例用 env 覆盖成 90 000 | **E0**：删 env 覆盖 |
| **62** | `TOOL_RESULT_CHARS` | 30 000 字符，单条工具结果 head/tail 截断 | 不动 |
| **64** | `DIRECT_MCP_CHARS` | 40 000，MCP schema 直暴露上限 | 不动（可调，见 E19） |
| **66** | `MAX_TOOLS` | 128（OpenAI 单请求函数上限，含内置 4 个） | 不动 |
| **67** | `KEEP_RECENT_RESULTS` | 12，一级压缩保留的最近工具结果条数 | 不动 |
| **68** | `FINISH_MARGIN_SEC` | 180，收尾窗口秒数 | 不动（E14 复用作审计余量） |
| **69** | `RETRY_WAITS` | `(5,15,30,60,120)`，共 5 次 | 不动 |
| 73–79 | `CONTEXT_FULL` | 认「context 超限」的正则（xAI/vLLM/OpenAI/Anthropic 各种说法） | 不动 |
| **82–88** | `WINDOW_RE` | 从 400 文案里抠**窗口大小** | 不动（§7） |
| **89–93** | `PROMPT_RE` | 从 400 文案里抠**prompt token 数** | 不动（§7） |
| 94 | `MIN_OUTPUT` | 1024，最小输出请求 | 不动 |
| **96** | `UNREAD_KEEP` | 600，未读结果硬保底字符数 | 不动 |
| 98 | `CONTEXT_MARGIN` | 1024，窗口留白 | 不动 |
| 101–106 | `UNSUPPORTED` | 认「参数不被支持」的正则（与「给了更小上限」区分开） | 不动 |
| 111–123 | `CALL_FIELDS` / `FUNCTION_FIELDS` / `DELTA_FIELDS` | 流式 delta 的标准字段集；之外的一律原样回传（Gemini thought signature 靠这个活） | 不动 |
| 125–130 | `client` | `AsyncOpenAI(max_retries=0, timeout=900)`；重试由 `complete()` 自己做 | 不动（E4 若要给审计单独超时再改） |
| 132–147 | `SYSTEM` | 主系统提示（工具说明 / How to work 8 条 / 收尾要求 / `{minutes}`） | **E15** 加 6 行 |
| 149–151 | `MCP_DIRECT` | 直暴露档的系统提示尾巴 | 不动 |
| 153–157 | `MCP_CATALOG` | 两段式档的系统提示尾巴（含服务器→工具名目录） | 不动 |
| 159–161 | `SUMMARY_PROMPT` | 二级压缩的 notes 指令 | 不动 |
| — | `AUDITOR` / `FIX_PROMPT` / `CHECKLIST` | **不存在** | **E11** 新增（§9） |

### 2.2 纯函数层（`164-563`）

| 行 | 单元 | 职责 | 改造 |
|---|---|---|---|
| 164–189 | `tool()` | 拼 OpenAI function 定义（`name/description/required/**properties`） | 不动 |
| 192–238 | `TOOLS` | 4 个内置工具：`bash` / `read_file` / `write_file`(含 `append`) / `edit_file`(含 `replace_all`) | 不动 |
| 239–257 | `MCP_TOOLS` | `mcp_find(query)` / `mcp_call(tool, arguments)` | 不动 |
| 260–276 | `clip()` | 中段截断：前 2/3 + `[... N characters cut ...]` + 后 1/3 | 不动 |
| 279–310 | `parse_arguments()` | 解析 tool call 的 JSON；**失败也把 `call["arguments"]` 重写成 `"{}"`**（否则严格端点会拒整段历史），返回错误串给模型 | **E7** 复用 |
| 313–327 | `unique_name()` | 64 字符截断 + `_2/_3` 去重（MCP 工具名冲突用） | 不动 |
| 330–339 | `visible()` | 剥掉 `<think>…</think>`，`.strip()` | **E7/E14** 复用（判定 PASS） |
| 342–355 | `extra_fields()` | 取 `model_extra` 里非标准字段 | 不动 |
| 358–378 | `merged()` | delta 合并：dict 递归、list 拼接、标量覆盖 | 不动 |
| 381–413 | `thinking_blocks()` | 累积流式 Claude thinking 块，遇 `signature`/`data` 收块 | 不动 |
| 416–427 | `signed()` | 块是否带 `signature`/`data`（Claude 只认带签名的） | 不动 |
| 430–454 | `drop_thinking()` | 按谓词过滤 `thinking_blocks`（`provider_specific_fields` 里那层也过滤） | 不动 |
| **457–487** | `marked()` | 仅 Claude：给 `messages[0]` 与 `messages[-1]` 打 `cache_control:ephemeral`（4 个断点里用 2 个） | 可选 **E17** 加第 3 个断点 |
| 490–540 | `response_input()` | Chat Completions 历史 → Responses API `input`（含 `output_items` 回放） | 不动 |
| 543–563 | `chat_usage()` | Responses usage → Chat usage 形状 | 不动 |
| 770–789 | `stream_error()` | 把流内错误分成 `StreamBadRequest`（不重试，走 `remedy`）与 `StreamCut`（重试） | 不动 |

### 2.3 三个正类

| 行 | 类 | 职责 |
|---|---|---|
| 566–607 | `MCPServer(taskbridge.MCPClient)` | `start()`=`initialize()`+`list_tools()`；`call()` 把 MCP `content` 数组（text / resource / resource_link / 其他）拍平成文本，`isError` 前置 `error: ` |
| 610–740 | `Reply` | 流式累积器。`add_call()`（624–689）按 `index`→`id`→「续上一个」三级归并 delta；`_another_call()`（691–712）判断无 id 的 delta 是否开启新调用；`message()`（714–740）产出历史消息并 `drop_thinking(unless=signed)` |
| 800–1779 | `Agent` | 全部状态与循环，见下表 |

### 2.4 `Agent` 的 29 个方法（按调用关系分组，不是行号顺序）

| 行 | 方法 | 职责 | 改造 |
|---|---|---|---|
| 803–821 | `__init__` | 工具表、MCP 表、消息、`prompt_tokens`、`output_ceiling`、`context_window`、`token_limit`、`effort`、`responses`、`finish_margin` | **E2** |
| **823–917** | **`run()`** | **主循环**（§4）；内含 MCP 连接、system 组装、`finish_margin` 计算、nudge 逻辑、压缩触发 | **E3**（拆分） |
| 919–949 | `finish(why)` | 收尾：追加「总结」user 消息，`tool_choice="none"`、`reserve=left-max(5,time_share(left,30))`；失败回退 `session.final_message` | **E3** 复用 |
| **951–1008** | `connect_mcp()` | MCP 双档发现（§6） | 不改，**E9/E10** 复用其结果 |
| 1010–1050 | `find_mcp(query)` | 打分排序取前 8（含嵌套 `score` 1024–1031），按 30 000 字符预算逐条弹 | 不改 |
| **1052–1116** | `complete(tool_choice, reserve, head)` | 重试外壳：400 且 `remedy()` 成功 → 重试；408/409/429/5xx/连接错/`StreamCut` → 退避；退避前检查「等完还剩多少」 | **E4** |
| **1118–1152** | `remedy(error)` | 400 分流：context-full → 4 步降级；否则 4 个单点修复 | **E4/E7**（加会话参数） |
| 1154–1166 | `use_responses()` | 切 Responses API | 不动 |
| 1168–1184 | `use_max_tokens()` | `max_completion_tokens` → `max_tokens` | 不动 |
| 1186–1201 | `drop_effort()` | 端点拒 `reasoning_effort` 就丢掉 | 不动 |
| 1203–1235 | `shrink_output()` | 抓「上限是 N」并把 `output_ceiling` 降到 N（或减半） | 不动 |
| **1237–1251** | `learn_context()` | 反推窗口 + prompt tokens（§7） | **E4**（加会话参数） |
| 1253–1264 | `room()` | `window - prompt - 1024` | 不动 |
| 1266–1278 | `output_limit()` | `min(output_ceiling, room)`，不低于 1024 | 不动 |
| 1280–1293 | `compact_at()` | 阈值；已知窗口时按窗口收窄（§5.1） | 不动 |
| 1295–1310 | `estimate_prompt_tokens()` | JSON 长度 / 4，跳过 `output_items` | 不动 |
| **1312–1397** | `stream(messages, tool_choice, reserve, head)` | 真·单次调用；支持 Chat 与 Responses；`wait_for(left - reserve)`；`CutOff` 时把已流出文本写进 `final_message`（1373–1380） | **E5** |
| 1399–1467 | `respond()` | Responses API 事件流（`response.output_text.delta` / `completed` / `incomplete`） | 不动 |
| **1469–1479** | `record(reply)` | 用 usage 更新 `prompt_tokens`（无 usage 时用估算），并 `report()` | **E6** |
| 1481–1498 | `report(reply)` | 发 `usage` 消息给评测端 | 不动（审计也要发） |
| **1500–1543** | `run_tool(name, args)` | 分发：`mcp_call` 拆包 → 直连 MCP → `mcp_find` → 4 个内置；异常转字符串 | **E12** 只读闸门 |
| 1545–1558 | `path()` | 相对路径拼 `workdir` | 不动 |
| **1560–1582** | `bash()` | `cap = min(exec_timeout_sec, left - finish_margin)`，再 `positive_int(timeout_sec,120,cap)` | **E13** |
| 1584–1599 | `read_file()` | 前 8 KB 有 `\0` → 判二进制并让模型改用 bash | 不动 |
| 1601–1617 | `write_file()` | 支持 `append` | 不动 |
| 1619–1647 | `edit_file()` | 精确串替换；0 次报错、>1 次要求 `replace_all` | 不动 |
| **1649–1659** | `unread()` | 最后一条 assistant 之后的 `tool` 消息 | 不动 |
| **1661–1751** | `compact(force)` | 三级压缩的 1、2 级（§5） | 不改（E4 只加门禁） |
| **1753–1779** | `cut_unread()` | 三级压缩的第 3 级（§5） | 不改 |
| 1782–1796 | `solve(session)` | 打印一行启动信息 → `Agent(session).run()` | 不动 |
| 1799–1800 | `__main__` | `taskbridge.run(solve)` | 不动 |

---

## 3. 主循环长什么样（`run()` `823-917`）

### 3.1 一次性准备（829–848）

1. `mcp = await self.connect_mcp()` —— 先连 MCP，返回要拼进 system 的那段（空串 = 无 MCP）。
2. `left = time_left()`；`finish_margin = 180 if left is None else min(180, max(left,0)/4)`。
   - 即：长任务固定留 3 分钟；**短于 12 分钟的任务**留 `left/4`（不会把自己收尾的窗口吃光）。
3. `self.messages = [system(SYSTEM.format(workdir, mcp, minutes=int(left/60))), user(instruction)]`。
   - 注意 `minutes=int(left/60) if left else "unlimited"`（844）：`left == 0.0` 也会被当成 unlimited（**小 bug，见 §13.2**）。

### 3.2 循环（851–917）

```
851  while True:
852    left = time_left()
853    if left < finish_margin:            → return finish("Time is almost up.")
856    reply = await complete(head=head)    ← 唯一的模型调用点
       ├ CutOff      → return finish("Time is almost up.")       # 流被 deadline 砍断
       └ OutOfTime   → return finish("…and the model is not answering.")
864    arguments = [parse_arguments(c) for c in reply.calls.values()]
865    if reply.text or reply.calls:  messages.append(reply.message())
870    text = visible(head + reply.text);  if text: final_message = text
873    if not reply.calls:                                     # ← 模型说「我做完了」
874      if visible(reply.text) and finish == "length":        # 输出被上限砍断
877        nudges, head = 0, head + reply.text
878        append user("…cut off at N tokens; continue, put long content in files")
886        continue
887      if visible(reply.text) or nudges >= 2:  return text    # ← 正常出口
889      nudges += 1; append user("Continue the task; …")       # ← 空轮最多催 3 次
898    nudges, head = 0, ""
899    for call,args in zip(reply.calls.values(), arguments):   # ← 工具串行执行
901      result = await run_tool(name, args)  或  参数解析错误串
905      if finish == "length" and 参数坏: result += "…write large files in parts"
910      messages.append({"role":"tool","tool_call_id":call["id"],"content":result})
913    left = time_left()
915    if left is not None: messages[-1]["content"] += f"\n[{int(left//60)} minutes left]"
916    if self.prompt_tokens > self.compact_at():  await self.compact()
```

四个出口：**① 自然完成**（887，返回 `text`）**② 输出截断续写**（886，累积 `head` 重来）**③ 空轮 nudge 用尽**（887，`nudges>=2`）**④ 时间到**（854/862/863，走 `finish()`）。

### 3.3 几个容易看漏的细节

- **工具是串行的**（899–912 `for` 里 `await`）。`max_output_bytes=100000` 的题不吃亏，但多工具并行能省时间——`NOTES-kit.md §12.E` 没提这点，属于可优化空间（P3）。
- **`visible()` 会剥 `<think>`**：如果模型把答案写进 `<think>` 里（部分 gpt-oss/DeepSeek 会），会被当空轮。
- **空轮不写进历史**（865 的 `if reply.text or reply.calls`）：因为严格端点拒绝 `content:null` 且无 tool_calls 的 assistant 消息。
- **时间提示贴在最后一条工具结果上**（915），不是单独消息——省一轮 token，且保证模型下一轮一定看到。
- **压缩只在工具轮之后检查**（916），自然完成那一轮不压缩（没必要，马上要收尾）。

### 3.4 与 `plan-verify` 的 `agent()`（`330-435`）对比

| 维度 | `custom-openai` | `plan-verify` |
|---|---|---|
| 形态 | `Agent` 类，状态在 `self`（`messages`/`prompt_tokens`/`context_window`） | 函数 `agent(session, messages, tools, until)`，状态全在参数里 |
| 步进预算 | 全局 `finish_margin`，一个循环跑到死 | 每步 `until` 阈值 + `WRAP_UP_SEC`，可分段 |
| 收尾 | 追加「总结」user 消息 + `tool_choice="none"` | 预先插 `WRAP_UP` 消息 + `tool_choice="none"`（353） |
| 循环内时间提示 | `[N minutes left]` 贴在工具结果 | `[N minutes left for this step]`（431–435） |
| 压缩 | 三级 | 一档（400 时砍老结果一半，`282-310`） |
| MCP | 双档 | 无 |
| **可复用性** | 要加角色就得**参数化会话**（这正是 E4/E5/E6 的由来） | 天生可复用（三角色就是三次 `agent()` 调用） |

**这就是移植的症结**：`plan-verify` 的优雅来自「状态全在参数里」，而 `custom-openai` 把会话写进了 `self`。所以「插一个 verifier」不是「加 150 行」，而是先做**会话参数化**，再写 verifier（§8.3）。

---

## 4. 三级上下文压缩：具体实现与阈值

### 4.1 触发阈值

| 项 | 公式 | 默认值 | 出处 |
|---|---|---|---|
| 触发点 | 每轮工具执行后 `prompt_tokens > compact_at()` | — | 916–917 |
| `compact_at()` | 无已知窗口 → `COMPACT_AT_TOKENS` | **150 000**（样例 env 覆盖 90 000） | 1287–1288 |
| `compact_at()` | 有已知窗口 → `max(window//4, min(COMPACT_AT_TOKENS, window - room - 1024))`，其中 `room = min(output_ceiling, window//2)` | 例：window=131 072、ceiling=32 000 → `room=32 000`，上限项 `= 131 072-32 000-1 024 = 98 048`。默认 150 000 时取 `max(32 768, 98 048) = 98 048`；样例 env 给 90 000 时取 `max(32 768, 90 000) = 90 000`——**这正好解释 90 000 这个数字的来历：≈ window − 32 000(回复) − 1 024(余量)，向下取整** | 1289–1293 |
| `prompt_tokens` 来源 | 优先真实 `usage.prompt_tokens`；无 usage 时 `sum(len(json.dumps(m)) for m)/4`（跳过 `output_items`） | 4 字符/token | 1295–1310, 1475–1478 |
| 单条工具结果上限 | `clip()` 30 000 字符（前 2/3 + 后 1/3） | — | 62, 260–276 |

> **注**：样例 `submission.yaml` 把 `COMPACT_AT_TOKENS` 设成 90 000 是因为 gpt-oss 窗口 131 072、默认回复 32 000，`90 000 + 32 000 ≈ 122 k` 留 9 k 余量。换 Opus/Gemini 大窗口后**必须删掉这个覆盖**，否则 6 小时任务在 90 k 就开始摘要，白丢一半可用的上下文。

### 4.2 第一级：截「已读」的老工具结果（`compact()` 前半，1676–1704）

```python
unread = {id(m) for m in self.unread()}                 # 最后一条 assistant 之后的 tool 消息 = 未读
results = [(n, m) for n, m in enumerate(messages) if m["role"] == "tool"]
cuts, size = [], max(estimate_prompt_tokens(), prompt_tokens if force else 0)
for n, m in (results if force else results[:-KEEP_RECENT_RESULTS]):   # ← 非 force 时保留最近 12 条
    if force and context_window and size <= compact_at(): break       # ← force 时够了就停
    if len(m["content"]) > 600 and id(m) not in unread:               # ← >600 字符、且已读
        size -= (len(m["content"]) - 400) // 4
        m["content"] = m["content"][:400] + "\n[... removed to save context; run the tool again if needed ...]"
        cuts.append(n)
```

- 阈值：单条 **> 600 字符**才动，砍到 **400 字符**（保头，留一句「需要就重跑」）。
- 保守性：未读的**一律不动**（模型还没看过的结果不能删，否则它以为命令没输出）。
- `force=False`（计划性压缩）：只动「除了最近 12 条以外」的，且**不看是否够用**，一刀切完。
- `force=True`（400 兜底）：从最老的开始，够用即停；此时 `size` 取 `max(估算, 端点给的 prompt_tokens)`（端点数字更准，1682–1684）。

### 4.3 第二级：把前面 N 轮摘要成 notes（`compact()` 后半，1705–1751）

| 步骤 | 代码 | 说明 |
|---|---|---|
| 保留窗口 | `cut = len(messages) - 6` | 留最近 **6 条**消息 |
| 边界修正 | `while cut > 3 and messages[cut]["role"] == "tool": cut -= 1` | 保留段不能以「工具结果」开头（它的 call 已被摘要带走，严格端点会 400） |
| 放弃门槛 | `if cut < 10: return bool(cuts)` | 总消息数 < 10 不值得花一次调用 |
| 摘要调用 | `stream(messages[:cut] + [user(SUMMARY_PROMPT)], "none", finish_margin)` | 直接调 `stream()` 而非 `complete()`：**故意不重试**，失败就保留原样 |
| 失败守卫 | `notes.finish is None` → 保留；`text` 空 → 保留；`notes.finish == "length"` → 加一句「notes 被截断」 | 1724–1736 |
| 重建 | `messages = messages[:2] + [user("Your notes on the work so far…\n" + text)] + messages[cut:]` | 1737–1748，只认「前 2 条 = system + 原始 instruction」 |
| 记账 | `prompt_tokens = estimate_prompt_tokens()`；`log("context: replaced N messages with notes")` | 1749–1750 |

`SUMMARY_PROMPT`（159–161）要求的字段：任务要求 / 在文件里发现了什么 / **每个创建或修改的文件（路径 + 状态）** / 已得的数字与决策 / 待办。这五条直接对应 PLE 的评分口径（交付物路径 + 数值 + 完整性）。

**Claude thinking 块的处置**（1666–1668 注释）：kept turns（`messages[cut:]`）里的 thinking 块**不删**——签名自包含，与更早的轮次无关；但删掉最新 assistant 轮的块会让 Claude 拒绝整段工具循环。

### 4.4 第三级：截「未读」（`cut_unread()` 1753–1779）

```python
unread = [m for m in self.unread() if len(m["content"]) > 2 * UNREAD_KEEP]   # > 1200 字符
if not unread: return False
if context_window:                                    # 已知窗口：精细分配
    rest  = estimate_prompt_tokens() - sum(len(m["content"]) for m in unread) // 4
    share = (compact_at() - rest) * 4 // len(unread)  # 每条能分到的字符数
for m in unread:
    keep = size // 2 if share is None or share >= size - 100 else share
    m["content"] = clip(m["content"], max(UNREAD_KEEP, keep))    # 保底 600
```

- 门槛：未读且 **> 1200 字符**。
- 有窗口：按 `(阈值 - 其余部分) / 条数` 平均分配；无窗口：直接减半。
- 保底 600（`UNREAD_KEEP`）；`share >= size - 100` 时也走减半（分到几乎没变，白算）。
- 返回 `True` 只要有改动，让 `remedy()` 有理由重试。

### 4.5 400 时的降级顺序（`remedy()` 1118–1152）——这是「第四层」调度器

```
1. 只有输出太多才爆的（报文给了 prompt 大小 + room() >= 1024 + output_limit() < 上次要的）
   → 什么都不压，只把 max_output 降下来就能过          （1127–1141，`return True`）
2. compact(force=True)                                  （1143）
3. 报文没给 prompt 大小、但降输出仍不够 → 再降         （1144）
4. cut_unread()                                         （1145）
```

顺序设计的意图：**能不压就不压**（降输出无信息损失）→ 压已读（信息便宜）→ 截未读（最贵）。

### 4.6 失效条件（改造时必须知道）

1. `prompt_tokens` 只在 `record()` 里更新；若端点不返回 usage，退化为 4 字符/token 估算，对中文/JSON 偏差可达 20–30% ⇒ 宁可让 `compact_at()` 保守一点。
2. 二级压缩依赖 `messages[:2]` 恰好是 `[system, instruction]`。**一旦在位置 2 之前插消息（例如把 plan 塞成第二条 user）就会破坏这个假设**——移植 planner 时要注意（E16 里明确要求「plan 拼进 instruction」而不是新增消息）。
3. `cut < 10` 的放弃门槛：长任务无影响，短任务（<10 条）压根不压缩，靠 `clip()` 兜。
4. 二级压缩的那次调用**不重试**：若此刻正好 429，就白丢一次机会（保留原样）。6 小时任务里不致命。

---

## 5. MCP 双档发现（`connect_mcp()` 951–1008）

### 5.1 连接与命名（961–973）

```python
for spec in session.mcp_servers:                      # 协议握手消息里的 mcp_servers
    server = MCPServer(session, spec)                 # 继承 taskbridge.MCPClient
    try: await server.start()                         # initialize + list_tools
    except (TaskError, RuntimeError, ValueError) as exc:
        log(f"MCP server {name} unavailable: {exc}"); continue     # ← 单台挂掉不拖垮整题
    for t in server.tools:
        name = re.sub(r"[^A-Za-z0-9_-]", "_", f"{server.name}__{t['name']}")
        self.mcp[unique_name(name, self.mcp)] = (server, t)
    log(f"MCP server {name}: {len(tools)} tools")
```

- 展平名 = `<服务器名>__<工具名>`，非法字符换 `_`（OpenAI function 名只允许 `[A-Za-z0-9_-]`），长度 > 64 或重名时 `unique_name()` 补 `_2/_3`。
- 映射表 `self.mcp: dict[展平名, (server, 原始 tool dict)]` —— 调用时反查原始名（1520），避免名字被改坏。
- 单服务器 `start()` 失败 **只跳这一台**，其余照常。这是关键：世界类任务的某个 app MCP 挂掉不该整题归零。

### 5.2 判据（994–1008）

```python
if len(json.dumps(functions)) <= DIRECT_MCP_CHARS and len(self.tools) + len(functions) <= MAX_TOOLS:
    self.tools += functions        # 档 A：直暴露
    return MCP_DIRECT              # "…the functions named <server>__<tool>"
self.tools += MCP_TOOLS            # 档 B：两段式
return MCP_CATALOG.format(catalog="\n".join(f"{s}: {', '.join(names)}" …))
```

| 档 | 条件 | 模型看到什么 | 代价 |
|---|---|---|---|
| **A 直暴露** | `json.dumps(functions)` **≤ 40 000 字符** 且 `4 + N ≤ 128` | 全部 schema 作为 function tools | 每轮都带全部 schema（token 固定开销） |
| **B 两段式** | 任一超限 | system prompt 里只有 `服务器: 工具名, 工具名…` 目录；额外给 `mcp_find` / `mcp_call` | 名字仍占 prompt，但 schema 按需取 |

- 40 000 是**序列化后字符数**，不是 token：`NOTES-kit.md §12.F` 提到某些任务 `tools/list` 几百个工具、响应 ~500 KB，超了就走 B。
- 档 B 的目录**列全部名字**（1005–1007）：这样模型至少知道「有什么」，只是不知道参数——配合 `mcp_find` 两跳。
- **注意**：`MAX_TOOLS` 判据用 `len(self.tools)`，而 `self.tools` 在 `__init__` 里就是 `list(TOOLS)`（4 个）；档 A 上限实际是 124 个 MCP 工具。
- 档 A 的 schema 处理：`inputSchema` 去掉 `$schema` 键（985–987），空则给 `{"type":"object","properties":{}}`；`description` 截 1024 字符。

### 5.3 `mcp_find` 的打分（1010–1050）

```python
words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]
def score(item):
    name, (_, t) = item;  text = f"{name} {t.description}".lower()
    return (query == name) * 100                      # 精确命中 100 分
         + sum(w in name.lower() for w in words) * 3  # 出现在名字里 ×3
         + sum(w in text for w in words)              # 出现在描述里 ×1
best = [item for item in sorted(mcp, key=score, reverse=True)[:8] if score(item) > 0]
# 每条：f"{name}: {desc[:400]}\nparameters: {json.dumps(inputSchema)}"
while len(found) > 1 and len("\n\n".join(found)) > TOOL_RESULT_CHARS: found.pop()
```

- 取前 8，**至少留 1 条**（哪怕超 30 000 也不截单条 schema——截了参数就没用）。
- 无命中返回「no tool matches …; the names are listed in the system prompt」。

### 5.4 调用链（`run_tool()` 1513–1522）

```python
if name == "mcp_call":
    name, args = str(args.get("tool","")), args.get("arguments") or {}
    args = json.loads(args) if isinstance(args, str) else args     # 模型常把 arguments 写成字符串
    if name not in self.mcp: return f"error: no tool named {name!r}; use mcp_find"
if name in self.mcp:
    server, t = self.mcp[name]
    return clip(await server.call(t["name"], args))                 # ← 用原始名，结果 30 000 截断
```

`MCPServer.call()`（579–607）把 MCP `content` 数组拍平：`text` 直取、`resource`/`resource_link`/其他 走 `json.dumps(...)[:30000]`、`isError` 前缀 `error: `、全空则退回 `structuredContent`。

---

## 6. 从 400 报文反推 context 窗口（`learn_context()` 1237–1251）

### 6.1 两个正则

```python
WINDOW_RE = re.compile(
    r"maximum context length (?:is |of )?(\d+)"                     # vLLM / OpenAI
    r"|context (?:window|length|size)(?: limit)?(?: is| of)? (\d+)" # 通用
    r"|tokens? > (\d+) maximum|maximum number of tokens allowed \((\d+)\)"  # Gemini
    r"|maximum prompt length is (\d+)", re.I)                       # xAI
PROMPT_RE = re.compile(
    r"(\d+) input tokens|(\d+) in the messages|prompt is too long: (\d+)"
    r"|input token count \((\d+)\)|request contains (\d+) tokens", re.I)
```

配套 `CONTEXT_FULL`（73–79）作为「这是不是 context 超限」的总闸门（也用于识别流内错误，787）。

### 6.2 状态更新

```python
def learn_context(self, error) -> bool:
    window, prompt = WINDOW_RE.search(error), PROMPT_RE.search(error)
    if window: self.context_window = int(next(g for g in window.groups() if g))
    if prompt: self.prompt_tokens     = int(next(g for g in prompt.groups() if g))
    return bool(prompt and self.context_window)      # 两个都拿到才算「量到了」
```

调用点只有一处：`remedy()` 的 `sent, measured = self.max_output, self.learn_context(error)`（1135）。`measured` 决定后续走「只降输出」还是「动真格压缩」。

### 6.3 反推出来的三个派生量

| 派生量 | 公式 | 行 | 效果 |
|---|---|---|---|
| `room()` | `context_window - prompt_tokens - CONTEXT_MARGIN` | 1260–1263 | 还剩多少输出空间 |
| `output_limit()` | `max(1024, min(output_ceiling, room()))` | 1273–1278 | 下次调用要多少 `max_*tokens` |
| `compact_at()` | 见 §4.1 | 1287–1293 | 压缩阈值按真实窗口收窄 |

**为什么值得保留**：这是全套代码里唯一「免费获得真实窗口」的机制——不调 `/models`、不猜，让端点自己在报错里说出数字。它同时解决两个问题：① 换模型时无需手调 `COMPACT_AT_TOKENS`；② `room()` 让输出上限自动减产，避免「prompt 刚好放下但回复被砍」。

**局限**（改造时注意）：
- 只在**已经爆过一次**之后才学到——第一次 400 仍然要付重试成本（`complete()` 里 400 → `remedy()` → `continue`，不睡不烧钱，代价小）。
- 有些端点报 `context_length_exceeded` 但**不给数字**（如部分网关）→ `measured=False` → 走「降输出 → 压缩 → 再降」的盲路。
- `learn_context` 写的是 `self.prompt_tokens` ——**审计会话里若也触发，会污染主会话的计数**（§8.3 的核心坑之一）。

---

## 7. deadline 夹紧：五处 reserve 语义

协议给的是 `limits.agent_timeout_sec`（6 小时题 = 21 600 s）；`taskbridge.Session` 在构造时把它变成 `deadline`（`taskbridge.py:98-103`），`time_left()` 返回剩余秒或 `None`。

| # | 位置 | 代码 | 语义 |
|---|---|---|---|
| 1 | `run()` 833–837 | `finish_margin = 180 if left is None else min(180, max(left,0)/4)` | 收尾窗口。长任务 3 分钟；短任务最多拿走 1/4 |
| 2 | 循环首检 852–854 | `if left < finish_margin: return await self.finish(...)` | 到点先收尾，不再开新工具轮 |
| 3 | `stream()` 1372 | `await asyncio.wait_for(read(), None if left is None else left - reserve)` | 单次流式调用**硬夹**到「收尾点」；`CutOff` 时把已流出文本写进 `final_message`（1374–1377） |
| 4 | `complete()` 1106–1112 | `if left is not None and left - wait < reserve: raise OutOfTime` | 退避等待会越过收尾点就直接放弃重试（不睡、不白等） |
| 5 | `bash()` 1571–1577 | `cap = min(exec_timeout_sec or 600, max(1, int(left - finish_margin)))`，再 `positive_int(timeout_sec,120,cap)` | **命令不会把收尾窗口吃掉**；协议侧还有一层 `min(timeout_sec, exec_timeout_sec, remaining)` |

另外两处「软夹紧」：

- `finish()` 936–940：`reserve = 0 if left is None else left - max(5, time_share(left, 30))`。`time_share(left,30) = left - min(30, left*0.25)`（`taskbridge.py:1404-1425`）⇒ 摘要调用最多拿到「剩余 - 至少 5 秒」，短任务按 1/4 折算。
- 每轮工具后 `[N minutes left]`（915）：让模型自己参与预算，减少「聊到最后一秒」。
- system prompt 144：`You have about {minutes} minutes in total; the time left is shown after the results of your tools.`

**行为含义**（改造时会撞到）：`finish_margin` 是**一个数被 5 处复用**：既当「不再开新轮」的门槛，又当 `bash` 的安全边界、又当重试禁界、又当 `stream` 的 reserve 默认值。**加验证阶段时不能只加一个常量，必须让这 5 处各自明确「现在服务于哪个阶段」**（E13/E14 的实质）。

`taskbridge._session()`（548–645）在收到 `cancel` 后：给 handler 2 秒返回、再 cancel、再等 6 秒，最后无论如何发 `done`（`_final(...)` 兜底 `"(stopped: time is up)"`）。若 handler 抛异常且 `session.final_message` 非空，仍会发 `done` + 那段摘要（598–614）——**这是「永远发 done」的最后一层保险，别改掉**。

---

## 8. 改造目标：从「单循环」到「worker + 只读审计 + 修复」

### 8.1 为什么不做完整三角色（planner/worker/verifier）

| 方案 | 成本 | 收益 | 判断 |
|---|---|---|---|
| 完整移植 `plan-verify`（planner 10% 时间 + 3 段会话） | 3 倍会话开销；planner 的产出对 `custom-openai` 的强 system prompt 增量有限 | 弱 prompt 的模型上有用（`minimal` 底子上确实有效） | **不做**（保留 E16 作 P3 备选） |
| worker + 只读审计 + ≤2 轮修复 | 审计是**干净的小上下文**（任务 + 摘要 + 交付物清单 + 工具结果），单次约 10–20 k tokens | 直接对上 PLE 的 `s = 0.4p + 0.6GO'`：`G=0`（破一条硬约束）就把该项封顶 0.4（`NOTES-kit.md §11.1`） | **做** |
| 只改 prompt 让 worker 自查 | 0 行 | `custom-openai` 已有（SYSTEM 143「open every deliverable again」） | 已验证不够——模型会「声称检查过」 |

**核心论点**：PLE 的分不是「做得多好」，是「**别破硬约束**」。硬约束（安全/可行性/格式/路径）由**另一个只读角色**逐条核对，比让 worker 自己保证可靠得多；而审计只读 ⇒ 不可能破坏已完成的交付物（这是选「只读工具集」而非「全工具 verifier」的理由）。

### 8.2 时间预算（与 `NOTES-kit.md §12.C` 的映射）

`NOTES-kit.md §12.C` 记的是 `plan-verify` 的分配：planner 用掉 10% → worker 剩 25% 收尾 → verifier 剩 12.5% → 修复剩 4 分钟。我们只有一个 worker 循环，映射成：

| 阶段 | 触发点 | 6 小时题（21 600 s） | 1 小时题（3 600 s） |
|---|---|---|---|
| worker 正常运行 | `left > check_start` | > 3 600 s | > 900 s |
| **收口点** `check_start` | `max(3*finish_margin, min(CHECK_MAX_WAIT, total*VERIFY_SHARE))` | `min(3600, 5400)=3600` → 剩 **60 分钟** | `max(540, 900)=900` → 剩 **15 分钟** |
| 审计预算 | `budget = min(AUDIT_MAX_SEC, max(300, (left-180)*0.6))` | `min(1800, 2052)=1800` → 审计窗口终点 `audit_reserve=1980` | `min(1800, 432)=432` → `audit_reserve=612` |
| 审计实际可用 | `left - audit_reserve` | **1 620 s（27 分钟）** | **288 s（4.8 分钟）** |
| 修复门槛 | `left - finish_margin >= max(FIX_MIN_SEC, total*0.05)` | 审计后 `left≈1980`，`1800 >= 1080` ✓ 可修 | `288 < 600` ✗ 不修（只审计） |
| 修复轮上限 | `MAX_FIX_ROUNDS = 2` | 每轮吃掉一半剩余 | — |
| 终稿 | `finish_margin` | 180 s | 180 s（`min(180, 3600/4)`） |

参数（建议初值，必须实验标定）：

```python
VERIFY_SHARE     = 0.25    # 收口点：剩 25% 总时间
CHECK_MAX_WAIT   = 3600    # 收口点最多提前 60 分钟
AUDIT_MAX_SEC    = 1800    # 一轮审计最多 30 分钟
AUDIT_KEEP_SEC   = 180     # 审计窗口结束时距 deadline 的余量
FIX_MIN_SEC      = 600     # 开一轮修复至少 10 分钟
MAX_FIX_ROUNDS   = 2
AUDIT_STEPS      = 8       # 审计会话最多 8 次模型调用
```

### 8.3 最难点：会话状态归属（E3/E4/E5/E6/E7）

**问题**：`custom-openai` 的所有状态都是 `self.*`，而审计需要**第二套 messages**：

| 方法 | 隐式依赖 | 审计会话调用时的后果 |
|---|---|---|
| `stream()` 1345–1354 | 请求体用 `messages` 参数 ✓，但 `self.max_output = self.output_limit()` 用 `self.prompt_tokens` | 轻微：输出上限按主会话估 |
| `record()` 1475–1478 | `self.prompt_tokens = usage.prompt_tokens` | **严重**：审计的大 prompt 会覆盖主会话计数 → 下一轮误判「该压缩了」或「还有空间」 |
| `remedy()` 1127–1146 | 调 `learn_context()`（写 `self.prompt_tokens`/`self.context_window`）、`compact()`、`cut_unread()`（全都在操作 `self.messages`） | **致命**：审计会话撞 context-full → **把 worker 的历史摘要成 notes 或砍掉** |
| `compact()` 1676–1751 | `self.messages`、`self.prompt_tokens` | 同上 |
| `finish()` 928 | append 到 `self.messages` | 无（只用于主会话） |
| `bash()` 1572–1574 | `self.finish_margin` | 需按阶段切换（审计阶段应夹到 `audit_reserve`） |

**两个方案**：

| | 方案 A：会话参数化 + 门禁（推荐） | 方案 B：彻底重构为 `Conversation` 对象 |
|---|---|---|
| 做法 | `complete/stream/record/remedy` 各加 `messages=None` 参数；`messages is not self.messages` 时：`record` 不写 `self.prompt_tokens`、`remedy` 只做「降输出 + 本地截工具结果」、不碰 `compact()/cut_unread()/learn_context()` | 把 `messages/prompt_tokens/context_window/tools` 抽成 `Conversation` 类，`Agent` 持有 `primary: Conversation` 与 `audit: Conversation` |
| 代码量 | **≈ 100 行**（含 E7 的 audit 循环 + `trim_tool_results`） | ≈ 260 行（重写 `compact/run/remedy/record` 的宿主） |
| 风险 | 低：主链路逻辑不动，只加分支 | 高：把已验证能跑的压缩逻辑重排 |
| 建议 | **第一阶段做这个** | 只在需要「多个 worker 并行」时才值得 |

方案 A 的关键 12 行（`remedy` 分支）：

```python
async def remedy(self, error: str, messages=None) -> bool:
    own = messages is None or messages is self.messages      # 是否主会话
    if CONTEXT_FULL.search(error):
        sent = self.max_output
        measured = self.learn_context(error) if own else False   # ← 审计会话不写主会话计数
        if own and measured and self.room() >= MIN_OUTPUT and self.output_limit() < sent:
            return True
        if own:
            return bool(await self.compact(force=True)
                        or (not measured and self.output_limit() < sent)
                        or self.cut_unread())
        return self.trim_tool_results(messages)              # ← 12 行本地函数：只截本条会话的工具结果
    return (self.use_responses(error) or self.use_max_tokens(error)
            or self.drop_effort(error) or self.shrink_output(error))
```

### 8.4 审计角色规格

| 项 | 值 |
|---|---|
| 会话 | 全新 `messages = [system(AUDITOR), user(review)]`，**不含** worker 的历史（避免被 worker 的自我叙述带偏） |
| 工具 | `bash` + `read_file`（+可选只读 MCP，E18）；**不给** `write_file`/`edit_file`/`mcp_call` |
| 输入 | 任务原文 + 交付物清单（`wanted()`，E10）+ worker 摘要 + （第 2 轮起）上一轮裁决 |
| 输出 | `PASS` 或编号缺口列表 |
| 预算 | `reserve = audit_reserve`（§8.2），`AUDIT_STEPS=8` 封顶 |
| 判定 | `visible(text).strip("*_. \r\n\t").upper() in ("", "PASS")`（照抄 `plan-verify:499`，兼容 `**PASS**.`） |
| 失败 | 任何异常/超时 → 视为「无裁决」，直接进终稿（审计绝不能让整题挂掉） |

### 8.5 修复回路

```python
self.messages.append({"role": "user", "content":
    "A reviewer checked your files and found these gaps:\n" + verdict +
    "\n\nFix them, then reply with your updated summary."})
text = await self.work() or text        # 复用主循环（work() 可重入：nudges/head 是局部变量）
```

要点：① 修复轮**复用**主循环，所以工具、压缩、deadline 全部自动继承；② `work()` 的 `nudges, head = 0, ""`（850）是局部变量 ⇒ 天然可重入；③ 修复后 worker 会再调一次 `finish()`（若又撞 `check_start`）——幂等，只是多一条 user 消息。

---

## 9. 审计提示词草案（E11，可直接粘贴）

```python
CHECKLIST = """\
Check, in this order:
1. Path and format: every deliverable the task names exists at exactly that path, with that name and extension.
   `ls -l` it; if the task asks for a zip, list its entries and check the required structure inside.
2. Real format: open each binary deliverable with code (python3: openpyxl / python-docx / zipfile / ifcopenshell,
   whichever the container has, or `file`). An HTML file named .xlsx is a failed deliverable.
3. Hard constraints: quote each "must / must not / at most / at least / only" clause of the task into a list and
   check every one. A violated hard constraint is worse than a missed optimization: it caps the score.
4. Numbers: recompute the key figures with code, do not eyeball them. Check units and magnitudes (mm vs m,
   kN vs N, W vs kW, gross vs net).
5. Completeness: every part the task asks for (each section, each sheet, each row group) is present, not just started.
6. Environment: the files must be self-contained — the task container has no network, so nothing may refer to
   a downloaded asset that is not in the container."""

AUDITOR = """You audit an engineer's finished work on a task in a Linux container with no internet access. You get \
the task, the engineer's summary, and (from the second round) the gaps found earlier. Your tools act inside the \
container and bash starts in {workdir}. You may read anything; you must not create or change any file.

{checklist}

Open the deliverables yourself and verify them; never trust the summary. If everything holds, reply with the single \
word PASS and nothing else. Otherwise reply with a numbered list of concrete gaps, each naming the file and what to \
change, most damaging first. Do not restate what is already right."""

FIX_PROMPT = """A reviewer checked your files and found these gaps:
{verdict}

Fix them, then reply with your updated summary (what you delivered, where, and the key results)."""
```

`review` 文本（`Agent.review()`，E10）拼法：

```
## Task
{instruction}

## Deliverables the task names
{wanted_paths 逐行，若为空则写 "(the task names no explicit path; judge by its wording)"}

## The engineer's summary
{summary}

## Gaps found in the previous round
{verdict or "(this is the first round)"}
```

**为什么把 `wanted()` 单独列出来**：`plan-verify` 的 verifier 只有任务原文 + 摘要，容易漏掉「任务在中间段落提到的第二个交付物」。用通用正则把路径抽成清单（**不含任何任务特判**，见 §13.1 合规）能稳定提高召回。

---

## 10. 可执行改动清单

> 约定：文件均指 `harness/serve.py`（除 E0）；行号指**改之前的** `sample_submissions/custom-openai/serve.py`。

### 10.1 必需项（E1–E15，合计 ≈ 310 行）

| # | 文件 | 位置 / 函数 | 改什么 | 行数 |
|---|---|---|---|---|
| **E0** | `submission.yaml` | `model_api` / `env` | 换 `google-cloud/claude-opus-5-5`；删 `COMPACT_AT_TOKENS: "90000"`（用默认 150 000）；其余原样 | 0 |
| **E1** | `serve.py` | 常量区 `61-98` 之间 | 新增 §8.2 的 7 个常量 + `AUDIT_TOOL_NAMES`（`("bash", "read_file")`） | +11 |
| **E2** | `serve.py` | `Agent.__init__` `803-821` | 新增 `self.total=None`、`self.worker_done=False`、`self.stopped_for_check=False`、`self.rounds=0` | +6 |
| **E3** | `serve.py` | `Agent.run` `823-917` | ① 把 `829-848` 抽成 `prepare()`（原样搬，返回 mcp 段）；② 原 `run` 主体改名 `work()`，在自然完成出口（`888` 前）置 `self.worker_done = True`，在循环首检后加「`left < check_start()` → `worker_done=True` + `finish("Time for the final check.")`」；③ **新 `run()`**：`prepare() → total=left → text=await work() → 审计/修复循环 → final_message → return` | +45 / 改 3 |
| **E4** | `serve.py` | `Agent.complete` `1052-1116` + `Agent.remedy` `1118-1152` + **新增** `Agent.trim_tool_results(messages)` | `complete` 加 `messages=None, tools=None` 形参；`self.stream(messages or self.messages, …)`；`record(track=(messages is None))`；`except …: await self.remedy(str(exc), messages)`。`remedy` 加 `messages=None`，非主会话时只走「降输出 + `trim_tool_results()`」，**不碰** `compact/cut_unread/learn_context`。`trim_tool_results()`：只把本会话里 `role=="tool"` 且 >1 200 字符的截到 600 字符（12 行） | +20 / 改 4 |
| **E5** | `serve.py` | `Agent.stream` `1312-1397` | 加 `tools=None` 形参；`tools=self.tools if tools is None else tools`；`self.max_output` 的赋值加 `if tools is None` 保护（审计不覆盖主会话输出上限） | +4 / 改 2 |
| **E6** | `serve.py` | `Agent.record` `1469-1479` | 加 `track=True`；`if track: self.prompt_tokens = …`；`report()` 无条件调（usage 必须报给评测端） | +4 |
| **E7** | `serve.py` | **新增** `Agent.audit(self, summary, round_no)`（放 `finish()` 之后） | 组干净会话 → 循环最多 `AUDIT_STEPS` 次 `complete(messages=msgs, tools=self.audit_tools(), reserve=audit_reserve)` → `run_tool` 执行只读工具 → 返回 `visible(text)`；全程 `try/except` 兜底成空串；`log("audit: …")` | +58 |
| **E8** | `serve.py` | **新增** `Agent.fix(self, verdict)` | append `FIX_PROMPT.format(...)` → `return await self.work()` | +16 |
| **E9** | `serve.py` | **新增** `Agent.audit_tools(self)` | `[t for t in self.tools if t["function"]["name"] in AUDIT_TOOL_NAMES]`（`("bash","read_file")`） | +6 |
| **E10** | `serve.py` | **新增** `wanted(instruction)`（模块级） + `Agent.review(self, summary, round_no)` | `wanted()`：通用正则抽 `/app|/filesystem|/tmp` 下绝对路径 + 常见交付物扩展名，去重保序上限 40 条，**零任务特判**；`review()`：按 §9 模板拼文本 | +48 |
| **E11** | `serve.py` | 提示词区 `161` 之后 | 新增 `CHECKLIST` / `AUDITOR` / `FIX_PROMPT` 三段（§9 原文） | +45 |
| **E12** | `serve.py` | `Agent.run_tool` `1500-1543` | 开头加闸门：`if self.phase != "work" and name not in AUDIT_TOOL_NAMES: return "error: … not available in the audit phase"`；新增 `self.phase` 属性（`"work"`/`"audit"`） | +10 / 改 2 |
| **E13** | `serve.py` | `Agent.bash` `1560-1582` | 把 `self.finish_margin` 换成 `self.reserve_now()`（审计阶段返回 `audit_reserve`，否则 `finish_margin`）；`cap = min(exec_timeout_sec, max(1, int(left - self.reserve_now())))` | +4 / 改 2 |
| **E14** | `serve.py` | **新增** `check_start()` / `audit_reserve()` / `can_check()` / `can_fix()` / `passed(text)` | 五个小函数，公式见 §8.2 / §8.4；`passed()` 用 `strip("*_. \r\n\t").upper() in ("", "PASS")` | +26 |
| **E15** | `serve.py` | `SYSTEM` `132-147` | 加 6 行：①「最后 25% 时间会停你、由一个只读审计员逐条核对，缺口会回灌给你修」②「硬约束优先于优化目标」③「每个交付物收尾跑 `ls -l` + 用代码打开一次」 | +6 |

**小计：新增/改动累计 ≈ 309 行（E1 11 + E2 6 + E3 45 + E4 20 + E5 4 + E6 4 + E7 58 + E8 16 + E9 6 + E10 48 + E11 45 + E12 10 + E13 4 + E14 26 + E15 6），其中 45 行是提示词。** 涉及既有函数 8 个（`run` 拆为 `prepare`+`work`+新 `run`、`complete`、`stream`、`record`、`remedy`、`run_tool`、`bash`、`__init__`；`connect_mcp`/`find_mcp`/`compact`/`cut_unread`/`finish`/`solve` 逻辑完全不动），新增函数/方法 12 个：`prepare` `audit` `fix` `audit_tools` `review` `wanted` `trim_tool_results` `check_start` `audit_reserve` `can_check` `can_fix` `passed`（另 `work` 为原名 `run` 的重命名）。

### 10.2 可选项（P2/P3，按收益排序）

| # | 文件 | 位置 | 改什么 | 行数 | 收益 |
|---|---|---|---|---|---|
| **E16** | `serve.py` | 新 `Agent.plan()` + `PLANNER` 提示词 | 在 `run()` 开头跑一次**无工具**的 plan 调用（把 plan 拼进 instruction 第二条 user，**不新增消息**，否则破坏 `compact()` 的 `messages[:2]` 假设） | +55 | 中（`NOTES-samples` 认为对弱 prompt 模型有效；Opus 上边际小） |
| **E17** | `serve.py` | `marked()` `457-487` | 再加第 3 个缓存断点（Claude 允许 4 个）：主会话把「最近一条 user 之前的 assistant」也打标，长任务省 5–10% 输入费 | +12 | 中（纯省钱） |
| **E18** | `serve.py` | 新 `Agent.readonly_mcp()` | 世界类任务（`/app/tools/.apps_data/`）审计只读工具从 `self.mcp` 里按 `^(get\|list\|read\|search\|query\|find\|show\|describe)_` 白名单挑，**不给 `mcp_call`** | +15 | 中（14 个 rubric 题里 12 个是 world 环境，`NOTES-kit §12.F`） |
| **E19** | `serve.py` | `connect_mcp()` `994-996` | 直暴露阈值改成按**估算 token**（`len(json.dumps)/4`）判，并支持 env 覆盖 `DIRECT_MCP_CHARS` | +8 | 低（40 000 字符对多数模型已够保守） |
| **E20** | `serve.py` | `run_tool` `899-912` | 工具并行：同一轮的多个 `tool_calls` 用 `asyncio.gather` 执行（协议允许 8 并发，`NOTES-kit §12.A`） | +10 | 中（省墙钟时间 → 多出的时间给审计） |

---

## 11. 关键函数伪代码（照着写）

```python
# ---------- E3：新的三段式 ----------
  async def prepare(self) -> None:                        # 原 829-848 原样搬
      mcp = await self.connect_mcp()
      left = self.session.time_left()
      self.total = left
      self.finish_margin = FINISH_MARGIN_SEC if left is None else min(FINISH_MARGIN_SEC, max(left, 0) / 4)
      self.messages = [
          {"role": "system", "content": SYSTEM.format(
              workdir=self.session.workdir, mcp=mcp,
              minutes=int(left / 60) if left is not None else "unlimited")},   # ← 顺手修 844 的 falsy bug
          {"role": "user", "content": self.session.instruction},
      ]

  async def run(self) -> str:
      await self.prepare()
      text = await self.work()
      while self.can_check() and self.rounds <= MAX_FIX_ROUNDS:
          verdict = await self.audit(text, self.rounds)
          self.rounds += 1
          if self.passed(verdict):
              await self.session.log("audit: PASS")
              break
          await self.session.log(f"audit: {len(verdict)} chars of gaps")
          if not self.can_fix():
              await self.session.log("audit: no time to fix")
              break
          text = await self.fix(verdict) or text
      self.session.final_message = text or self.session.final_message
      return self.session.final_message

  async def work(self) -> str:
      # 原 run() 的 while 循环，只改两处：
      #   a) 循环首检后：
      #        if left is not None and left < self.check_start():
      #            self.worker_done = True
      #            return await self.finish("Time for the final check.")
      #   b) 自然完成出口（原 887-888）前：
      #        self.worker_done = True
      #        return text or self.session.final_message
      ...

# ---------- E14：时间闸门 ----------
  def check_start(self) -> float:
      if self.total is None: return 0.0
      return max(3 * self.finish_margin, min(CHECK_MAX_WAIT, self.total * VERIFY_SHARE))

  def audit_reserve(self) -> float:
      left = self.session.time_left()
      if left is None: return AUDIT_KEEP_SEC
      budget = min(AUDIT_MAX_SEC, max(300.0, (left - AUDIT_KEEP_SEC) * 0.6))
      return AUDIT_KEEP_SEC + budget

  def can_check(self) -> bool:
      if not self.worker_done: return False
      left = self.session.time_left()
      return left is None or left >= 2 * self.finish_margin

  def can_fix(self) -> bool:
      left = self.session.time_left()
      return left is None or left - self.finish_margin >= max(FIX_MIN_SEC, (self.total or 21600) * 0.05)

  @staticmethod
  def passed(text: str) -> bool:
      return visible(text).strip("*_. \r\n\t").upper() in ("", "PASS")

# ---------- E7：审计 ----------
  async def audit(self, summary: str, round_no: int) -> str:
      saved_phase, saved_margin = self.phase, self.finish_margin
      self.phase = "audit"
      try:
          reserve = self.audit_reserve()
          msgs = [{"role": "system", "content": AUDITOR.format(
                       workdir=self.session.workdir, checklist=CHECKLIST)},
                  {"role": "user", "content": self.review(summary, round_no)}]
          for _ in range(AUDIT_STEPS):
              reply = await self.complete(tool_choice="auto", reserve=reserve,
                                          messages=msgs, tools=self.audit_tools())
              arguments = [parse_arguments(c) for c in reply.calls.values()]
              if reply.text or reply.calls:
                  msgs.append(reply.message())
              if not reply.calls:
                  return visible(reply.text)
              for call, args in zip(reply.calls.values(), arguments):
                  out = await self.run_tool(call["name"], args) if isinstance(args, dict) else args
                  msgs.append({"role": "tool", "tool_call_id": call["id"], "content": out})
          return ""
      except (CutOff, OutOfTime, openai.OpenAIError) as exc:
          await self.session.log(f"audit: skipped ({str(exc)[:200]})")
          return ""
      finally:
          self.phase, self.finish_margin = saved_phase, saved_margin
```

> 注意 `finally` 里恢复 `finish_margin`——审计期间 `bash()` 靠它夹紧（E13），不恢复会把主会话的 `bash` 上限永久缩小。

---

## 12. 验收、回归与回滚

### 12.1 三阶段验证（每阶段都要能独立回滚）

| 阶段 | 命令 | 判据 |
|---|---|---|
| ① 只验配置 | `uv run python -m kit validate ../harness` | 秒级；`submission.yaml` 无报错 |
| ② 全链路 | `uv run python -m kit check ../harness --cancel-test` | **10 个阶段全绿**（`submission.yaml/files/.env/build/model/listen/smoke/mcp/cancel/artifacts`），见到 `ready to submit` |
| ③ 小样本量收益 | `uv run python eval/run.py ../harness --tasks engineers-estimate-check,footing-settlement-verification,solar-supplemental-review` | 对比「无审计基线」的 dev 分；同时看每题 `final_message` 是否含交付物路径 |
| ④ 全量 | `uv run python eval/run.py ../harness` | 29 题；`python -m kit artifacts ../harness` 通过 |
| ⑤ 提交 | `git add -A && git commit`（harness + `artifacts/` 同一 commit） | `manifest.json` 哈希与代码一致 |

### 12.2 改造的回归风险表

| 改动 | 可能弄坏什么 | 怎么测 |
|---|---|---|
| E3 拆 `run→work` | 自然完成出口的返回值语义（`text` vs `final_message`） | 冒烟题：`/app/hello.txt` + 打印最终消息 |
| E4/E5/E6 加形参 | 主会话行为完全不变（默认路径） | 用 `--tasks` 重跑一题，对比 `runs/<name>/<task>` 的 token 与轮数 |
| E7 审计 | 审计异常拖垮整题 | 断网/超时/坏 JSON 三种故障注入；都必须 `done` |
| E13 `reserve_now()` | `bash` 超时被误夹成 1 秒 | 审计阶段跑 `sleep 30`；应有 30 s 或按 cap 截断，不是 1 s |
| E15 SYSTEM 加句 | 模型提前收工去「等审计」 | 看 6 小时题的 `done` 时间是否被压到 75% 之前 |

### 12.3 回滚

`git tag base`（移植完、未改前）→ 每项改动一个 commit（E1、E3、E4-E6、E7-E11 分开）→ 任何一项导致 dev 分下降就 revert 单个 commit。**不要一次提交全部**。

---

## 13. 风险、合规与已知债

### 13.1 合规（`NOTES-kit.md §12.H`，红线）

- `wanted()` **只能用通用路径/扩展名正则**，禁止任何任务名、题目关键词、dev 集特判——那属于「fingerprinting tasks」。
- 不读 `artifacts/`、不读 dev grader、不按任务指纹改行为。
- `usage` 必须继续上报（E6 明确保留 `report()` 无条件调用）。
- 审计阶段**不写文件**（工具集就不给写），避免「审计员把好文件改坏」。
- 不要把任务内容落盘到 `artifacts/` 之外的地方（`NOTES-kit §12.H`：不保留/分享任务内容）。

### 13.2 已知债（读代码时发现，改的时候顺手修）

| 位置 | 问题 | 修法 |
|---|---|---|
| `844` | `int(left/60) if left else "unlimited"`：`left == 0.0` 被当成无限时间 | `if left is not None` |
| `915` | 同上（`left // 60`） | 同 |
| `1226-1229` | `shrink_output` 的兜底分支用 `self.max_output // 2`；若 `max_output` 已被 `output_limit()` 压低，会连锁减半 | 用 `output_ceiling // 2` 更稳 |
| `1688` | 一级压缩只砍到 400 字符，未考虑「一条就占 30 000」的极端 | 已有 `clip()` 兜前；可加「若单条 > 10 000 则先 `clip` 到 4 000」 |
| `1705` | `cut = len(messages) - 6` 假定末尾 6 条是「1 轮 assistant + 若干 tool」；若一轮有 8 个 tool_call，保留段会截断一个工具轮 | 改成「从末尾往前找第 2 个 assistant 的边界」更稳（`plan-verify` 也是固定 6） |
| 899–912 | 工具串行执行 | E20 可选并行 |
| `1345` | `timeout=900` 固定；max effort 的一次调用可能 > 900 s 才出声（`NOTES-kit §12.D`） | 流式已有，900 s 够；若换 Gemini 长思考需抬到 1800 |

### 13.3 成本

- 审计一次 ≈ 任务原文 + 摘要 + 8 步工具（约 10–25 k 输入 token）⇒ 相对 6 小时主循环（几十万 token）可忽略。
- 修复轮才是贵的（整个主循环再跑一遍）。`MAX_FIX_ROUNDS=2` + `can_fix()` 门槛控制住上限：最坏额外 ≈ 40% 剩余时间。
- 隐藏 test set 跑 3 次 × 56 题，且评测端并发 ~36 题：**必须保留退避重试与 `429` 处理**（`complete()` 已有，别动）。

---

## 14. 对照已有笔记：确认、修正与增量

| 来源 | 原结论 | 本次核对 | 处置 |
|---|---|---|---|
| `NOTES-samples §2.4` | 「27 个函数 + `Agent`/`Reply`/`MCPServer` 三个类」 | 实测 **15 个模块级函数 + 7 个类 + 34 个方法**（另有 4 个异常类） | 修正计数；结构判断不变 |
| `NOTES-samples §2.4` | 三级压缩「保留最近 12 条」 | 正确（`KEEP_RECENT_RESULTS=12`，且只对「非 force」生效） | 采纳 |
| `NOTES-samples §2.4` | `remedy()` 顺序：少输出 → 压缩 → 再少输出 → 截未读 | 正确（`1127-1146`），且第一步带 `measured and room()>=MIN_OUTPUT` 的前置条件 | 采纳并补条件 |
| `NOTES-samples §2.4` | `finish_margin = min(180s, 剩余/4)` | 正确 | 采纳 |
| `NOTES-samples §5.2` | 「加 verifier 大致 150 行」 | 实测 **≈ 309 行**（含 45 行提示词、48 行 `wanted/review`、58 行审计循环、20 行会话门禁、26 行时间闸门）；150 行只够「能跑」，不够「不污染主会话」 | 修正预算 |
| `NOTES-samples §5.2` | 「在 `run()` 里 `finish_margin` 前留出 ~12.5% 时间」 | 12.5% 是 `plan-verify` 的 **verifier 起点**（worker 25% 收尾）；对 `custom-openai` 要改成「worker 收口 25% / 审计预算 ≤30 min」两段（§8.2） | 修正 |
| `NOTES-kit §12.C` | 时间预算表（planner 10% / worker 25% / verifier 12.5% / fix 4 min） | 单循环下不适用；映射见 §8.2 | 补映射表 |
| `NOTES-kit §12.F` | MCP 目录可能几百工具 / ~500 KB；策略 `<128 且 <40 000 字符` | 正确；补充「档 B 的目录列**全部**名字」与「单服务器失败只跳该台」 | 补细节 |
| `NOTES-kit §11.1` | `s = 0.4p + 0.6GO'`，破硬约束封顶 0.4 | 正确 ⇒ 审计的**第一优先级是硬约束清单**（§9 `CHECKLIST` 第 3 条） | 落进提示词 |
| `NOTES-samples §4` | 「没有任何样例做 IFC 语义校验」「单位/量纲全是空白区」 | 正确；本方案的 `CHECKLIST` 第 2/4 条是**提示词级**覆盖；若要程序化，需在审计里跑固定脚本（P3，+40 行） | 记为 P3 |

---

## 15. 三轮迭代建议

| 轮 | 范围 | 目标 | 判据 |
|---|---|---|---|
| R1（当天） | E0 + 什么都不改 | 基线能跑、`kit check` 全绿、dev 全量有 `artifacts/` | `ready to submit` + 29 题全部有交付物 |
| R2（1 天） | E1–E15 | 带审计的 harness | 3 题小样本 dev 分 ≥ 基线；无「审计拖垮整题」 |
| R3（按需） | E16–E20 + §13.2 的债 | 抠分 | 全量 dev 分提升；单题耗时/费用不失控 |

**先量后调**：R2 必须保留「同一 commit 下审计开关」——用 env `AUDIT=0` 关闭审计（`can_check()` 直接返回 False），这样 A/B 只差一个环境变量，不用两份代码（**这是 R2 唯一值得额外加的开关，+3 行**）。

---

## 附：常量速查（改造后）

| 常量 | 值 | 归属 |
|---|---|---|
| `COMPACT_AT_TOKENS` | 150 000（env 覆盖） | 原有 |
| `TOOL_RESULT_CHARS` | 30 000 | 原有 |
| `DIRECT_MCP_CHARS` / `MAX_TOOLS` | 40 000 / 128 | 原有 |
| `KEEP_RECENT_RESULTS` / `UNREAD_KEEP` | 12 / 600 | 原有 |
| `FINISH_MARGIN_SEC` | 180 | 原有 |
| `RETRY_WAITS` | (5,15,30,60,120) | 原有 |
| `MIN_OUTPUT` / `CONTEXT_MARGIN` | 1 024 / 1 024 | 原有 |
| `VERIFY_SHARE` | 0.25 | **新** |
| `CHECK_MAX_WAIT` | 3 600 | **新** |
| `AUDIT_MAX_SEC` / `AUDIT_KEEP_SEC` | 1 800 / 180 | **新** |
| `FIX_MIN_SEC` / `MAX_FIX_ROUNDS` / `AUDIT_STEPS` | 600 / 2 / 8 | **新** |
| `AUDIT_TOOL_NAMES` | `("bash","read_file")` | **新** |

（完）
