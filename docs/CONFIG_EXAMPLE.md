# nano-openclaw 配置说明

## 配置文件路径解析

配置文件按以下优先级查找（找到第一个即使用）：

| 优先级 | 路径 | 说明 |
|--------|------|------|
| 1 | `--config <path>` | 命令行显式指定 |
| 2 | `$OPENCLAW_CONFIG_PATH` | 环境变量 |
| 3 | `{stateDir}/nano-openclaw.json5` | 状态目录下 |
| 4 | `{cwd}/workspace/nano-openclaw.json5` | 项目 workspace 目录 |
| 5 | `~/.openclaw/nano-openclaw.json5` | 用户全局配置 |

**状态目录** (`stateDir`) 解析优先级：
1. `$OPENCLAW_STATE_DIR` 环境变量
2. `{cwd}/.openclaw`（项目级，如果存在）
3. `~/.openclaw`（全局）

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
3. 默认 agent：`~/.openclaw/workspace`（支持 `OPENCLAW_PROFILE`，变为 `~/.openclaw/workspace-<profile>`）
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
| `context.budget` | number | `100000` | 上下文 token 预算 |
| `context.threshold` | number | `0.8` | 触发压缩的阈值比例 |
| `context.recent_turns` | number | `3` | 压缩时保留的最近对话轮数 |

### activeMemory — Active Memory 插件配置

Active Memory 是可选插件，启用后在每次用户消息前自动搜索 `MEMORY.md` 和 `memory/*.md`，将相关记忆注入系统提示，让 agent 自动记住偏好和历史。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 是否启用 Active Memory |
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

### dreaming — Dreaming 插件配置

Dreaming 是可选插件，启用后追踪 memory_search 的召回记录，定期将高频、高质量的记忆片段自动提升到 MEMORY.md（长期记忆），并生成叙事性的 Dream Diary 写入 DREAMS.md。

状态存储在 `workspace/memory/.dreams/short-term-recall.json`。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `false` | 是否启用 Dreaming |
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

#### skills.allowBundled — 内置技能白名单

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `allowBundled` | string[] \| null | `null` | 允许的内置技能列表（null = 允许所有） |

---

## 环境变量

| 变量 | 说明 |
|------|------|
| `OPENCLAW_CONFIG_PATH` | 配置文件路径覆盖 |
| `OPENCLAW_STATE_DIR` | 状态目录覆盖 |
| `OPENCLAW_HOME` | 用户 home 目录覆盖 |
| `OPENCLAW_PROFILE` | 配置 profile（影响默认 workspace 路径） |
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
    // 限制内置技能
    allowBundled: ["frontend-design", "brainstorming"],
  },
}
```
