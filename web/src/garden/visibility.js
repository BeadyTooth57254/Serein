import * as THREE from 'three';

export const CHUNK_SIZE = 4;
export const NEARBY = {
  flowerRadius: 4.8, flowers: 200, residentFlowers: 216,
  fadeIn: .65, fadeOut: .45, groundInner: 4.8, groundOuter: 6.4,
};
const chunkKey = (x, z) => `${Math.floor(x / CHUNK_SIZE)}:${Math.floor(z / CHUNK_SIZE)}`;

export class GardenView {
  constructor(halfSize = 16) {
    this.chunks = [];
    for (let x = -halfSize; x < halfSize; x += CHUNK_SIZE) {
      for (let z = -halfSize; z < halfSize; z += CHUNK_SIZE) {
        this.chunks.push({ key: chunkKey(x, z), x, z });
      }
    }
    this.visible = [];
    this.version = 0;
    this.tileVersion = 0;
    this.previous = new THREE.Matrix4();
    this.matrix = new THREE.Matrix4();
    this.frustum = new THREE.Frustum();
    this.box = new THREE.Box3();
    this.sphere = new THREE.Sphere();
    this.lightTarget = new THREE.Vector3();
    this.lightAxis = new THREE.Vector3();
    this.signature = '';
    this.center = new THREE.Vector2();
    this.uniforms = {
      uGardenCenter: { value: this.center },
      uGardenInner: { value: NEARBY.groundInner },
      uGardenOuter: { value: NEARBY.groundOuter },
    };
  }

  update(camera, height, lightOrigin, lightTarget, lightAngle) {
    this.matrix.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse);
    const lightMoved = this.lightTarget.distanceToSquared(lightTarget) > .0064 || this.lightAngle !== lightAngle;
    if (this.version && this.previous.equals(this.matrix) && this.height === height && !lightMoved) return false;
    this.previous.copy(this.matrix);
    this.height = height;
    this.camera = camera;
    this.lightOrigin = lightOrigin;
    this.lightTarget.copy(lightTarget);
    this.lightAngle = lightAngle;
    this.lightAxis.subVectors(lightTarget, lightOrigin).normalize();
    this.lightSpread = Math.tan(lightAngle);
    this.pixelsPerUnit = height / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)));
    // A small border preloads the next patch before it reaches the viewport.
    const projection = camera.projectionMatrix.clone();
    projection.elements[0] *= .92; projection.elements[5] *= .92;
    this.frustum.setFromProjectionMatrix(this.matrix.multiplyMatrices(projection, camera.matrixWorldInverse));
    this.visible = this.chunks.filter(chunk => {
      const x0 = chunk.x - .55, z0 = chunk.z - .55;
      const x1 = chunk.x + CHUNK_SIZE + .55, z1 = chunk.z + CHUNK_SIZE + .55;
      const nearX = THREE.MathUtils.clamp(this.center.x, x0, x1);
      const nearZ = THREE.MathUtils.clamp(this.center.y, z0, z1);
      if (Math.hypot(nearX - this.center.x, nearZ - this.center.y) > NEARBY.groundOuter) return false;
      this.box.min.set(x0, -.2, z0);
      this.box.max.set(x1, 1.6, z1);
      // A plant just outside the image may still cast a shadow into it.
      // Include that footprint without loading distant unrelated patches.
      const reach = 1.6 / (lightOrigin.y - 1.6);
      this.box.min.x = Math.min(x0, x0 + (x0 - lightOrigin.x) * reach);
      this.box.max.x = Math.max(x1, x1 + (x1 - lightOrigin.x) * reach);
      this.box.min.z = Math.min(z0, z0 + (z0 - lightOrigin.z) * reach);
      this.box.max.z = Math.max(z1, z1 + (z1 - lightOrigin.z) * reach);
      return this.frustum.intersectsBox(this.box);
    });
    const signature = this.visible.map(chunk => chunk.key).join('|');
    if (signature !== this.signature) { this.signature = signature; this.tileVersion++; }
    this.version++;
    return true;
  }

  includes(x, y, z, radius) {
    if (Math.hypot(x - this.center.x, z - this.center.y) > NEARBY.groundOuter + radius) return false;
    this.sphere.center.set(x, y, z); this.sphere.radius = radius;
    if (this.frustum.intersectsSphere(this.sphere)) return true;
    // Keep offscreen casters whose projected shadow can enter the image.
    const origin = this.lightOrigin;
    const dx = x - origin.x, dy = y - origin.y, dz = z - origin.z;
    const along = dx * this.lightAxis.x + dy * this.lightAxis.y + dz * this.lightAxis.z;
    const side = Math.hypot(dx - this.lightAxis.x * along, dy - this.lightAxis.y * along,
      dz - this.lightAxis.z * along);
    if (along < 0 || side > along * this.lightSpread + radius + .15) return false;
    const scale = (origin.y + .12) / (origin.y - y);
    this.sphere.center.set(origin.x + (x - origin.x) * scale, -.12,
      origin.z + (z - origin.z) * scale);
    this.sphere.radius = radius * scale
      * (1 + Math.hypot(x - origin.x, z - origin.z) / (origin.y - y - radius));
    return this.frustum.intersectsSphere(this.sphere);
  }
}

// Rendering has a small local budget; the source flowers keep living even when
// their GPU rows have faded out. A few spare slots allow smooth handovers.
export class FlowerWindow {
  constructor(flowers) {
    this.flowers = flowers;
    this.weights = new Float32Array(flowers.length);
    this.resident = new Set();
    this.targets = new Map();
    this.radius = NEARBY.flowerRadius;
  }

  update(view, dt) {
    if (this.viewVersion !== view.version) {
      this.viewVersion = view.version;
      const candidates = this.flowers.map(f => ({
        flower: f, distance: Math.hypot(f.x - view.center.x, f.z - view.center.y),
      })).filter(({ flower: f, distance }) => distance < NEARBY.flowerRadius
        && view.includes(f.x, f.y + f.height, f.z, f.scale * .37));
      const score = item => item.distance - (this.resident.has(item.flower.id) ? .12 : 0);
      candidates.sort((a, b) => score(a) - score(b) || a.flower.id - b.flower.id);
      const chosen = candidates.slice(0, NEARBY.flowers);
      this.radius = candidates.length > NEARBY.flowers
        ? Math.min(NEARBY.flowerRadius, Math.max(...chosen.map(item => item.distance)) + .15)
        : NEARBY.flowerRadius;
      this.targets = new Map(chosen.map(({ flower, distance }) => [flower.id,
        1 - THREE.MathUtils.smoothstep(distance, this.radius * .76, this.radius)]));
    }
    let changed = false;
    for (const id of this.resident) {
      const target = this.targets.get(id) ?? 0, value = this.weights[id];
      this.weights[id] = target > value ? Math.min(target, value + dt / NEARBY.fadeIn)
        : Math.max(target, value - dt / NEARBY.fadeOut);
      if (this.weights[id] < .002 && target < .002) {
        this.weights[id] = 0; this.resident.delete(id); changed = true;
      }
    }
    for (const [id, target] of this.targets) {
      if (this.resident.size >= NEARBY.residentFlowers) break;
      if (target < .002 || this.resident.has(id)) continue;
      this.resident.add(id);
      this.weights[id] = Math.min(target, dt / NEARBY.fadeIn);
      changed = true;
    }
    return changed;
  }
}

// Source transforms are lightweight CPU data. Only visible rows are uploaded to
// bounded, reusable GPU batches; draw slots never become flower identities.
export class SpatialInstances extends THREE.Group {
  constructor(geometry, material, count, options = {}) {
    super();
    this.geometry = geometry;
    this.material = material;
    this.count = count;
    this.matrices = new Float32Array(count * 16);
    this.colors = null;
    this.options = options;
    this.templates = [geometry, ...(options.levels ?? [])];
    this.batches = this.templates.map(() => ({ mesh: null, indices: [], capacity: 0 }));
    this.itemSize = options.itemSize ?? 1;
    this.itemLevels = new Uint8Array(Math.ceil(count / this.itemSize));
    this.tiles = null;
    this.layoutDirty = true;
    this.visibleCount = 0;
  }

  setMatrixAt(index, matrix) {
    matrix.toArray(this.matrices, index * 16);
    this.tiles = null;
    this.layoutDirty = true;
  }

  setColorAt(index, color) {
    this.colors ??= new Float32Array(this.count * 3);
    color.toArray(this.colors, index * 3);
    this.layoutDirty = true;
  }

  invalidate() { this.layoutDirty = true; }

  wrapAround(view) {
    const wrap = this.options.wrap;
    if (!wrap) return;
    // Move only distant ground vegetation between repeated patches. The snap
    // stays beyond the fully faded edge, and flowers never use these slots.
    const x = Math.round(view.center.x / CHUNK_SIZE) * CHUNK_SIZE;
    const z = Math.round(view.center.y / CHUNK_SIZE) * CHUNK_SIZE;
    if (this.wrapX === x && this.wrapZ === z) return;
    this.wrapX = x; this.wrapZ = z;
    this.sourceMatrices ??= this.matrices.slice();
    let changed = false;
    for (let i = 0; i < this.count; i++) {
      const offset = i * 16;
      const sx = this.sourceMatrices[offset + 12], sz = this.sourceMatrices[offset + 14];
      const nx = sx + Math.round((x - sx) / wrap.width) * wrap.width;
      const nz = sz + Math.round((z - sz) / wrap.depth) * wrap.depth;
      if (Math.abs(this.matrices[offset + 12] - nx) < .001
        && Math.abs(this.matrices[offset + 14] - nz) < .001) continue;
      this.matrices[offset + 12] = nx;
      this.matrices[offset + 14] = nz;
      this.matrices[offset + 13] = this.sourceMatrices[offset + 13]
        + wrap.height(nx, nz) - wrap.height(sx, sz);
      changed = true;
    }
    if (changed) { this.tiles = null; this.layoutDirty = true; }
  }

  update(view) {
    this.wrapAround(view);
    const version = view.version;
    if (!this.layoutDirty && this.lastVersion === version) return;
    this.lastVersion = version;
    this.layoutDirty = false;
    if (!this.tiles) {
      this.tiles = new Map();
      const bounds = new THREE.Box3().setFromBufferAttribute(this.geometry.attributes.position);
      if (this.geometry.attributes.aOpenPosition) {
        bounds.union(new THREE.Box3().setFromBufferAttribute(this.geometry.attributes.aOpenPosition));
      }
      const localSphere = bounds.getBoundingSphere(new THREE.Sphere());
      const worldSphere = new THREE.Sphere(), matrix = new THREE.Matrix4();
      this.spheres = new Float32Array(this.count * 4);
      for (let i = 0; i < this.count; i++) {
        const key = chunkKey(this.matrices[i * 16 + 12], this.matrices[i * 16 + 14]);
        if (!this.tiles.has(key)) this.tiles.set(key, []);
        this.tiles.get(key).push(i);
        worldSphere.copy(localSphere).applyMatrix4(matrix.fromArray(this.matrices, i * 16));
        worldSphere.center.toArray(this.spheres, i * 4);
        this.spheres[i * 4 + 3] = worldSphere.radius;
      }
    }
    const rows = this.templates.map(() => []);
    const itemLevels = new Map();
    const itemVisibility = new Map();
    for (const chunk of view.visible) for (const index of this.tiles.get(chunk.key) ?? []) {
      const item = Math.floor(index / this.itemSize);
      if (this.options.include && !this.options.include(item)) continue;
      if (!itemVisibility.has(item)) {
        const first = item * this.itemSize;
        // A whole flower shares one visibility decision, including all petals.
        const sphere = first * 4, transform = first * 16;
        const visible = this.options.radius
          ? view.includes(this.matrices[transform + 12], this.matrices[transform + 13],
            this.matrices[transform + 14], this.options.radius(item) * 1.3)
          : view.includes(this.spheres[sphere], this.spheres[sphere + 1],
            this.spheres[sphere + 2], this.spheres[sphere + 3]);
        itemVisibility.set(item, visible);
      }
      if (!itemVisibility.get(item)) continue;
      let level = 0;
      if (rows.length > 1) {
        if (!itemLevels.has(item)) {
          const offset = item * this.itemSize * 16;
          const distance = Math.hypot(this.matrices[offset + 12] - view.camera.position.x,
            this.matrices[offset + 13] - view.camera.position.y, this.matrices[offset + 14] - view.camera.position.z);
          const pixels = this.options.radius(item) * 2 * view.pixelsPerUnit / distance;
          const forced = this.options.forceHigh?.(item);
          let next = forced || pixels >= 60 ? 0 : pixels >= 20 ? 1 : 2;
          const previous = this.itemLevels[item];
          if (!forced && Math.abs(next - previous) === 1) {
            const boundary = [60, 20][Math.min(next, previous)];
            if (next > previous && pixels > boundary * .88) next = previous;
            if (next < previous && pixels < boundary * 1.12) next = previous;
          }
          this.itemLevels[item] = next;
          itemLevels.set(item, next);
        }
        level = itemLevels.get(item);
      }
      rows[level].push(index);
    }
    this.visibleCount = rows.reduce((sum, list) => sum + list.length, 0);
    rows.forEach((indices, level) => this.pack(level, indices));
  }

  pack(level, indices) {
    const batch = this.batches[level];
    if (!indices.length) {
      this.releaseBatch(batch);
      batch.indices = [];
      return;
    }
    const quantum = this.itemSize > 1 ? this.itemSize * 8 : 128;
    const needed = Math.ceil(indices.length * 1.15 / quantum) * quantum;
    if (!batch.mesh || indices.length > batch.capacity || batch.capacity > needed * 2) {
      this.releaseBatch(batch);
      const geometry = new THREE.BufferGeometry();
      geometry.setIndex(this.templates[level].index);
      for (const [name, attribute] of Object.entries(this.templates[level].attributes)) {
        if (!attribute.isInstancedBufferAttribute) geometry.setAttribute(name, attribute);
      }
      for (const [name, attribute] of Object.entries(this.geometry.attributes)) {
        if (attribute.isInstancedBufferAttribute) {
          geometry.setAttribute(name, new THREE.InstancedBufferAttribute(
            new Float32Array(needed * attribute.itemSize), attribute.itemSize).setUsage(THREE.DynamicDrawUsage));
        }
      }
      batch.mesh = new THREE.InstancedMesh(geometry, this.material, needed);
      batch.mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      if (this.colors) batch.mesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(needed * 3), 3);
      batch.mesh.castShadow = this.castShadow;
      batch.mesh.receiveShadow = this.receiveShadow;
      batch.mesh.customDepthMaterial = this.customDepthMaterial;
      // GardenView includes deformed petals, edge padding, and shadow casters.
      batch.mesh.frustumCulled = false;
      batch.capacity = needed;
      this.add(batch.mesh);
    }
    batch.indices = indices;
    batch.mesh.count = indices.length;
    const matrix = batch.mesh.instanceMatrix.array;
    const color = batch.mesh.instanceColor?.array;
    indices.forEach((source, slot) => {
      matrix.set(this.matrices.subarray(source * 16, source * 16 + 16), slot * 16);
      if (color) color.set(this.colors.subarray(source * 3, source * 3 + 3), slot * 3);
    });
    batch.mesh.instanceMatrix.needsUpdate = true;
    if (color) batch.mesh.instanceColor.needsUpdate = true;
  }

  syncAttributes() {
    for (const batch of this.batches) {
      if (!batch.mesh) continue;
      for (const [name, source] of Object.entries(this.geometry.attributes)) {
        if (!source.isInstancedBufferAttribute) continue;
        const target = batch.mesh.geometry.attributes[name];
        const size = source.itemSize;
        batch.indices.forEach((index, slot) => {
          for (let j = 0; j < size; j++) target.array[slot * size + j] = source.array[index * size + j];
        });
        target.needsUpdate = true;
      }
    }
  }

  releaseBatch(batch) {
    if (!batch.mesh) return;
    this.remove(batch.mesh);
    batch.mesh.geometry.dispose();
    batch.mesh.dispose();
    batch.mesh = null;
    batch.capacity = 0;
  }

  getStats() {
    return {
      total: this.count, visible: this.visibleCount,
      capacity: this.batches.reduce((sum, batch) => sum + batch.capacity, 0),
      levels: this.batches.map(batch => batch.indices.length),
      triangles: this.batches.reduce((sum, batch, i) => sum + batch.indices.length
        * (this.templates[i].index?.count ?? this.templates[i].attributes.position.count) / 3, 0),
    };
  }
}

export class StreamedTerrain {
  constructor(scene, material, createGeometry) {
    this.scene = scene;
    this.material = material;
    this.createGeometry = createGeometry;
    this.active = new Map();
    this.pool = [];
  }

  update(view) {
    if (this.version === view.tileVersion) return;
    this.version = view.tileVersion;
    const visible = new Set(view.visible.map(chunk => chunk.key));
    for (const [key, mesh] of this.active) if (!visible.has(key)) {
      this.scene.remove(mesh);
      this.active.delete(key);
      if (this.pool.length < 4) this.pool.push(mesh);
      else mesh.geometry.dispose();
    }
    for (const chunk of view.visible) if (!this.active.has(chunk.key)) {
      const mesh = this.pool.pop() ?? new THREE.Mesh(undefined, this.material);
      mesh.geometry = this.createGeometry(chunk.x + CHUNK_SIZE / 2, chunk.z + CHUNK_SIZE / 2,
        CHUNK_SIZE, 28, mesh.userData.terrain ? mesh.geometry : null);
      mesh.userData.terrain = true;
      mesh.receiveShadow = true;
      this.active.set(chunk.key, mesh);
      this.scene.add(mesh);
    }
  }
}
