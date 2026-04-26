"""Entry point.

Mirrors the chain ``openclaw.mjs`` -> ``src/entry.ts`` -> ``src/run-main.ts``,
collapsed into one file because nano has no plugin loader, no auth-profile
resolution, and no telemetry init.
"""

from __future__ import annotations

import argparse
import os
import sys

from anthropic import Anthropic

from nano_openclaw.cli import repl
from nano_openclaw.loop import LoopConfig
from nano_openclaw.tools import ToolRegistry, build_default_registry

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nano-openclaw",
        description="Minimal educational reimplementation of OpenClaw's agent loop.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model id (default: {DEFAULT_MODEL})")
    parser.add_argument("--no-tools", action="store_true", help="Run as a plain chatbot, no tools registered.")
    parser.add_argument("--max-iterations", type=int, default=12, help="Max tool-use rounds per user turn.")
    parser.add_argument("--max-tokens", type=int, default=4096, help="Max tokens per assistant response.")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "error: ANTHROPIC_API_KEY is not set.\n"
            "  Get a key at https://console.anthropic.com and:\n"
            "    export ANTHROPIC_API_KEY=sk-ant-...   (Linux/macOS)\n"
            "    setx ANTHROPIC_API_KEY sk-ant-...     (Windows)",
            file=sys.stderr,
        )
        sys.exit(2)

    registry = ToolRegistry() if args.no_tools else build_default_registry()
    client = Anthropic(api_key=api_key)
    cfg = LoopConfig(
        model=args.model,
        max_iterations=args.max_iterations,
        max_tokens=args.max_tokens,
    )

    repl(registry, client=client, cfg=cfg)


if __name__ == "__main__":
    main()
