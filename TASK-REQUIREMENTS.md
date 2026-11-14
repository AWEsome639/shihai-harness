# PLE Agent Harness — 任务需求与合规自检协议

> ⚠️ 本文档是**工作产物，不是 entry 的一部分**：它在比赛仓库克隆根目录，含题面派生内容。
> 提交前不要 `git add` 它（`AGENTS.md` §11：`must not retain or share task contents`）。
>
> 依据：9 个已下载 dev 任务的**题面原文**（`task_cards.jsonl` 的 `instruction` 字段 / 任务目录 `instruction.md`）、
> **公开任务卡元数据**（`task_cards.jsonl`：workdir / deliverables / grading / 超时 / MCP）、
> **题面可见的容器定义**（`environment/Dockerfile`、`requirements.txt`）、以及比赛规则 `AGENTS.md` §8 / §11。
> 不包含任何评分脚本内容。

---

## 0. 结论先行

1. **官方评分器不在 agent 容器里。** 9 个任务里有 4 个（cooling-water-flush-plan、data-hall-device-takeoff、
   division-26-specifications、data-center-site-model）题面直接写明：本工作区**没有任何** verifier / oracle /
   evaluation data，唯一能跑的检查是"你自己写的那些"。另外 2 个 IFC 脚本任务则**要求**你用 workspace 里的
   `ifcopenshell` 自校验（`express_rules=True`）。⇒ "让 agent 在容器内调官方验证器拿诊断"在题面层面就不成立。
2. **规则层面也禁止。** `AGENTS.md` §11 Fair play 原文：`Do not bypass network restrictions, inspect grading
   scripts, or tamper with the evaluation container.`（`AGENTS.md:278`）。
3. **分数大头是交付物契约，不是算法。** §8：`Graders inspect the exact deliverable file paths, names, and
   formats requested in the task instruction. Deliverables account for the vast majority of every score.`
   （`AGENTS.md:192`）。
4. ⇒ 合规且有效的自检只有**两层半**：
   - **L1 容器内契约自检**（harness 自己发 `exec` 跑通用检查：路径/格式/表头/词汇表/算术/确定性）；
   - **L2 本地黑盒评分**（`uv run python eval/run.py ../my-harness`，只拿分数，不读评分代码）；
   - **L3 判官修复**（judge 报错时 `--regrade`，不重跑 trial）。
5. 本文档给出 L1 的完整协议（**与任务无关**，只吃题面契约），以及 L2/L3 的接入方式。

---

## 1. 合规边界（设计任何自检前先立规矩）

| 项 | 判据 | 出处 |
|---|---|---|
| 不读评分脚本 | 不打开任务内 `tests/`、`evaluator/`、`dcgrade/`、`grader/`、`corrina/**/grading/` 等评分实现 | `AGENTS.md:278` |
| 不按任务指纹写逻辑 | 自检器不得含具体 task_id、具体答案、具体阈值分支 | `AGENTS.md:278` |
| 不硬编码答案 | `artifacts/` 必须是 harness 未篡改的产物，不得含预置的 dev/test 答案 | `AGENTS.md:278` |
| 交付物路径/文件名/格式必须逐字对上题面 | 这是最高性价比的一条 | `AGENTS.md:192` |
| rubric 类要读 `done.final_message` | 必须给"产出了什么、每个文件存哪"的简短总结 | `AGENTS.md:193` |
| 单文件 ≤ 95 MiB、不得缺目录、不得用 symlink | 任一命中该 dev 任务记 0（`missing folder, a symlink, a file over 95 MiB, or no gradable deliverables`） | `AGENTS.md:243` |
| 早写初版、留足时间发 `done` | 超时前必须 `done`，`final_message` 与磁盘文件都算分 | `AGENTS.md:195`、§7 |

**推论**：自检器必须是**任务无关的通用器**（读一份 contract 描述 → 跑标准检查），而不是"针对某题写一个判分器"。
这样既合规，也能一套代码覆盖 29 个任务。

---

## 2. 9 个 dev 任务需求总表（表 A）

所有 `workdir` / `deliverables` 来自 `task_cards.jsonl`（公开任务卡）；契约细节来自题面原文。

| # | 任务 | 家族 | workdir | 交付物（精确路径 / 格式） | 题面明示的自检 / 反馈 |
|---|---|---|---|---|---|
| 1 | data-hall-mep-routing | IFC 脚本 | `/app/files/dc_model__hall_seed000_d000__mep_routing` | `output/solution.py`（自包含 Python 脚本，唯一 artifact） | **要求** `express_rules=True` 校验；`ifcopenshell==0.8.4` 可用 |
| 2 | data-hall-mep-routing-offset | IFC 脚本 | `/app/files/dc_model__hall_seed005_d100__mep_routing` | 同上 `output/solution.py` | 同上（难版：网格偏 150 mm、104 喷头） |
| 3 | data-center-site-model | IFC 直写 | `/app/files/frontier__DCBim__p6_002` | `submission/model.ifc`（raw STEP 文本） | **blind**：oracle 不服务、verifier 与库都不在 workspace、提交前无任何分数/反馈 |
| 4 | backfeed-energization-reply | docx rubric | `/filesystem` | `/filesystem/Halbray_Backfeed_Reply.docx` **+ OpenProject 工作包** | rubric judge 读 docx + `final_message`；17 条 criteria |
| 5 | emergency-spillway-check | docx rubric | `/filesystem` | `/filesystem/Memo_to_Callaway_Spillway_2026-10-31.docx` | 同上；15 条 criteria |
| 6 | engineers-estimate-check | docx rubric | `/filesystem` | `Correspondence/Client/Memo_to_Callaway_2026-10-31.docx`（相对 `/filesystem`） | 同上；10 条 criteria |
| 7 | cooling-water-flush-plan | zip 包 | `/filesystem` | `…blind/output/submission.zip`（7 个文件） | **blind**：无 verifier，只有自建检查 |
| 8 | data-hall-device-takeoff | zip 包 | `/filesystem` | `…blind/output/submission.zip`（5 个文件） | **blind**：同上 |
| 9 | division-26-specifications | zip 包 | `/filesystem` | `…blind/output/submission.zip`（`specs/*.docx` + `design_basis.xlsx`） | **blind**：同上 |

> 题面原文（3 个 zip 任务的共同措辞）：
> `No verifier, oracle, or evaluation data of any kind is available in this workspace — the only checks you can
> run are the ones you build yourself.`
> site-model：`This is a blind evaluation: no oracle is served in this run. … no score, verdict, or other
> feedback is returned before you submit. Plan the design and IFC serialization to correctness before submitting.`

补充元数据：全部任务 `agent_timeout_min = 360`；cpus/mem 为 2/4096MB（#1–3、5、6）或 4/8192MB（#4、7–9）；
`final_message` 仅 #4/#5/#6 为 `true`；`mcp_servers` 仅 #4–6 非空。

---

## 3. 四族要点

### 3.1 IFC 脚本族（#1 #2）

- **交付**：一个自包含 Python 脚本 `output/solution.py`，用 `ifcopenshell` 构建模型，写**一个** IFC4 文件到
  `sys.argv[1]`（缺省 `output/model.ifc`）。
- **运行契约**：`headless`、**300 s 内**、确定性、无网络、**运行期不得回头读 PNG**（七张图纸上的数据必须烘成字面量）。
- **建模契约**：项目单位 **mm，但 placement matrix 的 translation 用米**；profile 只用 2D 点；规定的空间结构；
  精确 element class（例：racks = `IfcFurniture` / `USERDEFINED` / `"Rack"`）；必需的 `Pset_DCM_*` 属性集；
  朝向绕 footprint 中心旋转。
- **题面自检要求**：`Validate with express_rules=True before submitting.`
- **题面要求的"可测量性"（诊断口径，非分数）**：每个喷头的垂直净空可测；每个 PDU 顶到最近 CHW 管可测；
  tray–busway 间距可测；每个服务在地板下/头顶的长度可区分；每个 segment 有明确标高；可选 `Support`/`Hanger`、
  `Drop`/`Tap`/`Termination`（`ObjectType` 关键字）；可选 `output/reconciliation.json`。
- **容器工具**：python 3.11-slim + `numpy==1.26.4` + `ifcopenshell==0.8.4.post1` + `pytest==9.1.1`。
  ⇒ 自检可以真的用 ifcopenshell 打开、`express_rules` 校验、读回 Pset 与元素类。

### 3.2 IFC 直写族（#3）

- **交付**：`submission/model.ifc`，**raw STEP 文本**；起手文件是"valid IFC header + empty site"。
- **环境**：裸 `python:3.11-slim`，`requirements.txt` 为空 ⇒ **没有任何 BIM 库**，必须手写 STEP 序列化。
- **输入**：`agent_view.json`（场地几何、两期建筑程序、目标权重、网络输出 schema、完整 `brief_document`，
  坐标**米**、局部 CRS）；`docs/ifc_contract.md`（IFC 约定）；`docs/example.ifc`（完整玩具样例）。
- **建模契约**：每栋程序建筑 = `IfcBuilding` + 挤出实体 footprint + 必需 identity Pset；道路/MV/图载荷嵌在
  `IfcSite` 的 `DCD_Networks` Pset。
- **评分结构（题面公开）**：FEASIBLE = `1.0`；否则 `0.5 ×`（满足的 17 条绑定要求比例）；解析/提取/语义/schema/
  几何硬门失败 = `0.0`。
- **自检**：没有库 ⇒ 自写 STEP 解析（实体计数、必填属性、引用完整性）+ 直接对照 `docs/example.ifc` 的结构 +
  用 `agent_view.json` 里的 schema 校验 `DCD_Networks` 载荷。

### 3.3 docx rubric 族（#4 #5 #6）

- **交付**：单个 `.docx`，路径逐字如题面（#6 是相对 `workdir` 的 `Correspondence/Client/...`）。
  #4 还要求**在 OpenProject 的 "DER Program Backlog Review" 项目里开一个工作包**——非文件型交付物，容易漏。
- **口径要求（题面写死的）**：
  - #4：以 **2025-07-14** 的记录状态为准；明确回答"能否 07/29 并网"并给依据；若有阻碍，说清是什么、依据什么、
    何时之前由谁办完。#4 还有一段"另行审查中"的清单——**不属于本记录范围**，不得算作阻碍。
  - #5：**日期 10/31/2026**；每个问题**给单一处置**而非选项；每个值带单位并注明来自哪份记录；
    **中间值不得四舍五入**。
  - #6：只动 Section 2；给修正后小计、逐条数量/单价变更表（带图号）、修正后 rounded bond；
    有 "Actual" 的沿用已列金额；无量测的沿用已列数量；管长**进位到整尺**。
- **容器工具**：`libreoffice-writer/calc`、`poppler-utils`、`qpdf`、`tesseract`、`python3`；MCP：
  `word_v2`/`excel_v2`/`pdfs`/`filesystem`/`code_execution`/`mail`（#4 另有 `openproject_mcp`/`mysql`/`wiki_js_mcp`/
  `nextcloud_mcp`/`calendar_v2`/`freecad_mcp`）。
- **自检**：docx 本质是 zip ⇒ `zipfile` 校验三件套 + 读 `word/document.xml` 抽段落文本；数字自洽
  （行合计 = 小计、bond = 规则化后的值）；日期/单位/署名/收件人齐备；`final_message` 覆盖每个交付物。

### 3.4 zip 包族（#7 #8 #9）

| 任务 | zip 内必须有的文件 | 关键契约 |
|---|---|---|
| cooling-water-flush-plan | `flush_plan.csv` `chemistry_plan.csv` `acceptance_matrix.csv` `schedule.csv` `resources.json` `constraint_register.csv` `MOP.md` | stage 只有 7 个 token（`HYDRO INITIAL_FLUSH CHEM_CLEAN DRAIN_RINSE PASSIVATE HOLD FINAL_FILL`）；列名大小写与 `_`/`-`/空格不敏感；**被读的数值列出现 `TBD/TBA/NaN/Inf/XXX/<fill in>` 整包 0 分**；`skid_id` 是闭集（3 台租用机组，多台用 `+` 连接）；`constraint_register` 里**一行引用被取代/撤销文档即挂整个 register 家族**；`MOP.md` 要给每回路 governing constraint 与 margin `(limit−achieved)/limit` |
| data-hall-device-takeoff | `takeoff.csv` `locations.csv` `bom.csv` `register.csv` `basis.md` | **数量零容差**；`locations.csv` 是"一网格格×一型号×一图"一行，**不是一设备一行**；`bom.csv` 有两类行（ATTRIBUTION 带 `for_type_mark`+condition、PURCHASE 带 `(PURCHASE)`/`(STOCK)`）；不得发明设备 |
| division-26-specifications | `specs/<每个交付的 26 分节>.docx` + `design_basis.xlsx` | 分节必须**基于 UFGS 改写并保留指南正文**；段落纯文本、编号是字面文本（Word 自动编号不读）、表格按段落逐格读、末段 `-- End of Section --`；`design_basis.xlsx` 7 张表（`Sections/Feeders/ShortCircuit/Photometric/StepLoad/Register/Findings`），表头在第 1 行 |

- **容器工具**：python 3.12 + `openpyxl 3.1.5`、`python-docx 1.2.0`、`pymupdf/pdfplumber/pdfminer.six`、
  `ezdxf 1.4.4`、`ifcopenshell 0.8.5`、`lxml`、`jq`、`poppler-utils`、`zip/unzip`。
  ⇒ 自检能真的解 zip、读 CSV、开 xlsx、抽 docx 段落、抽 PDF 文本。
- **题面共通**：**先在前 ~100 步内写出一个完整版本**，再原地迭代；跑完时磁盘上最后一版才算数；
  没文件 = 0 分；不完美但有文件 = 部分分。

---

## 4. 题面里"可机器判定"的硬约束（表 B）

这些是 L1 自检要逐条打的靶子（全部来自题面，未使用任何评分代码知识）。

| 类别 | 约束 | 命中任务 |
|---|---|---|
| 路径/命名 | 交付路径、文件名、扩展名逐字一致；#6 是相对路径 `Correspondence/Client/…` | 全部 |
| 归档结构 | zip 内文件必须落在根/指定子目录；不得有多余的顶层包装目录 | #7 #8 #9 |
| 词表 | stage 7 个 token；discharge_route ∈ {SEWER, HAUL, NONE}；water_source ∈ {MAKEUP, CASCADE_FROM:…}；skid 闭集 | #7 |
| 占位符 | 被读数值列禁止 `TBD/TBA/to be determined/to be advised/NaN/Inf/XXX/<fill in>` | #7 |
| 容差 | 数量零容差（#8）；管长进位到整尺（#6）；中间值不四舍五入（#5）；容量/长度按题面单位 | #5 #6 #8 |
| 单位 | mm 项目单位 + placement 用米（#1 #2）；英尺（#3 场地用米）；#7 的 gpm/fps/ft w.c./gal/°F | 多 |
| 数值自洽 | 行合计=小计；bond=规则化值；extended_price=qty×unit_price；margin=(limit−achieved)/limit | #5 #6 #7 |
| 跨文件一致 | 同一 circuit_id / type_mark / 分节号在多个文件间一致；locations 汇总=设备总数 | #7 #8 #9 |
| 日期/口径 | #4 as-of 2025-07-14、目标日 2025-07-29；#5 #6 文档日期 10/31/2026 | #4 #5 #6 |
| 运行契约 | 300 s、确定性、无网络、不回读 PNG | #1 #2 |
| 非文件交付 | OpenProject 工作包（#4）；`final_message`（#4 #5 #6） | #4 #5 #6 |

---

## 5. 通用自检协议 SELFCHECK v1（harness 用）

### 5.1 设计原则

1. **任务无关**：只吃一份 `contract.json`（由 harness 从题面/任务卡自动生成，或按任务族模板填），
   不含任何具体答案或阈值分支。
2. **只查"能不能被判"，不猜"判多少分"**：检查全部是格式/存在性/自洽性，**不实现评分逻辑**。
3. **fail 才说话**：全绿就静默继续；绿了还反复查＝原地打转，明令禁止。
4. **可执行**：每条反馈必须带精确路径/列名/行号 + 一句改法。
5. **有预算**：每轮最多 12 条，最多 3 轮；3 轮后带着已知缺陷收尾（有文件 > 完美）。

### 5.2 contract.json（harness 侧）

```json
{
  "task_id": "...", "workdir": "/app/files/...",
  "deliverables": [{"path": "output/solution.py", "kind": "py", "required": true}],
  "archives":    [{"path": "output/submission.zip",
                   "members": ["flush_plan.csv", "MOP.md"], "allow_extra": true}],
  "secondary":   [{"kind": "final_message", "required": true},
                  {"kind": "app_record", "app": "openproject", "project": "DER Program Backlog Review"}],
  "vocab":  {"stage": ["HYDRO","INITIAL_FLUSH","CHEM_CLEAN","DRAIN_RINSE","PASSIVATE","HOLD","FINAL_FILL"]},
  "numeric_cols": ["flow_gpm", "velocity_fps", "duration_h"],
  "banned_tokens": ["TBD","TBA","to be determined","to be advised","NaN","Inf","XXX","<fill in>"],
  "units": {"project": "mm", "placement": "m"},
  "runtime_limit_s": 300,
  "deterministic": true,
  "cross_joins": [["flush_plan.csv:circuit_id", "MOP.md:circuit_id"]]
}
```

### 5.3 七道闸（G1–G7）

| 闸 | 查什么 | 通用实现 | 失败形态 |
|---|---|---|---|
| **G0 合约** | 题面读到了吗、交付物清单是否抽全 | 读 instruction，正则抽 "Save it as / Produce / Hand back / containing"，与 `task_cards.deliverables` 交叉核对 | 清单缺项 ⇒ 先补清单再谈别的 |
| **G1 存在性** | 精确路径存在、是**普通文件**（非目录/非 symlink）、大小 > 0 且 ≤ 95 MiB | `os.path.isfile` + `os.path.islink` + `stat().st_size` | 0 分形态，最高优先级 |
| **G2 归档** | zip 能开、成员齐、无绝对路径/`..`/重复名/加密 | `zipfile.ZipFile(...).namelist()`；`zf.testzip()` | 缺成员 ⇒ 列出缺哪些 |
| **G3 模式** | CSV 表头、JSON 必需键、xlsx 表名、docx 段落可读、IFC `ISO-10303-21` 头 | `csv.reader` 首行；`json.load`；`openpyxl.load_workbook(read_only=True).sheetnames`；`zipfile` 读 `word/document.xml` 去标签；`head -1` 查 IFC header | 表头/键名不符 ⇒ 给"期望 vs 实际" |
| **G4 词表/占位符** | 枚举值在闭集内；被读数值列无禁用占位符 | 逐列 `set(col) - set(vocab)`；对 `numeric_cols` 做占位符扫描 | 致命项（#7 的 TBD 类）单独置顶 |
| **G5 自洽** | 算术与跨文件 join | `extended_price ≈ qty×unit_price`；小计=Σ；`(limit−achieved)/limit` 在 [0,1]；`circuit_id`/`type_mark` 集合相等；locations 汇总=总数 | 给具体行号与两个值 |
| **G6 运行** | 脚本可执行、时限、确定性 | 重跑两遍 → `sha256` 比对；`time` 记录墙钟；干净 cwd、断网跑 | 不确定/超时 ⇒ 明确报"重跑不一致/超 X s" |
| **G7 交付纪律** | `final_message` 是否写了、覆盖所有交付物；有无 stray 文件破坏包 | 生成 summary 模板（每个交付物一行"路径 + 一句话"）；`zipinfo` 复查 | 缺 `final_message` ⇒ 直接补 |

### 5.4 反馈给模型的格式（固定）

```json
{
  "gate": "G4", "status": "fail",
  "items": [
    {"severity": "fatal", "path": "output/submission.zip",
     "detail": "flush_plan.csv 第 12 行 flow_gpm = 'TBD'",
     "rule": "题面：a cell reading TBD … scores the whole package zero",
     "fix": "填实际流量，或该阶段不适用则留空/写 NONE"}
  ],
  "next": "先清占位符，再复查 G5"
}
```

规则：
- **severity 只有 fatal / fix / note**；fatal 一次只报 1 条（避免淹没）。
- 每条必须带 `rule`（题面原句片段）——让模型能回去重读题面，而不是听"检查器说"。
- **不报分数、不报"预计得分"、不模仿 grader 措辞**；只说"违了哪条契约"。
- 每轮 ≤12 条，按 fatal → fix → note 排序；同一 gate 连续两轮同一条 fail ⇒ 升级为"已知缺陷"，不再重复。

### 5.5 接进 harness 主循环

```
写交付物 ──► SELFCHECK(contract)
                ├─ 全绿 ──► 发 done（带 final_message）
                ├─ 有 fatal ──► 回灌 1 条 ──► 模型修 ──► 重跑 SELFCHECK
                └─ 3 轮或预算将尽 ──► 确保文件完整可解析 ──► 发 done
```
接入点：**每次写盘后**（`write_file`/`exec` 成功）触发轻量 G1/G2；**每轮结束前**触发完整 G1–G7。
时间预算：自检耗时必须 < 任务时限的 5%（`agent_timeout_sec=6h`；单次 `exec` 默认 120 s，长检查要显式调大
`timeout_sec` 或拆步）。

### 5.6 L2 / L3：唯一合规的"分数反馈"

```bash
# 本地黑盒评分（dev set）：只产出分数与 artifacts/，不暴露评分实现
uv run python eval/run.py ../my-harness
uv run python eval/run.py ../my-harness --tasks data-hall-mep-routing   # 单任务重跑
uv run python eval/run.py --artifacts ../my-harness                     # 只对已提交 artifacts 评分（仅调 judge）
# judge 报错后：不要重跑 trial，只重打分
uv run python eval/run.py --regrade runs/<name>-<time>
```
（命令出自 `AGENTS.md` §8。harness 必须放在**比赛仓库之外的独立 git 仓库**；放在比赛仓库克隆内时 `kit check`
只警告、永远不报可以提交。）

**迭代节奏**：L1 让单次任务"不因格式丢分"；L2 给任务级分数；只在 L2 分数与预期差距大时改 harness 策略，
**不要**为提分去读 grader——那是 §11 红线，且 organizer 会审计 top-ranking 代码与 artifacts。

---

## 6. 禁止清单（红线）

| 禁止 | 原因 |
|---|---|
| 打开任务内 `tests/`、`evaluator/`、`dcgrade/`、`grader/`、`corrino/**/grading/` 下的评分实现 | §11 `inspect grading scripts` |
| 让自检器按 task_id / 特定数值分支 | §11 `fingerprint specific tasks` |
| 在 `artifacts/` 里放预置答案或手改结果 | §11 `precomputed answers`、`Never create or edit artifacts/ by hand` |
| 绕过网络限制、动评估容器 | §11 |
| 用 symlink 交付、单文件 > 95 MiB、缺目录、无 artifact | `AGENTS.md:243` 记 0 |
| 反复跑同一检查而无新信息 | 边际收益归零 ⇒ 停 |

---

## 7. 在途 / 未完成

- `eor-review-corrections`：题面与 `environment/` 已下载（`grading=checks-judge`，`llm_judge=true`，
  deliverables = `…/dc_structural__eor_revision__blind/output/submission.zip`），但**验证材料尚未下载完**，
  本轮未纳入表 A。
- `fill-compaction-acceptance`：目录已出现但内容不完整（`instruction.md` 读取失败），未纳入。

---

## 8. 证据索引

| 结论 | 出处 |
|---|---|
| 交付物精确路径/名/格式决定大头分数 | `AGENTS.md:192` |
| rubric 判官读 `final_message` | `AGENTS.md:193` |
| 早写初版、留时间发 `done` | `AGENTS.md:195` |
| ±95 MiB / symlink / 无 gradable deliverables ⇒ 0 | `AGENTS.md:243` |
| dev 集评分命令与 `--regrade` | `AGENTS.md:201-229` |
| §11 Fair play 原文 | `AGENTS.md:278` |
| `exec`/`write_file` 协议、120 s 默认超时 | `AGENTS.md` §7 表格与说明 |
| 各任务 workdir / deliverables / grading / 超时 / MCP | `task_cards.jsonl`（29 条，本文用 9 条） |
| blind 措辞（无 verifier/oracle） | #3 #7 #8 #9 题面原文 |
| `express_rules=True`、mm/米、300 s、不回读 PNG | #1 #2 题面原文 |
| 7 个 stage token、占位符致命、skid 闭集 | #7 题面原文 |
| 数量零容差、locations 一行一格 | #8 题面原文 |
| UFGS 改写、`-- End of Section --`、7 张表 | #9 题面原文 |
| 容器可用工具（库版本/apt 包） | 各任务 `environment/Dockerfile`、`requirements.txt` |
