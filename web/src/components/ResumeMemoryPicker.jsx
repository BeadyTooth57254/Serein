import {useEffect,useRef,useState} from 'react';
import {createPortal} from 'react-dom';
import {X} from '@phosphor-icons/react';

export function ResumeMemoryPicker({ids,disabled,onChange}) {
  const dialog=useRef(null);
  const [open,setOpen]=useState(false),[draft,setDraft]=useState([]),[kind,setKind]=useState('event');
  const [query,setQuery]=useState(''),[date,setDate]=useState(''),[offset,setOffset]=useState(0);
  const [page,setPage]=useState({items:[],total:0}),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [selectedOnly,setSelectedOnly]=useState(false),[retry,setRetry]=useState(0);
  const selectedKey=selectedOnly?JSON.stringify(draft):'';
  useEffect(()=>{
    if(!open)return;
    const abort=new AbortController();setBusy(true);setError('');
    const timer=setTimeout(async()=>{
      try {
        if(selectedOnly&&!draft.length){setPage({items:[],total:0});return;}
        const params=new URLSearchParams({kind:selectedOnly?'':kind,q:query,date,offset:String(offset)});
        if(selectedOnly)draft.forEach(id=>params.append('ids',id));
        const response=await fetch('/__serein/settings/resume-candidates?'+params,{signal:abort.signal});
        if(!response.ok)throw new Error('暂时没有读到记忆，请重试。');
        const result=await response.json();if(!abort.signal.aborted)setPage(result);
      } catch(error) {if(!abort.signal.aborted)setError(error.message);}
      finally{if(!abort.signal.aborted)setBusy(false);}
    },180);
    return()=>{clearTimeout(timer);abort.abort();};
  },[open,kind,query,date,offset,selectedOnly,selectedKey,retry]);
  function begin(){setDraft([...ids]);setKind('event');setSelectedOnly(false);setQuery('');setDate('');setOffset(0);setOpen(true);dialog.current.showModal();}
  function close(){dialog.current.close();setOpen(false);}
  function toggle(id){setDraft(current=>current.includes(id)?current.filter(key=>key!==id):current.length<200?[...current,id]:current);}
  return <>
    <button className="resume-picker-trigger" type="button" disabled={disabled} onClick={begin}>选择内容 · {ids.length} 条</button>
    {createPortal(<dialog ref={dialog} className="resume-picker" aria-labelledby="resume-picker-title" onCancel={()=>setOpen(false)}>
      <header><div><span>RESUME</span><h3 id="resume-picker-title">自选事件 / Scene</h3></div><button type="button" aria-label="关闭选择器" onClick={close}><X size={22}/></button></header>
      <p>选好要带进新窗口的内容，确认后保存功能设置。</p>
      <div className="resume-picker-tabs" role="tablist" aria-label="续接内容类型">
        {[['event','事件'],['scene','Scene'],['selected','已选内容']].map(([key,label])=><button type="button" role="tab" key={key} aria-selected={key==='selected'?selectedOnly:!selectedOnly&&kind===key}
          onClick={()=>{setSelectedOnly(key==='selected');if(key!=='selected')setKind(key);setOffset(0);}}>{label}</button>)}
      </div>
      <div className="resume-picker-filters"><input type="search" aria-label="搜索续接记忆" placeholder="搜索标题或正文" value={query} onChange={event=>{setQuery(event.target.value);setOffset(0);}}/>
        <input type="date" aria-label="筛选记录日期" value={date} onChange={event=>{setDate(event.target.value);setOffset(0);}}/></div>
      <div className="resume-picker-actions"><span>已选 {draft.length} / 200 条</span><button type="button" disabled={busy||!!error} onClick={()=>setDraft(current=>[...new Set([...current,...page.items.map(item=>item.id)])].slice(0,200))}>选中本页</button><button type="button" onClick={()=>setDraft([])}>清空选择</button></div>
      <div className="resume-picker-list" aria-busy={busy}>
        {error?<p role="alert">{error}<button type="button" onClick={()=>setRetry(value=>value+1)}>重试</button></p>:busy?<p role="status">读取中…</p>:page.items.length?page.items.map(item=><article key={item.id}>
          <label><input type="checkbox" checked={draft.includes(item.id)} disabled={!draft.includes(item.id)&&draft.length>=200} onChange={()=>toggle(item.id)}/><span><small>{item.kind==='event'?'事件':'Scene'} · {item.date}</small><strong>{item.title||'未命名'}</strong><span className="resume-picker-preview">{item.body_md.slice(0,220)}</span></span></label>
          <details><summary>读全文</summary><p>{item.body_md}</p></details>
        </article>):<p>{selectedOnly?'没有符合筛选的已选内容。已删除或归档的内容不会续接，可清空后重新选择。':'没有符合筛选的内容。'}</p>}
      </div>
      <footer><div><button type="button" disabled={busy||offset===0} onClick={()=>setOffset(value=>Math.max(0,value-30))}>上一页</button><span>{Math.floor(offset/30)+1} / {Math.max(1,Math.ceil(page.total/30))}</span><button type="button" disabled={busy||!page.has_more} onClick={()=>setOffset(value=>value+30)}>下一页</button></div><button type="button" onClick={()=>{onChange(draft);close();}}>确认选择 · {draft.length} 条</button></footer>
    </dialog>,document.body)}
  </>;
}
