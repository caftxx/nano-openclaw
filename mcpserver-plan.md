# 实现方案：nano-openclaw 接入 MCP Server

## Context

openclaw 通过 `SessionMcpRuntime` 管理 MCP Server 连接，将 MCP 工具动态注册到 agent 工具列表中。nano-openclaw 目前只有内置工具（read_file/write_file/bash 等），缺少 MCP 扩展能力。本方案将 openclaw 的 MCP 集成逻辑移植到 Python，让用户能在 `nano-openclaw.json5` 中配置 MCP Server，工具自动注册到 ToolRegistry 供 agent 调用。

---

## 核心设计

**异步桥接**：MCP Python SDK 是 async，nano-openclaw 是同步代码。方案使用 **后台 asyncio 线程 + `run_coroutine_threadsafe`** 桥接，保持 MCP 连接持久化（与 openclaw SessionMcpRuntime 一致），不在每次工具调用时重新建立连接。

**工具命名**：采用 `{server}__{tool}` 格式，与 openclaw `buildSafeToolName()` 一致。

**传输支持**：stdio（子进程）、SSE（HTTP Server-Sent Events）、streamable-http，与 openclaw 的三种传输模式对应。

---

## 变更文件

### 1. `nano_openclaw/config/types.py`（修改）

在文件末尾、`NanoOpenClawConfig` 之前，添加两个 Pydantic 类型：

```python
class McpServerConfig(BaseModel):
    """MCP server 配置（对应 openclaw types.mcp.ts McpServerConfig）。"""
    model_config = ConfigDict(populate_by_name=True)
    # Stdio transport
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, Union[str, int, bool]]] = None
    cwd: Optional[str] = None
    workingDirectory: Optional[str] = None
    # HTTP transport
    url: Optional[str] = None
    transport: Optional[Literal["sse", "streamable-http"]] = None
    headers: Optional[Dict[str, Union[str, int, bool]]] = None
    connectionTimeoutMs: Optional[int] = Field(default=None)

class McpConfig(BaseModel):
    """MCP 全局配置（对应 openclaw McpConfig）。"""
    model_config = ConfigDict(populate_by_name=True)
    servers: Dict[str, McpServerConfig] = Field(default_factory=dict)
    sessionIdleTtlMs: Optional[int] = Field(default=None)
```

在 `NanoOpenClawConfig` 中新增字段：

```python
mcp: McpConfig = Field(default_factory=McpConfig)
```

---

### 2. 新建目录 `nano_openclaw/mcp/`

#### `nano_openclaw/mcp/__init__.py`
空文件。

#### `nano_openclaw/mcp/runtime.py`

`McpRuntime` 类，对应 openclaw `pi-bundle-mcp-runtime.ts`：

```
职责：
- 在后台 asyncio 线程中持久化管理各 server 连接
- 每个 server 对应一个长驻 asyncio Task，保持 ClientSession 上下文管理器开放
- initialize() 阻塞直到所有 server 连接就绪（或超时跳过失败 server）
- call_tool(server_name, tool_name, args) 同步调用，返回文本结果
- get_mcp_tools() 返回 List[McpToolInfo]（server_name, tool_name, description, input_schema）
- close() 发送 shutdown 信号，等待后台线程退出

连接类型判断：
- cfg.command → stdio_client(StdioServerParameters)
- cfg.transport == "streamable-http" → streamablehttp_client(url, headers)
- cfg.url → sse_client(url, headers)

依赖：mcp Python SDK (pip install mcp)
```

关键实现模式（保持 context manager 开放）：
```python
async def _run_server(self, name, cfg, ready):
    async with stdio_client(...) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            self._sessions[name] = session
            self._tool_infos.extend(...)
            ready.set()
            await self._shutdown.wait()  # 阻塞直到 close()
```

#### `nano_openclaw/mcp/materialize.py`

`materialize_mcp_tools(runtime, existing_names)` 函数，对应 openclaw `pi-bundle-mcp-materialize.ts`：

```
输入：McpRuntime 实例 + 已有工具名集合（避免冲突）
输出：List[Tool]（可直接 registry.register()）

每个 Tool 的 run 函数是 runtime.call_tool() 的同步包装。
工具名称：re.sub(r'[^a-zA-Z0-9_]', '_', server) + '__' + re.sub(r'[^a-zA-Z0-9_]', '_', tool)
description 格式：[MCP:server_name] {原始 description}
```

---

### 3. `nano_openclaw/__main__.py`（修改）

在 `registry = ...` 之后、`repl(...)` 之前，插入 MCP 初始化逻辑：

```python
mcp_runtime = None
if not config.noTools and config.mcp.servers:
    from nano_openclaw.mcp.runtime import McpRuntime
    from nano_openclaw.mcp.materialize import materialize_mcp_tools
    mcp_runtime = McpRuntime()
    mcp_runtime.initialize(config.mcp.servers)
    mcp_tools = materialize_mcp_tools(mcp_runtime, existing_names=set(registry.names()))
    for tool in mcp_tools:
        registry.register(tool)
    print(f"MCP: loaded {len(mcp_tools)} tools from {len(config.mcp.servers)} server(s)", file=sys.stderr)
```

在 `repl()` 返回后（在 `main()` 末尾）执行清理：

```python
if mcp_runtime:
    mcp_runtime.close()
```

---

## 配置示例

用户在 `~/.openclaw/nano-openclaw.json5` 中添加：

```json5
{
  "mcp": {
    "servers": {
      "context7": {
        "command": "uvx",
        "args": ["context7-mcp"]
      },
      "remote": {
        "url": "http://localhost:3000/mcp",
        "transport": "sse"
      }
    }
  }
}
```

启动后工具列表会出现 `context7__resolve_library_id`、`context7__get_library_docs` 等 MCP 工具。

---

## 依赖

`pyproject.toml` 添加：

```toml
"mcp>=1.0.0",
```

mcp SDK 提供：`mcp.client.ClientSession`、`mcp.client.stdio.stdio_client`、`mcp.client.sse.sse_client`、`mcp.client.streamablehttp.streamablehttp_client`。

---

## 验证方案

1. **单元验证**：用 `everything` MCP server（`npx @modelcontextprotocol/server-everything`）配置 stdio server，启动 nano-openclaw，检查 stderr 输出 `MCP: loaded N tools`
2. **工具调用验证**：向 agent 提问触发 MCP 工具使用，检查工具结果正确返回
3. **多 server 验证**：同时配置两个 server，确认工具名无冲突
4. **失败容忍验证**：配置一个不存在的命令，确认只跳过失败 server，不影响其余功能
5. **关闭验证**：退出 nano-openclaw 后确认子进程已终止（无僵尸进程）
