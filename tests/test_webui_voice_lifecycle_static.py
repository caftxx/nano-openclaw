from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "app.js"
PODCAST_JS = ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-podcast.js"
INDEX_HTML = ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "index.html"
STYLES_CSS = ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "styles.css"


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


def test_webui_rest_session_switch_syncs_websocket_current_session():
    source = APP_JS.read_text(encoding="utf-8")
    sync_body = _function_body(source, "function syncWebSocketSession(session)", "async function createSessionAndRender()")
    create_body = _function_body(source, "async function createSessionAndRender()", "async function selectSessionAndRender")
    select_body = _function_body(source, "async function selectSessionAndRender(sessionId)", "function restorablePodcastSessionId()")

    assert 'send("session.select", { session_id: sessionId });' in sync_body
    assert "syncWebSocketSession(session);" in create_body
    assert "syncWebSocketSession(session);" in select_body


def test_podcast_interjection_waits_for_acceptance_before_generation_reset():
    source = PODCAST_JS.read_text(encoding="utf-8")
    body = _function_body(source, "async function submitInterjection(text, attachments, displayText, requestContext)", "function init()")

    assert "resetForUserInput(" not in body
    assert '"/api/voice/podcast/input"' in body
    assert 'podcast.pendingInputText = "";' in body


def test_group_chat_composer_supports_typed_text_and_documents():
    source = PODCAST_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    submit_body = _function_body(
        source,
        "async function submitPodcastComposer(event)",
        "async function submitInterjection(text, attachments, displayText, requestContext)",
    )

    assert 'id="podcastTextInput"' in html
    assert 'id="podcastAttachmentInput"' in html
    assert ".pdf,.docx,.txt,.md,.csv,.json" in html
    assert ".png,.jpg,.jpeg,.gif,.webp" in html
    assert "支持图片、PDF、Word 和文本文件，单个不超过 50 MB" in html
    assert 'await submitInterjection(' in submit_body
    assert "await startPodcast(topic, attachments, requestContext);" in submit_body
    assert 'attachments: attachments || []' in source
    assert ".podcast-composer" in styles


def test_group_chat_composer_uses_compact_mobile_placeholder_without_overflow():
    source = PODCAST_JS.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    composer_body = _function_body(source, "function updatePodcastComposer()", "function resizePodcastTextInput()")
    textarea_rules = _function_body(styles, ".podcast-composer textarea {", ".podcast-composer textarea:focus")

    assert 'root.matchMedia("(max-width: 360px)").matches' in composer_body
    assert 'narrow ? "输入话题"' in composer_body
    assert 'narrow ? "输入观点"' in composer_body
    assert '"输入话题或点击说话"' in composer_body
    assert "min-width: 0;" in textarea_rules
    assert "max-width: 100%;" in textarea_rules


def test_group_chat_voice_action_is_integrated_into_the_composer():
    source = PODCAST_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    composer = html[html.index('<form id="podcastComposer"'):html.index('</form>', html.index('<form id="podcastComposer"'))]

    assert 'id="podcastCircle"' in composer
    assert '文字 · 附件 · 语音' in composer
    assert 'class="podcast-topic-suggestion"' in html
    assert 'var gridMembers = [systemParticipants()[1]].concat(agentParticipants());' in source
    assert 'id="podcastMemberCount">1</strong>' in html
    assert 'className = "group-participant group-stage-participant group-empty"' not in source
    assert "grid-template-columns: 44px minmax(0, 1fr) 44px 44px;" in styles


def test_group_chat_image_start_returns_before_visual_processing_finishes():
    source = PODCAST_JS.read_text(encoding="utf-8")
    start_body = _function_body(
        source,
        "async function startPodcast(topicOverride, attachments, requestContext)",
        "async function stopPodcast",
    )
    event_body = _function_body(source, "function onEvent(event)", "function stopSpeech()")
    api_body = _function_body(source, "async function apiSafe(path, body, timeoutMs)", "async function apiGetSafe(path)")

    assert "payload.processing_attachments" in start_body
    assert 'event.type === "podcast.attachments.processing"' in event_body
    assert 'event.type === "podcast.attachments.ready"' in event_body
    assert "new AbortController()" in api_body
    assert 'throw new Error("请求超时，请重试")' in api_body


def test_group_chat_session_switch_invalidates_pending_attachment_submission():
    source = PODCAST_JS.read_text(encoding="utf-8")
    reset_body = _function_body(
        source,
        "function resetPodcastRuntimeForSession(sessionId)",
        "function handleSessionChanged(sessionId)",
    )
    submit_body = _function_body(
        source,
        "async function submitPodcastComposer(event)",
        "async function submitInterjection(text, attachments, displayText, requestContext)",
    )

    assert "podcast.composerEpoch++;" in reset_body
    assert "podcast.inputSending = false;" in reset_body
    assert "clearPodcastComposer();" in reset_body
    assert "var requestContext = podcastRequestContext(podcast.runId);" in submit_body
    assert "if (!isPodcastRequestCurrent(requestContext)) return;" in submit_body
    assert "requestContext.runId" in submit_body


def test_group_chat_attachment_requests_cover_backend_processing_deadline():
    source = PODCAST_JS.read_text(encoding="utf-8")
    start_body = _function_body(
        source,
        "async function startPodcast(topicOverride, attachments, requestContext)",
        "async function stopPodcast",
    )
    input_body = _function_body(
        source,
        "async function submitInterjection(text, attachments, displayText, requestContext)",
        "function init()",
    )

    assert "attachments.length ? 360000 : 75000" in start_body
    assert "attachments.length ? 360000 : 75000" in input_body


def test_group_chat_start_response_cannot_revive_failed_background_run():
    source = PODCAST_JS.read_text(encoding="utf-8")
    start_body = _function_body(
        source,
        "async function startPodcast(topicOverride, attachments, requestContext)",
        "async function stopPodcast",
    )
    error_body = _function_body(
        source,
        'if (event.type === "podcast.error")',
        "function stopSpeech()",
    )

    assert "podcast.startFailures.get" in start_body
    assert "if (startFailure) throw new Error(startFailure);" in start_body
    assert 'await apiSafe("/api/voice/podcast/stop", { run_id: payload.run_id }, 75000);' in start_body
    assert "rememberPodcastStartFailure(event);" in error_body


def test_web_composer_actions_bottom_align_with_multiline_input():
    styles = STYLES_CSS.read_text(encoding="utf-8")
    composer = _function_body(styles, ".composer {", ".attachment-input {")
    mobile = styles[styles.index("@media (max-width: 720px)"):styles.index("/* ── Voice mode")]

    assert "align-items: end;" in composer
    assert "#sendBtn,\n.composer-tool {\n  align-self: end;" in styles
    assert "align-items: end;" in mobile


def test_podcast_start_resets_caption_timeline_for_new_run():
    source = PODCAST_JS.read_text(encoding="utf-8")
    reset_body = _function_body(source, "function resetCaptionsForRun(topic)", "function resetForUserInput")
    start_body = _function_body(
        source,
        "async function startPodcast(topicOverride, attachments, requestContext)",
        "async function stopPodcast",
    )
    event_body = _function_body(source, 'if (event.type === "podcast.started")', 'if (event.type === "podcast.host.updated")')

    assert 'captions.querySelectorAll(".vbubble")' in reset_body
    assert 'addBubble("you", "话题：" + topic);' in reset_body
    assert "resetCaptionsForRun(podcast.lastTopic);" in start_body
    assert "resetCaptionsForRun(event.topic || podcast.lastTopic);" in event_body


def test_podcast_topic_capture_keeps_group_mode_while_starting():
    source = PODCAST_JS.read_text(encoding="utf-8")
    body = _function_body(source, "function startTopicCapture()", "function stopTopicCapture()")
    ended_body = body[body.index("onEnded: function ()"):body.index("if (!submitted && !podcast.runId && !podcast.starting)")]

    assert "submitted && (podcast.starting || podcast.runId || podcast.agents.length)" in ended_body
    assert "setPodcastMode(true);" in ended_body
    assert "setActive(Boolean(podcast.runId || podcast.starting));" in ended_body


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


def test_group_transition_cancels_and_pauses_single_voice_once():
    source = PODCAST_JS.read_text(encoding="utf-8")
    body = _function_body(source, "function setPodcastMode(on, options)", "function setPodcastControl")

    assert "var nextMode = Boolean(on) && isGroupMode();" in body
    assert "var entering = nextMode && !podcast.mode;" in body
    assert "if (entering && root.VoiceMode" in body
    assert "root.VoiceMode.cancelAndPause();" in body


def test_group_mode_is_derived_from_membership_or_active_run():
    source = PODCAST_JS.read_text(encoding="utf-8")
    save_body = _function_body(source, "function savePodcastState(sessionId)", "function hasPodcastState")
    restore_body = _function_body(source, "function restorePodcastSurface(sessionId)", "function normalizeRounds")
    mode_body = _function_body(source, "function isGroupMode()", "function participantColor")

    assert 'sessionSet(MODE_KEY, "1", sid)' not in save_body
    assert "sessionRemove(MODE_KEY, sid);" in save_body
    assert "var savedMode" not in restore_body
    assert "setPodcastMode(true);" in restore_body
    assert "return Boolean(podcast.agents.length || podcast.runId);" in mode_body


def test_single_voice_uses_the_shared_composer_microphone():
    source = PODCAST_JS.read_text(encoding="utf-8")
    shell = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-shell.js").read_text(encoding="utf-8")
    init_body = _function_body(source, "function init()", "if (document.readyState")
    podcast_circle_body = init_body[init_body.index('var podcastCircle = $("podcastCircle");'):init_body.index("function restoreAndResumePodcast()")]

    assert 'circle: $("podcastCircle")' in (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-view.js").read_text(encoding="utf-8")
    assert "if (!isGroupMode()) return;" in podcast_circle_body
    assert 'if (els.circle) els.circle.onclick' in shell
    assert '$("voiceCircle")' not in source


def test_group_voice_uses_the_composer_button_without_ambient_overlay_clicks():
    source = PODCAST_JS.read_text(encoding="utf-8")
    init_body = _function_body(source, "function init()", "if (document.readyState")
    podcast_circle_body = init_body[init_body.index('var podcastCircle = $("podcastCircle");'):init_body.index("function restoreAndResumePodcast()")]

    assert "function shouldIgnoreAmbientPodcastTap()" not in source
    assert "shouldIgnoreAmbientPodcastTap" not in podcast_circle_body
    assert "if (!isGroupMode()) return;" in podcast_circle_body
    assert "event.stopImmediatePropagation();" in podcast_circle_body
    assert "else if (explicitTopicValue()) startPodcast();" in podcast_circle_body
    assert 'var overlay = $("voiceOverlay");' not in init_body


def test_group_stage_renders_host_and_all_agent_roles():
    source = PODCAST_JS.read_text(encoding="utf-8")
    body = _function_body(source, "function renderParticipants()", "function closeMemberMenu()")

    assert "var gridMembers = [systemParticipants()[1]].concat(agentParticipants());" in body
    assert "var gridMembers = [systemParticipants()[0]].concat(agentParticipants());" not in body
    assert "var gridContent = root.document.createDocumentFragment();" in body
    assert "renderMemberButton(gridContent, member, { stage: true });" in body
    assert "gridEl.replaceChildren(gridContent);" in body
    assert 'member.kind !== "add"' in body


def test_group_voice_button_toggles_active_capture_off():
    source = PODCAST_JS.read_text(encoding="utf-8")
    cancel_body = _function_body(source, "function cancelPodcastCapture()", "function podcastDocumentError")
    init_body = _function_body(source, "function init()", "if (document.readyState")
    circle_body = init_body[init_body.index('var podcastCircle = $("podcastCircle");'):init_body.index("function restoreAndResumePodcast()")]

    assert "if (podcast.capturingTopic)" in cancel_body
    assert "stopTopicCapture();" in cancel_body
    assert "if (podcast.capturingInput)" in cancel_body
    assert "stopInterjectionCapture();" in cancel_body
    assert "podcast.playbackPausedForInput = false;" in cancel_body
    assert "pumpPlayback();" in cancel_body
    assert "if (cancelPodcastCapture()) return;" in circle_body
    assert circle_body.index("if (cancelPodcastCapture()) return;") < circle_body.index("if (podcast.runId) startInterjectionCapture();")


def test_podcast_completion_state_has_clear_restart_action():
    source = PODCAST_JS.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    control_body = _function_body(source, "function updatePodcastControl()", "function phaseLabel")
    done_body = _function_body(source, 'if (event.type === "podcast.done")', 'if (event.type === "podcast.stopped")')
    pump_body = _function_body(source, "async function pumpPlayback()", "async function synthSpeech")

    assert 'setPodcastControl("speaking", "✦", "播放中")' in control_body
    assert 'setPodcastControl("done", "✓", "重新开始")' in control_body
    assert 'setStatus("群聊已完成。");' in done_body
    assert 'setVoiceStatus("群聊已完成，正在播放剩余语音...");' in done_body
    assert 'setVoiceStatus("群聊已完成。");' in pump_body
    assert ".podcast-action-chip.done" in styles


def test_voice_exit_stops_finished_podcast_playback_before_closing_overlay():
    source = PODCAST_JS.read_text(encoding="utf-8")
    playback_body = _function_body(source, "function stopPodcastLocalPlayback()", "function stalePlaybackError()")
    init_body = _function_body(source, "function init()", "if (document.readyState")
    exit_body = init_body[init_body.index('var exit = $("voiceExitBtn");'):init_body.index('var podcastCircle = $("podcastCircle");')]

    assert "podcast.playbackStopped = true;" in playback_body
    assert "invalidatePlaybackWork();" in playback_body
    assert "podcast.synthJobs.clear();" in playback_body
    assert "podcast.playPumpActive = false;" in playback_body
    assert "stopSpeech();" in playback_body
    assert "if (podcast.mode || podcast.topicCaptureArmed || podcast.generationDone) stopPodcastLocalPlayback();" in exit_body
    assert "exitGroupChat();" not in exit_body


def test_podcast_operation_notice_is_separate_from_flow_status():
    source = PODCAST_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    idle_add_body = _function_body(source, "function addGroupAgent(config)", "async function addGroupAgentDuringRun")
    add_body = _function_body(source, "async function addGroupAgentDuringRun(config)", "async function removeGroupAgent")
    remove_body = _function_body(source, "async function removeGroupAgent(agentId)", "function exitGroupChat")
    notice_body = _function_body(source, "function showPodcastNotice(text, options)", "function setPodcastMode")

    assert 'id="podcastNotice"' in html
    assert ".podcast-notice" in styles
    assert 'var el = $("podcastNotice");' in notice_body
    assert "if (podcast.generationDone)" in idle_add_body
    assert 'showPodcastNotice(hasPendingPlayback() ? "已添加群成员，重新开始后参与。" : "已添加群成员，可点击重新开始。");' in idle_add_body
    assert 'showPodcastNotice("正在添加群成员...");' in add_body
    assert 'showPodcastNotice("已添加群成员，下一轮开始参与。");' in add_body
    assert 'setVoiceStatus("已添加群成员，下一轮开始参与。")' not in add_body
    assert 'showPodcastNotice("已踢出群成员，当前话题继续。");' in remove_body
    assert 'setVoiceStatus("已踢出群成员，其他成员继续讨论。")' not in remove_body


def test_voice_render_does_not_touch_overlay_when_podcast_owns_it():
    source = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-shell.js").read_text(encoding="utf-8")
    body = _function_body(source, "function renderAll()", "// ── 打开/关闭")

    assert "if (!podcastOwnsOverlay()) view.render(model, { fallbackNotice });" in body


def test_shared_overlay_visibility_is_independent_of_engine_rendering():
    source = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-shell.js").read_text(encoding="utf-8")
    visibility_body = _function_body(source, "function setOverlayVisible(visible)", "function openOverlay(autoStart)")
    open_body = _function_body(source, "function openOverlay(autoStart)", "function closeOverlay()")
    close_body = _function_body(source, "function closeOverlay()", "function submitMessage(message)")

    assert "view.els.overlay.hidden = !visible;" in visibility_body
    assert 'document.body.classList.toggle("voice-open", Boolean(visible));' in visibility_body
    assert "setOverlayVisible(true);" in open_body
    assert "setOverlayVisible(false);" in close_body


def test_stopping_group_clears_all_playback_before_rendering_done_state():
    source = PODCAST_JS.read_text(encoding="utf-8")
    stop_body = _function_body(source, "async function stopPodcast(options)", "function renderAssignments")
    stopped_event = _function_body(source, 'if (event.type === "podcast.stopped")', 'if (event.type === "podcast.error")')

    assert "stopPodcastLocalPlayback();" in stop_body
    assert stop_body.index("stopPodcastLocalPlayback();") < stop_body.index("updatePodcastControl();")
    assert "stopPodcastLocalPlayback();" in stopped_event
    assert stopped_event.index("stopPodcastLocalPlayback();") < stopped_event.index("updatePodcastControl();")


def test_voice_mode_exposes_unified_execution_interface():
    source = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-shell.js").read_text(encoding="utf-8")

    assert "submitMessage," in source
    assert "toggleCapture," in source
    assert "cancelAndPause," in source
    assert "stateSnapshot," in source
    assert "suspendForPodcast" not in source


def test_webspeech_constructor_is_wrapped_in_try_catch():
    source = PODCAST_JS.read_text(encoding="utf-8")
    body = _function_body(source, "function createPodcastRecognizer(callbacks)", "function startTopicCapture()")

    assert "try { rec = new SR(); } catch (_) { return null; }" in body


def test_close_commands_are_centralized_in_voice_core():
    core_source = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-core.js").read_text(encoding="utf-8")
    shell_source = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-shell.js").read_text(encoding="utf-8")

    assert "function closeCommands()" in core_source
    assert "closeCommands: closeCommands," in core_source
    close_body = _function_body(core_source, 'case "CLOSE":', "// ── 点圆")
    assert "closeCommands()" in close_body
    assert 'dispatch({ type: "CLOSE" })' in shell_source


def test_restore_clears_stale_mode_key_when_no_agents():
    source = PODCAST_JS.read_text(encoding="utf-8")
    body = _function_body(source, "function restorePodcastSurface(sessionId)", "function normalizeRounds")

    assert "sessionRemove(MODE_KEY, sid);" in body
    assert "setPodcastMode(false);" in body


def test_single_and_group_share_one_compact_stage_and_composer():
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert html.count('id="podcastStage"') == 1
    assert html.count('id="groupAgentGrid"') == 1
    assert html.count('id="podcastComposer"') == 1
    assert html.count('id="podcastCircle"') == 1
    assert 'id="voiceCircle"' not in html
    assert 'class="voice-stage"' not in html
    assert 'class="voice-duo-grid"' not in html
    assert 'id="conversationModeTitle">单聊' in html
    assert 'id="conversationEmptyTitle"' in html
    assert '.voice-overlay.podcast-mode .voice-stage' not in styles


def test_empty_state_artwork_cannot_shrink_into_the_title():
    styles = STYLES_CSS.read_text(encoding="utf-8")
    mark_rules = _function_body(styles, ".podcast-empty-mark {", ".podcast-empty-mark span {")

    assert ".podcast-empty-state > * { flex-shrink: 0; }" in styles
    assert "flex-basis: 60px;" in mark_rules
    assert "min-height: 60px;" in mark_rules


def test_single_mode_compacts_empty_state_when_captions_exist():
    styles = STYLES_CSS.read_text(encoding="utf-8")
    view = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-view.js").read_text(encoding="utf-8")

    assert 'els.overlay.classList.add("voice-has-captions")' in view
    assert 'els.overlay.classList.remove("voice-has-captions")' in view
    assert ".voice-overlay.is-single.voice-has-captions .podcast-stage" in styles
    assert ".voice-overlay.is-single.voice-has-captions .podcast-empty-mark" in styles
    assert ".voice-overlay.is-single.voice-has-captions .podcast-empty-title" in styles


def test_short_landscape_keeps_voice_footer_inside_the_viewport():
    styles = STYLES_CSS.read_text(encoding="utf-8")
    landscape_rules = _function_body(
        styles,
        "@media (max-height: 480px) and (orientation: landscape) {",
        "\n\n.voice-captions {",
    )

    assert ".voice-overlay.is-single.voice-has-captions .podcast-stage" in landscape_rules
    assert "min-height: 42px;" in landscape_rules
    assert ".group-member-name { display: none; }" in landscape_rules
    assert "min-height: 60px;" in landscape_rules
    assert ".podcast-compose-meta { display: none; }" in landscape_rules


def test_single_composer_routes_text_and_attachments_to_voice_mode():
    source = PODCAST_JS.read_text(encoding="utf-8")
    shell = (ROOT / "nano_openclaw" / "adapters" / "webui" / "static" / "voice-shell.js").read_text(encoding="utf-8")
    submit_body = _function_body(source, "async function submitPodcastComposer(event)", "async function submitInterjection")
    command_body = _function_body(shell, 'case "chatSend":', 'case "cancelTurn":')

    assert "if (!isGroupMode())" in submit_body
    assert "root.VoiceMode.submitMessage({" in submit_body
    assert "attachments: attachments" in submit_body
    assert 'response_style: "voice"' in command_body
    assert "attachments: cmd.attachments || []" in command_body
