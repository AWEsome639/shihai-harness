# PLE artifacts 攻坚方案（2026-11-13 夜）

> **背景**：GitHub 已通（`AWEsome639/shihai-harness`，commit `9eeed5c7`）。
> 只剩 `artifacts/` 一道门 —— 它需要 x86_64，而本机是 arm64。
>
> **已确认**：`kit artifacts` 只拒绝「一个可用任务都没有」的提交。
> **⇒ 只要 1 个任务跑通，提交就有效。**

## 已知事实（全部实测过）

| 事实 | 依据 |
|---|---|
| `PLATFORM = "linux/amd64"` 写死在 `kit/stack.py:65` | 读源码 |
| arm64 上跑 amd64 镜像要靠 qemu —— **sidecar 需要 nftables/netlink，qemu 不提供** | 实测报错 `Unable to initialize Netlink socket: Protocol not supported` |
| 关掉 egress 控制也不行 | harbor 报 `network_mode='no-network' is not supported by EnvironmentType.DOCKER` |
| 最小的 dev 任务：`data-hall-mep-routing-offset`（34 文件 / 2 MB） | 实测文件数与体积 |
| `kit check` 已通过 7 个阶段（含真实模型 smoke + MCP） | 之前的实测 |

## 目标

跑通 **1 个** dev 任务，产出 `artifacts/<task-id>/`，让 `kit artifacts` 至少报 `1 of 29 dev tasks usable`。

## 步骤（00:00 后执行）

### ① 备份现状
```bash
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' > ~/.dsh/work/ple/images-before.txt
docker ps -a > ~/.dsh/work/ple/containers-before.txt
colima list
```

### ② 重建 colima 为 x86_64
```bash
colima stop
colima delete -f          # ⚠️ 会删掉所有镜像和容器
colima start --arch x86_64 --cpu 4 --memory 6 --disk 60
docker version --format '{{.Server.Arch}}'   # 必须输出 amd64
```

### ③ 验证 qemu 能跑 amd64
```bash
docker run --rm --platform linux/amd64 alpine uname -m   # 期望 x86_64
```

### ④ 跑最小任务
```bash
cd ~/.dsh/work/ple/ple
export MODEL_PROXY_URL=http://localhost:4000 MODEL_PROXY_KEY=localdev
export HF_TOKEN=$(cat ~/.dsh/.hf_token_write)
timeout 7200 uv run python eval/run.py ../my-harness \
  --tasks data-hall-mep-routing-offset 2>&1 | tail -40
```

### ⑤ 看结果
```bash
ls ../my-harness/artifacts/
uv run python -m kit artifacts ../my-harness 2>&1 | head -6
```

### ⑤·5 ⚠️ 仓库必须公开（正在本计划里漏掉的一条）
> AGENTS.md §1 原文：**「Use a public repository, or a private repository owned by the participant
> with the competition's GitHub App installed」**
>
> 我们的 `AWEsome639/shihai-harness` 现在是 **private**，而**没有装 competition GitHub App**
> ⇒ **提交前必须转 public**，否则会被拒。

```bash
curl -s -X PATCH -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/AWEsome639/shihai-harness -d '{"private":false}'
```

### ⑥ 推上 GitHub
```bash
cd ../my-harness && git add -A && git commit -m "artifacts: 1 of 29 dev tasks" && git push
```

## 判据（提前写死，防止看到结果后自我说服）

| 情况 | 判断 |
|---|---|
| 单任务 **> 2 小时**没跑完 | **放弃**，说明 qemu 太慢，这条路走不通 |
| `docker run --platform linux/amd64` 直接失败 | **放弃**，colima 不支持 x86_64 模拟 |
| sidecar 仍报 netlink 错误 | **放弃**，qemu 层解决不了 |
| 任务跑通但 `kit artifacts` 仍报 0 usable | 检查 artifacts 目录结构 |
| **成功** | 立刻 commit + push，**写一份 G0 复现记录** |

## 回滚（无论成败都要做）

```bash
colima stop && colima delete -f
colima start --arch aarch64 --cpu 4 --memory 6 --disk 60
# 重新拉需要的镜像（builder 1.1G / harness 1.3G / task 585M）
```

**⚠️ 回滚必须在 07:00 前完成** —— 白天他要机器，不能留一个坏掉的 Docker。

## 如果放弃

**如实记：PLE 的 artifacts 需要一台真 x86_64 主机。**
- 租一台 CPU 云主机（¥1–2/小时）就能做 —— 但那要花钱，得他点头
- 或者等 AutoDL 那台 GPU（13 天后释放）

## 时间预算

| 阶段 | 预计 |
|---|---|
| 备份 + 重建 colima | 15 分钟 |
| 拉镜像（qemu 下会慢） | 20–40 分钟 |
| 跑 1 个任务 | ? ← 这是未知数，也是判据要盯的 |
| 回滚 | 15 分钟 |

**总预算 2 小时；超过就按判据放弃。**
