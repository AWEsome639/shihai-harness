# 写稿修法 B（把描述改成仓库根目录实况）

## 替换 1（第 8 行，§1 The entry 首句）
原文：
> `shihai-harness` is a single-process Python 3.12 harness in four files: `serve.py` (1,973 lines), `taskbridge.py` (1,528, byte-identical to the organisers' `cus…

改为：
> `shihai-harness` is a single-process Python 3.12 harness. `serve.py` (21 lines) opens the WebSocket the evaluator speaks on `$PORT` and hands each task to `harness.solve`; `harness.py` (415 lines) holds the system prompt and the solve loop; `taskbridge.py` (1,528 lines, byte-identical to the organisers' `customer…`) is the transport. The agent itself is `pi`, a coding agent installed by the build from the repository's own `package.json`, driven offline over stdio with one tool extension (`task-tools.ts`).

## 替换 2（§4.2 Four gates, one probe → 改为 prompt-enforced verification）
原段（`contract.py` + `selfcheck.py` 四道门 + probe）整段替换为：
> ### 4.2 Verification is a procedure the model must run, not a module
>
> There is no separate checker binary. The gates live in the system prompt as an ordered procedure the model is told to execute with its own shell:
>
> - **Deliverable gate.** Before finishing, list the output directory and confirm every required path exists and is non-empty; a deliverable that exists only in the reply scores zero.
> - **Format gate.** Re-read what you wrote: the archive members if the deliverable is a zip, the sheet names if it is xlsx, `ISO-10303-21` if it is IFC. If the instruction names a `.zip`, the zip itself is graded — a directory of loose files beside it scores zero.
> - **Contract gate.** Turn the instruction into a checklist of its acceptance criteria — every numeric threshold, every `must` — and check the deliverable against each one; run any validator the workspace ships.
> - **Cross-gate gate.** Fixing one gate can break another; after every change re-run every gate you can test, in full, and report the numbers for all of them.
>
> The reason this is prompt-enforced rather than coded: the acceptance criteria differ per task and are written in prose the model can read. A generic checker can confirm a file parses; it cannot know that "every row is served" means what this drawing set says it means.

## 应用前提
- 先把 trial 结果拿到（zip 题 + trials=3），因为替换文本里要引用真实分数
- 字数：替换 2 比原段长约 60 词 ⇒ 需要从 §3（context management）压缩等量文字
