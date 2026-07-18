# nano-openclaw 配置说明

## 配置文件路径解析

配置文件按以下优先级查找（找到第一个即使用）：

| 优先级 | 路径 | 说明 |
|--------|------|------|
| 1 | `$NANO_OPENCLAW_CONFIG_PATH` | 环境变量 |
| 2 | `{stateDir}/nano-openclaw.json5` | 状态目录下 |
| 3 | `{cwd}/workspace/nano-openclaw.json5` | 项目 workspace 目录 |
| 4 | `~/.nano-openclaw/nano-openclaw.json5` | 用户全局配置 |

**状态目录** (`stateDir`) 解析优先级：
1. `$NANO_OPENCLAW_STATE_DIR` 环境变量
2. `{cwd}/.nano-openclaw`（项目级，如果存在）
3. `~/.nano-openclaw`（全局）

## Session 存储路径

Session 数据存储在状态目录下，按 agent 隔离：

```
{stateDir}/
└── agents/
    └── {agentId}/
        └── sessions/
            ├── sessions.json          # Session 索引
            └── {sessionId}.jsonl      # 对话转录文件
```

## Workspace 工作目录

Workspace 是 agent 操作文件的工作根目录，解析优先级（与 OpenClaw 一致）：

1. `agents.list[<agentId>].workspace` — 单个 agent 的显式配置
2. `agents.defaults.workspace` — 默认 agent 直接使用；非默认 agent 自动追加 `/<agentId>` 子目录
3. 默认 agent：`~/.nano-openclaw/workspace`（支持 `NANO_OPENCLAW_PROFILE`，变为 `~/.nano-openclaw/workspace-<profile>`）
4. 非默认 agent：`{stateDir}/workspace-<agentId>`

---

## 配置字段说明

### agents — Agent 配置

#### agents.defaults — 全局默认值

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model` | string | `"anthropic/claude-sonnet-4-5-20250929"` | 主模型，格式 `provider/model-id` |
| `imageModel` | string \| null | `null` | 图像理解模型，`null` 表示使用 Native Vision |
| `workspace` | string \| null | `null` | Agent 工作目录路径（相对或绝对） |
| `contextTokens` | number \| null | `null` | 上下文 token 上限 |
| `thinkingDefault` | string \| null | `null` | 默认思考等级：`off\|minimal\|low\|medium\|high\|xhigh\|adaptive\|max` |
| `bootstrapMaxChars` | number | `12000` | 单个 workspace 引导文件的最大字符数 |
| `bootstrapTotalMaxChars` | number | `60000` | 所有 workspace 引导文件合计的最大字符数 |

#### agents.list[] — Agent 列表

每个 agent 可覆盖 defaults 中的字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `id` | string | **必需** | Agent 唯一标识 |
| `default` | boolean | `false` | 是否为默认 agent |
| `name` | string \| null | `null` | 显示名称 |
| `workspace` | string \| null | `null` | 覆盖默认 workspace |
| `model` | string \| null | `null` | 覆盖默认 model |
| `imageModel` | string \| null | `null` | 覆盖默认 imageModel |

`imageModel` 行为：未配置时，图片会作为 Native Vision block 直接交给主模型（要求主模型 `input` 含 `image`）；配置后，图片先交给 image model 生成简短描述，再把描述注入主 prompt。若 image model 调用失败，运行时会优先回退到主模型 Native Vision；主模型也不支持图片时，会注入显式的图片处理失败文本，避免模型误判为用户没有发送图片。

### models — 模型/Provider 配置

#### models.mode

| 值 | 说明 |
|----|------|
| `"merge"` | 自定义 provider 合并到内置 provider（默认） |
| `"replace"` | 仅使用自定义 provider，忽略内置 |

#### models.providers.<id> — Provider 定义

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `baseUrl` | string | **必需** | API 端点 URL |
| `apiKey` | string \| null | `null` | API 密钥，支持 `${ENV_VAR}` 语法 |
| `api` | string | `"openai-completions"` | API 协议：`anthropic-messages` \| `openai-completions` \| `openai-responses` |
| `models[]` | array | `[]` | 模型列表 |

#### models.providers.<id>.models[] — 模型定义

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `id` | string | **必需** | 模型 ID（在此 provider 内唯一） |
| `name` | string | `null` | 显示名称 |
| `input` | string[] | `["text"]` | 输入模态：`text` \| `image` \| `video` \| `audio` |
| `reasoning` | boolean | `false` | 是否支持推理 |
| `contextWindow` | number | `8192` | 上下文窗口大小 |
| `maxTokens` | number | `4096` | 最大输出 token 数 |
| `cost` | object | 全 0 | 价格配置 |

#### models.providers.<id>.models[].cost

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `input` | number | `0` | 输入价格（每百万 token） |
| `output` | number | `0` | 输出价格（每百万 token） |
| `cacheRead` | number | `0` | 缓存读取价格 |
| `cacheWrite` | number | `0` | 缓存写入价格 |

### session — 会话配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `idleMinutes` | number | `60` | 空闲超时分钟数 |
| `reset.mode` | string | `"idle"` | 重置模式：`daily` \| `idle` |
| `reset.idleMinutes` | number | `120` | 空闲多少分钟后重置 |

### gateway — Gateway daemon 配置

`gateway` 子命令（`gateway start/status/stop/run`）启动一个 daemon 进程，内部组合 WebUI、`/rpc` WebSocket、外部 channels（如 WeChat）、cron scheduler、subagent runner。远程 TUI 通过 `/rpc`（`tui --connect`）接入；WebUI/TUI 是 frontend adapters，不计入 `channels.status`。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `host` | string | `"127.0.0.1"` | 绑定地址。改成 `"0.0.0.0"` 等非 loopback 时启动会打 warning（v1 无 auth） |
| `port` | number | `5000` | TCP 端口（范围 1-65535） |
| `log_path` | string | `""` | daemon 后台模式的 stdout/stderr 写入位置；留空 → `state_dir/log/gateway.log` |
| `tls_cert` | string | `""` | PEM 证书路径；与 `tls_key` 成对设置则启用 HTTPS（手机用 `/voice` 必需） |
| `tls_key` | string | `""` | PEM 私钥路径；与 `tls_cert` 成对，只给一个会启动失败 |
| `restart_strategy` | string | `"exec"` | daemon 重启策略：`exec`（原地 re-exec）\| `exit`（退出交给 systemd/docker 等 supervisor 拉起） |

**优先级**（高 → 低）：CLI 参数（`--host` / `--port` / `--tls-cert` / `--tls-key`）> `config.gateway.*` > 默认值。

`gateway status` / `gateway run` 的 URL 输出会反映实际 scheme（启用 TLS 时为 `https`/`wss`），绑定 `0.0.0.0`/`::` 时自动探测并显示局域网 IP；scheme 作为第 4 个字段写入 `gateway.pid`。

```json5
gateway: {
  host: "127.0.0.1",
  port: 5000,
  log_path: "",
  tls_cert: "",
  tls_key: "",
  restart_strategy: "exec",
}
```

### xiaozhi — xiaozhi-esp32 原生接入

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `false` | 是否启用 `xiaozhi/default` channel |
| `token` | string | `""` | OTA、WebSocket 与拍照接口共用的 Bearer Token；建议写成 `${XIAOZHI_TOKEN}` |
| `websocketUrl` | string | `""` | 设备连接地址；留空时从 OTA 请求地址推导，反向代理/公网部署应显式填写 `wss://.../xiaozhi/v1/` |
| `mcpTimeoutMs` | number | `10000` | 调用设备 MCP 工具的超时毫秒数（1000–120000） |
| `maxPhotoBytes` | number | `5242880` | 单张 JPEG 上限（1024–20971520 字节） |

启用前安装可选依赖：`uv tool install "nano-openclaw[xiaozhi]"`；源码开发使用 `uv sync --extra xiaozhi`。小智需要独立 `imageModel`，并复用阿里云语音配置：

```json5
voice: {
  provider: "aliyun",
  appkey: "你的智能语音交互项目 Appkey",
  accessKeyId: "${ALIYUN_AK_ID}",
  accessKeySecret: "${ALIYUN_AK_SECRET}",
  region: "cn-shanghai",
  ttsEnabled: true,
  ttsVoice: "xiaoxian",
  ttsSampleRate: 16000,
},

xiaozhi: {
  enabled: true,
  token: "${XIAOZHI_TOKEN}",
  websocketUrl: "",
  mcpTimeoutMs: 10000,
  maxPhotoBytes: 5242880,
  ttsVoice: "zhiqi",       // 选择支持 ttsSampleRate 的阿里云音色
  ttsSampleRate: 24000,    // 16000 | 24000；立创 S3 推荐 24000
  opusBitrate: 64000,      // 16000..128000 bit/s
}
```

gateway 需绑定设备可访问的地址，例如 `gateway.host: "0.0.0.0"`。在 xiaozhi-esp32 中运行 `idf.py menuconfig`，进入 `Xiaozhi Assistant → Default OTA URL`，填写 `http://<nano局域网IP>:5000/xiaozhi/ota/` 后重新刷机。首版仅支持 WebSocket v1（上行 16 kHz、下行默认 24 kHz、单声道、60 ms 裸 Opus），不支持 v2/v3、MQTT 或 UDP。上行固定为 16 kHz 供 ASR；下行独立合成和编码，立创 S3 推荐保持 `ttsSampleRate: 24000`，并搭配 `zhiqi`、`zhijia` 等支持 24 kHz 的音色。

每个 `Device-Id` 独立绑定 session，映射保存在 `{stateDir}/xiaozhi-sessions.json`；相机 JPEG 只在请求期间以内存/临时文件处理，关闭上传后立即释放，不写入 session 附件。设备 MCP 工具只注入该设备发起的 turn，从 WebUI 打开同一 session 不会自动获得硬件控制权。配置不完整时 channel 显示为 `error`，gateway/WebUI 仍会正常启动。

局域网接入点为 `/xiaozhi/ota/`、`/xiaozhi/v1/` 和 `/xiaozhi/vision/explain`。外网部署必须显式配置受信证书的 `wss` 地址，并在反向代理限制 OTA 接口访问；不要把 token 写进日志或提交到仓库。

### wechat — 没有 wechat 配置块

WeChat 已经从 nano-openclaw.json5 里彻底移除。**唯一接入方式是扫码登录**：

```bash
uv run nano-openclaw wechat login                  # 默认账号 'default'
uv run nano-openclaw wechat login --account=work   # 多账号:换标签即可
```

登录写入 `state_dir/wechat-tokens.{account}.json`(`default` 无后缀,其余 `wechat-tokens.{id}.json`),内容包括 token + iLink 服务器返回的 base_url + bot_id + login_at 时间戳。

daemon 启动时扫描 `state_dir/wechat-tokens*.json` 自动注册账号,每个文件一个 WeChat `ChannelAdapter`。运行时调优参数(long-poll 超时、typing 续命间隔等)使用代码内默认值,与 openilink-sdk-python 对齐,不再可配置。

**uid → session 映射**:每个 wechat uid 第一次发消息时通过 `BackendSessionManager.create()` 拿到真实 session,映射持久化到 `state_dir/wechat-sessions.{account}.json`。

**cron 通知路由**:cron 任务的 `created_by` 三段格式 `wechat:{account}:{uid}`,完成后通过 `ChannelManager` 路由到对应账号的通知队列,由 WechatBot 读取并推送给原 uid。cron 本身不是 channel；它是 scheduler feature，可选择 channel 作为投递目标。

**Token 失效时重新登录**:服务器返 `errcode=-14` 时 daemon 长退避 5 分钟并日志高优先级提示;直接重新跑 `wechat login` 写新 token 即可,daemon 下一轮长轮询自动捡起。


### exec-approvals.json — 审批门禁配置

审批策略**不在主配置文件中**，与 openclaw 一致：读取独立文件 `{stateDir}/exec-approvals.json`。

**文件格式**（与 openclaw 的 `ExecApprovalsFile` 完全相同）：

```json
{
  "version": 1,
  "defaults": {
    "ask": "on-miss",
    "security": "allowlist"
  },
  "agents": {
    "*": { "allowlist": [...] },
    "default": {
      "ask": "always",
      "allowlist": [
        { "id": "...", "pattern": "ls", "source": "allow-always", "lastUsedAt": 1234567890 }
      ]
    }
  }
}
```

**字段说明**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `defaults.ask` | string | `"off"` | 全局默认审批模式 |
| `defaults.security` | string | `"full"` | 全局默认安全模式 |
| `agents.*` | object | — | 通配符：所有 agent 共享的 allowlist |
| `agents.{id}` | object | — | 特定 agent 配置，覆盖 defaults 和通配符 |
| `agents.{id}.allowlist` | array | `[]` | 已授权的命令模式列表 |

**解析优先级**（镜像 `resolveExecApprovalsFromFile()`）：
`defaults` → `agents.*`（通配符）→ `agents.{agentId}`（特定 agent）

#### ask 值说明

| 值 | 说明 |
|----|------|
| `"off"` | 从不提示（默认） |
| `"on-miss"` | 未命中 allowlist 时提示 |
| `"always"` | 总是提示 |

#### security 值说明

| 值 | 说明 |
|----|------|
| `"full"` | 允许所有（默认） |
| `"allowlist"` | 未命中 allowlist 则提示（配合 ask=on-miss） |
| `"deny"` | 无 allowlist 门禁（openclaw 依赖 OS 沙箱；nano 中仅 ask=always 有效） |

**文件路径**：`{stateDir}/exec-approvals.json`（stateDir 解析同上）。`allow-always` 决策也持久化到此文件的对应 agent allowlist 中。

### 其他字段（nano-openclaw 自定义）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `noTools` | boolean | `false` | 禁用工具，纯对话模式 |
| `maxIterations` | number | `12` | 每轮用户输入最大工具调用次数 |
| `context.budget` | number \| null | `null` | 上下文 token 预算；`null` = 自动取模型 contextWindow |
| `context.threshold` | number | `0.8` | 触发压缩的阈值比例 |
| `context.recent_turns` | number | `3` | 压缩时保留的最近对话轮数 |
| `memorySearch` | object | 见下方 | memory_search 排序配置 |
| `plugins` | object | `{ enabled: true, load: ["memory","web","subagent","mcp","schedule","review-fork"] }` | 轻量插件加载配置 |
| `subagents` | object | 见下方 | 后台子 agent 编排配置 |
| `reviewFork` | object | 见下方 | Background Review Fork 自进化配置（默认开） |
| `extractMemories` | object | 见下方 | stop-hook 记忆 extractor 配置（默认开） |
| `schedule` | object | 见下方 | cron scheduler 配置 |
| `promptCaching` | object | `{ enabled: true, cache_ttl: "5m" }` | prompt caching（Anthropic provider）开关 |
| `memoryFlush` | object | `{ enabled: true, softThresholdTokens: 4000, reserveTokensFloor: 20000 }` | 接近预算时把记忆刷写到磁盘 |

### plugins — Plugin / Hook 系统

内置插件 `memory`、`web`、`subagent`、`mcp`、`schedule`、`review-fork` **始终加载**，无法通过配置禁用。`plugins.load` 仅用于加载额外的外部插件。

插件只拿到窄 `PluginApi` 注册面，不直接访问或修改 `AgentRuntime` 内部状态。可注册内容包括：

- `register_tool`
- `register_hook`
- `register_slash`
- `register_channel`
- `register_feature`

内置插件（始终加载）：

| 名称 | 注册内容 |
|------|----------|
| `"memory"` | `memory_get` / `memory_search` 工具，并通过 `before_prompt_build` 注入 daily memory |
| `"web"` | `web_search` / `web_fetch` 工具 |
| `"subagent"` | `sessions_spawn` / `subagents` 工具 |
| `"mcp"` | 在 `session_start` hook 初始化 MCP server 并注册 MCP 工具 |
| `"schedule"` | `cron_create` / `cron_list` / `cron_delete` / `schedule_wakeup` 工具 + cron scheduler |
| `"review-fork"` | Background Review Fork 自进化 sub-agent（`reviewFork` 配置，见下方） |

加载外部插件示例：

```json5
plugins: {
  enabled: true,
  load: [
    { module: "my_pkg.my_plugin", config: { key: "val" } },
    { path: "./plugins/custom.py", config: { key: "val" } },
  ],
}
```

注意：即使设置 `load: []`，内置插件仍会加载。`plugins.enabled: false` 可完全禁用插件系统（包括内置插件）。

### tools — 工具配置

`tools` 配置控制工具参数，对齐 openclaw 的 `tools.*` 配置。Web 工具通过内置 `"web"` 插件注册（始终加载）。

#### tools.noTools

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `noTools` | boolean | `false` | 禁用所有工具，纯对话模式（与顶层 `noTools` 等效） |

#### tools.web — Web 工具配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `web.search` | object | — | web_search 工具配置（见下方说明） |
| `web.fetch` | object | — | web_fetch 工具配置（见下方说明） |

#### tools.web.search — Web 搜索配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `search.enabled` | boolean | `true` | 是否启用 web_search 工具 |
| `search.maxResults` | number | `10` | 最大搜索结果数（范围 1-50） |
| `search.region` | string | `"wt-wt"` | DuckDuckGo 区域代码 |

**常见 region 值**：`wt-wt`（全球）、`us-en`（美国）、`uk-en`（英国）、`de-de`（德国）、`fr-fr`（法国）、`zh-cn`（中国）、`ja-jp`（日本）。

#### tools.web.fetch — Web 抓取配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `fetch.enabled` | boolean | `true` | 是否启用 web_fetch 工具 |
| `fetch.maxChars` | number | `20000` | 最大返回字符数（范围 100-500000） |
| `fetch.maxRedirects` | number | `3` | 最大重定向次数（范围 0-10） |
| `fetch.timeoutSeconds` | number | `30` | 请求超时秒数（范围 1-120） |
| `fetch.extractMode` | string | `"markdown"` | 提取模式：`markdown` \| `text` |

**SSRF 防护**：web_fetch 内置两阶段 SSRF 防护。Phase 1（预 DNS）：检查字面私有 IP 和已知黑名单 hostname（localhost、*.local、*.internal）。Phase 2（后 DNS）：DNS 解析后验证所有返回 IP 不是私有/内部地址。任何违规都会返回错误而非访问。

**外部内容安全**：web_search 和 web_fetch 的返回内容都通过 `wrap_external_content()` 包装，添加 `<EXTERNAL_UNTRUSTED_CONTENT>` 边界标记并清洗 LLM 特殊 token（如 `<antThinking>`、`</think>`、`[INST]` 等），防止 prompt injection 攻击。

### memorySearch — 记忆搜索配置

`memorySearch` 控制 `memory_search` 的 provider 与排序行为。默认 `lexical` provider 与 openclaw 保持一致：本地词法匹配，temporal decay 支持存在但不自动开启。安装 `nano-openclaw[zvec]` 后可选启用 `zvec` provider；默认安装不依赖 Zvec。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `provider` | string | `"lexical"` | `memory_search` provider id；未知 provider 会回退到 `lexical` |
| `providers` | object | `{}` | provider 专属配置，按 provider id 分组 |
| `temporalDecay.enabled` | boolean | `false` | 是否启用按时间衰减的排序 |
| `temporalDecay.halfLifeDays` | number | `30` | 半衰期天数；开启后 dated daily note 每 N 天分数减半 |

启用后，`memory/YYYY-MM-DD.md` 会按文件名日期衰减；`MEMORY.md` 和 `memory/projects.md` 这类非日期 evergreen 文件不会衰减。

```json5
{
  memorySearch: {
    provider: "lexical",
    providers: {},
    temporalDecay: {
      enabled: true,
      halfLifeDays: 30,
    },
  },
}
```

可选 Zvec FTS/BM25 配置（轻量、本地、无 embedding 模型下载）：

```json5
{
  memorySearch: {
    provider: "zvec",
    providers: {
      zvec: {
        mode: "fts",
        // zh -> jieba tokenizer；en/缺省 -> standard tokenizer
        bm25: { language: "zh" },
      },
    },
  },
}
```

显式启用 Zvec hybrid（FTS/BM25 + local dense）。`nano-openclaw[zvec]` 会安装 local dense 所需 Python 依赖；首次使用仍会下载/加载本地 dense embedding 模型，只建议在 CPU/内存/网络条件合适的机器上使用。

```json5
{
  memorySearch: {
    provider: "zvec",
    providers: {
      zvec: {
        mode: "hybrid",
        denseEmbedder: "local_dense",
        localDense: {
          modelSource: "modelscope", // 国内更稳；国际可用 huggingface
        },
        bm25: { language: "zh" },
      },
    },
  },
}
```

### activeMemory — Active Memory 插件配置

Active Memory 是可选插件，启用后在每次用户消息前自动搜索 `MEMORY.md` 和 `memory/*.md`，将相关记忆注入系统提示，让 agent 自动记住偏好和历史。

> **启用方式**：顶层 `activeMemory` 字段默认 `null`——**不写这个块 = 关闭**。一旦提供 `activeMemory: { ... }` 块，块内 `enabled` 默认 `true` 即生效。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 块内是否启用 Active Memory（块本身缺省 = 关） |
| `model` | string \| null | `null` | 子 agent 使用的模型（null = 复用主模型） |
| `thinking` | string | `"off"` | 子 agent 思考等级：`off|minimal|low|medium|high|xhigh|adaptive|max` |
| `queryMode` | string | `"recent"` | 查询模式：`message` \| `recent` \| `full` |
| `promptStyle` | string | `"balanced"` | 召回风格（见下方说明） |
| `timeoutMs` | number | `15000` | 超时时间（毫秒，范围 250-120000） |
| `maxSummaryChars` | number | `220` | 返回摘要最大字符数（范围 40-1000） |
| `recentUserTurns` | number | `2` | `recent` 模式保留的最近用户消息数（范围 0-4） |
| `recentAssistantTurns` | number | `1` | `recent` 模式保留的最近 assistant 回复数（范围 0-3） |
| `recentUserChars` | number | `220` | `recent` 模式每条用户消息字符限制（范围 40-1000） |
| `recentAssistantChars` | number | `180` | `recent` 模式每条 assistant 回复字符限制（范围 40-1000） |
| `cacheTtlMs` | number | `15000` | 结果缓存 TTL（毫秒，范围 1000-120000） |
| `logging` | boolean | `false` | 是否打印调试日志 |

#### queryMode 说明

| 值 | 说明 |
|----|------|
| `"message"` | 仅使用最新用户消息作为查询 |
| `"recent"` | 使用最近 N 轮对话（可配置 user/assistant 消息数和字符限制） |
| `"full"` | 使用完整对话历史 |

#### promptStyle 说明

| 值 | 说明 |
|----|------|
| `"balanced"` | 平衡召回：搜索相关决策、偏好、todo、日期、人物（默认） |
| `"strict"` | 精确匹配：只返回直接回答查询的事实 |
| `"contextual"` | 上下文关联：搜索项目、时间线、依赖关系 |
| `"recall-heavy"` | 广泛召回：搜索所有可能相关信息 |
| `"precision-heavy"` | 高精度召回：只返回高度置信的结果 |
| `"preference-only"` | 偏好搜索：只搜索用户偏好（代码风格、工具选择等） |

### subagents — 后台子 agent 编排配置

Subagent 能力会注册两个模型工具：`sessions_spawn` 用于派生 isolated 后台子会话，`subagents` 用于按需查看当前 session 的 run 状态。CLI 也提供 `/subagents`、`/subagents all`、`/subagents kill <run_id>` 和 `/subagents kill all`。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `maxConcurrent` | number | `10` | 同时运行的子 agent 上限（范围 1-10） |
| `maxSpawnDepth` | number | `1` | 最大派生深度；nano 固定只允许 1，避免子 agent 再派生子 agent |
| `runTimeoutSeconds` | number | `0` | 单个子 agent 运行超时秒数；`0` 表示不超时 |
| `archiveAfterMinutes` | number | `60` | 终态 run 保留多久后可清理；`0` 表示可立即清理 |
| `model` | string \| null | `null` | 子 agent 默认模型；`null` 表示复用父会话模型 |
| `thinking` | string \| null | `null` | 子 agent 默认思考等级；`null` 表示沿用模型/agent 默认解析结果 |

#### 运行语义

- 子 agent 继承父会话 workspace，但拥有独立 transcript/session key。
- 目前只支持 `context: "isolated"`；`fork` 预留给未来实现。
- 子 agent 的工具集会过滤掉 `sessions_spawn` 和 `subagents`，避免递归派生和后台会话管理。
- 完成、失败或超时后，结果自动作为一条 user message 注入父 session；模型不需要循环调用 `subagents` 轮询。
- 后台子 agent 不能弹出前台审批 UI；需要交互审批的工具调用会被默认拒绝。

### reviewFork — Background Review Fork 配置

每 N 个 `end_turn` 后台启动一个受限 sub-agent，读最近对话决定是否把"用户偏好/教训/可复用方法"沉淀进 `MEMORY.md` 或现有 `SKILL.md`。**默认开启**（每次触发 ~1 次 LLM 调用）。运行时控制：`/review-fork status|on|off|run`。

每次 spawn 写一行到 `state_dir/review-fork.jsonl`，结果写 `state_dir/review-fork-results.jsonl`。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 是否启用（默认开；设 false 关闭） |
| `trigger_n` | int | `10` | 每 N 个 `end_turn` 触发（范围 ≥1） |
| `cooldown_s` | int | `60` | 两次触发之间的最短间隔（秒，范围 ≥0） |
| `timeout_s` | int | `90` | sub-agent 单次 run 的硬超时（秒，范围 ≥10） |
| `model_aux` | string \| null | `null` | 辅助模型覆盖（`provider/id`）；`null` = 跟父 agent 模型 |

```json5
{
  reviewFork: {
    enabled: true,
    trigger_n: 10,
    cooldown_s: 60,
    timeout_s: 90,
    model_aux: "anthropic/claude-haiku-4-5-20251001",  // 用小模型省钱
  },
}
```

### extractMemories — Stop-hook 记忆 extractor 配置

对齐 claude-code 的 `extractMemories.ts`：在每个符合条件的 turn 结束后 fork 一个 subagent，把对话蒸馏进 `memory/topics/*.md` 并更新 `memory/MEMORY.md`。**默认开启**，与主 agent 的 topic 写入互斥。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 是否启用 stop-hook extractor |
| `triggerSources` | string[] | `["tui","webui","wechat"]` | 触发的 turn 来源；默认排除 cron / channel 自动触发 |
| `maxTurns` | int | `5` | extractor subagent 的硬 turn 上限（范围 1-20） |
| `cooldownTurns` | int | `1` | 每 N 个符合条件的 turn 跑一次（范围 ≥1） |
| `model` | string \| null | `null` | 模型覆盖（`provider/model-id`）；`null` 继承父 agent |
| `prompt` | string | 内置模板 | extractor 提示模板 |

### schedule — Cron scheduler 配置

daemon 内置 cron scheduler，配合 `cron_create` / `cron_list` / `cron_delete` / `schedule_wakeup` 工具与 `cron/` 目录下的任务定义。重启不重触发已跑过的任务（`last_run_at_ms` + grace 去重）。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 是否启用 cron scheduler |
| `maxConcurrentRuns` | int | `3` | 同时运行的 cron 任务上限 |
| `missedJobsLimit` | int | `5` | 重启后最多补跑的错过任务数 |

### dreaming — Dreaming 插件配置

Dreaming 追踪 memory_search 的召回记录，定期将高频、高质量的记忆片段自动提升到 MEMORY.md（长期记忆），并生成叙事性的 Dream Diary 写入 DREAMS.md。顶层 `dreaming` 字段 default-construct，**默认开启**。

状态存储在 `workspace/memory/.dreams/short-term-recall.json`。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 是否启用 Dreaming（默认开） |
| `frequency` | string | `"0 3 * * *"` | 调度频率（cron 格式，见下方说明） |
| `minScore` | number | `0.5` | 提升门槛：综合评分（范围 0.0-1.0） |
| `minRecallCount` | int | `2` | 提升门槛：最少召回次数（范围 ≥1） |
| `minUniqueQueries` | int | `1` | 提升门槛：最少不同查询数（范围 ≥1） |
| `maxPromotions` | int | `10` | 每次最多提升条目数（范围 1-50） |
| `diary` | boolean | `true` | 是否生成 Dream Diary 日记（需要额外 API 调用） |
| `model` | string \| null | `null` | Dream Diary 生成模型（null = 复用主模型） |

#### frequency 说明

支持 `"minute hour * * *" 格式的 cron 表达式：

| 示例 | 说明 |
|------|------|
| `"0 3 * * *"` | 每天凌晨 3:00（默认） |
| `"*/30 * * * *"` | 每 30 分钟 |
| `"0 */6 * * *"` | 每 6 小时 |
| `"*/5 */2 * * *"` | 每 2 小时的第 5、10、15...分钟 |

不支持 day-of-month、month、day-of-week 字段（必须为 `*`）。

#### 评分机制

综合评分基于三个信号（权重：频率 40% + 多样性 35% + 新鲜度 25%）：

- **频率分数**：召回次数越多，分数越高
- **多样性分数**：不同查询越多，分数越高
- **新鲜度分数**：最近召回的时间越近，分数越高

#### 工作流程

1. **Light Phase**：收集候选记忆片段（最多 50 个）
2. **Deep Phase**：评分并提升符合条件的片段到 MEMORY.md
3. **Dream Diary**：生成叙事性摘要写入 DREAMS.md（可选）

提升的内容会带有注释标记：`<!-- dreaming:promoted DATE score=X recalls=Y -->`

### MCP 配置

MCP（Model Context Protocol）用于连接外部工具服务器，将外部工具注册到 agent。

#### mcp.servers — 服务器列表

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `<name>.command` | string | `null` | stdio 传输：启动命令 |
| `<name>.args` | string[] | `null` | stdio 传输：命令参数 |
| `<name>.env` | object | `null` | stdio 传输：环境变量 |
| `<name>.cwd` | string | `null` | stdio 传输：工作目录 |
| `<name>.workingDirectory` | string | `null` | 同上（别名） |
| `<name>.url` | string | `null` | SSE/streamable-http 传输：服务器 URL |
| `<name>.transport` | string | `null` | 传输类型：`stdio` \| `sse` \| `streamable-http` |
| `<name>.headers` | object | `null` | SSE/streamable-http 传输：请求头 |
| `<name>.connectionTimeoutMs` | number | `null` | 连接超时（毫秒） |

#### mcp — 全局配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `sessionIdleTtlMs` | number | `null` | 会话空闲超时（毫秒） |

**传输类型**：
- **stdio**：通过标准输入/输出与 MCP 服务器通信（适用于本地进程）
- **sse**：通过 Server-Sent Events 连接（适用于远程服务器）
- **streamable-http**：通过流式 HTTP 连接（适用于支持流式的远程服务器）

启动时自动连接所有配置的 MCP 服务器，工具自动注册到 registry。退出时自动清理连接。

### skills — Skills 技能配置

Skills 配置管理技能加载和过滤行为，对齐 openclaw 的 `skills.*` 配置。

#### skills.entries — 单技能配置覆盖

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `<skillName>.enabled` | boolean | `true` | 启用/禁用该技能 |
| `<skillName>.apiKey` | string \| null | `null` | 该技能的 API key 覆盖 |
| `<skillName>.env` | object \| null | `null` | 该技能的环境变量覆盖 |

#### skills.load — 技能加载配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `extraDirs` | string[] | `[]` | 额外的技能搜索目录 |
| `watch` | boolean | `false` | 监听技能目录变化 |
| `maxCandidatesPerRoot` | number | `300` | 每个根目录最大扫描候选数 |
| `maxSkillsLoadedPerSource` | number | `200` | 每个来源最大加载技能数 |
| `maxSkillsInPrompt` | number | `150` | 提示中最大包含技能数 |
| `maxSkillsPromptChars` | number | `18000` | 技能部分最大字符数 |
| `maxSkillFileBytes` | number | `256000` | 单个 SKILL.md 文件最大字节 |

#### skills.install — 技能依赖安装策略

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `pythonIsolation` | `"venv"` | `"venv"` | Python skill 依赖安装到隔离 virtualenv |
| `allowGlobalPip` | boolean | `false` | 是否允许裸 `pip install` 写入全局 Python 环境 |

默认情况下，skill 的 Python 依赖通过 `skill_install` 工具安装到：
`{stateDir}/tools/python/skills/<skill-name>/venv`。

如果需要清理某个 skill 的 Python 依赖，删除对应的 venv 目录即可。普通 `bash` 中的 `pip install` / `python -m pip install` 会被设置 `PIP_REQUIRE_VIRTUALENV=true`，避免依赖误装到全局环境。

#### skills.allowBundled — 内置技能白名单

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `allowBundled` | string[] \| null | `null` | 允许的内置技能列表（null = 允许所有） |

---

## 环境变量

| 变量 | 说明 |
|------|------|
| `NANO_OPENCLAW_CONFIG_PATH` | 配置文件路径覆盖 |
| `NANO_OPENCLAW_STATE_DIR` | 状态目录覆盖 |
| `NANO_OPENCLAW_HOME` | 用户 home 目录覆盖 |
| `NANO_OPENCLAW_PROFILE` | 配置 profile（影响默认 workspace 路径） |
| `<PROVIDER>_API_KEY` | Provider API 密钥（如 `ANTHROPIC_API_KEY`） |

环境变量替换语法：`"${VAR_NAME}"`

---

## 内置 Provider

无需配置即可使用的 Provider：

| Provider ID | 默认模型 | 环境变量 |
|-------------|---------|---------|
| `anthropic` | `claude-sonnet-4-5-20250929` | `ANTHROPIC_API_KEY` |
| `openai` | `gpt-4o` | `OPENAI_API_KEY` |

---

## 示例

### 最小配置

```json5
{
  agents: {
    defaults: {
      model: "anthropic/claude-sonnet-4-5-20250929",
    },
  },
}
```

### 自定义 Provider

```json5
{
  agents: {
    defaults: {
      model: "openrouter/anthropic/claude-sonnet-4",
      imageModel: "openai/gpt-4o-mini",
      workspace: "./workspace",
    },
  },
  models: {
    providers: {
      "openrouter": {
        baseUrl: "https://openrouter.ai/api/v1",
        apiKey: "${OPENROUTER_API_KEY}",
        api: "openai-completions",
        models: [
          {
            id: "anthropic/claude-sonnet-4",
            name: "Claude Sonnet 4",
            input: ["text", "image"],
            contextWindow: 200000,
            maxTokens: 8192,
          },
        ],
      },
    },
  },
  maxIterations: 12,
  context: {
    budget: 100000,
    threshold: 0.8,
    recent_turns: 3,
  },
}
```

### 多 Agent 配置

```json5
{
  agents: {
    defaults: {
      model: "anthropic/claude-sonnet-4-5-20250929",
      workspace: "./workspace",
    },
    list: [
      { id: "default", default: true, name: "Default Agent" },
      { id: "coder", name: "Coding Agent", model: "anthropic/claude-sonnet-4-5-20250929" },
      { id: "analyst", name: "Analysis Agent" },
    ],
  },
}
```

多 Agent 的 workspace 解析：
- `default` → `./workspace`
- `coder` → `./workspace/coder`
- `analyst` → `./workspace/analyst`

### Active Memory 配置示例

```json5
{
  agents: {
    defaults: {
      model: "anthropic/claude-sonnet-4-5-20250929",
      workspace: "./workspace",
    },
  },
  activeMemory: {
    enabled: true,
    // 使用快速小模型节省成本
    model: "anthropic/claude-haiku-4-5-20251001",
    queryMode: "recent",
    promptStyle: "balanced",
    timeoutMs: 15000,
    maxSummaryChars: 220,
    recentUserTurns: 2,
    recentAssistantTurns: 1,
    recentUserChars: 220,
    recentAssistantChars: 180,
    cacheTtlMs: 15000,
    logging: false,
  },
}
```

### Dreaming 配置示例

```json5
{
  agents: {
    defaults: {
      model: "anthropic/claude-sonnet-4-5-20250929",
      workspace: "./workspace",
    },
  },
  dreaming: {
    enabled: true,
    // 每天凌晨 3 点运行
    frequency: "0 3 * * *",
    // 提升门槛：综合评分 >= 0.5
    minScore: 0.5,
    // 提升门槛：最少被召回 2 次
    minRecallCount: 2,
    // 提升门槛：最少来自 1 个不同查询
    minUniqueQueries: 1,
    // 每次最多提升 10 条记忆
    maxPromotions: 10,
    // 生成 Dream Diary 日记
    diary: true,
    // Dream Diary 使用快速小模型
    model: "anthropic/claude-haiku-4-5-20251001",
  },
}
```

状态文件位置：`workspace/memory/.dreams/short-term-recall.json`

提升结果写入：`workspace/MEMORY.md`（带 `<!-- dreaming:promoted -->` 注释）

Dream Diary 写入：`workspace/DREAMS.md`

### Subagents 配置示例

```json5
{
  subagents: {
    maxConcurrent: 10,
    maxSpawnDepth: 1,
    runTimeoutSeconds: 600,
    archiveAfterMinutes: 60,
    // 复用父会话模型；也可指定快速模型节省成本
    model: null,
    // null = 使用模型/agent 默认 thinking 解析结果
    thinking: null,
  },
}
```

模型可调用的派生参数示例：

```json5
{
  task: "阅读 README.md 并总结核心模块",
  label: "readme-summary",
  model: "anthropic/claude-haiku-4-5-20251001",
  thinking: "off",
  runTimeoutSeconds: 300,
  cleanup: "keep",
  context: "isolated",
}
```

### Skills 配置示例

```json5
{
  skills: {
    // 禁用特定技能
    entries: {
      "some-skill": { enabled: false },
      "api-skill": { apiKey: "${API_SKILL_KEY}" },
    },
    // 加载配置
    load: {
      extraDirs: ["~/.skills", "./custom-skills"],
      maxSkillsInPrompt: 100,
    },
    install: {
      pythonIsolation: "venv",
      allowGlobalPip: false,
    },
    // 限制内置技能
    allowBundled: ["frontend-design", "brainstorming"],
  },
}
```

### MCP 配置示例

```json5
{
  agents: {
    defaults: {
      model: "anthropic/claude-sonnet-4-5-20250929",
      workspace: "./workspace",
    },
  },
  mcp: {
    servers: {
      // stdio 传输示例：本地文件系统服务器
      "filesystem": {
        command: "npx",
        transport: "stdio",
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/dir"],
        // env: { "DEBUG": "1" },
        // cwd: "/working/directory",
      },
      // stdio 桥接器示例：mcp-proxy 连接 streamable HTTP 服务器
      "HomeAssistant": {
        command: "mcp-proxy",
        transport: "stdio",
        args: [
          "--transport=streamablehttp",
          "--stateless",
          "http://ha.lan/api/mcp",
        ],
        env: {
          API_ACCESS_TOKEN: "${HOME_ASSISTANT_TOKEN}",
        },
      },
      // SSE 传输示例：远程服务器
      // "remote-server": {
      //   url: "https://mcp.example.com/sse",
      //   transport: "sse",
      //   headers: { "Authorization": "Bearer ${MCP_API_KEY}" },
      // },
      // streamable-http 传输示例
      // "streamable-server": {
      //   url: "https://mcp.example.com/mcp",
      //   transport: "streamable-http",
      //   connectionTimeoutMs: 10000,
      // },
    },
    // 会话空闲超时（可选）
    // sessionIdleTtlMs: 300000, // 5 分钟
  },
}
```

### Web 工具配置示例

```json5
{
  agents: {
    defaults: {
      model: "anthropic/claude-sonnet-4-5-20250929",
      workspace: "./workspace",
    },
  },
  tools: {
    // 禁用所有工具（等效于顶层 noTools）
    // noTools: false,

    web: {
      // Web 搜索配置
      search: {
        enabled: true,
        maxResults: 15,        // 最多返回 15 条结果
        region: "zh-cn",       // 中文搜索结果
      },
      // Web 抓取配置
      fetch: {
        enabled: true,
        maxChars: 30000,       // 最多返回 30K 字符
        maxRedirects: 5,       // 最多跟随 5 次重定向
        timeoutSeconds: 45,    // 45 秒超时
        extractMode: "markdown", // 提取为 markdown 格式（可选 text）
      },
    },
  },
}
```

**禁用特定 Web 工具**：

```json5
{
  tools: {
    web: {
      search: { enabled: false },  // 禁用搜索，但保留 fetch
    },
  },
}
```
