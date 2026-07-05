/* 语音浮层视图 —— 纯 DOM 渲染层，消费 (model, viewState)，不做任何决策。
 *
 * 职责：
 *   - 全屏浮层显隐、phase 圆（class/emoji/label）、状态行、提示横幅
 *   - 字幕（镜像会话，用 app.js 的 renderMarkdown 保持渲染一致）
 *   - 四个下拉的渲染：思考等级 / 语音输入（识别引擎）/ 语音输出（合成引擎）/ 音色
 *     —— 音色列表跟随「当前生效」的输出引擎【B8】：本地=系统声音，阿里云=音色目录；
 *        阿里云未配置时禁用并标注；存储值不在列表时的降级决策在 shell（B7 的
 *        configLoaded 门控也在 shell），这里只渲染给定的 viewState。
 *
 * 状态 → phase 视觉映射：starting/capturing/cooldown 都显示「聆听」（cooldown 是
 * 内部时序细节，对用户就是"马上继续听"）。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createVoiceView = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var LEVEL_LABELS = {
    off: "关", minimal: "极简", low: "低", medium: "中",
    high: "高", xhigh: "超高", adaptive: "自适应", max: "最大",
  };

  var PHASE_UI = {
    paused:    { cls: "off",       emoji: "🎙️", label: "点击开始",   status: "点击麦克风，开始连续语音对话" },
    starting:  { cls: "listening", emoji: "👂", label: "正在聆听…", status: "请说话，停顿后自动发送 · 说完点屏幕立即发送" },
    capturing: { cls: "listening", emoji: "👂", label: "正在聆听…", status: "请说话，停顿后自动发送 · 说完点屏幕立即发送" },
    cooldown:  { cls: "listening", emoji: "👂", label: "正在聆听…", status: "请说话，停顿后自动发送 · 说完点屏幕立即发送" },
    thinking:  { cls: "thinking",  emoji: "🤔", label: "思考中…",   status: "已发送，等待回复… · 点屏幕停止" },
    speaking:  { cls: "speaking",  emoji: "🔊", label: "朗读中…",   status: "点屏幕任意处可打断" },
    error:     { cls: "error",     emoji: "⚠️", label: "出错",      status: "" },
  };

  function createVoiceView(deps) {
    deps = deps || {};
    var doc = deps.document || (typeof document !== "undefined" ? document : null);
    var renderMd = deps.renderMarkdown || function (t) { return null; };   // null → textContent
    var $ = function (id) { return doc.getElementById(id); };

    var els = {
      overlay: $("voiceOverlay"),
      circle: $("voiceCircle"),
      status: $("voiceStatus"),
      captions: $("voiceCaptions"),
      think: $("voiceThinkLevel"),
      unsupported: $("voiceUnsupported"),
      timbre: $("voiceVoice"),
      engine: $("voiceEngine"),
      outMode: $("voiceTtsVoice"),
      exit: $("voiceExitBtn"),
      micBtn: $("voiceMicBtn"),
    };
    var elEmoji = els.circle && els.circle.querySelector(".voice-emoji");
    var elLabel = els.circle && els.circle.querySelector(".voice-circle-label");

    var aiNode = null;          // 当前 turn 的 AI 字幕节点
    var thinkOptionsKey = "";
    var pendingAiText = null;   // rAF 合并待渲染的字幕全文（见 scheduleAiUpdate）
    var aiRafScheduled = false;

    // ── 主渲染 ───────────────────────────────────────────────────────────────
    function render(model, vs) {
      if (!els.overlay) return;
      var closed = model.state === "closed";
      els.overlay.hidden = closed;
      doc.body.classList.toggle("voice-open", !closed);
      if (closed) return;

      var ui = PHASE_UI[model.state] || PHASE_UI.paused;
      // 待唤醒模式【W1】：聆听族状态显示待机视觉（灰圆 💤），提示唤醒词
      var wake = model.ctx.wake;
      if (wake && !wake.awake
          && (model.state === "starting" || model.state === "capturing" || model.state === "cooldown")) {
        ui = {
          cls: "off", emoji: "💤", label: "待唤醒",
          status: "说“" + wake.keywords[0] + "”开始对话 · 点屏幕直接唤醒",
        };
      }
      if (els.circle) {
        els.circle.className = "voice-circle " + ui.cls;
        els.circle.disabled = Boolean(model.ctx.hardBlock);
      }
      if (elEmoji) elEmoji.textContent = ui.emoji;
      if (elLabel) elLabel.textContent = ui.label;
      if (els.status) els.status.textContent = model.ctx.statusOverride || ui.status;

      // 提示横幅：硬阻断（HTTPS/不支持）优先；其次 TTS 回退原因【B6】
      if (els.unsupported) {
        if (model.ctx.hardBlock === "https") {
          els.unsupported.innerHTML = "当前通过 <b>HTTP</b> 访问，手机浏览器会禁用麦克风（即使在设置里允许也无效）。请改用 <b>HTTPS</b> 地址访问。";
          els.unsupported.hidden = false;
        } else if (model.ctx.hardBlock === "no-sr") {
          els.unsupported.textContent = "当前浏览器不支持语音识别，请用 Android Chrome 打开。";
          els.unsupported.hidden = false;
        } else if (vs && vs.fallbackNotice) {
          els.unsupported.textContent = vs.fallbackNotice;
          els.unsupported.hidden = false;
        } else {
          els.unsupported.hidden = true;
        }
      }

      // AI 字幕跟随 turn 文本流：rAF 合并——markdown 全文重解析是 O(n²)，
      // 逐 token 跑会让长回复重解析几百次；合并到帧级，每帧最多一次。
      if (aiNode && model.ctx.turn) scheduleAiUpdate(model.ctx.turn.text);
    }

    function scheduleAiUpdate(text) {
      pendingAiText = text;
      if (aiRafScheduled) return;
      var raf = (typeof requestAnimationFrame === "function")
        ? requestAnimationFrame : function (f) { f(); };
      aiRafScheduled = true;
      raf(function () {
        aiRafScheduled = false;
        if (aiNode && pendingAiText != null) setNodeMarkdown(aiNode, pendingAiText);
        pendingAiText = null;
      });
    }

    // ── 字幕 ────────────────────────────────────────────────────────────────
    function setNodeMarkdown(node, text) {
      var html = text ? renderMd(text) : null;
      if (html != null) node.innerHTML = html;
      else node.textContent = text || "";
      if (els.captions) els.captions.scrollTop = els.captions.scrollHeight;
    }

    function addBubble(role, text) {
      if (!els.captions) return null;
      var div = doc.createElement("div");
      div.className = "vbubble " + (role === "you" ? "you" : "ai");
      if (role === "ai") setNodeMarkdown(div, text);
      else div.textContent = text || "";
      els.captions.appendChild(div);
      els.captions.scrollTop = els.captions.scrollHeight;
      return div;
    }

    function seedCaptions(history, extractText) {
      if (!els.captions) return;
      els.captions.innerHTML = "";
      aiNode = null;
      var hist = history || [];
      for (var i = Math.max(0, hist.length - 8); i < hist.length; i++) {
        var msg = hist[i];
        var text = (extractText ? extractText(msg) : "").trim();
        if (text) addBubble(msg.role === "user" ? "you" : "ai", text);
      }
    }

    function addUserBubble(text) { addBubble("you", text); }
    function startAiBubble() {
      pendingAiText = null;   // 防上一轮迟到的 rAF 把旧文本写进新气泡
      aiNode = addBubble("ai", "");
    }
    function finishAiBubble(finalText) {
      pendingAiText = null;   // 终态同步渲染，作废挂起的帧级更新
      if (aiNode && finalText != null) setNodeMarkdown(aiNode, finalText);
      aiNode = null;
    }
    function addAiError(message) {
      aiNode = null;
      addBubble("ai", "⚠️ 出错：" + (message || "未知错误"));
    }

    // ── 思考等级下拉（跟随后端，仅用户操作时下发）────────────────────────────
    function buildThinkOptions(levels) {
      if (!els.think || !Array.isArray(levels) || !levels.length) return;
      var key = levels.join("\n");
      if (key === thinkOptionsKey) return;
      els.think.innerHTML = "";
      for (var i = 0; i < levels.length; i++) {
        var o = doc.createElement("option");
        o.value = levels[i];
        o.textContent = "🧠 " + (LEVEL_LABELS[levels[i]] || levels[i]);
        els.think.appendChild(o);
      }
      thinkOptionsKey = key;
    }
    function reflectThinking(level) {
      if (typeof level !== "string" || !els.think) return;
      els.think.value = level;
      els.think.classList.toggle("on", level !== "off");
    }

    // ── 引擎/输出/音色下拉 ──────────────────────────────────────────────────
    // vs = { resolvedEngine, srSupported, aliyunUsable, aliyunTtsUsable,
    //        selectedOut, effectiveOut, aliyunVoice, voiceURI, ttsVoices, systemVoices }
    function renderControls(vs) {
      if (els.engine) {
        els.engine.value = vs.resolvedEngine;
        for (var i = 0; i < els.engine.options.length; i++) {
          var o = els.engine.options[i];
          if (o.value === "aliyun") {
            o.disabled = !vs.aliyunUsable;
            o.textContent = vs.aliyunUsable ? "🎤 阿里云" : "🎤 阿里云（未配置）";
          } else if (o.value === "webspeech") {
            o.disabled = !vs.srSupported;
            o.textContent = vs.srSupported ? "🎤 本地" : "🎤 本地（不支持）";
          }
        }
      }
      if (els.outMode) {
        for (var j = 0; j < els.outMode.options.length; j++) {
          var m = els.outMode.options[j];
          var isAliyun = m.value !== "local";
          m.disabled = isAliyun && !vs.aliyunTtsUsable;
          var label = m.value === "local" ? "本地" : (m.value === "aliyun-rest" ? "阿里云" : "阿里云流式");
          m.textContent = "🔊 " + label + ((isAliyun && !vs.aliyunTtsUsable) ? "（未配置）" : "");
        }
        // 显示「当前生效引擎」【B8】：回退后下拉反映真实在用的通道，不改用户偏好
        var eff = vs.effectiveOut || vs.selectedOut || "local";
        els.outMode.value = (eff !== "local" && !vs.aliyunTtsUsable) ? "local" : eff;
      }
      renderTimbres(vs);
    }

    // 音色列表跟随生效输出引擎：阿里云 → 音色目录；本地 → 系统声音（优先中文）。
    function renderTimbres(vs) {
      if (!els.timbre) return;
      var eff = vs.effectiveOut || vs.selectedOut || "local";
      els.timbre.innerHTML = "";
      if (eff !== "local" && vs.aliyunTtsUsable) {
        var voices = vs.ttsVoices || [];
        for (var i = 0; i < voices.length; i++) {
          var o = doc.createElement("option");
          o.value = voices[i].value;
          o.dataset.label = voices[i].label || voices[i].value || "";
          o.textContent = "🗣 " + voiceDisplayLabel(voices[i]);
          els.timbre.appendChild(o);
        }
        els.timbre.value = vs.aliyunVoice || (voices[0] ? voices[0].value : "");
        return;
      }
      var def = doc.createElement("option");
      def.value = "";
      def.textContent = "🗣 系统默认";
      els.timbre.appendChild(def);
      var sys = vs.systemVoices || [];
      var zh = sys.filter(function (v) { return /^zh/i.test(v.lang); });
      var list = zh.length ? zh : sys;
      for (var k = 0; k < list.length; k++) {
        var so = doc.createElement("option");
        so.value = list[k].voiceURI;
        so.textContent = "🗣 " + list[k].name;
        els.timbre.appendChild(so);
      }
      els.timbre.value = list.some(function (v) { return v.voiceURI === vs.voiceURI; }) ? vs.voiceURI : "";
    }

    function voiceDisplayLabel(voice) {
      var label = voice && voice.label || "";
      var score = Number(voice && voice.score);
      return label + (score > 0 ? " · " + score + "分" : "");
    }

    return {
      els: els,
      render: render,
      seedCaptions: seedCaptions,
      addUserBubble: addUserBubble,
      startAiBubble: startAiBubble,
      finishAiBubble: finishAiBubble,
      addAiError: addAiError,
      buildThinkOptions: buildThinkOptions,
      reflectThinking: reflectThinking,
      renderControls: renderControls,
    };
  }

  return createVoiceView;
});
