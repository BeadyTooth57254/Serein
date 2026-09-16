const object = (value) => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const rows = (value) => Array.isArray(value) ? value : [];
const number = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
  ? Number(value) : null;

export const recallScore = (value) => number(value) === null ? '—' : number(value).toFixed(4);

// The Hook returns typed live diagnostics separately from the older shadow budget.
// Keep missing telemetry distinct from an explicit zero or a stage that did not run.
export function recallSimulationDiagnostics(result) {
  const debug = object(result?.debug);
  const semantic = object(debug.semantic_recall_debug);
  const live = object(debug.typed_event_scene_live);
  const hasLive = Object.keys(live).length > 0;
  const router = object(semantic.semantic_recall_router ?? live.routing);
  const budget = object(semantic.retrieval_budget);
  const preGate = object(live.pre_candidate_gate ?? semantic.pre_candidate_gate);
  const surfaceGate = object(live.surface_reranker_gate ?? semantic.surface_reranker_gate);
  const retrieval = object(live.candidate_retrieval);
  const admission = object(live.admission);
  const routeAction = router.action ?? semantic.applied_action ?? semantic.route_action ?? null;
  const route = router.route ?? semantic.route ?? null;
  const routeScores = rows(router.routes ?? router.scores ?? semantic.scores).map((row) => ({
    ...row, route: row.name ?? row.route,
  }));
  const candidateCount = hasLive ? number(retrieval.candidate_count)
    : number(debug.candidate_count ?? budget.cheap_retrieval?.candidate_count);
  const retrievalNotRun = preGate.applied === true || retrieval.status === 'not_retrieved';
  const candidateCountLabel = retrievalNotRun ? '未运行' : candidateCount ?? '未返回';
  const cardCount = Array.isArray(result?.cards) ? result.cards.length : null;
  const selected = new Set(rows(live.selected_refs));
  const candidates = new Map();
  for (const row of rows(live.candidate_scores)) {
    if (row.ref) candidates.set(row.ref, {
      ...row, candidate_score: row.vector_score, rerank_score: row.reranker_score,
    });
  }
  for (const row of rows(admission.candidates ?? live.candidates)) {
    if (row.ref) candidates.set(row.ref, {...candidates.get(row.ref), ...row});
  }
  let stage = '未返回阶段', reason = live.reason ?? null;
  if (preGate.applied === true) {
    stage = '检索前停止'; reason = preGate.reason ?? reason;
  } else if (retrieval.status === 'not_retrieved') {
    stage = '候选检索未运行'; reason = retrieval.reason ?? reason;
  } else if (surfaceGate.applied === true) {
    stage = '候选后停止'; reason = surfaceGate.reason ?? reason;
  } else if (cardCount > 0) {
    stage = '已生成模拟卡片';
  } else if (hasLive && candidateCount === 0) {
    stage = '检索完成，无候选';
  } else if (Object.keys(admission).length) {
    stage = rows(admission.selected_refs).length || rows(admission.material_refs).length
      ? '已筛选，输出阶段无卡片' : '筛选完成，无卡片';
  } else if (live.status === 'skipped' || routeAction === 'skip') {
    stage = '已停止';
  }
  const suppressed = Object.entries(object(live.suppressed)).filter(([, count]) => number(count) > 0);
  if (!reason && cardCount === 0 && suppressed.length) {
    reason = suppressed.map(([key, count]) => `${key} × ${count}`).join('；');
  }
  return {
    hasLive, route, routeAction, routeScores,
    routeScore: number(router.score ?? semantic.score ?? semantic.confidence),
    routeReason: router.reason ?? semantic.reason ?? null,
    appliedAction: preGate.applied === true || surfaceGate.applied === true || retrieval.status === 'not_retrieved'
      ? 'skip' : routeAction,
    candidateCount, candidateCountLabel, cardCount, stage, reason,
    admission, rerankQuery: admission.rerank_query ?? null,
    candidates: [...candidates.values()].map((row) => ({...row, selected: selected.has(row.ref)})),
  };
}
