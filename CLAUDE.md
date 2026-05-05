# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

使用 `uv` 进行依赖管理和运行：

```bash
# 安装依赖
uv sync

# 运行测试
uv run pytest tests/

# 运行单个测试文件
uv run pytest tests/test_loop.py

# 运行 REPL
uv run python -m nano_openclaw

# 指定配置文件
uv run python -m nano_openclaw --config my-config.json5

# 恢复上次会话
uv run python -m nano_openclaw --resume

# 列出已保存会话
uv run python -m nano_openclaw --sessions

# 指定 agent（session 隔离）
uv run python -m nano_openclaw --agent coder
```

测试不需要 API key，纯本地工具单测。

## Architecture

nano-openclaw 是 OpenClaw agent loop 的最小化 Python 复刻，用于教学目的。核心循环只有三条规则：

1. 每一轮把**完整 history** 发回模型（超出预算时 `compact_if_needed` 会压缩）
2. 多个 `tool_use` 并存时，所有结果合并成**一条** user 消息回灌
3. 循环只在 `stop_reason != "tool_use"` 时终止

### Core Files (阅读顺序)

`loop.py` 是核心 spine，理解它就理解了整个 agent。推荐阅读顺序见 README.md 的"顺着循环读"部分。

### Key Modules

- **`loop.py`** — agent loop 核心，驱动用户输入到模型响应到工具调用的完整循环
- **`provider.py`** — 路由层，switch(api) 派发给 Anthropic/OpenAI transport
- **`_stream_events.py`** — 5 个共享 dataclass，两个 transport 的协议契约
- **`tools.py`** — ToolRegistry + 内置工具（read/write/list/bash）+ dispatch 永不抛异常
- **`compact.py`** — token 估算 + 历史压缩（调用 LLM 生成摘要）
- **`config/`** — JSON5 加载 + Pydantic 验证 + 环境变量替换 + 模型解析
- **`session/`** — transcript 持久化（.jsonl）+ sessions.json 索引
- **`approvals/`** — 危险命令门禁 + per-agent allowlist 持久化
- **`plugins/`** — 轻量 Plugin Protocol + HookRegistry + builtin wrappers
- **`subagent/`** — 后台子 agent runner + registry + completion auto-announce
- **`memory/`** — daily memory 加载 + memory_get/search 工具 + Active Memory 自动召回 + Dreaming 后台整合
- **`mcp/`** — MCP 服务器连接管理（stdio/SSE/streamable-http）→ 工具注册
- **`skills/`** — SKILL.md 加载 + slash commands + model-invokable Skill tool
- **`workspace/`** — bootstrap 文件加载（AGENTS.md 等 8 个标准文件）+ budget 截断

### Provider Transport

两个 transport 都翻译到 `_stream_events.py` 的 5 个事件类型：
- `TextDelta` — 文本增量
- `ToolUseStart/Delta/End` — 工具调用增量
- `ThinkingDelta/BlockComplete` — thinking 块（Anthropic 原生 / OpenAI reasoning_content）
- `MessageEnd` — 消息结束 + stop_reason

### Config Resolution

配置文件查找优先级：
1. `--config <path>` 命令行
2. `$NANO_OPENCLAW_CONFIG_PATH` 环境变量
3. `{stateDir}/nano-openclaw.json5`
4. `{cwd}/workspace/nano-openclaw.json5`
5. `~/.nano-openclaw/nano-openclaw.json5`

模型引用格式：`provider/model-id`（如 `anthropic/claude-sonnet-4`、`openai/gpt-4o`）

### Session Storage

- `{stateDir}/agents/{agentId}/sessions/` — 按 agent 隔离
- `{session_id}.jsonl` — transcript 文件
- `sessions.json` — session 索引 + metadata

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

### Invariants

读 `loop.py` 时记住：

1. `ToolRegistry.dispatch` **永不抛异常** — 错误编码为 `is_error=True`
2. 多个 `tool_use` 的结果合并成**一条** user 消息
3. `stop_reason == "tool_use"` 是唯一继续循环的条件
4. 取消的 turn（Esc）不会派发已触发的工具调用

### Debug

`NANO_DEBUG_PROMPT=1` 会把每次 API 请求的完整 payload 写入 `nano-openclaw-debug.jsonl`。