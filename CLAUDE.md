# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

使用 `uv` 进行依赖管理和运行。CLI 顶层只暴露 `tui` / `gateway` 两个子命令；`--config` / `--agent` 走环境变量（`NANO_OPENCLAW_CONFIG_PATH` / `NANO_OPENCLAW_STATE_DIR`）和 config 文件。

```bash
# 安装依赖
uv sync

# 运行测试
uv run pytest tests/

# 运行单个测试文件
uv run pytest tests/test_gateway_ws_protocol.py

# 默认入口：进入 TUI（自动探测本机 daemon，没探测到走 embedded REPL）
uv run nano-openclaw
uv run nano-openclaw tui

# TUI 远程接 daemon
uv run nano-openclaw tui --connect ws://host:5000/rpc

# Daemon 管理 —— daemon 内并发跑 WebUI + WeChat channels + cron + subagent
uv run nano-openclaw gateway start             # detached 后台
uv run nano-openclaw gateway start --port 8080 # 覆盖 config 端口
uv run nano-openclaw gateway status            # 多行结构化报告（含 RPC probe）
uv run nano-openclaw gateway stop
uv run nano-openclaw gateway run               # 前台模式（systemd / docker 用）

# 顶层 back-compat flags（forward 到 tui）
uv run nano-openclaw --resume
uv run nano-openclaw --sessions

# 指定配置文件走环境变量（CLI 不再有 --config 顶层 flag）
NANO_OPENCLAW_CONFIG_PATH=./my-config.json5 uv run nano-openclaw
```

测试不需要 API key，纯本地工具单测。

## Architecture

nano-openclaw 是 OpenClaw agent loop + gateway daemon 的最小化 Python 复刻。

**核心循环不变量（loop.py）：**

1. 每一轮把**完整 history** 发回模型（超出预算时 `compact_if_needed` 会压缩）
2. 多个 `tool_use` 并存时，所有结果合并成**一条** user 消息回灌
3. 循环只在 `stop_reason != "tool_use"` 时终止

**Backend / RPC 不变量：**

4. `EmbeddedBackend` 和 `WebSocketBackend` 满足同一个 `Backend` Protocol（28 个方法）；TUI 切换 backend 不感知差异
5. `chat.abort(turn_id)` 是 chat / cron / channel 的统一取消接口（依靠 `RunRegistry`）
6. daemon 内**单一** `BackendSessionManager` 实例同时被 WebUI、`/rpc`、Channels 共享 —— `/sessions` 在三个前端永远看到同一份 list

### Core Files (阅读顺序)

`loop.py` 是核心 spine，理解它就理解了整个 agent。然后看 `gateway/backend.py`（Protocol 定义）+ `gateway/server.py`（daemon 入口）+ `gateway/slash.py`（共享 slash 渲染）。

### Key Modules

#### Agent loop & provider
- **`loop.py`** — agent loop 核心，驱动用户输入到模型响应到工具调用的完整循环
- **`provider.py`** — 路由层，switch(api) 派发给 Anthropic/OpenAI transport
- **`_stream_events.py`** — 5 个共享 dataclass，两个 transport 的协议契约
- **`tools.py`** — ToolRegistry + 内置工具（read/write/list/bash）+ dispatch 永不抛异常
- **`compact.py`** — token 估算 + 历史压缩（调用 LLM 生成摘要）
- **`prompt.py`** — `build_system_prompt(registry)` 拼装 system prompt
- **`runtime.py`** — `AgentRuntime` 工厂：构建 model client + ToolRegistry + RunRegistry + RuntimeUpdateGuard 一并

#### Gateway / Backend
- **`gateway/server.py`** — `run_daemon`：daemon 入口，启动 channels + uvicorn FastAPI（WebUI + /rpc）
- **`gateway/backend.py`** — `Backend` Protocol（chat / sessions / approvals / models / runtime / channels / subagents / features / introspection / health）
- **`gateway/backend_embedded.py`** — `EmbeddedBackend`：直接持 AgentRuntime
- **`gateway/backend_websocket.py`** — `WebSocketBackend`：JSON-RPC 远程客户端
- **`gateway/protocol.py`** — Pydantic Request/Response/PushFrame + ErrorCode + METHODS_V1 catalog
- **`gateway/ws_route.py`** — FastAPI `/rpc` WebSocket dispatch + push fanout
- **`gateway/methods/`** — 一族 RPC 一个文件（chat / sessions / approvals / models / runtime / channels / subagents / features / introspection / health）
- **`gateway/slash.py`** — 共享 slash dispatcher + Rich Table/Panel 渲染（embedded + remote 共用）
- **`gateway/run_registry.py`** — turn_id ↔ asyncio.Task 注册表（统一 abort 入口）
- **`gateway/runtime_lock.py`** — `RuntimeUpdateGuard`：reader/writer fail-fast 协调（chat 持 reader，runtime.update 持 writer，冲突立即 BUSY）
- **`gateway/agent_backend_session.py`** — `AgentBackendSession` + `BackendSessionManager`（per-conversation 实体 + 持久化）
- **`gateway/approval_broker.py`** — `ApprovalBroker`（人交互）+ `NonInteractiveApprovalHandler`（cron/channel 自动决策走 allowlist）
- **`gateway/pidfile.py`** — `gateway start/stop/status` 的 PID 文件 + 端口存活检查
- **`gateway/cli.py`** — gateway 子命令 argparse handler（含 RPC probe 状态报告）

#### Channels
- **`channels/base.py`** — `Channel` ABC + `ChannelAccount` + `ChannelStatus`
- **`channels/registry.py`** — `ChannelRegistry`（class registry + 实例生命周期 + cron 通知 dispatch）
- **`channels/wechat/`** — WechatChannel：每 uid 持久化 session（uid → session_id 映射写到 `state_dir/wechat-sessions.{account}.json`）

#### Other
- **`config/`** — JSON5 加载 + Pydantic 验证 + 环境变量替换 + 模型解析
- **`session/`** — transcript 持久化（.jsonl）+ sessions.json 索引
- **`approvals/`** — 危险命令门禁 + per-agent allowlist 持久化（带 `_allowlist_lock`）
- **`plugins/`** — 轻量 Plugin Protocol + HookRegistry + builtin wrappers
- **`subagent/`** — 后台子 agent runner + registry + completion auto-announce
- **`memory/`** — daily memory 加载 + memory_get/search 工具 + Active Memory 自动召回 + Dreaming 后台整合
- **`mcp/`** — MCP 服务器连接管理（stdio/SSE/streamable-http）→ 工具注册
- **`skills/`** — SKILL.md 加载 + slash commands + model-invokable Skill tool + `skill_install` 隔离依赖安装
- **`workspace/`** — bootstrap 文件加载（AGENTS.md 等 8 个标准文件）+ budget 截断
- **`webui/`** — FastAPI 路由（mount 到 daemon 的 FastAPI app；不再独立子命令）
- **`schedule/`** — cron scheduler + recovery（restart 不重触发已跑过任务，按 `last_run_at_ms` 去重）
- **`wechat/bot.py`** — `WechatBot` long-poll runner（被 `WechatChannel` 拉起；token 来自扫码登录写入的 `state_dir/wechat-tokens.{id}.json`）
- **`wechat/login_cli.py`** — `nano-openclaw wechat login` 入口；执行 QR 状态机 + 持久化 token 到 state_dir;daemon 通过 `discover_persisted_account_ids` 发现账号
- **`cli.py`** — 单进程 REPL（embedded 模式）+ slash 命令通过 `gateway/slash.py` dispatch
- **`__main__.py`** — 顶层 argparse：`tui` / `gateway`

### Provider Transport

两个 transport 都翻译到 `_stream_events.py` 的 5 个事件类型：
- `TextDelta` — 文本增量
- `ToolUseStart/Delta/End` — 工具调用增量
- `ThinkingDelta/BlockComplete` — thinking 块（Anthropic 原生 / OpenAI reasoning_content）
- `MessageEnd` — 消息结束 + stop_reason

### Config Resolution

配置文件查找优先级：
1. `$NANO_OPENCLAW_CONFIG_PATH` 环境变量
2. `{stateDir}/nano-openclaw.json5`
3. `{cwd}/workspace/nano-openclaw.json5`
4. `~/.nano-openclaw/nano-openclaw.json5`

模型引用格式：`provider/model-id`（如 `anthropic/claude-sonnet-4`、`openai/gpt-4o`）

### Gateway Config

```jsonc
gateway: { host: "127.0.0.1", port: 5000, log_path: "" }
```

WeChat 不在配置里 —— 通过 `nano-openclaw wechat login [--account=ID]` 扫码登录,token 持久化在 `state_dir/wechat-tokens.{id}.json`,daemon 启动时自动发现并为每个文件起一个 `WechatChannel`。

CLI 覆盖：`gateway start --host 0.0.0.0 --port 8080` 仅本次启动生效。

### Storage Layout

```
state_dir/
├── nano-openclaw.json5                     # 主配置文件
├── exec-approvals.json                     # 审批 allowlist
├── gateway.pid                             # daemon 运行时 PID + bind 信息
├── log/
│   ├── nano-openclaw.log                   # JSON Lines（structured logger）
│   └── gateway.log                         # daemon stdout/stderr（仅后台模式）
├── agents/{agentId}/sessions/
│   ├── sessions.json                       # session 索引
│   └── {sessionId}.jsonl                   # transcript（per session）
├── wechat-tokens.{account}.json            # 扫码登录写入的 token（per account；daemon 启动时枚举）
├── wechat-sessions.{account}.json          # uid → session_id 映射（per account）
├── notify-queue.{account}.jsonl            # cron 完成通知队列（per account）
└── cron/
    ├── jobs.json                           # cron 任务定义
    └── jobs-state.json                     # nextRunAtMs / lastRunAtMs 状态
```

### Plugin Hooks

Plugin hooks 在 `loop.py` 关键点触发：
- `session_start` / `session_end` — 会话生命周期
- `before_prompt_build` — system prompt 构建前（可 prepend/append/替换）
- `on_loop_event` — 每个 loop 事件（ToolResult、Compaction 等）
- `before_tool_call` — 工具调用前（可 deny 或修改 args）

### Memory System

四层机制：
- **Daily Memory** — 启动加载 `memory/*.md` 最近 N 天文件
- **Memory Tools** — `memory_get` / `memory_search`（词法匹配 + 上下文窗口匹配）
- **Active Memory** — 每次 user message 前自动子 agent 搜索（可选）
- **Dreaming** — 追踪召回记录 + 定期提升到 MEMORY.md（可选）

### Slash Command Dispatch

`gateway/slash.py::handle_slash` 是 **embedded + remote 模式共享**的入口：

- `/quit /help /clear /new /context /compact`
- `/sessions [all|delete <id>]` `/session [prefix|#]`
- `/tools /skills /plugins /hooks` — Rich Table 渲染
- `/subagents [list|kill <id>|all]`
- `/active-memory [status|on|off|mode|style]` `/dreaming [status|on|off|run]`
- `/health /channels /runtime` — daemon 内省

所有命令通过 Backend RPC 调用，不直接读 `runtime.registry` / `cfg`，所以两种模式渲染 100% 一致。

### Approval routing

- 交互式 turn（TUI / WebUI / WeChat 用户主动发消息）：走 `ApprovalBroker`，等用户响应。
- 非交互式 turn（cron / channel 自动触发）：走 `NonInteractiveApprovalHandler`，consult 同一份 allowlist —— 命中 ALLOW，未命中强制 DENY，永不弹 prompt 阻塞。

### Invariants

读 `loop.py` 时记住：

1. `ToolRegistry.dispatch` **永不抛异常** — 错误编码为 `is_error=True`
2. 多个 `tool_use` 的结果合并成**一条** user 消息
3. `stop_reason == "tool_use"` 是唯一继续循环的条件
4. 取消的 turn（Esc）不会派发已触发的工具调用

读 `gateway/backend_embedded.py` / `gateway/server.py` 时记住：

5. `EmbeddedBackend.chat_send` 拿到锁占用的 session 立即抛 `BusyError`，**不静默排队**
6. `RuntimeUpdateGuard` 是 fail-fast 而非阻塞 —— writer-acquire 时有 reader 立即 BUSY
7. cron / channel 触发的 turn 也注册到 `RunRegistry`，`chat.abort(turn_id)` 通用
8. cron scheduler 重启不重触发已跑过的任务（用 `last_run_at_ms` + 60s grace 去重）

### Debug

`NANO_DEBUG_PROMPT=1` 会把每次 API 请求的完整 payload 写入 `nano-openclaw-debug.jsonl`。

`gateway status` 输出包含 RPC probe（health / runtime.get / channels.status），daemon 异常时降级显示 `rpc probe: timed out` 但基础信息（pid + port + log path）仍输出。
