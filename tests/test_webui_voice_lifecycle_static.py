from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "app.js"
PODCAST_JS = ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-podcast.js"


def _function_body(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


def test_webui_startup_restores_active_podcast_session_before_creating_new_session():
    source = APP_JS.read_text(encoding="utf-8")
    body = _function_body(source, "async function startWebClient()", "function send(")

    assert 'const PODCAST_RUN_KEY_PREFIX = "nanoPodcastRunId:";' in source
    assert "const restoreSessionId = restorablePodcastSessionId();" in body
    assert body.index("await selectSessionAndRender(restoreSessionId)") < body.index("await createSessionAndRender()")
    assert "PodcastMode.onSessionChanged" in _function_body(source, 'case "session.updated":', 'case "chat.accepted":')


def test_podcast_interjection_waits_for_acceptance_before_generation_reset():
    source = PODCAST_JS.read_text(encoding="utf-8")
    body = _function_body(source, "async function submitInterjection(text)", "function init()")

    assert "resetForUserInput(" not in body
    assert 'await apiSafe("/api/voice/podcast/input"' in body
    assert 'podcast.pendingInputText = "";' in body


def test_podcast_recognition_uses_current_voice_provider():
    source = PODCAST_JS.read_text(encoding="utf-8")
    factory = _function_body(source, "function createPodcastRecognizer(callbacks)", "function startTopicCapture()")
    topic_body = _function_body(source, "function startTopicCapture()", "function stopTopicCapture()")
    input_body = _function_body(source, "function startInterjectionCapture()", "function stopInterjectionCapture()")

    assert "root.VoiceMode.createRecognizer" in factory
    assert "await root.VoiceMode.ensureConfig()" in factory
    assert "new SR()" not in topic_body
    assert "new SR()" not in input_body
    assert "rec = await createPodcastRecognizer(" in topic_body
    assert "rec = await createPodcastRecognizer(" in input_body


def test_podcast_mode_only_suspends_voice_when_entering_group_mode():
    source = PODCAST_JS.read_text(encoding="utf-8")
    body = _function_body(source, "function setPodcastMode(on, options)", "function suspendNormalVoiceMode()")

    assert "var entering = Boolean(on) && !podcast.mode;" in body
    assert "if (entering) suspendNormalVoiceMode();" in body
    assert "if (on) suspendNormalVoiceMode();" not in body


def test_podcast_mode_persistence_is_separate_from_saved_agents():
    source = PODCAST_JS.read_text(encoding="utf-8")
    save_body = _function_body(source, "function savePodcastState(sessionId)", "function hasPodcastState")
    restore_body = _function_body(source, "function restorePodcastSurface(sessionId)", "function normalizeRounds")
    mode_body = _function_body(source, "function isGroupMode()", "function participantColor")

    assert 'if (podcast.mode || podcast.runId) sessionSet(MODE_KEY, "1", sid);' in save_body
    assert "else sessionRemove(MODE_KEY, sid);" in save_body
    assert "var savedMode = Boolean(sessionGet(MODE_KEY, sid));" in restore_body
    assert "setPodcastMode(true);" in restore_body
    assert "return podcast.mode || Boolean(podcast.runId);" in mode_body


def test_single_voice_clicks_are_not_intercepted_by_saved_group_agents():
    source = PODCAST_JS.read_text(encoding="utf-8")
    init_body = _function_body(source, "function init()", "if (document.readyState")
    normal_circle_body = init_body[init_body.index('var normalVoiceCircle = $("voiceCircle");'):init_body.index('var start = $("podcastStartBtn");')]
    exit_body = init_body[init_body.index('var exit = $("voiceExitBtn");'):init_body.index('var podcastCircle = $("podcastCircle");')]
    overlay_body = init_body[init_body.index('var overlay = $("voiceOverlay");'):init_body.index("function restoreAndResumePodcast()")]

    assert "&& !podcast.agents.length" not in normal_circle_body
    assert "if (!podcast.mode && podcast.agents.length)" not in normal_circle_body
    assert "podcast.topicCaptureArmed) setPodcastMode(true)" in normal_circle_body
    assert "if ((podcast.mode || podcast.topicCaptureArmed) && podcast.agents.length) exitGroupChat();" in exit_body
    assert "if (!podcast.mode && !podcast.runId && podcast.agents.length)" not in overlay_body


def test_voice_render_does_not_touch_overlay_when_podcast_owns_it():
    source = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-shell.js").read_text(encoding="utf-8")
    body = _function_body(source, "function renderAll()", "// ── 打开/关闭")

    assert "if (!podcastOwnsOverlay()) view.render(model, { fallbackNotice });" in body


def test_podcast_suspend_releases_voice_without_closing_overlay():
    source = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-shell.js").read_text(encoding="utf-8")
    body = _function_body(source, "function suspendForPodcast()", "// ── 来自 app.js handleEvent")

    assert 'if (model.state === "closed") return;' in body
    assert "core.closeCommands()" in body
    assert "model = core.createInitialModel();" in body
    assert "closeOverlay();" not in body
    assert "suspendForPodcast: closeOverlay" not in source
    assert "lastFocusMode" not in body


def test_webspeech_constructor_is_wrapped_in_try_catch():
    source = PODCAST_JS.read_text(encoding="utf-8")
    body = _function_body(source, "function createPodcastRecognizer(callbacks)", "function startTopicCapture()")

    assert "try { rec = new SR(); } catch (_) { return null; }" in body


def test_close_commands_shared_between_core_and_shell():
    core_source = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-core.js").read_text(encoding="utf-8")
    shell_source = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-shell.js").read_text(encoding="utf-8")

    assert "function closeCommands()" in core_source
    assert "closeCommands: closeCommands," in core_source
    close_body = _function_body(core_source, 'case "CLOSE":', "// ── 点圆")
    assert "closeCommands()" in close_body
    assert "core.closeCommands()" in shell_source


def test_restore_clears_stale_mode_key_when_no_agents():
    source = PODCAST_JS.read_text(encoding="utf-8")
    body = _function_body(source, "function restorePodcastSurface(sessionId)", "function normalizeRounds")

    assert "if (savedMode) sessionRemove(MODE_KEY, sid);" in body
    assert "setPodcastMode(false);" in body
