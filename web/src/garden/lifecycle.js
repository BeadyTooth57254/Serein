export const LIFE = {
  soakSeconds: 3.2,
  drySeconds: 9,
  openSeconds: 4.2,
  closeSeconds: 7,
  afterUserSeconds: 4.5,
  openThreshold: .15,
};

export function advanceFlower(flower, rain, dt) {
  flower.wetness = Math.max(0, Math.min(1,
    flower.wetness + dt * (rain > .05 ? rain / LIFE.soakSeconds : -1 / LIFE.drySeconds)));
  if (rain > .05) flower.afterUser = LIFE.afterUserSeconds;
  else flower.afterUser = Math.max(0, flower.afterUser - dt);
  const wetTarget = Math.min(1, Math.max(0, (flower.wetness - LIFE.openThreshold) / .62));
  const target = rain > .05
    ? Math.max(flower.bloom, wetTarget)
    : flower.afterUser > 0 ? flower.bloom : 0;
  const rate = target > flower.bloom ? 1 / LIFE.openSeconds : 1 / LIFE.closeSeconds;
  // Clamp the step to the remaining distance: rewetting can reverse any pose.
  flower.bloom += Math.sign(target - flower.bloom) * Math.min(Math.abs(target - flower.bloom), dt * rate);
}
