# 待应用的写作稿改动（方差诚实化）

## 改动 1（必须）
**位置**：第 115 行
**原文**：
> One change - each conclusion as its own checkable sentence - moved it from 0.0 to **0.529** on the dev-proxy model.

**改为**：
> One change - each conclusion as its own checkable sentence - took a single dev-proxy run from 0.0 to **0.529**. Rubric scores vary widely between runs, so we claim the direction of that change, not the level.

字数：+11 词

## 改动 2（为腾字数）
**位置**：§7 第一条
**原文**（截断前的完整句需现场确认）：
> All 29 dev tasks produced an `artifacts/` entry and a `final_message.md`; the kit's review accepts 28 of 29 (one task, ...)

**改为**：
> All 29 dev tasks produced an `artifacts/` entry and a `final_message.md`; the kit's review accepts 28 of 29.

字数：−11 词左右（删掉括号里的个案说明）

## 应用前提
- trials=3 的结果：
  - 三次全 0 ⇒ 必须应用（否则写稿是「挑最好一次报」）
  - 三次都 ≥0.5 ⇒ 只改「a single dev-proxy run」三字，改动 2 可免
- 应用后用 `wc -w` 复核 ≤2000
