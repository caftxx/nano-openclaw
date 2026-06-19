from rich.console import Console

from nano_openclaw.approvals import ApprovalManager, ApprovalPolicy
from nano_openclaw.features.subagents.runner import SubagentRunner
from nano_openclaw.core.tools import build_core_registry


def test_subagent_registry_does_not_inherit_interactive_approval_console():
    parent = build_core_registry()
    parent.console = Console()
    parent.approval_manager = ApprovalManager(
        ApprovalPolicy(ask_mode="always", dangerous_tools=["write_file"], allowlist=[])
    )
    parent.approval_handler = lambda _request, _context=None: None

    child = SubagentRunner()._build_filtered_registry(parent)

    assert child.approval_manager is parent.approval_manager
    assert child.console is None
    assert child.approval_handler is None


def test_subagent_fallback_registry_binds_skill_runtime():
    child = SubagentRunner()._build_filtered_registry(None)

    assert child.skill_installer is not None
    assert child.skill_usage_recorder is not None
