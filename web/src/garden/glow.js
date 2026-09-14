export const GLOW = {
  selected: 1,
  response: .26,
  outerPetals: .70,
  innerPetals: 2.1,
  fadeIn: 3.8,
  fadeOut: 2.8,
};

// User controls opening. Selection/response controls emission independently.
export function advanceGlow(flower, selected, response, dt) {
  if (flower.archived || flower.bloom <= .01) {
    flower.glow = 0;
    return;
  }
  const t = Math.max(0, Math.min(1, (flower.bloom - .08) / .77));
  const openness = t * t * (3 - 2 * t);
  const target = (selected ? GLOW.selected : response ? GLOW.response : 0) * openness;
  const speed = target > flower.glow ? GLOW.fadeIn : GLOW.fadeOut;
  flower.glow += (target - flower.glow) * (1 - Math.exp(-speed * dt));
}
