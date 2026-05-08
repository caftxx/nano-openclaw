---
name: session-debug
description: "Debug agent session failures by analyzing transcript files. Detects tool errors, approval denials, interrupted turns, and missing conclusions."
metadata:
  {
    "openclaw":
      {
        "emoji": "🔍",
      },
  }
---

# Session Debug Skill

分析 agent session transcript（`.jsonl`），诊断失败原因。

## When to Use

✅ **USE this skill when:**

- "debug 上次 session"
- "为什么我的 session 失败了？"
- "分析 session 错误"
- "上次 session 发生了什么？"
- "session abc-123 出了什么问题？"

## When NOT to Use

❌ **DON'T use this skill when:**

- Session 正常结束，只是想查看内容
- 需要修改 session 配置

## Usage

### 运行脚本

```bash
# 默认 agent 最近一次 session
python <skill_dir>/scripts/analyze.py

# 指定 agent
python <skill_dir>/scripts/analyze.py --agent-id coder

# 指定 session ID
python <skill_dir>/scripts/analyze.py --session-id abc-123-def

# 时间范围筛选（ISO 格式，默认 UTC）
python <skill_dir>/scripts/analyze.py --start-time 2025-05-01 --end-time 2025-05-08

# 限制分析数量（默认 10）
python <skill_dir>/scripts/analyze.py --start-time 2025-05-01 --limit 5

# 覆盖 state 目录
python <skill_dir>/scripts/analyze.py --state-dir /path/to/.nano-openclaw
```

`<skill_dir>` 是本 SKILL.md 所在目录。

## 检测内容

| 模式 | 描述 |
|------|------|
| **无结论** | session 末尾是 user/tool_result 消息，助手从未给出最终回复 |
| **Turn 中断** | 最后一条 assistant 消息含 tool_use 但没有后续 tool_result，session 被取消 |
| **Approval 拒绝** | tool_result 包含 `approval denied`，工具调用被门禁拦截 |
| **工具报错** | tool_result 标记 `is_error: true`，包含错误内容和重复次数 |
| **Context 压缩** | compaction 次数，反映 context 预算压力 |
| **工具错误率** | 所有工具调用的整体错误率 |

## 输出格式

```markdown
# Session Debug Report

**Session ID**: `...`
**Model**: `...`
**Timestamp**: ...
**CWD**: `...`
**Messages**: 26 | **Compactions**: 1 | **Last msg**: `msg-xxx`

---

## Failure Analysis

- No assistant conclusion: session ends with a user/tool_result message ...
- Tool `bash` error (3x repeated): AttributeError: 'Response' object has no attribute ...
- Tool `write_file` blocked by approval gate (1x)

## Tool Errors

1. `bash`
   > AttributeError: 'Response' object has no attribute 'status_text'

## Tool Statistics

| Tool | Calls | Errors |
|------|------:|-------:|
| bash |    12 |      3 |

## Last Messages
...
```

## Notes

- Transcript 路径：`$stateDir/agents/$agentId/sessions/$sessionId.jsonl`
- 脚本**只读**，不修改任何 session 文件
- 时间过滤用 `sessions.json` 中的 `updated_at`，精度到秒
