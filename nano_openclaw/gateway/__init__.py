"""Gateway: daemon process owning AgentRuntime, plus the Backend abstraction.

The Backend Protocol (see backend.py) is the single contract spoken by the
TUI REPL, the WebUI, and the WebSocket TUI client. Two implementations:

- EmbeddedBackend (backend_embedded.py) — direct calls into a local AgentRuntime.
  Used when nano-openclaw runs single-process (default `tui` invocation).
- WebSocketBackend (cli/backend_websocket.py, Phase 5) — JSON-RPC over WS to
  a remote daemon.

Mirrors openclaw's tui-backend.ts dual-implementation pattern.
"""
