"""WeChat bot main loop for nano-openclaw.

Connects to iLink WeChat API via long-polling and routes each message through
AgentSession.run_turn(), sending the reply back via iLink.

Each WeChat user gets an isolated history list (per-user session).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any
import base64

import httpx

from nano_openclaw.attachments import PromptAttachment
from nano_openclaw.logger import get_logger
from nano_openclaw.loop import AgentSession, TextDelta
from nano_openclaw.runtime import AgentRuntime, build_agent_runtime
from nano_openclaw.tools import ToolRegistry
from nano_openclaw.wechat.ilink import (
    download_wechat_file,
    download_wechat_image,
    extract_file_items,
    extract_image_items,
    extract_text,
    get_typing_ticket,
    get_updates,
    send_text,
    send_typing,
)

log = get_logger(__name__)


def _clone_registry(registry: ToolRegistry) -> ToolRegistry:
    clone = ToolRegistry(
        _tools=dict(registry._tools),
        approval_manager=registry.approval_manager,
        console=registry.console,
        _workspace_dir=registry._workspace_dir,
        _state_dir=registry._state_dir,
        _allow_global_pip=registry._allow_global_pip,
    )
    clone.set_session_status_context(**registry._session_status_context)
    clone.set_eligible_skills(dict(registry._eligible_skills))
    hook_reg = registry.hook_registry()
    if hook_reg is not None:
        clone.set_hook_registry(hook_reg)
    return clone


@dataclass
class WechatBot:
    runtime: AgentRuntime
    base_url: str
    token: str
    poll_timeout: int = 35
    typing_interval: int = 5
    # uid -> history list (in-memory per-user session)
    _sessions: dict[str, list[Any]] = field(default_factory=dict)

    def _get_or_create_history(self, uid: str) -> list[Any]:
        if uid not in self._sessions:
            self._sessions[uid] = []
        return self._sessions[uid]

    async def _handle_slash_command(self, uid: str, cmd: str) -> str | None:
        """Handle slash commands like /clear, /help, /tools, etc.

        Returns reply text if command matched, None if not a command.
        """
        cmd_lower = cmd.lower().strip()

        if cmd_lower in ("/quit", "/exit", "/q"):
            return "⚠️ **Bot cannot quit via WeChat command.** Send `/help` for available commands."

        if cmd_lower == "/clear":
            history = self._get_or_create_history(uid)
            history.clear()
            return "✅ **History cleared** for this session."

        if cmd_lower == "/new":
            history = self._get_or_create_history(uid)
            history.clear()
            return "✅ **New session started** (history cleared)."

        if cmd_lower == "/help":
            return (
                "📖 **Commands**\n\n"
                "- `/clear` — Clear history\n"
                "- `/new` — New session\n"
                "- `/help` — Show this help\n"
                "- `/context` — Show context stats\n"
                "- `/compact` — Compact context\n"
                "- `/tools` — List available tools\n"
                "- `/skills` — List installed skills\n"
                "- `/hooks` — List registered hooks\n"
                "- `/plugins` — List loaded plugins\n"
                "- `/active-memory` — Active memory status\n"
                "- `/dreaming` — Dreaming status\n"
                "- `/subagents` — Subagent status\n\n"
                "Anything else → sent to AI"
            )

        if cmd_lower == "/context":
            history = self._get_or_create_history(uid)
            msg_count = len(history)
            user_msgs = sum(1 for m in history if m.get("role") == "user")
            assistant_msgs = sum(1 for m in history if m.get("role") == "assistant")
            from nano_openclaw.compact import estimate_tokens
            tokens = estimate_tokens(history)
            return (
                f"📊 **Context stats**\n\n"
                f"- Messages: `{msg_count}` (user: `{user_msgs}`, assistant: `{assistant_msgs}`)\n"
                f"- Estimated tokens: `{tokens}`"
            )

        if cmd_lower == "/compact":
            history = self._get_or_create_history(uid)
            cfg = self.runtime.cfg
            if len(history) < cfg.context_recent_turns * 2:
                return "📊 **Compact**: Not enough history to compact."
            from nano_openclaw.compact import compact_if_needed
            try:
                await compact_if_needed(
                    history,
                    budget=1,
                    client=self.runtime.client,
                    model=cfg.model,
                    api=cfg.api,
                    threshold_ratio=1.0,
                    recent_turns=cfg.context_recent_turns,
                )
                from nano_openclaw.compact import estimate_tokens
                new_tokens = estimate_tokens(history)
                return f"✅ **Compacted**\n\nNew token estimate: `{new_tokens}`"
            except Exception as exc:
                return f"❌ **Compact failed**: {exc}"

        if cmd_lower == "/tools":
            tools = list(self.runtime.registry._tools.keys())
            tool_list = "- " + "\n- ".join(f"`{t}`" for t in sorted(tools))
            return f"🔧 **Tools** ({len(tools)})\n\n{tool_list}"

        if cmd_lower == "/skills":
            from nano_openclaw.skills import get_or_load_skills, filter_eligible_skills
            workspace = self.runtime.cfg.workspace_dir
            if workspace:
                entries = get_or_load_skills(
                    workspace,
                    self.runtime.cfg.session_key,
                    extra_dirs=self.runtime.cfg.extra_skill_dirs,
                    max_bytes=self.runtime.cfg.max_skill_file_bytes,
                )
                eligible = filter_eligible_skills(entries, skill_filter=self.runtime.cfg.skill_filter)
                names = [e.skill.name for e in eligible if e.eligible]
                skill_list = "- " + "\n- ".join(f"`{n}`" for n in sorted(names))
                return f"🧩 **Skills** ({len(names)})\n\n{skill_list}"
            return "🧩 **Skills**: (no workspace configured)"

        if cmd_lower == "/hooks":
            hooks = self.runtime.hook_registry
            hook_list = hooks._hooks if hooks else []
            if hook_list:
                hook_events = [h.event for h in hook_list]
                events_str = "- " + "\n- ".join(f"`{e}`" for e in sorted(set(hook_events)))
                return f"🪝 **Hooks** ({len(hook_list)} callbacks)\n\n{events_str}"
            return "🪝 **Hooks**: No hooks registered"

        if cmd_lower == "/plugins":
            hooks = self.runtime.hook_registry
            hook_list = hooks._hooks if hooks else []
            if hook_list:
                plugin_names = set(h.plugin_name for h in hook_list)
                plugins_str = "- " + "\n- ".join(f"`{p}`" for p in sorted(plugin_names))
                return f"🔌 **Plugins** ({len(plugin_names)})\n\n{plugins_str}"
            return "🔌 **Plugins**: No plugins loaded"

        if cmd_lower.startswith("/active-memory"):
            cfg = self.runtime.cfg
            am_cfg = cfg.active_memory_config
            if not am_cfg:
                return "🧠 **Active Memory**: Not configured."
            return (
                f"🧠 **Active Memory**\n\n"
                f"- Enabled: `{am_cfg.enabled}`\n"
                f"- Query mode: `{am_cfg.query_mode.value}`\n"
                f"- Prompt style: `{am_cfg.prompt_style.value}`\n"
                f"- Timeout: `{am_cfg.timeout_ms}ms`"
            )

        if cmd_lower.startswith("/dreaming"):
            cfg = self.runtime.cfg
            dc = cfg.dreaming_config
            if not dc or not dc.enabled:
                return "💤 **Dreaming**: Not configured or disabled."
            from nano_openclaw.memory.dreaming import get_dreaming_status
            workspace_dir = str(cfg.workspace_dir) if cfg.workspace_dir else ""
            status = get_dreaming_status(workspace_dir, dc)
            return (
                f"💤 **Dreaming**\n\n"
                f"- Enabled: `{status.get('enabled', False)}`\n"
                f"- Frequency: `{dc.frequency}`\n"
                f"- Min score: `{dc.min_score}`\n"
                f"- Max promotions: `{dc.max_promotions}`"
            )

        if cmd_lower.startswith("/subagents"):
            sa_cfg = self.runtime.config.subagents
            return (
                f"🤖 **Subagents**\n\n"
                f"- Max concurrent: `{sa_cfg.max_concurrent}`\n"
                f"- Max spawn depth: `{sa_cfg.max_spawn_depth}`\n"
                f"- Timeout: `{sa_cfg.run_timeout_seconds}s`"
            )

        # Not a recognized command, return None to let agent handle it
        return None

    async def run(self) -> None:
        """Main long-poll loop. Runs until cancelled."""
        buf = ""
        log.info("wechat.start", f"WeChat bot started (base_url={self.base_url})")
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    resp = await get_updates(
                        client, self.base_url, self.token, buf, self.poll_timeout
                    )
                    ret = resp.get("ret")
                    if ret not in (0, None):
                        log.warning("wechat.poll.error", f"iLink getUpdates ret={ret}, backing off")
                        await asyncio.sleep(5)
                        continue
                    buf = resp.get("get_updates_buf", buf)
                    for msg in resp.get("msgs", []):
                        asyncio.create_task(self._handle_message(msg))
                except httpx.HTTPStatusError as exc:
                    log.warning("wechat.poll.error", f"iLink HTTP error {exc.response.status_code}, backing off")
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("wechat.poll.error", f"poll error: {exc}, backing off")
                    await asyncio.sleep(5)

    async def _keep_typing(
        self,
        client: httpx.AsyncClient,
        uid: str,
        ctx: str,
        stop: asyncio.Event,
    ) -> None:
        """Send typing indicator and keep it alive until stop is set."""
        ticket = ""
        try:
            ticket = await get_typing_ticket(client, self.base_url, self.token, uid, ctx)
            if not ticket:
                return
            while not stop.is_set():
                try:
                    await send_typing(client, self.base_url, self.token, uid, ticket, status=1)
                except Exception as exc:
                    log.debug("wechat.typing.error", f"typing keepalive failed for {uid:.16}: {exc}")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.typing_interval)
                except asyncio.TimeoutError:
                    pass
        except Exception as exc:
            log.debug("wechat.typing.error", f"typing setup failed for {uid:.16}: {exc}")
        finally:
            if ticket:
                try:
                    await send_typing(client, self.base_url, self.token, uid, ticket, status=2)
                except Exception:
                    pass

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        uid = msg.get("from_user_id", "")
        ctx = msg.get("context_token", "")
        items = msg.get("item_list", [])
        text = extract_text(items)

        item_types = ",".join(str(it.get("type", 0)) for it in items)
        log.info("wechat.message.received", f"msg from {uid:.16} items={len(items)} types={item_types} text={text[:80] if text else ''!r}")

        # Handle slash commands before attachments
        if text.strip().startswith("/"):
            reply = await self._handle_slash_command(uid, text.strip())
            if reply:
                async with httpx.AsyncClient() as send_client:
                    await send_text(send_client, self.base_url, self.token, uid, reply, ctx)
                return

        # Download image attachments (type=2) with AES decryption
        attachments: list[PromptAttachment] = []
        image_items = extract_image_items(items)
        if image_items:
            log.info("wechat.image.count", f"found {len(image_items)} image item(s) to download")
        async with httpx.AsyncClient() as dl_client:
            for img_item in image_items:
                try:
                    data, mime = await download_wechat_image(dl_client, img_item, self.token)
                    aeskey = img_item.get("aeskey", "")
                    name = f"wechat-image-{aeskey[:8] if aeskey else 'raw'}.jpg"
                    log.debug("wechat.image.downloaded", f"downloaded image: aeskey={aeskey[:8] if aeskey else 'none'} mime={mime} size={len(data)}, b64[:30]={base64.b64encode(data[:30]).decode() if data else 'none'}")
                    attachments.append(
                        PromptAttachment(name=name, mime=mime, size=len(data), data=data)
                    )
                    log.info("wechat.image.downloaded", f"image downloaded: name={name} mime={mime} size={len(data)}")
                except Exception as exc:
                    log.warning("wechat.image.failed", f"image download failed: {exc}")

            # Download file attachments (type=4/5, PDFs, docs, etc.)
            file_items = extract_file_items(items)
            if file_items:
                log.info("wechat.file.count", f"found {len(file_items)} file item(s) to download")
            for file_item in file_items:
                try:
                    data, mime, filename = await download_wechat_file(dl_client, file_item, self.token)
                    log.debug("wechat.file.downloaded", f"downloaded file: filename={filename} mime={mime} size={len(data)}, b64[:30]={base64.b64encode(data[:30]).decode() if data else 'none'}")
                    attachments.append(
                        PromptAttachment(name=filename, mime=mime, size=len(data), data=data)
                    )
                    log.info("wechat.file.downloaded", f"file downloaded: name={filename} mime={mime} size={len(data)}")
                except Exception as exc:
                    log.warning("wechat.file.failed", f"file download failed: {exc}")

        if not text.strip() and not attachments:
            return

        history = self._get_or_create_history(uid)
        text_buf: list[str] = []

        def on_event(event: Any) -> None:
            event_type = type(event).__name__
            log.debug("event.received", "", event_type=event_type)
            if isinstance(event, TextDelta):
                text_buf.append(event.text)

        async with httpx.AsyncClient() as typing_client:
            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(
                self._keep_typing(typing_client, uid, ctx, stop_typing)
            )
            try:
                agent_session = AgentSession(
                    history=history,
                    registry=_clone_registry(self.runtime.registry),
                    on_event=on_event,
                    client=self.runtime.client,
                    cfg=self.runtime.cfg,
                )
                await agent_session.run_turn(
                    text or "(no text, maybe just attachments)",
                    attachments=attachments or None,
                )
            except Exception as exc:
                log.error("wechat.turn.failed", f"run_turn failed for {uid:.16}: {exc}")
            finally:
                stop_typing.set()
                await typing_task

        reply = "".join(text_buf).strip()
        if reply:
            async with httpx.AsyncClient() as send_client:
                try:
                    await send_text(send_client, self.base_url, self.token, uid, reply, ctx)
                except Exception as exc:
                    log.error("wechat.send.failed", f"send_text failed for {uid:.16}: {exc}")


async def run_wechat_bot(
    *,
    config_path: str | None,
    agent_id: str = "wechat",
    token_override: str | None = None,
) -> None:
    """Entry point called from __main__.py for the `wechat` subcommand."""
    from rich.console import Console

    console = Console()
    runtime = await build_agent_runtime(
        config_path=config_path,
        agent_id=agent_id,
        console=console,
    )

    for var_name, cfg_path in runtime.warnings:
        console.print(
            f"[yellow]warning:[/yellow] missing env var \"{var_name}\" at {cfg_path}"
        )

    cfg = runtime.config.wechat
    token = token_override or cfg.ilink_token or os.getenv("ILINK_TOKEN", "")
    if not token:
        console.print(
            "[red]error:[/red] iLink token required. Set wechat.ilink_token in config, "
            "pass --token, or set ILINK_TOKEN env var."
        )
        return

    base_url = cfg.ilink_base_url
    bot = WechatBot(
        runtime=runtime,
        base_url=base_url,
        token=token,
        poll_timeout=cfg.poll_timeout,
        typing_interval=cfg.typing_interval,
    )

    console.print(f"[green]WeChat bot running[/green] (agent={agent_id}, url={base_url})")
    try:
        await bot.run()
    finally:
        await runtime.close()
