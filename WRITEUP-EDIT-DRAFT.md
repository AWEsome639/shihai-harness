# 写提改写稿 —— 待今晚（前台可用时）贴到 Kaggle

## 目标

把 `WRITEUP-FINAL.md` 第 112–116 行那段，换成下面这版。

**原段落（约 110 词，现 2,000 词全文）：**

> The failure was in the pipe, not the reasoning. Early passes scored 0.0 while all 29 tasks finished.
> The agent never issued a successful model call: the model name selects the wire protocol. Once fixed, it
> ran - 77 calls, a 54 kB memo - and still scored zero: no claim could be scored alone.
> One change - each conclusion as its own checkable sentence - moved it from 0.0 to **0.529** on the dev-proxy model.
> A second dev task, three-dimensional routing, stayed at zero: our clash checker took its count from
> 36 to 0, yet the official scorer found three, and connectivity failed.

## 替换稿（实测 **216 词**；原文约 110 词 ⇒ 全文会到约 **2,105 词**，超 2,000 ⇒ 需在别处删 ~105 词）

```
The failure was in the pipe, not the reasoning. Early passes scored 0.0 while all 29
tasks finished. The agent never issued a successful model call: the model name selects
the wire protocol. Once fixed, it ran - 77 calls, a 54 kB memo - and still scored zero:
no claim could be scored alone. One change, each conclusion as its own checkable
sentence, moved it to **0.529**; a later note, that a gate usually has several
independent legs and a leg you cannot test is not a leg that passes, took the same task
to **0.918** on the dev-proxy model.

Three-dimensional routing stayed at zero through three passes, and each pass failed a
different leg. Pass one served every row 51% of its length and never reached the
service entry. Pass two reached 0.97 coverage with clean connectivity and acquired
seventeen hard clashes: lengthening routes to feed the rows is how a clean model starts
colliding. Pass three told the agent that a leg it had already passed is not a leg it
still passes, and to re-run every gate after each change. Throughout, our own clash
checker reported zero. The official scorer never agreed, because a checker we wrote
measures the boxes we chose to compare, not the gate the score is taken from.
```

## 为什么值得改

| 现在写的 | 改后 |
|---|---|
| 0.529（一个中等数字）| **0.918（接近满分）** |
| 一句「几何任务仍是 0」 | **三轮三个不同失败腿，每轮都有具体数字** |
| 没解释为什么自查器没用 | **点明「自查器量的是我选的盒子，不是门」** |

**这三条合起来才是这个 harness 真正的卖点：它记录失败、区分失败、并且每次只改一层。**

## 执行步骤（今晚）

1. 打开 `https://www.kaggle.com/competitions/physical-engineerings-last-exam/writeups/new-writeup-1794634472056`
2. 找到第 112–116 行那段（以 `The failure was in the pipe` 开头）
3. 整段替换成上面的替换稿
4. **保存前先看字数**：替换后约 **2,105 词**，超 2,000
   ⇒ **需从别处删约 105 词**。建议删 §Reproducibility 里那张九行脚本表（约 95 词）
   保留一句话说明「九个脚本、单一来源、可复现」即可
5. 保存，确认页面仍显示 `Submitted`

## ⚠️ 注意

- **不要动 entry SHA**（正文没引用 SHA，只有仓库 URL + `154ce35`）
- **改完不要重新提交**（写提已经在 Submitted 状态，编辑即生效）
