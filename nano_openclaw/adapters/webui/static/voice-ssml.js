/* SSML helpers for Web Voice.
 *
 * Aliyun accepts SSML through the normal text field, but cloud requests must
 * receive complete XML documents. These helpers keep SSML splitting out of the
 * state machine and speaker engines.
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.VoiceSsml = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var REST_VISIBLE_CHAR_LIMIT = 280;
  var FLOWING_RUN_WEIGHT_LIMIT = 9000;
  var FLOWING_SESSION_WEIGHT_LIMIT = 180000;
  var SENTENCE_END = /[。！？!?；;，,、\n]/;

  function decodeEntities(text) {
    return String(text || "")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&apos;/g, "'")
      .replace(/&amp;/g, "&");
  }

  function escapeText(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function escapeAttr(text) {
    return escapeText(text).replace(/"/g, "&quot;").replace(/'/g, "&apos;");
  }

  function parseAttrs(raw) {
    var attrs = {};
    var re = /([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*("([^"]*)"|'([^']*)')/g;
    var m;
    while ((m = re.exec(raw))) attrs[m[1]] = decodeEntities(m[3] != null ? m[3] : m[4]);
    return attrs;
  }

  function parseXml(text) {
    var src = String(text || "").trim();
    if (!src) return null;
    if (typeof DOMParser !== "undefined") {
      try {
        var doc = new DOMParser().parseFromString(src, "application/xml");
        if (doc && doc.getElementsByTagName("parsererror").length) return null;
      } catch (_) {
        return null;
      }
    }

    var stack = [{ name: "#doc", attrs: {}, children: [] }];
    var re = /<!--[\s\S]*?-->|<[^>]+>|[^<]+/g;
    var token;
    while ((token = re.exec(src))) {
      var part = token[0];
      if (!part) continue;
      if (part.indexOf("<!--") === 0) continue;
      if (part.charAt(0) !== "<") {
        stack[stack.length - 1].children.push({ type: "text", text: decodeEntities(part) });
        continue;
      }
      if (part.indexOf("<?") === 0 || part.indexOf("<!") === 0) return null;
      if (part.indexOf("</") === 0) {
        var closeName = part.slice(2, -1).trim();
        if (stack.length <= 1 || stack[stack.length - 1].name !== closeName) return null;
        var closed = stack.pop();
        stack[stack.length - 1].children.push(closed);
        continue;
      }
      var selfClosing = /\/\s*>$/.test(part);
      var inner = part.slice(1, selfClosing ? part.lastIndexOf("/") : -1).trim();
      var nameMatch = /^([A-Za-z_:][-A-Za-z0-9_:.]*)/.exec(inner);
      if (!nameMatch) return null;
      var name = nameMatch[1];
      var node = {
        type: "element",
        name: name,
        attrs: parseAttrs(inner.slice(name.length)),
        children: [],
        selfClosing: selfClosing,
      };
      if (selfClosing) stack[stack.length - 1].children.push(node);
      else stack.push(node);
    }
    if (stack.length !== 1) return null;
    var roots = stack[0].children.filter(function (n) {
      return n.type !== "text" || n.text.trim();
    });
    if (roots.length !== 1 || roots[0].type !== "element" || roots[0].name !== "speak") return null;
    return roots[0];
  }

  function serializeNode(node) {
    if (!node) return "";
    if (node.type === "text") return escapeText(node.text);
    var attrs = "";
    Object.keys(node.attrs || {}).forEach(function (k) {
      attrs += " " + k + "=\"" + escapeAttr(node.attrs[k]) + "\"";
    });
    if (node.selfClosing || !(node.children && node.children.length)) return "<" + node.name + attrs + "/>";
    return "<" + node.name + attrs + ">" + node.children.map(serializeNode).join("") + "</" + node.name + ">";
  }

  function serializeSpeak(children) {
    return "<speak>" + (children || []).map(serializeNode).join("") + "</speak>";
  }

  function textContent(node) {
    if (!node) return "";
    if (node.type === "text") return node.text || "";
    return (node.children || []).map(textContent).join("");
  }

  function visibleLen(node) {
    return textContent(node).length;
  }

  function weightedLen(node) {
    var text = typeof node === "string" ? node : textContent(node);
    var total = 0;
    for (var i = 0; i < text.length; i++) {
      total += /[\u3400-\u9fff]/.test(text[i]) ? 2 : 1;
    }
    return total;
  }

  function limitFor(mode) {
    return mode === "flowing" ? FLOWING_RUN_WEIGHT_LIMIT : REST_VISIBLE_CHAR_LIMIT;
  }

  function measure(node, mode) {
    return mode === "flowing" ? weightedLen(node) : visibleLen(node);
  }

  function splitTextByLimit(text, mode, limit) {
    var out = [];
    var buf = "";
    function pushBuf() {
      if (buf) out.push(buf);
      buf = "";
    }
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (buf && measure({ type: "text", text: buf + ch }, mode) > limit) pushBuf();
      buf += ch;
      if (SENTENCE_END.test(ch) && weightedLen(buf) > 0 && measure({ type: "text", text: buf }, mode) >= limit * 0.65) {
        pushBuf();
        continue;
      }
      if (measure({ type: "text", text: buf }, mode) >= limit) pushBuf();
    }
    pushBuf();
    return out;
  }

  function cloneWithText(node, text) {
    if (node && node.type === "element" && node.name === "emotion") {
      return {
        type: "element",
        name: "emotion",
        attrs: Object.assign({}, node.attrs || {}),
        children: [{ type: "text", text: text }],
        selfClosing: false,
      };
    }
    return { type: "text", text: text };
  }

  function splitNode(node, mode, limit) {
    if (measure(node, mode) <= limit) return [node];
    if (node.type === "text" || (node.type === "element" && node.name === "emotion")) {
      return splitTextByLimit(textContent(node), mode, limit).map(function (part) {
        return cloneWithText(node, part);
      });
    }
    if (node.type === "element" && node.selfClosing) return [node];
    return splitTextByLimit(textContent(node), mode, limit).map(function (part) {
      return { type: "text", text: part };
    });
  }

  function isSsml(text) {
    return Boolean(parseXml(text));
  }

  function stripSsmlToText(text) {
    var root = parseXml(text);
    if (root) return textContent(root).replace(/\s+/g, " ").trim();
    return decodeEntities(String(text || "").replace(/<[^>]+>/g, "")).replace(/\s+/g, " ").trim();
  }

  function chunkSsmlForAliyun(text, mode) {
    var root = parseXml(text);
    if (!root) return [String(text || "")];
    mode = mode === "flowing" ? "flowing" : "rest";
    var limit = limitFor(mode);
    var chunks = [];
    var buf = [];
    var bufMeasure = 0;

    function flush() {
      if (!buf.length) return;
      chunks.push(serializeSpeak(buf));
      buf = [];
      bufMeasure = 0;
    }

    (root.children || []).forEach(function (node) {
      if (node.type === "text" && !node.text.trim()) return;
      if (node.type === "element" && node.name === "break" && !buf.length) return;
      var parts = splitNode(node, mode, limit);
      parts.forEach(function (part) {
        var n = measure(part, mode);
        if (buf.length && bufMeasure + n > limit) flush();
        if (n > limit) {
          flush();
          chunks.push(serializeSpeak([part]));
          return;
        }
        buf.push(part);
        bufMeasure += n;
      });
    });
    flush();
    return chunks.length ? chunks : [serializeSpeak([])];
  }

  return {
    REST_VISIBLE_CHAR_LIMIT: REST_VISIBLE_CHAR_LIMIT,
    FLOWING_RUN_WEIGHT_LIMIT: FLOWING_RUN_WEIGHT_LIMIT,
    FLOWING_SESSION_WEIGHT_LIMIT: FLOWING_SESSION_WEIGHT_LIMIT,
    isSsml: isSsml,
    stripSsmlToText: stripSsmlToText,
    chunkSsmlForAliyun: chunkSsmlForAliyun,
    _parseXml: parseXml,
    _textContent: textContent,
    _weightedLen: weightedLen,
  };
});
