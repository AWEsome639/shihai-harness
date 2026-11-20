# 单题时限：3,600 s 是谁定的，加预算能不能救「计算类」

日期 2026-11-20。只动了一个变量：`roof-deck-design` 的单题硬停 3,600 → 7,200 s。
没碰 judge、没碰交付物内容、没读 `verifier/grade.py`、没提交。

---

## 0. 三行结论

1. **3,600 s 是我们的设置，不是平台限制。** `harness.py:91` `MAX_RUN_SEC`（默认 3600，可用环境变量覆盖）；
   PLE 官方 README 写的是每题统一 **6 小时**，`[agent] timeout_sec = 21600`。实测拿到手的是 **21,599 s**。
2. **加到 7,200 s 没救回来**：run `44657305537` 的 zip 仍是 4 个骨架成员（字节数与计划阶段一致、mtime
   停在计划时刻），分数 **0.0**，判分器同一句 `sheet 'zones' missing`。工作阶段跑了 5,377 s（基线 3,509 s，多 31 分钟）
   却一次都没往 `output/` 写；而且工作阶段没撞 cap（serve.log 里没有 `phase: ... budget spent` 行），
   7,200 s 只用到约 5,470 s，还剩约 29 分钟。
3. **不值得为计算类统一加预算。** 卡的不是时钟，是「永远不写一份有效交付物」这个行为；而且这发的第二小时
   尾部（约 93 分钟后）模型端点零响应，多出来的时间连「思考」都没换成。

---

## 1. 时限来源取证（①）

### 1.1 3,600 s：我们自己的 harness 常量

```
harness.py:91   MAX_RUN_SEC = float(os.environ.get("MAX_RUN_SEC", "3600"))
```

同一处的注释（改动前）：

> Hard stop for one task, whatever budget the evaluator hands out. A dev run
> once inherited a six-hour allowance and a single task sat for two hours; the
> skeletons are on disk long before this, so the cap only costs polish.

出处：commit `894d86f`（2026-11-20 03:30），提交信息自述 ——

> the baseline fire-test run needed 2,791 s to finish and the 2,700 s cap cut
> the last of it: `MAX_RUN_SEC` is now 3,600 s.

⇒ 3,600 的来源是「fire-test 基线用了 2,791 s，2700 s 的旧上限切掉了尾巴」，**不是任何平台规定**。

### 1.2 平台/规格书：每题 6 小时

`README.md:48`（PLE dev set 卡片）：

> Every task runs with a uniform **6-hour agent time limit** and no internet
> access beyond model API calls …

`README.md:99`：

> Note for standalone Harbor: every `task.toml` already sets the 6-hour agent
> time limit (`timeout_sec = 21600` under `[agent]`).

`README.md:72`：竞赛 kit 的 `eval/run.py`「**applies the 6-hour agent time limit**」。

协议样例 `AGENTS.md:154`：

```json
"limits": {"agent_timeout_sec": 21600, "exec_timeout_sec": 600, ...}
```

### 1.3 实测：这一发跑拿到手的就是 21,599 s

run `44633765518`（旧上限 3,600 s 的最后一发）`agent/my-harness.txt` 第 1 行：

```
task sent: workdir /filesystem, 21599 s left, 0 MCP server(s)
```

⇒ 判据：**远大于 3,600**。`MAX_RUN_SEC` 是唯一在 3,600 s 处截断它的人。

### 1.4 一个容易看错的数（记下来免得下次再吵）

- `data/tasks/roof-deck-design/task.toml:26` 的 `[agent] timeout_sec = 1800`；
- CI `.github/workflows/dev-score.yml` 的 fit 步又把 1800 改写成 **900**（注释说是为了 dev 循环别等两小时）。

但实测拿到 21,599 s ⇒ **卡里的这个数在我们这条 dev 链上不生效**，真正生效的是 kit 的 6 小时。
（如果哪天换回 Harbor 直跑，这条要重新量。）

---

## 2. 加预算前的状态（基线，3600 s）

run `44633765518`，同一题、同一模型、`attempts=1`。

| 项 | 值 | 出处 |
|---|---|---|
| 计划阶段 | 90 s，没写出 `.plan.md`，从任务卡推出 1 个交付物 | `agent/serve.log` |
| 骨架 | plan 阶段先落 `output/submission.zip` | `agent/serve.log` |
| 工作阶段 | **3,509 s**，然后 `phase: task cap reached` | `agent/serve.log` |
| review | 被 cap 挤掉，`no verdict; skipped` | `agent/serve.log` |
| selfcheck | 1 交付物，0 defect（闸门全过） | `agent/serve.log` |
| pi 请求 | **76**；工具调用 **67**（bash 65、read 2） | `agent/my-harness.txt` |
| zip 成员 | 4 个齐全，但**全是骨架** | 见下 |
| 分数 | **0.0**；`parse_ok: false`，`parse_errors: ["sheet 'zones' missing"]` | `verifier/metrics.json` |

zip 成员实测字节（`unzip -l` / `unzip -p | wc -c`）：

```
deck_design.xlsx          4,843
roof_deck_plan.dxf           36
constraint_register.xlsx  4,843
design_basis.md              60   ← "Placeholder written by the harness; the worker replaces it."
```

最后 3 次工具调用仍全是 `bash`（还在算），一次都没回头写 zip。

⇒ 基线判据：**不是「没东西可写」也不是「闸门拦住了」，是 60 分钟全花在设计计算上，zip 停在骨架。**
⇒ 分数 0 的直接原因是判分器解析不到 `zones` 表（骨架），而不是内容判错。

---

## 3. 实验：只改时限（3600 → 7200）

commit `d88a16f`（唯一改动）：

```python
TASK_MAX_RUN_SEC = {"roof_deck_design": 7200.0}

def run_sec_for(instruction: str) -> float:
  for key, sec in TASK_MAX_RUN_SEC.items():
    if key in instruction:
      return max(MAX_RUN_SEC, sec)
  return MAX_RUN_SEC
```

调用点：`cap = run_sec_for(session.instruction) - (time.monotonic() - started)`。
其它 28 题仍是 3,600 s；`MAX_RUN_SEC` 环境变量仍是全局默认。

跑法（与基线逐项相同，只有时限不同）：`workflow_dispatch` → run `44657305537`，
`tasks=roof-deck-design`、`trials=1`、`attempts=1`、模型不覆盖。

---

## 4. 结果（②）

run `44657305537`，commit `d88a16f`，2026-11-20 01:57:18Z 起、04:01:15Z 结束（**2 h 04 min**），
`attempts=1`、模型同为 `gpt-oss-120b`（与基线一致）。

| 项 | 3,600 s（`44633765518`） | 7,200 s（`44657305537`） |
|---|---|---|
| 计划阶段 | 90 s | 90 s |
| 工作阶段 | 3,509 s（**撞 cap**） | 约 **5,377 s**（**未撞 cap**；离 7,200 还差约 29 min） |
| review | 被 cap 挤掉，skipped | 240 s（超时；拿到 1 条乱码 gap） |
| repair | 无 | 1,493 s（**撞 cap**；端点零响应） |
| pi 请求 | 76 | **109** |
| 工具调用 | 67（bash 65 / read 2 / write 0） | **99**（bash 88 / read 7 / write 3 / edit 1） |
| 写交付物 | **0** | **0**（3 次 write 全给辅助脚本 `extract_ifc.py`） |
| zip 成员 | 4 个骨架，4,843 / 36 / 4,843 / 60 B | 4 个骨架，4,842 / 36 / 4,842 / 60 B |
| 分数 | 0.0 | **0.0** |
| 判分器 | `parse_ok:false`，`["sheet 'zones' missing"]` | **同一句** |

**工作阶段时长是倒推的**：`7,200 = 90(plan) + W + 240(review) + 1,493(repair)` ⇒ `W ≈ 5,377 s`
（serve.log 里工作阶段没有 `phase: ... budget spent` 行 ⇒ 它不是被 cap 砍的）。

**zip 还是骨架的硬证据**：

```
deck_design.xlsx          4,842
roof_deck_plan.dxf           36
constraint_register.xlsx  4,842
design_basis.md              60   ← 内容仍是 "Placeholder written by the harness; the worker replaces it."
zip mtime: 11-20-2026 02:01   ← 计划阶段写骨架的时刻（会话 02:00:23Z 开始）
```

工具的 write 只有 3 次，路径全是 `.../extract_ifc.py`；`output/` 下的交付物**一次都没写**。

**必须记下的混淆项：模型端点在这一发的后半段退化。**
同一模型 `gpt-oss-120b`，但 `model error` 从 2 次涨到 **20 次**、`auto_retry_start` **19 次**；
`egress.log` 显示约 92 分钟后 review/repair 的每次连都是「上行 263,735 B、下行 **0 B**」，连着 5 次
（94m11 / 99m19 / 104m36 / 110m07 / 116m08），119m40 还在新建连接，120m00 被 cap 砍掉。
⇒ 第二小时最后的 ~28 分钟不是在算，是在等一个不回话的端点。

---

## 5. 值不值得为计算类统一加预算（③）

**不值得按现状推广。** 三条判据，按证据强度排：

1. **行为没变**：多给的 30 分钟继续花在读输入（IFC/DXF 几何）上，`output/` 零写入；
   比基线多出的 33 次请求、32 次工具调用里没有一次是写交付物。
2. **cap 不是这次的瓶颈**：工作阶段只用到约 5,470 s / 7,200 s 就结束了（serve.log 没有 `phase: ... budget spent` 行），
   还剩约 29 分钟。它到底是「模型收了尾」还是「端点把它掉了」这份日志分不出来，
   但两种解释下 zip 都没动。
   ⇒ 加预算买不到「多算一会儿」，只买到「同样的行为多跑一会儿」。
3. **单价翻倍**：计算类每题 2 h，dev 循环从每题 ~1.1 h 变 ~2.2 h，CI 分钟数同理，换来的增量是 0。

**诚实的保留**：单发 + 端点退化，不能证明「健康的 2 h 也救不了」。要翻这个结论，需要的是
**端点健康时的第二发**，而不是先把预算铺到整类题上。

**下一步该改的不是时钟**：现在 harness 只保证「骨架文件存在」，而这个类的骨架过不了判分
（`sheet 'zones' missing`）。该做的是**按预算比例强制写一份真 v1**（例如预算 40% 时把骨架换成真内容、
只剩润色），这是另一个实验，不是加秒。

**代码回滚**（建议，等第二发或直接回）：删 `TASK_MAX_RUN_SEC` 那 4 行、`cap` 改回 `MAX_RUN_SEC`；
或最省事——把 `7200.0` 改回 `3600.0`，其余不动。

---

## 附：证据怎么复现

```bash
# 时限来源
sed -n '86,112p' ~/.dsh/work/ple/harness.py
grep -n "6-hour agent time limit\|timeout_sec = 21600" ~/.dsh/work/ple/README.md

# 任一发的实测时限
gh run download <run-id> -n results -D /tmp/x && head -1 /tmp/x/attempt1/jobs/dev/*/agent/my-harness.txt

# 分数与解析错误
cat /tmp/x/attempt1/jobs/dev/*/verifier/metrics.json
```

---

## 6. 给下一个人

- **3,600 s 是我们自己拍的**（`harness.py:91` `MAX_RUN_SEC`），平台给每题 **6 小时**（`README.md:48` / `README.md:99`）。
- **计算类 0.0 的原因是模型不写交付物**，加秒无效：7,200 s 那发 `output/` 零写入、zip 仍是骨架，而且 cap 根本没触发。
- **若要再试，唯一值得做的是「端点健康时补第二发」**，下注前先看 `model error` / `auto_retry_start` 计数（这一发 20 / 19，后段每次连接下行 0 B）。
