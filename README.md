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

# 编辑 `.nano-openclaw-dev/nano-openclaw.json5`，填入你的 API key 和 provider 信息
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

# 启动 WebUI（浏览器界面，默认 http://127.0.0.1:8765）
uv run python -m nano_openclaw web

# WebUI 指定端口和地址
uv run python -m nano_openclaw web --port 8765 --host 127.0.0.1

# 启动微信机器人（通过 iLink API）
uv run python -m nano_openclaw wechat
```

**WebUI**：运行 `uv run python -m nano_openclaw web` 后在浏览器打开 `http://127.0.0.1:8765`。

---

### Docker Compose 启动

不想装 Python 环境？用 Docker Compose 一键启动：

```bash
# 复制环境变量模板并填入 API key
cp .env.example .env

# web 模式（后台运行，访问 http://localhost:8765）
docker compose --profile web up -d

# CLI 模式（交互式终端）
docker compose run --rm cli

# 停止 web 服务
docker compose --profile web down
```

**端口**：默认 `8765`，通过 `.env` 里的 `WEB_PORT` 修改宿主机端口：

```bash
WEB_PORT=9000 docker compose --profile web up -d
# 访问 http://localhost:9000
```

**Volume 映射**：

| 宿主机路径 | 容器路径 | 用途 |
| --- | --- | --- |
| `./.nano-openclaw-dev/` | `/root/.nano-openclaw/` | 会话、配置、记忆等状态数据 |

配置文件放在 `.nano-openclaw-dev/nano-openclaw.json5` 即可自动加载，无需额外参数。容器中agent 的工作目录由配置文件中的 `workspaceDir` 决定，默认为 `~/.nano-openclaw/workspace/`。WebUI 支持斜杠命令、thinking 开关、图片/文件附件、活动历史回放、亮色/暗色/跟随系统主题，移动端自适应。

配置详解见 [CONFIG_EXAMPLE.md](docs/CONFIG_EXAMPLE.md)。

## 配置文件

配置文件采用 JSON5 格式（支持注释和尾逗号），路径按优先级查找：

1. `--config <path>` — 命令行显式指定
2. `$NANO_OPENCLAW_CONFIG_PATH` — 环境变量
3. `{stateDir}/nano-openclaw.json5` — 状态目录下
4. `{cwd}/workspace/nano-openclaw.json5` — 项目 workspace 目录
5. `~/.nano-openclaw/nano-openclaw.json5` — 用户全局配置

状态目录 (`stateDir`) 解析：`$NANO_OPENCLAW_STATE_DIR` > `{cwd}/.nano-openclaw` > `~/.nano-openclaw`

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

## 日志系统

支持结构化 JSON Lines 日志，通过环境变量或配置文件控制：

```bash
# 通过环境变量设置日志等级
NANO_LOG_LEVEL=DEBUG uv run python -m nano_openclaw

# 或在配置文件中设置
# config.logging.level = "INFO"
```

日志文件位于 `{stateDir}/logs/` 目录，支持：
- JSON Lines 格式（每行一个 JSON 对象）
- 自动轮转（单文件超过 10MB 时滚动）
- Gzip 压缩（旧日志自动压缩）
- 上下文注入（session_id、run_id、tool_call_id 自动关联）

日志等级：`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`（默认 `WARNING`）。

---

## 微信机器人

通过 `wechat` 子命令启动微信机器人，使用 iLink API 长轮询接入：

```bash
uv run python -m nano_openclaw wechat
```

**配置**（在 `nano-openclaw.json5` 中）：

```json5
{
  wechat: {
    ilink_token: "xxx"
  }
}
```

> `ilink_token` 获取方式：先用openclaw或hermes连接微信bot，然后从openclaw或hermes的token文件中读取

**特性**：
- **Per-user Session 隔离** — 每个微信用户独立 session，历史不混淆
- **Typing Indicator** — 处理消息时自动发送"正在输入"状态
- **图片处理** — 自动下载/解密微信图片，支持 Vision 模型分析
- **Vision Fallback** — 模型无视觉能力时返回文本占位符

---

## REPL 斜杠命令

支持的斜杠命令：`/quit`、`/clear`、`/new`、`/help`、`/context`、`/compact`、`/sessions`、`/save`、`/skills`、`/plugins`、`/hooks`、`/subagents`。

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
    image refs ─────▶ │ AgentRuntime.run_turn│  parse_image_refs → load_image
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

  runtime.py        = AgentRuntime 共享模块 + CLI 子命令入口（web/wechat）+ build_agent_runtime 工厂
  logger.py         = JSON Lines 日志系统 + 轮转/Gzip 压缩 + contextvars 注入（session_id/run_id/tool_call_id）
  wechat/           = 微信机器人集成（iLink API 长轮询 + per-user session 隔离 + typing indicator + 图片解密）
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
- **Memory Tools**：提供 `memory_get`（读取指定记忆文件）和 `memory_search`（搜索记忆内容）两个工具，agent 可主动查询记忆。nano 用词法匹配而非 embedding 搜索。`memorySearch.temporalDecay` 可显式开启按时间衰减；默认关闭以对齐 openclaw。
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
