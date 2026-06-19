"""Lightweight plugin and hook API for nano-openclaw."""

from nano_openclaw.plugins.registry import HookRegistry
from nano_openclaw.plugins.api import HookHandler, HookName, Plugin, PluginApi

__all__ = [
    "HookHandler",
    "HookName",
    "HookRegistry",
    "Plugin",
    "PluginApi",
]
