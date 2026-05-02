"""Load and resolve exec-approvals policy from exec-approvals.json.

Mirrors openclaw's resolveExecApprovalsFromFile() in src/infra/exec-approvals.ts:
- File: {stateDir}/exec-approvals.json
- Resolution order: defaults → agents.* (wildcard) → agents.{agentId}
- Allowlist: wildcard entries + agent entries (wildcard first)

ExecApprovalsFile format (same as openclaw):
  {
    "version": 1,
    "defaults": { "ask": "off", "security": "full" },
    "agents": {
      "*":       { "ask": "...", "security": "...", "allowlist": [...] },
      "default": { "ask": "...", "security": "...", "allowlist": [...] }
    }
  }
"""

import json
from pathlib import Path
from typing import Optional

from nano_openclaw.approvals.types import AllowlistEntry, ApprovalPolicy, DEFAULT_AGENT_ID

EXEC_APPROVALS_VERSION = 1

# Mirrors openclaw's DEFAULT_SECURITY / DEFAULT_ASK (src/infra/exec-approvals.ts:169-170)
DEFAULT_SECURITY = "full"
DEFAULT_ASK = "off"


def resolve_exec_approvals_path(state_dir: Path) -> Path:
    """Return path to exec-approvals.json. Mirrors resolveExecApprovalsPath()."""
    return state_dir / "exec-approvals.json"


def load_exec_approvals(
    state_dir: Path,
    agent_id: str = DEFAULT_AGENT_ID,
    overrides: Optional[dict] = None,
) -> ApprovalPolicy:
    """Load and resolve exec-approvals policy.

    Mirrors resolveExecApprovalsFromFile(): defaults → wildcard → agent.
    Returns ApprovalPolicy with ask_mode, security_mode, allowlist, and
    allow_always_store pointing to exec-approvals.json.
    """
    path = resolve_exec_approvals_path(state_dir)
    file_data = _load_file(path)
    return _resolve(file_data, agent_id, path, overrides or {})


def _load_file(path: Path) -> dict:
    if not path.exists():
        return {"version": EXEC_APPROVALS_VERSION}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == EXEC_APPROVALS_VERSION:
            return data
    except (json.JSONDecodeError, IOError):
        pass
    return {"version": EXEC_APPROVALS_VERSION}


def _resolve(
    file_data: dict,
    agent_id: str,
    path: Path,
    overrides: dict,
) -> ApprovalPolicy:
    """Mirrors resolveExecApprovalsFromFile() resolution logic."""
    defaults = file_data.get("defaults") or {}
    agents = file_data.get("agents") or {}
    wildcard = agents.get("*") or {}
    agent = agents.get(agent_id) or {}

    # Resolution: defaults → wildcard → agent → overrides (later wins)
    ask = (
        overrides.get("ask")
        or _first(agent.get("ask"), wildcard.get("ask"), defaults.get("ask"))
        or DEFAULT_ASK
    )
    security = (
        overrides.get("security")
        or _first(agent.get("security"), wildcard.get("security"), defaults.get("security"))
        or DEFAULT_SECURITY
    )

    # Allowlist: wildcard entries first, then agent entries (mirrors line 750-753)
    allowlist: list[AllowlistEntry] = []
    for raw in [*(wildcard.get("allowlist") or []), *(agent.get("allowlist") or [])]:
        try:
            allowlist.append(AllowlistEntry(**raw))
        except (TypeError, ValueError):
            pass

    return ApprovalPolicy(
        agent_id=agent_id,
        ask_mode=ask,
        security_mode=security,
        allow_always_store=str(path),
        allowlist=allowlist,
    )


def _first(*values: object) -> object:
    """Return first non-None, non-empty-string value."""
    for v in values:
        if v is not None and v != "":
            return v
    return None
