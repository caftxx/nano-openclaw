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
python bundled_skills/clawhub/scripts/clawhub_api.py search "calendar"
```

Output shows: `slug`, `displayName`, `summary`

Display results to user and ask which skill to install.

### 2. Install Skill

Install selected skill to workspace:

```bash
python bundled_skills/clawhub/scripts/clawhub_api.py install <slug> --workspace <workspace_dir>
```

**Behavior:**
- If skill already installed → CLI prompts: `Overwrite? [y/N]:`
- User confirms → skill downloads and extracts
- Installs to `<workspace>/skills/<slug>/`
- Use `--overwrite` flag to skip confirmation

### 3. Uninstall Skill

Remove an installed skill:

```bash
python bundled_skills/clawhub/scripts/clawhub_api.py uninstall <slug> --workspace <workspace_dir>
```

**Behavior:**
- CLI prompts: `Remove? [y/N]:`
- User confirms → skill directory deleted
- Use `--yes` or `-y` to skip confirmation

## User Confirmations

Both install (overwrite) and uninstall require user confirmation:

| Command | Prompt | Skip Flag |
|---------|--------|-----------|
| `install` | `Overwrite? [y/N]:` | `--overwrite` |
| `uninstall` | `Remove? [y/N]:` | `--yes` or `-y` |

**The CLI handles prompts automatically.** Just run the command and let user respond.

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