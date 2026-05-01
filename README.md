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

**配置示例**：复制 `nano-openclaw-example.json5` 并根据你的 provider 修改，详见下方配置说明。

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

支持的斜杠命令：`/quit`、`/clear`、`/new`、`/help`、`/context`、`/compact`、`/sessions`、`/save`。

- `/new` — 硬重置：生成全新 session ID，创建新 .jsonl transcript 文件，更新 session store（匹配 OpenClaw 语义）
- `/clear` — 仅清空内存历史，重置 transcript 文件保留 session header，立即更新 session metadata

## 60 秒架构图

```
                      ┌──────────────────────┐
    user types  ───▶ │   cli.repl()         │  rich-rendered REPL
                      └─────────┬────────────┘
                                │
                                ▼
                      ┌──────────────────────┐
    image refs ─────▶ │   loop.agent_loop()  │  parse_image_refs → load_image
    (@file.png)       └──┬──────────────┬────┘
          compact check │              │ tool_use blocks
                       ▼              ▼
              ┌──────────────┐  ┌──────────────────────┐
              │  compact.py  │  │  tools.dispatch()    │
              │  token est.  │  │  read/write/list/bash│
              │  summarize   │  └────────┬─────────────┘
              └──────┬───────┘           │
      history shrunk │    stream events  │
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
                      identity + cwd/platform/date + 工具清单
  compact.py        = estimate_tokens → compact_if_needed → summarize_history
  images.py         = parse_image_refs → load_image → describe_image（双路径架构）
  session/          = transcript 持久化（.jsonl）+ sessions.json 索引 + 8KB 截断 + store-first 初始化
  thinking          = Anthropic 原生 thinking / OpenAI reasoning_content → dim 样式渲染 → 持久化到消息历史
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
| `prompt.py::build_system_prompt`           | `src/agents/system-prompt.ts:189+` & `pi-embedded-runner/system-prompt.ts:12-95`     |
| `workspace/loader.py`                      | `src/agents/workspace.ts`（bootstrap 文件加载 + budget 截断）                         |
| `workspace/cache.py`                       | `src/agents/workspace.ts`（session-scoped 缓存）                                      |
| `workspace/constants.py`                   | `src/agents/workspace.ts`（bootstrap 文件常量定义）                                   |
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
7. **`tools.py`** — 模型能干什么。看 `Tool` 形状、4 个内置工具、`dispatch` 永不抛异常的契约。包含新增的 `session_status` 工具用于查询日期时间和会话上下文。
8. **`images.py`** — 图片怎么处理。`parse_image_refs` 检测引用 → `load_image` 加载 → `describe_image` 双路径架构。
9. **`_stream_events.py`** — provider 协议契约。5 个 dataclass 是两个 transport 共同说的语言 + thinking 事件。
10. **`_provider_anthropic.py`** — Anthropic transport：SDK SSE 事件 → 5 个 dataclass + 原生 thinking 支持。
11. **`_provider_openai.py`** — OpenAI transport：同样翻译到 5 个 dataclass，顺带做消息格式转换 + reasoning_content 支持。
12. **`provider.py`** — 路由层：`switch(api)` 派发给正确的 transport，对外只暴露一个 `stream_response`。
13. **`compact.py`** — 上下文压缩：`estimate_tokens` → `compact_if_needed` → `summarize_history`。
14. **`loop.py`** — 把上面全部粘起来。这一步最关键，看完你就懂 agent 了。
15. **`session/paths.py`** — Session 路径解析。按 agent 隔离的 session 存储结构。
16. **`session/store.py`** — sessions.json 管理。
17. **`session/transcript.py`** — JSONL 转录文件读写。
18. **`session/truncate.py`** — tool_result 截断。
19. **`cli.py`** — 给人看的部分。理解 `on_event` 回调如何把"loop 内部状态"暴露给"渲染层"。
20. **`__main__.py`** — 入口装配。配置加载 → 模型解析 → LoopConfig 构建 → 启动 REPL。

## 三条不变量

读 `loop.py` 时记住这三句话：

1. 每一轮把**完整 history** 发回模型——若超出 token 预算，`compact_if_needed` 会先把旧消息替换成一条摘要，再发送压缩后的 history。
2. 多个 `tool_use` 并存时，所有结果合并成**一条** user 消息回灌。
3. 循环只在 `stop_reason != "tool_use"` 时终止；其它都是中间态。

图片处理遵循**双路径架构**：未配置 `image_model` 时走 Native Vision（图片直接发给主模型）；配置后走 Media Understanding（图片模型先描述，文字注入 prompt）。若主模型无视觉能力且未配置 `image_model`，图片会被跳过并显示警告。`parse_image_refs` 在循环入口处统一处理用户输入中的 `@file.png`、Markdown `![]()` 和 URL 引用。

Thinking 支持：通过 `agents.defaults.thinkingDefault` 配置思考等级（`off|minimal|low|medium|high|xhigh|adaptive|max`）。Anthropic provider 使用原生 thinking API；OpenAI-compatible provider 使用 `reasoning_content` 流。Thinking 块会持久化到消息历史（`thinking`/`redacted_thinking` 类型），CLI 以 dim 样式在 assistant 输出前渲染。当设置为 `off` 时，会显式发送 `{"type": "disabled"}` 给 API，以覆盖某些默认启用 thinking 的 provider（如 DashScope）。

Workspace 引导文件：从 `workspaceDir`（默认为当前工作目录）加载 8 个标准引导文件（AGENTS.md、SOUL.md、IDENTITY.md、USER.md、MEMORY.md、TOOLS.md、BOOTSTRAP.md、HEARTBEAT.md），应用安全防护（路径遍历检查、文件大小限制）和预算截断（`bootstrapBudget` 字段控制总 token 预算），注入到系统提示的项目上下文部分。支持 session-scoped 缓存，避免重复加载。配置文件的 `workspaceDir` 字段可自定义工作目录。

Session Status 工具：内置 `session_status` 工具用于查询当前日期时间（避免模型凭空猜测）和会话上下文信息（模型 ID、session ID、上下文预算、token 使用量、压缩次数、消息计数）。工具结果由 `ToolRegistry` 注入会话上下文，模型可据此了解当前状态。

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

## 显式 Non-Goals（被刻意砍掉的功能 = 进阶练习）

读懂 nano 之后，把以下任意一项加回去就是很棒的练习：

- 工具内 `onUpdate` 流式进度回调（看 `bash` 长输出实时滚动）
- `AbortSignal` / Ctrl-C 优雅取消运行中的工具
- Gemini / Vertex 等第三方 provider（仿 `_provider_openai.py` 再加一个 transport）
- MCP 客户端（从 `~/.openclaw/mcp.json` 加载外部工具到同一个 registry）
- 危险命令审批门禁（执行 `rm -rf` 前弹出 y/n）
- 显式 prompt cache 控制（在 system prompt 上加 `cache_control`）
- CLI 参数回退（保留旧版 `--api`/`--model` 等参数作为配置文件的替代）

每完成一项，回到 OpenClaw 源码里看它真实的实现，对比你和它的设计差异——这就是从"会读"到"会写"的最快路径。

## 文件树

```
nano-openclaw/
├── README.md
├── docs/
│   ├── CONFIG_EXAMPLE.md   配置字段完整说明（每个字段的含义和示例）
│   └── reference/
│       └── templates/      Workspace 引导文件模板（8 个标准文件）
│           ├── AGENTS.md        Agent 行为指南模板
│           ├── AGENTS.dev.md    开发环境 Agent 指南模板
│           ├── SOUL.md          Agent 价值观模板
│           ├── SOUL.dev.md      开发环境价值观模板
│           ├── IDENTITY.md      Agent 身份模板
│           ├── IDENTITY.dev.md  开发环境身份模板
│           ├── USER.md          用户信息模板
│           ├── USER.dev.md      开发环境用户信息模板
│           ├── MEMORY.md        记忆存储模板
│           ├── TOOLS.md         工具使用指南模板
│           ├── TOOLS.dev.md     开发环境工具指南模板
│           ├── BOOTSTRAP.md     启动引导模板
│           ├── BOOT.md          启动命令模板
│           └── HEARTBEAT.md      心跳检查模板
├── nano-openclaw-example.json5  配置示例模板（复制为 nano-openclaw.json5 使用）
├── LICENSE                   MIT
├── pyproject.toml            uv 管理；anthropic + openai + rich + pillow + json5 + pydantic
├── uv.lock                   锁定版本
├── .python-version           3.11
├── nano_openclaw/            Python 包（包名用下划线，符合 PEP 8）
│   ├── __init__.py
│   ├── __main__.py           入口；配置加载 → 模型解析 → LoopConfig 构建 → 启动 REPL
│   ├── config/               配置系统（对齐 OpenClaw 路径解析）
│   │   ├── __init__.py       公开接口
│   │   ├── types.py          Pydantic 配置类型（AgentsConfig, ModelsConfig, SessionConfig 等）
│   │   ├── io.py             JSON5 文件加载、模型解析、API key 解析
│   │   ├── paths.py          路径解析（OPENCLAW_* 环境变量 + workspace 解析）
│   │   └── env_substitution.py ${ENV_VAR} 环境变量替换（递归遍历嵌套对象）
│   ├── prompt.py             build_system_prompt
│   ├── tools.py              Tool / ToolRegistry / 5 个内置工具（read_file 支持图片、session_status 查询会话上下文）
│   ├── images.py             parse_image_refs / load_image / describe_image（双路径）
│   ├── workspace/            Workspace 引导文件管理模块
│   │   ├── __init__.py       公开接口
│   │   ├── constants.py      引导文件常量定义（8 个标准文件名、预算配置）
│   │   ├── loader.py         load_workspace_bootstrap_files / build_bootstrap_context / trim_bootstrap_content
│   │   └── cache.py          session-scoped 缓存（get_or_load_bootstrap_files）
│   ├── _stream_events.py     5 个共享 StreamEvent dataclass + thinking 事件
│   ├── _provider_anthropic.py Anthropic Messages API transport + 原生 thinking
│   ├── _provider_openai.py   OpenAI Chat Completions transport + reasoning_content
│   ├── provider.py           路由层：switch(api) → 对应 transport
│   ├── compact.py            上下文压缩：estimate_tokens / summarize_history / compact_if_needed
│   ├── loop.py               agent_loop（spine）
│   ├── cli.py                rich REPL + 工具面板 + 会话管理
│   └── session/              会话持久化模块（按 agent 隔离）
│       ├── __init__.py       公开接口
│       ├── types.py          数据类：SessionHeader, TranscriptMessage, TranscriptCompaction
│       ├── paths.py          Session 路径解析（{stateDir}/agents/{id}/sessions/）
│       ├── store.py          sessions.json 管理
│       ├── transcript.py     JSONL 读写
│       └── truncate.py       tool_result 截断（8KB）
└── tests/
    ├── test_tools.py           工具注册/派发单测（包含 session_status 测试）
    ├── test_provider.py        消息格式转换、路由单测
    ├── test_compact.py         token 估算、压缩单测
    ├── test_config_types.py    Pydantic 配置类型验证单测
    ├── test_config_io.py       配置文件加载和模型解析单测
    ├── test_config_paths.py    路径解析单测（环境变量、workspace）
    ├── test_session_paths.py   Session 路径隔离单测
    ├── test_env_substitution.py 环境变量替换单测
    ├── test_session.py         会话持久化单测
    └── test_workspace.py       Workspace 引导文件加载、缓存、预算截断单测
```

## License

MIT — 见 [LICENSE](./LICENSE)。
