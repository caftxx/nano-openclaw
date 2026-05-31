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

## Development Principles

- **测试覆盖** — 完成新功能或 bugfix 后，必须补充测试覆盖对应改动（新功能补正向用例，bugfix 补回归用例）。
- **平台无关** — 本项目纯python实现支持运行在 Windows / Linux / macOS，代码和测试都必须保证平台无关实现：避开 Unix-only API，路径用 `pathlib` / `os.path` 而非硬编码分隔符，不依赖特定平台的 shell 行为或文件权限语义。
- **全量测试** — 提交代码前需要确保全量测试通过

## Debug

`NANO_DEBUG_PROMPT=1` 会把每次 API 请求的完整 payload 写入 `nano-openclaw-debug.jsonl`。

`gateway status` 输出包含 RPC probe（health / runtime.get / channels.status），daemon 异常时降级显示 `rpc probe: timed out` 但基础信息（pid + port + log path）仍输出。
