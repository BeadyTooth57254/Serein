import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { FXAAShader } from 'three/addons/shaders/FXAAShader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { groundHeight, terrainGeometry, mudTextures, petalGeometry, vegetation, random } from './geometry.js';
import { petalMaterials, flowerAccentMaterial, rainMaterial, atmosphereMaterial, nearbyMaterial } from './materials.js';
import { advanceFlower, LIFE } from './lifecycle.js';
import { advanceGlow, GLOW } from './glow.js';
import { GardenView, FlowerWindow, SpatialInstances, StreamedTerrain, NEARBY } from './visibility.js';
import { makeMemoryFlowers } from './memories.js';
import './style.css';

export function startGarden({ records, relations, layout, details }) {
const canvas = document.querySelector('#world');
const mobile = window.innerWidth < 650;
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const status = document.querySelector('#status');
document.querySelector('#hint').textContent = mobile
  ? '单指移雨 · 双指移景 / 缩放 · 轻触一朵花' : '拖动移雨 · 右键移景 · 滚轮靠近 · 轻触一朵花';
const flowers = makeMemoryFlowers(records, layout);
const flowersByKey = new Map(flowers.filter(f => f.record).map(f => [f.record.key, f]));
let responseFlowerIds = new Set();
const flowerBounds = {
  x: [Math.min(...flowers.map(f => f.x)), Math.max(...flowers.map(f => f.x))],
  z: [Math.min(...flowers.map(f => f.z)), Math.max(...flowers.map(f => f.z))],
};
const cameraBounds = {
  x: [flowerBounds.x[0] - 1.5, flowerBounds.x[1] + 1.5],
  z: [flowerBounds.z[0] - 1.5, flowerBounds.z[1] + 1.5],
};
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: false, powerPreference: 'high-performance' });
} catch (error) {
  document.querySelector('#failure').hidden = false;
  throw error;
}
const renderRatio = () => Math.min(window.devicePixelRatio, mobile ? 1.4 : 1.15,
  Math.sqrt(1450000 / (innerWidth * innerHeight)));
renderer.setPixelRatio(renderRatio());
renderer.setSize(innerWidth, innerHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.04;

const scene = new THREE.Scene();
scene.background = new THREE.Color('#090d11');
scene.fog = new THREE.FogExp2('#090d11', .039);
const camera = new THREE.PerspectiveCamera(mobile ? 48 : 43, innerWidth / innerHeight, .1, 45);
const focus = new THREE.Vector3(0, .1, -.95);
const cameraOffset = new THREE.Vector3(0, 6.2, 8.6);
const cameraDistance = { initial: mobile ? .94 : 1, min: .4, max: 1 };
let zoom = cameraDistance.initial;
function updateCamera() {
  focus.x = THREE.MathUtils.clamp(focus.x, ...cameraBounds.x);
  focus.z = THREE.MathUtils.clamp(focus.z, ...cameraBounds.z);
  camera.position.copy(focus).addScaledVector(cameraOffset, zoom);
  camera.lookAt(focus);
  camera.updateMatrixWorld();
}
updateCamera();

const pmrem = new THREE.PMREMGenerator(renderer);
const room = new RoomEnvironment();
const environment = pmrem.fromScene(room, .02);
scene.environment = environment.texture;
room.dispose(); pmrem.dispose();
const ambient = new THREE.HemisphereLight('#a0adbd', '#14140e', .095);
scene.add(ambient);

const beam = {
  uLightOrigin: { value: new THREE.Vector3(-5.4, 8.0, -1.5) },
  uLightTarget: { value: new THREE.Vector3(.25, 0, 1.0) },
  uLightPower: { value: 1 },
  uLightAngle: { value: .135 },
};
const time = { value: 0 };
const spotlight = new THREE.SpotLight('#f3f0ea', 670, 30, beam.uLightAngle.value, .65, 2);
spotlight.position.copy(beam.uLightOrigin.value);
spotlight.target.position.copy(beam.uLightTarget.value);
spotlight.castShadow = true;
spotlight.shadow.mapSize.set(mobile ? 1024 : 2048, mobile ? 1024 : 2048);
spotlight.shadow.camera.near = .5; spotlight.shadow.camera.far = 22;
spotlight.shadow.bias = -.00008; spotlight.shadow.normalBias = .012;
scene.add(spotlight, spotlight.target);

const terrainMaterial = new THREE.MeshPhysicalMaterial({
  ...mudTextures(), color: '#c5c6c1', roughness: .69, metalness: 0,
  normalScale: new THREE.Vector2(.95, .95),
  clearcoat: .75, clearcoatRoughness: .19, envMapIntensity: .035,
});
const gardenHalfSize = Math.ceil((Math.max(...cameraBounds.x.map(Math.abs), ...cameraBounds.z.map(Math.abs))
  + NEARBY.groundOuter + 1.25) / 4) * 4;
const gardenView = new GardenView(gardenHalfSize);
const ground = new StreamedTerrain(scene, terrainMaterial, terrainGeometry);

const flowerWindow = new FlowerWindow(flowers);
const leafMaterial = new THREE.MeshStandardMaterial({
  color: '#8a968d', roughness: .28, metalness: .03,
  side: THREE.DoubleSide, envMapIntensity: .045,
});
const greenery = vegetation(scene, leafMaterial, flowers, mobile ? .62 : 1);
for (const f of flowers.filter(f => f.archived)) {
  greenery.stems.setColorAt(f.id, new THREE.Color('#171b19'));
  for (let j = 0; j < 3; j++) greenery.stemLeaves.setColorAt(f.id * 3 + j, new THREE.Color('#141816'));
}

const petalsPerFlower = 12;
const petalGeo = petalGeometry();
const bloomData = new Float32Array(flowers.length * petalsPerFlower);
const glowData = new Float32Array(bloomData.length);
const presenceData = new Float32Array(bloomData.length);
petalGeo.setAttribute('aBloom', new THREE.InstancedBufferAttribute(bloomData, 1).setUsage(THREE.DynamicDrawUsage));
petalGeo.setAttribute('aGlow', new THREE.InstancedBufferAttribute(glowData, 1).setUsage(THREE.DynamicDrawUsage));
petalGeo.setAttribute('aPresence', new THREE.InstancedBufferAttribute(presenceData, 1).setUsage(THREE.DynamicDrawUsage));
const { material: flowerMaterial, depth: flowerDepth } = petalMaterials(beam);
flowerMaterial.envMapIntensity = .009;
flowerMaterial.color.set('#fff5f0');
const petals = new SpatialInstances(petalGeo, flowerMaterial, bloomData.length, {
  itemSize: petalsPerFlower,
  levels: [petalGeometry(5, 3), petalGeometry(3, 2)],
  radius: id => flowers[id].scale * .28,
  forceHigh: id => flowers[id].hero || selected === id,
});
petals.customDepthMaterial = flowerDepth;
petals.castShadow = true; petals.receiveShadow = true;
// Shader deformation can extend outside the closed-bud bounds.
petals.frustumCulled = false;
const temp = new THREE.Object3D();
for (const f of flowers) {
  for (let j = 0; j < petalsPerFlower; j++) {
    const inner = j >= 7;
    const count = inner ? 5 : 7;
    temp.position.set(f.x, f.y + f.height + (inner ? .025 * f.scale : 0), f.z);
    temp.rotation.set(inner ? -.43 : 0, f.rotation + (inner ? j - 7 + .35 : j) * Math.PI * 2 / count, 0);
    const size = f.scale * (inner ? .74 : 1);
    temp.scale.set(size, size, size); temp.updateMatrix();
    const index = f.id * petalsPerFlower + j;
    petals.setMatrixAt(index, temp.matrix);
    const c = f.archived ? new THREE.Color('#121318')
      : new THREE.Color('#fff5e5').lerp(new THREE.Color('#e8c5c0'), f.pink * .25);
    petals.setColorAt(index, c);
    bloomData[index] = f.bloom;
  }
}
scene.add(petals);

// Small, real seed heads also give the open flowers a centre without a glow sprite.
const hearts = new SpatialInstances(new THREE.SphereGeometry(.028, 7, 5),
  new THREE.MeshStandardMaterial({ color: '#d7b881', roughness: .48, envMapIntensity: .1 }), flowers.length);
for (const f of flowers) {
  temp.position.set(f.x, f.y + f.height + .023 * f.scale, f.z);
  temp.rotation.set(0, 0, 0); temp.scale.setScalar(f.scale); temp.updateMatrix();
  hearts.setMatrixAt(f.id, temp.matrix);
  hearts.setColorAt(f.id, new THREE.Color(f.archived ? '#121318' : '#ffffff'));
}
scene.add(hearts);

// Reuse the existing stem/leaf/centre meshes and draw calls for a faint
// continuation of the petal glow. The turf material remains unchanged.
const accentGlow = new Float32Array(flowers.length);
const leafGlow = new Float32Array(flowers.length * 3);
const leafPresence = new Float32Array(leafGlow.length);
const accentAttribute = new THREE.InstancedBufferAttribute(accentGlow, 1).setUsage(THREE.DynamicDrawUsage);
const leafGlowAttribute = new THREE.InstancedBufferAttribute(leafGlow, 1).setUsage(THREE.DynamicDrawUsage);
for (const [mesh, attribute, strength] of [
  [greenery.stems, accentAttribute, .030],
  [greenery.stemLeaves, leafGlowAttribute, .022],
  [hearts, accentAttribute, .12],
]) {
  mesh.geometry.setAttribute('aGlow', attribute);
  mesh.material = flowerAccentMaterial(mesh.material, strength);
}
const presenceAttribute = new THREE.InstancedBufferAttribute(flowerWindow.weights, 1).setUsage(THREE.DynamicDrawUsage);
greenery.stems.geometry.setAttribute('aPresence', presenceAttribute);
hearts.geometry.setAttribute('aPresence', presenceAttribute);
greenery.stemLeaves.geometry.setAttribute('aPresence',
  new THREE.InstancedBufferAttribute(leafPresence, 1).setUsage(THREE.DynamicDrawUsage));
const flowerFields = [petals, hearts, greenery.stems, greenery.stemLeaves];
for (const field of flowerFields) {
  field.options.include = index => flowerWindow.resident.has(
    field === greenery.stemLeaves ? Math.floor(index / 3) : index);
}

// Scattered damp stones break up the soil at the base of the vegetation.
const rng = random(4571);
const stones = new SpatialInstances(new THREE.IcosahedronGeometry(1, 0),
  new THREE.MeshStandardMaterial({ color: '#424740', roughness: .34, envMapIntensity: .1 }), 1100);
for (let i = 0; i < stones.count; i++) {
  const x = (rng() - .5) * 20, z = (rng() - .5) * 22 - 1;
  temp.position.set(x, groundHeight(x, z) + .008, z);
  temp.rotation.set(rng() * 2, rng() * 6, rng());
  temp.scale.set(.015 + rng() * .04, .008 + rng() * .025, .014 + rng() * .04);
  temp.updateMatrix(); stones.setMatrixAt(i, temp.matrix);
}
stones.receiveShadow = stones.castShadow = true; scene.add(stones);
const spatialFields = [...Object.values(greenery), petals, hearts, stones];
for (const field of [greenery.grass, greenery.leaves, greenery.weeds]) {
  field.options.wrap = { width: 18, depth: 20, height: groundHeight };
}
stones.options.wrap = { width: 20, depth: 22, height: groundHeight };
nearbyMaterial(terrainMaterial, gardenView.uniforms);
const fadedMaterials = new Set();
const plantDepth = nearbyMaterial(new THREE.MeshDepthMaterial({ depthPacking: THREE.RGBADepthPacking,
  side: THREE.DoubleSide }), gardenView.uniforms);
const accentDepth = nearbyMaterial(new THREE.MeshDepthMaterial({ depthPacking: THREE.RGBADepthPacking,
  side: THREE.DoubleSide }), gardenView.uniforms, true);
nearbyMaterial(flowerDepth, gardenView.uniforms, true);
for (const field of spatialFields) {
  const flower = flowerFields.includes(field);
  if (!fadedMaterials.has(field.material)) {
    nearbyMaterial(field.material, gardenView.uniforms, flower);
    fadedMaterials.add(field.material);
  }
  field.customDepthMaterial = field === petals ? flowerDepth : flower ? accentDepth : plantDepth;
}
let renderedFlowerIds = new Set();

const rainCount = mobile ? 19000 : 34000;
const rainGeometry = new THREE.InstancedBufferGeometry();
const rainPlane = new THREE.PlaneGeometry(1, 1);
rainGeometry.index = rainPlane.index;
rainGeometry.attributes.position = rainPlane.attributes.position;
rainGeometry.attributes.uv = rainPlane.attributes.uv;
const rainSeeds = new Float32Array(rainCount * 4);
for (let i = 0; i < rainSeeds.length; i++) rainSeeds[i] = rng();
rainGeometry.setAttribute('aSeed', new THREE.InstancedBufferAttribute(rainSeeds, 4));
rainGeometry.instanceCount = rainCount;
const rain = new THREE.Mesh(rainGeometry, rainMaterial(beam, time));
rain.frustumCulled = false; rain.renderOrder = 3;
scene.add(rain);

const size = renderer.getDrawingBufferSize(new THREE.Vector2());
const target = new THREE.WebGLRenderTarget(size.x, size.y, { type: THREE.HalfFloatType });
target.depthTexture = new THREE.DepthTexture(size.x, size.y, THREE.UnsignedIntType);
target.samples = mobile ? 0 : 2;
const atmosphere = atmosphereMaterial(target, camera, beam, time);
const finalScene = new THREE.Scene();
finalScene.add(new THREE.Mesh(new THREE.PlaneGeometry(2, 2), atmosphere));
const finalCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(finalScene, finalCamera));
const bloomPass = new UnrealBloomPass(new THREE.Vector2(innerWidth, innerHeight), .16, .5, .84);
composer.addPass(bloomPass);
composer.addPass(new OutputPass());
const antialias = new ShaderPass(FXAAShader);
antialias.uniforms.resolution.value.set(1 / size.x, 1 / size.y);
composer.addPass(antialias);

const tuning = { light: 780, exposure: 1.1, ambient: .14, angle: .175, mist: .015, bloom: .22 };
let raining = true;
let pressed = false;
let selected = null;
let elapsed = 0;
let lastTime = performance.now();
let frameCount = 0;
let fps = 0;
let fpsStart = lastTime;
const rainAim = beam.uLightTarget.value.clone();
const rainAxis = new THREE.Vector3();

function rainAmount(flower) {
  if (!raining && !pressed) return 0;
  const origin = beam.uLightOrigin.value;
  const x = flower.x - origin.x, y = flower.y + flower.height * .6 - origin.y, z = flower.z - origin.z;
  const along = x * rainAxis.x + y * rainAxis.y + z * rainAxis.z;
  const side = Math.hypot(x - rainAxis.x * along, y - rainAxis.y * along, z - rainAxis.z * along);
  const radius = along * Math.tan(beam.uLightAngle.value);
  return 1 - THREE.MathUtils.smoothstep(side, radius * .55, radius);
}

function animate(now) {
  const dt = Math.max(0, Math.min((now - lastTime) / 1000, .06));
  lastTime = now;
  if (!document.hidden) {
    elapsed += dt;
    time.value = elapsed * (reducedMotion ? .28 : 1);
    beam.uLightTarget.value.lerp(rainAim, 1 - Math.exp(-dt * 7));
    beam.uLightOrigin.value.set(-5.4 + focus.x, 8, -1.5 + focus.z + .95);
    spotlight.position.copy(beam.uLightOrigin.value);
    const intensity = raining || pressed ? 1 : .025;
    beam.uLightPower.value = THREE.MathUtils.damp(beam.uLightPower.value, intensity, 2.4, dt);
    spotlight.intensity = tuning.light * beam.uLightPower.value;
    spotlight.angle = beam.uLightAngle.value = tuning.angle;
    renderer.toneMappingExposure = tuning.exposure;
    ambient.intensity = tuning.ambient;
    atmosphere.uniforms.uMist.value = tuning.mist;
    bloomPass.strength = tuning.bloom;
    spotlight.target.position.copy(beam.uLightTarget.value);
    rainAxis.subVectors(beam.uLightTarget.value, beam.uLightOrigin.value).normalize();
    gardenView.center.set(focus.x, focus.z + 1.25 * zoom);
    const viewChanged = gardenView.update(camera, innerHeight, beam.uLightOrigin.value,
      beam.uLightTarget.value, beam.uLightAngle.value);
    if (flowerWindow.update(gardenView, dt)) {
      for (const field of flowerFields) field.invalidate();
    }
    const flowerLayoutChanged = viewChanged || petals.layoutDirty;
    ground.update(gardenView);
    for (const field of spatialFields) field.update(gardenView);
    if (flowerLayoutChanged) {
      renderedFlowerIds = new Set(petals.batches.flatMap(batch => batch.indices
        .filter(index => index % petalsPerFlower === 0).map(index => index / petalsPerFlower)));
    }
    for (const f of flowers) {
      advanceFlower(f, rainAmount(f), dt);
      advanceGlow(f, selected === f.id, responseFlowerIds.has(f.id), dt);
      bloomData.fill(f.bloom, f.id * petalsPerFlower, (f.id + 1) * petalsPerFlower);
      glowData.fill(f.glow * GLOW.outerPetals, f.id * petalsPerFlower, f.id * petalsPerFlower + 7);
      glowData.fill(f.glow * GLOW.innerPetals, f.id * petalsPerFlower + 7, (f.id + 1) * petalsPerFlower);
      accentGlow[f.id] = f.glow;
      leafGlow.fill(f.glow, f.id * 3, (f.id + 1) * 3);
      presenceData.fill(flowerWindow.weights[f.id], f.id * petalsPerFlower, (f.id + 1) * petalsPerFlower);
      leafPresence.fill(flowerWindow.weights[f.id], f.id * 3, (f.id + 1) * 3);
    }
    for (const field of spatialFields) field.syncAttributes();
    renderer.setRenderTarget(target); renderer.render(scene, camera);
    renderer.setRenderTarget(null); composer.render();
    frameCount++;
    if (now - fpsStart > 1200) {
      fps = frameCount * 1000 / (now - fpsStart); frameCount = 0; fpsStart = now;
    }
  }
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);

const rainToggle = document.querySelector('#rain-toggle');
const clearButton = document.querySelector('#clear-selection');
function setUser(value) {
  raining = value;
  rainToggle.textContent = raining ? '让雨停下' : '让雨落下';
  rainToggle.setAttribute('aria-pressed', String(raining));
}
function selectFlower(id) {
  selected = id;
  const flower = flowers[id];
  const related = [...(relations.get(flower?.record?.key) || [])]
    .map(key => flowersByKey.get(key)).filter(Boolean);
  responseFlowerIds = new Set(related.map(f => f.id));
  if (flower?.record) details.open(flower.record, related.map(f => f.record));
  else details?.close();
  petals.invalidate();
  clearButton.hidden = id === null;
  status.textContent = id === null ? '' : flower.archived ? '这一朵，静静留在这里。' : '这一朵，留着微光。';
}
rainToggle.addEventListener('click', () => setUser(!raining));
clearButton.addEventListener('click', () => selectFlower(null));
document.querySelector('#reset').addEventListener('click', () => {
  zoom = cameraDistance.initial;
  focus.set(0, .1, -.95); updateCamera();
  rainAim.set(.25, 0, 1); setUser(true); selectFlower(null);
});

const pointer = new THREE.Vector2();
const raycaster = new THREE.Raycaster();
const floor = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
const aim = new THREE.Vector3();
const pointers = new Map();
let gesture = null;
let pinch = null;
let holdTimer;

function groundAt(x, y, result) {
  pointer.set(x / innerWidth * 2 - 1, 1 - y / innerHeight * 2);
  raycaster.setFromCamera(pointer, camera);
  return raycaster.ray.intersectPlane(floor, result);
}
function pointAt(x, y) {
  if (groundAt(x, y, aim)) {
    rainAim.set(THREE.MathUtils.clamp(aim.x, ...flowerBounds.x), 0,
      THREE.MathUtils.clamp(aim.z, ...flowerBounds.z));
  }
}
function flowerScreen(f) {
  const p = new THREE.Vector3(f.x, f.y + f.height + .07, f.z).project(camera);
  return { x: (p.x + 1) * .5 * innerWidth, y: (1 - p.y) * .5 * innerHeight,
    visible: p.z > -1 && p.z < 1 && Math.abs(p.x) < 1.1 && Math.abs(p.y) < 1.1 };
}
function clickFlower(x, y) {
  let nearest = null, distance = Infinity;
  for (const f of flowers) {
    if (!renderedFlowerIds.has(f.id) || flowerWindow.weights[f.id] < .25) continue;
    const p = flowerScreen(f);
    const d = Math.hypot(p.x - x, p.y - y);
    const worldDistance = camera.position.distanceTo(new THREE.Vector3(f.x, f.y + f.height, f.z));
    const radius = Math.max(10, f.scale * .31 / worldDistance * innerHeight / Math.tan(camera.fov * Math.PI / 360) * .5);
    if (p.visible && d < radius && d < distance) { nearest = f; distance = d; }
  }
  if (nearest?.bloom > .6) selectFlower(nearest.id);
  else if (nearest) status.textContent = '把雨移到这里，等它慢慢开放。';
}
function pinchState() {
  const [a, b] = [...pointers.values()];
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2,
    distance: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)) };
}
function panCamera(dx, dy) {
  focus.x -= dx * .010 * zoom;
  focus.z -= dy * .014 * zoom;
  updateCamera();
}
function zoomCamera(distance, x = innerWidth / 2, y = innerHeight / 2) {
  // Keep the ground under the pointer in place as the camera approaches it.
  const anchor = groundAt(x, y, new THREE.Vector3());
  zoom = THREE.MathUtils.clamp(distance, cameraDistance.min, cameraDistance.max);
  updateCamera();
  if (anchor && groundAt(x, y, aim)) {
    focus.add(anchor.sub(aim));
    updateCamera();
  }
}
canvas.addEventListener('contextmenu', e => e.preventDefault());
canvas.addEventListener('pointerdown', e => {
  canvas.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pointers.size === 2) {
    clearTimeout(holdTimer); pressed = false; gesture = null;
    pinch = pinchState(); return;
  }
  gesture = { x: e.clientX, y: e.clientY, startX: e.clientX, startY: e.clientY, right: e.button === 2, held: false, at: performance.now() };
  if (e.button === 0) holdTimer = setTimeout(() => {
    if (!gesture) return;
    gesture.held = true; pressed = true; setUser(true); pointAt(e.clientX, e.clientY);
    status.textContent = '';
  }, 180);
});
canvas.addEventListener('pointermove', e => {
  if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pointers.size === 2 && pinch) {
    const next = pinchState();
    panCamera(next.x - pinch.x, next.y - pinch.y);
    zoomCamera(zoom * pinch.distance / next.distance, next.x, next.y);
    pinch = next; return;
  }
  if (!gesture) return;
  if (gesture.right) {
    panCamera(e.clientX - gesture.x, e.clientY - gesture.y);
  } else if (gesture.held) pointAt(e.clientX, e.clientY);
  gesture.x = e.clientX; gesture.y = e.clientY;
});
function finishPointer(e) {
  clearTimeout(holdTimer);
  if (gesture && !gesture.right && !gesture.held && e.type !== 'pointercancel'
    && Math.hypot(e.clientX - gesture.startX, e.clientY - gesture.startY) < 8) clickFlower(e.clientX, e.clientY);
  pointers.delete(e.pointerId);
  gesture = null; pressed = false; pinch = null;
}
canvas.addEventListener('pointerup', finishPointer);
canvas.addEventListener('pointercancel', finishPointer);
canvas.addEventListener('wheel', e => {
  e.preventDefault();
  const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? innerHeight : 1;
  zoomCamera(zoom * Math.exp(e.deltaY * unit * .0015), e.clientX, e.clientY);
}, { passive: false });
canvas.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (details?.isOpen()) details.close(); else selectFlower(null);
  }
  if (e.key === ' ') { e.preventDefault(); setUser(!raining); }
  if (e.key === '+' || e.key === '=') { e.preventDefault(); zoomCamera(zoom / 1.15); }
  if (e.key === '-') { e.preventDefault(); zoomCamera(zoom * 1.15); }
});
window.addEventListener('blur', () => {
  clearTimeout(holdTimer); pressed = false; pointers.clear(); gesture = null; pinch = null;
});
window.addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix();
  renderer.setPixelRatio(renderRatio());
  renderer.setSize(innerWidth, innerHeight);
  renderer.getDrawingBufferSize(size); target.setSize(size.x, size.y);
  composer.setPixelRatio(renderer.getPixelRatio());
  composer.setSize(innerWidth, innerHeight);
  antialias.uniforms.resolution.value.set(1 / size.x, 1 / size.y);
});

window.__gardenStudy = {
  ready: true,
  getState: () => ({
    raining, pressed, selected, fps: Math.round(fps), elapsed,
    live: Boolean(records), selectedMemoryKey: flowers[selected]?.record?.key ?? null,
    responseMemoryKeys: [...responseFlowerIds].map(id => flowers[id].record.key),
    flowerCount: flowers.length, grassCount: greenery.grass.count, rainCount,
    loadedChunks: ground.active.size, totalChunks: gardenView.chunks.length,
    terrainPool: ground.pool.length, visibleFlowerCount: renderedFlowerIds.size,
    nearbyBudget: NEARBY.flowers, transitionBudget: NEARBY.residentFlowers,
    nearbyRadius: flowerWindow.radius, residentFlowers: flowerWindow.resident.size,
    visibleGrassCount: greenery.grass.visibleCount,
    flowerLevels: petals.getStats().levels.map(count => count / petalsPerFlower),
    visibleTriangles: spatialFields.reduce((sum, field) => sum + field.getStats().triangles, 0),
    flowerCapacity: petals.getStats().capacity / petalsPerFlower,
    renderer: renderer.getContext().getParameter(renderer.getContext().RENDERER),
    zoom, lightPower: beam.uLightPower.value, renderRatio: renderer.getPixelRatio(),
    fixedZoom: false, cameraDistance, cameraBounds,
    focus: { x: focus.x, z: focus.z }, rainAim: { x: rainAim.x, z: rainAim.z },
    lightCount: scene.children.filter(object => object.isLight).length,
    flowers: flowers.map(f => ({ id: f.id, wetness: f.wetness, bloom: f.bloom, glow: f.glow,
      x: f.x, z: f.z, rendered: renderedFlowerIds.has(f.id), presence: flowerWindow.weights[f.id],
      memoryKey: f.record?.key ?? null, kind: f.record?.kind ?? null, archived: Boolean(f.archived) })),
  }),
  flowerScreen: id => flowerScreen(flowers[id]),
  groundScreen: id => {
    const f = flowers[id], p = new THREE.Vector3(f.x, f.y, f.z).project(camera);
    return { x: (p.x + 1) * .5 * innerWidth, y: (1 - p.y) * .5 * innerHeight };
  },
  life: LIFE,
  glow: GLOW,
  tuning,
};

}
