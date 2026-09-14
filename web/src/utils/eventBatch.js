const actions = {
  archive: { path: "set-fact-event-status", body: { status: "archived" } },
  restore: { path: "set-fact-event-status", body: { status: "active" } },
  mute: { path: "revise-fact-event", body: { recallable: false } },
  delete: { path: "delete-fact-event", body: {} },
};

// Each existing endpoint commits independently. Keep receipts and failures separate.
export async function runEventBatch(ids, action, { request = fetch, onProgress = () => {} } = {}) {
  const operation = actions[action];
  if (!operation) throw new Error("未知的批量操作。");
  const targets = [...new Set(ids)];
  const completed = new Set();
  const attempted = new Set();
  const receipts = [];
  const failures = [];
  for (const itemId of targets) {
    if (completed.has(itemId)) continue;
    try {
      const response = await request(`/__serein/memory/${operation.path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ itemId, ...operation.body }),
        signal: AbortSignal.timeout(60_000),
      });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || "保存失败。");
      if (action === "delete") {
        if (!(payload.deleted > 0) || payload.item_type !== "event"
          || !Array.isArray(payload.item_ids) || !payload.item_ids.includes(itemId)) {
          throw new Error("未收到完整的删除回执，请刷新核对后重试。");
        }
        payload.item_ids.forEach(id => completed.add(id));
      } else {
        const item = payload.item;
        if (item?.item_id !== itemId || item.item_type !== "event"
          || (action === "mute" ? item.recallable !== false : item.status !== operation.body.status)) {
          throw new Error("未收到完整的保存回执，请刷新核对后重试。");
        }
        completed.add(itemId);
      }
      receipts.push(payload);
    } catch (error) {
      failures.push({ itemId, error: error.name === "TimeoutError"
        ? "请求超时，结果尚未确认；请刷新核对后重试。" : error.message || "请求失败。" });
    }
    attempted.add(itemId);
    onProgress({ processed: targets.filter(id => completed.has(id) || attempted.has(id)).length, total: targets.length });
  }
  // A later deletion may also remove a previously failed member of the same family.
  return { receipts, completed, failures: failures.filter(f => !completed.has(f.itemId)) };
}

export function applyEventBatch(items, receipts, includeNew = false) {
  const updates = new Map(receipts.filter(receipt => receipt.item).map(receipt => [receipt.item.item_id, receipt.item]));
  const deleted = new Set(receipts.flatMap(receipt => receipt.item_ids || []));
  const existing = new Set(items.map(item => item.item_id));
  return items.filter(item => !deleted.has(item.item_id)).map(item => updates.get(item.item_id) || item)
    .concat(includeNew ? [...updates.values()].filter(item => !existing.has(item.item_id) && !deleted.has(item.item_id)) : []);
}
