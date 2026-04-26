# nano-openclaw

一个约 600 行的 Python 教学项目，用最少的代码复刻 OpenClaw 的 agent 运行原理。
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

# 运行交互 REPL（Anthropic，默认）
export ANTHROPIC_API_KEY=sk-ant-...
uv run python -m nano_openclaw

# 切换到 OpenAI
export OPENAI_API_KEY=sk-...
uv run python -m nano_openclaw --api openai --model gpt-4o

# 纯聊天模式（验证不带工具的循环）
uv run python -m nano_openclaw --no-tools

# 换模型
uv run python -m nano_openclaw --model claude-opus-4-5

# 开启上下文压缩：budget 设小一点方便触发
uv run python -m nano_openclaw --context-budget 8000 --context-threshold 0.7 --context-recent-turns 2
```

完整 CLI 参数一览：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--api` | `anthropic` | Provider：`anthropic` 或 `openai` |
| `--model` | 随 `--api` 变 | 模型 ID |
| `--no-tools` | — | 纯聊天，不注册任何工具 |
| `--max-iterations` | `12` | 每个用户轮次最多几轮 tool_use |
| `--max-tokens` | `4096` | 每次 assistant 响应的 token 上限 |
| `--context-budget` | `100000` | 上下文 token 预算（触发压缩的基准） |
| `--context-threshold` | `0.8` | 超过预算的多少比例时触发压缩 |
| `--context-recent-turns` | `3` | 压缩时保留的最近 N 轮（1 轮 = user + assistant） |

REPL 里支持的斜杠命令：`/quit`、`/clear`、`/help`。

## 60 秒架构图

```
                    ┌──────────────────────┐
   user types  ───▶ │   cli.repl()         │  rich-rendered REPL
                    └─────────┬────────────┘
                              │
                              ▼
                    ┌──────────────────────┐
                    │   loop.agent_loop()  │  the spine: messages, dispatch, recurse
                    └──┬──────────────┬────┘
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

_stream_events.py = 5 个共享 dataclass（两个 transport 的协议契约）
system prompt     = prompt.build_system_prompt(registry)
                    identity + cwd/platform/date + 工具清单
compact.py        = estimate_tokens → compact_if_needed → summarize_history
```

## 模块映射（nano ↔ OpenClaw）

| nano_openclaw 文件 / 符号                   | 对应的 OpenClaw 真实位置                                                              |
| ------------------------------------------ | ------------------------------------------------------------------------------------ |
| `loop.py::agent_loop`                      | `src/agents/pi-embedded-runner/run/attempt.ts:566` (`runEmbeddedAttempt`)            |
| 消息内容块结构                               | `src/agents/stream-message-shared.ts` (`AssistantMessage`)                           |
| `provider.py::stream_response`             | `src/agents/provider-transport-stream.ts`（switch(model.api) 路由层）                 |
| `_stream_events.py`                        | `src/agents/transport-stream-shared.ts`（共享事件类型契约）                            |
| `_provider_anthropic.py::stream_response`  | `src/agents/anthropic-transport-stream.ts:742+`（SSE → 归一化事件）                    |
| `_provider_openai.py::stream_response`     | `src/agents/openai-transport-stream.ts`（OpenAI → 归一化事件）                         |
| `_provider_openai._to_openai_messages`     | `src/agents/transport-message-transform.ts`（Anthropic↔OpenAI 格式转换）              |
| `compact.py::compact_if_needed`            | `src/agents/compaction.ts`（token 估算 + 摘要压缩旧消息）                              |
| `compact.py::summarize_history`            | `src/agents/compaction.ts`（调用 LLM 生成历史摘要）                                    |
| `tools.py::Tool` 数据类                     | `src/agents/tools/common.ts:1-36` (`AnyAgentTool` / `AgentTool`)                     |
| `tools.py::ToolRegistry.dispatch`          | `src/agents/pi-embedded-subscribe.handlers.tools.ts`                                 |
| `tools.py::read_file` / `write_file`       | `src/agents/pi-tools.read.ts` / `src/agents/pi-tools.ts`                             |
| `tools.py::bash`                           | `src/agents/bash-tools.exec.ts:1309+` (`createExecTool`)                             |
| `prompt.py::build_system_prompt`           | `src/agents/system-prompt.ts:189+` & `pi-embedded-runner/system-prompt.ts:12-95`     |
| `cli.py::repl`                             | `src/cli/tui-cli.ts:8-63` → `src/tui/tui.ts:1-52`                                    |
| `cli.py::_render_tool_result`              | `src/tui/components/tool-execution.ts:55-137`                                        |
| `__main__.py`                              | `openclaw.mjs` → `src/entry.ts` → `src/run-main.ts`（合并三层）                       |

## 顺着循环读：推荐阅读顺序

1. **`prompt.py`** — 我们告诉模型什么。简短，先建立"system prompt 是动态拼出来的"这个认知。
2. **`tools.py`** — 模型能干什么。看 `Tool` 形状、4 个内置工具、`dispatch` 永不抛异常的契约。
3. **`_stream_events.py`** — provider 协议契约。5 个 dataclass 是两个 transport 共同说的语言。
4. **`_provider_anthropic.py`** — Anthropic transport：SDK SSE 事件 → 5 个 dataclass。
5. **`_provider_openai.py`** — OpenAI transport：同样翻译到 5 个 dataclass，顺带做消息格式转换。
6. **`provider.py`** — 路由层：`switch(api)` 派发给正确的 transport，对外只暴露一个 `stream_response`。
7. **`compact.py`** — 上下文压缩：`estimate_tokens` → `compact_if_needed` → `summarize_history`。理解"什么时候、怎么把旧历史变成一条摘要消息"。
8. **`loop.py`** — 把上面全部粘起来。这一步最关键，看完你就懂 agent 了。
9. **`cli.py`** — 给人看的部分。理解 `on_event` 回调如何把"loop 内部状态"暴露给"渲染层"。
10. **`__main__.py`** — 入口装配。

## 三条不变量

读 `loop.py` 时记住这三句话：

1. 每一轮把**完整 history** 发回模型——若超出 token 预算，`compact_if_needed` 会先把旧消息替换成一条摘要，再发送压缩后的 history。
2. 多个 `tool_use` 并存时，所有结果合并成**一条** user 消息回灌。
3. 循环只在 `stop_reason != "tool_use"` 时终止；其它都是中间态。

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

## 显式 Non-Goals（被刻意砍掉的功能 = 进阶练习）

读懂 nano 之后，把以下任意一项加回去就是很棒的练习：

- 工具内 `onUpdate` 流式进度回调（看 `bash` 长输出实时滚动）
- `AbortSignal` / Ctrl-C 优雅取消运行中的工具
- Gemini / Vertex 等第三方 provider（仿 `_provider_openai.py` 再加一个 transport）
- MCP 客户端（从 `~/.openclaw/mcp.json` 加载外部工具到同一个 registry）
- 危险命令审批门禁（执行 `rm -rf` 前弹出 y/n）
- 显式 prompt cache 控制（在 system prompt 上加 `cache_control`）
- 会话持久化到 JSON + `--resume`
- Extended thinking / 思考块单独渲染
- 多模态输入（图片附件）

每完成一项，回到 OpenClaw 源码里看它真实的实现，对比你和它的设计差异——这就是从"会读"到"会写"的最快路径。

## 文件树

```
nano-openclaw/
├── README.md
├── LICENSE                     MIT
├── pyproject.toml              uv 管理；anthropic + openai + rich；dev: pytest
├── uv.lock                     锁定版本
├── .python-version             3.11
├── nano_openclaw/              Python 包（包名用下划线，符合 PEP 8）
│   ├── __init__.py
│   ├── __main__.py             入口；--api/--context-* 等 CLI 参数；构建 registry + LoopConfig
│   ├── prompt.py               build_system_prompt
│   ├── tools.py                Tool / ToolRegistry / 4 个内置工具
│   ├── _stream_events.py       5 个共享 StreamEvent dataclass（provider 协议契约）
│   ├── _provider_anthropic.py  Anthropic Messages API transport
│   ├── _provider_openai.py     OpenAI Chat Completions transport + 消息格式转换
│   ├── provider.py             路由层：switch(api) → 对应 transport
│   ├── compact.py              上下文压缩：estimate_tokens / summarize_history / compact_if_needed
│   ├── loop.py                 agent_loop（spine）；LoopConfig 含 api + context_* 字段
│   └── cli.py                  rich REPL + 工具调用面板渲染 + 压缩提示
└── tests/
    ├── test_tools.py           工具注册/派发单测（无需 API key）
    ├── test_provider.py        消息格式转换、路由、LoopConfig 单测（无需 API key）
    └── test_compact.py         token 估算、阈值检查、压缩逻辑单测（无需 API key）
```

## License

MIT — 见 [LICENSE](./LICENSE)。
