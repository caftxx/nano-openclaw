# nano-openclaw

一个约 600 行的 Python 教学项目，用最少的代码复刻 OpenClaw 的 agent 运行原理。
精神类比 [nanoGPT](https://github.com/karpathy/nanoGPT) 之于 GPT：**真实可跑，但只保留骨架，删掉一切可选层**。

读完这个仓库里的 6 个 `.py` 文件，你就理解了一个"会用工具的 LLM agent"的全部秘密。

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

# 运行交互 REPL（需要 ANTHROPIC_API_KEY）
export ANTHROPIC_API_KEY=sk-ant-...
uv run python -m nano_openclaw

# 纯聊天模式（验证不带工具的循环）
uv run python -m nano_openclaw --no-tools

# 换模型
uv run python -m nano_openclaw --model claude-opus-4-5
```

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
                    └────┬───────────┬─────┘
            stream events│           │tool_use blocks
                         ▼           ▼
                ┌──────────────┐  ┌──────────────────────┐
                │ provider.py  │  │  tools.dispatch()    │
                │  (Anthropic) │  │  read/write/list/bash│
                └──────┬───────┘  └────────┬─────────────┘
                       │                    │
                       ▼                    ▼
                ┌──────────────────────────────────────┐
                │     Anthropic Messages API           │
                └──────────────────────────────────────┘

system prompt = prompt.build_system_prompt(registry)
   identity + cwd/platform/date + 工具清单
```

每轮发送完整 history；多个 `tool_use` 的结果统一打包成**一条** user 消息回灌。

## 模块映射（nano ↔ OpenClaw）

| nano_openclaw 文件 / 符号             | 对应的 OpenClaw 真实位置                                                              |
| ------------------------------------ | ------------------------------------------------------------------------------------ |
| `loop.py::agent_loop`                | `src/agents/pi-embedded-runner/run/attempt.ts:566` (`runEmbeddedAttempt`)            |
| 消息内容块结构                         | `src/agents/stream-message-shared.ts` (`AssistantMessage`)                           |
| `provider.py::stream_response`       | `src/agents/anthropic-transport-stream.ts:742+`（SSE → 归一化事件）                    |
| `tools.py::Tool` 数据类               | `src/agents/tools/common.ts:1-36` (`AnyAgentTool` / `AgentTool`)                     |
| `tools.py::ToolRegistry.dispatch`    | `src/agents/pi-embedded-subscribe.handlers.tools.ts`                                 |
| `tools.py::read_file` / `write_file` | `src/agents/pi-tools.read.ts` / `src/agents/pi-tools.ts`                             |
| `tools.py::bash`                     | `src/agents/bash-tools.exec.ts:1309+` (`createExecTool`)                             |
| `prompt.py::build_system_prompt`     | `src/agents/system-prompt.ts:189+` & `pi-embedded-runner/system-prompt.ts:12-95`     |
| `cli.py::repl`                       | `src/cli/tui-cli.ts:8-63` → `src/tui/tui.ts:1-52`                                    |
| `cli.py::_render_tool_result`        | `src/tui/components/tool-execution.ts:55-137`                                        |
| `__main__.py`                        | `openclaw.mjs` → `src/entry.ts` → `src/run-main.ts`（合并三层）                       |

## 顺着循环读：推荐阅读顺序

1. **`prompt.py`** — 我们告诉模型什么。简短，先建立"system prompt 是动态拼出来的"这个认知。
2. **`tools.py`** — 模型能干什么。看 `Tool` 形状、4 个内置工具、`dispatch` 永不抛异常的契约。
3. **`provider.py`** — 我们怎么和 API 对话。看 SDK 的 SSE 事件如何被翻译成 5 个本地 dataclass。
4. **`loop.py`** — 把上面三个粘起来。这一步最关键，看完你就懂 agent 了。
5. **`cli.py`** — 给人看的部分。理解 `on_event` 回调如何把"loop 内部状态"暴露给"渲染层"。
6. **`__main__.py`** — 入口装配。

## 三条不变量

读 `loop.py` 时记住这三句话：

1. 每一轮把**完整 history** 发回模型（nano 不做截断/压缩）。
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
- 多 provider 抽象（让 OpenAI/Gemini 走同一个 `stream_response`）
- MCP 客户端（从 `~/.openclaw/mcp.json` 加载外部工具到同一个 registry）
- 危险命令审批门禁（执行 `rm -rf` 前弹出 y/n）
- 显式 prompt cache 控制（在 system prompt 上加 `cache_control`）
- 会话持久化到 JSON + `--resume`
- Extended thinking / 思考块单独渲染
- 上下文压缩（messages 超过 N 时摘要旧消息）
- 多模态输入（图片附件）

每完成一项，回到 OpenClaw 源码里看它真实的实现，对比你和它的设计差异——这就是从"会读"到"会写"的最快路径。

## 文件树

```
nano-openclaw/
├── README.md
├── LICENSE                 MIT
├── pyproject.toml          uv 管理；anthropic + rich + pytest(dev)
├── uv.lock                 锁定版本
├── .python-version         3.11
├── nano_openclaw/          Python 包（包名用下划线，符合 PEP 8）
│   ├── __init__.py
│   ├── __main__.py         入口；构建 registry 后交给 cli.repl
│   ├── prompt.py           build_system_prompt
│   ├── tools.py            Tool / ToolRegistry / 4 个内置工具
│   ├── provider.py         Anthropic 流式 → 归一化事件 iterator
│   ├── loop.py             agent_loop（spine）
│   └── cli.py              rich REPL + 工具调用面板渲染
└── tests/
    └── test_tools.py       本地工具单测（无需 API key）
```

## License

MIT — 见 [LICENSE](./LICENSE)。
