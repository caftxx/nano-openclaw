from rich.console import Console

from nano_openclaw.approvals import ApprovalManager, ApprovalPolicy
from nano_openclaw.subagent.runner import SubagentRunner
from nano_openclaw.tools import build_core_registry


def test_subagent_registry_does_not_inherit_interactive_approval_console():
    parent = build_core_registry()
    parent.console = Console()
    parent.approval_manager = ApprovalManager(
        ApprovalPolicy(ask_mode="always", dangerous_tools=["write_file"], allowlist=[])
    )

    child = SubagentRunner()._build_filtered_registry(parent)

    assert child.approval_manager is parent.approval_manager
    assert child.console is None
