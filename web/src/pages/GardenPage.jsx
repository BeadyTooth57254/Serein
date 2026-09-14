import { useEffect, useRef } from 'react';

// An independent document preserves the approved renderer and releases its
// animation loop, event listeners and WebGL context whenever the garden closes.
export function GardenPage({ onReady }) {
  const frame = useRef(null);
  useEffect(() => {
    const receive = event => {
      if (event.origin === location.origin && event.source === frame.current?.contentWindow
        && event.data?.type === 'serein:garden-ready') onReady();
    };
    window.addEventListener('message', receive);
    return () => window.removeEventListener('message', receive);
  }, [onReady]);
  return <iframe ref={frame} className="garden-frame" title="a garden of us · 记忆花园"
    src={`${import.meta.env.BASE_URL}garden.html`} />;
}
