"""Approval UI with Rich overlay.

Uses rich.Panel to create an overlay-style approval prompt
that interrupts the streaming output.
"""

import json
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich import markup

from nano_openclaw.approvals.types import ApprovalDecision, ApprovalRequest


class ApprovalUI:
    """Rich-based approval UI."""
    
    def __init__(self, console: Console) -> None:
        self.console = console
    
    def render_request(self, request: ApprovalRequest) -> None:
        """Render the approval request as a panel."""
        tool_info = f"[bold]{markup.escape(request.tool_name)}[/]"
        args_display = self._format_args(request.tool_args)
        
        risk_colors = {"low": "yellow", "medium": "orange1", "high": "red"}
        risk_color = risk_colors.get(request.risk_level, "yellow")
        
        content = Text.from_markup(
            f"{tool_info}\n"
            f"args: {markup.escape(args_display)}\n"
            f"risk: [{risk_color}]{request.risk_level}[/]\n"
            f"reason: {markup.escape(request.reason)}"
        )
        
        self.console.print(
            Panel(
                content,
                title="[bold red]Approval Required[/]",
                border_style="red",
                expand=False,
            )
        )
    
    def prompt_decision(self, request: ApprovalRequest) -> ApprovalDecision:
        """Prompt user for decision and return it.
        
        Shows three options:
        - y: allow once
        - Y: allow always (remember)
        - n: deny
        
        Returns ApprovalDecision.
        """
        self.console.print(
            "[dim]y[/] = allow once  |  [dim]Y[/] = allow always  |  [dim]n[/] = deny"
        )
        
        while True:
            try:
                response = self.console.input("[bold cyan]?[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                return ApprovalDecision.DENY
            
            if response == "y":
                return ApprovalDecision.ALLOW_ONCE
            elif response == "Y":
                return ApprovalDecision.ALLOW_ALWAYS
            elif response == "n":
                return ApprovalDecision.DENY
            else:
                self.console.print("[red]invalid choice: y/Y/n[/]")
    
    def render_denied(self, request: ApprovalRequest) -> None:
        """Render denial message."""
        self.console.print(
            Panel(
                Text.from_markup(f"[red]Denied:[/] {markup.escape(request.tool_name)}"),
                border_style="red",
                expand=False,
            )
        )
    
    def render_allowed(self, request: ApprovalRequest, decision: ApprovalDecision) -> None:
        """Render approval message."""
        mode = "once" if decision == ApprovalDecision.ALLOW_ONCE else "always"
        self.console.print(
            Panel(
                Text.from_markup(
                    f"[green]Allowed ({mode}):[/] {markup.escape(request.tool_name)}"
                ),
                border_style="green",
                expand=False,
            )
        )
    
    def _format_args(self, args: dict) -> str:
        """Format args dict for display."""
        try:
            rendered = json.dumps(args, ensure_ascii=False)
        except Exception:
            rendered = str(args)
        if len(rendered) > 60:
            rendered = rendered[:57] + "..."
        return rendered