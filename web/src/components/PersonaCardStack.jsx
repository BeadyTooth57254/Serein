import { Children, useEffect, useLayoutEffect, useRef, useState } from 'react';
import gsap from 'gsap';
import { personaChanges, readPersonaSeen, writePersonaSeen } from '../utils/personaSeen.js';

export function PersonaCardStack({ events, scope, visible, children }) {
  const container = useRef(null);
  const [pending, setPending] = useState(null);
  const [expanded, setExpanded] = useState(false);
  const [selectedId, setSelectedId] = useState(null);
  const wheel = useRef({ total: 0, last: 0, index: 0 });
  const motion = useRef({ position: 0, spread: 0 });
  const animation = useRef(null);
  const previousPage = useRef(null);
  const cards = Children.toArray(children);
  const active = Math.max(0, events.findIndex(event => event.id === selectedId));
  const activeId = events[active]?.id;
  const select = index => { setSelectedId(events[index].id); setExpanded(true); };

  // One continuous position drives both directions, including layer order.
  // Retarget the running tween instead of queuing or locking wheel gestures.
  useLayoutEffect(() => {
    if (!container.current) return;
    const context = gsap.context(() => {}, container);
    animation.current = context;
    return () => { context.revert(); animation.current = null; };
  }, [events.length > 0]);
  useLayoutEffect(() => {
    wheel.current.index = active;
    if (!animation.current) return;
    if (previousPage.current?.id === activeId) motion.current.position += active - previousPage.current.index;
    previousPage.current = { id: activeId, index: active };
    const sheets = [...container.current.children];
    const draw = () => sheets.forEach((sheet, index) => {
      const distance = index - motion.current.position;
      const depth = Math.min(Math.abs(distance), 4), spread = motion.current.spread;
      const openY = Math.sign(distance) * (Math.min(depth, 1) * 48 + Math.max(0, depth - 1) * 10);
      const y = openY * spread + depth * 9 * (1 - spread);
      const rotation = (index === 0 ? -1.2 : Math.sin(index * 2.4) * 1.6) * (1 - spread);
      sheet.style.transform = `translate3d(${Math.sin(index * 2) * 4 * (1 - spread)}px,${y}px,0) rotate(${rotation}deg) scale(${1 - depth * .028})`;
      sheet.style.zIndex = String(Math.round(1000 - depth * 100));
      sheet.style.opacity = String(Math.max(0, Math.min(1, 4 - depth)));
      sheet.style.visibility = depth >= 4 ? 'hidden' : 'visible';
      sheet.style.setProperty('--ink-opacity', String(Math.max(0, 1 - depth * 1.5)));
    });
    draw();
    animation.current.add(() => {
      gsap.to(motion.current, { position: active, spread: expanded ? 1 : 0,
        duration: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : Math.max(.18, .44 / Math.sqrt(Math.max(1, Math.abs(active - motion.current.position)))),
        ease: 'power3.out', overwrite: true, onUpdate: draw, onComplete: draw });
    });
  }, [active, activeId, expanded, events.length]);

  // Expanded deck gestures stay inside the deck, including either end.
  // Long card text scrolls first; scrolling outside the deck moves the page.
  useEffect(() => {
    const node = container.current;
    if (!node || !expanded || !visible) return;
    const onWheel = event => {
      if (event.ctrlKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
      event.stopPropagation();
      const body = node.querySelector('.is-current .persona-trace');
      const direction = Math.sign(event.deltaY);
      if (body?.contains(event.target) && (direction > 0
        ? body.scrollTop + body.clientHeight < body.scrollHeight - 2 : body.scrollTop > 2)) return;
      event.preventDefault();
      if (!direction) return;
      const now = performance.now();
      const gesture = wheel.current;
      if (now - gesture.last > 180 || Math.sign(gesture.total) !== direction) gesture.total = 0;
      gesture.last = now;
      gesture.total += event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? node.clientHeight : 1);
      const steps = Math.trunc(gesture.total / 100);
      if (!steps) return;
      gesture.total -= steps * 100;
      const next = Math.max(0, Math.min(events.length - 1, gesture.index + steps));
      gesture.index = next;
      setSelectedId(events[next].id);
    };
    node.addEventListener('wheel', onWheel, { passive: false });
    return () => node.removeEventListener('wheel', onWheel);
  }, [active, expanded, events, visible]);
  useEffect(() => {
    if (!visible) return;
    const changes = personaChanges(events, readPersonaSeen(scope));
    if (changes.baseline) writePersonaSeen(scope, changes.latest);
    setPending(previous => {
      if (!changes.unread.length) return null;
      return previous?.scope === scope && previous.id === changes.latest ? previous : {
        scope, id: changes.latest, cardId: Math.max(...changes.unread.map(event => event.id)),
      };
    });
  }, [events, scope, visible]);

  useEffect(() => {
    if (!pending || pending.scope !== scope || !visible || pending.cardId !== activeId) return;
    const card = container.current.querySelector(`[data-event-id="${pending.cardId}"]`);
    if (!card) return;
    const heading = card.querySelector('.persona-trace-heading');
    let started = false;
    const context = gsap.context(() => {}, container);
    const finish = () => {
      if (document.hidden) return;
      writePersonaSeen(scope, pending.id); setPending(null);
    };
    const observer = new IntersectionObserver(entries => {
      if (started || document.hidden || !entries.some(entry => entry.isIntersecting && entry.intersectionRatio >= .75)) return;
      started = true;
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { finish(); return; }
      context.add(() => {
        gsap.fromTo(card, { y: -22, opacity: .55 }, {
          y: 0, opacity: 1, duration: .75, ease: 'power2.out', onComplete: finish,
        });
      });
    }, { threshold: .75 });
    observer.observe(heading);
    return () => { observer.disconnect(); context.revert(); };
  }, [pending, scope, visible, activeId]);

  return <>
    {pending?.scope === scope && <p className="persona-new-note" role="status">新的一页，留在最上面。
      {pending.cardId !== activeId && <button type="button" onClick={() => setSelectedId(pending.cardId)}>看看新的一页</button>}</p>}
    {events.length > 0 && <>
      <div className={`persona-handnotes${expanded ? ' is-open' : ''}`}>
      <div className="persona-deck-toolbar">
        {events.length > 1 && <button type="button" onClick={() => { setExpanded(!expanded); if (expanded) setSelectedId(null); }}>{expanded ? '收起纸页' : '展开纸页'}</button>}
      </div>
      <div ref={container} className={`persona-card-stack${expanded ? ' is-expanded' : ''}`} role="region" aria-label="心情纸卡"
        tabIndex={0} onKeyDown={event => {
          if (event.target !== event.currentTarget) return;
          const next = event.key === 'ArrowDown' || event.key === 'ArrowRight' ? active + 1 : event.key === 'ArrowUp' || event.key === 'ArrowLeft' ? active - 1 : -1;
          if (next >= 0 && next < events.length) { event.preventDefault(); select(next); }
          if (event.key === 'Escape') { setExpanded(false); setSelectedId(null); }
        }}>
        {cards.map((card, index) => {
          const current = index === active;
          return <div key={events[index].id} className={`persona-deck-sheet${current ? ' is-current' : ''}`}>
            <div className="persona-deck-body" inert={!current}>{card}</div>
            {!current && <button type="button" className={`persona-deck-pick${index < active ? ' is-before' : ''}`} aria-label={`翻到第 ${index + 1} 张心情`} onClick={() => select(index)}><span>{new Date(events[index].created_at).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })}</span></button>}
          </div>;
        })}
      </div>
      <div className="persona-deck-pagination">
        <button type="button" aria-label="较新的心情" disabled={active === 0} onClick={() => select(active - 1)}>←</button>
        <span aria-live="polite">{String(active + 1).padStart(2, '0')} / {String(events.length).padStart(2, '0')}</span>
        <button type="button" aria-label="较早的心情" disabled={active === events.length - 1} onClick={() => select(active + 1)}>→</button>
      </div>
      </div>
    </>}
  </>;
}
