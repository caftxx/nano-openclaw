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
    assert "root.VoiceMode.ensureConfig" in factory
    assert "new SR()" not in topic_body
    assert "new SR()" not in input_body
