# 本地重跑 dev set 可行性

摸底时间：2026-11-16 00:49–01:05（本机实测，非推断）

## 一句话结论

**不可行。** 不是「慢」，是四条硬门槛各自都能否决：**模型是替身**（本地把 `claude-opus-5-5` 映射到 `openai/deepseek-chat`）、**内存差 2–4 倍**（要 16.25 GB / 有 8 GB）、**amd64 模拟 + `eval/run.py` 预构建漏传平台**（不设变量就 100% 在 verifier 挂）、**磁盘不够**（官方要 ~10 GB base + 12 个 rubric 世界镜像约 17 GB 级；本机起点 27 GB 空闲，跑**一个最小 task** 就掉到 19 GB）。本地跑出来的 artifacts 即使勉强生成，也不是「真模型 + amd64」的产物，交上去只会更糟。

## 环境

- **uv**：有，`0.12.23`（/Users/a0000/.local/bin/uv）
- **docker**：CLI 有 `29.9.3`；**daemon 原本没跑**（colima context 无 socket）。我起了 colima（vz，4 CPU / 4 GB / 25 GB），engine `29.5.2`，架构 **aarch64** —— 本地 socket，`DOCKER_HOST` 未设 ⇒ **是本机的**，不会被拒。
- **harbor**：有，`v0.20.0`（`/Users/a0000/.local/bin/harbor`）。**不需要再装**，`uv tool list` 已见 harbor/hb/hr ⇒ 前置安装 0 分钟。
- **比赛仓库位置**：`~/.dsh/work/ple-upstream/`（含 `kit/` + `eval/run.py`，remote `google-research/ple`）。另有一份干净副本 `~/.dsh/work/ple-kit/`。
  - ⚠️ **`ple-upstream/kit/stack.py` 被改过**：多了一段往 `/tmp/stack_state.log` 写调试日志的代码（改动者留了 `.bak-20261114`）。**要复现必须用 `ple-kit` 的干净 kit**，否则 kit 本身不是提交里那份。
  - 我们的 entry = `~/.dsh/work/ple/`（`submission.yaml` 在这里，manifest 的 12 个文件也对得上）。
- **模型代理**：`~/.dsh/work/ple/proxy-config.yaml` 把 `claude-opus-5-5` 指到 `openai/deepseek-chat` @ api.deepseek.com。litellm 原本没跑，我用 `litellm_wd.sh` 拉起（:4000，实测能回包）——**但这就意味着本地所有 run 用的都不是榜上那个模型。**

## dev set 规模

- **task 数量：29 个**（`eval/dev_set.yaml` 与 `data/tasks/` 都是 29）
- 单 task 资源（`task.toml`）：2–4 CPU、**4–12 GB**；再加 serve+egress 2 CPU / 4.25 GB
- 全量单个并发槽位：**6 CPU / 16.25 GB**（即 8 核机器一次只跑一个）
- **预估全量耗时：官方上限 174 小时串行**（每 task 6 h 硬上限）。现实估计光构建就 12 h+：12 个 rubric task 的世界镜像各约 17 GB、每个给 1 小时构建预算。本机还要叠 amd64 模拟的减速。

## 试跑记录

- **试的哪个 task**：`data-center-site-model`（全表最小：2 CPU / 4096 MB）
- **命令**（用本地 task 目录，免 HF 下载）：
  ```
  cd ~/.dsh/work/ple-upstream
  uv run python eval/run.py ~/.dsh/work/ple \
    --tasks data-center-site-model \
    --tasks-dir ~/.dsh/work/ple/data/tasks \
    --no-export --env-file /tmp/ple-local.env --out /tmp/ple-trial-2
  ```
  （`--no-export` 是关键：不带它会把结果写进 entry 的 `artifacts/`，会毁掉现有 artifacts）
- **耗时 / 内存**：
  - harness 镜像构建 `plebench-builder`（1.13 GB）：约 **5.5 分钟**
  - task 镜像构建：约 **35 秒**
  - harness 实跑：**32 秒**，3905 in / 138 out tokens
  - 总墙钟：**约 7 分钟后被我 420 s timeout 掐断**
  - 内存：给 VM 4 GB 就把 **swap 从 6.7 GB 顶到 9.7 GB**（宿主物理 8 GB，`memory_pressure` 空闲 27%）
  - 磁盘：**27 GB → 19 GB 可用**（一个最小 task 就吃掉 8 GB）
- **结果：失败。** 但失败点很有价值 —— **harness 本身跑通了**：
  - `serve.py` 起在 8765，harness 读了 `Task.md`、写了 `clash_check.py`、**产出了 `submission/model.ifc`（status ok）**
  - 挂在 **verifier 构建**：
    ```
    no match for platform in manifest: not found
    FROM plebench-task-data-center-site-model:0eb7ece003d569fc2e8e
    ```
  - **根因（已定位到行）**：`eval/run.py:1231` 预构建 task 镜像用的是
    `["docker","build","-q","-t",tag,dir]`，`env={**os.environ, "DOCKER_BUILDKIT":"1"}` —— **没有把 `DOCKER_DEFAULT_PLATFORM` 传进去**，于是按宿主架构构建成 **linux/arm64**；而 Harbor 的 verifier 侧被 `eval/run.py:2428` 强制 `DOCKER_DEFAULT_PLATFORM=linux/amd64`。两边对不上，`FROM` 就找不到 amd64 变体。
    实测证据：该镜像 `Created 2026-11-16T01:00:37`、`Os/Arch = linux/arm64`（就是这次构建出来的，不是陈旧缓存）。
  - **注意这是 `eval/run.py` 与 `kit/` 的不一致，不是 kit 的 bug**：`kit/build.py:342` 用 `stack.build_env()`（含 `DOCKER_DEFAULT_PLATFORM`）建 builder 镜像，所以 `plebench-builder` 实测正确地是 **linux/amd64** ✅；只有 task 镜像这条预构建路径漏了。
  - **一行 workaround**：跑之前 `export DOCKER_DEFAULT_PLATFORM=linux/amd64`，预构建会经 `os.environ` 继承下来（代价：task 镜像改走 amd64 模拟，构建变慢）。
  - 也就是说：**不设这个变量的话，29 个 task 会以完全相同的方式全部失败**。

## 建议

**现在不要跑全量。** 条件不成熟，且跑出来的东西不能用。

**最省事的一条路：让 CI 跑，不要在本地跑。**
`eval/run.py` 明确是给 linux/amd64 + 16 GB 内存 + 真模型设计的；GitHub Actions 的 linux runner 同时满足这四条，且 artifacts 与触发它的 commit 天然同源 —— 这正好是「artifacts 由该 commit 的 harness 生成」这个检查要的东西。这条路不需要本机出任何算力。

**如果非要在本地跑，先满足这四条再来找我：**
1. 机器 ≥ 32 GB 内存（16.25 GB 得留给 Docker，且不能靠 swap）
2. 磁盘 ≥ 40 GB 空闲（12 个 rubric 镜像约 17 GB 级别，加 base 和世界镜像）
3. 真模型 key（`claude-opus-5-5` 的真实上游，不是 deepseek-chat 替身）——否则 artifacts 与服务器复现必然对不上，**比不交更糟**
4. 跑之前 `export DOCKER_DEFAULT_PLATFORM=linux/amd64`（绕开上面那个预构建漏传；不改代码即可），并接受 amd64 模拟导致**计时类 verifier 分数系统性偏低**（官方 README 明说）

预计：即使四条都满足，全量也要 **1–2 天**，且分数仍非最优。

## 本次摸底对环境做了什么（已复原）

摸底前：Docker daemon **没跑**、无 colima 实例、litellm **没跑**、磁盘 **27 GB 空闲**。
收尾后：全部还原 —— colima 实例已 delete、镜像已删、litellm 已停、残留 VM 盘与 colima 镜像缓存（共 ~3.9 GB）已清，**磁盘回到 27 GB 空闲**，内存空闲 27% → **45%**。
保留了 `--out /tmp/ple-trial-2/`（62 MB，试跑证据，重启即清）与 `~/.dsh/work/ple/logs/LOCAL-DEVSET-FEASIBILITY.md`。

## 未验证项（如实标注）

- `HF_TOKEN` 未设；本次靠 `--tasks-dir` 绕开 Hugging Face。**本地 `data/tasks/` 与 `dev_set.yaml` 的 `sha256`/`dev_set digest` 是否一致我没有校验**（manifest 记的 dev_set 是 `ce9ebf50…`）。要正式跑之前必须校验，否则跑的是别的版本的题。
- `tests/evaluator/` 下的官方评分器按要求**没有读**。
- 未改动 `harness.py`、未 commit/push、未触发 CI。`artifacts/` 未被写入（全程 `--no-export`）。
- **`harness.py` digest 不一致已复核**：manifest `44b2fe39a3846b…` vs 当前 `2e91bb21708eb1…`（`git hash-object`）——与你说的相符。
- 机器上有个与本事无关的重活：DevEco 模拟器 `-start onemore7`（PID 44109，77% CPU、616 MB），已跑 45 分钟，属 One More 项目遗留。**我没有动它**（它不是本次失败的原因），但它在持续吃 CPU。
