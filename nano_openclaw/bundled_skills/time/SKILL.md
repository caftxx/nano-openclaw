---
name: time
description: "Get the current local date and time, timezone info, or convert between timezones."
metadata:
  {
    "openclaw":
      {
        "emoji": "🕐",
      },
  }
---

# Time Skill

Get the current date, time, and timezone information.

## When to Use

✅ **USE this skill when:**

- "What time is it?"
- "What's today's date?"
- "What day of the week is it?"
- "What timezone am I in?"
- "Convert time between timezones"
- Any task requiring the current date/time

## When NOT to Use

❌ **DON'T use this skill when:**

- Historical date calculations → use Python's `datetime` or `dateutil`
- Scheduling future events → no runtime access to calendars
- Precise atomic/UTC synchronization → use NTP tools

## How to Get Current Time

### Step 1 — Try Python first (works on all platforms)

```python
from datetime import datetime
now = datetime.now().astimezone()
print(now.strftime("%A, %Y-%m-%d %H:%M:%S %Z"))
```

If Python is unavailable or fails, fall back to Step 2.

### Step 2 — Platform-specific fallback

Detect the platform first, then use the appropriate command.

**Linux / macOS (GNU/BSD date)**

```bash
date "+%A, %Y-%m-%d %H:%M:%S %Z"
```

> macOS note: `date -Iseconds` is not supported on BSD `date`; use the format string above instead.

**Windows (PowerShell)**

```powershell
Get-Date -Format "dddd, yyyy-MM-dd HH:mm:ss K"
```

**Windows (cmd.exe)**

```bat
echo %date% %time%
```

## Timezone Queries

### Python (cross-platform)

```python
from datetime import datetime
now = datetime.now().astimezone()
print("Timezone:", now.strftime("%Z"), "/ UTC offset:", now.strftime("%z"))
```

### Linux/macOS fallback

```bash
# Current timezone name and UTC offset
date "+%Z %z"

# Convert to a specific timezone
TZ=America/New_York date "+%A, %Y-%m-%d %H:%M:%S %Z"
TZ=Asia/Shanghai  date "+%A, %Y-%m-%d %H:%M:%S %Z"
TZ=Europe/London  date "+%A, %Y-%m-%d %H:%M:%S %Z"
```

### Windows fallback

```powershell
# Timezone info
[System.TimeZoneInfo]::Local
# Convert to UTC
(Get-Date).ToUniversalTime()
```

## Quick Responses

Always try Python first; use the platform fallback only if Python fails.

**"What time is it?"**

```python
from datetime import datetime
print(datetime.now().astimezone().strftime("%H:%M:%S %Z"))
```

**"What's today's date?"**

```python
from datetime import datetime
print(datetime.now().astimezone().strftime("%A, %Y-%m-%d"))
```

**"What timezone am I in?"**

```python
from datetime import datetime
now = datetime.now().astimezone()
print(now.strftime("%Z"), now.strftime("%z"))
```
