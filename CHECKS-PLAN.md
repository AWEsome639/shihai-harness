# CHECKS-PLAN — checks 类交付物：要求、0 分根因、三条补丁

> **范围**：`data/tasks/{fire-test-validation-memo, data-hall-mep-routing, landscape-sheet-revision}`（已提交进仓库的三道）
> **状态**：分析与补丁设计，**未落地**。本次只写本文件；`data/tasks/**` 与 `harness.py` / `contract.py` / `selfcheck.py` 一个字未改（`git status --short data/tasks harness.py contract.py selfcheck.py` 为空）。
> **证据口径**：代码行号 = 本仓库 HEAD `1be1fc9`（2026-11-18 13:54 +0800）；`logs/*.log` 行号 = 文件物理行；CI 日志时间是 **UTC**，git 是 **+0800**，差 8 小时，这个坑见 §2.1。
> **补丁形状**：三条补丁都在 §4，附完整 unified diff（`patch -p1` 可直接用）；测试补丁在每条的「测试」小节。落地顺序 P1 → P2 → P3（后面的 diff 基于前面已应用的状态，见 §5 的构建脚本）。

---

## 0. 结论

1. **三道题 = 2 道 rubric（自由 `output/`）+ 1 道 checks（脚本产 IFC）**。rubric 侧现状不差：`landscape-sheet-revision` 在 `logs/ci47-landscape.log:486` 拿了 **0.727**（260 s）。
2. **`data-hall-mep-routing` 跑了三次，三次都是 0，但死法各不相同**（同一模型 `google-cloud/gpt-oss-120b`）：`ci42` 7066 s → **`ERR_CONNECTIVITY`**（模型有、schema 过、无碰撞，连通 0）；`ci44` 2271 s → **`ERR_NO_SUBMISSION`**（一个文件都没写）；`ci45` 2612 s → **`ERR_CLASH`**（模型有、schema 过，碰撞 0）。详见 §2.0。
3. **本文主案是第二种（没交）**，因为它是三次里唯一**不靠模型变强**就能消除的 0：kit 的 artifact capture 收的是"相对镜像有变化的文件"（推断，由日志推出，未读 kitrepo 源码），一个没写 → `empty data-hall-mep-routing (0 file(s))`（`logs/ci44.log:495-496`）→ verifier 拿到镜像自带的 stub → `evaluate_submission.py:92-94` 命中 `meta.json:10` 的 `stub_sha256` → `ERR_NO_SUBMISSION / "submission stub was never replaced"`（`logs/ci44.log:619-624`）。
4. **提示词当时没有任何一句提到 stub**：失败那轮跑的是 `1b7ae38`（`logs/ci44.log:101` 的 rev-parse），`git show 1b7ae38:harness.py` 的 NOTE 里 `grep -c stub` = **0**，「Never answer a task with nothing」是 **8 分 24 秒之后**的 `641c3c1` 才加的，stub 那句是两天后的 `547468b`。
5. **四道门当时还不存在**（`90649f2`，2026-11-18 08:31 +0800）；**即使按今天 HEAD 的四道门重放，stub 也是全绿**——实测同一现场输出 `selfcheck: 3 deliverable(s), 0 defect(s)`。
6. **全绿的原因是三层叠加**：G1 只查"普通文件 + 非空"（stub 536 B 通过）；G3 对 `.py` 只做 `compile()`（stub 能编译）；G7 跳过 `required=False`，而 `contract.py` 因为 ±200 字符窗口里出现了隔壁路径的 `(default ...)`，把**三个交付物全判成了非必需**（实测 `flags` 全是 False）。
7. **对 checks 类真正致命的两个盲区**：`zip` 的成员清单/根目录/空成员全不查（实测 3/4 成员缺失仍全绿），`ifc` 只看首行 `ISO-10303-21`（三种垃圾头全绿）。`.docx/.pdf` 则完全不解析，而 `fire-test-validation-memo` 的 208 分全在 `.docx` 里。
8. **三条补丁**（按性价比）：**P1 提示词**（唯一能改分数的，零代码风险）→ **P2 让 stub 显形**（contract 必需位 + `py_stub` 检测，≈40 行）→ **P3 G2 归档门**（zip 成员 + 成员抽取，覆盖 9/12 道 checks 题，≈80 行）。本地实测：selfcheck 用例 20 → 26 全过，contract 用例 7 → 9 全过。

---

## 1. 三道题的要求表

### 1.1 总表

| # | 题 | 评分族（证据） | 交付产物（题面原文 + 行号） | 格式 / 校验 | 验收要点 | 我们四道门的现状（实测） |
|---|---|---|---|---|---|---|
| 1 | `fire-test-validation-memo` | `rubric` / `weighted_rubric`；**48 条 criteria、总权重 208、12 条 Critical**；`expected_file_type` 只有 Word（`tests/grader/criteria.json`） | `instruction.md:9`「Give it to me as a Word memo」；`:11-17`「Save the files that make up your answer under `output/`」——**没有文件名、没有扩展名** | `.docx/.doc/.odt`；判分读 `output/` + 最终消息 | §1.2 | `contract.extract` → **deliverables = []**；只剩 `harness.py:353-369` 的 `ls -A output \| wc -l` 兜底；`.docx` 无解析（§3） |
| 2 | `data-hall-mep-routing` | `checks`；`task.toml:2` 的 artifacts **只有** `.../output/solution.py`；`tests/test.sh:104` `CANDIDATE=output/solution.py` | `instruction.md:48-52`：`output/solution.py` 一个自足脚本，用 ifcopenshell 建模型并写一个 IFC4 到 `sys.argv[1]`（默认 `output/model.ifc`）；`:74-76` `output/reconciliation.json` 是**可选**诊断 | IFC4 / `ifcopenshell==0.8.4`（+numpy）；项目单位 mm、placement 位移用 m；只许 2D profile；指定空间结构与元素类（racks=`IfcFurniture`/`USERDEFINED`/`"Rack"`）；`Pset_DCM_*`；`express_rules=True`；300 s 无网确定性；**不许回读 PNG**（把读图结果 bake 进脚本） | §1.2 | `deliverables` 抽出 3 条但**全部 `required=False`**；stub 全绿（§2.5） |
| 3 | `landscape-sheet-revision` | `rubric` / `weighted_rubric`；**18 条 criteria、总权重 77**；`expected_file_type` 含「All output (modified files **and final message in console**)」+ Word | `instruction.md:1`「One memo to Callaway for my signature, dated 10/31/2026」；`:3-9` `output/`——同样没有文件名 | `.docx`（+ 最终消息也算产出） | §1.2 | 同 1：`deliverables = []`；但注意 rubric 明说**最终消息也算产出**，与提示词「final reply is not a deliverable」有张力 |

### 1.2 逐题细节

**① fire-test-validation-memo（rubric）**

- 产物：一份 Word memo（`instruction.md:9`），落在 `output/`（`:11-17`）。题面没有给文件名，只说"what gets reviewed when you finish is `output/` … and your final message"（`:14-16`）。
- 题面点名的交付内容（`:5-9`，逐条可核对）：① 装配是否就是认证覆盖的那台、依据是什么；② 控制准则与到达时间；③ §7 stub 里的预测是否成立；④ 采用的实验值 + 对 Bianca 数字的处置；⑤ error / acceptance criterion / uncertainty / error 与后两者的比较；⑥ 仍能通过准则的最大预测值；⑦ 两组比较（两准则间隔分钟数 vs run 中同样间隔；governing point 高出九点均值多少）；⑧ rework 归属 + 是否已开工 + 对 Phase 1 的影响；⑨ wildfire claim 能/不能证明什么 + 如何补 + 未来比较必须以什么形式报告；⑩ Bianca 数字若被证实对 certified rating 的影响、谁有权行动；⑪ stamp language；⑫ checklist disposition（哪条能关、其余为何仍开）。
- 格式硬点（`:9`）：「crossing times rather than the samples that trip them」「times to the nearest minute」「limits as they fall」「every number carrying its unit」，另加「a short paragraph I can send Bianca today」。
- 评分面：48 条 criteria（12 Critical / 2 Major / 34 Minor），`criterion_type` 45 条 Reasoning、3 条 Extraction，`expected_file_type` **全是 Word**——交 `.md`/`.txt` 等于 208 分全丢。（交底里 `fire-test` 的四次真跑 1.0 / 0 / 0 / 0 不在本机日志里；本机 `logs/ci4*` 是 data-hall ×3 + landscape ×1，见 §2.0。方差问题不在本次范围。）

**② data-hall-mep-routing（checks）**

- 产物：`output/solution.py`（唯一被评分文件，`task.toml:2`）。它必须自己写出一份 IFC4；`output/model.ifc` 是脚本的默认输出（`:50`），`output/reconciliation.json` 是可选诊断（`:74-76`）。
- 硬约束（`:28-45`，全部机器校验）：0 硬碰撞（>2 mm）；每个 service 是**一个**连通网并接到 service entry；每行 ≥80% 长度被三服务覆盖（2500 mm 内）；CHW 2500 mm 内到每个 CRAH；2600 mm 净高；喷头下 460 mm + NFPA 13 的 450 mm；>1200 mm 宽与 containment 屋顶下要加喷头；tray 与 busway 至少一轴 ≥300 mm；PDU 上方 1800 mm 专用空间不得走 CHW；25.4 mm 保温计入碰撞；地板下 plenum 可用。
- 评分（`:49-52`）：schema gate → clash gate → connectivity gate（任一失败即 0），然后 `score = compliance × min(1, L_opt / L_yours)`。
- 诊断项（`:54-76`）不计分：净空、地上/地下长度、标高分层、支撑吊架、drop/termination、reconciliation 日志。

**③ landscape-sheet-revision（rubric）**

- 产物：一份给 Callaway、供签字、日期 10/31/2026 的 memo（`instruction.md:1`），`output/`（`:3-9`）。
- 验收（criteria 里权重最高的几条）：11/30 复报日期在「Comprehensive Planning 出 significant-change determination」之前**不能承诺**（w=10）；street tree revision 是否落在 Condition 14 范围内（w=8）；Condition 14 of TM-22-500041（dated 03/14/2022）是否管这件事（w=7）；streetscape planting 是否 HOA 的 common area；是否已发 significant-change determination。
- 注意：`expected_file_type` 写了「All output (modified files **and final message in console**)」——这道题的 rubric **把最终消息当产出读**。也就是说 G7 在这道题上不是"礼貌检查"，是判分面的一部分；反过来，提示词里「Your final reply is not a deliverable and is not graded as one」在这道题上是**错的**（至少对 judge 而言）。本次不改，但记在这里。

---

## 2. `data-hall-mep-routing` 为什么是 0：完整链条

### 2.0 同一道题三次 0 分，三种死法（先看这个，避免把根因看成单点）

| 运行 | 墙钟 | capture | verifier `exec_status` | 死在哪 | 证据 |
|---|---|---|---|---|---|
| CI42 | **7066 s** | `ok (1 file(s), 3.0 MiB)` | `ERR_CONNECTIVITY` | `gate_0` schema 过、`gate_1_no_hard_clash: true`，**连通门 0** | `logs/ci42.log:486,495,691-692,700-706` |
| CI44 | 2271 s | **`empty (0 file(s))`** | `ERR_NO_SUBMISSION` | **一个文件都没写**，verifier 跑的是自带 stub | `logs/ci44.log:486,495-496,619-624` |
| CI45 | 2612 s | `ok (1 file(s), 1.3 MiB)` | `ERR_CLASH` | `gate_0` schema 过、**`gate_1_no_hard_clash: false`** | `logs/ci45.log:486,495,691-692,700-706` |

三个结论：

1. **模型不是"完全不会"**：三次里有两次写出了能过 schema gate 的 IFC（CI42/CI45）。缺的是 clash 与 connectivity 这两道**工程**门——那不是提示词或自检门能直接修好的，是模型能力 + 自查工具的问题。`tools/clash_check.py` 在三次运行里都在容器里（`harness.py:414-421` 在 `solve()` 开头写入），两次仍然没过 clash/connectivity，说明"给个检查器"不等于"模型会用它把门过掉"。
2. **"没交"是第三种，也是唯一可以 100% 从 harness 侧消灭的**：CI44 花了 2271 s / 57.2M input tokens 读图，最后**连一个文件都没留下**。这一类的期望值不是"拿分"，是"**不要连判分入口都没进**"。
3. **三道门都在 verifier 侧，且任一失败直接 0**（`instruction.md:49-52`：schema → clash → connectivity each 0 if failed）。所以对这道题，harness 侧最划算的动作是把"没交"清零，把"交了什么"变成可诊断的日志（§4），而不是幻想一次补丁把 0 变 1。

### 2.1 时间线（CI 是 UTC，git 是 +0800）

| UTC | +0800 | 事件 | 证据 |
|---|---|---|---|
| 11-15 16:46:23 | 11-16 00:46:23 | CI44 job 开始 | `logs/ci44.log:1` |
| 11-15 16:46:25 | 00:46:25 | checkout `1b7ae38d131f82db90b4a18fdaedd47349fa1164` | `logs/ci44.log:101-104` |
| 11-15 16:48:17 | 00:48:17 | 建 data-hall 镜像 | `logs/ci44.log:482` |
| 11-15 17:27:11 | 01:27:11 | 结果 `data-hall-mep-routing 0.000 ok (2271 s)` | `logs/ci44.log:486` |
| 11-15 17:27:11 | 01:27:11 | capture：`empty data-hall-mep-routing (0 file(s), 3.8 MiB)` / `refused: nothing this task's grader reads` | `logs/ci44.log:495-496` |
| 11-15 17:27:11 | 01:27:11 | `ERR_NO_SUBMISSION` / `submission stub was never replaced` | `logs/ci44.log:619-624` |
| 11-15 17:35:35 | 11-16 01:35 | `641c3c1` 才加「Never answer a task with nothing」——**跑完 8 分 24 秒之后** | `git log` |
| 11-17 16:39 | — | `547468b` 才加「A stub the instruction pre-creates … scores exactly zero」 | `git log` |
| 11-18 08:31 | — | `90649f2` 才有四道门 | `git log` |

> 失败那轮 = `1b7ae38`。`git show 1b7ae38:harness.py | sed -n '/^NOTE = (/,/^)/p' | grep -c stub` → **0**；同一份 NOTE 里也没有 "nothing"。

### 2.2 失败现场（数字都来自 ci44.log）

- 耗时 **2271 s**、**854** 次请求、input **57,218,414**（其中 cache 56,424,192）、output **184,723**（`:486,604-609,700`）。agent 预算是 1800 s（`task.toml:32`），2271 s 是含 verifier 的墙钟，但它确实把读图预算用干了。
- 它的最终消息（`:703`，`:720` 复述）：
  - 「I have not produced a model. I reached the end of a working budget while still inside drawing interpretation, and writing a route now would mean guessing the numbers the gates are measured against — **which the task explicitly says is worse than delivering nothing**.」
  - 「`.../output/solution.py` **is still the original raising stub**.」
  - 「The correct call is `ENTRY_UNKNOWN` and stop, which is where the deliverable stands.」
- **"the task explicitly says" 是幻觉**：`grep -n "guess" data/tasks/data-hall-mep-routing/instruction.md` 无命中；"nothing" 只出现在「nothing collides」（`:7`）和「nothing else」（`:10`）。题面既没说"猜比空更糟"，也没说"可以停"。这句话是模型自己编的规则，用来给"空交"盖章。

### 2.3 为什么"一个文件都没写"等于 0

1. 环境镜像自带 `output/solution.py`：**12 行 / 536 B**，第 12 行 `raise SystemExit("dc_model submission stub was never replaced: no model was authored.")`（`environment/filesystem/.../output/solution.py:1-12`）。
2. kit 的 artifact capture 收的是**相对镜像有变化的文件**（推断：由下面的 `0 file(s)` + `nothing this task's grader reads` 推出，未读 kitrepo 源码）；agent 没写任何文件 → 该题 artifact 是空集：`empty data-hall-mep-routing (0 file(s), 3.8 MiB)`、`refused: nothing this task's grader reads: no file under files/`（`logs/ci44.log:495-496`），并直接提示「data-hall-mep-routing would count 0 on the server」（`:582`）。
3. verifier 于是只拿到镜像里的 stub：`tests/test.sh:104` 把 `output/solution.py` 当候选，`evaluate_submission.py:86-94` 先算 sha256，命中 `meta.json:10` 的 `stub_sha256` 就返回 `ERR_NO_SUBMISSION`，score 0（`logs/ci44.log:619-624`）。
4. 注意这个 hash 是**逐字节**的：改一个注释就能绕过它，然后脚本会被真的执行、在第 12 行 raise，仍然 0 分（状态变 `ERR_*`）。所以"靠 hash 兜底"不是防线，只是分类器。

### 2.4 提示词为什么没拦住

- **当时没有相关句子**：`1b7ae38` 的 NOTE 里只有「Deliverables are FILES, not prose … list the output directory and confirm each required file is there and **non-empty**」。stub 恰好**存在且非空**——这句话被 stub 100% 满足。这是最要命的一处：我们把"存在且非空"当成了"交了"。
- **现在的句子是"后果"不是"动作"**（`harness.py:75`）：「A stub the instruction pre-creates and you never replace scores exactly zero」。它没说 t=0 该做什么：没有"打开题面点名的文件看头 20 行"，没有"先写一个能跑的再研究"，也没有"如果你的产物是脚本，按 grader 的命令跑一遍"。而且这 1073 个字符的三个段落被**并进了一个字符串字面量**（`547468b` 的 diff 就是这个形状），在 prompt 里是一大块连续文字，模型很难把它当成一条首步指令。
- **没有任何"跑自己的脚本"的要求**：全篇 NOTE 只有「If the workspace has a validator, checker, or scoring script, run it」（`harness.py:87-89`）和 clash_check 那段（`:90-93`）。对一个"交脚本"的题，缺的是「用 grader 的调用方式端到端跑一遍，确认它产出的文件出现且能解析」。
- 附带：`fire-test`/`landscape` 的题面没有文件名，靠 `harness.py:353-369` 的目录计数兜底；一个 `.md` 也能过——但 criteria 全是 Word（§1.2）。

### 2.5 四道门为什么没拦住

四道门是**跑完之后**才跑的，而且只写日志，不回灌模型、不影响分数：

- `harness.py:485-493`：`run_process` 返回后才调 `_audit`；`_log_audit`（`:373-395`）只 `print` 到 stdout。`TASK-REQUIREMENTS.md:217-232`（§5.5）设计的是「写盘 → SELFCHECK → 有 fatal 回灌 → 模型修 → 重跑」，实际只实现了"写日志"这半边。
- **G1**（`selfcheck.py:695-780`）：存在、非符号链接、非目录、0 < size ≤ 95 MiB。stub 536 B → 通过。
- **G3**（`selfcheck.py:411-429`，`_analyze_py`）：只 `compile(text, path, "exec")`。stub 是合法 Python → 通过。**"能编译"与"是提交物"之间没有检查**。
- **G4**（`selfcheck.py:809-861`）：需要 `contract["vocab"]` / `numeric_cols`；本题两边都空（实测 `vocab=[]`、`numeric_cols=null`）→ 整道门空转。
- **G7**（`selfcheck.py:863-919`）：`if not item["required"]: continue` —— **三个交付物全被 `contract.py` 判成 `required=False`，所以 G7 一个都不查**。就算查了也没用：失败那条最终消息里 `solution.py` 和 `model.ifc` 都出现了（实测 `"solution.py" in fm` = True、`"model.ifc" in fm` = True），G7 只查"有没有点名"。
- **`required=False` 的根因**（`contract.py:126-129` + `454-469`）：`_OPTIONAL = ...\bdefault\b`，而 `_optional_here` 在路径首次出现处 ±200 字符里找这个正则。`output/solution.py` 首现于 `instruction.md:48`，同一句的后半截就是「(default `output/model.ifc`)」——于是**隔壁路径的 "default" 把主脚本也判成了可选**。实测：

  | 路径 | HEAD 的 required | 触发词（窗口原文） |
  |---|---|---|
  | `output/solution.py` | **False** | `default`（来自 `(default output/model.ifc)`，距离 ~40 字符） |
  | `output/model.ifc` | **False** | `default` |
  | `output/reconciliation.json` | False（正确） | `optional side file` / `You may write` |

- **同一现场重放（HEAD 模块，stub + model.ifc + reconciliation.json + 失败那条最终消息）**：

  ```
  selfcheck: 3 deliverable(s), 0 defect(s)
  flags: [('output/solution.py', False), ('output/model.ifc', False), ('output/reconciliation.json', False)]
  ```

  也就是说：**今天上线这四道门，这道题仍然会拿到干净日志、0 分、一模一样。**

### 2.6 三个系统性缺口（比单个 bug 重要）

1. **门在环外**：四道门是"事后尸检日志"，不是"发车前检查"。要改成环内（或第二轮补交）才有阻止能力（§6 说明为什么本次不做）。
2. **capture 是差分的**：不写 = 空集。所以"没写"与"写错"在服务器上是两种状态（`ERR_NO_SUBMISSION` vs 低分），而我们把资源都花在提高"写对"的概率上，没花在"保证写了"上。
3. **没有 G6（跑脚本）**：`TASK-REQUIREMENTS.md:199` 定义了 G6（重跑两遍比 sha256、记墙钟、断网跑），`selfcheck.py` 的 docstring（`:27-33`）也明说"G0/G2/G5/G6 未实现"。对 checks 类的"交脚本"题，**没有比"把脚本按 grader 的方式跑一遍"更直接的检查**——stub、`raise SystemExit`、故意没写的 `if __name__`，都能被 G6 一次抓住。

---

## 3. 四道门对 checks 类够不够

### 3.1 覆盖矩阵（全部实测，fixture 见附录 A）

| 产物 | G1 存在/类型/大小 | G3 格式 | G4 词表/占位符 | G7 最终消息点名 | 综合 |
|---|---|---|---|---|---|
| `.py` = 自带 stub（536 B） | ✅ 通过（普通文件、非空） | ✅ 通过（能编译） | — 空转 | ✅ 通过（且 `required=False` 时直接跳过） | **全绿**（= 0 分） |
| `.py` = 真脚本但运行时什么都不写 | ✅ | ✅ | — | ✅ | **全绿**（只有 `model.ifc` 缺失会报；P2 后为 fatal） |
| `.ifc` | ✅ 缺失/0 B 能抓 | ⚠️ **只看第一行**有没有 `ISO-10303-21` | — 空转 | ✅ | 抓到"根本没有/不是 STEP"，抓不到 schema、`express_rules`、实体数、单位、Pset、几何 |
| `.zip` | ✅ 缺失/0 B 能抓 | ⚠️ 能开 + 成员非空 + **只解析 CSV 成员** | ⚠️ 仅当 contract 里有 vocab | ✅ | **成员缺、根目录、空成员、绝对/`..` 路径、重复名，全绿**（5 个 fixture 实测） |
| `.docx` / `.pdf` | ✅ | ❌ **没有解析器**（实测：`.docx` 里塞二进制垃圾、`.pdf` 里塞文本，全绿） | — | ✅ | 只查"在不在"，而 fire-test 的 208 分全在 `.docx` |

### 3.2 逐门：能抓什么 / 抓不到什么

- **G1**：能抓"没交/空文件/交了目录或链接/超过 95 MiB"。抓不到"文件在但不是提交物"——本案例正是这一格。它也无法区分"模板文件"与"作者写了但内容为默认值"。
- **G3（zip）**：能抓"不是 zip / 空 zip / 成员里的 CSV 表头坏或编码坏"。抓不到：**成员清单**（题面点名 4 个、只交 1 个 → 绿）、**成员在不在根**（全塞 `output/` 下 → 绿）、**0 字节成员**（→ 绿）、**绝对路径 / `..` / 重复名**（→ 绿）、**非 CSV 成员的内容**（`.xlsx` 是 0 字节也绿——`_analyze_zip` 只对 `*.csv` 成员递归，`selfcheck.py:379-409`）。
- **G3（ifc）**：`_analyze_ifc`（`selfcheck.py:431-441`）只读前 4096 B 的第一行找 `ISO-10303-21`。实测三种"垃圾"全绿：只有头 + 空 DATA、只有头 + `FILE_SCHEMA(('IFC4'))`、头后跟一行乱码。而题目真正的门是 schema gate（`express_rules=True`）→ clash gate → connectivity gate，**这三道门我们一道都没有**。
- **G4**：对 checks 类基本空转（题面不给词表）。它不是缺陷，是设计边界；但 §1 的 zip 题（`roof-deck-design`）其实有大量列名/枚举，`contract._vocab` 抽不出来（实测 `vocab=[]`），所以 G4 在那 9 道题上也基本不工作。
- **G7**：只能抓"最终消息没提这个路径"。抓不到"提了但没做"（本案最终消息把 `solution.py` 和 `model.ifc` 都点了名，还诚实地说它是 stub——G7 判通过）。它也不能用于 `required=False` 的条目（本题三条全中）。

### 3.3 与 `TASK-REQUIREMENTS.md` §5.3 的差距

| 文档承诺 | 代码现实 | 证据 |
|---|---|---|
| G2 归档：zip 能开、**成员齐**、无绝对路径/`..`/重复名/加密 | **未实现**（`_deliverables` 只把 zip 当普通文件过 G1/G3；docstring 自认 G2 未实现） | `TASK-REQUIREMENTS.md:196`；`selfcheck.py:589-630`、`:27-33` |
| G3 模式：**docx 段落可读** | **未实现**（`_KINDS` 里没有 `.docx`） | `TASK-REQUIREMENTS.md:197`；`selfcheck.py:191-195` |
| G6 运行：脚本可执行、时限、确定性 | **未实现** | `TASK-REQUIREMENTS.md:199`；`selfcheck.py:27-33` |
| §5.5：写盘后触发、fatal 回灌、3 轮 | **只实现"事后跑一次并打日志"** | `TASK-REQUIREMENTS.md:217-232`；`harness.py:485-493` |

> 结论：**对 checks 类不够，差的不是"更严"，是三类结构性检查：脚本能不能跑（G6）、包是不是完整的（G2）、产物是不是模板（stub 检测）。** 其中 G6 最大，但成本也最大（要执行候选代码、限时、断网、查确定性），本次不进三条补丁（§6）。

---

## 4. 三条补丁（按性价比排序）

排序理由：**P1 是唯一直接作用在"交不交"上的**——对 9 道 zip 题最可能直接换来分数（部分分），对 `data-hall-mep-routing` 至少把 `ERR_NO_SUBMISSION` 变成"进评分"；P2 成本 ≈40 行且完全可本地验证，把"看见了但说不清"变成"一眼是 stub"；P3 覆盖 9/12 道 checks 题，但抽取规则多一条路径、风险最高，放最后。

### P1 — 提示词：开工先看"交付物是不是模板"，交脚本要先跑一遍

- **改哪**：`harness.py:75`（那一行是 1073 个字符、把 3 个段落并进了 1 个字符串字面量）。改成一句一个字面量，并在「Write a first, valid deliverable EARLY」之前插入新段落。
- **为什么放这个位置**：它是 t=0 的动作；现有的 stub 句子在「先写一版」段落**里面**，是"后果陈述"，不是"首步指令"。
- **成本**：每次运行多 ≈260 tokens（新增段落 1036 字符；顺带把那 1073 字符的合并行拆开，纯格式，不改语义）。**风险**：纯字符串，无机械风险；效果只能靠下一次真跑验证（§5）。
- **diff**：

```diff
--- a/harness.py
+++ b/harness.py
@@ -72,7 +72,34 @@
     " matplotlib, numpy, scipy, shapely, trimesh, ezdxf, and the `zip` and `unzip`"
     " binaries. A missing capability is almost always a missing import rather than a"
     " missing library - check the list before writing a format off."
-    "\n\nMost of these tasks name an ARCHIVE as the deliverable - `output/submission.zip`"    " or similar. If the instruction names a .zip, the zip itself is what is graded:"    " a directory of loose files next to it scores zero. Build the archive with `zip -r`"    " (or python zipfile), list its members with `unzip -l`, and re-check that the"    " archive exists and is non-empty before you finish. Rebuild it after every edit -"    " editing a file inside an already-built archive does not update the archive."    "\n\nWrite a first, valid deliverable EARLY and then improve it. A stub the"    " instruction pre-creates and you never replace scores exactly zero, and so does"    " a deliverable you were still about to write when time ran out. So: as soon as"    " you know the output format, write one complete file that satisfies it, confirm"    " it exists, and only then iterate on quality. Every later pass overwrites a file"    " that already exists, never a stub that does not."    "\n\nDeliverables are FILES, not prose. If the instruction names a directory"
+    "\n\nMost of these tasks name an ARCHIVE as the deliverable - `output/submission.zip`"
+    " or similar. If the instruction names a .zip, the zip itself is what is graded:"
+    " a directory of loose files next to it scores zero. Build the archive with `zip -r`"
+    " (or python zipfile), list its members with `unzip -l`, and re-check that the"
+    " archive exists and is non-empty before you finish. Rebuild it after every edit -"
+    " editing a file inside an already-built archive does not update the archive."
+    "\n\nBefore you read the source documents, look at what the workspace"
+    " already holds. If a file the instruction names as a deliverable already"
+    " exists when you start, read its first twenty lines: `Submission stub`,"
+    " `replace this whole file`, or a module-level `raise SystemExit` means it"
+    " is a stub, and while it stands your score is zero however good the work"
+    " in your head is. Overwrite it with a complete, runnable file in your"
+    " first few steps - even one that writes a small but valid deliverable -"
+    " and improve it from there. One run spent its entire budget decoding the"
+    " drawing sheets and left the stub in place; the grader recorded exactly"
+    " that and scored 0, so every reading it had bought was lost."
+    "\n\nIf the deliverable is a script that writes another file, run it the"
+    " way the grader will, with the command the instruction gives, and confirm"
+    " the file it should write appears and parses. A script you never executed"
+    " is a script you never knew worked: an unparsed IFC, a zero-byte model and"
+    " the shipped stub all look the same to the grader."
+    "\n\nWrite a first, valid deliverable EARLY and then improve it. A stub the"
+    " instruction pre-creates and you never replace scores exactly zero, and so does"
+    " a deliverable you were still about to write when time ran out. So: as soon as"
+    " you know the output format, write one complete file that satisfies it, confirm"
+    " it exists, and only then iterate on quality. Every later pass overwrites a file"
+    " that already exists, never a stub that does not."
+    "\n\nDeliverables are FILES, not prose. If the instruction names a directory"
     " (for example `output/`), create that directory and write every deliverable"
     " into it as a real file, in the format the instruction asks for. Your final"
     " reply is not a deliverable and is not graded as one - a memo, report, or"
```

### P2 — 让 stub 显形：必需位修正 + `py_stub` 检测

两处根因，一起修：

1. `contract.py:126-129`：`_OPTIONAL` 去掉 `\bdefault\b`。`(default output/model.ifc)` 是"脚本的第二个输出"，不是"可交可不交"。
2. `selfcheck.py`：给 `.py` 交付物加 `py_stub` 检查——① 源码里带模板文字（`Submission stub` / `replace this whole file` / `never replaced` / `your code here` / `todo: implement` / `not implemented yet`），② 模块级 `raise SystemExit|exit|quit`。二者都不需要知道具体题目。检查报在 **G1**（"这不是交付物"），severity fatal，`rule`/`fix` 单独写。

- **成本**：≈40 行（含 docstring）。**风险**：低；唯一可预见的误报是"作者故意在脚本里留 TODO 注释"——但那本来也是没写完。
- **diff**：

```diff
--- a/contract.py
+++ b/contract.py
@@ -123,9 +123,13 @@
     r"|contain(?:s|ing)?)\b"
 )
 # A file the statement asks for but does not grade ("optional side file").
+# `default` is deliberately not an optionality marker: in "writes one IFC4 file
+# to `sys.argv[1]` (default `output/model.ifc`)" it names the script's second
+# output, and reading it as "optional" demoted BOTH paths in that sentence -
+# the graded script included - to required=False.
 _OPTIONAL = re.compile(
     r"(?i)\boptional\b|\bnot a gate\b|\bgate failure\b|\babsence of\b"
-    r"|\byou may\b|\bdefault\b"
+    r"|\byou may\b"
 )
 
 # Directories whose contents are inputs, not deliverables.
--- a/selfcheck.py
+++ b/selfcheck.py
@@ -114,6 +114,12 @@
     " task statement, and numeric columns may not carry placeholders such as"
     " TBD/TBA/NaN/Inf."
 )
+RULE_STUB = (
+    "A deliverable that is still the pre-created stub is not a submission: the"
+    " file exists and parses, but executing it raises instead of building the"
+    " answer, so the grader receives nothing."
+)
+
 RULE_G7 = (
     "The final message must state what was produced and where each"
     " deliverable is stored, naming every deliverable path."
@@ -159,6 +165,11 @@
         "fix the syntax error so the script runs as delivered",
     ),
     "py_encoding": (RULE_G3, "write the script as UTF-8"),
+    "py_stub": (
+        RULE_STUB,
+        "replace the whole file with a script that builds the deliverable, run"
+        " it, and confirm the file it writes exists and parses",
+    ),
     "ifc_header": (
         RULE_G3,
         "the file must start with an ISO-10303-21 STEP header line (write the"
@@ -174,6 +185,7 @@
 # The probe. It runs inside the task container and reads only the paths it is
 # given, printing a single JSON line; nothing here knows about a task.
 PROBE_SOURCE = r'''
+import ast
 import base64
 import csv
 import html
@@ -406,8 +418,54 @@
                           "%s: %s" % (name, check["detail"])))
     violations.extend(sub_violations)
   return {"checks": checks, "violations": violations}
+
+
+# Text a shipped stub carries. They are generic on purpose: "replace this whole
+# file", "your code here" and a module-level raise appear in every task family's
+# stub and in no working script.
+_STUB_MARKS = (
+    "submission stub",
+    "replace this whole file",
+    "never replaced",
+    "your code here",
+    "todo: implement",
+    "not implemented yet",
+)
 
 
+def _stub_reason(text, tree):
+  """Says why this script cannot be a submission, or "" when it can.
+
+  A stub is not a syntax error: it compiles, it is a regular file, and it is
+  non-empty, so G1 and G3 both pass it while the grader gets a script that
+  raises. Two facts identify one without knowing the task: the marker text the
+  stub ships with, and a module-level `raise SystemExit`.
+
+  Args:
+    text: The script source.
+    tree: Its module AST.
+
+  Returns:
+    One sentence for the defect detail, or "" for a real script.
+  """
+  low = text.casefold()
+  for mark in _STUB_MARKS:
+    if mark in low:
+      return "the file still carries the stub text %r, so it was never replaced" % mark
+  for node in getattr(tree, "body", []):
+    if not (isinstance(node, ast.Raise) or isinstance(node, ast.Expr)):
+      continue
+    call = node.exc if isinstance(node, ast.Raise) else node.value
+    if not isinstance(call, ast.Call):
+      continue
+    func = call.func
+    name = getattr(func, "id", "") or getattr(func, "attr", "")
+    if name in ("SystemExit", "exit", "quit"):
+      return ("the script raises %s at module level (line %s), so executing it"
+              " builds no deliverable" % (name, node.lineno))
+  return ""
+
+
 def _analyze_py(path, spec):
   try:
     data, truncated = _read(path)
@@ -421,11 +479,15 @@
   except UnicodeDecodeError as exc:
     return {"checks": [_ck("py_encoding", "fail", str(exc))]}
   try:
-    compile(text, path, "exec")
+    tree = compile(text, path, "exec", ast.PyCF_ONLY_AST)
   except SyntaxError as exc:
     return {"checks": [_ck("py_syntax", "fail",
                            "%s (line %s)" % (exc.msg, exc.lineno))]}
-  return {"checks": [_ck("py_syntax", "ok")]}
+  checks = [_ck("py_syntax", "ok")]
+  stub = _stub_reason(text, tree)
+  if stub:
+    checks.append(_ck("py_stub", "fail", stub))
+  return {"checks": checks}
 
 
 def _analyze_ifc(path, spec):
@@ -803,7 +865,8 @@
           ext.lstrip(".") or "-",
           check.get("detail") or name,
       )
-      report.add("G3", severity, item["path"], detail, rule, fix)
+      gate = "G1" if name == "py_stub" else "G3"
+      report.add(gate, severity, item["path"], detail, rule, fix)
 
 
 def _check_g4(report: _Report, useful: Sequence[Mapping[str, Any]],
```

- **测试**（`my-harness/test_selfcheck.py` 必须同步，否则 11 个老用例会把自带的 stub 当"真脚本"用）：

```diff
--- a/my-harness/test_selfcheck.py
+++ b/my-harness/test_selfcheck.py
@@ -100,11 +100,22 @@
   return 127, "bash: line 1: python3: command not found"
 
 
+STUB_PY = os.path.join(REAL_TASK, "output", "solution.py")
+REAL_PY = "import sys\n\nprint('build %s' % (sys.argv[1:] or ['model.ifc']))\n"
+
+
 def prepare(work):
-  """Copies the real task workspace into `work` and returns it."""
+  """Copies the real task workspace into `work` and returns it.
+
+  The file the task ships at `output/solution.py` is a stub, so it is replaced
+  with a real script here: a case that wants a working deliverable must not be
+  handed the stub, and the stub itself is exercised by its own cases.
+  """
   root = os.path.join(work, "task")
   shutil.copytree(REAL_TASK, root)
   os.makedirs(os.path.join(root, "output"), exist_ok=True)
+  with open(os.path.join(root, "output", "solution.py"), "w") as handle:
+    handle.write(REAL_PY)
   return root
 
 
@@ -613,8 +624,55 @@
     expect(item["status"] == "fail", "status must be fail")
     expect(isinstance(item["detail"], str) and item["detail"],
            "detail must be a non-empty string")
+
+
+@case
+def stub_script_is_a_g1_defect(work):
+  """The shipped stub compiles, is non-empty and is not a deliverable."""
+  root = prepare(work)
+  shutil.copyfile(STUB_PY, os.path.join(root, "output", "solution.py"))
+  run = LocalContainer(root)
+  plan = contract(root, [deliverable("output/solution.py", "py")])
+  defects = selfcheck.selfcheck(
+      run, plan, final_message="Wrote output/solution.py."
+  )
+  only(defects, "G1", "fatal", contains=("stub text", "never replaced"))
+  return defects
+
+
+@case
+def module_level_exit_without_marker_text_is_a_stub(work):
+  """A stub with the shipped wording stripped still never builds anything."""
+  root = prepare(work)
+  write(root, "output/solution.py",
+        "import sys\n\nraise SystemExit('nothing built')\n")
+  run = LocalContainer(root)
+  plan = contract(root, [deliverable("output/solution.py", "py")])
+  defects = selfcheck.selfcheck(
+      run, plan, final_message="Wrote output/solution.py."
+  )
+  only(defects, "G1", "fatal", contains=("SystemExit", "builds no deliverable"))
+  return defects
 
 
+@case
+def a_real_script_is_green(work):
+  """A script that does not raise at module level is not called a stub."""
+  root = prepare(work)
+  shutil.copyfile(REAL_IFC, os.path.join(root, "output", "model.ifc"))
+  run = LocalContainer(root)
+  plan = contract(
+      root,
+      [deliverable("output/solution.py", "py"),
+       deliverable("output/model.ifc", "ifc")],
+  )
+  defects = selfcheck.selfcheck(
+      run, plan, final_message="Wrote output/solution.py and output/model.ifc."
+  )
+  expect(defects == [], "expected a green run, got: %s" % details(defects))
+  return defects
+
+
 def main():
   failures = 0
   print("selfcheck.py: %d bytes, probe %d bytes; task data under %s"
```

```diff
--- a/my-harness/test_contract.py
+++ b/my-harness/test_contract.py
@@ -283,7 +283,30 @@
   return "one zip path, no relative twin, members kept off the file list"
 
 
+def case_required_flags() -> str:
+  """The graded script stays required even next to a "(default X)" sibling.
+
+  Returns:
+    A one-line summary for the runner.
+  """
+  rows = {row["task_id"]: row for row in cards()}
+  instruction = rows["data-hall-mep-routing"]["instruction"]
+  prose = contract.extract(instruction)
+  flags = {item["path"]: item["required"] for item in prose["deliverables"]}
+  expect(flags.get("output/solution.py") is True,
+         "the graded script must be required: %r" % flags)
+  expect(flags.get("output/reconciliation.json") is False,
+         "an explicitly optional side file must stay optional: %r" % flags)
+  with_card = contract.extract(instruction, rows["data-hall-mep-routing"])
+  expect(all(item["required"] for item in with_card["deliverables"]
+             if item["path"].endswith("solution.py")),
+         "the card's graded script must be required: %r"
+         % [i["required"] for i in with_card["deliverables"]])
+  return "solution.py required=True, reconciliation.json required=False"
+
+
 CASES = (
+    ("real-card: required flags", case_required_flags),
     ("real-cards: no deliverable dropped", case_real_cards),
     ("prose-only: graded files recovered", case_instruction_only),
     ("synthetic: plain text invents nothing", case_plain_text),
```

- **上线后的日志对比**（同一现场：stub + model.ifc + reconciliation.json + 失败那条最终消息）：

  | | 日志 |
  |---|---|
  | HEAD | `selfcheck: 3 deliverable(s), 0 defect(s)` |
  | P2 | `selfcheck: 3 deliverable(s), 1 defect(s)` / `gate G1 output/solution.py fail: … the file still carries the stub text 'submission stub', so it was never replaced` |

- **已知边界**：fatal 一次只报一条（`_Report.add` 的设计）。若同时缺 `model.ifc`，先报的是"`model.ifc` 不存在"（G1 按交付物顺序走），stub 那条降级为 `fix` 但仍会打印。两条都在日志里，不影响判读。

### P3 — G2 归档门：成员清单 + 根目录 + 空成员 + 路径安全

要让它不是空门，**必须同时修成员抽取**，否则 zip 题抽不到 `members`：

1. `contract.py:80-92`：`KINDS` 加 `.dxf`。没有它，`wind-diaphragm-shear` 的 5 个成员只能认出 4 个 → 计数不符 → `_members` 直接返回 `[]`（实测）。
2. `contract.py`：加 `_numbered_members()`，读"containing at its root: `### 1. file`"这种**编号清单**（`roof-deck-design` 题面没有数量词，只有编号小节，现有 `_MEMBER_COUNT` 规则必然放弃）。只在"提到 zip 的那段话里出现 contain/at its root/these files"时才认，且至少 2 个成员才算数（保持"宁缺勿猜"）。
3. `selfcheck.py`：`_GATES` 加 G2；`_analyze_zip` 增加 `_zip_member_checks()`：缺成员（fatal，列名）、成员不在根（fatal，报告实际路径）、绝对/`..` 路径（fatal）、重复名（fix）、0 字节成员（fix）。成员表通过 scan spec 的 `archives` 字段从 contract 传进去。

- **成本**：≈80 行。**风险**：中（抽取规则多一条路径）；因此每条都有 fixture 测试（见下）。
- **diff**：

```diff
--- a/contract.py
+++ b/contract.py
@@ -89,6 +89,7 @@
     ".md": "md",
     ".txt": "txt",
     ".pdf": "pdf",
+    ".dxf": "dxf",
 }
 
 _EXT = "|".join(ext.lstrip(".") for ext in KINDS)
@@ -615,8 +616,52 @@
       entry["members"] = members
     archives.append(entry)
   return archives
+
+
+# An instruction that lists the archive contents as numbered items instead of
+# stating a count: "containing at its root:\n### 1. `deck_design.xlsx`".
+_ROOT_INTRO = re.compile(
+    r"(?i)\bcontain(?:s|ing|ed)?\b|\bat its root\b|\bthese files\b"
+)
+_NUMBERED_ITEM = re.compile(
+    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?\d{1,2}\.[ \t]*(?:\*\*)?`?"
+    r"([A-Za-z0-9_][A-Za-z0-9_.\-]*\.(?:%s))`?" % _EXT,
+    re.M,
+)
+
+
+def _numbered_members(text: str, archive: str) -> list[str]:
+  """Reads root members an instruction lists as numbered items.
+
+  Some instructions say what the archive holds with a numbered list instead of
+  a count ("containing at its root: 1. `deck_design.xlsx` ..."). The member
+  window is truncated at the next heading, which would drop exactly that list,
+  so the numbered items are read from the archive's own mention instead, and
+  only where the mention introduces contents.
+
+  Args:
+    text: The instruction.
+    archive: Basename of the zip, e.g. `submission.zip`.
 
+  Returns:
+    Member basenames in the order listed; empty when the instruction states
+    none (a list that is one short would fail a grader on a file that was
+    never asked for).
+  """
+  out: list[str] = []
+  for match in re.finditer(re.escape(archive), text):
+    window = text[match.start(): match.start() + 12000]
+    if not _ROOT_INTRO.search(window[:800]):
+      continue
+    for name in _NUMBERED_ITEM.findall(window):
+      if name == archive or name in out:
+        continue
+      if name.lower() in _NOT_DELIVERABLES:
+        continue
+      out.append(name)
+  return out
 
+
 def _members(text: str, archive: str) -> list[str]:
   """Reads the archive members the instruction requires, or nothing.
 
@@ -648,7 +693,8 @@
 
   count = _MEMBER_COUNT.search(text)
   if not count:
-    return []
+    listed = _numbered_members(text, archive)
+    return listed if len(listed) >= 2 else []
   wanted = _NUMBER_WORDS.get(count.group(1).lower(), 0) or int(count.group(1))
   return names if len(names) == wanted else []
 
--- a/selfcheck.py
+++ b/selfcheck.py
@@ -93,7 +93,7 @@
     "<fill in>",
 )
 SEVERITY_ORDER = ("fatal", "fix", "note")
-_GATES = ("G1", "G3", "G4", "G7")
+_GATES = ("G1", "G2", "G3", "G4", "G7")
 MAX_ITEMS = 12
 _HEREDOC = "SELFCHECK_PROBE_EOF"
 
@@ -109,6 +109,11 @@
     "Every deliverable must be readable in the format the task statement"
     " names: existing is not enough, the bytes must parse."
 )
+RULE_G2 = (
+    "An archive is graded as a package: it must open, carry every member the"
+    " task statement names at its root, and contain nothing a grader would"
+    " refuse (absolute or parent paths, duplicate names, empty members)."
+)
 RULE_G4 = (
     "Enumerated columns may only use values from the closed vocabulary in the"
     " task statement, and numeric columns may not carry placeholders such as"
@@ -160,6 +165,30 @@
         "a member of the archive cannot be read; rebuild the archive",
     ),
     "zip_member_encoding": (RULE_G3, "store archive members as UTF-8"),
+    "zip_members_missing": (
+        RULE_G2,
+        "put the named members back in the archive, at its root, and rebuild"
+        " it with zipfile after the last edit",
+    ),
+    "zip_member_not_at_root": (
+        RULE_G2,
+        "move the member to the archive root; a grader that reads"
+        " `name.xlsx` does not look inside a subdirectory",
+    ),
+    "zip_member_unsafe": (
+        RULE_G2,
+        "rebuild the archive without absolute or `..` member paths",
+    ),
+    "zip_member_duplicate": (
+        RULE_G2,
+        "keep one copy of each member; a duplicate name makes the read"
+        " ambiguous",
+    ),
+    "zip_member_empty": (
+        RULE_G2,
+        "write the member content before archiving; a 0-byte member carries"
+        " no answer",
+    ),
     "py_syntax": (
         RULE_G3,
         "fix the syntax error so the script runs as delivered",
@@ -386,6 +415,44 @@
                            "the workbook declares no sheets")]}
   return {"checks": [_ck("xlsx_sheets", "ok",
                          "%d sheets: %s" % (len(names), ", ".join(names[:8])))]}
+
+
+def _zip_member_checks(names, sizes, wanted):
+  """G2: the archive carries the named members, at its root, and is sane."""
+  checks = []
+  if wanted:
+    missing, nested = [], []
+    for member in wanted:
+      if member in names:
+        continue
+      at = [n for n in names if n.replace("\\", "/").endswith("/" + member)]
+      if at:
+        nested.append((member, at[0]))
+      else:
+        missing.append(member)
+    for member, at in nested:
+      checks.append(_ck("zip_member_not_at_root", "fail",
+                        "%s sits at %r, not at the archive root" % (member, at)))
+    if missing:
+      checks.append(_ck("zip_members_missing", "fail", "%d of %d required"
+                        " members absent: %s"
+                        % (len(missing), len(wanted), ", ".join(missing[:8]))))
+  bad = [n for n in names
+         if n.startswith("/") or ".." in n.replace("\\", "/").split("/")]
+  if bad:
+    checks.append(_ck("zip_member_unsafe", "fail",
+                      "absolute or parent path in the archive: %s"
+                      % ", ".join(bad[:4])))
+  dup = sorted(set(n for n in names if names.count(n) > 1))
+  if dup:
+    checks.append(_ck("zip_member_duplicate", "fix",
+                      "duplicate member names: %s" % ", ".join(dup[:4])))
+  empty = [n for n in names if sizes.get(n) == 0 and not n.endswith("/")][:5]
+  if empty:
+    checks.append(_ck("zip_member_empty", "fix",
+                      "empty member(s), so they carry no answer: %s"
+                      % ", ".join(empty)))
+  return checks
 
 
 def _analyze_zip(path, spec):
@@ -393,6 +460,7 @@
   try:
     zf = zipfile.ZipFile(path)
     names = zf.namelist()
+    sizes = dict((i.filename, i.file_size) for i in zf.infolist())
   except (zipfile.BadZipFile, OSError) as exc:
     return {"checks": [_ck("zip_members", "fail", str(exc))]}
   if not names:
@@ -400,6 +468,8 @@
                            "the archive lists no members")]}
   checks.append(_ck("zip_members", "ok",
                     "%d members: %s" % (len(names), ", ".join(names[:8]))))
+  wanted = [str(m) for m in ((spec.get("archives") or {}).get(path) or [])]
+  checks.extend(_zip_member_checks(names, sizes, wanted))
   for name in [n for n in names if n.lower().endswith(".csv")][:MEMBER_CAP]:
     try:
       raw = zf.read(name)[:TEXT_CAP]
@@ -865,7 +935,12 @@
           ext.lstrip(".") or "-",
           check.get("detail") or name,
       )
-      gate = "G1" if name == "py_stub" else "G3"
+      if name == "py_stub":
+        gate = "G1"
+      elif name.startswith("zip_member"):
+        gate = "G2"
+      else:
+        gate = "G3"
       report.add(gate, severity, item["path"], detail, rule, fix)
 
 
@@ -1055,6 +1130,13 @@
             )
         ],
         "violation_cap": 4,
+        "archives": {
+            _resolve(workdir, str(entry.get("path") or "")): [
+                str(member) for member in (entry.get("members") or [])
+            ]
+            for entry in (contract.get("archives") or [])
+            if isinstance(entry, Mapping) and entry.get("path")
+        },
     }
     encoded = _encode(spec)
     scan_result = _probe(run, "scan", encoded)
```

- **测试**：

```diff
--- a/my-harness/test_selfcheck.py
+++ b/my-harness/test_selfcheck.py
@@ -617,7 +617,7 @@
     expect(isinstance(item, dict), "defect must be a dict")
     for key in ("gate", "status", "severity", "path", "detail", "rule", "fix"):
       expect(key in item, "defect is missing %r: %r" % (key, item))
-    expect(item["gate"] in ("G1", "G3", "G4", "G7"),
+    expect(item["gate"] in ("G1", "G2", "G3", "G4", "G7"),
            "unexpected gate %r" % item["gate"])
     expect(item["severity"] in selfcheck.SEVERITY_ORDER,
            "unexpected severity %r" % item["severity"])
@@ -671,8 +671,71 @@
   )
   expect(defects == [], "expected a green run, got: %s" % details(defects))
   return defects
+
+
+def archive(root, members):
+  """Builds `output/submission.zip` with the given members."""
+  full = os.path.join(root, "output", "submission.zip")
+  with zipfile.ZipFile(full, "w") as handle:
+    for name, data in members.items():
+      handle.writestr(name, data)
+  return full
+
+
+FOUR = ("deck_design.xlsx", "roof_deck_plan.dxf",
+        "constraint_register.xlsx", "design_basis.md")
+MESSAGE = "Wrote output/submission.zip: " + ", ".join(FOUR) + "."
+
 
+def archive_plan(root):
+  return contract(
+      root,
+      [deliverable("output/submission.zip", "zip")],
+      archives=[{"path": "output/submission.zip", "required": True,
+                 "allow_extra": True, "members": list(FOUR)}],
+  )
 
+
+@case
+def zip_missing_members_is_fatal(work):
+  """G2: the package is graded as a package, so a missing member is a defect."""
+  root = prepare(work)
+  write(root, "output/deck_design.xlsx", b"x")
+  archive(root, {"design_basis.md": "hi\n"})
+  run = LocalContainer(root)
+  defects = selfcheck.selfcheck(run, archive_plan(root), final_message=MESSAGE)
+  item = only(defects, "G2", "fatal", contains=("required members absent",))
+  expect("roof_deck_plan.dxf" in item["detail"] and
+         "constraint_register.xlsx" in item["detail"],
+         "the defect must name the missing members: %r" % item["detail"])
+  return defects
+
+
+@case
+def zip_member_nested_is_not_at_root(work):
+  """G2: a grader that reads `name.xlsx` does not look in a subdirectory."""
+  root = prepare(work)
+  archive(root, dict(("output/" + name, b"x") for name in FOUR))
+  run = LocalContainer(root)
+  defects = selfcheck.selfcheck(run, archive_plan(root), final_message=MESSAGE)
+  only_first = defects[0]
+  expect(only_first["gate"] == "G2" and "not at the archive root"
+         in only_first["detail"], "expected a root defect, got %r" % only_first)
+  return defects
+
+
+@case
+def zip_member_empty_is_a_fix(work):
+  """G2: a 0-byte member carries no answer."""
+  root = prepare(work)
+  archive(root, {"deck_design.xlsx": b"", "roof_deck_plan.dxf": b"0\nSECTION\n",
+                 "constraint_register.xlsx": b"", "design_basis.md": "hi\n"})
+  run = LocalContainer(root)
+  defects = selfcheck.selfcheck(run, archive_plan(root), final_message=MESSAGE)
+  only(defects, "G2", "fix", contains=("empty member",))
+  return defects
+
+
 def main():
   failures = 0
   print("selfcheck.py: %d bytes, probe %d bytes; task data under %s"
```

```diff
--- a/my-harness/test_contract.py
+++ b/my-harness/test_contract.py
@@ -303,9 +303,26 @@
          "the card's graded script must be required: %r"
          % [i["required"] for i in with_card["deliverables"]])
   return "solution.py required=True, reconciliation.json required=False"
+
 
+def case_numbered_members() -> str:
+  """A numbered root list is read when the instruction states no count.
 
+  Returns:
+    A one-line summary for the runner.
+  """
+  rows = {row["task_id"]: row for row in cards()}
+  roof = contract.extract(
+      rows["roof-deck-design"]["instruction"], rows["roof-deck-design"])
+  members = roof["archives"][0].get("members") or []
+  expect(members == ["deck_design.xlsx", "roof_deck_plan.dxf",
+                     "constraint_register.xlsx", "design_basis.md"],
+         "the four root members were not read: %r" % members)
+  return "4/4 numbered root members read"
+
+
 CASES = (
+    ("real-card: numbered root members", case_numbered_members),
     ("real-card: required flags", case_required_flags),
     ("real-cards: no deliverable dropped", case_real_cards),
     ("prose-only: graded files recovered", case_instruction_only),
```

- **上线前后对比**（`roof-deck-design` 的 4 成员，fixture 见附录 A）：

  | fixture | HEAD | P3 |
  |---|---|---|
  | 4 个成员、内容正常 | 绿 | 绿 |
  | 只有 `design_basis.md`（缺 3 个） | **绿** | G2 fatal：`3 of 4 required members absent: deck_design.xlsx, …` |
  | 4 个都在，但塞在 `output/` 子目录下 | **绿** | G2 fatal：`deck_design.xlsx sits at 'output/deck_design.xlsx', not at the archive root` |
  | 4 个都在，其中 2 个 0 字节 | **绿** | G2 fix：`empty member(s), so they carry no answer: …` |
  | 成员里含 `/abs.csv`、`../passwd` | **绿** | G2 fatal：`absolute or parent path in the archive: …` |

- **抽取效果**（实测）：`roof-deck-design` → 4/4 成员；`wind-diaphragm-shear` → 5/5 成员（`lateral_schedule.xlsx, diaphragm_plan.dxf, constraint_register.xlsx, calculations.pdf, design_basis.md`）。

---

## 5. 验证方法与实测结果

### 5.1 补丁怎么叠（可重复构建）

三条补丁是三个状态，按顺序应用；`/tmp` 下的构建脚本就是 §4 的 diff 来源（anchors 都带断言，源文件漂移会直接报错）：

```bash
python3 /tmp/ple-build/build.py        # base -> p1 -> p2 -> p3
diff -u ~/.dsh/work/ple/harness.py   /tmp/ple-build/p1/harness.py      # = P1.diff
diff -u /tmp/ple-build/p1/contract.py /tmp/ple-build/p2/contract.py     # = P2.diff 的一半
...
```

### 5.2 回归（应用后必须全绿）

```bash
cd ~/.dsh/work/ple/my-harness
cp ../contract.py ../selfcheck.py .        # 根目录是出货件；my-harness/ 是镜像 + 测试
python3 test_selfcheck.py                  # 20 -> 26 例
python3 test_contract.py                   # 7  -> 9 例
```

**端到端实测**：把 §4 的 7 个 patch 按顺序 `patch -p1` 打到 HEAD 的干净副本上 → **7/7 全部干净应用**（无 fuzz、无 reject），随后 `test_selfcheck.py` **26/26**、`test_contract.py` **9/9** 全过：

```
== p0 (P1, harness.py)                 patching file harness.py
== p1 (P2, contract+selfcheck)         patching file contract.py / selfcheck.py
== p2 (P2 测试, test_selfcheck)         patching file 'my-harness/test_selfcheck.py'
== p3 (P2 测试, test_contract)          patching file 'my-harness/test_contract.py'
== p4 (P3, contract+selfcheck)         patching file contract.py / selfcheck.py
== p5 (P3 测试, test_selfcheck)         patching file 'my-harness/test_selfcheck.py'
== p6 (P3 测试, test_contract)          patching file 'my-harness/test_contract.py'
ALL 26 CASES PASSED      # 20 老例 + 3 个 stub 例 + 3 个 zip 例
ALL 9 CASES PASSED       # 7 老例 + required 标记 + 编号成员
```

> `test_selfcheck.py` 的 `prepare()` 原来把**自带的 stub** 当"真交付物"复制进每个用例。P2 之后这 11 个用例会多出 `py_stub` 缺陷——这不是误报，是 fixture 本身把"模板"当成了"提交"。所以测试补丁里把 `prepare()` 换成真脚本，另加 3 个 stub 专用用例。

### 5.3 现场复现（附录 A 的脚本）

一条命令就能看到 before/after（`logs/ci44.log` 里那条真实最终消息也一起复现）：

```
$ PLE_MODULES=~/.dsh/work/ple          python3 /tmp/replay.py     # HEAD
data-hall: GREEN                      # <- stub + model.ifc + reconciliation 都在，四道门 0 缺陷
all four     GREEN
one of four  GREEN                    # <- 3/4 成员缺失也绿
nested       GREEN

$ PLE_MODULES=/path/to/patched-modules python3 /tmp/replay.py     # P2+P3
data-hall: [('G1', 'fatal', 'output/solution.py')]
all four     GREEN
one of four  [('G2', 'fatal')]
nested       [('G2', 'fatal'), ('G2', 'fix'), ('G2', 'fix'), ('G2', 'fix')]
```

| 场景 | HEAD | P2+P3 |
|---|---|---|
| data-hall：stub 原样 + 失败那条最终消息 | 2 note（缺 model.ifc / reconciliation），**stub 无缺陷** | G1 fatal + fix（stub 点名）+ 1 note |
| data-hall：stub + model.ifc + reconciliation 都在 | **全绿**（`0 defect(s)`） | **G1 fatal**：stub 文本 |
| data-hall：真脚本 + model.ifc | 绿 | 绿（不误报） |
| zip：缺成员 / 嵌套 / 空成员 / 绝对路径 | 全绿 | G2 fatal/fix（逐条） |
| `.docx` 塞二进制垃圾 + `.pdf` 塞文本 | 绿 | 绿（**P2/P3 都不修这个**，见 §6） |

### 5.4 下一步真跑怎么读日志

1. 先看 `selfcheck: N deliverable(s), M defect(s)`：`M=0` 且题是 checks 类 ⇒ 要么真交了，要么门又空转，必须看 `deliverables` 条数（0 条 = 抽取失败，不是"没问题"）。
2. 看 `artifacts/: ... empty <task>`：出现即"一个文件都没写"，与分数 0 同义（§2.3）。
3. 看 `grading: checks` 题的 `<verifier>/metrics.json` 的 `exec_status`：
   - `ERR_NO_SUBMISSION` = **没交**（stub 或空集）——harness 侧的问题，本计划的 P1/P2 负责；
   - `ERR_CONNECTIVITY` / `ERR_CLASH` / `ERR_SCHEMA` = **交了，门没过**——模型侧的问题，`report.gates` 里有逐门布尔值和数字，应据此写下一轮提示词；
   - 有 `combined_score` 数字 = 真到了评分公式，这时才谈质量。

---

## 6. 不做 / 后续（写清代价，别偷偷做）

0. **期望值先说清**：这三条补丁只针对 `ERR_NO_SUBMISSION` 这一类 0（CI44）。`ERR_CLASH`（CI45）与 `ERR_CONNECTIVITY`（CI42）是**模型侧**的 0，本次一条都不修——那要靠 clash/connectivity 自查工具 + 提示词迭代，属于另一条线。落地后对 `data-hall-mep-routing` 的合理预期是"0 分但**有产物、有逐门数字**"，不是"0 变 1"；对 9 道 zip 题预期更高，因为它们的 grader 给部分分（`roof-deck-design/instruction.md:3`「An imperfect early submission earns partial credit」、`:90-99` 的 `0.4·p + 0.6·[h=1]·O`）。
1. **G6（跑候选脚本）不做**：它是唯一能直接复现本次失败的检查（`python3 output/solution.py /tmp/model.ifc` → 看退出码 + 产物能否 `ifcopenshell.open`）。代价：要在容器里执行候选代码、限 300 s（`instruction.md:44`）、断网、跑两遍比确定性（`TASK-REQUIREMENTS.md:199` 的要求），并且要把结果作为 fatal 而不是 note。**建议作为 P4，单独一轮做**：它比重写环内回灌便宜得多。
2. **环内回灌不做**：`TASK-REQUIREMENTS.md:217-232` 设计的"fatal → 回灌 → 模型修 → 重跑"需要第二轮 pi 会话。`STOP_BEFORE_SEC = 90`（`harness.py:53`）没有余量；CI44 那次整题 2271 s ≥ agent 预算 1800 s（`task.toml:32`），模型自述 "reached the end of a working budget"，即 agent 阶段大概率是被预算截停的，没有留出第二轮。这是**预算决策**，不是代码微调，留给主代理。
3. **不动 stub 本身**：不删、不改、不预先替换。stub 是"未作答"的哨兵，`ERR_NO_SUBMISSION` 比 G1 missing 携带更多信息；删掉只会把"没写"伪装成"缺文件"。
4. **不加 `.docx` 解析**：`fire-test` 的 208 分全在 Word 上，G3 加 docx 段落可读是另一条线（`TASK-REQUIREMENTS.md:197` 已承诺但未实现）。本次三条补丁不含它，避免把 checks 类和 rubric 类混在一轮里改。
5. **不改 `data/tasks/**`**：仓库里仍有 **17/29** 道题的 `task.toml` 写着 `cpus = 4`（`grep -l '^cpus = 4' data/tasks/*/task.toml | wc -l` = 17）；CI 侧靠 `dev-score.yml` 的资源改写兜底（commit `ed1c6a2`），本地直跑仍会踩。**这是资源层，不是交付层**，本次不动。

---

## 附录 A — 复现脚本

把下面存成 `/tmp/replay.py`。`PLE_MODULES` 指向哪份 `contract.py`/`selfcheck.py`，就复现哪一版（仓库根目录 = before；打了 P2/P3 的副本 = after）：

```bash
PLE_MODULES=~/.dsh/work/ple              python3 /tmp/replay.py   # before
PLE_MODULES=/path/to/patched-modules     python3 /tmp/replay.py   # after
```

```python
import pathlib, shutil, subprocess, sys, zipfile
import os
REPO = pathlib.Path(os.environ.get("PLE_REPO", "/Users/a0000/.dsh/work/ple"))
MODULES = os.environ.get("PLE_MODULES", str(REPO))   # HEAD 目录 = before；补丁后的副本 = after
sys.path.insert(0, MODULES)
import contract, selfcheck

def run(cmd):
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")

fm = [l for l in (REPO / "logs/ci44.log").read_text(errors="replace").splitlines()
      if '"type": "result"' in l][0].split("Z ", 1)[1]
fm = __import__("json").loads(fm)["result"]        # 失败那轮的最终消息（原文）

# A. data-hall: stub 原样
ws = pathlib.Path("/tmp/replay-ws"); shutil.rmtree(ws, ignore_errors=True)
(ws / "output").mkdir(parents=True)
stub = REPO / "data/tasks/data-hall-mep-routing/environment/filesystem" \
             "/dc_model__hall_seed000_d000__mep_routing/output/solution.py"
shutil.copy(stub, ws / "output/solution.py")
(ws / "output/model.ifc").write_text(
    "ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n")
(ws / "output/reconciliation.json").write_text("{}")
spec = contract.extract(
    (REPO / "data/tasks/data-hall-mep-routing/instruction.md").read_text())
spec["workdir"] = str(ws)
defects = selfcheck.selfcheck(run, spec, final_message=fm)
print("data-hall:", [(d["gate"], d["severity"], d["path"]) for d in defects]
      or "GREEN")

# B. zip: roof-deck 的四个根成员
base = pathlib.Path("/tmp/replay-zip"); shutil.rmtree(base, ignore_errors=True)
(base / "output").mkdir(parents=True)
members = ["deck_design.xlsx", "roof_deck_plan.dxf",
           "constraint_register.xlsx", "design_basis.md"]
zspec = {"workdir": str(base),
         "deliverables": [{"path": "output/submission.zip", "kind": "zip",
                           "required": True}],
         "archives": [{"path": "output/submission.zip", "required": True,
                       "allow_extra": True, "members": members}],
         "secondary": [{"kind": "final_message", "required": True}]}
for label, files in [
    ("all four", {m: b"x" for m in members}),
    ("one of four", {"design_basis.md": b"hi\n"}),
    ("nested", {"output/" + m: b"x" for m in members}),
]:
    z = base / "output/submission.zip"
    if z.exists():
        z.unlink()
    with zipfile.ZipFile(z, "w") as zf:
        for n, b in files.items():
            zf.writestr(n, b)
    d = selfcheck.selfcheck(run, zspec, final_message="Wrote output/submission.zip")
    print("%-12s" % label, [(x["gate"], x["severity"]) for x in d] or "GREEN")
```

**参考数字（本机实测，2026-11-18）**

| 项 | 值 |
|---|---|
| stub 大小 | 12 行 / 536 B |
| HEAD：stub 现场 | `selfcheck: 3 deliverable(s), 0 defect(s)` |
| P2+P3：stub 现场 | `selfcheck: 3 deliverable(s), 1 defect(s)`，`gate G1 output/solution.py fail` |
| contract 变更 | `solution.py` / `model.ifc`：False → **True**；`reconciliation.json`：False → False |
| zip 成员抽取 | `roof-deck-design` 4/4、`wind-diaphragm-shear` 5/5（HEAD：0/4、0/5） |
| 测试 | selfcheck 20→26 全过；contract 7→9 全过 |

---

*本文件只含分析与补丁。落地由主代理统一执行：应用 §4 的 diff → 同步到 `my-harness/` → 跑 §5.2 的回归 → 下一次 CI 先只跑 `data-hall-mep-routing`（一道题，~40 min），读 §5.4 的三行日志。*
