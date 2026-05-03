---
name: clawhub
description: Search and install skills from ClawHub registry to your workspace.
user-invocable: true
metadata:
  openclaw:
    emoji: "🦞"
---

# ClawHub Skill

Search, install, and uninstall skills from the ClawHub registry (https://clawhub.ai).

## CLI Tool

This skill includes `scripts/clawhub_api.py` — a CLI tool to manage ClawHub skills.
The CLI is non-interactive: if overwrite or removal confirmation is needed, it tells you to ask the user first, then rerun with the required flag only after the user confirms.

### Usage

```bash
python scripts/clawhub_api.py <command> [options]
```

### Commands

| Command | Description |
|---------|-------------|
| `search <query>` | Search skills by keyword |
| `install <slug> --workspace <dir>` | Install a skill |
| `uninstall <slug> --workspace <dir>` | Remove an installed skill |

## Workflow

### 1. Search Skills

Search for skills matching a query:

```bash
python scripts/clawhub_api.py search "calendar"
```

Output shows: `slug`, `displayName`, `summary`

Display results to user and ask which skill to install.

### 2. Install Skill

Install selected skill to workspace:

```bash
python scripts/clawhub_api.py install <slug> --workspace <workspace_dir>
```

**Behavior:**
- If skill already installed without `--overwrite` → CLI exits and tells you to ask the user whether replacement is OK
- Installs to `<workspace>/skills/<slug>/`
- Use `--overwrite` only after the user confirms replacement

### 3. Uninstall Skill

Remove an installed skill:

```bash
python scripts/clawhub_api.py uninstall <slug> --workspace <workspace_dir>
```

**Behavior:**
- Without `--yes` or `-y` → CLI exits and tells you to ask the user whether removal is OK
- With `--yes` → skill directory deleted

## Non-Interactive Confirmations

Install overwrite and uninstall are explicit, non-interactive operations:

| Command | Prompt | Skip Flag |
|---------|--------|-----------|
| `install` | exits with rerun instruction | `--overwrite` |
| `uninstall` | exits with rerun instruction | `--yes` or `-y` |

**The CLI never waits for stdin.** Agents should inspect the output, ask the user for confirmation, and rerun with the requested flag only after the user agrees.

## Installation Path

Skills are installed to: `<workspace>/skills/<slug>/`

The installed skill will be available in the **next session** after skills reload.

## Examples

### Search

```bash
python scripts/clawhub_api.py search "weather" --limit 5
```

### Install

```bash
python scripts/clawhub_api.py install weather --workspace /path/to/project
```

### Uninstall

```bash
python scripts/clawhub_api.py uninstall weather --workspace /path/to/project
```

## Error Handling

| Error | CLI Output |
|-------|------------|
| Search fails (network) | `Search failed: <error>` |
| Download fails | `Download failed (HTTP <code>): <error>` |
| Extract fails | `Extract failed: <error>` |
| Skill not installed | `Skill '<slug>' not installed.` |

## Notes

- ClawHub URL: https://clawhub.ai
- No authentication required for search/download
- Skills are downloaded as zip and extracted automatically
- SKILL.md must exist in extracted files (verified by CLI)
