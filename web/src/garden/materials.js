import * as THREE from 'three';

// Fade the local patch with opaque coverage, including its shadow pass. This
// keeps the existing lighting and avoids adding transparent plant layers.
export function nearbyMaterial(material, uniforms, flower = false) {
  const compile = material.onBeforeCompile;
  const cacheKey = material.customProgramCacheKey();
  material.onBeforeCompile = (shader, renderer) => {
    compile.call(material, shader, renderer);
    Object.assign(shader.uniforms, uniforms);
    shader.vertexShader = `varying vec3 vPatchWorld;
      ${flower ? 'attribute float aPresence; varying float vPresence;' : ''}
    ` + shader.vertexShader;
    shader.vertexShader = shader.vertexShader.replace('#include <project_vertex>', `
      #include <project_vertex>
      vec4 gardenVertex = vec4(transformed, 1.0);
      #ifdef USE_INSTANCING
        gardenVertex = instanceMatrix * gardenVertex;
      #endif
      vPatchWorld = (modelMatrix * gardenVertex).xyz;
      ${flower ? 'vPresence = aPresence;' : ''}
    `);
    shader.fragmentShader = `
      uniform vec2 uGardenCenter;
      uniform float uGardenInner;
      uniform float uGardenOuter;
      varying vec3 vPatchWorld;
      ${flower ? 'varying float vPresence;' : ''}
    ` + shader.fragmentShader;
    shader.fragmentShader = shader.fragmentShader.replace('#include <clipping_planes_fragment>', `
      #include <clipping_planes_fragment>
      float gardenCoverage = 1.0 - smoothstep(uGardenInner, uGardenOuter,
        distance(vPatchWorld.xz, uGardenCenter));
      ${flower ? 'gardenCoverage *= vPresence;' : ''}
      float gardenNoise = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(.06711056, .00583715))));
      if (gardenCoverage < .001 || gardenCoverage < gardenNoise) discard;
    `);
  };
  material.customProgramCacheKey = () => `${cacheKey}-nearby-${flower}`;
  return material;
}

export const beamGLSL = `
uniform vec3 uLightOrigin;
uniform vec3 uLightTarget;
uniform float uLightPower;
uniform float uLightAngle;
float beamAt(vec3 p) {
  vec3 axis = normalize(uLightTarget - uLightOrigin);
  vec3 fromLight = p - uLightOrigin;
  float along = dot(fromLight, axis);
  float radius = max(.01, along * tan(uLightAngle));
  float side = length(fromLight - axis * along);
  return (1.0 - smoothstep(radius * .62, radius, side)) * step(0.0, along);
}
`;

export function petalMaterials(beam) {
  const material = new THREE.MeshStandardMaterial({
    color: '#f4ebe2', roughness: .57, metalness: 0,
    side: THREE.DoubleSide, vertexColors: true,
  });
  const depth = new THREE.MeshDepthMaterial({ depthPacking: THREE.RGBADepthPacking, side: THREE.DoubleSide });
  const deform = (shader, shaded) => {
    shader.vertexShader = `
      attribute vec3 aOpenPosition;
      attribute vec3 aOpenNormal;
      attribute float aBloom;
      attribute float aGlow;
      varying vec3 vGardenWorld;
      varying float vBloom;
      varying float vGlow;
      varying vec2 vPetalUv;
    ` + shader.vertexShader;
    shader.vertexShader = shader.vertexShader
      .replace('#include <beginnormal_vertex>', `
        vec3 objectNormal = normalize(mix(normal, aOpenNormal, smoothstep(0.0, 1.0, aBloom)));
      `)
      .replace('#include <begin_vertex>', `
        vec3 transformed = mix(position, aOpenPosition, smoothstep(0.0, 1.0, aBloom));
        vBloom = aBloom; vGlow = aGlow; vPetalUv = uv;
      `)
      .replace('#include <project_vertex>', `
        #include <project_vertex>
        vGardenWorld = (modelMatrix * instanceMatrix * vec4(transformed, 1.0)).xyz;
      `);
    if (shaded) {
      Object.assign(shader.uniforms, beam);
      shader.fragmentShader = beamGLSL + `
        varying vec3 vGardenWorld;
        varying float vBloom;
        varying float vGlow;
        varying vec2 vPetalUv;
      ` + shader.fragmentShader;
      shader.fragmentShader = shader.fragmentShader.replace('#include <emissivemap_fragment>', `
        #include <emissivemap_fragment>
        float edge = pow(1.0 - abs(dot(normal, normalize(vViewPosition))), 2.0);
        float throughPetal = beamAt(vGardenWorld) * uLightPower * smoothstep(.12, .60, vBloom);
        float vein = .94 + .06 * sin(vPetalUv.x * 76.0 + vPetalUv.y * 9.0);
        totalEmissiveRadiance += diffuseColor.rgb * vein * (
          throughPetal * (.42 + .65 * edge) * (.5 + .5 * vPetalUv.y));
        // Whole curved petals carry the glow. A gentle gradient preserves their
        // folds in darkness; it is independent of the rain beam and adds no lamp.
        float cup = sin(vPetalUv.y * 3.14159265);
        float glowShape = (.55 + .48 * cup + .12 * edge) * (.78 + .22 * abs(normal.y));
        vec3 glowColor = mix(diffuseColor.rgb, vec3(1.0, .84, .68), .38);
        totalEmissiveRadiance += glowColor * vGlow * glowShape * vein
          * smoothstep(.01, .12, vBloom);
      `);
    }
  };
  material.onBeforeCompile = shader => deform(shader, true);
  depth.onBeforeCompile = shader => deform(shader, false);
  material.customProgramCacheKey = () => 'garden-petal-glow-v2';
  depth.customProgramCacheKey = () => 'garden-petal-depth-v1';
  return { material, depth };
}

export function flowerAccentMaterial(base, strength) {
  const material = base.clone();
  material.onBeforeCompile = shader => {
    shader.uniforms.uFlowerGlowStrength = { value: strength };
    shader.vertexShader = 'attribute float aGlow; varying float vFlowerGlow;\n' + shader.vertexShader;
    shader.vertexShader = shader.vertexShader.replace('#include <begin_vertex>', `
      #include <begin_vertex>
      vFlowerGlow = aGlow;
    `);
    shader.fragmentShader = 'uniform float uFlowerGlowStrength; varying float vFlowerGlow;\n' + shader.fragmentShader;
    shader.fragmentShader = shader.fragmentShader.replace('#include <emissivemap_fragment>', `
      #include <emissivemap_fragment>
      totalEmissiveRadiance += vec3(.57, .55, .38) * vFlowerGlow * uFlowerGlowStrength;
    `);
  };
  material.customProgramCacheKey = () => `garden-flower-accent-${strength}`;
  return material;
}

export function rainMaterial(beam, time) {
  return new THREE.ShaderMaterial({
    uniforms: { ...beam, uTime: time },
    transparent: true, depthWrite: false, blending: THREE.NormalBlending,
    vertexShader: beamGLSL + `
      uniform float uTime;
      attribute vec4 aSeed;
      varying vec2 vUv;
      varying float vStrength;
      varying float vHeight;
      void main() {
        vUv = uv;
        float speed = 5.5 + aSeed.w * 3.0;
        float y = mod(aSeed.y * 11.0 - uTime * speed, 11.0);
        float focused = step(.36, aSeed.w);
        vec3 axis = normalize(uLightOrigin - uLightTarget);
        vec3 center = uLightTarget + axis * (y / max(.1, axis.y));
        vec3 p = vec3(
          mix(aSeed.x * 25.0 - 12.5 + y * .65 + uLightOrigin.x + 5.4, center.x + (aSeed.x - .5) * 5.2, focused),
          y,
          mix(aSeed.z * 24.0 - 12.0 + uLightOrigin.z + 1.5, center.z + (aSeed.z - .5) * 5.2, focused)
        );
        vec3 fall = normalize(vec3(.62, -1.0, .12));
        vec3 side = normalize(cross(fall, cameraPosition - p));
        float distanceToCamera = length(p - cameraPosition);
        p += side * position.x * (.0034 + distanceToCamera * .00016);
        p += fall * position.y * (.08 + .13 * aSeed.w);
        vStrength = .006 + beamAt(p) * uLightPower * (.30 + .27 * aSeed.x);
        vStrength *= 1.0 - smoothstep(17.0, 27.0, distanceToCamera);
        vHeight = p.y;
        gl_Position = projectionMatrix * viewMatrix * vec4(p, 1.0);
      }
    `,
    fragmentShader: `
      varying vec2 vUv;
      varying float vStrength;
      varying float vHeight;
      void main() {
        float profile = pow(max(0.0, 1.0 - abs(vUv.x * 2.0 - 1.0)), 1.2);
        profile *= smoothstep(0.0, .22, vUv.y) * (1.0 - smoothstep(.60, 1.0, vUv.y));
        if (vHeight < .02) discard;
        gl_FragColor = vec4(vec3(.78, .79, .77), profile * vStrength);
      }
    `,
  });
}

export function atmosphereMaterial(target, camera, beam, time) {
  return new THREE.ShaderMaterial({
    depthTest: false, depthWrite: false,
    uniforms: {
      ...beam, uTime: time,
      tColor: { value: target.texture }, tDepth: { value: target.depthTexture },
      uInverseProjection: { value: camera.projectionMatrixInverse },
      uCameraWorld: { value: camera.matrixWorld },
      uCameraPosition: { value: camera.position },
      uMist: { value: .014 },
    },
    vertexShader: `varying vec2 vUv; void main() { vUv = uv; gl_Position = vec4(position.xy, 0., 1.); }`,
    fragmentShader: beamGLSL + `
      uniform sampler2D tColor;
      uniform sampler2D tDepth;
      uniform mat4 uInverseProjection;
      uniform mat4 uCameraWorld;
      uniform vec3 uCameraPosition;
      uniform float uTime;
      uniform float uMist;
      varying vec2 vUv;
      float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
      void main() {
        vec3 color = texture2D(tColor, vUv).rgb;
        float depth = texture2D(tDepth, vUv).x;
        vec4 view = uInverseProjection * vec4(vUv * 2.0 - 1.0, depth * 2.0 - 1.0, 1.0);
        view /= view.w;
        vec3 world = (uCameraWorld * view).xyz;
        vec3 dir = normalize(world - uCameraPosition);
        float distanceToSurface = min(length(world - uCameraPosition), 24.0);
        float stepLength = distanceToSurface / 20.0;
        float jitter = hash(gl_FragCoord.xy);
        float scattering = 0.0;
        for (int i = 0; i < 20; i++) {
          vec3 p = uCameraPosition + dir * (float(i) + jitter) * stepLength;
          float density = .68 + .20 * sin(p.x * 3.4 + p.z * 1.8 + uTime * .13)
            * sin(p.y * 2.1 - p.z * 2.4 - uTime * .11);
          scattering += beamAt(p) * density * stepLength * step(.025, p.y);
        }
        color += vec3(.77, .79, .79) * scattering * uMist * uLightPower;
        float vignette = smoothstep(.16, .77, length((vUv - vec2(.51, .47)) * vec2(.92, 1.0)));
        color *= 1.0 - vignette * .65;
        float grain = (hash(gl_FragCoord.xy + fract(uTime) * 29.0) - .5) * .0017;
        gl_FragColor = vec4(max(vec3(0.0), color + grain), 1.0);
      }
    `,
  });
}
