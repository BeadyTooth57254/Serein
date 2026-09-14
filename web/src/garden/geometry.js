import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { SpatialInstances } from './visibility.js';

export function random(seed = 7092026) {
  return () => {
    seed |= 0; seed = seed + 0x6D2B79F5 | 0;
    let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}

export function groundHeight(x, z) {
  return .055 * Math.sin(x * 1.4 + z * .7)
    + .034 * Math.cos(z * 2.3 - x * .6)
    + .018 * Math.sin(x * 5.1) * Math.cos(z * 4.2)
    + .007 * Math.sin(x * 18.1 + z * 13.3);
}

function hash(x, y) {
  const a = Math.sin(x * 127.1 + y * 311.7) * 43758.5453;
  return a - Math.floor(a);
}
function noise(x, y) {
  const ix = Math.floor(x), iy = Math.floor(y);
  let fx = x - ix, fy = y - iy;
  fx = fx * fx * (3 - 2 * fx); fy = fy * fy * (3 - 2 * fy);
  return THREE.MathUtils.lerp(
    THREE.MathUtils.lerp(hash(ix, iy), hash(ix + 1, iy), fx),
    THREE.MathUtils.lerp(hash(ix, iy + 1), hash(ix + 1, iy + 1), fx), fy);
}

// Small-scale mud texture is generated locally. No photographed scene or flower sprites.
export function mudTextures() {
  const size = 512, height = new Float32Array(size * size);
  const rgb = new Uint8Array(size * size * 4), normal = rgb.slice(), rough = rgb.slice();
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    height[y * size + x] = .40 * noise(x / 37, y / 37)
      + .26 * noise(x / 11, y / 11) + .19 * noise(x / 3, y / 3) + .15 * hash(x, y);
  }
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const idx = y * size + x, p = idx * 4, n = height[idx];
    const grit = hash(x + 5, y + 9) > .97 ? 13 : 0;
    rgb[p] = 33 + n * 37 + grit; rgb[p + 1] = 32 + n * 31 + grit;
    rgb[p + 2] = 27 + n * 28 + grit; rgb[p + 3] = 255;
    const dx = height[y * size + (x + 1) % size] - height[y * size + (x + size - 1) % size];
    const dy = height[((y + 1) % size) * size + x] - height[((y + size - 1) % size) * size + x];
    const v = new THREE.Vector3(-dx * 3, -dy * 3, 1).normalize();
    normal[p] = (v.x * .5 + .5) * 255; normal[p + 1] = (v.y * .5 + .5) * 255;
    normal[p + 2] = (v.z * .5 + .5) * 255; normal[p + 3] = 255;
    const r = 58 + n * 137;
    rough[p] = r; rough[p + 1] = r; rough[p + 2] = r; rough[p + 3] = 255;
  }
  const texture = (array, color = false) => {
    const t = new THREE.DataTexture(array, size, size, THREE.RGBAFormat);
    t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(10, 10);
    t.magFilter = THREE.LinearFilter; t.minFilter = THREE.LinearMipmapLinearFilter;
    t.generateMipmaps = true; t.anisotropy = 4;
    if (color) t.colorSpace = THREE.SRGBColorSpace;
    t.needsUpdate = true;
    return t;
  };
  return { map: texture(rgb, true), normalMap: texture(normal), roughnessMap: texture(rough) };
}

export function terrainGeometry(cx = 0, cz = 0, size = 26, segments = 180, existing = null) {
  const geo = existing ?? new THREE.PlaneGeometry(size, size, segments, segments).rotateX(-Math.PI / 2);
  const { position: p, normal, uv } = geo.attributes;
  const step = size / segments, n = new THREE.Vector3();
  for (let i = 0; i < p.count; i++) {
    const x = cx - size / 2 + i % (segments + 1) * step;
    const z = cz - size / 2 + Math.floor(i / (segments + 1)) * step;
    p.setXYZ(i, x, groundHeight(x, z), z);
    // World-space UVs and height derivatives keep adjoining patches seamless.
    uv.setXY(i, (x + 13) / 26, (13 - z) / 26);
    n.set(groundHeight(x - step, z) - groundHeight(x + step, z), 2 * step,
      groundHeight(x, z - step) - groundHeight(x, z + step)).normalize();
    normal.setXYZ(i, n.x, n.y, n.z);
  }
  p.needsUpdate = normal.needsUpdate = uv.needsUpdate = true;
  geo.computeBoundingSphere();
  return geo;
}

function surfaceGeometry(sample, lengthSteps = 8, widthSteps = 4) {
  const p = [], uv = [], index = [];
  for (let i = 0; i <= lengthSteps; i++) for (let j = 0; j <= widthSteps; j++) {
    p.push(...sample(i / lengthSteps, j / widthSteps * 2 - 1));
    uv.push(j / widthSteps, i / lengthSteps);
  }
  for (let i = 0; i < lengthSteps; i++) for (let j = 0; j < widthSteps; j++) {
    const a = i * (widthSteps + 1) + j, b = a + widthSteps + 1;
    index.push(a, b, a + 1, b, b + 1, a + 1);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(p, 3));
  geo.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
  geo.setIndex(index); geo.computeVertexNormals();
  return geo;
}

export function petalGeometry(lengthSteps = 10, widthSteps = 6) {
  const closed = surfaceGeometry((s, t) => {
    const width = Math.pow(Math.sin(Math.PI * s), .7) * .054 + .001;
    return [t * width, s * .28 + t * t * .012, .018 + Math.sin(Math.PI * s) * .062 + t * t * .014];
  }, lengthSteps, widthSteps);
  const open = surfaceGeometry((s, t) => {
    const width = Math.pow(Math.sin(Math.PI * s), .55) * .099 + .001;
    return [t * width, .008 + .205 * s * s + .044 * t * t * Math.sin(Math.PI * s), .019 + s * .255];
  }, lengthSteps, widthSteps);
  closed.setAttribute('aOpenPosition', open.attributes.position.clone());
  closed.setAttribute('aOpenNormal', open.attributes.normal.clone());
  const colors = [];
  for (let i = 0; i < closed.attributes.position.count; i++) {
    const s = closed.attributes.uv.getY(i);
    const c = new THREE.Color('#ae727e').lerp(new THREE.Color('#f5e7d5'), Math.pow(s, .7));
    colors.push(c.r, c.g, c.b);
  }
  closed.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
  open.dispose();
  return closed;
}

export function leafGeometry() {
  return surfaceGeometry((s, t) => [
    t * .045 * Math.pow(Math.sin(Math.PI * s), .85),
    .09 * Math.sin(s * Math.PI) + .013 * Math.abs(t),
    s * .31,
  ], 5, 2);
}

export function bladeGeometry() {
  const geo = surfaceGeometry((s, t) => [
    t * .022 * (1 - Math.pow(s, 1.35)) + .018 * s * s,
    .36 * (s - .16 * s * s),
    .17 * s * s,
  ], 3, 1);
  tintPlant(geo, '#73796b', '#c1c8b1');
  return geo;
}

function tintPlant(geo, root, tip) {
  const colors = [], dark = new THREE.Color(root), light = new THREE.Color(tip);
  for (let i = 0; i < geo.attributes.position.count; i++) {
    const c = dark.clone().lerp(light, geo.attributes.uv.getY(i));
    colors.push(c.r, c.g, c.b);
  }
  geo.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
}

function groundLeafGeometry() {
  const geo = surfaceGeometry((s, t) => [
    t * .022 * Math.pow(Math.sin(Math.PI * s), .8),
    .030 * Math.sin(Math.PI * s) + .008 * Math.abs(t) * Math.sin(Math.PI * s),
    s * .14,
  ], 3, 2);
  tintPlant(geo, '#59654a', '#a7b590');
  return geo;
}

function weedGeometry() {
  const stalk = new THREE.CylinderGeometry(.0017, .003, .34, 3, 1, true);
  stalk.translate(0, .17, 0);
  tintPlant(stalk, '#747656', '#b0b597');
  const parts = [stalk];
  for (let j = 0; j < 4; j++) {
    const leaf = surfaceGeometry((s, t) => [
      t * .018 * Math.sin(Math.PI * s),
      .055 * s + .018 * Math.sin(Math.PI * s),
      .11 * s,
    ], 3, 1);
    tintPlant(leaf, '#5c694b', '#acba91');
    leaf.rotateY(j * 2.4);
    leaf.translate(0, .075 + j * .060, 0);
    parts.push(leaf);
  }
  const geo = mergeGeometries(parts);
  parts.forEach(part => part.dispose());
  return geo;
}

export function stemGeometry() {
  return new THREE.TubeGeometry(new THREE.CatmullRomCurve3([
    new THREE.Vector3(0, 0, 0), new THREE.Vector3(.015, .33, 0),
    new THREE.Vector3(-.012, .67, .014), new THREE.Vector3(0, 1, 0),
  ]), 8, .009, 5, false);
}

export function makeFlowers(total = 253) {
  const rng = random(914), flowers = [];
  // This study has no Scene records or fabricated relation edges.
  const add = (x, z, hero = false) => {
    const height = hero ? .93 : .39 + rng() * .33;
    flowers.push({
      id: flowers.length, x, z, y: groundHeight(x, z), height,
      scale: hero ? 1.04 : .28 + rng() * .23,
      rotation: rng() * Math.PI * 2, pink: rng(),
      wetness: hero ? .64 : 0, bloom: hero ? .68 : 0,
      afterUser: hero ? 4.5 : 0, glow: 0, hero,
    });
  };
  add(.25, 1.0, true);
  for (let i = 0; i < 235; i++) {
    const x = (rng() - .5) * 17, z = (rng() - .5) * 20 - 2;
    if (Math.hypot(x - .25, z - 1) < .55) continue;
    add(x, z);
  }
  // A small loose cluster around the first patch of rain, with the same lifecycle.
  for (let i = 0; i < 17; i++) {
    const a = rng() * Math.PI * 2, radius = .50 + rng() * 1.5;
    const x = .25 + Math.cos(a) * radius, z = 1 + Math.sin(a) * radius;
    add(x, z);
  }
  // Preserve the approved study exactly. More records extend the garden,
  // rather than increasing the density of its first patch. Outward rings and
  // a fixed seed keep every existing position stable when the total grows.
  const cellSize = 1.25;
  const addCell = (cx, cz) => {
    if (flowers.length >= total) return;
    const x = (cx + (rng() - .5) * .72) * cellSize;
    const z = (cz + (rng() - .5) * .72) * cellSize - 2;
    if (Math.abs(x) < 9 && z > -12.5 && z < 8.5) return;
    if (flowers.some(f => Math.hypot(f.x - x, f.z - z) < .65)) return;
    add(x, z);
  };
  for (let ring = 1; flowers.length < total; ring++) {
    for (let x = -ring; x <= ring; x++) { addCell(x, -ring); addCell(x, ring); }
    for (let z = -ring + 1; z < ring; z++) { addCell(-ring, z); addCell(ring, z); }
  }
  return flowers;
}

export function vegetation(scene, material, flowerData, detail = 1) {
  const rng = random(2313), temp = new THREE.Object3D();
  const turfMaterial = material.clone();
  turfMaterial.vertexColors = true;
  turfMaterial.roughness = .38;
  turfMaterial.color.set('#b4bbac');
  // Reallocate the original 53,500 ground plants. Curved six-triangle blades
  // replace most broad leaves; the whole ground layer uses fewer triangles.
  const count = Math.round(48000 * detail);
  const grass = new SpatialInstances(bladeGeometry(), turfMaterial, count);
  let tuftX, tuftZ, tuftSize, tuftAngle;
  for (let i = 0; i < count; i++) {
    if (i % 8 === 0) {
      // The 12 m study patch is dense; the surrounding margin thins gradually.
      tuftX = (rng() + rng() - 1) * 9; tuftZ = (rng() + rng() - 1) * 10 - 1;
      tuftSize = .55 + noise(tuftX * .8 + 12, tuftZ * .8 + 8) * .70;
      tuftAngle = rng() * Math.PI * 2;
    }
    const angle = tuftAngle + i % 8 * 2.4 + rng() * .45;
    const spread = .02 + rng() * .10;
    const x = tuftX + Math.cos(angle) * spread, z = tuftZ + Math.sin(angle) * spread;
    // Slightly thinner patches leave wet soil visible between the grass roots.
    const gap = THREE.MathUtils.smoothstep(noise(x * 1.3 + 30, z * 1.3 + 20), .56, .82);
    const size = (.55 + rng() * .57) * tuftSize * (1 - gap * .62);
    temp.position.set(x, groundHeight(x, z) - .008, z);
    temp.rotation.set((rng() - .5) * .30, angle, (rng() - .5) * .36);
    temp.scale.set(.50 + rng() * .55, size, .55 + rng() * .85); temp.updateMatrix();
    grass.setMatrixAt(i, temp.matrix);
    const dry = rng() < .07;
    grass.setColorAt(i, new THREE.Color().setHSL(dry ? .13 : .20 + rng() * .045,
      dry ? .18 : .18 + rng() * .14, .23 + rng() * .15));
  }
  grass.castShadow = true; grass.receiveShadow = true;
  scene.add(grass);

  const leafCount = Math.round(4200 * detail);
  const leaves = new SpatialInstances(groundLeafGeometry(), turfMaterial, leafCount);
  let rosetteX, rosetteZ, rosetteSize, rosetteAngle;
  for (let i = 0; i < leafCount; i++) {
    if (i % 6 === 0) {
      rosetteX = (rng() - .5) * 18; rosetteZ = (rng() - .5) * 20 - 1;
      rosetteSize = .50 + rng() * .65; rosetteAngle = rng() * Math.PI * 2;
    }
    temp.position.set(rosetteX, groundHeight(rosetteX, rosetteZ) - .004, rosetteZ);
    temp.rotation.set(-rng() * .35, rosetteAngle + i % 6 * 2.4, (rng() - .5) * .2);
    const size = rosetteSize * (.7 + rng() * .5);
    temp.scale.set(size, size, size); temp.updateMatrix();
    leaves.setMatrixAt(i, temp.matrix);
    leaves.setColorAt(i, new THREE.Color().setHSL(.21 + rng() * .04, .24 + rng() * .15, .25 + rng() * .12));
  }
  leaves.castShadow = true; leaves.receiveShadow = true; scene.add(leaves);

  const weedCount = Math.round(1300 * detail);
  const weeds = new SpatialInstances(weedGeometry(), turfMaterial, weedCount);
  for (let i = 0; i < weedCount; i++) {
    const x = (rng() - .5) * 18, z = (rng() - .5) * 20 - 1;
    temp.position.set(x, groundHeight(x, z) - .010, z);
    temp.rotation.set((rng() - .5) * .5, rng() * Math.PI * 2, (rng() - .5) * .5);
    const size = .45 + rng() * .55;
    temp.scale.set(size, size, size); temp.updateMatrix(); weeds.setMatrixAt(i, temp.matrix);
    weeds.setColorAt(i, new THREE.Color().setHSL(.20 + rng() * .045, .24, .28 + rng() * .12));
  }
  weeds.castShadow = weeds.receiveShadow = true; scene.add(weeds);

  const stems = new SpatialInstances(stemGeometry(), material, flowerData.length);
  const stemLeaves = new SpatialInstances(leafGeometry(), material, flowerData.length * 3);
  flowerData.forEach((f, i) => {
    temp.position.set(f.x, f.y - .014, f.z); temp.rotation.set(0, f.rotation, 0);
    temp.scale.set(f.hero ? .9 : .6, f.height, f.hero ? .9 : .6); temp.updateMatrix();
    stems.setMatrixAt(i, temp.matrix);
    stems.setColorAt(i, new THREE.Color('#63664a'));
    for (let j = 0; j < 3; j++) {
      temp.position.set(f.x, f.y + f.height * (.24 + j * .17), f.z);
      temp.rotation.set(-.45 - rng() * .3, f.rotation + j * 2.4, .2);
      const s = f.hero ? .9 : .55;
      temp.scale.set(s * .7, s, s); temp.updateMatrix();
      stemLeaves.setMatrixAt(i * 3 + j, temp.matrix);
      stemLeaves.setColorAt(i * 3 + j, new THREE.Color('#59644b'));
    }
  });
  stems.castShadow = stems.receiveShadow = true;
  stemLeaves.castShadow = stemLeaves.receiveShadow = true;
  scene.add(stems, stemLeaves);
  return { grass, leaves, weeds, stems, stemLeaves };
}
