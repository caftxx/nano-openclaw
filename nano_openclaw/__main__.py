"""Entry point.

Mirrors the chain ``openclaw.mjs`` -> ``src/entry.ts`` -> ``src/run-main.ts``,
collapsed into one file because nano has no plugin loader, no auth-profile
resolution, and no telemetry init.
"""

from __future__ import annotations

import argparse
import os
import sys

from nano_openclaw.cli import repl
from nano_openclaw.loop import LoopConfig
from nano_openclaw.provider import SUPPORTED_APIS
from nano_openclaw.tools import ToolRegistry, build_default_registry

_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-5-20250929",
    "openai": "gpt-4o",
}

_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nano-openclaw",
        description="Minimal educational reimplementation of OpenClaw's agent loop.",
    )
    parser.add_argument(
        "--api",
        choices=list(SUPPORTED_APIS),
        default="anthropic",
        help="Provider API to use (default: anthropic).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model id. Defaults to claude-sonnet-4-5-20250929 (anthropic) or gpt-4o (openai).",
    )
    parser.add_argument("--no-tools", action="store_true", help="Run as a plain chatbot, no tools registered.")
    parser.add_argument("--max-iterations", type=int, default=12, help="Max tool-use rounds per user turn.")
    parser.add_argument("--max-tokens", type=int, default=4096, help="Max tokens per assistant response.")
    args = parser.parse_args()

    api: str = args.api
    model: str = args.model or _DEFAULT_MODELS[api]
    env_key = _ENV_KEYS[api]

    api_key = os.environ.get(env_key)
    if not api_key:
        if api == "anthropic":
            hint = "  Get a key at https://console.anthropic.com"
        else:
            hint = "  Get a key at https://platform.openai.com/api-keys"
        print(
            f"error: {env_key} is not set.\n"
            f"{hint}\n"
            f"    export {env_key}=...   (Linux/macOS/Git Bash)\n"
            f"    setx   {env_key} ...   (Windows — open a new terminal after)",
            file=sys.stderr,
        )
        sys.exit(2)

    client = _build_client(api, api_key)
    registry = ToolRegistry() if args.no_tools else build_default_registry()
    cfg = LoopConfig(
        model=model,
        api=api,
        max_iterations=args.max_iterations,
        max_tokens=args.max_tokens,
    )

    repl(registry, client=client, cfg=cfg)


def _build_client(api: str, api_key: str):
    if api == "anthropic":
        from anthropic import Anthropic
        return Anthropic(api_key=api_key)
    if api == "openai":
        from openai import OpenAI
        return OpenAI(api_key=api_key)
    raise ValueError(f"unsupported api: {api!r}")


if __name__ == "__main__":
    main()
