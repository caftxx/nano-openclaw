"use strict";

const test = require("node:test");
const assert = require("node:assert");
const resolveTapAction = require("../nano_openclaw/gateway/webui/static/voice-tap.js");

test("正在朗读（speaking=true）任意 phase → interrupt", () => {
  assert.strictEqual(resolveTapAction("listening", true), "interrupt");
  assert.strictEqual(resolveTapAction("thinking", true), "interrupt");
  assert.strictEqual(resolveTapAction("idle", true), "interrupt");
});

test("phase=speaking（即便 speaking=false）→ interrupt", () => {
  assert.strictEqual(resolveTapAction("speaking", false), "interrupt");
});

test("思考中 phase=thinking → cancel（取消后端回复）", () => {
  assert.strictEqual(resolveTapAction("thinking", false), "cancel");
});

test("聆听中 phase=listening → flush（立即发送）", () => {
  assert.strictEqual(resolveTapAction("listening", false), "flush");
});

test("idle / error / 未知 phase → none", () => {
  assert.strictEqual(resolveTapAction("idle", false), "none");
  assert.strictEqual(resolveTapAction("error", false), "none");
  assert.strictEqual(resolveTapAction("bogus", false), "none");
});
