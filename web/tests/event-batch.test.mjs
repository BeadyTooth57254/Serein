import test from "node:test";
import assert from "node:assert/strict";
import { applyEventBatch, runEventBatch } from "../src/utils/eventBatch.js";

const event = (id, overrides = {}) => ({ item_id: id, item_type: "event", title: id, body: "原文", status: "active", recallable: true, ...overrides });
const response = (payload, status = 200) => new Response(JSON.stringify(payload), { status });

test("mixed results preserve failures and only acknowledge persisted archive receipts", async () => {
  const progress = [];
  const result = await runEventBatch(["a", "b", "c"], "archive", {
    request: async (url, options) => {
      const body = JSON.parse(options.body);
      assert.equal(url, "/__serein/memory/set-fact-event-status");
      assert.equal(body.status, "archived");
      return body.itemId === "b" ? response({ error: "conflict" }, 409)
        : response({ item: event(body.itemId, { status: "archived" }) });
    },
    onProgress: value => progress.push(value),
  });
  assert.deepEqual([...result.completed], ["a", "c"]);
  assert.deepEqual(result.failures, [{ itemId: "b", error: "conflict" }]);
  assert.equal(result.receipts.length, 2);
  assert.deepEqual(progress.at(-1), { processed: 3, total: 3 });
  const updated = applyEventBatch([event("a"), event("b"), event("c")], result.receipts);
  assert.deepEqual(updated.map(item => item.status), ["archived", "active", "archived"]);
  assert.deepEqual(updated.map(item => item.recallable), [true, true, true]);
});

test("HTTP 200 without the requested result does not clear selection", async () => {
  for (const action of ["archive", "restore", "mute", "delete"]) {
    const payload = action === "restore" ? { item: event("wrong-id") } : { item: event("a"), deleted: 1 };
    const result = await runEventBatch(["a"], action, { request: async () => response(payload) });
    assert.equal(result.completed.size, 0);
    assert.equal(result.failures.length, 1);
  }
});

test("mute sends no prose or lifecycle changes, restore does not re-enable recall", async () => {
  const muted = event("a", { status: "archived", recallable: false });
  const result = await runEventBatch(["a"], "mute", { request: async (url, options) => {
    assert.equal(url, "/__serein/memory/revise-fact-event");
    assert.deepEqual(JSON.parse(options.body), { itemId: "a", recallable: false });
    return response({ item: muted });
  } });
  assert.deepEqual(applyEventBatch([event("a")], result.receipts), [muted]);
  await runEventBatch(["a"], "restore", { request: async (url, options) => {
    assert.deepEqual(JSON.parse(options.body), { itemId: "a", status: "active" });
    return response({ item: { ...muted, status: "active" } });
  } });
});

test("delete honors revision-family receipts and avoids deleting family members twice", async () => {
  let requests = 0;
  const result = await runEventBatch(["a", "b", "a"], "delete", { request: async () => {
    requests++;
    return response({ deleted: 3, item_type: "event", item_ids: ["a", "b", "old"] });
  } });
  assert.equal(requests, 1);
  assert.equal(result.failures.length, 0);
  assert.deepEqual(applyEventBatch([event("a"), event("b"), event("old"), event("unselected")], result.receipts), [event("unselected")]);
});

test("search and detail projections update existing matches without introducing unrelated events", () => {
  const receipts = [{ item: event("a", { recallable: false }) }, { item: event("new") }];
  assert.deepEqual(applyEventBatch([event("a")], receipts), [receipts[0].item]);
  assert.deepEqual(applyEventBatch([event("a")], receipts, true), receipts.map(receipt => receipt.item));
});

test("network failure keeps its target pending and allows later items to finish", async () => {
  const result = await runEventBatch(["a", "b"], "mute", { request: async (_, options) => {
    const { itemId } = JSON.parse(options.body);
    if (itemId === "a") throw new DOMException("timeout", "TimeoutError");
    return response({ item: event(itemId, { recallable: false }) });
  } });
  assert.deepEqual([...result.completed], ["b"]);
  assert.equal(result.failures[0].itemId, "a");
  assert.match(result.failures[0].error, /结果尚未确认/);
});
