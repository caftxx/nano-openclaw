"""ChannelAdapter abstraction tests — ChannelAdapter ABC, ChannelManager, WechatChannel.

End-to-end iLink integration is not exercised here (needs a live server);
these focus on the routing / registry / config-migration mechanics.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nano_openclaw.adapters.channels.base import ChannelAdapter, ChannelAccount, ChannelStatus
from nano_openclaw.services.channels import ChannelManager
from nano_openclaw.features.schedule.types import CronJob, CronRunRecord


# ────────────────────────────────────────────────────────────────────────────
# A minimal ChannelAdapter for testing routing without launching real I/O.
# ────────────────────────────────────────────────────────────────────────────


class _RecordingChannel(ChannelAdapter):
    id = "recording"

    async def start(self, ctx):
        self._state = "running"
        self._started_at = time.time()
        self.notifications: list[dict] = []
        self.exits: list[dict] = []

    async def stop(self):
        self._state = "stopped"
        self._started_at = None

    async def notify_completion(self, *, target_key, status, summary, job, record):
        self.notifications.append({
            "target_key": target_key,
            "status": status,
            "summary": summary,
            "job_name": job.name,
        })

    async def exit_interaction(self, *, sender_key, reason=""):
        self.exits.append({"sender_key": sender_key, "reason": reason})


def _fake_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="default",
        state_dir=Path("/tmp/fake-state"),
        config=SimpleNamespace(),
    )


def _make_job(created_by: str = "recording:default:user1", name: str = "test-job") -> CronJob:
    return CronJob(
        id="abc12345-test",
        name=name,
        expression="0 0 * * *",
        prompt="hello",
        enabled=True,
        created_at="2026-05-10T00:00:00",
        created_by=created_by,
    )


def _make_record() -> CronRunRecord:
    return CronRunRecord(
        job_id="abc12345-test",
        run_id="run-123",
        started_at="2026-05-10T12:00:00",
        ended_at="2026-05-10T12:00:01",
        status="ok",
        error=None,
        elapsed_ms=1000,
    )


# ────────────────────────────────────────────────────────────────────────────
# ChannelAccount / ChannelStatus / make_created_by
# ────────────────────────────────────────────────────────────────────────────


def test_channel_id_must_be_set():
    class _BadChannel(ChannelAdapter):
        id = ""

        async def start(self, ctx): ...
        async def stop(self): ...

    with pytest.raises(TypeError):
        _BadChannel(ChannelAccount(id="x"))


def test_make_created_by_three_segment():
    ch = _RecordingChannel(ChannelAccount(id="work"))
    assert ch.make_created_by("user42") == "recording:work:user42"


def test_default_decorate_tools_is_passthrough():
    ch = _RecordingChannel(ChannelAccount(id="default"))
    sentinel = object()
    assert ch.decorate_tools(sentinel, "user1") is sentinel  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────────────
# ChannelManager: registration + lifecycle
# ────────────────────────────────────────────────────────────────────────────


def test_registry_register_and_get():
    reg = ChannelManager()
    reg.register(_RecordingChannel)
    assert reg.get_class("recording") is _RecordingChannel
    # Idempotent: same class re-registers cleanly
    reg.register(_RecordingChannel)


def test_registry_register_blank_id_rejected():
    class _NoId(ChannelAdapter):
        id = ""

        async def start(self, ctx): ...
        async def stop(self): ...

    reg = ChannelManager()
    with pytest.raises(ValueError):
        reg.register(_NoId)


def test_registry_double_register_different_class_rejected():
    class _Other(ChannelAdapter):
        id = "recording"

        async def start(self, ctx): ...
        async def stop(self): ...

    reg = ChannelManager()
    reg.register(_RecordingChannel)
    with pytest.raises(ValueError):
        reg.register(_Other)


def test_registry_replace_allows_plugin_reload_class_swap():
    class _Reloaded(ChannelAdapter):
        id = "recording"

        async def start(self, ctx): ...
        async def stop(self): ...

    reg = ChannelManager()
    reg.register(_RecordingChannel)
    reg.register(_Reloaded, replace=True)

    assert reg.get_class("recording") is _Reloaded


def test_registry_start_stop_lifecycle():
    reg = ChannelManager()
    reg.register(_RecordingChannel)

    async def run():
        runtime = _fake_runtime()
        inst = await reg.start("recording", ChannelAccount(id="default"), runtime)
        assert inst.status().state == "running"
        # Idempotent start returns the same instance
        same = await reg.start("recording", ChannelAccount(id="default"), runtime)
        assert same is inst
        await reg.stop("recording", "default")
        assert inst.status().state == "stopped"
        # Stop again is a no-op
        await reg.stop("recording", "default")

    asyncio.run(run())


def test_registry_dispatches_channel_exit_to_sender():
    reg = ChannelManager()
    reg.register(_RecordingChannel)

    async def run():
        inst = await reg.start(
            "recording",
            ChannelAccount(id="work"),
            _fake_runtime(),
        )
        assert await reg.dispatch_exit(
            channel_id="recording",
            account_id="work",
            sender_key="user42",
            reason="user said goodbye",
        )
        assert inst.exits == [{
            "sender_key": "user42",
            "reason": "user said goodbye",
        }]
        assert not await reg.dispatch_exit(
            channel_id="recording",
            account_id="missing",
            sender_key="user42",
        )

    asyncio.run(run())


def test_registry_restart_all_recreates_instances_with_new_runtime():
    class _RuntimeChannel(ChannelAdapter):
        id = "runtime"

        async def start(self, ctx):
            self._state = "running"
            self.runtime = ctx.runtime
            self.gateway = ctx.gateway

        async def stop(self):
            self._state = "stopped"

    reg = ChannelManager()
    reg.register(_RuntimeChannel)

    async def run():
        first_runtime = SimpleNamespace(name="first")
        second_runtime = SimpleNamespace(name="second")
        first = await reg.start("runtime", ChannelAccount(id="default"), first_runtime)
        await reg.restart_all(second_runtime, SimpleNamespace(runtime=second_runtime))
        second = reg.get_instance("runtime", "default")
        assert second is not first
        assert second.runtime is second_runtime
        assert second.gateway.runtime is second_runtime
        assert first.status().state == "stopped"

    asyncio.run(run())


def test_registry_restart_all_keeps_old_instance_when_stop_fails():
    class _FailingStopChannel(ChannelAdapter):
        id = "failing-stop"
        instances: list["_FailingStopChannel"] = []

        def __init__(self, account):
            super().__init__(account)
            self.instances.append(self)
            self.alive = False

        async def start(self, ctx):
            self._state = "running"
            self.alive = True

        async def stop(self):
            raise RuntimeError("still alive")

    _FailingStopChannel.instances = []
    reg = ChannelManager()
    reg.register(_FailingStopChannel)

    async def run():
        first = await reg.start("failing-stop", ChannelAccount(id="default"), _fake_runtime())
        await reg.restart_all(_fake_runtime(), SimpleNamespace())
        assert reg.get_instance("failing-stop", "default") is first
        assert first.alive is True
        assert len(_FailingStopChannel.instances) == 1

    asyncio.run(run())


def test_registry_unknown_channel_raises_keyerror():
    reg = ChannelManager()

    async def run():
        with pytest.raises(KeyError):
            await reg.start("nope", ChannelAccount(id="x"), _fake_runtime())

    asyncio.run(run())


# ────────────────────────────────────────────────────────────────────────────
# parse_created_by: legacy + new format
# ────────────────────────────────────────────────────────────────────────────


def test_parse_created_by_three_segment():
    assert ChannelManager.parse_created_by("wechat:work:o9cq80abc") == ("wechat", "work", "o9cq80abc")


def test_parse_created_by_legacy_two_segment_defaults_to_default_account():
    assert ChannelManager.parse_created_by("wechat:o9cq80abc") == ("wechat", "default", "o9cq80abc")


def test_parse_created_by_non_routable_returns_none():
    assert ChannelManager.parse_created_by("cli") is None
    assert ChannelManager.parse_created_by("") is None


# ────────────────────────────────────────────────────────────────────────────
# dispatch_notify routing
# ────────────────────────────────────────────────────────────────────────────


def test_dispatch_notify_routes_to_correct_account():
    reg = ChannelManager()
    reg.register(_RecordingChannel)

    async def run():
        runtime = _fake_runtime()
        await reg.start("recording", ChannelAccount(id="work"), runtime)
        await reg.start("recording", ChannelAccount(id="personal"), runtime)

        delivered = await reg.dispatch_notify(
            created_by="recording:work:user42",
            status="ok",
            summary="done",
            job=_make_job(created_by="recording:work:user42"),
            record=_make_record(),
        )
        assert delivered

        work = reg.get_instance("recording", "work")
        personal = reg.get_instance("recording", "personal")
        assert len(work.notifications) == 1  # type: ignore[attr-defined]
        assert work.notifications[0]["target_key"] == "user42"  # type: ignore[attr-defined]
        assert len(personal.notifications) == 0  # type: ignore[attr-defined]

        await reg.stop_all()

    asyncio.run(run())


def test_dispatch_notify_legacy_format_routes_to_default():
    reg = ChannelManager()
    reg.register(_RecordingChannel)

    async def run():
        runtime = _fake_runtime()
        await reg.start("recording", ChannelAccount(id="default"), runtime)

        delivered = await reg.dispatch_notify(
            created_by="recording:user42",  # legacy two-segment
            status="ok",
            summary="legacy",
            job=_make_job(created_by="recording:user42"),
            record=_make_record(),
        )
        assert delivered
        default = reg.get_instance("recording", "default")
        assert default.notifications[0]["target_key"] == "user42"  # type: ignore[attr-defined]

        await reg.stop_all()

    asyncio.run(run())


def test_dispatch_notify_no_instance_returns_false():
    reg = ChannelManager()
    reg.register(_RecordingChannel)

    async def run():
        delivered = await reg.dispatch_notify(
            created_by="recording:work:user42",
            status="ok",
            summary="x",
            job=_make_job(),
            record=_make_record(),
        )
        assert delivered is False

    asyncio.run(run())


def test_dispatch_notify_unparseable_created_by_returns_false():
    reg = ChannelManager()

    async def run():
        delivered = await reg.dispatch_notify(
            created_by="cli",
            status="ok",
            summary="",
            job=_make_job(created_by="cli"),
            record=_make_record(),
        )
        assert delivered is False

    asyncio.run(run())


def test_dispatch_notify_swallows_handler_errors():
    class _FailingChannel(ChannelAdapter):
        id = "failing"

        async def start(self, ctx):
            self._state = "running"

        async def stop(self):
            self._state = "stopped"

        async def notify_completion(self, *, target_key, status, summary, job, record):
            raise RuntimeError("boom")

    reg = ChannelManager()
    reg.register(_FailingChannel)

    async def run():
        await reg.start("failing", ChannelAccount(id="default"), _fake_runtime())
        delivered = await reg.dispatch_notify(
            created_by="failing:default:user1",
            status="ok",
            summary="x",
            job=_make_job(created_by="failing:default:user1"),
            record=_make_record(),
        )
        # Returns False so the cron scheduler can fall back to a legacy path
        # without crashing.
        assert delivered is False
        await reg.stop_all()

    asyncio.run(run())


# ────────────────────────────────────────────────────────────────────────────
# WechatChannel — three-segment created_by injection
# ────────────────────────────────────────────────────────────────────────────


def test_wechat_channel_decorate_tools_injects_three_segment_marker():
    """WechatChannel.decorate_tools (via _clone_registry) wraps cron_create
    so that args.created_by becomes 'wechat:{account}:{sender}'.
    """
    from nano_openclaw.adapters.channels.wechat import WechatChannel
    from nano_openclaw.core.tools import Tool, ToolRegistry

    captured: dict = {}

    def fake_cron_create_run(args: dict[str, Any]) -> str:
        captured["args"] = dict(args)
        return "ok"

    base = ToolRegistry()
    base.register(Tool(
        name="cron_create",
        description="",
        input_schema={"type": "object"},
        run=fake_cron_create_run,
    ))

    ch = WechatChannel(ChannelAccount(id="work"))
    decorated = ch.decorate_tools(base, sender_key="o9cq80abc")
    asyncio.run(decorated._tools["cron_create"].run({"prompt": "hi"}))

    assert captured["args"]["created_by"] == "wechat:work:o9cq80abc"
    assert captured["args"]["notify_wechat"] is True


def test_wechat_channel_default_account_marker():
    from nano_openclaw.adapters.channels.wechat import WechatChannel
    from nano_openclaw.core.tools import Tool, ToolRegistry

    captured: dict = {}

    def fake_run(args: dict[str, Any]) -> str:
        captured["args"] = dict(args)
        return ""

    base = ToolRegistry()
    base.register(Tool(name="cron_create", description="", input_schema={}, run=fake_run))

    ch = WechatChannel(ChannelAccount(id="default"))
    decorated = ch.decorate_tools(base, sender_key="uid42")
    asyncio.run(decorated._tools["cron_create"].run({}))
    assert captured["args"]["created_by"] == "wechat:default:uid42"
