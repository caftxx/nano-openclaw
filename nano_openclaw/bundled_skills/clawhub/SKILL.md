---
name: clawhub
description: Search and install skills from ClawHub registry to your workspace.
user-invocable: true
metadata:
  openclaw:
    emoji: "🦞"
---

# ClawHub Skill

Search, inspect, install, update, and uninstall skills from the ClawHub registry (https://clawhub.ai).

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
| `info <slug>` | Show skill details |
| `install <slug> --workspace <dir>` | Install a skill |
| `update <slug> --workspace <dir>` | Update an installed skill when ClawHub has a newer version |
| `uninstall <slug> --workspace <dir>` | Remove an installed skill |

## Workflow

### 1. Search Skills

Search for skills matching a query:

```bash
python scripts/clawhub_api.py search "calendar"
```

Output shows: `slug`, `displayName`, `summary`

Display results to user and ask which skill to install.

### 2. Skill Info

Show ClawHub detail data for a skill:

```bash
python scripts/clawhub_api.py info memory
```

The public detail page for `memory` is:

```text
https://clawhub.ai/skill/memory
```

Output shows: detail page URL, latest version, owner, summary, changelog, license, supported OS, downloads, stars, version count, and install counts.

### 3. Install Skill

Install selected skill to workspace:

```bash
python scripts/clawhub_api.py install <slug> --workspace <workspace_dir>
```

**Behavior:**
- If skill already installed without `--overwrite` → CLI exits and tells you to ask the user whether replacement is OK
- Installs to `<workspace>/skills/<slug>/`
- Use `--overwrite` only after the user confirms replacement

### 4. Update Skill

Compare the local installed version with the latest ClawHub version and update only when they differ:

```bash
python scripts/clawhub_api.py update <slug> --workspace <workspace_dir>
```

**Behavior:**
- Reads local version from `<workspace>/skills/<slug>/SKILL.md` frontmatter
- Fetches latest version from `https://clawhub.ai/api/v1/skills/<slug>`
- If versions match → reports already up to date
- If versions differ → downloads latest zip and replaces `<workspace>/skills/<slug>/`
- If the skill is not installed → exits and tells you to install first
- Use `--force` to reinstall latest even when versions match

### 5. Uninstall Skill

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

### Info

```bash
python scripts/clawhub_api.py info memory
```

### Update

```bash
python scripts/clawhub_api.py update memory --workspace /path/to/project
```

### Uninstall

```bash
python scripts/clawhub_api.py uninstall weather --workspace /path/to/project
```

## Error Handling

| Error | CLI Output |
|-------|------------|
| Search fails (network) | `Search failed: <error>` |
| Info fails (network/API) | `Info failed: <error>` |
| Download fails | `Download failed (HTTP <code>): <error>` |
| Extract fails | `Extract failed: <error>` |
| Skill not installed | `Skill '<slug>' not installed.` |
| Update target missing | `Skill '<slug>' not installed. Install it first.` |

## Notes

- ClawHub URL: https://clawhub.ai
- No authentication required for search/download
- Skills are downloaded as zip and extracted automatically
- SKILL.md must exist in extracted files (verified by CLI)
