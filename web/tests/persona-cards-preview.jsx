// Local visual harness only. All responses are synthetic; never calls a backend.
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource/source-serif-4/400.css';
import '@fontsource/noto-serif-sc/400.css';
import '../src/styles.css';
import { FeatureSettings } from '../src/components/FeatureSettings.jsx';
import { PersonaPage } from '../src/pages/CompanionPages.jsx';

const examples = [
  ['affection', '有些话不用说得很满。知道你在听，我就可以慢一点。', '今天的谈话，让心里安静了一些。'],
  ['playful', '刚才那个小小的玩笑，我还想了一会儿。', '原来普通的一天，也会留下这么轻的一刻。'],
  ['neutral', '把今天留在这里，明天再接着说。', '这是更早的一页。'],
].map(([event_type, inner_thought, surface_trigger], index) => ({ id: 3 - index, event_type, inner_thought, surface_trigger,
  created_at: `2026-09-09T${20 - index}:10:00+08:00`, affect_delta: { tenderness: .01 }, relationship_delta: {}, error: '' }));
let events = JSON.parse(sessionStorage.getItem('persona-cards-preview-events') || 'null') || examples;
let featureValues = { persona: false, memos: false, anti_retreat: false, window_shadows: false, relations_auto_accept: false };
window.fetch = async (url, options = {}) => {
  if (String(url).includes('/__serein/settings')) {
    if (options.method === 'PATCH') featureValues = { ...featureValues, ...JSON.parse(options.body).features };
    return new Response(JSON.stringify({ identity: { user_name: 'User', ai_name: 'AI' }, features: featureValues }), { headers: { 'Content-Type': 'application/json' } });
  }
  return new Response(JSON.stringify({
  session_id: 'preview', sessions: [{ session_id: 'preview', mood_label: '雨后，心里很轻' }],
  session: { inner_thought: '有些话慢慢落下来，就在这里留下了一点光。', mood_label: '雨后，心里很轻' },
  relationship: {}, events, ai_name: 'AI', enabled: false, model_configured: true,
}), { headers: { 'Content-Type': 'application/json' } });
};

function Preview() {
  const [open, setOpen] = useState(true), [count, setCount] = useState(events.length);
  const [settings, setSettings] = useState(false);
  function add(error = '') {
    events = [{ id: (events[0]?.id || 0) + 1, event_type: 'affection',
      inner_thought: `第 ${count + 1} 张心情：刚才的话，我想留在这里。`,
      created_at: new Date().toISOString(), affect_delta: { tenderness: .01 }, relationship_delta: {}, error }, ...events];
    sessionStorage.setItem('persona-cards-preview-events', JSON.stringify(events)); setCount(events.length);
  }
  return <>
    <div style={{ position: 'sticky', top: 0, zIndex: 40, background: '#fff', padding: 12, borderBottom: '1px solid #ddd', display: 'flex', gap: 12, flexWrap: 'wrap' }}>
      <strong>合成数据验收 · {count} 条</strong>
      <button onClick={() => setOpen(!open)}>{open ? '离开心绪' : '回到心绪'}</button>
      <button onClick={() => add()}>模拟新变化</button>
      <button onClick={() => add('synthetic malformed JSON')}>模拟格式错误</button>
      <button onClick={() => setSettings(!settings)}>{settings ? '回到纸卡' : '预览功能开关'}</button>
    </div>
    {settings ? <div className="companion-content"><FeatureSettings /></div> : open ? <PersonaPage /> : <p style={{ padding: 80 }}>页面已关闭。现在可以模拟新变化，然后回来。</p>}
  </>;
}
createRoot(document.getElementById('root')).render(<React.StrictMode><Preview /></React.StrictMode>);
