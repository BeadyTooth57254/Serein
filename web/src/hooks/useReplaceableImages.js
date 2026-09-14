import { useEffect, useRef, useState } from "react";
import { people } from "../data/awake.js";
import { loadImages, saveImage } from '../storage/appearanceStore.js';

export function useReplaceableImages() {
  const [images, setImages] = useState({
    hero: `${import.meta.env.BASE_URL}assets/awake-cover.svg`,
    [people[0].key]: people[0].image,
    [people[1].key]: people[1].image,
  });
  const [busy, setBusy] = useState(true);
  const [status, setStatus] = useState('');
  const pending = useRef(true);
  const mounted = useRef(false);
  const keys = {hero: 'hero', [people[0].key]: 'user', [people[1].key]: 'assistant'};

  useEffect(() => {
    let active = true;
    mounted.current = true;
    loadImages().then(saved => {
      if (active) setImages(current => ({...current, ...Object.fromEntries(
        Object.entries(keys).filter(([, key]) => saved[key]).map(([key, stored]) => [key, saved[stored]]))}));
    }).catch(() => { if (active) setStatus('已保存的图片暂时无法读回，请刷新后重试。'); })
      .finally(() => { if (active) { pending.current = false; setBusy(false); } });
    return () => { active = false; mounted.current = false; };
  }, []);

  const replace = async (key, file) => {
    if (!file || pending.current || !keys[key]) return false;
    pending.current = true; setBusy(true); setStatus('正在保存图片…');
    try {
      const saved = await saveImage(keys[key], file);
      if (mounted.current) {
        setImages(current => ({...current, [key]: saved}));
        setStatus(key === 'hero' ? '背景图已保存。' : '头像已保存。');
      }
      return true;
    } catch (error) { if (mounted.current) setStatus(error.message); return false; }
    finally { pending.current = false; if (mounted.current) setBusy(false); }
  };

  return { images, replace, busy, status };
}
