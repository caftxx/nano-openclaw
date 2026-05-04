"""ClawHub CLI tool.

This CLI is intentionally non-interactive. When a destructive action needs
confirmation, it prints the required retry flag and exits instead of waiting
for stdin.

Usage:
    python clawhub_api.py search <query> [--limit 10]
    python clawhub_api.py info <slug>
    python clawhub_api.py install <slug> --workspace <dir> [--overwrite]
    python clawhub_api.py update <slug> --workspace <dir> [--force]
    python clawhub_api.py uninstall <slug> --workspace <dir> [--yes]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

CLAWHUB_BASE_URL = "https://clawhub.ai/api/v1"


@dataclass
class ClawHubSkill:
    slug: str
    displayName: str
    summary: str
    version: str | None
    score: float
    downloads: int = 0
    stars: int = 0
    updatedAt: int = 0


@dataclass
class ClawHubSkillDetail:
    slug: str
    displayName: str
    summary: str
    latestVersion: str | None
    changelog: str | None
    ownerHandle: str | None
    ownerName: str | None
    downloads: int = 0
    installsCurrent: int = 0
    installsAllTime: int = 0
    stars: int = 0
    versions: int = 0
    updatedAt: int = 0
    license: str | None = None
    os: list[str] | None = None


def _format_timestamp_ms(value: int) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d")


def _format_value(value: object) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "—"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def get_skill_stats(client: httpx.Client, slug: str) -> dict:
    """Get skill stats (downloads, stars) from detail API."""
    try:
        r = client.get(f"{CLAWHUB_BASE_URL}/skills/{slug}")
        r.raise_for_status()
        data = r.json()
        stats = data.get("skill", {}).get("stats", {})
        return {
            "downloads": stats.get("downloads", 0),
            "stars": stats.get("stars", 0),
        }
    except Exception:
        return {"downloads": 0, "stars": 0}


def get_skill_detail(slug: str) -> ClawHubSkillDetail:
    """Fetch detail data for one skill from ClawHub."""
    client = httpx.Client(timeout=10.0)
    try:
        r = client.get(f"{CLAWHUB_BASE_URL}/skills/{slug}")
        r.raise_for_status()
        data = r.json()
    finally:
        client.close()

    skill = data.get("skill", {})
    latest = data.get("latestVersion") or {}
    owner = data.get("owner") or {}
    stats = skill.get("stats") or {}
    metadata = data.get("metadata") or {}
    return ClawHubSkillDetail(
        slug=skill.get("slug", slug),
        displayName=skill.get("displayName", slug),
        summary=skill.get("summary", ""),
        latestVersion=latest.get("version") or (skill.get("tags") or {}).get("latest"),
        changelog=latest.get("changelog"),
        ownerHandle=owner.get("handle"),
        ownerName=owner.get("displayName"),
        downloads=stats.get("downloads", 0),
        installsCurrent=stats.get("installsCurrent", 0),
        installsAllTime=stats.get("installsAllTime", 0),
        stars=stats.get("stars", 0),
        versions=stats.get("versions", 0),
        updatedAt=skill.get("updatedAt", 0),
        license=latest.get("license"),
        os=metadata.get("os"),
    )


def search_skills(query: str, limit: int = 10) -> list[ClawHubSkill]:
    """Search ClawHub API and fetch stats for each result."""
    client = httpx.Client(timeout=10.0)
    try:
        r = client.get(f"{CLAWHUB_BASE_URL}/search", params={"q": query})
        r.raise_for_status()
        results = r.json().get("results", [])[:limit]
        
        skills = []
        for item in results:
            stats = get_skill_stats(client, item["slug"])
            skills.append(
                ClawHubSkill(
                    slug=item["slug"],
                    displayName=item["displayName"],
                    summary=item["summary"],
                    version=item.get("version"),
                    score=item.get("score", 0.0),
                    downloads=stats["downloads"],
                    stars=stats["stars"],
                    updatedAt=item.get("updatedAt", 0),
                )
            )
        return skills
    finally:
        client.close()


def read_local_skill_version(skill_dir: Path) -> str | None:
    """Read version from a local skill SKILL.md frontmatter block."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = skill_md.read_text().splitlines()

    if not lines or lines[0].strip() != "---":
        return None

    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            return None
        if stripped.startswith("version:"):
            return stripped.split(":", 1)[1].strip().strip("\"'")
    return None


def install_skill(slug: str, workspace_dir: Path, overwrite: bool = False) -> tuple[bool, str]:
    """Download and install skill."""
    target_dir = workspace_dir / "skills" / slug

    if target_dir.exists() and not overwrite:
        return False, f"Skill '{slug}' already installed at {target_dir}. Use --overwrite to replace."

    client = httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        r = client.get(f"{CLAWHUB_BASE_URL}/download", params={"slug": slug, "tag": "latest"})
        r.raise_for_status()
        zip_bytes = r.content
    except httpx.HTTPStatusError as e:
        return False, f"Download failed (HTTP {e.response.status_code}): {e}"
    except httpx.HTTPError as e:
        return False, f"Download failed: {e}"
    finally:
        client.close()

    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(zip_bytes)
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path, "r") as zf:
            zf.extractall(target_dir)
        Path(tmp_path).unlink(missing_ok=True)
    except zipfile.BadZipFile:
        return False, "Downloaded file is not a valid zip archive."
    except Exception as e:
        return False, f"Extract failed: {e}"

    skill_md = target_dir / "SKILL.md"
    if not skill_md.exists():
        for subdir in target_dir.iterdir():
            if subdir.is_dir() and (subdir / "SKILL.md").exists():
                for item in subdir.iterdir():
                    item.rename(target_dir / item.name)
                subdir.rmdir()
                break

    if not (target_dir / "SKILL.md").exists():
        return False, f"Installed but SKILL.md not found in '{slug}'"

    return True, f"Skill '{slug}' installed to {target_dir}"


def update_skill(slug: str, workspace_dir: Path, force: bool = False) -> tuple[bool, str]:
    """Update an installed skill when ClawHub has a different latest version."""
    target_dir = workspace_dir / "skills" / slug
    if not target_dir.exists():
        return False, f"Skill '{slug}' not installed. Install it first."

    detail = get_skill_detail(slug)
    local_version = read_local_skill_version(target_dir)
    remote_version = detail.latestVersion

    if not remote_version:
        return False, f"Could not determine latest ClawHub version for '{slug}'."

    if local_version == remote_version and not force:
        return True, f"Skill '{slug}' is already up to date (version {local_version})."

    success, msg = install_skill(slug, workspace_dir, overwrite=True)
    if not success:
        return False, msg

    before = local_version or "unknown"
    return True, f"Skill '{slug}' updated from {before} to {remote_version}. {msg}"


def uninstall_skill(slug: str, workspace_dir: Path) -> tuple[bool, str]:
    """Remove skill."""
    target_dir = workspace_dir / "skills" / slug

    if not target_dir.exists():
        return False, f"Skill '{slug}' not installed."

    try:
        shutil.rmtree(target_dir)
        return True, f"Skill '{slug}' removed from {target_dir}"
    except Exception as e:
        return False, f"Remove failed: {e}"


def cmd_search(args: argparse.Namespace) -> None:
    """Handle search command."""
    try:
        results = search_skills(args.query, args.limit)
        if not results:
            print("No results found.")
            return
        
        # Sort by downloads descending
        results.sort(key=lambda s: s.downloads, reverse=True)
        
        print(f"Found {len(results)} skills:\n")
        print(f"  {'Slug':20} {'Downloads':>10} {'Stars':>5}  {'Updated':>10}  {'Summary'}")
        print(f"  {'-'*20} {'-'*10} {'-'*5}  {'-'*10}  {'-'*40}")
        for s in results:
            summary_preview = s.summary[:40] + "..." if len(s.summary) > 40 else s.summary
            downloads_str = f"{s.downloads:,}" if s.downloads else "—"
            stars_str = str(s.stars) if s.stars else "—"
            updated_str = datetime.fromtimestamp(s.updatedAt / 1000).strftime("%Y-%m-%d") if s.updatedAt else "—"
            print(f"  {s.slug:20} {downloads_str:>10} {stars_str:>5}  {updated_str:>10}  {summary_preview}")
    except Exception as e:
        print(f"Search failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_info(args: argparse.Namespace) -> None:
    """Handle info command."""
    try:
        detail = get_skill_detail(args.slug)
    except httpx.HTTPStatusError as e:
        print(f"Info failed (HTTP {e.response.status_code}): {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Info failed: {e}", file=sys.stderr)
        sys.exit(1)

    owner = detail.ownerName
    if detail.ownerHandle:
        owner = f"{owner or detail.ownerHandle} (@{detail.ownerHandle})"

    print(f"{detail.displayName} ({detail.slug})")
    print(f"URL: https://clawhub.ai/skill/{detail.slug}")
    print(f"Version: {_format_value(detail.latestVersion)}")
    print(f"Updated: {_format_timestamp_ms(detail.updatedAt)}")
    print(f"Owner: {_format_value(owner)}")
    print(f"Summary: {_format_value(detail.summary)}")
    print(f"Changelog: {_format_value(detail.changelog)}")
    print(f"License: {_format_value(detail.license)}")
    print(f"OS: {_format_value(detail.os)}")
    print(f"Stats: {_format_value(detail.downloads)} downloads, {_format_value(detail.stars)} stars, {_format_value(detail.versions)} versions")
    print(f"Installs: {_format_value(detail.installsCurrent)} current, {_format_value(detail.installsAllTime)} all-time")


def cmd_install(args: argparse.Namespace) -> None:
    """Handle install command."""
    ws = Path(args.workspace)
    if not ws.is_dir():
        print(f"Workspace directory not found: {ws}", file=sys.stderr)
        sys.exit(1)

    target = ws / "skills" / args.slug

    if target.exists() and not args.overwrite:
        print(f"Skill '{args.slug}' already installed at {target}", file=sys.stderr)
        print("User confirmation required before replacing the existing skill.", file=sys.stderr)
        print(
            f"If the user confirms, re-run with --overwrite: "
            f"install {args.slug} --workspace {ws} --overwrite",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Installing '{args.slug}'...")
    success, msg = install_skill(args.slug, ws, overwrite=args.overwrite)
    if success:
        print(msg)
    else:
        print(msg, file=sys.stderr)
        sys.exit(1)


def cmd_update(args: argparse.Namespace) -> None:
    """Handle update command."""
    ws = Path(args.workspace)
    if not ws.is_dir():
        print(f"Workspace directory not found: {ws}", file=sys.stderr)
        sys.exit(1)

    target = ws / "skills" / args.slug
    if not target.exists():
        print(f"Skill '{args.slug}' not installed. Install it first.", file=sys.stderr)
        sys.exit(1)

    local_version = read_local_skill_version(target) or "unknown"
    try:
        detail = get_skill_detail(args.slug)
    except Exception as e:
        print(f"Update failed: {e}", file=sys.stderr)
        sys.exit(1)

    remote_version = detail.latestVersion or "unknown"
    print(f"Local version: {local_version}")
    print(f"ClawHub version: {remote_version}")

    if local_version == remote_version and not args.force:
        print(f"Skill '{args.slug}' is already up to date.")
        return

    if remote_version == "unknown":
        print(f"Could not determine latest ClawHub version for '{args.slug}'.", file=sys.stderr)
        sys.exit(1)

    print(f"Updating '{args.slug}'...")
    success, msg = install_skill(args.slug, ws, overwrite=True)
    if success:
        print(msg)
    else:
        print(msg, file=sys.stderr)
        sys.exit(1)


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Handle uninstall command."""
    ws = Path(args.workspace)
    if not ws.is_dir():
        print(f"Workspace directory not found: {ws}", file=sys.stderr)
        sys.exit(1)

    target = ws / "skills" / args.slug

    if not target.exists():
        print(f"Skill '{args.slug}' not installed.")
        return

    if not args.yes:
        print(f"Skill '{args.slug}' is installed at {target}", file=sys.stderr)
        print("User confirmation required before removing the installed skill.", file=sys.stderr)
        print(
            f"If the user confirms, re-run with --yes: "
            f"uninstall {args.slug} --workspace {ws} --yes",
            file=sys.stderr,
        )
        sys.exit(1)

    success, msg = uninstall_skill(args.slug, ws)
    if success:
        print(msg)
    else:
        print(msg, file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="clawhub_api.py",
        description="ClawHub CLI tool for searching, inspecting, installing, updating, and uninstalling skills.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Commands")

    p_search = subparsers.add_parser("search", help="Search skills on ClawHub")
    p_search.add_argument("query", help="Search query (e.g., 'calendar', 'weather')")
    p_search.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    p_search.set_defaults(func=cmd_search)

    p_info = subparsers.add_parser("info", help="Show skill details from ClawHub")
    p_info.add_argument("slug", help="Skill slug (e.g., 'memory')")
    p_info.set_defaults(func=cmd_info)

    p_install = subparsers.add_parser("install", help="Install a skill from ClawHub")
    p_install.add_argument("slug", help="Skill slug from search results")
    p_install.add_argument("--workspace", required=True, help="Workspace directory path")
    p_install.add_argument("--overwrite", action="store_true", help="Overwrite if already installed")
    p_install.set_defaults(func=cmd_install)

    p_update = subparsers.add_parser("update", help="Update an installed skill from ClawHub")
    p_update.add_argument("slug", help="Skill slug to update")
    p_update.add_argument("--workspace", required=True, help="Workspace directory path")
    p_update.add_argument("--force", action="store_true", help="Reinstall even if versions match")
    p_update.set_defaults(func=cmd_update)

    p_uninstall = subparsers.add_parser("uninstall", help="Uninstall a skill")
    p_uninstall.add_argument("slug", help="Skill slug to remove")
    p_uninstall.add_argument("--workspace", required=True, help="Workspace directory path")
    p_uninstall.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    p_uninstall.set_defaults(func=cmd_uninstall)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
