# nano-openclaw Architecture

This document defines the target layering for nano-openclaw. The goal is a
small, readable agent framework with clear ownership boundaries. The project
does not preserve old internal import paths during this refactor; behavior is
kept by re-exposing features through the new layers.

## Layers

Dependency direction is one-way:

```text
daemon / adapters / api
          |
          v
       services
          |
          v
        core
          |
          v
 config, session primitives, provider SDKs, filesystem primitives
```

### core

Pure agent execution primitives:

- model/provider streaming
- tool schemas and dispatch
- prompt construction
- compaction
- attachments and media normalization
- runtime assembly primitives

`core` must not import `daemon`, `api`, `adapters`, or UI/channel code.

### services

Application services that own product behavior:

- backend facade used by all frontends
- sessions and run lifecycle
- approvals
- runtime updates
- channel manager
- slash command registry and dispatch

Every user-facing adapter talks to the agent through services. Adapters must
not reach into `AgentRuntime` directly.

### api

Wire protocol only:

- JSON/WebSocket frame shapes
- request dispatch
- method handlers that translate RPC params into service calls
- payload serialization

The API layer should not contain session, runtime, or feature business logic.

### adapters

Human or platform-facing I/O:

- CLI/TUI
- WebUI and voice web surface
- channel adapters such as WeChat

Adapters translate user/platform events into service calls and translate
service output back to the platform format.

### daemon

Process lifecycle and composition:

- command-line daemon supervisor
- pidfile/status/start/stop/restart
- server bootstrap
- TLS binding
- wiring runtime, backend, API routes, WebUI, channels, and scheduler

Daemon code may compose layers, but domain logic belongs in services or
features.

### features

Feature modules own cohesive capabilities. A feature should expose only the
pieces it needs:

- `service.py` for domain behavior
- `tools.py` for model-callable tools
- `slash.py` for slash command registration
- `config.py` when feature-specific config helpers are needed

Slash command handlers are registered by features and routed by the shared
slash service. The dispatcher does not implement feature behavior.

### plugins

Plugins extend the system through a narrow API:

- register tools
- register hooks
- register slash commands
- register channel adapters
- register feature components

Plugins receive a `PluginApi` context, not direct access to mutable runtime
internals.

## Migration Map

| Old module | New owner |
| --- | --- |
| `gateway/backend.py` | `services/backend.py` |
| `gateway/backend_embedded.py` | `services/backend_embedded.py` |
| `gateway/agent_backend_session.py` | `services/agent_session.py` |
| `gateway/approval_broker.py` | `services/approval_broker.py` |
| `gateway/run_registry.py` | `services/runs.py` |
| `gateway/runtime_lock.py` | `services/runtime_update.py` |
| `gateway/protocol.py` | `api/protocol.py` |
| `gateway/context.py` | `api/context.py` |
| `gateway/ws_route.py` | `api/ws_route.py` |
| `gateway/methods/` | `api/methods/` |
| `gateway/backend_websocket.py` | `api/backend_websocket.py` |
| `gateway/cli.py` | `daemon/cli.py` |
| `gateway/server.py` | `daemon/server.py` |
| `gateway/pidfile.py` | `daemon/pidfile.py` |
| `gateway/restart.py` | `daemon/restart.py` |
| `cli.py` | `adapters/cli/repl.py` |
| `gateway/ws_repl.py` | `adapters/cli/ws_repl.py` |
| `gateway/webui/` | `adapters/webui/` |
| `channels/` | `adapters/channels/` plus `services/channels.py` |

## Compatibility Policy

This refactor does not keep legacy module paths or RPC names alive. Tests and
callers in this repository should move to the new names. The required
compatibility target is functional: existing capabilities must have an
equivalent path through the new architecture.
