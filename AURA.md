# AURA Harness — 架构清单、与我们的差异、落地了什么

调查时间 2026-11-19。代码来源：Devpost 页 `https://devpost.com/software/aura-harness-ple`
→ GitHub `https://github.com/wenizmohamed/aura-harness-ple`（public，clone 成功，
HEAD `f13b80b`，2026-11-16）。**代码拿到了**，下面每一条都能在仓库里指出文件与行。

---

## 1. AURA 架构清单（逐条抄，带出处）

### 1.1 运行入口与模块

| 文件 | 作用 |
|---|---|
| `submission.yaml`（8 行） | PLE 运行时入口：`model_api` / `uv venv` 安装 / `serve: serve.py` |
| `harness_config.json`（31 行） | 阶段预算、模型上限、telemetry 默认值，加载时校验，非法值 `ConfigError` |
| `serve.py`（297 行） | 多阶段编排：Plan → Work → Verify ↔ Repair |
| `engine.py`（846 行） | 自研 tool-calling agent 核心（流式、重试、compaction、400 恢复） |
| `taskbridge.py`（1528 行） | PLE 协议（WebSocket Session / HTTP Relay / MCPClient），与官方样例同源 |
| `aura_harness/config.py` | 配置 schema + 环境变量覆盖 + `validate_runtime_env()` |
| `aura_harness/telemetry.py` | JSONL 事件 + 每 session 一份 summary JSON |
| `aura_harness/compaction.py` | 上下文估算、截断/压缩、上下文溢出 400 的恢复 |
| `aura_harness/stream.py` | SSE/Responses 解析、参数校验、user-turn 合并 |
| `aura_harness/mcp_tools.py` | 内置工具 schema + MCPServer |
| `scripts/validate.py`、`tests/` | 5 个测试文件 + 配置校验 |

### 1.2 四个阶段（`serve.py:217-289`）

| 阶段 | 预算公式 | 上限 | 干什么 |
|---|---|---|---|
| **Plan** | `plan_share × 剩余` = 0.10 | `plan_max_sec` 1200 s | 只读不改：读全部输入，列出每个交付物的**确切路径/格式/必须包含什么**，写 `/tmp/aura_plan.md` |
| **Work** | `剩余 × (1 − verify_share)` = 0.70 | — | 先读 plan，再产出交付物；用代码算每个数、查单位、守硬约束 |
| **Verify** | `min(verify_round_share × (剩余−wrap), verify_max_sec)`，0.35 / 900 s | — | **开一个全新 `PhaseAgent`**，工具裁到只读 `("bash","read_file","mcp_find","mcp_call")`，拿 plan + worker 摘要，逐份打开交付物；只回 `PASS` 或每行一个缺口 |
| **Repair** | `剩余 − wrap_sec(300)` | `max_repair_rounds` 3 | 把缺口原文丢回**同一个 worker** 的会话，再回 Verify |

进入 Verify 的门槛：`剩余 ≥ min_repair_sec(600) + wrap_sec(300)`；不满足就跳过（
`serve.py:238-241`）。`is_pass()` 只看 `PASS` 或空（`serve.py:161-164`）。

### 1.3 compaction 策略（`harness_config.json` + `compaction.py`）

- `compact_at_tokens` 150000；`context_window` 未知时用它，已知时
  `max(窗口/4, min(150k, 窗口 − reserve − 1024))`（`compute_compact_at`）。
- `keep_recent_results` 12 条工具结果保留原文；`unread_keep` 600 字符是更早结果的截断长度。
- `tool_result_chars` 30000 截断单条工具结果；`direct_mcp_chars` 40000。
- 上下文溢出时从报错串里**反推**窗口与 prompt 大小
  （`learn_context_from_error`，正则抓 "maximum prompt length is N"）。
- `finish_margin_sec` 180：临近截止先停手要摘要。

### 1.4 telemetry 记录什么（`telemetry.py`）

严格脱敏（不写 prompt、不写任务文件、不写 key），一行一个 JSONL 事件：

- `session_start`（model / mcp 数 / 总预算）、`phase`（名字 / 状态 `done|pass|gaps|skipped_low_time` / 预算秒 / 实耗秒）
- `model_call`（model / prompt+completion+cached tokens / 延迟 / finish 原因）
- 计数器：`model_calls` `retries` `compactions` `tool_calls` `tool_errors` `prompt_tokens` `completion_tokens` `cached_tokens`
- 结束时一份 summary JSON：`{model, status, outcome{summary_chars}}`

### 1.5 AURA 自己的 dev 成绩（`artifacts/manifest.json`，gemini-4-argon）

| task | reward |
|---|---|
| data-center-site-model | **1.000** |
| lv-cable-sizing | **0.787** |
| landscape-sheet-revision | **0.571** |
| fire-test-validation-memo | 0.471 |
| roof-deck-design | 0.343 |
| seismic-diaphragm-shear | 0.306 |
| wind-diaphragm-shear | 0.281 |
| data-hall-mep-routing | 0.000 |
| data-hall-mep-routing-offset | 0.000 |

⚠️ 它是 Gemini 4 Argon 跑的，我们是 gpt-oss-120b dev 替身，**分数不可直接比**；
能比的是结构。

---

## 2. 逐条对照我们的 harness

我们的入口是仓库根的 `submission.yaml → serve.py（21 行）→ harness.solve`，
真正干活的是 **Pi**（`@earendil-works/pi-coding-agent`，`--print --mode json
--no-session`），工具被 `task-tools.ts` 换成走 `taskbridge.Relay` 打到任务容器。

| 问题 | AURA | 我们（改之前） |
|---|---|---|
| 有 stages 吗 | 4 个，各带预算 | **没有**：一个 Pi 会话跑到底 |
| 有 phase budget 吗 | 有（0.10 / 0.70 / 0.35 / wrap 300） | 只有一个 `STOP_BEFORE_SEC=90` 的全局收尾余量 |
| 有 verify-repair 吗 | Verify 开**新会话**读文件 → 缺口回 worker → 最多 3 轮 | **没有**。有 `_audit`，但它是"跑完之后的体检"：`harness.py:710-718` 明写 "cannot fail the task"，只往日志里写，**从不回喂给模型** |
| 自检靠什么 | 模型读交付物 vs plan | 确定性闸门 `contract.extract` + `selfcheck.py` |
| compaction | 自研，可反推窗口 | **Pi 自己的**（`UNKNOWN_MODEL` 128k 兜底） |
| telemetry | JSONL + summary | 只有 `session.log` / usage |

### 我们的闸门在两个 dev 题上的实际读数

fire-test 拿 1.0 的那次（CI `44393241297`），`serve.log` 只有两行：

```
selfcheck: 0 deliverable(s), 1 defect(s)
  gate G1  fail: the contract lists no deliverables, so G1/G3/G4 had nothing to check
```

⇒ rubric 类题（指令是散文）**G0 抽不出交付物**，闸门等于空转。
所以"再加一条确定性检查"没有用；缺的是**能读懂指令、并且真的去打开文件的人**。

---

## 3. 落地了哪一条：Verify → Repair（新会话审阅）

只抄一条：**Verify 是第二个会话，不带 worker 的历史，自己打开交付物，只回 PASS 或缺口**。

改动（`harness.py`，commit `16f70c9`，分支 `aura-verify`）：

1. worker 的 Pi 会话跑完后，若它**没用完自己的时间**（剩余 ≥ `REVIEW_MIN_SEC` 60 s），
   起第二个 Pi 会话：`--append-system-prompt REVIEWER_NOTE`，stdin = 任务原文 +
   worker 摘要，要求逐份打开交付物，只回 `PASS` 或 `<path> | <缺口> | <怎么修>`。
2. 非 PASS ⇒ **恰好一次** Repair 会话（同一个 NOTE 系统提示），它的**自己的**最终文本
   成为答案；审阅者的文本永远不会变成答案（`session.final_message` 显式存回）。
3. 预算只花 worker 剩下的时间：`max(0, min(240, left − 90 − 30))`。
   没富余就不跑，日志写 `review: N s spare; skipped`，答案原样返回。
4. 任何异常都在 `solve()` 里被兜住，worker 的答案照发（和一贯的 fail-safe 同款）；
5. 只有**带 ` | ` 的缺口行**才触发 repair。审阅者若两次都没按格式回（例如写了一段散文），
   就不动答案——repair 会**覆盖**最终回复，宁可不修。

为什么是"机会式"而不是 AURA 的固定 30% 预留：我们的 dev CI 把
`agent timeout` 从 1800/4800 秒统一砍到 **900 秒**（workflow 的 *fit the task
resources* 一步）。fire-test 的 1.0 那次用了 673 s，固定预留 30% 会把它的工作
时间压到 630 s 以下，**直接制造回归**。机会式设计保证"需要全部时间的题一分不抢"，
只在有余量的题上生效。

新增 `test_review.py`（8 例，纯控制流，不碰网络/容器）：
无时间就跳过、PASS 收尾、缺口换一次 repair、审阅者挂掉答案不变、
审阅者的文本不会变成答案。

---

## 4. 五题前后分数

全部指 CI 的 `dev-score` run（模型 = `google-cloud/gpt-oss-120b` 替身，
agent timeout 被 workflow 统一砍到 900 s）。"前" = `main` @ `3dd55d9`，
"后" = 分支 `aura-verify` @ `c5209e5`。

| 题 | 前（main `3dd55d9`） | 后（`aura-verify`） | 证据 |
|---|---|---|---|
| fire-test-validation-memo | **1.0** | 跑完即补 | 前：run `44393241297`，673 s |
| landscape-sheet-revision | **0.104** | **0.961** | 前 `44475151129`（2410 s）/ 交付物优先 `44501387225`（2640 s） |
| roof-deck-design | **0.0** | v1 **0.0**（诊断有效）；v2 跑完即补 | 前 `44475156143`（983 s）→ v1 `44476508108`（1298 s） |
| wind-diaphragm-shear | **0.0** | **0.0** | 前 `44475161459`（1837 s）/ 后 v1 `44476514402`（1188 s，review PASS） |
| data-hall-mep-routing | **0.0** | 跑完即补 | 前：run `44475166446`，2047 s |

（上面 table 里的 0.026 是只加 Review 的那一版；交付物优先那版是 0.961，见 §4.3。）

landscape 的第一次后测是**负结果**，而且原因不在 review：这一次 worker 自己在 900 s 里
**什么都没交**（`output/` 目录不存在），review 用 7 条缺口准确描述了这个事实
（"The sole required deliverable is entirely absent"），repair 也跑了一轮，
但 240 s 的 repair 补不出一份本来就不存在的 memo。基线那次 worker 交了一份（0.104），
这次没交——**同模型同题的方差**（我们在 fire-test 上见过 1.0/0.0 两次）。
⇒ 这条增量修的是"交了但缺东西"，修不了"压根没交"。

wind 那题的 review 判 PASS，而确定性闸门报 2 个 defect——**闸门是错的**：
它把 card 里写的 `/filesystem/<task>/output/submission.zip` 当容器内绝对路径查，
容器里实际是 `/app/filesystem/...`（见下面 run 日志）。所以 wind 的 0.0 不是"文件没交"，
这条增量也救不了它——**这类题不属于 Verify 能修的那一类**。

### 4.1 中间版本（v1，被格式守卫挡住的那一版）

roof-deck-design 的后测暴露了这条增量的真实形态：

```
review: budget 240 s
review: 3 gap(s)
review: The submission zip does not exist. The output directory only contains `.keep`.
review: The worker's closing summary shows ... had NOT yet created any deliverable file.
review: Let me confirm there's no zip anywhere in the filesystem ...
review: verdict has no gap list; not repairing
```

三件事同时被证实：

1. **审阅者看得见真问题**——它自己发现 `output/` 里只有 `.keep`，worker 根本没交
   `submission.zip`；这正是这道题 0 分的原因（`selfcheck: 1 deliverable(s), 2 defect(s)`
   的确定性闸门没拦住，因为它只核对已存在的路径）。
2. **它不说我们要的格式**——`gpt-oss-120b` 把它写成了"边想边说"的散文，
   而 v1 的守卫要求 `path | gap | fix`，于是唯一的信号被丢掉，repair 没跑，分数没动。
3. ⇒ 这条增量要生效，**必须把散文也算 brief**。v2（`c5209e5`）就是这么改的：
   没有 ` | ` 时把整段散文压成一条 brief（≤1200 字）交给 repair，
   而不是丢弃。

---

## 4.2 判据（这次实测换来的，比分数重要）

五个仓库内 dev 题里，**4 个 checks 类题我们和 AURA 都是 0.0**（AURA 在
data-hall 上也是 0.0 两次）。review 日志给出的原因高度一致：

- roof v1：`output/` 里只有 `.keep`，worker 没交 `submission.zip`；
- landscape v2：`output/` 目录根本不存在；worker 的总结断在
  "Let me focus and now consolidate my analysis"；
- wind：review 判 PASS（文件在），闸门报"不存在"——**闸门把 card 的
  `/filesystem/...` 当容器绝对路径**，容器里其实是 `/app/filesystem/...`。

⇒ **0 分的主因不是"没人复核"，是 worker 在 900 s 内压根没把交付物写出来。**
AURA 的 Verify 修的是"交了但缺件/格式错"，修不了"没交"。
⇒ 下一刀应该切在**预算与交付物优先**（把 `submission.zip` 一类的骨架先写出来再研究），
而不是继续加复核层。这一条 AURA 的 Plan 阶段反而更对症——它花 10% 时间先把
"每个交付物的确切路径/格式/必须包含什么"写死，Work 再照单生产。
（本次没抄 Plan：时间只够落地一条，而 Verify 是它最有辨识度的那条。）

---

### 4.3 交付物优先（Plan + 骨架落盘）实测

**结论先行**：`landscape-sheet-revision` 从 **0.104 涨到 0.961**，这是这条线第一个
0.9 级的 checks/rubric 混合题；但同一版代码在 fire-test 上拿了 0.0，而那一轮的
serve.log 证明**不是骨架的锅**（见 4.4）。后来查明是**模型端点当晚退化**：
main 分支（基线代码、无 plan/无骨架）重跑 fire-test，17–35 min 的基线变成 97 min
仍未完成。


`landscape-sheet-revision`：**0.104 → 0.961**（run `44501387225`）。

日志给出的因果链是清楚的：

```
plan: budget 1200 s
plan: 1 deliverable(s) listed: ['app/files/output/DSR_Ph3_L4_Street_Tree_Memo_Callaway_10312026.md']
plan: skeleton app/files/output/DSR_Ph3_L4_Street_Tree_Memo_Callaway_10312026.md
plan: delivered 'app/files/output/DSR_Ph3_L4_Street_Tree_Memo_Callaway_10312026.md +'
review: budget 240 s
review: 1 gap(s)
review: repair session
```

plan 把"要交什么"钉死成一个路径，骨架先落盘，worker 拿到的是"改这份文件"
而不是"从零写一份"。产出的 memo 覆盖了 Condition 14、208 vs 58 棵树、11/30
能不能承诺这些 rubric 点（`artifacts/.../output/` 里 1 个文件，正文见 run）。

已知缺陷（下一刀）：plan 写的是 `app/files/output/...`（带 `app/files/` 前缀），
我们又拼了一次 workdir，骨架被写到 `/app/app/files/output/...` 这个嵌套路径。
本题无害（worker 按正确路径写了），但应剥掉前缀。

### 4.4 fire-test 0.0 的取证（run `44539486704`）

```
plan: budget 1200 s
phase: 1200 s budget spent
plan: unreadable (could not read /app/files/.plan.md: no such file or directory)
plan: 0 deliverable(s) listed: []
phase: 1499 s budget spent
review: budget 240 s
phase: task cap reached
review: no verdict; skipped
selfcheck: 0 deliverable(s), 2 defect(s)
  gate G1 output/ fail: output/ holds 0 entr(y|ies)
```

- `deliverables/` 只有 `final_message.md`(3734 字符) / `capture.json` / harness 自带的
  `tools/clash_check.py`；**`output/` 目录不存在，0 个产出文件**。
- `pi:` 工具调用计数 **0**：模型 2700 s 内一次工具都没调，最终回复断在
  "Let me analyze the CSV more carefully with pandas."
- 因为 plan 文件从未写出，`plan_text` 为空 ⇒ **WORK_PLAN_HEAD 不会注入**，
  worker 拿到的 prompt 与 1.0 基线一字不差；**骨架也一条都没写**（清单为空）。

⇒ 这一轮是"模型没动手"，不是"骨架占位污染 rubric"。同一晚 main 分支的对照跑
97 min 未完成（基线 17–35 min），指向端点退化。

为防再犯，已落两条（commit `bfc1736`）：
1. `PLAN_MAX_SEC` 1200 → **300 s**，且 plan 文件缺失时直接进 work；
2. **memo 类不写骨架**（`.md/.docx/.pdf/.txt/.rtf/.tex/.pptx` 返回 None），
   只给 `.zip/.py/.json/.csv` 这类"存在即判据"的交付物落骨架。

### 4.5 骨架之后：G1/G3/G7 全过、分数仍 0 的那道题

roof-deck-design 的交付物是 `output/submission.zip`（**结构型**，内含
`deck_design.xlsx`、`roof_deck_plan.dxf`、`constraint_register.xlsx`、
`design_basis.md`）。四轮演进：

| 轮 | run | output | 闸门 | 分数 |
|---|---|---|---|---|
| 无骨架（plan 空转） | `44599053335` | 1 文件（README zip） | G3 fail: 4/4 members absent；G7 fail | 0.0 |
| 成员骨架 | `44611561234` | 1 文件（4 成员骨架） | **G3 过**；G7 fail | 0.0 |
| G7 只改指令 | `44622608673` | 1 文件 | G7 仍 fail（worker 到 60 min 仍在设计，没写收尾） | 0.0 |
| 指令 + harness 补 `delivered:` | `44633765518` | 1 文件 | **G1/G3/G7 全过（无 defect）** | 0.0 |

⇒ 结论：**模板骨架能让"文件存在/成员齐全/总结点到路径"三条全过；分数 0 是内容问题**——
checks 类判的是每行数字（zone 的 gage/pattern、容量对需求、成本），占位内容过不了。

## 5. 结论：AURA 里哪一条值得抄

**值得抄的只有 Verify→Repair 这一条，而且是它的"新会话 + 只给缺口"形态，不是它的阶段划分。**
理由：

- AURA 自己在 `data-hall-mep-routing` 上也是 **0.0**（两次），说明 Verify 修不了
  "模型自己臆造了一个常量、自查器又用同一个常量当尺子"这类错（见 `logs/CI42-ANALYSIS.md`）。
  它修的是**另一类**：交付物缺件、格式不对、要求的点没写进去。
- 我们的闸门在 rubric 题上 `0 deliverable(s)`，正好是 AURA Verify 覆盖的那一类。
- 不抄 Plan 阶段：我们的 `NOTE` 系统提示已经把"先读全部输入、先写一个能跑的交付物、
  每个点一句话"写进去了，再加一层 plan 是重复计费。
- 不抄它自研的 engine / telemetry：我们跑在 Pi 上，Pi 自带 compaction 与重试；
  换引擎的收益远小于风险。

---

## 6. 来源

- Devpost：`https://devpost.com/software/aura-harness-ple`（2026-11-15）
- 仓库：`https://github.com/wenizmohamed/aura-harness-ple`（HEAD `f13b80b`）
- 官方判分口径：`https://github.com/google-research/ple`
