# PLE 提交套件（`kit`）技术手册 —— 写出「能过 check」的 harness

> 仓库：`/Users/a0000/.dsh/work/ple/ple`（Google PLE Bench，commit `8320f04`）
> 阅读对象：后续要改 harness / 跑 dev set / 提交的人
> 原则：**一切以仓库代码与 `docs/` 为准**；工作区 `AGENTS.md` 有些数字已经过期（见 §14）。
> 本手册中 `文件:行号` 都是该 commit 的真实位置，可直接跳读。

---

## 0. 一页结论（TL;DR）

一个能过 check 的 harness 必须同时满足五件事：

1. **它是一个 WebSocket 服务端**：`serve` 命令在容器里启动，120 秒内 bind `0.0.0.0:$PORT`，一个任务一个连接，跑完必须发 `done`（或 `error`）。
2. **它只通过 4 个动作操作任务容器**：`exec`（bash -c）、`read_file`、`write_file`、`http`（只能打任务的 MCP server）。不能联网、不能起后台守护进程、不能直连任务容器的其它端口。
3. **模型只能打自己声明的那一个 endpoint**：出口被 egress 代理锁死（TLS SNI 校验），web search / 外部 API / provider 内置工具全部被禁。
4. **交付物必须落在 dev_set.yaml 声明的 `deliverables` 路径下**，格式/文件名一字不差；`final_message` 要写清"做了什么、在哪、关键数字"。
5. **仓库根 = entry 根 = git 仓库根，全部提交**；`artifacts/` 必须是 `eval/run.py` 跑出来的原样产物，`manifest.json` 的 harness digest 要与提交的 harness 一致。

一句话记法：**`serve.py` 是壳，协议是命脉，交付物是分，`done` 是命。**

---

## 1. 系统全貌：一次任务到底发生了什么

`kit/README.md:47-53` 给了最准确的图：

```
  build container (once)              serve container (one per task)          task container (one per task)
  debian:bookworm + uv + Node 22      builder image + your /opt/harness       the task's files and tools
  internet access, no task data       your server listening on $PORT          no network access
  install runs as root                internet only via the egress proxy      your requests run here
          |                                    ^         |
          +--> /opt/harness (the bundle) ------+         +-- websocket <--> evaluator --> exec / read / write / http
```

拆开看是七个阶段：

| # | 阶段 | 在哪里跑 | 关键事实 |
|---|---|---|---|
| 1 | clone 提交的 commit（含 `.git`） | build 容器 | 匿名 clone，不跑 LFS，不带云凭证 |
| 2 | 跑 `install`（root，有网，30 分钟） | build 容器 | 只有 `/opt/harness` 会被打包；`apt-get` 装的东西**不保留** |
| 3 | 打 bundle → serve 镜像 | 本地/评测机 Docker | 只装 `/opt/harness`（2 GiB 压缩 / 8 GiB 解压） |
| 4 | 每任务起一个全新 serve 容器，跑 `serve` | serve 容器 | uid 1000，只读根，2 CPU / 4 GB / 512 进程 |
| 5 | 评测端连 WebSocket，发 `task` 帧 | — | 120 秒内必须 accept |
| 6 | harness 发请求 → 评测端在任务容器执行 | 任务容器 | 无网络；4 种请求；同时最多 8 个 in-flight |
| 7 | 收交付物 + `final_message` → 独立 verifier 打分 | verifier 容器 | harness **不参与**；只看 `deliverables` |

三个容器是**物理隔离**的：harness 的代码在 serve 容器里，任务的文件在 task 容器里，两者之间只有那条 WebSocket。任务容器里没有 harness 的代码，serve 容器里没有任务文件。

---

## 2. 提交物结构

`kit/README.md:12-17`：

```
submission.yaml      how to install and start your harness, and which model it calls
serve.py             your harness: a WebSocket server (any language; `serve` runs the command you give)
artifacts/           what your harness produced on the dev set, exported by eval/run.py
...                  anything else it needs: requirements.txt, prompts, plugins, a vendored agent
```

硬规则：

- **entry 是"一个 GitHub 仓库的一个 commit"**，`submission.yaml` 必须在**仓库根**。
  在竞赛仓库 clone 里面、或在某个子目录里，`kit check` 只会 WARN，永远不打印 `ready to submit`（`kit/check.py:1058-1086`，`UNREADY` 文案见 `kit/check.py:475-506`）。
- 必须**全部提交**（`git add -A`）；有未提交改动 → `unready="uncommitted"`。
- 不要用 Git LFS 存 harness 文件（`check.py:1167-1174` 直接 FAIL：评测端 clone 时没有 LFS，只会拿到 pointer 文件）。
- 脚本必须是 LF 行尾（CRLF 会 WARN，因为 Linux 上 `bash` 会报 `$'\r': command not found`，见 `check.py:272-307`）。
- 大文件直接 commit（GitHub 单文件 ≤100 MiB）；submodule 必须是公开 `https://`。
- **不要把 harness 代码放进 `artifacts/`、`Artifacts/`、`artifact/`、`dev_artifacts/`、`dev-artifacts/` 或 `runs/<run>/artifacts/`** —— 这些目录会被 build 剔除（`kit/README.md:19`，判定函数 `kit/artifacts.py:243-257` 的 `bundle_excluded`），放进去的代码在评测时**根本不存在**。

---

## 3. `submission.yaml` 全字段

### 3.1 模板（`kit/README.md:24-32`）

```yaml
name: my-harness                          # leaderboard entry name: 2-39 characters of a-z, 0-9, and -
model_api: google-cloud/claude-opus-5-5   # the model your harness calls
install: >-                               # optional: runs once at build time as root, with internet access
  uv venv --python 3.12 /opt/harness/venv
  && uv pip install --python /opt/harness/venv/bin/python -r {src}/requirements.txt
serve: /opt/harness/venv/bin/python {src}/serve.py    # required: starts your WebSocket server on $PORT
env:                                      # optional: non-secret settings, as quoted strings
  LOG_LEVEL: "info"
```

### 3.2 字段逐条（代码校验在 `kit/spec.py`）

| 字段 | 必填 | 规则 | 代码位置 |
|---|---|---|---|
| `name` | 是 | 2–39 字符，`[a-z0-9-]`，首字符是字母或数字；正则 `[a-z0-9][a-z0-9-]{1,38}` | `spec.py:34,179-185` |
| `model_api` | 是 | `<provider>/<model>`；provider ∈ `google-cloud`、`openai`、`anthropic`、`gemini`、`openai-compatible`；`google-cloud` 的 id 必须在这 8 个里 | `models.py:27-36,214-254` |
| `model_base_url` | 条件 | 只在 `openai-compatible/` 时允许且必须；`https://` + 公网 DNS 名（不能是 IP、不能带 userinfo/query/fragment、不能以 `/chat/completions` 结尾） | `models.py:257-319` |
| `install` | 否 | shell 命令字符串；`{src}` → `/opt/harness/src`；`${src}`/`${port}` 留给 shell 自己展开 | `spec.py:124-139,335-349` |
| `serve` | **是** | shell 命令；`{src}` 与 `{port}` 会被填（端口也在 `$PORT`）；必须能被 `shlex.split` 拆开（引号平衡），不能只有注释 | `spec.py:204-222` |
| `env` | 否 | `NAME: "value"`，键匹配 `[A-Z_][A-Z0-9_]{0,63}`，值必须是**带引号的字符串** | `spec.py:223-244` |

其它约束：

- 文件 ≤ **16 KiB**；不是 UTF-8 / YAML 解析失败 / 嵌套过深都会报 SpecError（`spec.py:29,261-269`）。
- **未知字段直接拒绝**：`unknown key ... (allowed: name, model_api, model_base_url, install, serve, env)`（`spec.py:172-178`）。
- `secrets` 与 `egress` 两个旧字段被显式移除：`"removed; the competition takes no keys"` / `"removed; a harness reaches its model's endpoint and no other host"`（`spec.py:46-55`）。
- `install`/`serve` 里出现 NUL、孤立代理对（lone surrogate）→ 拒绝（`spec.py:142-158,197-202`）。
- `install`/`serve` 只认 `{src}`，`install`/`env` 里出现 `{port}` → `"{port} is not available here"`（`spec.py:37-45,124-139`）。

### 3.3 `env` 的保留名（改了直接拒绝）

`spec.py:56-68` 的 `RESERVED` = 下面 8 个 + `models.ENV_NAMES`（10 个）：

```
PORT, HOME, USER,
HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY, NODE_USE_ENV_PROXY,
MODEL_API, MODEL_NAME, MODEL_BASE_URL, MODEL_API_KEY,
OPENAI_BASE_URL, OPENAI_API_KEY, ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY,
GOOGLE_GEMINI_BASE_URL, GEMINI_API_KEY
```

报错分两种：`set by the evaluator`（评测端自己设的）或 `reserved: a model variable ... your model does not use`（你的模型用不上的模型变量，`spec.py:100-121`）。

### 3.4 `env` 值必须带引号的原因

YAML 会把 `true` 读成 bool、`1.10` 读成 float、`0755` 读成 int。代码会检查 `isinstance(v, str)`，否则报
`env.MAX_TOKENS: quote the value (MAX_TOKENS: "..."); YAML read it as int 32000`（`spec.py:234-238`）。

同时 `env` 值里**不允许** `${...}` 插值、换行、NUL、孤立代理对（`spec.py:239-244`）。

### 3.5 `model_api` 的两套语义（很容易踩）

- **可提交**：只有 `google-cloud/<id>`。8 个 id 见 `kit/models.py:27-36`：

  `gemini-4-argon`、`gemini-3.8-flash`、`gemini-3.1-pro-preview`、`claude-opus-5-5`、`gpt-oss-120b`、`glm-5.2`、`gemma-4-26b`、`llama-4-maverick`。

- **只能本地跑**：`openai/`、`anthropic/`、`gemini/`、`openai-compatible/`。`kit check` 会通过，但永远 `unready="own-model"`，文案是
  `"model_api ... is a model of your own, which runs locally only: the competition accepts only google-cloud/<id> models"`（`spec.py:352-370`，`check.py:989-992`）。

> 注意：工作区 `AGENTS.md` 列的 13 个模型（含 qwen3-coder / kimi-k2 / minimax-m2 / deepseek-v3.2 等）**在本 commit 的代码与 `docs/models.md` 里不存在**。以 `kit/models.py:27-36` 为准。

### 3.6 serve 容器里实际拿到的环境变量（`kit/models.py:346-377`）

| 变量 | 值 |
|---|---|
| `PORT` | 必须监听的端口 |
| `MODEL_API` | `submission.yaml` 里写的完整字符串 |
| `MODEL_NAME` | 裸模型 id（如 `claude-opus-5-5`）——**请求里 model 字段用它** |
| `MODEL_BASE_URL` / `MODEL_API_KEY` | OpenAI 兼容 base（`.../v1`）与 key |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | 同上，所以 `AsyncOpenAI()` 零配置可用 |
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` | Claude 系模型才有；base **不带** `/v1` |
| `GOOGLE_GEMINI_BASE_URL` / `GEMINI_API_KEY` | Gemini 系模型才有；base **不带** `/v1` |
| `env:` 里的键 | 原样透传（`{port}` 已替换） |

评测端 key 只对声明的那一个模型有效：调用别的模型会拿到 **403**（`docs/models.md:82-86`）。

### 3.7 `install` 的工程约束（血泪点）

`kit/README.md:59-65` + `kit/builder/Dockerfile`：

- 运行身份 `root`、有网、**30 分钟**上限、单文件 ≤ 8 GiB。
- 镜像里预装：`uv`（`/opt/harness/bin/uv`）、Node 22（`/opt/harness/node`，`npm -g` 装到 Node 目录）、`git`、`curl`、编译工具链、`python3`。
- **只有 `/opt/harness` 会被打包**：`apt-get install` 的系统包在 serve 容器里**不存在**。
- 建 venv 必须用 `uv venv --python 3.12 /opt/harness/venv`（Python 落在 `/opt/harness/python`）。**不要用 `python3 -m venv`**——它会 symlink 到 Debian 的 `/usr/bin/python3`，打包后是断链。
- 依赖要 pin 版本（评测端每次从零构建，`kit` 会复用缓存所以本地感觉不出来；用 `--rebuild` 验证一次）。
- Claude 的 harness 别忘 `requirements.txt` 里 pin `openai`/`websockets` 的版本（见 `sample_submissions/minimal/requirements.txt`）。

---

## 4. WebSocket 协议（protocol v1）

权威文档：`docs/protocol.md`；实现参考：`sample_submissions/taskbridge.py`；评测端实现：`kit/relay.py`；帧校验：`kit/protocol.py`。

### 4.1 基本形态

- 每个任务**一条** WebSocket 连接，UTF-8 **text** 帧，**一帧一个 JSON 对象**，≤ **16 MiB**（`docs/protocol.md:6`，`protocol.py:31`）。
- 服务端必须在 `0.0.0.0:$PORT` 监听；**120 秒**内要能 accept（`protocol.START_SEC=120`，`protocol.py:30`）。
- **不要开 keepalive ping**：
  > "Run your server without keepalive pings (`ping_interval=None` in Python `websockets`) or with a `ping_timeout` longer than your longest command."（`docs/protocol.md:49-51`）
  因为评测端可能因为 8 个 in-flight 打满而暂停读帧，ping 超时会把连接掐掉。
- 每个容器**只服务一个连接、一个任务**；任务结束容器即销毁。

### 4.2 握手：第一条消息是评测端发来的 `task`

连接建立后评测端**立刻**发（`docs/protocol.md:12-31`）：

```json
{
  "type": "task",
  "protocol": 1,
  "session": "3f2c9a1e0b7d4c55",
  "instruction": "<the task, verbatim>",
  "workdir": "/app",
  "limits": {
    "agent_timeout_sec": 21600,
    "exec_timeout_sec": 600,
    "max_output_bytes": 100000,
    "max_write_bytes": 10000000
  },
  "mcp_servers": [
    {"name": "apps", "url": "http://127.0.0.1:8000/mcp/", "transport": "streamable-http"}
  ]
}
```

字段语义：

- `workdir`：shell 起点，也是相对路径解析的基准（通常是 `/app`，也可能是 `/filesystem`）。
- `limits.agent_timeout_sec`：**剩余**时间（向上取整的整秒，至少 1），**无时限时为 `null`**（`protocol.py:143-183`）。
- `mcp_servers`：通常 `[]`；有值时是任务自带的"项目应用"（文档/表格/邮件/CAD），只能从任务容器内部访问。

> 时间预算的正确写法：**每次模型调用 / 每条命令之前都重新读剩余时间**，不要只在一开始算一次。

### 4.3 请求（harness → 评测端）

每个请求自带 `id`：非空 UTF-8 字符串，≤ 128 字符，不能有孤立代理对（`protocol.py:124-140`）。每个合法请求**恰好**收到一个 `{"type":"result","id":...}`。

| 请求 | 必填 | 可选 | 结果字段 |
|---|---|---|---|
| `exec` | `command`（非空） | `timeout_sec`（默认 120，夹到 1..600） | `exit_code`, `output`, `truncated`, `timed_out` |
| `read_file` | `path` | — | `ok`, `content_b64`, `size`, `truncated` |
| `write_file` | `path`, `content_b64`（**必填**，`""` 表示空文件） | `append`（bool） | `ok`, `size` |
| `http` | `url`（必须是任务 `mcp_servers` 之一） | `method`（默认 GET）、`headers`、`body_b64`、`timeout_sec` | `ok`, `status`, `headers`, `body_b64`, `truncated` |

具体语义（`docs/protocol.md:62-96`）：

**`exec`**
- 在任务容器里 `bash -c <command>`，cwd = `workdir`，身份是任务用户。
- **每次都是新 subshell**：文件改动持久，`cd` 和环境变量**不持久**。
- stdout/stderr 合并成 `output`；超过 100 KB 保留**头 + 尾**，`truncated: true`（`relay.py:77-79`：头约 2/3、尾约 1/3）。
- 超时：`min(timeout_sec, exec_timeout_sec, remaining-2)`，最小 1 秒；超时 `timed_out: true`、`exit_code: 124`。
- 命令太长（超过 argv 限制）会用临时脚本执行，仍受 16 MiB 帧限制。
- 退出码非 0 **不算错误**，正常返回 `exit_code`。

**`read_file` / `write_file`**
- 两个方向都是 base64；单次读 ≤ 10 MB、单次写 ≤ 10 MB（`protocol.py:38-40`）。
- 写大文件：多次 `write_file` + `append: true`（`taskbridge.py` 的 `WRITE_CHUNK = 4 MiB` 是参考值）。
- `write_file` **自动建父目录**。
- 两者都跟随 symlink；`read_file` 对非普通文件（FIFO/socket/device）返回 `ok:false`。
- 读超过 10 MB 的文件：会被截断（`truncated: true`），要么分段 `exec`（`head -c`/`split`），要么在容器里用 python 处理。

**`http`**
- 只能打任务声明的 `mcp_servers`（精确 URL、`<base>?query`、`<base>/subpath` 三种匹配；`.`/`..`/`%2F`/`%5C`/`\`/`;` 一律拒绝，`protocol.py:512-549`）。
- 不跟随 3xx，直接返回状态码和 `Location`。
- 请求/响应体 ≤ 10 MB；`timeout_sec` 默认 120、最大 600。
- 评测端会**丢掉** hop-by-hop 与 framing 头（`Host`、`Content-Length`、`Transfer-Encoding`、`Connection`、`Keep-Alive`、`Upgrade`、`TE`、`Trailer`、`Proxy-*`）再自己设（`protocol.py:55-68`）。
- 任务 MCP 是 **Streamable HTTP**：JSON-RPC `POST`（`initialize` → `notifications/initialized` → `tools/list` → `tools/call`），带 `Accept: application/json, text/event-stream`，复用返回的 `Mcp-Session-Id`，后续请求带 `MCP-Protocol-Version`，响应可能是 JSON 也可能是 SSE。
- **工具目录可能几百个、约 500 KB** —— 不要一次性塞进 prompt（见 §12.D）。

### 4.4 并发与背压

- **同时最多 8 个请求在跑**；第 9 个到来时评测端**暂停读取新帧**，直到有一个完成（`docs/protocol.md:45-51`）。
- 推论：如果你在第 9 个位置后面紧跟着发 `done`，`done` 也会被卡住。**收尾前先把并发降下来**（等待所有未完成请求，再发 `done`）。
- 单向消息（`log`/`usage`/`done`/`error`）不占槽位，读到即处理。

### 4.5 单向消息

| 消息 | 含义 | 细节 |
|---|---|---|
| `{"type":"log","text":"..."}` | 往试次日志追加一行 | 单条截断到 20 000 字符（`relay.py:1222`） |
| `{"type":"usage","model":"...","input_tokens":N,"output_tokens":N,"cache_read_tokens":N}` | 记录一次模型调用的 token | 计数必须 0..2^53-1 的整数；缺省为 0；`model` 非字符串会被判 malformed；最多记 16 个不同模型名（`relay.py:1229`） |
| `{"type":"done","final_message":"..."}` | 正常结束 | `final_message` 会交给 grader |
| `{"type":"error","message":"..."}` | 提前中止 | 已经写进容器的交付物**仍然评分** |

**`usage` 不是装饰**：`eval/run.py` 判断"harness 是不是根本没启动"时，会看 `output_tokens`（为 0 或没有 usage 报告时，看轨迹里有没有工具/模型活动）。不发 `usage` 有可能被误判成 not started 而重跑（`eval/README.md:188-194`）。

### 4.6 错误、malformed 与上限

- 任务容器里执行失败（文件不存在、权限、磁盘满、内存/PID 耗尽、Docker 拷贝卡 >60 秒）→ `ok:false` + `error`，**任务继续**（`docs/protocol.md:100-104`）。
- **malformed 帧**：有合法 `id` 就能收到 `ok:false` 回执；**累计超过 20 个 malformed 帧，整个 session 结束、任务算 harness 的失败**（`protocol.MAX_MALFORMED=20`，`protocol.py:32`）。
- malformed 的定义（`docs/protocol.md:106-117`）：非法 JSON / 未知 `type`；必填字段缺失或为空（`id`、`command`、`path`、`url`、`write_file` 的 `content_b64`）；`command`/`path` 里有 NUL 或非法 UTF-8；`write_file` 载荷 >10 MB；`http` URL 不在 `mcp_servers` 里；`usage` 的 `model` 非字符串或计数非整数。
- **任何超过 16 MiB 的帧 → 直接结束任务**（`docs/protocol.md:164`）。

### 4.7 超时与取消（最容易被扣分的地方）

`agent_timeout_sec` 到期时（`docs/protocol.md:143-150`）：

1. 评测端发 `{"type":"cancel","reason":"timeout"}`；
2. 任务容器里 in-flight 的请求被停掉，返回 `ok:false` + `"cancelled: the time is up"`；
3. WebSocket **再保持 10 秒**（`protocol.CANCEL_GRACE_SEC=10`），等你发 `done`。

**正确姿势**（`sample_submissions/minimal/serve.py`）：

- 收到 `cancel` 立刻**中断在途模型调用**，发 `done` + 已有的最好摘要。
- 主动做时间预算：`WRAP_UP_SEC = 240`（比时限提前 4 分钟开始收尾），并把 `tool_choice` 设为 `"none"` 让模型只写摘要。
- 把"目前最好的文本"挂在 `session.final_message`，`taskbridge` 超时/异常时会自动兜底发送。

`kit check --cancel-test` 就是干这个的：第 45 秒取消任务，检查 harness 是否仍在 10 秒内 `done`（`kit/check.py:1830-1880`）。

### 4.8 提前终止与责任归属

以下算**你的**失败（交付物仍然评分，但不重跑）（`docs/protocol.md:160-175`）：

- 没发 `done`/`error` 就关连接或崩了；
- malformed 帧 >20，或发出 >16 MiB 帧；
- 120 秒内没 accept；
- 出口连接打爆 egress 代理的 256 MiB 内存。

以下算**评测端**的失败，会重跑（`docs/protocol.md:172-174`、`eval/README.md:188-194`）：

- Docker daemon 错误、任务容器消失、egress 代理自己挂了（不是你流量打爆）、宿主/网络故障；
- 对 `google-cloud` 模型：endpoint 故障、超时、限流。

**混合判定要注意**：容器资源耗尽（OOM、PID 打满）来自你发的命令 ⇒ **算你的**，不重跑。

---

## 5. harness 怎么操作任务容器

### 5.1 只有一层抽象：4 个协议动作

| 能力 | 协议动作 | 等价工具名（sample 里的做法） |
|---|---|---|
| 跑命令 | `exec` | `bash` |
| 读文件 | `read_file` | `read_file` |
| 写文件 | `write_file`（可 append） | `write_file`、`edit_file`（读+改+写） |
| 调任务 MCP | `http` + JSON-RPC | `mcp_find` / `mcp_call` / 直接把工具摊平 |

没有"拷贝目录"、"解压"、"上传附件"这类原语——都用 `exec` 里的 shell 命令做。

### 5.2 为什么不做成"任务容器里的进程"

常见误解：以为 harness 可以和任务容器共享进程/文件系统。**不行**。

- harness 跑在 serve 容器，任务容器是另一个容器，两者**没有共享 mount、没有共享网络**。
- 唯一通道是 WebSocket；`exec` 是评测端代你在任务容器里执行。
- 所以 `subprocess.run(["ls","/app"])` 在 serve 容器里看到的是 serve 容器的 `/app`（不存在或有别的东西），不是任务的。

### 5.3 包装现成 agent 的两条路

- **本地 HTTP Relay**（`taskbridge.Relay`）：在 serve 容器里起一个本地 HTTP 服务，把子进程 agent 的 bash/文件调用转成协议请求。`relay.env()` 给它环境变量，`relay.mcp_url(name)` 给它 MCP 地址。`hermes-opus`/`pi-opus` 走这条。
- **工具重定向**（`pi-opus/task-tools.ts`）：把 agent 自带的 shell/文件工具实现替换成协议调用。

两条路的共同约束：子进程 agent **不能联网**（serve 容器只有模型出口），所以 agent 本体和依赖都必须在 `install` 阶段装好、打包进 `/opt/harness`。

### 5.4 容器里的"脏"细节

- 每次 `exec` 是新 subshell：**`cd` 不持久**。要么一条命令里 `cd /app && ...`，要么全部用绝对路径。
- 导出变量不持久：需要环境就每条命令前缀 `FOO=1 cmd`，或写文件。
- 后台进程有风险：试次结束时评测端会"杀掉试次期间在任务容器里启动的所有进程（不分用户和环境）"（`docs/protocol.md:175`）。但**用 `env -i` 清环境启动的后台进程不带标记，杀不掉**，会在收集交付物时继续改文件 —— 所以**自己在 `done` 之前把它们停掉**。
- 任务容器的 `/app` 是 tmpfs（本地 kit 里 `size=1g`）—— 别往里写大文件当暂存。

---

## 6. 容器与网络：硬边界

### 6.1 serve 容器（`kit/stack.py:894-965`）

| 项 | 值 |
|---|---|
| 用户 | `1000:1000`（非特权），`--cap-drop ALL`，`no-new-privileges` |
| 根文件系统 | **只读** `--read-only` |
| 可写 | `/tmp` tmpfs 1 GB、`$HOME`（`/home/harness`）tmpfs 1 GB |
| CPU / 内存 / 进程 | **2 CPU / 4 GB / 512 pids**（`stack.py:77`） |
| 网络 | 只能走 egress 代理；无直连 DNS |
| 生命周期 | 每任务一个全新容器，一个 WebSocket |

### 6.2 egress 代理（`kit/egress.py`）

- 只允许连接到**你声明模型的 host:port**（`allow` 列表 + 私有地址例外）。
- TLS 隧道必须 SNI = 该 host（"shared front end routes by that name"）。
- 拒绝可疑/歧义请求头（裸 CR/LF、控制字符、折叠头）。
- 空闲 **1800 秒**双向无字节即断（`egress.IDLE_SEC`）——所以**长模型调用要 streaming**，否则静默 30 分钟会被切。
- 代理自身内存上限 **256 MiB**（`stack.EGRESS_MEMORY="256m"`），打爆算你的失败。
- 连接上限：serve 侧出向 **256** 条、评测端入向转发 **32** 条（`egress.py:56-61`）。
- 禁止的东西（`kit/README.md:78-87`）：web search、外部 API；**provider 自带工具也禁**（Gemini 的 Google Search / URL context / code execution，Claude 的 web search / web fetch / code execution，OpenAI 的 `web_search_options` 与 hosted tools）——`google-cloud` 模型在网关和 `kit/model_proxy` 都会被 **HTTP 400** 拒绝。

### 6.3 任务容器

- **完全没有网络**：本地 `kit check` 的 smoke 容器是 `--network none`（`check.py:1455-1466`），评测端同样阻断。
- 本地 kit 的 smoke/mcp 容器：`python:3.12-slim`，`--cpus 2`、`--memory 4g`、`pids=max(4096, 8*CPU)`、`fsize=8GiB`、`/app` tmpfs 1g（`check.py:46-76`）。
- dev 评测里任务容器由任务 `task.toml` 决定：**2–4 CPU、4–12 GB**（`eval/README.md:120`）。
- 任何单文件 ≤ 8 GiB（文件系统 `fsize` 限制）。

### 6.4 为什么"本地能跑、评测挂"

三处常见差异（`kit/README.md:163-168` 明说 `kit check` 不覆盖）：

1. **模型 key 的作用域**：本地 proxy 服务全部 8 个模型，评测端的 key 只对声明的那一个有效（换模型 → 403）。
2. **submodule 的 `.git`**：评测端会带 `.git`（depth 1）检出，kit 不带。
3. **构建缓存**：kit 复用缓存，评测端每次从零构建；依赖没 pin 就可能炸。
4. 另外：评测端会**并发跑很多任务**（每条连接各自的容器），本地 `eval/run.py` 默认按机器资源排。

---

## 7. `kit check` 具体检查什么（10 个阶段）

入口：`uv run python -m kit check ../my-harness [--cancel-test] [--no-smoke] [--keep] [--rebuild] [--env-file ...] [--model-base-url ...]`。
**按顺序执行，遇到第一个 FAIL 就停**（`kit/README.md:116`）。

| # | 阶段 | 检查内容 | 代码 |
|---|---|---|---|
| 1 | `submission.yaml` | 3.2 的全部规则；通过后打印 `name: model, egress host1, host2` | `check.py:972-986` |
| 2 | `model`（本地设置） | **还没构建**就先校验本地模型设置：`google-cloud` 要 `MODEL_PROXY_URL`+`MODEL_PROXY_KEY`；自有模型要对应 key；`--model-base-url` 只能配 `openai-compatible`；值不能带换行/NUL | `check.py:816-867,1003-1009` |
| 3 | `files` | 见下 | `check.py:1046-1207` |
| 4 | `build` | 在 builder 容器跑 `install`，校验并打包 bundle（未变更时复用，`--rebuild` 强制） | `check.py:1021-1043` |
| 5 | `model`（连通性） | 在 serve 镜像里起 `sleep infinity` 容器，用 `python3 -I` 通过 egress 打一次模型，要求回 `OK`（单次调用 150 s，总 330 s） | `check.py:1576-1659` |
| 6 | `listen` | 120 秒内能否 accept WebSocket；打印 `websocket accepted N s after start` | `check.py:1694-1715` |
| 7 | `smoke` | 一个真任务：harness 在 `/app/hello.txt` 写 `HELLO` 并 `done`；**结果必须逐字节等于 `HELLO`**（`content.strip() != want` 即 FAIL） | `check.py:1717-1813` |
| 8 | `mcp` | 任务容器里跑一个 MCP stub（`secret_word` 工具），harness 必须**通过 `http` 请求**拿到词并写 `/app/mcp.txt`；没发 http 请求只 WARN，不会 FAIL | `check.py:95-140,1815-1828` |
| 9 | `cancel` | 仅 `--cancel-test`：第 45 秒取消，要求 10 秒内 `done`；任务提前自然完成会 `skip` | `check.py:1830-1880` |
| 10 | `artifacts` | 按 §8 规则审 `artifacts/`，并检查是否已提交 | `check.py:1209-1302` |

### 7.1 `files` 阶段逐条（`check.py:1046-1207`）

- entry 必须**是它自己 git 仓库的根**；在竞赛仓库 clone 里 / 无 git / 在仓库子目录 → WARN + `unready`（`clone`/`no-git`/`subfolder`）。
- 未提交改动 → WARN + `unready="uncommitted"`。
- `git assume-unchanged`/`skip-worktree` 标记 → WARN（无法判断提交内容）。
- shallow clone → WARN（历史大小测不全）。
- `serve` 里 `{src}/...` 指向的文件不存在 → WARN（可能由 `install` 生成，`listen` 会兜底）。
- `install`/`serve` 指向的脚本有 CRLF → WARN。
- 被 build 排除的目录（artifacts 系）里有文件 → WARN（"left out of the build, as on the server"）。
- **Git LFS 跟踪 → FAIL**。
- submodule 匿名拉取失败 → FAIL/WARN。
- **疑似 API key / 私钥 → FAIL**（正则覆盖 `sk-`、`sk-ant-`、`sk-proj-`、`AIza`、`gh[pousr]_`、`hf_`、`xox[baprs]-`、`-----BEGIN ... PRIVATE KEY-----`；`test/`、`fixtures/`、`vendor/`、`node_modules/`、`site-packages/` 里的只 WARN）。见 `check.py:161-231`。
- 文件读不完（权限/超时 120 s）→ WARN "not scanned for keys"。

> 推论：**`google-cloud` 的 entry 里不该出现任何 key**，包括 `.env`。工作区 `AGENTS.md` 提到的"`.env` 阶段"在本 commit 的 kit 里并不存在；把 key 写进 entry 会被 `files` 阶段当泄漏直接 FAIL。

### 7.2 `ready to submit` 的判定（`check.py:2185-2217`）

```python
ready = report.ok and not report.untested and not report.smoke_skipped and not report.unready
```

即：所有阶段 pass/warn/skip、请求的阶段都真跑了、smoke 没被跳过、没有 `unready` 理由。
`unready` 的 6 种理由：`clone`、`no-git`、`subfolder`、`uncommitted`、`artifacts`、`own-model`（`check.py:475-506`）。

### 7.3 退出码

- `0`：跑到的阶段全过（**即使还没 ready**，比如缺 artifacts）；
- `1`：有 FAIL，或你请求的阶段没跑（如 `--cancel-test`）；
- `130`：被中断。

### 7.4 日志

`~/.cache/plebench/<name>/`（或 `--work`）：`build.log`、`serve.log`、`egress.log`、`relay.log`、各阶段 `serve-mcp.log`/`egress-model.log`、`smoke.json`/`mcp.json`、`check.json`。
密钥在控制台与 `check.json` 里被替换成 `[NAME]`；**原始日志文件里不脱敏**，别把 `~/.cache/plebench` 传给别人（`kit/README.md:139-144`）。

### 7.5 `kit check` 不覆盖什么（`kit/README.md:163-168`）

- key 作用域（本地 proxy 服务 8 个模型）；
- submodule 的 `.git` 差异；
- 构建缓存（评测端从零构建）；
- **dev set 打分与并发**：`kit check` 只跑 smoke + mcp，29 个 dev 任务要用 `eval/run.py`。

---

## 8. `artifacts/`：dev 集的产物

权威文档：`docs/artifacts.md`；校验代码：`kit/artifacts.py`（`review()` 是 server/portal/kit 共用的一份逻辑）。

### 8.1 布局（`docs/artifacts.md:33-48`）

```
artifacts/
  manifest.json            written by the kit: which run produced each task's files (do not edit)
  .gitattributes           written by the kit: git stores these files byte for byte
  .gitignore               written by the kit: git tracks these files even when a parent .gitignore would ignore them
  <task-id>/               one folder per dev task, named by its id in eval/dev_set.yaml
    final_message.md       your harness's final message for the task
    files/<path>           every file your harness created or changed under the task's deliverables,
                           stored at its container path without the leading "/"
    deleted.txt            optional: deliverable files your harness deleted, one container path per line
    trajectory.json        optional: the run's trajectory, kept for inspection and never graded
```

示例（`docs/artifacts.md:56-59`）：

```
artifacts/data-center-site-model/final_message.md
artifacts/data-center-site-model/files/app/files/frontier__DCBim__p6_002/submission/model.ifc
```

### 8.2 "哪些文件会被导出"

只导出：**在 `deliverables` 路径下**、且**被 harness 创建或修改**的普通文件（`docs/artifacts.md:50-55`；实现在 `kit/capture.py`）。

排除：
- 没修改的文件（打分从干净任务容器开始）；
- git 元数据（`.git/`、`.gitignore`、`.gitattributes`、`.gitmodules`）；
- OS 元数据（`.DS_Store`、`._*`、`__MACOSX/`）；
- 仅大小写不同的第二个路径（macOS/Windows checkout 会合并，打印 WARN）。

判"修改"用的是 **ctime**（内核变更时间）：即使你重写回原内容、原 mtime，也算改过（`capture.py:55-60` 注释）。

### 8.3 限额（`docs/artifacts.md:103-111`，常量在 `artifacts.py:85-102`）

| 范围 | 限制 |
|---|---|
| 单个交付物文件 | **95 MiB**（GitHub 拒 >100 MiB） |
| 单个任务目录 | **256 MiB / 2 000 个交付文件** |
| 整个 `artifacts/` | **512 MiB / 10 000 个文件** |
| `final_message.md` | **1 MiB**（超出截断） |
| `deleted.txt` | 256 KiB |
| `trajectory.json` | 32 MiB（超出则被导出器省略） |
| `manifest.json` | 4 MiB（超出忽略并 WARN） |
| 路径 | 深度 ≤64 层、≤1024 字节、单名 ≤255 字节 |

`trajectory.json` 不评分，所以超限时**先丢它**（从大到小）。

### 8.4 `kit artifacts` 到底校验什么

命令：`uv run python -m kit artifacts ../my-harness [--from runs/<run>] [--json]`。
退出码：`0` = 服务器会接受（允许 WARN）；`1` = 会拒绝。

**拒绝（整个 commit 不可用）**（`docs/artifacts.md:124-130`）：

1. 没有可用的 `artifacts/`（含 fallback 名）；
2. 提交了压缩包（`artifacts.zip`、`artifacts.tar.gz`…）而不是目录；
3. Git LFS 跟踪 artifacts；
4. `artifacts/` > 512 MiB 或 > 10 000 文件；
5. **没有任何一个 dev 任务**有可用 artifacts（全挂 → 直接拒绝而不是 0 分）。

**某任务记 0 分（其它任务照常）**（`docs/artifacts.md:132-146`）：

- 该任务目录里有 symlink 或 submodule；
- 单个交付文件 >95 MiB、>2 000 文件、>256 MiB；
- 路径含 `.`/`..` 段、反斜杠、控制字符、非 UTF-8；
- `files/<container path>` 嵌套 >64 层、路径 >1024 字节、文件名 >255 字节；
- 仅大小写不同的两个交付路径；
- 文件是 Git LFS pointer；
- 没东西可评（既无交付文件也无非空 `final_message.md`）；
- 对只读交付文件的 grader，只有 `final_message.md`。

**WARN 但仍评分**（`docs/artifacts.md:150-162`）：

- 某些 dev 任务没有目录（记 0）；
- 读 `final_message.md` 的 grader 却没有/为空；
- 文件落在 `deliverables` 之外（忽略）；
- 任务目录里混入 `.gitignore`/`.gitattributes`/`.gitmodules`（评分时忽略）；
- `artifacts/` 里有不是 dev 任务的目录（忽略，并提示最接近的 task id）；
- 任务目录看起来像原始 Harbor trial 目录（`agent/`、`verifier/`、`result.json`）；
- **`manifest.json` 缺失或与提交文件不匹配**（导出后手改过、dev set 版本不同、harness 版本不同）。

只由 kit 检查的（`docs/artifacts.md:165-177`）：被 git 忽略的文件、`git status` 看不到但内容与对象库不一致、文件不可读、`git status` 失败、artifacts 里有疑似 key。

**fallback 名**（`docs/artifacts.md:185-194`）：`Artifacts/`、`artifact/`、`dev_artifacts/`、`dev-artifacts/`、唯一的 `runs/<run>/artifacts/`、多包一层 wrapper、Harbor trial 名、大小写/下划线变体、`<task-id>/app/files/...`、`init.py` 别名、`final_message.txt` 等。

### 8.5 `manifest.json`（`docs/artifacts.md:207-243`）

```json
{
  "format": 1,
  "dev_set": "sha256 of eval/dev_set.yaml's data",
  "created": "2026-10-10T12:00:00Z",
  "harness": {
    "name": "my-harness",
    "model_api": "google-cloud/claude-opus-5-5",
    "tree": "sha256 of the harness's files",
    "files": {"serve.py": "100644 git blob id"},
    "commit": "git commit of the run, or null",
    "dirty": false
  },
  "tasks": {
    "data-center-site-model": {
      "run": "my-harness-20261010-120000",
      "trial": "data-center-site-model__a1B2c3D",
      "ended": "ok",
      "reward": 0.83,
      "tree": "the harness digest of that run",
      "final_message": "git blob id",
      "files": {"app/files/.../model.ifc": "git blob id"},
      "deleted": [],
      "skipped": []
    }
  }
}
```

- `harness.tree` = 按 git 记录（mode + blob + path）算的 SHA-256，**排除 `.env` 与 artifacts 目录**。
  提交的 harness 与它一致 ⇒ 证明这些 artifacts 是这份代码跑出来的。
- 只有文档（`README`/`LICENSE`/`CHANGELOG`）变化时，给 note（`docs-only`）而不是 warning；其它文件变了 → `"the harness changed after these artifacts were made (...)"`，评测端的 reproduction run 会实测你有没有掉分（`artifacts.py:2900-2930`）。
- `tasks.<id>.reward` 只是本地参考，不参与排名。

> **最容易犯的错**：改完 harness 忘了重跑 dev set，或只改了 README 之外的注释/配置就提交。任何非文档文件变化都要重跑并 `git add -A` 一起提交。

### 8.6 artifacts 是怎么被打分的（`docs/artifacts.md:88-99`）

对每个 dev 任务：

1. 起一个**全新任务容器**；
2. 按 `deleted.txt` 删文件；
3. 把 `files/` 按原容器路径拷进去；
4. 写入 `final_message.md`；
5. 跑该任务的 grader。

**harness 不运行**。dev 分 = 29 个任务的平均；没有可用 artifacts 的任务记 0。

---

## 9. 一次 dev 评测怎么跑（`eval/run.py`）

### 9.1 前置

- Docker（本地 daemon，**不支持远程 `DOCKER_HOST`**），约 10 GB 空闲磁盘；
- `uv`；Harbor **0.20.0** + `websockets`：`uv tool install harbor==0.20.0 --with websockets`（版本在 `eval/run.py:135-137` 硬校验）；
- 任务来自 **gated** HF 数据集 `google/physical-engineerings-last-exam`（`eval/dev_set.yaml:20-21`）：
  要在数据集页面申请访问 + `HF_TOKEN`（read token），或 `--tasks-dir` 指本地已下载目录；
- 模型访问：
  - `google-cloud/<id>`：`MODEL_PROXY_URL` + `MODEL_PROXY_KEY`（本地 LiteLLM proxy，`kit/model_proxy/start.sh`，计费到你的 GCP 项目）；
  - 自有模型：对应 key（`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`GEMINI_API_KEY`/`MODEL_API_KEY`）；
- judge（`rubric` / `checks-judge` 任务需要），按优先级取：
  1. `JUDGE_BASE_URL` + `JUDGE_API_KEY`（任何 OpenAI 兼容端点；只给 `JUDGE_API_KEY` 不给 `JUDGE_BASE_URL` 会**直接停**）；
  2. `MODEL_PROXY_URL` + `MODEL_PROXY_KEY`；
  3. entry 自己的模型与 key（本地自有模型时）。
  `JUDGE_MODEL` 默认 `claude-opus-5-5`（排行榜用的就是它）。走 model_proxy 时请求 `<model>-judge`，用标准推理档而不是最高档。

以上都放 entry **之外**的 env 文件（如仓库根的 `local.env`，`.gitignore` 已忽略 `*.env`），用 `--env-file` 传。

### 9.2 命令

```bash
# 全量 29 个 dev 任务（最常用）
uv run python eval/run.py ../my-harness --env-file local.env

# 只跑指定任务
uv run python eval/run.py ../my-harness --env-file local.env --tasks data-center-site-model,engineers-estimate-check

# 用已在跑的服务调试（不构建）
uv run python -m kit up ../my-harness        # 打印 ws:// 地址
uv run python eval/run.py --url ws://127.0.0.1:<port> --name my-harness --tasks <id>

# judge 挂了：只重判，不重跑 harness
uv run python eval/run.py --regrade runs/<name>-<time> --env-file local.env

# 只给已提交的 artifacts 打分（不跑 harness）
uv run python eval/run.py --artifacts ../my-harness
```

### 9.3 参数全表（`eval/run.py:3308-3403`）

| 参数 | 含义 |
|---|---|
| `submission`（位置） | 含 `submission.yaml` 的目录（或该文件） |
| `--url` | 用已在跑的服务，不构建（配 `--name`、`--model`） |
| `--name` / `--model` | `--url` 时结果里显示的名字/模型 |
| `--tasks` | 逗号分隔的 dev task id |
| `--trials` | 同时跑（并评分）几个任务；默认按 Docker CPU/内存自动，`--url` 时 1 |
| `--attempts` | 评测端故障时每任务最多跑几次（默认 3） |
| `--env-file` | `NAME=value` 行：`MODEL_PROXY_*`、自有模型 key、`JUDGE_*`（**绝不进 entry**） |
| `--model-base-url` | 本地自有模型：替代 `model_base_url` |
| `--out` | 指定运行目录（默认 `runs/<name>-<time>/`，必须是新/空目录且在 entry 之外） |
| `--rebuild` | 强制重建（否则复用缓存） |
| `--tasks-dir` | 用本地 dev 任务目录，替代数据集固定版本 |
| `--regrade RUN_DIR` | 只重判某次运行里 judge 失败的任务（只允许配 `--env-file`/`--tasks-dir`） |
| `--no-export` | 不写 entry 的 `artifacts/`（run 自己的 `artifacts/` 仍写） |
| `--artifacts DIR` | 给 entry 的 `artifacts/` 打分（harness 不运行，rubric 仍调 judge） |

### 9.4 输出

`runs/<name>-<time>/`（`eval/README.md:150-164`）：

```
results.json                  总体与每任务分数、状态、token 用量
export.json, artifacts/       导出记录与带 manifest.json 的 artifacts（--artifacts 时没有）
tasks/                        每个任务的一份副本（让 judge verifier 能连通）
attempt1/                     重试会写到 attempt2/、attempt3/
  harbor.log                  Harbor 控制台输出
  job.yaml, task-limits.yaml  Harbor job 配置与容器资源限制
  jobs/dev/<task>__<id>/      一个 trial
    result.json               Harbor 的 trial 记录
    agent/                    <name>.txt（协议转录）、serve.log、egress.log、trajectory.json
    artifacts/                Harbor 交给 verifier 的交付物
    deliverables/             kit 拷出的交付物（--regrade 用）
    verifier/                 test-stdout.txt、grade.json、grade.log 或 GRADER_FAILED.txt
attempt1-regrade1/            只重判 judge 的 replay
```

控制台表格（`eval/run.py:2584-2658`）：

```
task                        score  status
data-center-site-model      0.830  ok (3120 s)
...
my-harness (google-cloud/claude-opus-5-5): 0.612 over 29 task(s)
results: runs/my-harness-20261113-001234/results.json
```

- 每行是 `reward / reward_scale` 归一化到 0–1 的分数、状态、harness 耗时；
- 有任务没测到会 `warning: N task(s) have no measurement ... They count 0 above`；
- 退出码：`0` 成功；`1` 有任务没测到或 artifacts 不可用；`130` 被中断。

### 9.5 trial 结果怎么处理（`eval/README.md:188-194`）

| trial 结果 | `eval/run.py` 的动作 |
|---|---|
| harness 正常结束（`done`/`error`/`AgentTimeoutError`/`NonZeroAgentExitCodeError`） | 计分：容器里有什么交付物就评什么 |
| 模型还没产出任何输出就崩了（`output_tokens` 为 0，或轨迹里无活动） | 视为 not started：重跑，最多 `--attempts` 次；一直起不来记 0（例外：context 溢出或 provider 限额导致的结束，照常评分） |
| `rubric`/`checks-judge` grader 失败 | 用快照的交付物在**新容器**里重判最多 3 次，不重跑 harness；3 次都失败记 unmeasured |
| 其它评测端故障（checks grader 失败、缺 reward、Harbor/Docker/网络错误，或 harness 被端点/网络切断） | 不测量：整个 trial 重跑，最多 `--attempts` 次 |
| `task.toml` 坏了或任务镜像没构建出来 | 不测量、不重跑 |

### 9.6 时间与开销（`eval/README.md:118-140`）

- 每任务：6 小时上限（29 个逐跑 = 最多 174 小时，大多数 harness 远早结束）。
- 12 个 rubric 任务跑在"world"环境里（邮件/文档/表格/CAD 的 MCP），镜像约 **17 GB**，首次要预构建（每个至少给 1 小时）；
- 资源：任务容器 2–4 CPU / 4–12 GB，**再加** serve+egress 的 2 CPU / 4.25 GB；默认按"最大任务"配槽位（全量 dev set 是 6 CPU + 16.25 GB/槽）。
- macOS/Windows 的 Docker Desktop 建议 ≥6 CPU、**16.25 GB** 内存。

---

## 10. 硬性限制总表（背下来）

### 10.1 时间

| 项 | 值 | 出处 |
|---|---|---|
| 服务端 accept WebSocket | **120 s** | `protocol.py:30` |
| 单任务 agent 时限 | **6 h = 21 600 s**（dev 任务 `time_limit_min: 360`） | `spec.py:31-32`；`dev_set.yaml` |
| `exec` 默认 / 上限 | 120 s / **600 s**，且夹到 `剩余-2 s` | `protocol.py:35-36`；`docs/protocol.md:69-72` |
| `http` 默认 / 上限 | 120 s / 600 s | `docs/protocol.md:84-85` |
| `cancel` 后宽限 | **10 s** | `protocol.py:34` |
| 构建 `install` | **30 min** | `build.py:54` |
| egress 空闲断连 | **1800 s** | `egress.py:65` |
| 模型探针（kit） | 单次 150 s，总 330 s | `check.py:152-154` |

### 10.2 尺寸

| 项 | 值 |
|---|---|
| WebSocket 帧 | **16 MiB**（超了直接终止任务） |
| `exec` 输出 | 100 KB（保留头尾，约 2/3 + 1/3） |
| `read_file` / `write_file` / `http` body | 各 **10 MB** |
| 评测端单次 `docker exec` 读回 | 32 MiB（超了命令被停） |
| `submission.yaml` | 16 KiB |
| 仓库（含 `.git` 历史） | **1 GiB** |
| bundle | 2 GiB 压缩 / 8 GiB 解压 / ≤200 000 成员 |
| build 期间单文件 | 8 GiB |
| 文件名 / symlink 目标 | 1024 字符 |
| 任务容器单文件 | 8 GiB |
| 交付物单文件 | 95 MiB |
| `artifacts/` | 512 MiB / 10 000 文件 |

### 10.3 内存 / CPU / 进程

| 容器 | CPU | 内存 | 进程 |
|---|---|---|---|
| serve | 2 | 4 GB | 512（`stack.py:77`） |
| egress 代理 | — | 256 MiB | 256 |
| 任务（dev 实际） | 2–4 | 4–12 GB | 4096+（`max(4096, 8*CPU)`） |

> serve 容器根文件系统**只读**，只有 `/tmp` 与 `$HOME`（各 1 GB tmpfs）可写。

### 10.4 协议 / 并发

| 项 | 值 |
|---|---|
| 同时在跑的请求 | **8**（第 9 个会让评测端暂停读帧） |
| malformed 帧 | **20**（超了整场结束） |
| 请求 `id` | 非空、≤128 字符、合法 UTF-8 |
| `log` 单条 | ≤20 000 字符 |
| `usage` 记录模型数 | ≤16 |
| egress 出向连接 | 256；入向转发 32 |

### 10.5 网络

| 谁 | 能到哪 |
|---|---|
| build 容器 | 全互联网（30 分钟，装依赖用） |
| serve 容器 | **只有**声明模型的 host:port（TLS + SNI 校验）；无直连 DNS |
| 任务容器 | **完全无网络** |
| verifier 容器 | 只有 judge host（rubric/checks-judge） |

---

## 11. 评分机制（决定了 harness 该优化什么）

### 11.1 单任务分（`docs/rules.md:23-37`）

- 每个任务 0–1 分：`reward / reward_max`。
- "约束 + 设计目标"类任务（dev set 里 grader 含 `tests/paper_score.py` 的）按交付项算：

  ```
  s = 0.4 p + 0.6 G O'
  ```

  - `p`：满足的约束比例；
  - `G`：**所有硬约束（安全/可行性）都满足为 1，否则 0** —— 破一条硬约束，该项最高只有 0.4；
  - `O' = clip((O - O_naive) / (1 - O_naive), 0, 1)`：设计目标相对"未优化的合规基线"的改进度（`O=1` 只在任务的认证边界；成本类任务用 `成本下界/实际成本`）。
- 一次运行的分 = 所有任务的平均；**任何没有结果的任务记 0**。

> 这条公式对 harness 的直接含义：**先保证"硬约束全满足"（G=1），再谈优化目标**。一个安全违规的漂亮设计，分数还不如一个保守但合规的设计。

### 11.2 dev 分与 test 分

- **dev 分**：提交的 `artifacts/` 被各 dev 任务 grader 打分（无可用 artifacts → 该任务 0）。
- **test 分**：隐藏 56 个任务，跑 **3 次取平均**（工作区 `AGENTS.md` §9；仓库 `docs/rules.md:13-17` 说 test set 56 个、评测方式"将公布"）。
- 提交时评测端顺序做四件事：build + smoke → 用 dev grader 打 committed artifacts → 在少数随机 dev 任务上**实跑**做 reproduction 校验 → 跑隐藏 test set。分数明显低于 committed artifacts 的会被标记人工复核。

### 11.3 哪些算"你的失败"

- 崩溃、协议违规、撞 6 小时上限、对模型的非法请求（如 400）；
- 容器资源被你打爆；
- 失败前已写入的交付物**仍然评分**（所以"早写交付物"永远是对的）。

---

## 12. 写出高质量 harness 的关键点

下面每一条都能在代码/文档里找到依据，按重要性排序。

### A. 协议层：先把"不扣分"做对（最容易丢的分）

1. **永远发 `done`**。成功、失败、被 cancel、模型 400、上下文爆了——都要发。发 `error` 也行，但 `done` + 摘要更值钱（rubric 会读 `final_message`）。
2. **`ping_interval=None`**（或 `ping_timeout` 大于最长命令）。否则 8 个 in-flight 卡住读帧时，ping 超时会把连接弄断。
3. **120 秒内 bind**：`serve` 命令里不要做重活（不要 import 时下模型、不要装包）。启动路径尽量短。
4. **并发收敛**：收尾前先 `await` 掉所有未完成请求，把 in-flight 降到 0 再发 `done`（否则 `done` 排在第 9 个后面）。
5. **`id` 唯一且短**（≤128）；结果按 `id` 匹配；不要假设结果按发送顺序回来。
6. **不要发 >16 MiB 的帧**；`write_file` 单次 ≤10 MB，大文件用 `append` 分块（参考 `WRITE_CHUNK = 4 MiB`）。
7. **处理 `cancel`**：置一个全局 deadline，模型调用用 `asyncio.wait_for(..., remaining - WRAP_UP)` 包住；收到 cancel 立即返回已有摘要。
8. **本地错误不要变成 malformed**：你的请求字段类型错了（`timeout_sec: "120"` 字符串、`headers` 里有非字符串值）都会累计 malformed 计数。

### B. 任务层：PLE 的任务长什么样

- 输入在 `workdir`（多为 `/app` 或 `/filesystem`），通常是某个"项目目录"：图纸、IFC、表格、规范、邮件。
- 交付物路径**每个任务都不一样**（见附录 A），三类典型：
  - `/app/files/<world>/output/solution.py`（MEP 路由，`checks`）；
  - `/filesystem/<world>/output/submission.zip`（多数 checks/checks-judge，要求 zip 里有指定结构）；
  - `/filesystem/` + `/app/output/` + `/app/tools/.apps_data/`（rubric 的"world"任务：要真的在邮件/文档/CAD 应用里操作，状态存在 app 数据库里）。
- **只读 `deliverables` 声明的东西**：写在别处的分析、脚本、日志都不评分。
- 格式优先级（`docs/protocol.md:134-139`）：纯文本（`.txt/.md/.csv/.json/.yaml/.yml/.py/.html/.xml`）、Office（`.docx/.xlsx/.pptx`）、`.pdf`；`.odt/.ods/.odp` 之类只按文件名+大小列出 ⇒ **能选格式时避开它们**。
- 流程建议（工作区 `AGENTS.md` §8 总结的高分共性；仓库 `docs/` 里没有这段，但和 `docs/protocol.md` 的设计一致）：
  > "read the full instruction carefully before acting, inspect the starting files in `workdir`, work in small verified steps, write an initial deliverable early and refine it, and budget time so they always send `done` before `agent_timeout_sec`."

### C. 时间预算：6 小时不是"够用"，是"要分配"

`plan-verify` 的做法（`sample_submissions/plan-verify/README.md`）值得抄：

| 步骤 | 收尾点 |
|---|---|
| Planner | 用掉总时间 10% |
| Worker | 剩 25% 时收尾 |
| Verifier | 剩 12.5% |
| Fix round | 剩 4 分钟 |

外加：任何一步若在"它的收尾点前不足 2 分钟"，**直接跳过**（避免开一个来不及收尾的回合）。

`minimal` 的做法：
- `WRAP_UP_SEC = 240`：剩 4 分钟时插入一条 "Time is almost up. Stop and reply with your summary."，并把 `tool_choice` 设为 `"none"`；
- 每轮把 `[N minutes left]` 追加到最后一条工具结果里，让模型自己感知时间。

### D. 模型层：省钱、稳态、别 400

1. **用 `OPENAI_BASE_URL`/`OPENAI_API_KEY` + `MODEL_NAME`**，provider 无关（`docs/models.md:68-80`）。
2. **推理档**：默认最高（Claude/GLM `max`，Gemini/gpt-oss/Kimi/MiniMax `high`）。要省钱/提速就显式传 `reasoning_effort`。Gemini 4 Argon 只接受 `low/medium/high`，给 `thinkingBudget` 会 400；`max_tokens` ≤65 536。
3. **Claude 的坑**：Opus 5.5 **拒绝** `temperature`/`top_p`/`top_k`/`thinking.budget_tokens`，传了就是 400；`max_tokens` 给足（32 000 是好默认）。
4. **Claude 缓存**：不标 `cache_control` 就每轮全价重算输入 —— 要把 `{"type":"ephemeral"}` 打在**system prompt** 和**最新一条消息**上（Claude 最多 4 个）。`minimal/serve.py:marked()` 是标准写法。成本可能差好几倍。
5. **重试策略**（`AGENTS.md` §3 / `docs/models.md:150`）：429、超时、5xx 用指数退避；**400 不重试**（重试只会继续 400，还烧钱）。区分"context 超限的 400"——那种要压缩上下文后重试。
6. **流式**：长推理可能静默几分钟；egress 1800 秒空闲才断，但自己设 timeout 时别低于实际需要（`minimal` 用 `timeout=1800`）。
7. **上下文管理**：
   - 工具输出立刻 head/tail 截断（`RESULT_CHARS = 20_000`）；
   - 超阈值（`custom-openai` 的 `COMPACT_AT_TOKENS` 默认 150 000）先裁旧工具结果，再让模型把旧轮次总结成笔记；
   - **必须原样回传 provider 特有字段**：Gemini 的 thought signature、Claude 的 signed thinking blocks。丢了就 400/工具循环断裂（`minimal/serve.py` 里 `message.model_dump(exclude_none=True)` 的注释就是讲这个）。
8. **`finish_reason == "length"`** 要接住：把已生成内容拼起来，发 "continue" 让它接着写（`minimal` 有 `MAX_NUDGES = 3`）。
9. **`usage` 一定要发**：既是成本观测，也避免被误判 not started。

### E. 工具层：给模型的工具面要"少而稳"

`minimal` 只给 3 个工具（`bash`/`read_file`/`write_file`），已经能拿分；`custom-openai` 加 `edit_file` + MCP 两段式发现。经验：

- 工具**少**比多好（每个工具的 schema 都占 prompt，且模型容易选错）。
- `bash` 的 timeout 要显式给，并且**小于剩余时间**；不要让一条命令吃掉 600 秒。
- 工具失败要**返回字符串**而不是抛异常（`run_tool` 的 `try/except` 返回 `f"error: ..."`），否则整个循环挂掉。
- 工具参数用 `json.loads` 后**重新 dump 成规范 JSON** 再回填，严格端点（Gemini/部分 vLLM）会校验。
- 长输出在**返回给模型前**就截断，别等上下文爆了再处理。

### F. MCP：会用到就别只会 `tools/list`

- 检测 `mcp_servers` 是否为空；为空的任务不要浪费时间。
- 握手顺序：`initialize` → `notifications/initialized` → `tools/list` → `tools/call`；带 `Accept: application/json, text/event-stream`；复用 `Mcp-Session-Id`；后续带 `MCP-Protocol-Version`；SSE 和 JSON 两种回复都要能解析。
- **目录可能几百个工具 / ~500 KB**：`custom-openai` 的策略是 <128 个工具且 schema <40 000 字符就直接摊平，否则给 `mcp_find` + `mcp_call` 两段式。
- `kit check` 的 `mcp` 阶段只 WARN，但隐藏任务**真的需要** MCP：dev set 里 14 个 `rubric` 任务中有 12 个跑在 world 环境（邮件/文档/表格/CAD 都通过 MCP 暴露），必须真的在应用里操作才能拿到分（`eval/README.md:126-134`）。这是 dev 分与 test 分的重要差异点。

### G. 容器资源：4 GB / 2 CPU 下别把自己搞死

- 不要 `pip install` 在运行时（无网）；依赖全部在 `install` 阶段装进 `/opt/harness`。
- 大计算（有限元、IFC 生成、大表格）要分块 + 及时清理临时文件；`/tmp` 和 `$HOME` 各只有 1 GB。
- 不要起常驻后台进程（尤其是 `env -i` 启动的），`done` 前自己杀掉。
- 读大文件用 `exec` + `head`/`python` 分段，不要硬 `read_file` 10 MB 就以为拿到全文（会 `truncated`）。
- 一条命令的输出别乱喷（100 KB 截断，头尾保留）；调试信息走 `log` 消息。

### H. 合规（会被取消资格的红线，`docs/rules.md:56-72`）

- 不 hardcode 任务答案、不按任务指纹分支（dev 任务也不行）；
- 不读 dev-set grader / 参考文件 / 自己的 `artifacts/`（磁盘、git 历史、网络都算）；
- 不绕过 egress、不连非模型 host、不用 provider 自带搜索工具、不 tamper 评测容器；
- `model_api` 必须与实际调用的模型一致；
- 不保留/分享任务内容（prompt 和文件都会发到你的模型端点，注意端点日志策略）。

### I. 交付纪律

- 每次改 harness → 重跑 dev set → `git add -A` → 提交。`manifest.json` 会哈希 harness 文件，改一个非文档文件就对不上。
- `artifacts/` **永不手改**；单个任务可以 `--tasks <id>` 重跑（其余目录保留）。
- 提交前跑三件套：`python -m kit check`（要看到 `ready to submit`）、`python eval/run.py`、`python -m kit artifacts`。

---

## 13. 本地开发闭环与常见坑

### 13.1 推荐顺序

```bash
# 0) 一次性
uv tool install harbor==0.20.0 --with websockets
export HF_TOKEN=...                      # gated 数据集
gcloud auth application-default login
export VERTEXAI_PROJECT=... MODEL_PROXY_KEY=$(openssl rand -hex 24)
kit/model_proxy/start.sh &               # localhost:4000，服务 8 个模型
export MODEL_PROXY_URL=http://localhost:4000

# 1) 只验 submission.yaml（不需要 Docker，秒级）
uv run python -m kit validate ../my-harness

# 2) 构建 + 模型连通 + listen + smoke + mcp + cancel
uv run python -m kit check ../my-harness --cancel-test

# 3) 全量 dev set（生成 artifacts/）
uv run python eval/run.py ../my-harness --env-file local.env

# 4) 审 artifacts + 确认已提交
uv run python -m kit artifacts ../my-harness
git -C ../my-harness add -A && git -C ../my-harness commit -m "harness and dev artifacts"

# 5) 最终确认
uv run python -m kit check ../my-harness      # 等 "ready to submit"
```

### 13.2 症状 → 原因对照（`AGENTS.md` §10 的表 + 代码印证）

| 症状 | 原因 / 修法 |
|---|---|
| `refused: has no submission.yaml at its root` | 文件不在仓库根，或没 push 到提交的 SHA |
| `smoke failed: the server never listened` | 绑了 `127.0.0.1` 而不是 `0.0.0.0`；或 120 秒内没起来；或依赖装在 `/opt/harness` 之外 |
| `connection refused / 403 from a host` | serve 容器只能到模型端点；去掉别的 host 调用，确认 HTTP 客户端走了 `HTTPS_PROXY` |
| `401 from the model` | key 变量名/值错了（`google-cloud` 不需要 key，是评测端注入的） |
| `socket closed` / `harness:AgentTimeoutError` | 没在 `agent_timeout_sec` 前发 `done`；长调用要 streaming + 时间预算 |
| 分数低但跑完了 | 交付物路径/文件名不对；`final_message` 没写清；格式是 `.odt` 之类不被提取的 |
| `the harness changed after these artifacts were made` | 改了代码没重跑 dev set |
| `artifacts/` 某任务 0 分 | 跑 `python -m kit artifacts` 看该任务的原因，`--tasks <id>` 重跑 |
| Docker 相关 `InfraError` | 本地 Docker 资源不足（macOS 要给 ≥6 CPU / 16.25 GB）；或远程 `DOCKER_HOST`（不支持） |

### 13.3 这个工作区的两个现实

1. **本机 `eval.yaml` 显示 HF 数据集受限**：
   `Access to dataset google/physical-engineerings-last-exam is restricted. ... Please log in.`
   ⇒ 没 `HF_TOKEN` / 没在数据集页面通过申请，`eval/run.py` 下不了任务。`kit check` 不受影响（它只用 smoke/mcp 的假任务）。
2. **`ple/` 是只读参考仓库**：本手册写在它的**上一级**（entry 仓库工作区）。注意 entry 仓库根就是 `/Users/a0000/.dsh/work/ple`，`NOTES-kit.md` 若留着会变成 untracked 文件，进而在 `kit check` 报"not committed"、若被提交还会改 `manifest` 的 harness digest。**提交前把它移出 entry，或写进 `.git/info/exclude`（本地生效、不进 commit）**：
   ```bash
   echo NOTES-kit.md >> /Users/a0000/.dsh/work/ple/.git/info/exclude
   ```

---

## 14. 文档 / 代码不一致清单（以代码为准）

| 项 | 工作区 `AGENTS.md` / 旧文档 | 本 commit 的代码与 `docs/` |
|---|---|---|
| google-cloud 模型数 | 13 个（含 qwen3-coder/kimi-k2/minimax-m2/deepseek-v3.2/…） | **8 个**（`kit/models.py:27-36`；`docs/models.md:15` "these eight models"） |
| `kit check` 阶段 | 有 `.env` 阶段 | **没有**；key 不进 entry，`files` 阶段扫到疑似 key 直接 FAIL |
| 日志目录 | `~/.cache/pele/<name>/` | `~/.cache/plebench/<name>/`（`stack.py:121`、`kit/README.md:139`） |
| 失败重试 | "up to three attempts" | `--attempts` 默认 3，per task |
| test set | 56 个隐藏任务、跑 3 次取均值 | `docs/rules.md:13-17` 只写"56 个，评测方式将公布"；3 次均值来自 `AGENTS.md` |

> 结论：**模型 id、阶段名、路径、限额一律以 `kit/*.py` + `docs/*.md` 为准**，`AGENTS.md` 当流程建议看。

---

## 15. 附录

### A. 29 个 dev 任务（`eval/dev_set.yaml`，全部 `time_limit_min: 360`）

| task id | 领域 | grader | final_message | deliverables |
|---|---|---|---|---|
| data-hall-mep-routing | 机械 | checks | 否 | `/app/files/dc_model__hall_seed000_d000__mep_routing/output/solution.py` |
| fire-test-validation-memo | 机械 | rubric | 是 | `/app/files/` |
| data-hall-mep-routing-offset | 机械 | checks | 否 | `/app/files/dc_model__hall_seed005_d100__mep_routing/output/solution.py` |
| eor-review-corrections | 结构 | checks-judge | 否 | `/filesystem/dc_structural__eor_revision__blind/output/submission.zip`（+ `/filesystem/filesystem/...`） |
| foundation-strategy | 土木 | checks-judge | 否 | `/filesystem/dc_structural__foundation_strategy__blind/output/submission.zip`（+ 同上双路径） |
| landscape-sheet-revision | 土木 | rubric | 是 | `/app/files/` |
| data-center-site-model | 土木 | checks | 否 | `/app/files/frontier__DCBim__p6_002/submission/model.ifc` |
| engineers-estimate-check | 土木 | rubric | 是 | `/filesystem/`, `/app/output/`, `/app/tools/.apps_data/` |
| footing-settlement-verification | 土木 | rubric | 是 | 同上三路径 |
| solar-supplemental-review | 电气 | rubric | 是 | 同上三路径 |
| solar-witness-test-package | 电气 | rubric | 是 | 同上三路径 |
| frame-extension-method | 土木 | checks | 否 | `/filesystem/field_eng__constructability_4d__blind/output/submission.zip`（+ 双路径） |
| fire-solve-convergence | 机械 | rubric | 是 | `/filesystem/`, `/app/output/`, `/app/tools/.apps_data/` |
| les-mesh-resolution | 机械 | rubric | 是 | 同上 |
| mv-conveyance-study | 电气 | checks | 否 | `/filesystem/final_batch4__electrical_task12_conveyance_study__blind/output/submission.zip` |
| data-hall-device-takeoff | 电气 | checks | 否 | `/filesystem/final_batch4__electrical_task17_ec_0205_d100__blind/output/submission.zip` |
| division-26-specifications | 电气 | checks | 否 | `/filesystem/final_batch4__electrical_task8_electrical_specs__blind/output/submission.zip` |
| heat-rejection-life-cycle-cost | 机械 | checks | 否 | `/filesystem/final_batch4__mechanical_task3_ct_heat_lcc__blind/output/submission.zip` |
| cooling-water-flush-plan | 机械 | checks | 否 | `/filesystem/final_batch4__mechanical_task5_pcw_flush_mop__blind/output/submission.zip` |
| wind-diaphragm-shear | 结构 | checks | 否 | `/filesystem/final_batch4__structural_task5_wind_diaphragm_shear__blind/output/submission.zip` |
| seismic-diaphragm-shear | 结构 | checks | 否 | `/filesystem/final_batch4__structural_task6_seismic_diaphragm_shear__blind/output/submission.zip` |
| roof-deck-design | 结构 | checks | 否 | `/filesystem/final_batch4__structural_task7_roof_deck_design__blind/output/submission.zip` |
| fill-compaction-acceptance | 土木 | rubric | 是 | `/filesystem/`, `/app/output/`, `/app/tools/.apps_data/` |
| shoring-design-verification | 土木 | rubric | 是 | 同上 |
| emergency-spillway-check | 土木 | rubric | 是 | 同上 |
| pre-application-capacity-response | 电气 | rubric | 是 | 同上 |
| permission-to-operate-memo | 电气 | rubric | 是 | 同上 |
| backfeed-energization-reply | 电气 | rubric | 是 | 同上 |
| lv-cable-sizing | 电气 | checks-judge | 否 | `/filesystem/electrical__lv_cable_sizing__blind/output/submission.zip`（+ 双路径） |

统计：`checks` 12、`rubric` 14、`checks-judge` 3；14 个会读 `final_message`。

> 双路径（`/filesystem/...` + `/filesystem/filesystem/...`）与三路径（`/filesystem/` + `/app/output/` + `/app/tools/.apps_data/`）是任务镜像本身的布局差异，**两条都写**才稳（grader 只读它自己那套）。

### B. 常量速查（可以直接抄进代码）

```python
# kit/protocol.py:29-49
PORT = 8765                # 本地 kit 的默认端口（评测端另给 $PORT）
START_SEC = 120            # accept 超时
MAX_FRAME_BYTES = 16 << 20
MAX_MALFORMED = 20
MAX_IN_FLIGHT = 8
CANCEL_GRACE_SEC = 10
DEFAULT_EXEC_TIMEOUT_SEC = 120
EXEC_TIMEOUT_SEC = 600
MAX_OUTPUT_BYTES = 100_000
MAX_READ_BYTES = 10_000_000
MAX_HTTP_BYTES = 10_000_000
MAX_WRITE_BYTES = 10_000_000
MAX_EXEC_READ_BYTES = 32 << 20
MAX_ID_CHARS = 128
MAX_LOG_CHARS = 20_000
MAX_MODELS = 16
MAX_COUNT = 2**53 - 1

# kit/spec.py:32
AGENT_TIMEOUT_SEC = 6 * 3600

# kit/stack.py:77-78
CPUS, MEMORY, PIDS = "2", "4g", "512"
EGRESS_MEMORY = "256m"

# kit/egress.py:54-65
IDLE_SEC = 1800
MAX_CONNECTIONS = 256
MAX_FORWARDS = 32

# kit/build.py:54,64-67,81
BUILD_TIMEOUT_SEC = 1800
MAX_SOURCE_BYTES = 1 << 30
MAX_BUNDLE_BYTES = 2 << 30
MAX_UNPACKED_BYTES = 8 << 30
MAX_FILE_BYTES = 8 << 30

# kit/artifacts.py:85-102
MAX_FILE_BYTES = 95 << 20        # 单交付物
MAX_TASK_BYTES = 256 << 20
MAX_TASK_FILES = 2000
MAX_TOTAL_BYTES = 512 << 20
MAX_TOTAL_FILES = 10_000
MAX_MESSAGE_BYTES = 1 << 20
MAX_TRAJECTORY_BYTES = 32 << 20
MAX_MANIFEST_BYTES = 4 << 20
MAX_DELETED_BYTES = 256 << 10
MAX_PATH_FOLDERS = 64
MAX_PATH_BYTES = 1024
MAX_NAME_BYTES = 255
```

### C. 文件地图（想继续读代码看这里）

| 文件 | 作用 | 值得先读 |
|---|---|---|
| `kit/README.md` | 提交物、容器、限额、`kit check` 阶段、不覆盖项 | 全文（168 行） |
| `docs/protocol.md` | 协议 v1 权威说明 + 最小服务端示例 | 全文（216 行） |
| `docs/artifacts.md` | artifacts 布局、限额、拒绝/警告、fallback、manifest | 全文（243 行） |
| `docs/rules.md` | 评分公式、责任归属、公平竞赛 | 全文（74 行） |
| `docs/models.md` | 8 个模型、环境变量、调用建议 | 1–90 行 |
| `kit/spec.py` | `submission.yaml` 解析与校验 | 全文（370 行） |
| `kit/protocol.py` | 帧校验、限额、MCP URL 白名单 | 31–49、374–549 |
| `kit/check.py` | `kit check` 十阶段 | 902–1330、1490–1880、2116–2220 |
| `kit/artifacts.py` | artifacts 审查（server/portal/kit 共用） | 45–160、1418–1540、3035–3369 |
| `kit/relay.py` | 评测端协议实现（本地与 Harbor 共用） | 45–100、761–800、988–1130 |
| `kit/egress.py` | 出口代理 | 14–100 |
| `kit/stack.py` | serve/egress 容器编排 | 47–100、860–975 |
| `kit/build.py` | 构建、打包、限额 | 14–100 |
| `kit/harbor_agent.py` | Harbor 侧 agent 适配（含 capture 调用） | 1–140 |
| `kit/harbor_docker.py` | Harbor Docker 环境（egress sidecar 头超时） | 全文（59 行） |
| `kit/capture.py` | 交付物基线/差分/拷贝 | 14–60、240–360 |
| `kit/export.py` | 写 `artifacts/`（含 deleted.txt、manifest） | 180–300 |
| `kit/replay_agent.py` | `--artifacts` 离线重放评分 | 14–40 |
| `eval/run.py` | dev 评测主程序 | 15–140（docstring）、2340–2450、2580–2660、3294–3420 |
| `eval/dev_set.yaml` | 29 任务清单（deliverables/time/grading） | 全文 |
| `sample_submissions/minimal/serve.py` | 最小可用 harness（<250 行） | 全文 —— **最好的起点** |
| `sample_submissions/taskbridge.py` | 协议实现（WebSocket/Relay/MCPClient） | 14–120、`run()`、`Session` |
| `sample_submissions/plan-verify/` | 计划-执行-验证三角色 + 时间预算 | `README.md`、`serve.py` |
| `sample_submissions/custom-openai/` | 全功能参考（MCP、流式、压缩、重试） | `README.md`、`serve.py` |

### D. 提交前 15 条自检

1. `submission.yaml` 在仓库根，`name`/`model_api` 合法，`serve` 能被 `shlex.split`；
2. `model_api` 是 8 个 `google-cloud/<id>` 之一（要提交的话）；
3. entry 里**没有**任何 key / 私钥 / `.env`；
4. 没有 Git LFS、没有 CRLF 脚本、没有 submodule（或公开 https）；
5. 所有文件已提交，仓库根 = entry 根；
6. `install` 把所有东西装进 `/opt/harness`，venv 用 `uv venv --python 3.12 /opt/harness/venv`；
7. `serve` 用绝对路径，120 秒内 bind `0.0.0.0:$PORT`；
8. 服务器 `ping_interval=None`；
9. 每个任务路径都发 `done`；`cancel` 后 10 秒内 `done`；
10. 请求 `id` 唯一、字段类型正确；大文件分块写；
11. 交付物落在 `deliverables` 里、格式是 grader 能提取的；
12. `final_message` 写清"每份交付物的路径 + 关键结果"；
13. `kit check --cancel-test` 全过、打印 `ready to submit`；
14. `eval/run.py` 全量跑完，`kit artifacts` 退出码 0，`artifacts/` 与 harness **同一个 commit**；
15. 改过任何非文档文件 → 重跑 dev set（否则 reproduction 会掉分）。

---

*本手册由代码与文档逐行核对写成；数字与引文对应 commit `8320f04`。发现不一致时，以 `ple/` 仓库里的代码为最终依据。*
