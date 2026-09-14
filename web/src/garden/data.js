// Reuse the authenticated frontend read endpoints; credentials stay on the VPS.
export async function readGardenSnapshot(read = readMemory) {
  async function activeEvents() {
    const items = [];
    let count;
    do {
      const page = await read('/__serein/live/fact-events', {
        type: 'event', status: 'active', includeSources: false, limit: 500, offset: items.length,
      });
      if (!Array.isArray(page.items) || !Number.isInteger(page.count)) throw new Error('invalid_event_page');
      count = page.count;
      if (!page.items.length && items.length < count) throw new Error('incomplete_event_page');
      items.push(...page.items);
    } while (items.length < count);
    return items;
  }
  const [projection, events] = await Promise.all([
    read('/__serein/live/memory-scenes', { sourceIds: [] }), activeEvents(),
  ]);
  if (projection.status !== 'ok' || !Array.isArray(projection.scenes) || !Array.isArray(projection.edges)) {
    throw new Error('invalid_scene_projection');
  }
  return {
    source: projection.source, snapshotId: projection.snapshotId,
    readAt: new Date().toISOString(), scenes: projection.scenes, edges: projection.edges, events,
  };
}

async function readMemory(path, body) {
  const response = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body), cache: 'no-store', signal: AbortSignal.timeout(45_000),
  });
  if (!response.ok) throw new Error('暂时没有读到记忆，请稍后重试。');
  return response.json();
}
