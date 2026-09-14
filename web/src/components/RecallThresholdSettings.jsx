import {useState} from 'react';
import {instanceSettings} from '../storage/instanceStore.js';

export function RecallThresholdSettings({config,draft,setDraft,onSaved,disabled=false}) {
  const [busy,setBusy]=useState(false),[status,setStatus]=useState('');
  const value=draft??config.recall?.direct_threshold??0.65;
  const valid=value!==''&&Number.isFinite(Number(value))&&Number(value)>=0&&Number(value)<=1;
  async function save(){
    if(!valid)return;
    setBusy(true);setStatus('');
    try{
      const result=await instanceSettings({expected_version:config.settings_version,recall:{direct_threshold:Number(value)}});
      onSaved(result);setDraft(null);setStatus('已保存，后续聊天使用此阈值。');
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  return <section className="settings-group" aria-labelledby="settings-recall-threshold-title">
    <div className="settings-group__heading"><h3 id="settings-recall-threshold-title">召回严格程度</h3><p>只调整最终筛选。越低越容易带入记忆，也可能带入不相关内容；分数不代表正确率。</p></div>
    <label className="settings-field"><span>最终筛选阈值（默认 0.65）</span>
      <input type="number" min="0" max="1" step="0.01" value={value} disabled={disabled||busy} onChange={event=>setDraft(event.target.value)}/></label>
    <p className="model-connection-help">路由跳过、主域限制和召回冷却仍照常执行。保存后下一轮生效，无需重建向量。</p>
    <div className="settings-actions threshold-actions">
      <button type="button" disabled={disabled||busy||!valid||Number(value)===(config.recall?.direct_threshold??0.65)} onClick={save}>{busy?'保存中…':'保存阈值'}</button>
      <button type="button" disabled={disabled||busy||!valid} onClick={()=>{
        window.dispatchEvent(new CustomEvent('serein:open-recall-simulation',{detail:{threshold:Number(value)}}));
        window.location.hash='#basement';
      }}>去召回模拟试调</button>
    </div>
    {status&&<p role="status">{status}</p>}
  </section>;
}
