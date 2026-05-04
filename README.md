# nano-openclaw

[![Tests](https://github.com/caftxx/nano-openclaw/actions/workflows/test.yml/badge.svg)](https://github.com/caftxx/nano-openclaw/actions/workflows/test.yml)

用最少的代码复刻 OpenClaw 的 agent 运行原理。
精神类比 [nanoGPT](https://github.com/karpathy/nanoGPT) 之于 GPT：**真实可跑，但只保留骨架，删掉一切可选层**。

读完这个仓库里的核心 `.py` 文件，你就理解了一个"会用工具的 LLM agent"的全部秘密。

## 为什么要写这个

OpenClaw 是一个生产级的 TypeScript agent 框架，能力丰富但代码量很大；想真正"读懂它怎么跑"会被插件系统、provider 抽象、TUI 渲染、会话持久化、权限审批等一系列层层包裹的概念劝退。
nano-openclaw 把这些层全部砍掉，只留**最核心的循环**：用户输入 → 拼消息 → 调模型 → 流式接收 → 派发工具 → 把结果喂回去 → 直到模型说"完事了"。

每个文件都明确映射到 OpenClaw 的真实 TS 源文件（见下方对照表），方便你在 nano 里看明白概念，再去真实代码里查实现细节。

## 快速运行

依赖管理用 [uv](https://github.com/astral-sh/uv)。

```bash
# 第一次：克隆仓库、装依赖、创虚拟环境
git clone git@github.com:caftxx/nano-openclaw.git
cd nano-openclaw
uv sync

# 跑测试（不需要 API key，纯本地工具单测）
uv run pytest tests/

# 复制并编辑配置文件
cp nano-openclaw-example.json5 nano-openclaw.json5
# 编辑 nano-openclaw.json5，填入你的 API key 和 provider 信息
```

```bash
# 运行交互 REPL
uv run python -m nano_openclaw

# 指定配置文件
uv run python -m nano_openclaw --config my-config.json5

# 恢复上次会话
uv run python -m nano_openclaw --resume

# 列出所有已保存的会话
uv run python -m nano_openclaw --sessions

# 指定 agent（session 隔离）
uv run python -m nano_openclaw --agent coder
```

**配置示例**：复制 `nano-openclaw-example.json5` 并根据你的 provider 修改。内置插件（memory、web、subagent、mcp）始终加载，无法通过 plugins.load 禁用。详见下方配置说明。

配置详解见 [CONFIG_EXAMPLE.md](docs/CONFIG_EXAMPLE.md)。

## 配置文件

配置文件采用 JSON5 格式（支持注释和尾逗号），路径按优先级查找：

1. `--config <path>` — 命令行显式指定
2. `$OPENCLAW_CONFIG_PATH` — 环境变量
3. `{stateDir}/nano-openclaw.json5` — 状态目录下
4. `{cwd}/workspace/nano-openclaw.json5` — 项目 workspace 目录
5. `~/.openclaw/nano-openclaw.json5` — 用户全局配置

状态目录 (`stateDir`) 解析：`$OPENCLAW_STATE_DIR` > `{cwd}/.openclaw` > `~/.openclaw`

### 快速开始

1. 复制示例配置：`cp nano-openclaw-example.json5 nano-openclaw.json5`
2. 编辑配置文件，填入你的 API key 和 provider 信息
3. 运行 `uv run python -m nano_openclaw`

### 模型引用格式

所有模型统一使用 `provider/model-id` 格式：

| 引用示例 | 说明 |
| --- | --- |
| `anthropic/claude-sonnet-4` | 内置 Anthropic provider |
| `openai/gpt-4o` | 内置 OpenAI provider |
| `openrouter/anthropic/claude-sonnet-4` | 自定义 provider + 远程模型 |

内置 provider（`anthropic`、`openai`）无需配置，自动从环境变量读取 API key。

**完整配置字段说明见 [CONFIG_EXAMPLE.md](docs/CONFIG_EXAMPLE.md)。**

## REPL 斜杠命令

支持的斜杠命令：`/quit`、`/clear`、`/new`、`/help`、`/context`、`/compact`、`/sessions`、`/save`、`/skills`、`/subagents`。

- `/new` — 硬重置：生成全新 session ID，创建新 .jsonl transcript 文件，更新 session store（匹配 OpenClaw 语义）
- `/clear` — 仅清空内存历史，重置 transcript 文件保留 session header，立即更新 session metadata
- `/subagents` — 查看当前 session 启动的后台子 agent；支持 `/subagents all`、`/subagents kill <run_id>` 和 `/subagents kill all`

### 操作提示

在 REPL 输入过程中按 **Esc** 键可取消当前进行中的 agent turn，不会修改历史或 transcript，并打印 `(turn cancelled)` 提示。

## 60 秒架构图

```
                      ┌──────────────────────┐
    user types  ───▶ │   cli.repl()         │  rich-rendered REPL
                      └─────────┬────────────┘
                                │
                                ▼
                      ┌──────────────────────┐
    image refs ─────▶ │   loop.agent_loop()  │  parse_image_refs → load_image
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

  config/           = JSON5 加载 + Pydantic 类型验证 + 环境变量替换 + 模型解析
  _stream_events.py = 5 个共享 dataclass（两个 transport 的协议契约）+ thinking 事件
  system prompt     = prompt.build_system_prompt(registry)
                      identity + cwd/platform/date + 工具清单 + plugin prompt hooks
   compact.py        = estimate_tokens → compact_if_needed → summarize_history
   approvals/        = requiresExecApproval() 门禁 → Rich 审批提示 → per-agent allowlist 持久化
   plugins/          = 轻量 Plugin Protocol + HookRegistry + builtin wrappers（始终加载）
   subagent/         = 后台子 agent runner + registry + completion auto-announce
   images.py         = parse_image_refs → load_image → describe_image（双路径架构）
    mcp/              = MCP 服务器连接管理（stdio/SSE/streamable-http）→ 工具注册
    session/          = transcript 持久化（.jsonl）+ sessions.json 索引 + 8KB 截断 + store-first 初始化
    web_fetch.py      = URL 内容抓取 → readability 提取 → markdown 转换 → 缓存 + SSRF 防护
    web_search.py     = DuckDuckGo 网页搜索 → 结果格式化 → 缓存
    ssrf_guard.py     = 两阶段 SSRF 防护（预 DNS 检查 + 后 DNS 解析验证私有 IP）
    external_content.py = 外部内容安全包装（<EXTERNAL_UNTRUSTED_CONTENT> + LLM token 清洗）
   thinking          = Anthropic 原生 thinking / OpenAI reasoning_content → dim 样式渲染 → 持久化到消息历史
   memory/           = daily memory 文件加载 + memory_get/search 工具 + Active Memory 自动召回
```

## 模块映射（nano ↔ OpenClaw）

| nano_openclaw 文件 / 符号                   | 对应的 OpenClaw 真实位置                                                              |
| ------------------------------------------ | ------------------------------------------------------------------------------------ |
| `config/types.py`                          | `src/config/`（Pydantic 类型验证 + 配置结构）                                          |
| `config/paths.py`                          | `src/config/paths.ts` + `src/agents/agent-scope-config.ts`（路径 + workspace 解析）     |
| `config/io.py`                             | `src/config/load.ts`（配置文件加载 + 模型解析）                                        |
| `config/env_substitution.py`               | `src/config/env-substitution.ts`（`${ENV_VAR}` 替换）                                 |
| `loop.py::agent_loop`                      | `src/agents/pi-embedded-runner/run/attempt.ts:566` (`runEmbeddedAttempt`)            |
| 消息内容块结构                               | `src/agents/stream-message-shared.ts` (`AssistantMessage`)                           |
| `provider.py::stream_response`             | `src/agents/provider-transport-stream.ts`（switch(model.api) 路由层）                 |
| `_stream_events.py`                        | `src/agents/transport-stream-shared.ts`（共享事件类型契约 + thinking 事件）|
| `_provider_anthropic.py::stream_response`  | `src/agents/anthropic-transport-stream.ts:742+`（SSE → 归一化事件）                    |
| `_provider_openai.py::stream_response`     | `src/agents/openai-transport-stream.ts`（OpenAI → 归一化事件）                         |
| `_provider_openai._to_openai_messages`     | `src/agents/transport-message-transform.ts`（Anthropic↔OpenAI 格式转换）              |
| `compact.py::compact_if_needed`            | `src/agents/compaction.ts`（token 估算 + 摘要压缩旧消息）                              |
| `compact.py::summarize_history`            | `src/agents/compaction.ts`（调用 LLM 生成历史摘要）                                    |
| `images.py::parse_image_refs`              | `src/media/parse.ts`（检测 @file.png、Markdown ![]()、URL 等图片引用）                |
| `images.py::load_image`                    | `src/media/input-files.ts`（SSRF 防护 + 大小限制 + 自动压缩）                          |
| `images.py::describe_image`                | `src/media-understanding/`（Media Understanding 路径：调用模型描述图片）              |
| `tools.py::Tool` 数据类                     | `src/agents/tools/common.ts:1-36` (`AnyAgentTool` / `AgentTool`)                     |
| `tools.py::ToolRegistry.dispatch`          | `src/agents/pi-embedded-subscribe.handlers.tools.ts`                                 |
| `tools.py::read_file` / `write_file`       | `src/agents/pi-tools.read.ts` / `src/agents/pi-tools.ts`                             |
| `tools.py::bash`                           | `src/agents/bash-tools.exec.ts:1309+` (`createExecTool`)                             |
| `approvals/exec_approvals.py`              | `src/infra/exec-approvals.ts::resolveExecApprovalsFromFile()`（加载 exec-approvals.json + 多层解析） |
| `approvals/types.py`                       | `src/infra/exec-approvals.types.ts`（ApprovalDecision / Request / Policy / AllowlistEntry） |
| `approvals/policy.py`                      | `src/infra/exec-approvals-allowlist.ts`（危险模式匹配 + allowlist 评估）                |
| `approvals/manager.py`                     | `src/gateway/exec-approval-manager.ts` + `src/infra/exec-approvals.ts`（请求生命周期 + allowlist 持久化） |
| `approvals/ui.py`                          | 审批提示 Rich UI（openclaw TUI 审批弹窗的简化版）                                       |
| `subagent/runner.py`                       | `src/agents/subagent/runner` / session orchestration（后台子 agent 生命周期的简化版）      |
| `subagent/registry.py`                     | OpenClaw 子 agent run registry（run 状态、session key、requester 关联）                   |
| `subagent/tools.py::sessions_spawn`        | OpenClaw `sessions_spawn` 子 agent 派生工具（nano 版只支持 isolated context）             |
| `subagent/announce.py`                     | 子 agent completion auto-announce（完成后作为 user message 回灌父 session）              |
| `prompt.py::build_system_prompt`           | `src/agents/system-prompt.ts:189+` & `pi-embedded-runner/system-prompt.ts:12-95`     |
| `workspace/loader.py`                      | `src/agents/workspace.ts`（bootstrap 文件加载 + budget 截断）                         |
| `workspace/cache.py`                       | `src/agents/workspace.ts`（session-scoped 缓存）                                      |
| `workspace/constants.py`                   | `src/agents/workspace.ts`（bootstrap 文件常量定义）                                   |
| `memory/daily.py::build_daily_memory_prelude` | `src/auto-reply/reply/startup-context.ts`（每日记忆文件加载 + 日期戳生成）             |
| `memory/tools.py::memory_get`              | `extensions/memory-core/src/tools.ts:memory_get`（读取记忆文件/片段）                  |
| `memory/tools.py::memory_search`           | `extensions/memory-core/src/tools.ts:memory_search`（搜索记忆文件，nano 用词法匹配 + 上下文窗口匹配 + 停用词过滤）    |
| `memory/tools.py::context_window_match`    | `extensions/memory-core/src/tools.ts`（上下文窗口匹配，提高记忆搜索相关度）           |
| `memory/tools.py::stopword_filter`         | `extensions/memory-core/src/tools.ts`（停用词过滤，避免无意义匹配）                   |
| `mcp/runtime.py`                           | `src/agents/pi-bundle-mcp-runtime.ts`（MCP 服务器连接管理 + 工具调用）               |
| `mcp/materialize.py`                       | `src/agents/pi-bundle-mcp-materialize.ts`（MCP 工具转换为 Agent 工具）               |
| `memory/active.py::ActiveMemoryManager`    | `extensions/active-memory/index.ts`（before_prompt_build hook + 子 agent 召回）       |
| `memory/active.py::QueryMode/PromptStyle`  | `extensions/active-memory/index.ts:17-34`（查询模式和召回风格枚举）                     |
| `memory/dreaming.py::track_recall`         | `extensions/memory-core/src/dreaming.ts`（记忆召回追踪 + cron 调度）                   |
| `memory/dreaming.py::run_light/deep_phase` | `extensions/memory-core/src/dreaming-phases.ts`（Light/Deep Phase 评分 + 提升）        |
| `web_search.py::web_search`                | `src/agents/tools/web-search.ts`（DuckDuckGo 搜索 + 缓存 + 外部内容包装）               |
| `web_fetch.py::web_fetch`                  | `src/agents/tools/web-fetch.ts`（HTML 提取 + readability + SSRF 防护 + 缓存）           |
| `ssrf_guard.py::assert_public_url`         | `src/infra/net/ssrf.ts`（两阶段 hostname 验证：预 DNS + 后 DNS）                        |
| `external_content.py::wrap_external_content` | `src/security/external-content.ts`（<EXTERNAL_UNTRUSTED_CONTENT> 边界标记 + LLM token 清洗） |
| `cli.py::repl`                             | `src/cli/tui-cli.ts:8-63` → `src/tui/tui.ts:1-52`                                    |
| `cli.py::_render_tool_result`              | `src/tui/components/tool-execution.ts:55-137`                                        |
| `session/types.py`                         | `src/config/sessions/types.ts`（SessionEntry 数据结构）                               |
| `session/paths.py`                         | `src/config/sessions/paths.ts`（Session 路径解析）                                    |
| `session/store.py`                         | `src/config/sessions/store.ts`（sessions.json 管理）                                  |
| `session/transcript.py`                    | `src/config/sessions/transcript.ts`（JSONL 读写）                                     |
| `session/truncate.py`                      | `src/agents/session-tool-result-guard.ts`（tool_result 截断）                         |
| `__main__.py`                              | `openclaw.mjs` → `src/entry.ts` → `src/run-main.ts`（合并三层）                       |

## 顺着循环读：推荐阅读顺序

1. **`config/types.py`** — 配置结构定义。Pydantic 类型验证，理解配置文件的 schema。
2. **`config/paths.py`** — 路径解析。`OPENCLAW_*` 环境变量处理、workspace 解析优先级。
3. **`config/io.py`** — 配置加载 + 模型解析。理解 `provider/model-id` 格式如何解析为 API 参数。
4. **`config/env_substitution.py`** — 环境变量替换。`${ENV_VAR}` 语法，递归遍历嵌套对象。
5. **`prompt.py`** — 我们告诉模型什么。简短，先建立"system prompt 是动态拼出来的"这个认知。
6. **`workspace/`** — Workspace 引导文件加载。从项目目录加载 AGENTS.md、SOUL.md 等 8 个标准文件，应用安全防护和预算截断，注入到系统提示中。先看 `constants.py` 了解文件列表，再看 `loader.py` 理解加载和截断逻辑，最后看 `cache.py` 理解 session-scoped 缓存。
7. **`memory/daily.py`** — Daily Memory 加载。理解如何扫描 `memory/` 目录，按日期加载每日记忆文件，生成 startup context prelude。
8. **`memory/tools.py`** — Memory 工具。`memory_get` 读取指定记忆文件，`memory_search` 搜索相关内容（nano 用词法匹配 + 上下文窗口匹配 + 停用词过滤，提升搜索相关度）。
9. **`memory/active.py`** — Active Memory 自动召回。理解 `before_prompt_build` hook 模式、子 agent 执行、QueryMode 和 PromptStyle 的含义。
10. **`memory/dreaming.py`** — Dreaming 后台记忆整合。理解 `track_recall` 追踪机制、cron 调度频率、Light/Deep Phase 评分逻辑、MEMORY.md 提升、Dream Diary 生成。状态存储在 `memory/.dreams/short-term-recall.json`。
11. **`mcp/`** — MCP 服务器集成。`runtime.py` 管理到 MCP 服务器的持久连接（支持 stdio/SSE/streamable-http 三种传输），`materialize.py` 将 MCP 工具转换为 nano-openclaw Tool 对象。启动时连接服务器，关闭时清理资源。
12. **`web_search.py`** — DuckDuckGo 网页搜索。理解缓存机制、结果格式化、外部内容安全包装。
13. **`web_fetch.py`** — URL 内容抓取。理解 HTML→markdown 转换、readability 提取、缓存、SSRF 防护。
14. **`ssrf_guard.py`** — SSRF 防护。理解两阶段 hostname 检查：预 DNS（字面 IP + 已知黑名单）+ 后 DNS（解析后验证私有 IP）。
15. **`external_content.py`** — 外部内容安全包装。理解 `<EXTERNAL_UNTRUSTED_CONTENT>` 边界标记和 LLM 特殊 token 清洗，防止 prompt injection。
16. **`tools.py`** — 模型能干什么。看 `Tool` 形状、内置工具（含 web_search/web_fetch 条件注册）、`dispatch` 永不抛异常的契约。包含 `session_status` 工具用于查询日期时间和会话上下文。
17. **`approvals/`** — 危险命令门禁。`policy.py` 评估风险，`manager.py` 管理审批请求和 per-agent allowlist 持久化，`ui.py` 渲染 Rich 提示（输入捕获暂停以避免与后台 Esc watcher 冲突）。`check_request()` 镜像 openclaw 的 `requiresExecApproval()`：`on-miss + allowlist + 未命中 = 提示用户`。
18. **`subagent/`** — 后台子 agent 编排。先看 `types.py` 理解 run/session key/status，再看 `registry.py` 的内存 run 表，接着看 `tools.py` 如何暴露 `sessions_spawn` 和 `subagents`，最后看 `runner.py` 如何用过滤后的 ToolRegistry 启动 isolated 子会话并把结果 auto-announce 回父会话。
19. **`images.py`** — 图片怎么处理。`parse_image_refs` 检测引用 → `load_image` 加载 → `describe_image` 双路径架构。
20. **`_stream_events.py`** — provider 协议契约。5 个 dataclass 是两个 transport 共同说的语言 + thinking 事件。
21. **`_provider_anthropic.py`** — Anthropic transport：SDK SSE 事件 → 5 个 dataclass + 原生 thinking 支持。
22. **`_provider_openai.py`** — OpenAI transport：同样翻译到 5 个 dataclass，顺带做消息格式转换 + reasoning_content 支持。
23. **`provider.py`** — 路由层：`switch(api)` 派发给正确的 transport，对外只暴露一个 `stream_response`。
24. **`compact.py`** — 上下文压缩：`estimate_tokens` → `compact_if_needed` → `summarize_history`。
25. **`loop.py`** — 把上面全部粘起来。这一步最关键，看完你就懂 agent 了。
26. **`session/paths.py`** — Session 路径解析。按 agent 隔离的 session 存储结构。
27. **`session/store.py`** — sessions.json 管理。
28. **`session/transcript.py`** — JSONL 转录文件读写。
29. **`session/truncate.py`** — tool_result 截断。
30. **`cli.py`** — 给人看的部分。理解 `on_event` 回调如何把"loop 内部状态"暴露给"渲染层"。包含 Windows msvcrt 原生 Esc watcher 作为 prompt_toolkit 不可用时的降级方案。
31. **`__main__.py`** — 入口装配。配置加载 → 模型解析 → LoopConfig 构建 → 启动 REPL。

## 三条不变量

读 `loop.py` 时记住这三句话：

1. 每一轮把**完整 history** 发回模型——若超出 token 预算，`compact_if_needed` 会先把旧消息替换成一条摘要，再发送压缩后的 history。
2. 多个 `tool_use` 并存时，所有结果合并成**一条** user 消息回灌。
3. 循环只在 `stop_reason != "tool_use"` 时终止；其它都是中间态。

Subagent 编排：模型可调用 `sessions_spawn` 启动 isolated 后台子 agent，适合复杂、慢、可并行的任务。子 agent 继承 workspace、模型和 thinking 默认值，可通过顶层 `subagents` 配置限制并发、超时和默认模型；它不会继承 `sessions_spawn` 等会话管理工具，避免递归派生。完成、失败或超时后，结果会自动作为一条 user message 注入父 session，模型不需要轮询；`subagents` 工具和 `/subagents` 命令只用于按需查看或 kill。后台子 agent 不能弹出前台审批 UI，任何需要交互审批的工具调用会默认拒绝，避免后台任务卡住 REPL。

图片处理遵循**双路径架构**：未配置 `image_model` 时走 Native Vision（图片直接发给主模型）；配置后走 Media Understanding（图片模型先描述，文字注入 prompt）。若主模型无视觉能力且未配置 `image_model`，图片会被跳过并显示警告。`parse_image_refs` 在循环入口处统一处理用户输入中的 `@file.png`、Markdown `![]()` 和 URL 引用。

MCP 工具集成：通过 `config.mcp.servers` 配置外部 MCP 服务器，启动时建立持久连接（支持 stdio/SSE/streamable-http 三种传输），工具自动注册到 ToolRegistry。服务器连接在后台 asyncio 线程中运行，REPL 退出时自动清理。取消的 agent turn（按 Esc）不会派发已触发的工具调用。

Web 工具集成：内置 `web_search`（DuckDuckGo 搜索）和 `web_fetch`（URL 内容抓取）工具，默认启用，可通过 `tools.web` 配置单独控制。所有外部内容通过 `<EXTERNAL_UNTRUSTED_CONTENT>` 边界标记包装并清洗 LLM 特殊 token，防止 prompt injection。`web_fetch` 带有 SSRF 两阶段防护（预 DNS 黑名单 + 后 DNS 私有 IP 验证），确保不会访问内部网络地址。搜索结果和抓取内容均有 10 分钟缓存。

Thinking 支持：通过 `agents.defaults.thinkingDefault` 配置思考等级（`off|minimal|low|medium|high|xhigh|adaptive|max`）。Anthropic provider 使用原生 thinking API；OpenAI-compatible provider 使用 `reasoning_content` 流。Thinking 块会持久化到消息历史（`thinking`/`redacted_thinking` 类型），CLI 以 dim 样式在 assistant 输出前渲染。当设置为 `off` 时，会显式发送 `{"type": "disabled"}` 给 API，以覆盖某些默认启用 thinking 的 provider（如 DashScope）。

Workspace 引导文件：从 `workspaceDir`（默认为当前工作目录）加载 8 个标准引导文件（AGENTS.md、SOUL.md、IDENTITY.md、USER.md、MEMORY.md、TOOLS.md、BOOTSTRAP.md、HEARTBEAT.md），应用安全防护（路径遍历检查、文件大小限制）和预算截断（`bootstrapBudget` 字段控制总 token 预算），注入到系统提示的项目上下文部分。支持 session-scoped 缓存，避免重复加载。配置文件的 `workspaceDir` 字段可自定义工作目录。

Memory 系统：包含四层机制：
- **Daily Memory**：启动时自动加载 `workspace/memory/*.md` 中最近 N 天的记忆文件（默认 2 天），生成日期戳 prelude 注入系统提示，让 agent 知道"最近发生了什么"。
- **Memory Tools**：提供 `memory_get`（读取指定记忆文件）和 `memory_search`（搜索记忆内容）两个工具，agent 可主动查询记忆。nano 用词法匹配而非 embedding 搜索。
- **Active Memory**：可选插件，启用后在每次用户消息前自动执行子 agent 搜索记忆，将相关结果注入系统提示，实现"自动记住偏好和历史"的效果。通过 `activeMemory` 配置字段启用。
- **Dreaming**：可选插件，启用后追踪 memory_search 的召回记录，定期将高频、高质量的记忆片段自动提升到 MEMORY.md（长期记忆），并生成叙事性的 Dream Diary 写入 DREAMS.md。通过 `dreaming` 配置字段启用。

Session Status 工具：内置 `session_status` 工具用于查询当前日期时间（避免模型凭空猜测）和会话上下文信息（模型 ID、session ID、上下文预算、token 使用量、压缩次数、消息计数）。工具结果由 `ToolRegistry` 注入会话上下文，模型可据此了解当前状态。

Subagent 试试：

```
>>> 让一个子 agent 单独阅读 README.md，总结这个项目的核心模块；你继续在当前会话里等待它完成即可
```

如果模型决定派生任务，会看到 `sessions_spawn` 工具面板；子 agent 完成后，CLI 会显示 completion announcement，并把结果回灌给当前会话。需要人工查看或停止后台任务时：

```bash
/subagents
/subagents all
/subagents kill <run_id>
```

## 端到端验证

试试这一句：

```
>>> 列出当前目录的文件，再读一下 pyproject.toml 的内容并简要总结
```

期望看到：先一个绿色的 `list_dir({"path":"."})` 面板，再一个 `read_file({"path":"pyproject.toml"})` 面板（长输出会被截到 12 行 + `(... +N more lines)` 脚注），最后模型给你一段总结后正常结束。

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

你也可以让工具读图片：

```
>>> 读取 images/ 目录下的 test.jpg 并描述它
```

`read_file` 会识别图片扩展名，返回图片内容块而非文本。

网络搜索试试：

```
>>> 搜索一下 Python 3.13 的新特性
```

`web_search` 会使用 DuckDuckGo 搜索返回标题、URL 和摘要，结果包裹在 `<EXTERNAL_UNTRUSTED_CONTENT>` 中。模型可以据此回复，也可以继续用 `web_fetch` 抓取具体页面：

```
>>> 打开 https://docs.python.org/3.13/whatsnew/3.13.html 看看详细内容
```

`web_fetch` 会提取页面可读内容并转换为 markdown，带有 SSRF 防护（阻止私有 IP 和 localhost 访问）。

## 显式 Non-Goals（被刻意砍掉的功能 = 进阶练习）

读懂 nano 之后，把以下任意一项加回去就是很棒的练习：

- 工具内 `onUpdate` 流式进度回调（看 `bash` 长输出实时滚动）
- `AbortSignal` / Ctrl-C 优雅取消运行中的工具
- Gemini / Vertex 等第三方 provider（仿 `_provider_openai.py` 再加一个 transport）
- 显式 prompt cache 控制（在 system prompt 上加 `cache_control`）
- CLI 参数回退（保留旧版 `--api`/`--model` 等参数作为配置文件的替代）

每完成一项，回到 OpenClaw 源码里看它真实的实现，对比你和它的设计差异——这就是从"会读"到"会写"的最快路径。

## License

MIT — 见 [LICENSE](./LICENSE)。
