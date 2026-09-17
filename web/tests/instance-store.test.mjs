import {test} from "node:test";
import assert from "node:assert/strict";
import {settingsResponseError} from "../src/storage/settingsError.js";

function failedResponse(status, payload, jsonError = null) {
  return {
    ok: false,
    status,
    json: async () => {
      if (jsonError) throw jsonError;
      return payload;
    },
  };
}

test("settings save shows safe backend validation details", async () => {
  for (const [status, detail] of [
    [400, "API 自动摘要需要为三个阶段选择模型"],
    [422, "开启聊天图片转录前，请先选择“图片转录”模型"],
    [503, "设置服务正在重启，请稍后重试"],
  ]) {
    const error = await settingsResponseError(failedResponse(status, {detail}));
    assert.equal(error.message, detail);
  }
});

test("settings save never renders structured backend error payloads", async () => {
  const validation = await settingsResponseError(failedResponse(422, {
    detail: [{loc: ["body", "upstreams", 0, "api_key"], input: "provider-secret"}],
  }));
  assert.equal(validation.message, "配置未保存，请核对模型名称、接口地址与功能选择。");
  assert.doesNotMatch(validation.message, /provider-secret|api_key/);

  const unavailable = await settingsResponseError(failedResponse(502, {detail: {token: "backend-secret"}}));
  assert.equal(unavailable.message, "设置服务暂不可用，请检查后端连接后重试。");
  assert.doesNotMatch(unavailable.message, /backend-secret/);
});

test("settings conflict keeps its refresh semantics", async () => {
  const fallback = await settingsResponseError(failedResponse(409, {}));
  assert.equal(fallback.message, "设置已更新，请刷新后重试。");
  const detailed = await settingsResponseError(failedResponse(409, {detail: "设置已在其他页面更新，请刷新后重试"}));
  assert.equal(detailed.message, "设置已在其他页面更新，请刷新后重试");
});
