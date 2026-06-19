# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

使用 `uv` 进行依赖管理和运行。CLI 顶层暴露 `tui` / `gateway` / `wechat` 子命令；`--config` / `--agent` 走环境变量（`NANO_OPENCLAW_CONFIG_PATH` / `NANO_OPENCLAW_STATE_DIR`）和 config 文件。

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

# Daemon 管理 —— daemon 内组合 WebUI + /rpc + external channels + scheduler + subagent
uv run nano-openclaw gateway start             # detached 后台
uv run nano-openclaw gateway start --port 8080 # 覆盖 config 端口
uv run nano-openclaw gateway status            # 多行结构化报告（含 RPC probe）
uv run nano-openclaw gateway stop
uv run nano-openclaw gateway run               # 前台模式（systemd / docker 用）

# WeChat 是 external channel，需先扫码登录；WebUI/TUI 不算 channel
uv run nano-openclaw wechat login

# 顶层 back-compat flags（forward 到 tui）
uv run nano-openclaw --resume
uv run nano-openclaw --sessions

# 指定配置文件走环境变量（CLI 不再有 --config 顶层 flag）
NANO_OPENCLAW_CONFIG_PATH=./my-config.json5 uv run nano-openclaw
```

测试不需要 API key，纯本地工具单测。

Windows / sandbox 注意：如果默认 `%TEMP%\pytest-of-*` 或 `.pytest_cache` 无权限，可用仓库内临时目录跑测试：

```bash
uv run pytest tests/ --basetemp .pytest-tmp
```

## Architecture Notes

当前代码按五层组织，依赖方向只能向下：

```text
daemon / adapters / api
          ↓
       services
          ↓
        core
          ↓
 config, session primitives, provider SDKs, filesystem primitives
```

- `core/`：纯 agent 内核，包含 `loop.py`、provider、tools、prompt、compact、attachments/images、runtime primitives。不得 import `daemon`、`api`、`adapters`。
- `services/`：产品行为边界，包含 `BackendService`、embedded backend、sessions/runs、approvals、runtime update、slash、channels。TUI/WebUI/WeChat 都只能通过 backend/service 进入 agent。
- `api/`：`/rpc` WebSocket wire protocol 和 method handlers，只做参数/返回值转换，不放业务逻辑。
- `adapters/`：CLI/TUI、WebUI/voice、外部 channel adapters（WeChat）。WebUI/TUI 是 frontend adapters，不是 channel；`/channels` 只列 WeChat 这类外部消息通道。
- `daemon/`：进程生命周期和组合：pidfile、start/stop/status/run、TLS、WebUI、RPC route、channel manager、scheduler。
- `features/`：memory、skills、subagents、schedule、checkpoint、web、mcp、review_fork、voice 等能力归属。Slash 命令由 feature 注册到 `services.slash`，dispatcher 不承载业务实现。
- `plugins/`：窄注册面，插件通过 `PluginApi` 注册 tool/hook/slash/channel/feature，不直接修改 runtime 内部状态。

Recent UX contracts to preserve:

- Remote TUI 默认新建 session；只有 `--resume` 才接 daemon last session。
- WebUI 输入框 `Enter` 发送，`Shift+Enter` 换行。
- WebUI user bubble 不显示内部附件描述块；图片描述失败时后端要回退到 native vision 或显式注入处理失败上下文。
- WebUI session row hover/focus 显示 `×` 删除按钮；删除走 websocket `session.delete` 并刷新到剩余 session。

## Development Principles

- **测试覆盖** — 完成新功能或 bugfix 后，必须补充测试覆盖对应改动（新功能补正向用例，bugfix 补回归用例）。
- **平台无关** — 本项目纯python实现支持运行在 Windows / Linux / macOS，代码和测试都必须保证平台无关实现：避开 Unix-only API，路径用 `pathlib` / `os.path` 而非硬编码分隔符，不依赖特定平台的 shell 行为或文件权限语义。
- **全量测试** — 提交代码前需要确保全量测试通过

## Debug

`NANO_DEBUG_PROMPT=1` 会把每次 API 请求的完整 payload 写入 `nano-openclaw-debug.jsonl`。

`gateway status` 输出包含 RPC probe（health / runtime.get / channels.status），daemon 异常时降级显示 `rpc probe: timed out` 但基础信息（pid + port + log path）仍输出。
