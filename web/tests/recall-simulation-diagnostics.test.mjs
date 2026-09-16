import assert from 'node:assert/strict';
import test from 'node:test';
import {recallScore, recallSimulationDiagnostics} from '../src/utils/recallSimulationDiagnostics.js';
import {createRecallSimulationTrainingLabel} from '../src/storage/recallSimulationTraining.js';

// Current /api/hook/recall wire shape, with synthetic memories only.
export function hookResponse(live = {}, semantic = {}) {
  return {
    query: '还记得你的生日是什么时候吗？', cards: [], injected: false,
    debug: {
      records_injections: false,
      semantic_recall_debug: {
        route: 'present_chitchat', route_action: 'recall', score: 0.47,
        semantic_recall_router: {
          route: 'present_chitchat', action: 'recall', score: 0.47, reason: 'uncertain_route',
          routes: [{name: 'present_chitchat', action: 'skip', score: 0.47, threshold: 0.6}],
        }, ...semantic,
      },
      typed_event_scene_live: {status: 'no_match', selected_refs: [], ...live},
    },
  };
}

test('eight retrieved candidates are not displayed as zero when admission defers', () => {
  const result = hookResponse({
    candidate_retrieval: {status: 'ok', candidate_count: 8},
    admission: {mode: 'defer_to_exact_evidence', candidates: []},
    suppressed: {defer_to_exact_evidence: 8},
  });
  const view = recallSimulationDiagnostics(result);
  assert.equal(view.candidateCountLabel, 8);
  assert.equal(view.cardCount, 0);
  assert.equal(view.stage, '筛选完成，无卡片');
  assert.match(view.reason, /defer_to_exact_evidence × 8/);
  assert.equal(view.routeScore, 0.47);
  assert.equal(view.routeAction, 'recall'); // Winner's template skip did not win the decision.
  assert.equal(view.routeScores[0].route, 'present_chitchat');
  assert.equal(view.routeScores[0].action, 'skip');
});

test('pre-retrieval skip, post-retrieval skip, explicit zero and missing counts remain distinct', () => {
  const pre = recallSimulationDiagnostics(hookResponse({pre_candidate_gate: {applied: true, reason: 'pre_reason'}}));
  assert.equal(pre.candidateCountLabel, '未运行');
  assert.equal(pre.stage, '检索前停止');
  assert.equal(pre.reason, 'pre_reason');
  assert.equal(pre.appliedAction, 'skip');
  assert.equal(pre.routeAction, 'recall');
  const post = recallSimulationDiagnostics(hookResponse({
    candidate_retrieval: {status: 'ok', candidate_count: 7},
    surface_reranker_gate: {applied: true, reason: 'post_reason'},
  }));
  assert.equal(post.candidateCountLabel, 7);
  assert.equal(post.stage, '候选后停止');
  assert.equal(post.reason, 'post_reason');
  assert.equal(recallSimulationDiagnostics(hookResponse({candidate_retrieval: {candidate_count: 0}})).candidateCountLabel, 0);
  assert.equal(recallSimulationDiagnostics(hookResponse()).candidateCountLabel, '未返回');
  assert.equal(recallSimulationDiagnostics({}).routeAction, null);
  assert.equal(recallSimulationDiagnostics({}).cardCount, null);
  assert.equal(recallScore(null), '—');
  assert.equal(recallScore(undefined), '—');
  assert.equal(recallScore(0), '0.0000');
});

test('current candidate scores, decisions and scorer query join by stable typed ref', () => {
  const result = hookResponse({
    status: 'matched', selected_refs: ['scene:assistant_birthday'],
    candidate_retrieval: {candidate_count: 8},
    candidate_scores: [{ref: 'scene:assistant_birthday', title: 'Orion的生日', vector_score: 0.61,
      reranker_score: 0.99, candidate_sources: ['scene_whole_embedding']}],
    admission: {mode: 'direct_evidence_rerank', direct_threshold: 0.65, rerank_query: 'Orion的生日是哪天？',
      candidates: [{ref: 'scene:assistant_birthday', disposition: 'direct', reason: 'reranker_direct_evidence', rerank_score: 0.99},
        {ref: 'event:user_birthday', disposition: 'reject', reason: 'reranker_score_missing'}]},
  });
  result.cards = [{id: 'scene:assistant_birthday', title: 'Orion的生日'}];
  const view = recallSimulationDiagnostics(result);
  assert.equal(view.candidateCount, 8); // Partial details do not become the pool count.
  assert.equal(view.stage, '已生成模拟卡片');
  assert.equal(view.rerankQuery, 'Orion的生日是哪天？');
  assert.equal(view.candidates[0].title, 'Orion的生日');
  assert.equal(view.candidates[0].candidate_score, 0.61);
  assert.equal(view.candidates[0].rerank_score, 0.99);
  assert.equal(view.candidates[0].selected, true);
  assert.equal(recallScore(view.candidates[1].rerank_score), '—');
});

test('legacy diagnostics still render and current training captures route score without inventing confidence', () => {
  const legacy = recallSimulationDiagnostics({cards: [], debug: {candidate_count: 3,
    semantic_recall_debug: {route: 'recall_needed', applied_action: 'recall', confidence: 0.9,
      scores: [{route: 'recall_needed', action: 'recall', score: 0.9}]}}});
  assert.equal(legacy.candidateCount, 3);
  assert.equal(legacy.routeScore, 0.9);
  assert.equal(legacy.routeScores[0].route, 'recall_needed');
  const response = hookResponse();
  const label = createRecallSimulationTrainingLabel({query: response.query, expectedAction: 'recall',
    observedAction: 'recall', observedRoute: 'present_chitchat',
    simulationTelemetry: {semantic: response.debug.semantic_recall_debug}}, {batchId: 'fixture'});
  assert.equal(label.observedAction, 'recall');
  assert.equal(label.simulationTelemetry.route.score, 0.47);
  assert.equal(label.simulationTelemetry.route.confidence, null);
  assert.equal(label.simulationTelemetry.route.scores[0].route, 'present_chitchat');
  assert.equal(label.simulationTelemetry.route.reason, 'uncertain_route');
});
