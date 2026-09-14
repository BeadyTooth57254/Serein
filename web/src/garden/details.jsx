import React from 'react';
import { createRoot } from 'react-dom/client';
import { MarkdownProjection } from '../components/MarkdownProjection.jsx';

export function createMemoryDetails() {
  const panel = document.querySelector('#memory-detail');
  const root = createRoot(panel);
  function close() {
    panel.hidden = true;
    document.querySelector('#world').focus({ preventScroll: true });
  }
  panel.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.stopPropagation(); close(); }
  });
  return {
    close,
    isOpen: () => !panel.hidden,
    open(record, related) {
      panel.hidden = false;
      root.render(<>
        <header className="memory-detail-header">
          <div className="memory-detail-meta">
            <span>{record.kind === 'scene' ? 'Scene' : 'Event'}</span>
            {record.date && <time dateTime={record.date}>{record.date.replaceAll('-', '.')}</time>}
            {record.archived && <span>已归档</span>}
          </div>
          <button className="memory-detail-close" aria-label="关闭记忆详情" onClick={close}>×</button>
          <h2 id="memory-detail-title">{record.title}</h2>
        </header>
        <div className="memory-detail-scroll" data-memory-key={record.key}>
          <MarkdownProjection content={record.content} />
          {related.length > 0 && <section className="memory-detail-relations">
            <h3>相连的记忆</h3>
            <ul>{related.map(item => <li key={item.key}>{item.title}</li>)}</ul>
          </section>}
        </div>
      </>);
    },
  };
}
