"use strict";

const test = require("node:test");
const assert = require("node:assert");
const resolveEngine = require("../nano_openclaw/gateway/webui/static/voice-engine.js");

test("用户显式选本地：阿里云可用时仍用 webspeech（尊重用户）", () => {
  assert.strictEqual(resolveEngine("webspeech", true), "webspeech");
});

test("用户显式选本地：阿里云不可用也是 webspeech", () => {
  assert.strictEqual(resolveEngine("webspeech", false), "webspeech");
});

test("用户选阿里云且可用 → aliyun", () => {
  assert.strictEqual(resolveEngine("aliyun", true), "aliyun");
});

test("用户选阿里云但不可用 → 回退 webspeech", () => {
  assert.strictEqual(resolveEngine("aliyun", false), "webspeech");
});

test("无偏好(空串) + 阿里云可用 → 自动默认 aliyun", () => {
  assert.strictEqual(resolveEngine("", true), "aliyun");
});

test("无偏好(空串) + 阿里云不可用 → webspeech", () => {
  assert.strictEqual(resolveEngine("", false), "webspeech");
});

test("偏好为 null → 等同无偏好，走自动默认", () => {
  assert.strictEqual(resolveEngine(null, true), "aliyun");
  assert.strictEqual(resolveEngine(null, false), "webspeech");
});

test("未知偏好值 → 等同无偏好，走自动默认", () => {
  assert.strictEqual(resolveEngine("bogus", true), "aliyun");
  assert.strictEqual(resolveEngine("bogus", false), "webspeech");
});
