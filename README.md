# nano-openclaw

[![Tests](https://github.com/caftxx/nano-openclaw/actions/workflows/test.yml/badge.svg)](https://github.com/caftxx/nano-openclaw/actions/workflows/test.yml)

用最少的代码复刻 OpenClaw 的 agent 运行原理。
精神类比 [nanoGPT](https://github.com/karpathy/nanoGPT) 之于 GPT：**真实可跑，但只保留骨架，删掉一切可选层**。

读完这个仓库里的核心 `.py` 文件，你就理解了一个"会用工具的 LLM agent"的全部秘密。

## 快速运行

依赖管理用 [uv](https://github.com/astral-sh/uv)。

```bash
# 第一次：克隆仓库、装依赖、创虚拟环境
git clone git@github.com:caftxx/nano-openclaw.git
cd nano-openclaw
uv sync

# 跑测试（不需要 API key，纯本地工具单测）
uv run pytest tests/

# 仓库自带一份 .nano-openclaw-dev/ 作为模板。nano-openclaw 默认识别项目根的
# .nano-openclaw/ —— 把模板拷过去再改：
cp -r .nano-openclaw-dev .nano-openclaw

# 然后编辑 .nano-openclaw/nano-openclaw.json5，填入你的 API key 和 provider 信息
```

```bash
# 默认：进入 TUI（自动探测本机 daemon；没探测到就走单进程 embedded REPL）
uv run nano-openclaw

# 显式 TUI（同上）
uv run nano-openclaw tui

# TUI 远程连远程 daemon
uv run nano-openclaw tui --connect ws://remote-host:5000/rpc

# 启动 / 停止 / 查看 daemon —— daemon 内部跑 WebUI、WeChat、cron、subagent 等
uv run nano-openclaw gateway start              # 后台 spawn detached
uv run nano-openclaw gateway start --port 8080  # 覆盖 config 端口
uv run nano-openclaw gateway status             # 多行结构化状态报告
uv run nano-openclaw gateway stop
uv run nano-openclaw gateway run                # 前台模式（systemd / docker 用）

# 顶层 back-compat flags（等价于 tui --resume / tui --list-sessions）
uv run nano-openclaw --resume
uv run nano-openclaw --sessions

# 指定配置文件走环境变量（CLI 不再有 --config 顶层 flag）
NANO_OPENCLAW_CONFIG_PATH=./my-config.json5 uv run nano-openclaw
```

**架构一览：**

```
┌────────────────────────────────────────────────────┐
│  daemon（gateway run）                              │
│  ├─ AgentRuntime（单实例，跨前端共享）              │
│  ├─ WebUI HTTP 路由（FastAPI）→  浏览器              │
│  ├─ /rpc WebSocket          →  TUI --connect 远程   │
│  ├─ Channels：wechat × N 账号、cron、subagent       │
│  └─ Backend Protocol 单一管理器（Sessions, Runs）    │
└────────────────────────────────────────────────────┘

  TUI 进程（独立）
  ├─ embedded 模式：无 daemon 时自建 runtime
  └─ remote 模式：tui --connect 走 WebSocket
```

**WebUI**：`gateway start` 后浏览器打开 `http://127.0.0.1:5000`（默认端口可配，见下文）。

---

### Docker Compose 启动

不想装 Python 环境？用 Docker Compose 一键启动 gateway daemon：

```bash

# 仓库自带一份 .nano-openclaw-dev/ 作为模板。nano-openclaw 默认识别项目根的
# .nano-openclaw/ —— 把模板拷过去再改：
cp -r .nano-openclaw-dev .nano-openclaw

# 复制环境变量模板并填入 API key
cp .env.example .env

# 启动 daemon（后台运行，访问 http://localhost:5000）
docker compose --profile gateway up -d

# 进入 TUI（连本机 daemon）
docker compose run --rm tui

# 停止 daemon
docker compose --profile gateway down
```

**端口**：默认 `5000`，通过 `.env` 里的 `GATEWAY_PORT` 修改宿主机端口：

```bash
GATEWAY_PORT=9000 docker compose --profile gateway up -d
# 访问 http://localhost:9000
```

**Volume 映射**：

| 宿主机路径 | 容器路径 | 用途 |
| --- | --- | --- |
| `./.nano-openclaw/` | `/data/.nano-openclaw/` | 会话、配置、记忆、PID/log 等 |

第一次启动前先拷模板：`cp -r .nano-openclaw-dev .nano-openclaw`，然后编辑 `.nano-openclaw/nano-openclaw.json5` 即可自动加载。容器中 agent 的工作目录由配置文件中的 `workspaceDir` 决定，默认为 `~/.nano-openclaw/workspace/`。WebUI 支持斜杠命令、thinking 开关、图片/文件附件、活动历史回放、亮色/暗色/跟随系统主题，移动端自适应。

配置详解见 [CONFIG_EXAMPLE.md](docs/CONFIG_EXAMPLE.md)。

## 配置文件

配置文件采用 JSON5 格式（支持注释和尾逗号），路径按优先级查找：

1. `$NANO_OPENCLAW_CONFIG_PATH` — 环境变量
2. `{stateDir}/nano-openclaw.json5` — 状态目录下
3. `{cwd}/workspace/nano-openclaw.json5` — 项目 workspace 目录
4. `~/.nano-openclaw/nano-openclaw.json5` — 用户全局配置

状态目录 (`stateDir`) 解析：`$NANO_OPENCLAW_STATE_DIR` > `{cwd}/.nano-openclaw` > `~/.nano-openclaw`

### 快速开始

1. 复制示例配置：`cp nano-openclaw-example.json5 nano-openclaw.json5`
2. 编辑配置文件，填入你的 API key 和 provider 信息
3. 运行 `uv run nano-openclaw`

### 模型引用格式

所有模型统一使用 `provider/model-id` 格式：

| 引用示例 | 说明 |
| --- | --- |
| `anthropic/claude-sonnet-4` | 内置 Anthropic provider |
| `openai/gpt-4o` | 内置 OpenAI provider |
| `openrouter/anthropic/claude-sonnet-4` | 自定义 provider + 远程模型 |

内置 provider（`anthropic`、`openai`）无需配置，自动从环境变量读取 API key。

### Gateway 配置

```json5
gateway: {
  host: "127.0.0.1",   // 默认 loopback；改成 0.0.0.0 时启动会打 warning（v1 无 auth）
  port: 5000,
  log_path: "",        // 留空 → state_dir/log/gateway.log
}
```

CLI 覆盖：`gateway start --host 0.0.0.0 --port 8080` 仅本次启动生效。

### WeChat 多账号配置

```json5
wechat: {
  accounts: [
    {
      id: "default",
      ilink_token: "${ILINK_TOKEN}",         // 支持 ${ENV_VAR} 替换
    },
    { id: "work", ilink_token: "..." },      // 多账号继续加
  ],
}
```

> `ilink_token` 获取方式：先用 openclaw 或 hermes 连接微信 bot，然后从其 token 文件中读取。

WeChat 作为 daemon 内的 Channel 运行；用户每个 uid 自动绑定一个真实的持久化 session（与 TUI/WebUI 共用 `/sessions` 列表）。uid → session_id 映射持久化在 `state_dir/wechat-sessions.{account}.json`。

**完整配置字段说明见 [CONFIG_EXAMPLE.md](docs/CONFIG_EXAMPLE.md)。**

## 日志系统

支持结构化 JSON Lines 日志，通过环境变量或配置文件控制：

```bash
# 通过环境变量设置日志等级
NANO_LOG_LEVEL=DEBUG uv run nano-openclaw

# 或在配置文件中设置
# config.logging.level = "INFO"
```

日志文件位于 `{stateDir}/log/`：

| 文件 | 说明 |
|---|---|
| `nano-openclaw.log` | 结构化 JSON Lines（自动轮转 + Gzip） |
| `gateway.log` | daemon 进程的 stdout/stderr（仅 `gateway start` 后台启动时写入） |

`nano-openclaw.log` 支持 JSON Lines、自动轮转（>10MB 滚动）、Gzip 压缩、上下文注入（session_id、run_id、tool_call_id）。日志等级：`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`（默认 `WARNING`）。

---

## TUI 斜杠命令

在 TUI（embedded 或 remote）输入。两种模式现在共享同一套 dispatcher，渲染和行为完全一致：

| 命令 | 说明 |
|---|---|
| `/quit` | 退出 REPL |
| `/help` | 显示命令列表 |
| `/clear` | 清空当前 session 历史 |
| `/new` | 创建新 session（保留当前会话已有数据） |
| `/sessions [all]` | 渲染 Rich Table；`all` 显示全部 |
| `/sessions delete <id_prefix>` | 删除指定 session（活跃 session 拒绝删除，返回 BUSY） |
| `/session <prefix\|#>` | 切换到指定 session |
| `/context` | Context window 用量 + 模型/thinking 摘要 |
| `/compact` | 强制压缩当前 session 历史 |
| `/tools` | Rich Table 列出全部工具 + 描述 |
| `/skills` | Rich Table 列出 skills + status / in_prompt / reason |
| `/plugins` | Rich Table 列出 plugins + tools/hooks |
| `/hooks` | Rich Table 列出 hooks + plugins/priorities |
| `/subagents [list\|kill <id>\|all]` | 后台子 agent 状态 |
| `/active-memory [status\|on\|off\|mode\|style]` | Active Memory 配置 |
| `/dreaming [status\|on\|off\|run]` | Dreaming 配置 + 立即跑一次 |
| `/health` | daemon 健康状态（runtime_ready, channels, in-flight） |
| `/channels` | 已运行的 channel 列表 |
| `/runtime` | agent / model / workspace 摘要 |

### 操作提示

在 REPL 输入过程中按 **Esc** 键可取消当前进行中的 agent turn（embedded 模式），不会修改历史或 transcript。`tui --connect` 远程模式按 Ctrl-C 通过 `chat.abort` RPC 取消。

## 60 秒架构图

```
                      ┌──────────────────────┐
    user types  ───▶ │   cli.repl()         │  rich-rendered REPL
                      └─────────┬────────────┘
                                │ Backend Protocol
                                ▼
                      ┌──────────────────────┐
                      │   EmbeddedBackend     │  embedded 模式
                      │   or                  │
                      │   WebSocketBackend    │  --connect 模式
                      └─────────┬────────────┘
                                │
                                ▼
                      ┌──────────────────────┐
    image refs ─────▶ │ AgentSession.run_turn│  parse_image_refs → load_image
    (@file.png)       │  plugin hooks        │  before_prompt_build / on_loop_event
                       └──┬──────────────┬────┘
           compact check │              │ tool_use blocks
                         ▼              ▼
                ┌──────────────┐  ┌──────────────────────┐
                │  compact.py  │  │  tools.dispatch()    │
                │  token est.  │  │  read/write/list/bash│
                │  summarize   │  │  plugin tools        │
                │              │  │  before/after hooks  │
                └──────┬───────┘  │                      │
        history shrunk │          └────────┬─────────────┘
                       ▼                   ▼
                     ┌──────────────────────┐
                     │     provider.py      │
                     │  路由层 switch(api)   │
                     └────┬─────────────────┘
           ┌─────────────┴──────────────┐
           ▼                            ▼
  ┌──────────────────────┐   ┌──────────────────────┐
  │ _provider_anthropic  │   │  _provider_openai    │
  │  Anthropic Messages  │   │  OpenAI Completions  │
  └──────────────────────┘   └──────────────────────┘

  gateway/         = daemon 主入口 + Backend Protocol + RPC + slash dispatch
                     ├─ server.py: run_daemon (uvicorn + channels + WS)
                     ├─ backend.py: Backend Protocol（28 个方法）
                     ├─ backend_embedded.py / backend_websocket.py
                     ├─ ws_route.py: FastAPI /rpc dispatch
                     ├─ methods/: chat / sessions / approvals / models / runtime / channels / subagents / features / introspection / health
                     ├─ slash.py: 共享 slash dispatch + Rich 渲染（embedded + remote 共用）
                     ├─ run_registry.py: turn_id ↔ asyncio.Task（chat.abort 统一接口）
                     ├─ runtime_lock.py: RuntimeUpdateGuard（runtime hot-reload reader/writer）
                     └─ pidfile.py + cli.py: gateway start/stop/status/run
  channels/        = Channel 抽象（id × accountId 多账号）
                     └─ wechat/: WechatChannel (per-uid 持久 session)
  cli.py           = 单进程 REPL（embedded 模式）
  __main__.py      = 顶层 argparse: tui / gateway
  runtime.py        = AgentRuntime + RunRegistry + RuntimeUpdateGuard 一并构建
  logger.py         = JSON Lines 日志（state_dir/log/）+ 轮转/Gzip + contextvars
  config/           = JSON5 加载 + Pydantic 验证 + 环境变量替换 + 模型解析
  _stream_events.py = 5 个共享 dataclass（两个 transport 的协议契约）
  prompt.py         = build_system_prompt(registry)
  compact.py        = estimate_tokens → compact_if_needed → summarize_history
  approvals/        = requiresExecApproval() 门禁 + per-agent allowlist 持久化 +
                       NonInteractiveApprovalHandler（cron / channel 自动决策）
  plugins/          = 轻量 Plugin Protocol + HookRegistry + builtin wrappers
  subagent/         = 后台子 agent runner + registry + completion auto-announce
  webui/            = FastAPI 路由（mount 到 daemon FastAPI app；不再独立子命令）
  schedule/         = cron scheduler + recovery（restart 不重触发已跑过的任务）
  images.py         = parse_image_refs → load_image → describe_image
  mcp/              = MCP 服务器连接管理（stdio/SSE/streamable-http）
  session/          = transcript 持久化（.jsonl）+ sessions.json 索引
  web_fetch.py      = URL 内容抓取 → readability → markdown + 缓存 + SSRF 防护
  web_search.py     = DuckDuckGo 搜索 + 缓存
  ssrf_guard.py     = 两阶段 SSRF 防护
  external_content.py = 外部内容安全包装 + LLM token 清洗
  memory/           = daily memory + memory_get/search + Active Memory + Dreaming
```

## 三条不变量

读 `loop.py` 时记住这三句话：

1. 每一轮把**完整 history** 发回模型——若超出 token 预算，`compact_if_needed` 会先把旧消息替换成一条摘要，再发送压缩后的 history。
2. 多个 `tool_use` 并存时，所有结果合并成**一条** user 消息回灌。
3. 循环只在 `stop_reason != "tool_use"` 时终止；其它都是中间态。

Backend / RPC 不变量：

4. `EmbeddedBackend` 和 `WebSocketBackend` 满足同一个 `Backend` Protocol，TUI 切换 backend 不感知差异。
5. `chat.abort(turn_id)` 是统一的取消接口：chat / cron / channel 任何 origin 的 in-flight turn 都能从这里中断（依靠 `RunRegistry`）。
6. daemon 内**单一** `BackendSessionManager` 实例同时被 WebUI、`/rpc`、Channels 共享 —— `/sessions` 在三个前端永远看到同一份 list。

Subagent 编排：模型可调用 `sessions_spawn` 启动 isolated 后台子 agent，适合复杂、慢、可并行的任务。子 agent 继承 workspace、模型和 thinking 默认值，可通过顶层 `subagents` 配置限制并发、超时和默认模型；它不会继承 `sessions_spawn` 等会话管理工具，避免递归派生。完成、失败或超时后，结果会自动作为一条 user message 注入父 session。后台子 agent 不能弹出前台审批 UI：触发 approval 的工具调用走 `NonInteractiveApprovalHandler`——allowlist 命中即放行，否则拒绝。

图片处理遵循**双路径架构**：未配置 `image_model` 时走 Native Vision（图片直接发给主模型）；配置后走 Media Understanding（图片模型先描述，文字注入 prompt）。若主模型无视觉能力且未配置 `image_model`，图片会被跳过并显示警告。`parse_image_refs` 在循环入口处统一处理用户输入中的 `@file.png`、Markdown `![]()` 和 URL 引用。

MCP 工具集成：通过 `config.mcp.servers` 配置外部 MCP 服务器，启动时建立持久连接（支持 stdio/SSE/streamable-http 三种传输），工具自动注册到 ToolRegistry。服务器连接在后台 asyncio 线程中运行，daemon 退出时自动清理。

Web 工具集成：内置 `web_search`（DuckDuckGo 搜索）和 `web_fetch`（URL 内容抓取）工具，默认启用，可通过 `tools.web` 配置单独控制。所有外部内容通过 `<EXTERNAL_UNTRUSTED_CONTENT>` 边界标记包装并清洗 LLM 特殊 token，防止 prompt injection。`web_fetch` 带有 SSRF 两阶段防护（预 DNS 黑名单 + 后 DNS 私有 IP 验证）。搜索结果和抓取内容均有 10 分钟缓存。

Thinking 支持：通过 `agents.defaults.thinkingDefault` 配置思考等级（`off|minimal|low|medium|high|xhigh|adaptive|max`）。Anthropic provider 使用原生 thinking API；OpenAI-compatible provider 使用 `reasoning_content` 流。Thinking 块会持久化到消息历史，CLI 以 dim 样式在 assistant 输出前渲染。

Workspace 引导文件：从 `workspaceDir` 加载 8 个标准引导文件（AGENTS.md、SOUL.md、IDENTITY.md、USER.md、MEMORY.md、TOOLS.md、BOOTSTRAP.md、HEARTBEAT.md），应用安全防护和预算截断，注入到系统提示的项目上下文部分。支持 session-scoped 缓存。

Memory 系统：包含四层机制：
- **Daily Memory**：启动时自动加载 `workspace/memory/*.md` 中最近 N 天的记忆文件（默认 2 天）。
- **Memory Tools**：`memory_get` / `memory_search` 工具。nano 用词法匹配而非 embedding 搜索。
- **Active Memory**：可选，启用后在每次用户消息前自动子 agent 搜索记忆。通过 `activeMemory` 配置。
- **Dreaming**：可选，定期将高频记忆提升到 MEMORY.md。通过 `dreaming` 配置。

Cron 任务：通过 `cron_create` 工具或配置文件定义；daemon 内部跑 cron scheduler；任务完成可定向通知发起方（wechat 用户收到 daemon 推送的消息）。daemon 重启不会重复触发同一时间窗已经跑过的任务（`last_run_at_ms` 去重）。

Session Status 工具：内置 `session_status` 工具用于查询当前日期时间和会话上下文信息（模型 ID、session ID、token 使用量等）。

## 端到端验证

启动 daemon 然后用 TUI 试这一句：

```bash
uv run nano-openclaw gateway start
uv run nano-openclaw tui
```

```
>>> 列出当前目录的文件，再读一下 pyproject.toml 的内容并简要总结
```

期望看到：先一个绿色的 `list_dir({"path":"."})` 面板，再一个 `read_file({"path":"pyproject.toml"})` 面板，最后模型给你一段总结后正常结束。

错误路径试试：

```
>>> 用 bash 跑一下 cat /this/path/does/not/exist
```

bash 工具面板会带**红色边框**，显示非零 exit 与 stderr；模型据此回复合理总结，整个程序不应崩溃。

图片处理试试：

```
>>> 看看 @screenshot.png 里有什么内容
```

模型会解析 `@` 引用，加载图片（自动压缩超大图片），然后：
- **Native Vision**（默认）：图片以 base64 块发送给主模型，直接分析
- **Media Understanding**（配置 `imageModel`）：先用图片模型描述成文字，再注入 prompt
- **跳过**（主模型无视觉能力且未配置 `imageModel`）：显示黄色警告，图片被跳过

网络搜索试试：

```
>>> 搜索一下 Python 3.13 的新特性
```

`web_search` 会使用 DuckDuckGo 搜索返回标题、URL 和摘要，结果包裹在 `<EXTERNAL_UNTRUSTED_CONTENT>` 中。模型可以据此回复，也可以继续用 `web_fetch` 抓取具体页面。

跨前端验证 session 一致性：

```bash
# 终端 1：启动 daemon + 在 TUI 里聊几句
uv run nano-openclaw gateway start
uv run nano-openclaw tui
>>> hello

# 浏览器：打开 http://127.0.0.1:5000，应该看到刚才在 TUI 里发的会话
# WeChat：给配置的账号发消息，TUI 的 /sessions 也能看到那个会话
```

## License

MIT — 见 [LICENSE](./LICENSE)。
