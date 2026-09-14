import { useEffect, useRef, useState } from 'react';
import { WarningCircle, X } from '@phosphor-icons/react';
import { MarkdownProjection } from './MarkdownProjection.jsx';
import './revision-materials.css';

const labels = { event:'Event', scene:'Scene', diary:'日记', darkroom:'暗室', upload:'附件' };
const unavailable = { deleted:'已删除', locked:'尚未解锁', not_found:'材料已不可用', archived:'已归档' };
export function materialCount(item) {
  return Object.keys(labels).reduce((sum, kind) => sum + new Set(item[`source_${kind}_ids`] || []).size, 0);
}
async function request(body, signal) {
  const response = await fetch('/__serein/memory/revision-materials', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), signal,
  });
  const result = await response.json();
  if (!response.ok || result.status === 'not_found') throw new Error(result.message || '材料已不可用，请刷新修订箱。');
  return result;
}

function MaterialPreview({ proposalId, material, onClose }) {
  const dialog = useRef(null);
  const [state, setState] = useState({ loading:true });
  useEffect(() => {
    const trigger = document.activeElement;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.current.showModal();
    return () => {
      document.body.style.overflow = overflow;
      if (trigger?.isConnected) trigger.focus({ preventScroll:true });
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    request({ proposalId, kind:material.kind, identifier:material.id }, controller.signal).then(result => {
      if (!result.readable) throw new Error(unavailable[result.status] || '这条材料当前不可读取。');
      setState({ loading:false, document:result.document });
    }).catch(error => {
      if (!controller.signal.aborted) setState({ loading:false, error:error.message });
    });
    return () => controller.abort();
  }, [proposalId, material.kind, material.id]);
  return <dialog ref={dialog} className="basement-scene-preview revision-material-preview"
    aria-labelledby="revision-material-title" onCancel={onClose} onClick={event => {
      if (event.target !== event.currentTarget) return;
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
    }}>
    <button type="button" className="basement-scene-preview__close" aria-label="关闭材料卡" onClick={onClose}><X size={19}/></button>
    <header><span>{labels[material.kind]} · 只读</span><time>{material.date?.slice(0, 10) || '日期未写'}</time>
      <h3 id="revision-material-title">{state.document?.title || material.title || '未命名材料'}</h3></header>
    <div className="basement-scene-preview__body">
      {state.loading && <p className="basement-scene-preview__loading">正在读取这条材料……</p>}
      {state.error && <p className="basement-scene-preview__error" role="alert"><WarningCircle size={16}/>{state.error}</p>}
      {state.document && <MarkdownProjection content={state.document.body_md || '（正文为空）'}/>}
    </div>
    <footer><code>{material.id}</code><span>Esc 关闭</span></footer>
  </dialog>;
}

export function RevisionMaterials({ item }) {
  const [expanded, setExpanded] = useState(false);
  const [state, setState] = useState({ items:[], loading:false, error:'', next:null });
  const [preview, setPreview] = useState(null);
  const controller = useRef(null);
  async function load(offset = 0) {
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setState(previous => ({ ...previous, loading:true, error:'' }));
    try {
      const result = await request({ proposalId:item.proposal_id, offset }, current.signal);
      if (!current.signal.aborted) setState(previous => ({ items:offset ? [...previous.items, ...result.items] : result.items,
        total:result.total, loading:false, error:'', next:result.next_offset }));
    } catch (error) {
      if (!current.signal.aborted) setState(previous => ({ ...previous, loading:false, error:error.message }));
    }
  }
  useEffect(() => {
    if (expanded) load();
    return () => controller.current?.abort();
  }, [expanded, item.proposal_id, item.updated_at]);
  return <>
    <details className="revision-materials" onToggle={event => setExpanded(event.currentTarget.open)}>
      <summary>{state.total ?? materialCount(item)} 条候选材料</summary>
      {state.items.length > 0 && <ul>{state.items.map(material => <li key={`${material.kind}:${material.id}`}>
        <button type="button" disabled={!material.readable} onClick={() => setPreview(material)}>
          <span>{labels[material.kind] || material.kind}</span><strong>{material.title || (material.readable ? '未命名材料' : unavailable[material.status] || '当前不可读取')}</strong>
          {material.date && <time>{material.date.slice(0, 10)}</time>}
        </button>
      </li>)}</ul>}
      {state.loading && <p role="status">正在读取材料标题……</p>}
      {state.error && <p role="alert">{state.error} <button type="button" onClick={() => load(state.next || 0)}>重试</button></p>}
      {!state.error && !state.loading && state.next != null && <button type="button" onClick={() => load(state.next)}>更多材料</button>}
    </details>
    {preview && <MaterialPreview key={`${preview.kind}:${preview.id}`} proposalId={item.proposal_id} material={preview} onClose={() => setPreview(null)}/>}
  </>;
}
